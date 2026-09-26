"""SQLite storage: place cache, search history, saved leads, per-user session."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .places import Place

SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
    place_id   TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS searches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    query      TEXT NOT NULL,
    place_ids  TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS saved_leads (
    user_id    INTEGER NOT NULL,
    place_id   TEXT NOT NULL,
    note       TEXT DEFAULT '',
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, place_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    user_id    INTEGER PRIMARY KEY,
    data       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_settings (
    user_id       INTEGER PRIMARY KEY,
    result_count  INTEGER,
    provider      TEXT,
    anthropic_key TEXT,
    gemini_key    TEXT
);
"""


class Database:
    def __init__(self, path: str, cache_days: int = 30):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._cache_ttl = cache_days * 86400

    # ---- place cache -------------------------------------------------------

    def get_place(self, place_id: str) -> Place | None:
        row = self._conn.execute(
            "SELECT data, fetched_at FROM places WHERE place_id = ?", (place_id,)
        ).fetchone()
        if not row or time.time() - row["fetched_at"] > self._cache_ttl:
            return None
        return Place.from_dict(json.loads(row["data"]))

    def get_places(self, place_ids: list[str]) -> list[Place]:
        out = []
        for pid in place_ids:
            p = self.get_place(pid)
            if p:
                out.append(p)
        return out

    def upsert_places(self, places: list[Place]) -> None:
        now = int(time.time())
        self._conn.executemany(
            "INSERT OR REPLACE INTO places (place_id, data, fetched_at) VALUES (?, ?, ?)",
            [(p.place_id, json.dumps(p.to_dict()), now) for p in places],
        )
        self._conn.commit()

    # ---- search history ----------------------------------------------------

    def log_search(self, user_id: int, query: str, place_ids: list[str]) -> None:
        self._conn.execute(
            "INSERT INTO searches (user_id, query, place_ids, created_at) VALUES (?, ?, ?, ?)",
            (user_id, query, json.dumps(place_ids), int(time.time())),
        )
        self._conn.commit()

    def recent_searches(self, user_id: int, limit: int = 5) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT query, place_ids, created_at FROM searches "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()

    # ---- saved leads -------------------------------------------------------

    def save_lead(self, user_id: int, place_id: str, note: str = "") -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO saved_leads (user_id, place_id, note, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, place_id, note, int(time.time())),
        )
        self._conn.commit()

    def remove_lead(self, user_id: int, place_id: str) -> None:
        self._conn.execute(
            "DELETE FROM saved_leads WHERE user_id = ? AND place_id = ?", (user_id, place_id)
        )
        self._conn.commit()

    def saved_leads(self, user_id: int) -> list[Place]:
        rows = self._conn.execute(
            "SELECT place_id FROM saved_leads WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        out = []
        for r in rows:
            # Saved leads must survive cache expiry, so read directly
            row = self._conn.execute(
                "SELECT data FROM places WHERE place_id = ?", (r["place_id"],)
            ).fetchone()
            if row:
                out.append(Place.from_dict(json.loads(row["data"])))
        return out

    # ---- per-user session (current search, page token, current page) -------

    def get_session(self, user_id: int) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT data FROM sessions WHERE user_id = ?", (user_id,)
        ).fetchone()
        return json.loads(row["data"]) if row else {}

    def set_session(self, user_id: int, data: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO sessions (user_id, data) VALUES (?, ?)",
            (user_id, json.dumps(data)),
        )
        self._conn.commit()

    # ---- per-user settings (result count, AI provider, AI keys) ------------

    def get_settings(self, user_id: int) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT result_count, provider, anthropic_key, gemini_key "
            "FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return {"result_count": None, "provider": None, "anthropic_key": "", "gemini_key": ""}
        return {
            "result_count": row["result_count"],
            "provider": row["provider"],
            "anthropic_key": row["anthropic_key"] or "",
            "gemini_key": row["gemini_key"] or "",
        }

    def set_setting(self, user_id: int, field: str, value: Any) -> None:
        if field not in ("result_count", "provider", "anthropic_key", "gemini_key"):
            raise ValueError(field)
        self._conn.execute(
            "INSERT INTO user_settings (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )
        self._conn.execute(
            f"UPDATE user_settings SET {field} = ? WHERE user_id = ?", (value, user_id)
        )
        self._conn.commit()
