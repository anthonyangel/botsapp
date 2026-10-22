"""
Constructs the (Bridge, MessageStore) pair. Shared by botsapp.app (the
MCP server) and botsapp.admin.app (the admin UI) so bridge construction
can't drift between the two processes.

WAHA is the only bridge provider. The Bridge/MessageStore Protocol split
(bridge.py, message_store.py) stays regardless, since AllowlistedMessageStore
wraps whatever MessageStore is given and both processes are tested against
the Protocol rather than a concrete client.
"""

import logging
import os

from botsapp.bridge import Bridge
from botsapp.bridge_registry import BridgeRegistry
from botsapp.db import DatabaseManager
from botsapp.message_store import MessageStore
from botsapp.secretbox import SecretBox
from botsapp.waha_client import WAHAClient

logger = logging.getLogger(__name__)


def build_bridge_and_store() -> tuple[Bridge, MessageStore]:
    client = WAHAClient(
        base_url=os.environ["WAHA_URL"],
        api_key=os.environ["WAHA_API_KEY"],
        session=os.environ.get("WAHA_SESSION", "default"),
    )
    logger.info("Bridge ready (WAHA: %s)", os.environ["WAHA_URL"])
    return client, client  # one class satisfies both protocols


def bridge_provider_name() -> str:
    """The active provider's name, for display (e.g. the admin UI header)."""
    return "waha"


def scoped_keys_required() -> bool:
    """WAHA_REQUIRE_SCOPED_KEYS: the MCP side refuses to use the shared admin key
    for an account that has no scoped key (docs/decisions/0027)."""
    return os.environ.get("WAHA_REQUIRE_SCOPED_KEYS", "").strip().lower() in ("1", "true", "yes")


def build_registry(db: DatabaseManager, box: SecretBox | None = None) -> BridgeRegistry:
    """Per-account bridges (0025, 0027). The admin key is optional here: the MCP
    process in strict mode has it stripped from its environment."""
    return BridgeRegistry(
        db,
        os.environ["WAHA_URL"],
        os.environ.get("WAHA_API_KEY") or None,
        box=box,
        require_scoped_keys=scoped_keys_required(),
    )
