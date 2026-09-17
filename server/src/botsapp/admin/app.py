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

import phonenumbers
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
from botsapp.waha_client import WAHAClient

logger = logging.getLogger(__name__)

# The *unfiltered* store — deliberately not AllowlistedMessageStore, since
# this UI's whole purpose is to see (and allow) chats that aren't allowed
# yet. Set during the app's own lifespan; this process never touches the
# MCP server's state.message_store.
_raw_store: MessageStore | None = None

STATUS_BROADCAST_JID = "status@broadcast"

# A community is just a group whose GroupInfo.is_community is set — jid
# pattern alone can't tell them apart (both end "@g.us"), so _classify()
# can't do this split on its own. Populated fresh on every chats_index()
# load; consulted (best-effort — a stale/empty set just falls back to the
# Groups tab) by _redirect_to_tab() below, which only ever gets a bare jid
# from the allow/tags POST routes, not the GroupInfo that classified it.
_community_jids: set[str] = set()


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
    if jid.endswith("@newsletter"):
        return "newsletter"
    return "direct"  # @s.whatsapp.net, @lid (privacy-preserving IDs), etc.


def _country_flag(jid: str) -> str:
    """Best-effort "🇮🇱 +972" label for a direct chat's jid, derived from its
    E.164 phone number via the same calling-code table libphonenumber uses
    (no network call, no API key). A flag emoji is just two Unicode
    "regional indicator" characters offset from the ISO 3166-1 alpha-2
    region code's letters — no separate emoji table needed.

    Falls back to "" for anything libphonenumber can't resolve to a region
    (an @lid contact never resolved to a phone jid, a non-phone jid, or a
    calling code libphonenumber doesn't recognize) — groups/newsletters/
    status don't have a single phone number at all, so callers should only
    use this for "direct" chats.
    """
    phone = jid.split("@", 1)[0]
    if not phone.isdigit():
        return ""
    try:
        parsed = phonenumbers.parse(f"+{phone}", None)
        region = phonenumbers.region_code_for_number(parsed)
    except phonenumbers.NumberParseException:
        return ""
    if not region or region == "ZZ":  # ZZ = libphonenumber's "unknown region"
        return ""
    flag = "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in region)
    return f"{flag} +{phonenumbers.country_code_for_region(region)}"


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


async def session_disconnect(_request: Request) -> Response:
    """Log out of WhatsApp (WAHA logout endpoint). Leaves the session in
    STOPPED state so the user can immediately hit Connect WhatsApp and
    re-pair with a fresh QR."""
    assert state.bridge is not None
    await state.bridge.disconnect()
    return RedirectResponse(url="/", status_code=303)


