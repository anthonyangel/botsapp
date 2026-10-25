"""AuthKit login for the admin UI (docs/decisions/0026), against a stubbed
WorkOS client — no network. Policy per route is in admin/auth.py's docstring."""

import time
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient
from workos import TransportError, WorkOSError

import botsapp.admin.app as admin_app
import botsapp.admin.auth as auth
import botsapp.state as state
from botsapp.db import DatabaseManager

SECRET = b"s" * 32
BASE = "https://admin.example.test"
ADMIN, OWNER, GUEST, STRANGER = (
    "admin@example.com",
    "owner@example.com",
    "guest@example.com",
    "stranger@example.com",
)


class StubAuthKit:
    """Stands in for WorkOS: codes map to the user the provider would return."""

    def __init__(self) -> None:
        self.users: dict[str, auth.AuthUser] = {}
        self.fail = False

    def authorization_url(self, *, redirect_uri: str, state: str) -> str:
        return f"https://authkit.test/authorize?redirect_uri={redirect_uri}&state={state}"

    async def exchange_code(self, code: str) -> auth.AuthUser:
        if self.fail or code not in self.users:
            raise auth.AuthError("bad code")
        return self.users[code]


@pytest.fixture
def stub() -> StubAuthKit:
    s = StubAuthKit()
    s.users = {
        "admin": auth.AuthUser(ADMIN, True, "org_1"),
        "owner": auth.AuthUser(OWNER, True, "org_1"),
        "guest": auth.AuthUser(GUEST, True, "org_1"),
        "stranger": auth.AuthUser(STRANGER, True, "org_1"),
        "unverified": auth.AuthUser(OWNER, False, "org_1"),
        "otherorg": auth.AuthUser(OWNER, True, "org_other"),
    }
    return s


@pytest.fixture
def client(monkeypatch, stub):
    async def fake_connect(self):
        self._conn = object()
        return self._conn

    monkeypatch.setattr(DatabaseManager, "connect", fake_connect)
    monkeypatch.setattr(DatabaseManager, "ensure_schema", AsyncMock())
    monkeypatch.setattr(DatabaseManager, "close", AsyncMock())
    monkeypatch.setenv("DATABASE_URL", "fake.db")
    monkeypatch.setenv("WAHA_URL", "http://fake")
    monkeypatch.setenv("WAHA_API_KEY", "fake-key")
    cfg = auth.AdminAuthConfig(client=stub, secret=SECRET, base_url=BASE, organization_id="org_1")
    monkeypatch.setattr(auth, "config_from_env", lambda: cfg)

    users = {
        ADMIN: {"email": ADMIN, "is_admin": True},
        OWNER: {"email": OWNER, "is_admin": False},
        GUEST: {"email": GUEST, "is_admin": False},
    }
    holders = {ADMIN, OWNER, GUEST}  # STRANGER has no user row

    with TestClient(admin_app.app, base_url=BASE, follow_redirects=False) as c:
        bridge = AsyncMock()
        bridge.get_session_status.return_value = AsyncMock()
        bridge.list_groups.return_value = []
        bridge.get_contacts.return_value = []
        bridge.get_avatar_url.return_value = None
        bridge.get_chats.return_value = []
        state.db.get_user = AsyncMock(side_effect=lambda e: users.get(e))
        primary = {"id": "default", "label": "Primary", "owner_email": OWNER}
        state.db.list_accounts = AsyncMock(return_value=[primary])
        state.db.get_account = AsyncMock(return_value=primary)
        state.db.list_grants = AsyncMock(return_value=[GUEST])
        state.db.list_allowed_jids_for = AsyncMock(return_value=set())
        state.db.get_chat_access = AsyncMock(return_value=None)
        state.db.list_accounts_for_user = AsyncMock(
            side_effect=lambda e: [primary] if e in holders else []
        )

        assert state.registry is not None
        state.registry.for_account = AsyncMock(return_value=bridge)

        async def _bridge(_request):
            return bridge

        monkeypatch.setattr(admin_app, "_bridge", _bridge)
        state.db.list_users = AsyncMock(return_value=[])
        state.db.add_user = AsyncMock()
        state.db.remove_user = AsyncMock()
        c.cfg = cfg
        c.bridge = bridge
        yield c


