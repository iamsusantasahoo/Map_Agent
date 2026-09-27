"""Telegram lead-research bot.

Type a natural-language search such as "dental clinics in Andheri Mumbai".
The bot asks how many results you want, fetches businesses from Google Places,
enriches them from their websites, and returns contact details, social links,
and ready-to-send message drafts. Outreach itself is done manually from the
user's own phone.
"""
from __future__ import annotations

import asyncio
import logging
import re

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .ai import PROVIDERS, MessageDrafter
from .config import settings
from .db import Database
from .enrich import enrich_many
from .formatter import format_draft, format_page, format_place
from .places import Place, PlacesClient, PlacesError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

db = Database(settings.db_path, settings.cache_days)
places_client = PlacesClient(settings.places_api_key, 20)
drafter = MessageDrafter(settings.agency_name, settings.anthropic_model, settings.gemini_model)

dp = Dispatcher()

HELP = (
    "Send me a search like:\n"
    "• <code>dental clinics in Andheri Mumbai</code>\n"
    "• <code>cafes in Bandra West</code>\n"
    "• <code>gyms near Koramangala Bangalore</code>\n\n"
    "I return contact details, website, social links and emails for each business, "
    "with one-tap WhatsApp links.\n\n"
    "<b>Results</b>\n"
    f"/count N – how many businesses per search (1–{settings.max_result_count})\n"
    "/more – load the next batch of the same search\n"
    "<code>save N</code> – save lead number N\n"
    "/saved – show saved leads\n"
    "/history – your recent searches\n\n"
    "<b>Message drafts</b>\n"
    "/drafts – write outreach messages for the current results\n"
    "/setkey anthropic YOUR_KEY – use your own Claude key\n"
    "/setkey gemini YOUR_KEY – use your own Gemini key\n"
    "/provider anthropic | gemini | template – choose who writes drafts\n"
    "/settings – show your current settings\n"
    "/help – this message"
)

