"""
Admin Session/Accounts page (docs/decisions/0025, 0026, 0029, #50): WhatsApp
accounts, who may use them, and live pairing for each.

* Admins create and delete accounts (and so their WAHA sessions); every
  account's owner, or an admin, sees and drives its live pairing state
  (Connect/QR/Disconnect/Force re-pair) right on its own card — no separate
  "select an account, then go to a Session page" step (0029: Session and
  Accounts were two pages with the same "whose WhatsApp is this and is it
  connected" content; merged into one).
* An account's owner — or an admin — grants it to, and revokes it from, existing
  users. Revoking also removes everything that viewer had been shown.
* Anyone can select, from the accounts available to them, the one the Chats
  page acts on (the only page left that needs a single "selected account").

Ordering, so a failure leaves nothing half-made:

* create — DB row first (the registry resolves the WAHA session through it), then
  the WAHA session; if WAHA fails the row is removed again.
* delete — the WAHA session first (a missing session is fine), then the row with
  its grants and chat access; if WAHA fails nothing changes, and if the row delete
  fails retrying is safe. The ``default`` account can't be deleted: the Chats
  page falls back to it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict
from typing import Any
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import botsapp.state as state
from botsapp.admin import auth, templates
from botsapp.admin.prefix import p
from botsapp.admin.users import _back, _form_text, live_admin_check
from botsapp.bridge import SessionStatus
from botsapp.bridge_registry import session_name_for
from botsapp.key_manager import KeyManagerError, describe_drift, drift_from_policy
from botsapp.session import connect_or_get_status, get_status
from botsapp.waha_client import WAHAClient

logger = logging.getLogger(__name__)

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$")
_BACK = p("/")


def _err(text: str) -> RedirectResponse:
    return _back("error", text, _BACK)


def _ok(text: str) -> RedirectResponse:
    return _back("notice", text, _BACK)


async def accounts_redirect(_request: Request) -> Response:
    """The old standalone Accounts page lives on as / (0029)."""
    return RedirectResponse(url=p("/"), status_code=308)


async def _bridge_for(account_id: str) -> WAHAClient:
    assert state.registry is not None
    return await state.registry.for_account(account_id, purpose="admin")


async def _account_status(account_id: str) -> SessionStatus:
    """Live pairing status for one account's card; never raises."""
    try:
        return await get_status(await _bridge_for(account_id))
    except Exception as exc:
        logger.warning("session status for %s unavailable: %s", account_id, exc)
        return SessionStatus(errors=[str(exc)])


async def session_index(request: Request) -> Response:
    assert state.db is not None
    user = request.state.user
    email, is_admin = user["email"], bool(user["is_admin"])
    every = await state.db.list_accounts()
    manageable = every if is_admin else [a for a in every if a["owner_email"] == email]
    mine = {a["id"] for a in await state.db.list_accounts_for_user(email)}
    shared = [a for a in every if a["id"] in mine and a["owner_email"] != email]
    users = [u["email"] for u in await state.db.list_users()]

    statuses = await asyncio.gather(*(_account_status(a["id"]) for a in manageable))
    rows: list[dict[str, Any]] = []
    for account, status in zip(manageable, statuses, strict=True):
        granted = await state.db.list_grants(account["id"])
        rows.append(
            {
                "id": account["id"],
                "label": account["label"] or account["id"],
                "owner": account["owner_email"],
                "session": asdict(status),
                "granted": granted,
                "grantable": [u for u in users if u != account["owner_email"] and u not in granted],
                "deletable": is_admin and account["id"] != auth.DEFAULT_ACCOUNT_ID,
                "manage_key": is_admin,
                "key_id": account["waha_key_id"],
                "key_drift": (
                    describe_drift(drift_from_policy(account["waha_key_actions"] or {}))
                    if account["waha_key_id"]
                    else ""
                ),
                "can_store_key": bool(state.key_manager and state.key_manager.can_store),
            }
        )
    return HTMLResponse(
        templates.session_page(
            rows,
            shared=[(a["id"], a["label"] or a["id"], a["owner_email"]) for a in shared],
            users=users,
            is_admin=is_admin,
            current=request.state.account or "",
            notice=request.query_params.get("notice", ""),
            error=request.query_params.get("error", ""),
        )
    )


