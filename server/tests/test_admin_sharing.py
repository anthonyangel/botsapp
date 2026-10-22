"""Chats page for owners vs. people an account is shared with (#51).

The point under test: someone the account was shared with must not be able to
see, or grant themself, anything the owner didn't make visible to them, because
the MCP server honours exactly what is in ``chat_access``.
"""

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

import botsapp.admin.app as admin_app
import botsapp.admin.auth as auth
import botsapp.state as state
from botsapp.bridge import SessionStatus

BASE = "https://admin.example.test"
ADMIN, SAM, HER, STRANGER = (
    "admin@example.com",
    "sam@example.com",
    "her@example.com",
    "stranger@example.com",
)
SHARED, PRIVATE = "111@s.whatsapp.net", "222@s.whatsapp.net"
CHATS = [
    {"jid": SHARED, "name": "Shared Alice"},
    {"jid": PRIVATE, "name": "Private Bob"},
]


@pytest.fixture
def client(monkeypatch, tmp_path) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", str(tmp_path / "sharing.db"))
    monkeypatch.setenv("BOTSAPP_ADMIN_EMAIL", ADMIN)
    monkeypatch.setenv("WAHA_URL", "http://fake")
    monkeypatch.setenv("WAHA_API_KEY", "fake-key")
    cfg = auth.AdminAuthConfig(client=None, secret=b"s" * 32, base_url=BASE)
    monkeypatch.setattr(auth, "config_from_env", lambda: cfg)

    bridge = SimpleNamespace(
        get_session_status=AsyncMock(return_value=SessionStatus(connected=True, logged_in=True)),
        get_chats=AsyncMock(return_value=CHATS),
        get_contacts=AsyncMock(return_value=[]),
        list_groups=AsyncMock(return_value=[]),
        get_avatar_url=AsyncMock(return_value=None),
        disconnect=AsyncMock(),
    )

    async def fake_bridge(_request):
        return bridge

    monkeypatch.setattr(admin_app, "_bridge", fake_bridge)
    with TestClient(admin_app.app, base_url=BASE, follow_redirects=False) as c:
        c.cfg = cfg
        yield c


async def _share() -> None:
    """Sam owns 'sam' and shares it with her, offering only SHARED to her."""
    db = state.db
    assert db is not None
    for email in (SAM, HER, STRANGER):
        await db.add_user(email)
    await db.add_account("sam", SAM, "acct-sam")
    await db.grant_account("sam", HER)
    await db.set_chat_access_allowed("sam", SAM, SHARED, True)
    await db.set_chat_access_allowed("sam", SAM, PRIVATE, True)
    await db.set_chat_access_allowed("sam", HER, SHARED, True)


@pytest.fixture
async def shared(client):
    await _share()


def _as(client: TestClient, email: str) -> None:
    client.cookies.set(auth.SESSION_COOKIE, auth.issue_session(client.cfg, email))


def _post(client: TestClient, path: str, **data: str):
    return client.post(path, data=data, headers={"origin": BASE})


async def _allowed(viewer: str) -> set[str]:
    assert state.db is not None
    return await state.db.list_allowed_jids_for("sam", viewer)


# ── what each person sees ────────────────────────────────────────────────────


async def test_owner_sees_every_chat(client, shared):
    _as(client, SAM)
    page = client.get("/chats").text
    assert "Shared Alice" in page and "Private Bob" in page


async def test_a_shared_viewer_sees_only_what_was_made_visible_to_them(client, shared):
    _as(client, HER)
    page = client.get("/chats").text
    assert "Shared Alice" in page
    assert "Private Bob" not in page and PRIVATE not in page
    assert "shared with you" in page.lower()


async def test_a_shared_viewer_gets_no_visibility_switches(client, shared):
    _as(client, HER)
    page = client.get("/chats").text
    assert 'name="is_allowed"' not in page and "/allow" not in page


async def test_owner_gets_switches_for_their_own_view(client, shared):
    _as(client, SAM)
    page = client.get("/chats").text
    assert 'name="is_allowed"' in page and "/allow?viewer=" not in page


# ── the escalation this page must not allow ──────────────────────────────────


async def test_a_viewer_cannot_allow_a_chat_for_themself(client, shared):
    _as(client, HER)
    r = _post(client, f"/chats/{PRIVATE}/allow", is_allowed="on")
    assert r.status_code == 403
    assert await _allowed(HER) == {SHARED}  # what the MCP server would honour


