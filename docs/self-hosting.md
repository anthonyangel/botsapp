# Self-hosting botsapp

The shape this project supports for sharing is **one deployment per person**
([0036](decisions/0036-self-host-per-person-and-the-auth-seam.md)): whoever runs
the host can read the WhatsApp accounts connected to it, so run your own. WAHA's
NOWEB engine is an unofficial WhatsApp client, so accept the ban risk knowingly.

You need: a [Fly.io](https://fly.io) account and `fly` CLI, a
[WorkOS](https://workos.com) account and the `workos` CLI, and `openssl`.

## 1. WorkOS

```bash
workos auth login
workos project create myapp --yes          # or use an existing project
workos environment list                    # note the environment id and AuthKit domain
workos/setup.sh <environment-id> https://<app>.fly.dev
```

`setup.sh` registers both redirect URIs and the homepage URL, then prints the
three operations that need the WorkOS MCP server (or Dashboard): Dynamic Client
Registration, the OAuth resource `<base>/mcp`, and the JWT template that adds
`email` to the access token. Also create one organization for your users; its id
is `WORKOS_ORG_ID`. Collect the environment's client id, API key and AuthKit
domain from the Dashboard.

## 2. Deploy

```bash
export WORKOS_AUTHKIT_DOMAIN=https://<your>.authkit.app
export WORKOS_CLIENT_ID=client_...
export WORKOS_API_KEY=sk_...
export WORKOS_ORG_ID=org_...        # optional; enables invite/remove/re-role
scripts/fly-deploy.sh <app> you@example.com [region]
```

Add `DRY_RUN=1` to print the plan without changing anything. The script is
re-runnable: it creates the two apps (`<app>`, `<app>-waha`) if missing, generates
the secrets (`WAHA_API_KEY`, `BOTSAPP_SECRET_KEY`, `BOTSAPP_SESSION_SECRET`),
and **never overwrites a secret that already exists**, because replacing
`BOTSAPP_SECRET_KEY` makes the stored WAHA keys unreadable and `WAHA_API_KEY` must
match on both apps. Secrets go in over stdin, not the command line. WAHA is
deployed without a public IP.

## 3. Pair and connect

Open `https://<app>.fly.dev/admin/`, sign in, scan the QR code on the Session
page, create the account's scoped WAHA key, then allowlist chats. Add
`https://<app>.fly.dev/mcp` as a custom connector in claude.ai.

## Verify

Everything below is read-only. With the WorkOS MCP server connected, run each
query with your `environment_id`:

| Check | Query | Expect |
|---|---|---|
| Token carries email | `jwtTemplate` | content contains `"email": {{ user.email }}` |
| Claude can register | `authkitSettings` | `isAuthkitDynamicClientRegistrationEnabled: true` |
| Audience validates | `authkitOauthResources` | `<base>/mcp` listed, `isDefault: true` |
| Both logins can return | `redirectUris` | `<base>/oauth2/callback` and `<base>/admin/auth/callback` |
| Roles exist | `roles` | environment roles with slugs `admin` and `member` |
| Org and members | `organizations`, `organizationUsers` | your org; the owner has role `admin` |
| Invites work | `invitations` | an invitation reaching `Accepted` with a `member` membership |

On the Fly side, `fly secrets list -a <app>` should show `WAHA_API_KEY`,
`BOTSAPP_SECRET_KEY`, `BOTSAPP_SESSION_SECRET`, `WORKOS_AUTHKIT_DOMAIN`,
`WORKOS_CLIENT_ID`, `WORKOS_API_KEY`, `ADMIN_BASE_URL`, `BOTSAPP_ADMIN_EMAIL`
(plus `WORKOS_ORG_ID`), and `fly logs` must not show an admin-ui refusal to start
(it is backgrounded, so Fly's health checks won't tell you).

### What has been verified live

Against the maintainer's production environment (2026-11-17, read-only):
the JWT template, Dynamic Client Registration, the `/mcp` OAuth resource and both
redirect URIs are in place; roles `admin` and `member` exist; the organization has
the owner as `admin` and one invited user who accepted and holds `member`, which
exercises the invitation flow end to end. **Not exercised live:** removing a member
and changing a role from the Users page, because both mutate real users. Try them
on a throwaway invitee before relying on them.

### Hardening to consider

`allowSignUp` is `true` in that environment. Access is still gated (the MCP email
allowlist, and the admin login is restricted to the organization), but turning
sign-up off (`updateAuthkitSettings { "allowSignUp": false }`) removes needless
surface; confirm invitations still work afterwards.
