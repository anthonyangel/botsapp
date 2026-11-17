#!/usr/bin/env bash
# One-command Fly deploy for a self-hosted botsapp (docs/decisions/0036,
# docs/self-hosting.md). Creates two Fly apps (<name> and <name>-waha), sets
# their secrets, and deploys both. Re-runnable: existing apps are reused, and
# secrets that already exist on the app are left alone (never rotated).
#
# Usage: scripts/fly-deploy.sh <app-name> <admin-email> [region]
#
# Prerequisites: `fly auth login`, and the WorkOS values below exported (see
# docs/self-hosting.md; `workos/setup.sh` prints them):
#   WORKOS_AUTHKIT_DOMAIN  e.g. https://your-app.authkit.app
#   WORKOS_CLIENT_ID       AuthKit client id
#   WORKOS_API_KEY         secret API key for the same environment
#   WORKOS_ORG_ID          (optional) the single org; enables the Users page
#                          to invite/remove/re-role people in WorkOS
# Optional: ELEVENLABS_API_KEY (voice-note transcription).
#
# Set DRY_RUN=1 to print every command instead of running it.
set -euo pipefail

app="${1:?usage: scripts/fly-deploy.sh <app-name> <admin-email> [region]}"
admin_email="${2:?usage: scripts/fly-deploy.sh <app-name> <admin-email> [region]}"
region="${3:-fra}"
waha_app="${app}-waha"
base_url="https://${app}.fly.dev"

for v in WORKOS_AUTHKIT_DOMAIN WORKOS_CLIENT_ID WORKOS_API_KEY; do
  [ -n "${!v:-}" ] || { echo "error: $v must be set (see docs/self-hosting.md)" >&2; exit 1; }
done
case "$WORKOS_AUTHKIT_DOMAIN" in
  https://*) ;;
  *) echo "error: WORKOS_AUTHKIT_DOMAIN must include the scheme (https://...)" >&2; exit 1 ;;
esac
# Fly app names are lowercase letters, digits and dashes; the .internal DNS
# name used below is derived from it, so reject anything else up front.
case "$app" in
  *[!a-z0-9-]*|-*|*-) echo "error: app name must be lowercase letters, digits, dashes" >&2; exit 1 ;;
esac

run() { if [ "${DRY_RUN:-}" = 1 ]; then printf '+ %s\n' "$*"; else "$@"; fi; }
repo_root="$(cd "$(dirname "$0")/.." && pwd)"

have_secret() { # app name -> 0 if the secret already exists on the app
  fly secrets list --app "$1" --json 2>/dev/null | grep -q "\"name\": *\"$2\""
}
rand_urlsafe() { openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n'; }  # a valid Fernet key
rand_hex() { openssl rand -hex 32; }

ensure_app() {
  if fly apps list --json 2>/dev/null | grep -q "\"Name\": *\"$1\""; then
    echo "app $1 exists; reusing"
  else
    run fly apps create "$1"
  fi
}

ensure_app "$waha_app"
ensure_app "$app"

# Secrets go in through `fly secrets import` (stdin), never argv, so values
# don't show in `ps` or shell history. Existing secrets are never overwritten:
# WAHA_API_KEY must match on both apps, and BOTSAPP_SECRET_KEY encrypts the
# stored WAHA keys, so replacing either breaks a working deployment.
# Plain strings, not associative arrays: macOS ships bash 3.2.
pending_app=""; pending_waha=""
queue() { # target(app|waha) name value
  local target_app="$app"; [ "$1" = waha ] && target_app="$waha_app"
  if have_secret "$target_app" "$2"; then
    echo "secret $2 already set on $target_app; leaving it"; return
  fi
  if [ "$1" = waha ]; then pending_waha="${pending_waha}$2=$3"$'\n'
  else pending_app="${pending_app}$2=$3"$'\n'; fi
}
flush() { # target(app|waha) app_name
  local buf="$pending_app"; [ "$1" = waha ] && buf="$pending_waha"
  [ -n "$buf" ] || return 0
  if [ "${DRY_RUN:-}" = 1 ]; then
    echo "+ fly secrets import --stage --app $2   (names: $(printf '%s' "$buf" | cut -d= -f1 | tr '\n' ' '))"
    return
  fi
  printf '%s' "$buf" | fly secrets import --stage --app "$2"
}

# One WAHA admin key, set on both apps together or on neither.
if ! have_secret "$waha_app" WAHA_API_KEY && ! have_secret "$app" WAHA_API_KEY; then
  waha_key="$(rand_hex)"
  queue waha WAHA_API_KEY "$waha_key"
  queue app WAHA_API_KEY "$waha_key"
elif have_secret "$waha_app" WAHA_API_KEY && have_secret "$app" WAHA_API_KEY; then
  echo "WAHA_API_KEY already set on both apps; leaving it"
else
  echo "error: WAHA_API_KEY exists on only one of $app / $waha_app; fix that by hand" >&2
  exit 1
fi
queue app BOTSAPP_SECRET_KEY "$(rand_urlsafe)"
queue app BOTSAPP_SESSION_SECRET "$(rand_hex)"
queue app WORKOS_AUTHKIT_DOMAIN "$WORKOS_AUTHKIT_DOMAIN"
queue app WORKOS_CLIENT_ID "$WORKOS_CLIENT_ID"
queue app WORKOS_API_KEY "$WORKOS_API_KEY"
queue app ADMIN_BASE_URL "$base_url"
queue app BOTSAPP_ADMIN_EMAIL "$admin_email"
[ -z "${WORKOS_ORG_ID:-}" ] || queue app WORKOS_ORG_ID "$WORKOS_ORG_ID"
[ -z "${ELEVENLABS_API_KEY:-}" ] || queue app ELEVENLABS_API_KEY "$ELEVENLABS_API_KEY"
flush waha "$waha_app"
flush app "$app"

# WAHA first: botsapp reaches it at <waha_app>.internal over Fly's private net.
# Each deploy runs from its own directory so the Docker build context matches
# what `fly deploy` does by hand. WAHA gets no public IP: it must never be
# reachable beyond Fly's private network (docs/decisions/0010).
if [ "${DRY_RUN:-}" = 1 ]; then
  echo "+ (cd waha && fly deploy --app $waha_app --primary-region $region --ha=false --no-public-ips --yes)"
  echo "+ (cd server && fly deploy --app $app --primary-region $region --ha=false --yes -e MCP_BASE_URL=$base_url -e WAHA_URL=http://${waha_app}.internal:3000)"
else
  (cd "$repo_root/waha" && fly deploy --app "$waha_app" --primary-region "$region" \
    --ha=false --no-public-ips --yes)
  (cd "$repo_root/server" && fly deploy --app "$app" --primary-region "$region" \
    --ha=false --yes \
    -e "MCP_BASE_URL=$base_url" -e "WAHA_URL=http://${waha_app}.internal:3000")
fi

cat <<MSG

Deployed. Next:
  1. Register these in WorkOS (workos/setup.sh does it):
       redirect URIs  ${base_url}/oauth2/callback  and  ${base_url}/admin/auth/callback
       OAuth resource ${base_url}/mcp
  2. Pair WhatsApp: fly proxy 8100:8100 -a ${app}, or open ${base_url}/admin/
  3. Add ${base_url}/mcp as a custom connector in claude.ai.
MSG
