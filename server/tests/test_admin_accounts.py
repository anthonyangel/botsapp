"""The admin Accounts page (#50): create/delete accounts, share and unshare them,
choose which one the other pages act on. Real temp SQLite; the WAHA calls
(session create/delete/status) are stubbed on the registry."""

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from fake_waha import FakeWaha
from starlette.testclient import TestClient

import botsapp.admin.accounts as accounts_module
import botsapp.admin.app as admin_app
import botsapp.admin.auth as auth
import botsapp.state as state
from botsapp.bridge import SessionStatus
from botsapp.key_manager import KeyManager
from botsapp.secretbox import SecretBox

BASE = "https://admin.example.test"
ADMIN, SAM, HER, STRANGER = (
    "admin@example.com",
    "sam@example.com",
    "her@example.com",
    "stranger@example.com",
)


@pytest.fixture
def client(monkeypatch, tmp_path) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", str(tmp_path / "accounts.db"))
    monkeypatch.setenv("BOTSAPP_ADMIN_EMAIL", ADMIN)
    monkeypatch.setenv("WAHA_URL", "http://fake")
    monkeypatch.setenv("WAHA_API_KEY", "fake-key")
    cfg = auth.AdminAuthConfig(client=None, secret=b"s" * 32, base_url=BASE)
    monkeypatch.setattr(auth, "config_from_env", lambda: cfg)
    monkeypatch.setattr(
        accounts_module,
        "_account_status",
        AsyncMock(return_value=SessionStatus(connected=True, logged_in=True)),
    )
    with TestClient(admin_app.app, base_url=BASE, follow_redirects=False) as c:
        c.cfg = cfg
        # The registry would talk to WAHA; record instead.
        assert state.registry is not None
        state.registry.create_session = AsyncMock()
        state.registry.delete_session = AsyncMock()
        c.create_session = state.registry.create_session
        c.delete_session = state.registry.delete_session
        # Scoped WAHA keys (0027): a real KeyManager over an in-memory WAHA.
        c.waha = FakeWaha()
        c.box = SecretBox([Fernet.generate_key().decode()])
        state.key_manager = KeyManager(_db(), c.waha, c.box)
        yield c


def _as(client: TestClient, email: str) -> None:
    client.cookies.set(auth.SESSION_COOKIE, auth.issue_session(client.cfg, email))


def _post(client: TestClient, path: str, **data: str):
    return client.post(path, data=data, headers={"origin": BASE})


def _flash(response) -> dict[str, list[str]]:
    return parse_qs(urlparse(response.headers["location"]).query)


def _db():
    assert state.db is not None
    return state.db


async def _stored_key_id(account_id: str) -> str:
    stored = await _db().get_account_key(account_id)
    assert stored is not None
    return stored["key_id"]


async def _setup_people() -> None:
    db = state.db
    assert db is not None
    for email in (SAM, HER, STRANGER):
        await db.add_user(email)


@pytest.fixture
async def people(client):
    await _setup_people()


# ── create ───────────────────────────────────────────────────────────────────


async def test_admin_creates_an_account_and_its_session(client, people):
    _as(client, ADMIN)
    r = _post(client, "/accounts/create", id="sam", label="Sam's phone", owner=SAM)
    assert "notice" in _flash(r)
    account = await state.db.get_account("sam")
    assert account is not None
    assert account["owner_email"] == SAM and account["waha_session"] == "acct-sam"
    client.create_session.assert_awaited_once_with("sam")


@pytest.mark.parametrize("bad_id", ["", "A", "-x", "x-", "has space", "x" * 40, "UP per"])
async def test_create_validates_the_id(client, people, bad_id):
    _as(client, ADMIN)
    assert "error" in _flash(_post(client, "/accounts/create", id=bad_id, owner=SAM))
    client.create_session.assert_not_awaited()