async def session_connect(request: Request) -> Response:
    """The only route that actually calls the bridge's connect — a deliberate
    user action (button click), not an automatic side effect of viewing or
    polling the account's card."""
    account = await _manageable(request)
    if isinstance(account, Response):
        return account
    await connect_or_get_status(await _bridge_for(account["id"]))
    return RedirectResponse(url=_BACK, status_code=303)


async def session_disconnect(request: Request) -> Response:
    """Log out of WhatsApp (WAHA logout endpoint). Leaves the session in
    STOPPED state so the owner can immediately hit Connect WhatsApp and
    re-pair with a fresh QR."""
    account = await _manageable(request)
    if isinstance(account, Response):
        return account
    await (await _bridge_for(account["id"])).disconnect()
    return RedirectResponse(url=_BACK, status_code=303)


async def session_reset(request: Request) -> Response:
    """Nuclear option: delete the WAHA session entirely (removes all
    persisted auth material). Use this when logout alone isn't enough to
    escape a stuck WORKING state. After deletion, Connect WhatsApp will
    create a fresh session and show a new QR code.

    Named ``reset``, not ``delete`` — ``/accounts/{id}/session/delete``
    would otherwise end in ``/delete`` and collide with _policy's
    admin-only "delete the whole account" pattern, which matches any path
    ending that way."""
    account = await _manageable(request)
    if isinstance(account, Response):
        return account
    try:
        await (await _bridge_for(account["id"])).delete_session()
    except Exception as exc:
        logger.warning("session reset failed for %s: %s", account["id"], exc)
    return RedirectResponse(url=_BACK, status_code=303)


async def session_status_json(request: Request) -> Response:
    """Polled by a card's QR-waiting panel client-side (see
    templates._pairing_panel) to refresh the QR/connected state in place,
    without reloading / and without ever re-triggering connect."""
    account = await _manageable(request)
    if isinstance(account, Response):
        return account
    return JSONResponse(asdict(await _account_status(account["id"])))


async def create_account(request: Request) -> Response:
    assert state.db is not None and state.registry is not None
    form = await request.form()
    account_id = _form_text(form, "id").lower()
    label = _form_text(form, "label")
    owner = _form_text(form, "owner").lower()
    if not _ID.match(account_id):
        return _err("Account id: 2–32 lowercase letters, digits or hyphens.")
    if await state.db.get_account(account_id) is not None:
        return _err(f"Account '{account_id}' already exists.")
    if await state.db.get_user(owner) is None:
        return _err(f"{owner or 'That owner'} is not a user.")
    if refused := await live_admin_check(request, _BACK):
        return refused

    await state.db.add_account(account_id, owner, session_name_for(account_id), label=label)
    try:
        await state.registry.create_session(account_id)
    except Exception as exc:
        logger.exception("creating the WAHA session for %s failed", account_id)
        await state.db.delete_account(account_id)
        return _err(f"Couldn't create the WhatsApp session ({exc}); nothing was created.")

    manager = state.key_manager
    if manager is None or not manager.can_store:
        return _ok(
            f"Created account '{account_id}' for {owner}, but no access key: BOTSAPP_SECRET_KEY "
            "or the WAHA admin key isn't set here. Until one is created the MCP server "
            "uses the shared key for it."
        )
    try:
        await manager.provision(account_id)
    except KeyManagerError as exc:
        # Undo the session and the row: an account without its key would silently
        # fall back to (or, in strict mode, be locked out of) the shared key.
        try:
            await state.registry.delete_session(account_id)
        except Exception:
            logger.exception(
                "could not remove the WAHA session for %s after a key failure", account_id
            )
        await state.db.delete_account(account_id)
        return _err(f"Couldn't create the access key ({exc}); nothing was created.")
    return _ok(f"Created account '{account_id}' for {owner}. They can now pair it.")


async def delete_account(request: Request) -> Response:
    assert state.db is not None and state.registry is not None
    account_id = request.path_params["account_id"]
    if account_id == auth.DEFAULT_ACCOUNT_ID:
        return _err("The default account can't be deleted.")
    if await state.db.get_account(account_id) is None:
        return _err("Unknown account.")
    if refused := await live_admin_check(request, _BACK):
        return refused

    account = await state.db.get_account(account_id)
    if account is not None and account["waha_key_id"]:
        if state.key_manager is None:
            return _err("Can't delete the account's access key: the WAHA admin key isn't set here.")
        try:
            await state.key_manager.revoke(account_id)
        except KeyManagerError as exc:
            return _err(str(exc))
    try:
        await state.registry.delete_session(account_id)
    except Exception as exc:
        logger.exception("deleting the WAHA session for %s failed", account_id)
        return _err(f"Couldn't delete the WhatsApp session ({exc}); nothing was changed.")
    try:
        await state.db.delete_account(account_id)
    except Exception:
        logger.exception("deleted the WAHA session for %s but not its record", account_id)
        return _err("The session was deleted but the record wasn't; try Delete again.")
    return _ok(f"Deleted account '{account_id}'.")


