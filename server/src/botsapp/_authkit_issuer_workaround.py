"""
ASGI workaround for an upstream fastmcp/mcp-SDK bug — remove once fixed.

`AuthKitProvider` advertises AuthKit's issuer with a trailing slash in
`authorization_servers` (a `pydantic.AnyHttpUrl` normalization artifact,
confirmed via source read of `fastmcp/server/auth/providers/workos.py` and
`mcp/server/auth/routes.py`), but WorkOS's real `issuer` has none. Every real
MCP client's strict RFC 8414 issuer check
(`mcp.client.auth.oauth2.validate_metadata_issuer`) then rejects login with
"Authorization server metadata issuer mismatch". Tracked upstream at
https://github.com/modelcontextprotocol/python-sdk/issues/1919 — a real bug
(reproduced against a live IdP, not botsapp-specific), with two PRs already
closed unmerged and two still open as of 2026-09.

This rewrites the JSON body of `/.well-known/oauth-protected-resource*`
responses only, stripping a trailing "/" from `resource` and every
`authorization_servers` entry. Nothing else is touched — no fastmcp/mcp
internals are patched, no other route is affected.
"""

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_TARGET_PATH_PREFIX = "/.well-known/oauth-protected-resource"


def _strip_trailing_slashes(data: dict) -> dict:
    if isinstance(data.get("resource"), str):
        data["resource"] = data["resource"].rstrip("/")
    servers = data.get("authorization_servers")
    if isinstance(servers, list):
        data["authorization_servers"] = [
            s.rstrip("/") if isinstance(s, str) else s for s in servers
        ]
    return data


class FixAuthkitIssuerTrailingSlashMiddleware:
    """Strips the trailing slash fastmcp's AuthKitProvider adds to
    `authorization_servers` (and, for safety, `resource`) in protected
    resource metadata responses. See module docstring."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(
            _TARGET_PATH_PREFIX
        ):
            await self.app(scope, receive, send)
            return

        start_message: dict | None = None
        body_chunks: list[bytes] = []

        async def send_wrapper(message: Message) -> None:
            nonlocal start_message
            if message["type"] == "http.response.start":
                # Deferred until the body is known, so content-length can be
                # corrected if we end up rewriting it.
                start_message = message
                return
            if message["type"] != "http.response.body":
                await send(message)
                return

            body_chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return

            body = b"".join(body_chunks)
            assert start_message is not None
            headers = list(start_message.get("headers", []))
            if start_message.get("status", 200) == 200:
                try:
                    data = json.loads(body)
                    if isinstance(data, dict):
                        body = json.dumps(_strip_trailing_slashes(data)).encode()
                        headers = [
                            (k, v) for k, v in headers if k.lower() != b"content-length"
                        ]
                        headers.append((b"content-length", str(len(body)).encode()))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass  # not JSON (e.g. an error page) — pass through unmodified

            await send({**start_message, "headers": headers})
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, send_wrapper)
