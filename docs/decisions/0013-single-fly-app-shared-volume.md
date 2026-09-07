# 0013 — `mcp-server`/`admin-ui` deploy as one Fly Machine, two processes; `waha` stays separate

**Status:** Decided (2026-09). Amends the app count in [0010](0010-drop-cloudflare-two-provider-deploy.md) — everything else there (no Cloudflare, Fly's own HTTPS + AuthKit as the whole exposure story, `waha`/`admin-ui` never getting a public `[[services]]` block) is unchanged.

## Context

0010 committed to three Fly apps: `botsapp-mcp` (public), `botsapp-waha` (private), `botsapp-admin` (private). That was written before re-checking it against [0007](0007-sqlite-for-metadata-store.md): `mcp-server` and `admin-ui` share one SQLite file (`chat_metadata.db`) over one Docker volume today, WAL mode handling the two processes' concurrent access to the same file. Deploying `mcp-server` and `admin-ui` as separate Fly apps, as originally written, would break that: **a Fly Volume attaches to exactly one Machine at a time** (confirmed against Fly's own docs — "a volume can be attached to only one Machine"), so two separate apps (each getting their own Machine) can never both mount the same volume, regardless of region.

An earlier draft of this record tried to fix that with Fly's [process groups](https://fly.io/docs/apps/processes/) feature instead — still wrong for the same reason: a process group is Fly's mechanism for giving a *named set of Machines* its own scaling/command, but each Machine in that set is still a separate Machine, so two process groups still can't share one volume between them either. The fix has to put both processes on the *same* Machine.

Two ways to actually close the gap:
1. Give each of `mcp-server`/`admin-ui` its own volume and have `admin-ui` reach the metadata store over an internal API call to `mcp-server` instead of a shared file.
2. Run both processes inside **one Machine** — one container, one shared volume, exactly how WAL-mode SQLite already works locally (two OS processes on one host, same file) — with only `mcp-server`'s port declared as a public Fly service.

Option 2 is the smaller change: no new internal API surface to build/auth/test just to satisfy a deploy-topology preference, and it matches this project's running theme (0002, 0007, 0010) of not adding infrastructure a family-scale service doesn't need.

## Decision

- **One Fly app** (`botsapp`), **one Machine**, running both processes from one container via a small startup script (`server/start.sh`, the image's `CMD`):
  ```sh
  uv run uvicorn botsapp.admin_main:app --host 0.0.0.0 --port 8100 &
  exec uv run uvicorn botsapp.main:app --host 0.0.0.0 --port 8000
  ```
  `admin_main` backgrounded, `main` execed in the foreground so the container's PID 1 is the MCP server and dies (taking the admin process with it, restarting the whole Machine) if it ever crashes — the process actually meant to stay up is the one Fly's own health checks and restart policy watch.
- `fly.toml` declares a `[[services]]` block only for port 8000 (`mcp-server`, AuthKit-gated) — port 8100 (`admin-ui`) gets no public service block at all, so it's reachable only over Fly's private 6PN networking / `fly proxy` from a laptop, exactly like today's loopback-only Compose binding.
- **One Fly Volume**, mounted at `/app/data` on that single Machine, holding `chat_metadata.db` — this is what actually needed preserving from 0007. Since both processes run in the same container on the same Machine, they see the same mount exactly as they do under `docker-compose.yml` today; WAL mode handles the concurrent access unchanged.
- **`waha` stays its own separate Fly app** (`botsapp-waha`, private) — unaffected by this change. It's a different image entirely (`devlikeapro/waha:dev-arm`, not this repo's `server/Dockerfile`) with its own volumes (`waha-data`/`waha-media`), so folding it into `botsapp` would solve nothing and only complicate the container.
- Net Fly footprint: **two apps**, each a single Machine (for now) — `botsapp` and `botsapp-waha`.

## Consequences

- `chat_metadata.db` keeps working exactly as WAL-mode-shared-file today, deployed or local — no new code, no new internal API, no new auth surface between `mcp-server` and `admin-ui`.
- `admin-ui`'s network-privacy story (0004) is enforced the same way as `waha`'s: simply never declaring a `[[services]]` block for that port, not a second layer of access control.
- **`mcp-server` and `admin-ui` can no longer scale or restart independently** — they live and die together as one Machine. Accepted: neither needs independent scaling at family-server traffic, and `admin-ui` was never meant to run without `mcp-server`'s bridge/db state anyway (bridge_factory.py's whole point is that both processes build it the same way).
- `server/Dockerfile` gains `server/start.sh` (`COPY`+`chmod +x`) but keeps its existing default `CMD` (`botsapp.main:app` alone) — `docker-compose.yml`'s two-service, two-container split (each with its own `command:`) is untouched and keeps working exactly as today, since Compose containers *can* all mount the same named volume, unlike Fly Machines. `fly.toml`'s `[processes]` table points the single Fly Machine at `server/start.sh` instead, purely a Fly-side override.
- If `admin-ui` ever needs to scale, fail, or restart independently of `mcp-server` in a way one Machine can't express, revisit this and split it out per option 1 above (a real internal API) — nothing here forecloses that later.
