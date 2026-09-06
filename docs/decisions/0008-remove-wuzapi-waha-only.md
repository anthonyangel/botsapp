# 0008 — Remove WuzAPI; WAHA is the only bridge provider

**Status:** Decided (2026-09). Builds on 0006's bridge abstraction and 0007's Postgres removal.

## Context

Since 0006, this project supported two interchangeable WhatsApp bridge providers behind a shared `Bridge`/`MessageStore` interface, selected at runtime via `BRIDGE_PROVIDER`:

- **WAHA** — the provider actually deployed and used in production.
- **WuzAPI** (whatsmeow) — the original bridge (see 0001), kept alive as a second implementation once WAHA proved out, but never run in production after the switch.

0007 already reflected WuzAPI's reduced relevance by moving `chat_metadata` off Postgres and profiling `postgres`/`whatsapp-bridge` out of the default `docker compose up`. Since then, WuzAPI's implementation (`wuzapi_client.py`, `WuzAPIPostgresMessageStore`, the `queries/` Postgres query set, the `wuzapi` Compose profile) has kept costing maintenance — every `Bridge`/`MessageStore` protocol change had to be implemented and tested twice, a code-review pass found several places where the WuzAPI implementation had silently drifted from WAHA's behavior (missing exception handling, a narrower "never raises" contract) — for a code path with no actual user.

## Decision

Remove WuzAPI entirely rather than keep it as unused optionality:

- Deleted `wuzapi_client.py`, `WuzAPIPostgresMessageStore` (`message_store.py`), the Postgres `queries/` directory and its `aiosql` loader (`_queries.py`), and their tests (`test_wuzapi_client.py`, `test_wuzapi_bridge.py`, the `WuzAPIPostgresMessageStore` half of `test_message_store.py`).
- `bridge_factory.build_bridge_and_store()` no longer branches on `BRIDGE_PROVIDER` or takes a `pg_pool` — it constructs `WAHAClient` directly. `BRIDGE_PROVIDER` is no longer read anywhere; `bridge_provider_name()` stays (the admin UI displays it) but now just returns the constant `"waha"`.
- `docker-compose.yml`: `postgres` and `whatsapp-bridge` services, the `wuzapi` Compose profile, and the `postgres-data`/`wuzapi-data` volumes are gone. `WUZAPI_*` env vars are gone from `docker-compose.yml` and `.env.example`.
- `asyncpg` dropped from `pyproject.toml` — nothing in the codebase talks to Postgres anymore.
- The `Bridge`/`MessageStore` Protocol split (`bridge.py`, `message_store.py`) stays as-is: it costs nothing with one implementation, keeps `AllowlistedMessageStore` decoupled from the concrete client, and means adding a different bridge later still wouldn't require touching `tools.py`.
- ADRs 0001, 0006, and 0007 are left untouched — they're a historical record of decisions made when WuzAPI was still a live option, not living documentation. (The `docs/plans/` documents referenced from several of these records were implementation plans, not decision records — those were fully implemented and later deleted from the repo entirely as noise; see the note in [docs/decisions/README.md](README.md) on why decision records and implementation plans have different lifespans.)

## Consequences

- One bridge implementation to keep correct instead of two; the class of bug this surfaced (behavior silently diverging between the "live" and "unused" implementation) can't recur.
- Reactivating WuzAPI (or adding a different provider) would mean re-adding a `Bridge`/`MessageStore` implementation from scratch — deliberately not kept "just in case," per this record's own reasoning about unused optionality.
- `docker compose up` starts fewer services locally (`waha`, `mcp-server`, `admin-ui` — no `postgres`/`whatsapp-bridge`), with no profile flag needed.
