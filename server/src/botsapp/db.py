"""
Database access layer for chat metadata (tags/allowlist).

This is data whatsapp-mcp owns itself — unlike chat/message history (see
message_store.py), it has nothing bridge-specific about it and doesn't move
if the WhatsApp bridge underneath ever changes.

SQLite-backed (via aiosqlite), not Postgres — this table is small
(family-scale, a few dozen chats at most) and doesn't need a separate
database server.

``is_allowed`` here is the actual privacy boundary the MCP server enforces
— it's only ever written by ``set_chat_allowed``, which the admin UI calls
and nothing in ``tools.py`` does, so a chat can never allow itself.
"""

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from botsapp._queries import metadata_queries as queries

logger = logging.getLogger(__name__)


class DatabaseManager:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> aiosqlite.Connection:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        # WAL mode: the MCP server and the admin UI are separate processes
        # that both open this same file — WAL lets the rare writer (a tag
        # edit, an allow-toggle click) proceed without blocking readers.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.commit()
        logger.info("SQLite metadata store connected: %s", self._db_path)
        return self._conn

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("SQLite metadata store connection closed")

    @property
    def connection(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Database not connected — call connect() first.")
        return self._conn

    # ── Chat metadata ─────────────────────────────────────────────────────

    async def ensure_schema(self) -> None:
        """Create the table if it doesn't exist yet. Called once at startup."""
        await queries.create_chat_metadata_table(self.connection)
        await self.connection.commit()

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        """SQLite has no array or boolean type — tags round-trips through
        JSON text, is_allowed through 0/1 — so every read goes through here
        to hand callers the same shapes the old Postgres version did
        (tags: list[str], is_allowed: bool)."""
        d = dict(row)
        d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
        d["is_allowed"] = bool(d.get("is_allowed"))
        return d

    async def upsert_chat_metadata(self, jid: str, tags: list[str]) -> None:
        await queries.upsert_chat_metadata(self.connection, jid=jid, tags=json.dumps(tags))
        await self.connection.commit()

    async def get_chat_metadata(self, jid: str) -> dict[str, Any] | None:
        row = await queries.get_chat_metadata(self.connection, jid=jid)
        return self._row_to_dict(row) if row else None

    async def search_by_tags(self, tags: list[str]) -> list[dict[str, Any]]:
        """Allowed chats carrying any of ``tags`` — backs the MCP-facing
        ``list_chats(tags=...)`` and ``list_groups(tags=...)`` tools, the
        primary (metadata-first) way those tools look a chat/group up.

        Case-insensitive: tags are typed by a person (via the admin UI or
        add_tag) or guessed by Claude from a user's phrasing, and "Shaked"
        vs "shaked" shouldn't be the difference between a hit and a miss.

        Fetches every allowed row and filters in Python rather than in SQL —
        a full scan that's free at this table's size (see module docstring).
        ``name`` here just falls back to the jid; this table has no separate
        contacts data to resolve a display name from.
        """
        wanted = {t.lower() for t in tags}
        rows = [self._row_to_dict(r) async for r in queries.list_metadata_rows(self.connection)]
        matches = [r for r in rows if wanted & {t.lower() for t in r["tags"]}]
        for r in matches:
            r["name"] = r["jid"]
        matches.sort(key=lambda r: r["updated_at"], reverse=True)
        return matches

    # ── Allowlist ────────────────────────────────────────────────────────

    async def list_allowed_jids(self) -> set[str]:
        return {r["jid"] async for r in queries.list_allowed_jids(self.connection)}

    async def set_chat_allowed(self, jid: str, is_allowed: bool) -> None:
        await queries.set_chat_allowed(self.connection, jid=jid, is_allowed=is_allowed)
        await self.connection.commit()
