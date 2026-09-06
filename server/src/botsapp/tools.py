"""
MCP tool definitions.

Tool functions are decorated with @mcp.tool() at definition time.
They remain importable and directly testable without going through the
MCP framework because the decorator only registers metadata — it does
not alter the function's calling convention.

Tags matter beyond documentation here: every tool that mutates WhatsApp
state carries OUTBOUND_TAG, and app.py's AuthMiddleware permanently blocks
any tool carrying it. Chat/message/group tools also enforce the chat
allowlist — either implicitly, via state.message_store (an
AllowlistedMessageStore), or explicitly, via a state.db.list_allowed_jids()
check for the tools that talk to the bridge or chat_metadata directly.
get_media is a hybrid: it resolves message_id to a chat_jid via
state.message_store (unfiltered — see
AllowlistedMessageStore.get_message_chat_jid) and then checks that jid
explicitly, since the media cache on disk is keyed only by message_id and
populated bridge-side independent of the allowlist.

Bridge-agnostic: every write/group/contact/session call goes through
state.bridge, which returns bridge.py's dataclasses (SendResult, GroupInfo,
ContactInfo, SessionStatus) rather than a bridge-specific shape — nothing
here branches on which bridge is selected.
"""

import logging
from dataclasses import asdict
from typing import Any

from fastmcp import Context
from fastmcp.utilities.types import Image

import botsapp.state as state
from botsapp.app import OUTBOUND_TAG, mcp
from botsapp.message_store import ChatNotAllowedError

logger = logging.getLogger(__name__)

# Hard ceiling on any caller-supplied `limit` for a list/search tool. Without
# this, a client could pass an arbitrarily large limit and, if enough rows
# exist to satisfy it, get back a response that blows past the MCP token
# budget the same way list_groups's per-item asdict() leak once did (see
# git history) — this guards the "many rows" side of that failure mode, as
# list_groups's explicit field allowlist guards the "huge fields" side.
MAX_LIST_LIMIT = 200


async def _require_allowed(jid: str) -> None:
    """Raise ChatNotAllowedError unless ``jid`` is on the allowlist.

    Centralizes the check duplicated across every tool that talks to
    state.bridge/state.db directly for group/tag/metadata data (read-path
    tools get this for free via AllowlistedMessageStore instead — see the
    module docstring above).
    """
    assert state.db is not None
    if jid not in await state.db.list_allowed_jids():
        raise ChatNotAllowedError(f"chat '{jid}' is not in the allowlist")


# ── Read tools (query chat/message history) ──────────────────────────────────


@mcp.tool(tags={"read", "chat"})
async def list_chats(
    limit: int = 50, offset: int = 0, query: str = "", tags: list[str] | None = None
) -> list[dict[str, Any]]:
    """List WhatsApp chats ordered by most recent activity, optionally filtering by query or tags.

    Only chats allowed via the admin UI are ever returned.

    Args:
        limit: Maximum number of chats to return (default 50, capped at 200).
        offset: Pagination offset (default 0).
        query: Optional search term matched against chat name, JID, or tags.
        tags: Optional list of tags to search for (matches if chat has ANY of these).

    Returns:
        List with fields: id, jid, name, unread_count, last_message, last_message_at.
    """
    try:
        assert state.db is not None
        assert state.message_store is not None
        limit = min(limit, MAX_LIST_LIMIT)
        if tags:
            matches = await state.db.search_by_tags(tags=tags)
            chats = []
            for match in matches[offset : offset + limit]:
                chat = await state.message_store.get_chat(match["jid"])
                if chat is not None:
                    chats.append(chat)
            return chats
        elif query:
            return await state.message_store.search_chats(query=query, limit=limit, offset=offset)
        else:
            return await state.message_store.get_chats(limit=limit, offset=offset)
    except Exception as exc:
        logger.error("list_chats failed: %s", exc)
        return [{"error": str(exc)}]


