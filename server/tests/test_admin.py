"""
Route tests for the admin UI (botsapp.admin.app). Bridge-agnostic:
state.bridge is mocked against bridge.Bridge, same as test_tools.py, so
these tests never assume a concrete client.

Bypasses the real lifespan's DB/bridge connections (no SQLite file or live
bridge needed to run these) by monkeypatching DatabaseManager.connect/
ensure_schema before the Starlette TestClient triggers startup, then
injecting mocks into botsapp.state / admin.app._raw_store directly —
the same pattern conftest.py uses for the MCP server's own tools.
"""

from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

import botsapp.admin.app as admin_app
import botsapp.admin.auth as admin_auth
import botsapp.state as state
from botsapp.bridge import ContactInfo, GroupInfo, SessionStatus
from botsapp.db import DatabaseManager

_LOGGED_IN_STATUS = SessionStatus(
    connected=True, logged_in=True, jid="972@s.whatsapp.net", name="Family Phone"
)


@pytest.fixture
def client(monkeypatch):
    async def fake_connect(self):
        self._conn = object()
        return self._conn

    async def fake_ensure_schema(self):
        pass

    monkeypatch.setattr(DatabaseManager, "connect", fake_connect)
    monkeypatch.setattr(DatabaseManager, "ensure_schema", fake_ensure_schema)
    monkeypatch.setattr(DatabaseManager, "close", AsyncMock())
    monkeypatch.setenv("DATABASE_URL", "fake.db")
    monkeypatch.setenv("WAHA_URL", "http://fake")
    monkeypatch.setenv("WAHA_API_KEY", "fake-key")

    dev_config = admin_auth.AdminAuthConfig(
        client=None, secret=b"", base_url="", dev_user="admin@example.com"
    )
    monkeypatch.setattr(admin_auth, "config_from_env", lambda: dev_config)

    with TestClient(admin_app.app) as c:
        # Replace the real bridge the lifespan built with a mock, and give
        # the module its own fake unfiltered store — same idea as
        # conftest.py's _patch_state, scoped to this app instead.
        state.bridge = AsyncMock()
        state.bridge.get_session_status.return_value = _LOGGED_IN_STATUS
        state.bridge.get_contacts.return_value = []
        state.bridge.list_groups.return_value = []
        state.bridge.get_avatar_url.return_value = None
        # Sign in as an admin who owns the default account (auth is covered
        # in test_admin_auth.py); chat_db stands in for the per-viewer handle.
        state.db.get_user = AsyncMock(return_value={"email": "admin@example.com", "is_admin": True})
        primary = {"id": "default", "label": "Primary", "owner_email": "admin@example.com"}
        state.db.list_accounts_for_user = AsyncMock(return_value=[primary])
        state.db.list_accounts = AsyncMock(return_value=[primary])
        state.db.get_account = AsyncMock(return_value=primary)
        state.db.list_grants = AsyncMock(return_value=[])

        async def _bridge(_request):
            return state.bridge

        monkeypatch.setattr(admin_app, "_bridge", _bridge)
        chat_db = AsyncMock()
        chat_db.get_chat_metadata.return_value = None
        monkeypatch.setattr(admin_app, "_chat_db", lambda _request, _viewer=None: chat_db)
        c.chat_db = chat_db
        # Module-level, populated fresh by each /chats GET (see its own
        # docstring) — reset between tests so one test's community jid
        # can't leak into the next's redirect-tab lookup.
        admin_app._community_jids.clear()
        state.bridge.get_chats.return_value = []
        yield c


def test_session_page_shows_qr_when_not_logged_in(client):
    state.bridge.get_session_status.return_value = SessionStatus(
        connected=True, logged_in=False, qr_code="data:image/png;base64,AAAA"
    )
    r = client.get("/")
    assert r.status_code == 200
    assert "Waiting for QR scan" in r.text


