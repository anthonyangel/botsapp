"""
What an account's WAHA key allows, and what a tool needs (docs/decisions/0027, #54).

There is no mapping table: a tool declares what it needs with WAHA's own action
names as tags (``read``, ``send``, ``control``, ``setting``, ``app``, ``delete``).
Any other tag (``chat``, ``group``, ``metadata`` ...) is descriptive and has no
effect, and a tool with none of the six needs nothing from WAHA, as for the tools
that only touch our own database.

An account's actions are the ones its stored key allows. An account with no key
uses the shared admin key (the transition, see ``BridgeRegistry``), which allows
everything, unless keys are required, when it allows nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from typing import Any

from botsapp.waha_keys import WAHA_ACTIONS

_ALL = frozenset(WAHA_ACTIONS)

# Set per tool call by the middleware (tool_scopes.py) so ``resolve_scope`` can
# check the specific account the call is about; empty means no requirement.
required_actions: ContextVar[frozenset[str]] = ContextVar(
    "required_waha_actions", default=frozenset()
)


def required_for(tags: Iterable[str]) -> frozenset[str]:
    """The WAHA actions a tool needs: its tags that are WAHA action names."""
    return frozenset(tags) & _ALL


def account_actions(account: Mapping[str, Any], *, strict: bool) -> frozenset[str]:
    """The WAHA actions available for an account, from its stored key."""
    if account.get("waha_key_id"):
        stored = account.get("waha_key_actions") or {}
        return frozenset(name for name, allowed in stored.items() if allowed)
    return frozenset() if strict else _ALL


def allows(account: Mapping[str, Any], required: frozenset[str], *, strict: bool) -> bool:
    return required <= account_actions(account, strict=strict)


def missing(account: Mapping[str, Any], required: frozenset[str], *, strict: bool) -> list[str]:
    return sorted(required - account_actions(account, strict=strict))
