# 0001 — Keep the custom FastMCP server; treat the WhatsApp bridge as swappable

**Status:** Decided (2026-09)

## Context

The original plan for this project (established before this build existed) was:
- Bridge: **WAHA**, self-hosted, kept network-private.
- MCP layer: WAHA's *native* MCP feature (`waha.devlike.pro/docs/apps/mcp`), scoped `read:true, send:false` via a per-session "MCP App" key — no custom server code.
- Exposure: Cloudflare Tunnel (private origin) + Cloudflare Access "MCP server portal" in front, handing WAHA's API key to the portal centrally.
- Hosting: Fly.io, WAHA on 6PN private networking, never a public IP.

Reviewing the existing build at `~/github/whatsapp-mcp` found something different had actually been built:
- Bridge: **WuzAPI** (whatsmeow-based), not WAHA.
- MCP layer: a hand-rolled Python **FastMCP** server (`whatsapp-mcp-server`) — its own `tools.py`, a `DatabaseManager` reading WuzAPI's internal Postgres tables (`message_history`, `whatsmeow_contacts`) directly, and a thin REST client (`wuzapi_client.py`) for writes/contacts/groups.
- 3 containers (Postgres + WuzAPI + the custom mcp-server) instead of WAHA's 1.
- No enforced read-only boundary: ~15 tools mutate WhatsApp state (send/edit/delete messages, manage groups, disconnect the session) with no server-side gate — the opposite of the `read:true, send:false` goal.
- No authentication on the MCP server (`mcp.http_app()` had no `auth=`).
- No git repository, stale RabbitMQ/MinIO remnants from an abandoned earlier iteration, `docker-compose.yml` publishing ports to all interfaces.
- 104 passing unit tests; last actually run ~6 months prior (per stopped Docker containers/volumes).

Nothing in the repo recorded *why* the bridge and architecture changed from the original plan — this decision log exists specifically so that doesn't happen again.

## Options considered

**A. Switch to WAHA**, matching the original plan exactly. Retire the WuzAPI/Postgres/custom-server stack.
- ✅ Native per-key scopes (`read`/`send`/`control`/`setting`/`app`/`delete`) enforced by WAHA itself, not by code review.
- ✅ 1 container instead of 3.
- ✅ Matches the original Cloudflare Access "hand WAHA's API key to it centrally" plan almost verbatim.
- ❌ Discards a working, tested custom server (104 passing tests) and its nice-to-haves (chat tagging/notes, full-text search).
- ❌ Auth is a single static API key — no per-person login, no revoke-a-device story.

**B. Patch the WuzAPI build**: strip write tools, add auth, fix `docker-compose.yml` networking.
- ✅ Keeps the tested code.
- ❌ At the time this was first proposed, FastMCP's server had no clear path to real authentication (no `auth=` configured) — this looked like the weaker option until FastMCP's own auth/authz docs were read in full (see [0003](0003-google-oauth-authn-authz.md)).

**C. Keep the custom FastMCP server, treat WuzAPI as one swappable implementation behind it** (chosen).
- Once FastMCP's own authorization primitives were read from source (`restrict_tag`, `AuthMiddleware`, `get_access_token`) and its `GoogleProvider` for authentication, the scoping and auth gaps that favored WAHA turned out to be closable *within* FastMCP — arguably more granularly (per-tool tag-based restriction with explicit `AuthorizationError`s, real OAuth login, vs. a static shared key). Full detail in [0003](0003-google-oauth-authn-authz.md).
- Fly.io hosting mechanics turned out to be identical either way — see [0002](0002-flyio-hosting-mechanics.md) — so hosting didn't favor either option.
- The remaining difference is real but smaller: 3 containers vs. 1, and `db.py` coupling directly to WuzAPI/whatsmeow's undocumented internal Postgres schema.

## Decision

Keep the FastMCP server. WuzAPI is *a* bridge implementation behind it, not a permanent architectural commitment — explicitly including WAHA as a live fallback option if WuzAPI needs replacing later (a whatsmeow fork going stale, a licensing change, whatever).

To make that swappability real rather than aspirational, extract a `MessageStore` interface so the one part of the design that's actually WuzAPI-specific (direct SQL reads against whatsmeow's internal tables) sits behind an interface a future WAHA-backed implementation could satisfy differently (via WAHA's own REST API instead of Postgres) — `tools.py` wouldn't need to change. (Implemented via `docs/plans/message-store-auth-cleanup.md`, an implementation plan removed from the repo once it landed — see `message_store.py` for the resulting interface.)

## Consequences

- Accepted: 3 containers instead of 1; ongoing (now-abstracted) coupling to WuzAPI's internal schema; more custom code to maintain than "point Claude at WAHA's `/mcp`."
- Gained: keeps working, tested code; a real per-tool authorization boundary and OAuth login (see 0003) rather than a static key; room for the chat-tagging/full-text-search features WuzAPI's build already added.
- If WuzAPI ever needs replacing, the cost is bounded to writing a new `MessageStore` implementation (and swapping `wuzapi_client.py`-equivalent for writes) — not rebuilding the MCP layer, tools, tests, or auth wiring.
