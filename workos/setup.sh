#!/usr/bin/env bash
# Generic WorkOS AuthKit setup for a self-hosted botsapp (docs/decisions/0036,
# docs/self-hosting.md). Unlike apply.sh, which carries the maintainer's own
# environment ids, this takes the environment and URL as arguments.
#
# Usage: workos/setup.sh <environment-id> <base-url>
#   environment-id  from `workos environment list` (your own project)
#   base-url        your deployment, e.g. https://myapp.fly.dev (no trailing /)
#
# Does the part the `workos` CLI can do (idempotently: URIs already present are
# skipped), then prints the three operations that only the WorkOS MCP server or
# Dashboard can do, as ready-to-paste payloads. Run `workos auth login` first.
set -euo pipefail

env_id="${1:?usage: workos/setup.sh <environment-id> <base-url>}"
base_url="${2:?usage: workos/setup.sh <environment-id> <base-url>}"
base_url="${base_url%/}"
case "$base_url" in https://*|http://localhost*) ;; *)
  echo "error: base-url must be https:// (or http://localhost for local dev)" >&2; exit 1 ;;
esac

export WORKOS_MODE=agent
existing="$(workos authkit redirect-uris list --environment-id "$env_id" --json)"

add_redirect() {
  if printf '%s' "$existing" | grep -qF "\"uri\":\"$1\""; then
    echo "redirect URI already present: $1"
  else
    workos config redirect add "$1" --environment-id "$env_id" --json
  fi
}
add_redirect "${base_url}/oauth2/callback"        # MCP login (fastmcp AuthKitProvider)
add_redirect "${base_url}/admin/auth/callback"    # admin UI login (ADMIN_URL_PREFIX=/admin)
workos config homepage-url set "$base_url" --environment-id "$env_id" --json

echo
echo "Final redirect URIs:"
workos authkit redirect-uris list --environment-id "$env_id" --json

cat <<MSG

── Remaining steps (no \`workos\` CLI command exists; use the WorkOS MCP server's
   \`mutate\` tool or the Dashboard) — environment_id: ${env_id}

1. Dynamic Client Registration (lets claude.ai register itself):
     updateAuthkitSettings  { "isAuthkitDynamicClientRegistrationEnabled": true }

2. OAuth resource (so access tokens carry a validated audience):
     setAuthkitOauthResources  { "resources": [ { "uri": "${base_url}/mcp", "isDefault": true } ] }

3. JWT template (stamps the email the server authorises on):
     upsertJwtTemplate  { "content": "{\\n    \\"email\\": {{ user.email }}\\n}" }

4. Roles: the Users page needs the environment roles \`admin\` and \`member\`
   (WorkOS creates both by default). Create an organization for your people and
   put its id in WORKOS_ORG_ID; the first admin must be a member with role admin.

Then check everything: docs/self-hosting.md → "Verify".
MSG
