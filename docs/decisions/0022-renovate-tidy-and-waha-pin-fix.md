# 0022 — Renovate: fewer, grouped PRs; no Python-minor PRs; fix the WAHA digest pin

**Status:** Decided (2026-10). Changes parts of [0020](0020-renovate-dependency-management.md):
the Python grouping/range strategy, the Python-interpreter approval gate, and the
regex managers. The token/exit-code decisions in [0021](0021-renovate-token-fallback.md) stand
(and were confirmed working: run 7 failed loudly on a real error).

## What the first real runs showed

Ticking "create all" on the dashboard opened PRs #16–#25 at once. Eight of them were
per-package `uv.lock`-only updates (boto3, phonenumbers, python-dotenv, pydantic, starlette,
uvicorn, the dev-tooling group, fastmcp) that the monthly **lock-file maintenance** PR (#25)
made redundant — merging #25 autoclosed all of them. Also:

- **#24 (Python 3.15) failed CI:** `beartype` (via `fastmcp` → `py-key-value-aio`) does
  `from typing import no_type_check_decorator`, which Python 3.15 removed. Not an image
  problem; the dependency isn't 3.15-ready.
- **`renovate/waha` failed with "update failure"** (run 7 exited 1 via the 0021 remap).
  The regex manager's `replaceString` is the *whole match* (`image = "devlikeapro/waha:dev"`),
  and with no `autoReplaceStringTemplate` Renovate can only substitute values already in that
  string — it can't add the `@sha256:` of a digest *pin*. Reproduced with Renovate's own
  `doAutoReplace` on a scratch copy: the old config throws `update-failure`; with the template the
  pin and a later digest bump both rewrite the line correctly.

## Decisions

- **One PR for Python minor+patch** (runtime and dev tooling together, "Python dependencies");
  majors stay one PR per package; `fastmcp`/`mcp` always get their own "MCP stack" PR.
- **`rangeStrategy: "bump"`** for pyproject: a minor/patch update edits the `>=` floor in
  `pyproject.toml` (and `uv.lock`) instead of changing only the lockfile.
- **Lock-file maintenance stays monthly** but now only refreshes what the grouped PR doesn't touch.
- **No Python minor/major PRs at all** (`enabled: false` on the `python` package). The old rule
  only hid them behind a dashboard tick, which "create all" bypassed. Bump Python by hand —
  `Dockerfile`, `.python-version`, `requires-python` and `[tool.ty]` together — once the dependency
  set supports it (3.15: not yet).
- **`python`/`uv` image refreshes are grouped** ("Docker base images"); WAHA stays separate and gated.
- **WAHA pin fixed:** `fly.toml` regex manager captures `currentDigest` and sets
  `autoReplaceStringTemplate`. The `docker-compose.yml` `${WAHA_IMAGE:-…dev-arm}` regex manager is
  **removed**: it's a local arm64 dev tag, and its closing `}` can't be expressed after the optional
  digest in the Handlebars template.

## Verified / not verified

Config validates; a local dry run now yields five branches (`waha`, `docker-base-images`,
`mcp-stack`, `python-dependencies`, `lock-file-maintenance`) with the Python-interpreter updates
filtered out as disabled, and pyproject floors shown as `isBump`. The WAHA rewrite was exercised
through `doAutoReplace`, not a real branch push. The `bump` path (Renovate regenerating `uv.lock`
for a changed floor) is Renovate's normal pep621 flow but hasn't run here yet.
