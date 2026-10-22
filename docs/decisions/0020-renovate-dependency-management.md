# 0020 — Renovate (self-hosted, via GitHub Actions) for dependency updates

**Status:** Decided (2026-10).

## Context

Dependencies were updated by hand. The drift shows: `ci.yml` pins
`actions/checkout@v4` and `setup-uv@v3` (current majors are far ahead), the
Dockerfile pulls `ghcr.io/astral-sh/uv:latest` unpinned, and `uv.lock` pins
`aiohttp 3.13.5`, which OSV lists two advisories against (PYSEC-2026-2112,
GHSA-hpj7-wq8m-9hgp). Sources of versions in this repo: Python (`pyproject.toml`
+ `uv.lock`, `.python-version`), Docker (`server/Dockerfile`, `docker-compose.yml`,
`waha/fly.toml`), and GitHub Actions.

## Decision

Self-hosted Renovate, run by `.github/workflows/renovate.yml`
(`renovatebot/github-action`), configured in `.github/renovate.json5`, with the
Dependency Dashboard issue enabled. Chosen over the hosted Mend app to keep the
config and its trigger in the repo; the cost is one secret (below).

- **Coverage:** `pep621` (+ `uv.lock`), `pyenv`, `dockerfile`, `docker-compose`,
  `github-actions`, plus two regex managers for the image refs the built-ins
  can't read — `image = "..."` in `waha/fly.toml` and the `${WAHA_IMAGE:-...}`
  default in `docker-compose.yml`. Verified by a local `renovate --platform=local`
  run, not just schema validation.
- **Pinning:** Docker image digests and GitHub Action SHAs are pinned.
- **Python:** `pyproject.toml` keeps `>=` floors; only `uv.lock` moves unless a
  floor must rise (`rangeStrategy: update-lockfile`). Monthly lockfile
  maintenance. Dev tooling is one grouped PR; runtime deps are separate PRs.
- **Held for a human:** `fastmcp`/`mcp` (grouped, never automerge), the Python
  interpreter (needs `requires-python` and `[tool.ty]` updated together), and
  `devlikeapro/waha` (moving tags; engine changes have broken the mapper before,
  see [0019](0019-noweb-aware-waha-mapping.md)) all require a tick on the
  dashboard before a PR opens.
- **Cadence:** PRs open weekly (Monday, before 06:00 UTC); security updates
  ignore the schedule. No automerge anywhere yet.
- **CI:** `renovate-config-validator --strict` runs on PRs that touch the config.

## Setup required (not in the repo)

Add a `RENOVATE_TOKEN` secret: a fine-grained PAT or GitHub App token with
contents, pull-requests, issues and workflows write on this repo. It must not be
`GITHUB_TOKEN` — PRs opened with that don't trigger the CI workflow. Issues must
be enabled for the dashboard. Without the secret the scheduled job fails and
nothing else is affected.

## Not decided here

Automerge for patch updates (needs required status checks on `main`), and
pinning `uv` in the Dockerfile to a version instead of `latest`.
