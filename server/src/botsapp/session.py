"""
Shared WhatsApp session connect/status orchestration.

Used by the admin UI's session page (admin/app.py, a plain browser page
reachable only over the private network), the only place session/pairing
happens now. This used to also back an MCP-reachable
whatsapp_session/whatsapp_session_poll tool pair, removed as redundant once
the admin UI covered it. Kept as its own module regardless, since a second
consumer could come back later and this check-then-maybe-connect logic
shouldn't live inside admin/app.py itself.

Genuinely bridge-agnostic: every bridge-specific detail (error string
formats, the exact multi-step dance a bridge needs to start a session and
surface a QR code) lives inside that bridge's own Bridge.get_session_status()/
connect() implementation (see bridge.py, waha_client.py). This module only
orchestrates the check-then-maybe-connect decision, the same regardless of
which bridge is active.
"""

import logging

from botsapp.bridge import Bridge, SessionStatus

logger = logging.getLogger(__name__)


async def connect_or_get_status(bridge: Bridge) -> SessionStatus:
    """Check current WhatsApp session status; connect if not already
    connected. Never raises — every failure mode is captured in
    ``SessionStatus.errors`` instead, since both callers (an MCP tool, a
    plain HTTP page) want a renderable result rather than an exception to
    handle themselves.
    """
    status = await bridge.get_session_status()

    # Only attempt to connect on a clean "nothing going on yet" status —
    # not if the status check itself already reported a problem (e.g.
    # unreachable, token rejected). Retrying connect on top of a known
    # error is more likely to compound it than fix it; the caller should
    # see the error and decide whether to retry.
    if not status.connected and not status.logged_in and not status.qr_code and not status.errors:
        status = await bridge.connect()

    if not status.logged_in and not status.qr_code and not status.errors:
        status.errors.append("Not logged in — no QR code available. Try reconnecting.")
    elif not status.logged_in and status.qr_code and status.hint is None:
        status.hint = (
            "QR code displayed. After scanning, poll session status again "
            "to confirm login succeeded."
        )
    return status


async def get_status(bridge: Bridge) -> SessionStatus:
    """Poll-only status check (no connect attempt) — for repeated polling."""
    return await bridge.get_session_status()
