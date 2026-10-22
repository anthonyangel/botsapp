# 0025 — Multi-tenancy: users, accounts, grants, per-viewer chat access

**Status:** Decided (2026-10); phases 1–3 implemented (schema, bridge registry, scoped MCP enforcement), phases 4–5 planned below.

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
`noweb-2026.9.2`) is newer. Not yet verified against a live instance (the
authoring sandbox had no Docker); phase 2's first step is the probe
`POST /api/sessions {"name":"probe","start":false}` → expect 201. Memory/CPU
grow per NOWEB session, so size the Fly machine before adding one.

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
- **Admin surfaces (planned, phase 4):** two, replacing the single login-less UI.
  1. *Global admin* (`is_admin` only): users, accounts, grants, all sessions.
  2. *Self-service* (any user, scoped to their own accounts): pair a session
     (QR), edit tags, choose which chats are visible, and grant/revoke their own
     account to others.
- **MCP (planned, phase 3):** the token's email resolves to the caller's
  accounts; new `list_accounts` tool; tools take an `account` argument, implicit
  when the caller has exactly one. A `Scope(account, viewer)` replaces the global
  allowed set in `AllowlistedMessageStore`. The `outbound`/`restrict_tag` block is
  unchanged.

## Supersedes / amends

- **Supersedes** the "admin-ui has no login of its own, private-only, always"
  invariant in [0004](0004-chat-allowlist-and-admin-ui.md) and
  [0013](0013-single-fly-app-shared-volume.md) — *once phase 4 ships*. Self-service
  means she must reach and authenticate to the admin UI; the login mechanism
  (AuthKit web session vs. a private overlay such as Tailscale identity) gets
  its own record before that phase. Until then the admin UI is unchanged and
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
4. Admin auth decision record, then the two admin surfaces.
5. Onboard the second user.

## Consequences

- **Deploy requirement:** `BOTSAPP_ADMIN_EMAIL` must be set on both processes
  (Fly: `[env]` or `fly secrets`) before this ships, or nothing is visible and the
  admin UI's chat edits are disabled. First start copies the old allowlist/tags
  into that user's `chat_access`; edits after that are not mirrored back.
- The admin UI still has no login and operates as the first admin until phase 4.
- Removing a user who owns an account is refused; transfer/delete the account first.
- Same-jid chats across accounts are independent rows (keyed by account).
- Operating the admin UI as the sole admin means the admin can see a paired
  account's chat list; that is the trust model until self-service pairing lands.
