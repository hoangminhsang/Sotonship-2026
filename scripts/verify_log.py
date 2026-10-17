#!/usr/bin/env python3
"""Verify a hash-chained audit log. Read-only; never changes the file.

Usage:
    python scripts/verify_log.py PATH

Exit codes:
    0  chain intact (also for an empty log, with a warning)
    1  tampering detected (the first bad entry is named by position and kind)
    2  usage error (no path, or the file does not exist)

Use it on a *copy* of the real log to show the demo: copy, change one field,
verify the copy, then verify the real log again.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lab.governance import audit  # noqa: E402

USAGE = "usage: verify_log.py PATH   (read-only; exit 0 intact, 1 tampered, 2 usage error)"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] in ("-h", "--help"):
        print(USAGE, file=sys.stderr)
        return 2

    path = Path(args[0])
    if not path.is_file():
        print(f"verify_log: {path}: no such file", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2

    check = audit.verify_log(path)
    print(f"Log: {path}")
    print(check.detail)
    if not check.ok:
        print(f">>> TAMPERING DETECTED at entry #{check.bad_seq} ({check.kind}) <<<")
        return 1
    if check.entries == 0:
        print("WARNING: nothing to verify. A deleted or emptied log also looks like this.")
        return 0
    print(f"Chain intact: {check.entries} entries, head hash {check.last_hash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
