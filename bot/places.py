"""Client for Google Places API (New) Text Search.

Endpoint: POST https://places.googleapis.com/v1/places:searchText

Billing note: the field mask below includes phone, website and rating, which
puts every call in the "Text Search Enterprise" SKU (1,000 free calls/month,
then roughly $35 per 1,000). Each call returns up to 20 places. Results are
cached in SQLite so a repeated search costs nothing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

SEARCH_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"

# Only request what the bot actually shows. Every extra field can move the
# call into a more expensive SKU.
FIELD_MASK = ",".join(
    [
        "nextPageToken",
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.websiteUri",
        "places.googleMapsUri",
        "places.rating",
        "places.userRatingCount",
        "places.primaryTypeDisplayName",
        "places.types",
        "places.businessStatus",
    ]
)


@dataclass
class Place:
    place_id: str
    name: str
    address: str = ""
    phone: str = ""
    phone_intl: str = ""
    website: str = ""
    maps_url: str = ""
    rating: float | None = None
    rating_count: int = 0
    category: str = ""
    types: list[str] = field(default_factory=list)
    business_status: str = ""
    # Filled by enrichment
    emails: list[str] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)
    enriched: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Place":
        return cls(**data)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Place":
        return cls(
            place_id=raw.get("id", ""),
            name=(raw.get("displayName") or {}).get("text", "Unknown"),
            address=raw.get("formattedAddress", ""),
            phone=raw.get("nationalPhoneNumber", ""),
            phone_intl=raw.get("internationalPhoneNumber", ""),
            website=raw.get("websiteUri", ""),
            maps_url=raw.get("googleMapsUri", ""),
            rating=raw.get("rating"),
            rating_count=raw.get("userRatingCount", 0) or 0,
            category=(raw.get("primaryTypeDisplayName") or {}).get("text", ""),
            types=raw.get("types", []) or [],
            business_status=raw.get("businessStatus", ""),
        )


@dataclass
class SearchPage:
    places: list[Place]
    next_page_token: str | None


class PlacesError(RuntimeError):
    pass


class PlacesClient:
    def __init__(self, api_key: str, page_size: int = 20, timeout: float = 20.0):
        self._api_key = api_key
        self._page_size = max(1, min(page_size, 20))
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search_text(self, query: str, page_token: str | None = None) -> SearchPage:
        body: dict[str, Any] = {"textQuery": query, "pageSize": self._page_size}
        if page_token:
            body["pageToken"] = page_token

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        }

        resp = await self._client.post(SEARCH_TEXT_URL, json=body, headers=headers)
        if resp.status_code != 200:
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except ValueError:
                detail = resp.text[:300]
            raise PlacesError(f"Places API {resp.status_code}: {detail}")

        data = resp.json()
        places = [Place.from_api(p) for p in data.get("places", [])]
        return SearchPage(places=places, next_page_token=data.get("nextPageToken"))
