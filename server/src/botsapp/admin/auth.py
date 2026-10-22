"""
Admin UI login (docs/decisions/0026).

Browser flow with AuthKit as the OpenID Connect provider: ``/login`` redirects
to AuthKit, ``/auth/callback`` exchanges the code, and a signed HTTP-only
session cookie then carries *only the caller's email*. Everything else — does
this person have a ``users`` row, are they an admin, do they own or hold the
account — is looked up per request, so a demotion or revocation applies on the
next click rather than when a cookie expires.

The WorkOS API is behind the ``AuthKitClient`` protocol so tests (and any
future provider swap) never touch the network. ``WorkOSAuthKitClient`` is the
real implementation; its request shapes follow WorkOS's User Management API
and have not yet been exercised against a live environment.

Route policy (``_policy``):

* ``/users`` (and legacy ``/access``)           — admins only
* ``/accounts/create``, ``/accounts/*/delete``, ``/accounts/*/key/*`` — admins only
* ``/accounts*`` (list, grant, revoke, select) — any user; handlers check that
  the account in the path is theirs (owner) or that they are an admin
* ``/`` and ``/session/*``         — the selected account's owner, or an admin
* ``/chats*``                      — anyone holding the selected account
* ``/login``, ``/auth/callback``, ``/logout`` — public

Which account the session/chats pages act on is the ``botsapp_account`` cookie
(set by ``/accounts/<id>/select``), validated on every request against what the
user may select: their own and granted accounts, plus every account for an admin
(who can then pair or reset a session but, holding no grant, sees no chats).

CSRF: the session cookie is ``SameSite=Lax`` and every state-changing request
must carry an ``Origin`` (or ``Referer``) matching this host. The hand-rolled
forms carry no token.
"""

from __future__ import annotations

import hmac
import html
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode, urlparse

from itsdangerous import BadSignature, URLSafeSerializer
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from workos import AsyncWorkOSClient, TransportError, WorkOSError

import botsapp.state as state
from botsapp.admin import templates
from botsapp.admin.directory import DirectoryError, UserDirectory, WorkOSUserDirectory

logger = logging.getLogger(__name__)

DEFAULT_ACCOUNT_ID = "default"
SESSION_COOKIE = "botsapp_session"
ACCOUNT_COOKIE = "botsapp_account"
STATE_COOKIE = "botsapp_oauth"
SESSION_TTL_SECONDS = 8 * 60 * 60
STATE_TTL_SECONDS = 10 * 60
MIN_SECRET_LENGTH = 32

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_PUBLIC_PATHS = {"/login", "/auth/callback", "/logout"}


# ── AuthKit client ───────────────────────────────────────────────────────────


class AuthError(Exception):
    """The provider refused or failed the code exchange."""


@dataclass(frozen=True)
class AuthUser:
    email: str
    email_verified: bool
    organization_id: str | None = None


class AuthKitClient(Protocol):
    def authorization_url(self, *, redirect_uri: str, state: str) -> str: ...

    async def exchange_code(self, code: str) -> AuthUser: ...


class WorkOSAuthKitClient:
    """AuthKit login via the official ``workos`` SDK (authorize URL + code exchange)."""

    def __init__(self, client: AsyncWorkOSClient, organization_id: str | None = None) -> None:
        self._client = client
        self._organization_id = organization_id

    def authorization_url(self, *, redirect_uri: str, state: str) -> str:
        return self._client.user_management.get_authorization_url(
            redirect_uri=redirect_uri,
            provider="authkit",
            state=state,
            organization_id=self._organization_id,
        )

    async def exchange_code(self, code: str) -> AuthUser:
        try:
            response = await self._client.user_management.authenticate_with_code(code=code)
        except (WorkOSError, TransportError) as exc:
            raise AuthError(f"WorkOS authenticate failed: {exc}") from exc
        return AuthUser(
            email=response.user.email.strip().lower(),
            email_verified=response.user.email_verified,
            organization_id=response.organization_id,
        )


