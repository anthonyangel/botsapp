"""
Unit tests for transcription.py's transcribe_audio() — the best-effort
ElevenLabs Scribe call get_media() attaches to a voice note.

The ElevenLabs SDK talks over httpx, not aiohttp, so aresponses (see
test_waha_client.py's module docstring) can't intercept it; these tests swap
the SDK client class for a small fake and assert on what it was called with.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from botsapp import transcription
from botsapp.transcription import transcribe_audio


class FakeClient:
    """Stands in for AsyncElevenLabs; records constructor/convert arguments."""

    instances: list[FakeClient] = []
    result: object = SimpleNamespace(text="Hi, it is on the fifth floor.")
    error: Exception | None = None

    def __init__(self, api_key=None, **_kwargs):
        self.api_key = api_key
        self.convert_kwargs: dict = {}
        self.speech_to_text = SimpleNamespace(convert=self._convert)
        FakeClient.instances.append(self)

    async def _convert(self, **kwargs):
        self.convert_kwargs = kwargs
        if FakeClient.error is not None:
            raise FakeClient.error
        return FakeClient.result


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeClient.instances = []
    FakeClient.result = SimpleNamespace(text="Hi, it is on the fifth floor.")
    FakeClient.error = None
    monkeypatch.setattr(transcription, "AsyncElevenLabs", FakeClient)
    return FakeClient


async def test_returns_none_when_no_api_key_configured(monkeypatch, fake_client):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    result = await transcribe_audio(b"fake-audio-bytes", "audio/ogg; codecs=opus")
    assert result is None
    # No client was even constructed, proving no request was attempted.
    assert fake_client.instances == []


async def test_returns_transcript_text_on_success(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    result = await transcribe_audio(b"fake-audio-bytes", "audio/ogg; codecs=opus")
    assert result == "Hi, it is on the fifth floor."


async def test_strips_whitespace_from_transcript(monkeypatch, fake_client):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    fake_client.result = SimpleNamespace(text=" Hello there. ")
    assert await transcribe_audio(b"fake-audio-bytes", "audio/ogg") == "Hello there."


async def test_passes_key_model_and_plain_text_options(monkeypatch, fake_client):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    await transcribe_audio(b"fake-audio-bytes", "audio/ogg; codecs=opus", "note.ogg")
    client = fake_client.instances[0]
    assert client.api_key == "test-key"
    kwargs = client.convert_kwargs
    assert kwargs["model_id"] == "scribe_v2"
    assert kwargs["file"] == ("note.ogg", b"fake-audio-bytes", "audio/ogg")
    assert kwargs["diarize"] is False
    assert kwargs["tag_audio_events"] is False
    assert kwargs["timestamps_granularity"] == "none"
    # Language is left unset so ElevenLabs auto-detects it.
    assert "language_code" not in kwargs


async def test_derives_filename_from_mimetype_when_missing(monkeypatch, fake_client):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    await transcribe_audio(b"fake-audio-bytes", "audio/ogg; codecs=opus")
    assert fake_client.instances[0].convert_kwargs["file"][0] == "audio.ogg"


async def test_returns_none_on_request_error(monkeypatch, fake_client):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    fake_client.error = RuntimeError("401 invalid key")
    assert await transcribe_audio(b"fake-audio-bytes", "audio/ogg") is None


async def test_returns_none_on_empty_transcript(monkeypatch, fake_client):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    fake_client.result = SimpleNamespace(text="  ")
    assert await transcribe_audio(b"fake-audio-bytes", "audio/ogg") is None


async def test_returns_none_for_response_without_text(monkeypatch, fake_client):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    # e.g. a multichannel or webhook-accepted response has no top-level text.
    fake_client.result = SimpleNamespace(transcripts=[])
    assert await transcribe_audio(b"fake-audio-bytes", "audio/ogg") is None
