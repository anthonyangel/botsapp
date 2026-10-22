"""The admin Users page (#49): WorkOS and the local users table kept in step.

Real temp SQLite (so ordering/rollback is exercised for real) and a stub
UserDirectory in place of WorkOS — no network.
"""

from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

import botsapp.admin.app as admin_app
import botsapp.admin.auth as auth
import botsapp.state as state
from botsapp.admin.directory import DirectoryError

BASE = "https://admin.example.test"
ADMIN, OTHER_ADMIN, MEMBER = "admin@example.com", "boss@example.com", "member@example.com"


class StubDirectory:
    """In-memory WorkOS: email -> role for active members, plus pending invites."""

    def __init__(self) -> None:
        self.members: dict[str, str] = {ADMIN: "admin"}
        self.invitations: dict[str, tuple[str, str]] = {}  # id -> (email, role)
        self.calls: list[tuple] = []
        self.fail_on: set[str] = set()
        self._n = 0

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_on:
            raise DirectoryError(f"{op} unavailable")

    async def invite(self, email: str, role: str) -> str:
        self.calls.append(("invite", email, role))
        self._maybe_fail("invite")
        self._n += 1
        self.invitations[f"inv_{self._n}"] = (email, role)
        return f"inv_{self._n}"

    async def revoke_invitation(self, invitation_id: str) -> None:
        self.calls.append(("revoke", invitation_id))
        self.invitations.pop(invitation_id, None)

    async def remove(self, email: str) -> None:
        self.calls.append(("remove", email))
        self._maybe_fail("remove")
        self.members.pop(email, None)

    async def set_role(self, email: str, role: str) -> None:
        self.calls.append(("set_role", email, role))
        self._maybe_fail("set_role")
        self.members[email] = role

    async def get_role(self, email: str) -> str | None:
        self._maybe_fail("get_role")
        return self.members.get(email)

    def ops(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]


@pytest.fixture
def directory() -> StubDirectory:
    return StubDirectory()


@pytest.fixture
def client(monkeypatch, tmp_path, directory) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", str(tmp_path / "users.db"))
    monkeypatch.setenv("BOTSAPP_ADMIN_EMAIL", ADMIN)
    monkeypatch.setenv("WAHA_URL", "http://fake")
    monkeypatch.setenv("WAHA_API_KEY", "fake-key")
    cfg = auth.AdminAuthConfig(
        client=None, secret=b"s" * 32, base_url=BASE, directory=directory, dev_user=None
    )
    monkeypatch.setattr(auth, "config_from_env", lambda: cfg)
    with TestClient(admin_app.app, base_url=BASE, follow_redirects=False) as c:
        c.cfg = cfg
        c.cookies.set(auth.SESSION_COOKIE, auth.issue_session(cfg, ADMIN))
        yield c


def _post(client: TestClient, path: str, **data: str):
    return client.post(path, data=data, headers={"origin": BASE})


def _flash(response) -> dict[str, list[str]]:
    return parse_qs(urlparse(response.headers["location"]).query)


async def _db_users() -> dict[str, bool]:
    assert state.db is not None
    return {u["email"]: u["is_admin"] for u in await state.db.list_users()}


# ── page ─────────────────────────────────────────────────────────────────────


def test_page_lists_users_and_marks_the_caller(client):
    r = client.get("/users")
    assert r.status_code == 200
    assert ADMIN in r.text and "you" in r.text
    assert "WorkOS is not connected" not in r.text


def test_page_warns_when_workos_is_not_connected(client):
    from dataclasses import replace

    auth.config = replace(client.cfg, directory=None)
    assert "WorkOS is not connected" in client.get("/users").text


def test_flash_messages_are_escaped(client):
    r = client.get("/users", params={"error": "<script>alert(1)</script>"})
    assert "<script>alert" not in r.text


# ── invite ───────────────────────────────────────────────────────────────────


async def test_invite_sends_invitation_then_records_the_user(client, directory):
    r = _post(client, "/users/invite", email=" New@Example.com ", role="member")
    assert r.status_code == 303 and "notice" in _flash(r)
    assert directory.ops("invite") == [("invite", "new@example.com", "member")]
    assert (await _db_users())["new@example.com"] is False


