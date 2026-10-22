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


# ── email allowlist ──────────────────────────────────────────────────────
# Who may authenticate to the MCP server at all (app.py's
# allowed_family_email) — see docs/decisions/0015-email-allowlist-in-db.md.
# Distinct from the chat allowlist above.


async def test_is_email_allowed_false_when_empty(db: DatabaseManager):
    assert await db.is_email_allowed("anthony@angelfamily.net") is False


async def test_add_allowed_email_then_is_allowed(db: DatabaseManager):
    await db.add_allowed_email("anthony@angelfamily.net")
    assert await db.is_email_allowed("anthony@angelfamily.net") is True


async def test_is_email_allowed_is_case_and_whitespace_insensitive(db: DatabaseManager):
    await db.add_allowed_email("  Anthony@AngelFamily.NET  ")
    assert await db.is_email_allowed("anthony@angelfamily.net") is True
    assert await db.is_email_allowed("ANTHONY@ANGELFAMILY.NET") is True


async def test_add_allowed_email_is_idempotent(db: DatabaseManager):
    await db.add_allowed_email("anthony@angelfamily.net")
    await db.add_allowed_email("anthony@angelfamily.net")
    assert await db.list_allowed_emails() == ["anthony@angelfamily.net"]


async def test_remove_allowed_email(db: DatabaseManager):
    await db.add_allowed_email("anthony@angelfamily.net")
    await db.remove_allowed_email("anthony@angelfamily.net")
    assert await db.is_email_allowed("anthony@angelfamily.net") is False


async def test_remove_allowed_email_missing_is_a_noop(db: DatabaseManager):
    await db.remove_allowed_email("nobody@example.com")  # must not raise
    assert await db.list_allowed_emails() == []


async def test_list_allowed_emails_empty(db: DatabaseManager):
    assert await db.list_allowed_emails() == []


# ── multi-tenancy: users, accounts, grants, per-viewer chat access (0025) ────


async def _two_users(db: DatabaseManager) -> None:
    await db.bootstrap("me@example.com", "default")
    await db.add_user("her@example.com")


async def test_email_allowlist_is_backed_by_users(db: DatabaseManager):
    await db.add_allowed_email("Her@Example.com")
    assert await db.is_email_allowed("her@example.com")
    user = await db.get_user("her@example.com")
    assert user is not None and user["is_admin"] is False


async def test_legacy_allowed_emails_backfilled_into_users(tmp_path):
    first = DatabaseManager(str(tmp_path / "legacy.db"))
    await first.connect()
    await first.ensure_schema()
    await first.connection.execute("INSERT INTO allowed_emails (email) VALUES ('old@example.com')")
    await first.connection.commit()
    await first.ensure_schema()  # next start
    assert await first.is_email_allowed("old@example.com")
    await first.close()


async def test_bootstrap_adopts_legacy_chat_metadata_and_is_idempotent(db: DatabaseManager):
    await db.set_chat_allowed("a@g.us", True)
    await db.upsert_chat_metadata("a@g.us", ["fam"])
    await db.bootstrap("me@example.com", "default")
    await db.bootstrap("me@example.com", "default")
    user = await db.get_user("me@example.com")
    assert user is not None and user["is_admin"] is True
    assert [a["id"] for a in await db.list_accounts()] == ["default"]
    assert await db.list_allowed_jids_for("default", "me@example.com") == {"a@g.us"}
    rows = await db.search_by_tags_for("default", "me@example.com", ["FAM"])
    assert [r["jid"] for r in rows] == ["a@g.us"]


async def test_visibility_is_per_viewer(db: DatabaseManager):
    await _two_users(db)
    await db.set_chat_access_allowed("default", "me@example.com", "a@g.us", True)
    await db.grant_account("default", "her@example.com")
    assert await db.list_allowed_jids_for("default", "her@example.com") == set()
    await db.set_chat_access_allowed("default", "her@example.com", "b@g.us", True)
    assert await db.list_allowed_jids_for("default", "her@example.com") == {"b@g.us"}
    assert await db.list_allowed_jids_for("default", "me@example.com") == {"a@g.us"}


async def test_tags_are_per_viewer(db: DatabaseManager):
    await _two_users(db)
    await db.grant_account("default", "her@example.com")
    for who, tag in (("me@example.com", "mine"), ("her@example.com", "hers")):
        await db.set_chat_access_allowed("default", who, "a@g.us", True)
        await db.upsert_chat_access_tags("default", who, "a@g.us", [tag])
    assert await db.search_by_tags_for("default", "her@example.com", ["mine"]) == []
    assert len(await db.search_by_tags_for("default", "her@example.com", ["hers"])) == 1


async def test_no_grant_means_no_access_and_no_writes(db: DatabaseManager):
    await _two_users(db)
    assert await db.list_allowed_jids_for("default", "her@example.com") == set()
    with pytest.raises(PermissionError):
        await db.set_chat_access_allowed("default", "her@example.com", "a@g.us", True)
    with pytest.raises(PermissionError):
        await db.upsert_chat_access_tags("default", "her@example.com", "a@g.us", ["x"])


async def test_revoke_hides_and_drops_viewer_rows(db: DatabaseManager):
    await _two_users(db)
    await db.grant_account("default", "her@example.com")
    await db.set_chat_access_allowed("default", "her@example.com", "b@g.us", True)
    await db.revoke_account("default", "her@example.com")
    assert await db.list_allowed_jids_for("default", "her@example.com") == set()
    await db.grant_account("default", "her@example.com")  # re-grant starts empty
    assert await db.list_allowed_jids_for("default", "her@example.com") == set()


async def test_accounts_for_user_covers_owned_and_granted(db: DatabaseManager):
    await _two_users(db)
    await db.add_account("hers", "her@example.com", "acct-hers")
    assert [a["id"] for a in await db.list_accounts_for_user("her@example.com")] == ["hers"]
    await db.grant_account("default", "her@example.com")
    ids = [a["id"] for a in await db.list_accounts_for_user("her@example.com")]
    assert sorted(ids) == ["default", "hers"]
    assert [a["id"] for a in await db.list_accounts_for_user("me@example.com")] == ["default"]


async def test_cannot_remove_user_who_owns_an_account(db: DatabaseManager):
    await _two_users(db)
    with pytest.raises(ValueError, match="owns"):
        await db.remove_user("me@example.com")
    await db.grant_account("default", "her@example.com")
    await db.remove_user("her@example.com")
    assert await db.list_grants("default") == []
    assert not await db.is_email_allowed("her@example.com")


async def test_account_requires_known_owner(db: DatabaseManager):
    with pytest.raises(ValueError):
        await db.add_account("x", "ghost@example.com", "acct-x")


async def test_delete_account_removes_grants_and_all_viewers_access(db: DatabaseManager):
    await _two_users(db)
    await db.add_account("hers", "her@example.com", "acct-hers")
    await db.grant_account("hers", "me@example.com")
    await db.set_chat_access_allowed("hers", "me@example.com", "a@g.us", True)
    await db.set_chat_access_allowed("hers", "her@example.com", "b@g.us", True)
    await db.delete_account("hers")
    assert await db.get_account("hers") is None
    assert await db.list_grants("hers") == []
    cursor = await db.connection.execute("SELECT COUNT(*) FROM chat_access WHERE account_id='hers'")
    row = await cursor.fetchone()
    assert row is not None and row[0] == 0
    assert await db.get_account("default") is not None  # other accounts untouched
    assert await db.get_user("her@example.com") is not None
