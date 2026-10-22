# 0021 — Renovate runs on GITHUB_TOKEN when no RENOVATE_TOKEN exists, and fails loudly

**Status:** Decided (2026-10). Supersedes the "must not be `GITHUB_TOKEN`" part of
[0020](0020-renovate-dependency-management.md); the rest of 0020 stands.

## Context

The first run after 0020 merged failed in 10s: `RENOVATE_TOKEN` didn't exist. The
workflow was then switched to `GITHUB_TOKEN` directly, and the next run went
green — but did nothing. The job's `permissions: contents: read` made every
write fail (`Could not ensure issue ... integration-unauthorized`, so no
Dependency Dashboard), yet Renovate exits 0 for warnings, so the run reported success.

## Decision

- **Token:** `secrets.RENOVATE_TOKEN || secrets.GITHUB_TOKEN`. A PAT/GitHub App
  token is still preferred; with it nothing changes from 0020.
- **GITHUB_TOKEN mode is deliberately reduced:** the job gets
  `contents`/`pull-requests`/`issues: write`; GitHub Actions updates are
  disabled (GitHub never lets `GITHUB_TOKEN` modify `.github/workflows/*`);
  PRs Renovate opens won't trigger CI; and the repo setting *Actions → General →
  "Allow GitHub Actions to create and approve pull requests"* must be on. The
  run prints a warning annotation saying so.
- **A run that can't write fails — via Renovate's exit code, no wrapper.**
  Renovate exits non-zero only when an *error-level* entry was logged
  (renovatebot/renovate#1338, #3605 made that so), and its write failures are
  logged as *warnings*. `logLevelRemap` in `.github/renovate.json5` promotes the
  GitHub write-failure messages (`Could not ensure issue`, `Error updating
  branch`, `Error updating PR`, `Error ensuring comment`) to errors, so the
  action step fails on its own. Checked locally: a remapped warning logs as
  ERROR and the exit code flips 0 → 1. (An earlier draft parsed Renovate's log
  file in a script; dropped in favour of this.)

## Consequences

Dashboard and Python/Docker updates work without creating a PAT. Without CI on
Renovate PRs, merging them relies on running checks by hand or adding the PAT
later; the warning is the reminder. The remap list is
hand-picked from Renovate's source (v42/v44): a write failure logged under a
message not on it would still exit 0, and a Renovate release that rewords one
of these messages silently stops matching. Review the list when bumping Renovate.
