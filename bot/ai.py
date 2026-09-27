"""Cold-message drafting with Anthropic (Claude), Google Gemini, or templates.

Provider and API key are chosen per user. Order of preference:
  1. the user's own key set with /setkey inside Telegram
  2. the server-wide key from .env
  3. the built-in template (no AI cost)
"""
from __future__ import annotations

import asyncio
import logging
import random
import re

import httpx

from .places import Place

log = logging.getLogger(__name__)

PROVIDERS = ("anthropic", "gemini", "template")

SYSTEM_PROMPT = """You write short WhatsApp outreach messages for a social media and website agency.

Rules:
- 3 to 4 sentences, under 70 words. Plain text, no emojis, no subject line, no bullet points, no line breaks.
- Open with a greeting that uses the business name, then ONE specific observation taken from the data.
  Pick the strongest gap, in this order of priority:
    1. no website at all
    2. website listed but not reachable (dead or broken site)
    3. website has no Instagram link
    4. many reviews but no social presence found
    5. only if none of the above apply: strong rating that could bring more customers with content
- Offer one clear benefit tied to that gap: a modern website, Instagram content, or Google visibility.
- End with a soft question, like asking if they are open to a quick 10 minute chat this week.
- Sign off with the agency name in the first sentence ("this is <agency>") so the reader knows who is writing.
- Emails and phone numbers in the data are for the agency's own reference. Never mention email,
  phone, or "contact details" in the message, and never pitch fixing them.
- Never invent facts not present in the data. Do not include placeholders like [Name].
- Return only the message text, as a single paragraph."""

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Free-tier Gemini allows only a handful of requests per minute. Keep parallel
# calls low and retry on 429/503 with backoff instead of failing to templates.
MAX_PARALLEL = 3
MAX_ATTEMPTS = 3
RETRY_STATUSES = {429, 500, 503}
MAX_RETRY_WAIT = 65.0

_RETRY_IN = re.compile(r"retry in ([0-9.]+)s", re.I)


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    """Use the server's own wait hint (Retry-After header or 'retry in Ns' text), else back off."""
    header = resp.headers.get("retry-after")
    if header and header.isdigit():
        return min(float(header) + 1, MAX_RETRY_WAIT)
    m = _RETRY_IN.search(resp.text)
    if m:
        return min(float(m.group(1)) + 1, MAX_RETRY_WAIT)
    return (2 ** attempt) + random.uniform(0, 1)


def template_draft(place: Place, agency: str) -> str:
    niche = (place.category or "business").lower()
    if not place.website:
        hook = ("I noticed you do not have a website listed on Google yet, which means customers "
                "searching online cannot find you easily.")
    elif place.enriched and not place.website_ok:
        hook = ("I noticed the website linked on your Google listing is not opening, so customers "
                "who click it are hitting a dead end.")
    elif "instagram" not in place.socials:
        hook = ("I noticed your website does not link to an Instagram page, which is where most "
                "local customers discover businesses today.")
    elif place.rating and place.rating >= 4.3 and place.rating_count >= 30:
        hook = (f"You already have a strong {place.rating} star rating on Google, and that reputation "
                "could be pulling in far more customers with consistent content.")
    else:
        hook = "I took a look at your online presence and see a few quick wins that could bring in more customers."
    return (
        f"Hi, this is {agency}. {hook} We help {niche}s like {place.name} with Instagram content, "
        f"websites and Google visibility. Would you be open to a quick 10 minute chat this week?"
    )


def describe(place: Place) -> str:
    if not place.website:
        site = "none listed"
    elif place.enriched and not place.website_ok:
        site = f"{place.website} (NOT REACHABLE, site appears dead)"
    else:
        site = place.website
    return "\n".join(
        [
            f"Business name: {place.name}",
            f"Category: {place.category or 'unknown'}",
            f"Address: {place.address or 'unknown'}",
            f"Google rating: {place.rating if place.rating is not None else 'none'} from {place.rating_count} reviews",
            f"Website: {site}",
            f"Social links found on website: {', '.join(place.socials) if place.socials else 'none'}",
            f"Emails found: {', '.join(place.emails) if place.emails else 'none'}",
        ]
    )


def _looks_complete(text: str) -> bool:
    """A finished draft ends with sentence punctuation and is not a fragment."""
    text = text.strip()
    return len(text) > 60 and text[-1] in ".?!"


class QuotaExhausted(RuntimeError):
    """The provider's daily free quota is used up. Retrying is pointless."""


def _is_daily_quota(text: str) -> bool:
    t = text.lower()
    return "perday" in t or "per day" in t or "requests_per_day" in t


