"""ADMIN_URL_PREFIX (docs/decisions/0028): the path prefix admin-ui's routes,
redirects and hrefs all carry when it's set, so a reverse proxy (Caddy, on
Fly) can front both mcp-server and admin-ui on one public port without
stripping the path. The prefix is read once at import time (every admin
module bakes it into route tables, redirect targets and templates at
import), so exercising a non-default value needs its own subprocess rather
than monkeypatching env vars against the already-imported modules the rest
of the suite shares.
"""

import subprocess
import sys


def test_prefix_is_baked_into_routes_redirects_and_templates():
    script = """
import os
os.environ["ADMIN_URL_PREFIX"] = "/admin"
os.environ.setdefault("DATABASE_URL", "fake.db")
os.environ.setdefault("WAHA_URL", "http://fake")
os.environ.setdefault("WAHA_API_KEY", "fake-key")

import botsapp.admin.app as admin_app
import botsapp.admin.auth as auth
import botsapp.admin.templates as templates

paths = {getattr(r, "path", None) for r in admin_app.app.routes}
assert "/admin/accounts" in paths, paths
assert "/accounts" not in paths, paths
assert "/admin/login" in paths, paths
assert "/admin/auth/callback" in paths, paths

cfg = auth.AdminAuthConfig(client=None, secret=b"s" * 32, base_url="https://x.test")
assert cfg.redirect_uri == "https://x.test/admin/auth/callback", cfg.redirect_uri

assert auth._policy("/admin/users") == "admin"
assert auth._policy("/admin/accounts/create") == "admin"
assert auth._policy("/admin/accounts") == "user"
assert auth._policy("/admin/") == "owner"
assert auth._policy("/admin") == "owner"  # the mount root itself, no trailing slash

form = templates._select_account_form("sam", label="Use this account")
assert 'action="/admin/accounts/sam/select"' in form, form

print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd="src"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