async def test_create_refuses_duplicates_and_unknown_owners(client, people):
    _as(client, ADMIN)
    assert (
        "already exists"
        in _flash(_post(client, "/accounts/create", id="default", owner=SAM))["error"][0]
    )
    assert (
        "not a user"
        in _flash(_post(client, "/accounts/create", id="x1", owner="ghost@example.com"))["error"][0]
    )
    assert await state.db.get_account("x1") is None


async def test_failed_waha_session_removes_the_row_again(client, people):
    _as(client, ADMIN)
    client.create_session.side_effect = RuntimeError("WAHA down")
    r = _post(client, "/accounts/create", id="sam", owner=SAM)
    assert "nothing was created" in _flash(r)["error"][0]
    assert await state.db.get_account("sam") is None


async def test_members_cannot_create_or_delete(client, people):
    await state.db.add_account("sam", SAM, "acct-sam")
    _as(client, SAM)
    assert _post(client, "/accounts/create", id="mine", owner=SAM).status_code == 403
    assert _post(client, "/accounts/sam/delete").status_code == 403
    assert await state.db.get_account("sam") is not None


async def test_create_rechecks_the_admin_role_live(client, people):
    directory = SimpleNamespace(get_role=AsyncMock(return_value="member"))
    from dataclasses import replace

    auth.config = replace(client.cfg, directory=directory)
    _as(client, ADMIN)
    # The bootstrap admin is accepted only when WorkOS has no role for them; a
    # role of "member" (demoted in the dashboard) is refused.
    assert _post(client, "/accounts/create", id="sam", owner=SAM).status_code == 403
    assert await state.db.get_account("sam") is None


# ── delete ───────────────────────────────────────────────────────────────────


async def test_delete_removes_session_then_everything_about_the_account(client, people):
    await state.db.add_account("sam", SAM, "acct-sam")
    await state.db.grant_account("sam", HER)
    await state.db.set_chat_access_allowed("sam", HER, "a@g.us", True, acting_email=SAM)
    _as(client, ADMIN)
    r = _post(client, "/accounts/sam/delete")
    assert "notice" in _flash(r)
    client.delete_session.assert_awaited_once_with("sam")
    assert await state.db.get_account("sam") is None
    assert await state.db.list_grants("sam") == []
    assert await state.db.list_allowed_jids_for("sam", HER) == set()
    assert await state.db.get_user(SAM) is not None  # the owner remains a user


async def test_failed_waha_delete_changes_nothing(client, people):
    await state.db.add_account("sam", SAM, "acct-sam")
    _as(client, ADMIN)
    client.delete_session.side_effect = RuntimeError("WAHA down")
    assert "nothing was changed" in _flash(_post(client, "/accounts/sam/delete"))["error"][0]
    assert await state.db.get_account("sam") is not None


async def test_default_account_cannot_be_deleted(client, people):
    _as(client, ADMIN)
    assert "default" in _flash(_post(client, "/accounts/default/delete"))["error"][0]
    client.delete_session.assert_not_awaited()
    assert await state.db.get_account("default") is not None


async def test_failed_row_delete_says_to_retry(client, people, monkeypatch):
    await state.db.add_account("sam", SAM, "acct-sam")
    _as(client, ADMIN)

    async def boom(*_a, **_k):
        raise RuntimeError("locked")

    monkeypatch.setattr(state.db, "delete_account", boom)
    assert "try Delete again" in _flash(_post(client, "/accounts/sam/delete"))["error"][0]


# ── grant / revoke ───────────────────────────────────────────────────────────


async def test_owner_shares_and_unshares_their_account(client, people):
    await state.db.add_account("sam", SAM, "acct-sam")
    _as(client, SAM)
    assert "notice" in _flash(_post(client, "/accounts/sam/grant", email=HER))
    assert await state.db.has_account_access("sam", HER)
    await state.db.set_chat_access_allowed("sam", HER, "a@g.us", True, acting_email=SAM)
    assert "notice" in _flash(_post(client, "/accounts/sam/revoke", email=HER))
    assert not await state.db.has_account_access("sam", HER)
    assert await state.db.list_allowed_jids_for("sam", HER) == set()


