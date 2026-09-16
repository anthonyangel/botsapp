# 0007 — Move `chat_metadata` off Postgres onto SQLite; drop Postgres from the default stack

**Status:** Decided (2026-09). Builds on 0006's bridge abstraction (WuzAPI/WAHA) — see the note at the end about that record.

## Context

Postgres was in this stack for two unrelated reasons that happened to share one server:

1. WuzAPI (whatsmeow) persists all chat/message history directly into Postgres tables it owns (`message_history`, `whatsmeow_contacts`) — `WuzAPIPostgresMessageStore` reads those directly rather than duplicating that state.
2. This project's own `chat_metadata` table (tags/notes/`is_allowed`) — data whatsapp-mcp owns itself, with nothing bridge-specific about it.

With `BRIDGE_PROVIDER=waha` as the deployed choice going forward, reason (1) no longer applies: WAHA's `MessageStore` reads its own REST API, never Postgres. That leaves Postgres running solely for (2) — one small table, family-scale (a few dozen chats, `AllowlistedMessageStore`'s own docstring already says as much), auth tokens live at WorkOS ([0005](0005-authkit-oauth-provider.md)), not here.

Worth checking before deciding what to replace it with: [WAHA's own storage docs](https://waha.devlike.pro/docs/how-to/storages/) show WAHA's *own* default session storage, for a comparably-small piece of durable per-install state, is an embedded SQLite file (`waha.sqlite3`) — Postgres is offered there only as an alternative for multi-*worker* deployments sharing one session store, a scaling concern that doesn't apply here. Independent confirmation, not just this project's own reasoning, and it also means WAHA's optional Postgres support (if ever turned on) wouldn't help `chat_metadata` regardless — WAHA creates its own isolated per-namespace databases, a schema this project's `DatabaseManager` has no access to or use for.

## Options considered

| Option | Verdict |
|---|---|
| Keep Postgres | Works, but a full RDBMS server + volume + healthcheck for one tiny table, once WuzAPI's reason for it is gone. |
| MongoDB | Lateral move, not a downsize — same operational weight (a full separate server daemon) for a workload with zero variable/nested schema to justify it. |
| Valkey/Redis | Wrong center of gravity — `is_allowed` is the actual privacy boundary ([0004](0004-chat-allowlist-and-admin-ui.md)); durability should be the default, not something tuned in a store built around speed/TTL/pub-sub. Not in WAHA's own storage vocabulary either. |
| **SQLite (chosen)** | Embedded, zero extra container/volume/healthcheck, real WAL-mode durability, trivially handles the light concurrent access from `mcp-server` + `admin-ui` at this scale. Mirrors WAHA's own choice for the same class of problem. |

## Decision

- `chat_metadata` moves to SQLite (via `aiosqlite`), unconditionally — this table is bridge-agnostic and was already documented as such; it doesn't depend on which provider is active.
- `DatabaseManager` (`db.py`) is rewritten against `aiosqlite` instead of `asyncpg`: `create_pool()`/`close_pool()`/`.pool` are renamed to `connect()`/`close()`/`.connection` since "pool" stopped being accurate (a single file connection, not a pool). Tags round-trip through a JSON-encoded `TEXT` column (SQLite has no array type) and `is_allowed` through `INTEGER` 0/1 (no boolean type) — both encode/decode at the `db.py` boundary so every caller still sees `list[str]` / `bool` exactly as before.
- The old `search_by_tags` query's `LEFT JOIN whatsmeow_contacts` (WuzAPI/Postgres-only data, for resolving a display name) is dropped — SQLite can't join across engines, and this join never resolved anything under WAHA anyway (that table simply doesn't exist there), so `name` falling back to the jid is not a regression, just made explicit. Tag-overlap filtering (previously Postgres's `&&` operator) moves into Python — a full scan of a family-scale table is free.
- Two `aiosql` loaders now, not one — `queries/` (Postgres dialect, `chats.sql`/`messages.sql`, wuzapi-only) and `queries_sqlite/` (SQLite dialect, `metadata.sql`) — since the two tables are genuinely different databases now, not just different files sharing one connection.
- `bridge_factory.build_bridge_and_store()`'s `pg_pool` parameter becomes optional (`None` default) and wuzapi-only — it used to double as the metadata store's own pool; now that pool is SQLite, wuzapi's own message-history Postgres pool (if reactivated) is the caller's responsibility to construct and pass in separately (e.g. from a `WUZAPI_DATABASE_URL` this project no longer defines by default). Passing `pg_pool=None` while `BRIDGE_PROVIDER=wuzapi` raises a clear `ValueError` rather than failing confusingly deeper in a query.
- `docker-compose.yml`: `postgres` and `whatsapp-bridge` (WuzAPI) are moved behind a `wuzapi` Compose profile — `docker compose up` no longer starts either by default; `docker compose --profile wuzapi up` brings both back if wuzapi is ever reactivated. `mcp-server`/`admin-ui` now depend only on `waha`, and `DATABASE_URL` for both is a SQLite file path (`/app/data/chat_metadata.db`) on a new shared `metadata-data` volume (WAL mode handles the two processes opening the same file concurrently).
- **Media, while reviewing storage options**: WAHA's own docs default it to local-file media storage with a **180-second lifetime** — silently deleting received media before `get_media()` has any real chance of being called for it. `docker-compose.yml` now sets `WHATSAPP_FILES_LIFETIME=0` (persist) with `WHATSAPP_FILES_FOLDER` on a new `waha-media` volume. Local files remain the right choice here (same family-scale, single-host reasoning as the metadata store) over WAHA's Postgres/S3 media-storage alternatives — this was an existing latent gap in the running config, not something the SQLite move itself required changing.
- **Migration**: existing `chat_metadata` rows (35 rows: 23 allowed chats with real tags, 12 disallowed direct chats) were exported from the live Postgres instance and seeded into the new SQLite file via `DatabaseManager`'s own API before cutover — verified against the live admin UI and MCP server post-cutover. `postgres`/`whatsapp-bridge` containers were stopped, not removed — their volumes are intact if a rollback is ever needed.

## Consequences

- One fewer service in the default local/production stack; the wuzapi profile keeps that path available without deleting it, per 0006's bridge-agnostic goal.
- Reactivating `BRIDGE_PROVIDER=wuzapi` now needs a small amount of new wiring (a `pg_pool` the caller constructs and passes to `build_bridge_and_store`) that didn't exist before — a deliberate, documented trade for not carrying Postgres pool-lifecycle code for a currently-unused path.
- Tests: `test_db.py` was rewritten against a real temp-file SQLite database (`aiosqlite` is fast enough that this is simpler and more honest than mocking `aiosql` internals) rather than mocking the query layer.

## Addendum: dropped `notes`

Right after this migration, the `notes` free-text column (present since `chat_metadata`'s original design under [0004](0004-chat-allowlist-and-admin-ui.md)) was dropped entirely — no actual use case for plaintext per-chat notes, tags cover it. Removed from the SQLite schema (`queries_sqlite/metadata.sql`), `DatabaseManager`, the `tag_chat`/`get_chat_metadata` MCP tools, and the admin UI's metadata form. The wuzapi-only `search_chats` query (`queries/chats.sql`) also had its `LEFT JOIN chat_metadata` dropped in the same pass — that join stopped working the moment `chat_metadata` moved to SQLite above (Postgres can't join across engines) and had gone unnoticed since wuzapi is inactive; fixed here rather than left as a second latent bug now that the file was already open for the `notes` column removal.

## Note on 0006

Several existing docstrings across this codebase (`bridge_factory.py`, `message_store.py`, `waha_client.py`, others) cite `docs/decisions/0006-bridge-abstraction-and-waha-support.md` for the WuzAPI/WAHA bridge-abstraction decision this record builds on — that file does not actually exist in this repo (confirmed while writing this record) and isn't listed in [`docs/decisions/README.md`](README.md) either. That decision clearly *was* made and implemented (the abstraction is real, in `bridge.py`/`bridge_factory.py`/`waha_client.py`), just never written up. Worth creating separately so those references resolve to something.
