"""
Database access layer for chat metadata (tags/allowlist).

This is data whatsapp-mcp owns itself — unlike chat/message history (see
message_store.py), it has nothing bridge-specific about it and doesn't move
if the WhatsApp bridge underneath ever changes.

SQLite-backed (via aiosqlite), not Postgres — this table is small
(family-scale, a few dozen chats at most) and doesn't need a separate
database server.

``is_allowed`` here is the actual privacy boundary the MCP server enforces
— it's only ever written by ``set_chat_allowed``, which the admin UI calls
and nothing in ``tools.py`` does, so a chat can never allow itself.
"""

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from botsapp._queries import metadata_queries as queries

logger = logging.getLogger(__name__)


class DatabaseManager:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> aiosqlite.Connection:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        # WAL mode: the MCP server and the admin UI are separate processes
        # that both open this same file — WAL lets the rare writer (a tag
        # edit, an allow-toggle click) proceed without blocking readers.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.commit()
        logger.info("SQLite metadata store connected: %s", self._db_path)
        return self._conn

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("SQLite metadata store connection closed")

    @property
    def connection(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Database not connected — call connect() first.")
        return self._conn

    # ── Chat metadata ─────────────────────────────────────────────────────

    async def ensure_schema(self) -> None:
        """Create the tables if they don't exist yet. Called once at startup."""
        await queries.create_chat_metadata_table(self.connection)
        await queries.create_allowed_emails_table(self.connection)
        await queries.create_users_table(self.connection)
        await queries.create_accounts_table(self.connection)
        await self._add_missing_account_columns()
        await queries.create_grants_table(self.connection)
        await queries.create_chat_access_table(self.connection)
        # One-time, idempotent: anyone on the legacy email allowlist becomes
        # a user. After this users is the source of truth (0025).
        await queries.backfill_users_from_allowed_emails(self.connection)
        await self.connection.commit()

    async def _add_missing_account_columns(self) -> None:
        """CREATE TABLE IF NOT EXISTS leaves an older ``accounts`` table as it was;
        add the scoped-key columns to it. Idempotent."""
        cursor = await self.connection.execute("PRAGMA table_info(accounts)")
        have = {row[1] async for row in cursor}
        for column in ("waha_key_id", "waha_key_ciphertext", "waha_key_actions"):
            if column not in have:
                await self.connection.execute(f"ALTER TABLE accounts ADD COLUMN {column} TEXT")

    @staticmethod
    def _account_row(row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        raw = d.get("waha_key_actions")
        d["waha_key_actions"] = json.loads(raw) if raw else None
        return d

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        """SQLite has no array or boolean type — tags round-trips through
        JSON text, is_allowed through 0/1 — so every read goes through here
        to hand callers the same shapes the old Postgres version did
        (tags: list[str], is_allowed: bool)."""
        d = dict(row)
        d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
        d["is_allowed"] = bool(d.get("is_allowed"))
        return d

    async def upsert_chat_metadata(self, jid: str, tags: list[str]) -> None:
        await queries.upsert_chat_metadata(self.connection, jid=jid, tags=json.dumps(tags))
        await self.connection.commit()

    async def get_chat_metadata(self, jid: str) -> dict[str, Any] | None:
        row = await queries.get_chat_metadata(self.connection, jid=jid)
        return self._row_to_dict(row) if row else None

    async def search_by_tags(self, tags: list[str]) -> list[dict[str, Any]]:
        """Allowed chats carrying any of ``tags`` — backs the MCP-facing
        ``list_chats(tags=...)`` and ``list_groups(tags=...)`` tools, the
        primary (metadata-first) way those tools look a chat/group up.

        Case-insensitive: tags are typed by a person (via the admin UI or
        add_tag) or guessed by Claude from a user's phrasing, and "Shaked"
        vs "shaked" shouldn't be the difference between a hit and a miss.

        Fetches every allowed row and filters in Python rather than in SQL —
        a full scan that's free at this table's size (see module docstring).
        ``name`` here just falls back to the jid; this table has no separate
        contacts data to resolve a display name from.
        """
        wanted = {t.lower() for t in tags}
        rows = [self._row_to_dict(r) async for r in queries.list_metadata_rows(self.connection)]
        matches = [r for r in rows if wanted & {t.lower() for t in r["tags"]}]
        for r in matches:
            r["name"] = r["jid"]
        matches.sort(key=lambda r: r["updated_at"], reverse=True)
        return matches

    # ── Allowlist ────────────────────────────────────────────────────────

    async def list_allowed_jids(self) -> set[str]:
        return {r["jid"] async for r in queries.list_allowed_jids(self.connection)}

    async def set_chat_allowed(self, jid: str, is_allowed: bool) -> None:
        await queries.set_chat_allowed(self.connection, jid=jid, is_allowed=is_allowed)
        await self.connection.commit()

    # ── Email allowlist ──────────────────────────────────────────────────
    # Who's allowed to authenticate to the MCP server at all — app.py's
    # allowed_family_email checks this per request. Distinct from the chat
    # allowlist above (which gates what an already-authenticated caller can
    # see). See docs/decisions/0015-email-allowlist-in-db.md.

    @staticmethod
    def _normalize_email(email: str) -> str:
        return email.strip().lower()

    async def list_allowed_emails(self) -> list[str]:
        return [u["email"] for u in await self.list_users()]

    async def add_allowed_email(self, email: str) -> None:
        await self.add_user(email)

    async def remove_allowed_email(self, email: str) -> None:
        await self.remove_user(email)

    async def is_email_allowed(self, email: str) -> bool:
        return await self.get_user(email) is not None

    # ── Users ────────────────────────────────────────────────────────────
    # The "botsapp user" (docs/decisions/0025): an email that may log in.
    # Replaces the bare allowed_emails list; the email-allowlist methods
    # above are thin wrappers kept so callers don't change until phase 3.

    @staticmethod
    def _user_row(row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        d["is_admin"] = bool(d["is_admin"])
        return d

    async def add_user(self, email: str, display_name: str = "", is_admin: bool = False) -> None:
        await queries.add_user(
            self.connection,
            email=self._normalize_email(email),
            display_name=display_name,
            is_admin=is_admin,
        )
        await self.connection.commit()

    async def get_user(self, email: str) -> dict[str, Any] | None:
        row = await queries.get_user(self.connection, email=self._normalize_email(email))
        return self._user_row(row) if row else None

    async def list_users(self) -> list[dict[str, Any]]:
        return [self._user_row(r) async for r in queries.list_users(self.connection)]

    async def set_user_admin(self, email: str, is_admin: bool) -> None:
        await queries.set_user_admin(
            self.connection, email=self._normalize_email(email), is_admin=is_admin
        )
        await self.connection.commit()

    async def set_user_display_name(self, email: str, display_name: str) -> None:
        await queries.set_user_display_name(
            self.connection, email=self._normalize_email(email), display_name=display_name
        )
        await self.connection.commit()

    async def owned_account_count(self, email: str) -> int:
        row = await queries.count_accounts_owned_by(
            self.connection, email=self._normalize_email(email)
        )
        return int(row["n"]) if row else 0

    async def remove_user(self, email: str) -> None:
        """Delete a user, their grants and what they'd been shown. Refuses if
        they still own an account — transfer or delete that first, so a paired
        WhatsApp number is never orphaned."""
        email = self._normalize_email(email)
        owned = await queries.count_accounts_owned_by(self.connection, email=email)
        if owned and owned["n"]:
            raise ValueError(f"{email} still owns {owned['n']} account(s)")
        await queries.delete_user_grants(self.connection, email=email)
        await queries.delete_user_chat_access(self.connection, email=email)
        await queries.delete_user(self.connection, email=email)
        await self.connection.commit()

    # ── Accounts & grants ────────────────────────────────────────────────

    async def add_account(
        self, account_id: str, owner_email: str, waha_session: str, label: str = ""
    ) -> None:
        owner = self._normalize_email(owner_email)
        if await self.get_user(owner) is None:
            raise ValueError(f"unknown user {owner}")
        await queries.add_account(
            self.connection,
            id=account_id,
            owner_email=owner,
            label=label,
            waha_session=waha_session,
        )
        await self.connection.commit()

    # ── Scoped WAHA key (docs/decisions/0027) ────────────────────────────

    async def get_account_key(self, account_id: str) -> dict[str, Any] | None:
        """``{key_id, ciphertext, actions}`` for the account, or None if it has no
        key. The only read that returns the ciphertext."""
        row = await queries.get_account_key(self.connection, account_id=account_id)
        if row is None or not row["waha_key_ciphertext"]:
            return None
        return {
            "key_id": row["waha_key_id"],
            "ciphertext": row["waha_key_ciphertext"],
            "actions": json.loads(row["waha_key_actions"]) if row["waha_key_actions"] else {},
        }

    async def set_account_key(
        self, account_id: str, key_id: str, ciphertext: str, actions: dict[str, bool]
    ) -> None:
        await queries.set_account_key(
            self.connection,
            account_id=account_id,
            key_id=key_id,
            ciphertext=ciphertext,
            actions=json.dumps(actions),
        )
        await self.connection.commit()

    async def set_account_key_actions(self, account_id: str, actions: dict[str, bool]) -> None:
        await queries.set_account_key_actions(
            self.connection, account_id=account_id, actions=json.dumps(actions)
        )
        await self.connection.commit()

    async def clear_account_key(self, account_id: str) -> None:
        await queries.clear_account_key(self.connection, account_id=account_id)
        await self.connection.commit()

    async def delete_account(self, account_id: str) -> None:
        """Remove an account with its grants and every viewer's chat access.
        The caller deletes the WAHA session first (see BridgeRegistry)."""
        await queries.delete_account_grants(self.connection, account_id=account_id)
        await queries.delete_account_chat_access(self.connection, account_id=account_id)
        await queries.delete_account(self.connection, account_id=account_id)
        await self.connection.commit()

    async def get_account(self, account_id: str) -> dict[str, Any] | None:
        row = await queries.get_account(self.connection, id=account_id)
        return self._account_row(row) if row else None

    async def list_accounts(self) -> list[dict[str, Any]]:
        return [self._account_row(r) async for r in queries.list_accounts(self.connection)]

    async def list_accounts_for_user(self, email: str) -> list[dict[str, Any]]:
        email = self._normalize_email(email)
        return [
            self._account_row(r)
            async for r in queries.list_accounts_for_user(self.connection, email=email)
        ]

    async def has_account_access(self, account_id: str, email: str) -> bool:
        row = await queries.has_account_access(
            self.connection, account_id=account_id, email=self._normalize_email(email)
        )
        return row is not None

    async def grant_account(self, account_id: str, user_email: str) -> None:
        user = self._normalize_email(user_email)
        if await self.get_user(user) is None or await self.get_account(account_id) is None:
            raise ValueError("unknown user or account")
        await queries.add_grant(self.connection, account_id=account_id, user_email=user)
        await self.connection.commit()

    async def revoke_account(self, account_id: str, user_email: str) -> None:
        """Remove a grant and everything that viewer had been shown.

        A no-op for the account's owner: the owner holds access implicitly
        (see ``has_account_access``), never via a grant, so there is nothing
        to revoke — and running the delete anyway would wipe the owner's own
        chat_access rows (their allowlist/tags), since those are keyed by
        viewer_email like any other viewer's.
        """
        user = self._normalize_email(user_email)
        account = await self.get_account(account_id)
        if account is not None and account["owner_email"] == user:
            return
        await queries.remove_grant(self.connection, account_id=account_id, user_email=user)
        await queries.remove_viewer_chat_access(
            self.connection, account_id=account_id, user_email=user
        )
        await self.connection.commit()

    async def list_grants(self, account_id: str) -> list[str]:
        return [
            r["user_email"]
            async for r in queries.list_grants(self.connection, account_id=account_id)
        ]

    # ── Per-(account, viewer) chat access ────────────────────────────────
    # Successor to the global chat_metadata methods above. Every method
    # requires the viewer to actually hold access to the account, so a
    # revoked or never-granted viewer can neither read nor write rows.

    async def _require_access(self, account_id: str, viewer_email: str) -> str:
        viewer = self._normalize_email(viewer_email)
        if not await self.has_account_access(account_id, viewer):
            raise PermissionError(f"{viewer} has no access to account {account_id}")
        return viewer

    async def set_chat_access_allowed(
        self, account_id: str, viewer_email: str, jid: str, is_allowed: bool, *, acting_email: str
    ) -> None:
        """Change what ``viewer_email`` is allowed to see on this account.

        Only the account's owner may call this — otherwise a viewer the
        account was shared with could simply allow every chat for themself,
        which the MCP server would then honour. Enforced here, not just by
        the admin UI's route handler, so no other caller can bypass it.
        """
        viewer = await self._require_access(account_id, viewer_email)
        account = await self.get_account(account_id)
        actor = self._normalize_email(acting_email)
        if account is None or account["owner_email"] != actor:
            raise PermissionError(f"{actor} is not the owner of account {account_id}")
        await queries.set_chat_access_allowed(
            self.connection,
            account_id=account_id,
            viewer_email=viewer,
            jid=jid,
            is_allowed=is_allowed,
        )
        await self.connection.commit()

    async def upsert_chat_access_tags(
        self, account_id: str, viewer_email: str, jid: str, tags: list[str]
    ) -> None:
        viewer = await self._require_access(account_id, viewer_email)
        await queries.upsert_chat_access_tags(
            self.connection,
            account_id=account_id,
            viewer_email=viewer,
            jid=jid,
            tags=json.dumps(tags),
        )
        await self.connection.commit()

    async def get_chat_access(
        self, account_id: str, viewer_email: str, jid: str
    ) -> dict[str, Any] | None:
        viewer = self._normalize_email(viewer_email)
        if not await self.has_account_access(account_id, viewer):
            return None
        row = await queries.get_chat_access(
            self.connection, account_id=account_id, viewer_email=viewer, jid=jid
        )
        return self._row_to_dict(row) if row else None

    async def list_allowed_jids_for(self, account_id: str, viewer_email: str) -> set[str]:
        """Empty for a viewer without access to the account, even if stale
        chat_access rows exist — the grant is checked on every read."""
        viewer = self._normalize_email(viewer_email)
        if not await self.has_account_access(account_id, viewer):
            return set()
        return {
            r["jid"]
            async for r in queries.list_allowed_jids_for_viewer(
                self.connection, account_id=account_id, viewer_email=viewer
            )
        }

    async def search_by_tags_for(
        self, account_id: str, viewer_email: str, tags: list[str]
    ) -> list[dict[str, Any]]:
        """Per-viewer counterpart of ``search_by_tags`` (case-insensitive)."""
        viewer = self._normalize_email(viewer_email)
        if not await self.has_account_access(account_id, viewer):
            return []
        wanted = {t.lower() for t in tags}
        rows = [
            self._row_to_dict(r)
            async for r in queries.list_chat_access_rows(
                self.connection, account_id=account_id, viewer_email=viewer
            )
        ]
        matches = [r for r in rows if wanted & {t.lower() for t in r["tags"]}]
        for r in matches:
            r["name"] = r["jid"]
        matches.sort(key=lambda r: r["updated_at"], reverse=True)
        return matches

    # ── Bootstrap ────────────────────────────────────────────────────────

    async def bootstrap(
        self, admin_email: str, waha_session: str, account_id: str = "default"
    ) -> None:
        """Idempotently adopt the single-tenant data (0025 phase 1): make
        ``admin_email`` an admin user, give them the bootstrap account bound to
        the existing WAHA session, and copy the legacy ``chat_metadata`` rows
        into their ``chat_access``. Safe to run from both processes at every
        start; never overwrites rows that already exist."""
        admin = self._normalize_email(admin_email)
        await self.add_user(admin, is_admin=True)
        await self.set_user_admin(admin, True)
        await self.add_account(account_id, admin, waha_session, label="Primary")
        await queries.backfill_chat_access_from_legacy(
            self.connection, account_id=account_id, viewer_email=admin
        )
        await self.connection.commit()
