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


def test_append_writes_a_head_anchor(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed(path)
    last = audit.read_log(path)[-1]
    assert audit.read_anchor(path) == {"seq": 3, "hash": last["hash"]}


def test_deleting_the_last_entry_is_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")  # cut off entry 3

    check = audit.verify_log(path)
    assert not check.ok
    assert check.truncated_after == 2
    assert "truncated after entry 2" in check.detail


def test_deleting_the_whole_log_is_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed(path)
    path.unlink()
    check = audit.verify_log(path)
    assert not check.ok and check.truncated_after == 0


def test_log_without_anchor_still_verifies_but_says_so(tmp_path):
    # A log written before anchors existed: the chain still checks, with a caveat.
    path = tmp_path / "audit.jsonl"
    _seed(path)
    audit.anchor_path(path).unlink()
    check = audit.verify_log(path)
    assert check.ok and "truncation cannot be checked" in check.detail


def test_a_garbled_line_is_reported_not_crashed_on(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1][:40]  # cut entry 2 mid-record
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(audit.LogCorruptError, match="entry 2"):
        audit.read_log(path)
    check = audit.verify_log(path)
    assert not check.ok and check.bad_seq == 2 and "not valid JSON" in check.detail


def test_a_line_that_is_not_an_object_is_reported(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed(path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("42\n")
    check = audit.verify_log(path)
    assert not check.ok and check.bad_seq == 4


def test_a_garbled_head_anchor_is_reported(tmp_path):
    path = tmp_path / "audit.jsonl"
    _seed(path)
    audit.anchor_path(path).write_text("{oops", encoding="utf-8")
    check = audit.verify_log(path)
    assert not check.ok and "head anchor" in check.detail


def test_each_log_has_its_own_head_anchor(tmp_path):
    main, demo = tmp_path / "audit_log.jsonl", tmp_path / "demo_audit_log.jsonl"
    assert audit.anchor_path(main) == tmp_path / "audit_log_head.json"
    assert audit.anchor_path(demo) == tmp_path / "demo_audit_log_head.json"

    _seed(main)                                            # 3 entries
    audit.append(demo, {"event": "submit", "candidate_id": "RC-900", "by": "tom"})  # 1 entry
    assert audit.read_anchor(main)["seq"] == 3
    assert audit.read_anchor(demo)["seq"] == 1             # appending to one never moves the other
    assert audit.verify_log(main).ok and audit.verify_log(demo).ok

    # Truncating one log is caught against its own anchor; the other stays intact.
    lines = main.read_text(encoding="utf-8").splitlines()
    main.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
    assert audit.verify_log(main).truncated_after == 2
    assert audit.verify_log(demo).ok
