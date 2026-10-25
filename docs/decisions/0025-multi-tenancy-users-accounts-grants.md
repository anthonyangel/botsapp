# 0025 — Multi-tenancy: users, accounts, grants, per-viewer chat access

**Status:** Decided (2026-10). Phases 1–4 are implemented on PR #45 (schema, bridge registry, scoped MCP enforcement, and the admin login and pages decided in [0026](0026-admin-login-authkit-single-org-authn-authz-split.md)); phase 5 (onboarding the second user) waits on the live checks below.

**Where things stand / what's left** (ideas and follow-ups live in GitHub issues, not in this file). Everything below up to #54 is built in PR #45 against stubbed WorkOS/WAHA and stays open until verified live:
- Live WAHA probe: multi-session, session keys, key updates, scope table — #47. Run 2026-10-25 without a paired account: multi-session and scoped keys work, `PUT /api/keys` edits in place, scope table recorded (see [0027](0027-scoped-waha-session-keys.md)). Still open: checks needing a paired phone (full read path, media key, memory of a paired session).
- Admin login, Users, Accounts, self-service Chats — #48, #49, #50, #51 (each has a "remaining" checklist; a real WorkOS login is the common blocker).
- Per-account WAHA session keys, encrypted in the DB — [0027](0027-scoped-waha-session-keys.md), #52; MCP tools shown only when the key's scopes allow them — #54.
- Not built: pending-invitation display on the Users page — #57; `tools/list_changed` after key/share/pairing changes — #58; the setup runbook — #56; Jinja2 for the admin templates — #55.
- Retire the legacy tables, single-tenant code and transition scaffolding (admin-key fallback, `ADMIN_DISABLE_AUTH`) once live — #53.

## Context

botsapp was single-tenant by construction: one WAHA session (`WAHA_SESSION`),
one global `chat_metadata` allowlist/tag table, one global `allowed_emails`
list, and a login-less admin UI reachable only on the private network
([0004](0004-chat-allowlist-and-admin-ui.md), [0013](0013-single-fly-app-shared-volume.md),
[0015](0015-email-allowlist-in-db.md)). A second person now needs access, in
two shapes: her own WhatsApp number paired and exposed to her own MCP login,
and — on top of that — some of the owner's chats exposed to her too.

The second shape rules out plain tenant isolation: a shared WhatsApp account
must show different chats to different viewers.

