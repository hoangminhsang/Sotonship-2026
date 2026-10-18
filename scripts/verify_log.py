#!/usr/bin/env python3
"""Verify a hash-chained audit log. Read-only; never changes the file.

Usage:
    python scripts/verify_log.py PATH [--json OUT]

``--json OUT`` also writes the result as raw JSON ("audit-verify/1": ok, bad_seq, kind, detail, entries,
last_hash, verified_at, mode, tamper_demo): the same fields as ``<out>/audit_verify.json`` from
``scripts/run_governance.py``. ``mode`` and ``tamper_demo`` are null here; only that CLI knows them.

Exit codes:
    0  chain intact (also for an empty log, with a warning)
    1  tampering detected (the first bad entry is named by position and kind)
    2  usage error (no path, or the file does not exist, or OUT is the log itself or cannot be written)

Use it on a *copy* of the real log to show the demo: copy, change one field,
verify the copy, then verify the real log again.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lab.governance import audit  # noqa: E402

SCHEMA = "audit-verify/1"

USAGE = ("usage: verify_log.py PATH [--json OUT]   "
         "(read-only; exit 0 intact, 1 tampered, 2 usage error)")


def verify_document(check, *, mode: str | None = None, tamper_demo: dict | None = None,
                    verified_at: str | None = None) -> dict:
    """The ``audit-verify/1`` document for one :class:`audit.LogCheck`.

    One shape for both writers: ``scripts/run_governance.py`` (``<out>/audit_verify.json``, with
    ``mode`` and the forged copy's result in ``tamper_demo``) and ``verify_log.py --json`` (both null).
    ``last_hash`` is the full head hash; ``bad_seq`` is a position in the file, never an entry's own seq.
    """
    return {
        "schema": SCHEMA,
        "ok": check.ok,
        "bad_seq": check.bad_seq,
        "kind": check.kind,
        "detail": check.detail,
        "entries": check.entries,
        "last_hash": check.last_hash,
        "verified_at": verified_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode,
        "tamper_demo": tamper_demo,
    }


def write_json(path: Path | str, document: dict) -> Path:
    """Write ``document`` as raw JSON (sorted keys, ASCII, LF); a temp file is renamed over ``path``.

    A reader that opens ``path`` while this runs sees the old file or the new one, never half of one.
    """
    path = Path(path)
    text = json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True, allow_nan=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return path


def parse_args(args: list[str]) -> tuple[str, str | None] | None:
    """``(log path, json path or None)``, or None when the arguments are not ``PATH [--json OUT]``."""
    paths: list[str] = []
    out: str | None = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-h", "--help"):
            return None
        if arg == "--json":
            if i + 1 >= len(args) or out is not None:
                return None
            out, i = args[i + 1], i + 2
            continue
        if arg.startswith("--json="):
            if out is not None or not arg[len("--json="):]:
                return None
            out, i = arg[len("--json="):], i + 1
            continue
        if arg.startswith("-"):
            return None
        paths.append(arg)
        i += 1
    if len(paths) != 1 or out == "":
        return None
    return paths[0], out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    parsed = parse_args(args)
    if parsed is None:
        print(USAGE, file=sys.stderr)
        return 2
    log_arg, json_arg = parsed

    path = Path(log_arg)
    if not path.is_file():
        print(f"verify_log: {path}: no such file", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    if json_arg is not None and Path(json_arg).resolve() == path.resolve():
        print(f"verify_log: --json {json_arg} is the same file as the log; the log is never written", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2

    check = audit.verify_log(path)
    print(f"Log: {path}")
    print(check.detail)
    code = 0
    if not check.ok:
        where = f" at entry #{check.bad_seq}" if check.bad_seq is not None else ""
        print(f">>> TAMPERING DETECTED{where} ({check.kind}) <<<")
        code = 1
    elif check.entries == 0:
        print("WARNING: nothing to verify. A deleted or emptied log also looks like this.")
    else:
        print(f"Chain intact: {check.entries} entries, head hash {check.last_hash}")

    if json_arg is not None:
        try:
            write_json(json_arg, verify_document(check))
        except OSError as exc:  # the verdict above stands; tampering (1) still outranks a failed write (2)
            print(f"verify_log: could not write {json_arg}: {exc}", file=sys.stderr)
            return code or 2
    return code


if __name__ == "__main__":
    sys.exit(main())
