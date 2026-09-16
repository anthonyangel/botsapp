"""
Admin UI Starlette app.

Runs as its own process (botsapp.admin_main:app), independent of the
public MCP server (botsapp.main:app) — they share only the SQLite
chat_metadata database and WAHA's own storage, never a Python process. No
auth of its own: network-private reachability is the only gate. Never give
this app a public Fly service / public docker-compose port binding.

Uses state.bridge + the raw MessageStore from bridge_factory, same as the
MCP server. The active provider is shown in the page header so it's never
ambiguous which one you're looking at.
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from starlette.applications import Starlette
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

import botsapp.state as state
from botsapp.admin import templates
from botsapp.bridge_factory import bridge_provider_name, build_bridge_and_store
from botsapp.db import DatabaseManager
from botsapp.message_store import MessageStore
from botsapp.session import connect_or_get_status, get_status

logger = logging.getLogger(__name__)

# The *unfiltered* store — deliberately not AllowlistedMessageStore, since
# this UI's whole purpose is to see (and allow) chats that aren't allowed
# yet. Set during the app's own lifespan; this process never touches the
# MCP server's state.message_store.
_raw_store: MessageStore | None = None

STATUS_BROADCAST_JID = "status@broadcast"


@asynccontextmanager
async def lifespan(_app: Starlette) -> AsyncIterator[None]:
    global _raw_store
    state.db = DatabaseManager(os.environ["DATABASE_URL"])  # a SQLite file path
    await state.db.connect()
    await state.db.ensure_schema()

    bridge, raw_store = build_bridge_and_store()
    state.bridge = bridge
    _raw_store = raw_store
    logger.info("Admin UI ready (bridge: %s)", bridge_provider_name())

    yield

    await state.bridge.close()
    await state.db.close()
    state.db = None
    state.bridge = None
    _raw_store = None


def _classify(jid: str) -> str:
    """Coarse WhatsApp JID category, for splitting the admin UI into tabs."""
    if jid == STATUS_BROADCAST_JID:
        return "status"
    if jid.endswith("@g.us"):
        return "group"
    return "direct"  # @s.whatsapp.net, @lid (privacy-preserving IDs), etc.


async def _poll_status() -> dict[str, Any]:
    """Status only — never triggers a connect. Calling the bridge's connect
    on every page view/poll was the actual bug behind "QR linking isn't
    working": each reload issued a fresh QR and killed the bridge's channel
    for whichever QR was already mid-scan. get_status() is safe to call as
    often as we like; connect_or_get_status() (which does call connect) is
    reserved for the explicit "Connect WhatsApp" button below."""
    assert state.bridge is not None
    status = await get_status(state.bridge)
    return asdict(status)


async def session_index(_request: Request) -> Response:
    status = await _poll_status()
    return HTMLResponse(templates.session_page(status, bridge_provider_name()))


async def session_status_json(_request: Request) -> Response:
    """Polled by the QR-waiting page client-side (see templates.session_page)
    to refresh the QR/connected state in place, without reloading / and
    without ever re-triggering connect."""
    return JSONResponse(await _poll_status())


async def session_connect(_request: Request) -> Response:
    """The only route that actually calls the bridge's connect — a
    deliberate user action (button click), not an automatic side effect of
    viewing or polling the session page."""
    assert state.bridge is not None
    await connect_or_get_status(state.bridge)
    return RedirectResponse(url="/", status_code=303)


async def chats_index(_request: Request) -> Response:
    assert state.bridge is not None
    assert state.db is not None
    assert _raw_store is not None

    session_status = await get_status(state.bridge)
    if not session_status.logged_in:
        return HTMLResponse(templates.not_connected_page(bridge_provider_name()))

    all_chats = await _raw_store.get_chats(limit=1000, offset=0)
    direct_chats = [c for c in all_chats if _classify(c["jid"]) == "direct"]
    group_jids_with_history = {c["jid"] for c in all_chats if _classify(c["jid"]) == "group"}
    status_seen = any(_classify(c["jid"]) == "status" for c in all_chats)

    # ── Direct chats: enrich with the bridge's own contact names + avatars ─
    try:
        contacts = {c.jid: c for c in await state.bridge.get_contacts()}
    except Exception as exc:
        logger.warning("get_contacts failed while building admin chat list: %s", exc)
        contacts = {}

    # Pass the full jid, not just the number — a bridge needs the suffix
    # to tell an @lid (privacy-preserving id) contact from a plain phone
    # number; each bridge's own get_avatar_url translates it as needed.
    direct_avatars = await asyncio.gather(
        *(state.bridge.get_avatar_url(c["jid"]) for c in direct_chats)
    )
    direct_rows: list[dict[str, Any]] = []
    for chat, avatar in zip(direct_chats, direct_avatars, strict=True):
        jid = chat["jid"]
        contact = contacts.get(jid)
        display_name = (contact.name if contact else None) or chat.get("name") or jid
        meta = await state.db.get_chat_metadata(jid=jid) or {}
        direct_rows.append(
            {
                "jid": jid,
                "name": display_name,
                "avatar_url": avatar,
                "is_allowed": bool(meta.get("is_allowed")),
                "tags": meta.get("tags") or [],
            }
        )

    # ── Groups: source from the bridge's live group list, not just chats
    # with history, so a group with zero messages so far can still be
    # allowed ──────────────────────────────────────────────────────────────
    try:
        groups = await state.bridge.list_groups()
    except Exception as exc:
        logger.warning("list_groups failed while building admin group list: %s", exc)
        groups = []

    group_avatars = await asyncio.gather(*(state.bridge.get_avatar_url(g.jid) for g in groups))
    group_rows: list[dict[str, Any]] = []
    for g, avatar in zip(groups, group_avatars, strict=True):
        meta = await state.db.get_chat_metadata(jid=g.jid) or {}
        group_rows.append(
            {
                "jid": g.jid,
                "name": g.name or g.jid,
                "topic": g.topic,
                "participant_count": g.participant_count,
                "is_community": g.is_community,
                "has_history": g.jid in group_jids_with_history,
                "avatar_url": avatar,
                "is_allowed": bool(meta.get("is_allowed")),
                "tags": meta.get("tags") or [],
            }
        )

    status_row = None
    if status_seen:
        meta = await state.db.get_chat_metadata(jid=STATUS_BROADCAST_JID) or {}
        status_row = {
            "jid": STATUS_BROADCAST_JID,
            "is_allowed": bool(meta.get("is_allowed")),
            "tags": meta.get("tags") or [],
        }

    return HTMLResponse(
        templates.browse_page(direct_rows, group_rows, status_row, bridge_provider_name())
    )


_PANEL_BY_CLASS = {"group": "groups-panel", "status": "status-panel", "direct": "direct-panel"}


def _redirect_to_tab(jid: str) -> RedirectResponse:
    """Redirect back to /chats on the tab the acted-on jid actually lives
    in, not always the first (Chats) tab — otherwise ticking a checkbox on
    the Groups tab flips the page back to Chats, discarding where you were.
    browse_page()'s inline script reads the #fragment on load and activates
    the matching tab."""
    panel = _PANEL_BY_CLASS[_classify(jid)]
    return RedirectResponse(url=f"/chats#{panel}", status_code=303)


async def toggle_allowed(request: Request) -> Response:
    assert state.db is not None
    jid = request.path_params["jid"]
    form = await request.form()
    is_allowed = form.get("is_allowed") == "on"
    await state.db.set_chat_allowed(jid=jid, is_allowed=is_allowed)
    return _redirect_to_tab(jid)


def _form_str(form: FormData, key: str) -> str:
    """Text field as a plain string. These forms have no file inputs, but
    FormData.get()'s return type includes UploadFile — guard against it
    rather than assuming."""
    value = form.get(key)
    return value if isinstance(value, str) else ""


async def save_metadata(request: Request) -> Response:
    assert state.db is not None
    jid = request.path_params["jid"]
    form = await request.form()
    tags = [t.strip() for t in _form_str(form, "tags").split(",") if t.strip()]
    await state.db.upsert_chat_metadata(jid=jid, tags=tags)
    return _redirect_to_tab(jid)


async def access_index(_request: Request) -> Response:
    assert state.db is not None
    emails = await state.db.list_allowed_emails()
    return HTMLResponse(templates.access_page(emails, bridge_provider_name()))


async def add_allowed_email(request: Request) -> Response:
    assert state.db is not None
    form = await request.form()
    email = _form_str(form, "email").strip()
    if email:
        await state.db.add_allowed_email(email)
    return RedirectResponse(url="/access", status_code=303)


async def remove_allowed_email(request: Request) -> Response:
    assert state.db is not None
    await state.db.remove_allowed_email(request.path_params["email"])
    return RedirectResponse(url="/access", status_code=303)


app = Starlette(
    routes=[
        Route("/", session_index),
        Route("/session/status.json", session_status_json),
        Route("/session/connect", session_connect, methods=["POST"]),
        Route("/chats", chats_index),
        Route("/chats/{jid}/allow", toggle_allowed, methods=["POST"]),
        Route("/chats/{jid}/metadata", save_metadata, methods=["POST"]),
        Route("/access", access_index),
        Route("/access/add", add_allowed_email, methods=["POST"]),
        Route("/access/{email}/remove", remove_allowed_email, methods=["POST"]),
    ],
    lifespan=lifespan,
)
