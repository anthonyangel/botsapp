-- SQLite dialect (aiosql "aiosqlite" driver) — see docs/decisions/
-- 0007-sqlite-for-metadata-store.md. tags is TEXT holding a JSON-encoded
-- array and is_allowed an INTEGER (0/1), since SQLite has neither type;
-- db.py converts at the boundary.

-- ── Multi-tenancy (docs/decisions/0025) ─────────────────────────────────
-- users is the "may log in at all" list. accounts are paired WhatsApp
-- numbers; grants say which users may query an account; chat_access is the
-- per-(account, viewer) allowlist and tags.

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
-- Does not touch is_allowed (only set_chat_access_allowed changes it, so a chat can't allow itself).
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
