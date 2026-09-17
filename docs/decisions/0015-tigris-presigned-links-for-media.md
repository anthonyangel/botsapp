# 0015 — Video/documents leave get_media() as a Tigris presigned link, not an embedded blob

**Status:** Decided (2026-09).

## Context

`get_media()` returns Image/Audio content blocks for images and voice notes —
MCP has first-class types for both. Video and documents (PDFs, Office files,
...) don't have one, so they fell back to a generic embedded resource
(`File`), carrying the real mimetype and, when WAHA supplies one, a real
filename (60a8784).

That fallback is correct per the MCP spec but not reliably usable in
practice. Confirmed live: a real PowerPoint attachment, fetched through
`get_media()` via one MCP client, came back to the user as `Resources of
type 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
are not currently supported` — the *client* refused to render the embedded
blob at all, based on its mimetype. The same message_id, fetched in a
different session/client, succeeded and saved the file fine. So this isn't a
bug in this server's output (the resource was correctly typed and named) —
it's that MCP clients vary in which embedded-resource mimetypes they're
willing to render, and there's no server-side fix for a client's own
allowlist.

Two ways to sidestep a client's blob-rendering allowlist entirely:
1. **Extract readable content** (e.g. slide text for `.pptx` via
   `python-pptx`) and return that as text, mirroring how `transcription.py`
   already attaches a transcript to a voice note. Solves "what does this say"
   but not "hand me the actual file," and needs a parser per format.
2. **A download link.** Every MCP client can pass through a plain text
   string; none of them have to decide how to render one. Format-agnostic —
   works the same for a `.pptx`, a `.mp4`, or anything else that lands in the
   no-dedicated-content-type bucket.

WAHA can't serve as the link target itself — its media endpoint is private
only, always (root `CLAUDE.md`'s architecture table); there's nothing public
to point a link at without a separate store. Fly's Tigris (S3-compatible
object storage, billed through the normal Fly invoice, presigned URLs
built in) fills that gap with the least new infrastructure: `fly storage
create` provisions a bucket and sets `BUCKET_NAME`/`AWS_ACCESS_KEY_ID`/
`AWS_SECRET_ACCESS_KEY`/`AWS_ENDPOINT_URL_S3` directly on the app as
secrets, and any S3 SDK (`boto3`) works against it with `region_name="auto"`.

Cost is negligible at this project's scale: $0.02/GB/month storage (first
5GB/month free), ~$0.005/1,000 PUT + ~$0.0005/1,000 GET requests (first
10,000/100,000 free), zero egress fees. Files here are a few MB and deleted
within 15 minutes of upload — this stays inside the free tier by a wide
margin for family-scale traffic.

## Decision

- `get_media()` (via a new `object_storage.py`) uploads video/document bytes
  to Tigris and returns a **presigned GET URL, valid 15 minutes**, as plain
  text — instead of embedding the blob — whenever Tigris is configured.
- The uploaded object is **deleted 15 minutes after upload**, matching the
  link's own expiry: nothing should outlive the one link anyone was ever
  given to reach it. Deletion is scheduled in-process (`asyncio` background
  task); a process restart inside that window orphans the object. A bucket
  lifecycle rule is worth adding on the Tigris/Fly side as a backstop for
  that edge case, but isn't configured by this code.
- **Opt-in**, via `BUCKET_NAME`/`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/
  `AWS_ENDPOINT_URL_S3` (documented in `.env.example`, unset by default) —
  deliberately the exact names `fly storage create` sets, so its output
  pastes in with no renaming. Not configuring it is fully supported:
  `get_media()` falls back to today's embedded-`File` behavior.
- Like `OPENAI_API_KEY`/`WAHA_API_KEY`, these go through `fly secrets set`
  in production, never `fly.toml`'s `[env]` block.
- Upload failures (missing config, network error, bad credentials) are
  silent and non-fatal — logged, not raised — mirroring
  `transcription.py`'s contract: a link is a bonus delivery mechanism, never
  a dependency `get_media()` needs to succeed.
- Text extraction (option 1 above) was considered and set aside for now,
  not ruled out — it solves a different problem (readable content for the
  model) than this one (handing over the actual file), and can be added
  later as an additional item alongside the link, the same way the audio
  transcript sits alongside the `Audio` block.

## Consequences

- A presigned link is bearer-access: anyone holding the URL can fetch the
  file, no WorkOS/`ALLOWED_EMAILS` login required, for up to 15 minutes.
  That's a real (if narrow and time-boxed) departure from this project's
  usual "nothing is visible until explicitly allowlisted" posture
  ([0004](0004-chat-allowlist-and-admin-ui.md)) — accepted here because the
  window is short, the object is deleted immediately after, and the
  alternative (an embedded blob many clients won't render at all) wasn't a
  functioning read path in the first place.
- Whoever enables this is explicitly letting video/document bytes from an
  allowlisted family chat pass through Fly's Tigris. Like 0014's OpenAI
  trade-off, this is left opt-in and legible rather than assumed.
- New runtime dependency: `boto3`. No new Fly Machine or memory budget —
  the upload is a single outbound HTTPS call, same shape as 0014's
  transcription call.
- Video and documents now behave differently depending on whether Tigris is
  configured (a link vs. an embedded blob) — deliberate, since the blob path
  remains the correct fallback for deployments that don't want any media
  leaving WAHA/this server's boundary at all.
