# 0003 — Authentication & authorization: `GoogleProvider` + email allowlist + `restrict_tag`, not a static key

**Status:** Decided (2026-09). **Provider choice superseded by [0005](0005-authkit-oauth-provider.md)** (`GoogleProvider` → `AuthKitProvider`) — the allowlist/`restrict_tag`/outbound-tag reasoning below is unchanged and still applies verbatim under the new provider.

## Context

The strongest argument for switching to WAHA ([0001](0001-keep-custom-fastmcp-server.md)) was that the custom FastMCP server had no authentication (`mcp.http_app()` with no `auth=`) and no enforced read-only boundary, while WAHA gets both natively (per-key `read`/`send`/`control`/`setting`/`app`/`delete` scopes, checked by WAHA itself). Before accepting that trade-off, FastMCP's own authentication and authorization documentation was read in full — including reading the installed package's source directly (`fastmcp/server/auth/*`, `fastmcp/server/middleware/authorization.py`) to confirm exact call signatures rather than trusting doc summaries alone.

## Findings — Authentication (who you are)

| Class | Use case |
|---|---|
| `JWTVerifier` | Validate tokens only (JWKS/HMAC/static key) — no OAuth flow of its own |
| `RemoteAuthProvider` | Identity provider **with** Dynamic Client Registration (Descope, WorkOS AuthKit) — zero manual app registration |
| `OAuthProxy` | Provider **without** DCR (raw GitHub/Google/Azure/AWS APIs) — bridges DCR-expecting MCP clients to manually pre-registered app credentials; issues its own short-lived JWTs rather than passing through upstream tokens (a "token factory," preventing token-passthrough attacks) |
| `OIDCProxy` | `OAuthProxy` specialized for standard OIDC providers — configure via `.well-known/openid-configuration` + client id/secret |
| Pre-built per-vendor providers (`GoogleProvider`, `GitHubProvider`, etc., `fastmcp.server.auth.providers.*`) | Thin `OAuthProxy`/`OIDCProxy` subclasses per vendor — this is what you actually construct, not the raw proxy classes |
| `OAuthProvider` | Full DIY OAuth 2.1 authorization server — 11 abstract methods to implement yourself. Docs explicitly discourage this for nearly all use cases ("extremely advanced pattern most users should avoid") |
| `MultiAuth` | Compose several of the above (AND logic across a server-level check, OR-style fallback across token verifiers) — for accepting tokens from multiple sources at once; not needed here (single family, single provider) |

**Chosen: `GoogleProvider`.** Everyone in the family already has a Google account — no separate identity system to stand up, register users in, or maintain.

## Findings — Authorization (what an authenticated caller can do)

Confirmed by reading `fastmcp/server/auth/authorization.py` and `fastmcp/server/middleware/authorization.py` directly:

- Auth checks are plain callables: `Callable[[AuthContext], bool | Awaitable[bool]]`. `AuthContext` carries `.token: AccessToken | None` and `.component` (the tool/resource/prompt being accessed).
- `require_scopes(*scopes)` — built-in check requiring all given OAuth scopes to be present on the token.
- `restrict_tag(tag, *, scopes)` — if the component carries `tag`, the token must have all `scopes`; if it doesn't carry the tag, the check passes unconditionally. Exact signature confirmed from source.
- `AuthMiddleware(auth=...)` — applies auth checks globally to every tool/resource/prompt (filters `list_tools` results and blocks `call_tool` with `AuthorizationError`, raising `InsufficientScopeError` with a `required_scopes` attribute when a scope check fails). Accepts **either a single check or a `list[AuthCheck]`, combined with AND logic** — confirmed from source — so multiple independent checks can share one `AuthMiddleware`.
- Component-level auth (`auth=` passed to `@mcp.tool(...)` directly) is the alternative to server-level `AuthMiddleware`: it hides the component from unauthorized callers entirely (looks not-found) rather than raising an explicit error.
- `get_access_token()` (from `fastmcp.server.dependencies`) returns the current `AccessToken | None`. `AccessToken.claims: dict[str, Any]` (FastMCP's subclass of the MCP SDK's `AccessToken`, per `fastmcp/server/auth/auth.py`) holds every claim from the token. For `GoogleProvider` specifically, `providers/google.py` shows `claims["email"]` populated straight from Google's userinfo response — confirmed directly in source, not just docs.

## Decision

- **Authentication**: `GoogleProvider`, restricted at the application layer to a small `ALLOWED_EMAILS` allowlist — a custom check (`allowed_family_email`) reads `get_access_token().claims.get("email")` and raises `AuthorizationError` if it isn't in the list. This gives real, per-person login (consent screen, revocable Google session) rather than a single shared bearer secret.
- **Read-only enforcement**: a new `"outbound"` tag applied to every tool that mutates WhatsApp state, gated by `restrict_tag("outbound", scopes=["whatsapp:send"])`. No Google identity token will ever carry a custom `"whatsapp:send"` scope, so every `"outbound"`-tagged tool becomes permanently unreachable by construction — a hard-off switch enforced by the server on every call, not a promise that nobody registers those tools.
- Both checks are combined in **one** `AuthMiddleware(auth=[allowed_family_email, restrict_tag("outbound", scopes=["whatsapp:send"])])`, per the confirmed AND-list behavior above.
- This closes the gap that originally favored WAHA in [0001](0001-keep-custom-fastmcp-server.md) — arguably more precisely: per-tool tagging plus explicit `AuthorizationError`s, versus WAHA's single opaque API key with no per-person identity or revocation story.
- **Cloudflare Access's role is reduced**, not eliminated: it was originally meant to be *the* identity gate in front of WAHA. With `GoogleProvider` as the real identity layer, Cloudflare (Tunnel, and optionally Access) becomes about not exposing a raw origin IP / extra defense-in-depth, independent of authentication — the network-privacy decision from the original plan is unchanged.

Implementation detail lived in `docs/plans/message-store-auth-cleanup.md`, an implementation plan removed from the repo once it landed — see `app.py` for the resulting auth wiring.

## Addendum: `LOCAL_DEV_DISABLE_AUTH` (added when getting the stack running locally)

Setting up a real Google OAuth client just to run `docker compose up` locally was friction with no security benefit on a machine only the owner can reach. Added `LOCAL_DEV_DISABLE_AUTH` (env var, `.env`-only, defaults unset/off): when set, `_build_auth()` returns `None` (no `GoogleProvider` built at all — no client id/secret/base URL required either) and `allowed_family_email` is dropped from the `AuthMiddleware` check list in `_build_auth_checks()`.

Deliberately **not** a general "disable all auth" switch: `restrict_tag(OUTBOUND_TAG, ...)` is never removed from the check list regardless of this flag — every WhatsApp-mutating tool stays permanently unreachable whether or not login is required, since that check depends on a scope no token (real or absent) will ever carry, not on `allowed_family_email` having run first. So the worst case with this flag on is "anyone who can reach the server can read allowed chats" — never "anyone can send."

`.env` ships with this on by default for local dev; `.env.example` documents it commented-out with the warning to never set it anywhere reachable by more than the owner.
