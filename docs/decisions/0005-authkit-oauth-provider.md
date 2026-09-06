# 0005 — Switch authentication from `GoogleProvider` to WorkOS AuthKit

**Status:** Decided (2026-09) — supersedes the *provider* choice in [0003](0003-google-oauth-authn-authz.md) only

## Context

[0003](0003-google-oauth-authn-authz.md) chose `GoogleProvider` because everyone in the family already has a Google account. Revisiting that choice after comparing FastMCP's built-in providers against free-tier identity platforms (WorkOS AuthKit, Clerk, Auth0):

- **AuthKit's free tier covers up to 1M MAUs** — no realistic ceiling for a family-scale server.
- **AuthKit is DCR-native** (`RemoteAuthProvider`-backed, per `fastmcp/server/auth/*`), meaning MCP clients that expect Dynamic Client Registration can self-register against it. `GoogleProvider` is an `OAuthProxy`/`OIDCProxy` subclass — Google doesn't support DCR, so FastMCP has to broker fixed, manually-registered app credentials instead.
- **Google sign-in isn't lost, just relocated.** AuthKit's Social Login feature supports Google as one of several hosted-login buttons. The Google Cloud Console setup 0003 already required (OAuth consent screen, Web application client, redirect URI) is unchanged — the only difference is the redirect URI points at WorkOS's callback instead of directly at `<MCP_BASE_URL>/auth/callback`, and the resulting client id/secret get pasted into the WorkOS Dashboard instead of into `app.py`.
- AuthKit also ships a hosted login page, user management/session handling, and room to add more social providers (GitHub, Microsoft, Apple) later purely via dashboard config, with no further `app.py` changes.

## Decision

- Replace `GoogleProvider` with `AuthKitProvider` (`fastmcp.server.auth.providers.workos.AuthKitProvider`) as the identity layer in `app.py`.
- **Sequencing, deliberately staged:**
  1. **First**, wire up `AuthKitProvider` alone, with only AuthKit's own default hosted login (no social providers enabled yet). Verify end-to-end against the existing test suite plus a manual login, confirming the allowlist/outbound gates still behave identically with the new provider.
  2. **Only after that's confirmed working**, enable Google as an AuthKit Social Login provider — a WorkOS Dashboard + Google Cloud Console change (create the GCP OAuth client, point its redirect URI at the one WorkOS's dashboard gives you, paste the resulting client id/secret into WorkOS), not a FastMCP code change. `app.py` doesn't need to be touched again for this step.
- **Unchanged from 0003**: `allowed_family_email` allowlist check, `restrict_tag("outbound", scopes=["whatsapp:send"])`, the AND-composed `AuthMiddleware`, and the `LOCAL_DEV_DISABLE_AUTH` local-dev bypass. Only the provider construction changes — need to confirm during implementation whether `AuthKitProvider` populates the email claim under the same `claims["email"]` key `GoogleProvider` used (read `fastmcp/server/auth/providers/workos.py` directly rather than assuming).
- **Env vars**: `WORKOS_AUTHKIT_DOMAIN` and (kept) `MCP_BASE_URL` replace `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` — `AuthKitProvider` is DCR-based, so there's no app-specific client id/secret pair for us to hold. `ALLOWED_EMAILS` is unchanged.

## Consequences

- This is a follow-up patch to already-completed work, not new scope: `docs/plans/message-store-auth-cleanup.md` section 3 shipped `GoogleProvider` with 146 tests passing. (That plan was updated alongside this record to reflect the swap, then later removed from the repo entirely once fully landed — see `app.py` for the current `AuthKitProvider` wiring.)
- 0003's allowlist/`restrict_tag`/outbound-tag reasoning stands unchanged — this record only revisits *which provider issues the identity token*, not what the server does with it afterward.
- If AuthKit's free tier terms change materially, revisit — the 1M-MAU headroom is the main practical reason this beats `GoogleProvider`'s DCR gap for an MCP-client audience.

## Addendum: the flagged `claims["email"]` question — resolved by overriding `token_verifier`

Implemented in `app.py`. Reading `fastmcp/server/auth/providers/workos.py` directly resolved the open question from the Decision section above:

- `AuthKitProvider`'s *default* `token_verifier` (used when none is passed) is a bare `JWTVerifier` that decodes AuthKit's access-token JWT locally via JWKS. Its claims are whatever WorkOS puts in that JWT — and WorkOS's own MCP token-verification docs example extracts only `sub`, not `email`, which is not strong enough evidence to rely on `email` being present.
- The same `workos.py` module also defines `WorkOSTokenVerifier` (normally used by the separate, non-DCR `WorkOSProvider`) — it calls WorkOS's `/oauth2/userinfo` endpoint per verification and explicitly builds `claims={"email": user_data.get("email"), ...}`.
- `AuthKitProvider.__init__` accepts an optional `token_verifier=` override for exactly this kind of substitution — passing one skips the provider's own audience auto-binding (RFC 8707), which `WorkOSTokenVerifier` doesn't need since it validates the token against WorkOS directly rather than checking a local `aud` claim.
- **Decision**: construct `AuthKitProvider` with `token_verifier=WorkOSTokenVerifier(authkit_domain=...)` explicitly, rather than trusting the default verifier's JWT to carry `email`. This makes `allowed_family_email`'s behavior identical to what it was under `GoogleProvider`, confirmed from source rather than assumed.
- **Trade-off accepted**: one extra HTTP round-trip to WorkOS per tool call (userinfo lookup) instead of a local JWT decode. Negligible at family-server request volumes; revisit only if latency ever matters, by switching back to the default local verifier once a live login has confirmed AuthKit's access token does carry `email` in this WorkOS project's configuration.
