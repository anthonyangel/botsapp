"""
Unit tests for DatabaseManager (chat metadata + allowlist only — chat/
message history lives in message_store.py, tested in test_message_store.py).

Runs against a real temp-file SQLite database via ``tmp_path`` rather than
mocking aiosql internals. aiosqlite is fast enough that this costs nothing
per test, and it exercises the real JSON/bool coercion in db.py's
_row_to_dict rather than assuming it works.
"""

from collections.abc import AsyncIterator

import pytest

from botsapp.db import DatabaseManager

# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def db(tmp_path) -> AsyncIterator[DatabaseManager]:
    manager = DatabaseManager(str(tmp_path / "test.db"))
    await manager.connect()
    await manager.ensure_schema()
    yield manager
    await manager.close()


# ── connection guard ─────────────────────────────────────────────────────────


def test_connection_raises_when_not_connected():
    manager = DatabaseManager("unused.db")
    with pytest.raises(RuntimeError, match="not connected"):
        _ = manager.connection


# ── chat metadata ────────────────────────────────────────────────────────


async def test_get_chat_metadata_returns_none_when_missing(db: DatabaseManager):
    result = await db.get_chat_metadata(jid="missing@s.whatsapp.net")
    assert result is None


async def test_upsert_then_get_round_trips(db: DatabaseManager):
    await db.upsert_chat_metadata(jid="123@s.whatsapp.net", tags=["vip", "lead"])
    result = await db.get_chat_metadata(jid="123@s.whatsapp.net")
    assert result is not None
    assert result["tags"] == ["vip", "lead"]
    assert result["is_allowed"] is False  # upsert_chat_metadata never touches this


async def test_upsert_overwrites_existing_tags(db: DatabaseManager):
    await db.upsert_chat_metadata(jid="123@s.whatsapp.net", tags=["vip"])
    await db.upsert_chat_metadata(jid="123@s.whatsapp.net", tags=["family"])
    result = await db.get_chat_metadata(jid="123@s.whatsapp.net")
    assert result is not None
    assert result["tags"] == ["family"]


async def test_upsert_does_not_clear_is_allowed(db: DatabaseManager):
    await db.set_chat_allowed(jid="123@s.whatsapp.net", is_allowed=True)
    await db.upsert_chat_metadata(jid="123@s.whatsapp.net", tags=["vip"])
    result = await db.get_chat_metadata(jid="123@s.whatsapp.net")
    assert result is not None
    assert result["is_allowed"] is True


async def test_empty_tags_round_trip_as_empty_list(db: DatabaseManager):
    await db.upsert_chat_metadata(jid="123@s.whatsapp.net", tags=[])
    result = await db.get_chat_metadata(jid="123@s.whatsapp.net")
    assert result is not None
    assert result["tags"] == []


# ── search_by_tags ────────────────────────────────────────────────────────


async def test_search_by_tags_matches_any_overlap(db: DatabaseManager):
    await db.set_chat_allowed(jid="a@g.us", is_allowed=True)
    await db.upsert_chat_metadata(jid="a@g.us", tags=["vip", "family"])
    await db.set_chat_allowed(jid="b@g.us", is_allowed=True)
    await db.upsert_chat_metadata(jid="b@g.us", tags=["work"])

    result = await db.search_by_tags(tags=["vip"])
    assert [r["jid"] for r in result] == ["a@g.us"]


async def test_search_by_tags_is_case_insensitive(db: DatabaseManager):
    await db.set_chat_allowed(jid="a@g.us", is_allowed=True)
    await db.upsert_chat_metadata(jid="a@g.us", tags=["Shaked"])
    result = await db.search_by_tags(tags=["shaked"])
    assert [r["jid"] for r in result] == ["a@g.us"]


async def test_search_by_tags_excludes_disallowed_chats(db: DatabaseManager):
    # Never allowed — tagged but shouldn't surface via tag search.
    await db.upsert_chat_metadata(jid="a@g.us", tags=["vip"])
    result = await db.search_by_tags(tags=["vip"])
    assert result == []


async def test_search_by_tags_falls_back_name_to_jid(db: DatabaseManager):
    # No contacts data to resolve a display name from — name is just the jid.
    await db.set_chat_allowed(jid="a@g.us", is_allowed=True)
    await db.upsert_chat_metadata(jid="a@g.us", tags=["vip"])
    result = await db.search_by_tags(tags=["vip"])
    assert result[0]["name"] == "a@g.us"


async def test_search_by_tags_empty_when_no_match(db: DatabaseManager):
    await db.set_chat_allowed(jid="a@g.us", is_allowed=True)
    await db.upsert_chat_metadata(jid="a@g.us", tags=["vip"])
    result = await db.search_by_tags(tags=["nonexistent"])
    assert result == []


# ── allowlist ─────────────────────────────────────────────────────────────


async def test_list_allowed_jids_returns_set(db: DatabaseManager):
    await db.set_chat_allowed(jid="a@s.whatsapp.net", is_allowed=True)
    await db.set_chat_allowed(jid="b@g.us", is_allowed=True)
    result = await db.list_allowed_jids()
    assert result == {"a@s.whatsapp.net", "b@g.us"}


async def test_list_allowed_jids_empty(db: DatabaseManager):
    result = await db.list_allowed_jids()
    assert result == set()


async def test_set_chat_allowed_false_removes_from_allowlist(db: DatabaseManager):
    await db.set_chat_allowed(jid="a@g.us", is_allowed=True)
    await db.set_chat_allowed(jid="a@g.us", is_allowed=False)
    assert await db.list_allowed_jids() == set()


async def test_set_chat_allowed_does_not_touch_tags(db: DatabaseManager):
    await db.upsert_chat_metadata(jid="a@g.us", tags=["vip"])
    await db.set_chat_allowed(jid="a@g.us", is_allowed=True)
    result = await db.get_chat_metadata(jid="a@g.us")
    assert result is not None
    assert result["tags"] == ["vip"]
    assert result["is_allowed"] is True
