# 0004 — Enforced chat allowlist + a separate, network-private admin UI

**Status:** Decided (2026-09)

## Context

Every prior version of this plan (and the existing build) relied on an out-of-band whitelist: a dedicated WhatsApp session/number joined only to the chats you want visible. Nothing in the code has ever enforced that — the MCP tools read/return whatever chats and groups the connected account happens to belong to. That's a real gap: if the account is ever added to a chat you didn't intend to expose (a group admin adds you, you join something temporarily), the MCP surface picks it up automatically with no code-level check.

Requirement (from the user): keep the MCP server as the *only* publicly exposed part, but add an admin UI — reachable only on an internal Fly address — for three things: (1) WhatsApp login via QR code, (2) viewing/editing chat metadata (tags/notes — already exists as a tool-facing feature via `chat_metadata`), and (3) **selecting which groups/chats are actually presented to the MCP.**

## Decision

**Allowlist becomes a real, enforced, in-app boundary — not just an account-membership convention.**

- Add `is_allowed BOOLEAN NOT NULL DEFAULT false` to the existing `chat_metadata` table. Default is allowlist-closed: a chat is invisible to every MCP read tool until someone explicitly turns it on from the admin UI. This is an *additional* layer on top of the account-membership whitelist from the original plan, not a replacement for it.
- Enforce it with a decorator around the swappable store from [0001](0001-keep-custom-fastmcp-server.md): a new `AllowlistedMessageStore(MessageStore)` in `message_store.py` wraps whichever concrete `MessageStore` is active (today `WuzAPIPostgresMessageStore`; a future WAHA-backed one later) and filters every chat/message read to allowed JIDs. `app.py`'s lifespan wraps the concrete store once; `tools.py` and any future store implementation are unaffected — this is exactly the seam 0001 set up for.
- `list_groups`/`get_group_info` bypass `MessageStore` (they call `state.wuzapi` directly) — they get their own filter step in `tools.py`, following the same pattern the existing `exclude_communities` filter already uses.
- A tool asked about a specific JID that isn't on the allowlist returns an explicit "not in allowlist" error, not a silently empty result — so Claude can't confuse "doesn't exist" with "exists but isn't permitted," which matters for what it tells you.
- The MCP-facing metadata tools (`tag_chat`, `get_chat_metadata`) also respect the allowlist — Claude shouldn't be able to discover or read metadata for a chat that isn't otherwise visible to it. The admin UI is exempt from this check: it talks to the database directly (not through the MCP tool layer) and needs to see and edit every chat WuzAPI knows about, including ones not yet allowed, in order to allow them.

**The admin UI is a separate, network-private application, not an MCP resource.**

- It's a plain browser page, not an MCP "App" (the existing `whatsapp_session` tool + `session_view` HTML resource are MCP Apps, only usable inside an MCP client via `app.callServerTool` — not something you'd open in a normal browser tab). The QR/connect/status logic currently inline in the `whatsapp_session` tool gets factored into a shared function both the tool and the new admin page call, rather than duplicated.
- Deployed as its **own Fly app**, distinct from `mcp-server`, with **no public `[[services]]` block at all** — reachable only via Fly's private 6PN networking (`fly proxy` / WireGuard from your machine). This was chosen over making it a second process inside the mcp-server's `fly.toml`: with a fully separate app, the admin UI is unreachable from the internet by construction, independent of how the Cloudflare Tunnel/Access ingress rules in front of the MCP server are configured — no shared blast radius if that config is ever wrong.
- **Auth: network-reachability only, no login screen.** Considered adding the same `GoogleProvider`/allowlist check from [0003](0003-google-oauth-authn-authz.md) as a second layer, since the wiring already exists and would be cheap to reuse — decided against it. "Accessible on an internal fly address" is already the intended control for a single-user admin surface; an extra login screen would be a moving part with no one else it's protecting against.

## Consequences

- Service count grows from 3 (Postgres, WuzAPI, mcp-server) to 4 (+ admin-ui). Accepted for the isolation guarantee described above.
- The allowlist becomes the actual privacy boundary Claude's tool responses are built on — worth treating any change to `AllowlistedMessageStore` or the `list_groups`/`get_group_info` filters with the same care as the auth changes in 0003, since a bug there has the same shape of consequence (an unintended chat becomes visible).
- If the admin UI's network isolation is ever weakened later (e.g. WireGuard access shared more broadly than intended), the "network-only" auth decision above should be revisited — it was made for the current single-user threat model, not as a permanent rule.

Implemented via `docs/plans/admin-ui-and-chat-allowlist.md`, an implementation plan removed from the repo once it landed — see `admin/app.py` and `admin/templates.py` for the resulting admin UI.
