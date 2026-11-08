"""
Constructs the per-account ``BridgeRegistry``. Shared by botsapp.app (the MCP
server) and botsapp.admin.app (the admin UI) so construction can't drift between
the two processes.

WAHA is the only bridge provider. The Bridge/MessageStore Protocol split
(bridge.py, message_store.py) stays regardless, since AllowlistedMessageStore
wraps whatever MessageStore is given and both processes are tested against
the Protocol rather than a concrete client.
"""

import logging
import os

from botsapp.bridge_registry import BridgeRegistry
from botsapp.db import DatabaseManager
from botsapp.secretbox import SecretBox

logger = logging.getLogger(__name__)


def bridge_provider_name() -> str:
    """The active provider's name, for display (e.g. the admin UI header)."""
    return "waha"


def build_registry(db: DatabaseManager, box: SecretBox | None = None) -> BridgeRegistry:
    """Per-account bridges (0025, 0027). The admin key is optional here: the MCP
    process has it stripped from its environment (start.sh)."""
    return BridgeRegistry(
        db,
        os.environ["WAHA_URL"],
        os.environ.get("WAHA_API_KEY") or None,
        box=box,
    )
