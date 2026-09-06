"""
Unit tests for WAHAClient — both its bridge.Bridge side (session/write/
group/contact) and its message_store.MessageStore side (chat/message
history), plus the `@c.us` <-> `@s.whatsapp.net` JID normalization that's
WAHA-specific and contained entirely in this module.

Uses aresponses (not aioresponses) to intercept aiohttp calls without
network I/O: it runs a real local aiohttp server and redirects DNS
resolution to it for
the duration of a test, so aiohttp builds its own request/response objects
exactly as it would against a real server — nothing here depends on
aiohttp's internal object shapes the way a monkeypatch-based mock would.
"""

import base64
from collections.abc import AsyncIterator

import pytest
from aiohttp import web

from botsapp.bridge import SessionStatus
from botsapp.waha_client import (
    WAHAClient,
    _from_waha_jid,
    _is_real_message,
    _iso_timestamp,
    _to_waha_jid,
)

BASE_URL = "http://waha.test"
HOST = "waha.test"
API_KEY = "test-key"
SESSION = "default"


@pytest.fixture
async def client() -> AsyncIterator[WAHAClient]:
    c = WAHAClient(base_url=BASE_URL, api_key=API_KEY, session=SESSION)
    yield c
    await c.close()


def _mock(
    ar, method: str, url: str, *, payload=None, status: int = 200, body=None, content_type=None
):
    """Register a response for ``url`` (a full ``f"{BASE_URL}/..."`` string,
    same shape the old aioresponses-based tests used).

    aiohttp's server-side request decodes percent-escapes before exposing
    ``path_qs``, so a plain (unescaped) query string here matches the wire
    request exactly the same way it did for the old aioresponses-based
    tests, even for values containing characters like `@` that urlencode
    escapes on the way out.
    """
    suffix = url[len(BASE_URL) :]
    match_qs = "?" in suffix
    if body is not None:
        response = web.Response(body=body, content_type=content_type, status=status)
    else:
        response = web.json_response(payload, status=status)
    ar.add(HOST, suffix, method, response=response, match_querystring=match_qs)


class _Capture:
    """A response handler that records every request it receives (and its
    decoded JSON body, if any) for inspection, instead of just returning a
    canned response — for tests asserting on what was actually sent.

    A bound method, not a plain closure with a bolted-on attribute: aresponses
    decides whether to await its response callable via
    ``asyncio.iscoroutinefunction``, which — unlike a callable class
    instance's ``__call__`` — correctly recognizes a bound ``async def``
    method as a coroutine function.
    """

    def __init__(self, payload=None, status: int = 200) -> None:
        self._payload = payload if payload is not None else {}
        self._status = status
        self.calls: list[tuple[web.Request, dict | None]] = []

    async def handle(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = None
        self.calls.append((request, body))
        return web.json_response(self._payload, status=self._status)


def _capture(payload=None, status: int = 200) -> _Capture:
    return _Capture(payload=payload, status=status)


# ── JID normalization ────────────────────────────────────────────────────


def test_to_waha_jid_converts_canonical_suffix():
    assert _to_waha_jid("15551234567@s.whatsapp.net") == "15551234567@c.us"


def test_to_waha_jid_leaves_group_jid_unchanged():
    assert _to_waha_jid("123@g.us") == "123@g.us"


def test_to_waha_jid_leaves_status_broadcast_unchanged():
    assert _to_waha_jid("status@broadcast") == "status@broadcast"


def test_from_waha_jid_converts_waha_suffix():
    assert _from_waha_jid("15551234567@c.us") == "15551234567@s.whatsapp.net"


def test_from_waha_jid_leaves_group_jid_unchanged():
    assert _from_waha_jid("123@g.us") == "123@g.us"


def test_jid_roundtrip():
    original = "15551234567@s.whatsapp.net"
    assert _from_waha_jid(_to_waha_jid(original)) == original


# ── Timestamp normalization / message-noise filtering ───────────────────────


def test_iso_timestamp_converts_epoch_seconds():
    assert _iso_timestamp(100) == "1970-01-01T00:01:40+00:00"


def test_iso_timestamp_none_for_falsy_input():
    assert _iso_timestamp(None) is None
    assert _iso_timestamp(0) is None
    assert _iso_timestamp("") is None


def test_iso_timestamp_none_for_unparseable_input():
    assert _iso_timestamp("not-a-number") is None


@pytest.mark.parametrize(
    "msg_type", ["chat", "image", "video", "vcard", "unknown", "revoked", None]
)
def test_is_real_message_keeps_content_and_unclassified_types(msg_type):
    assert _is_real_message({"_data": {"type": msg_type}}) is True


@pytest.mark.parametrize(
    "msg_type",
    [
        "call_log",
        "gp2",
        "notification",
        "notification_template",
        "group_notification",
        "broadcast_notification",
        "e2e_notification",
        "protocol",
        "ciphertext",
    ],
)
def test_is_real_message_excludes_protocol_and_system_noise(msg_type):
    assert _is_real_message({"_data": {"type": msg_type}}) is False


def test_is_real_message_handles_missing_data():
    assert _is_real_message({}) is True


# ── Auth ──────────────────────────────────────────────────────────────────


async def test_requests_include_api_key_header(client, aresponses):
    handler = _capture({"status": "WORKING", "me": {}})
    aresponses.add(HOST, f"/api/sessions/{SESSION}", "get", response=handler.handle)
    await client.get_session_status()
    request, _ = handler.calls[0]
    assert request.headers["X-Api-Key"] == API_KEY


# ── Session / pairing ─────────────────────────────────────────────────────


async def test_get_session_status_working(client, aresponses):
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/sessions/{SESSION}",
        payload={"status": "WORKING", "me": {"id": "1@c.us", "pushName": "Anthony"}},
    )
    status = await client.get_session_status()
    assert status == SessionStatus(
        connected=True, logged_in=True, qr_code=None, jid="1@s.whatsapp.net", name="Anthony"
    )


