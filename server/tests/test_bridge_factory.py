"""
Unit tests for bridge_factory.build_bridge_and_store — WAHA is the only
bridge provider, so this just confirms construction reads the right env
vars and one client satisfies both the Bridge and MessageStore protocols.
"""

from botsapp.bridge_factory import bridge_provider_name, build_bridge_and_store
from botsapp.waha_client import WAHAClient


def test_builds_waha_client(monkeypatch):
    monkeypatch.setenv("WAHA_URL", "http://fake")
    monkeypatch.setenv("WAHA_API_KEY", "fake-key")
    bridge, store = build_bridge_and_store()
    assert isinstance(bridge, WAHAClient)
    # One client satisfies both protocols (see waha_client.py's module docstring).
    assert bridge is store
    assert bridge_provider_name() == "waha"


def test_uses_default_session_name_when_unset(monkeypatch):
    monkeypatch.setenv("WAHA_URL", "http://fake")
    monkeypatch.setenv("WAHA_API_KEY", "fake-key")
    monkeypatch.delenv("WAHA_SESSION", raising=False)
    bridge, _ = build_bridge_and_store()
    assert isinstance(bridge, WAHAClient)
    assert bridge._session_name == "default"


def test_honors_custom_session_name(monkeypatch):
    monkeypatch.setenv("WAHA_URL", "http://fake")
    monkeypatch.setenv("WAHA_API_KEY", "fake-key")
    monkeypatch.setenv("WAHA_SESSION", "family")
    bridge, _ = build_bridge_and_store()
    assert isinstance(bridge, WAHAClient)
    assert bridge._session_name == "family"