def test_session_page_shows_connected(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Family Phone" in r.text


def test_session_page_shows_active_bridge(client):
    r = client.get("/")
    assert "WAHA" in r.text


def test_session_page_shows_connect_button_when_never_attempted(client):
    state.bridge.get_session_status.return_value = SessionStatus(connected=False, logged_in=False)
    r = client.get("/")
    assert r.status_code == 200
    assert "Connect WhatsApp" in r.text


def test_loading_session_page_never_triggers_connect(client):
    # Regression test: GET / used to call connect_or_get_status, which
    # re-issued a fresh QR (and killed the bridge's channel for whatever QR
    # was already mid-scan) on every single page load/refresh — the actual
    # cause of "QR linking isn't working". / must only ever poll status.
    state.bridge.get_session_status.return_value = SessionStatus(
        connected=True, logged_in=False, qr_code="data:image/png;base64,AAAA"
    )
    client.get("/")
    client.get("/")
    client.get("/")
    state.bridge.connect.assert_not_awaited()


def test_session_status_json_reflects_current_state(client):
    state.bridge.get_session_status.return_value = SessionStatus(
        connected=True, logged_in=False, qr_code="data:image/png;base64,AAAA"
    )
    r = client.get("/session/status.json")
    assert r.status_code == 200
    body = r.json()
    assert body["logged_in"] is False
    assert body["qr_code"] == "data:image/png;base64,AAAA"
    state.bridge.connect.assert_not_awaited()


def test_session_status_json_reports_unreachable_bridge_as_error(client):
    # A well-behaved Bridge.get_session_status() never raises (see
    # WAHAClient) — it's already captured in .errors.
    state.bridge.get_session_status.return_value = SessionStatus(
        errors=["Cannot reach the bridge: no route to host"]
    )
    r = client.get("/session/status.json")
    assert r.status_code == 200
    assert "no route to host" in r.json()["errors"][0]


def test_connect_button_triggers_the_actual_connect_attempt(client):
    state.bridge.get_session_status.return_value = SessionStatus(connected=False, logged_in=False)
    state.bridge.connect.return_value = SessionStatus(connected=True, logged_in=False, qr_code="x")
    r = client.post("/session/connect", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    state.bridge.connect.assert_awaited_once()


def test_chats_page_shows_not_connected_state_when_logged_out(client):
    state.bridge.get_session_status.return_value = SessionStatus(connected=False, logged_in=False)
    r = client.get("/chats")
    assert r.status_code == 200
    assert "isn't connected" in r.text
    # Shouldn't even try to enrich chat/group data if there's nothing to show.
    state.bridge.get_contacts.assert_not_awaited()
    state.bridge.list_groups.assert_not_awaited()


def test_chats_page_lists_direct_chats_with_contact_name_and_metadata(client):
    state.bridge.get_chats.return_value = [
        {"jid": "155@s.whatsapp.net", "name": "155"},
    ]
    state.bridge.get_contacts.return_value = [
        ContactInfo(jid="155@s.whatsapp.net", name="Bob Smith", phone_number="155"),
    ]
    client.chat_db.get_chat_metadata.return_value = {"is_allowed": True, "tags": ["family"]}
    r = client.get("/chats")
    assert r.status_code == 200
    assert "Bob Smith" in r.text
    assert "family" in r.text


def test_chats_page_lists_groups_from_live_bridge_list_even_without_history(client):
    # No message history for this group at all — still must appear, since
    # you should be able to allow a group before it's ever posted anything.
    state.bridge.get_chats.return_value = []
    state.bridge.list_groups.return_value = [
        GroupInfo(
            jid="g1@g.us",
            name="Family Group",
            topic="Just us",
            participant_count=2,
            participants=["a@s.whatsapp.net", "b@s.whatsapp.net"],
            is_community=False,
        ),
    ]
    r = client.get("/chats")
    assert r.status_code == 200
    assert "Family Group" in r.text
    assert "2 members" in r.text
    assert "Just us" in r.text


def test_chats_page_splits_communities_into_their_own_tab(client):
    # A WhatsApp Community is structurally a group (jid also ends "@g.us")
    # but its own participant count is meaningless (just its admins, not
    # real membership — see _community_row's docstring) — it gets its own
    # tab, not a badge bolted onto the Groups table.
    state.bridge.list_groups.return_value = [
        GroupInfo(jid="g1@g.us", name="Community HQ", is_community=True, participant_count=1),
        GroupInfo(jid="g2@g.us", name="Regular Group", is_community=False, participant_count=5),
    ]
    r = client.get("/chats")
    assert r.status_code == 200
    assert "#community-panel" in r.text
    assert ">Communities<" in r.text

    community_start = r.text.index('id="community-panel"')
    community_end = r.text.index('id="newsletter-panel"')
    community_section = r.text[community_start:community_end]
    assert "Community HQ" in community_section
    assert "Regular Group" not in community_section
    assert ">Members<" not in community_section  # no member-count column at all

    groups_section = r.text[r.text.index('id="groups-panel"') : community_start]
    assert "Regular Group" in groups_section
    assert "5 members" in groups_section
    assert "Community HQ" not in groups_section


def test_chats_page_shows_groups_loaded_gap(client):
    # Regression test for the diagnosed live gap: WAHA's NOWEB engine can
    # know about far more group *chats* (every real group jid ends
    # "@g.us") than it has full group metadata for. Surface the gap
    # instead of silently under-showing.
    state.bridge.get_chats.return_value = [
        {"jid": "g1@g.us", "name": "g1"},
        {"jid": "g2@g.us", "name": "g2"},  # no metadata from list_groups()
    ]
    state.bridge.list_groups.return_value = [
        GroupInfo(jid="g1@g.us", name="Group One"),
    ]
    r = client.get("/chats")
    assert "Loaded 1/2 groups" in r.text


def test_chats_page_hides_groups_loaded_note_once_caught_up(client):
    state.bridge.get_chats.return_value = [
        {"jid": "g1@g.us", "name": "g1"},
    ]
    state.bridge.list_groups.return_value = [
        GroupInfo(jid="g1@g.us", name="Group One"),
    ]
    r = client.get("/chats")
    assert "groups loaded" not in r.text.lower()


def test_chats_page_hides_status_tab_when_never_seen(client):
    # Some bridges (WAHA, confirmed live) never surface a status@broadcast
    # chat at all — nothing to toggle, so the tab is omitted entirely
    # rather than showing a dead "nothing here" panel.
    r = client.get("/chats")
    assert "#status-panel" not in r.text
    assert ">Status<" not in r.text


def test_chats_page_shows_status_tab_when_seen(client):
    state.bridge.get_chats.return_value = [{"jid": "status@broadcast", "name": ""}]
    r = client.get("/chats")
    assert "Status/Stories" in r.text
    assert ">Status<" in r.text


def test_chats_page_separates_newsletters_into_their_own_tab(client):
    # @newsletter (WhatsApp Channels) jids must not be lumped into the
    # direct-chats tab — they get their own, alongside Chats/Groups.
    state.bridge.get_chats.return_value = [
        {"jid": "123@newsletter", "name": "Tech News"},
        {"jid": "155@s.whatsapp.net", "name": "155"},
    ]
    r = client.get("/chats")
    assert r.status_code == 200
    assert "Tech News" in r.text
    assert "#newsletter-panel" in r.text
    assert ">Newsletters<" in r.text


def test_chats_page_shows_country_flag_for_direct_chat(client):
    state.bridge.get_chats.return_value = [
        {"jid": "972501234567@s.whatsapp.net", "name": "972501234567"},
    ]
    r = client.get("/chats")
    assert "🇮🇱" in r.text
    assert "+972" in r.text


def test_chats_page_omits_country_flag_for_unresolvable_jid(client):
    # An @lid contact never resolved to a phone jid — no crash, just no flag.
    state.bridge.get_chats.return_value = [
        {"jid": "12345@lid", "name": "Mystery"},
    ]
    r = client.get("/chats")
    assert r.status_code == 200
    assert "Mystery" in r.text


def test_chats_page_shows_placeholder_when_empty(client):
    r = client.get("/chats")
    assert r.status_code == 200
    assert "No direct chats yet" in r.text
    assert "any groups yet" in r.text


def test_chats_page_shows_active_bridge(client):
    r = client.get("/chats")
    assert "WAHA" in r.text


def test_toggle_allowed_on(client):
    r = client.post("/chats/g1@g.us/allow", data={"is_allowed": "on"}, follow_redirects=False)
    assert r.status_code == 303
    # Regression: redirect must land back on the tab the jid actually
    # belongs to (a group here) — not always Chats, or ticking a checkbox
    # on the Groups tab flips the page back to Chats underneath the user.
    assert r.headers["location"] == "/chats#groups-panel"
    client.chat_db.set_chat_allowed.assert_awaited_once_with(jid="g1@g.us", is_allowed=True)


def test_toggle_allowed_redirects_to_chats_tab_for_direct_chat(client):
    r = client.post(
        "/chats/155@s.whatsapp.net/allow", data={"is_allowed": "on"}, follow_redirects=False
    )
    assert r.headers["location"] == "/chats#direct-panel"


def test_toggle_allowed_redirects_to_community_tab(client):
    # _classify() can't tell a community from an ordinary group on jid
    # alone (both end "@g.us") — a prior /chats load must have populated
    # _community_jids for this redirect to land on the right tab.
    state.bridge.list_groups.return_value = [
        GroupInfo(jid="g1@g.us", name="Community HQ", is_community=True),
    ]
    client.get("/chats")
    r = client.post("/chats/g1@g.us/allow", data={"is_allowed": "on"}, follow_redirects=False)
    assert r.headers["location"] == "/chats#community-panel"


def test_toggle_allowed_redirects_to_status_tab(client):
    r = client.post(
        "/chats/status@broadcast/allow", data={"is_allowed": "on"}, follow_redirects=False
    )
    assert r.headers["location"] == "/chats#status-panel"


def test_toggle_allowed_off_when_unchecked(client):
    # An unchecked checkbox simply isn't submitted — no "is_allowed" key at all.
    client.post("/chats/g1@g.us/allow", data={}, follow_redirects=False)
    client.chat_db.set_chat_allowed.assert_awaited_once_with(jid="g1@g.us", is_allowed=False)


def test_save_metadata_parses_comma_separated_tags(client):
    r = client.post(
        "/chats/g1@g.us/metadata",
        data={"tags": " family , core "},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/chats#groups-panel"
    client.chat_db.upsert_chat_metadata.assert_awaited_once_with(
        jid="g1@g.us", tags=["family", "core"]
    )


def test_save_metadata_empty_tags(client):
    client.post("/chats/g1@g.us/metadata", data={"tags": ""}, follow_redirects=False)
    client.chat_db.upsert_chat_metadata.assert_awaited_once_with(jid="g1@g.us", tags=[])
