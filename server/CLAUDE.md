# server/ — the `botsapp` Python package

Root of this repo's only real code: a `uv`-managed Python 3.13 package
(`src/botsapp/`) that builds two ASGI apps from one codebase — the MCP
server and the admin UI. See the root [CLAUDE.md](../CLAUDE.md) for the
project-level architecture and the decision log this all implements.

## Entry points

One image (`Dockerfile`), two `command:` overrides for local dev (Compose,
each on its own port, no proxy, no prefix) vs. three processes on Fly via
`server/start.sh` ([0013](../docs/decisions/0013-single-fly-app-shared-volume.md),
[0028](../docs/decisions/0028-caddy-fronted-admin-ui-public-prefix.md)):
Caddy (`server/Caddyfile`) is the only thing Fly's public port reaches,
forwarding `/admin/*` to admin-ui and everything else to mcp-server, which on
Fly binds loopback-only instead of the public interface.

- `botsapp.main:app` — the MCP server, port 8000 locally (8001, loopback-only,
  behind Caddy on Fly). FastMCP instance lives in `app.py` (kept separate from
  `main.py` so `tools.py` can import it without a circular dependency); tools
  live in `tools.py`.
- `botsapp.admin_main:app` — the admin UI, port 8100. Starlette app in
  `admin/app.py`, login + route policy in `admin/auth.py` (AuthKit via an
  `AuthKitClient` protocol, signed-cookie session, per-request role lookup,
  Origin check for CSRF), `admin/users.py` (Users page) and `admin/accounts.py` (the
  merged Session/Accounts page, [0029](../docs/decisions/0029-session-and-accounts-pages-merged.md)), hand-rolled f-string templates in `admin/templates.py`
  (no Jinja2 — the UI is a handful of pages, not enough surface to justify
  the dependency). `admin/prefix.py` holds `ADMIN_URL_PREFIX` (unset locally;
  `/admin` on Fly, behind Caddy) — every route, redirect, href and form
  action in the package goes through its `p()` helper, since Caddy forwards
  the path unstripped and nothing here uses Starlette's `url_for`.

## Module map

- `bridge.py` / `waha_client.py` — `Bridge` Protocol + its one implementation
  (WAHA REST API). WAHA is the *only* provider ([0008](../docs/decisions/0008-remove-wuzapi-waha-only.md))
  but the Protocol split stays so tests target the interface, not a concrete
  client.
- `message_store.py` — `MessageStore` Protocol (bridge-agnostic chat/message
  reads) + `AllowlistedMessageStore`, which wraps any store to enforce the
  chat allowlist. **This wrapper is the actual privacy boundary** — see
  [0004](../docs/decisions/0004-chat-allowlist-and-admin-ui.md). Raises
  `ChatNotAllowedError` (distinct from "no such chat") when a jid exists but
  isn't allowlisted.
- `bridge_registry.py` — `BridgeRegistry`: one `WAHAClient` per `accounts` row (its own
  WAHA session), built lazily; maps account → client, decides no authorization.
  See [0025](../docs/decisions/0025-multi-tenancy-users-accounts-grants.md).
- `secretbox.py` / `waha_keys.py` / `key_manager.py` — scoped WAHA session keys
  ([0027](../docs/decisions/0027-scoped-waha-session-keys.md)): Fernet encryption of the
  stored keys, WAHA key parsing + the `KeyApi` protocol, and `KeyManager`
  (provision/rotate/verify/revoke: change WAHA, read it back, then record). The registry
  takes a required `purpose` (`"mcp"` = the account's scoped key, `"admin"` = the admin
  key) and fails closed under `WAHA_REQUIRE_SCOPED_KEYS`.
- `capabilities.py` / `tool_scopes.py` — tool visibility from key scopes
  ([0027](../docs/decisions/0027-scoped-waha-session-keys.md)): a tool's WAHA-action tags
  (`read`, `send`, ...) are what it needs; `WahaScopeMiddleware` hides and refuses tools no
  account's key can serve, and `resolve_scope` checks the specific account. Keep a tool's
  tags honest: label WAHA-calling tools with the action, `local` ones with none.
- `scope.py` — `resolve_scope(account)`: per tool call, caller email (token) +
  account → `Scope(bridge, ScopedChatDb, AllowlistedMessageStore)` bound to that
  (account, viewer). Every reachable tool must start with it; never read
  allowlist/tags straight from `state.db`.
