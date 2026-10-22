"""
Per-request scope for MCP tools (docs/decisions/0025, phase 3).

Every tool call is resolved to a ``Scope(account, viewer)``:

* *viewer* is the authenticated caller's email, taken from the access token;
* *account* is one of the WhatsApp accounts that viewer owns or was granted,
  chosen explicitly via the tool's ``account`` argument or implicitly when the
  viewer has exactly one.

The scope carries that account's own bridge, a chat-metadata handle bound to
(account, viewer), and a ``MessageStore`` wrapped in ``AllowlistedMessageStore``
over *that viewer's* allowlist. So the privacy boundary stays where it was —
the allowlist wrapper — but is now built per request instead of once globally.
An account the caller can't hold is reported the same way as one that doesn't
exist, so ids can't be probed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

import botsapp.state as state
from botsapp.bridge_factory import scoped_keys_required
from botsapp.bridge_registry import ScopedKeyError
from botsapp.capabilities import allows, missing, required_actions
from botsapp.message_store import AllowlistedMessageStore

if TYPE_CHECKING:
    from botsapp.bridge import Bridge
    from botsapp.db import DatabaseManager
    from botsapp.message_store import MessageStore


logger = logging.getLogger(__name__)


class ScopedChatDb:
    """``DatabaseManager``'s chat-metadata methods bound to one (account, viewer).

    Keeps the method names the single-tenant code used (``list_allowed_jids``,
    ``search_by_tags``, ``get_chat_metadata``, ...) so ``AllowlistedMessageStore``
    and the tools read the same; every call is checked against the viewer's
    grant inside ``DatabaseManager``.
    """

    def __init__(self, db: DatabaseManager, account_id: str, viewer: str) -> None:
        self._db = db
        self.account_id = account_id
        self.viewer = viewer

    async def list_allowed_jids(self) -> set[str]:
        return await self._db.list_allowed_jids_for(self.account_id, self.viewer)

    async def search_by_tags(self, tags: list[str]) -> list[dict[str, Any]]:
        return await self._db.search_by_tags_for(self.account_id, self.viewer, tags)

    async def get_chat_metadata(self, jid: str) -> dict[str, Any] | None:
        return await self._db.get_chat_access(self.account_id, self.viewer, jid)

    async def upsert_chat_metadata(self, jid: str, tags: list[str]) -> None:
        await self._db.upsert_chat_access_tags(self.account_id, self.viewer, jid, tags)

    async def set_chat_allowed(self, jid: str, is_allowed: bool) -> None:
        await self._db.set_chat_access_allowed(self.account_id, self.viewer, jid, is_allowed)


@dataclass(frozen=True)
class Scope:
    account_id: str
    viewer: str
    db: ScopedChatDb
    store: MessageStore
    bridge: Bridge


def caller_email() -> str:
    """The authenticated caller's email, lowercased."""
    token = get_access_token()
    email = (token.claims.get("email") or "").strip().lower() if token else ""
    if email:
        return email
    # Imported late: botsapp.app imports nothing from here, but tools (which
    # import both) are loaded after it.
    from botsapp.app import LOCAL_DEV_DISABLE_AUTH

    if LOCAL_DEV_DISABLE_AUTH:
        dev = os.environ.get("BOTSAPP_ADMIN_EMAIL", "").strip().lower()
        if dev:
            return dev
    raise ToolError("Not authenticated.")


async def resolve_scope(account: str | None = None) -> Scope:
    """Resolve the caller and account for one tool call, or raise ToolError."""
    db, registry = state.db, state.registry
    assert db is not None and registry is not None
    viewer = caller_email()
    accounts = await db.list_accounts_for_user(viewer)
    ids = [a["id"] for a in accounts]

    if account is None:
        if not ids:
            raise ToolError("No WhatsApp accounts are available to you.")
        if len(ids) > 1:
            raise ToolError(
                f"You have several WhatsApp accounts ({', '.join(ids)}); "
                "pass account=<id> (see list_accounts)."
            )
        account = ids[0]
    elif account not in ids:
        raise ToolError(f"Unknown account '{account}'. Use list_accounts to see yours.")

    # The middleware (tool_scopes.py) puts the WAHA actions this tool needs in a
    # ContextVar; the listing check was a union, this is the account actually used.
    required = required_actions.get()
    if required:
        chosen = next(a for a in accounts if a["id"] == account)
        if not allows(chosen, required, strict=scoped_keys_required()):
            logger.warning(
                "account %s's key lacks %s",
                account,
                ", ".join(missing(chosen, required, strict=scoped_keys_required())),
            )
            raise ToolError(
                "This WhatsApp account's access key doesn't allow that; "
                "ask an administrator, or try another account (see list_accounts)."
            )
    try:
        bridge = await registry.for_account(account, purpose="mcp")
    except ScopedKeyError as exc:
        logger.error("no usable scoped WAHA key for %s: %s", account, exc)
        raise ToolError(
            "This WhatsApp account isn't ready for MCP access yet; ask an administrator "
            "to create its access key."
        ) from exc
    scoped = ScopedChatDb(db, account, viewer)
    return Scope(
        account_id=account,
        viewer=viewer,
        db=scoped,
        store=AllowlistedMessageStore(bridge, scoped),
        bridge=bridge,
    )
