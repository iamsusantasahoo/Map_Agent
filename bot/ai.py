"""Cold-message drafting with Anthropic (Claude), Google Gemini, or templates.

Provider and API key are chosen per user. Order of preference:
  1. the user's own key set with /setkey inside Telegram
  2. the server-wide key from .env
  3. the built-in template (no AI cost)
"""
from __future__ import annotations

import logging

import httpx

from .places import Place

log = logging.getLogger(__name__)

PROVIDERS = ("anthropic", "gemini", "template")

SYSTEM_PROMPT = """You write short WhatsApp outreach messages for a social media and website agency.

Rules:
- 3 to 4 sentences, under 70 words. Plain text, no emojis, no subject line, no bullet points.
- Address the business by name and mention one specific observation from the data
  (for example: strong Google rating but no Instagram link on the site, no website at all,
  many reviews but an outdated site).
- Offer one clear benefit: more customers via Instagram content, a modern website, or Google visibility.
- End with a soft question, like asking if they are open to a quick chat this week.
- Never invent facts not present in the data. Do not include placeholders like [Name].
- Return only the message text."""

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def template_draft(place: Place, agency: str) -> str:
    niche = (place.category or "business").lower()
    if not place.website:
        hook = ("I noticed you do not have a website listed on Google yet, which means customers "
                "searching online cannot find you easily.")
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
    return "\n".join(
        [
            f"Business name: {place.name}",
            f"Category: {place.category or 'unknown'}",
            f"Address: {place.address or 'unknown'}",
            f"Google rating: {place.rating if place.rating is not None else 'none'} from {place.rating_count} reviews",
            f"Website: {place.website or 'none'}",
            f"Social links found on website: {', '.join(place.socials) if place.socials else 'none'}",
            f"Emails found: {', '.join(place.emails) if place.emails else 'none'}",
        ]
    )


class MessageDrafter:
    def __init__(self, agency_name: str, anthropic_model: str, gemini_model: str):
        self._agency = agency_name
        self._anthropic_model = anthropic_model
        self._gemini_model = gemini_model
        self._anthropic_clients: dict[str, object] = {}
        self._http = httpx.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _user_prompt(self, place: Place) -> str:
        return f"Agency name: {self._agency}\n\nLead data:\n{describe(place)}\n\nWrite the WhatsApp message."

    async def draft(self, place: Place, provider: str, api_key: str) -> str:
        """Draft a message. Falls back to the template on any provider error except a bad key."""
        if provider == "anthropic" and api_key:
            return await self._anthropic(place, api_key)
        if provider == "gemini" and api_key:
            return await self._gemini(place, api_key)
        return template_draft(place, self._agency)

    async def verify_key(self, provider: str, api_key: str) -> str | None:
        """Return an error string if the key does not work, else None."""
        probe = Place(place_id="probe", name="Test Cafe", category="Cafe", rating=4.5, rating_count=40)
        try:
            if provider == "anthropic":
                await self._anthropic(place=probe, api_key=api_key, raise_on_error=True)
            elif provider == "gemini":
                await self._gemini(place=probe, api_key=api_key, raise_on_error=True)
            return None
        except Exception as exc:
            return str(exc)[:200]

    # ---- Anthropic -----------------------------------------------------

    async def _anthropic(self, place: Place, api_key: str, raise_on_error: bool = False) -> str:
        try:
            import anthropic

            client = self._anthropic_clients.get(api_key)
            if client is None:
                client = anthropic.AsyncAnthropic(api_key=api_key)
                self._anthropic_clients[api_key] = client
            response = await client.messages.create(
                model=self._anthropic_model,
                max_tokens=400,
                output_config={"effort": "low"},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": self._user_prompt(place)}],
            )
            if response.stop_reason == "refusal":
                return template_draft(place, self._agency)
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            return text or template_draft(place, self._agency)
        except Exception as exc:
            if raise_on_error:
                raise
            log.warning("Anthropic draft failed for %s: %s", place.name, exc)
            return template_draft(place, self._agency)

    # ---- Gemini (REST, no extra dependency) ----------------------------

    async def _gemini(self, place: Place, api_key: str, raise_on_error: bool = False) -> str:
        try:
            resp = await self._http.post(
                GEMINI_URL.format(model=self._gemini_model),
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json={
                    "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                    "contents": [{"role": "user", "parts": [{"text": self._user_prompt(place)}]}],
                    "generationConfig": {"maxOutputTokens": 400, "temperature": 0.7},
                },
            )
            if resp.status_code != 200:
                try:
                    msg = resp.json().get("error", {}).get("message", resp.text[:200])
                except ValueError:
                    msg = resp.text[:200]
                raise RuntimeError(f"Gemini {resp.status_code}: {msg}")
            data = resp.json()
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()
            return text or template_draft(place, self._agency)
        except Exception as exc:
            if raise_on_error:
                raise
            log.warning("Gemini draft failed for %s: %s", place.name, exc)
            return template_draft(place, self._agency)
