# 0034 — Admin UI shows and edits WhatsApp labels (distinct from tags)

**Status:** Decided and implemented (2026-11). Builds on [0033](0033-whatsapp-labels-as-a-read-only-chat-filter.md); amends only its "Read-only" bullet (the admin UI may now write labels; the MCP tools still cannot).

## Context

0033 made WhatsApp labels readable by the MCP server. The owner wants to see and assign them from the admin UI and to move toward labels, away from botsapp tags, because labels are native WhatsApp (they show on the phone).

## Decision

- **A separate "Labels" column** on every chats table (and the Status panel), rendered as coloured outlined pills; tags keep their own column and editor. The two are never merged: tags are per viewer in SQLite, labels belong to the account and live in WhatsApp.
- **Nothing stored locally.** Each page load reads `/labels` plus `/labels/{id}/chats` (one call per label) and inverts it to jid → labels.
- **Owner only.** Setting a chat's labels (`POST /chats/{jid}/labels`) and creating a label (`POST /labels/create`) change the owner's WhatsApp, so a shared-with viewer gets 403, like the allowlist switch. Viewers still see the labels on chats they can see.
- **Writes use the admin key** (`purpose="admin"`), as other admin-UI WAHA calls do. Bridge gains `create_label` and `set_chat_labels` (WAHA `POST /labels`, `PUT /labels/chats/{chatId}`, which replaces the whole set). The MCP tools remain read-only, and the scoped read-only keys are untouched.
- **Built-in filters (ids 1–3) are hidden** and not offered, but preserved when a chat's set is rewritten, since the PUT would otherwise drop them.

## Not verified

The WAHA write endpoints are implemented from WAHA's documented API and unit-tested against mocks only; not yet exercised live against NOWEB. Labels are fetched per page load (N+1 calls), which is fine for tens of labels. Hiding ids 1–3 relies on the observation in 0033. Deleting/renaming labels and bulk assignment are not offered. Moving *away* from tags (e.g. importing tags as labels, or MCP tag tools → labels) is a later step.
