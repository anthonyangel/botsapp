# 0019 — Map WAHA payloads as the NOWEB engine emits them

**Status:** Decided (2026-10). Corrects assumptions in `waha_client.py` that
dated from the WEBJS engine; related to the engine switch noted in
`waha/fly.toml` (WEBJS → NOWEB, 2026-09).

## Context

`waha_client.py`'s mappers and their test fixtures were written against
whatsapp-web.js (WEBJS) payloads. The deployed WAHA runs NOWEB (Baileys), where
several assumed fields don't exist. Checked against WAHA's source
(`devlikeapro/waha`: `toWAMessage`, `getFromToParticipant`,
`NowebPersistentStore.getChats`) and the pinned Baileys fork
(`fork-master-2026-04-28`: `WAProto` `WebMessageInfo`/`Conversation`,
`Types/Chat`):

- `/chats` rows are Baileys `Chat`s: `conversationTimestamp`, no `timestamp`,
  no `lastMessage`; WAHA deletes `unreadCount`.
- A message's `_data` is a Baileys `WebMessageInfo`: no `type`, no `body`;
  system noise is `messageStubType` or protocol-only content.
- In groups `from` is the group; the sender is `participant`.

Effect on the old mapper under NOWEB: `last_message_at`/`last_message` always
null, notification noise unfiltered, group `sender_name` resolved against the
wrong jid.

## Decisions

- `last_message_at` falls back to `conversationTimestamp` /
  `lastMessageRecvTimestamp`, and protobuf-Long timestamps are unwrapped.
- `_is_real_message` recognises both engines' noise (WEBJS `_data.type`,
  NOWEB `messageStubType` / protocol-only `message`).
- Sender is `participant`, then `author`, then `from`.
- `last_message` is fetched per chat for the returned page only, via a new
  `MessageStore.with_last_messages` (called by `AllowlistedMessageStore` after
  allowlist filtering and paging — never for hidden chats or the full list).
  Chosen over `/chats/overview`, which does a message and picture lookup per
  chat server-side for every chat in the request.
- WEBJS shapes keep working (the mapper accepts both).

## Caveat

`tests/test_waha_noweb_shapes.py` fixtures are **derived from source, not
captured live**. Replace them with scrubbed real NOWEB responses when
available. Not addressed: messages sent by the account itself in a DM, where
NOWEB's `from` is still the chat, so `sender_name` may name the contact.
