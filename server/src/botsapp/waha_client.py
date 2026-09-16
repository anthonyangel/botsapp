"""
Async HTTP client for WAHA's REST API — implements both ``bridge.Bridge``
(session/write/group/contact operations) and ``message_store.MessageStore``
(chat/message history) in one class.

WAHA has no shared database to read directly for history — everything
comes through its own REST API, so there's no split between a raw client
and a separate store; one class satisfies both protocols (message_store.py's
docstring already anticipated this).

JID normalization (a WAHA-specific opinion, contained entirely here): WAHA
addresses individual chats as ``<number>@c.us``; this project's canonical
JID form — matching every existing ``chat_metadata`` row already in the
database — is ``<number>@s.whatsapp.net``.
``_to_waha_jid``/``_from_waha_jid`` translate at this module's boundary
only; nothing above this file ever sees ``@c.us``. Group JIDs (``@g.us``)
and ``status@broadcast`` are identical across both bridges and pass
through unchanged.

Auth: ``X-Api-Key`` header.

A few endpoints (send_contact, send_poll, set_group_name/topic, the exact
invite-code response field, create_group's response shape) weren't
directly confirmed against WAHA's docs or a live paired session — each is
marked ``# unverified`` below with the best-effort shape inferred from
WAHA's otherwise-consistent naming conventions. Confirm/adjust the first
time each is actually exercised.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

import aiohttp

from botsapp.bridge import ContactInfo, GroupInfo, SendResult, SessionStatus

logger = logging.getLogger(__name__)

_WAHA_SUFFIX = "@c.us"
_CANONICAL_SUFFIX = "@s.whatsapp.net"

# A WAHA message id is whatsapp-web.js's own serialized message-key string:
# "{fromMe}_{remoteJid}_{id}", optionally with a trailing "_{participant}"
# for group messages. remoteJid never contains "_", so the first segment
# after the leading true/false is always the chat jid — see
# get_message_chat_jid below.
_MESSAGE_ID_CHAT_RE = re.compile(r"^(?:true|false)_([^_]+)_")


def _to_waha_jid(jid: str) -> str:
    """Our canonical JID -> WAHA's chatId. Groups/status pass through."""
    if jid.endswith(_CANONICAL_SUFFIX):
        return jid[: -len(_CANONICAL_SUFFIX)] + _WAHA_SUFFIX
    return jid


def _from_waha_jid(chat_id: str) -> str:
    """WAHA's chatId -> our canonical JID. Groups/status pass through."""
    if chat_id.endswith(_WAHA_SUFFIX):
        return chat_id[: -len(_WAHA_SUFFIX)] + _CANONICAL_SUFFIX
    return chat_id


def _looks_like_masked_phone(name: str) -> bool:
    """WAHA/WhatsApp can hand back a contact's "name" field as the phone
    number with its middle digits privacy-masked — e.g. "+972∙∙∙∙∙∙∙87"
    (U+2219 BULLET OPERATOR) — instead of an actual saved display name.
    Confirmed live under NOWEB for a contact that *is* saved on the phone:
    WhatsApp still hands the companion/NOWEB session this masked form as
    "name", while the contact's own broadcast "pushname" (e.g. their real
    name) comes through unmasked in the same contact record. A string made
    of nothing but digits/"+"/this mask character carries no more info
    than the raw phone number already does, so treat it as absent and let
    _contact_from_raw fall through to pushname instead.
    """
    stripped = name.strip()
    return bool(stripped) and all(ch in "0123456789+∙" for ch in stripped)


def _serialized_id(value: Any) -> str:
    """WAHA is inconsistent about how it shapes an "id" field: the
    dedicated /messages endpoint flattens it to a plain string, but
    /chats (and nested objects like groupMetadata) return whatsapp-web.js's
    raw ``{server, user, _serialized}`` form instead (confirmed live).
    Accept either.
    """
    if isinstance(value, dict):
        return value.get("_serialized", "")
    return value or ""


