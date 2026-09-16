"""
ASGI entry point.

Configures logging, triggers tool registration (via import side-effect),
and exposes the ASGI `app` for uvicorn.
"""

import logging
import os

from dotenv import load_dotenv

import botsapp.tools  # noqa: F401 — registers @mcp.tool() decorators
from botsapp._authkit_issuer_workaround import FixAuthkitIssuerTrailingSlashMiddleware
from botsapp.app import mcp

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# ASGI app served by uvicorn (Streamable HTTP transport at /mcp). Wrapped to
# work around an upstream fastmcp/mcp-SDK bug — see
# _authkit_issuer_workaround.py — remove once that's fixed there.
app = FixAuthkitIssuerTrailingSlashMiddleware(mcp.http_app())