async def test_get_session_status_scan_qr_code(client, aresponses):
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/sessions/{SESSION}",
        payload={"status": "SCAN_QR_CODE", "me": None, "qr": "base64data"},
    )
    status = await client.get_session_status()
    assert status.connected is True
    assert status.logged_in is False
    assert status.qr_code == "base64data"


async def test_get_session_status_stopped(client, aresponses):
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/sessions/{SESSION}",
        payload={"status": "STOPPED", "me": None},
    )
    status = await client.get_session_status()
    assert status.connected is False
    assert status.logged_in is False


async def test_get_session_status_failed_carries_error(client, aresponses):
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/sessions/{SESSION}",
        payload={"status": "FAILED", "me": None, "error": "boom"},
    )
    status = await client.get_session_status()
    assert status.errors == ["boom"]


async def test_get_session_status_unreachable():
    # No aresponses fixture here — it globally redirects DNS resolution
    # for every aiohttp connection for the life of the test, so it can't
    # be used to simulate an unreachable host. A real closed local port
    # gives an immediate, genuine ConnectionRefusedError instead.
    unreachable = WAHAClient(base_url="http://127.0.0.1:1", api_key=API_KEY, session=SESSION)
    try:
        status = await unreachable.get_session_status()
    finally:
        await unreachable.close()
    assert any("Cannot reach WAHA" in e for e in status.errors)


async def test_get_session_status_scan_qr_code_fetches_qr_itself(client, aresponses):
    # Real WAHA never embeds "qr" in GET /api/sessions/{name} — a plain
    # poll (page reload, background refresh) must fetch the dedicated
    # auth/qr endpoint itself or the QR never renders outside the one-off
    # connect() call. Regression test for the "clicked Connect, no QR
    # shown" bug.
    #
    # format=image is real PNG bytes (confirmed live), not JSON — unlike
    # format=raw, which returns the *decoded* scanner payload (comma-
    # separated ref/keys), not an encoded image, so it can't feed an
    # <img src="data:..."> directly. See _get_qr_base64's docstring.
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/sessions/{SESSION}",
        payload={"status": "SCAN_QR_CODE", "me": None},  # note: no "qr" key
    )
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/auth/qr?format=image",
        body=b"\x89PNG\r\n\x1a\nfakepngbytes",
        content_type="image/png",
    )
    status = await client.get_session_status()
    assert status.qr_code == base64.b64encode(b"\x89PNG\r\n\x1a\nfakepngbytes").decode("ascii")


async def test_get_session_status_404_means_not_connected_not_an_error(client, aresponses):
    # WAHA 404s GET /api/sessions/{name} until that session has been
    # explicitly created — the normal state on a fresh deployment, before
    # anyone has ever pressed "Connect WhatsApp". Must not surface as an
    # error.
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/sessions/{SESSION}",
        status=404,
        payload={"message": "Session not found", "error": "Not Found", "statusCode": 404},
    )
    status = await client.get_session_status()
    assert status == SessionStatus()
    assert status.errors == []