# ── Config ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AdminAuthConfig:
    client: AuthKitClient | None
    secret: bytes
    base_url: str
    organization_id: str | None = None
    # WorkOS membership management for the Users page; None means the page
    # edits only the local users table (dev / private-admin mode).
    directory: UserDirectory | None = None
    # The SDK client behind `client`/`directory`, kept so shutdown can close it.
    workos_client: AsyncWorkOSClient | None = None
    # Set only by LOCAL_DEV_DISABLE_AUTH: every request acts as this user.
    dev_user: str | None = None

    async def aclose(self) -> None:
        if self.workos_client is not None:
            await self.workos_client.close()

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.startswith("https://")

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url.rstrip('/')}/auth/callback"


# Set by admin.app's lifespan (tests assign it directly), like state.db.
config: AdminAuthConfig | None = None


def config_from_env() -> AdminAuthConfig:
    """Build the config from the environment, or raise with everything missing."""
    # Two switches: LOCAL_DEV_DISABLE_AUTH (shared with the MCP server, for local
    # dev) and ADMIN_DISABLE_AUTH (admin UI only — the explicit opt-out for a
    # deployment where the admin UI is still reachable only over the private
    # network and the login isn't configured yet). Deliberately not an
    # implicit fallback: with login unconfigured and neither set, startup fails.
    disabled_by = next(
        (
            name
            for name in ("ADMIN_DISABLE_AUTH", "LOCAL_DEV_DISABLE_AUTH")
            if os.environ.get(name, "").strip().lower() in ("1", "true", "yes")
        ),
        None,
    )
    if disabled_by:
        dev_user = os.environ.get("BOTSAPP_ADMIN_EMAIL", "").strip().lower()
        if not dev_user:
            raise RuntimeError(f"{disabled_by} needs BOTSAPP_ADMIN_EMAIL to act as")
        logger.warning(
            "%s is set — the admin UI has NO login and acts as %s. Only acceptable while "
            "it is reachable by nobody but you.",
            disabled_by,
            dev_user,
        )
        return AdminAuthConfig(client=None, secret=b"", base_url="", dev_user=dev_user)

    env = {
        k: os.environ.get(k, "").strip()
        for k in (
            "WORKOS_CLIENT_ID",
            "WORKOS_API_KEY",
            "ADMIN_BASE_URL",
            "BOTSAPP_SESSION_SECRET",
        )
    }
    missing = [k for k, v in env.items() if not v]
    if missing:
        raise RuntimeError(f"Admin UI login is not configured; missing: {', '.join(missing)}")
    if len(env["BOTSAPP_SESSION_SECRET"]) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"BOTSAPP_SESSION_SECRET must be at least {MIN_SECRET_LENGTH} characters"
        )
    org = os.environ.get("WORKOS_ORG_ID", "").strip() or None
    workos = AsyncWorkOSClient(api_key=env["WORKOS_API_KEY"], client_id=env["WORKOS_CLIENT_ID"])
    return AdminAuthConfig(
        client=WorkOSAuthKitClient(workos, org),
        directory=WorkOSUserDirectory(workos, org) if org else None,
        workos_client=workos,
        secret=env["BOTSAPP_SESSION_SECRET"].encode(),
        base_url=env["ADMIN_BASE_URL"].rstrip("/"),
        organization_id=org,
    )


# ── Signed cookies ───────────────────────────────────────────────────────────


_SESSION_PURPOSE = "session"
_STATE_PURPOSE = "oauth-state"


def _serializer(secret: bytes, purpose: str) -> URLSafeSerializer:
    # The purpose is the salt, so a cookie signed for one use (e.g. the login
    # state) can never be replayed as another (a session).
    return URLSafeSerializer(secret, salt=f"botsapp-admin-{purpose}")


def _sign(secret: bytes, payload: dict[str, Any], ttl: int, purpose: str = _SESSION_PURPOSE) -> str:
    return _serializer(secret, purpose).dumps({**payload, "x": int(time.time()) + ttl})


def _unsign(
    secret: bytes, token: str | None, purpose: str = _SESSION_PURPOSE
) -> dict[str, Any] | None:
    """The payload if the signature is valid and unexpired, else None."""
    if not token or not secret:
        return None
    try:
        payload = _serializer(secret, purpose).loads(token)
    except BadSignature:
        return None
    if not isinstance(payload, dict) or payload.get("x", 0) < time.time():
        return None
    return payload


