"""Tests for the hash-chained audit log."""
from __future__ import annotations

import json

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