**WAHA multi-session was checked, not assumed.** WAHA Core used to accept only a
session named `default` (HTTP 422 `OnlyDefaultSessionIsAllowed`; more needed
"WAHA Plus"). The official waha-docs FAQ says that since **2026.6.1** WAHA is
fully free/open source, "everything is part of WAHA Core", with no separate Plus
image and unlimited sessions. The pinned image ([0023](0023-waha-noweb-version-pinned-images.md),
`noweb-2026.9.2`) is newer. Verified live on 2026-10-25 (#47): `POST /api/sessions {"name":"probe","start":false}` returns 201 on the pinned image. Memory/CPU
grow per NOWEB session (an unpaired session adds almost nothing; a paired one was not measured), so size the Fly machine before adding one.

## Decision

- **Botsapp user** is a first-class record: `users(email PK, display_name,
  is_admin)`. A row is required to authenticate to the MCP server and to the
  admin UI. It supersedes `allowed_emails` (backfilled once; the old table is
  left unread). The DB stays the source of truth ([0015](0015-email-allowlist-in-db.md)).
- **Account** = one paired WhatsApp number: `accounts(id, owner_email,
  waha_session UNIQUE)`. The existing setup becomes account `default`, bound to
  the existing WAHA session, owned by the first admin.
- **Grant** = `(account, user)`: who may query an account through MCP.
  The owner is implicit.
- **Chat access is per (account, viewer)**, not per account:
  `chat_access(account_id, viewer_email, jid, tags, is_allowed)`. Default deny.
  Sharing an account does not share its chats; each viewer is shown only what
  the owner (or the viewer, for chats on an account they own) allowed for them.
  Tags are per viewer. Revoking a grant deletes that viewer's rows, so a
  re-grant starts empty. Every read re-checks the grant, so stale rows never leak.
- **Bootstrap:** `BOTSAPP_ADMIN_EMAIL` makes that email an admin, creates the
  `default` account for it, and copies legacy `chat_metadata` into its
  `chat_access`. Idempotent, run by both processes at start.
- **Admin surfaces (phase 4, implemented):** two, replacing the single login-less UI.
  1. *Global admin* (`is_admin` only): users, accounts, grants, all sessions.
  2. *Self-service* (any user, scoped to their own accounts): pair a session
     (QR), edit tags, choose which chats are visible, and grant/revoke their own
     account to others.
- **MCP (phase 3, implemented):** the token's email resolves to the caller's
  accounts; new `list_accounts` tool; tools take an `account` argument, implicit
  when the caller has exactly one. A `Scope(account, viewer)` replaces the global
  allowed set in `AllowlistedMessageStore`. The `outbound`/`restrict_tag` block is
  unchanged.

## Supersedes / amends

- **Supersedes** the "admin-ui has no login of its own, private-only, always"
  invariant in [0004](0004-chat-allowlist-and-admin-ui.md) and
  [0013](0013-single-fly-app-shared-volume.md) — *once phase 4 ships*. Self-service
  means she must reach and authenticate to the admin UI; the login mechanism is
  decided in [0026](0026-admin-login-authkit-single-org-authn-authz-split.md)
  (AuthKit web session). Until then the admin UI is unchanged and
  stays private, with the owner operating it.
- **Supersedes** `allowed_emails` ([0015](0015-email-allowlist-in-db.md)) as the
  login gate (users table), keeping its "DB, not env var" reasoning.
- **Amends** [0004](0004-chat-allowlist-and-admin-ui.md): the allowlist is no
  longer global; `chat_metadata` is superseded by `chat_access` in phase 3.

## Phases

1. Schema + migration + tests (**done**: no behaviour change; email-allowlist
   methods now read `users`).
2. `BridgeRegistry` (account → its own `WAHAClient`/WAHA session) **done**, built
   alongside the old single bridge and not yet used by tools. The live WAHA
   multi-session probe is still to be run once against a real instance.
3. Scoped enforcement **done**. Every reachable tool starts with
   `scope.resolve_scope(account)`: caller email from the token, account from the
   `account` argument (implicit when the caller has exactly one; an account the
   caller doesn't hold gets the same "Unknown account" error as a missing one),
   then a per-request bridge, `ScopedChatDb` and `AllowlistedMessageStore` bound
   to (account, viewer). New `list_accounts` tool. `state.message_store` is gone;
   `state.bridge` remains only for the permanently blocked outbound tools. The
   admin UI now reads and writes `chat_access` as the first admin on the `default`
   account. `chat_metadata` is no longer read or written (left in place, as with
   `allowed_emails`). Tigris keys already carry a random UUID and a 15-minute
   lifetime, so no per-account prefix was added.
4. Admin auth decision record, then the two admin surfaces — **done** (0026; login and pages built against stubbed WorkOS/WAHA, not yet verified live).
5. Onboard the second user — **pending** a real WorkOS login, #52 and the paired-phone checks left in #47.

## Consequences

- **Deploy requirement:** `BOTSAPP_ADMIN_EMAIL` must be set on both processes
  (Fly: `[env]` or `fly secrets`) before this ships, or nothing is visible and the
  admin UI's chat edits are disabled. First start copies the old allowlist/tags
  into that user's `chat_access`; edits after that are not mirrored back.
- The admin UI has a login (0026). Until it has been verified against a live WorkOS, the private setup keeps `ADMIN_DISABLE_AUTH`, under which it acts as the first admin.
- Removing a user who owns an account is refused; transfer/delete the account first.
- Same-jid chats across accounts are independent rows (keyed by account).
- Operating the admin UI as the sole admin means the admin can see a paired
  account's chat list; that is the trust model until self-service pairing lands.
