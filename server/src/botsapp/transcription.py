"""
Best-effort speech-to-text for voice notes, via OpenAI's transcription API.

Deliberately not local (whisper.cpp/faster-whisper, or any other
in-process model): mcp-server and admin-ui already share a single 512mb
Fly Machine (see docs/decisions/0013-single-fly-app-shared-volume.md)
alongside `waha`'s own separate, already Chromium-heavy Machine — there's
no memory headroom here to load a speech model in-process, and bundling
model weights into this image would work against the same
family-scale/single-host reasoning the rest of this deployment follows
(see e.g. docs/decisions/0002, 0007). A single outbound HTTPS call per
voice note fits that model far better.

Opt-in via OPENAI_API_KEY (unset by default — see .env.example): tools.py's
get_media() always returns the raw audio regardless of whether this is
configured or succeeds. A transcript is a bonus attached alongside it,
never a dependency get_media() needs to succeed.
"""

from __future__ import annotations

import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

_TRANSCRIPTION_URL = "https://api.openai.com/v1/audio/transcriptions"
_MODEL = "whisper-1"


async def transcribe_audio(data: bytes, mimetype: str, filename: str | None = None) -> str | None:
    """Best-effort transcript of an audio clip.

    Returns None — never raises — if OPENAI_API_KEY isn't set, or on any
    request failure. Callers should treat the result as optional.
    """
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    clean_mimetype = mimetype.split(";", 1)[0].strip() or "application/octet-stream"
    subtype = clean_mimetype.partition("/")[2] or "bin"

    form = aiohttp.FormData()
    form.add_field("model", _MODEL)
    form.add_field(
        "file",
        data,
        filename=filename or f"audio.{subtype}",
        content_type=clean_mimetype,
    )
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                _TRANSCRIPTION_URL,
                data=form,
                headers={"Authorization": f"Bearer {api_key}"},
            ) as resp,
        ):
            if resp.status >= 400:
                body = await resp.text()
                logger.warning("Transcription request failed: %s %s", resp.status, body)
                return None
            result = await resp.json(content_type=None)
    except Exception as exc:
        logger.warning("Transcription request failed: %s", exc)
        return None

    text = (result or {}).get("text")
    return text.strip() if text and text.strip() else None