async def test_you_cannot_manage_someone_elses_account_and_cannot_tell_if_it_exists(client, people):
    await state.db.add_account("sam", SAM, "acct-sam")
    _as(client, HER)
    unknown = _flash(_post(client, "/accounts/sam/grant", email=HER))
    missing = _flash(_post(client, "/accounts/nope/grant", email=HER))
    assert unknown == missing == {"error": ["Unknown account."]}
    assert not await state.db.has_account_access("sam", HER)


async def test_a_viewer_cannot_share_the_account_further(client, people):
    await state.db.add_account("sam", SAM, "acct-sam")
    await state.db.grant_account("sam", HER)
    _as(client, HER)
    assert (
        "Unknown account"
        in _flash(_post(client, "/accounts/sam/grant", email=STRANGER))["error"][0]
    )
    assert not await state.db.has_account_access("sam", STRANGER)


async def test_admin_can_manage_any_account(client, people):
    await state.db.add_account("sam", SAM, "acct-sam")
    _as(client, ADMIN)
    assert "notice" in _flash(_post(client, "/accounts/sam/grant", email=HER))


async def test_grant_validates_the_person(client, people):
    await state.db.add_account("sam", SAM, "acct-sam")
    _as(client, SAM)
    assert (
        "not a user"
        in _flash(_post(client, "/accounts/sam/grant", email="ghost@example.com"))["error"][0]
    )
    assert "already has" in _flash(_post(client, "/accounts/sam/grant", email=SAM))["error"][0]


# ── page ─────────────────────────────────────────────────────────────────────


async def test_member_sees_only_their_own_and_shared_accounts(client, people):
    await state.db.add_account("sam", SAM, "acct-sam", label="Sam's phone")
    await state.db.add_account("her", HER, "acct-her", label="Her phone")
    await state.db.grant_account("sam", HER)
    _as(client, SAM)
    page = client.get("/").text
    assert "Sam&#x27;s phone" in page and "Her phone" not in page
    assert "New account" not in page  # creating is admin-only
    _as(client, HER)
    page = client.get("/").text
    assert "Her phone" in page and "shared by sam@example.com" in page


async def test_admin_sees_every_account_and_the_create_form(client, people):
    await state.db.add_account("sam", SAM, "acct-sam", label="Sam's phone")
    _as(client, ADMIN)
    page = client.get("/").text
    assert "Sam&#x27;s phone" in page and "New account" in page and "connected" in page


async def test_labels_and_emails_are_escaped(client, people):
    await state.db.add_account("sam", SAM, "acct-sam", label="<script>alert(1)</script>")
    _as(client, ADMIN)
    assert "<script>alert" not in client.get("/").text


# ── selecting an account ─────────────────────────────────────────────────────


async def test_select_sets_a_validated_cookie_and_drives_the_pages(client, people, monkeypatch):
    await state.db.add_account("her", HER, "acct-her")
    await state.db.add_account("her2", HER, "acct-her2")
    await state.db.grant_account("default", HER)  # shared, but she owns two of her own
    seen: list[str] = []

    async def fake_bridge(request):
        seen.append(request.state.account)
        return SimpleNamespace(
            get_session_status=AsyncMock(
                return_value=SessionStatus(connected=True, logged_in=False)
            )
        )

    monkeypatch.setattr(admin_app, "_bridge", fake_bridge)
    _as(client, HER)
    client.get("/chats")
    assert seen[-1] == "her"  # owned beats the shared default
    r = client.post(
        "/accounts/her2/select",
        headers={"origin": BASE, "referer": f"{BASE}/chats"},
    )
    assert r.status_code == 303 and r.headers["location"] == "/chats"
    assert "HttpOnly" in r.headers["set-cookie"]
    client.get("/chats")
    assert seen[-1] == "her2"


async def test_cannot_select_an_account_you_do_not_hold(client, people):
    await state.db.add_account("sam", SAM, "acct-sam")
    _as(client, HER)
    r = client.post("/accounts/sam/select", headers={"origin": BASE})
    assert "Unknown account" in _flash(r)["error"][0]
    assert auth.ACCOUNT_COOKIE not in r.headers.get("set-cookie", "")


