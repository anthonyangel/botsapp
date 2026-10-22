# 0026 — Admin login via AuthKit, one WorkOS organization, and the authn/authz split

**Status:** Decided (2026-10). Login implemented in #48 against a stubbed WorkOS client (`admin/auth.py`); **the real WorkOS code exchange has not been exercised against a live environment.** Users page implemented in #49 (below). Accounts page implemented in #50 and the self-service Chats behaviour in #51 (both below). Still to do: (self-service), #52 (scoped WAHA keys).

## Context

[0025](0025-multi-tenancy-users-accounts-grants.md) made botsapp multi-user: users own WhatsApp accounts, accounts can be granted to other users, and chat visibility is per (account, viewer). The MCP side is done and resolves a scope per call from the caller's email. The admin UI is the remaining single-tenant piece: it has no login and is reachable only on the private network ([0004](0004-chat-allowlist-and-admin-ui.md), [0013](0013-single-fly-app-shared-volume.md)), so the second person cannot pair her own number or choose her own chats without the owner operating it for her.

Two questions follow: how do people log in to the admin UI, and where does each kind of permission live. The earlier stance was "AuthKit authenticates only; everything else is the app's DB, and WorkOS RBAC is out of scope" (root `CLAUDE.md`, [0015](0015-email-allowlist-in-db.md)). That stance is partly reversed here.

