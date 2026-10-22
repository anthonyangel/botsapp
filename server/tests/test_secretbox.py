"""SecretBox: authenticated encryption for the stored WAHA keys (0027)."""

import pytest
from cryptography.fernet import Fernet

from botsapp.secretbox import SecretBox, SecretBoxError


def _key() -> str:
    return Fernet.generate_key().decode()


def test_round_trip_and_ciphertext_is_not_the_secret():
    box = SecretBox([_key()])
    token = box.encrypt("wk_secret_value")
    assert "wk_secret_value" not in token
    assert box.decrypt(token) == "wk_secret_value"


def test_each_encryption_differs():
    box = SecretBox([_key()])
    assert box.encrypt("same") != box.encrypt("same")


def test_wrong_master_key_fails_closed_without_leaking():
    token = SecretBox([_key()]).encrypt("wk_secret_value")
    with pytest.raises(SecretBoxError) as excinfo:
        SecretBox([_key()]).decrypt(token)
    assert "wk_secret_value" not in str(excinfo.value)


def test_tampered_value_fails():
    box = SecretBox([_key()])
    token = box.encrypt("x")
    with pytest.raises(SecretBoxError):
        box.decrypt(token[:-4] + "AAAA")


def test_master_key_can_be_rotated_by_prepending_a_new_one():
    old, new = _key(), _key()
    token = SecretBox([old]).encrypt("wk_secret_value")
    rotated = SecretBox([new, old])  # new encrypts, both decrypt
    assert rotated.decrypt(token) == "wk_secret_value"
    assert SecretBox([new]).decrypt(rotated.encrypt("later")) == "later"
    with pytest.raises(SecretBoxError):
        SecretBox([new]).decrypt(token)  # once the old key is dropped


@pytest.mark.parametrize("bad", ["", "not-a-fernet-key", "short"])
def test_malformed_keys_are_rejected_clearly(bad):
    with pytest.raises(SecretBoxError):
        SecretBox([bad] if bad else [])


def test_from_env(monkeypatch):
    monkeypatch.delenv("BOTSAPP_SECRET_KEY", raising=False)
    assert SecretBox.from_env() is None
    a, b = _key(), _key()
    monkeypatch.setenv("BOTSAPP_SECRET_KEY", f" {a} , {b} ")
    box = SecretBox.from_env()
    assert box is not None and box.decrypt(SecretBox([b]).encrypt("x")) == "x"
    monkeypatch.setenv("BOTSAPP_SECRET_KEY", "garbage")
    with pytest.raises(SecretBoxError, match="Fernet"):
        SecretBox.from_env()
