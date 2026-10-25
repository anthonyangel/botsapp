"""
WAHA API keys (docs/decisions/0027, #52): parsing and a small protocol.

WAHA has admin keys (every session) and session keys (one session, with an
``actions`` map limiting what they may do). ``KeyApi`` is what the rest of the
code uses so tests need no network; ``WahaKeyApi`` implements it over the admin
``WAHAClient``. The response field names (``id``, ``key``, ``isAdmin``,
``session``, ``isActive``, ``actions``) follow WAHA's security docs and have not
been checked against a live instance (#47).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from botsapp.waha_client import WAHAClient

logger = logging.getLogger(__name__)

# WAHA's own action names. Everything else about scopes reuses these labels.
WAHA_ACTIONS = ("read", "send", "control", "setting", "app", "delete")

# What WAHA grants when a key's ``actions`` is null (its documented defaults).
DEFAULT_ACTIONS: dict[str, bool] = {
    "read": True,
    "send": True,
    "control": True,
    "setting": True,
    "app": True,
    "delete": False,
}


class KeyApiError(Exception):
    """A WAHA key call failed or returned something unusable."""


def effective_actions(raw: dict[str, Any] | None) -> dict[str, bool]:
    """The actions a key really has, as WAHA enforces them.

    ``None`` means WAHA's documented defaults. A reported map is taken as is: WAHA
    denies every action a partial map omits (verified live, #47), so it is never
    overlaid on the defaults. Names missing from a map are filled in as denied so
    callers can index every action.

    A name outside ``WAHA_ACTIONS`` is kept (so drift against our policy shows it)
    but logged, so a scope WAHA adds later doesn't go unnoticed.
    """
    if raw is None:
        return dict(DEFAULT_ACTIONS)
    actions = {name: False for name in WAHA_ACTIONS}
    for name, allowed in raw.items():
        if name not in WAHA_ACTIONS:
            logger.warning("WAHA reports an action we don't know: %r", name)
        actions[name] = bool(allowed)
    return actions


@dataclass(frozen=True)
class KeyInfo:
    id: str
    session: str | None
    is_admin: bool
    is_active: bool
    actions: dict[str, bool]
    # Set only on the response that creates the key. WAHA does list key values again
    # in GET /api/keys, so this is not the only place the value can be read.
    secret: str | None = None

    def __repr__(self) -> str:  # never let the secret reach a log line
        return (
            f"KeyInfo(id={self.id!r}, session={self.session!r}, is_admin={self.is_admin}, "
            f"is_active={self.is_active}, actions={self.actions}, secret=<hidden>)"
        )


def parse_key(data: dict[str, Any]) -> KeyInfo:
    try:
        return KeyInfo(
            id=str(data["id"]),
            session=data.get("session"),
            is_admin=bool(data.get("isAdmin")),
            is_active=bool(data.get("isActive", True)),
            actions=effective_actions(data.get("actions")),
            secret=data.get("key"),
        )
    except (KeyError, TypeError) as exc:
        raise KeyApiError(f"unexpected key payload from WAHA: {exc!r}") from exc


class KeyApi(Protocol):
    async def create_key(self, *, session: str, actions: dict[str, bool]) -> KeyInfo: ...

    async def get_key(self, key_id: str) -> KeyInfo | None: ...

    async def update_key_actions(self, key_id: str, actions: dict[str, bool]) -> KeyInfo: ...

    async def delete_key(self, key_id: str) -> None:
        """Delete a key; one that is already gone is not an error."""
        ...


class WahaKeyApi:
    """``KeyApi`` over an admin-key ``WAHAClient``."""

    def __init__(self, client: WAHAClient) -> None:
        self._client = client

    async def create_key(self, *, session: str, actions: dict[str, bool]) -> KeyInfo:
        data = await self._client.create_key(
            {"isAdmin": False, "session": session, "actions": actions}
        )
        info = parse_key(data)
        if not info.secret:
            raise KeyApiError("WAHA created the key but returned no key value")
        return info

    async def get_key(self, key_id: str) -> KeyInfo | None:
        for data in await self._client.list_keys():
            if str(data.get("id")) == key_id:
                return parse_key(data)
        return None

    async def update_key_actions(self, key_id: str, actions: dict[str, bool]) -> KeyInfo:
        current = await self.get_key(key_id)
        if current is None:
            raise KeyApiError(f"key {key_id} no longer exists in WAHA")
        data = await self._client.update_key(
            key_id,
            {
                "isAdmin": current.is_admin,
                "session": current.session,
                "isActive": current.is_active,
                "actions": actions,
            },
        )
        return parse_key(data)

    async def delete_key(self, key_id: str) -> None:
        await self._client.delete_key(key_id)
