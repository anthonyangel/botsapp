# 0015 — Email allowlist moves from `ALLOWED_EMAILS` env var into the shared DB

**Status:** Decided (2026-09)

## Context

Since [0003](0003-google-oauth-authn-authz.md)/[0005](0005-authkit-oauth-provider.md),
`allowed_family_email` gated MCP-server access by reading a comma-separated
`ALLOWED_EMAILS` env var (`fly secrets set`/`.env`) at check time. Editing it
meant a Fly secrets update and a redeploy/restart — friction for something
that's really just data (who's in the family), not configuration, and the
kind of thing that should be a quick admin-UI edit the same way the chat
allowlist already is ([0004](0004-chat-allowlist-and-admin-ui.md)).

Two things made moving it low-risk to check before committing to this:

- `mcp-server` and `admin-ui` already share one SQLite DB for exactly this
  kind of allow/deny data ([0007](0007-sqlite-for-metadata-store.md)/[0013](0013-single-fly-app-shared-volume.md)).
- `allowed_family_email` needs to become an `async def` to query it — read
  `fastmcp/utilities/authorization.py` directly to confirm `AuthCheck` is
  typed `Callable[[AuthContext], bool] | Callable[[AuthContext], Awaitable[bool]]`
  and its evaluator (`_evaluate_check`) awaits the result when it's a
  coroutine. No framework fight; this is a supported case, not a hack.

No bootstrap/lockout risk either: `admin-ui` has no login of its own by
design (network-private reachability is the only gate, per 0004) — there's
no chicken-and-egg where you'd need an already-allowed email to reach the
UI that grants allowed emails.

## Decision

- New `allowed_emails` table in `chat_metadata.db` (`email TEXT PRIMARY KEY`,
  `added_at`), alongside the existing `chat_metadata` table — same file,
  same WAL-mode sharing, `db.py`/`metadata.sql` gain
  `list_allowed_emails`/`add_allowed_email`/`remove_allowed_email`/`is_email_allowed`.
- `allowed_family_email` (`app.py`) is now `async def`, checking
  `state.db.is_email_allowed(email)` instead of parsing an env var. The
  `restrict_tag`/outbound-tag reasoning from 0003/0005/[0011](0011-jwt-template-drops-workostokenverifier.md)
  is untouched — this only changes where the *email* allowlist's data lives.
- New **Access** tab in the admin UI (`/access`) — add/remove emails, same
  Starlette-route-plus-hand-rolled-template pattern `/chats` already uses
  ([0004](0004-chat-allowlist-and-admin-ui.md)). Distinct from `/chats`:
  this gates who can authenticate at all, not what an authenticated caller
  can see.
- **`ALLOWED_EMAILS` is removed outright** — no env var, no fallback, no
  migration path. The user chose this explicitly over keeping it as a
  seed/fallback, to avoid the exact two-sources-of-truth drift
  [0011](0011-jwt-template-drops-workostokenverifier.md)/[0012](0012-authkitprovider-issuer-trailing-slash-bug.md)
  already had to untangle for AuthKit config. Consequence: the table starts
  empty on any environment that hasn't had emails added via `/access` yet —
  nobody can use that MCP server until someone does. This includes the
  already-deployed Fly Production environment: its family emails need
  adding via `/access` post-deploy, the same one-time step a fresh chat
  allowlist already requires per [0004](0004-chat-allowlist-and-admin-ui.md).

## Consequences

- `ALLOWED_EMAILS` no longer appears anywhere: `.env.example`,
  `docker-compose.yml`, `server/fly.toml`'s comment, `README.md`,
  `CLAUDE.md`/`server/CLAUDE.md`, and `tests/conftest.py` are all updated to
  match. A grep for `ALLOWED_EMAILS` across the repo should come back empty
  from this record forward.
- Editing the allowlist is now a same-second admin-UI action, not a Fly
  secrets/redeploy cycle — this was the actual motivation.
- `tests/test_app_auth.py`'s email-allowlist tests now stub
  `state.db.is_email_allowed` (via the autouse `_patch_state`/`mock_db`
  fixtures in `conftest.py`) instead of `patch.dict(os.environ, ...)`, and
  are `async def` to await the check directly.
