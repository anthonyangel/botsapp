"""
Admin UI Starlette app.

Runs as its own process (botsapp.admin_main:app), independent of the
public MCP server (botsapp.main:app) — they share only the SQLite
chat_metadata database and WAHA's own storage, never a Python process.
Every route except the login ones sits behind admin/auth.py's AuthKit login
(docs/decisions/0026); only LOCAL_DEV_DISABLE_AUTH turns that off, for local
development. Until the per-account pages land (#51) these pages operate on the
selected account (the ``botsapp_account`` cookie; see admin/auth.py).

Uses the selected account's client from state.registry (also the raw
MessageStore), same as the MCP server. The active provider is shown in the page header so it's never
ambiguous which one you're looking at.
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlencode

import phonenumbers
from starlette.applications import Starlette
from starlette.datastructures import FormData
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

import botsapp.state as state
from botsapp.admin import accounts, auth, templates, users
from botsapp.bridge_factory import bridge_provider_name, build_registry
from botsapp.db import DatabaseManager
from botsapp.scope import ScopedChatDb
from botsapp.session import connect_or_get_status, get_status
from botsapp.waha_client import WAHAClient

logger = logging.getLogger(__name__)

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
    auth.config = auth.config_from_env()  # fails loudly if login isn't configured
    state.db = DatabaseManager(os.environ["DATABASE_URL"])  # a SQLite file path
    await state.db.connect()
    await state.db.ensure_schema()
    if admin_email := os.environ.get("BOTSAPP_ADMIN_EMAIL", "").strip():
        # Adopts the single-tenant data as the first admin's primary account
        # (idempotent) — docs/decisions/0025.
        await state.db.bootstrap(admin_email, os.environ.get("WAHA_SESSION", "default"))
    else:
        logger.error("BOTSAPP_ADMIN_EMAIL is not set: no user will be an admin or own an account")

    state.registry = build_registry(state.db)
    logger.info("Admin UI ready (bridge: %s)", bridge_provider_name())

    yield

    await state.registry.close()
    await state.db.close()
    state.db = None
    state.registry = None
    if auth.config is not None:
        await auth.config.aclose()
    auth.config = None


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


async def _bridge(request: Request) -> WAHAClient:
    """The selected account's WhatsApp client. It is also the *unfiltered*
    MessageStore — deliberately not AllowlistedMessageStore, since this UI's
    whole purpose is to see (and allow) chats that aren't allowed yet."""
    assert state.registry is not None
    return await state.registry.for_account(request.state.account)


def _chat_db(request: Request, viewer: str | None = None) -> ScopedChatDb:
    """Chat allowlist/tags for ``viewer`` (default: the signed-in user) on the
    selected account — the same (account, viewer) an MCP call from that user
    resolves to."""
    assert state.db is not None
    return ScopedChatDb(state.db, request.state.account, viewer or request.state.user["email"])


@dataclass(frozen=True)
class _ChatAccess:
    """What the signed-in user may do on the Chats pages of the selected account.

    The account's *owner* sees every chat and decides what each person with
    access can see (their own view included). Anyone it was *shared with* sees
    only the chats the owner made visible to them, can label those for
    themself, and can't change what is shared: otherwise they could simply
    allow every chat for themself, which the MCP server would then honour.
    """

    me: str
    owner: str
    viewer: str  # whose visibility is being read/edited
    is_owner: bool
    chat_db: ScopedChatDb

    @property
    def can_toggle(self) -> bool:
        return self.is_owner

    @property
    def can_tag(self) -> bool:
        return self.viewer == self.me


async def _chat_access(request: Request) -> _ChatAccess | Response:
    assert state.db is not None
    me = request.state.user["email"]
    account = await state.db.get_account(request.state.account)
    if account is None:
        return PlainTextResponse("Unknown account", status_code=404)
    is_owner = account["owner_email"] == me
    viewer = request.query_params.get("viewer", "").strip().lower() or me
    if viewer != me:
        holders = {account["owner_email"], *await state.db.list_grants(account["id"])}
        if not is_owner or viewer not in holders:
            # Same answer whether the person exists or not.
            return PlainTextResponse("Unknown viewer", status_code=403)
    return _ChatAccess(
        me=me,
        owner=account["owner_email"],
        viewer=viewer,
        is_owner=is_owner,
        chat_db=_chat_db(request, viewer),
    )