What WorkOS can express was checked against its own docs ([FGA](https://workos.com/docs/fga/index.md), [RBAC/FGA blog](https://workos.com/blog/rbac-fga-multi-tenant-b2b)): organization-level roles and permissions are carried in the access token; per-resource grants are not, and WorkOS itself recommends API checks for those because of token size, freshness and verification cost. Roles attach to an organization membership, and a session is scoped to one organization at a time.

## Decision

**1. The admin UI gets a login, through AuthKit.** The browser flow uses AuthKit as the OpenID Connect provider (`/login`, callback, signed HTTP-only session cookie holding only the email). The MCP server keeps using AuthKit as an OAuth resource server, unchanged. The admin app is therefore publicly reachable, but only behind that login.

**2. One WorkOS organization ("botsapp").** Members are botsapp's users, each with a role of `admin` or `member`. Accounts are *not* organizations, so a user with several WhatsApp accounts needs no org switching and every token carries the same `org_id` and a single role.

**3. The split — who decides what:**

| Concern | Lives in | Why |
|---|---|---|
| Identity (is this really that email) | AuthKit | Unchanged. |
| Membership: is this person in the system at all | WorkOS org membership, plus a `users` row | Admin invites/removes through the WorkOS API (WorkOS sends the invitation email). The `users` row is created only on invite, so a stranger who signs up at AuthKit gets nothing. |
| `admin` vs `member` | WorkOS role, surfaced in the JWT via the JWT template | Coarse, org-wide, fits a token. |
| Which accounts a user may use (`grants`) | App DB | Per-resource and changes often; checked live on every call, so revocation is instant. |
| Which chats a viewer sees, and their tags (`chat_access`) | App DB | High-cardinality, per viewer. |
| Never send | Code (`outbound` tag, `restrict_tag`) | Unchanged and not configurable. |

**4. MCP tokens carry identity only.** Grants are resolved server side per call (`scope.resolve_scope`), so the MCP path needs no role claim. The role claim matters only to the admin UI, which can read it from its own session flow. The JWT template change is therefore optional for MCP.

**5. Route rules in the admin UI.** Any signed-in user reaches `/me/*` (their own accounts only: pair a session, tags, visible chats, grant or revoke their own account to existing users). Only `admin` reaches `/admin/*` (users, accounts). Every data access goes through the same scoped `DatabaseManager` methods the MCP tools use, so authorization stays in one place rather than in route handlers.

**6. Failure and consistency rules.**
- Invite/remove is one operation across WorkOS and the DB: a failed WorkOS call leaves no `users` row, and a failed DB write leaves no dangling invitation.
- Removing or demoting someone takes effect on their next token. For sensitive admin actions the role is re-checked live through the WorkOS API rather than trusted from the cookie or token.
- If the WorkOS API is down, existing access keeps working; invite, remove and role changes fail with a clear message.
- `users.is_admin` either becomes a cached mirror of the WorkOS role or is dropped; decided in #49 (see Consequences).

## Supersedes / amends

- **Supersedes** the "admin-ui has no login of its own, private-only, always" invariant in [0004](0004-chat-allowlist-and-admin-ui.md) and [0013](0013-single-fly-app-shared-volume.md), and completes the item 0025 deferred ("the login mechanism gets its own record").
- **Amends** the root `CLAUDE.md` line that botsapp has "no WorkOS RBAC (permissions/roles/orgs)": from this record on there is one org and an `admin`/`member` role. Fine-grained permissions, multiple orgs and FGA remain out of scope.
- **Narrows** [0015](0015-email-allowlist-in-db.md): the DB `users` table is still the login gate on the MCP side, but membership is now also reflected in WorkOS.
- Leaves [0005](0005-authkit-oauth-provider.md), [0011](0011-jwt-template-drops-workostokenverifier.md) and the open bug [0012](0012-authkitprovider-issuer-trailing-slash-bug.md) untouched for the MCP path.

## Alternatives considered

| Option | Why not |
|---|---|
| Keep authorization entirely in the app DB; AuthKit only authenticates | Works, and is what exists today. Rejected as the *whole* answer because admin/member and invitations are naturally WorkOS features (invitation emails, role in the token), and the admin page can drive them by API. Grants and chat visibility stay here for the reasons above. |
| One WorkOS organization per WhatsApp account | A session is scoped to one org, so a user with two accounts would need org switching; conflicts with `list_accounts` and the `account` argument. |
| WorkOS FGA for per-account grants | Every check is an API call, adds a dependency to the hot path, and buys nothing at this scale. |
| Per-account permissions or roles encoded in the token | Unclear whether a membership can hold several roles; and per-resource state in a token reintroduces revocation lag. |
| Tailscale (or another private overlay) identity instead of AuthKit | A second identity system for one extra user; the owner already chose AuthKit. |

## Consequences

- Revocation of *account grants and chat visibility* is instant (DB). Revocation of *membership and role* lags by token lifetime, except where the admin UI re-checks live.
- The app and WorkOS can disagree about who exists. Deleting someone from the app leaves them able to sign in at AuthKit but with no access; the invite/remove operation must keep the two in step.
- The admin UI gains a public attack surface. Required with it: CSRF protection for the existing POST forms, a signed `SameSite` cookie, and per-request role lookup rather than trusting the cookie.
- New secrets for the admin process only: a WorkOS API key and client credentials. The org id and extra redirect URI go through `workos/apply.sh`.
- `docker-compose.yml`, `server/fly.toml`, the README and the root `CLAUDE.md` architecture table ("private only, always — no login of its own") must be updated by the implementing PR, not now, since nothing here is live yet.
- Per-account WAHA session keys ([#52](https://github.com/anthonyangel/botsapp/issues/52)) are a separate decision (stored encrypted in the DB under `BOTSAPP_SECRET_KEY`, not in Fly secrets, because setting a Fly secret restarts the machine). Record it as 0027 when that work starts.

## As built (#48)

- `AuthKitClient` protocol with `WorkOSAuthKitClient` (authorize redirect + code exchange) built on the official `workos` Python SDK's async client, and a test stub. Optional `WORKOS_ORG_ID` restricts sign-in to the one org.
- Session cookie signed with `itsdangerous` (a per-purpose salt, so the login-state cookie can never be replayed as a session), `SameSite=Lax`, HTTP-only, `Secure`-on-https, holding only the email; 8 h lifetime. Starlette's `SessionMiddleware` was not used because it needs the secret when the app is constructed, whereas ours comes from config at startup. Role and `users` row are looked up per request, so removal or demotion applies on the next click even before the cookie expires.
- State cookie + nonce on `/login` → `/auth/callback`; `next` is restricted to same-site relative paths.
- CSRF: `SameSite=Lax` plus a required matching `Origin`/`Referer` on every state-changing request. The existing hand-rolled forms carry no token.
- Policy: `/users` (and the old `/access`, which redirects) admins only; `/` and `/session/*` the account's owner or an admin; `/chats*` anyone holding the account. Until the per-account pages (#51) all of these act on the `default` account.
- Fails closed: unconfigured login stops the admin process at startup. Explicit opt-outs: `LOCAL_DEV_DISABLE_AUTH` (shared with the MCP server, local dev) and `ADMIN_DISABLE_AUTH` (admin UI only, for a deployment still reachable by nobody but the owner). The Fly entrypoint backgrounds the admin process, so a refusal to start is silent to health checks; check the logs after deploying.
- Checked against the SDK's models (not a live call): the authenticate response carries `user.email`, `user.email_verified` and `organization_id`, and `get_authorization_url` accepts `organization_id`. Still not exercised against a live WorkOS environment.

## As built (#49, Users page)

- `/users` replaces the Access tab (`/access` redirects). Invite, remove and change role act through a `UserDirectory` protocol (`WorkOSUserDirectory` for real, over the `workos` SDK's invitation, user and `organization_membership` calls; a stub in tests; not yet run against a live environment). Needs `WORKOS_ORG_ID` for the WorkOS-connected mode; without it the page edits only the local `users` table and says so.
- **`users.is_admin` stays, as a mirror of the WorkOS role** (resolving the open question above): the per-request check in the middleware must not call WorkOS. It is written by this page after WorkOS succeeds, and refreshed from WorkOS at every sign-in. Sign-in never demotes `BOTSAPP_ADMIN_EMAIL`, so a missing WorkOS role can't lock the owner out.
- Ordering: invite = WorkOS then row (invitation revoked if the row can't be saved); remove = pre-checks (owns an account, is yourself) then WorkOS then row (idempotent, so retry heals a half-done removal); role = WorkOS then mirror (WorkOS role restored if the mirror write fails).
- The acting admin's role is re-checked live against WorkOS on every change. The bootstrap admin is accepted when WorkOS has no role for them yet. There is no "last administrator" guard because the caller must be an admin and cannot target themselves.
- Users are created on invite, before the invitation is accepted; they can't sign in until they accept. A pending invitation is not shown separately yet.

## As built (#50, Accounts page)

- `/accounts` lists the accounts you own (every account for an admin) with their WAHA session status and who they are shared with, plus accounts shared with you. Admins create and delete accounts; an owner or an admin grants and revokes. Revoking also deletes that viewer's chat access, so a re-grant starts empty. Asking about an account you don't own gets the same "Unknown account" as one that doesn't exist.
- **Creating** adds the row, then creates an empty (stopped, NOWEB-store-enabled) WAHA session, and removes the row again if WAHA fails. **Deleting** removes the WAHA session first (a missing one is fine), then the row with its grants and chat access. The `default` account can't be deleted. Both re-check the acting admin's role live like the Users page.
- **Account selection.** The Session and Chats pages now act on the account in the `botsapp_account` cookie, validated on every request against what the user may select (their own and shared accounts; every account for an admin). With no valid cookie the choice falls back to an account the user *owns* (the default if it's theirs), then one shared with them. The header gets an account switcher when there is more than one choice, and hides Users for non-admins.
- An admin can select any account and so pair or reset its session, but holds no grant and therefore sees no chats there; the Chats pages need a real grant.
- Session creation uses `POST /api/sessions` with `start: false`, and pairing later relies on `restart` of a STOPPED session. Neither multi-session creation nor that restart path has been run against a live WAHA (#47).
- The select link is a GET that sets a preference cookie; it grants nothing (every request revalidates), so a cross-site trigger can only change which of the user's own accounts is shown.

## As built (#51, self-service Chats)

There is no separate `/me/*` tree: the existing Session and Chats pages, acting on the selected account (#50), *are* the self-service pages. #51 made the Chats page role-aware, which closed a hole:

- **The hole.** Before #51 the Chats page treated anyone holding the selected account like its owner: a person an account was *shared with* saw every chat on it and could POST `/chats/<jid>/allow` to allow any of them for themself, which the MCP server would then honour (it reads `chat_access`). Account sharing was only reachable via the database until #50 added the UI, so nothing shipped to production exposed it, but #48–#50 on this branch had it.
- **Owner** (`accounts.owner_email`): sees every chat, toggles "Visible to MCP" for their own view, and picks any granted viewer (`?viewer=`) to choose what *that person* sees. Only the owner can toggle, for any viewer, and only for people who actually hold the account.
- **Shared viewer:** sees only the chats made visible to them, not the others or how many there are; cannot toggle (not even with `?viewer=` set to themself); can label those chats for themself; cannot label a chat that wasn't shared with them; cannot read or change anyone else's view. A scope or page that has been loaded before a revoke stops yielding chats at once because every DB read re-checks the grant.
- **Tags are each person's own.** The owner choosing someone's visibility does not edit that person's tags.
- **Admins** have no special rights on the Chats pages: without a grant they get a 403, as before.
- Landing: someone whose only account is shared with them is sent from `/` to `/accounts` instead of a 403, and the Session link is hidden unless they own the selected account (or are an admin). Pairing, disconnect and reset stay with the owner.
- The Accounts page links each granted person to "Choose chats", which selects the account and opens that person's view.

## Open check before implementing

Confirm that AuthKit places the single org and role in the tokens it issues automatically when a user has exactly one membership. That is expected for the browser login but is **not yet verified for MCP OAuth tokens**. Log in once and inspect the claims. It affects only whether the role can also be read from MCP tokens, which this design does not rely on.
