#!/usr/bin/env sh
# Fly-only entrypoint (see docs/decisions/0013) — runs both processes in one
# container so they can share one Fly Volume, which attaches to only one
# Machine at a time. docker-compose.yml doesn't use this: its two containers
# each get their own `command:` and can both mount the same named volume.
#
# admin-ui backgrounded, mcp-server execed as PID 1: mcp-server is the
# process Fly's health checks and restart policy actually watch, so if it
# dies the whole Machine restarts (taking admin-ui with it) rather than the
# container silently running on with only half its services alive.
set -eu

# Deliberately different bind addresses per process — confirmed live via
# `fly ssh console` (connect probes: 127.0.0.1:8000 refused, ::1:8000 OK)
# after a same-machine IPv6-only regression broke public login:
# - mcp-server (8000) is reached through Fly's public [http_service], whose
#   proxy forwards to the Machine over IPv4 127.0.0.1 — needs 0.0.0.0.
# - admin-ui (8100) is private-only, reached solely via `fly proxy` over the
#   Machine's real 6PN IPv6 interface — needs ::, not 0.0.0.0.
# Don't "simplify" these to match each other; each is required as-is.
uv run uvicorn botsapp.admin_main:app --host :: --port 8100 &
exec uv run uvicorn botsapp.main:app --host 0.0.0.0 --port 8000
