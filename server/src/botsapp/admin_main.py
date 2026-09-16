"""
ASGI entry point for the admin UI.

Separate process/service from the public MCP server (see main.py) — deploy
this with no public network listener. Local dev: docker-compose binds it to
127.0.0.1 only.
"""

import logging
import os

from dotenv import load_dotenv

from botsapp.admin.app import app  # noqa: F401 — re-exported for uvicorn

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