def issue_session(cfg: AdminAuthConfig, email: str) -> str:
    return _sign(cfg.secret, {"e": email.lower()}, SESSION_TTL_SECONDS)


def _session_email(cfg: AdminAuthConfig, request: Request) -> str | None:
    payload = _unsign(cfg.secret, request.cookies.get(SESSION_COOKIE))
    email = payload.get("e") if payload else None
    return email if isinstance(email, str) and email else None


def _safe_next(target: str | None) -> str:
    """Only same-site relative paths: no scheme, no `//host`, no backslashes."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


# ── Policy ───────────────────────────────────────────────────────────────────


def _policy(path: str) -> str:
    if path.startswith(("/users", "/access")):
        return "admin"
    if path == "/accounts/create" or (path.startswith("/accounts/") and path.endswith("/delete")):
        return "admin"
    if path.startswith("/accounts/") and "/key/" in path:
        return "admin"
    if path.startswith("/accounts"):
        return "user"  # the handlers check ownership of the account in the path
    if path == "/" or path.startswith("/session"):
        return "owner"
    return "member"


def _origin_ok(cfg: AdminAuthConfig, request: Request) -> bool:
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return False
    allowed = {request.headers.get("host"), urlparse(cfg.base_url).netloc} - {None, ""}
    return urlparse(source).netloc in allowed


def _denied(status: int, message: str) -> Response:
    return HTMLResponse(
        templates.message_page("Access denied", html.escape(message)), status_code=status
    )


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        cfg = config
        if cfg is None:
            return PlainTextResponse("Admin login is not configured", status_code=503)
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        email = cfg.dev_user or _session_email(cfg, request)
        if not email:
            if request.method in _SAFE_METHODS:
                query = urlencode({"next": request.url.path})
                return RedirectResponse(url=f"/login?{query}", status_code=303)
            return PlainTextResponse("Not signed in", status_code=401)

        assert state.db is not None
        user = await state.db.get_user(email)
        if user is None:
            return _denied(403, f"{email} is not a botsapp user.")
        if (
            request.method not in _SAFE_METHODS
            and not cfg.dev_user
            and not _origin_ok(cfg, request)
        ):
            return _denied(403, "Cross-site request refused.")

        policy = _policy(request.url.path)
        if policy == "admin" and not user["is_admin"]:
            return _denied(403, "Administrators only.")

        mine = await state.db.list_accounts_for_user(email)
        selectable = await state.db.list_accounts() if user["is_admin"] else mine
        account_id = _resolve_account(request, email, mine, selectable)
        chosen = next((a for a in selectable if a["id"] == account_id), None)
        can_manage_session = chosen is not None and (
            bool(user["is_admin"]) or chosen["owner_email"] == email
        )
        if policy == "owner" and not can_manage_session:
            if request.url.path == "/" and request.method == "GET":
                # The landing page is the Session page; for someone who only has
                # a shared account, send them somewhere they can act.
                return RedirectResponse(url="/accounts", status_code=303)
            if chosen is None:
                return _denied(403, "You don't have a WhatsApp account yet.")
            return _denied(403, "Only the account's owner can manage its session.")
        elif policy == "member" and account_id not in {a["id"] for a in mine}:
            return _denied(403, "You don't have access to a WhatsApp account.")

        request.state.user = user
        request.state.account = account_id
        token = templates.nav.set(
            templates.NavContext(
                email=email,
                is_admin=bool(user["is_admin"]),
                accounts=tuple((a["id"], a["label"] or a["id"]) for a in selectable),
                account=account_id or "",
                can_manage_session=can_manage_session,
            )
        )
        try:
            return await call_next(request)
        finally:
            templates.nav.reset(token)


def _resolve_account(
    request: Request, email: str, mine: list[dict[str, Any]], selectable: list[dict[str, Any]]
) -> str | None:
    """The account this request acts on: the cookie if the user may select it,
    else one they own (the default if it's theirs), else one shared with them,
    else (admins) the first overall. Owned comes first so someone who owns an
    account and has also been granted the default lands on their own."""
    ids = {a["id"] for a in selectable}
    chosen = request.cookies.get(ACCOUNT_COOKIE)
    if chosen in ids:
        return chosen
    owned = [a["id"] for a in mine if a["owner_email"] == email]
    shared = [a["id"] for a in mine if a["owner_email"] != email]
    for group in (owned, shared):
        if DEFAULT_ACCOUNT_ID in group:
            return DEFAULT_ACCOUNT_ID
        if group:
            return group[0]
    return next(iter(sorted(ids)), None)


# ── Routes ───────────────────────────────────────────────────────────────────


def _set_cookie(response: Response, cfg: AdminAuthConfig, name: str, value: str, ttl: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=ttl,
        httponly=True,
        secure=cfg.secure_cookies,
        samesite="lax",
        path="/",
    )


async def login(request: Request) -> Response:
    cfg = config
    assert cfg is not None
    if cfg.client is None:  # dev mode: nothing to log in to
        return RedirectResponse(url="/", status_code=303)
    nonce = secrets.token_urlsafe(24)
    cookie = _sign(
        cfg.secret,
        {"s": nonce, "n": _safe_next(request.query_params.get("next"))},
        STATE_TTL_SECONDS,
        _STATE_PURPOSE,
    )
    url = cfg.client.authorization_url(redirect_uri=cfg.redirect_uri, state=nonce)
    response = RedirectResponse(url=url, status_code=303)
    _set_cookie(response, cfg, STATE_COOKIE, cookie, STATE_TTL_SECONDS)
    return response


async def auth_callback(request: Request) -> Response:
    cfg = config
    assert cfg is not None and state.db is not None
    if cfg.client is None:
        return RedirectResponse(url="/", status_code=303)

    pending = _unsign(cfg.secret, request.cookies.get(STATE_COOKIE), _STATE_PURPOSE)
    returned = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    if not pending or not code or not hmac.compare_digest(str(pending.get("s", "")), returned):
        return PlainTextResponse("Login expired or invalid. Start again.", status_code=400)

    try:
        user = await cfg.client.exchange_code(code)
    except AuthError as exc:
        logger.warning("admin login failed: %s", exc)
        return _denied(502, "Sign-in failed. Try again.")
    if not user.email_verified:
        return _denied(403, "Verify your email with the sign-in provider first.")
    if cfg.organization_id and user.organization_id != cfg.organization_id:
        return _denied(403, f"{user.email} is not a member of this organization.")
    if await state.db.get_user(user.email) is None:
        return _denied(403, f"{user.email} hasn't been invited. Ask an administrator.")

    await _mirror_role(cfg, user.email)

    response = RedirectResponse(url=_safe_next(str(pending.get("n", "/"))), status_code=303)
    _set_cookie(response, cfg, SESSION_COOKIE, issue_session(cfg, user.email), SESSION_TTL_SECONDS)
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


async def _mirror_role(cfg: AdminAuthConfig, email: str) -> None:
    """Refresh the local ``is_admin`` mirror from the live WorkOS role at sign-in,
    so a role changed in the WorkOS dashboard converges. Best effort: a WorkOS
    outage keeps the stored value. The bootstrap admin is never demoted here, so
    a missing WorkOS role can't lock the owner out of their own deployment."""
    assert state.db is not None
    if cfg.directory is None:
        return
    try:
        role = await cfg.directory.get_role(email)
    except DirectoryError as exc:
        logger.warning("could not refresh role for %s: %s", email, exc)
        return
    if role is None:
        return
    bootstrap = os.environ.get("BOTSAPP_ADMIN_EMAIL", "").strip().lower()
    await state.db.set_user_admin(email, role == "admin" or email == bootstrap)


async def logout(_request: Request) -> Response:
    # A page, not a redirect to "/": AuthKit's own session is still live, so
    # bouncing straight into /login would silently sign the user back in.
    response = HTMLResponse(
        templates.message_page(
            "Signed out", 'You have been signed out. <a href="/login">Sign in again</a>.'
        )
    )
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


routes = [
    Route("/login", login),
    Route("/auth/callback", auth_callback),
    Route("/logout", logout),
]
