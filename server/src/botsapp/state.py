"""
Global singletons set during application lifespan.

Tools import this module and access `db` / `bridge` / `message_store` at
call-time (after startup), so there is no need for lazy proxies or
dependency injection frameworks for this scale of project.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from botsapp.bridge import Bridge
    from botsapp.db import DatabaseManager
    from botsapp.message_store import MessageStore

db: DatabaseManager | None = None
message_store: MessageStore | None = None
bridge: Bridge | None = None
media_dir: Path | None = None
