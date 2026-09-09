"""
Bridge-agnostic chat/message read interface.

``tools.py`` depends on ``MessageStore`` for chat and message history — it
knows nothing about which WhatsApp bridge sits underneath. The concrete
implementation is ``WAHAClient`` (waha_client.py), which calls WAHA's own
REST API. Keeping this a Protocol rather than a concrete class means
``tools.py`` still wouldn't need to change if a different bridge were added
later.

``AllowlistedMessageStore`` wraps any concrete store to enforce the chat
allowlist — this is the actual privacy boundary the MCP server's read tools
are built on.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from botsapp.db import DatabaseManager

logger = logging.getLogger(__name__)


class ChatNotAllowedError(Exception):
    """Raised when asked about a chat that isn't on the allowlist.

    Deliberately distinct from "no such chat" (a jid the bridge has never
    heard of) — callers should be able to tell "not visible to this server"
    apart from "doesn't exist", both for a clearer error to the caller and
    so the allowlist boundary itself is legible rather than looking like an
    empty result.
    """


class MessageStore(Protocol):
    """Read-only interface for chat and message history."""

    async def get_chats(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]: ...

    async def get_chat(self, jid: str) -> dict[str, Any] | None: ...

    async def list_messages(
        self, jid: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]: ...

    async def search_messages(
        self, query: str, jid: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]: ...

    async def get_last_message(self, jid: str) -> dict[str, Any] | None: ...

    async def search_chats(
        self, query: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]: ...

    async def get_message_chat_jid(self, message_id: str) -> str | None: ...

    async def get_media(self, message_id: str, chat_jid: str) -> dict[str, Any] | None: ...


class AllowlistedMessageStore:
    """Wraps any ``MessageStore`` so only allowlisted chats are ever visible.

    Chats are allowed exclusively via the network-private admin UI
    (``DatabaseManager.set_chat_allowed``); nothing reachable through the
    MCP server can grant itself visibility. Wrapping the *interface* rather
    than baking allowlist checks into the concrete store keeps the
    enforcement working unchanged if that store is ever swapped for a
    different one.

    Listing methods fetch a large, unfiltered batch from the inner store,
    filter to allowed chats, then paginate in Python. That trades a larger
    inner query for correctness: filtering *after* the inner store's own
    LIMIT/OFFSET would silently drop chats whenever only a few of them are
    allowed (a page of 50 recent chats might contain none of the 3 you
    actually allowed). Fine at the scale this project runs at — a handful
    of family chats, not thousands.
    """

    _FETCH_ALL = 10_000  # effectively "no limit" for a family-scale chat list

    def __init__(self, inner: MessageStore, db: DatabaseManager) -> None:
        self._inner = inner
        self._db = db

    async def _allowed(self) -> set[str]:
        return await self._db.list_allowed_jids()

    async def _require_allowed(self, jid: str) -> None:
        if jid not in await self._allowed():
            raise ChatNotAllowedError(f"chat '{jid}' is not in the allowlist")

    async def get_chats(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        allowed = await self._allowed()
        rows = await self._inner.get_chats(limit=self._FETCH_ALL, offset=0)
        filtered = [r for r in rows if r["jid"] in allowed]
        return filtered[offset : offset + limit]

    async def get_chat(self, jid: str) -> dict[str, Any] | None:
        await self._require_allowed(jid)
        return await self._inner.get_chat(jid)

    async def list_messages(
        self, jid: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        await self._require_allowed(jid)
        return await self._inner.list_messages(jid=jid, limit=limit, offset=offset)

    async def search_messages(
        self, query: str, jid: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        if jid is not None:
            await self._require_allowed(jid)
            return await self._inner.search_messages(
                query=query, jid=jid, limit=limit, offset=offset
            )
        allowed = await self._allowed()
        rows = await self._inner.search_messages(
            query=query, jid=None, limit=self._FETCH_ALL, offset=0
        )
        filtered = [r for r in rows if r["chat_jid"] in allowed]
        return filtered[offset : offset + limit]

    async def get_last_message(self, jid: str) -> dict[str, Any] | None:
        await self._require_allowed(jid)
        return await self._inner.get_last_message(jid)

    async def search_chats(
        self, query: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        allowed = await self._allowed()
        rows = await self._inner.search_chats(query=query, limit=self._FETCH_ALL, offset=0)
        filtered = [r for r in rows if r["jid"] in allowed]
        return filtered[offset : offset + limit]

    async def get_message_chat_jid(self, message_id: str) -> str | None:
        # Deliberately unfiltered, unlike every other method here: the
        # caller (get_media) doesn't have a jid to check yet — resolving
        # message_id -> jid *is* the point of this call. It must check the
        # returned jid against the allowlist itself, the same way
        # get_chat_metadata/add_tag check explicitly via state.db rather
        # than through this wrapper.
        return await self._inner.get_message_chat_jid(message_id=message_id)

    async def get_media(self, message_id: str, chat_jid: str) -> dict[str, Any] | None:
        # Deliberately unfiltered, same reasoning as get_message_chat_jid
        # above: tools.get_media() already resolved chat_jid via that call
        # and checked it against the allowlist itself before ever calling
        # this.
        return await self._inner.get_media(message_id=message_id, chat_jid=chat_jid)
