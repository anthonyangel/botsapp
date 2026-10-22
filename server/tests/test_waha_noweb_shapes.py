"""
WAHA payloads as the NOWEB (Baileys) engine produces them, run through the
mappers and then through the MCP-facing models.

IMPORTANT: these fixtures are derived from reading source, not captured from
a live session — devlikeapro/waha (src/structures/*.dto.ts,
core/engines/noweb/session.noweb.core.ts: toWAMessage, getFromToParticipant,
buildMessageId; store/NowebPersistentStore.getChats) and devlikeapro/Baileys
at fork-master-2026-04-28 (WAProto WebMessageInfo / Conversation, Types/Chat).
Replace them with scrubbed live captures when available. They exist so the
mapper keeps working on NOWEB's shapes, which the older WEBJS-shaped tests in
test_waha_client.py never exercised.
"""

from collections.abc import AsyncIterator

import pytest
from aiohttp import web

from botsapp.tools import Chat, Message
from botsapp.waha_client import WAHAClient, _is_real_message, _iso_timestamp

BASE_URL = "http://waha.test"
HOST = "waha.test"
SESSION = "default"


@pytest.fixture
async def client() -> AsyncIterator[WAHAClient]:
    c = WAHAClient(base_url=BASE_URL, api_key="k", session=SESSION)
    yield c
    await c.close()


def _mock(ar, url: str, *, payload=None, status: int = 200):
    suffix = url[len(BASE_URL) :]
    ar.add(
        HOST,
        suffix,
        "get",
        response=web.json_response(payload, status=status),
        match_querystring="?" in suffix,
    )


def _noweb_message(**overrides):
    """A message as NOWEB's toWAMessage emits it: ``_data`` is the raw Baileys
    WebMessageInfo (key/message/messageTimestamp) with no ``type`` or ``body``."""
    base = {
        "id": "false_1@c.us_ABC",
        "timestamp": 100,
        "from": "1@c.us",
        "fromMe": False,
        "source": "app",
        "to": None,
        "participant": None,
        "body": "hello",
        "hasMedia": False,
        "media": None,
        "ack": 3,
        "ackName": "READ",
        "_data": {
            "key": {"remoteJid": "1@s.whatsapp.net", "fromMe": False, "id": "ABC"},
            "message": {"conversation": "hello"},
            "messageTimestamp": 100,
        },
    }
    return {**base, **overrides}


# ── Chats ────────────────────────────────────────────────────────────────


async def test_chat_row_uses_conversation_timestamp(client, aresponses):
    # Baileys Chat rows: plain-string id, conversationTimestamp, no "timestamp",
    # no lastMessage, unreadCount deleted by WAHA.
    _mock(
        aresponses,
        f"{BASE_URL}/api/{SESSION}/chats?limit=50&offset=0&sortBy=conversationTimestamp&sortOrder=desc",
        payload=[{"id": "1@c.us", "name": "Bob", "conversationTimestamp": 100}],
    )
    [chat] = await client.get_chats()
    assert chat["last_message_at"] == "1970-01-01T00:01:40+00:00"
    assert chat["last_message"] is None
    Chat.model_validate(chat)


async def test_chat_timestamp_as_protobuf_long(client, aresponses):
    _mock(
        aresponses,
        f"{BASE_URL}/api/{SESSION}/chats?limit=50&offset=0&sortBy=conversationTimestamp&sortOrder=desc",
        payload=[
            {
                "id": "1@c.us",
                "name": "Bob",
                "conversationTimestamp": {"low": 100, "high": 0, "unsigned": True},
            }
        ],
    )
    [chat] = await client.get_chats()
    assert chat["last_message_at"] == "1970-01-01T00:01:40+00:00"


def test_iso_timestamp_rejects_malformed_long():
    assert _iso_timestamp({"low": "x"}) is None


# ── Last-message enrichment ──────────────────────────────────────────────


async def test_with_last_messages_fetches_latest_real_text(client, aresponses):
    _mock(
        aresponses,
        f"{BASE_URL}/api/{SESSION}/chats/1@c.us/messages?limit=5&offset=0",
        payload=[
            _noweb_message(body=None, _data={"messageStubType": 20, "key": {}}),
            _noweb_message(body="the real one"),
        ],
    )
    [chat] = await client.with_last_messages([{"jid": "1@s.whatsapp.net", "name": "Bob"}])
    assert chat["last_message"] == "the real one"


