"""The stone diary: an append-only, hash-chained audit log.

Every submit, verdict and decision is one line in a JSONL file. Each line
stores the hash of the line before it, like links in a chain. Change one letter
of an old line and its hash no longer matches what the next line recorded, so
:func:`verify_log` can point at exactly where the tampering happened.

A chain alone cannot see its own tail being cut off: drop the newest lines and
every remaining link still holds. So each append also writes a small *head
anchor* file next to the log (the latest ``seq`` and ``hash``), and
:func:`verify_log` checks the log's last entry against it.

The chain catches edits; it does not prevent them. Someone with full disk
access could rewrite the log *and* the anchor, or delete both. A real product
would also copy the log (or at least its head) to a second place -- see the
honest-limits section of the idea doc.
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


def anchor_path(path: Path | str) -> Path:
    """Where the head anchor for ``path`` lives (``audit_log.jsonl`` -> ``audit_log_head.json``)."""
    p = Path(path)
    return p.with_name(f"{p.stem}_head.json")


def read_anchor(path: Path | str) -> dict | None:
    """The stored ``{"seq", "hash"}`` of the newest entry, or ``None`` if absent."""
    a = anchor_path(path)
    if not a.exists():
        return None
    return json.loads(a.read_text(encoding="utf-8"))


def _write_anchor(path: Path | str, record: dict) -> None:
    head = {"seq": record["seq"], "hash": record["hash"]}
    anchor_path(path).write_text(json.dumps(head, sort_keys=True) + "\n", encoding="utf-8")


class LogCorruptError(ValueError):
    """An audit log line that is not a JSON record (garbled or hand-edited)."""

    def __init__(self, entry: int, what: str) -> None:
        super().__init__(f"entry {entry} of the audit log is not a valid record ({what})")
        self.entry = entry


def read_log(path: Path | str) -> list[dict]:
    """Read all entries, in order. Missing file -> empty list.

    Raises :class:`LogCorruptError` naming the first unreadable entry, rather
    than skipping it: a damaged line is itself evidence of tampering.
    """
    p = Path(path)
    if not p.exists():
        return []
    entries = []
    for i, line in enumerate((ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()), start=1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LogCorruptError(i, "not valid JSON") from exc
        if not isinstance(rec, dict):
            raise LogCorruptError(i, f"a JSON {type(rec).__name__}, not an object")
        entries.append(rec)
    return entries


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
    _write_anchor(p, record)
    return record


@dataclass
class LogCheck:
    """Result of verifying the chain."""

    ok: bool
    bad_seq: int | None = None
    detail: str = ""
    truncated_after: int | None = None  # set when the newest entries were cut off


def verify_log(path: Path | str) -> LogCheck:
    """Recompute the chain and report the first entry that does not hold."""
    try:
        entries = read_log(path)
    except LogCorruptError as exc:
        return LogCheck(False, exc.entry, str(exc))
    prev = GENESIS
    for i, rec in enumerate(entries, start=1):
        if rec.get("seq") != i:
            return LogCheck(False, rec.get("seq"), f"sequence jumped at entry {i} (saw seq={rec.get('seq')})")
        if rec.get("prev_hash") != prev:
            return LogCheck(False, rec.get("seq"), f"broken link at entry {i}: prev_hash does not match entry {i - 1}")
        if _hash_record(rec) != rec.get("hash"):
            return LogCheck(False, rec.get("seq"), f"contents of entry {i} were changed after it was written")
        prev = rec["hash"]

    # The links hold; now check the tail against the external head anchor.
    last_seq = entries[-1]["seq"] if entries else 0
    last_hash = entries[-1]["hash"] if entries else GENESIS
    try:
        anchor = read_anchor(path)
    except json.JSONDecodeError:
        return LogCheck(False, None, f"head anchor {anchor_path(path).name} is not valid JSON")
    if anchor is not None and not isinstance(anchor, dict):
        return LogCheck(False, None, f"head anchor {anchor_path(path).name} is not a JSON object")
    if anchor is None:
        note = " (no head anchor yet, so truncation cannot be checked)" if entries else ""
        return LogCheck(True, None, f"{len(entries)} entries, chain intact{note}")
    if isinstance(anchor.get("seq"), int) and anchor["seq"] > last_seq:
        return LogCheck(False, last_seq + 1,
                        f"log truncated after entry {last_seq}: the head anchor records entry {anchor['seq']}",
                        truncated_after=last_seq)
    if anchor.get("seq") != last_seq or anchor.get("hash") != last_hash:
        return LogCheck(False, last_seq,
                        f"last entry {last_seq} does not match the head anchor (entry {anchor.get('seq')})")
    return LogCheck(True, None, f"{len(entries)} entries, chain intact")
