"""
Show MCP tools only when a WAHA key can serve them (docs/decisions/0027, #54).

FastMCP's own ``AuthMiddleware`` already runs its checks both when building
``tools/list`` and before a call, hiding what fails. This middleware adds one
more rule in the same two places, driven by the caller's accounts:

* **Listing** — a tool is presented only if at least one account the caller owns
  or was granted has a key allowing every WAHA action the tool's tags name
  (``capabilities.required_for``). The list has no ``account`` argument, so it is
  a union across the caller's accounts; with one account (the usual case) it is
  exact. A tool naming no WAHA action (``list_accounts`` and the tag tools, which
  never touch WhatsApp) is always presented.
* **Calling** — the same rule is enforced, and the tool's required actions are put
  in ``capabilities.required_actions`` so ``resolve_scope`` can refuse the
  *specific* account the call is about if its own key lacks them.

WAHA remains the enforcer: the stored actions are advisory (``KeyManager.verify``
refreshes them), so a stale copy can only make a tool look available and then fail
at WAHA, or hide one that would have worked; it can never widen access.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import mcp_types as mt
from fastmcp.exceptions import AuthorizationError, ToolError
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext

import botsapp.state as state
from botsapp.capabilities import allows, required_actions, required_for
from botsapp.scope import caller_email

logger = logging.getLogger(__name__)


def _is_stdio() -> bool:
    # Late import, as FastMCP's own AuthMiddleware does (circular import).
    from fastmcp.server.context import _current_transport

    return _current_transport.get() == "stdio"


async def _caller_accounts() -> list[dict[str, Any]]:
    """The caller's accounts; none if the caller can't be identified, so tools that
    need WAHA are withheld rather than shown to someone we can't place."""
    try:
        email = caller_email()
    except ToolError:
        return []
    assert state.db is not None
    return await state.db.list_accounts_for_user(email)


def _servable(tags: Any, accounts: list[dict[str, Any]]) -> bool:
    required = required_for(tags)
    return not required or any(allows(a, required) for a in accounts)


class WahaScopeMiddleware(Middleware):
    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Any]],
    ) -> Sequence[Any]:
        tools = await call_next(context)
        if _is_stdio():
            return tools
        accounts = await _caller_accounts()  # once per listing, not per tool
        return [t for t in tools if _servable(t.tags, accounts)]

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, Any],
    ) -> Any:
        if _is_stdio():
            return await call_next(context)
        name = context.message.name
        fastmcp = context.fastmcp_context
        if fastmcp is None:
            # Fail closed, as AuthMiddleware does.
            raise AuthorizationError(f"Authorization failed for tool '{name}': missing context")
        tool = await fastmcp.fastmcp.get_tool(name)
        if tool is None:
            raise AuthorizationError(
                f"Authorization failed for tool '{name}': not found or not authorized"
            )
        required = required_for(tool.tags)
        if required and not _servable(tool.tags, await _caller_accounts()):
            raise AuthorizationError(
                f"Authorization failed for tool '{name}': no account you can use allows it"
            )
        token = required_actions.set(required)
        try:
            return await call_next(context)
        finally:
            required_actions.reset(token)
