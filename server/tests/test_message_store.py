"""
Unit tests for message_store.py's AllowlistedMessageStore — the wrapper
that enforces the chat allowlist over whatever concrete MessageStore is
passed in (WAHAClient in production; a plain AsyncMock here, since the
wrapper is deliberately implementation-agnostic).
"""

from unittest.mock import AsyncMock

import pytest

from botsapp.message_store import AllowlistedMessageStore, ChatNotAllowedError

# ── AllowlistedMessageStore ─────────────────────────────────────────────────


@pytest.fixture
def inner() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def fake_db() -> AsyncMock:
    db = AsyncMock()
    db.list_allowed_jids = AsyncMock(return_value={"allowed@g.us"})
    return db


@pytest.fixture
def wrapped(inner, fake_db) -> AllowlistedMessageStore:
    return AllowlistedMessageStore(inner, fake_db)


async def test_get_chats_filters_to_allowed(wrapped, inner):
    inner.get_chats.return_value = [
        {"jid": "allowed@g.us", "name": "Family"},
        {"jid": "other@g.us", "name": "Not visible"},
    ]
    result = await wrapped.get_chats(limit=50, offset=0)
    assert result == [{"jid": "allowed@g.us", "name": "Family"}]
    # Fetches unfiltered/unpaginated from the inner store, per the class's
    # documented trade-off — pagination happens after filtering.
    inner.get_chats.assert_awaited_once()
    _, kwargs = inner.get_chats.call_args
    assert kwargs["offset"] == 0


async def test_get_chats_paginates_after_filtering(wrapped, inner, fake_db):
    fake_db.list_allowed_jids.return_value = {"a@g.us", "b@g.us", "c@g.us"}
    inner.get_chats.return_value = [
        {"jid": "a@g.us"},
        {"jid": "b@g.us"},
        {"jid": "c@g.us"},
        {"jid": "not-allowed@g.us"},
    ]
    result = await wrapped.get_chats(limit=2, offset=1)
    assert result == [{"jid": "b@g.us"}, {"jid": "c@g.us"}]


async def test_get_chat_raises_when_not_allowed(wrapped, inner):
    with pytest.raises(ChatNotAllowedError):
        await wrapped.get_chat(jid="other@g.us")
    inner.get_chat.assert_not_awaited()


async def test_get_chat_delegates_when_allowed(wrapped, inner):
    inner.get_chat.return_value = {"jid": "allowed@g.us"}
    result = await wrapped.get_chat(jid="allowed@g.us")
    assert result == {"jid": "allowed@g.us"}
    inner.get_chat.assert_awaited_once_with("allowed@g.us")


async def test_list_messages_raises_when_not_allowed(wrapped, inner):
    with pytest.raises(ChatNotAllowedError):
        await wrapped.list_messages(jid="other@g.us")
    inner.list_messages.assert_not_awaited()


async def test_list_messages_delegates_when_allowed(wrapped, inner):
    inner.list_messages.return_value = [{"message_id": "m1"}]
    result = await wrapped.list_messages(jid="allowed@g.us", limit=10, offset=5)
    assert result == [{"message_id": "m1"}]
    inner.list_messages.assert_awaited_once_with(jid="allowed@g.us", limit=10, offset=5)


async def test_search_messages_with_jid_raises_when_not_allowed(wrapped, inner):
    with pytest.raises(ChatNotAllowedError):
        await wrapped.search_messages(query="hi", jid="other@g.us")
    inner.search_messages.assert_not_awaited()


async def test_search_messages_with_jid_delegates_when_allowed(wrapped, inner):
    inner.search_messages.return_value = [{"message_id": "m1", "chat_jid": "allowed@g.us"}]
    result = await wrapped.search_messages(query="hi", jid="allowed@g.us")
    assert result == [{"message_id": "m1", "chat_jid": "allowed@g.us"}]


async def test_search_messages_without_jid_filters_by_chat(wrapped, inner):
    inner.search_messages.return_value = [
        {"message_id": "m1", "chat_jid": "allowed@g.us"},
        {"message_id": "m2", "chat_jid": "other@g.us"},
    ]
    result = await wrapped.search_messages(query="hi")
    assert result == [{"message_id": "m1", "chat_jid": "allowed@g.us"}]
    _, kwargs = inner.search_messages.call_args
    assert kwargs["jid"] is None


async def test_get_last_message_raises_when_not_allowed(wrapped, inner):
    with pytest.raises(ChatNotAllowedError):
        await wrapped.get_last_message(jid="other@g.us")


async def test_get_last_message_delegates_when_allowed(wrapped, inner):
    inner.get_last_message.return_value = {"message_id": "m1"}
    result = await wrapped.get_last_message(jid="allowed@g.us")
    assert result == {"message_id": "m1"}


async def test_search_chats_filters_to_allowed(wrapped, inner):
    inner.search_chats.return_value = [
        {"jid": "allowed@g.us", "name": "Family"},
        {"jid": "other@g.us", "name": "Not visible"},
    ]
    result = await wrapped.search_chats(query="fam")
    assert result == [{"jid": "allowed@g.us", "name": "Family"}]


async def test_update_media_link_delegates_unfiltered(wrapped, inner):
    await wrapped.update_media_link(message_id="m1", media_link="/x.jpg")
    inner.update_media_link.assert_awaited_once_with(message_id="m1", media_link="/x.jpg")


async def test_get_message_chat_jid_delegates_unfiltered(wrapped, inner):
    # Deliberately not allowlist-checked here — get_media resolves the jid
    # via this method and then checks it explicitly against state.db,
    # since there's no jid to check *before* the lookup happens.
    inner.get_message_chat_jid.return_value = "other@g.us"
    result = await wrapped.get_message_chat_jid(message_id="m1")
    assert result == "other@g.us"
    inner.get_message_chat_jid.assert_awaited_once_with(message_id="m1")
