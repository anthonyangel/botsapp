# 0023 — WAHA: NOWEB engine everywhere, on version-pinned images (no `dev` tags)

**Status:** Decided (2026-10). Supersedes: the `dev-arm` / "stable `arm` has a QR bug" note in
[0007](0007-sqlite-for-metadata-store.md) and `docker-compose.yml`; the "WEBJS is deliberate" comment in
`docker-compose.yml`; the "temporary diagnostic swap" caveat in `waha/fly.toml`; and, in
[0022](0022-renovate-tidy-and-waha-pin-fix.md), the digest-pin template for `fly.toml` and dropping the
compose regex manager (both replaced below).

## Context

Fly ran `devlikeapro/waha:dev` and local Compose ran `devlikeapro/waha:dev-arm` with the **WEBJS** engine,
while Fly had switched to **NOWEB**. The `dev` tags were chosen for two reasons that no longer hold:

- The stable `arm` tag (2026.8.2) failed QR pairing with `Cmd.refreshQR is not a function` — a WEBJS
  (headless Chromium) problem. `dev-arm` (2026.9.1) worked. Between those two releases WAHA bumped bundled
  Chrome 142 → 152 and puppeteer 24 → 25 (2026-09-11); both are still in 2026.9.2. That is the likely
  fix, inferred from the diff — no WAHA commit or changelog entry mentions it.
- On Fly, `dev` was simply the first tag that built on amd64.

As of 2026-10-22 on Docker Hub, `dev` (amd64, 2026-10-16) and `dev-arm` (arm64) are **2026.9.2 plus one commit**:
`fix(NOWEB): count rows in sqlite` (#2306), a PostgreSQL-only query error in the key-value store; this project
uses SQLite. Stable, dated tags exist for every flavour, including `noweb-<version>` (amd64) and
`noweb-arm-<version>` (arm64).

## Decision

- **Engine:** NOWEB in both places, so local matches production. The Chromium-specific concerns (the QR bug,
  `shm_size`, Chromium memory sizing) don't apply to NOWEB.
- **Images:** version-pinned, NOWEB-only. Fly: `devlikeapro/waha:noweb-2026.9.2`. Compose default:
  `devlikeapro/waha:noweb-arm-2026.9.2` (override `WAHA_IMAGE` with `noweb-<version>` on x86_64).
- **Renovate tracks both** with regex versioning scoped to each prefix (`noweb-` / `noweb-arm-`), one
  "WAHA" PR, dashboard tick required, never automerged. Digest pinning is off for WAHA: the tags are
  versioned, and without a digest the default string replacement is enough — so no `autoReplaceStringTemplate`,
  and the compose `${WAHA_IMAGE:-…}` manager returns. Verified with Renovate's own replace code: each line
  changes in place and the `${WAHA_IMAGE:-…}` wrapper survives.

## Consequences / not verified

- **Local re-pair needed.** An existing local `default` session was created under WEBJS; delete it (WAHA API
  `DELETE /api/sessions/default`, or remove the `waha-data` volume) and scan the QR again. Fly stays on
  NOWEB with the same volume, so its session persists across the image change.
- **Accepted risk:** NOWEB is a protocol reimplementation and can hit upstream breakage
  (`companion_reg_refresh` class) that WEBJS avoided; the previous Compose comment chose WEBJS to dodge that.
  Prod already runs NOWEB, so local now shares that exposure.
- **Not verified:** pairing on the `noweb-arm-2026.9.2` image — I can't run an arm image or scan a QR here.
  The first local `docker compose up` after this change is that test.
