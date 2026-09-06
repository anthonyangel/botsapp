#!/usr/bin/env bash
# Apply botsapp's AuthKit app config (redirect URI + homepage URL) to a
# WorkOS environment. `workos seed`'s `config:` block doesn't actually take
# effect in CLI v0.22.0 (verified empirically — an environment's redirect-uri
# list stayed empty after seeding a populated config block) — these are the
# commands confirmed to work instead, using --environment-id explicitly
# rather than the CLI's "active environment" (observed stale/lagging after
# `workos environment use` in v0.22.0 — re-check with `workos whoami --json`
# before trusting it implicitly).
#
# Usage: workos/apply.sh <staging|production> [base-url]
#   base-url defaults to http://localhost:8000 for staging,
#   and must be passed explicitly for production (your Fly mcp-server URL).
#
# NOT covered by this script — neither is a `workos` CLI operation at all
# (confirmed against `workos --help --json`); both are WorkOS-MCP-server-only
# (`upsertJwtTemplate`/`setAuthkitOauthResources`), and both are
# per-environment like everything else here:
#   - the JWT Template stamping `email` onto the access token
#   - the AuthKit OAuth resource registration (`{base-url}/mcp`) needed for
#     AuthKitProvider's default verifier to bind/validate `aud`

set -euo pipefail

# botsapp project environment IDs (workos project list --json)
STAGING_ENV_ID="environment_01M1TNTVVC7GW4KT3XQ4SX8WJ9"      # mystical-goal-76-staging.authkit.app
PRODUCTION_ENV_ID="environment_01M1TNTWAHFZ0D06KHVYVRYHJ6"   # charming-van-23.authkit.app

target="${1:?usage: workos/apply.sh <staging|production> [base-url]}"
case "$target" in
  staging)
    env_id="$STAGING_ENV_ID"
    base_url="${2:-http://localhost:8000}"
    ;;
  production)
    env_id="$PRODUCTION_ENV_ID"
    base_url="${2:?production requires an explicit base-url, e.g. https://botsapp-mcp.fly.dev}"
    ;;
  *)
    echo "unknown target: $target (expected staging|production)" >&2
    exit 1
    ;;
esac

export WORKOS_MODE=agent
workos config redirect add "${base_url}/oauth2/callback" --environment-id "$env_id" --json
workos config homepage-url set "$base_url" --environment-id "$env_id" --json
workos authkit redirect-uris list --environment-id "$env_id" --json