async def test_connect_creates_session_when_none_exists(client, aresponses):
    _mock(aresponses, "get", f"{BASE_URL}/api/sessions/{SESSION}", status=404, payload={})
    _mock(aresponses, "post", f"{BASE_URL}/api/sessions", payload={"status": "STARTING"})
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/sessions/{SESSION}",
        payload={"status": "SCAN_QR_CODE", "me": None},
    )
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/auth/qr?format=image",
        body=b"pngbytes",
        content_type="image/png",
    )
    status = await client.connect()
    assert status.qr_code == base64.b64encode(b"pngbytes").decode("ascii")


async def test_connect_restarts_stopped_session(client, aresponses):
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/sessions/{SESSION}",
        payload={"status": "STOPPED", "me": None},
    )
    _mock(
        aresponses,
        "post",
        f"{BASE_URL}/api/sessions/{SESSION}/restart",
        payload={"status": "STARTING"},
    )
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/sessions/{SESSION}",
        payload={"status": "WORKING", "me": {"id": "1@c.us"}},
    )
    status = await client.connect()
    assert status.logged_in is True


async def test_connect_restarts_failed_session(client, aresponses):
    # Regression test: /start alone silently no-ops on a FAILED session (it
    # only handles STOPPED), so a session stuck FAILED after a botched
    # pairing attempt (e.g. WEBJS's post-auth re-injection timeout) never
    # recovered when the user clicked "Connect" again.
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/sessions/{SESSION}",
        payload={"status": "FAILED", "me": None},
    )
    _mock(
        aresponses,
        "post",
        f"{BASE_URL}/api/sessions/{SESSION}/restart",
        payload={"status": "STARTING"},
    )
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/sessions/{SESSION}",
        payload={"status": "SCAN_QR_CODE", "me": None},
    )
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/auth/qr?format=image",
        body=b"pngbytes",
        content_type="image/png",
    )
    status = await client.connect()
    assert status.qr_code == base64.b64encode(b"pngbytes").decode("ascii")


async def test_disconnect_success(client, aresponses):
    _mock(aresponses, "post", f"{BASE_URL}/api/sessions/{SESSION}/logout", payload={})
    result = await client.disconnect()
    assert result.success is True


# ── Messaging ─────────────────────────────────────────────────────────────


async def test_send_message_converts_jid_and_posts(client, aresponses):
    handler = _capture({"id": "m1"})
    aresponses.add(HOST, "/api/sendText", "post", response=handler.handle)
    result = await client.send_message(to="15551234567@s.whatsapp.net", text="hi")
    _, body = handler.calls[0]
    assert body == {
        "session": SESSION,
        "chatId": "15551234567@c.us",
        "text": "hi",
    }
    assert result.success is True
    assert result.message_id == "m1"


async def test_set_chat_presence_composing_starts_typing(client, aresponses):
    handler = _capture({})
    aresponses.add(HOST, "/api/startTyping", "post", response=handler.handle)
    await client.set_chat_presence(to="1@s.whatsapp.net", state="composing")
    _, body = handler.calls[0]
    assert body == {"session": SESSION, "chatId": "1@c.us"}


async def test_set_chat_presence_paused_stops_typing(client, aresponses):
    handler = _capture({})
    aresponses.add(HOST, "/api/stopTyping", "post", response=handler.handle)
    await client.set_chat_presence(to="1@s.whatsapp.net", state="paused")
    assert len(handler.calls) == 1


# ── Groups ────────────────────────────────────────────────────────────────


async def test_list_groups_translates(client, aresponses):
    # Real shape (confirmed live): "id" is a nested {server, user,
    # _serialized} object, and everything that matters
    # (subject/description/participants/community flag) lives under
    # "groupMetadata", not at the top level.
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/groups",
        payload=[
            {
                "id": {"server": "g.us", "user": "g1", "_serialized": "g1@g.us"},
                "name": "Family",
                "groupMetadata": {
                    "subject": "Family",
                    "desc": "topic",
                    "isParentGroup": False,
                    "participants": [
                        {"id": {"server": "c.us", "user": "a", "_serialized": "a@c.us"}},
                        {"id": {"server": "c.us", "user": "b", "_serialized": "b@c.us"}},
                    ],
                },
            },
        ],
    )
    groups = await client.list_groups()
    assert groups[0].jid == "g1@g.us"
    assert groups[0].name == "Family"
    assert groups[0].topic == "topic"
    assert groups[0].participant_count == 2
    assert groups[0].participants == ["a@c.us", "b@c.us"]