class MessageDrafter:
    def __init__(self, agency_name: str, anthropic_model: str, gemini_model: str):
        self._agency = agency_name
        self.notice: str | None = None   # set during draft_all when something the user should know happened
        self._anthropic_model = anthropic_model
        self._gemini_model = gemini_model
        self._anthropic_clients: dict[str, object] = {}
        self._http = httpx.AsyncClient(timeout=60.0)
        self._sem = asyncio.Semaphore(MAX_PARALLEL)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _user_prompt(self, place: Place) -> str:
        return f"Agency name: {self._agency}\n\nLead data:\n{describe(place)}\n\nWrite the WhatsApp message."

    async def draft(self, place: Place, provider: str, api_key: str) -> str:
        """Draft a message. Falls back to the template on any provider error."""
        async with self._sem:
            if provider == "anthropic" and api_key:
                return await self._anthropic(place, api_key)
            if provider == "gemini" and api_key:
                return await self._gemini(place, api_key)
        return template_draft(place, self._agency)

    async def draft_all(self, places: list[Place], provider: str, api_key: str, on_progress=None) -> list[str]:
        """Draft every place, preserving order, calling on_progress(done, total) as each finishes."""
        results: list[str | None] = [None] * len(places)
        done = 0
        self.notice = None

        async def one(i: int, place: Place) -> None:
            nonlocal done
            results[i] = await self.draft(place, provider, api_key)
            done += 1
            if on_progress:
                try:
                    await on_progress(done, len(places))
                except Exception:
                    pass

        await asyncio.gather(*(one(i, p) for i, p in enumerate(places)))
        return [r or "" for r in results]

    async def verify_key(self, provider: str, api_key: str) -> str | None:
        """Return an error string if the key does not work, else None."""
        probe = Place(place_id="probe", name="Test Cafe", category="Cafe", rating=4.5, rating_count=40)
        try:
            if provider == "anthropic":
                await self._anthropic(probe, api_key, raise_on_error=True)
            elif provider == "gemini":
                await self._gemini(probe, api_key, raise_on_error=True)
            return None
        except Exception as exc:
            return str(exc)[:200]

    # ---- Anthropic -----------------------------------------------------

    async def _anthropic(self, place: Place, api_key: str, raise_on_error: bool = False) -> str:
        try:
            import anthropic

            client = self._anthropic_clients.get(api_key)
            if client is None:
                client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)
                self._anthropic_clients[api_key] = client
            response = await client.messages.create(
                model=self._anthropic_model,
                max_tokens=1024,
                output_config={"effort": "low"},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": self._user_prompt(place)}],
            )
            if response.stop_reason == "refusal":
                return template_draft(place, self._agency)
            text = " ".join(
                "".join(b.text for b in response.content if b.type == "text").split()
            )
            if not _looks_complete(text):
                log.warning("Anthropic draft incomplete for %s (stop=%s)", place.name, response.stop_reason)
                return template_draft(place, self._agency)
            return text
        except Exception as exc:
            if raise_on_error:
                raise
            log.warning("Anthropic draft failed for %s: %s", place.name, exc)
            return template_draft(place, self._agency)

    # ---- Gemini (REST, no extra dependency) ----------------------------

    async def _gemini(self, place: Place, api_key: str, raise_on_error: bool = False) -> str:
        body = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": self._user_prompt(place)}]}],
            "generationConfig": {
                "maxOutputTokens": 1024,
                "temperature": 0.7,
                # Gemini 2.5 models "think" before answering and those tokens count
                # against maxOutputTokens. A 70-word message needs no thinking.
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        url = GEMINI_URL.format(model=self._gemini_model)
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

        try:
            last_error = "unknown error"
            for attempt in range(MAX_ATTEMPTS):
                resp = await self._http.post(url, headers=headers, json=body)
                if resp.status_code == 429 and _is_daily_quota(resp.text):
                    raise QuotaExhausted("Gemini daily free quota is used up for today")
                if resp.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS - 1:
                    delay = _retry_delay(resp, attempt)
                    log.info("Gemini %s for %s, retrying in %.1fs", resp.status_code, place.name, delay)
                    await asyncio.sleep(delay)
                    continue
                if resp.status_code != 200:
                    try:
                        last_error = resp.json().get("error", {}).get("message", resp.text[:200])
                    except ValueError:
                        last_error = resp.text[:200]
                    raise RuntimeError(f"Gemini {resp.status_code}: {last_error}")

                data = resp.json()
                cand = (data.get("candidates") or [{}])[0]
                parts = cand.get("content", {}).get("parts", [])
                text = " ".join("".join(p.get("text", "") for p in parts).split())
                finish = cand.get("finishReason", "")
                if finish != "STOP" or not _looks_complete(text):
                    log.warning("Gemini draft incomplete for %s (finish=%s)", place.name, finish)
                    if raise_on_error:
                        raise RuntimeError(f"Gemini returned an incomplete draft (finishReason={finish})")
                    return template_draft(place, self._agency)
                return text
            raise RuntimeError(f"Gemini failed after {MAX_ATTEMPTS} attempts: {last_error}")
        except QuotaExhausted as exc:
            if raise_on_error:
                raise
            self.notice = (
                "Your Gemini key hit its free daily limit, so the remaining drafts used templates. "
                "Enable billing on the key at aistudio.google.com (drafts cost a fraction of a cent each), "
                "or add another key with /setkey."
            )
            log.warning("Gemini quota exhausted for %s", place.name)
            return template_draft(place, self._agency)
        except Exception as exc:
            if raise_on_error:
                raise
            log.warning("Gemini draft failed for %s: %s", place.name, exc)
            return template_draft(place, self._agency)
