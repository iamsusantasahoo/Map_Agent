"""Environment-driven configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    places_api_key: str = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()

    # Server-wide AI keys. Users can override with /setkey inside Telegram.
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "").strip()
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-5").strip()
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
    default_provider: str = os.getenv("DEFAULT_AI_PROVIDER", "auto").strip().lower()

    allowed_user_ids: frozenset[int] = field(
        default_factory=lambda: frozenset(
            int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()
        )
    )

    # How many businesses fit in one Telegram message (4096 char limit)
    results_per_message: int = _int("RESULTS_PER_MESSAGE", 5)
    # Google returns at most 20 per call and at most 60 per query via paging
    max_result_count: int = 60
    cache_days: int = _int("CACHE_DAYS", 30)
    db_path: str = os.getenv("DB_PATH", "leads.db")
    agency_name: str = os.getenv("AGENCY_NAME", "our agency")

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("TELEGRAM_BOT_TOKEN", self.telegram_token),
                ("GOOGLE_PLACES_API_KEY", self.places_api_key),
            )
            if not value
        ]
        if missing:
            raise SystemExit(f"Missing required env vars: {', '.join(missing)} (see .env.example)")


settings = Settings()
