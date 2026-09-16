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
from botsapp.message_store import MessageStore
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
