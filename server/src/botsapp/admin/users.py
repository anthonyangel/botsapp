"""
Admin Users page (docs/decisions/0026, #49): who is a botsapp user, and whether
they are an ``admin`` or a ``member``.

With WorkOS connected (``auth.config.directory``) every change is one operation
across WorkOS and the local ``users`` table, ordered so a failure never leaves
the two disagreeing in a harmful way:

* invite  — WorkOS invitation first, then the ``users`` row; if the row can't be
  written the invitation is revoked again. A failed invite leaves no row.
* remove  — refused up front (before touching WorkOS) when the user still owns an
  account or is the caller; then WorkOS, then the row.
  ``directory.remove`` is idempotent, so a failed row delete is fixed by retrying.
* re-role — WorkOS first, then the local ``is_admin`` mirror; if the mirror write
  fails the WorkOS role is put back.

The caller's own administrator role is re-checked live against WorkOS on every
change (their token or cookie may be older than a demotion). The bootstrap admin
(``BOTSAPP_ADMIN_EMAIL``) is also accepted when WorkOS has no role for them yet,
so first-time setup can't lock the owner out.

There is deliberately no "last administrator" guard: the caller must be an admin
and cannot target themselves, so at least one admin always remains.

Without a directory (dev, or the private ``ADMIN_DISABLE_AUTH`` setup) the page
edits only the local table and says so.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlencode

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

import botsapp.state as state
from botsapp.admin import auth, templates
from botsapp.admin.directory import ROLES, DirectoryError

logger = logging.getLogger(__name__)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _back(kind: str, text: str) -> RedirectResponse:
    return RedirectResponse(url=f"/users?{urlencode({kind: text})}", status_code=303)


def _form_text(form: FormData, key: str) -> str:
    value = form.get(key)
    return value.strip() if isinstance(value, str) else ""


def _actor(request: Request) -> str:
    return request.state.user["email"]


async def _live_admin_check(request: Request) -> Response | None:
    """None if the caller is still an admin according to WorkOS, else a response."""
    cfg = auth.config
    assert cfg is not None
    if cfg.directory is None or cfg.dev_user:
        return None
    actor = _actor(request)
    try:
        role = await cfg.directory.get_role(actor)
    except DirectoryError as exc:
        return _back("error", f"Couldn't verify your role with WorkOS: {exc}")
    bootstrap = os.environ.get("BOTSAPP_ADMIN_EMAIL", "").strip().lower()
    if role == "admin" or (role is None and actor == bootstrap):
        return None
    return auth._denied(403, "Your administrator role was changed in WorkOS.")


async def users_index(request: Request) -> Response:
    assert state.db is not None and auth.config is not None
    return HTMLResponse(
        templates.users_page(
            await state.db.list_users(),
            current=_actor(request),
            managed=auth.config.directory is not None,
            notice=request.query_params.get("notice", ""),
            error=request.query_params.get("error", ""),
        )
    )


async def invite_user(request: Request) -> Response:
    assert state.db is not None and auth.config is not None
    directory = auth.config.directory
    form = await request.form()
    email = _form_text(form, "email").lower()
    role = _form_text(form, "role") or "member"
    if not _EMAIL.match(email):
        return _back("error", "Enter a valid email address.")
    if role not in ROLES:
        return _back("error", "Unknown role.")
    if await state.db.get_user(email) is not None:
        return _back("error", f"{email} is already a user.")
    if refused := await _live_admin_check(request):
        return refused

    invitation_id: str | None = None
    if directory is not None:
        try:
            invitation_id = await directory.invite(email, role)
        except DirectoryError as exc:
            return _back("error", f"Invitation failed: {exc}")
    try:
        await state.db.add_user(email, is_admin=role == "admin")
    except Exception:
        logger.exception("could not record %s after inviting; revoking the invitation", email)
        if directory is not None and invitation_id is not None:
            try:
                await directory.revoke_invitation(invitation_id)
            except DirectoryError as exc:
                logger.error("could not revoke invitation %s: %s", invitation_id, exc)
        return _back("error", "Couldn't save the user; the invitation was cancelled.")
    return _back("notice", f"Invited {email} as {role}." if directory else f"Added {email}.")


async def remove_user(request: Request) -> Response:
    assert state.db is not None and auth.config is not None
    directory = auth.config.directory
    email = request.path_params["email"].strip().lower()
    user = await state.db.get_user(email)
    if user is None:
        return _back("error", f"{email} is not a user.")
    if email == _actor(request):
        return _back("error", "You can't remove yourself; ask another administrator.")
    if owned := await state.db.owned_account_count(email):
        return _back(
            "error", f"{email} owns {owned} WhatsApp account(s); delete or transfer them first."
        )
    if refused := await _live_admin_check(request):
        return refused

    if directory is not None:
        try:
            await directory.remove(email)
        except DirectoryError as exc:
            return _back("error", f"Removal failed: {exc}")
    try:
        await state.db.remove_user(email)
    except Exception:
        logger.exception("removed %s from WorkOS but not locally", email)
        return _back("error", "Removed from WorkOS but not locally; try Remove again.")
    return _back("notice", f"Removed {email}.")


async def change_role(request: Request) -> Response:
    assert state.db is not None and auth.config is not None
    directory = auth.config.directory
    email = request.path_params["email"].strip().lower()
    role = _form_text(await request.form(), "role")
    user = await state.db.get_user(email)
    if user is None:
        return _back("error", f"{email} is not a user.")
    if role not in ROLES:
        return _back("error", "Unknown role.")
    if email == _actor(request):
        return _back("error", "You can't change your own role; ask another administrator.")
    previous = "admin" if user["is_admin"] else "member"
    if role == previous:
        return _back("notice", f"{email} is already {role}.")
    if refused := await _live_admin_check(request):
        return refused

    if directory is not None:
        try:
            await directory.set_role(email, role)
        except DirectoryError as exc:
            return _back("error", f"Role change failed: {exc}")
    try:
        await state.db.set_user_admin(email, role == "admin")
    except Exception:
        logger.exception("could not record %s's new role; restoring it in WorkOS", email)
        if directory is not None:
            try:
                await directory.set_role(email, previous)
            except DirectoryError as exc:
                logger.error("could not restore %s to %s in WorkOS: %s", email, previous, exc)
        return _back("error", "Couldn't save the role change; it was reverted.")
    return _back("notice", f"{email} is now {role}.")
