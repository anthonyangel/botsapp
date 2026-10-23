#!/usr/bin/env sh
# Fly-only entrypoint (see docs/decisions/0013, 0028) — runs three processes
# in one container so mcp-server and admin-ui can share one Fly Volume
# (which attaches to only one Machine at a time) while still sitting behind
# one public port. docker-compose.yml doesn't use this: its two containers
# each get their own `command:` and talk directly to the host's 8000/8100.
set -eu

# admin-ui: unchanged from pre-0028 — bound to :: (not 0.0.0.0) because it's
# reached two ways on the same Machine: `fly proxy` over the real 6PN IPv6
# interface (confirmed live: 127.0.0.1:8100 refused, ::1:8100 OK, after a
# same-machine IPv6-only regression broke public login), and now also the
# Caddy proxy below, over ::1. Don't "simplify" this to 0.0.0.0.
uv run uvicorn botsapp.admin_main:app --host :: --port 8100 &
ADMIN_PID=$!

# mcp-server: no longer Fly's public [http_service] target (that's Caddy,
# below) — it's only ever reached by Caddy, in this same container, so it
# moves to a loopback-only port instead of 0.0.0.0:8000.
#
# Strict mode (docs/decisions/0027) still applies here: the MCP process uses
# each account's own scoped WAHA key and must not hold the shared admin key
# at all, so a bug or a compromise there can't reach other sessions or send.
# Both processes share one environment on Fly, so withhold it from this one
# only; admin-ui, started above, keeps it for session lifecycle and key
# management.
case "${WAHA_REQUIRE_SCOPED_KEYS:-}" in
  1 | true | TRUE | True | yes | YES)
    env -u WAHA_API_KEY uv run uvicorn botsapp.main:app --host 127.0.0.1 --port 8001 &
    ;;
  *)
    uv run uvicorn botsapp.main:app --host 127.0.0.1 --port 8001 &
    ;;
esac
MCP_PID=$!

# Before 0028, mcp-server was execed as PID 1, so Fly's health checks and
# restart policy watched it directly: if it died, the whole Machine
# restarted, taking admin-ui with it rather than limping on half-alive.
# Caddy is PID 1 now and would happily keep answering health checks while
# proxying into a dead backend, so reproduce that guarantee explicitly: if
# either backend exits, bring the container down so Fly restarts it.
(
  while kill -0 "$ADMIN_PID" 2>/dev/null && kill -0 "$MCP_PID" 2>/dev/null; do
    sleep 2
  done
  echo "start.sh: a backend process exited; stopping the container" >&2
  kill -TERM 1 2>/dev/null || true
) &

# Caddy: the only thing Fly's public [http_service] (port 8000) ever
# reaches now — bound 0.0.0.0 for the same reason mcp-server used to be
# (Fly's proxy forwards over IPv4 127.0.0.1). Routes /admin/* to admin-ui,
# everything else to mcp-server; see server/Caddyfile.
exec caddy run --config /app/Caddyfile --adapter caddyfile
