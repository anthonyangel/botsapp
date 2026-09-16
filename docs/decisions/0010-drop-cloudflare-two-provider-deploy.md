# 0010 — Drop Cloudflare Tunnel/Access; Fly.io's own HTTPS + AuthKit is the whole exposure story

**Status:** Decided (2026-09). Revisits the exposure plan carried since 0001/0002; supersedes the Cloudflare-related parts of [0001](0001-keep-custom-fastmcp-server.md), [0002](0002-flyio-hosting-mechanics.md), and [0003](0003-google-oauth-authn-authz.md) only — nothing else in those records changes.

## Context

Every prior record ([0001](0001-keep-custom-fastmcp-server.md), [0002](0002-flyio-hosting-mechanics.md), [0003](0003-google-oauth-authn-authz.md)) assumed `mcp-server` would sit behind Cloudflare Tunnel (private origin, no public IP on the Fly side) with Cloudflare Access as an identity-aware proxy in front of it. That assumption predates [0005](0005-authkit-oauth-provider.md): back when the server had no login of its own, Access was *the* identity gate. 0003 already noted its role was "reduced, not eliminated" once `GoogleProvider` (later `AuthKitProvider`) became the real per-person identity layer — but left Cloudflare in the stack for defense-in-depth / not exposing a raw origin IP.

Deploying now, going into this deliberately as a two-external-provider system (AuthKit + Fly.io) rather than three: a third provider (Cloudflare) means a third account, a third thing that can misconfigure or lapse, and a third place a redirect URI or DNS record can drift — for a family-scale server where the actual access-control decision already happens at AuthKit, not at the network edge.

## Decision

- **No Cloudflare Tunnel, no Cloudflare Access.** `mcp-server` is deployed as an ordinary public Fly app — Fly's own edge proxy gives it `https://<app>.fly.dev` with automatic TLS, no extra config. `AuthKitProvider` + `allowed_family_email` (unchanged from [0005](0005-authkit-oauth-provider.md)) is the entire identity/access gate in front of every tool.
- **The network-privacy half of the original plan is unchanged**, just enforced entirely by Fly rather than partly by Cloudflare: `waha` and `admin-ui` are separate Fly apps with **no public `[[services]]` block at all** — reachable only over Fly's 6PN private networking (`<app>.internal:<port>` from `mcp-server`; `fly proxy`/WireGuard from a laptop for `admin-ui`). This was already the plan for `admin-ui` in [0004](0004-chat-allowlist-and-admin-ui.md); `waha` gets the same treatment rather than living in the same app/process group as `mcp-server`, so its internal DNS name is unambiguous and it can never accidentally inherit a public service block.
- **What's accepted in exchange for dropping Access**: the origin IP (`<app>.fly.dev`) is now visible to anyone who resolves it, and there's no WAF/bot-filtering layer in front of the login page itself. Judged acceptable at family-server traffic levels — `AuthKitProvider` still requires a real OAuth login before any tool call succeeds, and every outbound/mutating tool stays permanently blocked by `restrict_tag` regardless of who authenticates (0003/0005 reasoning, unchanged).

## Consequences

- Deploy footprint is exactly two external providers: **WorkOS** (AuthKit — identity) and **Fly.io** (compute, networking, TLS, secrets). No Cloudflare account, tunnel daemon (`cloudflared`), or Access policy to provision, rotate, or keep in sync with redirect URIs.
- Three Fly apps, one org: `botsapp-mcp` (public), `botsapp-waha` (private), `botsapp-admin` (private) — each with its own `fly.toml`, Fly's answer to infrastructure-as-code for this project (no Terraform; matches Fly's own recommendation and 0002's finding that Fly hosting mechanics need nothing fancier than `fly launch`/`fly deploy` per app).
- If a future need arises for WAF/bot-filtering or hiding the origin IP specifically (not identity — AuthKit already covers that), Cloudflare (or Fly's own upcoming edge features) can be re-added in front of `botsapp-mcp` alone without touching `waha`/`admin-ui`'s private-only posture or any `app.py` auth code.
