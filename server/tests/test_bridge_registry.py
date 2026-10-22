"""BridgeRegistry: account -> its own WAHA session client (0025 phase 2)."""

from collections.abc import AsyncIterator

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
