"""Tests for the candidate fingerprint."""
from __future__ import annotations

import json

from lab.governance import candidate


def _fp(**kw):
    base = dict(
        candidate_id="RC-001", submitted_by="tom", model="qwen2.5:7b-instruct",
        model_digest="sha256:aaa", system_prompt="You are a helpful assistant.",
        tools=["create_ticket", "read_doc", "query_assets"], defences=[],
        rules_text="rules", test_set_text="tests",
    )
    base.update(kw)
    return candidate.fingerprint(**base)


def test_fingerprint_is_deterministic():
    assert _fp().identity_hash() == _fp().identity_hash()


def test_tools_order_does_not_change_identity():
    a = _fp(tools=["read_doc", "query_assets", "create_ticket"])
    b = _fp(tools=["create_ticket", "read_doc", "query_assets"])
    assert a.identity_hash() == b.identity_hash()


def test_prompt_change_changes_identity():
    assert _fp().identity_hash() != _fp(system_prompt="different prompt").identity_hash()


def test_defences_change_identity():
    assert _fp().identity_hash() != _fp(defences=["D2"]).identity_hash()


def test_bookkeeping_does_not_change_identity():
    # candidate_id / submitted_by / created_at are not identity fields.
    assert _fp().identity_hash() == _fp(candidate_id="RC-999", submitted_by="sarah").identity_hash()


def test_save_and_load_roundtrip(tmp_path):
    fp = _fp(defences=["D2"])
    path = candidate.save(fp, tmp_path)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["identity_hash"] == fp.identity_hash()
    assert candidate.load(path).identity_hash() == fp.identity_hash()
