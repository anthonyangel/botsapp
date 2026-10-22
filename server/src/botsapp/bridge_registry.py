"""
Per-account WhatsApp bridges (docs/decisions/0025, 0027).

Each ``accounts`` row is bound to its own WAHA session (``waha_session``). The
registry hands out one ``WAHAClient`` per (account, purpose), built lazily and
cached, sharing the WAHA base URL.

Which WAHA key a client uses depends on *who is asking*, and that is chosen by the
caller with a required ``purpose`` (there is deliberately no default, so a caller
can't end up on the powerful key by forgetting to say):

* ``"mcp"``   — the MCP server's read path. Uses the account's own scoped session
  key (decrypted from the database). With no stored key it **fails closed**
  (``NoScopedKeyError``) when ``require_scoped_keys`` is set; otherwise, as a
  transition while keys are being provisioned, it falls back to the admin key and
  logs a warning once per account.
* ``"admin"`` — the admin UI: session lifecycle, QR and the key API. Uses the
  admin key.

The cached MCP client is checked against the stored key on every call, so a key
rotated by the admin process is picked up by the MCP process without any message
between them. The account is resolved through the DB each time, so a deleted
account stops resolving immediately.

Authorization is *not* decided here: callers must already have checked that the
viewer holds the account; the registry only maps account -> client.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Literal

from botsapp.secretbox import SecretBox, SecretBoxError
from botsapp.waha_client import WAHAClient

if TYPE_CHECKING:
    from botsapp.db import DatabaseManager

logger = logging.getLogger(__name__)

Purpose = Literal["mcp", "admin"]

# How long a stale client (replaced by a key rotation) is kept open after being
# evicted, so an HTTP call already in flight on it doesn't have its session
# closed out from under it. Rotations are rare admin actions; a request slower
# than this grace period would still race, but that's a far smaller window
# than closing the old client immediately.
_STALE_CLIENT_GRACE_SECONDS = 30


class UnknownAccountError(LookupError):
    """No such account (distinct from "exists but you can't have it")."""


class ScopedKeyError(Exception):
    """The MCP side can't get a usable scoped key for this account. Subclasses say
    why; none carries key material, and the message is safe to log."""


class NoScopedKeyError(ScopedKeyError):
    """The account has no scoped key and the fallback to the admin key is off."""


class ScopedKeyUnreadableError(ScopedKeyError):
    """A key is stored but can't be opened (no master key, or the wrong one)."""


_SESSION_SAFE = re.compile(r"[^a-z0-9-]+")


def session_name_for(account_id: str) -> str:
    """WAHA session name for a new account. The bootstrap account keeps its
    historical session (``WAHA_SESSION``), so this is only for additional ones."""
    slug = _SESSION_SAFE.sub("-", account_id.lower()).strip("-")
    if not slug:
        raise ValueError(f"cannot derive a WAHA session name from {account_id!r}")
    return f"acct-{slug}"


class BridgeRegistry:
    def __init__(
        self,
        db: DatabaseManager,
        base_url: str,
        admin_api_key: str | None,
        *,
        box: SecretBox | None = None,
        require_scoped_keys: bool = False,
    ) -> None:
        self._db = db
        self._base_url = base_url
        self._admin_api_key = admin_api_key
        self._box = box
        self._require_scoped_keys = require_scoped_keys
        # (waha_session, purpose) -> (fingerprint of the key it was built with, client)
        self._clients: dict[tuple[str, str], tuple[str, WAHAClient]] = {}
        self._warned_fallback: set[str] = set()
        self._clients_lock = asyncio.Lock()

    # ── clients ──────────────────────────────────────────────────────────

    def _cached_client(self, session: str, purpose: str, fingerprint: str) -> WAHAClient | None:
        cached = self._clients.get((session, purpose))
        return cached[1] if cached is not None and cached[0] == fingerprint else None

    async def _client(self, session: str, purpose: str, fingerprint: str, api_key: str):
        async with self._clients_lock:
            cached = self._clients.get((session, purpose))
            if cached is not None and cached[0] == fingerprint:
                return cached[1]
            stale = cached[1] if cached is not None else None
            client = WAHAClient(self._base_url, api_key, session=session)
            self._clients[(session, purpose)] = (fingerprint, client)
        if stale is not None:  # the key changed (rotation): drop the stale client
            asyncio.create_task(self._close_after_grace(stale))
        return client

    async def _close_after_grace(self, client: WAHAClient) -> None:
        """Close a client replaced by rotation, after giving any request that
        was already holding it a chance to finish."""
        await asyncio.sleep(_STALE_CLIENT_GRACE_SECONDS)
        try:
            await client.close()
        except Exception:
            logger.exception("failed to close a stale WAHA client")

    def _admin_key(self) -> str:
        if not self._admin_api_key:
            raise ScopedKeyError("this process has no WAHA admin key")
        return self._admin_api_key

    async def for_account(self, account_id: str, *, purpose: Purpose) -> WAHAClient:
        account = await self._db.get_account(account_id)
        if account is None:
            raise UnknownAccountError(account_id)
        session = account["waha_session"]

        if purpose == "admin":
            return await self._client(session, "admin", "admin", self._admin_key())

        stored = await self._db.get_account_key(account_id)
        if stored is None:
            if self._require_scoped_keys:
                raise NoScopedKeyError(f"account {account_id!r} has no scoped WAHA key")
            if account_id not in self._warned_fallback:
                self._warned_fallback.add(account_id)
                logger.warning(
                    "account %r has no scoped WAHA key: the MCP server is using the shared "
                    "admin key for it. Create one on the Accounts page, then set "
                    "WAHA_REQUIRE_SCOPED_KEYS=true.",
                    account_id,
                )
            return await self._client(session, "mcp", "admin-fallback", self._admin_key())

        # The ciphertext (not the secret) identifies the key: it changes on rotation.
        # Check the cache before decrypting — the common case (same key as last
        # call) shouldn't pay for a decrypt it doesn't need.
        cached = self._cached_client(session, "mcp", stored["ciphertext"])
        if cached is not None:
            return cached
        if self._box is None:
            raise ScopedKeyUnreadableError(
                f"account {account_id!r} has a stored key but BOTSAPP_SECRET_KEY is not set"
            )
        try:
            secret = self._box.decrypt(stored["ciphertext"])
        except SecretBoxError as exc:
            raise ScopedKeyUnreadableError(
                f"account {account_id!r}: the stored key can't be decrypted ({exc})"
            ) from exc
        return await self._client(session, "mcp", stored["ciphertext"], secret)

    # ── session lifecycle (admin) ────────────────────────────────────────

    async def create_session(self, account_id: str) -> None:
        """Create the account's WAHA session (stopped, no QR yet)."""
        await (await self.for_account(account_id, purpose="admin")).create_session()

    async def delete_session(self, account_id: str) -> None:
        """Delete the account's WAHA session and drop its cached clients. Call
        before removing the account row — it resolves the session through it."""
        client = await self.for_account(account_id, purpose="admin")
        await client.delete_session()
        session = client._session_name
        for key in [k for k in self._clients if k[0] == session]:
            await self._clients.pop(key)[1].close()

    async def close(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for _fingerprint, client in clients:
            await client.close()
