# 0013 — `mcp-server`/`admin-ui` deploy as one Fly app, two process groups; `waha` stays separate

**Status:** Decided (2026-09). Amends the app count in [0010](0010-drop-cloudflare-two-provider-deploy.md) — everything else there (no Cloudflare, Fly's own HTTPS + AuthKit as the whole exposure story, `waha`/`admin-ui` never getting a public `[[services]]` block) is unchanged.

## Context

0010 committed to three Fly apps: `botsapp-mcp` (public), `botsapp-waha` (private), `botsapp-admin` (private). That was written before re-checking it against [0007](0007-sqlite-for-metadata-store.md): `mcp-server` and `admin-ui` share one SQLite file (`chat_metadata.db`) over one Docker volume today, WAL mode handling the two processes' concurrent access to the same file. A Fly Volume attaches to one app (specifically one machine, in one region) — it isn't a Docker bind mount two independent Fly apps can both point at. Deploying `mcp-server` and `admin-ui` as separate Fly apps, as originally written, would silently break the shared-metadata-file design that 0007 depends on.

Two ways to close the gap:
1. Give each of `mcp-server`/`admin-ui` its own volume and have `admin-ui` reach the metadata store over an internal API call to `mcp-server` instead of a shared file.
2. Keep them as one deployable unit with one shared volume — matching how they already run today (same image, same volume, per [docker-compose.yml](../../docker-compose.yml)) — and use Fly's [process groups](https://fly.io/docs/apps/processes/) feature to give the two entrypoints independent scaling/networking within that one app.

Option 2 is the smaller change and the one already implicit in the existing Compose setup — no new internal API surface to build/auth/test just to satisfy a deploy-topology preference, and it matches this project's running theme (0002, 0007, 0010) of not adding infrastructure a family-scale service doesn't need.

## Decision

- **One Fly app** (`botsapp`) hosts both `mcp-server` and `admin-ui`, as two [process groups](https://fly.io/docs/apps/processes/) in its `fly.toml`:
  - `app` (or `mcp`) — runs `uv run uvicorn botsapp.main:app --host 0.0.0.0 --port 8000`, the only group with a `[[services]]` block (public, AuthKit-gated).
  - `admin` — runs `uv run uvicorn botsapp.admin_main:app --host 0.0.0.0 --port 8100`, **no `[[services]]` block at all** — unreachable except over Fly's private 6PN networking / `fly proxy` from a laptop, exactly like today's loopback-only Compose binding.
  - Both process groups mount the **same Fly Volume** at `/app/data`, holding `chat_metadata.db` — this is what actually needed to be preserved from 0007, and a same-app, same-image, shared-volume setup gets it for free the way separate apps couldn't. (Fly volumes are per-region/per-machine; both process groups' machines must be placed in the same region for the mount to attach — a one-time `fly.toml`/`fly scale` detail, not an ongoing concern at this traffic level.)
- **`waha` stays its own separate Fly app** (`botsapp-waha`, private) — unaffected by this change. It's a different image entirely (`devlikeapro/waha:dev-arm`, not this repo's `server/Dockerfile`) with its own volumes (`waha-data`/`waha-media`), so folding it into the same app as `mcp-server`/`admin-ui` would solve nothing and only complicate the `fly.toml`.
- Net Fly footprint: **two apps**, not three — `botsapp` (2 process groups) and `botsapp-waha`.

## Consequences

- `chat_metadata.db` keeps working exactly as WAL-mode-shared-file today, deployed or local — no new code, no new internal API, no new auth surface between `mcp-server` and `admin-ui`.
- `admin-ui`'s network-privacy story (0004) is enforced the same way as `waha`'s: simply never declaring a `[[services]]` block for that process group, not a second layer of access control.
- The two process groups still scale/restart independently (Fly process groups support per-group `min_machines_running`, VM size, etc.) despite sharing an app and a volume — this wasn't a scaling constraint, just a deploy-topology one.
- `server/Dockerfile` needs no change — it already builds one image and lets `command:` pick the entrypoint (see the comment in [server/Dockerfile](../../server/Dockerfile)); `fly.toml`'s per-process-group `[processes]` table does the same job Compose's `command:` override does today.
- If `admin-ui` ever needs to scale or fail independently of `mcp-server` in a way one shared app can't express, revisit this and split it out per option 1 above — nothing here forecloses that later.
