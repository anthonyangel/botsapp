"""
Best-effort presigned-download links for media that shouldn't be embedded
directly in an MCP tool result, via Fly.io Tigris (S3-compatible).

get_media() in tools.py hands back documents/video as a generic embedded
resource blob (no dedicated MCP content type for them, unlike Image/Audio)
— and some MCP clients flatly refuse to render an embedded resource whose
mimetype they don't recognize (confirmed live: a PowerPoint attachment
came back "Resources of type '...' are not currently supported" from one
client, while the same blob was accepted fine by another). A presigned
link sidesteps that entirely: the client just gets a URL string, never a
blob it has to decide how to render.

WAHA itself can't serve this role — its media endpoint is private-only,
always (see the root CLAUDE.md's architecture table), so there's nothing
public to point a link at without a separate store.

Opt-in via BUCKET_NAME/AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/
AWS_ENDPOINT_URL_S3 (unset by default — see .env.example) — these are
exactly the secret names `fly storage create` sets on the app, so no
renaming is needed. Mirrors transcription.py's contract: get_media() must
keep working (falling back to the embedded blob) whether or not this is
configured or succeeds.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

logger = logging.getLogger(__name__)

_LINK_TTL_SECONDS = 15 * 60

# Keeps references to in-flight delete-after-TTL tasks so they aren't
# garbage-collected mid-sleep — asyncio only holds a weak reference to a
# task once nothing else does (see asyncio.create_task's own docs).
_pending_deletes: set[asyncio.Task[None]] = set()


def _client_and_bucket():
    bucket = os.environ.get("BUCKET_NAME", "").strip()
    access_key = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3", "").strip()
    if not (bucket and access_key and secret_key and endpoint):
        return None, None

    import boto3  # local import: only needed on this opt-in path

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=os.environ.get("AWS_REGION", "auto"),
    )
    return client, bucket


async def upload_and_presign(data: bytes, filename: str, content_type: str) -> str | None:
    """Upload `data` to Tigris and return a presigned GET URL, valid 15 minutes.

    Returns None — never raises — if object storage isn't configured, or
    if the upload itself fails; callers should fall back to embedding the
    data directly. The object is deleted after the same 15 minutes so
    nothing outlives the one link anyone was ever given to reach it. A
    process restart during that window orphans the object (there's no
    cross-restart cleanup here) — a bucket lifecycle rule is worth adding
    as a backstop for that edge case, but isn't set up by this code.
    """
    client, bucket = _client_and_bucket()
    if client is None:
        return None

    key = f"{uuid.uuid4()}/{filename}"
    try:
        await asyncio.to_thread(
            client.put_object,
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=_LINK_TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning("Tigris upload failed, falling back to embedded blob: %s", exc)
        return None

    task = asyncio.create_task(_delete_after_ttl(client, bucket, key))
    _pending_deletes.add(task)
    task.add_done_callback(_pending_deletes.discard)
    return url


async def _delete_after_ttl(client, bucket: str, key: str) -> None:
    await asyncio.sleep(_LINK_TTL_SECONDS)
    try:
        await asyncio.to_thread(client.delete_object, Bucket=bucket, Key=key)
    except Exception as exc:
        logger.warning("Failed to delete expired Tigris object %s: %s", key, exc)
