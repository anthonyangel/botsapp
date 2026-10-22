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
    mine = await registry.for_account("default")
    hers = await registry.for_account("her")
    assert mine is not hers
    assert mine._session_name == "default"
    assert hers._session_name == "acct-her"
    await registry.close()


async def test_clients_are_cached(db: DatabaseManager):
    registry = BridgeRegistry(db, "http://waha:3000", "key")
    assert await registry.for_account("her") is await registry.for_account("her")
    await registry.close()


async def test_unknown_account_raises(db: DatabaseManager):
    registry = BridgeRegistry(db, "http://waha:3000", "key")
    with pytest.raises(UnknownAccountError):
        await registry.for_account("ghost")
    await registry.close()


def test_session_name_for_is_url_safe():
    assert session_name_for("Her Phone!") == "acct-her-phone"
    with pytest.raises(ValueError):
        session_name_for("!!!")


async def test_delete_session_removes_it_in_waha_and_forgets_the_client(db: DatabaseManager):
    registry = BridgeRegistry(db, "http://waha:3000", "key")
    client = await registry.for_account("her")
    client.delete_session = AsyncMock()
    await registry.delete_session("her")
    client.delete_session.assert_awaited_once()
    assert await registry.for_account("her") is not client  # rebuilt, not cached
    await registry.close()


async def test_create_session_delegates_to_the_accounts_client(db: DatabaseManager):
    registry = BridgeRegistry(db, "http://waha:3000", "key")
    client = await registry.for_account("her")
    client.create_session = AsyncMock()
    await registry.create_session("her")
    client.create_session.assert_awaited_once()
    await registry.close()
