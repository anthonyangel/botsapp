"""
Bridge-agnostic dataclasses and the ``Bridge`` protocol every WhatsApp
bridge client (currently just ``WAHAClient``) must satisfy.

Kept separate from message_store.py: ``MessageStore`` is about reading
chat/message *history*; ``Bridge`` is about live session/pairing state and
every write/group/contact operation. Neither module needs to know a
concrete client's raw JSON shapes — WAHA calls things "chatId"/"text"/
"numberExists"/`@c.us` — that translation stays entirely inside the
concrete client (waha_client.py). tools.py, session.py, and the admin UI
only ever see these dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class SessionStatus:
    """Bridge-agnostic WhatsApp session/pairing state.

    Every bridge's status/connect call returns this same shape, whatever
    the underlying bridge's own representation is (e.g. WAHA's
    STARTING/SCAN_QR_CODE/WORKING status enum). session.py and the admin
    UI's session page only ever see this.
    """

    connected: bool = False
    logged_in: bool = False
    qr_code: str | None = None  # base64 or a data: URI — bridge normalizes
    jid: str | None = None
    name: str | None = None
    errors: list[str] = field(default_factory=list)
    hint: str | None = None


@dataclass
class SendResult:
    """Result of any mutating call (send/edit/delete/leave/etc.).

    ``raw`` carries the bridge's original response for callers that want
    it (tools.py currently forwards a lot of this straight to Claude) —
    but code that needs to branch on outcome should use ``success``, not
    inspect ``raw``, which is bridge-specific by definition.
    """

    success: bool
    message_id: str | None = None
    details: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class GroupInfo:
    jid: str
    name: str
    topic: str = ""
    participant_count: int = 0
    participants: list[str] = field(default_factory=list)
    is_community: bool = False
    linked_parent_jid: str = ""
    invite_link: str | None = None


@dataclass
class ContactInfo:
    jid: str
    name: str
    business_name: str = ""
    phone_number: str = ""
    is_on_whatsapp: bool | None = None
    # Escape hatch for bridge-specific extras (e.g. live presence/device
    # info) that don't fit the normalized shape — anything landing here is
    # understood to be bridge-specific by whoever reads it.
    extra: dict = field(default_factory=dict)


class Bridge(Protocol):
    """Everything tools.py/the admin UI need from a WhatsApp bridge, beyond
    chat/message history (see message_store.MessageStore for that)."""

    async def close(self) -> None: ...

    # ── Session / pairing ────────────────────────────────────────────────
    async def get_session_status(self) -> SessionStatus: ...
    async def connect(self) -> SessionStatus: ...
    async def disconnect(self) -> SendResult: ...

    # ── Messaging ────────────────────────────────────────────────────────
    async def send_message(self, to: str, text: str) -> SendResult: ...
    async def send_media(
        self, to: str, media_url: str, media_type: str, caption: str = ""
    ) -> SendResult: ...
    async def send_reaction(self, to: str, message_id: str, emoji: str) -> SendResult: ...
    async def mark_read(self, to: str, message_ids: list[str]) -> SendResult: ...
    async def edit_message(self, to: str, message_id: str, text: str) -> SendResult: ...
    async def delete_message(self, to: str, message_id: str) -> SendResult: ...
    async def send_location(
        self, to: str, latitude: float, longitude: float, name: str = ""
    ) -> SendResult: ...
    async def send_contact(self, to: str, name: str, vcard: str) -> SendResult: ...
    async def send_poll(self, group_jid: str, question: str, options: list[str]) -> SendResult: ...
    async def set_chat_presence(self, to: str, state: str, media: str = "") -> SendResult: ...

    # ── Groups ───────────────────────────────────────────────────────────
    async def list_groups(self) -> list[GroupInfo]: ...
    async def get_group_info(self, group_jid: str) -> GroupInfo: ...
    async def create_group(self, name: str, participants: list[str]) -> GroupInfo: ...
    async def leave_group(self, group_jid: str) -> SendResult: ...
    async def get_group_invite_link(self, group_jid: str, reset: bool = False) -> str: ...
    async def update_group_participants(
        self, group_jid: str, action: str, phones: list[str]
    ) -> SendResult: ...
    async def set_group_name(self, group_jid: str, name: str) -> SendResult: ...
    async def set_group_topic(self, group_jid: str, topic: str) -> SendResult: ...

    # ── Contacts ─────────────────────────────────────────────────────────
    async def get_contacts(self) -> list[ContactInfo]: ...
    async def get_contact(self, jid: str) -> ContactInfo | None: ...
    async def check_phone(self, phones: list[str]) -> list[ContactInfo]: ...
    async def get_avatar_url(self, jid_or_phone: str, preview: bool = True) -> str | None: ...