async def _poll_status(request: Request) -> dict[str, Any]:
    """Status only — never triggers a connect. Calling the bridge's connect
    on every page view/poll was the actual bug behind "QR linking isn't
    working": each reload issued a fresh QR and killed the bridge's channel
    for whichever QR was already mid-scan. get_status() is safe to call as
    often as we like; connect_or_get_status() (which does call connect) is
    reserved for the explicit "Connect WhatsApp" button below."""
    return asdict(await get_status(await _bridge(request)))


async def session_index(request: Request) -> Response:
    status = await _poll_status(request)
    return HTMLResponse(templates.session_page(status, bridge_provider_name()))


async def session_status_json(request: Request) -> Response:
    """Polled by the QR-waiting page client-side (see templates.session_page)
    to refresh the QR/connected state in place, without reloading / and
    without ever re-triggering connect."""
    return JSONResponse(await _poll_status(request))


async def session_connect(request: Request) -> Response:
    """The only route that actually calls the bridge's connect — a
    deliberate user action (button click), not an automatic side effect of
    viewing or polling the session page."""
    await connect_or_get_status(await _bridge(request))
    return RedirectResponse(url="/", status_code=303)


async def session_disconnect(request: Request) -> Response:
    """Log out of WhatsApp (WAHA logout endpoint). Leaves the session in
    STOPPED state so the user can immediately hit Connect WhatsApp and
    re-pair with a fresh QR."""
    await (await _bridge(request)).disconnect()
    return RedirectResponse(url="/", status_code=303)


async def session_delete(request: Request) -> Response:
    """Nuclear option: delete the WAHA session entirely (removes all
    persisted auth material). Use this when logout alone isn't enough to
    escape a stuck WORKING state. After deletion, Connect WhatsApp will
    create a fresh session and show a new QR code."""
    try:
        await (await _bridge(request)).delete_session()
    except Exception as exc:
        logger.warning("session delete failed: %s", exc)
    return RedirectResponse(url="/", status_code=303)


async def chats_index(request: Request) -> Response:
    assert state.db is not None
    access = await _chat_access(request)
    if isinstance(access, Response):
        return access
    chat_db = access.chat_db
    bridge = await _bridge(request)

    session_status = await get_status(bridge)
    if not session_status.logged_in:
        return HTMLResponse(templates.not_connected_page(bridge_provider_name()))

    # Someone the account was shared with sees only what the owner made visible
    # to them: not the owner's other chats, nor how many there are.
    visible = None if access.is_owner else await chat_db.list_allowed_jids()

    all_chats = await bridge.get_chats(limit=1000, offset=0)
    if visible is not None:
        all_chats = [c for c in all_chats if c["jid"] in visible]
    direct_chats = [c for c in all_chats if _classify(c["jid"]) == "direct"]
    newsletter_chats = [c for c in all_chats if _classify(c["jid"]) == "newsletter"]
    group_jids_with_history = {c["jid"] for c in all_chats if _classify(c["jid"]) == "group"}
    status_seen = any(_classify(c["jid"]) == "status" for c in all_chats)

    # ── Direct chats: enrich with the bridge's own contact names + avatars ─
    try:
        contacts = {c.jid: c for c in await bridge.get_contacts()}
    except Exception as exc:
        logger.warning("get_contacts failed while building admin chat list: %s", exc)
        contacts = {}

    # Pass the full jid, not just the number — a bridge needs the suffix
    # to tell an @lid (privacy-preserving id) contact from a plain phone
    # number; each bridge's own get_avatar_url translates it as needed.
    direct_avatars = await asyncio.gather(*(bridge.get_avatar_url(c["jid"]) for c in direct_chats))
    direct_rows: list[dict[str, Any]] = []
    for chat, avatar in zip(direct_chats, direct_avatars, strict=True):
        jid = chat["jid"]
        contact = contacts.get(jid)
        display_name = (contact.name if contact else None) or chat.get("name") or jid
        meta = await chat_db.get_chat_metadata(jid=jid) or {}
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
        *(bridge.get_avatar_url(c["jid"]) for c in newsletter_chats)
    )
    newsletter_rows: list[dict[str, Any]] = []
    for chat, avatar in zip(newsletter_chats, newsletter_avatars, strict=True):
        jid = chat["jid"]
        meta = await chat_db.get_chat_metadata(jid=jid) or {}
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
        groups = await bridge.list_groups()
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
    if visible is not None:
        groups = [g for g in groups if g.jid in visible]
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

    group_avatars = await asyncio.gather(*(bridge.get_avatar_url(g.jid) for g in regular_groups))
    group_rows: list[dict[str, Any]] = []
    for g, avatar in zip(regular_groups, group_avatars, strict=True):
        meta = await chat_db.get_chat_metadata(jid=g.jid) or {}
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
        *(bridge.get_avatar_url(g.jid) for g in community_groups)
    )
    community_rows: list[dict[str, Any]] = []
    for g, avatar in zip(community_groups, community_avatars, strict=True):
        meta = await chat_db.get_chat_metadata(jid=g.jid) or {}
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
        meta = await chat_db.get_chat_metadata(jid=STATUS_BROADCAST_JID) or {}
        status_row = {
            "jid": STATUS_BROADCAST_JID,
            "is_allowed": bool(meta.get("is_allowed")),
            "tags": meta.get("tags") or [],
        }

    grants = await state.db.list_grants(request.state.account) if access.is_owner else []
    token = templates.chat_view.set(
        templates.ChatView(
            can_toggle=access.can_toggle,
            can_tag=access.can_tag,
            for_viewer="" if access.viewer == access.me else access.viewer,
            me=access.me,
            owner=access.owner,
            viewers=(access.me, *grants) if access.is_owner else (),
        )
    )
    try:
        page = templates.browse_page(
            direct_rows,
            group_rows,
            community_rows,
            newsletter_rows,
            status_row,
            bridge_provider_name(),
            group_count_loaded=group_count_loaded,
            group_count_total=group_count_total,
        )
    finally:
        templates.chat_view.reset(token)
    return HTMLResponse(page)


