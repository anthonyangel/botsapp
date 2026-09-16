"""
Shared aiosql query loader for db.py's chat_metadata store.

SQLite, via aiosqlite — the only query set this project loads; WAHA has no
shared database of its own to read.
"""

from pathlib import Path

import aiosql

_PACKAGE_ROOT = Path(__file__).parent
_SQLITE_QUERIES_PATH = _PACKAGE_ROOT / "queries_sqlite"

metadata_queries = aiosql.from_path(
    str(_SQLITE_QUERIES_PATH), "aiosqlite", mandatory_parameters=False
)
