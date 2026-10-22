"""Tools are shown only when a WAHA key can serve them (0027, #54)."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import AuthorizationError, ToolError

import botsapp.state as state
from botsapp.app import OUTBOUND_TAG, mcp
from botsapp.capabilities import (
    account_actions,
    allows,
    missing,
    required_actions,
    required_for,
)
from botsapp.tool_scopes import WahaScopeMiddleware
from botsapp.waha_keys import WAHA_ACTIONS

READ_ONLY = {
    "read": True,
    "send": False,
    "control": False,
    "setting": False,
    "app": False,
    "delete": False,
}
NO_READ = {**READ_ONLY, "read": False}


def _tool(name: str, *tags: str):
    return SimpleNamespace(name=name, tags=set(tags))


READ_TOOL = _tool("list_chats", "read", "chat")
SEND_TOOL = _tool("send_message", "send", "chat", OUTBOUND_TAG)
LOCAL_TOOL = _tool("list_accounts", "account", "local")


def _keyed(account_id: str, actions: dict[str, bool]) -> dict:
    return {"id": account_id, "waha_key_id": f"key_{account_id}", "waha_key_actions": actions}


def _unkeyed(account_id: str) -> dict:
    return {"id": account_id, "waha_key_id": None, "waha_key_actions": None}


# ── capabilities ─────────────────────────────────────────────────────────────


def test_only_wahas_own_labels_count_as_requirements():
    assert required_for({"read", "chat", "group"}) == {"read"}
    assert required_for({"account", "local", "metadata"}) == frozenset()
    assert required_for({"Read", "READ"}) == frozenset()  # exact labels only
    assert required_for(set(WAHA_ACTIONS)) == frozenset(WAHA_ACTIONS)


def test_a_keyed_account_has_exactly_its_stored_actions():
    assert account_actions(_keyed("a", READ_ONLY), strict=False) == {"read"}
    assert allows(_keyed("a", READ_ONLY), frozenset({"read"}), strict=True)
    assert not allows(_keyed("a", READ_ONLY), frozenset({"send"}), strict=False)
    assert missing(_keyed("a", READ_ONLY), frozenset({"read", "send"}), strict=False) == ["send"]


def test_an_unkeyed_account_uses_the_admin_key_unless_keys_are_required():
    assert account_actions(_unkeyed("a"), strict=False) == frozenset(WAHA_ACTIONS)
    assert account_actions(_unkeyed("a"), strict=True) == frozenset()


def test_a_key_row_with_no_recorded_actions_allows_nothing():
    assert (
        account_actions({"waha_key_id": "k", "waha_key_actions": None}, strict=False) == frozenset()
    )


# ── listing ──────────────────────────────────────────────────────────────────


@pytest.fixture
def middleware():
    return WahaScopeMiddleware()


@pytest.fixture
def caller(monkeypatch, mock_db):
    """Sign the caller in with the given accounts."""

    def sign_in(*accounts: dict) -> None:
        monkeypatch.setattr("botsapp.tool_scopes.caller_email", lambda: "me@example.com")
        mock_db.list_accounts_for_user = AsyncMock(return_value=list(accounts))
        monkeypatch.setattr(state, "db", mock_db)

    return sign_in


@pytest.fixture(autouse=True)
def _not_stdio(monkeypatch):
    monkeypatch.setattr("botsapp.tool_scopes._is_stdio", lambda: False)
    monkeypatch.delenv("WAHA_REQUIRE_SCOPED_KEYS", raising=False)


async def _listed(middleware: WahaScopeMiddleware, *tools) -> list[str]:
    context: Any = SimpleNamespace()
    call_next: Any = AsyncMock(return_value=list(tools))
    return [t.name for t in await middleware.on_list_tools(context, call_next)]


async def test_a_read_only_key_shows_read_tools_and_hides_send(middleware, caller):
    caller(_keyed("a", READ_ONLY))
    assert await _listed(middleware, READ_TOOL, SEND_TOOL, LOCAL_TOOL) == [
        "list_chats",
        "list_accounts",
    ]


async def test_an_account_whose_key_lacks_read_presents_no_read_tools_but_keeps_local_ones(
    middleware, caller
):
    caller(_keyed("a", NO_READ))
    assert await _listed(middleware, READ_TOOL, LOCAL_TOOL) == ["list_accounts"]


async def test_the_listing_is_a_union_across_the_callers_accounts(middleware, caller):
    caller(_keyed("a", NO_READ), _keyed("b", READ_ONLY))
    assert await _listed(middleware, READ_TOOL) == ["list_chats"]


async def test_no_accounts_means_no_whatsapp_tools(middleware, caller):
    caller()
    assert await _listed(middleware, READ_TOOL, LOCAL_TOOL) == ["list_accounts"]


async def test_an_unkeyed_account_is_served_by_the_admin_key_in_the_transition(middleware, caller):
    caller(_unkeyed("a"))
    assert await _listed(middleware, READ_TOOL) == ["list_chats"]


async def test_an_unkeyed_account_has_nothing_when_keys_are_required(
    middleware, caller, monkeypatch
):
    monkeypatch.setenv("WAHA_REQUIRE_SCOPED_KEYS", "true")
    caller(_unkeyed("a"))
    assert await _listed(middleware, READ_TOOL, LOCAL_TOOL) == ["list_accounts"]


async def test_a_caller_who_cannot_be_identified_gets_only_local_tools(
    middleware, monkeypatch, mock_db
):
    def nobody():
        raise ToolError("Not authenticated.")

    monkeypatch.setattr("botsapp.tool_scopes.caller_email", nobody)
    monkeypatch.setattr(state, "db", mock_db)
    assert await _listed(middleware, READ_TOOL, LOCAL_TOOL) == ["list_accounts"]


async def test_the_accounts_are_looked_up_once_per_listing_not_per_tool(
    middleware, caller, mock_db
):
    caller(_keyed("a", READ_ONLY))
    await _listed(middleware, READ_TOOL, SEND_TOOL, LOCAL_TOOL, _tool("x", "read"))
    mock_db.list_accounts_for_user.assert_awaited_once()


async def test_stdio_is_left_alone_like_fastmcps_own_auth(middleware, caller, monkeypatch):
    monkeypatch.setattr("botsapp.tool_scopes._is_stdio", lambda: True)
    caller()
    assert await _listed(middleware, READ_TOOL, SEND_TOOL) == ["list_chats", "send_message"]


# ── calling ──────────────────────────────────────────────────────────────────


def _call_context(tool):
    return SimpleNamespace(
        message=SimpleNamespace(name=tool.name if tool else "ghost"),
        fastmcp_context=SimpleNamespace(
            fastmcp=SimpleNamespace(get_tool=AsyncMock(return_value=tool))
        ),
    )


async def test_a_call_to_a_tool_no_account_can_serve_is_refused(middleware, caller):
    caller(_keyed("a", NO_READ))
    call_next = AsyncMock()
    with pytest.raises(AuthorizationError, match="no account you can use allows it"):
        await middleware.on_call_tool(_call_context(READ_TOOL), call_next)
    call_next.assert_not_awaited()


async def test_a_servable_call_sees_the_required_actions_and_they_are_reset_after(
    middleware, caller
):
    caller(_keyed("a", READ_ONLY))
    seen = []

    async def call_next(_ctx):
        seen.append(required_actions.get())
        return "ok"

    assert await middleware.on_call_tool(_call_context(READ_TOOL), call_next) == "ok"
    assert seen == [frozenset({"read"})]
    assert required_actions.get() == frozenset()


async def test_the_actions_are_reset_even_when_the_tool_fails(middleware, caller):
    caller(_keyed("a", READ_ONLY))

    async def call_next(_ctx):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await middleware.on_call_tool(_call_context(READ_TOOL), call_next)
    assert required_actions.get() == frozenset()


async def test_local_tools_are_always_callable(middleware, caller):
    caller()
    assert (
        await middleware.on_call_tool(_call_context(LOCAL_TOOL), AsyncMock(return_value="ok"))
        == "ok"
    )


async def test_unknown_tool_and_missing_context_fail_closed(middleware, caller):
    caller(_keyed("a", READ_ONLY))
    with pytest.raises(AuthorizationError, match="not found or not authorized"):
        await middleware.on_call_tool(_call_context(None), AsyncMock())
    with pytest.raises(AuthorizationError, match="missing context"):
        await middleware.on_call_tool(
            SimpleNamespace(message=SimpleNamespace(name="x"), fastmcp_context=None), AsyncMock()
        )


# ── the real tools ───────────────────────────────────────────────────────────

LOCAL_ONLY = {"list_accounts", "add_tag", "remove_tag", "clear_tags", "get_chat_metadata"}


async def _real_tools():
    return await mcp.list_tools(run_middleware=False)


async def test_every_reachable_whatsapp_tool_declares_the_waha_action_it_needs():
    for tool in await _real_tools():
        if OUTBOUND_TAG in tool.tags:
            continue  # permanently blocked, whatever it is labelled
        if tool.name in LOCAL_ONLY:
            continue
        assert required_for(tool.tags), f"{tool.name} calls WAHA but names no WAHA action"


async def test_local_only_tools_name_no_waha_action():
    tools = {t.name: t for t in await _real_tools()}
    assert LOCAL_ONLY <= set(tools)
    for name in LOCAL_ONLY:
        assert not required_for(tools[name].tags), name
        assert "local" in tools[name].tags, name


async def test_everything_labelled_send_is_permanently_blocked():
    for tool in await _real_tools():
        if "send" in tool.tags:
            assert OUTBOUND_TAG in tool.tags, tool.name


async def test_reachable_read_tools_need_only_read():
    for tool in await _real_tools():
        if OUTBOUND_TAG not in tool.tags and tool.name not in LOCAL_ONLY:
            assert required_for(tool.tags) == {"read"}, tool.name


def test_the_middleware_is_installed_after_fastmcps_auth():
    names = [type(m).__name__ for m in mcp.middleware]
    assert names.index("AuthMiddleware") < names.index("WahaScopeMiddleware")


# ── inside a real FastMCP server ─────────────────────────────────────────────


async def test_the_hooks_fire_in_a_real_server_and_the_actions_reach_the_tool(caller):
    from fastmcp import Client, FastMCP

    server = FastMCP("scoped", middleware=[WahaScopeMiddleware()])

    @server.tool(tags={"read"})
    def reads() -> list[str]:
        return sorted(required_actions.get())

    @server.tool(tags={"send"})
    def sends() -> str:
        return "sent"

    @server.tool(tags={"local"})
    def local() -> list[str]:
        return sorted(required_actions.get())

    caller(_keyed("a", READ_ONLY))
    async with Client(server) as client:
        assert {t.name for t in await client.list_tools()} == {"reads", "local"}
        assert (await client.call_tool("reads", {})).data == ["read"]
        assert (await client.call_tool("local", {})).data == []
        with pytest.raises(ToolError):
            await client.call_tool("sends", {})  # hidden tools can't be called either