async def test_a_viewer_cannot_allow_via_the_viewer_parameter_either(client, shared):
    _as(client, HER)
    assert _post(client, f"/chats/{PRIVATE}/allow?viewer={HER}", is_allowed="on").status_code == 403
    assert _post(client, f"/chats/{PRIVATE}/allow?viewer={SAM}", is_allowed="on").status_code == 403
    assert await _allowed(HER) == {SHARED} and await _allowed(SAM) == {SHARED, PRIVATE}


async def test_a_viewer_cannot_look_at_or_change_someone_elses_view(client, shared):
    _as(client, HER)
    assert client.get(f"/chats?viewer={SAM}").status_code == 403
    assert client.get("/chats?viewer=ghost@example.com").status_code == 403  # same answer


async def test_a_viewer_cannot_turn_off_what_the_owner_shared_either(client, shared):
    _as(client, HER)
    assert _post(client, f"/chats/{SHARED}/allow").status_code == 403
    assert await _allowed(HER) == {SHARED}


# ── the owner choosing what someone sees ─────────────────────────────────────


async def test_owner_chooses_what_a_viewer_sees(client, shared):
    _as(client, SAM)
    r = _post(client, f"/chats/{PRIVATE}/allow?viewer={HER}", is_allowed="on")
    assert r.status_code == 303
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["viewer"] == [HER]  # stays on her view after saving
    assert await _allowed(HER) == {SHARED, PRIVATE}
    assert await _allowed(SAM) == {SHARED, PRIVATE}  # untouched

    _post(client, f"/chats/{SHARED}/allow?viewer={HER}")  # unchecked = off
    assert await _allowed(HER) == {PRIVATE}


async def test_owner_page_for_a_viewer_shows_their_state_and_a_picker(client, shared):
    _as(client, SAM)
    page = client.get(f"/chats?viewer={HER}").text
    assert f"/chats/{SHARED}/allow?viewer=" in page
    assert 'aria-label="Viewer"' in page and HER in page
    # Her tags are hers: the owner choosing visibility doesn't edit them.
    assert '<form class="meta-form"' not in page


async def test_owner_cannot_manage_someone_without_access_to_the_account(client, shared):
    _as(client, SAM)
    assert client.get(f"/chats?viewer={STRANGER}").status_code == 403
    assert (
        _post(client, f"/chats/{SHARED}/allow?viewer={STRANGER}", is_allowed="on").status_code
        == 403
    )
    assert await _allowed(STRANGER) == set()


async def test_revoking_access_ends_the_viewers_chats_at_once(client, shared):
    assert state.db is not None
    _as(client, HER)
    assert client.get("/chats").status_code == 200
    await state.db.revoke_account("sam", HER)
    assert client.get("/chats").status_code == 403
    assert await _allowed(HER) == set()


# ── tags ─────────────────────────────────────────────────────────────────────


async def test_a_viewer_labels_what_was_shared_with_them(client, shared):
    _as(client, HER)
    assert _post(client, f"/chats/{SHARED}/metadata", tags="friend, work").status_code == 303
    row = await state.db.get_chat_access("sam", HER, SHARED)
    assert row is not None and row["tags"] == ["friend", "work"]
    mine = await state.db.get_chat_access("sam", SAM, SHARED)
    assert mine is not None and mine["tags"] == []  # the owner's labels are separate


async def test_a_viewer_cannot_label_a_chat_that_was_not_shared(client, shared):
    _as(client, HER)
    assert _post(client, f"/chats/{PRIVATE}/metadata", tags="x").status_code == 403
    assert await state.db.get_chat_access("sam", HER, PRIVATE) is None


async def test_owner_cannot_edit_someone_elses_tags(client, shared):
    _as(client, SAM)
    assert _post(client, f"/chats/{SHARED}/metadata?viewer={HER}", tags="x").status_code == 403


# ── landing and navigation ───────────────────────────────────────────────────


async def test_someone_with_only_a_shared_account_is_sent_to_accounts_not_a_403(client, shared):
    _as(client, HER)
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/accounts"
    assert 'href="/">Session' not in client.get("/accounts").text  # no Session link


async def test_the_owner_still_gets_the_session_page_and_link(client, shared):
    _as(client, SAM)
    assert client.get("/").status_code == 200
    assert 'href="/">Session' in client.get("/accounts").text


async def test_pairing_controls_stay_with_the_owner(client, shared):
    _as(client, HER)
    assert _post(client, "/session/disconnect").status_code == 403
    _as(client, SAM)
    assert _post(client, "/session/disconnect").status_code == 303


async def test_accounts_page_links_each_viewer_to_their_chat_choices(client, shared):
    _as(client, SAM)
    page = client.get("/accounts").text
    assert "Choose chats" in page and "viewer%3Dher%40example.com" in page
