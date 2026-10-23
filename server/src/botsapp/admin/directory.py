"""
WorkOS organization membership, behind a protocol (docs/decisions/0026).

The admin Users page invites, removes and re-roles people through
``UserDirectory``; tests use a stub, so nothing here touches the network in CI.
``WorkOSUserDirectory`` is a thin layer over the official ``workos`` SDK (its
async client), so request/response shapes come from the SDK, not from us. It has
still **not** been exercised against a live WorkOS environment.

Roles are exactly ``admin`` and ``member``; they are WorkOS role slugs and must
exist in the WorkOS environment.
"""

from __future__ import annotations

from typing import Protocol

from workos import AsyncWorkOSClient, TransportError, WorkOSError
from workos.organization_membership import RoleSingle

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
    def __init__(self, client: AsyncWorkOSClient, organization_id: str) -> None:
        self._client = client
        self._org = organization_id

    @staticmethod
    def _fail(exc: Exception) -> DirectoryError:
        return DirectoryError(f"WorkOS: {exc}")

    async def _membership_id_and_role(self, email: str) -> tuple[str, str | None] | None:
        users = await self._client.user_management.list_users(email=email)
        if not users.data:
            return None
        found = await self._client.organization_membership.list_organization_memberships(
            user_id=users.data[0].id, organization_id=self._org, statuses=["active"]
        )
        if not found.data:
            return None
        membership = found.data[0]
        return membership.id, membership.role.slug

    async def _pending_invitations(self, email: str) -> list[str]:
        found = await self._client.user_management.list_invitations(
            email=email, organization_id=self._org
        )
        return [i.id for i in found.data if i.state.value == "pending"]

    async def invite(self, email: str, role: str) -> str:
        try:
            invitation = await self._client.user_management.send_invitation(
                email=email, organization_id=self._org, role_slug=role
            )
        except (WorkOSError, TransportError) as exc:
            raise self._fail(exc) from exc
        return invitation.id

    async def revoke_invitation(self, invitation_id: str) -> None:
        try:
            await self._client.user_management.revoke_invitation(invitation_id)
        except (WorkOSError, TransportError) as exc:
            raise self._fail(exc) from exc

    async def remove(self, email: str) -> None:
        try:
            for invitation_id in await self._pending_invitations(email):
                await self._client.user_management.revoke_invitation(invitation_id)
            found = await self._membership_id_and_role(email)
            if found:
                await self._client.organization_membership.delete_organization_membership(found[0])
        except (WorkOSError, TransportError) as exc:
            raise self._fail(exc) from exc

    async def set_role(self, email: str, role: str) -> None:
        try:
            found = await self._membership_id_and_role(email)
            if found:
                await self._client.organization_membership.update_organization_membership(
                    found[0], role=RoleSingle(role_slug=role)
                )
                return
            pending = await self._pending_invitations(email)
            if not pending:
                raise DirectoryError(f"{email} is neither a member nor invited")
            # An invitation's role can't be edited: replace it.
            for invitation_id in pending:
                await self._client.user_management.revoke_invitation(invitation_id)
            await self._client.user_management.send_invitation(
                email=email, organization_id=self._org, role_slug=role
            )
        except (WorkOSError, TransportError) as exc:
            raise self._fail(exc) from exc

    async def get_role(self, email: str) -> str | None:
        try:
            found = await self._membership_id_and_role(email)
        except (WorkOSError, TransportError) as exc:
            raise self._fail(exc) from exc
        return found[1] if found and found[1] in ROLES else None
