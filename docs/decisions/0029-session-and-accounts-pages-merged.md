# 0029 — Session and Accounts pages merged; pairing is per account, not per "selected account"

**Status:** Decided and implemented (2026-10). Amends [0025](0025-multi-tenancy-users-accounts-grants.md) and [0026](0026-admin-login-authkit-single-org-authn-authz-split.md): the admin UI's `"owner"` route-policy tier is gone, and the Session page no longer follows the selected-account cookie.

## Context

After 0025/0026 the admin UI had two pages about the same thing. **Accounts** listed every WhatsApp account a person owned or was granted, with its connection state as a one-word label. **Session** showed the QR / Connect / Disconnect / Force re-pair controls, but only for whichever single account the header's account switcher had selected. To pair a second account you switched to it, then went to Session. Most people have one account, so this was a detour for the common case, and for the planned supervision case (one login, several accounts) it was a trap.

The Session page was also gated by its own policy tier, `"owner"`: the caller had to own (or, as admin, be able to select) the *selected* account just to view it. The per-account ownership check already lived in each mutating Accounts handler (`_manageable`), so the page-level tier was a second copy of the same rule.

## Decision

- **One page, `/`, titled Session.** It lists every account the caller owns (every account, for an admin) as a card with live pairing controls inline, followed by the accounts shared with them. The old `/accounts` URL redirects to `/` (308), as `/access` does to `/users`.
- **Pairing actions are per account, in the path:** `/accounts/{id}/session/connect|disconnect|reset` (POST) and `/accounts/{id}/session/status.json` (GET, polled while a QR is showing). They go through the same `_manageable()` check as grant/revoke/delete/key: owner or admin, and "Unknown account." for anything else, indistinguishable from an account that doesn't exist. The previous 403 ("Only the account's owner can manage its session") confirmed that the account existed.
- **The force-re-pair route is `/session/reset`, not `/session/delete`.** `_policy` treats any `/accounts/…/delete` as the admin-only "delete the whole account" route, so `/session/delete` would have silently become admin-only.
- **The `"owner"` policy tier is removed.** `/` and `/accounts*` are the "user" tier (any known user); mutating handlers check ownership themselves. The `"admin"` and `"member"` tiers are unchanged.
- **The selected-account cookie now matters only for Chats**, the one page whose state is genuinely per (account, viewer). The header switcher stays for that.
- **Each card polls on its own.** The QR poll script is wrapped in a closure keyed by account id, so two accounts mid-pairing on one page don't share a timer or image element.
- **Users stays its own page.** Identity (who can sign in, and as what role) and WhatsApp accounts are different things with a many-to-many relationship between them (ownership plus grants); only the *account* views were duplicates.

## Alternatives considered

| Option | Why not |
|---|---|
| Keep both pages, add links between them | Leaves the select-then-pair detour and the duplicated ownership rule. |
| Merge Accounts into Session but keep pairing on the *selected* account | Smaller diff, but keeps the detour for the second account and the `"owner"` tier. |
| Merge Users into the same page too | Conflates login identity with WhatsApp pairing; a member who only holds a grant has a user row and no account of their own. |

## Consequences

- Fewer concepts: no page needs "the selected account" except Chats.
- Pairing a shared account's phone is still owner/admin only; a person who only holds a grant sees the account under "Shared with you" with no controls.
- Bookmarks to `/session/*` stop working (those routes are gone); `/accounts` keeps working via the redirect.
- `test_admin_prefix.py`, `test_admin.py`, `test_admin_accounts.py`, `test_admin_sharing.py` and `test_admin_auth.py` were updated for the new URLs and the "Unknown account." denial.
