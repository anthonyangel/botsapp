# 0033 — WhatsApp labels/lists as a read-only chat filter

**Status:** Decided and implemented (2026-11). Adds a data source next to tags; amends nothing.

## Context

The account owner already organises chats on their phone with WhatsApp labels and lists ("Estate agents", per-child labels, "Shul / religious life"). WAHA exposes them (verified live against 2026.10.1 NOWEB, both sessions): `GET /api/{session}/labels` returns `id`, `name`, `color`, `colorHex` — the built-in filters Unread/Favourites/Groups (ids 1–3) come back as labels too — and `GET /api/{session}/labels/{id}/chats` returns the chats under one. botsapp did not read them; the only way to group chats was botsapp's own **tags** (`chat_access.tags`, per account and viewer, set in the admin UI), which duplicates curation the owner has already done.

The two are different things. Labels belong to the account owner and live on the phone; tags are per viewer and live in SQLite next to the allowlist.

## Decision

- **Labels stay in WhatsApp; nothing is copied into SQLite.** Resolved live per call, so there is no sync and no drift when the owner edits a label on their phone. Tags are unchanged.
- **`list_labels` tool** (tags `read`, `label`): the account's labels, each with `chat_count` of chats *the caller can see*. A label with no visible chat is omitted, so a label name never reveals a chat that is not allowlisted for that viewer.
- **`list_chats(labels=[...])` filter**: labels by name (case-insensitive) or id, matching ANY of them. Combined with `tags`, a chat must match both. As with `tags`, `query` is ignored when it is given. An unknown label returns an empty page rather than an error, again so names cannot be probed.
- **The allowlist is still the privacy boundary.** Label membership is intersected with the viewer's allowed jids, and each chat is then loaded through the allowlisted store like every other branch.
- **Bridge protocol grows `list_labels` and `get_label_chat_jids`** (`LabelInfo` dataclass); the WAHA JSON stays inside `waha_client.py`.
- **Read-only**: only the GET endpoints are used; WAHA's label create/update/delete and "put labels on a chat" calls are not exposed.

## Not verified

The tools are tagged with WAHA's `read` action. Whether a *scoped* session key with only `read` can call the labels endpoints has not been checked live (admin key only so far) — see #47 for the same caveat on other endpoints. If it cannot, `list_labels` and the `labels` filter will report that the account's key does not allow it.

WAHA 2026.10.1's changelog says Labels gained `type` and `isActive` fields, but the live responses did not include them, so they are not used to tell built-in filters from custom lists.

## Alternatives considered

| Option | Why not |
|---|---|
| Import labels into tags (one-off or synced) | Simpler to query, but drifts from the phone and makes viewer-owned tags look like the owner's curation. |
| Only add `list_labels`, no filter | Leaves Claude to cross-reference chats by hand. |
| Show every label, with raw counts | Leaks the existence of chats the viewer is not allowed to see. |
