# 0009 — Replace aioresponses with aresponses for aiohttp test mocking

**Status:** Decided (2026-09).

## Context

`test_waha_client.py` mocked WAHAClient's outgoing `aiohttp` calls with `aioresponses`, which works by monkeypatching `aiohttp.ClientSession._request` and constructing a fake `aiohttp.ClientResponse` object by hand to return in its place. That object's constructor is aiohttp-internal, not part of aiohttp's public API — aiohttp 3.14 changed its signature (added a required keyword-only `stream_writer`), and every one of `aioresponses`' 28 affected tests started failing with `ClientResponse.__init__() missing 1 required keyword-only argument: 'stream_writer'`. `aioresponses` has had no release since 0.7.9 (its last, already over a year old) to track the change, so pinning `aiohttp<3.14` was the only way to keep the suite green — a real constraint blocking normal dependency upgrades, not a cosmetic one.

## Options considered

| Option | Verdict |
|---|---|
| Pin `aiohttp<3.14` indefinitely | Works today, but freezes a core dependency solely to accommodate an unmaintained test library — the exact tradeoff this record exists to avoid. |
| [pook](https://github.com/h2non/pook) | Has its own documented aiohttp compatibility issues ([h2non/pook#37](https://github.com/h2non/pook/issues/37)); likely does its own internals-patching, so no reason to expect it's any more durable than aioresponses against future aiohttp changes. |
| Python Mocket (socket-level mocking) | Would dodge this exact failure mode (it intercepts below aiohttp entirely) but at the cost of a much larger rewrite — registrations are by raw socket address, not URL/method, a poor match for this suite's per-endpoint mocks. |
| **[aresponses](https://github.com/aresponses/aresponses) (chosen)** | Runs a real local `aiohttp.web` test server and redirects DNS resolution to it for the test's duration — aiohttp builds its own request/response objects normally, so it can't break this way again. API (`aresponses.add(host, path, method, response=...)`, a pytest fixture) is close enough to `aioresponses`' to port mechanically. |

## Decision

- `test_waha_client.py` now uses the `aresponses` pytest fixture (auto-registered via its `pytest11` entry point — no explicit import needed) instead of a locally-defined `aioresponses()`-backed `mock_http` fixture.
- A `_mock(ar, method, url, *, payload=None, status=200, body=None, content_type=None)` helper keeps call sites close to the old `mock_http.get(url, payload=...)` shape. `aresponses` matches `path_qs` as a literal string when `match_querystring=True` — aiohttp's server-side request decodes percent-escapes before exposing it, so a plain, unescaped query string (e.g. `contactId=155@c.us`) matches the real wire request exactly, the same as it did under `aioresponses`' structural comparison.
- Tests that need to inspect what was actually sent (headers, JSON body, or just "was this endpoint hit") use a small `_Capture` class instead of `aioresponses.requests`. Its `handle` method is a bound `async def`, not a callable class instance or a plain function with an attribute bolted on — `aresponses` decides whether to `await` a response callable via `asyncio.iscoroutinefunction`, which (unlike `__call__` on a class instance) correctly recognizes a bound async method.
- `test_get_session_status_unreachable` (simulating a genuine connection failure, as opposed to an HTTP error status) doesn't use the `aresponses` fixture at all: `aresponses` redirects DNS resolution for *every* aiohttp connection made during a test to its local mock server, so there's no way to ask it to simulate an unreachable host from inside its own context. Pointing a throwaway `WAHAClient` at `http://127.0.0.1:1` (nothing listens there) gets a real, immediate `ConnectionRefusedError` instead — simpler and more genuine than asking a mocking library to fake one.
- The `aiohttp<3.14` pin is removed.

## Consequences

- `aiohttp` (and every other dependency) can be upgraded normally again; nothing in this project's own test suite constrains it anymore.
- `aresponses` was last released January 2024, which is a feature (it doesn't need to track aiohttp's internal API the way `aioresponses` did) rather than a concern here — its mechanism doesn't depend on aiohttp's internals staying stable.
- Regenerating `uv.lock` for this swap did a full fresh dependency resolution rather than an in-place update, which separately picked up an `mcp`/`fastmcp` release that renamed `mcp.types.Icon`'s `mimeType` field to `mime_type` — caught by `ty` (`pydantic-discarded-extra-argument`) and fixed in the same pass (`app.py`'s `_WHATSAPP_ICON` construction). Unrelated to the mocking-library swap itself, but a reminder that a full re-lock is worth a `ty check` pass regardless of what motivated it.
