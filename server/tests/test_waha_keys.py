"""WAHA key parsing and the KeyApi wrapper over WAHAClient (0027). No network."""

import logging
from unittest.mock import AsyncMock

import pytest

from botsapp.waha_keys import (
    DEFAULT_ACTIONS,
    WAHA_ACTIONS,
    KeyApiError,
    WahaKeyApi,
    effective_actions,
    parse_key,
)


def test_null_actions_mean_wahas_defaults():
    assert effective_actions(None) == DEFAULT_ACTIONS
    assert DEFAULT_ACTIONS["delete"] is False and DEFAULT_ACTIONS["send"] is True


def test_a_partial_map_denies_what_it_omits():
    got = effective_actions({"send": True})
    assert got["send"] is True
    assert not any(got[a] for a in WAHA_ACTIONS if a != "send")


def test_unknown_actions_are_kept_but_logged(caplog):
    with caplog.at_level(logging.WARNING):
        got = effective_actions({"telepathy": True})
    assert got["telepathy"] is True
    assert "telepathy" in caplog.text


def test_parse_key_and_the_secret_never_appears_in_repr():
    info = parse_key(
        {
            "id": "k1",
            "key": "wk_supersecret",
            "isAdmin": False,
            "session": "acct-sam",
            "isActive": True,
            "actions": {"send": False},
        }
    )
    assert info.id == "k1" and info.session == "acct-sam" and info.secret == "wk_supersecret"
    assert "wk_supersecret" not in repr(info) and "wk_supersecret" not in str(info)


def test_parse_key_rejects_unusable_payloads():
    with pytest.raises(KeyApiError):
        parse_key({"key": "x"})  # no id


@pytest.fixture
def client():
    return AsyncMock()


async def test_create_sends_a_non_admin_session_scoped_body(client):
    client.create_key.return_value = {"id": "k1", "key": "wk_s", "session": "acct-sam"}
    info = await WahaKeyApi(client).create_key(session="acct-sam", actions={"send": False})
    client.create_key.assert_awaited_once_with(
        {"isAdmin": False, "session": "acct-sam", "actions": {"send": False}}
    )
    assert info.secret == "wk_s"


async def test_create_without_a_returned_key_value_is_an_error(client):
    client.create_key.return_value = {"id": "k1", "session": "acct-sam"}
    with pytest.raises(KeyApiError):
        await WahaKeyApi(client).create_key(session="acct-sam", actions={})


async def test_get_key_finds_by_id_in_the_listing(client):
    client.list_keys.return_value = [{"id": "a"}, {"id": "b", "session": "s"}]
    api = WahaKeyApi(client)
    assert (await api.get_key("b")).session == "s"
    assert await api.get_key("zzz") is None


async def test_update_preserves_the_other_fields(client):
    client.list_keys.return_value = [
        {"id": "k1", "isAdmin": False, "session": "acct-sam", "isActive": True}
    ]
    client.update_key.return_value = {"id": "k1", "session": "acct-sam", "actions": {"send": False}}
    await WahaKeyApi(client).update_key_actions("k1", {"send": False})
    client.update_key.assert_awaited_once_with(
        "k1",
        {"isAdmin": False, "session": "acct-sam", "isActive": True, "actions": {"send": False}},
    )


async def test_update_of_a_missing_key_is_an_error(client):
    client.list_keys.return_value = []
    with pytest.raises(KeyApiError):
        await WahaKeyApi(client).update_key_actions("k1", {})
