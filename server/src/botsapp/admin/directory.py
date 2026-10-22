"""
WorkOS organization membership, behind a protocol (docs/decisions/0026).

The admin Users page invites, removes and re-roles people through
``UserDirectory``; tests use a stub, so nothing here touches the network in CI.
``WorkOSUserDirectory`` follows WorkOS's User Management API (invitations,
users, organization memberships) and has **not** been exercised against a live
environment — treat the request/response shapes as unverified until it has.

Roles are exactly ``admin`` and ``member``; they are WorkOS role slugs and must
exist in the WorkOS environment.
"""

from __future__ import annotations

from typing import Any, Protocol

import aiohttp

ROLES = ("admin", "member")


class DirectoryError(Exception):
    """A WorkOS call failed or was refused. The message is safe to show."""


class UserDirectory(Protocol):
    async def invite(self, email: str, role: str) -> str:
        """Send an invitation into the organization; returns its id."""
        ...

    async def revoke_invitation(self, invitation_id: str) -> None: ...

    async def remove(self, email: str) -> None:
        """Revoke pending invitations and delete the membership, if any."""
        ...

    async def set_role(self, email: str, role: str) -> None: ...

    async def get_role(self, email: str) -> str | None:
        """Live role for an active member, or None if not a member."""
        ...


class WorkOSUserDirectory:
    _BASE = "https://api.workos.com/user_management"

    def __init__(self, api_key: str, organization_id: str) -> None:
        self._api_key = api_key
        self._org = organization_id

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with (
                aiohttp.ClientSession(headers={"Authorization": f"Bearer {self._api_key}"}) as http,
                http.request(
                    method,
                    f"{self._BASE}{path}",
                    params=params,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp,
            ):
                if resp.status >= 400:
                    detail = ""
                    try:
                        detail = (await resp.json()).get("message", "")
                    except (aiohttp.ContentTypeError, ValueError):
                        pass
                    raise DirectoryError(f"WorkOS {method} {path} → {resp.status} {detail}".strip())
                if resp.status == 204 or resp.content_length == 0:
                    return {}
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise DirectoryError(f"WorkOS unreachable: {exc}") from exc

    async def _membership(self, email: str) -> dict[str, Any] | None:
        users = (await self._call("GET", "/users", params={"email": email})).get("data", [])
        if not users:
            return None
        found = await self._call(
            "GET",
            "/organization_memberships",
            params={"user_id": users[0]["id"], "organization_id": self._org},
        )
        memberships = [m for m in found.get("data", []) if m.get("status", "active") == "active"]
        return memberships[0] if memberships else None

    async def _pending_invitations(self, email: str) -> list[dict[str, Any]]:
        found = await self._call(
            "GET", "/invitations", params={"email": email, "organization_id": self._org}
        )
        return [i for i in found.get("data", []) if i.get("state") == "pending"]

    async def invite(self, email: str, role: str) -> str:
        data = await self._call(
            "POST",
            "/invitations",
            body={"email": email, "organization_id": self._org, "role_slug": role},
        )
        return str(data["id"])

    async def revoke_invitation(self, invitation_id: str) -> None:
        await self._call("POST", f"/invitations/{invitation_id}/revoke")

    async def remove(self, email: str) -> None:
        for invitation in await self._pending_invitations(email):
            await self.revoke_invitation(invitation["id"])
        membership = await self._membership(email)
        if membership:
            await self._call("DELETE", f"/organization_memberships/{membership['id']}")

    async def set_role(self, email: str, role: str) -> None:
        membership = await self._membership(email)
        if membership:
            await self._call(
                "PUT", f"/organization_memberships/{membership['id']}", body={"role_slug": role}
            )
            return
        pending = await self._pending_invitations(email)
        if not pending:
            raise DirectoryError(f"{email} is neither a member nor invited")
        # An invitation's role can't be edited: replace it.
        for invitation in pending:
            await self.revoke_invitation(invitation["id"])
        await self.invite(email, role)

    async def get_role(self, email: str) -> str | None:
        membership = await self._membership(email)
        slug = ((membership or {}).get("role") or {}).get("slug")
        return slug if slug in ROLES else None