def _iso_timestamp(epoch_seconds: Any) -> str | None:
    """Normalize a raw WAHA Unix-epoch-seconds timestamp to an ISO-8601 UTC
    string.

    Without this, a "timestamp"/"last_message_at" field would be a raw
    epoch int rather than one unambiguous, caller-independent shape.
    """
    if not epoch_seconds:
        return None
    try:
        return datetime.fromtimestamp(float(epoch_seconds), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


# WhatsApp/whatsapp-web.js protocol & system entries (call logs, group
# membership changes, e2e key notifications, etc. — the full domain per
# wa-js's MSG_TYPE enum) that show up interleaved with real messages in
# WAHA's /messages feed. Confirmed live: 14 of 50 messages in one real
# group were pure noise like this, each with an empty body indistinguishable
# from a genuinely empty text message. Deliberately an exclude-list, not an
# include-list: anything not recognized here (a real content type this list
# hasn't seen yet, or a genuinely unclassified "unknown"/"revoked" entry)
# passes through rather than risk silently dropping real content.
_NON_MESSAGE_TYPES = frozenset(
    {
        "notification",
        "notification_template",
        "group_notification",
        "gp2",
        "broadcast_notification",
        "e2e_notification",
        "call_log",
        "protocol",
        "ciphertext",
    }
)


def _is_real_message(m: dict[str, Any]) -> bool:
    msg_type = (m.get("_data") or {}).get("type")
    return msg_type not in _NON_MESSAGE_TYPES


class WAHAClient:
    def __init__(self, base_url: str, api_key: str, session: str = "default") -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._session_name = session
        self._http: aiohttp.ClientSession | None = None

    # ── HTTP plumbing ────────────────────────────────────────────────────

    async def _get_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()
            self._http = None

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._api_key, "Content-Type": "application/json"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        http = await self._get_http()
        qs = ("?" + urlencode(params)) if params else ""
        url = f"{self._base_url}{path}{qs}"
        async with http.request(method, url, json=json, headers=self._headers()) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"WAHA {method} {path} → {resp.status}: {data}")
            return data

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        return await self._request("POST", path, json=payload)

    async def _put(self, path: str, payload: dict[str, Any]) -> Any:
        return await self._request("PUT", path, json=payload)

    async def _get_qr_base64(self) -> str | None:
        """Fetch the pairing QR as base64 PNG bytes — the shape bridge.py's
        SessionStatus.qr_code contract expects.

        WAHA's ``format=raw`` variant returns the *decoded* QR payload — the
        literal string a scanner would read out of the image (comma-
        separated ref/keys, not remotely base64) — not an encoded image, so
        it can't be dropped into an ``<img src="data:...">`` as-is. Use
        ``format=image`` instead, which returns the actual PNG bytes, and
        base64-encode those ourselves.
        """
        http = await self._get_http()
        qs = urlencode({"format": "image"})
        url = f"{self._base_url}/api/{self._session_name}/auth/qr?{qs}"
        async with http.request("GET", url, headers=self._headers()) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(
                    f"WAHA GET /api/{self._session_name}/auth/qr → {resp.status}: {body}"
                )
            png_bytes = await resp.read()
        return base64.b64encode(png_bytes).decode("ascii") if png_bytes else None

    # ── Session / pairing (bridge.Bridge) ───────────────────────────────

    async def get_session_status(self) -> SessionStatus:
        try:
            data = await self._get(f"/api/sessions/{self._session_name}")
        except RuntimeError as exc:
            if "404" in str(exc):
                # The named session has never been created (first run, or
                # after a full delete) — this is "not connected yet", not a
                # failure. Reads as a clean SessionStatus so the UI shows
                # "Connect WhatsApp" rather than an error card.
                return SessionStatus()
            return SessionStatus(errors=[f"WAHA error: {exc}"])
        except Exception as exc:
            return SessionStatus(errors=[f"Cannot reach WAHA: {exc}"])
        if data.get("status") == "STARTING":
            # Wait up to ~10 s for WAHA to leave STARTING before we return.
            # 5 × 2 s: short enough that callers (3 s poll, meta-refresh)
            # still see a timely transition to SCAN_QR_CODE / WORKING, but
            # long enough to bridge the typical cold-start window. Exit as
            # soon as the status changes rather than sleeping the full duration.
            for _ in range(5):
                await asyncio.sleep(2)
                try:
                    data = await self._get(f"/api/sessions/{self._session_name}")
                    if data.get("status") != "STARTING":
                        break
                except Exception:
                    pass
        waha_status = data.get("status", "")
        status = self._status_from_session(data)
        if waha_status == "SCAN_QR_CODE" and not status.qr_code:
            # WAHA's own session-status payload never embeds the QR
            # (confirmed live — GET /api/sessions/{name} has no "qr"
            # key even mid SCAN_QR_CODE); it only comes from the dedicated
            # auth/qr endpoint. Guard on the raw waha_status rather than
            # the derived SessionStatus fields: STARTING also satisfies
            # (connected=True, logged_in=False, qr_code=None), and calling
            # the QR endpoint while still STARTING causes WAHA to block for
            # its own internal 10 s timeout before returning a 422.
            try:
                status.qr_code = await self._get_qr_base64()
            except Exception as exc:
                logger.debug("Could not fetch QR for %s: %s", self._session_name, exc)
        return status

    async def connect(self) -> SessionStatus:
        # Idempotent: create-and-start if the session doesn't exist yet,
        # otherwise just (re)start it. WAHA errors on POST /api/sessions
        # for a session name that already exists, so check first.
        try:
            existing = await self._get(f"/api/sessions/{self._session_name}")
        except Exception:
            existing = None

        try:
            if existing is None:
                await self._post(
                    "/api/sessions",
                    {
                        "name": self._session_name,
                        "start": True,
                        # Required for the NOWEB engine (see waha/fly.toml's
                        # WHATSAPP_DEFAULT_ENGINE note): WAHA's chat/message-
                        # history endpoints 400 with "Enable NOWEB store"
                        # unless the store was turned on at session-creation
                        # time — it can't be toggled after pairing without
                        # losing history, so it has to be set here, not
                        # patched in later. fullSync=True trades a longer
                        # initial sync for ~1yr/100k-msg history instead of
                        # NOWEB's ~3-month default; harmless no-op under
                        # WEBJS, which ignores config.noweb entirely.
                        "config": {"noweb": {"store": {"enabled": True, "fullSync": True}}},
                    },
                )
            elif existing.get("status") in ("STOPPED", "FAILED"):
                # WAHA's own guidance for a dead session (whether never-
                # started or crashed mid-pairing): restart, not start.
                await self._post(f"/api/sessions/{self._session_name}/restart", {})
            # else: already STARTING/SCAN_QR_CODE/WORKING — nothing to do,
            # just read status (and QR) below.
        except Exception as exc:
            status = await self.get_session_status()
            status.errors.append(f"Connection failed: {exc}")
            return status

        # get_session_status() already fetches the QR itself once the
        # session reaches SCAN_QR_CODE — nothing further to do here. If
        # WAHA hasn't reached that state yet (still STARTING), the caller
        # (session.py) re-polls anyway.
        return await self.get_session_status()

    async def disconnect(self) -> SendResult:
        # /logout deregisters the session from WhatsApp's servers gracefully,
        # but blocks until the browser job finishes — up to 60 s when Chromium
        # is mid-load. /stop just kills the browser process immediately and
        # leaves the session in STOPPED state, which is all we need to allow
        # a fresh QR-scan. Use /stop unless the session is genuinely WORKING
        # (i.e. already paired), where a proper logout from WA's servers matters.
        try:
            data = await self._get(f"/api/sessions/{self._session_name}")
            is_working = data.get("status") == "WORKING"
        except Exception:
            is_working = False

        endpoint = "logout" if is_working else "stop"
        try:
            await self._post(f"/api/sessions/{self._session_name}/{endpoint}", {})
        except Exception as exc:
            # /stop can 422 if the session is already stopped — ignore.
            if endpoint == "stop" and "422" in str(exc):
                return SendResult(success=True, details="Already stopped")
            return SendResult(success=False, details=str(exc))
        return SendResult(success=True, details="Logged out" if is_working else "Stopped")

    def _status_from_session(self, data: dict[str, Any]) -> SessionStatus:
        waha_status = data.get("status", "")
        me = data.get("me") or {}
        me_id: str | None = me.get("id")
        error: str | None = data.get("error")
        qr_code = None
        if waha_status == "SCAN_QR_CODE":
            qr_code = data.get("qr")  # populated if the caller asked WAHA to include it
        return SessionStatus(
            connected=waha_status not in ("STOPPED", "FAILED"),
            logged_in=waha_status == "WORKING",
            qr_code=qr_code,
            jid=_from_waha_jid(me_id) if me_id else None,
            name=me.get("pushName"),
            errors=[error] if waha_status == "FAILED" and error else [],
        )

    # ── Messaging (bridge.Bridge) ───────────────────────────────────────

    async def send_message(self, to: str, text: str) -> SendResult:
        return await self._send_result(
            self._post(
                "/api/sendText",
                {"session": self._session_name, "chatId": _to_waha_jid(to), "text": text},
            )
        )

    async def send_media(
        self, to: str, media_url: str, media_type: str, caption: str = ""
    ) -> SendResult:
        endpoint = {
            "image": "sendImage",
            "video": "sendVideo",
            "document": "sendFile",
            "audio": "sendFile",
        }.get(media_type, "sendFile")
        payload = {
            "session": self._session_name,
            "chatId": _to_waha_jid(to),
            "file": {"mimetype": "", "url": media_url, "filename": media_url.rsplit("/", 1)[-1]},
            "caption": caption,
        }
        return await self._send_result(self._post(f"/api/{endpoint}", payload))

    async def send_reaction(self, to: str, message_id: str, emoji: str) -> SendResult:
        return await self._send_result(
            self._post(
                "/api/reaction",
                {
                    "session": self._session_name,
                    "chatId": _to_waha_jid(to),
                    "messageId": message_id,
                    "emoji": emoji,
                },
            )
        )

    async def mark_read(self, to: str, message_ids: list[str]) -> SendResult:
        return await self._send_result(
            self._post(
                "/api/sendSeen",
                {
                    "session": self._session_name,
                    "chatId": _to_waha_jid(to),
                    "messageIds": message_ids,
                },
            )
        )

    async def edit_message(self, to: str, message_id: str, text: str) -> SendResult:
        try:
            data = await self._request(
                "PUT",
                f"/api/{self._session_name}/chats/{_to_waha_jid(to)}/messages/{message_id}",
                json={"text": text},
            )
        except Exception as exc:
            return SendResult(success=False, details=str(exc))
        return SendResult(
            success=True, details="Message updated", raw=data if isinstance(data, dict) else {}
        )

    async def delete_message(self, to: str, message_id: str) -> SendResult:
        try:
            await self._request(
                "DELETE",
                f"/api/{self._session_name}/chats/{_to_waha_jid(to)}/messages/{message_id}",
            )
        except Exception as exc:
            return SendResult(success=False, details=str(exc))
        return SendResult(success=True, details="Message deleted")

    async def send_location(
        self, to: str, latitude: float, longitude: float, name: str = ""
    ) -> SendResult:
        payload = {
            "session": self._session_name,
            "chatId": _to_waha_jid(to),
            "latitude": latitude,
            "longitude": longitude,
        }
        if name:
            payload["title"] = name
        return await self._send_result(self._post("/api/sendLocation", payload))

    async def send_contact(self, to: str, name: str, vcard: str) -> SendResult:
        # unverified — WAHA's exact contact-card endpoint/body wasn't
        # confirmed; best-effort per its otherwise-consistent send* shape.
        payload = {
            "session": self._session_name,
            "chatId": _to_waha_jid(to),
            "contacts": [{"vcard": vcard, "displayName": name}],
        }
        return await self._send_result(self._post("/api/sendContactVcard", payload))

    async def send_poll(self, group_jid: str, question: str, options: list[str]) -> SendResult:
        # unverified — see module docstring.
        payload = {
            "session": self._session_name,
            "chatId": group_jid,
            "poll": {"name": question, "options": options, "multipleAnswers": False},
        }
        return await self._send_result(self._post("/api/sendPoll", payload))

    async def set_chat_presence(self, to: str, state: str, media: str = "") -> SendResult:
        endpoint = "/api/startTyping" if state == "composing" else "/api/stopTyping"
        return await self._send_result(
            self._post(endpoint, {"session": self._session_name, "chatId": _to_waha_jid(to)})
        )

    # ── Groups (bridge.Bridge) ───────────────────────────────────────────

    async def list_groups(self) -> list[GroupInfo]:
        groups = await self._get(f"/api/{self._session_name}/groups")
        # Shape differs by engine: WEBJS returns a JSON array; NOWEB returns
        # an object keyed by group jid (confirmed live) — iterating a dict
        # directly yields its string keys, not the group objects, which is
        # why this crashed with "'str' object has no attribute 'get'" after
        # the WEBJS->NOWEB engine swap (see waha/fly.toml).
        if isinstance(groups, dict):
            groups = list(groups.values())
        return [self._group_from_raw(g) for g in (groups or [])]

    async def get_group_info(self, group_jid: str) -> GroupInfo:
        g = await self._get(f"/api/{self._session_name}/groups/{group_jid}")
        info = self._group_from_raw(g)
        try:
            participants = await self._get(
                f"/api/{self._session_name}/groups/{group_jid}/participants/v2"
            )
            info.participants = [
                p["id"] for p in (participants or []) if p.get("role") != "left" and p.get("id")
            ]
            info.participant_count = len(info.participants)
        except Exception as exc:
            logger.debug("Could not fetch participants for %s: %s", group_jid, exc)
        return info

    async def create_group(self, name: str, participants: list[str]) -> GroupInfo:
        # unverified response shape — see module docstring.
        g = await self._post(
            f"/api/{self._session_name}/groups",
            {"name": name, "participants": [{"id": _to_waha_jid(p)} for p in participants]},
        )
        return self._group_from_raw(g)

    async def leave_group(self, group_jid: str) -> SendResult:
        return await self._send_result(
            self._post(f"/api/{self._session_name}/groups/{group_jid}/leave", {})
        )

    async def get_group_invite_link(self, group_jid: str, reset: bool = False) -> str:
        if reset:
            try:
                await self._post(
                    f"/api/{self._session_name}/groups/{group_jid}/invite-code/revoke", {}
                )
            except Exception as exc:
                logger.warning("Failed to revoke invite code for %s: %s", group_jid, exc)
        data = await self._get(f"/api/{self._session_name}/groups/{group_jid}/invite-code")
        # unverified exact field name — try the plausible candidates.
        return data.get("inviteCode") or data.get("code") or data.get("link") or ""

    async def update_group_participants(
        self, group_jid: str, action: str, phones: list[str]
    ) -> SendResult:
        endpoint_by_action = {
            "add": f"/api/{self._session_name}/groups/{group_jid}/participants/add",
            "remove": f"/api/{self._session_name}/groups/{group_jid}/participants/remove",
            "promote": f"/api/{self._session_name}/groups/{group_jid}/admin/promote",
            "demote": f"/api/{self._session_name}/groups/{group_jid}/admin/demote",
        }
        endpoint = endpoint_by_action.get(action)
        if endpoint is None:
            return SendResult(success=False, details=f"Unknown action: {action}")
        payload = {"participants": [{"id": _to_waha_jid(p)} for p in phones]}
        return await self._send_result(self._post(endpoint, payload))

    async def set_group_name(self, group_jid: str, name: str) -> SendResult:
        # unverified endpoint — see module docstring.
        return await self._send_result(
            self._put(f"/api/{self._session_name}/groups/{group_jid}/subject", {"subject": name})
        )

    async def set_group_topic(self, group_jid: str, topic: str) -> SendResult:
        # unverified endpoint — see module docstring.
        return await self._send_result(
            self._put(
                f"/api/{self._session_name}/groups/{group_jid}/description", {"description": topic}
            )
        )

    # ── Contacts (bridge.Bridge) ─────────────────────────────────────────

    async def get_contacts(self) -> list[ContactInfo]:
        contacts = await self._get("/api/contacts/all", params={"session": self._session_name})
        return [self._contact_from_raw(c) for c in (contacts or []) if not c.get("isGroup")]

    async def get_contact(self, jid: str) -> ContactInfo | None:
        try:
            c = await self._get(
                "/api/contacts",
                params={"contactId": _to_waha_jid(jid), "session": self._session_name},
            )
        except Exception:
            return None
        return self._contact_from_raw(c) if c else None

    async def check_phone(self, phones: list[str]) -> list[ContactInfo]:
        results = []
        for phone in phones:
            try:
                data = await self._get(
                    "/api/contacts/check-exists",
                    params={"phone": phone, "session": self._session_name},
                )
            except Exception:
                results.append(
                    ContactInfo(jid="", name=phone, phone_number=phone, is_on_whatsapp=None)
                )
                continue
            chat_id = data.get("chatId") or ""
            results.append(
                ContactInfo(
                    jid=_from_waha_jid(chat_id) if chat_id else "",
                    name=phone,
                    phone_number=phone,
                    is_on_whatsapp=data.get("numberExists"),
                )
            )
        return results

    async def get_avatar_url(self, jid_or_phone: str, preview: bool = True) -> str | None:
        # A full jid needs translating (our canonical @s.whatsapp.net ->
        # WAHA's @c.us; @lid/@g.us pass through unchanged) — using it
        # as-is broke every @lid contact (privacy-preserving ids, common
        # for group participants) since @lid isn't @s.whatsapp.net and so
        # was left un-suffixed as a bare id WAHA doesn't recognize.
        contact_id = (
            _to_waha_jid(jid_or_phone) if "@" in jid_or_phone else f"{jid_or_phone}{_WAHA_SUFFIX}"
        )
        try:
            data = await self._get(
                "/api/contacts/profile-picture",
                params={"contactId": contact_id, "session": self._session_name},
            )
        except Exception:
            return None
        return data.get("profilePictureURL") or None

    # ── Chat/message history (message_store.MessageStore) ───────────────
    # WAHA has no shared Postgres schema to read directly — this reads
    # WAHA's own REST API instead, normalized to the dict shape
    # message_store.MessageStore's callers expect.

    async def get_chats(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        chats = await self._get(
            f"/api/{self._session_name}/chats",
            params={
                "limit": str(limit),
                "offset": str(offset),
                # Confirmed live (previously an unverified guess —
                # "messageTimestamp" — that 400'd): WAHA's sortBy enum is
                # conversationTimestamp/id/name only.
                "sortBy": "conversationTimestamp",
                "sortOrder": "desc",
            },
        )
        return [self._chat_summary_from_raw(c) for c in (chats or [])]

    async def get_chat(self, jid: str) -> dict[str, Any] | None:
        try:
            c = await self._get(f"/api/{self._session_name}/chats/{_to_waha_jid(jid)}")
        except Exception:
            return None
        return self._chat_summary_from_raw(c) if c else None

    async def list_messages(
        self, jid: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        # Over-fetch and filter client-side: WAHA's limit/offset paginate
        # the *raw* feed, which includes protocol/system noise (see
        # _is_real_message) — paginating before filtering would silently
        # short-change a page whenever noise fell inside the requested
        # window. The multiplier is a bounded guess, not exact — same
        # trade-off search_messages below makes at a larger fixed scale.
        fetch_n = min((offset + limit) * 3 + 20, 1000)
        messages = await self._get(
            f"/api/{self._session_name}/chats/{_to_waha_jid(jid)}/messages",
            params={"limit": str(fetch_n), "offset": "0"},
        )
        real = [m for m in (messages or []) if _is_real_message(m)]
        page = real[offset : offset + limit]
        if not page:
            return []
        # chat_name/sender_name enrichment: one bulk lookup each, not one
        # per message — skipped entirely above when the page is empty.
        chat_name, contact_names, lid_to_phone = await asyncio.gather(
            self._resolve_chat_name(jid), self._contact_names(), self._lid_to_phone()
        )
        return [
            self._message_from_raw(
                jid,
                m,
                chat_name=chat_name,
                contact_names=contact_names,
                lid_to_phone=lid_to_phone,
            )
            for m in page
        ]

    async def search_messages(
        self, query: str, jid: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        # WAHA has no cross-chat full-text search endpoint — fetch and
        # filter client-side, scoped to one chat if given, or across every
        # chat otherwise. Fine at family scale; would need rethinking for
        # a large number of chats.
        query_lower = query.lower()
        matches: list[tuple[str, dict[str, Any]]] = []
        if jid is not None:
            jids = [jid]
            chat_names: dict[str, str] = {}
        else:
            chats = await self.get_chats(limit=1000, offset=0)
            jids = [c["jid"] for c in chats]
            # Already fetched to build jids above — reuse it for chat_name
            # instead of a separate per-chat lookup.
            chat_names = {c["jid"]: c["name"] for c in chats}
        # One independent HTTP round-trip per chat — fire them concurrently
        # rather than paying N sequential round-trips for N chats.
        results = await asyncio.gather(
            *(
                self._get(
                    f"/api/{self._session_name}/chats/{_to_waha_jid(chat_jid)}/messages",
                    params={"limit": "1000", "offset": "0"},
                )
                for chat_jid in jids
            ),
            return_exceptions=True,
        )
        for chat_jid, messages in zip(jids, results, strict=True):
            if isinstance(messages, BaseException):
                continue
            for m in messages or []:
                if not _is_real_message(m):
                    continue
                if query_lower in (m.get("body") or "").lower():
                    matches.append((chat_jid, m))
        # Sort on the raw numeric epoch, before _message_from_raw converts
        # it to an ISO string below — keeps this a plain numeric comparison
        # rather than leaning on ISO-8601 strings' lexicographic ordering.
        matches.sort(key=lambda pair: pair[1].get("timestamp") or 0, reverse=True)
        page = matches[offset : offset + limit]
        if not page:
            return []
        # The jid-scoped case above has no bulk get_chats result to borrow
        # a chat_name from — resolve it now (only once matches exist), in
        # parallel with the sender-name lookups.
        if jid is not None:
            chat_names[jid], contact_names, lid_to_phone = await asyncio.gather(
                self._resolve_chat_name(jid), self._contact_names(), self._lid_to_phone()
            )
        else:
            contact_names, lid_to_phone = await asyncio.gather(
                self._contact_names(), self._lid_to_phone()
            )
        return [
            self._message_from_raw(
                chat_jid,
                m,
                chat_name=chat_names.get(chat_jid),
                contact_names=contact_names,
                lid_to_phone=lid_to_phone,
            )
            for chat_jid, m in page
        ]

    async def get_last_message(self, jid: str) -> dict[str, Any] | None:
        messages = await self.list_messages(jid, limit=1, offset=0)
        return messages[0] if messages else None

    async def search_chats(
        self, query: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        query_lower = query.lower()
        all_chats = await self.get_chats(limit=1000, offset=0)
        matches = [
            c
            for c in all_chats
            if query_lower in (c.get("name") or "").lower() or query_lower in c["jid"].lower()
        ]
        return matches[offset : offset + limit]

    async def get_message_chat_jid(self, message_id: str) -> str | None:
        """Resolve a message_id to the chat_jid it belongs to.

        WAHA's message ids *are* whatsapp-web.js's serialized message-key
        string — "{fromMe}_{remoteJid}_{id}" for a direct chat, or
        "{fromMe}_{remoteJid}_{id}_{participant}" for a group (confirmed
        live: matches the raw message's own `_data.id.$1`/`_serialized`
        field exactly, for both shapes). remoteJid *is* the chat jid, so
        this is a plain string parse in the common case — no request at
        all — rather than a lookup.

        Only falls back to the old "scan every chat's recent messages"
        approach when a message_id doesn't match that shape (a WAHA format
        change, or a synthetic id from somewhere else) — belt-and-braces,
        not the expected path, and the reason this method stays async.
        """
        match = _MESSAGE_ID_CHAT_RE.match(message_id)
        if match:
            return _from_waha_jid(match.group(1))
        return await self._get_message_chat_jid_by_scan(message_id)

    async def _get_message_chat_jid_by_scan(self, message_id: str) -> str | None:
        # No cross-chat "look up by message id" endpoint — same trade-off
        # as search_messages above: scan every chat's recent messages
        # client-side. Fine at family scale; would need rethinking for a
        # large number of chats. (In practice this is now a fallback path —
        # see get_message_chat_jid — since a real WAHA message_id is
        # parseable directly.)
        jids = [c["jid"] for c in await self.get_chats(limit=1000, offset=0)]
        results = await asyncio.gather(
            *(
                self._get(
                    f"/api/{self._session_name}/chats/{_to_waha_jid(chat_jid)}/messages",
                    params={"limit": "1000", "offset": "0"},
                )
                for chat_jid in jids
            ),
            return_exceptions=True,
        )
        for chat_jid, messages in zip(jids, results, strict=True):
            if isinstance(messages, BaseException):
                continue
            for m in messages or []:
                if m.get("id") == message_id:
                    return chat_jid
        return None

    async def get_media(self, message_id: str, chat_jid: str) -> dict[str, Any] | None:
        """Fetch a message's media bytes + mimetype straight from WAHA.

        WAHA keeps its own persistent copy of everything it has downloaded
        (WHATSAPP_FILES_LIFETIME=0 — see
        docs/decisions/0007-sqlite-for-metadata-store.md) and serves it
        back over its own REST API; this project doesn't need — and, until
        now, didn't actually have — a local media cache of its own.

        Two calls: the per-message endpoint (confirmed live to return a
        "media": {"url", "filename", "mimetype"} object whenever
        "hasMedia" is true) to learn where the file lives, then a plain GET
        against that URL for the bytes. Returns None if the message has no
        media, or WAHA no longer has a copy of it.

        The "url" WAHA hands back is built from *its own* idea of its
        public address (confirmed live: a fixed "http://localhost:3000/..."
        regardless of the host/port this client actually used to reach
        it) — not necessarily how this process can reach it (e.g. the
        "waha" Docker-network hostname, or Fly's 6PN
        botsapp-waha.internal). Only the path is trustworthy; refetch
        against self._base_url instead of following the URL as given.
        """
        try:
            data = await self._get(
                f"/api/{self._session_name}/chats/{_to_waha_jid(chat_jid)}/messages/{message_id}"
            )
        except Exception as exc:
            logger.debug("Could not fetch message %s for media: %s", message_id, exc)
            return None
        media = (data or {}).get("media") or {}
        raw_url = media.get("url")
        if not raw_url:
            return None
        parsed = urlsplit(raw_url)
        url = f"{self._base_url}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")
        http = await self._get_http()
        async with http.request("GET", url, headers=self._headers()) as resp:
            if resp.status >= 400:
                logger.warning("WAHA media fetch %s → %s", url, resp.status)
                return None
            file_bytes = await resp.read()
        return {
            "data": file_bytes,
            "mimetype": media.get("mimetype") or "application/octet-stream",
            "filename": media.get("filename"),
        }

    # ── Internal helpers ─────────────────────────────────────────────────

    async def _send_result(self, coro) -> SendResult:
        try:
            data = await coro
        except Exception as exc:
            return SendResult(success=False, details=str(exc))
        if isinstance(data, dict):
            return SendResult(success=True, message_id=data.get("id"), details="", raw=data)
        return SendResult(success=True, raw={})

    @staticmethod
    def _group_from_raw(g: dict[str, Any]) -> GroupInfo:
        # Shape differs by engine (see list_groups()'s comment on the dict-
        # vs-array split for the same swap): under WEBJS, the fields that
        # matter here live one level down under "groupMetadata"; under
        # NOWEB (confirmed live), /api/{session}/groups and
        # /groups/{jid} both return them flat at the top level instead
        # ("subject" for name, "isCommunity" directly) — check both.
        # "desc" for the topic text is WhatsApp's usual internal field name
        # for this but wasn't directly confirmed under either engine (no
        # group with a topic set to test against).
        meta = g.get("groupMetadata") or {}
        participants = meta.get("participants") or g.get("participants") or []
        # Exclude departed members here too, matching get_group_info()'s
        # later /participants/v2 filter — otherwise list_groups() and
        # get_group_info() report different counts for the same group.
        active_participant_ids = [
            _serialized_id(p.get("id"))
            for p in participants
            if isinstance(p, dict) and p.get("id") and p.get("role") != "left"
        ]
        return GroupInfo(
            jid=_serialized_id(g.get("id") or meta.get("id") or ""),
            name=g.get("name") or meta.get("subject") or g.get("subject") or "",
            topic=meta.get("desc") or meta.get("description") or g.get("desc") or "",
            participant_count=len(active_participant_ids),
            participants=active_participant_ids,
            is_community=bool(
                meta.get("isParentGroup") or meta.get("isCommunity") or g.get("isCommunity")
            ),
            invite_link=g.get("invite") or meta.get("invite") or None,
        )

    @staticmethod
    def _contact_from_raw(c: dict[str, Any]) -> ContactInfo:
        jid = c.get("id", "")
        raw_name = c.get("name") or ""
        name = "" if _looks_like_masked_phone(raw_name) else raw_name
        return ContactInfo(
            jid=_from_waha_jid(jid) if jid else "",
            name=name or c.get("pushname") or c.get("shortName") or jid,
            phone_number=c.get("number", ""),
            is_on_whatsapp=c.get("isWAContact"),
        )

    @staticmethod
    def _chat_summary_from_raw(c: dict[str, Any]) -> dict[str, Any]:
        chat_id = _serialized_id(c.get("id", ""))
        # /api/{session}/chats' lastMessage is whatsapp-web.js's raw
        # internal Store.Msg object (nested under "_data", full of
        # WhatsApp-internal fields) rather than the clean flat shape the
        # dedicated /messages endpoint returns — confirmed live. Pull just
        # what we need out of it, and skip non-text bodies (media messages
        # put a base64 data blob or empty string in "body", not a caption).
        last_data = (c.get("lastMessage") or {}).get("_data") or {}
        last_message = last_data.get("body") if last_data.get("type") == "chat" else None
        return {
            "jid": _from_waha_jid(chat_id),
            "name": c.get("name") or chat_id,
            "last_message": last_message or None,
            # The chat's own "timestamp" (what sortBy=conversationTimestamp
            # sorts on) is more reliable than digging through lastMessage.
            # Normalized to ISO-8601 — see _iso_timestamp.
            "last_message_at": _iso_timestamp(c.get("timestamp")),
        }

    async def _resolve_chat_name(self, jid: str) -> str:
        """Best-effort chat display name for a single jid.

        get_chat() already catches its own request errors and returns
        None, so a chat that can't be fetched (or has no "name" set)
        falls back to the jid itself here — the same behavior
        _message_from_raw had before chat_name was resolved at all.
        """
        chat = await self.get_chat(jid)
        return (chat or {}).get("name") or jid

    async def _contact_names(self) -> dict[str, str]:
        """Bulk sender-name lookup: jid -> contact display name.

        One call, reused across every message in a page, instead of one
        contact lookup per sender. Falls back to an empty map (message
        rows then just carry the raw jid in "sender_name" too, same as
        before this existed) if the contacts list can't be fetched.
        """
        try:
            contacts = await self.get_contacts()
        except Exception as exc:
            logger.debug("Could not fetch contacts for sender-name lookup: %s", exc)
            return {}
        return {c.jid: c.name for c in contacts if c.jid}

    async def _lid_to_phone(self) -> dict[str, str]:
        """Bulk @lid -> canonical phone-jid map, for sender-name resolution.

        WhatsApp increasingly hands group participants a privacy-preserving
        @lid instead of their phone number (see _to_waha_jid's docstring) —
        _contact_names() above is keyed by phone-based jids, so it can't
        match a @lid sender at all, contact saved or not. WAHA exposes the
        mapping itself. Deliberately not cached/persisted on this side:
        WAHA already stores it durably (this call is just asking WAHA for
        it fresh, same "fetch bulk once per call" trade-off get_chats() and
        _contact_names() already make above), and its resolution is known
        to fill in over time — a cached snapshot here would only risk
        going stale against WAHA's own, more current, answer.
        Unverified against a live paired session — see module docstring.
        """
        try:
            rows = await self._get(
                f"/api/{self._session_name}/lids", params={"limit": "1000", "offset": "0"}
            )
        except Exception as exc:
            logger.debug("Could not fetch lid->phone mappings: %s", exc)
            return {}
        result: dict[str, str] = {}
        for row in rows or []:
            lid, pn = row.get("lid"), row.get("pn")
            if lid and pn:
                result[_from_waha_jid(lid)] = _from_waha_jid(pn)
        return result

    @staticmethod
    def _message_from_raw(
        chat_jid: str,
        m: dict[str, Any],
        *,
        chat_name: str | None = None,
        contact_names: dict[str, str] | None = None,
        lid_to_phone: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        from_jid = _from_waha_jid(m.get("from", ""))
        # A @lid sender needs translating to its phone jid before a contact
        # lookup can match it at all — falls back to from_jid unchanged
        # when it's already phone-based, or the mapping is unknown.
        lookup_jid = (lid_to_phone or {}).get(from_jid, from_jid)
        return {
            "message_id": m.get("id"),
            "from": from_jid,
            # Resolved display name for "from" — via the @lid mapping above
            # when needed, then falling back to the phone number (still
            # more useful than an opaque @lid) or, last resort, the raw
            # from_jid exactly as WAHA gave it.
            "sender_name": (contact_names or {}).get(lookup_jid) or lookup_jid,
            "to": chat_jid,
            "chat_jid": chat_jid,
            "chat_name": chat_name or chat_jid,
            "text": m.get("body"),
            # ISO-8601 UTC string, not a raw epoch int — see _iso_timestamp.
            "timestamp": _iso_timestamp(m.get("timestamp")),
            "message_type": "media" if m.get("hasMedia") else "text",
            # Not WAHA's own file URL — that's internal to the private
            # waha/mcp-server network and needs the WAHA API key to fetch,
            # so it wouldn't be usable by an MCP client anyway. Kept as an
            # explicit None rather than dropped, so callers can still see
            # the field; get_media() is the one place that actually
            # resolves+fetches it, using the server's own credentials.
            "media_url": None,
            "mimetype": (m.get("media") or {}).get("mimetype"),
        }
