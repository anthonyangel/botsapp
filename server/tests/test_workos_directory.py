"""WorkOSUserDirectory over a fake SDK client: right SDK calls, right ordering,
SDK errors surfaced as DirectoryError. No network."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from workos import TransportError, WorkOSError
from workos.organization_membership import RoleSingle

from botsapp.admin.directory import DirectoryError, WorkOSUserDirectory

ORG = "org_1"


def _page(*items):
    return SimpleNamespace(data=list(items))


def _invitation(id_: str, state: str = "pending"):
    return SimpleNamespace(id=id_, state=SimpleNamespace(value=state))


def _membership(id_: str = "om_1", role: str = "member"):
    return SimpleNamespace(id=id_, role=SimpleNamespace(slug=role))


@pytest.fixture
def sdk():
    return SimpleNamespace(
        user_management=SimpleNamespace(
            list_users=AsyncMock(return_value=_page(SimpleNamespace(id="user_1"))),
            list_invitations=AsyncMock(return_value=_page()),
            send_invitation=AsyncMock(return_value=SimpleNamespace(id="inv_new")),
            revoke_invitation=AsyncMock(),
        ),
        organization_membership=SimpleNamespace(
            list_organization_memberships=AsyncMock(return_value=_page(_membership())),
            update_organization_membership=AsyncMock(),
            delete_organization_membership=AsyncMock(),
        ),
    )


@pytest.fixture
def directory(sdk):
    return WorkOSUserDirectory(sdk, ORG)


async def test_invite_sends_into_the_org_with_the_role(directory, sdk):
    assert await directory.invite("a@b.co", "admin") == "inv_new"
    sdk.user_management.send_invitation.assert_awaited_once_with(
        email="a@b.co", organization_id=ORG, role_slug="admin"
    )


async def test_remove_revokes_pending_invitations_then_deletes_the_membership(directory, sdk):
    sdk.user_management.list_invitations.return_value = _page(
        _invitation("inv_1"), _invitation("inv_old", "revoked")
    )
    await directory.remove("a@b.co")
    sdk.user_management.revoke_invitation.assert_awaited_once_with("inv_1")
    sdk.organization_membership.delete_organization_membership.assert_awaited_once_with("om_1")


async def test_remove_is_idempotent_when_nothing_exists(directory, sdk):
    sdk.user_management.list_users.return_value = _page()
    await directory.remove("ghost@b.co")  # no error
    sdk.organization_membership.delete_organization_membership.assert_not_awaited()


async def test_membership_lookup_is_scoped_to_the_org_and_active_members(directory, sdk):
    await directory.get_role("a@b.co")
    sdk.organization_membership.list_organization_memberships.assert_awaited_once_with(
        user_id="user_1", organization_id=ORG, statuses=["active"]
    )


async def test_set_role_updates_the_membership(directory, sdk):
    await directory.set_role("a@b.co", "admin")
    sdk.organization_membership.update_organization_membership.assert_awaited_once_with(
        "om_1", role=RoleSingle(role_slug="admin")
    )


async def test_set_role_on_a_pending_invitation_replaces_it(directory, sdk):
    sdk.organization_membership.list_organization_memberships.return_value = _page()
    sdk.user_management.list_invitations.return_value = _page(_invitation("inv_1"))
    await directory.set_role("a@b.co", "admin")
    sdk.user_management.revoke_invitation.assert_awaited_once_with("inv_1")
    sdk.user_management.send_invitation.assert_awaited_once_with(
        email="a@b.co", organization_id=ORG, role_slug="admin"
    )


async def test_set_role_for_someone_unknown_is_an_error(directory, sdk):
    sdk.organization_membership.list_organization_memberships.return_value = _page()
    with pytest.raises(DirectoryError, match="neither a member nor invited"):
        await directory.set_role("a@b.co", "admin")


@pytest.mark.parametrize(
    ("slug", "expected"), [("admin", "admin"), ("member", "member"), ("custom", None)]
)
async def test_get_role_only_reports_known_roles(directory, sdk, slug, expected):
    sdk.organization_membership.list_organization_memberships.return_value = _page(
        _membership(role=slug)
    )
    assert await directory.get_role("a@b.co") == expected


async def test_get_role_for_a_non_member_is_none(directory, sdk):
    sdk.organization_membership.list_organization_memberships.return_value = _page()
    assert await directory.get_role("a@b.co") is None


@pytest.mark.parametrize("error", [WorkOSError("refused"), TransportError("down")])
@pytest.mark.parametrize("op", ["invite", "revoke", "remove", "set_role", "get_role"])
async def test_sdk_errors_surface_as_directory_errors(directory, sdk, op, error):
    sdk.user_management.send_invitation.side_effect = error
    sdk.user_management.revoke_invitation.side_effect = error
    sdk.user_management.list_users.side_effect = error
    calls = {
        "invite": lambda: directory.invite("a@b.co", "member"),
        "revoke": lambda: directory.revoke_invitation("inv_1"),
        "remove": lambda: directory.remove("a@b.co"),
        "set_role": lambda: directory.set_role("a@b.co", "member"),
        "get_role": lambda: directory.get_role("a@b.co"),
    }
    with pytest.raises(DirectoryError):
        await calls[op]()
