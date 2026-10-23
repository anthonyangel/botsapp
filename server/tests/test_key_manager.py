"""KeyManager: provision / rotate / verify / revoke with a stand-in WAHA key API
and a real SQLite database. The ordering and rollback rules are the point."""

import logging
from collections.abc import AsyncIterator

import pytest
from cryptography.fernet import Fernet
from fake_waha import FakeWaha

from botsapp.db import DatabaseManager
from botsapp.key_manager import (
    MCP_KEY_ACTIONS,
    KeyManager,
    KeyManagerError,
    drift_from_policy,
)
from botsapp.secretbox import SecretBox
from botsapp.waha_keys import DEFAULT_ACTIONS, KeyApiError, KeyInfo


@pytest.fixture
async def db(tmp_path) -> AsyncIterator[DatabaseManager]:
    manager = DatabaseManager(str(tmp_path / "keys.db"))
    await manager.connect()
    await manager.ensure_schema()
    await manager.bootstrap("me@example.com", "default")
    await manager.add_user("sam@example.com")
    await manager.add_account("sam", "sam@example.com", "acct-sam")
    yield manager
    await manager.close()


@pytest.fixture
def box() -> SecretBox:
    return SecretBox([Fernet.generate_key().decode()])


@pytest.fixture
def waha() -> FakeWaha:
    return FakeWaha()


@pytest.fixture
def manager(db, waha, box) -> KeyManager:
    return KeyManager(db, waha, box)


async def _stored_secret(db, box, account="sam") -> str:
    row = await db.get_account_key(account)
    assert row is not None
    return box.decrypt(row["ciphertext"])


# ── provision ────────────────────────────────────────────────────────────────


async def test_provision_creates_a_read_only_key_and_stores_it_encrypted(manager, db, waha, box):
    info = await manager.provision("sam")
    assert waha.keys[info.id].actions == {**DEFAULT_ACTIONS, **MCP_KEY_ACTIONS}
    assert waha.keys[info.id].actions["send"] is False and waha.keys[info.id].actions["read"]
    row = await db.get_account_key("sam")
    assert row is not None and row["key_id"] == info.id
    assert info.secret is not None and info.secret not in row["ciphertext"]
    assert await _stored_secret(db, box) == info.secret
    assert row["actions"]["send"] is False  # what WAHA reported, not just what we asked


async def test_the_secret_never_reaches_the_general_account_reads(manager, db):
    info = await manager.provision("sam")
    account = await db.get_account("sam")
    assert account is not None and account["waha_key_id"] == info.id
    assert info.secret not in repr(account) and "ciphertext" not in repr(account)
    for listed in await db.list_accounts():
        assert info.secret not in repr(listed) and "ciphertext" not in repr(listed)


async def test_provision_twice_is_refused(manager):
    await manager.provision("sam")
    with pytest.raises(KeyManagerError, match="rotate"):
        await manager.provision("sam")


async def test_provision_needs_a_master_key(db, waha):
    with pytest.raises(KeyManagerError, match="BOTSAPP_SECRET_KEY"):
        await KeyManager(db, waha, None).provision("sam")
    assert not waha.keys


async def test_provision_unknown_account(manager):
    with pytest.raises(KeyManagerError, match="Unknown account"):
        await manager.provision("ghost")


@pytest.mark.parametrize(
    ("flag", "value", "message"),
    [("make_admin", True, "wrong scope"), ("wrong_session", True, "wrong scope")],
)
async def test_a_key_with_the_wrong_scope_is_deleted_not_kept(
    manager, db, waha, flag, value, message
):
    setattr(waha, flag, value)
    with pytest.raises(KeyManagerError, match=message):
        await manager.provision("sam")
    assert not waha.keys and await db.get_account_key("sam") is None


async def test_wahas_readback_is_what_is_checked_not_our_request(manager, db, waha):
    waha.apply_actions = {"send": True}  # WAHA ignores our scope and allows sending
    with pytest.raises(KeyManagerError, match="did not apply"):
        await manager.provision("sam")
    assert not waha.keys and await db.get_account_key("sam") is None


async def test_a_key_without_a_returned_value_is_removed(manager, db, waha):
    waha.no_secret = True
    with pytest.raises(KeyManagerError, match="did not return"):
        await manager.provision("sam")
    assert not waha.keys and await db.get_account_key("sam") is None


async def test_waha_failures_leave_nothing_behind(manager, db, waha):
    waha.fail.add("create")
    with pytest.raises(KeyManagerError):
        await manager.provision("sam")
    assert await db.get_account_key("sam") is None


