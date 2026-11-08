"""
Unit tests for bridge_factory — WAHA is the only bridge provider; construction
goes through the per-account registry (0025).
"""

from unittest.mock import AsyncMock

from botsapp.bridge_factory import bridge_provider_name, build_registry


async def test_builds_a_registry_whose_admin_client_is_a_waha_client(monkeypatch):
    monkeypatch.setenv("WAHA_URL", "http://fake")
    monkeypatch.setenv("WAHA_API_KEY", "fake-key")
    db = AsyncMock()
    db.get_account.return_value = {"id": "default", "waha_session": "family"}
    registry = build_registry(db)
    client = await registry.for_account("default", purpose="admin")
    assert client._session_name == "family"
    assert client._api_key == "fake-key"
    assert bridge_provider_name() == "waha"
    await registry.close()


async def test_the_admin_key_is_optional(monkeypatch):
    monkeypatch.setenv("WAHA_URL", "http://fake")
    monkeypatch.delenv("WAHA_API_KEY", raising=False)
    registry = build_registry(AsyncMock())
    assert registry is not None
    await registry.close()
