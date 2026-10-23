"""
Global singletons set during application lifespan.

Tools import this module and access `db` / `bridge` / `registry` at
call-time (after startup), so there is no need for lazy proxies or
dependency injection frameworks for this scale of project.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from botsapp.bridge import Bridge
    from botsapp.bridge_registry import BridgeRegistry
    from botsapp.db import DatabaseManager
    from botsapp.key_manager import KeyManager

db: DatabaseManager | None = None
bridge: Bridge | None = None
registry: BridgeRegistry | None = None
# Admin UI only: provisions/rotates/verifies the scoped WAHA keys (0027).
key_manager: KeyManager | None = None