async def test_list_groups_excludes_left_participants(client, aresponses):
    # list_groups() must agree with get_group_info() on participant count
    # for the same group — both exclude departed ("left") members.
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/groups",
        payload=[
            {
                "id": {"server": "g.us", "user": "g1", "_serialized": "g1@g.us"},
                "name": "Family",
                "groupMetadata": {
                    "subject": "Family",
                    "participants": [
                        {"id": {"server": "c.us", "user": "a", "_serialized": "a@c.us"}},
                        {
                            "id": {"server": "c.us", "user": "b", "_serialized": "b@c.us"},
                            "role": "left",
                        },
                    ],
                },
            },
        ],
    )
    groups = await client.list_groups()
    assert groups[0].participants == ["a@c.us"]
    assert groups[0].participant_count == 1


async def test_get_group_info_fetches_participants_v2(client, aresponses):
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/groups/g1@g.us",
        payload={
            "id": {"server": "g.us", "user": "g1", "_serialized": "g1@g.us"},
            "name": "Family",
            "groupMetadata": {"subject": "Family"},
        },
    )
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/groups/g1@g.us/participants/v2",
        payload=[{"id": "a@c.us", "role": "admin"}, {"id": "b@c.us", "role": "left"}],
    )
    info = await client.get_group_info("g1@g.us")
    assert info.participants == ["a@c.us"]  # "left" participants excluded
    assert info.participant_count == 1


# ── Contacts ─────────────────────────────────────────────────────────────


async def test_get_contacts_excludes_groups_and_translates_jid(client, aresponses):
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/contacts/all?session={SESSION}",
        payload=[
            {"id": "1@c.us", "name": "Bob", "isGroup": False, "isWAContact": True},
            {"id": "g1@g.us", "name": "A Group", "isGroup": True},
        ],
    )
    contacts = await client.get_contacts()
    assert len(contacts) == 1
    assert contacts[0].jid == "1@s.whatsapp.net"
    assert contacts[0].name == "Bob"


async def test_check_phone_translates_numberExists(client, aresponses):
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/contacts/check-exists?phone=155&session={SESSION}",
        payload={"numberExists": True, "chatId": "155@c.us"},
    )
    results = await client.check_phone(["155"])
    assert results[0].is_on_whatsapp is True
    assert results[0].jid == "155@s.whatsapp.net"


async def test_get_avatar_url(client, aresponses):
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/contacts/profile-picture?contactId=155@c.us&session={SESSION}",
        payload={"profilePictureURL": "https://cdn.example/x.jpg"},
    )
    url = await client.get_avatar_url("155")
    assert url == "https://cdn.example/x.jpg"


async def test_get_avatar_url_translates_canonical_jid(client, aresponses):
    # Regression: our canonical @s.whatsapp.net jid was passed straight
    # through as WAHA's contactId unmodified, so it never matched anything
    # in WAHA (which only knows @c.us) — every canonical-suffixed direct
    # chat's avatar silently failed.
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/contacts/profile-picture?contactId=155@c.us&session={SESSION}",
        payload={"profilePictureURL": "https://cdn.example/x.jpg"},
    )
    url = await client.get_avatar_url("155@s.whatsapp.net")
    assert url == "https://cdn.example/x.jpg"


async def test_get_avatar_url_passes_lid_jid_through(client, aresponses):
    # @lid (privacy-preserving id, common for group participants) isn't
    # @s.whatsapp.net and must pass through unchanged, not get treated as
    # a bare phone number and given a bogus @c.us suffix.
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/contacts/profile-picture?contactId=99@lid&session={SESSION}",
        payload={"profilePictureURL": "https://cdn.example/y.jpg"},
    )
    url = await client.get_avatar_url("99@lid")
    assert url == "https://cdn.example/y.jpg"


# ── Chat/message history (MessageStore) ──────────────────────────────────


