# 0016 — Bound WAHA media lifetime to 7 days; grow the deployed volume to 5GB

**Status:** Decided (2026-09). Supersedes the media-persistence part of
[0007](0007-sqlite-for-metadata-store.md) (`WHATSAPP_FILES_LIFETIME=0`) for
both the deployed Fly app and local Compose; 0007's storage-*backend*
choice (local files over Postgres/S3) still stands.

## Context

The deployed `botsapp-waha` Fly app hit a real outage: its volume
(`initial_size = "1gb"` in [waha/fly.toml](../../waha/fly.toml)) filled to
100% (958M/974M used, 0 available) and WAHA started throwing `Unhandled
Rejection: Error: ENOSPC: no space left on device, write` on any new write.
Reads still worked (cached chat/group list endpoints kept returning 200),
but new incoming media (and likely session-state writes) were failing.

Root cause: `WHATSAPP_FILES_LIFETIME=0` (never expire), set in 0007
specifically so `get_media()` would keep working — WAHA's own default is a
180-second lifetime that deletes media before the MCP tool has any real
chance of being called for it. `0` fixed that, but as an unbounded policy
it inevitably fills a fixed-size volume; at the time of the outage, 1703
files (775M) had accumulated in `/app/data/media` alone.

`get_media()` ([waha_client.py](../../server/src/botsapp/waha_client.py))
has no path to WhatsApp's own servers — it only ever asks WAHA for a file
it has already downloaded and still has a local copy of. There is no
"fetch on demand, don't store" option: once WAHA's copy of a file is gone,
that media is unrecoverable through this server (short of the sender
resending it). Moving to WAHA's S3/Postgres media-storage backends would
still be storage, just off this volume — considered, but out of scope for
this fix (see 0007 on why local files were chosen over those to begin
with); revisit if 5GB proves not enough.

## Decision

- `WHATSAPP_FILES_LIFETIME` changes from `"0"` to `"604800"` (7 days, in
  seconds) in both [waha/fly.toml](../../waha/fly.toml) (deployed) and
  [docker-compose.yml](../../docker-compose.yml) (local) — kept identical
  across environments deliberately, same reasoning as
  `x-bridge-env` sharing config between `mcp-server`/`admin-ui`: no reason
  for local dev and production to silently disagree on media retention.
- The already-full 1GB Fly volume (`vol_v3g1gyndn3g880m4`) is extended to
  5GB via `fly volumes extend` — a live, non-destructive resize (existing
  data untouched); Fly volumes can grow but not shrink, so this isn't
  reversible if 5GB later turns out to be more than needed.
  `waha/fly.toml`'s `initial_size` is also bumped to `"5gb"` so a
  from-scratch volume creation (a fresh region, a rebuild after deleting
  the volume) matches what's actually running, though that field only
  takes effect at volume-creation time and did not itself resize the live
  volume.

## Consequences

- `get_media()` will start returning "WAHA no longer has a copy of it" for
  media older than 7 days, where it previously never expired — a real
  behavior change for the deployed assistant, traded for bounded disk
  growth. 7 days was picked as generous enough for normal
  catch-up-on-messages use without dragging along videos/documents
  indefinitely.
- 5GB at the same accumulation rate that filled 1GB gives meaningfully more
  runway, but is still a fixed ceiling — an unbounded-lifetime message
  volume could refill even 5GB. Worth revisiting (shorter lifetime, or an
  actual S3 backend) if `ENOSPC` recurs rather than growing the volume
  again by reflex.