async def test_select_requires_post_and_a_matching_origin(client, people):
    """GET must be refused (405): a state-changing navigation is exactly what
    AuthMiddleware's CSRF/Origin check is meant to block, and it only runs for
    non-GET/HEAD/OPTIONS requests."""
    await state.db.add_account("her", HER, "acct-her")
    _as(client, HER)
    assert client.get("/accounts/her/select").status_code == 405
    wrong_origin = client.post("/accounts/her/select", headers={"origin": "https://evil.test"})
    assert wrong_origin.status_code == 403
    r = client.post("/accounts/her/select", headers={"origin": BASE})
    assert r.status_code == 303
    assert "HttpOnly" in r.headers["set-cookie"]


async def test_a_forged_cookie_for_someone_elses_account_is_ignored(client, people, monkeypatch):
    await state.db.add_account("sam", SAM, "acct-sam")
    seen: list[str] = []

    async def fake_bridge(request):
        seen.append(request.state.account)
        raise RuntimeError("stop here")

    monkeypatch.setattr(admin_app, "_bridge", fake_bridge)
    await state.db.grant_account("default", HER)
    _as(client, HER)
    client.cookies.set(auth.ACCOUNT_COOKIE, "sam")
    with pytest.raises(RuntimeError):
        client.get("/chats")
    assert seen == ["default"]


async def test_admin_can_reset_any_session_but_sees_no_chats_without_a_grant(client, people):
    await state.db.add_account("sam", SAM, "acct-sam")
    _as(client, ADMIN)
    client.cookies.set(auth.ACCOUNT_COOKIE, "sam")
    # Session actions are per-account-in-the-path now, open to an admin for
    # any account regardless of the selected-account cookie...
    state.registry.for_account = AsyncMock(return_value=SimpleNamespace(disconnect=AsyncMock()))
    assert _post(client, "/accounts/sam/session/disconnect").status_code == 303
    # ...but the chats pages need a real grant, which an admin doesn't have.
    assert client.get("/chats").status_code == 403


async def test_switcher_appears_only_with_several_accounts_and_admin_links_only_for_admins(
    client, people
):
    await state.db.add_account("her", HER, "acct-her", label="Her phone")
    _as(client, HER)
    page = client.get("/").text
    assert 'aria-label="Account"' not in page  # she has one account
    assert 'href="/users"' not in page  # not an admin
    await state.db.grant_account("default", HER)
    page = client.get("/").text
    assert 'aria-label="Account"' in page and "Her phone" in page
    _as(client, ADMIN)
    assert 'href="/users"' in client.get("/").text


# ── scoped WAHA keys on the Accounts page (0027) ─────────────────────────────


async def test_creating_an_account_also_creates_its_read_only_key(client, people):
    _as(client, ADMIN)
    assert "notice" in _flash(_post(client, "/accounts/create", id="sam", owner=SAM))
    stored = await state.db.get_account_key("sam")
    assert stored is not None and stored["actions"]["send"] is False
    assert len(client.waha.keys) == 1
    assert client.waha.keys[stored["key_id"]].session == "acct-sam"


async def test_a_key_failure_undoes_the_session_and_the_account(client, people):
    _as(client, ADMIN)
    client.waha.fail.add("create")
    r = _post(client, "/accounts/create", id="sam", owner=SAM)
    assert "nothing was created" in _flash(r)["error"][0]
    assert await state.db.get_account("sam") is None
    client.delete_session.assert_awaited_once_with("sam")
    assert not client.waha.keys


async def test_without_a_master_key_the_account_is_made_but_says_it_has_no_key(client, people):
    state.key_manager = KeyManager(_db(), client.waha, None)
    _as(client, ADMIN)
    r = _post(client, "/accounts/create", id="sam", owner=SAM)
    assert "BOTSAPP_SECRET_KEY" in _flash(r)["notice"][0]
    assert await state.db.get_account("sam") is not None
    assert await state.db.get_account_key("sam") is None and not client.waha.keys


