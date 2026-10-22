# 0021 — Renovate runs on GITHUB_TOKEN when no RENOVATE_TOKEN exists, and fails loudly

**Status:** Decided (2026-10). Supersedes the "must not be `GITHUB_TOKEN`" part of
[0020](0020-renovate-dependency-management.md); the rest of 0020 stands.

## Context

The first run after 0020 merged failed in 10s: `RENOVATE_TOKEN` didn't exist. The
workflow was then switched to `GITHUB_TOKEN` directly, and the next run went
green — but did nothing. The job's `permissions: contents: read` made every
write fail (`Could not ensure issue ... integration-unauthorized`, so no
Dependency Dashboard), yet Renovate exits 0, so the run reported success.

## Decision

- **Token:** `secrets.RENOVATE_TOKEN || secrets.GITHUB_TOKEN`. A PAT/GitHub App
  token is still preferred; with it nothing changes from 0020.
- **GITHUB_TOKEN mode is deliberately reduced:** the job gets
  `contents`/`pull-requests`/`issues: write`; GitHub Actions updates are
  disabled (GitHub never lets `GITHUB_TOKEN` modify `.github/workflows/*`);
  PRs Renovate opens won't trigger CI; and the repo setting *Actions → General →
  "Allow GitHub Actions to create and approve pull requests"* must be on. The
  run prints a warning annotation saying so.
- **A run that can't write fails:** Renovate logs warn+ to a JSON-lines file and
  `.github/scripts/check_renovate_log.py` fails the job on any error-level entry
  or known write failure (`Could not ensure/create/update ...`,
  `integration-unauthorized`, `Resource not accessible`, `refusing to allow`).

## Consequences

Dashboard and Python/Docker updates work without creating a PAT. Without CI on
Renovate PRs, merging them relies on running checks by hand or adding the PAT
later; the warning is the reminder. The log patterns are a heuristic — an
unrecognised write failure at warn level would still pass.
