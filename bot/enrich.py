"""Website enrichment: pull emails and social links from a business website.

This uses our own HTTP fetch, not the Places API, so it costs nothing and
keeps us out of the pricier Places SKUs.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .places import Place

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

SOCIAL_HOSTS = {
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "linkedin.com": "linkedin",
    "twitter.com": "x",
    "x.com": "x",
    "youtube.com": "youtube",
    "tiktok.com": "tiktok",
    "wa.me": "whatsapp",
    "api.whatsapp.com": "whatsapp",
}

# Skip share buttons, login pages and tracking pixels that point at social hosts
SOCIAL_IGNORE = re.compile(r"/(sharer|share|intent|login|signup|plugins|policies|help|legal|tr\?)", re.I)
EMAIL_IGNORE = re.compile(
    r"\.(png|jpg|jpeg|gif|svg|webp|css|js)$|example\.com|sentry|wixpress|^no-?reply@|@domain\.|yourname|email@", re.I
)

CONTACT_PATHS = ("/contact", "/contact-us", "/about", "/about-us")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MAX_BYTES = 1_500_000


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _extract(html: str, base_url: str, emails: set[str], socials: dict[str, str]) -> None:
    soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip()
            if EMAIL_RE.fullmatch(addr) and not EMAIL_IGNORE.search(addr):
                emails.add(addr.lower())
            continue
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        host = _host(full)
        for known, label in SOCIAL_HOSTS.items():
            if host == known or host.endswith("." + known):
                if SOCIAL_IGNORE.search(parsed.path + "?" + parsed.query):
                    break
                if label == "whatsapp":
                    # Keep only links that carry a phone number, drop bare share widgets
                    digits = re.sub(r"\D", "", parsed.path + parsed.query)
                    if len(digits) < 8:
                        break
                    socials.setdefault(label, f"https://wa.me/{digits}")
                    break
                socials.setdefault(label, full.split("?")[0].rstrip("/"))
                break

    text = soup.get_text(" ")
    for match in EMAIL_RE.findall(text):
        if not EMAIL_IGNORE.search(match):
            emails.add(match.lower())


async def _fetch(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                return None
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype and "text" not in ctype:
                return None
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    break
            return b"".join(chunks).decode(resp.encoding or "utf-8", errors="ignore")
    except (httpx.HTTPError, ValueError):
        return None


async def enrich_place(place: Place, client: httpx.AsyncClient) -> Place:
    """Fill emails and socials from the business website. Safe to call on any Place."""
    if place.enriched or not place.website:
        place.enriched = True
        return place

    emails: set[str] = set()
    socials: dict[str, str] = {}

    home = await _fetch(client, place.website)
    place.website_ok = home is not None
    if home:
        _extract(home, place.website, emails, socials)
        # Only visit a contact page when the homepage gave us no email
        if not emails:
            for path in CONTACT_PATHS:
                page = await _fetch(client, urljoin(place.website, path))
                if page:
                    _extract(page, place.website, emails, socials)
                if emails:
                    break

    place.emails = sorted(emails)[:3]
    place.socials = socials
    place.enriched = True
    return place


async def enrich_many(places: list[Place], concurrency: int = 5, timeout: float = 8.0) -> list[Place]:
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:

        async def run(place: Place) -> Place:
            async with sem:
                try:
                    return await enrich_place(place, client)
                except Exception:
                    place.enriched = True
                    return place

        return list(await asyncio.gather(*(run(p) for p in places)))