async def test_deleting_an_account_deletes_its_key_first(client, people):
    _as(client, ADMIN)
    _post(client, "/accounts/create", id="sam", owner=SAM)
    key_id = await _stored_key_id("sam")
    assert "notice" in _flash(_post(client, "/accounts/sam/delete"))
    assert key_id in client.waha.deleted and not client.waha.keys
    assert await state.db.get_account("sam") is None


async def test_a_failed_key_delete_keeps_the_account_and_its_session(client, people):
    _as(client, ADMIN)
    _post(client, "/accounts/create", id="sam", owner=SAM)
    client.delete_session.reset_mock()
    client.waha.fail.add("delete")
    assert "nothing changed" in _flash(_post(client, "/accounts/sam/delete"))["error"][0]
    assert await state.db.get_account("sam") is not None
    client.delete_session.assert_not_awaited()


async def test_an_existing_account_without_a_key_can_be_given_one(client, people):
    _as(client, ADMIN)
    page = client.get("/").text
    assert "using the shared admin key" in page and "Create access key" in page
    assert "notice" in _flash(_post(client, "/accounts/default/key/create"))
    assert await state.db.get_account_key("default") is not None
    page = client.get("/").text
    assert "read-only, as intended" in page and "Rotate" in page and "Verify" in page


async def test_rotating_replaces_the_key(client, people):
    _as(client, ADMIN)
    _post(client, "/accounts/default/key/create")
    first = await _stored_key_id("default")
    assert "notice" in _flash(_post(client, "/accounts/default/key/rotate"))
    second = await _stored_key_id("default")
    assert second != first and first in client.waha.deleted


async def test_verify_shows_a_scope_edited_in_wahas_dashboard(client, people):
    from botsapp.waha_keys import KeyInfo

    _as(client, ADMIN)
    _post(client, "/accounts/default/key/create")
    key_id = await _stored_key_id("default")
    info = client.waha.keys[key_id]
    client.waha.keys[key_id] = KeyInfo(
        key_id, info.session, False, True, {**info.actions, "send": True}
    )
    assert "differs" in _flash(_post(client, "/accounts/default/key/verify"))["error"][0]
    assert "differs from intended" in client.get("/").text


async def test_repair_button_appears_on_drift_and_restores_the_scope(client, people):
    from botsapp.waha_keys import KeyInfo

    _as(client, ADMIN)
    _post(client, "/accounts/default/key/create")
    assert "key/repair" not in client.get("/").text
    key_id = await _stored_key_id("default")
    info = client.waha.keys[key_id]
    client.waha.keys[key_id] = KeyInfo(
        key_id, info.session, False, True, {**info.actions, "send": True}
    )
    _post(client, "/accounts/default/key/verify")
    assert "key/repair" in client.get("/").text
    assert "notice" in _flash(_post(client, "/accounts/default/key/repair"))
    assert await _stored_key_id("default") == key_id
    assert "key/repair" not in client.get("/").text


async def test_creating_a_second_key_is_refused(client, people):
    _as(client, ADMIN)
    _post(client, "/accounts/default/key/create")
    assert "rotate" in _flash(_post(client, "/accounts/default/key/create"))["error"][0].lower()


async def test_key_controls_are_admin_only_and_hidden_from_owners(client, people):
    await state.db.add_account("sam", SAM, "acct-sam")
    _as(client, SAM)
    assert "/key/" not in client.get("/").text
    for action in ("create", "rotate", "verify", "repair"):
        assert _post(client, f"/accounts/sam/key/{action}").status_code == 403
    assert await state.db.get_account_key("sam") is None


async def test_key_actions_on_an_unknown_account_or_without_the_admin_key(client, people):
    _as(client, ADMIN)
    assert "Unknown account" in _flash(_post(client, "/accounts/ghost/key/create"))["error"][0]
    state.key_manager = None
    assert "admin key" in _flash(_post(client, "/accounts/default/key/create"))["error"][0]
