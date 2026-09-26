"""Telegram HTML formatting for places and message drafts."""
from __future__ import annotations

import re
from html import escape

from .places import Place

SOCIAL_LABELS = {
    "instagram": "Instagram",
    "facebook": "Facebook",
    "linkedin": "LinkedIn",
    "x": "X",
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "whatsapp": "WhatsApp",
}


def whatsapp_link(place: Place, text: str | None = None) -> str | None:
    """wa.me link that opens a chat with the business number on the user's own phone."""
    digits = re.sub(r"\D", "", place.phone_intl or place.phone)
    if len(digits) < 8:
        return None
    url = f"https://wa.me/{digits}"
    if text:
        from urllib.parse import quote

        url += f"?text={quote(text)}"
    return url


def format_place(index: int, place: Place) -> str:
    lines = [f"<b>{index}. {escape(place.name)}</b>"]
    if place.category:
        lines.append(f"🏷 {escape(place.category)}")
    if place.address:
        addr = escape(place.address)
        lines.append(f'📍 <a href="{escape(place.maps_url)}">{addr}</a>' if place.maps_url else f"📍 {addr}")
    if place.rating is not None:
        lines.append(f"⭐ {place.rating} ({place.rating_count} reviews)")
    if place.phone:
        wa = whatsapp_link(place)
        phone = escape(place.phone)
        lines.append(f'📞 {phone}' + (f' · <a href="{wa}">Open in WhatsApp</a>' if wa else ""))
    if place.website:
        lines.append(f'🌐 <a href="{escape(place.website)}">{escape(_short_url(place.website))}</a>')
    for key, url in place.socials.items():
        label = SOCIAL_LABELS.get(key, key.title())
        lines.append(f'🔗 <a href="{escape(url)}">{label}</a>')
    for email in place.emails:
        lines.append(f"✉️ {escape(email)}")
    if place.business_status and place.business_status != "OPERATIONAL":
        lines.append(f"⚠️ {escape(place.business_status.replace('_', ' ').title())}")
    if not place.website and not place.socials:
        lines.append("💡 No website or socials found. Strong lead for a web build.")
    return "\n".join(lines)


def format_page(query: str, places: list[Place], start_index: int, total_seen: int) -> str:
    header = f"🔎 <b>{escape(query)}</b>\nShowing {start_index}–{start_index + len(places) - 1} · {total_seen} loaded so far\n"
    blocks = [format_place(start_index + i, p) for i, p in enumerate(places)]
    footer = "\nReply <code>save N</code> to save a lead, or tap a button below."
    return header + "\n\n".join(blocks) + "\n" + footer


def format_draft(index: int, place: Place, text: str) -> str:
    wa = whatsapp_link(place, text)
    out = f"<b>{index}. {escape(place.name)}</b>\n<code>{escape(text)}</code>"
    if wa:
        out += f'\n<a href="{wa}">Send on WhatsApp</a>'
    if "instagram" in place.socials:
        out += f' · <a href="{escape(place.socials["instagram"])}">Open Instagram</a>'
    return out


def _short_url(url: str) -> str:
    url = re.sub(r"^https?://(www\.)?", "", url).split("?")[0]
    return url.rstrip("/")[:40]