async def _manageable(request: Request) -> dict[str, Any] | Response:
    """The account in the path if the caller owns it or is an admin, else the
    error response to return."""
    assert state.db is not None
    account = await state.db.get_account(request.path_params["account_id"])
    user = request.state.user
    if account is None or not (user["is_admin"] or account["owner_email"] == user["email"]):
        # Same answer for "missing" and "not yours": ids aren't probeable.
        return _err("Unknown account.")
    return account


async def grant_account(request: Request) -> Response:
    assert state.db is not None
    account = await _manageable(request)
    if isinstance(account, Response):
        return account
    email = _form_text(await request.form(), "email").lower()
    if await state.db.get_user(email) is None:
        return _err(f"{email or 'That person'} is not a user.")
    if email == account["owner_email"]:
        return _err("The owner already has this account.")
    await state.db.grant_account(account["id"], email)
    return _ok(f"{email} can now use '{account['id']}'. Choose what they see on the Chats page.")


async def revoke_account(request: Request) -> Response:
    assert state.db is not None
    account = await _manageable(request)
    if isinstance(account, Response):
        return account
    email = _form_text(await request.form(), "email").lower()
    await state.db.revoke_account(account["id"], email)
    return _ok(f"{email} no longer has access to '{account['id']}'.")


async def select_account(request: Request) -> Response:
    """Set which account the Session and Chats pages act on. The cookie is only
    a preference: auth.py re-validates it against what the user may select on
    every request."""
    assert state.db is not None
    user = request.state.user
    account_id = request.path_params["account_id"]
    selectable = (
        await state.db.list_accounts()
        if user["is_admin"]
        else await state.db.list_accounts_for_user(user["email"])
    )
    if account_id not in {a["id"] for a in selectable}:
        return _err("Unknown account.")

    target = _BACK
    referer = urlparse(request.headers.get("referer", ""))
    if request.query_params.get("next"):
        target = auth._safe_next(request.query_params["next"])
    elif referer.netloc and referer.netloc == request.headers.get("host") and referer.path:
        target = auth._safe_next(referer.path)
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(
        auth.ACCOUNT_COOKIE,
        account_id,
        max_age=365 * 24 * 3600,
        httponly=True,
        secure=bool(auth.config and auth.config.secure_cookies),
        samesite="lax",
        path="/",
    )
    return response


async def _key_action(request: Request, action: str) -> Response:
    assert state.db is not None
    account_id = request.path_params["account_id"]
    if await state.db.get_account(account_id) is None:
        return _err("Unknown account.")
    manager = state.key_manager
    if manager is None:
        return _err("The WAHA admin key isn't set here, so keys can't be managed.")
    if refused := await live_admin_check(request, _BACK):
        return refused
    try:
        if action == "create":
            await manager.provision(account_id)
            return _ok(f"Created a read-only access key for '{account_id}'.")
        if action == "rotate":
            _info, old_deleted = await manager.rotate(account_id)
            extra = (
                "" if old_deleted else " The old key couldn't be deleted in WAHA; remove it there."
            )
            return _ok(f"Rotated the access key for '{account_id}'.{extra}")
        verification = await manager.verify(account_id)
    except KeyManagerError as exc:
        return _err(str(exc))
    if verification.drift:
        drift = describe_drift(verification.drift)
        return _err(
            f"The key's scope in WAHA differs from what we want ({drift}). "
            "Rotate it to restore the intended scope."
        )
    return _ok(f"The access key for '{account_id}' is intact and read-only.")


async def key_create(request: Request) -> Response:
    return await _key_action(request, "create")


async def key_rotate(request: Request) -> Response:
    return await _key_action(request, "rotate")


async def key_verify(request: Request) -> Response:
    return await _key_action(request, "verify")
