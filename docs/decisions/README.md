# Decision log

Lightweight architecture-decision records for `botsapp`. Read in order — each builds on the last.

- [0001 — Keep the custom FastMCP server; treat the WhatsApp bridge as swappable](0001-keep-custom-fastmcp-server.md)
- [0002 — Fly.io hosting mechanics: `flyctl mcp-server` and the remote-MCP blueprint don't apply](0002-flyio-hosting-mechanics.md)
- [0003 — Authentication & authorization: `GoogleProvider` + email allowlist + `restrict_tag`](0003-google-oauth-authn-authz.md) *(provider choice superseded by 0005; allowlist/`restrict_tag` reasoning still stands)*
- [0004 — Enforced chat allowlist + a network-private admin UI](0004-chat-allowlist-and-admin-ui.md)
- [0005 — Switch authentication from `GoogleProvider` to WorkOS AuthKit](0005-authkit-oauth-provider.md)
- 0006 — Bridge abstraction (WuzAPI/WAHA): referenced throughout the codebase but the record itself was never committed — see the note at the end of 0007.
- [0007 — Move `chat_metadata` off Postgres onto SQLite; drop Postgres from the default stack](0007-sqlite-for-metadata-store.md)
- [0008 — Remove WuzAPI; WAHA is the only bridge provider](0008-remove-wuzapi-waha-only.md)
- [0009 — Replace aioresponses with aresponses for aiohttp test mocking](0009-aresponses-for-http-mocking.md)
- [0010 — Drop Cloudflare Tunnel/Access; Fly.io's own HTTPS + AuthKit is the whole exposure story](0010-drop-cloudflare-two-provider-deploy.md) *(supersedes the Cloudflare-related parts of 0001/0002/0003)*
- [0011 — WorkOS JWT Template replaces `WorkOSTokenVerifier`; real `aud` validation restored](0011-jwt-template-drops-workostokenverifier.md) *(supersedes the verifier-construction half of 0005's addendum)*
- [0012 — `AuthKitProvider` advertises a trailing-slash issuer; blocks every real login](0012-authkitprovider-issuer-trailing-slash-bug.md) *(open bug, not fixed by 0011)*
- [0013 — `mcp-server`/`admin-ui` deploy as one Fly Machine, two processes; `waha` stays separate](0013-single-fly-app-shared-volume.md) *(amends the app count in 0010)*
- [0014 — Transcribe voice notes via OpenAI's transcription API, not a local model](0014-openai-whisper-api-for-voice-transcription.md)

This log exists because the project's first build (WuzAPI + a hand-rolled server, full write access, no auth) silently diverged from an earlier plan (WAHA, native read-only MCP) with nothing recorded about why. Add a new numbered entry here for the next architectural pivot instead of letting it happen unrecorded again — supersede old records explicitly (name which parts, as 0010/0011/0013 do above) rather than editing or deleting them.

**Decision records and implementation plans have different lifespans.** Records here are a permanent, append-only history — they stay even once superseded, because the point is knowing *what changed and why*, not just the current state. A `docs/plans/*.md` implementation plan is different: it's a one-time "here's exactly how I'm about to implement decision N" document, useful mainly while the work is in flight or under review. Once a plan has landed and its own decision record + the code are the durable truth, the plan itself is deleted rather than kept around as stale, drifting documentation (this happened once already — `message-store-auth-cleanup.md` and `admin-ui-and-chat-allowlist.md`, both fully implemented, were removed alongside adding 0013). Several records above still mention those two files by name for historical color, deliberately as plain text rather than a working link — treat a `docs/plans/...` mention in an old record as "see the record's own description of what was built," not as a live path.
