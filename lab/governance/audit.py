"""The stone diary: an append-only, hash-chained audit log.

Every submit, verdict and decision is one line in a JSONL file. Each line
stores the hash of the line before it, like links in a chain. Change one letter
of an old line and its hash no longer matches what the next line recorded, so
:func:`verify_log` can point at exactly where the tampering happened.

The chain catches edits; it does not prevent them. Someone with full disk
access could delete the whole file. A real product would also copy the log to a
second place -- see the honest-limits section of the idea doc.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _hash_record(record: dict) -> str:
    """Hash of a record excluding its own ``hash`` field (canonical JSON)."""
    body = {k: v for k, v in record.items() if k != "hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_log(path: Path | str) -> list[dict]:
    """Read all entries, in order. Missing file -> empty list."""
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def append(path: Path | str, event: dict) -> dict:
    """Append ``event`` as the next chained entry and return the stored record."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = read_log(p)

    seq = len(existing) + 1
    prev_hash = existing[-1]["hash"] if existing else GENESIS
    record = {
        "seq": seq,
        "time": event.get("time") or datetime.now(timezone.utc).isoformat(),
        "prev_hash": prev_hash,
        **{k: v for k, v in event.items() if k != "time"},
    }
    record["hash"] = _hash_record(record)

    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return record


@dataclass
class LogCheck:
    """Result of verifying the chain."""

    ok: bool
    bad_seq: int | None = None
    detail: str = ""


def verify_log(path: Path | str) -> LogCheck:
    """Recompute the chain and report the first entry that does not hold."""
    entries = read_log(path)
    prev = GENESIS
    for i, rec in enumerate(entries, start=1):
        if rec.get("seq") != i:
            return LogCheck(False, rec.get("seq"), f"sequence jumped at entry {i} (saw seq={rec.get('seq')})")
        if rec.get("prev_hash") != prev:
            return LogCheck(False, rec.get("seq"), f"broken link at entry {i}: prev_hash does not match entry {i - 1}")
        if _hash_record(rec) != rec.get("hash"):
            return LogCheck(False, rec.get("seq"), f"contents of entry {i} were changed after it was written")
        prev = rec["hash"]
    return LogCheck(True, None, f"{len(entries)} entries, chain intact")
