"""Tests for the hash-chained audit log."""
from __future__ import annotations

import json

import pytest

from lab.governance import audit


def _seed(path):
    audit.append(path, {"event": "submit", "candidate_id": "RC-001", "by": "tom"})
    audit.append(path, {"event": "verdict", "candidate_id": "RC-001", "verdict": "FAIL"})
    audit.append(path, {"event": "decision", "candidate_id": "RC-001", "decision": "BLOCK",
                        "reviewer": "sarah", "reason": "leaks data"})


def test_append_builds_a_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed(path)
    entries = audit.read_log(path)
    assert [e["seq"] for e in entries] == [1, 2, 3]
    assert entries[0]["prev_hash"] == audit.GENESIS
    assert entries[1]["prev_hash"] == entries[0]["hash"]


def test_clean_log_verifies(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed(path)
    check = audit.verify_log(path)
    assert check.ok and check.bad_seq is None


def test_editing_an_entry_is_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed(path)

    # Tamper: flip the reviewer's BLOCK into an APPROVE on entry 3.
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[2])
    rec["decision"] = "APPROVE"
    lines[2] = json.dumps(rec, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    check = audit.verify_log(path)
    assert not check.ok
    assert check.bad_seq == 3


def test_deleting_an_entry_is_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")  # drop entry 2
    check = audit.verify_log(path)
    assert not check.ok


def test_missing_log_is_empty(tmp_path):
    assert audit.read_log(tmp_path / "nope.jsonl") == []


# ---------------------------------------------------------------------------
# B-09 / B-12 / D-20 / B-27: verify names the POSITION and a kind, never
# raises on a malformed line, warns on an empty log, and append refuses to
# chain onto a broken log. All tampering happens on tmp_path copies.
# ---------------------------------------------------------------------------

def _seed6(path):
    """Six entries so middle deletions, swaps and inserts are unambiguous."""
    _seed(path)
    audit.append(path, {"event": "submit", "candidate_id": "RC-002", "by": "tom"})
    audit.append(path, {"event": "verdict", "candidate_id": "RC-002", "verdict": "PASS"})
    audit.append(path, {"event": "decision", "candidate_id": "RC-002", "decision": "APPROVE",
                        "reviewer": "sarah", "reason": ""})


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def _write(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_intact_log_reports_count_and_head(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed6(path)
    check = audit.verify_log(path)
    assert check.ok and check.kind is None and check.bad_seq is None
    assert check.entries == 6
    assert check.last_hash == audit.read_log(path)[-1]["hash"]
    assert "6 entries" in check.detail


def test_edit_names_position_and_kind(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed6(path)
    lines = _lines(path)
    rec = json.loads(lines[2])
    rec["decision"] = "APPROVE"
    lines[2] = json.dumps(rec, sort_keys=True)
    _write(path, lines)

    check = audit.verify_log(path)
    assert not check.ok
    assert check.bad_seq == 3
    assert check.kind == "edited"
    assert "#3" in check.detail


def test_forged_insert_names_position_not_forged_seq(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed6(path)
    lines = _lines(path)
    forged = json.loads(lines[2])
    forged["seq"] = 99
    forged["decision"] = "APPROVE"
    lines.insert(2, json.dumps(forged, sort_keys=True))  # now sits at position 3
    _write(path, lines)

    check = audit.verify_log(path)
    assert not check.ok
    assert check.bad_seq == 3
    assert check.kind == "inserted"
    assert "99" not in check.detail


def test_deleted_middle_entry_names_the_gap(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed6(path)
    lines = _lines(path)
    del lines[2]  # drop entry #3
    _write(path, lines)

    check = audit.verify_log(path)
    assert not check.ok
    assert check.bad_seq == 3
    assert check.kind == "missing"
    assert check.detail == "entry #3 missing (between #2 and #4)"


def test_swapped_entries_are_reordered(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed6(path)
    lines = _lines(path)
    lines[2], lines[3] = lines[3], lines[2]  # swap #3 and #4
    _write(path, lines)

    check = audit.verify_log(path)
    assert not check.ok
    assert check.bad_seq == 3
    assert check.kind == "reordered"


def test_malformed_line_is_named_not_raised(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed6(path)
    lines = _lines(path)
    lines[2] = lines[2][:-1]  # a judge deletes one character of entry #3
    _write(path, lines)

    entries = audit.read_log(path)  # must not raise
    assert len(entries) == 6
    assert entries[2].get("malformed") is True

    check = audit.verify_log(path)  # must not raise
    assert not check.ok
    assert check.bad_seq == 3
    assert check.kind == "malformed"
    assert check.detail == "entry 3 is not valid JSON"


def test_empty_or_missing_log_is_ok_but_says_so(tmp_path):
    missing = audit.verify_log(tmp_path / "nope.jsonl")
    assert missing.ok and missing.entries == 0 and missing.last_hash is None
    assert missing.detail == "no entries (log missing or empty)"

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    check = audit.verify_log(empty)
    assert check.ok and check.entries == 0
    assert check.detail == "no entries (log missing or empty)"


def test_append_refuses_a_broken_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed6(path)
    lines = _lines(path)
    rec = json.loads(lines[2])
    rec["decision"] = "APPROVE"
    lines[2] = json.dumps(rec, sort_keys=True)
    _write(path, lines)

    with pytest.raises(audit.AuditError, match="#3"):
        audit.append(path, {"event": "submit", "candidate_id": "RC-003", "by": "tom"})
    assert len(_lines(path)) == 6  # nothing was chained onto the forged line


def test_append_refuses_a_malformed_log_without_traceback(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed6(path)
    lines = _lines(path)
    lines[4] = lines[4][:-1]
    _write(path, lines)

    with pytest.raises(audit.AuditError, match="not valid JSON"):
        audit.append(path, {"event": "submit", "candidate_id": "RC-003", "by": "tom"})
    assert len(_lines(path)) == 6


# ---------------------------------------------------------------------------
# B-08: head() is the cheap anchor outside the file. The chain alone cannot
# see tail truncation or a full re-chain; a recorded (count, last_hash) can.
# ---------------------------------------------------------------------------

def test_head_returns_count_and_last_hash(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed6(path)
    count, last_hash = audit.head(path)
    assert count == 6
    assert last_hash == audit.read_log(path)[-1]["hash"]
    assert audit.verify_log(path).last_hash == last_hash


def test_head_of_missing_or_empty_log(tmp_path):
    assert audit.head(tmp_path / "nope.jsonl") == (0, None)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert audit.head(empty) == (0, None)


def test_tail_truncation_passes_verify_but_moves_the_head(tmp_path):
    """The documented limit: cutting entries off the end keeps the chain intact.
    The anchor recorded before (count + last hash) is what catches it."""
    path = tmp_path / "audit.jsonl"
    _seed6(path)
    anchor = audit.head(path)
    _write(path, _lines(path)[:4])  # the last two entries vanish

    assert audit.verify_log(path).ok  # the chain itself cannot tell
    assert audit.head(path) != anchor  # the anchor can
    assert audit.head(path)[0] == 4


def test_rechaining_passes_verify_but_moves_the_head(tmp_path):
    """The other documented limit: edit an entry and recompute every later hash."""
    path = tmp_path / "audit.jsonl"
    _seed6(path)
    anchor = audit.head(path)
    entries = audit.read_log(path)
    entries[2]["decision"] = "APPROVE"
    prev = entries[1]["hash"]
    for rec in entries[2:]:
        rec["prev_hash"] = prev
        rec["hash"] = audit._hash_record(rec)
        prev = rec["hash"]
    _write(path, [json.dumps(r, sort_keys=True) for r in entries])

    assert audit.verify_log(path).ok  # re-chained: undetectable from inside
    assert audit.head(path)[0] == anchor[0] and audit.head(path)[1] != anchor[1]


def test_append_on_empty_log_starts_at_genesis(tmp_path):
    path = tmp_path / "fresh.jsonl"
    rec = audit.append(path, {"event": "submit", "candidate_id": "RC-001", "by": "tom"})
    assert rec["seq"] == 1 and rec["prev_hash"] == audit.GENESIS
    assert audit.verify_log(path).ok