async def test_invite_as_admin_sets_the_mirror(client):
    _post(client, "/users/invite", email="boss@example.com", role="admin")
    assert (await _db_users())[OTHER_ADMIN] is True


async def test_failed_invitation_leaves_no_user_row(client, directory):
    directory.fail_on.add("invite")
    r = _post(client, "/users/invite", email=MEMBER, role="member")
    assert "error" in _flash(r)
    assert MEMBER not in await _db_users()


async def test_failed_db_write_cancels_the_invitation(client, directory, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(state.db, "add_user", boom)
    r = _post(client, "/users/invite", email=MEMBER, role="member")
    assert "error" in _flash(r)
    assert directory.ops("revoke") and not directory.invitations
    assert MEMBER not in await _db_users()


@pytest.mark.parametrize(
    "data",
    [{"email": "not-an-email", "role": "member"}, {"email": MEMBER, "role": "root"}],
)
def test_invite_validates_input(client, directory, data):
    assert "error" in _flash(_post(client, "/users/invite", **data))
    assert not directory.calls


async def test_inviting_an_existing_user_is_refused(client, directory):
    r = _post(client, "/users/invite", email=ADMIN, role="member")
    assert "error" in _flash(r)
    assert not directory.ops("invite")


# ── remove ───────────────────────────────────────────────────────────────────


async def test_remove_deletes_in_workos_then_locally(client, directory):
    _post(client, "/users/invite", email=MEMBER, role="member")
    directory.members[MEMBER] = "member"
    r = _post(client, f"/users/{MEMBER}/remove")
    assert "notice" in _flash(r)
    assert directory.ops("remove") == [("remove", MEMBER)]
    assert MEMBER not in await _db_users()


async def test_cannot_remove_yourself(client, directory):
    r = _post(client, f"/users/{ADMIN}/remove")
    assert "error" in _flash(r) and not directory.ops("remove")
    assert ADMIN in await _db_users()


async def test_cannot_remove_someone_who_owns_an_account_and_workos_is_untouched(client, directory):
    _post(client, "/users/invite", email=MEMBER, role="member")
    await state.db.add_account("hers", MEMBER, "acct-hers")
    directory.calls.clear()
    r = _post(client, f"/users/{MEMBER}/remove")
    assert "owns 1" in _flash(r)["error"][0]
    assert not directory.ops("remove")
    assert MEMBER in await _db_users()


async def test_failed_workos_removal_keeps_the_local_user(client, directory):
    _post(client, "/users/invite", email=MEMBER, role="member")
    directory.fail_on.add("remove")
    assert "error" in _flash(_post(client, f"/users/{MEMBER}/remove"))
    assert MEMBER in await _db_users()


async def test_failed_local_removal_says_to_retry(client, directory, monkeypatch):
    _post(client, "/users/invite", email=MEMBER, role="member")

    async def boom(*_a, **_k):
        raise RuntimeError("locked")

    monkeypatch.setattr(state.db, "remove_user", boom)
    r = _post(client, f"/users/{MEMBER}/remove")
    assert "try Remove again" in _flash(r)["error"][0]


# ── role changes ─────────────────────────────────────────────────────────────


async def test_role_change_updates_workos_then_the_mirror(client, directory):
    _post(client, "/users/invite", email=MEMBER, role="member")
    directory.members[MEMBER] = "member"
    r = _post(client, f"/users/{MEMBER}/role", role="admin")
    assert "notice" in _flash(r)
    assert directory.members[MEMBER] == "admin"
    assert (await _db_users())[MEMBER] is True


async def test_failed_workos_role_change_leaves_the_mirror_alone(client, directory):
    _post(client, "/users/invite", email=MEMBER, role="member")
    directory.fail_on.add("set_role")
    assert "error" in _flash(_post(client, f"/users/{MEMBER}/role", role="admin"))
    assert (await _db_users())[MEMBER] is False


async def test_failed_mirror_write_restores_the_workos_role(client, directory, monkeypatch):
    _post(client, "/users/invite", email=MEMBER, role="member")
    directory.members[MEMBER] = "member"

    async def boom(*_a, **_k):
        raise RuntimeError("locked")

    monkeypatch.setattr(state.db, "set_user_admin", boom)
    assert "reverted" in _flash(_post(client, f"/users/{MEMBER}/role", role="admin"))["error"][0]
    assert directory.members[MEMBER] == "member"


async def test_cannot_change_your_own_role(client, directory):
    r = _post(client, f"/users/{ADMIN}/role", role="member")
    assert "error" in _flash(r) and not directory.ops("set_role")


async def test_demoting_another_admin_leaves_the_caller_as_admin(client, directory):
    _post(client, "/users/invite", email=OTHER_ADMIN, role="admin")
    directory.members[OTHER_ADMIN] = "admin"
    assert "notice" in _flash(_post(client, f"/users/{OTHER_ADMIN}/role", role="member"))
    assert (await _db_users())[ADMIN] is True
    assert (
        "already member"
        in _flash(_post(client, f"/users/{OTHER_ADMIN}/role", role="member"))["notice"][0]
    )


# ── who may do this ──────────────────────────────────────────────────────────


async def test_members_cannot_reach_the_page(client):
    _post(client, "/users/invite", email=MEMBER, role="member")
    client.cookies.set(auth.SESSION_COOKIE, auth.issue_session(client.cfg, MEMBER))
    assert client.get("/users").status_code == 403
    assert _post(client, "/users/invite", email="x@example.com", role="member").status_code == 403


async def test_admin_demoted_in_workos_is_stopped_even_with_a_valid_cookie(client, directory):
    _post(client, "/users/invite", email=OTHER_ADMIN, role="admin")
    directory.members[OTHER_ADMIN] = "admin"
    client.cookies.set(auth.SESSION_COOKIE, auth.issue_session(client.cfg, OTHER_ADMIN))
    directory.members[OTHER_ADMIN] = "member"  # demoted in the WorkOS dashboard
    r = _post(client, "/users/invite", email="x@example.com", role="member")
    assert r.status_code == 403
    assert "x@example.com" not in await _db_users()


async def test_bootstrap_admin_without_a_workos_role_is_not_locked_out(client, directory):
    directory.members.pop(ADMIN)  # first-time setup: not in the org yet
    r = _post(client, "/users/invite", email=MEMBER, role="member")
    assert r.status_code == 303 and "notice" in _flash(r)


async def test_workos_outage_blocks_changes_with_a_clear_error(client, directory):
    directory.fail_on.add("get_role")
    r = _post(client, "/users/invite", email=MEMBER, role="member")
    assert "Couldn't verify" in _flash(r)["error"][0]
    assert MEMBER not in await _db_users()


# ── local-only mode ──────────────────────────────────────────────────────────


async def test_without_workos_changes_are_local_only(client):
    from dataclasses import replace

    auth.config = replace(client.cfg, directory=None)
    assert "notice" in _flash(_post(client, "/users/invite", email=MEMBER, role="admin"))
    assert (await _db_users())[MEMBER] is True
    assert "notice" in _flash(_post(client, f"/users/{MEMBER}/role", role="member"))
    assert "notice" in _flash(_post(client, f"/users/{MEMBER}/remove"))


# ── sign-in refreshes the mirror ─────────────────────────────────────────────


async def test_sign_in_mirrors_the_workos_role(client, directory):
    _post(client, "/users/invite", email=MEMBER, role="member")
    directory.members[MEMBER] = "admin"  # promoted in the WorkOS dashboard
    await auth._mirror_role(client.cfg, MEMBER)
    assert (await _db_users())[MEMBER] is True


async def test_sign_in_never_demotes_the_bootstrap_admin(client, directory):
    directory.members[ADMIN] = "member"
    await auth._mirror_role(client.cfg, ADMIN)
    assert (await _db_users())[ADMIN] is True


async def test_sign_in_keeps_the_stored_role_when_workos_is_down(client, directory):
    _post(client, "/users/invite", email=MEMBER, role="admin")
    directory.fail_on.add("get_role")
    await auth._mirror_role(client.cfg, MEMBER)
    assert (await _db_users())[MEMBER] is True
