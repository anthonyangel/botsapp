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
