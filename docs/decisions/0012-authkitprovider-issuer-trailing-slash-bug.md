# 0012 — `AuthKitProvider` advertises a trailing-slash issuer; blocks every real login

**Status:** Open upstream bug; **worked around locally** as of this writing (`FixAuthkitIssuerTrailingSlashMiddleware`, `server/src/botsapp/_authkit_issuer_workaround.py`, wired into `main.py`). Recorded so it isn't re-discovered/re-investigated later, and so nobody assumes [0011](0011-jwt-template-drops-workostokenverifier.md)'s verifier change fixes it on its own (it doesn't — see below).

## Symptom

Confirmed via a real `authorization_code`+PKCE login attempt (`fastmcp.client.auth.OAuth` against a locally running `mcp-server`): the MCP SDK client rejects the login with

```
OAuthFlowError: Authorization server metadata issuer mismatch:
https://mystical-goal-76-staging.authkit.app != https://mystical-goal-76-staging.authkit.app/
```

This is not specific to that test client — it's the MCP Python SDK's own strict RFC 8414 `validate_metadata_issuer` check (`mcp/client/auth/utils.py`), which any real client (including claude.ai) runs during OAuth discovery. **No client can currently log into `botsapp`'s `mcp-server`, real or test, while this stands.**

## Root cause (confirmed via source, not guessed)

- `botsapp`'s own `/.well-known/oauth-protected-resource/mcp` (served by `AuthKitProvider`) advertises `"authorization_servers":["https://mystical-goal-76-staging.authkit.app/"]` — **with** a trailing slash.
- WorkOS's real `/.well-known/oauth-authorization-server` at that same domain reports `"issuer":"https://mystical-goal-76-staging.authkit.app"` — **without** one. Confirmed by curling both endpoints live, not assumed.
- Traced to `fastmcp/server/auth/providers/workos.py:388`, inside `AuthKitProvider.__init__`:
  ```python
  authorization_servers=[AnyHttpUrl(self.authkit_domain)],
  ```
  `self.authkit_domain` is `.rstrip("/")`'d beforehand (no trailing slash going in), but pydantic's `AnyHttpUrl` normalizes a path-less URL by appending `/` on serialization — so the advertised value gains a trailing slash that WorkOS's own bare-string `issuer` field never had.
- This line runs **unconditionally** in `AuthKitProvider.__init__`, regardless of whether a custom `token_verifier` is passed — confirmed by reading the constructor. [0011](0011-jwt-template-drops-workostokenverifier.md)'s switch away from `WorkOSTokenVerifier` does **not** avoid this; both configurations hit the identical failure.

## Why this isn't already fixed upstream

Checked `PrefectHQ/fastmcp` (the actual repo — `jlowin/fastmcp` redirects there) for prior art via `gh search issues`/`gh pr view`, not guessed:

- **Issue #1848** ("AnyHttpUrl fields in OIDCConfiguration break issuer validation") is the *same class* of bug — verbatim reproduction: `str(AnyHttpUrl("https://example.com")) == "https://example.com/"`, causing a strict issuer string-compare to fail exactly like ours. **Fixed** by PR #1850 ("fix: Improve URL handling in OIDCConfiguration") — but that PR only touched `src/fastmcp/server/auth/oidc_proxy.py` (the generic `OIDCProxy`/`OIDCConfiguration` code path). It never touched `providers/workos.py`, confirmed from the PR's file diff.
- **Issue #1431** ("`resource` is returned with a trailing slash") looks adjacent but is a different field (`resource`, not `authorization_servers`) and a different root cause — the maintainer closed it pointing at the upstream `mcp` SDK's own route code, not fastmcp's. It also doesn't reproduce here: `botsapp`'s own `resource` field (`http://localhost:8000/mcp`) has no trailing slash in practice.
- No `PrefectHQ/fastmcp` issue or PR mentions `AuthKitProvider`, `authorization_servers`, or this domain-plus-real-IdP combination specifically — that half is a genuine, unreported fastmcp-specific bug.
- **The underlying mechanism is a known, actively-worked-on bug in the `mcp` Python SDK itself**: [modelcontextprotocol/python-sdk#1919](https://github.com/modelcontextprotocol/python-sdk/issues/1919) ("Trailing slash in OAuthMetadata's `issuer` causes issues with clients") — open, root-caused to `mcp/server/auth/routes.py`'s `create_protected_resource_routes()`/`build_metadata()` not stripping trailing slashes the way it already does for `authorization_endpoint`/`token_endpoint`. A commenter reproduced the *identical* failure against a live production server (Indeed's `mcp.indeed.com`), confirming this isn't a botsapp/WorkOS-specific quirk. Four PRs attempted so far (#1932, #2019 closed unmerged; #1938, #3013 still open) — no fix has shipped; installed `mcp==2.1.1` is the latest on PyPI and still exhibits the bug.
- A python-sdk commenter independently proposed the same client-side mitigation reached for below (`.rstrip("/")` before comparing) — but that only helps a client *we* control, not claude.ai's.

## Decision (revised)

Given upstream has attempted and failed to merge a fix twice already, **implemented a local workaround** rather than continue waiting indefinitely: `FixAuthkitIssuerTrailingSlashMiddleware` wraps the ASGI app in `main.py`, rewriting the JSON body of `/.well-known/oauth-protected-resource*` responses to strip the trailing slash from `resource` and every `authorization_servers` entry. Verified against the running local stack:

- `curl http://localhost:8000/.well-known/oauth-protected-resource/mcp` — `authorization_servers` now reads `["https://mystical-goal-76-staging.authkit.app"]`, no trailing slash.
- A real `authorization_code`+PKCE login attempt (unpatched MCP client, no client-side workaround) that previously crashed immediately with `OAuthFlowError` now proceeds cleanly through discovery and authorize, all the way to AuthKit's own password-entry step — proof the fix addresses exactly what it targets. (The attempt didn't complete login end-to-end — a throwaway test user's unverified email got a generic "Invalid email or password" rather than a clear verification prompt, and this environment's account tier can't toggle `isEmailVerificationRequired` off via API — but that's an artifact of test-user setup, not the issuer bug, and doesn't apply to a real person's already-verified account.)

## Consequences

- Contained, revertible fix (one file, one import in `main.py`) — no fastmcp/mcp internals patched, no global state touched.
- Remove `FixAuthkitIssuerTrailingSlashMiddleware` and the `main.py` wrapper once `modelcontextprotocol/python-sdk#1919` ships in a released `mcp` version — check `authorization_servers`/`resource` in a fresh `/.well-known/oauth-protected-resource/mcp` response after upgrading `mcp` before removing.
- Not filed upstream from this session (publishing to a public tracker needs the user's own say-so) — worth doing, since the fastmcp-specific half (the `AnyHttpUrl(self.authkit_domain)` line, distinct from the SDK-level bug) has no issue of its own yet.
- Separately surfaced during this investigation: `workos api` (the raw CLI passthrough) resolves its target environment independently of `workos environment use`/`profile list` — repeatedly and reliably landed on the wrong project even when `profile list` confirmed the correct one immediately beforehand. Stronger and more persistent than the "lag" noted in [README.md](../../README.md)'s AuthKit setup section; avoid `workos api` for anything environment-sensitive and use the WorkOS MCP server's `mutate`/`query` tools (explicit `environment_id`, confirmed reliable throughout) instead.
