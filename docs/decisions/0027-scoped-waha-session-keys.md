# 0027 — Per-account scoped WAHA session keys, encrypted in the database

**Status:** Decided and implemented (2026-10) for #52, against a stubbed WAHA key API. **Not yet verified against a live WAHA** (#47): the key endpoints' field names, the scope each read call needs, and `PUT /api/keys` behaviour are documented-but-unchecked assumptions listed below. Tool visibility from key scopes (#54) is described in its own section below and is built the same way (stubs, not live).

## Context

After [0025](0025-multi-tenancy-users-accounts-grants.md) every account has its own WAHA session, but all of them were reached with one shared admin key (`WAHA_API_KEY`). The wall between accounts, and the "never send" rule, therefore existed only in botsapp's code: a bug that passed the wrong session, or a compromise of the MCP process, could read any account or send as it. WAHA supports session-scoped keys (`POST /api/keys` with `isAdmin: false`, a `session`, and an `actions` map: `read`, `send`, `control`, `setting`, `app`, `delete`), which enforce this inside WAHA.

## Decision

- **Each account gets its own scoped session key** for the MCP server, created with `MCP_KEY_ACTIONS` (`key_manager.py`): `read` only; `send`, `control`, `setting`, `app` and `delete` off.
- **Stored encrypted in `accounts`** (`waha_key_ciphertext`) with Fernet under `BOTSAPP_SECRET_KEY` (`secretbox.py`). The variable may hold several comma-separated keys (first encrypts, all decrypt) so the master key itself can be rotated. The ciphertext is returned by exactly one query (`get_account_key`); the general account reads carry only the non-secret `waha_key_id` and `waha_key_actions`. The key is never logged, never rendered, and `KeyInfo.__repr__` hides it.
- **Not Fly's secrets API.** Setting a Fly secret restarts the machine and is meant for a few static values, not per-account data created at runtime. Only the master key is a deployment secret, set once.
- **The key is chosen by process, not by tool.** `BridgeRegistry.for_account(account, purpose=...)` requires a `purpose` (no default, so a forgotten argument can't land on the powerful key): `"mcp"` gets the account's scoped key; `"admin"` gets the admin key, for session lifecycle, QR and the key API. A rotated key is picked up across processes because the MCP side compares the stored ciphertext on every call.
- **Fail closed, with an explicit transition.** With `WAHA_REQUIRE_SCOPED_KEYS=true` the MCP side refuses an account with no stored key, and a stored key it cannot open (no or wrong master key) is never replaced by the admin key. Without the flag, an account lacking a key falls back to the admin key with a one-time warning, so existing deployments keep working while keys are created. Startup logs which accounts lack one. In strict mode the server also refuses to start without a master key, and on Fly `start.sh` launches the MCP process with `WAHA_API_KEY` removed from its environment (both processes share one environment there); the admin process keeps it.
- **Store what WAHA reports.** `KeyManager` (`key_manager.py`) never trusts its request: after creating a key it reads it back and requires a non-admin key for that session with exactly the requested actions, then smoke-tests it (the session status call, which works before pairing) and deletes it again on any failure. Only then is it stored, and if storing fails the WAHA key is deleted. Rotation stores the new key before deleting the old one (a failed old-key delete is reported, not fatal). `verify` re-reads the key from WAHA, stores the actions it reports, and shows differences from policy as drift. `revoke` deletes in WAHA, then forgets.
- **Admin UI (Accounts page, admins only):** creating an account also creates its key (undoing the session and row if that fails); each account shows its key state and offers *Create access key*, *Verify* and *Rotate*; deleting an account deletes its key first. Without a master key an account is still created, with a notice that it has no key.
- **WAHA's own labels** are the only vocabulary (`WAHA_ACTIONS` in `waha_keys.py`); unknown action names WAHA reports are kept, logged, and count as drift.

## Tool visibility from key scopes (#54)

A tool is presented to the model, and callable, only when a WAHA key can serve it.

- **WAHA's labels are the vocabulary; there is no mapping table.** A tool names what it needs by carrying WAHA's own action names as tags (`read`, `send`, `control`, `setting`, `app`, `delete`: `waha_keys.WAHA_ACTIONS`). Any other tag (`chat`, `group`, `metadata`, ...) is descriptive. A tool with none of the six needs nothing from WAHA; the ones that only touch our database (`list_accounts`, `add_tag`, `remove_tag`, `clear_tags`, `get_chat_metadata`) are tagged `local` and are always presented. `capabilities.py` holds the rule.
- **`WahaScopeMiddleware` (`tool_scopes.py`), after FastMCP's own `AuthMiddleware`,** applies it in the same two places FastMCP applies auth checks. *Listing*: the caller's accounts are fetched once, and a tool is shown if at least one account's key allows every action its tags name; `tools/list` carries no `account` argument, so it is a union (exact when the caller has one account). *Calling*: the same rule is enforced ("not found or not authorized"), and the tool's required actions are placed in a `ContextVar` so `resolve_scope` can refuse the specific account the call is about if its own key lacks them. With no recorded requirement (direct calls, local tools) nothing extra is checked.
- **What an account allows:** its stored key's actions (what WAHA reported, refreshed by *Verify*); with no key, everything while the shared admin key is the fallback, and nothing under `WAHA_REQUIRE_SCOPED_KEYS`. A caller who can't be identified gets only the local tools. stdio is left alone, as with FastMCP's own auth.
- **Tag changes:** `write` became `send` on the `send_*` tools (all permanently blocked by `outbound`, which stays the authority); the other blocked tools keep `write` because which WAHA action they need isn't documented (#47 item 16). The descriptive `media` tag on `get_media` became `attachment` so it can't be mistaken for WAHA's separate "media" key. The tag tools' `write` became `edit`.
- **Guard tests** (`tests/test_tool_scopes.py`): every reachable tool that calls WAHA names at least one action; the local-only tools (an explicit list) name none; anything tagged `send` is also `outbound`; every reachable read tool needs exactly `read`; the middleware is installed after `AuthMiddleware`; and it works inside a real FastMCP server.
- **Limits.** The stored actions are advisory and WAHA is the enforcer: a stale copy can only make a tool look available and then fail at WAHA, or hide one that would work, never widen access. Clients fetch the tool list when they connect, so after pairing, a new key or a rotation a client may need to reconnect; `list_changed` notifications are not sent. That every reachable tool needs only `read` is our reading of the docs and is unconfirmed (#47 items 14 to 16).

## Alternatives considered

| Option | Why not |
|---|---|
| Keep one shared admin key; rely on botsapp's code | Status quo: one bug or compromise reaches every session and can send. |
| A WAHA container per account, each with its own key | Real isolation, but a Fly machine, volume and memory per person; session keys give most of it for free. |
| Store the keys in Fly secrets | Restarts the machine per account, needs a powerful Fly token in the app, and doesn't suit runtime data. |
| Plaintext keys in SQLite | A volume snapshot or stray dump would leak every key. |
| Derive each key from a master secret instead of storing it | WAHA generates the key value; not documented as choosable. |
| Per-tool decorators choosing a key | Every reachable tool needs the same thing (read); a per-tool grant is another place to be wrong. The key is a floor chosen per process. |

## Consequences

- **Rollout order** (also in `fly.toml`): set `BOTSAPP_SECRET_KEY` on both processes; deploy; create each account's key on the Accounts page (including `default`, whose WAHA session already exists); confirm the MCP server still works; then set `WAHA_REQUIRE_SCOPED_KEYS=true`. To back out, remove the flag.
- **Losing the master key** makes stored keys unreadable: the MCP server answers "this account isn't ready" for those accounts rather than falling back. Restore the key, or *Rotate* on the Accounts page (rotation does not need to decrypt the old key).
- The MCP read path now depends on a stored key being valid; *Verify* exists so drift is noticed, but WAHA remains the enforcer (a stale record can only make a tool fail or look available, never widen access).
- A production `main` deployment keeps working unchanged until keys are created: the flag is off by default.

## Unverified assumptions (to confirm in #47)

- The key endpoints' payloads (`id`, `key`, `isAdmin`, `session`, `isActive`, `actions`) and that `GET /api/keys` lists every key with its effective `actions`.
- That the read calls we make (chats, messages, groups, contacts, avatars, media, session status) are all allowed by `read` alone; if WAHA refuses any with `MCP_KEY_ACTIONS`, widen that constant deliberately. The smoke test only proves the session status call.
- Whether `PUT /api/keys/{id}` can change `actions` in place (the code uses rotation, which works either way), and when a change takes effect.
- Which WAHA action each endpoint needs (the table for #54).

## Supersedes / amends

Amends [0025](0025-multi-tenancy-users-accounts-grants.md): the shared admin key is no longer the only way the MCP side reaches WAHA, and the "wall between accounts" is now also enforced by WAHA once keys are in place.
