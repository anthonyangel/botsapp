"""
The path prefix the admin UI is mounted under (docs/decisions/0028).

Empty by default: admin-ui owns its whole path space, as it does over
``fly proxy`` or docker-compose's direct port 8100. On Fly, both mcp-server
and admin-ui now sit behind one public port (443) via a small Caddy reverse
proxy keyed on this prefix, so ``ADMIN_URL_PREFIX=/admin`` is set there and
every admin-UI route, redirect, href and form action must carry it — nothing
below this module resolves the prefix dynamically per request, since Caddy
forwards the path *unstripped* and the two sides must agree on it statically.
"""

from __future__ import annotations

import os

PREFIX = os.environ.get("ADMIN_URL_PREFIX", "").rstrip("/")


def p(path: str) -> str:
    """Prefix an admin-UI path (``/accounts``, ``/login``, ...) for use in a
    route registration, redirect, href or form action."""
    return f"{PREFIX}{path}"
