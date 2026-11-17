# 0036 — Self-host per person: single-tenant is the portable shape; WorkOS stays the default login behind a narrow seam

*Status: accepted. Extends [0025](0025-multi-tenancy-users-accounts-grants.md) and
[0026](0026-admin-login-authkit-single-org-authn-authz-split.md); changes no
multi-tenant behaviour.*

## Context

The question was how to share botsapp more widely. Productising it as a hosted
service is ruled out by an unfixable trust problem: WAHA holds each account's
WhatsApp session keys and the server decrypts messages to serve them, so whoever
operates the host can read every connected account, whatever the app-level
grants say. Encrypting at rest with a user-held key would not help, because the
operator still controls the code. Separately, WAHA's NOWEB engine is an
unofficial WhatsApp client, so running it as a service for others would put the
operator in the position of risking other people's accounts.

## Decision

1. **The shareable unit is the software, run by each person for themselves.**
   Multi-tenancy (0025) stays, for a circle that already trusts the operator
   (family, friends). It is not a path to a hosted product.
2. **WorkOS AuthKit remains the default login.** It gives the public boundary a
   maintained identity provider rather than hand-rolled auth, and each
   self-hoster uses their own WorkOS project.
3. **The MCP login provider is a narrow seam.** `app._build_auth()` is the only
   place a provider is constructed, and downstream code reads only the token's
   `email` claim (`scope.caller_email`, `allowed_family_email`). Another fastmcp
   auth provider that supplies a verified `email` could replace it. The admin
   UI already has the equivalent seam (`AuthKitClient`, `UserDirectory`
   protocols). No second provider is built now.
4. **`LOCAL_DEV_DISABLE_AUTH` cannot reach the public deploy.** `server/start.sh`,
   the Fly entrypoint, refuses to start when it is set. Compose and local runs
   are unaffected (they already publish only on 127.0.0.1).

## Coupling audit (what a swap would touch)

- MCP: `AuthKitProvider` in `app.py`, plus `_authkit_issuer_workaround.py`
  (0012), which exists only for that provider.
- Admin UI: `WorkOSAuthKitClient` and the `workos` SDK in `admin/auth.py`;
  `WorkOSUserDirectory` in `admin/directory.py` (optional: without
  `WORKOS_ORG_ID` the Users page manages local rows only).
- Authorization (allowlist, grants, chat visibility) already lives in SQLite
  and does not depend on WorkOS.

## Follow-ups

Done in the same PR (see [docs/self-hosting.md](../self-hosting.md)):

- `scripts/fly-deploy.sh`: one-command, re-runnable Fly deploy that never
  overwrites existing secrets.
- `workos/setup.sh`: generic WorkOS setup (the CLI part, idempotent) plus
  ready-to-paste payloads for the MCP-only operations.
- Live read-only verification of the WorkOS config, roles, and invitation flow
  against the maintainer's production environment. Member removal and role
  change from the Users page remain unexercised live (they mutate real users).

Still open:

- Single-tenant bootstrap mode that hides users, grants and invitations.
- Optional: a second auth provider, only if someone needs one.
