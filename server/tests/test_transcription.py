"""
Unit tests for transcription.py's transcribe_audio() — the best-effort
OpenAI-transcription-API call get_media() attaches to a voice note.

Uses aresponses (see test_waha_client.py's module docstring for why, over
aioresponses) against the real "api.openai.com" host — transcribe_audio()
doesn't take a configurable base_url the way WAHAClient does, so there's no
BASE_URL indirection here.
"""

from botsapp.transcription import transcribe_audio

HOST = "api.openai.com"
PATH = "/v1/audio/transcriptions"


async def test_returns_none_when_no_api_key_configured(monkeypatch, aresponses):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = await transcribe_audio(b"fake-audio-bytes", "audio/ogg; codecs=opus")
    assert result is None
    # No aresponses mock registered — a network call here would fail the
    # test outright, proving no request was made without an API key.


async def test_returns_transcript_text_on_success(monkeypatch, aresponses):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    aresponses.add(
        HOST,
        PATH,
        "post",
        response=aresponses.Response(
            status=200,
            text='{"text": "Hi, it is on the fifth floor."}',
            content_type="application/json",
        ),
    )
    result = await transcribe_audio(b"fake-audio-bytes", "audio/ogg; codecs=opus")
    assert result == "Hi, it is on the fifth floor."


async def test_sends_bearer_auth_header(monkeypatch, aresponses):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = {}

    async def handler(request):
        captured["auth"] = request.headers.get("Authorization")
        return aresponses.Response(
            status=200, text='{"text": "hi"}', content_type="application/json"
        )

    aresponses.add(HOST, PATH, "post", response=handler)
    await transcribe_audio(b"fake-audio-bytes", "audio/ogg")
    assert captured["auth"] == "Bearer test-key"


async def test_returns_none_on_error_status(monkeypatch, aresponses):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    aresponses.add(
        HOST,
        PATH,
        "post",
        response=aresponses.Response(status=401, text='{"error": "invalid key"}'),
    )
    result = await transcribe_audio(b"fake-audio-bytes", "audio/ogg")
    assert result is None


async def test_returns_none_on_empty_transcript(monkeypatch, aresponses):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    aresponses.add(
        HOST,
        PATH,
        "post",
        response=aresponses.Response(
            status=200, text='{"text": "  "}', content_type="application/json"
        ),
    )
    result = await transcribe_audio(b"fake-audio-bytes", "audio/ogg")
    assert result is None
