"""
Shared fixtures for the botsapp test suite.
"""

import os

# botsapp.app builds its AuthKitProvider at import time — set harmless dummy
# values *before* anything below imports botsapp.app (directly, or
# transitively via botsapp.tools), so collecting the test suite doesn't
# require a real WorkOS project. setdefault: a real .env already loaded
# (e.g. running under docker-compose) wins over these. No ALLOWED_EMAILS
# equivalent needed — the email allowlist lives in the DB now (see mock_db
# below / docs/decisions/0015-email-allowlist-in-db.md), not an env var.
os.environ.setdefault("WORKOS_AUTHKIT_DOMAIN", "https://test.authkit.app")
os.environ.setdefault("MCP_BASE_URL", "http://localhost:8000")

from unittest.mock import AsyncMock  # noqa: E402

import pytest  # noqa: E402

import botsapp.state as state  # noqa: E402
from botsapp.bridge import Bridge  # noqa: E402
from botsapp.db import DatabaseManager  # noqa: E402
from botsapp.message_store import MessageStore  # noqa: E402


@pytest.fixture
def mock_db() -> AsyncMock:
    mock = AsyncMock(spec=DatabaseManager)
    # Every tool that reads chat/group state checks the allowlist first;
    # default to "everything allowed" so existing tests don't all need to
    # know about it. Tests that care about the allowlist override this.
    mock.list_allowed_jids = AsyncMock(return_value=_ALLOW_ALL)
    # Same idea for the email allowlist (see test_app_auth.py) — default to
    # "allowed" so tests unrelated to auth don't each need to stub this out.
    mock.is_email_allowed = AsyncMock(return_value=True)
    return mock


@pytest.fixture
def mock_message_store() -> AsyncMock:
    return AsyncMock(spec=MessageStore)


@pytest.fixture
def mock_bridge() -> AsyncMock:
    """Spec'd against bridge.Bridge (a Protocol) — bridge-agnostic by
    construction, since tools.py never imports a concrete client."""
    return AsyncMock(spec=Bridge)


class _AllowAll(set):
    """A set that claims to contain everything — the default allowlist
    stand-in for tests that aren't specifically exercising allowlist
    enforcement."""

    def __contains__(self, _item: object) -> bool:
        return True


_ALLOW_ALL = _AllowAll()


@pytest.fixture(autouse=True)
def _patch_state(mock_db: AsyncMock, mock_message_store: AsyncMock, mock_bridge: AsyncMock):
    """
    Inject mock singletons into the state module before every test and tear
    them down afterwards. autouse=True means every test gets a clean state
    without having to import or reference this fixture explicitly.
    """
    state.db = mock_db
    state.message_store = mock_message_store
    state.bridge = mock_bridge
    yield
    state.db = None
    state.message_store = None
    state.bridge = None
