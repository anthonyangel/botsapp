"""
Unit tests for object_storage.py's upload_and_presign() — the best-effort
Tigris upload get_media() uses to hand back a presigned link instead of an
embedded resource blob for documents/video.

boto3 is a synchronous SDK wrapped in asyncio.to_thread, not aiohttp, so
this mocks the boto3 client directly rather than using aresponses (see
test_waha_client.py's module docstring for why aresponses is otherwise
this project's default for HTTP mocking).
"""

import asyncio
from unittest.mock import MagicMock, patch

from botsapp.object_storage import upload_and_presign

ENV = {
    "BUCKET_NAME": "test-bucket",
    "AWS_ACCESS_KEY_ID": "test-key-id",
    "AWS_SECRET_ACCESS_KEY": "test-secret",
    "AWS_ENDPOINT_URL_S3": "https://fly.storage.tigris.dev",
}


def _clear_tigris_env(monkeypatch):
    for key in (*ENV, "AWS_REGION"):
        monkeypatch.delenv(key, raising=False)


async def test_returns_none_when_not_configured(monkeypatch):
    _clear_tigris_env(monkeypatch)
    result = await upload_and_presign(b"fake-bytes", "file.pptx", "application/octet-stream")
    assert result is None
    # No boto3 client should even be constructed — nothing to assert on
    # since nothing was mocked in, but a real network call here (there's
    # no mock target) would raise rather than silently succeed.


async def test_uploads_and_returns_presigned_url(monkeypatch):
    _clear_tigris_env(monkeypatch)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)

    mock_client = MagicMock()
    mock_client.generate_presigned_url.return_value = "https://fly.storage.tigris.dev/signed-url"
    with patch("boto3.client", return_value=mock_client) as mock_boto3_client:
        result = await upload_and_presign(
            b"fake-bytes", "Q4 Slides.pptx", "application/vnd.ms-powerpoint"
        )

    assert result == "https://fly.storage.tigris.dev/signed-url"
    mock_boto3_client.assert_called_once_with(
        "s3",
        endpoint_url="https://fly.storage.tigris.dev",
        aws_access_key_id="test-key-id",
        aws_secret_access_key="test-secret",
        region_name="auto",
    )
    put_kwargs = mock_client.put_object.call_args.kwargs
    assert put_kwargs["Bucket"] == "test-bucket"
    assert put_kwargs["Body"] == b"fake-bytes"
    assert put_kwargs["ContentType"] == "application/vnd.ms-powerpoint"
    assert put_kwargs["Key"].endswith("/Q4 Slides.pptx")

    presign_kwargs = mock_client.generate_presigned_url.call_args
    assert presign_kwargs.args[0] == "get_object"
    assert presign_kwargs.kwargs["Params"]["Bucket"] == "test-bucket"
    assert presign_kwargs.kwargs["ExpiresIn"] == 15 * 60

    # Let the scheduled delete-after-TTL task get created (and immediately
    # cancel it) rather than actually sleeping 15 minutes in a test.
    await asyncio.sleep(0)
    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            task.cancel()


async def test_returns_none_and_does_not_raise_when_upload_fails(monkeypatch):
    _clear_tigris_env(monkeypatch)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)

    mock_client = MagicMock()
    mock_client.put_object.side_effect = RuntimeError("network error")
    with patch("boto3.client", return_value=mock_client):
        result = await upload_and_presign(b"fake-bytes", "file.pptx", "application/octet-stream")

    assert result is None


async def test_uses_custom_region_when_set(monkeypatch):
    _clear_tigris_env(monkeypatch)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    mock_client = MagicMock()
    mock_client.generate_presigned_url.return_value = "https://example.com/signed"
    with patch("boto3.client", return_value=mock_client) as mock_boto3_client:
        await upload_and_presign(b"fake-bytes", "file.pptx", "application/octet-stream")

    assert mock_boto3_client.call_args.kwargs["region_name"] == "us-east-1"
