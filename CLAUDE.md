# botsapp

A read-only WhatsApp MCP server for Claude, plus a network-private admin UI for
QR login and deciding which chats the MCP server can see. Built on
[WAHA](https://waha.devlike.pro/) as the WhatsApp bridge.

## Read this first

**[docs/decisions/](docs/decisions/README.md)** is the source of truth for why
things are the way they are — read it in order before assuming something is
undecided or wrong. It exists because this project's first build silently
diverged from an earlier plan with nothing recorded about why; don't repeat
that. When you make or reverse an architectural call (provider swap, auth
change, deploy topology, storage choice), **add a new numbered decision
record** rather than just changing code — supersede old records explicitly
(state which parts) rather than deleting them; the log is meant to stay an
honest history, not just the current state. `docs/decisions/README.md` has
been kept in date order, current through 0027.

There is no `docs/plans/` anymore — implementation plans are deleted once
landed, on purpose (see the convention note in [docs/decisions/README.md](docs/decisions/README.md)):
they duplicate what the decision record + code already say once the work is
done. Decision records are append-only and never deleted, even once
superseded; plans are disposable. Don't recreate a `docs/plans/` doc for
finished work, and don't be surprised that a few old decision records still
mention deleted `docs/plans/*.md` filenames by name — that's expected, not a
broken link to fix.

## Architecture

Three logical services, two Fly apps once deployed ([0013](docs/decisions/0013-single-fly-app-shared-volume.md)):

| Service | What | Reachable from |
|---|---|---|
| `waha` | WhatsApp bridge, NOWEB engine (`devlikeapro/waha:noweb-<version>` on Fly, `noweb-arm-<version>` locally — version-pinned, never `dev`; see [0023](docs/decisions/0023-waha-noweb-version-pinned-images.md)) | private only, always |
| `mcp-server` | The MCP endpoint (`botsapp.main:app`, port 8000) — the *only* thing meant to be public | public, gated by WorkOS AuthKit login |
| `admin-ui` | QR login + chat allowlist/tags (`botsapp.admin_main:app`, port 8100) — same image as `mcp-server`, different `command:` | private until the AuthKit login ([0026](docs/decisions/0026-admin-login-authkit-single-org-authn-authz-split.md), `admin/auth.py`) is verified against live WorkOS; then reachable behind that login. Needs login settings or an explicit `ADMIN_DISABLE_AUTH` |

- `mcp-server` and `admin-ui` share one SQLite file (`chat_metadata.db`, WAL
  mode) for tags/allowlist — see [0007](docs/decisions/0007-sqlite-for-metadata-store.md).
  This is why they run as **two processes inside one Fly Machine**, sharing
  one volume — a Fly Volume attaches to only one Machine at a time, so two
  separate Machines (or Fly "process groups", which are still separate
  Machines) can't share it. See [0013](docs/decisions/0013-single-fly-app-shared-volume.md).
- Nothing is visible to the MCP server until explicitly allowlisted in the
  admin UI — see [0004](docs/decisions/0004-chat-allowlist-and-admin-ui.md).
- Multi-tenancy is in progress ([0025](docs/decisions/0025-multi-tenancy-users-accounts-grants.md)):
  users/accounts/grants/per-viewer `chat_access` are live; MCP tools resolve a
  per-caller scope (`server/src/botsapp/scope.py`). The admin UI has the AuthKit
  login decided in
  [0026](docs/decisions/0026-admin-login-authkit-single-org-authn-authz-split.md)
  (built and tested against a stubbed WorkOS client; not yet verified live;
  one WorkOS org, role in WorkOS, grants and chat visibility in the DB). Its
  Session and Chats pages act on the account selected in the header (a validated
  cookie); the Accounts and Users pages manage accounts, sharing and people.
  Needs `BOTSAPP_ADMIN_EMAIL`. Each account's MCP access can use its own read-only
  WAHA session key, stored encrypted ([0027](docs/decisions/0027-scoped-waha-session-keys.md);
  needs `BOTSAPP_SECRET_KEY`; `WAHA_REQUIRE_SCOPED_KEYS` makes it mandatory). Open follow-ups (live WAHA/WorkOS verification,
  per-account WAHA keys, tool visibility from key scopes, cleanup) are GitHub
  issues, summarised under "Where things stand" in 0025.
- Every WhatsApp-mutating tool is tagged `"outbound"` and permanently blocked
  server-side via `restrict_tag` (a scope no real auth token will ever carry)
  — this server is read-only by construction, not by convention. See
  `server/CLAUDE.md` and [0001](docs/decisions/0001-keep-custom-fastmcp-server.md)/[0003](docs/decisions/0003-google-oauth-authn-authz.md).
- Auth is WorkOS AuthKit + an email allowlist stored in the shared SQLite DB
  and managed from the admin UI's Users page (not an env var — see
  [0015](docs/decisions/0015-email-allowlist-in-db.md)), no Google/Cloudflare
  in the stack — see [0005](docs/decisions/0005-authkit-oauth-provider.md), [0010](docs/decisions/0010-drop-cloudflare-two-provider-deploy.md), [0011](docs/decisions/0011-jwt-template-drops-workostokenverifier.md), [0012](docs/decisions/0012-authkitprovider-issuer-trailing-slash-bug.md) (open bug).
- Deploy target is Fly.io, plain `fly launch`/`fly deploy` against `server/Dockerfile`
  — no `fly mcp *` tooling, no multi-tenant blueprint, see [0002](docs/decisions/0002-flyio-hosting-mechanics.md).

## Working in this repo

- Everything under `server/` is a `uv`-managed Python package — see
  [server/CLAUDE.md](server/CLAUDE.md) for that layer's conventions.
- Local stack: `docker compose up -d --build` (or `task up`). Admin UI at
  `http://localhost:8100/`, MCP endpoint at `http://localhost:8000`.
- `Taskfile.yml` is the command surface — `task --list` for everything;
  `task check` mirrors what CI should run (lint + typecheck + test).
- `workos/apply.sh` drives WorkOS AuthKit app config (redirect URI, homepage
  URL) via the `workos` CLI — see the root [README.md](README.md) for the
  full sequence (project creation, Dynamic Client Registration, JWT
  Template). Don't use `workos seed`'s `config:` block — confirmed not to
  apply against this project's CLI version — and don't recreate a
  `workos/seed.yml`; botsapp has no WorkOS RBAC (permissions/roles/orgs) for
  it to seed.
- `.env.example` documents every env var this stack reads; `.env` (gitignored)
  is the real local config. Never commit real WorkOS/WAHA secrets.
