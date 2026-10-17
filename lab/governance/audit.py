"""The stone diary: an append-only, hash-chained audit log.

Every submit, verdict and decision is one line in a JSONL file. Each line
stores the hash of the line before it, like links in a chain. Change one letter
of an old line and its hash no longer matches what the next line recorded, so
:func:`verify_log` can point at exactly where the tampering happened.

What the chain catches: an edited entry, an inserted entry, a deleted middle
entry, a reordered entry and a line that is no longer valid JSON. In every
case :func:`verify_log` names the *position* in the file (not the entry's own
``seq`` field, which a forger controls) and a kind.

What the chain does NOT catch (honest limits): re-chaining (edit an entry and
recompute every later hash), truncating entries off the end, and deleting the
whole file. Plain SHA-256 has no key, so anyone with write access can recompute
it. The cheap anchor is :func:`head`: record the entry count and last hash
somewhere outside the file (the gate report, the manager summary) and compare.
A real product would add an HMAC key held by the rule owner or a second copy.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64

#: ``event`` value of the placeholder :func:`read_log` returns for a line that
#: is not valid JSON. The placeholder keeps the log readable (the app can still
#: list it) and lets :func:`verify_log` name the position.
MALFORMED_EVENT = "malformed_line"

#: Kinds of tampering :func:`verify_log` can name.
KINDS = ("edited", "missing", "inserted", "reordered", "malformed")


class AuditError(RuntimeError):
    """The audit log cannot be used as-is. The message is safe to show a user."""


def _hash_record(record: dict) -> str:
    """Hash of a record excluding its own ``hash`` field (canonical JSON)."""
    body = {k: v for k, v in record.items() if k != "hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_line(position: int, line: str) -> dict:
    """Parse one JSONL line; a bad line becomes a placeholder, never an exception."""
    try:
        rec = json.loads(line)
    except json.JSONDecodeError as exc:
        return {"seq": position, "event": MALFORMED_EVENT, "malformed": True, "error": str(exc)}
    if not isinstance(rec, dict):
        return {"seq": position, "event": MALFORMED_EVENT, "malformed": True,
                "error": f"line is a JSON {type(rec).__name__}, not an object"}
    return rec


def read_log(path: Path | str) -> list[dict]:
    """Read all entries, in file order. Missing file -> empty list.

    A line that is not valid JSON never raises: it is returned as a placeholder
    record with ``event == MALFORMED_EVENT`` and ``malformed: True`` so the
    position is kept and :func:`verify_log` can name it.
    """
    p = Path(path)
    if not p.exists():
        return []
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [_parse_line(i, ln) for i, ln in enumerate(lines, start=1)]


def head(path: Path | str) -> tuple[int, str | None]:
    """``(entry_count, last_hash)`` -- a cheap anchor to record outside the file.

    Compare it later with a fresh call: a changed count with the same intact
    chain means entries were cut off the end or the chain was rebuilt, which
    :func:`verify_log` alone cannot see. ``(0, None)`` for a missing or empty log.
    """
    entries = read_log(path)
    if not entries:
        return 0, None
    last = entries[-1].get("hash")
    return len(entries), (last if isinstance(last, str) else None)


def append(path: Path | str, event: dict) -> dict:
    """Append ``event`` as the next chained entry and return the stored record.

    The chain is verified first. Appending onto a tampered or malformed log
    raises :class:`AuditError` instead of silently linking a good entry to a
    forged one (B-27). An empty or missing log starts a new chain.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = read_log(p)
    check = _verify_entries(existing)
    if not check.ok:
        raise AuditError(
            f"refusing to append to {p}: the audit log is not intact "
            f"({check.kind} at entry #{check.bad_seq}: {check.detail}). "
            "Investigate the log before adding to it."
        )

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
    """Result of verifying the chain.

    ``bad_seq`` is the 1-based *position* in the file of the first entry that
    does not hold (never the entry's own ``seq`` field). ``kind`` is one of
    :data:`KINDS` when ``ok`` is False, else ``None``. ``entries`` and
    ``last_hash`` are the head anchor (see :func:`head`).
    """

    ok: bool
    bad_seq: int | None = None
    detail: str = ""
    kind: str | None = None
    entries: int = 0
    last_hash: str | None = None


def verify_log(path: Path | str) -> LogCheck:
    """Recompute the chain and report the first position that does not hold.

    Never raises on a malformed line. A missing or empty log is ``ok`` (there
    is nothing to be tampered) but says so in ``detail`` so a caller can warn.
    """
    return _verify_entries(read_log(path))


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _verify_entries(entries: list[dict]) -> LogCheck:
    if not entries:
        return LogCheck(True, None, "no entries (log missing or empty)", None, 0, None)

    n = len(entries)
    prev = GENESIS
    for i, rec in enumerate(entries, start=1):
        if rec.get("malformed"):
            return LogCheck(False, i, f"entry {i} is not valid JSON", "malformed", n, None)

        self_ok = _hash_record(rec) == rec.get("hash")
        if rec.get("seq") == i and rec.get("prev_hash") == prev and self_ok:
            prev = rec["hash"]
            continue

        return _classify(entries, i, prev, self_ok)

    return LogCheck(True, None, f"{n} entries, chain intact (head {prev[:12]})", None, n, prev)


def _classify(entries: list[dict], i: int, prev: str, self_ok: bool) -> LogCheck:
    """Name the kind of tampering at position ``i`` using only positions."""
    n = len(entries)
    rec = entries[i - 1]
    earlier_hashes = {e.get("hash"): k for k, e in enumerate(entries[: i - 1], start=1)}

    # The genuine successor of #(i-1) is further down the file: the entry at
    # position i is either an extra (inserted) or a real entry out of place.
    successor = next(
        (k for k in range(i + 1, n + 1) if entries[k - 1].get("prev_hash") == prev), None
    )
    if successor is not None:
        claims_same_parent = rec.get("prev_hash") == prev
        if claims_same_parent or not self_ok:
            detail = f"entry #{i} inserted (not in the chain: #{i - 1} links to #{successor})"
            return LogCheck(False, i, detail, "inserted", n, None)
        detail = f"entry #{i} out of order (the entry that follows #{i - 1} is now at #{successor})"
        return LogCheck(False, i, detail, "reordered", n, None)

    if rec.get("hash") in earlier_hashes:
        detail = f"entry #{i} inserted (duplicate of #{earlier_hashes[rec.get('hash')]})"
        return LogCheck(False, i, detail, "inserted", n, None)

    if not self_ok:
        detail = f"entry #{i} edited (contents changed after it was written)"
        return LogCheck(False, i, detail, "edited", n, None)

    # Self-consistent entry. If it links to #(i-1) correctly, only its seq is
    # wrong: an edit that was re-hashed. Otherwise there is a gap before it
    # (or it was forged); the claimed seq is reported as a claim, never as a
    # real entry number.
    seq = rec.get("seq")
    if rec.get("prev_hash") == prev:
        detail = f"entry #{i} edited (sequence number changed and re-hashed)"
        return LogCheck(False, i, detail, "edited", n, None)
    if _is_int(seq) and seq == i + 1:
        detail = f"entry #{i} missing (between #{i - 1} and #{seq})"
        return LogCheck(False, i, detail, "missing", n, None)
    if _is_int(seq) and seq > i + 1:
        detail = f"{seq - i} entries missing before #{i} (the entry at #{i} claims seq {seq})"
        return LogCheck(False, i, detail, "missing", n, None)

    detail = f"entry #{i} does not link to #{i - 1} (one of them was edited and re-hashed)"
    return LogCheck(False, i, detail, "edited", n, None)