- `bridge_factory.py` — `build_bridge_and_store()`, the one place that
  constructs the bridge; both `app.py` and `admin/app.py` call it so
  construction can't drift between the two processes. One `WAHAClient`
  instance satisfies both the `Bridge` and `MessageStore` protocols.
- `db.py` — `DatabaseManager`, SQLite (`aiosqlite`) for users, accounts, grants and
  per-(account, viewer) `chat_access` (tags + `is_allowed`); legacy
  `chat_metadata`/`allowed_emails` are no longer read — not chat/message history, that's
  `MessageStore`'s job. Shared file, WAL mode, read by both processes
  ([0007](../docs/decisions/0007-sqlite-for-metadata-store.md)).
- `session.py` — WhatsApp session/QR pairing logic, called from both
  `tools.whatsapp_session` and the admin UI's per-account pairing controls — kept in one
  place so the two surfaces can't drift.
- `tools.py` — the `@mcp.tool()`-decorated MCP tools. Every tool that
  mutates WhatsApp state is tagged `"outbound"`; `app.py` wires
  `restrict_tag("outbound", scopes=["whatsapp:send"])` against a scope no
  real auth token will ever carry, making those tools permanently
  unreachable **by construction** — not by leaving them unregistered. Adding
  a new tool that sends/edits/deletes/manages groups: tag it `"outbound"` or
  it will actually be callable.
- `app.py` — FastMCP instance, lifespan (constructs bridge + `DatabaseManager`
  into `state`), `AuthKitProvider` wiring, the email-allowlist gate
  (`allowed_family_email` — DB-backed via `state.db.is_email_allowed`, not an
  env var, see [0015](../docs/decisions/0015-email-allowlist-in-db.md)),
  `LOCAL_DEV_DISABLE_AUTH` escape hatch (dev only — does *not* relax the
  outbound-tag block).
- `state.py` — module-level globals (`db`, `bridge`, `registry`, `admin_chat_db`)
  set up in `app.py`'s lifespan; tools read through this rather than a
  request-scoped context.
- `_authkit_issuer_workaround.py` — works around the open bug in
  [0012](../docs/decisions/0012-authkitprovider-issuer-trailing-slash-bug.md).
  Don't remove until that record is marked fixed.

## Testing

- `pytest`, `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed).
- `conftest.py` sets dummy `WORKOS_AUTHKIT_DOMAIN`/`MCP_BASE_URL` via
  `os.environ.setdefault` *before* importing `botsapp.app` — that module
  builds its `AuthKitProvider` at import time, so collection would otherwise
  need a real WorkOS project. A real `.env` (e.g. under docker-compose) wins
  over these. No `ALLOWED_EMAILS` equivalent needed here anymore — the email
  allowlist lives in the DB (`mock_db`/the real `db` fixture), not an env var.
- `mock_db`/`mock_message_store` fixtures are `AsyncMock(spec=...)` against
  the real classes/Protocols — keeps mocks honest against signature drift.
- `test_db.py` runs against a real temp-file SQLite database via `aiosqlite`
  rather than mocking the query layer (`aiosql` internals aren't worth
  mocking around — see [0007](../docs/decisions/0007-sqlite-for-metadata-store.md)).
- HTTP mocking uses **`aresponses`**, not `aioresponses` — it runs a real
  local `aiohttp` test server instead of monkeypatching `aiohttp` internals,
  so it survives `aiohttp` version bumps. See [0009](../docs/decisions/0009-aresponses-for-http-mocking.md).
  Don't reach for `aioresponses` in new tests.
- `pyproject.toml`'s `[[tool.ty.overrides]]` relaxes `unresolved-attribute`/
  `invalid-assignment` for `tests/**` only — deliberate, documented there:
  tests monkeypatch `Optional`-typed module globals (`state.db`, `state.bridge`)
  with `AsyncMock`s and read them back via `.return_value`/`.assert_awaited_once_with`,
  which `ty` can't flow-narrow across a module-attribute boundary. Don't
  "fix" this by adding `assert x is not None` at every call site or loosening
  real (non-test) code to `Any`.

## Commands

Run from `server/` directly, or via the root `Taskfile.yml` (`task test`,
`task lint`, `task typecheck`, `task fmt`, `task check` — the last mirrors
what CI should run):

```bash
uv sync --all-extras
uv run pytest
uv run ruff check src tests
uv run ruff format src tests   # task fmt also runs --fix
uv run ty check
```
