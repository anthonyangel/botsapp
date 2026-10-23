"""The MCP server's startup rules around scoped WAHA keys (0027)."""

import pytest
from cryptography.fernet import Fernet

import botsapp.state as state
from botsapp.app import lifespan, mcp


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", str(tmp_path / "life.db"))
    monkeypatch.setenv("WAHA_URL", "http://waha.invalid")
    monkeypatch.delenv("BOTSAPP_ADMIN_EMAIL", raising=False)
    for name in ("WAHA_API_KEY", "WAHA_REQUIRE_SCOPED_KEYS", "BOTSAPP_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    # the autouse fixture installs mocks; the lifespan sets its own
    state.db = state.bridge = state.registry = None


async def test_strict_mode_without_a_master_key_refuses_to_start(env, monkeypatch):
    monkeypatch.setenv("WAHA_REQUIRE_SCOPED_KEYS", "true")
    with pytest.raises(RuntimeError, match="BOTSAPP_SECRET_KEY"):
        async with lifespan(mcp):
            pass


async def test_a_malformed_master_key_refuses_to_start(env, monkeypatch):
    monkeypatch.setenv("BOTSAPP_SECRET_KEY", "not-a-key")
    from botsapp.secretbox import SecretBoxError

    with pytest.raises(SecretBoxError):
        async with lifespan(mcp):
            pass


async def test_strict_mode_starts_without_the_admin_key_and_has_no_default_bridge(env, monkeypatch):
    monkeypatch.setenv("WAHA_REQUIRE_SCOPED_KEYS", "true")
    monkeypatch.setenv("BOTSAPP_SECRET_KEY", Fernet.generate_key().decode())
    async with lifespan(mcp):
        assert state.bridge is None  # the blocked outbound tools have nothing to use
        assert state.registry is not None
    assert state.registry is None


async def test_with_the_admin_key_the_default_bridge_still_exists(env, monkeypatch):
    monkeypatch.setenv("WAHA_API_KEY", "ADMIN")
    async with lifespan(mcp):
        assert state.bridge is not None and state.registry is not None


async def test_accounts_without_keys_are_logged_at_startup(env, monkeypatch, caplog):
    monkeypatch.setenv("WAHA_API_KEY", "ADMIN")
    monkeypatch.setenv("BOTSAPP_ADMIN_EMAIL", "me@example.com")
    with caplog.at_level("WARNING"):
        async with lifespan(mcp):
            pass
    assert "accounts without a scoped WAHA key: default" in caplog.text


async def test_an_unconfigured_owner_is_called_out_loudly_at_startup(env, monkeypatch, caplog):
    monkeypatch.setenv("WAHA_API_KEY", "ADMIN")
    with caplog.at_level("ERROR"):
        async with lifespan(mcp):
            pass
    assert "No WhatsApp accounts exist" in caplog.text and "BOTSAPP_ADMIN_EMAIL" in caplog.text


async def test_no_startup_error_once_an_account_exists(env, monkeypatch, caplog):
    monkeypatch.setenv("WAHA_API_KEY", "ADMIN")
    monkeypatch.setenv("BOTSAPP_ADMIN_EMAIL", "me@example.com")
    with caplog.at_level("ERROR"):
        async with lifespan(mcp):
            pass
    assert "No WhatsApp accounts exist" not in caplog.text
