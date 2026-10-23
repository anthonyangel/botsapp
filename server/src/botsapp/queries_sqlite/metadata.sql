-- SQLite dialect (aiosql "aiosqlite" driver) — see docs/decisions/
-- 0007-sqlite-for-metadata-store.md for why chat_metadata moved off
-- Postgres. Differences from the old Postgres version worth knowing:
--   * tags is TEXT holding a JSON-encoded array, not a native array type —
--     SQLite has none. db.py encodes/decodes it at the boundary.
--   * is_allowed is INTEGER (0/1) — SQLite has no boolean type. db.py
--     coerces it back to bool on read.
--   * No "notes" column — dropped, no use case for free-text notes (tags
--     cover it).
--   * No "search_by_tags" query here — SQLite can't do Postgres's `&&`
--     array-overlap operator, and the old query's JOIN to whatsmeow_contacts
--     (a WuzAPI/Postgres-only table) no longer applies now that WAHA is the
--     only supported bridge. list_metadata_rows fetches every allowed row
--     instead; db.py does the tag-overlap filtering in Python — a full
--     scan is free at the row counts this table actually has (family-scale,
--     comments elsewhere in this codebase note dozens of chats, not
--     thousands).

-- name: create_chat_metadata_table#
-- Create the chat_metadata table if it does not exist. Called during
-- application startup (lifespan). No separate is_allowed migration here
-- (unlike the old Postgres version) — this is a fresh file with the column
-- present from the start, not a volume that might predate it.
CREATE TABLE IF NOT EXISTS chat_metadata (
    jid        TEXT PRIMARY KEY,
    tags       TEXT NOT NULL DEFAULT '[]',
    is_allowed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- name: upsert_chat_metadata!
-- Insert or update tags for a JID. Does not touch is_allowed — that's only
-- ever changed via set_chat_allowed (the admin UI), never by anything
-- Claude/the MCP server can call, so a chat can't allow itself.
INSERT INTO chat_metadata (jid, tags, updated_at)
VALUES (:jid, :tags, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
ON CONFLICT (jid) DO UPDATE SET
    tags       = :tags,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now');

-- name: set_chat_allowed!
-- Insert or update the allowlist flag for a JID. Does not touch tags —
-- admin-UI-only, never exposed as an MCP tool.
INSERT INTO chat_metadata (jid, is_allowed, updated_at)
VALUES (:jid, :is_allowed, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
ON CONFLICT (jid) DO UPDATE SET
    is_allowed = :is_allowed,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now');

-- name: get_chat_metadata^
-- Get metadata for a single JID.
SELECT jid, tags, is_allowed, updated_at
FROM   chat_metadata
WHERE  jid = :jid;

-- name: list_allowed_jids
-- Every JID currently marked visible to the MCP server.
SELECT jid FROM chat_metadata WHERE is_allowed = 1;

-- name: list_metadata_rows
-- Every *allowed* chat's metadata — db.py.search_by_tags filters this down
-- to rows overlapping the requested tags in Python (see note above).
SELECT jid, tags, is_allowed, updated_at
FROM   chat_metadata
WHERE  is_allowed = 1;

-- name: create_allowed_emails_table#
-- The email allowlist that gates whether an authenticated AuthKit caller
-- may use the MCP server at all (app.py's allowed_family_email) — see
-- docs/decisions/0015-email-allowlist-in-db.md for why this moved off the
-- ALLOWED_EMAILS env var. Storing here rather than a new file/service
-- keeps it in the one place mcp-server and admin-ui already share.
CREATE TABLE IF NOT EXISTS allowed_emails (
    email      TEXT PRIMARY KEY,
    added_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- name: add_allowed_email!
-- Idempotent — adding an already-present email is a no-op, not an error.
INSERT INTO allowed_emails (email)
VALUES (:email)
ON CONFLICT (email) DO NOTHING;

-- name: remove_allowed_email!
DELETE FROM allowed_emails WHERE email = :email;

-- name: list_allowed_emails
SELECT email, added_at FROM allowed_emails ORDER BY added_at;

-- name: get_allowed_email^
-- Single-row lookup for the auth check itself — no need to pull every row
-- for a per-request membership test.
SELECT email FROM allowed_emails WHERE email = :email;

-- ── Multi-tenancy (docs/decisions/0025) ─────────────────────────────────
-- users supersedes allowed_emails as the "may log in at all" list (the old
-- table is left in place, unread, after a one-time backfill). accounts are
-- paired WhatsApp numbers; grants say which users may query an account;
-- chat_access is the per-(account, viewer) successor to chat_metadata.

-- name: create_users_table#
CREATE TABLE IF NOT EXISTS users (
    email        TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    is_admin     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- name: create_accounts_table#
CREATE TABLE IF NOT EXISTS accounts (
    id           TEXT PRIMARY KEY,
    owner_email  TEXT NOT NULL REFERENCES users (email),
    label        TEXT NOT NULL DEFAULT '',
    waha_session TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- Scoped WAHA session key (docs/decisions/0027). The id and the effective
    -- actions are not secret; the ciphertext is, and is only ever read by
    -- get_account_key, never by the general account queries below. Databases
    -- created before these columns existed get them from db.py's migration.
    waha_key_id         TEXT,
    waha_key_ciphertext TEXT,
    waha_key_actions    TEXT
);

-- name: create_grants_table#
CREATE TABLE IF NOT EXISTS grants (
    account_id TEXT NOT NULL REFERENCES accounts (id),
    user_email TEXT NOT NULL REFERENCES users (email),
    PRIMARY KEY (account_id, user_email)
);

-- name: create_chat_access_table#
CREATE TABLE IF NOT EXISTS chat_access (
    account_id   TEXT NOT NULL REFERENCES accounts (id),
    viewer_email TEXT NOT NULL REFERENCES users (email),
    jid          TEXT NOT NULL,
    tags         TEXT NOT NULL DEFAULT '[]',
    is_allowed   INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (account_id, viewer_email, jid)
);

-- name: backfill_users_from_allowed_emails!
-- (WHERE true: SQLite needs a WHERE to parse INSERT..SELECT..ON CONFLICT.)
INSERT INTO users (email, created_at)
SELECT email, added_at FROM allowed_emails WHERE true
ON CONFLICT (email) DO NOTHING;

-- name: add_user!
INSERT INTO users (email, display_name, is_admin)
VALUES (:email, :display_name, :is_admin)
ON CONFLICT (email) DO NOTHING;

-- name: set_user_admin!
UPDATE users SET is_admin = :is_admin WHERE email = :email;

-- name: set_user_display_name!
UPDATE users SET display_name = :display_name WHERE email = :email;

-- name: get_user^
SELECT email, display_name, is_admin, created_at FROM users WHERE email = :email;

-- name: list_users
SELECT email, display_name, is_admin, created_at FROM users ORDER BY created_at, email;

-- name: delete_user!
DELETE FROM users WHERE email = :email;

-- name: delete_user_grants!
DELETE FROM grants WHERE user_email = :email;

-- name: delete_user_chat_access!
DELETE FROM chat_access WHERE viewer_email = :email;

-- name: count_accounts_owned_by^
SELECT COUNT(*) AS n FROM accounts WHERE owner_email = :email;

-- name: add_account!
INSERT INTO accounts (id, owner_email, label, waha_session)
VALUES (:id, :owner_email, :label, :waha_session)
ON CONFLICT (id) DO NOTHING;

-- name: get_account^
SELECT id, owner_email, label, waha_session, created_at, waha_key_id, waha_key_actions FROM accounts WHERE id = :id;

-- name: list_accounts
SELECT id, owner_email, label, waha_session, created_at, waha_key_id, waha_key_actions FROM accounts ORDER BY created_at, id;

-- name: list_accounts_for_user
-- Accounts the user owns or has been granted.
SELECT id, owner_email, label, waha_session, created_at, waha_key_id, waha_key_actions FROM accounts
WHERE owner_email = :email
   OR id IN (SELECT account_id FROM grants WHERE user_email = :email)
ORDER BY created_at, id;

-- name: add_grant!
INSERT INTO grants (account_id, user_email) VALUES (:account_id, :user_email)
ON CONFLICT DO NOTHING;

-- name: remove_grant!
DELETE FROM grants WHERE account_id = :account_id AND user_email = :user_email;

-- name: remove_viewer_chat_access!
-- Revoking a grant also drops what that viewer had been shown.
DELETE FROM chat_access WHERE account_id = :account_id AND viewer_email = :user_email;

-- name: list_grants
SELECT account_id, user_email FROM grants WHERE account_id = :account_id ORDER BY user_email;

-- name: has_account_access^
SELECT 1 AS ok FROM accounts a
WHERE a.id = :account_id
  AND (a.owner_email = :email
       OR EXISTS (SELECT 1 FROM grants g WHERE g.account_id = a.id AND g.user_email = :email));

-- name: upsert_chat_access_tags!
-- Does not touch is_allowed (same invariant as upsert_chat_metadata).
INSERT INTO chat_access (account_id, viewer_email, jid, tags, updated_at)
VALUES (:account_id, :viewer_email, :jid, :tags, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
ON CONFLICT (account_id, viewer_email, jid) DO UPDATE SET
    tags       = :tags,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now');

-- name: set_chat_access_allowed!
INSERT INTO chat_access (account_id, viewer_email, jid, is_allowed, updated_at)
VALUES (:account_id, :viewer_email, :jid, :is_allowed, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
ON CONFLICT (account_id, viewer_email, jid) DO UPDATE SET
    is_allowed = :is_allowed,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now');

-- name: get_chat_access^
SELECT jid, tags, is_allowed, updated_at FROM chat_access
WHERE account_id = :account_id AND viewer_email = :viewer_email AND jid = :jid;

-- name: list_allowed_jids_for_viewer
SELECT jid FROM chat_access
WHERE account_id = :account_id AND viewer_email = :viewer_email AND is_allowed = 1;

-- name: list_chat_access_rows
SELECT jid, tags, is_allowed, updated_at FROM chat_access
WHERE account_id = :account_id AND viewer_email = :viewer_email AND is_allowed = 1;

-- name: backfill_chat_access_from_legacy!
-- One-time, idempotent copy of the single-tenant chat_metadata rows into the
-- bootstrap account/viewer. Never overwrites a row that already exists.
INSERT INTO chat_access (account_id, viewer_email, jid, tags, is_allowed, updated_at)
SELECT :account_id, :viewer_email, jid, tags, is_allowed, updated_at FROM chat_metadata WHERE true
ON CONFLICT (account_id, viewer_email, jid) DO NOTHING;

-- name: delete_account_grants!
DELETE FROM grants WHERE account_id = :account_id;

-- name: delete_account_chat_access!
DELETE FROM chat_access WHERE account_id = :account_id;

-- name: delete_account!
DELETE FROM accounts WHERE id = :account_id;

-- name: get_account_key^
-- The only query that returns the encrypted key.
SELECT waha_key_id, waha_key_ciphertext, waha_key_actions FROM accounts WHERE id = :account_id;

-- name: set_account_key!
UPDATE accounts
SET waha_key_id = :key_id, waha_key_ciphertext = :ciphertext, waha_key_actions = :actions
WHERE id = :account_id;

-- name: set_account_key_actions!
UPDATE accounts SET waha_key_actions = :actions WHERE id = :account_id;

-- name: clear_account_key!
UPDATE accounts
SET waha_key_id = NULL, waha_key_ciphertext = NULL, waha_key_actions = NULL
WHERE id = :account_id;
