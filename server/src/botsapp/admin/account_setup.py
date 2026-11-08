"""
Creating an account end to end (docs/decisions/0031, 0025, 0027), shared by the
Accounts page's *Create* form and the Users page's *Invite* (which makes the
invitee's account so there is no second step).

Ordering, so a failure leaves nothing half-made:

* the DB row first (the registry resolves the WAHA session through it),
* then the WAHA session; if WAHA fails the row is removed again,
* then the scoped WAHA key; if that fails the session and the row are removed.

Without a key manager that can store (no ``BOTSAPP_SECRET_KEY`` or admin key) the
account is still made, but the caller is told it has no key yet.
"""

from __future__ import annotations

import logging
import re

import botsapp.state as state
from botsapp.bridge_registry import session_name_for
from botsapp.key_manager import KeyManagerError

logger = logging.getLogger(__name__)

ACCOUNT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$")
_NOT_ID_CHARS = re.compile(r"[^a-z0-9]+")


class AccountSetupError(Exception):
    """Creating the account failed; everything already done was undone."""


def valid_account_id(account_id: str) -> bool:
    return ACCOUNT_ID.match(account_id) is not None


async def free_account_id(email: str) -> str:
    """An unused account id derived from the email's local part (``danni@x`` → ``danni``)."""
    assert state.db is not None
    base = _NOT_ID_CHARS.sub("-", email.split("@", 1)[0].lower()).strip("-")[:28] or "user"
    if len(base) < 2:
        base = f"{base}-acct"
    candidate, n = base, 1
    while await state.db.get_account(candidate) is not None:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


async def create_account(account_id: str, owner: str, label: str = "") -> str | None:
    """Create the account row, WAHA session and read-only key.

    Returns a warning when the account exists but has no key (the MCP server can't
    use it until one is created), else ``None``. Raises ``AccountSetupError`` with
    nothing left behind.
    """
    assert state.db is not None and state.registry is not None
    await state.db.add_account(account_id, owner, session_name_for(account_id), label=label)
    try:
        await state.registry.create_session(account_id)
    except Exception as exc:
        logger.exception("creating the WAHA session for %s failed", account_id)
        await state.db.delete_account(account_id)
        raise AccountSetupError(
            f"Couldn't create the WhatsApp session ({exc}); nothing was created."
        ) from exc

    manager = state.key_manager
    if manager is None or not manager.can_store:
        return (
            "no access key: BOTSAPP_SECRET_KEY or the WAHA admin key isn't set here. "
            "The MCP server can't use this account until one is created."
        )
    try:
        await manager.provision(account_id)
    except KeyManagerError as exc:
        # An account without its key would silently be locked out of the MCP server.
        try:
            await state.registry.delete_session(account_id)
        except Exception:
            logger.exception(
                "could not remove the WAHA session for %s after a key failure", account_id
            )
        await state.db.delete_account(account_id)
        raise AccountSetupError(
            f"Couldn't create the access key ({exc}); nothing was created."
        ) from exc
    return None
