"""The stone diary: an append-only, hash-chained audit log.

Every submit, verdict and decision is one line in a JSONL file. Each line
stores the hash of the line before it, like links in a chain. Change one letter
of an old line and its hash no longer matches what the next line recorded, so
:func:`verify_log` can point at exactly where the tampering happened.

What the chain catches: an edited entry, an inserted entry, a deleted middle
entry, a reordered entry and a line that is no longer valid JSON. In every
case :func:`verify_log` names the *position* in the file (not the entry's own
``seq`` field, which a forger controls) and a kind.

A chain alone cannot see its own tail being cut off: drop the newest lines and
every remaining link still holds. So each append also writes a small *head
anchor* file next to the log (``audit_log.jsonl`` -> ``audit_log_head.json``,
holding the newest ``seq`` and ``hash``), and :func:`verify_log` checks the
log's last entry against it. That catches truncating entries off the end,
deleting the whole log, and re-chaining (edit an entry and recompute every
later hash): kinds ``truncated`` and ``anchor``. A log written before anchors
existed still verifies, with a caveat in ``detail``.

Honest limits: plain SHA-256 has no key, so someone with full disk access can
rewrite the log *and* the anchor, or delete both. :func:`head` gives the same
``(count, last_hash)`` to record somewhere else (the gate report, the manager
summary). A real product would add an HMAC key held by the rule owner, or a
second copy of the log (or at least its head) in another place.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64

#: ``event`` value of the placeholder :func:`read_log` returns for a line that
#: is not valid JSON. The placeholder keeps the log readable (the app can still
#: list it) and lets :func:`verify_log` name the position.
MALFORMED_EVENT = "malformed_line"

#: Kinds of tampering :func:`verify_log` can name. ``truncated`` and ``anchor``
#: come from the head anchor file: entries cut off the end (or the whole log
#: deleted), and a last entry that no longer matches the anchor (or an anchor
#: file that is itself damaged).
KINDS = ("edited", "missing", "inserted", "reordered", "malformed", "truncated", "anchor")


class AuditError(RuntimeError):
    """The audit log cannot be used as-is. The message is safe to show a user."""


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
    """The stored ``{"seq", "hash"}`` of the newest entry, or ``None`` if absent.

    Raises :class:`json.JSONDecodeError` for a garbled anchor file;
    :func:`verify_log` reports that as kind ``anchor`` instead.
    """
    a = anchor_path(path)
    if not a.exists():
        return None
    return json.loads(a.read_text(encoding="utf-8"))


def _write_anchor(path: Path | str, record: dict) -> None:
    """Record the newest entry's ``seq`` and ``hash`` (temp file, then swapped in)."""
    target = anchor_path(path)
    text = json.dumps({"seq": record["seq"], "hash": record["hash"]}, sort_keys=True) + "\n"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(tmp, target)
    except PermissionError:  # e.g. Windows, while another process holds the anchor open
        target.write_text(text, encoding="utf-8")
        tmp.unlink(missing_ok=True)


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

    The same pair the head anchor file holds; record it somewhere else too
    (the gate report, the manager summary) and compare it later with a fresh
    call. ``(0, None)`` for a missing or empty log.
    """
    entries = read_log(path)
    if not entries:
        return 0, None
    last = entries[-1].get("hash")
    return len(entries), (last if isinstance(last, str) else None)


def append(path: Path | str, event: dict) -> dict:
    """Append ``event`` as the next chained entry and return the stored record.

    The chain and the head anchor are verified first. Appending onto a
    tampered, malformed or truncated log raises :class:`AuditError` instead of
    silently linking a good entry to a forged one (B-27), or re-anchoring a log
    whose newest entries were cut off. An empty or missing log with no anchor
    starts a new chain; every append then updates the anchor.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = read_log(p)
    check = _check_anchor(p, existing, _verify_entries(existing))
    if not check.ok:
        where = f" at entry #{check.bad_seq}" if check.bad_seq is not None else ""
        raise AuditError(
            f"refusing to append to {p}: the audit log is not intact "
            f"({check.kind}{where}: {check.detail}). "
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
    _write_anchor(p, record)
    return record


@dataclass
class LogCheck:
    """Result of verifying the chain and its head anchor.

    ``bad_seq`` is the 1-based *position* in the file of the first entry that
    does not hold (never the entry's own ``seq`` field); it is ``None`` only
    when the anchor file itself is damaged. ``kind`` is one of :data:`KINDS`
    when ``ok`` is False, else ``None``. ``entries`` and ``last_hash`` are the
    head (see :func:`head`). ``truncated_after`` is set when the newest entries
    were cut off: the last position still there (0 when the whole log is gone).
    """

    ok: bool
    bad_seq: int | None = None
    detail: str = ""
    kind: str | None = None
    entries: int = 0
    last_hash: str | None = None
    truncated_after: int | None = None


def verify_log(path: Path | str) -> LogCheck:
    """Recompute the chain, then check the tail against the head anchor.

    Never raises on a malformed line or a garbled anchor. A missing or empty
    log with no anchor is ``ok`` (there is nothing to be tampered) but says so
    in ``detail`` so a caller can warn. A log with no anchor (written before
    anchors existed) is ``ok`` with a caveat that truncation cannot be checked.
    """
    entries = read_log(path)
    return _check_anchor(path, entries, _verify_entries(entries))


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_anchor(path: Path | str, entries: list[dict], check: LogCheck) -> LogCheck:
    """Compare an intact chain's head with the anchor file next to the log."""
    if not check.ok:
        return check  # the chain already names the first bad position
    n, last = check.entries, check.last_hash
    name = anchor_path(path).name
    try:
        anchor = read_anchor(path)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return LogCheck(False, None, f"head anchor {name} is not valid JSON", "anchor", n, None)
    if anchor is None:
        if n:
            check.detail += " (no head anchor yet, so truncation cannot be checked)"
        return check
    if not (isinstance(anchor, dict) and _is_int(anchor.get("seq"))
            and isinstance(anchor.get("hash"), str)):
        return LogCheck(False, None, f"head anchor {name} is not a JSON object with seq and hash",
                        "anchor", n, None)

    a = anchor["seq"]
    if a > n:
        return LogCheck(False, n + 1,
                        f"log truncated after entry {n}: the head anchor records entry {a}",
                        "truncated", n, None, truncated_after=n)
    if a == n and anchor["hash"] == (last or GENESIS):
        return check
    if 1 <= a < n and entries[a - 1].get("hash") == anchor["hash"]:
        return LogCheck(False, a + 1,
                        f"entries #{a + 1} to #{n} were added without updating the head anchor "
                        f"(it records entry {a})", "inserted", n, None)
    return LogCheck(False, n,
                    f"last entry #{n} does not match the head anchor (entry {a}): "
                    "the log was rebuilt or its last entry replaced", "anchor", n, None)


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
