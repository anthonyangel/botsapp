# botsapp

A read-only WhatsApp MCP server for Claude — plus a network-private admin UI
for logging in and deciding which chats it can see. Built on
[WAHA](https://waha.devlike.pro/) — see [docs/decisions/](docs/decisions/README.md)
for the reasoning behind every architectural choice here.

## Services

| Service | Purpose | Network |
|---|---|---|
| `waha` | Talks to WhatsApp | private |
| `mcp-server` | The MCP endpoint — the only thing meant to be reachable beyond this machine, gated by AuthKit login (the only port exposed publicly once deployed — see [docs/decisions/0013](docs/decisions/0013-single-fly-app-shared-volume.md)) | loopback locally |
| `admin-ui` | QR login, chat allowlist, tags — **no auth of its own**, network-privacy is the only gate | loopback locally |

`mcp-server` and `admin-ui` run from the same image (`server/Dockerfile`, different `command:`) and share one small SQLite file for `chat_metadata` (tags/allowlist) rather than a database service — see [docs/decisions/0007](docs/decisions/0007-sqlite-for-metadata-store.md). On Fly.io they run as two processes inside one Machine, sharing one volume, precisely to keep that shared file working (Fly volumes attach to only one Machine at a time) — see [docs/decisions/0013](docs/decisions/0013-single-fly-app-shared-volume.md). `waha` deploys as its own separate Fly app.

## Run it locally

```bash
docker compose up -d --build
```

### Log in to WhatsApp

Open the admin UI at **http://localhost:8100/** and scan the QR code with
WhatsApp on your phone (WhatsApp → Linked Devices → Link a Device).

### Choose which chats the MCP server can see

Open **http://localhost:8100/chats** — every chat/group WAHA knows about
is listed, hidden from the MCP server by default. Check "Visible to MCP" on
the ones you actually want Claude to read, and add tags if you like.
Nothing is visible to the MCP server until you do this — see
[docs/decisions/0004](docs/decisions/0004-chat-allowlist-and-admin-ui.md).

### WorkOS AuthKit setup

The MCP server itself (**http://localhost:8000**) normally requires AuthKit
login — only emails on the allowlist at **http://localhost:8100/access**
(the admin UI's Access tab, backed by the same shared DB as the chat
allowlist — see [docs/decisions/0015](docs/decisions/0015-email-allowlist-in-db.md))
can call any tool. Nobody can use the MCP server until you add at least one
email there. To actually log in via claude.ai:

Driven with the [`workos` CLI](https://github.com/workos/cli) rather than
clicking through the Dashboard by hand:

1. `workos auth login` (browser OAuth device flow), then
   `workos project create botsapp --yes` — a dedicated project (not the
   account's auto-created default) with `Staging`/`Production` environments,
   each with its own AuthKit domain. `workos project list --json` shows
   both environments' domains (`authkitDomains[].domain`) and client ids
   without opening the Dashboard. `workos environment use <id>` switches
   which one subsequent commands target — **but re-check with
   `workos whoami --json` before trusting it**, it was observed to lag by
   one call in CLI v0.22.0.
2. `workos/apply.sh staging` — sets the redirect URI + homepage URL on the
   Staging environment via `workos config redirect add`/
   `workos config homepage-url set` with an explicit `--environment-id`.
   **Not** `workos seed` — its `config:` YAML block was tested against this
   project and never actually applied (v0.22.0). botsapp doesn't use WorkOS
   RBAC (permissions/roles/organizations) either, so there's nothing else
   for `workos seed` to do here.
3. Two things the `workos` **CLI** has no command for at all (confirmed
   against `workos --help --json`) — but both are reachable through the
   [WorkOS MCP server](https://workos.com/docs/mcp) (`workos mcp` installs
   it; authorize it with `/mcp` in an interactive Claude Code session), via
   its `mutate` tool once `list_operations` surfaces the operation name.
   Dashboard is the fallback if you don't have the MCP server connected:
   - Enable **Dynamic Client Registration** —
     `updateAuthkitSettings { isAuthkitDynamicClientRegistrationEnabled: true }`
     — lets claude.ai (or any MCP client) register itself against AuthKit
     with no manual app-credential exchange. Verify with the `authkitSettings`
     query — it defaults to `false`.
   - Rename the application (defaults to "\<your name\>'s Application") —
     `updateAuthkitApplication { applicationId, name: "botsapp" }`.
4. Put the Staging environment's AuthKit domain (from step 1) into `.env` as
   `WORKOS_AUTHKIT_DOMAIN`, and restart `mcp-server`.

Google sign-in isn't required but can be added later purely via the
WorkOS Dashboard (Social Login → Google) with no code change — see
[docs/decisions/0005](docs/decisions/0005-authkit-oauth-provider.md).

**Local dev without AuthKit**: `.env` ships with
`LOCAL_DEV_DISABLE_AUTH=true`, which skips the login requirement entirely —
no WorkOS project needed just to run the stack and poke at `/mcp` directly.
Outbound/write tools stay blocked regardless (see
[docs/decisions/0003](docs/decisions/0003-google-oauth-authn-authz.md)) —
this only removes the login step. **Unset it (or set it to `false`)
before this server is reachable by anyone but you** — with it on, the
email allowlist isn't enforced and anyone who can reach the server can
read every allowed chat.

### Running the test suite / linting

```bash
cd server
uv sync --all-extras
uv run pytest
uv run ruff check src tests
uv run ty check
```

## Never expose `admin-ui` or `waha` publicly

Both `docker-compose.yml` (loopback-only ports) and the eventual Fly.io
deployment are built around this: `waha` and `admin-ui` are never given a
public Fly service — only `mcp-server` is reachable beyond this machine,
gated by AuthKit login (see [docs/decisions/0010](docs/decisions/0010-drop-cloudflare-two-provider-deploy.md)
and [0013](docs/decisions/0013-single-fly-app-shared-volume.md) for how
`admin-ui` stays private even though it shares a Machine with `mcp-server` —
simply no public service block for its port).
`admin-ui` has no login screen by design — see
[docs/decisions/0004](docs/decisions/0004-chat-allowlist-and-admin-ui.md).