async def test_get_chats_translates_to_common_shape(client, aresponses):
    # Real shape (confirmed live): "id" is nested, and lastMessage is
    # whatsapp-web.js's raw Store.Msg object nested under
    # "_data" — not the clean flat shape /messages returns.
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats?limit=50&offset=0&sortBy=conversationTimestamp&sortOrder=desc",
        payload=[
            {
                "id": {"server": "c.us", "user": "1", "_serialized": "1@c.us"},
                "name": "Bob",
                "timestamp": 100,
                "lastMessage": {"_data": {"body": "hi", "type": "chat", "t": 100}},
            },
        ],
    )
    chats = await client.get_chats()
    assert chats == [
        {
            "jid": "1@s.whatsapp.net",
            "name": "Bob",
            "last_message": "hi",
            "last_message_at": "1970-01-01T00:01:40+00:00",
        }
    ]


async def test_get_chats_skips_non_text_last_message_body(client, aresponses):
    # A media message's raw "body" is a base64 blob (or empty), not a
    # caption — must not be surfaced as the chat's last_message text.
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats?limit=50&offset=0&sortBy=conversationTimestamp&sortOrder=desc",
        payload=[
            {
                "id": {"server": "c.us", "user": "1", "_serialized": "1@c.us"},
                "name": "Bob",
                "timestamp": 100,
                "lastMessage": {"_data": {"body": "/9j/4AAQ...", "type": "image", "t": 100}},
            },
        ],
    )
    chats = await client.get_chats()
    assert chats[0]["last_message"] is None


async def test_list_messages_translates_to_common_shape(client, aresponses):
    # list_messages over-fetches (see waha_client.py) so it can filter
    # protocol/system noise without short-changing the requested page —
    # limit=50/offset=0 becomes limit=170/offset=0 at the wire.
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats/1@c.us/messages?limit=170&offset=0",
        payload=[
            {
                "id": "m1",
                "from": "1@c.us",
                "body": "hi",
                "timestamp": 100,
                "hasMedia": False,
                "_data": {"type": "chat"},
            },
        ],
    )
    _mock(aresponses, "get", f"{BASE_URL}/api/{SESSION}/chats/1@c.us", payload={"name": "Bob"})
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/contacts/all?session={SESSION}",
        payload=[{"id": "1@c.us", "name": "Bob", "isGroup": False}],
    )
    messages = await client.list_messages("1@s.whatsapp.net")
    assert messages[0]["message_id"] == "m1"
    assert messages[0]["from"] == "1@s.whatsapp.net"
    assert messages[0]["sender_name"] == "Bob"
    assert messages[0]["chat_name"] == "Bob"
    assert messages[0]["text"] == "hi"
    assert messages[0]["message_type"] == "text"
    assert messages[0]["timestamp"] == "1970-01-01T00:01:40+00:00"


async def test_list_messages_falls_back_to_jid_when_name_unresolvable(client, aresponses):
    # No matching contact/chat name available (fetch fails, or none set) —
    # sender_name/chat_name must fall back to the raw jid, same as "from"
    # and "chat_jid" always have, rather than surfacing an error or None.
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats/1@c.us/messages?limit=170&offset=0",
        payload=[{"id": "m1", "from": "1@c.us", "body": "hi", "_data": {"type": "chat"}}],
    )
    _mock(aresponses, "get", f"{BASE_URL}/api/{SESSION}/chats/1@c.us", status=404, payload={})
    _mock(aresponses, "get", f"{BASE_URL}/api/contacts/all?session={SESSION}", payload=[])
    messages = await client.list_messages("1@s.whatsapp.net")
    assert messages[0]["sender_name"] == "1@s.whatsapp.net"
    assert messages[0]["chat_name"] == "1@s.whatsapp.net"


async def test_list_messages_resolves_sender_name_through_lid(client, aresponses):
    # Group participants are increasingly surfaced as a privacy-preserving
    # @lid rather than a phone number — a plain phone-keyed contacts
    # lookup can't match that at all until it's translated via WAHA's own
    # lid->phone mapping first.
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats/g1@g.us/messages?limit=170&offset=0",
        payload=[{"id": "m1", "from": "999@lid", "body": "hi", "_data": {"type": "chat"}}],
    )
    _mock(aresponses, "get", f"{BASE_URL}/api/{SESSION}/chats/g1@g.us", payload={"name": "Chabura"})
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/contacts/all?session={SESSION}",
        payload=[{"id": "1@c.us", "name": "Rafi", "isGroup": False}],
    )
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/lids?limit=1000&offset=0",
        payload=[{"lid": "999@lid", "pn": "1@c.us"}],
    )
    messages = await client.list_messages("g1@g.us")
    assert messages[0]["from"] == "999@lid"  # unchanged — the raw wire id
    assert messages[0]["sender_name"] == "Rafi"


