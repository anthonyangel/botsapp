# 0028 — Admin UI goes public on one port, via Caddy and an `/admin` prefix

**Status:** Decided and implemented (2026-10). Supersedes the "admin-ui never public" framing in [0004](0004-chat-allowlist-and-admin-ui.md), [0013](0013-single-fly-app-shared-volume.md) and [0026](0026-admin-login-authkit-single-org-authn-authz-split.md), now that [0026](0026-admin-login-authkit-single-org-authn-authz-split.md)'s AuthKit login is the thing standing between a request and the admin pages, not Fly's network ACLs.

## Context

0026 built real login for the admin UI, but it shipped still reachable only over Fly's private 6PN networking (`fly proxy`) — `fly.toml` only ever declared mcp-server's port 8000 as a public `[http_service]`. The goal was always to make the admin UI reachable the normal way once login was verified; doing that surfaced two problems:

- **No path to a second public port without a port number in the URL.** Fly's own dynamic request routing (`fly-replay`) routes between apps, regions or Machines — never between two internal ports of one app — and splitting admin-ui into its own Fly app to use it would break 0013's reason for keeping both processes on one Machine (a Fly Volume attaches to only one Machine; the shared SQLite file needs them together). Fly's own docs land on the same conclusion for this exact shape: "a small in-app reverse proxy remains the option."
- **The redirect URI I'd registered with WorkOS pointed at the wrong process.** `https://botsapp.fly.dev/auth/callback` resolves publicly to mcp-server (the only thing `[http_service]` ever reached), which has no such route — AuthKit's redirect back from login would have 404'd regardless of how admin-ui was tested locally.

## Decision

- **Caddy fronts both processes on Fly's one public port (8000/443).** A static binary copied from the official `caddy` image into the existing Python Dockerfile (`COPY --from=caddy:... /usr/bin/caddy ...`), not installed via a package manager. `server/Caddyfile` routes `/admin` and `/admin/*` to admin-ui, everything else to mcp-server — a few lines, not a hand-rolled ASGI proxy, so streaming/chunked responses (the MCP streamable-HTTP transport needs these) are handled by something that already gets this right, not by new code written for this.
- **The `/admin` prefix is baked into admin-ui's own routes, redirects and templates — not stripped by Caddy.** Caddy forwards the path unstripped (`handle /admin /admin/*`, no `handle_path`), because admin-ui's handlers hand-build absolute paths everywhere (redirects, `href`s, form `action`s, a `next=` query param) rather than going through Starlette's `url_for`; a prefix-stripping proxy would leave every one of those pointing at the wrong place once the browser follows them. `admin/prefix.py` reads `ADMIN_URL_PREFIX` once at import (`""` unless set) and every admin module imports its `p()` helper to build paths. Unset (local dev, `fly proxy` direct access) it's a no-op — nothing changes there.
- **mcp-server and admin-ui keep their process isolation from [0027](0027-scoped-waha-session-keys.md).** Collapsing them into one Python process (and therefore one environment) was considered and rejected outright: 0027's strict mode depends on `start.sh` withholding `WAHA_API_KEY` from the MCP process's environment specifically, so a bug or compromise there can't reach the shared admin key. One process means one environment — no way to keep that wall. mcp-server instead moves off its public bind (`0.0.0.0:8000` → `127.0.0.1:8001`, now only ever reached by Caddy, in-container) while admin-ui's bind is untouched (`::`, port 8100 — still also reachable directly via `fly proxy`, unaffected).
- **Reproduced the restart-on-crash guarantee Caddy's arrival would otherwise remove.** Before this, mcp-server was `exec`'d as PID 1, so Fly's health checks and restart policy watched it directly. Caddy becomes PID 1 now and would happily keep answering health checks while proxying into a dead backend. `start.sh` adds a small monitor loop: if either backgrounded process (admin-ui or mcp-server) exits, it sends `SIGTERM` to PID 1 (Caddy), bringing the container down so Fly restarts it — same effective guarantee, explicit instead of incidental.
- **`ADMIN_DISABLE_AUTH` changes meaning.** Before, it covered "the network already keeps everyone out." With `/admin` public, leaving it set means "anyone who can reach `/admin` acts as `BOTSAPP_ADMIN_EMAIL` with no login at all" — it's now an emergency fallback for if login itself breaks post-deploy, not the default posture.

## Alternatives considered

| Option | Why not |
|---|---|
| `fly-replay` to a second Fly app for admin-ui | Breaks 0013's shared-volume premise (one Volume, one Machine); also an extra network hop per request. |
| Merge mcp-server and admin-ui into one ASGI process, mounted under `/admin` | Destroys 0027's `WAHA_API_KEY` isolation between the two — one process, one environment, no way to withhold the key from just the MCP code paths. |
| Hand-rolled ASGI reverse proxy (Starlette + httpx) in-process | More code to get right for a solved problem — streaming/chunked responses, keep-alives, WebSocket upgrades are exactly where a custom proxy tends to have subtle bugs; a dedicated binary already handles these. |
| Second Fly `[[services]]` block exposing 8100 on its own port | Works, but puts a port number in the public URL — ruled out because the goal was "both reachable on 443, no port." |
| Strip the `/admin` prefix at the proxy (so admin-ui stays prefix-agnostic) | Admin-ui's hand-rolled absolute paths (redirects, hrefs, forms) would still render without the prefix, so the browser's next request for any of them lands back at mcp-server (wrong process) instead of admin-ui. |

## Consequences

- `ADMIN_BASE_URL` stays the bare public domain (`https://botsapp.fly.dev`); the `/admin` prefix is appended separately when building the AuthKit redirect URI, so it doesn't need re-deriving by hand. The WorkOS Production redirect URI is `https://botsapp.fly.dev/admin/auth/callback`, replacing the unreachable one registered against the bare domain before this record.
- Local dev (docker-compose, `fly proxy`) is unchanged: `ADMIN_URL_PREFIX` stays unset there, so admin-ui keeps answering at its own root exactly as before.
- `server/tests/test_admin_prefix.py` exercises the prefixed case in a subprocess (every admin module bakes the prefix in at import time, so the rest of the suite — which shares one process — can't just monkeypatch the env var and re-check).
- The new `caddy` binary is pinned by tag and digest in the Dockerfile, matching how every other image in this stack is pinned.
