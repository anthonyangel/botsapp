# 0002 — Fly.io hosting mechanics: `flyctl mcp-server` and the remote-MCP blueprint don't apply

**Status:** Settled (2026-09) — recorded so it isn't re-investigated later

## Context

While revisiting the bridge choice ([0001](0001-keep-custom-fastmcp-server.md)), the question came up: does Fly.io's `flyctl mcp-server` feature (`https://fly.io/docs/flyctl/mcp-server/`) give either bridge (WAHA or the custom FastMCP server) a special, easier path to hosting on Fly.io?

## Findings

Fetched Fly.io's own docs directly rather than relying on assumptions.

- **`fly mcp server`** is a *local* MCP server (binds to `127.0.0.1:8080` by default) that exposes **flyctl/Fly.io account management operations** (deploy, scale, status, logs) as MCP tools, so an AI assistant like Claude Code can drive *your Fly.io infrastructure*. It is the reverse of "host an MCP server on Fly" — it's an MCP server *for* flyctl itself, meant to stay local, with no public-exposure or auth story of its own.
- The full **`fly mcp` subcommand family** (`wrap`, `launch`, `add`, `proxy`, `destroy`, `inspect`, `list`, `logs`, `remove`, `server`) exists to take a **stdio-based** MCP server — the kind that only knows how to run as a local subprocess talking over stdin/stdout, the common shape for desktop MCP servers — and run/proxy it as a Fly Machine. Neither WAHA's native MCP nor the custom FastMCP server needs this: both already speak HTTP/streamable-HTTP directly (WAHA at `/mcp`; the custom server via `mcp.http_app()` on port 8000). Deploying either is just an ordinary containerized web app on Fly (`fly launch`/`fly deploy` against its Dockerfile) — no `fly mcp` tooling involved.
- Fly's **"remote MCP servers" blueprint** (`https://fly.io/docs/blueprints/remote-mcp-servers/`) describes a **multi-tenant SaaS** pattern: a router app + `fly-replay` + per-user isolated Fly Machines, so many different customers each get an isolated MCP server instance, with an optional client-side "shim" handling auth via a shared secret between shim and router. This solves "I have many users and need per-user isolation" — not "one family, one WhatsApp session." Applying it here would be pure overkill, and it says nothing about exposing a single server publicly to something like a claude.ai custom connector.
- Fly apps get a public HTTPS endpoint (`<app>.fly.dev`) with automatic TLS by default via Fly's own proxy — general Fly.io behavior, not something either of the above docs pages needed to explain.

## Conclusion

Fly.io deployment mechanics are **identical between WAHA and the custom FastMCP server** — neither benefits from or needs any of the `fly mcp *` tooling or the multi-tenant blueprint. This is why hosting mechanics played no role in the bridge decision ([0001](0001-keep-custom-fastmcp-server.md)); it was a wash.

The original exposure plan is unaffected: the bridge (WuzAPI today, or WAHA if swapped later) stays on Fly's 6PN private networking, never a public IP; `cloudflared` runs alongside it and reaches out to Cloudflare's edge; Cloudflare Access gates the public side. Fly has no equivalent to Cloudflare Access (an identity-aware proxy), so nothing in Fly's own tooling substitutes for that layer — though see [0003](0003-google-oauth-authn-authz.md) for how FastMCP's own auth now reduces how much that layer needs to do.