async def test_list_messages_resolved_lid_falls_back_to_phone_without_contact(client, aresponses):
    # The lid resolves to a real phone number, but that number isn't a
    # saved contact — still more useful than the opaque @lid itself.
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats/g1@g.us/messages?limit=170&offset=0",
        payload=[{"id": "m1", "from": "999@lid", "body": "hi", "_data": {"type": "chat"}}],
    )
    _mock(aresponses, "get", f"{BASE_URL}/api/{SESSION}/chats/g1@g.us", payload={"name": "Chabura"})
    _mock(aresponses, "get", f"{BASE_URL}/api/contacts/all?session={SESSION}", payload=[])
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/lids?limit=1000&offset=0",
        payload=[{"lid": "999@lid", "pn": "1@c.us"}],
    )
    messages = await client.list_messages("g1@g.us")
    assert messages[0]["sender_name"] == "1@s.whatsapp.net"


async def test_list_messages_unresolvable_lid_falls_back_to_raw_lid(client, aresponses):
    # No lid->phone mapping known for this sender at all (WAHA's own
    # resolution can lag) — sender_name falls back to the raw @lid, not an
    # error, same as any other unresolvable jid.
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats/g1@g.us/messages?limit=170&offset=0",
        payload=[{"id": "m1", "from": "999@lid", "body": "hi", "_data": {"type": "chat"}}],
    )
    _mock(aresponses, "get", f"{BASE_URL}/api/{SESSION}/chats/g1@g.us", payload={"name": "Chabura"})
    _mock(aresponses, "get", f"{BASE_URL}/api/contacts/all?session={SESSION}", payload=[])
    _mock(aresponses, "get", f"{BASE_URL}/api/{SESSION}/lids?limit=1000&offset=0", payload=[])
    messages = await client.list_messages("g1@g.us")
    assert messages[0]["sender_name"] == "999@lid"


async def test_list_messages_excludes_system_and_call_log_noise(client, aresponses):
    # Confirmed live: WAHA's /messages feed
    # interleaves real chat content with protocol/system entries — call
    # logs, group membership changes, etc. — that must not be handed to
    # the caller as if they were genuine (empty) text messages.
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats/1@c.us/messages?limit=170&offset=0",
        payload=[
            {"id": "m1", "from": "1@c.us", "body": "real", "_data": {"type": "chat"}},
            {"id": "m2", "from": "1@c.us", "body": "", "_data": {"type": "call_log"}},
            {"id": "m3", "from": "1@c.us", "body": "", "_data": {"type": "gp2"}},
        ],
    )
    _mock(aresponses, "get", f"{BASE_URL}/api/{SESSION}/chats/1@c.us", payload={"name": "Bob"})
    _mock(aresponses, "get", f"{BASE_URL}/api/contacts/all?session={SESSION}", payload=[])
    messages = await client.list_messages("1@s.whatsapp.net")
    assert [m["message_id"] for m in messages] == ["m1"]


async def test_list_messages_paginates_after_filtering(client, aresponses):
    # The requested page must be 50 *real* messages, not 50 raw feed items
    # some of which get dropped as noise — regression test for exactly
    # that short-changing bug.
    raw = [{"id": "noise", "body": "", "_data": {"type": "call_log"}}] * 5 + [
        {"id": f"m{i}", "from": "1@c.us", "body": f"msg{i}", "_data": {"type": "chat"}}
        for i in range(10)
    ]
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats/1@c.us/messages?limit=32&offset=0",
        payload=raw,
    )
    _mock(aresponses, "get", f"{BASE_URL}/api/{SESSION}/chats/1@c.us", payload={"name": "Bob"})
    _mock(aresponses, "get", f"{BASE_URL}/api/contacts/all?session={SESSION}", payload=[])
    messages = await client.list_messages("1@s.whatsapp.net", limit=3, offset=1)
    assert [m["message_id"] for m in messages] == ["m1", "m2", "m3"]


