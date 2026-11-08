"""
Scoped WAHA session keys for accounts (docs/decisions/0027, #52).

Each account's MCP access uses its own WAHA *session* key instead of the shared
admin key, so WAHA itself refuses to touch another session or to send. This
module provisions, rotates, verifies and revokes those keys.

Ordering, the same idea as the Users and Accounts pages: change WAHA first, read
back what WAHA reports, then record it, and undo the WAHA side if recording
fails, so a failure never leaves a live key we don't know about:

* issue   — create the key, read it back (it must be a non-admin key for this
            session with exactly the requested actions), smoke-test it against
            WAHA, and delete it again if any of that fails. Nothing is stored yet.
* provision / rotate — issue, then store the *encrypted* key; if storing fails the
            new key is deleted. Rotation deletes the old key last (best effort).
* verify  — re-read the key from WAHA and store the actions WAHA reports, so edits
            made in WAHA's own dashboard are noticed; differences from our policy
            are returned as drift, not hidden.
* repair  — put a drifted key back to the policy in place (WAHA's PUT keeps the key
            value, so nothing else has to change); rotation is the fallback.
* revoke  — delete in WAHA, then forget it locally.

What is stored is what WAHA reported, never what we asked for. The secret is only
ever in memory here and as ciphertext in the database; it is never logged.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiohttp

from botsapp.secretbox import SecretBox
from botsapp.waha_client import WAHAClient
from botsapp.waha_keys import WAHA_ACTIONS, KeyApi, KeyApiError, KeyInfo

if TYPE_CHECKING:
    from botsapp.db import DatabaseManager

logger = logging.getLogger(__name__)

# What the MCP process may do with an account's session. Everything it calls is a
# read; nothing may send or delete. Which WAHA action each read call needs has not
# been confirmed against a live instance (#47): if a read call is refused with
# this scope, widen it here, deliberately.
MCP_KEY_ACTIONS: dict[str, bool] = {
    "read": True,
    "send": False,
    "control": False,
    "setting": False,
    "app": False,
    "delete": False,
}

# (session name, key secret) -> raises KeyManagerError if WAHA refuses the key.
Probe = Callable[[str, str], Awaitable[None]]


class KeyManagerError(Exception):
    """Something went wrong; the message is safe to show an administrator."""


@dataclass(frozen=True)
class Verification:
    key_id: str
    actions: dict[str, bool]
    drift: dict[str, tuple[bool, bool]]  # action -> (our policy, what WAHA reports)


def drift_from_policy(reported: dict[str, bool]) -> dict[str, tuple[bool, bool]]:
    names = set(WAHA_ACTIONS) | set(MCP_KEY_ACTIONS) | set(reported)
    return {
        name: (MCP_KEY_ACTIONS.get(name, False), reported.get(name, False))
        for name in sorted(names)
        if MCP_KEY_ACTIONS.get(name, False) != reported.get(name, False)
    }


def describe_drift(drift: dict[str, tuple[bool, bool]]) -> str:
    return ", ".join(f"{n}: wanted {w}, WAHA has {r}" for n, (w, r) in drift.items())


def make_probe(base_url: str) -> Probe:
    """A smoke test: the new key must be accepted for its own session. Only the
    session status is requested, since that works before the account is paired."""

    async def probe(session: str, secret: str) -> None:
        client = WAHAClient(base_url, secret, session=session)
        try:
            status = await client.get_session_status()
        finally:
            await client.close()
        if status.errors:
            raise KeyManagerError(f"WAHA refused the new key: {status.errors[0]}")

    return probe


class KeyManager:
    def __init__(
        self,
        db: DatabaseManager,
        api: KeyApi,
        box: SecretBox | None,
        probe: Probe | None = None,
    ) -> None:
        self._db = db
        self._api = api
        self._box = box
        self._probe = probe

    @property
    def can_store(self) -> bool:
        """Whether a master key is configured, i.e. keys can be created and kept."""
        return self._box is not None

    # ── helpers ──────────────────────────────────────────────────────────

    def _need_box(self) -> SecretBox:
        if self._box is None:
            raise KeyManagerError(
                "BOTSAPP_SECRET_KEY is not set, so keys can't be stored. Set it and restart."
            )
        return self._box

    @staticmethod
    def _wrap(exc: Exception) -> KeyManagerError:
        if isinstance(exc, KeyManagerError):
            return exc
        return KeyManagerError(f"WAHA: {exc}")

    async def _account(self, account_id: str) -> dict:
        account = await self._db.get_account(account_id)
        if account is None:
            raise KeyManagerError("Unknown account.")
        return account

    async def _discard(self, key_id: str) -> None:
        """Delete a key we created but won't keep. Never raises."""
        try:
            await self._api.delete_key(key_id)
        except Exception as exc:
            logger.error("could not delete the unused WAHA key %s: %s", key_id, exc)

    async def _issue(self, session: str) -> KeyInfo:
        """Create, read back and smoke-test a key; delete it again on any failure."""
        try:
            info = await self._api.create_key(session=session, actions=dict(MCP_KEY_ACTIONS))
        except (KeyApiError, RuntimeError, aiohttp.ClientError, TimeoutError) as exc:
            raise self._wrap(exc) from exc
        if not info.secret:
            await self._discard(info.id)
            raise KeyManagerError("WAHA created a key but did not return its value.")
        try:
            reported = await self._api.get_key(info.id)
            if reported is None:
                raise KeyManagerError("WAHA does not list the key it just created.")
            if reported.is_admin or reported.session != session:
                raise KeyManagerError("WAHA created a key with the wrong scope; it was removed.")
            if drift := drift_from_policy(reported.actions):
                raise KeyManagerError(
                    f"WAHA did not apply the requested scope ({describe_drift(drift)}); "
                    "the key was removed."
                )
            if self._probe is not None:
                await self._probe(session, info.secret)
        except (KeyApiError, RuntimeError, aiohttp.ClientError, TimeoutError) as exc:
            await self._discard(info.id)
            raise self._wrap(exc) from exc
        except KeyManagerError:
            await self._discard(info.id)
            raise
        return KeyInfo(
            id=info.id,
            session=reported.session,
            is_admin=False,
            is_active=reported.is_active,
            actions=reported.actions,
            secret=info.secret,
        )

    async def _store(self, account_id: str, info: KeyInfo, box: SecretBox) -> None:
        assert info.secret is not None
        try:
            await self._db.set_account_key(
                account_id, info.id, box.encrypt(info.secret), info.actions
            )
        except Exception as exc:
            logger.exception("could not store the key for %s; deleting it in WAHA", account_id)
            await self._discard(info.id)
            raise KeyManagerError("Couldn't save the key; it was removed from WAHA.") from exc

    # ── operations ───────────────────────────────────────────────────────

    async def provision(self, account_id: str) -> KeyInfo:
        """Give an account its first scoped key."""
        box = self._need_box()
        account = await self._account(account_id)
        if await self._db.get_account_key(account_id) is not None:
            raise KeyManagerError("This account already has a key; rotate it instead.")
        info = await self._issue(account["waha_session"])
        await self._store(account_id, info, box)
        return info

    async def rotate(self, account_id: str) -> tuple[KeyInfo, bool]:
        """Replace the key. Returns the new key's info and whether the old key was
        deleted in WAHA (False leaves an unused key there; it's logged)."""
        box = self._need_box()
        account = await self._account(account_id)
        old = await self._db.get_account_key(account_id)
        if old is None:
            raise KeyManagerError("This account has no key to rotate; create one.")
        info = await self._issue(account["waha_session"])
        await self._store(account_id, info, box)
        try:
            await self._api.delete_key(old["key_id"])
        except Exception as exc:
            logger.error(
                "rotated %s but could not delete old key %s: %s", account_id, old["key_id"], exc
            )
            return info, False
        return info, True

    async def verify(self, account_id: str) -> Verification:
        """Re-read the key from WAHA and store the actions it reports."""
        await self._account(account_id)
        stored = await self._db.get_account_key(account_id)
        if stored is None:
            raise KeyManagerError("This account has no key.")
        try:
            reported = await self._api.get_key(stored["key_id"])
        except (KeyApiError, RuntimeError, aiohttp.ClientError, TimeoutError) as exc:
            raise self._wrap(exc) from exc
        if reported is None:
            raise KeyManagerError("The key no longer exists in WAHA; rotate it.")
        if not reported.is_active:
            raise KeyManagerError("The key is deactivated in WAHA; rotate it.")
        await self._db.set_account_key_actions(account_id, reported.actions)
        return Verification(stored["key_id"], reported.actions, drift_from_policy(reported.actions))

    async def repair(self, account_id: str) -> Verification:
        """Reapply the intended scope to the existing key, in place.

        WAHA's ``PUT /api/keys/{id}`` changes ``actions`` without changing the key
        value, effective on the next request (#47), so the stored secret stays valid.
        What is stored afterwards is what WAHA reports, and any remaining drift is an
        error.
        """
        await self._account(account_id)
        stored = await self._db.get_account_key(account_id)
        if stored is None:
            raise KeyManagerError("This account has no key.")
        try:
            updated = await self._api.update_key_actions(stored["key_id"], dict(MCP_KEY_ACTIONS))
        except (KeyApiError, RuntimeError, aiohttp.ClientError, TimeoutError) as exc:
            raise self._wrap(exc) from exc
        await self._db.set_account_key_actions(account_id, updated.actions)
        if drift := drift_from_policy(updated.actions):
            raise KeyManagerError(
                f"WAHA did not apply the intended scope ({describe_drift(drift)}); rotate the key."
            )
        return Verification(stored["key_id"], updated.actions, {})

    async def revoke(self, account_id: str) -> None:
        """Delete the key in WAHA, then forget it. No key is a no-op."""
        stored = await self._db.get_account_key(account_id)
        if stored is None:
            return
        try:
            await self._api.delete_key(stored["key_id"])
        except (KeyApiError, RuntimeError, aiohttp.ClientError, TimeoutError) as exc:
            raise KeyManagerError(
                f"Couldn't delete the key in WAHA ({exc}); nothing changed."
            ) from exc
        await self._db.clear_account_key(account_id)


__all__ = [
    "MCP_KEY_ACTIONS",
    "KeyManager",
    "KeyManagerError",
    "Verification",
    "describe_drift",
    "drift_from_policy",
    "make_probe",
]