COUNT_PROMPT = (
    "How many businesses do you want for <b>{query}</b>?\n"
    f"Reply with a number from 1 to {settings.max_result_count}. "
    "Tip: 10–20 is a good batch to work through in one sitting."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def allowed(user_id: int) -> bool:
    return not settings.allowed_user_ids or user_id in settings.allowed_user_ids


def resolve_provider(user_id: int) -> tuple[str, str]:
    """Pick the AI provider and key for this user: own key first, then server key, then template."""
    prefs = db.get_settings(user_id)
    choice = prefs["provider"] or settings.default_provider

    def key_for(provider: str) -> str:
        if provider == "anthropic":
            return prefs["anthropic_key"] or settings.anthropic_api_key
        if provider == "gemini":
            return prefs["gemini_key"] or settings.gemini_api_key
        return ""

    if choice in ("anthropic", "gemini"):
        key = key_for(choice)
        return (choice, key) if key else ("template", "")
    if choice == "template":
        return "template", ""
    # auto: whichever key exists
    for provider in ("anthropic", "gemini"):
        key = key_for(provider)
        if key:
            return provider, key
    return "template", ""


def mask(key: str) -> str:
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else ("set" if key else "not set")


def page_keyboard(has_more: bool) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text="✍️ Draft messages", callback_data="drafts")]
    if has_more:
        row.append(InlineKeyboardButton(text="➡️ More results", callback_data="more"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


# ---------------------------------------------------------------------------
# Core search flow
# ---------------------------------------------------------------------------

async def fetch_and_enrich(query: str, count: int, page_token: str | None) -> tuple[list[Place], str | None]:
    """Fetch up to `count` businesses, paging through Google as needed, and enrich new ones."""
    collected: list[Place] = []
    token = page_token
    while len(collected) < count:
        page = await places_client.search_text(query, token)
        collected.extend(page.places)
        token = page.next_page_token
        if not token or not page.places:
            break
    collected = collected[:count]

    fresh: list[Place] = []
    merged: list[Place] = []
    for p in collected:
        cached = db.get_place(p.place_id)
        if cached and cached.enriched:
            merged.append(cached)
        else:
            fresh.append(p)
            merged.append(p)
    if fresh:
        await enrich_many(fresh)
        db.upsert_places(fresh)
    return merged, token


async def send_results(message: Message, user_id: int, query: str, count: int,
                       page_token: str | None, is_new: bool) -> None:
    status = await message.answer(f"🔎 Searching Google Maps for up to {count} businesses and checking websites…")
    try:
        places, next_token = await fetch_and_enrich(query, count, page_token)
    except PlacesError as exc:
        await status.edit_text(f"❌ {exc}")
        return
    except Exception as exc:
        log.exception("search failed")
        await status.edit_text(f"❌ Something went wrong: {exc}")
        return

    if not places:
        await status.edit_text("No businesses found. Try a broader area or a different niche.")
        return

    session = db.get_session(user_id) if not is_new else {}
    all_ids: list[str] = session.get("place_ids", []) if not is_new else []
    start_index = len(all_ids) + 1
    all_ids.extend(p.place_id for p in places)

    db.set_session(
        user_id,
        {
            "query": query,
            "count": count,
            "place_ids": all_ids,
            "next_token": next_token,
            "current_ids": [p.place_id for p in places],
            "current_start": start_index,
        },
    )
    if is_new:
        db.log_search(user_id, query, [p.place_id for p in places])

    await status.delete()

    per = settings.results_per_message
    for i in range(0, len(places), per):
        chunk = places[i : i + per]
        text = format_page(query, chunk, start_index + i, len(all_ids))
        last = i + per >= len(places)
        await message.answer(
            text,
            reply_markup=page_keyboard(bool(next_token)) if last else None,
            disable_web_page_preview=True,
        )


async def start_search(message: Message, user_id: int, query: str) -> None:
    """Run the search if the user has a count, otherwise ask for one and remember the query."""
    count = db.get_settings(user_id)["result_count"]
    if not count:
        session = db.get_session(user_id)
        session["pending_query"] = query
        db.set_session(user_id, session)
        await message.answer(COUNT_PROMPT.format(query=query))
        return
    await send_results(message, user_id, query, count, None, is_new=True)


async def load_more(message: Message, user_id: int) -> None:
    session = db.get_session(user_id)
    if not session.get("query"):
        await message.answer("Start with a search first.")
        return
    token = session.get("next_token")
    if not token:
        await message.answer("No more results for this search.")
        return
    count = session.get("count") or db.get_settings(user_id)["result_count"] or 10
    await send_results(message, user_id, session["query"], count, token, is_new=False)


async def send_drafts(message: Message, user_id: int) -> None:
    session = db.get_session(user_id)
    ids = session.get("current_ids", [])
    if not ids:
        await message.answer("Start with a search first.")
        return
    places = db.get_places(ids)
    start = session.get("current_start", 1)
    provider, key = resolve_provider(user_id)
    label = {"anthropic": "Claude", "gemini": "Gemini", "template": "built-in templates"}[provider]
    note = ""
    if provider == "gemini" and len(places) > 5:
        note = "\nFree Gemini keys allow 5 messages per minute, so this may take a couple of minutes."
    status = await message.answer(f"✍️ Writing {len(places)} messages with {label}…{note}")

    last_edit = 0.0

    async def progress(done: int, total: int) -> None:
        nonlocal last_edit
        now = asyncio.get_event_loop().time()
        if done < total and now - last_edit < 4:
            return
        last_edit = now
        await status.edit_text(f"✍️ Writing messages with {label}… {done}/{total} done{note}")

    drafts = await drafter.draft_all(places, provider, key, on_progress=progress)
    await status.delete()

    per = settings.results_per_message
    for i in range(0, len(places), per):
        chunk = list(zip(places[i : i + per], drafts[i : i + per]))
        text = (
            "✍️ <b>Message drafts</b>\nTap a link to open WhatsApp with the text pre-filled.\n\n"
            + "\n\n".join(format_draft(start + i + j, p, d) for j, (p, d) in enumerate(chunk))
        )
        await message.answer(text, disable_web_page_preview=True)
    if drafter.notice:
        await message.answer("⚠️ " + drafter.notice)
    elif provider == "template":
        await message.answer(
            "These are template drafts. For personalised AI drafts, add a key with "
            "<code>/setkey gemini YOUR_KEY</code> or <code>/setkey anthropic YOUR_KEY</code>."
        )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not allowed(message.from_user.id):
        await message.answer("This bot is private.")
        return
    await message.answer("👋 Lead research bot ready.\n\n" + HELP)


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@dp.message(Command("count"))
async def cmd_count(message: Message, command: CommandObject) -> None:
    if not allowed(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        current = db.get_settings(message.from_user.id)["result_count"]
        await message.answer(
            f"Current: <b>{current or 'not set'}</b> businesses per search.\n"
            f"Use <code>/count 15</code> to change it (1–{settings.max_result_count})."
        )
        return
    await set_count(message, message.from_user.id, int(arg))


async def set_count(message: Message, user_id: int, n: int) -> None:
    n = max(1, min(n, settings.max_result_count))
    db.set_setting(user_id, "result_count", n)
    session = db.get_session(user_id)
    pending = session.pop("pending_query", None)
    db.set_session(user_id, session)
    if pending:
        await message.answer(f"✅ Set to {n} per search. Running your search…")
        await send_results(message, user_id, pending, n, None, is_new=True)
    else:
        await message.answer(f"✅ I will return {n} businesses per search. Change any time with /count.")


@dp.message(Command("setkey"))
async def cmd_setkey(message: Message, command: CommandObject) -> None:
    if not allowed(message.from_user.id):
        return
    parts = (command.args or "").split()
    if len(parts) != 2 or parts[0].lower() not in ("anthropic", "gemini"):
        await message.answer(
            "Usage:\n<code>/setkey gemini YOUR_KEY</code>\n<code>/setkey anthropic YOUR_KEY</code>\n"
            "Send <code>/setkey gemini clear</code> to remove a key."
        )
        return
    provider, key = parts[0].lower(), parts[1].strip()
    field = f"{provider}_key"

    # Remove the message so the key does not sit in the chat history
    try:
        await message.delete()
    except Exception:
        pass

    if key.lower() == "clear":
        db.set_setting(message.from_user.id, field, "")
        await message.answer(f"🗑 Removed your {provider} key.")
        return

    status = await message.answer(f"🔑 Checking your {provider} key…")
    error = await drafter.verify_key(provider, key)
    if error:
        await status.edit_text(f"❌ That {provider} key did not work:\n<code>{error}</code>")
        return
    db.set_setting(message.from_user.id, field, key)
    db.set_setting(message.from_user.id, "provider", provider)
    await status.edit_text(
        f"✅ {provider.title()} key saved ({mask(key)}) and set as your draft provider. "
        "Your original message was deleted for safety."
    )


@dp.message(Command("provider"))
async def cmd_provider(message: Message, command: CommandObject) -> None:
    if not allowed(message.from_user.id):
        return
    choice = (command.args or "").strip().lower()
    if choice not in PROVIDERS:
        provider, _ = resolve_provider(message.from_user.id)
        await message.answer(
            f"Drafts are currently written by <b>{provider}</b>.\n"
            "Choose with <code>/provider anthropic</code>, <code>/provider gemini</code> "
            "or <code>/provider template</code>."
        )
        return
    db.set_setting(message.from_user.id, "provider", choice)
    provider, key = resolve_provider(message.from_user.id)
    if choice != "template" and provider == "template":
        await message.answer(
            f"Saved, but no {choice} key is available yet. Add one with "
            f"<code>/setkey {choice} YOUR_KEY</code>. Templates will be used until then."
        )
    else:
        await message.answer(f"✅ Drafts will be written by <b>{choice}</b>.")


@dp.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    if not allowed(message.from_user.id):
        return
    prefs = db.get_settings(message.from_user.id)
    provider, _ = resolve_provider(message.from_user.id)
    await message.answer(
        "⚙️ <b>Your settings</b>\n"
        f"Results per search: {prefs['result_count'] or 'not set'}\n"
        f"Draft provider in use: {provider}\n"
        f"Your Anthropic key: {mask(prefs['anthropic_key'])}\n"
        f"Your Gemini key: {mask(prefs['gemini_key'])}\n"
        f"Server Anthropic key: {'available' if settings.anthropic_api_key else 'none'}\n"
        f"Server Gemini key: {'available' if settings.gemini_api_key else 'none'}"
    )


@dp.message(Command("more"))
async def cmd_more(message: Message) -> None:
    if allowed(message.from_user.id):
        await load_more(message, message.from_user.id)


@dp.message(Command("drafts"))
async def cmd_drafts(message: Message) -> None:
    if allowed(message.from_user.id):
        await send_drafts(message, message.from_user.id)


@dp.message(Command("saved"))
async def cmd_saved(message: Message) -> None:
    if not allowed(message.from_user.id):
        return
    leads = db.saved_leads(message.from_user.id)
    if not leads:
        await message.answer("No saved leads yet. Reply <code>save N</code> after a search.")
        return
    per = settings.results_per_message
    for i in range(0, len(leads), per):
        chunk = leads[i : i + per]
        text = "💾 <b>Saved leads</b>\n\n" + "\n\n".join(
            format_place(i + j + 1, p) for j, p in enumerate(chunk)
        )
        await message.answer(text, disable_web_page_preview=True)


@dp.message(Command("history"))
async def cmd_history(message: Message) -> None:
    if not allowed(message.from_user.id):
        return
    rows = db.recent_searches(message.from_user.id)
    if not rows:
        await message.answer("No searches yet.")
        return
    lines = ["🕘 <b>Recent searches</b>"] + [f"• {r['query']}" for r in rows]
    lines.append("\nSend any of them again to reload from cache.")
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# Plain-text messages
# ---------------------------------------------------------------------------

@dp.message(F.text.regexp(r"(?i)^save\s+(\d+)$"))
async def on_save(message: Message) -> None:
    if not allowed(message.from_user.id):
        return
    n = int(re.match(r"(?i)^save\s+(\d+)$", message.text).group(1))
    ids = db.get_session(message.from_user.id).get("place_ids", [])
    if not ids or n < 1 or n > len(ids):
        await message.answer("That number is not in your current results.")
        return
    place = db.get_place(ids[n - 1])
    if not place:
        await message.answer("That lead has expired from the cache. Search again.")
        return
    db.save_lead(message.from_user.id, place.place_id)
    await message.answer(f"💾 Saved <b>{place.name}</b>. See /saved.")


@dp.message(F.text.regexp(r"^\s*\d{1,3}\s*$"))
async def on_number(message: Message) -> None:
    """A bare number answers the 'how many results?' question, or sets the count."""
    if not allowed(message.from_user.id):
        return
    await set_count(message, message.from_user.id, int(message.text.strip()))


@dp.message(F.text & ~F.text.startswith("/"))
async def on_search(message: Message) -> None:
    if not allowed(message.from_user.id):
        await message.answer("This bot is private.")
        return
    query = message.text.strip()
    if len(query) < 3:
        await message.answer("Type a niche and a location, for example: cafes in Bandra")
        return
    await start_search(message, message.from_user.id, query)


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------

@dp.callback_query(F.data == "more")
async def cb_more(callback: CallbackQuery) -> None:
    await callback.answer()
    if allowed(callback.from_user.id):
        await load_more(callback.message, callback.from_user.id)


@dp.callback_query(F.data == "drafts")
async def cb_drafts(callback: CallbackQuery) -> None:
    await callback.answer()
    if allowed(callback.from_user.id):
        await send_drafts(callback.message, callback.from_user.id)


# ---------------------------------------------------------------------------

async def main() -> None:
    settings.validate()
    bot = Bot(settings.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    log.info("Bot starting. Server AI keys: anthropic=%s gemini=%s",
             bool(settings.anthropic_api_key), bool(settings.gemini_api_key))
    try:
        await dp.start_polling(bot)
    finally:
        await places_client.aclose()
        await drafter.aclose()


if __name__ == "__main__":
    asyncio.run(main())
