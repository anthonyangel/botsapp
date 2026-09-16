"""
Unit tests for MCP tool functions.

Tools import botsapp.state, which conftest.py replaces with mocks,
so these tests never touch a real database, HTTP endpoint, or concrete
bridge (mock_bridge is spec'd against bridge.Bridge, the Protocol
WAHAClient implements).
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.utilities.types import Audio, File, Image

from botsapp.bridge import GroupInfo, SendResult
from botsapp.message_store import ChatNotAllowedError
from botsapp.tools import (
    add_tag,
    clear_tags,
    create_group,
    delete_message,
    disconnect_whatsapp,
    edit_message,
    get_chat_metadata,
    get_group_info,
    get_media,
    leave_group,
    list_chats,
    list_groups,
    list_messages,
    list_newsletters,
    mark_read,
    remove_tag,
    search_messages,
    send_contact,
    send_location,
    send_media,
    send_message,
    send_poll,
    send_reaction,
    set_chat_presence,
    set_group_name,
    set_group_topic,
    update_group_participants,
)

# ── Read tools ────────────────────────────────────────────────────────────────


async def test_list_chats_delegates_to_message_store(mock_message_store: AsyncMock):
    mock_message_store.get_chats.return_value = [{"id": "1", "name": "Alice"}]
    result = await list_chats(limit=10)
    mock_message_store.get_chats.assert_awaited_once_with(limit=10, offset=0)
    assert result == [{"id": "1", "name": "Alice"}]


async def test_list_chats_default_limit(mock_message_store: AsyncMock):
    mock_message_store.get_chats.return_value = []
    await list_chats()
    mock_message_store.get_chats.assert_awaited_once_with(limit=50, offset=0)


async def test_list_chats_clamps_excessive_limit(mock_message_store: AsyncMock):
    # A caller (or a confused MCP client) passing an enormous limit must
    # not be able to force an unbounded-size response — same failure mode
    # as the list_groups field leak, via a different lever (row count
    # instead of row size). See MAX_LIST_LIMIT in tools.py.
    mock_message_store.get_chats.return_value = []
    await list_chats(limit=1_000_000)
    mock_message_store.get_chats.assert_awaited_once_with(limit=200, offset=0)


async def test_list_chats_returns_error_dict_on_exception(mock_message_store: AsyncMock):
    mock_message_store.get_chats.side_effect = RuntimeError("pool closed")
    result = await list_chats()
    assert result == [{"error": "pool closed"}]


async def test_list_chats_by_query_uses_search_chats(mock_message_store: AsyncMock):
    mock_message_store.search_chats.return_value = [{"jid": "a@g.us", "name": "Alice"}]
    result = await list_chats(query="Alice")
    mock_message_store.search_chats.assert_awaited_once_with(query="Alice", limit=50, offset=0)
    assert result == [{"jid": "a@g.us", "name": "Alice"}]


async def test_list_chats_by_tags_uses_db_search_by_tags(
    mock_db: AsyncMock, mock_message_store: AsyncMock
):
    # search_by_tags only locates matching jids — the full chat record
    # (matching every other branch's documented shape: id, jid, name,
    # unread_count, last_message, last_message_at) comes from
    # message_store.get_chat, same as the query/default branches.
    mock_db.search_by_tags.return_value = [{"jid": "a@g.us", "tags": ["vip"]}]
    mock_message_store.get_chat.return_value = {
        "id": "1",
        "jid": "a@g.us",
        "name": "Alice",
        "unread_count": 0,
        "last_message": "hi",
        "last_message_at": "2026-01-01T00:00:00Z",
    }
    result = await list_chats(tags=["vip"])
    mock_db.search_by_tags.assert_awaited_once_with(tags=["vip"])
    mock_message_store.get_chat.assert_awaited_once_with("a@g.us")
    assert result == [
        {
            "id": "1",
            "jid": "a@g.us",
            "name": "Alice",
            "unread_count": 0,
            "last_message": "hi",
            "last_message_at": "2026-01-01T00:00:00Z",
        }
    ]


async def test_list_chats_by_tags_skips_matches_with_no_chat_record(
    mock_db: AsyncMock, mock_message_store: AsyncMock
):
    mock_db.search_by_tags.return_value = [{"jid": "a@g.us", "tags": ["vip"]}]
    mock_message_store.get_chat.return_value = None
    result = await list_chats(tags=["vip"])
    assert result == []


async def test_list_messages_passes_all_params(mock_message_store: AsyncMock):
    mock_message_store.list_messages.return_value = []
    await list_messages(jid="972123@s.whatsapp.net", limit=20, offset=40)
    mock_message_store.list_messages.assert_awaited_once_with(
        jid="972123@s.whatsapp.net", limit=20, offset=40
    )


async def test_list_messages_defaults(mock_message_store: AsyncMock):
    mock_message_store.list_messages.return_value = []
    await list_messages(jid="972123@s.whatsapp.net")
    mock_message_store.list_messages.assert_awaited_once_with(
        jid="972123@s.whatsapp.net", limit=50, offset=0
    )


async def test_list_messages_clamps_excessive_limit(mock_message_store: AsyncMock):
    mock_message_store.list_messages.return_value = []
    await list_messages(jid="972123@s.whatsapp.net", limit=1_000_000)
    mock_message_store.list_messages.assert_awaited_once_with(
        jid="972123@s.whatsapp.net", limit=200, offset=0
    )


async def test_list_messages_not_allowed_returns_error(mock_message_store: AsyncMock):
    mock_message_store.list_messages.side_effect = ChatNotAllowedError(
        "chat 'x@g.us' is not in the allowlist"
    )
    result = await list_messages(jid="x@g.us")
    assert "not in the allowlist" in result[0]["error"]


# ── Write tools ───────────────────────────────────────────────────────────────


async def test_send_message_delegates_to_bridge(mock_bridge: AsyncMock):
    mock_bridge.send_message.return_value = SendResult(success=True, message_id="abc")
    result = await send_message(to="15551234567", text="Hello")
    mock_bridge.send_message.assert_awaited_once_with(to="15551234567", text="Hello")
    assert result["success"] is True
    assert result["message_id"] == "abc"


async def test_send_message_error(mock_bridge: AsyncMock):
    mock_bridge.send_message.side_effect = RuntimeError("network error")
    result = await send_message(to="123", text="hi")
    assert "error" in result


async def test_send_media_delegates(mock_bridge: AsyncMock):
    mock_bridge.send_media.return_value = SendResult(success=True)
    result = await send_media(
        to="15551234567", media_url="http://img.example/a.jpg", media_type="image", caption="hi"
    )
    mock_bridge.send_media.assert_awaited_once_with(
        to="15551234567", media_url="http://img.example/a.jpg", media_type="image", caption="hi"
    )
    assert result["success"] is True


# ── Group tools ───────────────────────────────────────────────────────────────


def _group(
    jid: str = "g1@g.us",
    name: str = "Test",
    topic: str = "",
    participant_count: int = 2,
    participants: list[str] | None = None,
    is_community: bool = False,
    linked_parent_jid: str = "",
    invite_link: str | None = None,
) -> GroupInfo:
    return GroupInfo(
        jid=jid,
        name=name,
        topic=topic,
        participant_count=participant_count,
        participants=participants or [],
        is_community=is_community,
        linked_parent_jid=linked_parent_jid,
        invite_link=invite_link,
    )


async def test_list_groups_delegates_to_bridge(mock_bridge: AsyncMock):
    mock_bridge.list_groups.return_value = [_group()]
    result = await list_groups()
    mock_bridge.list_groups.assert_awaited_once()
    assert result[0]["jid"] == "g1@g.us"
    assert result[0]["name"] == "Test"
    assert result[0]["participant_count"] == 2
    assert result[0]["is_community"] is False


async def test_list_groups_excludes_chats_not_in_allowlist(
    mock_bridge: AsyncMock, mock_db: AsyncMock
):
    mock_db.list_allowed_jids.return_value = {"g1@g.us"}
    mock_bridge.list_groups.return_value = [
        _group(jid="g1@g.us", name="Allowed"),
        _group(jid="g2@g.us", name="Not allowed"),
    ]
    result = await list_groups()
    assert [g["jid"] for g in result] == ["g1@g.us"]


async def test_list_groups_excludes_communities_by_default(mock_bridge: AsyncMock):
    mock_bridge.list_groups.return_value = [
        _group(jid="g1@g.us", name="Community", is_community=True),
        _group(jid="g2@g.us", name="Regular", is_community=False),
    ]
    result = await list_groups()
    assert len(result) == 1
    assert result[0]["name"] == "Regular"


async def test_list_groups_include_communities(mock_bridge: AsyncMock):
    mock_bridge.list_groups.return_value = [
        _group(jid="g1@g.us", name="Community", is_community=True),
        _group(jid="g2@g.us", name="Regular", is_community=False),
    ]
    result = await list_groups(exclude_communities=False)
    assert len(result) == 2
    assert result[0]["is_community"] is True


async def test_list_groups_error(mock_bridge: AsyncMock):
    mock_bridge.list_groups.side_effect = RuntimeError("timeout")
    result = await list_groups()
    assert "error" in result[0]


async def test_list_groups_filters_by_tags(mock_bridge: AsyncMock, mock_db: AsyncMock):
    mock_bridge.list_groups.return_value = [
        _group(jid="g1@g.us", name="Gan Shaked Parents"),
        _group(jid="g2@g.us", name="Unrelated Group"),
    ]
    mock_db.search_by_tags.return_value = [{"jid": "g1@g.us", "tags": ["Gan"], "name": "g1@g.us"}]
    result = await list_groups(tags=["Gan"])
    mock_db.search_by_tags.assert_awaited_once_with(tags=["Gan"])
    assert [g["jid"] for g in result] == ["g1@g.us"]


async def test_list_groups_tags_and_query_combine_with_and(
    mock_bridge: AsyncMock, mock_db: AsyncMock
):
    # Both filters given — a group must satisfy both, not either.
    mock_bridge.list_groups.return_value = [
        _group(jid="g1@g.us", name="Gan Shaked Parents"),
        _group(jid="g2@g.us", name="Gan Shaked Afternoon"),
    ]
    mock_db.search_by_tags.return_value = [
        {"jid": "g1@g.us", "tags": ["Gan"], "name": "g1@g.us"},
        {"jid": "g2@g.us", "tags": ["Gan"], "name": "g2@g.us"},
    ]
    result = await list_groups(tags=["Gan"], query="parents")
    assert [g["jid"] for g in result] == ["g1@g.us"]


async def test_list_groups_tags_no_match_returns_empty(mock_bridge: AsyncMock, mock_db: AsyncMock):
    mock_bridge.list_groups.return_value = [_group(jid="g1@g.us", name="Something Else")]
    mock_db.search_by_tags.return_value = []
    result = await list_groups(tags=["nonexistent"])
    assert result == []


async def test_list_groups_without_tags_skips_tag_lookup(
    mock_bridge: AsyncMock, mock_db: AsyncMock
):
    mock_bridge.list_groups.return_value = [_group()]
    await list_groups()
    mock_db.search_by_tags.assert_not_awaited()


# Regression coverage for the incident where list_groups did
# `results.append(asdict(g))` — dumping the *entire* GroupInfo dataclass,
# including the full `participants` roster and `invite_link`, into a
# supposedly lightweight bulk listing. One real account's list_groups()
# call (29 groups, one with ~2000 members) produced a >100K-character
# response. See the list_groups docstring, which promises a fixed, small
# field set precisely so this can't recur.
_DOCUMENTED_LIST_GROUPS_FIELDS = {
    "jid",
    "name",
    "topic",
    "participant_count",
    "is_community",
    "linked_parent_jid",
}


async def test_list_groups_never_leaks_participants_or_invite_link(mock_bridge: AsyncMock):
    # A "poisoned" group carrying exactly what leaked before: a large
    # member roster and a sensitive join link.
    mock_bridge.list_groups.return_value = [
        _group(
            jid="big@g.us",
            participant_count=2000,
            participants=[f"9725{i:07d}@s.whatsapp.net" for i in range(2000)],
            invite_link="https://chat.whatsapp.com/SuperSecretInviteCode",
        )
    ]
    result = await list_groups()
    assert "participants" not in result[0]
    assert "invite_link" not in result[0]
    assert result[0].keys() == _DOCUMENTED_LIST_GROUPS_FIELDS


async def test_list_groups_result_keys_match_documented_contract(mock_bridge: AsyncMock):
    # Any future field added to GroupInfo (or reversion to asdict(g)) must
    # be a deliberate, reviewed change to both list_groups and this set —
    # not a silent pass-through.
    mock_bridge.list_groups.return_value = [_group()]
    result = await list_groups()
    assert result[0].keys() == _DOCUMENTED_LIST_GROUPS_FIELDS


async def test_list_groups_output_size_bounded_with_many_large_groups(mock_bridge: AsyncMock):
    # Reproduces the reported scale (dozens of groups, one with thousands
    # of members) and asserts the response stays small — the actual
    # symptom that surfaced the bug, not just the field names.
    groups = [
        _group(
            jid=f"g{i}@g.us",
            name=f"Group {i}",
            participant_count=50,
            participants=[f"9725{j:07d}@s.whatsapp.net" for j in range(50)],
        )
        for i in range(29)
    ]
    groups.append(
        _group(
            jid="huge@g.us",
            name="Huge Group",
            participant_count=2000,
            participants=[f"9725{j:07d}@s.whatsapp.net" for j in range(2000)],
            invite_link="https://chat.whatsapp.com/SuperSecretInviteCode",
        )
    )
    mock_bridge.list_groups.return_value = groups
    result = await list_groups()
    # 30 groups of ~six short fields each is a few KB at most; the old
    # asdict()-based version would have serialized well over 100K chars
    # for this exact shape (2000+50*29 member JIDs).
    assert len(json.dumps(result)) < 10_000


async def test_get_group_info_delegates_to_bridge(mock_bridge: AsyncMock):
    mock_bridge.get_group_info.return_value = _group(
        jid="g1@g.us",
        topic="hi",
        participant_count=1,
        participants=["972501@s.whatsapp.net"],
        linked_parent_jid="parent@g.us",
    )
    result = await get_group_info(jid="g1@g.us")
    mock_bridge.get_group_info.assert_awaited_once_with("g1@g.us")
    assert result["participants"] == ["972501@s.whatsapp.net"]
    assert result["participant_count"] == 1
    assert result["is_community"] is False
    assert result["linked_parent_jid"] == "parent@g.us"


async def test_get_group_info_includes_invite_link(mock_bridge: AsyncMock):
    # Unlike list_groups, a single explicitly-requested group's full detail
    # is documented to include its invite_link — this pins that as a
    # deliberate choice rather than an untested asdict() side effect.
    mock_bridge.get_group_info.return_value = _group(
        jid="g1@g.us", invite_link="https://chat.whatsapp.com/abc123"
    )
    result = await get_group_info(jid="g1@g.us")
    assert result["invite_link"] == "https://chat.whatsapp.com/abc123"


async def test_get_group_info_not_allowed_returns_error(mock_bridge: AsyncMock, mock_db: AsyncMock):
    mock_db.list_allowed_jids.return_value = set()
    result = await get_group_info(jid="g1@g.us")
    assert "not in the allowlist" in result["error"]
    mock_bridge.get_group_info.assert_not_awaited()


# ── Chat interactions ─────────────────────────────────────────────────────────


async def test_send_reaction_delegates_to_bridge(mock_bridge: AsyncMock):
    mock_bridge.send_reaction.return_value = SendResult(success=True, message_id="m1")
    await send_reaction(to="123@s.whatsapp.net", message_id="MSGID", emoji="👍")
    mock_bridge.send_reaction.assert_awaited_once_with(
        to="123@s.whatsapp.net", message_id="MSGID", emoji="👍"
    )


async def test_mark_read_delegates_to_bridge(mock_bridge: AsyncMock):
    mock_bridge.mark_read.return_value = SendResult(success=True, details="done")
    await mark_read(to="123@s.whatsapp.net", message_ids=["ID1", "ID2"])
    mock_bridge.mark_read.assert_awaited_once_with(
        to="123@s.whatsapp.net", message_ids=["ID1", "ID2"]
    )


# ── Search tools ─────────────────────────────────────────────────────────


async def test_search_messages_delegates_to_message_store(mock_message_store: AsyncMock):
    mock_message_store.search_messages.return_value = [{"message_id": "m1", "text": "hello"}]
    result = await search_messages(query="hello", jid="123@s.whatsapp.net", limit=10)
    mock_message_store.search_messages.assert_awaited_once_with(
        query="hello", jid="123@s.whatsapp.net", limit=10, offset=0
    )
    assert result[0]["text"] == "hello"


async def test_search_messages_defaults(mock_message_store: AsyncMock):
    mock_message_store.search_messages.return_value = []
    await search_messages(query="test")
    mock_message_store.search_messages.assert_awaited_once_with(
        query="test", jid=None, limit=50, offset=0
    )


async def test_search_messages_clamps_excessive_limit(mock_message_store: AsyncMock):
    mock_message_store.search_messages.return_value = []
    await search_messages(query="test", limit=1_000_000)
    mock_message_store.search_messages.assert_awaited_once_with(
        query="test", jid=None, limit=200, offset=0
    )


async def test_search_messages_error(mock_message_store: AsyncMock):
    mock_message_store.search_messages.side_effect = Exception("db error")
    result = await search_messages(query="x")
    assert "error" in result[0]


async def test_search_messages_not_allowed_returns_error(mock_message_store: AsyncMock):
    mock_message_store.search_messages.side_effect = ChatNotAllowedError(
        "chat 'x@g.us' is not in the allowlist"
    )
    result = await search_messages(query="hi", jid="x@g.us")
    assert "not in the allowlist" in result[0]["error"]


# ── Chat metadata tools ─────────────────────────────────────────────────


async def test_add_tag_merges_into_existing_tags(mock_db: AsyncMock):
    mock_db.get_chat_metadata.return_value = {"jid": "123@s.whatsapp.net", "tags": ["vip"]}
    result = await add_tag(jid="123@s.whatsapp.net", tag="lead")
    mock_db.upsert_chat_metadata.assert_awaited_once_with(
        jid="123@s.whatsapp.net", tags=["vip", "lead"]
    )
    assert result["tags"] == ["vip", "lead"]
    assert result["status"] == "saved"


async def test_add_tag_no_existing_metadata(mock_db: AsyncMock):
    mock_db.get_chat_metadata.return_value = None
    result = await add_tag(jid="123@s.whatsapp.net", tag="lead")
    mock_db.upsert_chat_metadata.assert_awaited_once_with(jid="123@s.whatsapp.net", tags=["lead"])
    assert result["tags"] == ["lead"]


async def test_add_tag_is_idempotent(mock_db: AsyncMock):
    mock_db.get_chat_metadata.return_value = {"jid": "123@s.whatsapp.net", "tags": ["vip"]}
    result = await add_tag(jid="123@s.whatsapp.net", tag="vip")
    mock_db.upsert_chat_metadata.assert_awaited_once_with(jid="123@s.whatsapp.net", tags=["vip"])
    assert result["tags"] == ["vip"]


async def test_add_tag_error(mock_db: AsyncMock):
    mock_db.get_chat_metadata.side_effect = Exception("db error")
    result = await add_tag(jid="123@s.whatsapp.net", tag="vip")
    assert "error" in result


async def test_add_tag_not_allowed_returns_error(mock_db: AsyncMock):
    mock_db.list_allowed_jids.return_value = set()
    result = await add_tag(jid="123@s.whatsapp.net", tag="vip")
    assert "not in the allowlist" in result["error"]
    mock_db.upsert_chat_metadata.assert_not_awaited()


async def test_remove_tag_drops_only_that_tag(mock_db: AsyncMock):
    mock_db.get_chat_metadata.return_value = {
        "jid": "123@s.whatsapp.net",
        "tags": ["vip", "lead"],
    }
    result = await remove_tag(jid="123@s.whatsapp.net", tag="lead")
    mock_db.upsert_chat_metadata.assert_awaited_once_with(jid="123@s.whatsapp.net", tags=["vip"])
    assert result["tags"] == ["vip"]


async def test_remove_tag_missing_tag_is_noop(mock_db: AsyncMock):
    mock_db.get_chat_metadata.return_value = {"jid": "123@s.whatsapp.net", "tags": ["vip"]}
    result = await remove_tag(jid="123@s.whatsapp.net", tag="nonexistent")
    mock_db.upsert_chat_metadata.assert_awaited_once_with(jid="123@s.whatsapp.net", tags=["vip"])
    assert result["tags"] == ["vip"]


async def test_remove_tag_no_existing_metadata(mock_db: AsyncMock):
    mock_db.get_chat_metadata.return_value = None
    result = await remove_tag(jid="123@s.whatsapp.net", tag="vip")
    mock_db.upsert_chat_metadata.assert_awaited_once_with(jid="123@s.whatsapp.net", tags=[])
    assert result["tags"] == []


async def test_remove_tag_error(mock_db: AsyncMock):
    mock_db.get_chat_metadata.side_effect = Exception("db error")
    result = await remove_tag(jid="123@s.whatsapp.net", tag="vip")
    assert "error" in result


async def test_remove_tag_not_allowed_returns_error(mock_db: AsyncMock):
    mock_db.list_allowed_jids.return_value = set()
    result = await remove_tag(jid="123@s.whatsapp.net", tag="vip")
    assert "not in the allowlist" in result["error"]
    mock_db.upsert_chat_metadata.assert_not_awaited()


async def test_clear_tags_saves_empty_list(mock_db: AsyncMock):
    result = await clear_tags(jid="123@s.whatsapp.net")
    mock_db.upsert_chat_metadata.assert_awaited_once_with(jid="123@s.whatsapp.net", tags=[])
    assert result["tags"] == []
    assert result["status"] == "saved"


async def test_clear_tags_error(mock_db: AsyncMock):
    mock_db.upsert_chat_metadata.side_effect = Exception("db error")
    result = await clear_tags(jid="123@s.whatsapp.net")
    assert "error" in result


async def test_clear_tags_not_allowed_returns_error(mock_db: AsyncMock):
    mock_db.list_allowed_jids.return_value = set()
    result = await clear_tags(jid="123@s.whatsapp.net")
    assert "not in the allowlist" in result["error"]
    mock_db.upsert_chat_metadata.assert_not_awaited()


async def test_get_chat_metadata_found(mock_db: AsyncMock):
    mock_db.get_chat_metadata.return_value = {
        "jid": "123@s.whatsapp.net",
        "tags": ["vip"],
        "updated_at": "2024-01-01",
    }
    result = await get_chat_metadata(jid="123@s.whatsapp.net")
    assert result["tags"] == ["vip"]


async def test_get_chat_metadata_not_found(mock_db: AsyncMock):
    mock_db.get_chat_metadata.return_value = None
    result = await get_chat_metadata(jid="missing@s.whatsapp.net")
    assert result["tags"] == []
    assert result["status"] == "no metadata"


async def test_get_chat_metadata_error(mock_db: AsyncMock):
    mock_db.get_chat_metadata.side_effect = Exception("db error")
    result = await get_chat_metadata(jid="123@s.whatsapp.net")
    assert "error" in result


async def test_get_chat_metadata_not_allowed_returns_error(mock_db: AsyncMock):
    mock_db.list_allowed_jids.return_value = set()
    result = await get_chat_metadata(jid="123@s.whatsapp.net")
    assert "not in the allowlist" in result["error"]
    mock_db.get_chat_metadata.assert_not_awaited()


# ── Media ─────────────────────────────────────────────────────────────────


async def test_get_media_returns_image_for_photo(mock_db: AsyncMock, mock_message_store: AsyncMock):
    mock_message_store.get_message_chat_jid.return_value = "allowed@g.us"
    mock_db.list_allowed_jids.return_value = {"allowed@g.us"}
    jpeg_bytes = b"\xff\xd8\xff\xe0fake-jpeg-data"
    mock_message_store.get_media.return_value = {
        "data": jpeg_bytes,
        "mimetype": "image/jpeg",
        "filename": None,
    }

    result = await get_media(message_id="M1")

    mock_message_store.get_message_chat_jid.assert_awaited_once_with(message_id="M1")
    mock_message_store.get_media.assert_awaited_once_with(message_id="M1", chat_jid="allowed@g.us")
    assert isinstance(result, Image)
    assert result.data == jpeg_bytes


async def test_get_media_returns_audio_for_voice_note(
    mock_db: AsyncMock, mock_message_store: AsyncMock, monkeypatch
):
    # WhatsApp voice notes ("ptt" messages) come back from WAHA as
    # audio/ogg with a codec parameter — must be stripped down to a plain
    # "ogg" format, not passed through verbatim.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)  # no transcription attempt
    mock_message_store.get_message_chat_jid.return_value = "allowed@g.us"
    mock_db.list_allowed_jids.return_value = {"allowed@g.us"}
    ogg_bytes = b"OggS-fake-opus-bytes"
    mock_message_store.get_media.return_value = {
        "data": ogg_bytes,
        "mimetype": "audio/ogg; codecs=opus",
        "filename": None,
    }

    result = await get_media(message_id="M1")

    assert isinstance(result, Audio)
    assert result.data == ogg_bytes
    assert result.to_audio_content().mime_type == "audio/ogg"


async def test_get_media_attaches_transcript_for_voice_note(
    mock_db: AsyncMock, mock_message_store: AsyncMock, monkeypatch
):
    # A voice note is unreadable to the model as raw bytes — get_media()
    # attaches a transcript alongside the audio when one's available,
    # rather than leaving Claude with an opaque blob it can't interpret.
    mock_message_store.get_message_chat_jid.return_value = "allowed@g.us"
    mock_db.list_allowed_jids.return_value = {"allowed@g.us"}
    ogg_bytes = b"OggS-fake-opus-bytes"
    mock_message_store.get_media.return_value = {
        "data": ogg_bytes,
        "mimetype": "audio/ogg; codecs=opus",
        "filename": None,
    }
    transcribe_mock = AsyncMock(return_value="Hi, it's on the fifth floor.")
    monkeypatch.setattr("botsapp.tools.transcribe_audio", transcribe_mock)

    result = await get_media(message_id="M1")

    assert isinstance(result, list)
    assert isinstance(result[0], Audio)
    assert result[0].data == ogg_bytes
    assert result[1] == "Hi, it's on the fifth floor."
    transcribe_mock.assert_awaited_once_with(ogg_bytes, "audio/ogg; codecs=opus", None)


async def test_get_media_returns_audio_alone_when_transcription_unavailable(
    mock_db: AsyncMock, mock_message_store: AsyncMock, monkeypatch
):
    # transcribe_audio() itself is best-effort (no API key configured, or
    # the request failed) and returns None in that case — get_media() must
    # still succeed with just the audio, not error or wait forever.
    mock_message_store.get_message_chat_jid.return_value = "allowed@g.us"
    mock_db.list_allowed_jids.return_value = {"allowed@g.us"}
    ogg_bytes = b"OggS-fake-opus-bytes"
    mock_message_store.get_media.return_value = {
        "data": ogg_bytes,
        "mimetype": "audio/ogg; codecs=opus",
        "filename": None,
    }
    monkeypatch.setattr("botsapp.tools.transcribe_audio", AsyncMock(return_value=None))

    result = await get_media(message_id="M1")

    assert isinstance(result, Audio)
    assert result.data == ogg_bytes


async def test_get_media_does_not_attempt_transcription_for_non_audio(
    mock_db: AsyncMock, mock_message_store: AsyncMock, monkeypatch
):
    mock_message_store.get_message_chat_jid.return_value = "allowed@g.us"
    mock_db.list_allowed_jids.return_value = {"allowed@g.us"}
    mock_message_store.get_media.return_value = {
        "data": b"\xff\xd8\xff\xe0fake-jpeg-data",
        "mimetype": "image/jpeg",
        "filename": None,
    }
    transcribe_mock = AsyncMock()
    monkeypatch.setattr("botsapp.tools.transcribe_audio", transcribe_mock)

    result = await get_media(message_id="M1")

    assert isinstance(result, Image)
    transcribe_mock.assert_not_awaited()


async def test_get_media_returns_file_for_video(mock_db: AsyncMock, mock_message_store: AsyncMock):
    # No dedicated MCP content type for video — falls back to a generic
    # File/embedded-resource, with the real mimetype preserved (not File's
    # own "application/<format>" guess).
    mock_message_store.get_message_chat_jid.return_value = "allowed@g.us"
    mock_db.list_allowed_jids.return_value = {"allowed@g.us"}
    mp4_bytes = b"fake-mp4-bytes"
    mock_message_store.get_media.return_value = {
        "data": mp4_bytes,
        "mimetype": "video/mp4",
        "filename": None,
    }

    result = await get_media(message_id="M1")

    assert isinstance(result, File)
    assert result.data == mp4_bytes
    assert result.to_resource_content().resource.mime_type == "video/mp4"


async def test_get_media_not_allowed_raises(mock_db: AsyncMock, mock_message_store: AsyncMock):
    mock_message_store.get_message_chat_jid.return_value = "other@g.us"
    mock_db.list_allowed_jids.return_value = {"allowed@g.us"}

    with pytest.raises(ChatNotAllowedError):
        await get_media(message_id="M1")

    mock_message_store.get_media.assert_not_awaited()


async def test_get_media_unknown_message_id_raises(mock_message_store: AsyncMock):
    # message_id doesn't resolve to any chat at all — must not fall through
    # to fetching media for a chat that was never actually checked.
    mock_message_store.get_message_chat_jid.return_value = None

    with pytest.raises(ChatNotAllowedError):
        await get_media(message_id="unknown")


async def test_get_media_no_media_raises_not_found(
    mock_db: AsyncMock, mock_message_store: AsyncMock
):
    mock_message_store.get_message_chat_jid.return_value = "allowed@g.us"
    mock_db.list_allowed_jids.return_value = {"allowed@g.us"}
    mock_message_store.get_media.return_value = None

    with pytest.raises(FileNotFoundError):
        await get_media(message_id="M1")


# ── Disconnect ──────────────────────────────────────────────────────────


async def test_disconnect_whatsapp(mock_bridge: AsyncMock):
    mock_bridge.disconnect.return_value = SendResult(success=True, details="Logged out")
    result = await disconnect_whatsapp()
    mock_bridge.disconnect.assert_awaited_once()
    assert result["details"] == "Logged out"


async def test_disconnect_whatsapp_error(mock_bridge: AsyncMock):
    mock_bridge.disconnect.side_effect = RuntimeError("no session")
    result = await disconnect_whatsapp()
    assert "error" in result


# ── Chat action tools ────────────────────────────────────────────────────


async def test_edit_message_delegates(mock_bridge: AsyncMock):
    mock_bridge.edit_message.return_value = SendResult(success=True, details="Message updated")
    result = await edit_message(to="123@s.whatsapp.net", message_id="MSG1", text="edited")
    mock_bridge.edit_message.assert_awaited_once_with(
        to="123@s.whatsapp.net", message_id="MSG1", text="edited"
    )
    assert result["details"] == "Message updated"


async def test_edit_message_error(mock_bridge: AsyncMock):
    mock_bridge.edit_message.side_effect = RuntimeError("fail")
    result = await edit_message(to="123", message_id="M1", text="x")
    assert "error" in result


async def test_delete_message_delegates(mock_bridge: AsyncMock):
    mock_bridge.delete_message.return_value = SendResult(success=True, details="Message deleted")
    result = await delete_message(to="123@s.whatsapp.net", message_id="MSG1")
    mock_bridge.delete_message.assert_awaited_once_with(to="123@s.whatsapp.net", message_id="MSG1")
    assert "deleted" in result["details"].lower()


async def test_send_location_delegates(mock_bridge: AsyncMock):
    mock_bridge.send_location.return_value = SendResult(success=True, details="Sent")
    result = await send_location(to="123", latitude=48.858, longitude=2.294, name="Eiffel Tower")
    mock_bridge.send_location.assert_awaited_once_with(
        to="123", latitude=48.858, longitude=2.294, name="Eiffel Tower"
    )
    assert result["details"] == "Sent"


async def test_send_location_default_name(mock_bridge: AsyncMock):
    mock_bridge.send_location.return_value = SendResult(success=True, details="Sent")
    await send_location(to="123", latitude=0.0, longitude=0.0)
    mock_bridge.send_location.assert_awaited_once_with(
        to="123", latitude=0.0, longitude=0.0, name=""
    )


async def test_send_contact_delegates(mock_bridge: AsyncMock):
    mock_bridge.send_contact.return_value = SendResult(success=True, details="Sent")
    result = await send_contact(to="123", name="John", vcard="BEGIN:VCARD\nEND:VCARD")
    mock_bridge.send_contact.assert_awaited_once_with(
        to="123", name="John", vcard="BEGIN:VCARD\nEND:VCARD"
    )
    assert result["details"] == "Sent"


async def test_send_poll_delegates(mock_bridge: AsyncMock):
    mock_bridge.send_poll.return_value = SendResult(success=True, details="Sent")
    result = await send_poll(group_jid="g1@g.us", question="Favorite?", options=["A", "B", "C"])
    mock_bridge.send_poll.assert_awaited_once_with(
        group_jid="g1@g.us", question="Favorite?", options=["A", "B", "C"]
    )
    assert result["details"] == "Sent"


async def test_set_chat_presence_delegates(mock_bridge: AsyncMock):
    mock_bridge.set_chat_presence.return_value = SendResult(success=True, details="set")
    result = await set_chat_presence(to="123", state_value="composing", media="audio")
    mock_bridge.set_chat_presence.assert_awaited_once_with(
        to="123", state="composing", media="audio"
    )
    assert "set" in result["details"].lower()


async def test_set_chat_presence_default_media(mock_bridge: AsyncMock):
    mock_bridge.set_chat_presence.return_value = SendResult(success=True, details="set")
    await set_chat_presence(to="123", state_value="paused")
    mock_bridge.set_chat_presence.assert_awaited_once_with(to="123", state="paused", media="")


# ── Group management tools ───────────────────────────────────────────────


async def test_leave_group_delegates(mock_bridge: AsyncMock):
    mock_bridge.leave_group.return_value = SendResult(success=True, details="Left group")
    result = await leave_group(group_jid="g1@g.us")
    mock_bridge.leave_group.assert_awaited_once_with("g1@g.us")
    assert "left" in result["details"].lower()


async def test_leave_group_error(mock_bridge: AsyncMock):
    mock_bridge.leave_group.side_effect = RuntimeError("fail")
    result = await leave_group(group_jid="g1@g.us")
    assert "error" in result


async def test_create_group_delegates(mock_bridge: AsyncMock):
    mock_bridge.create_group.return_value = _group(jid="new@g.us", name="Test")
    result = await create_group(name="Test", participants=["155", "156"])
    mock_bridge.create_group.assert_awaited_once_with(name="Test", participants=["155", "156"])
    assert result["jid"] == "new@g.us"


async def test_update_group_participants_delegates(mock_bridge: AsyncMock):
    mock_bridge.update_group_participants.return_value = SendResult(success=True, details="done")
    result = await update_group_participants(group_jid="g1@g.us", action="add", phones=["155"])
    mock_bridge.update_group_participants.assert_awaited_once_with(
        group_jid="g1@g.us", action="add", phones=["155"]
    )
    assert result["details"] == "done"


async def test_set_group_name_delegates(mock_bridge: AsyncMock):
    mock_bridge.set_group_name.return_value = SendResult(success=True, details="set")
    await set_group_name(group_jid="g1@g.us", name="New Name")
    mock_bridge.set_group_name.assert_awaited_once_with("g1@g.us", "New Name")


async def test_set_group_topic_delegates(mock_bridge: AsyncMock):
    mock_bridge.set_group_topic.return_value = SendResult(success=True, details="set")
    await set_group_topic(group_jid="g1@g.us", topic="New topic")
    mock_bridge.set_group_topic.assert_awaited_once_with("g1@g.us", "New topic")


async def test_list_newsletters_not_supported():
    result = await list_newsletters()
    assert "error" in result[0]


# ── Elicitation (dangerous tools) ─────────────────────────────────────────


def _mock_ctx(accept: bool = True):
    """Create a mock Context whose elicit() returns accept/decline."""
    ctx = AsyncMock()
    elicit_result = MagicMock()
    elicit_result.action = "accept" if accept else "decline"
    elicit_result.data = accept
    ctx.elicit = AsyncMock(return_value=elicit_result)
    return ctx


async def test_disconnect_whatsapp_elicitation_accepted(mock_bridge: AsyncMock):
    mock_bridge.disconnect.return_value = SendResult(success=True, details="Logged out")
    ctx = _mock_ctx(accept=True)
    result = await disconnect_whatsapp(ctx=ctx)
    ctx.elicit.assert_awaited_once()
    mock_bridge.disconnect.assert_awaited_once()
    assert result["details"] == "Logged out"


async def test_disconnect_whatsapp_elicitation_declined(mock_bridge: AsyncMock):
    ctx = _mock_ctx(accept=False)
    result = await disconnect_whatsapp(ctx=ctx)
    ctx.elicit.assert_awaited_once()
    mock_bridge.disconnect.assert_not_awaited()
    assert result["status"] == "cancelled"


async def test_leave_group_elicitation_declined(mock_bridge: AsyncMock):
    ctx = _mock_ctx(accept=False)
    result = await leave_group(group_jid="g1@g.us", ctx=ctx)
    ctx.elicit.assert_awaited_once()
    mock_bridge.leave_group.assert_not_awaited()
    assert result["status"] == "cancelled"


async def test_delete_message_elicitation_declined(mock_bridge: AsyncMock):
    ctx = _mock_ctx(accept=False)
    result = await delete_message(to="123@s.whatsapp.net", message_id="M1", ctx=ctx)
    ctx.elicit.assert_awaited_once()
    mock_bridge.delete_message.assert_not_awaited()
    assert result["status"] == "cancelled"
