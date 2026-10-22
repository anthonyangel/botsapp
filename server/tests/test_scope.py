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
    await real_db.set_chat_access_allowed("default", ME, "mine@g.us", True)
    await real_db.set_chat_access_allowed("default", HER, "shared@g.us", True)

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
    await real_db.set_chat_access_allowed("default", HER, "shared@g.us", True)
    _as(monkeypatch, HER)
    held = await resolve_scope("default")
    await real_db.revoke_account("default", HER)
    # A scope resolved before the revoke stops yielding chats too.
    assert await held.db.list_allowed_jids() == set()
    with pytest.raises(PermissionError):
        await held.db.set_chat_allowed("shared@g.us", True)
    with pytest.raises(ToolError):
        await resolve_scope("default")