def _sign_in(client: TestClient, email: str) -> None:
    client.cookies.set(auth.SESSION_COOKIE, auth.issue_session(client.cfg, email))


def _same_origin() -> dict[str, str]:
    return {"origin": BASE}


# ── unauthenticated ──────────────────────────────────────────────────────────


def test_unauthenticated_get_redirects_to_login_with_next(client):
    r = client.get("/chats")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fchats"


def test_unauthenticated_post_is_401(client):
    assert client.post("/session/connect", headers=_same_origin()).status_code == 401


def test_tampered_or_expired_cookie_is_signed_out(client):
    good = auth.issue_session(client.cfg, ADMIN)
    client.cookies.set(auth.SESSION_COOKIE, good[:-2] + "xx")
    assert client.get("/chats").status_code == 303
    expired = auth._sign(SECRET, {"e": ADMIN}, -1)
    client.cookies.set(auth.SESSION_COOKIE, expired, domain="admin.example.test")
    assert client.get("/chats").status_code == 303


def test_cookie_signed_with_another_secret_is_rejected(client):
    forged = auth._sign(b"x" * 32, {"e": ADMIN}, 60)
    client.cookies.set(auth.SESSION_COOKIE, forged)
    assert client.get("/users").status_code == 303


# ── login flow ───────────────────────────────────────────────────────────────


def _begin_login(client: TestClient, next_path: str = "/chats") -> str:
    r = client.get("/login", params={"next": next_path})
    assert r.status_code == 303
    loc = urlparse(r.headers["location"])
    assert loc.netloc == "authkit.test"
    query = parse_qs(loc.query)
    assert query["redirect_uri"] == [f"{BASE}/auth/callback"]
    assert auth.STATE_COOKIE in r.headers["set-cookie"]
    assert "HttpOnly" in r.headers["set-cookie"]
    return query["state"][0]


def test_full_login_sets_session_and_returns_to_next(client):
    nonce = _begin_login(client)
    r = client.get("/auth/callback", params={"code": "owner", "state": nonce})
    assert r.status_code == 303
    assert r.headers["location"] == "/chats"
    cookies = r.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith(auth.SESSION_COOKIE))
    assert "HttpOnly" in session and "SameSite=lax" in session and "Secure" in session
    # The cookie holds the email only; it works on the next request.
    assert client.get("/chats").status_code == 200


def test_callback_with_wrong_state_is_refused(client):
    _begin_login(client)
    r = client.get("/auth/callback", params={"code": "owner", "state": "not-the-nonce"})
    assert r.status_code == 400
    assert auth.SESSION_COOKIE not in r.headers.get("set-cookie", "")


def test_callback_without_login_attempt_is_refused(client):
    assert client.get("/auth/callback", params={"code": "owner", "state": "x"}).status_code == 400


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("stranger", 403),  # signed up at AuthKit but never invited
        ("unverified", 403),
        ("otherorg", 403),
        ("garbage", 502),  # provider refused the code
    ],
)
def test_callback_refusals_issue_no_session(client, code, expected):
    nonce = _begin_login(client)
    r = client.get("/auth/callback", params={"code": code, "state": nonce})
    assert r.status_code == expected
    assert auth.SESSION_COOKIE not in r.headers.get("set-cookie", "")


@pytest.mark.parametrize("bad", ["//evil.example", "https://evil.example", "/\\evil.example", ""])
def test_next_cannot_redirect_off_site(client, bad):
    nonce = _begin_login(client, next_path=bad)
    r = client.get("/auth/callback", params={"code": "owner", "state": nonce})
    assert r.headers["location"] == "/"


def test_denial_page_escapes_the_email(client, stub):
    stub.users["xss"] = auth.AuthUser("<script>alert(1)</script>@x.test", True, "org_1")
    nonce = _begin_login(client)
    r = client.get("/auth/callback", params={"code": "xss", "state": nonce})
    assert r.status_code == 403
    assert "<script>alert" not in r.text


