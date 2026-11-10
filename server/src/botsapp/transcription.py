"""
Best-effort speech-to-text for voice notes, via the ElevenLabs Scribe API
(official `elevenlabs` SDK). See
docs/decisions/0035-elevenlabs-scribe-as-a-free-tier-transcription-backend.md
for why ElevenLabs (it has a free tier) and how it replaced OpenAI's whisper-1
from docs/decisions/0014.

Deliberately not local (whisper.cpp/faster-whisper, or any other
in-process model): mcp-server and admin-ui already share a single 512mb
Fly Machine (see docs/decisions/0013-single-fly-app-shared-volume.md)
alongside `waha`'s own separate, already Chromium-heavy Machine — there's
no memory headroom here to load a speech model in-process, and bundling
model weights into this image would work against the same
family-scale/single-host reasoning the rest of this deployment follows
(see e.g. docs/decisions/0002, 0007). A single outbound HTTPS call per
voice note fits that model far better.

Opt-in via ELEVENLABS_API_KEY (unset by default — see .env.example): tools.py's
get_media() always returns the raw audio regardless of whether this is
configured or succeeds. A transcript is a bonus attached alongside it,
never a dependency get_media() needs to succeed.
"""

from __future__ import annotations

import logging
import os

from elevenlabs.client import AsyncElevenLabs

logger = logging.getLogger(__name__)

# The model_id the ElevenLabs API reference shows. The docs list no enum of
# allowed values, so the id for a newer model (e.g. Scribe v3) is unconfirmed.
_MODEL = "scribe_v2"


async def transcribe_audio(data: bytes, mimetype: str, filename: str | None = None) -> str | None:
    """Best-effort transcript of an audio clip.

    Returns None — never raises — if ELEVENLABS_API_KEY isn't set, or on any
    request failure. Callers should treat the result as optional.
    """
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        return None

    clean_mimetype = mimetype.split(";", 1)[0].strip() or "application/octet-stream"
    subtype = clean_mimetype.partition("/")[2] or "bin"

    try:
        client = AsyncElevenLabs(api_key=api_key)
        result = await client.speech_to_text.convert(
            model_id=_MODEL,
            file=(filename or f"audio.{subtype}", data, clean_mimetype),
            # Plain text only: the server doesn't use speakers, audio-event
            # tags or timestamps, and they'd just add noise to the transcript.
            diarize=False,
            tag_audio_events=False,
            timestamps_granularity="none",
        )
    except Exception as exc:
        logger.warning("Transcription request failed: %s", exc)
        return None

    # The response is a union (single-channel, multichannel, webhook-accepted);
    # only the single-channel shape carries top-level text.
    text = getattr(result, "text", None)
    return text.strip() if isinstance(text, str) and text.strip() else None