@mcp.tool(tags={"read", "chat"})
async def list_messages(jid: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """List messages in a chat, newest first.

    Args:
        jid: WhatsApp JID of the chat (e.g. "15551234567@s.whatsapp.net" or group JID).
        limit: Max messages to return (default 50, capped at 200).
        offset: Pagination offset (default 0).

    Returns:
        List with fields: message_id, from, sender_name, to, chat_jid, chat_name,
        text, timestamp, message_type, media_url. sender_name/chat_name fall
        back to the raw jid when no matching contact/chat name is known.
    """
    try:
        assert state.message_store is not None
        limit = min(limit, MAX_LIST_LIMIT)
        return await state.message_store.list_messages(jid=jid, limit=limit, offset=offset)
    except ChatNotAllowedError as exc:
        return [{"error": str(exc)}]
    except Exception as exc:
        logger.error("list_messages failed: %s", exc)
        return [{"error": str(exc)}]


@mcp.tool(tags={"read", "search"})
async def search_messages(
    query: str, jid: str | None = None, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    """Search message content across all chats.

    Args:
        query: Text to search for (case-insensitive).
        jid: Optional chat JID to scope search to a single chat.
        limit: Maximum results to return (default 50, capped at 200).
        offset: Pagination offset (default 0).

    Returns:
        List with fields: message_id, from, sender_name, chat_jid, chat_name,
        text, timestamp, message_type, media_url. sender_name/chat_name fall
        back to the raw jid when no matching contact/chat name is known.
    """
    try:
        assert state.message_store is not None
        limit = min(limit, MAX_LIST_LIMIT)
        return await state.message_store.search_messages(
            query=query, jid=jid, limit=limit, offset=offset
        )
    except ChatNotAllowedError as exc:
        return [{"error": str(exc)}]
    except Exception as exc:
        logger.error("search_messages failed: %s", exc)
        return [{"error": str(exc)}]


# ── Write tools (call the active bridge) ──────────────────────────────────────


@mcp.tool(tags={"write", "chat", OUTBOUND_TAG})
async def send_message(to: str, text: str) -> dict[str, Any]:
    """Send a plain-text WhatsApp message.

    Args:
        to: Recipient phone number (e.g. "15551234567") or chat JID.
        text: Message body.

    Returns:
        Dict with fields: success, message_id, details.
    """
    try:
        assert state.bridge is not None
        return asdict(await state.bridge.send_message(to=to, text=text))
    except Exception as exc:
        logger.error("send_message failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(tags={"write", "chat", OUTBOUND_TAG})
async def send_media(to: str, media_url: str, media_type: str, caption: str = "") -> dict[str, Any]:
    """Send a media file via WhatsApp.

    Args:
        to: Recipient phone number or JID.
        media_url: Publicly accessible media URL.
        media_type: One of "image", "document", "video", "audio".
        caption: Optional caption text.
    """
    try:
        assert state.bridge is not None
        result = await state.bridge.send_media(
            to=to, media_url=media_url, media_type=media_type, caption=caption
        )
        return asdict(result)
    except Exception as exc:
        logger.error("send_media failed: %s", exc)
        return {"error": str(exc)}


# ── Groups ────────────────────────────────────────────────────────────────────


@mcp.tool(tags={"read", "group"})
async def list_groups(
    query: str = "",
    tags: list[str] | None = None,
    min_participants: int = 0,
    max_participants: int = 0,
    exclude_communities: bool = True,
) -> list[dict[str, Any]]:
    """List or search WhatsApp groups the account belongs to.

    Only groups allowed via the admin UI are ever returned. All filters are
    optional and combined with AND logic.

    ``tags`` is the primary way to find a group when the user's request
    names or implies a tag someone set via the admin UI or add_tag/
    remove_tag — it's a precise, deliberate match, unlike ``query``, which
    is a fuzzy substring match against the group's live WhatsApp name and
    will happily match on totally unrelated groups that share a word. Try
    ``tags`` first; if that comes up empty, ask the user before falling
    back to a ``query`` search rather than silently guessing at a looser
    match.

    Args:
        query: Case-insensitive substring to match against group names (empty = all).
        tags: Optional list of tags to search for (matches if group has ANY of
            these, case-insensitive) — see docstring above for when to prefer
            this over query.
        min_participants: Only include groups with at least this many members (0 = no minimum).
        max_participants: Only include groups with at most this many members (0 = no maximum).
        exclude_communities: Exclude community parent groups whose participant counts
            are unreliable (default true). Set false to include them.

    Returns:
        Filtered list with fields: jid, name, topic, participant_count,
        is_community, linked_parent_jid.
    """
    try:
        assert state.bridge is not None
        assert state.db is not None
        allowed = await state.db.list_allowed_jids()
        groups = await state.bridge.list_groups()

        tag_matched_jids: set[str] | None = None
        if tags:
            tag_matched_jids = {r["jid"] for r in await state.db.search_by_tags(tags=tags)}

        query_lower = query.lower()
        results = []
        for g in groups:
            if g.jid not in allowed:
                continue
            if exclude_communities and g.is_community:
                continue
            if tag_matched_jids is not None and g.jid not in tag_matched_jids:
                continue
            if query_lower and query_lower not in g.name.lower():
                continue
            if min_participants and g.participant_count < min_participants:
                continue
            if max_participants and g.participant_count > max_participants:
                continue
            results.append(
                {
                    "jid": g.jid,
                    "name": g.name,
                    "topic": g.topic,
                    "participant_count": g.participant_count,
                    "is_community": g.is_community,
                    "linked_parent_jid": g.linked_parent_jid,
                }
            )
        return results
    except Exception as exc:
        logger.error("list_groups failed: %s", exc)
        return [{"error": str(exc)}]


@mcp.tool(tags={"read", "group"})
async def get_group_info(jid: str) -> dict[str, Any]:
    """Get detailed info and participant list for a WhatsApp group.

    Args:
        jid: Group JID (e.g. "120363170416693245@g.us").

    Returns:
        Dict with fields: jid, name, topic, participant_count,
        participants (list of JIDs), is_community, linked_parent_jid,
        invite_link. Unlike list_groups (a bulk roster, deliberately
        stripped down to avoid leaking every group's join link and full
        member list at once), this is a single, explicitly requested
        group's full detail — including its invite link.
    """
    try:
        assert state.bridge is not None
        await _require_allowed(jid)
        return asdict(await state.bridge.get_group_info(jid))
    except ChatNotAllowedError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        logger.error("get_group_info failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(tags={"write", "group", "dangerous", OUTBOUND_TAG})
async def leave_group(group_jid: str, ctx: Context | None = None) -> dict[str, Any]:
    """Leave a WhatsApp group.

    Args:
        group_jid: Group JID (e.g. "120363170416693245@g.us").
    """
    try:
        if ctx is not None:
            confirm = await ctx.elicit(
                f"Leave group {group_jid}? This cannot be undone.",
                response_type=bool,
            )
            if confirm.action != "accept" or not confirm.data:
                return {"status": "cancelled", "reason": "User declined to leave group"}
        assert state.bridge is not None
        return asdict(await state.bridge.leave_group(group_jid))
    except Exception as exc:
        logger.error("leave_group failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(tags={"write", "group", OUTBOUND_TAG})
async def create_group(name: str, participants: list[str]) -> dict[str, Any]:
    """Create a new WhatsApp group.

    Args:
        name: Group name.
        participants: List of phone numbers to add (e.g. ["15551234567", "15559876543"]).

    Returns:
        Group metadata including jid, name, and participants.
    """
    try:
        assert state.bridge is not None
        return asdict(await state.bridge.create_group(name=name, participants=participants))
    except Exception as exc:
        logger.error("create_group failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(tags={"write", "group", OUTBOUND_TAG})
async def update_group_participants(
    group_jid: str, action: str, phones: list[str]
) -> dict[str, Any]:
    """Add, remove, promote, or demote participants in a WhatsApp group.

    Args:
        group_jid: Group JID.
        action: One of "add", "remove", "promote", "demote".
        phones: List of phone numbers or JIDs.
    """
    try:
        assert state.bridge is not None
        result = await state.bridge.update_group_participants(
            group_jid=group_jid, action=action, phones=phones
        )
        return asdict(result)
    except Exception as exc:
        logger.error("update_group_participants failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(tags={"write", "group", OUTBOUND_TAG})
async def set_group_name(group_jid: str, name: str) -> dict[str, Any]:
    """Change the name of a WhatsApp group.

    Args:
        group_jid: Group JID.
        name: New group name.
    """
    try:
        assert state.bridge is not None
        return asdict(await state.bridge.set_group_name(group_jid, name))
    except Exception as exc:
        logger.error("set_group_name failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(tags={"write", "group", OUTBOUND_TAG})
async def set_group_topic(group_jid: str, topic: str) -> dict[str, Any]:
    """Change the topic/description of a WhatsApp group.

    Args:
        group_jid: Group JID.
        topic: New topic/description text.
    """
    try:
        assert state.bridge is not None
        return asdict(await state.bridge.set_group_topic(group_jid, topic))
    except Exception as exc:
        logger.error("set_group_topic failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(tags={"read"})
async def list_newsletters() -> list[dict[str, Any]]:
    """List subscribed WhatsApp newsletters/channels.

    Not currently implemented for the active bridge.
    """
    return [{"error": "list_newsletters is not currently supported by the active bridge"}]


# Disable newsletters tool to declutter namespace by default
mcp.disable(names={"list_newsletters"})


# ── Chat interactions ─────────────────────────────────────────────────────────


@mcp.tool(tags={"write", "chat", OUTBOUND_TAG})
async def send_reaction(to: str, message_id: str, emoji: str) -> dict[str, Any]:
    """React to a WhatsApp message with an emoji.

    Args:
        to: Chat JID or phone number.
        message_id: ID of the message to react to.
        emoji: Emoji character (e.g. "👍", "❤️").
    """
    try:
        assert state.bridge is not None
        return asdict(await state.bridge.send_reaction(to=to, message_id=message_id, emoji=emoji))
    except Exception as exc:
        logger.error("send_reaction failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(tags={"write", "chat", OUTBOUND_TAG})
async def mark_read(to: str, message_ids: list[str]) -> dict[str, Any]:
    """Mark one or more messages as read.

    Args:
        to: Chat JID or phone number.
        message_ids: List of message IDs to mark as read.
    """
    try:
        assert state.bridge is not None
        return asdict(await state.bridge.mark_read(to=to, message_ids=message_ids))
    except Exception as exc:
        logger.error("mark_read failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(tags={"write", "chat", OUTBOUND_TAG})
async def edit_message(to: str, message_id: str, text: str) -> dict[str, Any]:
    """Edit a previously sent WhatsApp message.

    Args:
        to: Chat JID or phone number where the message was sent.
        message_id: ID of the message to edit.
        text: New message content.
    """
    try:
        assert state.bridge is not None
        return asdict(await state.bridge.edit_message(to=to, message_id=message_id, text=text))
    except Exception as exc:
        logger.error("edit_message failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(tags={"write", "chat", "dangerous", OUTBOUND_TAG})
async def delete_message(to: str, message_id: str, ctx: Context | None = None) -> dict[str, Any]:
    """Delete a message you sent in a WhatsApp chat.

    Args:
        to: Chat JID or phone number.
        message_id: ID of the message to delete.
    """
    try:
        if ctx is not None:
            confirm = await ctx.elicit(
                f"Delete message {message_id}? This cannot be undone.",
                response_type=bool,
            )
            if confirm.action != "accept" or not confirm.data:
                return {"status": "cancelled", "reason": "User declined to delete message"}
        assert state.bridge is not None
        return asdict(await state.bridge.delete_message(to=to, message_id=message_id))
    except Exception as exc:
        logger.error("delete_message failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(tags={"write", "chat", OUTBOUND_TAG})
async def send_location(
    to: str, latitude: float, longitude: float, name: str = ""
) -> dict[str, Any]:
    """Send a location pin via WhatsApp.

    Args:
        to: Recipient phone number or JID.
        latitude: Latitude coordinate.
        longitude: Longitude coordinate.
        name: Optional place name.
    """
    try:
        assert state.bridge is not None
        result = await state.bridge.send_location(
            to=to, latitude=latitude, longitude=longitude, name=name
        )
        return asdict(result)
    except Exception as exc:
        logger.error("send_location failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(tags={"write", "chat", OUTBOUND_TAG})
async def send_contact(to: str, name: str, vcard: str) -> dict[str, Any]:
    """Send a contact card via WhatsApp.

    Args:
        to: Recipient phone number or JID.
        name: Display name for the contact.
        vcard: Contact info in vCard format.
    """
    try:
        assert state.bridge is not None
        return asdict(await state.bridge.send_contact(to=to, name=name, vcard=vcard))
    except Exception as exc:
        logger.error("send_contact failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(tags={"write", "group", OUTBOUND_TAG})
async def send_poll(group_jid: str, question: str, options: list[str]) -> dict[str, Any]:
    """Send a poll to a WhatsApp group.

    Args:
        group_jid: Group JID (e.g. "120363170416693245@g.us").
        question: Poll question text.
        options: List of answer options (2-12 items).
    """
    try:
        assert state.bridge is not None
        result = await state.bridge.send_poll(
            group_jid=group_jid, question=question, options=options
        )
        return asdict(result)
    except Exception as exc:
        logger.error("send_poll failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(tags={"write", "chat", OUTBOUND_TAG})
async def set_chat_presence(to: str, state_value: str, media: str = "") -> dict[str, Any]:
    """Show typing or recording indicator in a chat.

    Args:
        to: Chat JID or phone number.
        state_value: "composing" or "paused".
        media: Optional "audio" to show recording indicator.
    """
    try:
        assert state.bridge is not None
        result = await state.bridge.set_chat_presence(to=to, state=state_value, media=media)
        return asdict(result)
    except Exception as exc:
        logger.error("set_chat_presence failed: %s", exc)
        return {"error": str(exc)}


# ── Chat metadata ────────────────────────────────────────────────────────


@mcp.tool(tags={"write", "metadata"})
async def add_tag(jid: str, tag: str) -> dict[str, Any]:
    """Add a single tag to a chat, leaving its other tags untouched.

    This reads the chat's current tags, adds ``tag`` if it isn't already
    present, and saves the merged list.

    The chat must already be on the allowlist (set via the admin UI) —
    this tool has no way to allow a chat itself, only annotate one that's
    already visible.

    Args:
        jid: Chat JID to tag.
        tag: Tag string to add.

    Returns:
        Confirmation dict with the saved jid and full tag list.
    """
    try:
        assert state.db is not None
        await _require_allowed(jid)
        meta = await state.db.get_chat_metadata(jid=jid)
        current = list(meta["tags"]) if meta else []
        if tag not in current:
            current.append(tag)
        await state.db.upsert_chat_metadata(jid=jid, tags=current)
        return {"jid": jid, "tags": current, "status": "saved"}
    except ChatNotAllowedError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        logger.error("add_tag failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(tags={"write", "metadata"})
async def remove_tag(jid: str, tag: str) -> dict[str, Any]:
    """Remove a single tag from a chat, leaving its other tags untouched.

    Same allowlist requirement as add_tag. Removing a tag the chat
    doesn't have is a no-op, not an error.

    Args:
        jid: Chat JID to untag.
        tag: Tag string to remove.

    Returns:
        Confirmation dict with the saved jid and remaining tag list.
    """
    try:
        assert state.db is not None
        await _require_allowed(jid)
        meta = await state.db.get_chat_metadata(jid=jid)
        current = [t for t in (meta["tags"] if meta else []) if t != tag]
        await state.db.upsert_chat_metadata(jid=jid, tags=current)
        return {"jid": jid, "tags": current, "status": "saved"}
    except ChatNotAllowedError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        logger.error("remove_tag failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(tags={"write", "metadata"})
async def clear_tags(jid: str) -> dict[str, Any]:
    """Remove every tag from a chat.

    Same allowlist requirement as add_tag.

    Args:
        jid: Chat JID to clear tags for.

    Returns:
        Confirmation dict with the saved jid and an empty tag list.
    """
    try:
        assert state.db is not None
        await _require_allowed(jid)
        await state.db.upsert_chat_metadata(jid=jid, tags=[])
        return {"jid": jid, "tags": [], "status": "saved"}
    except ChatNotAllowedError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        logger.error("clear_tags failed: %s", exc)
        return {"error": str(exc)}


@mcp.tool(tags={"read", "metadata"})
async def get_chat_metadata(jid: str) -> dict[str, Any]:
    """Get tags for a chat.

    Args:
        jid: Chat JID to look up.

    Returns:
        Dict with fields: jid, tags, updated_at.
    """
    try:
        assert state.db is not None
        await _require_allowed(jid)
        meta = await state.db.get_chat_metadata(jid=jid)
        if meta is None:
            return {"jid": jid, "tags": [], "status": "no metadata"}
        return meta
    except ChatNotAllowedError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        logger.error("get_chat_metadata failed: %s", exc)
        return {"error": str(exc)}


# ── Media ────────────────────────────────────────────────────────────────


@mcp.tool(tags={"read", "media"})
async def get_media(message_id: str) -> Image:
    """Retrieve a saved media thumbnail (image/video/sticker) for a message.

    Checks local disk for the media thumbnail. Only returns media for
    messages belonging to an allowlisted chat. The media cache on
    disk is keyed only by message_id and is populated bridge-side
    independent of the allowlist, so this resolves message_id back to its
    chat_jid and checks that explicitly, the same way get_chat_metadata/
    add_tag do for tools that don't already go through
    AllowlistedMessageStore.

    Args:
        message_id: The message_id field from list_messages or search_messages.

    Returns:
        The JPEG thumbnail image.
    """
    assert state.message_store is not None
    assert state.db is not None
    chat_jid = await state.message_store.get_message_chat_jid(message_id=message_id)
    if chat_jid is None or chat_jid not in await state.db.list_allowed_jids():
        raise ChatNotAllowedError(f"message '{message_id}' is not in an allowed chat")

    safe_id = message_id.replace("/", "_").replace(":", "_")

    if state.media_dir is not None:
        path = state.media_dir / f"{safe_id}.jpg"
        if path.exists():
            return Image(data=path.read_bytes(), format="jpeg")

    raise FileNotFoundError(
        f"No media found for message '{message_id}'. "
        "Media is only saved for messages received while the server is running."
    )


# ── Session management ────────────────────────────────────────────────────
#
# Connecting/pairing/status-polling is deliberately NOT exposed as MCP
# tools (it was: whatsapp_session, whatsapp_session_poll, plus an MCP App
# resource for the QR view) — the admin UI (network-private, no auth of
# its own) is the only place session/pairing is meant to happen, so this
# doesn't need to be MCP-reachable too. Disconnect
# stays: it's a plain permanently-blocked OUTBOUND_TAG tool like every
# other write tool, not part of that surface.


@mcp.tool(tags={"session", "dangerous", OUTBOUND_TAG})
async def disconnect_whatsapp(ctx: Context | None = None) -> dict[str, Any]:
    """Disconnect and log out from WhatsApp.

    WARNING: This wipes WhatsApp device keys and contacts from the database.
    Message history and chat metadata (tags) are preserved. Reconnecting
    requires scanning a new QR code at the admin UI.
    """
    try:
        if ctx is not None:
            confirm = await ctx.elicit(
                "This will wipe device keys and contacts. "
                "You'll need to re-scan a QR code at the admin UI. Continue?",
                response_type=bool,
            )
            if confirm.action != "accept" or not confirm.data:
                return {"status": "cancelled", "reason": "User declined disconnect"}
        assert state.bridge is not None
        return asdict(await state.bridge.disconnect())
    except Exception as exc:
        logger.error("disconnect_whatsapp failed: %s", exc)
        return {"error": str(exc)}