async def test_search_messages_scoped_to_jid_excludes_noise_and_normalizes(client, aresponses):
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats/1@c.us/messages?limit=1000&offset=0",
        payload=[
            {
                "id": "m1",
                "from": "1@c.us",
                "body": "call me back",
                "timestamp": 100,
                "_data": {"type": "chat"},
            },
            {"id": "m2", "from": "1@c.us", "body": "", "_data": {"type": "call_log"}},
            {"id": "m3", "from": "1@c.us", "body": "no match here", "_data": {"type": "chat"}},
        ],
    )
    _mock(aresponses, "get", f"{BASE_URL}/api/{SESSION}/chats/1@c.us", payload={"name": "Bob"})
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/contacts/all?session={SESSION}",
        payload=[{"id": "1@c.us", "name": "Bob", "isGroup": False}],
    )
    results = await client.search_messages(query="call", jid="1@s.whatsapp.net")
    assert [m["message_id"] for m in results] == ["m1"]
    assert results[0]["timestamp"] == "1970-01-01T00:01:40+00:00"
    assert results[0]["sender_name"] == "Bob"
    assert results[0]["chat_name"] == "Bob"


async def test_search_messages_across_chats_sorts_by_timestamp_desc(client, aresponses):
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats"
        "?limit=1000&offset=0&sortBy=conversationTimestamp&sortOrder=desc",
        payload=[
            {"id": {"server": "c.us", "user": "1", "_serialized": "1@c.us"}, "name": "A"},
            {"id": {"server": "c.us", "user": "2", "_serialized": "2@c.us"}, "name": "B"},
        ],
    )
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats/1@c.us/messages?limit=1000&offset=0",
        payload=[{"id": "old", "from": "1@c.us", "body": "match", "timestamp": 100}],
    )
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats/2@c.us/messages?limit=1000&offset=0",
        payload=[{"id": "new", "from": "2@c.us", "body": "match", "timestamp": 200}],
    )
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/contacts/all?session={SESSION}",
        payload=[{"id": "2@c.us", "name": "Bob", "isGroup": False}],
    )
    results = await client.search_messages(query="match")
    assert [m["message_id"] for m in results] == ["new", "old"]
    # chat_name comes from the get_chats call already made to build the
    # search's chat list — "A"/"B" here, not a separate per-chat lookup.
    assert results[0]["chat_name"] == "B"
    assert results[1]["chat_name"] == "A"
    # sender_name resolves per-message from the one bulk contacts fetch:
    # "new" (from 2@c.us) has a matching contact, "old" (from 1@c.us) doesn't.
    assert results[0]["sender_name"] == "Bob"
    assert results[1]["sender_name"] == "1@s.whatsapp.net"


async def test_get_message_chat_jid_finds_owning_chat(client, aresponses):
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats"
        "?limit=1000&offset=0&sortBy=conversationTimestamp&sortOrder=desc",
        payload=[
            {"id": {"server": "c.us", "user": "1", "_serialized": "1@c.us"}, "name": "A"},
            {"id": {"server": "c.us", "user": "2", "_serialized": "2@c.us"}, "name": "B"},
        ],
    )
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats/1@c.us/messages?limit=1000&offset=0",
        payload=[{"id": "m1", "from": "1@c.us", "body": "hi"}],
    )
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats/2@c.us/messages?limit=1000&offset=0",
        payload=[{"id": "m2", "from": "2@c.us", "body": "hey"}],
    )
    result = await client.get_message_chat_jid(message_id="m2")
    assert result == "2@s.whatsapp.net"


async def test_get_message_chat_jid_returns_none_when_not_found(client, aresponses):
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats"
        "?limit=1000&offset=0&sortBy=conversationTimestamp&sortOrder=desc",
        payload=[{"id": {"server": "c.us", "user": "1", "_serialized": "1@c.us"}, "name": "A"}],
    )
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats/1@c.us/messages?limit=1000&offset=0",
        payload=[{"id": "m1", "from": "1@c.us", "body": "hi"}],
    )
    result = await client.get_message_chat_jid(message_id="missing")
    assert result is None


async def test_search_chats_filters_by_name_or_jid(client, aresponses):
    _mock(
        aresponses,
        "get",
        f"{BASE_URL}/api/{SESSION}/chats"
        "?limit=1000&offset=0&sortBy=conversationTimestamp&sortOrder=desc",
        payload=[
            {
                "id": {"server": "c.us", "user": "1", "_serialized": "1@c.us"},
                "name": "Family Group",
            },
            {"id": {"server": "c.us", "user": "2", "_serialized": "2@c.us"}, "name": "Work"},
        ],
    )
    result = await client.search_chats(query="family")
    assert len(result) == 1
    assert result[0]["name"] == "Family Group"
