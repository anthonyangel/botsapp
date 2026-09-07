# server/ — the `botsapp` Python package

Root of this repo's only real code: a `uv`-managed Python 3.13 package
(`src/botsapp/`) that builds two ASGI apps from one codebase — the MCP
server and the admin UI. See the root [CLAUDE.md](../CLAUDE.md) for the
project-level architecture and the decision log this all implements.

## Entry points

One image (`Dockerfile`), two `command:` overrides (Compose today, two
processes inside one Fly Machine once deployed via `server/start.sh` — see
[0013](../docs/decisions/0013-single-fly-app-shared-volume.md)):

- `botsapp.main:app` — the MCP server, port 8000. FastMCP instance lives in
  `app.py` (kept separate from `main.py` so `tools.py` can import it without
  a circular dependency); tools live in `tools.py`.
- `botsapp.admin_main:app` — the admin UI, port 8100. Starlette app in
  `admin/app.py`, hand-rolled f-string templates in `admin/templates.py`
  (no Jinja2 — the UI is a handful of pages, not enough surface to justify
  the dependency).

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
- `bridge_factory.py` — `build_bridge_and_store()`, the one place that
  constructs the bridge; both `app.py` and `admin/app.py` call it so
  construction can't drift between the two processes. One `WAHAClient`
  instance satisfies both the `Bridge` and `MessageStore` protocols.
- `db.py` — `DatabaseManager`, SQLite (`aiosqlite`) for `chat_metadata`
  (tags + `is_allowed`) only — not chat/message history, that's
  `MessageStore`'s job. Shared file, WAL mode, read by both processes
  ([0007](../docs/decisions/0007-sqlite-for-metadata-store.md)).
- `session.py` — WhatsApp session/QR pairing logic, called from both
  `tools.whatsapp_session` and the admin UI's session page — kept in one
  place so the two surfaces can't drift.
- `tools.py` — the `@mcp.tool()`-decorated MCP tools. Every tool that
  mutates WhatsApp state is tagged `"outbound"`; `app.py` wires
  `restrict_tag("outbound", scopes=["whatsapp:send"])` against a scope no
  real auth token will ever carry, making those tools permanently
  unreachable **by construction** — not by leaving them unregistered. Adding
  a new tool that sends/edits/deletes/manages groups: tag it `"outbound"` or
  it will actually be callable.
- `app.py` — FastMCP instance, lifespan (constructs bridge + `DatabaseManager`
  into `state`), `AuthKitProvider` wiring, the `ALLOWED_EMAILS` gate
  (`allowed_family_email`), `LOCAL_DEV_DISABLE_AUTH` escape hatch (dev only —
  does *not* relax the outbound-tag block).
- `state.py` — module-level globals (`db`, `bridge`, `message_store`, `mcp`)
  set up in `app.py`'s lifespan; tools read through this rather than a
  request-scoped context.
- `_authkit_issuer_workaround.py` — works around the open bug in
  [0012](../docs/decisions/0012-authkitprovider-issuer-trailing-slash-bug.md).
  Don't remove until that record is marked fixed.

## Testing

- `pytest`, `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed).
- `conftest.py` sets dummy `WORKOS_AUTHKIT_DOMAIN`/`MCP_BASE_URL`/`ALLOWED_EMAILS`
  via `os.environ.setdefault` *before* importing `botsapp.app` — that module
  builds its `AuthKitProvider` at import time, so collection would otherwise
  need a real WorkOS project. A real `.env` (e.g. under docker-compose) wins
  over these.
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
