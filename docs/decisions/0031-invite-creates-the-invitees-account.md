# 0031 — Inviting a user also creates their WhatsApp account

**Status:** Decided and implemented (2026-11). Amends [0025](0025-multi-tenancy-users-accounts-grants.md) (user and account were two admin steps) and [0029](0029-session-and-accounts-pages-merged.md) (the Accounts *create* form stays, but is no longer the normal path for a new person).

## Context

Adding the second person took two admin steps: invite them on the Users page, then separately create their account (id, owner, WAHA session, scoped key) on the Session/Accounts page. Nothing useful can happen for a new member between the two: with no account they have nothing to pair and nothing to see. The second step only repeated the email already typed in the first.

## Decision

- **Inviting creates the account.** After the WorkOS invitation and the `users` row, `invite_user` creates an account owned by the invitee: DB row, WAHA session and read-only key, through the same code the Create form uses (`admin/account_setup.py`).
- **The id is derived from the email's local part** (`danni@x` → `danni`), lowercased, anything outside `[a-z0-9-]` collapsed to `-`, with `-2`, `-3`… added if taken (including `default`).
- **All or nothing.** If the account can't be made, the user row is removed and the WorkOS invitation revoked: a failed invite leaves nothing behind, as before. The reverse order is deliberate: the invitation is the only part with an outside effect (an email), and it is undone last.
- **No master key is not a failure.** As with the Create form, the account is made without a key and the flash message says so; the invite stands.
- **Admins get one too.** An invitee of either role gets an account; deleting it later is the normal Delete.
- **Consequence:** someone invited by mistake owns an account, and Users → Remove refuses until it is deleted (existing rule: [0026](0026-admin-login-authkit-single-org-authn-authz-split.md)).
- The Accounts *Create* form stays for extra accounts (one person, several numbers) and accounts that predate this.

## Alternatives considered

| Option | Why not |
|---|---|
| Create the account when the invitee first signs in | Puts WAHA session and key creation in the login path; a failure there blocks sign-in. |
| A "create account" checkbox on the invite form | A choice nobody wants to make; the member role with no account is not a useful state. |