def test_logout_clears_session_and_does_not_bounce_back_into_login(client):
    _sign_in(client, OWNER)
    r = client.get("/logout")
    assert r.status_code == 200 and "Signed out" in r.text
    assert (
        "max-age=0" in r.headers["set-cookie"].lower()
        or "expires" in r.headers["set-cookie"].lower()
    )


# ── authorization per route ──────────────────────────────────────────────────


def test_signed_in_cookie_without_user_row_is_refused(client):
    _sign_in(client, STRANGER)
    assert client.get("/chats").status_code == 403


def test_access_page_is_admin_only(client):
    _sign_in(client, OWNER)
    assert client.get("/users").status_code == 403
    _sign_in(client, ADMIN)
    assert client.get("/users").status_code == 200


def test_session_pages_are_owner_or_admin_only(client):
    # Session actions go through the same _manageable() check as
    # grant/revoke/delete/key (0029) — a redirect with an "Unknown
    # account." flash either way, not a 403, so a non-owner can't tell a
    # real account they don't own from one that doesn't exist.
    _sign_in(client, GUEST)  # holds the account but doesn't own it
    r = client.post("/accounts/default/session/disconnect", headers=_same_origin())
    assert r.status_code == 303 and "error=Unknown" in r.headers["location"]
    r = client.post("/accounts/default/session/reset", headers=_same_origin())
    assert r.status_code == 303 and "error=Unknown" in r.headers["location"]
    _sign_in(client, OWNER)
    r = client.post("/accounts/default/session/disconnect", headers=_same_origin())
    assert r.status_code == 303 and "error=" not in r.headers["location"]
    _sign_in(client, ADMIN)
    r = client.post("/accounts/default/session/disconnect", headers=_same_origin())
    assert r.status_code == 303 and "error=" not in r.headers["location"]


def test_chat_pages_need_access_to_the_account(client):
    _sign_in(client, GUEST)
    assert client.get("/chats").status_code == 200
    state.db.list_accounts_for_user = AsyncMock(return_value=[])
    assert client.get("/chats").status_code == 403


def test_revoked_user_loses_access_on_the_next_request(client):
    _sign_in(client, GUEST)
    assert client.get("/chats").status_code == 200
    state.db.get_user = AsyncMock(return_value=None)  # removed after the cookie was issued
    assert client.get("/chats").status_code == 403


# ── CSRF ─────────────────────────────────────────────────────────────────────


def test_cross_site_post_is_refused(client):
    _sign_in(client, ADMIN)
    r = client.post(
        "/users/invite", data={"email": "x@example.com"}, headers={"origin": "https://evil.test"}
    )
    assert r.status_code == 403
    state.db.add_user.assert_not_awaited()


def test_post_without_origin_or_referer_is_refused(client):
    _sign_in(client, ADMIN)
    assert client.post("/users/invite", data={"email": "x@example.com"}).status_code == 403


def test_same_origin_post_is_accepted(client):
    _sign_in(client, ADMIN)
    r = client.post("/users/invite", data={"email": "x@example.com"}, headers=_same_origin())
    assert r.status_code == 303
    state.db.add_user.assert_awaited_once_with("x@example.com", is_admin=False)


# ── config ───────────────────────────────────────────────────────────────────

_LOGIN_ENV = {
    "WORKOS_CLIENT_ID": "client_1",
    "WORKOS_API_KEY": "sk_test",
    "ADMIN_BASE_URL": "https://admin.example.test/",
    "BOTSAPP_SESSION_SECRET": "z" * 40,
}


def _clear(monkeypatch):
    for k in (
        *_LOGIN_ENV,
        "LOCAL_DEV_DISABLE_AUTH",
        "BOTSAPP_ADMIN_EMAIL",
        "WORKOS_ORG_ID",
    ):
        monkeypatch.delenv(k, raising=False)


def test_config_lists_everything_that_is_missing(monkeypatch):
    _clear(monkeypatch)
    with pytest.raises(RuntimeError, match="WORKOS_CLIENT_ID.*BOTSAPP_SESSION_SECRET"):
        auth.config_from_env()


