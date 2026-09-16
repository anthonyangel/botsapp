"""
Unit tests for the authn/authz wiring in app.py.

allowed_family_email and restrict_tag are plain callables (AuthCheck), so
they're testable directly against a minimal AuthContext without spinning up
a real HTTP server or OAuth flow. allowed_family_email is async (it queries
state.db — see docs/decisions/0015-email-allowlist-in-db.md) — fastmcp's
AuthCheck type accepts sync or async callables either way.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import AuthorizationError
from fastmcp.server.auth import AuthContext, restrict_tag
from fastmcp.utilities.components import FastMCPComponent

import botsapp.app as app_module
import botsapp.state as state
from botsapp.app import _UNGRANTABLE_SCOPE, OUTBOUND_TAG, allowed_family_email


class _FakeToken:
    def __init__(self, claims: dict | None = None, scopes: list[str] | None = None) -> None:
        self.claims = claims or {}
        self.scopes = scopes or []


def _ctx(token, tags: set[str] | None = None) -> AuthContext:
    return AuthContext(token=token, component=FastMCPComponent(name="fake", tags=tags or set()))


# ── allowed_family_email ────────────────────────────────────────────────────
#
# state.db is set by conftest.py's autouse _patch_state fixture; each test
# below stubs is_email_allowed directly rather than going through a real
# DatabaseManager (that's test_db.py's job — see its "email allowlist"
# section).


async def test_allowed_family_email_accepts_listed_address():
    state.db.is_email_allowed = AsyncMock(return_value=True)
    ctx = _ctx(_FakeToken(claims={"email": "anthony@angelfamily.net"}))
    assert await allowed_family_email(ctx) is True
    state.db.is_email_allowed.assert_awaited_once_with("anthony@angelfamily.net")


async def test_allowed_family_email_is_case_insensitive():
    state.db.is_email_allowed = AsyncMock(return_value=True)
    ctx = _ctx(_FakeToken(claims={"email": "Anthony@AngelFamily.NET"}))
    assert await allowed_family_email(ctx) is True
    # The claim is lowercased before ever reaching the DB — is_email_allowed
    # itself also normalizes (see db.py), but the check shouldn't rely on that.
    state.db.is_email_allowed.assert_awaited_once_with("anthony@angelfamily.net")


async def test_allowed_family_email_rejects_unlisted_address():
    state.db.is_email_allowed = AsyncMock(return_value=False)
    ctx = _ctx(_FakeToken(claims={"email": "stranger@example.com"}))
    with pytest.raises(AuthorizationError):
        await allowed_family_email(ctx)


async def test_allowed_family_email_rejects_unauthenticated_caller():
    ctx = _ctx(token=None)
    with pytest.raises(AuthorizationError):
        await allowed_family_email(ctx)
    # No email to check — shouldn't even hit the DB.
    state.db.is_email_allowed.assert_not_awaited()


async def test_allowed_family_email_rejects_token_without_email_claim():
    ctx = _ctx(_FakeToken(claims={}))
    with pytest.raises(AuthorizationError):
        await allowed_family_email(ctx)
    state.db.is_email_allowed.assert_not_awaited()


# ── outbound-tag restriction ─────────────────────────────────────────────


def test_outbound_tagged_tool_blocked_without_ungrantable_scope():
    check = restrict_tag(OUTBOUND_TAG, scopes=[_UNGRANTABLE_SCOPE])
    ctx = _ctx(_FakeToken(scopes=["openid", "email"]), tags={OUTBOUND_TAG})
    assert check(ctx) is False


def test_non_outbound_tool_unaffected_by_outbound_restriction():
    check = restrict_tag(OUTBOUND_TAG, scopes=[_UNGRANTABLE_SCOPE])
    ctx = _ctx(_FakeToken(scopes=["openid", "email"]), tags={"read", "chat"})
    assert check(ctx) is True


# ── LOCAL_DEV_DISABLE_AUTH (local-dev-only escape hatch) ────────────────────


def test_build_auth_skips_authkit_provider_when_disabled():
    with patch.object(app_module, "LOCAL_DEV_DISABLE_AUTH", True):
        assert app_module._build_auth() is None


@patch.dict(
    os.environ,
    {
        "WORKOS_AUTHKIT_DOMAIN": "https://example.authkit.app",
        "MCP_BASE_URL": "http://localhost:8000",
    },
)
def test_build_auth_builds_authkit_provider_when_enabled():
    with patch.object(app_module, "LOCAL_DEV_DISABLE_AUTH", False):
        auth = app_module._build_auth()
        assert auth is not None


def test_build_auth_checks_drops_email_allowlist_when_disabled():
    with patch.object(app_module, "LOCAL_DEV_DISABLE_AUTH", True):
        checks = app_module._build_auth_checks()
    assert allowed_family_email not in checks
    # The outbound restriction is never dropped, disabled or not.
    ctx = _ctx(_FakeToken(scopes=[]), tags={OUTBOUND_TAG})
    assert all(check(ctx) is False for check in checks)


def test_build_auth_checks_keeps_email_allowlist_when_enabled():
    with patch.object(app_module, "LOCAL_DEV_DISABLE_AUTH", False):
        checks = app_module._build_auth_checks()
    assert allowed_family_email in checks
