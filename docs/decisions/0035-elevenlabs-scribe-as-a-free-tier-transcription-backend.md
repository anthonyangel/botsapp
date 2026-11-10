# 0035 — ElevenLabs Scribe replaces OpenAI whisper-1 for voice-note transcription

**Status:** Decided (2026-11). Supersedes [0014](0014-openai-whisper-api-for-voice-transcription.md)'s
provider choice (OpenAI `whisper-1`, `OPENAI_API_KEY`). Its other decisions carry over
unchanged: a hosted API rather than a local model, opt-in via a key that is unset by default,
a transcript attached alongside the audio, silent non-fatal failures, secrets via
`fly secrets set`.

## Context

0014 transcribes voice notes with OpenAI's `whisper-1`. The ask now is for something free at
family-scale volume. Researched from ElevenLabs' official pages (2026-11):

- **Free tier:** the API pricing page's STT "Hours included" column shows about 4.5 hours for
  the first (inferred Free) row. The table's tiers are unlabeled, so this is inferred, and the
  page never states free-plan API access outright (an API key was issuable on a fresh account).
- **Price beyond free:** $0.20/hour for batch Scribe (v2, v3). Realtime models are $0.22/hour
  and not relevant here.
- **Input:** OGG and Opus are listed as supported formats, so no transcoding. Limits (5 GB,
  15 hours) are far above a voice note.
- **Language:** auto-detected when `language_code` is omitted; the response returns
  `language_code` and `language_probability`; 90+ languages. Scribe does not translate, which
  suits this server staying non-agentic (the calling Claude translates the transcript).
- **Privacy:** no better than OpenAI for a standard account. Data is retained by default (no
  STT-specific period documented); Zero Retention Mode (`enable_logging=false`) is
  enterprise-only; the privacy policy says personal information is used to train models, with
  an account-level opt-out whose coverage of API traffic is unconfirmed.

Not confirmed: the `model_id` string for Scribe v3 (the docs show only `scribe_v2`), free-tier
retention/training differences, and the effect of `enable_logging=false` on non-enterprise
accounts.

## Decision

- `transcription.py` calls ElevenLabs Scribe through the official `elevenlabs` Python SDK
  (`AsyncElevenLabs().speech_to_text.convert`, `model_id="scribe_v2"`, plain text only:
  diarization, audio-event tags and timestamps off, language left unset for auto-detection).
- **Opt-in via `ELEVENLABS_API_KEY`** (documented in `.env.example`, unset by default).
  `OPENAI_API_KEY` is no longer read; the OpenAI code path is removed rather than kept as a
  fallback.
- Failures (no key, request error, empty or text-less response) stay silent and non-fatal;
  `get_media()` still always returns the raw audio.

## Consequences

- At family-scale volume this can cost nothing, within the free allowance.
- The privacy trade-off 0014 made legible is unchanged, not improved: whoever sets the key
  sends allowlisted voice-note audio to a third party, and the stricter retention controls
  need an enterprise plan. A deployment with a strict no-third-party requirement should leave
  the key unset (or revisit a local model, per 0014).
- Adds the `elevenlabs` dependency (and its `httpx`/`requests` tree) to the image.
- A deployment that had `OPENAI_API_KEY` set silently loses transcripts until it sets
  `ELEVENLABS_API_KEY`.
- If ElevenLabs' free allowance or terms change, the provider is isolated to
  `transcription.py`, so swapping again is a small change plus a new record.
