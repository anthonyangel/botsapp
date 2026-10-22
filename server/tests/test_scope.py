"""Per-request scope resolution and cross-user isolation (0025 phase 3).

Runs against a real temp SQLite DB. conftest's autouse patch of
``botsapp.tools.resolve_scope`` doesn't affect ``botsapp.scope.resolve_scope``,
which is what's under test here.
"""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import ToolError

import botsapp.scope as scope_module
import botsapp.state as state
from botsapp.bridge_registry import BridgeRegistry, session_name_for
from botsapp.db import DatabaseManager
from botsapp.message_store import ChatNotAllowedError
from botsapp.scope import resolve_scope

ME, HER = "me@example.com", "her@example.com"


@pytest.fixture
async def real_db(tmp_path) -> AsyncIterator[DatabaseManager]:
    db = DatabaseManager(str(tmp_path / "scope.db"))
    await db.connect()
    await db.ensure_schema()
    await db.bootstrap(ME, "default")
    await db.add_user(HER)
    await db.add_account("hers", HER, session_name_for("hers"))
    state.db = db
    state.registry = BridgeRegistry(db, "http://waha:3000", "key")
    yield db
    await state.registry.close()
    await db.close()
    state.db = None
    state.registry = None


def _as(monkeypatch, email: str | None) -> None:
    token = SimpleNamespace(claims={"email": email}) if email else None
    monkeypatch.setattr(scope_module, "get_access_token", lambda: token)


async def test_single_account_is_implicit(real_db, monkeypatch):
    _as(monkeypatch, HER)
    scope = await resolve_scope(None)
    assert scope.account_id == "hers" and scope.viewer == HER


async def test_several_accounts_require_an_explicit_choice(real_db, monkeypatch):
    await real_db.grant_account("default", HER)
    _as(monkeypatch, HER)
    with pytest.raises(ToolError, match="pass account"):
        await resolve_scope(None)
    assert (await resolve_scope("default")).account_id == "default"


async def test_account_you_do_not_hold_looks_like_it_does_not_exist(real_db, monkeypatch):
    _as(monkeypatch, HER)
    with pytest.raises(ToolError, match="Unknown account 'default'"):
        await resolve_scope("default")
    with pytest.raises(ToolError, match="Unknown account 'nope'"):
        await resolve_scope("nope")


async def test_unauthenticated_caller_is_refused(real_db, monkeypatch):
    _as(monkeypatch, None)
    monkeypatch.delenv("BOTSAPP_ADMIN_EMAIL", raising=False)
    with pytest.raises(ToolError, match="Not authenticated"):
        await resolve_scope(None)


async def test_each_viewer_sees_only_their_own_allowlist(real_db, monkeypatch):
    await real_db.grant_account("default", HER)
    await real_db.set_chat_access_allowed("default", ME, "mine@g.us", True, acting_email=ME)
    await real_db.set_chat_access_allowed("default", HER, "shared@g.us", True, acting_email=ME)

    _as(monkeypatch, HER)
    her = await resolve_scope("default")
    assert await her.db.list_allowed_jids() == {"shared@g.us"}

    inner = AsyncMock()
    inner.get_chats.return_value = [{"jid": "mine@g.us"}, {"jid": "shared@g.us"}]
    inner.with_last_messages = AsyncMock(side_effect=lambda chats: chats)
    from botsapp.message_store import AllowlistedMessageStore

    store = AllowlistedMessageStore(inner, her.db)
    assert await store.get_chats() == [{"jid": "shared@g.us"}]
    with pytest.raises(ChatNotAllowedError):
        await store.list_messages("mine@g.us")


async def test_revoked_viewer_loses_the_account_immediately(real_db, monkeypatch):
    await real_db.grant_account("default", HER)
    await real_db.set_chat_access_allowed("default", HER, "shared@g.us", True, acting_email=ME)
    _as(monkeypatch, HER)
    held = await resolve_scope("default")
    await real_db.revoke_account("default", HER)
    # A scope resolved before the revoke stops yielding chats too.
    assert await held.db.list_allowed_jids() == set()
    with pytest.raises(PermissionError):
        await held.db.set_chat_allowed("shared@g.us", True)
    with pytest.raises(ToolError):
        await resolve_scope("default")


# ── scoped WAHA keys (0027) ──────────────────────────────────────────────────


async def test_scope_resolution_asks_for_the_mcp_key_not_the_admin_key(real_db, monkeypatch):
    _as(monkeypatch, HER)
    seen = {}
    real = state.registry.for_account

    async def spy(account_id, *, purpose):
        seen["purpose"] = purpose
        return await real(account_id, purpose=purpose)

    monkeypatch.setattr(state.registry, "for_account", spy)
    await resolve_scope(None)
    assert seen == {"purpose": "mcp"}


async def test_a_missing_scoped_key_reads_as_not_ready_without_internals(
    real_db, monkeypatch, caplog
):
    from botsapp.bridge_registry import BridgeRegistry

    await state.registry.close()
    state.registry = BridgeRegistry(real_db, "http://waha:3000", "ADMIN", require_scoped_keys=True)
    _as(monkeypatch, HER)
    with pytest.raises(ToolError) as excinfo:
        await resolve_scope(None)
    message = str(excinfo.value)
    assert "isn't ready" in message and "administrator" in message
    assert "scoped" not in message.lower() and "ADMIN" not in message and "acct-" not in message


# ── per-account check against the tool's required WAHA actions (#54) ─────────


async def _keyed_registry(real_db, **keys: dict[str, bool]) -> None:
    """Give accounts real (encrypted) keys with the given actions, and a registry
    that can open them."""
    from cryptography.fernet import Fernet

    from botsapp.bridge_registry import BridgeRegistry
    from botsapp.secretbox import SecretBox

    box = SecretBox([Fernet.generate_key().decode()])
    for account_id, actions in keys.items():
        await real_db.set_account_key(account_id, f"key_{account_id}", box.encrypt("wk_s"), actions)
    await state.registry.close()
    state.registry = BridgeRegistry(real_db, "http://waha:3000", "ADMIN", box=box)


@pytest.fixture
def needs_read():
    from botsapp.capabilities import required_actions

    token = required_actions.set(frozenset({"read"}))
    yield
    required_actions.reset(token)


async def test_the_account_actually_used_must_allow_what_the_tool_needs(
    real_db, monkeypatch, needs_read
):
    await real_db.grant_account("default", HER)
    await _keyed_registry(real_db, hers={"read": False}, default={"read": True})
    _as(monkeypatch, HER)
    with pytest.raises(ToolError, match="doesn't allow that"):
        await resolve_scope("hers")  # her own account's key can't read
    assert (await resolve_scope("default")).account_id == "default"


async def test_a_single_account_lacking_the_action_is_refused_without_naming_internals(
    real_db, monkeypatch, needs_read
):
    await _keyed_registry(real_db, hers={"read": False})
    _as(monkeypatch, HER)
    with pytest.raises(ToolError) as excinfo:
        await resolve_scope(None)
    assert "key_hers" not in str(excinfo.value) and "acct-" not in str(excinfo.value)


async def test_with_no_requirement_set_nothing_is_checked(real_db, monkeypatch):
    await _keyed_registry(real_db, hers={"read": False})
    _as(monkeypatch, HER)
    assert (await resolve_scope(None)).account_id == "hers"  # direct calls and local tools