def test_config_rejects_a_short_session_secret(monkeypatch):
    _clear(monkeypatch)
    for k, v in {**_LOGIN_ENV, "BOTSAPP_SESSION_SECRET": "short"}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(RuntimeError, match="at least"):
        auth.config_from_env()


def test_config_builds_from_env(monkeypatch):
    _clear(monkeypatch)
    for k, v in _LOGIN_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("WORKOS_ORG_ID", "org_1")
    cfg = auth.config_from_env()
    assert cfg.base_url == "https://admin.example.test"
    assert cfg.redirect_uri == "https://admin.example.test/auth/callback"
    assert cfg.organization_id == "org_1" and cfg.secure_cookies and cfg.dev_user is None


def test_dev_mode_needs_an_identity_and_skips_login(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("LOCAL_DEV_DISABLE_AUTH", "1")
    with pytest.raises(RuntimeError, match="BOTSAPP_ADMIN_EMAIL"):
        auth.config_from_env()
    monkeypatch.setenv("BOTSAPP_ADMIN_EMAIL", "Dev@Example.com")
    assert auth.config_from_env().dev_user == "dev@example.com"


def test_unconfigured_login_fails_closed_without_an_explicit_opt_out(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("BOTSAPP_ADMIN_EMAIL", "me@example.com")  # not an opt-out by itself
    with pytest.raises(RuntimeError, match="not configured"):
        auth.config_from_env()


def _sdk(org: str | None = "org_1"):
    from workos import AsyncWorkOSClient

    return AsyncWorkOSClient(api_key="sk_test", client_id="client_1"), org


def test_workos_authorization_url_shape():
    sdk, org = _sdk()
    url = auth.WorkOSAuthKitClient(sdk, org).authorization_url(
        redirect_uri="https://a.test/auth/callback", state="n"
    )
    query = parse_qs(urlparse(url).query)
    assert urlparse(url).netloc == "api.workos.com"
    assert query["client_id"] == ["client_1"] and query["provider"] == ["authkit"]
    assert query["organization_id"] == ["org_1"] and query["state"] == ["n"]
    assert query["redirect_uri"] == ["https://a.test/auth/callback"]


async def test_workos_exchange_code_maps_the_sdk_response(monkeypatch):
    from types import SimpleNamespace

    sdk, org = _sdk()
    response = SimpleNamespace(
        user=SimpleNamespace(email=" Mixed@Case.com ", email_verified=True),
        organization_id="org_1",
    )
    exchange = AsyncMock(return_value=response)
    monkeypatch.setattr(sdk.user_management, "authenticate_with_code", exchange)
    user = await auth.WorkOSAuthKitClient(sdk, org).exchange_code("the-code")
    exchange.assert_awaited_once_with(code="the-code")
    assert user == auth.AuthUser("mixed@case.com", True, "org_1")


@pytest.mark.parametrize("error", [WorkOSError("nope"), TransportError("down")])
async def test_workos_exchange_failures_become_auth_errors(monkeypatch, error):
    sdk, org = _sdk()
    monkeypatch.setattr(sdk.user_management, "authenticate_with_code", AsyncMock(side_effect=error))
    with pytest.raises(auth.AuthError):
        await auth.WorkOSAuthKitClient(sdk, org).exchange_code("x")


def test_a_cookie_for_one_purpose_is_not_valid_for_another():
    state_cookie = auth._sign(SECRET, {"e": ADMIN}, 60, auth._STATE_PURPOSE)
    assert auth._unsign(SECRET, state_cookie) is None  # not accepted as a session
    assert auth._unsign(SECRET, state_cookie, auth._STATE_PURPOSE) is not None


def test_signed_payload_round_trip_and_expiry():
    token = auth._sign(SECRET, {"e": "a@b.c"}, 60)
    payload = auth._unsign(SECRET, token)
    assert payload is not None
    assert payload["e"] == "a@b.c"
    assert time.time() < payload["x"]
    assert auth._unsign(SECRET, auth._sign(SECRET, {"e": "a@b.c"}, -1)) is None
    assert auth._unsign(b"", token) is None
    assert auth._unsign(SECRET, token + "x") is None
