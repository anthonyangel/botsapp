# 0030 — Multi-tenancy cleanup: legacy tables and transition fallbacks removed

*Status: implemented (#53). Completes [0025](0025-multi-tenancy-users-accounts-grants.md) and [0026](0026-admin-login-authkit-single-org-authn-authz-split.md); amends [0027](0027-scoped-waha-session-keys.md) and [0028](0028-caddy-fronted-admin-ui-public-prefix.md).*

## Context

0025–0029 added multi-tenancy beside the single-tenant code instead of replacing
it, so each step could roll out and be undone. #53 held the cleanup for after the
rollouts were live. This record removes the scaffolding in one go.

## Decision

- **Legacy tables dropped.** `chat_metadata` and `allowed_emails` are no longer
  created, read or written; `ensure_schema` drops them if present, but **only once
  an account exists** (the 0025 bootstrap, which adopted their data into
  `chat_access` / `users`, creates one). A database that was never bootstrapped keeps
  them; there is no remaining code path that adopts them, so such a database must be
  bootstrapped on the previous release first. The drop is irreversible.
- **Dead `DatabaseManager` code removed:** the single-tenant chat methods, the
  email-allowlist wrappers (`is_email_allowed` stays: the MCP gate uses it) and the
  one-time backfills. `ScopedChatDb` keeps its method names and maps onto the
  per-viewer methods.
- **No default-session bridge.** `state.bridge` and `build_bridge_and_store` are
  gone. The permanently blocked outbound tools now take their bridge from
  `resolve_scope()`, i.e. the account's read-only scoped key, so even unblocked
  they could not send.
- **Scoped keys are mandatory.** `WAHA_REQUIRE_SCOPED_KEYS` is removed and its
  behaviour is the only one: `BridgeRegistry` fails closed for an account without a
  key, `capabilities.account_actions` gives an unkeyed account no actions, the MCP
  process requires `BOTSAPP_SECRET_KEY`, and `start.sh` always withholds
  `WAHA_API_KEY` from it. *Every account must have a key before deploying this.*
- **`ADMIN_DISABLE_AUTH` removed.** The admin UI needs the AuthKit login settings.
  `LOCAL_DEV_DISABLE_AUTH` (and the `dev_user` path) stays as the local-development
  opt-out. *The WorkOS login (#48) must be working before deploying this, or the
  admin UI will not start.*
- **`/access` redirect removed** (`/users` has been the page since 0026).
- The "blocked tools still tagged `write`" item was already done in #64.

## Consequences

- Rolling back to a build that expects the legacy tables is not supported once they
  are dropped; restore the SQLite volume from a backup.
- Nothing here was exercised against live WAHA/WorkOS; the checks are the unit tests.
