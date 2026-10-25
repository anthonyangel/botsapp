"""
Unit tests for DatabaseManager (users, accounts, grants and per-viewer chat access —
chat/message history lives in message_store.py, tested in test_message_store.py).

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


# ── email allowlist ──────────────────────────────────────────────────────
# Who may authenticate to the MCP server at all (app.py's
# allowed_family_email) — see docs/decisions/0015-email-allowlist-in-db.md.
# It is the users table (0025); distinct from the per-viewer chat allowlist.


async def test_is_email_allowed_false_when_empty(db: DatabaseManager):
    assert await db.is_email_allowed("anthony@angelfamily.net") is False


async def test_added_user_is_allowed(db: DatabaseManager):
    await db.add_user("anthony@angelfamily.net")
    assert await db.is_email_allowed("anthony@angelfamily.net") is True


async def test_is_email_allowed_is_case_and_whitespace_insensitive(db: DatabaseManager):
    await db.add_user("  Anthony@AngelFamily.NET  ")
    assert await db.is_email_allowed("anthony@angelfamily.net") is True
    assert await db.is_email_allowed("ANTHONY@ANGELFAMILY.NET") is True


async def test_add_user_is_idempotent(db: DatabaseManager):
    await db.add_user("anthony@angelfamily.net")
    await db.add_user("anthony@angelfamily.net")
    assert [u["email"] for u in await db.list_users()] == ["anthony@angelfamily.net"]


async def test_remove_user(db: DatabaseManager):
    await db.add_user("anthony@angelfamily.net")
    await db.remove_user("anthony@angelfamily.net")
    assert await db.is_email_allowed("anthony@angelfamily.net") is False


async def test_remove_user_missing_is_a_noop(db: DatabaseManager):
    await db.remove_user("nobody@example.com")  # must not raise
    assert await db.list_users() == []


# ── multi-tenancy: users, accounts, grants, per-viewer chat access (0025) ────


async def _two_users(db: DatabaseManager) -> None:
    await db.bootstrap("me@example.com", "default")
    await db.add_user("her@example.com")


async def test_added_user_is_not_an_admin(db: DatabaseManager):
    await db.add_user("Her@Example.com")
    user = await db.get_user("her@example.com")
    assert user is not None and user["is_admin"] is False


async def test_bootstrap_is_idempotent(db: DatabaseManager):
    await db.bootstrap("me@example.com", "default")
    await db.bootstrap("me@example.com", "default")
    user = await db.get_user("me@example.com")
    assert user is not None and user["is_admin"] is True
    assert [a["id"] for a in await db.list_accounts()] == ["default"]


async def _legacy_db(tmp_path) -> DatabaseManager:
    """A database as the single-tenant releases left it: both legacy tables, filled."""
    manager = DatabaseManager(str(tmp_path / "legacy.db"))
    await manager.connect()
    await manager.connection.execute(
        "CREATE TABLE chat_metadata (jid TEXT PRIMARY KEY, tags TEXT, is_allowed INTEGER)"
    )
    await manager.connection.execute("CREATE TABLE allowed_emails (email TEXT PRIMARY KEY)")
    await manager.connection.commit()
    return manager


async def _tables(manager: DatabaseManager) -> set[str]:
    cursor = await manager.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row[0] async for row in cursor}


async def test_legacy_tables_are_dropped_once_an_account_exists(tmp_path):
    manager = await _legacy_db(tmp_path)
    await manager.ensure_schema()
    await manager.bootstrap("me@example.com", "default")  # what adopted the data (0025)
    await manager.ensure_schema()  # next start
    assert not {"chat_metadata", "allowed_emails"} & await _tables(manager)
    await manager.close()


async def test_legacy_tables_are_kept_until_the_database_was_bootstrapped(tmp_path):
    manager = await _legacy_db(tmp_path)
    await manager.ensure_schema()
    assert {"chat_metadata", "allowed_emails"} <= await _tables(manager)
    await manager.close()


async def test_visibility_is_per_viewer(db: DatabaseManager):
    await _two_users(db)
    await db.set_chat_access_allowed(
        "default", "me@example.com", "a@g.us", True, acting_email="me@example.com"
    )
    await db.grant_account("default", "her@example.com")
    assert await db.list_allowed_jids_for("default", "her@example.com") == set()
    await db.set_chat_access_allowed(
        "default", "her@example.com", "b@g.us", True, acting_email="me@example.com"
    )
    assert await db.list_allowed_jids_for("default", "her@example.com") == {"b@g.us"}
    assert await db.list_allowed_jids_for("default", "me@example.com") == {"a@g.us"}


async def test_tags_are_per_viewer(db: DatabaseManager):
    await _two_users(db)
    await db.grant_account("default", "her@example.com")
    for who, tag in (("me@example.com", "mine"), ("her@example.com", "hers")):
        await db.set_chat_access_allowed(
            "default", who, "a@g.us", True, acting_email="me@example.com"
        )
        await db.upsert_chat_access_tags("default", who, "a@g.us", [tag])
    assert await db.search_by_tags_for("default", "her@example.com", ["mine"]) == []
    assert len(await db.search_by_tags_for("default", "her@example.com", ["hers"])) == 1


async def test_no_grant_means_no_access_and_no_writes(db: DatabaseManager):
    await _two_users(db)
    assert await db.list_allowed_jids_for("default", "her@example.com") == set()
    with pytest.raises(PermissionError):
        await db.set_chat_access_allowed(
            "default", "her@example.com", "a@g.us", True, acting_email="me@example.com"
        )
    with pytest.raises(PermissionError):
        await db.upsert_chat_access_tags("default", "her@example.com", "a@g.us", ["x"])


async def test_revoke_hides_and_drops_viewer_rows(db: DatabaseManager):
    await _two_users(db)
    await db.grant_account("default", "her@example.com")
    await db.set_chat_access_allowed(
        "default", "her@example.com", "b@g.us", True, acting_email="me@example.com"
    )
    await db.revoke_account("default", "her@example.com")
    assert await db.list_allowed_jids_for("default", "her@example.com") == set()
    await db.grant_account("default", "her@example.com")  # re-grant starts empty
    assert await db.list_allowed_jids_for("default", "her@example.com") == set()


async def test_revoking_the_owners_own_email_is_a_no_op(db: DatabaseManager):
    """The owner has no grant row to revoke, and revoking by email must not
    wipe their own chat_access rows (they're keyed by viewer_email too)."""
    await _two_users(db)
    await db.set_chat_access_allowed(
        "default", "me@example.com", "a@g.us", True, acting_email="me@example.com"
    )
    await db.revoke_account("default", "me@example.com")
    assert await db.list_allowed_jids_for("default", "me@example.com") == {"a@g.us"}
    assert await db.has_account_access("default", "me@example.com")


async def test_only_the_owner_may_set_chat_access_allowed(db: DatabaseManager):
    await _two_users(db)
    await db.grant_account("default", "her@example.com")
    with pytest.raises(PermissionError):
        await db.set_chat_access_allowed(
            "default", "her@example.com", "a@g.us", True, acting_email="her@example.com"
        )
    # the account's viewer rows are untouched by the refused call
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
    await db.set_chat_access_allowed(
        "hers", "me@example.com", "a@g.us", True, acting_email="her@example.com"
    )
    await db.set_chat_access_allowed(
        "hers", "her@example.com", "b@g.us", True, acting_email="her@example.com"
    )
    await db.delete_account("hers")
    assert await db.get_account("hers") is None
    assert await db.list_grants("hers") == []
    cursor = await db.connection.execute("SELECT COUNT(*) FROM chat_access WHERE account_id='hers'")
    row = await cursor.fetchone()
    assert row is not None and row[0] == 0
    assert await db.get_account("default") is not None  # other accounts untouched
    assert await db.get_user("her@example.com") is not None


# ── scoped WAHA key columns (0027) ───────────────────────────────────────────


async def test_account_key_round_trip_and_clear(db: DatabaseManager):
    await _two_users(db)
    await db.add_account("hers", "her@example.com", "acct-hers")
    assert await db.get_account_key("hers") is None
    await db.set_account_key("hers", "key_1", "CIPHERTEXT", {"read": True, "send": False})
    assert await db.get_account_key("hers") == {
        "key_id": "key_1",
        "ciphertext": "CIPHERTEXT",
        "actions": {"read": True, "send": False},
    }
    await db.set_account_key_actions("hers", {"read": True, "send": True})
    stored = await db.get_account_key("hers")
    assert (
        stored is not None
        and stored["actions"]["send"] is True
        and stored["ciphertext"] == "CIPHERTEXT"
    )
    await db.clear_account_key("hers")
    assert await db.get_account_key("hers") is None


async def test_general_account_reads_expose_key_id_and_actions_but_never_the_ciphertext(
    db: DatabaseManager,
):
    await _two_users(db)
    await db.add_account("hers", "her@example.com", "acct-hers")
    await db.set_account_key("hers", "key_1", "SECRET_CIPHERTEXT", {"read": True})
    reads = [
        await db.get_account("hers"),
        *await db.list_accounts(),
        *await db.list_accounts_for_user("her@example.com"),
    ]
    for account in reads:
        assert account is not None and "SECRET_CIPHERTEXT" not in repr(account)
        assert "waha_key_ciphertext" not in account
    hers = await db.get_account("hers")
    assert (
        hers is not None
        and hers["waha_key_id"] == "key_1"
        and hers["waha_key_actions"] == {"read": True}
    )
    default = await db.get_account("default")
    assert (
        default is not None
        and default["waha_key_id"] is None
        and default["waha_key_actions"] is None
    )


async def test_an_accounts_table_from_before_the_key_columns_is_migrated(tmp_path):
    path = str(tmp_path / "old.db")
    old = DatabaseManager(path)
    await old.connect()
    await old.connection.execute(
        "CREATE TABLE users (email TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '', "
        "is_admin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT '')"
    )
    await old.connection.execute(
        "CREATE TABLE accounts (id TEXT PRIMARY KEY, owner_email TEXT NOT NULL, "
        "label TEXT NOT NULL DEFAULT '', waha_session TEXT NOT NULL UNIQUE, "
        "created_at TEXT NOT NULL DEFAULT '')"
    )
    await old.connection.execute("INSERT INTO users (email) VALUES ('me@example.com')")
    await old.connection.execute(
        "INSERT INTO accounts (id, owner_email, waha_session) "
        "VALUES ('default','me@example.com','default')"
    )
    await old.connection.commit()
    await old.ensure_schema()
    await old.ensure_schema()  # idempotent
    account = await old.get_account("default")
    assert account is not None and account["waha_key_id"] is None  # existing row survives
    await old.set_account_key("default", "k", "C", {"read": True})
    assert await old.get_account_key("default") is not None
    await old.close()


async def test_adding_a_column_another_process_just_added_is_not_an_error(tmp_path):
    """The MCP server and the admin UI run the migration at the same time on the
    first start after an upgrade; the loser of the check-then-ALTER race must not
    crash (a crashed admin UI stays down on Fly)."""
    manager = DatabaseManager(str(tmp_path / "race.db"))
    await manager.connect()
    await manager.ensure_schema()
    await manager._add_text_column("accounts", "waha_key_id")  # already there: fine
    with pytest.raises(Exception, match="no such table"):
        await manager._add_text_column("nope", "x")  # other errors still surface
    await manager.close()


async def test_two_processes_migrating_an_old_database_at_once_both_succeed(tmp_path):
    import asyncio

    for trial in range(15):  # the race is timing-dependent, so give it many chances
        path = str(tmp_path / f"old{trial}.db")
        old = DatabaseManager(path)
        await old.connect()
        await old.connection.execute(
            "CREATE TABLE users (email TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '', "
            "is_admin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT '')"
        )
        await old.connection.execute(
            "CREATE TABLE accounts (id TEXT PRIMARY KEY, owner_email TEXT NOT NULL, "
            "label TEXT NOT NULL DEFAULT '', waha_session TEXT NOT NULL UNIQUE, "
            "created_at TEXT NOT NULL DEFAULT '')"
        )
        await old.connection.commit()
        await old.close()
        a, b = DatabaseManager(path), DatabaseManager(path)
        await asyncio.gather(a.connect(), b.connect())
        results = await asyncio.gather(a.ensure_schema(), b.ensure_schema(), return_exceptions=True)
        await asyncio.gather(a.close(), b.close())
        assert [r for r in results if isinstance(r, Exception)] == []
