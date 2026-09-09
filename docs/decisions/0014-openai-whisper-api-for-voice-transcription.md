# 0014 — Transcribe voice notes via OpenAI's transcription API, not a local model

**Status:** Decided (2026-09).

## Context

`get_media()` was fixed (same session, no separate record — see the commit) to
actually fetch media from WAHA instead of reading a local disk cache that
nothing ever populated. That fix returns a voice note as an MCP `Audio`
content block: raw bytes + mimetype.

That's necessary but not sufficient. `Audio` is for *playback* — nothing
about MCP's content-block spec, or this server, gets those bytes understood
by the model calling the tool. Confirmed live: a real Claude Code session
that called the fixed `get_media()` on an actual voice note received "Binary
content (audio/ogg, 187KB) saved to a file" and nothing else — no transcript,
no ability to say what was said. The model doesn't take raw audio as an input
modality; a saved file is not something it can read. So for the stated goal —
Claude being able to tell you what's in a voice note, the way it already can
for a text message — retrieving the bytes doesn't get there. Something needs
to convert speech to text.

Two shapes for that:
1. **Local**, in-process (whisper.cpp, faster-whisper, or similar) —
   entirely private, no third party ever sees the audio, no new secret.
2. **An external API call** (OpenAI's transcription endpoint, or a
   comparable provider) — near-zero compute/memory cost on this side, but
   the audio bytes leave this server to a third party, and it needs a new
   secret.

Normally (1) would be the more private default for a family WhatsApp
server. It doesn't fit *this* deployment topology, though: `mcp-server` and
`admin-ui` already share one 512mb `shared-cpu-1x` Fly Machine
([0013](0013-single-fly-app-shared-volume.md)), and `waha` needs its own
separate, already Chromium-heavy Machine just to run headless WhatsApp Web
([0002](0002-flyio-hosting-mechanics.md) region, docker-compose.yml's
`shm_size: "2gb"` for it). There's no memory headroom on the mcp-server/
admin-ui Machine to load a speech model in-process, and bundling model
weights into `server/Dockerfile` works against the same
family-scale/single-host reasoning the rest of this deployment already
follows (0002, [0007](0007-sqlite-for-metadata-store.md)). A single outbound
HTTPS call per voice note costs this Machine nothing.

## Decision

- `get_media()` (via a new `transcription.py`) calls **OpenAI's
  `/v1/audio/transcriptions` endpoint** (`whisper-1`) with the fetched audio
  bytes, and — when it returns a transcript — attaches that transcript as a
  second content item alongside the `Audio` block, rather than replacing it
  (a human can still get the actual audio; the model gets text it can read).
- **Opt-in, via `OPENAI_API_KEY`** (documented in `.env.example`, unset by
  default). Not configuring it is a fully supported choice — `get_media()`
  still returns the raw audio either way; the only difference is whether a
  transcript comes with it. This is deliberately a privacy-relevant
  decision left to whoever runs this deployment, not assumed.
- Like `WAHA_API_KEY`/`ALLOWED_EMAILS`, `OPENAI_API_KEY` goes through
  `fly secrets set` in production, never `fly.toml`'s `[env]` block.
- Transcription failures (no key, request error, empty result) are silent
  and non-fatal — logged, not raised. A voice note must still come back
  successfully with no `OPENAI_API_KEY` set, or if OpenAI's API is briefly
  down; a transcript is a bonus, never a dependency for `get_media()` to
  succeed.

## Consequences

- Whoever enables this is explicitly sending voice-note audio from an
  allowlisted family chat to OpenAI. That's the actual privacy trade-off
  this record exists to make legible — it doesn't happen unless
  `OPENAI_API_KEY` is deliberately set.
- No new Fly Machine, no memory budgeting, no model weights in the image —
  the `mcp-server`/`admin-ui` Machine's resource footprint from 0013 is
  unchanged.
- If this Machine's memory budget ever grows enough to fit local inference,
  or a strict "never send family data to a third party" requirement shows
  up, revisit toward a local model — nothing here forecloses that; it
  simply wasn't the fit for the deployment as it stands today.
