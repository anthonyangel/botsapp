#!/usr/bin/env python3
"""Fail the job if Renovate's log shows it couldn't do its work.

Renovate exits 0 even when it can't write anything (e.g. a read-only token
logs "Could not ensure issue ... integration-unauthorized" as a WARN and still
finishes "done"), so a green run proves nothing. Reads the JSON-lines log file
(RENOVATE_LOG_FILE, level warn and above) and exits 1 on any error-level entry
or any known write-failure message.
"""

import json
import re
import sys
from pathlib import Path

WRITE_FAILURE = re.compile(
    r"could not (ensure|create|update|push|commit|merge)"
    r"|integration-unauthorized"
    r"|resource not accessible"
    r"|refusing to allow"
    r"|bad credentials",
    re.IGNORECASE,
)
ERROR_LEVEL = 50  # bunyan: 40 warn, 50 error, 60 fatal


def failures(lines):
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        err = entry.get("err")
        detail = err.get("message", "") if isinstance(err, dict) else str(err or "")
        text = f"{entry.get('msg', '')} {detail}".strip()
        if entry.get("level", 0) >= ERROR_LEVEL or WRITE_FAILURE.search(text):
            yield text


def main(argv):
    path = Path(argv[1])
    if not path.exists():
        print(f"::warning::Renovate log {path} not found; cannot check for failures")
        return 0
    found = sorted(set(failures(path.read_text().splitlines())))
    for text in found:
        print(f"::error title=Renovate could not complete its work::{text}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
