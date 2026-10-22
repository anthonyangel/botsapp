# 0018 — MCP spec conformance pass on the read tools

**Status:** Decided (2026-10). Amends the error-handling and media-return
shape of [0017](0017-tigris-presigned-links-for-media.md) (the link is now a
`resource_link` block alongside a text description, not a bare string).

## Context

A review of the server against the MCP spec found the reachable tools
returning failures as ordinary successful results (`{"error": ...}`), no tool
annotations, no usable `outputSchema`, unbounded inline media, and no
continuation signal on paginated reads. (The spec text itself could not be
fetched from the review environment; this is based on the spec as known.)

## Decisions

- **Errors raise `ToolError`** (reported with `isError: true`). Only the
  allowlist refusal is shown verbatim; anything else is logged server-side and
  the caller gets a generic message, so WAHA/SQLite internals don't leak.
- **Annotations + titles** on every reachable tool: reads are
  `readOnlyHint`; `add_tag`/`remove_tag`/`clear_tags` are non-destructive and
  idempotent. Chat/message/group reads are `openWorldHint=True`.
- **Typed return shapes** (TypedDicts) so each tool advertises a real
  `outputSchema`. Not validated at runtime.
- **Pagination contract changed:** `list_chats`, `list_messages` and
  `search_messages` return `{items, next_offset}` (fetching limit+1 to know
  whether more exist) instead of a bare list. The `tags` branch of
  `list_chats` now paginates after dropping unresolved matches.
- **`get_media`**: the presigned link is a `resource_link` block plus a text
  description; inline media over `MAX_INLINE_MEDIA_BYTES` (10 MB) is refused.
- Tool descriptions warn that names/topics/message text are untrusted
  third-party content.

## Deliberately not changed

- The permanently blocked `"outbound"` tools still return error dicts and
  keep their `ctx.elicit` confirmations — unreachable today, and the
  confirmation is a useful backstop if one is ever unblocked.
- Origin validation / audience checks were not verified here; see the PR.