async def test_a_refused_smoke_test_removes_the_key(db, waha, box):
    async def refuse(session: str, secret: str) -> None:
        raise KeyManagerError("WAHA refused the new key: 401")

    manager = KeyManager(db, waha, box, probe=refuse)
    with pytest.raises(KeyManagerError, match="refused"):
        await manager.provision("sam")
    assert not waha.keys and await db.get_account_key("sam") is None


async def test_the_smoke_test_is_given_the_session_and_the_new_secret(db, waha, box):
    seen = []

    async def probe(session: str, secret: str) -> None:
        seen.append((session, secret))

    info = await KeyManager(db, waha, box, probe=probe).provision("sam")
    assert seen == [("acct-sam", info.secret)]


async def test_failing_to_store_deletes_the_new_key_in_waha(manager, db, waha, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "set_account_key", boom)
    with pytest.raises(KeyManagerError, match="removed from WAHA"):
        await manager.provision("sam")
    assert not waha.keys


# ── rotate ───────────────────────────────────────────────────────────────────


async def test_rotate_swaps_the_key_and_deletes_the_old_one_last(manager, db, waha, box):
    first = await manager.provision("sam")
    second, old_deleted = await manager.rotate("sam")
    assert old_deleted and second.id != first.id
    assert first.id in waha.deleted and set(waha.keys) == {second.id}
    assert await _stored_secret(db, box) == second.secret != first.secret


async def test_rotate_failure_keeps_the_old_key_working(manager, db, waha, box):
    first = await manager.provision("sam")
    waha.fail.add("create")
    with pytest.raises(KeyManagerError):
        await manager.rotate("sam")
    assert await _stored_secret(db, box) == first.secret and first.id in waha.keys


async def test_rotate_store_failure_deletes_the_new_key_and_keeps_the_old(
    manager, db, waha, box, monkeypatch
):
    first = await manager.provision("sam")

    async def boom(*_a, **_k):
        raise RuntimeError("locked")

    monkeypatch.setattr(db, "set_account_key", boom)
    with pytest.raises(KeyManagerError):
        await manager.rotate("sam")
    assert set(waha.keys) == {first.id}
    assert await _stored_secret(db, box) == first.secret


async def test_a_failed_old_key_delete_is_reported_not_fatal(manager, waha, caplog):
    first = await manager.provision("sam")
    original = waha.delete_key

    async def flaky(key_id: str) -> None:
        if key_id == first.id:
            raise KeyApiError("boom")
        await original(key_id)

    waha.delete_key = flaky  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        _new, old_deleted = await manager.rotate("sam")
    assert old_deleted is False and first.id in waha.keys
    assert "wk_secret" not in caplog.text


async def test_rotate_without_a_key(manager):
    with pytest.raises(KeyManagerError, match="no key to rotate"):
        await manager.rotate("sam")


# ── verify ───────────────────────────────────────────────────────────────────


async def test_verify_an_intact_key(manager, db):
    info = await manager.provision("sam")
    result = await manager.verify("sam")
    assert result.key_id == info.id and result.drift == {}


async def test_verify_notices_an_edit_made_in_wahas_dashboard_and_stores_it(manager, db, waha):
    info = await manager.provision("sam")
    waha.keys[info.id] = KeyInfo(info.id, "acct-sam", False, True, {**info.actions, "send": True})
    result = await manager.verify("sam")
    assert result.drift == {"send": (False, True)}
    stored = await db.get_account_key("sam")
    assert stored is not None and stored["actions"]["send"] is True  # what WAHA reports


async def test_verify_reports_a_key_that_vanished_or_was_deactivated(manager, waha):
    info = await manager.provision("sam")
    waha.keys[info.id] = KeyInfo(info.id, "acct-sam", False, False, info.actions)
    with pytest.raises(KeyManagerError, match="deactivated"):
        await manager.verify("sam")
    del waha.keys[info.id]
    with pytest.raises(KeyManagerError, match="no longer exists"):
        await manager.verify("sam")


async def test_verify_unknown_actions_count_as_drift():
    assert drift_from_policy({**MCP_KEY_ACTIONS, "telepathy": True}) == {"telepathy": (False, True)}


# ── revoke ───────────────────────────────────────────────────────────────────


async def test_revoke_deletes_in_waha_then_forgets(manager, db, waha):
    info = await manager.provision("sam")
    await manager.revoke("sam")
    assert info.id in waha.deleted and await db.get_account_key("sam") is None


async def test_failed_revoke_changes_nothing(manager, db, waha):
    info = await manager.provision("sam")
    waha.fail.add("delete")
    with pytest.raises(KeyManagerError, match="nothing changed"):
        await manager.revoke("sam")
    stored = await db.get_account_key("sam")
    assert stored is not None and stored["key_id"] == info.id


async def test_revoke_without_a_key_is_a_noop(manager, waha):
    await manager.revoke("sam")
    assert not waha.deleted
