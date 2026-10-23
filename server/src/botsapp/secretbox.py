"""
Encryption for secrets stored in the database (docs/decisions/0027, #52).

Per-account WAHA session keys live in ``accounts`` as ciphertext. The master key
is ``BOTSAPP_SECRET_KEY``: a normal deployment secret, set once, never changed at
runtime (so it is not a Fly secret that has to be rewritten per account).

* Fernet (AES-128-CBC + HMAC-SHA256, authenticated): a wrong key or a tampered
  value fails to decrypt instead of returning garbage.
* The variable may hold several comma-separated keys. The first encrypts; all are
  tried when decrypting (``MultiFernet``), so the master key can be rotated by
  prepending a new one, re-saving the stored keys, then dropping the old one.

Generate a key with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class SecretBoxError(Exception):
    """Bad configuration, or a stored value that this key cannot open."""


class SecretBox:
    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise SecretBoxError("no encryption key given")
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])
        except (ValueError, TypeError) as exc:
            raise SecretBoxError(
                "BOTSAPP_SECRET_KEY must be 32 url-safe base64 bytes (a Fernet key)"
            ) from exc

    @classmethod
    def from_env(cls) -> SecretBox | None:
        """The box for ``BOTSAPP_SECRET_KEY``, or None when it isn't set."""
        raw = os.environ.get("BOTSAPP_SECRET_KEY", "").strip()
        if not raw:
            return None
        return cls([k.strip() for k in raw.split(",") if k.strip()])

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            # Deliberately says nothing about the value or the key.
            raise SecretBoxError("cannot decrypt: wrong key, or the value was altered") from exc