_PANEL_BY_CLASS = {
    "group": "groups-panel",
    "community": "community-panel",
    "status": "status-panel",
    "direct": "direct-panel",
    "newsletter": "newsletter-panel",
}


def _redirect_to_tab(jid: str, viewer: str = "") -> RedirectResponse:
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
    query = f"?{urlencode({'viewer': viewer})}" if viewer else ""
    return RedirectResponse(url=f"/chats{query}#{panel}", status_code=303)


async def toggle_allowed(request: Request) -> Response:
    access = await _chat_access(request)
    if isinstance(access, Response):
        return access
    if not access.can_toggle:
        # Not the account's owner: a shared-with viewer can't widen what they see.
        return PlainTextResponse("Only the account's owner can change what is shared", 403)
    jid = request.path_params["jid"]
    form = await request.form()
    is_allowed = form.get("is_allowed") == "on"
    await access.chat_db.set_chat_allowed(jid=jid, is_allowed=is_allowed)
    return _redirect_to_tab(jid, "" if access.viewer == access.me else access.viewer)


def _form_str(form: FormData, key: str) -> str:
    """Text field as a plain string. These forms have no file inputs, but
    FormData.get()'s return type includes UploadFile — guard against it
    rather than assuming."""
    value = form.get(key)
    return value if isinstance(value, str) else ""


async def save_metadata(request: Request) -> Response:
    access = await _chat_access(request)
    if isinstance(access, Response):
        return access
    if not access.can_tag:
        return PlainTextResponse("Tags are each person's own to edit", status_code=403)
    jid = request.path_params["jid"]
    if not access.is_owner and jid not in await access.chat_db.list_allowed_jids():
        # A viewer labels what was shared with them, not arbitrary chats.
        return PlainTextResponse("Unknown chat", status_code=403)
    form = await request.form()
    tags = [t.strip() for t in _form_str(form, "tags").split(",") if t.strip()]
    await access.chat_db.upsert_chat_metadata(jid=jid, tags=tags)
    return _redirect_to_tab(jid)


async def access_redirect(_request: Request) -> Response:
    """The old Access tab lives on as /users (docs/decisions/0026)."""
    return RedirectResponse(url="/users", status_code=308)


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
        Route("/users", users.users_index),
        Route("/users/invite", users.invite_user, methods=["POST"]),
        Route("/users/{email}/role", users.change_role, methods=["POST"]),
        Route("/users/{email}/remove", users.remove_user, methods=["POST"]),
        Route("/access", access_redirect),
        Route("/accounts", accounts.accounts_index),
        Route("/accounts/create", accounts.create_account, methods=["POST"]),
        Route("/accounts/{account_id}/select", accounts.select_account),
        Route("/accounts/{account_id}/grant", accounts.grant_account, methods=["POST"]),
        Route("/accounts/{account_id}/revoke", accounts.revoke_account, methods=["POST"]),
        Route("/accounts/{account_id}/delete", accounts.delete_account, methods=["POST"]),
        *auth.routes,
    ],
    middleware=[Middleware(auth.AuthMiddleware)],
    lifespan=lifespan,
)