async def session_delete(_request: Request) -> Response:
    """Nuclear option: delete the WAHA session entirely (removes all
    persisted auth material). Use this when logout alone isn't enough to
    escape a stuck WORKING state. After deletion, Connect WhatsApp will
    create a fresh session and show a new QR code."""
    assert state.bridge is not None
    if isinstance(state.bridge, WAHAClient):
        try:
            await state.bridge._request("DELETE", f"/api/sessions/{state.bridge._session_name}")
        except Exception as exc:
            logger.warning("session delete failed: %s", exc)
    else:
        # Non-WAHA bridge: just try a normal logout.
        await state.bridge.disconnect()
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
    newsletter_chats = [c for c in all_chats if _classify(c["jid"]) == "newsletter"]
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
                "country_flag": _country_flag(jid),
                "is_allowed": bool(meta.get("is_allowed")),
                "tags": meta.get("tags") or [],
            }
        )

    # ── Newsletters (WhatsApp Channels, @newsletter jids): their own tab,
    # not lumped into Chats — a channel broadcast isn't a conversation with
    # a person, same reasoning as the Status tab below. Not in
    # get_contacts() (that's phone-number contacts only), so no
    # contact-name enrichment — WAHA's own chat "name" (the channel name)
    # is already the right display name ────────────────────────────────────
    newsletter_avatars = await asyncio.gather(
        *(state.bridge.get_avatar_url(c["jid"]) for c in newsletter_chats)
    )
    newsletter_rows: list[dict[str, Any]] = []
    for chat, avatar in zip(newsletter_chats, newsletter_avatars, strict=True):
        jid = chat["jid"]
        meta = await state.db.get_chat_metadata(jid=jid) or {}
        newsletter_rows.append(
            {
                "jid": jid,
                "name": chat.get("name") or jid,
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

    # Every real group jid ends "@g.us" (see _classify), so the chat list
    # itself — independent of whatever the bridge's dedicated groups
    # endpoint returns — is a second, authoritative source of "how many
    # groups this account is actually in". The two can disagree: WAHA's
    # NOWEB engine (confirmed live) only returns full metadata for groups
    # its own store has synced, which can lag well behind the chat list —
    # e.g. 42 of 159 known groups after a fresh re-pair. group_count_total
    # unions both sources so a group WAHA hasn't caught up on yet is still
    # counted (even though it can't render a row without that metadata),
    # and the gap is surfaced in the UI instead of silently under-showing.
    # Counts *all* @g.us jids (communities included) — the gap isn't
    # specific to either kind.
    group_count_loaded = len(groups)
    group_count_total = len(group_jids_with_history | {g.jid for g in groups})

    # A WhatsApp Community is structurally a group (jid also ends "@g.us")
    # but its own "participants" list is just the admin(s)/owner of the
    # community shell, not real membership (confirmed live: every
    # isCommunity=true group here has a participant_count in the
    # single-digits, same as WhatsApp's own "size" field) — actual members
    # live in the sub-groups underneath, which already appear here as
    # their own ordinary groups. Split out to its own tab, both so it
    # isn't confused with a small group and so the (meaningless for a
    # community) member count doesn't have to be shown at all.
    community_groups = [g for g in groups if g.is_community]
    regular_groups = [g for g in groups if not g.is_community]
    _community_jids.clear()
    _community_jids.update(g.jid for g in community_groups)

    group_avatars = await asyncio.gather(
        *(state.bridge.get_avatar_url(g.jid) for g in regular_groups)
    )
    group_rows: list[dict[str, Any]] = []
    for g, avatar in zip(regular_groups, group_avatars, strict=True):
        meta = await state.db.get_chat_metadata(jid=g.jid) or {}
        group_rows.append(
            {
                "jid": g.jid,
                "name": g.name or g.jid,
                "topic": g.topic,
                "participant_count": g.participant_count,
                "has_history": g.jid in group_jids_with_history,
                "avatar_url": avatar,
                "is_allowed": bool(meta.get("is_allowed")),
                "tags": meta.get("tags") or [],
            }
        )

    community_avatars = await asyncio.gather(
        *(state.bridge.get_avatar_url(g.jid) for g in community_groups)
    )
    community_rows: list[dict[str, Any]] = []
    for g, avatar in zip(community_groups, community_avatars, strict=True):
        meta = await state.db.get_chat_metadata(jid=g.jid) or {}
        community_rows.append(
            {
                "jid": g.jid,
                "name": g.name or g.jid,
                "topic": g.topic,
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
        templates.browse_page(
            direct_rows,
            group_rows,
            community_rows,
            newsletter_rows,
            status_row,
            bridge_provider_name(),
            group_count_loaded=group_count_loaded,
            group_count_total=group_count_total,
        )
    )


_PANEL_BY_CLASS = {
    "group": "groups-panel",
    "community": "community-panel",
    "status": "status-panel",
    "direct": "direct-panel",
    "newsletter": "newsletter-panel",
}


def _redirect_to_tab(jid: str) -> RedirectResponse:
    """Redirect back to /chats on the tab the acted-on jid actually lives
    in, not always the first (Chats) tab — otherwise ticking a checkbox on
    the Groups tab flips the page back to Chats, discarding where you were.
    browse_page()'s inline script reads the #fragment on load and activates
    the matching tab.

    _classify() can't tell a community from an ordinary group on jid
    alone (see _community_jids) — check that first, and only fall back to
    _classify() for jid's it doesn't (yet) know about.
    """
    panel_class = "community" if jid in _community_jids else _classify(jid)
    panel = _PANEL_BY_CLASS[panel_class]
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
        Route("/session/disconnect", session_disconnect, methods=["POST"]),
        Route("/session/delete", session_delete, methods=["POST"]),
        Route("/chats", chats_index),
        Route("/chats/{jid}/allow", toggle_allowed, methods=["POST"]),
        Route("/chats/{jid}/metadata", save_metadata, methods=["POST"]),
        Route("/access", access_index),
        Route("/access/add", add_allowed_email, methods=["POST"]),
        Route("/access/{email}/remove", remove_allowed_email, methods=["POST"]),
    ],
    lifespan=lifespan,
)
