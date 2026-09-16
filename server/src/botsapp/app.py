"""
FastMCP application instance and lifespan.

Kept in its own module so tools.py can import `mcp` for @mcp.tool()
decorators without creating a circular dependency with main.py.
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.exceptions import AuthorizationError
from fastmcp.server.auth import AuthContext, restrict_tag
from fastmcp.server.auth.providers.workos import AuthKitProvider
from fastmcp.server.middleware import AuthMiddleware
from mcp.types import Icon

import botsapp.state as state
from botsapp.bridge_factory import bridge_provider_name, build_bridge_and_store
from botsapp.db import DatabaseManager
from botsapp.message_store import AllowlistedMessageStore

logger = logging.getLogger(__name__)

# No AuthKit token will ever carry this scope, so restrict_tag below makes
# every "outbound"-tagged tool permanently unreachable — enforced by the
# server itself, not by remembering which tools to register.
OUTBOUND_TAG = "outbound"
_UNGRANTABLE_SCOPE = "whatsapp:send"

# Skips AuthKit login for local dev only. Does NOT relax the outbound-tag
# restriction — sending/mutating tools stay blocked either way. Never set
# this anywhere the server is reachable by anyone but you: with it on,
# ALLOWED_EMAILS is not enforced either.
LOCAL_DEV_DISABLE_AUTH = os.getenv("LOCAL_DEV_DISABLE_AUTH", "").strip().lower() in (
    "1",
    "true",
    "yes",
)


async def allowed_family_email(ctx: AuthContext) -> bool:
    """Global auth check: only emails in the admin UI's email allowlist may
    call anything. Stored in the shared SQLite DB (state.db), not an env
    var — see docs/decisions/0015-email-allowlist-in-db.md for why. fastmcp's
    AuthCheck accepts sync or async callables (it awaits the result either
    way), so this can query the DB directly rather than cache/env-parse."""
    token = ctx.token
    email = (token.claims.get("email") or "").lower() if token else None
    if not email or state.db is None or not await state.db.is_email_allowed(email):
        raise AuthorizationError(
            f"Access denied: {email or 'unauthenticated caller'} is not on the email allowlist"
        )
    return True


def _build_auth() -> AuthKitProvider | None:
    if LOCAL_DEV_DISABLE_AUTH:
        logger.warning(
            "LOCAL_DEV_DISABLE_AUTH is set — running with NO login. Every tool is "
            "reachable by anyone who can reach this server, allowlisted chats included. "
            "Never set this outside local development."
        )
        return None
    authkit_domain = os.environ["WORKOS_AUTHKIT_DOMAIN"]
    # No custom token_verifier needed: a WorkOS JWT Template stamps `email`
    # onto the access token itself, so AuthKitProvider's default verifier —
    # a local, audience-bound JWT decode — is sufficient on its own.
    return AuthKitProvider(
        authkit_domain=authkit_domain,
        base_url=os.environ["MCP_BASE_URL"],
    )


def _build_auth_checks() -> list:
    # restrict_tag always applies, with or without LOCAL_DEV_DISABLE_AUTH.
    # allowed_family_email is the piece that's skipped — there's no caller
    # identity to check once login itself is disabled.
    checks = [restrict_tag(OUTBOUND_TAG, scopes=[_UNGRANTABLE_SCOPE])]
    if not LOCAL_DEV_DISABLE_AUTH:
        checks.insert(0, allowed_family_email)
    return checks


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
    state.db = DatabaseManager(os.environ["DATABASE_URL"])  # a SQLite file path
    await state.db.connect()
    await state.db.ensure_schema()

    # The admin UI talks to the raw store directly — it needs to see
    # not-yet-allowed chats in order to allow them. Every other consumer
    # goes through the allowlist wrapper.
    bridge, raw_store = build_bridge_and_store()
    state.bridge = bridge
    state.message_store = AllowlistedMessageStore(raw_store, state.db)
    logger.info("Bridge ready (%s)", bridge_provider_name())

    yield

    await state.bridge.close()
    await state.db.close()
    state.db = None
    state.message_store = None
    state.bridge = None
    logger.info("Shutdown complete")


# WhatsApp green chat bubble icon (SVG data URI)
_WHATSAPP_ICON = Icon(
    src=(
        "data:image/svg+xml;base64,"
        "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAy"
        "NCAyNCIgZmlsbD0iIzI1RDM2NiI+PHBhdGggZD0iTTEyIDJDNi40OCAyIDIgNi40OCAyIDEy"
        "YzAgMS44NS40NSAzLjU4IDEuMjMgNS4xMkwyIDIybDUuMTItMS4yM0MyMC41MiAyMi41NSAy"
        "MiAxNy41MiAyMiAxMiAyMiA2LjQ4IDE3LjUyIDIgMTIgMnptNS4yIDE0Ljg0Yy0uMjUuNy0x"
        "LjQ1IDEuMjgtMi0xLjM4LS41NS0uMS0xLjI0LS41Mi0yLjQzLTEuMDMtMS40NC0uNjEtMi4z"
        "OS0xLjUtMy4zNC0yLjYtLjk1LTEuMS0uNS0yLjQ3LjA5LTMuMjkuNTktLjgyIDEuNDMtLjY4"
        "IDEuNjMtLjQ5LjIuMTkuNi44NCAxLjIyIDEuMzguNjIuNTQuNzguODguNTIgMS4wNy0uMjYu"
        "MTktLjk4LjI0LTEuMjQuMDhzLS41LS4yNy0uNzQtLjUxbC0uNTUtLjQ0cy41MyAxIDEuMjcg"
        "MS44NGMuNzMuODMgMS41OCAxLjMzIDIuNDQgMS43MS44Ni4zOCAxLjUuMzIgMS45Mi4wNS40"
        "Mi0uMjcuNjUtLjg1Ljc2LTEuMjcuMTEtLjQyLS4xOC0uNzItLjM4LS44NnoiLz48L3N2Zz4="
    ),
    mime_type="image/svg+xml",
)

mcp = FastMCP(
    "whatsapp",
    lifespan=lifespan,
    icons=[_WHATSAPP_ICON],
    auth=_build_auth(),
    middleware=[AuthMiddleware(auth=_build_auth_checks())],
)
