"""Tests for the attack/benign corpus generator and the split."""
from __future__ import annotations

from lab import attacks, benign, corpus

REQUIRED_ATTACK_FIELDS = {
    "id", "kind", "goal", "technique", "place", "user_task", "poison",
    "inject_target", "expect_ticket", "expected_safe",
}


def test_generator_deterministic():
    assert attacks.generate(7) == attacks.generate(7)


def test_is_valid_excludes_nonsense_pairs():
    assert not attacks.is_valid("G1_read_restricted", "T4_hidden", "P3_asset_notes")
    assert not attacks.is_valid("G1_read_restricted", "T5_encoded", "P3_asset_notes")
    assert attacks.is_valid("G1_read_restricted", "T1_plain", "P3_asset_notes")
    assert attacks.is_valid("G2_exfiltrate", "T4_hidden", "P1_doc_body")


def test_every_attack_record_has_required_fields():
    for rec in attacks.generate(7):
        assert REQUIRED_ATTACK_FIELDS <= set(rec), rec["id"]
        assert rec["kind"] == "attack"
        assert rec["poison"]


def test_corpus_size_in_range():
    atk = attacks.generate(7)
    assert 55 <= len(atk) <= 63  # ~60 attacks as specified


def test_benign_records_well_formed():
    ben = benign.generate(7, 40)
    assert len(ben) == 40
    for rec in ben:
        assert rec["kind"] == "benign"
        assert rec["poison"] is None
        assert "expect_ticket" in rec


def test_split_disjoint_and_covers_every_technique():
    records = corpus.build_corpus(7, 40)
    dev, test = corpus.split(records, 7)
    dev_ids = {r["id"] for r in dev}
    test_ids = {r["id"] for r in test}
    assert dev_ids.isdisjoint(test_ids)
    assert len(dev) + len(test) == len(records)

    all_tech = {r["technique"] for r in records if r["kind"] == "attack"}
    dev_tech = {r["technique"] for r in dev if r["kind"] == "attack"}
    test_tech = {r["technique"] for r in test if r["kind"] == "attack"}
    assert all_tech <= dev_tech
    assert all_tech <= test_tech
    # both kinds present in each split
    assert {r["kind"] for r in dev} == {"attack", "benign"}
    assert {r["kind"] for r in test} == {"attack", "benign"}


def test_split_deterministic():
    records = corpus.build_corpus(7, 40)
    assert corpus.split(records, 7) == corpus.split(records, 7)


def test_t5_payload_is_base64_lure():
    recs = [r for r in attacks.generate(7) if r["technique"] == "T5_encoded"]
    assert recs
    assert all("decode" in r["poison"].lower() for r in recs)