async def test_with_last_messages_leaves_media_and_failures_null(client, aresponses):
    _mock(
        aresponses,
        f"{BASE_URL}/api/{SESSION}/chats/1@c.us/messages?limit=5&offset=0",
        payload=[_noweb_message(body="", hasMedia=True)],
    )
    _mock(
        aresponses,
        f"{BASE_URL}/api/{SESSION}/chats/2@c.us/messages?limit=5&offset=0",
        status=500,
        payload={},
    )
    chats = await client.with_last_messages(
        [
            {"jid": "1@s.whatsapp.net", "name": "A"},
            {"jid": "2@s.whatsapp.net", "name": "B"},
        ]
    )
    assert [c["last_message"] for c in chats] == [None, None]


async def test_with_last_messages_skips_rows_that_already_have_one(client):
    # No HTTP mocked: any request would fail the test.
    rows = [{"jid": "1@s.whatsapp.net", "name": "A", "last_message": "hi"}]
    assert await client.with_last_messages(rows) == rows


# ── Noise filtering ──────────────────────────────────────────────────────


def test_noweb_stub_messages_are_noise():
    assert not _is_real_message({"_data": {"messageStubType": 20, "key": {}}})


def test_noweb_protocol_only_messages_are_noise():
    assert not _is_real_message(
        {"_data": {"message": {"protocolMessage": {"type": 0}, "messageContextInfo": {}}}}
    )
    assert not _is_real_message({"_data": {"message": {"senderKeyDistributionMessage": {}}}})


def test_noweb_content_messages_are_real():
    assert _is_real_message(_noweb_message())
    assert _is_real_message({"_data": {"message": {"imageMessage": {}}}})
    # No content info at all: pass through rather than risk dropping real data.
    assert _is_real_message({"id": "m1"})


def test_webjs_notification_types_are_still_noise():
    assert not _is_real_message({"_data": {"type": "notification_template"}})


# ── Group sender ─────────────────────────────────────────────────────────


async def test_group_message_sender_comes_from_participant(client, aresponses):
    # NOWEB: "from" is the group (key.remoteJid), the sender is "participant".
    group_message = _noweb_message(
        id="false_g1@g.us_ABC_999@lid",
        **{"from": "g1@g.us", "participant": "999@lid", "to": "g1@g.us"},
    )
    _mock(
        aresponses,
        f"{BASE_URL}/api/{SESSION}/chats/g1@g.us/messages?limit=170&offset=0",
        payload=[group_message],
    )
    _mock(aresponses, f"{BASE_URL}/api/{SESSION}/chats/g1@g.us", payload={"name": "Chabura"})
    _mock(
        aresponses,
        f"{BASE_URL}/api/contacts/all?session={SESSION}",
        payload=[{"id": "5@c.us", "name": "Dana", "isGroup": False}],
    )
    _mock(
        aresponses,
        f"{BASE_URL}/api/{SESSION}/lids?limit=1000&offset=0",
        payload=[{"lid": "999@lid", "pn": "5@c.us"}],
    )
    [message] = await client.list_messages("g1@g.us")
    assert message["from"] == "999@lid"
    assert message["sender_name"] == "Dana"
    assert message["chat_jid"] == "g1@g.us"
    Message.model_validate(message)


async def test_direct_message_still_uses_from(client, aresponses):
    _mock(
        aresponses,
        f"{BASE_URL}/api/{SESSION}/chats/1@c.us/messages?limit=170&offset=0",
        payload=[_noweb_message()],
    )
    _mock(aresponses, f"{BASE_URL}/api/{SESSION}/chats/1@c.us", payload={"name": "Bob"})
    _mock(aresponses, f"{BASE_URL}/api/contacts/all?session={SESSION}", payload=[])
    _mock(aresponses, f"{BASE_URL}/api/{SESSION}/lids?limit=1000&offset=0", payload=[])
    [message] = await client.list_messages("1@s.whatsapp.net")
    assert message["from"] == "1@s.whatsapp.net"
    assert message["text"] == "hello"
    assert message["message_type"] == "text"
    Message.model_validate(message)
