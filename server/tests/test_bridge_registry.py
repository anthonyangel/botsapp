"""BridgeRegistry: account -> its own WAHA session client (0025 phase 2)."""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from botsapp.bridge_registry import BridgeRegistry, UnknownAccountError, session_name_for
from botsapp.db import DatabaseManager


@pytest.fixture
async def db(tmp_path) -> AsyncIterator[DatabaseManager]:
    manager = DatabaseManager(str(tmp_path / "test.db"))
    await manager.connect()
    await manager.ensure_schema()
    await manager.bootstrap("me@example.com", "default")
    await manager.add_user("her@example.com")
    await manager.add_account("her", "her@example.com", session_name_for("her"))
    yield manager
    await manager.close()


async def test_each_account_gets_its_own_session_client(db: DatabaseManager):
    registry = BridgeRegistry(db, "http://waha:3000", "key")
    mine = await registry.for_account("default", purpose="admin")
    hers = await registry.for_account("her", purpose="admin")
    assert mine is not hers
    assert mine._session_name == "default"
    assert hers._session_name == "acct-her"
    await registry.close()


async def test_clients_are_cached(db: DatabaseManager):
    registry = BridgeRegistry(db, "http://waha:3000", "key")
    assert await registry.for_account("her", purpose="admin") is await registry.for_account(
        "her", purpose="admin"
    )
    await registry.close()


async def test_unknown_account_raises(db: DatabaseManager):
    registry = BridgeRegistry(db, "http://waha:3000", "key")
    with pytest.raises(UnknownAccountError):
        await registry.for_account("ghost", purpose="admin")
    await registry.close()


def test_session_name_for_is_url_safe():
    assert session_name_for("Her Phone!") == "acct-her-phone"
    with pytest.raises(ValueError):
        session_name_for("!!!")


async def test_delete_session_removes_it_in_waha_and_forgets_the_client(db: DatabaseManager):
    registry = BridgeRegistry(db, "http://waha:3000", "key")
    client = await registry.for_account("her", purpose="admin")
    client.delete_session = AsyncMock()
    await registry.delete_session("her")
    client.delete_session.assert_awaited_once()
    assert await registry.for_account("her", purpose="admin") is not client  # rebuilt, not cached
    await registry.close()


async def test_create_session_delegates_to_the_accounts_client(db: DatabaseManager):
    registry = BridgeRegistry(db, "http://waha:3000", "key")
    client = await registry.for_account("her", purpose="admin")
    client.create_session = AsyncMock()
    await registry.create_session("her")
    client.create_session.assert_awaited_once()
    await registry.close()


# ── which key a process gets (0027) ──────────────────────────────────────────

import logging  # noqa: E402

from cryptography.fernet import Fernet  # noqa: E402

from botsapp.bridge_registry import (  # noqa: E402
    NoScopedKeyError,
    ScopedKeyError,
    ScopedKeyUnreadableError,
)
from botsapp.secretbox import SecretBox  # noqa: E402


@pytest.fixture
def box() -> SecretBox:
    return SecretBox([Fernet.generate_key().decode()])


async def _give_key(db: DatabaseManager, box: SecretBox, secret: str = "wk_acct_secret") -> str:
    token = box.encrypt(secret)
    await db.set_account_key("her", "key_1", token, {"read": True, "send": False})
    return token


async def test_mcp_uses_the_accounts_own_key_never_the_admin_key(db, box):
    await _give_key(db, box)
    registry = BridgeRegistry(db, "http://waha", "ADMIN_KEY", box=box)
    mcp = await registry.for_account("her", purpose="mcp")
    admin = await registry.for_account("her", purpose="admin")
    assert mcp._api_key == "wk_acct_secret" and admin._api_key == "ADMIN_KEY"
    assert mcp is not admin
    await registry.close()


async def test_strict_mode_fails_closed_for_an_account_without_a_key(db, box):
    registry = BridgeRegistry(db, "http://waha", "ADMIN_KEY", box=box, require_scoped_keys=True)
    with pytest.raises(NoScopedKeyError):
        await registry.for_account("her", purpose="mcp")
    # ...but the admin side is unaffected, so the key can still be created.
    assert (await registry.for_account("her", purpose="admin"))._api_key == "ADMIN_KEY"
    await registry.close()


async def test_transition_mode_falls_back_to_the_admin_key_and_warns_once(db, box, caplog):
    registry = BridgeRegistry(db, "http://waha", "ADMIN_KEY", box=box)
    with caplog.at_level(logging.WARNING):
        first = await registry.for_account("her", purpose="mcp")
        await registry.for_account("her", purpose="mcp")
    assert first._api_key == "ADMIN_KEY"
    assert caplog.text.count("no scoped WAHA key") == 1
    await registry.close()


async def test_strict_mode_works_with_no_admin_key_in_the_process(db, box):
    await _give_key(db, box)
    registry = BridgeRegistry(db, "http://waha", None, box=box, require_scoped_keys=True)
    assert (await registry.for_account("her", purpose="mcp"))._api_key == "wk_acct_secret"
    with pytest.raises(ScopedKeyError):
        await registry.for_account("her", purpose="admin")
    await registry.close()


async def test_a_stored_key_is_never_replaced_by_the_admin_key_when_it_cant_be_opened(db, box):
    await _give_key(db, box)
    no_box = BridgeRegistry(db, "http://waha", "ADMIN_KEY")
    with pytest.raises(ScopedKeyUnreadableError, match="BOTSAPP_SECRET_KEY"):
        await no_box.for_account("her", purpose="mcp")
    other_box = SecretBox([Fernet.generate_key().decode()])
    wrong = BridgeRegistry(db, "http://waha", "ADMIN_KEY", box=other_box)
    with pytest.raises(ScopedKeyUnreadableError) as excinfo:
        await wrong.for_account("her", purpose="mcp")
    assert "wk_acct_secret" not in str(excinfo.value)


async def test_clients_are_cached_until_the_key_changes(db, box):
    await _give_key(db, box)
    registry = BridgeRegistry(db, "http://waha", "ADMIN_KEY", box=box)
    first = await registry.for_account("her", purpose="mcp")
    assert await registry.for_account("her", purpose="mcp") is first
    first.close = AsyncMock()  # the stale client is closed when the key is rotated
    await _give_key(db, box, secret="wk_rotated")  # as the admin process would
    second = await registry.for_account("her", purpose="mcp")
    assert second is not first and second._api_key == "wk_rotated"
    first.close.assert_awaited_once()
    await registry.close()


async def test_unknown_account_for_either_purpose(db, box):
    registry = BridgeRegistry(db, "http://waha", "ADMIN_KEY", box=box)
    for purpose in ("mcp", "admin"):
        with pytest.raises(UnknownAccountError):
            await registry.for_account("ghost", purpose=purpose)  # type: ignore[arg-type]
    await registry.close()


async def test_deleting_a_session_drops_both_purposes_clients(db, box):
    await _give_key(db, box)
    registry = BridgeRegistry(db, "http://waha", "ADMIN_KEY", box=box)
    admin = await registry.for_account("her", purpose="admin")
    mcp = await registry.for_account("her", purpose="mcp")
    admin.delete_session = AsyncMock()
    admin.close, mcp.close = AsyncMock(), AsyncMock()
    await registry.delete_session("her")
    assert not registry._clients
    admin.close.assert_awaited_once()
    mcp.close.assert_awaited_once()
