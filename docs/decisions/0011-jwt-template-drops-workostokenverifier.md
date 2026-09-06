# 0011 — WorkOS JWT Template replaces `WorkOSTokenVerifier`; real `aud` validation restored

**Status:** Decided (2026-09). Supersedes the verifier-construction half of [0005](0005-authkit-oauth-provider.md)'s addendum only — the allowlist/`restrict_tag`/outbound-tag reasoning from [0003](0003-google-oauth-authn-authz.md)/0005 is unchanged, and still reads `token.claims.get("email")` exactly as before.

## Context

0005's addendum picked `WorkOSTokenVerifier` (calls WorkOS's `/oauth2/userinfo` per request) over `AuthKitProvider`'s default verifier (a local JWT decode) because the default verifier's JWT only reliably carried `sub` — not the `email` claim `allowed_family_email` depends on — per WorkOS's own MCP token-verification example. Accepted trade-offs at the time: one extra HTTP round-trip per call, and no local `aud`/audience validation (`WorkOSTokenVerifier` trusts a 200 from WorkOS rather than checking a resource-bound claim, which 0005 flagged explicitly as skipping RFC 8707 audience auto-binding).

Revisited because WorkOS AuthKit environments support **JWT Templates** — a per-environment template that adds custom claims to the access token itself, discovered via the WorkOS MCP server's `jwtTemplate`/`upsertJwtTemplate` operations and their live context schema (confirmed to expose `user.email`, not assumed from docs alone).

## Decision

- Added a JWT Template to the `botsapp` WorkOS project's Staging environment: `{"email": {{ user.email }}}`. A bare top-level `email` key is accepted (WorkOS only reserves `iss`/`sub`/`exp`/`iat`/`nbf`/`jti` — confirmed against the live environment, not just docs wording, since WorkOS's own examples always namespace custom claims and don't explicitly say a bare key is fine).
- Registered `http://localhost:8000/mcp` as an AuthKit OAuth resource (`setAuthkitOauthResources`) — required for `AuthKitProvider`'s default verifier to bind and validate the token's `aud` claim; without it, per FastMCP's own docs, "AuthKit falls back to a default environment-scoped audience and audience validation will fail with a 401." Re-register per environment (Production's Fly URL, once deployed).
- `app.py`'s `_build_auth()` no longer passes `token_verifier=WorkOSTokenVerifier(...)` — `AuthKitProvider` now builds its own default `JWTVerifier`, which per its source (`fastmcp/server/auth/providers/workos.py`) auto-binds `audience` to the resource URL once `set_mcp_path()` runs. `allowed_family_email` is untouched: it already read `token.claims.get("email")` generically, so the JWT Template closing that gap needed no code change beyond removing the override.
- Full test suite (197 tests) and `ruff check` pass unchanged — `test_app_auth.py` never referenced `WorkOSTokenVerifier` directly, confirming 0005's own note that the allowlist check is provider-agnostic.

## Consequences

- No more per-request userinfo round-trip; token verification is a local JWT/JWKS decode.
- Real RFC 8707 audience validation is now active (0005's `WorkOSTokenVerifier` had none) — a token minted for a different resource is rejected locally rather than trusted on a bare 200.
- **This does not fix or route around [0012](0012-authkitprovider-issuer-trailing-slash-bug.md)'s issuer-mismatch bug** — that bug lives in the unconditional `authorization_servers=[AnyHttpUrl(self.authkit_domain)]` line `AuthKitProvider.__init__` runs regardless of which token_verifier is used, confirmed by reading the source. Both the old (`WorkOSTokenVerifier`) and new (default `JWTVerifier`) configurations hit the same client-side discovery failure until that's addressed separately.
- Whenever `botsapp` deploys a new environment (Fly's Production URL), both the JWT Template and the OAuth resource registration need to be repeated there — neither is project-wide, both are per-environment.
