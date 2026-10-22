"""
Per-account WhatsApp bridges (docs/decisions/0025, phase 2).

Each ``accounts`` row is bound to its own WAHA session (``waha_session``). The
registry hands out one ``WAHAClient`` per account, built lazily and cached, all
sharing the same WAHA base URL and API key. It resolves the account through the
DB on every call, so a deleted account stops resolving immediately.

Authorization is *not* decided here: callers must already have checked that the
viewer holds the account (``DatabaseManager.has_account_access``); the registry
only maps account -> client.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from botsapp.waha_client import WAHAClient

if TYPE_CHECKING:
    from botsapp.db import DatabaseManager


class UnknownAccountError(LookupError):
    """No such account (distinct from "exists but you can't have it")."""


_SESSION_SAFE = re.compile(r"[^a-z0-9-]+")


def session_name_for(account_id: str) -> str:
    """WAHA session name for a new account. The bootstrap account keeps its
    historical session (``WAHA_SESSION``), so this is only for additional ones."""
    slug = _SESSION_SAFE.sub("-", account_id.lower()).strip("-")
    if not slug:
        raise ValueError(f"cannot derive a WAHA session name from {account_id!r}")
    return f"acct-{slug}"


class BridgeRegistry:
    def __init__(self, db: DatabaseManager, base_url: str, api_key: str) -> None:
        self._db = db
        self._base_url = base_url
        self._api_key = api_key
        self._clients: dict[str, WAHAClient] = {}  # keyed by waha_session

    async def for_account(self, account_id: str) -> WAHAClient:
        account = await self._db.get_account(account_id)
        if account is None:
            raise UnknownAccountError(account_id)
        session = account["waha_session"]
        client = self._clients.get(session)
        if client is None:
            client = WAHAClient(self._base_url, self._api_key, session=session)
            self._clients[session] = client
        return client

    async def create_session(self, account_id: str) -> None:
        """Create the account's WAHA session (stopped, no QR yet)."""
        await (await self.for_account(account_id)).create_session()

    async def delete_session(self, account_id: str) -> None:
        """Delete the account's WAHA session and drop the cached client. Call
        before removing the account row — it resolves the session through it."""
        client = await self.for_account(account_id)
        await client.delete_session()
        self._clients.pop(client._session_name, None)
        await client.close()

    async def close(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await client.close()
