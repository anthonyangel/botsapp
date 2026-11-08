"""start.sh: the MCP process is started without the admin key (0027).

Runs the real script with a stand-in `uv` that just reports its arguments and
whether WAHA_API_KEY is visible to it."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "start.sh"


def _run(tmp_path: Path, **env: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/sh\necho "$* KEY=${WAHA_API_KEY-<unset>}"\n')
    uv.chmod(0o755)
    # start.sh execs caddy last (0028) — a stub that exits straight away, like
    # the uv stub above, so the script completes instead of blocking on (or
    # failing to find) a real reverse proxy.
    caddy = bin_dir / "caddy"
    caddy.write_text('#!/bin/sh\necho "caddy $*"\n')
    caddy.chmod(0o755)
    result = subprocess.run(
        ["sh", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "WAHA_API_KEY": "ADMIN", **env},
        check=True,
    )
    lines = {}
    for line in result.stdout.splitlines():
        if "KEY=" not in line:
            continue  # the caddy stub's own line — not a uv process
        which = "mcp" if "botsapp.main:app" in line else "admin"
        lines[which] = line.rsplit("KEY=", 1)[1]
    return lines


def test_script_is_valid_shell():
    subprocess.run(["sh", "-n", str(SCRIPT)], check=True)


def test_the_admin_key_is_withheld_from_the_mcp_process_only(tmp_path):
    assert _run(tmp_path) == {"mcp": "<unset>", "admin": "ADMIN"}
