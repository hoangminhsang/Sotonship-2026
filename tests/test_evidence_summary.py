"""Evidence results per control, and the manager summary's attack rate."""
from __future__ import annotations

import pytest

from lab import corpus, defences, demo, metrics, world
from lab.assurance import evidence, load_control_map, risks, summary_page
from lab.governance import load_policy


@pytest.fixture
def registers():
    """Risk registers for the demo run with no defences and with D2 replayed."""
    corpus.build_and_write()
    tests = corpus.read_jsonl(corpus.TEST_PATH)
    by_id, rules = {t["id"]: t for t in tests}, world.load_rules()
    policy, cmap = load_policy(), load_control_map()
    base = demo.demo_traces(tests)
    before = risks.build_register(metrics.build_runs_frame(base, rules, by_id), policy, cmap)
    d2_runs = metrics.build_runs_frame(defences.replay(base, rules, ["D2"]), rules, by_id)
    after = risks.build_register(d2_runs, policy, cmap, prior=risks.prior_from(before))
    return before, after


def test_d2_blocks_the_p1_tickets_too(registers):
    # The landed demo attack's P1 ticket goes to an outside domain, so D2 refuses
    # the whole call: V_tool_misuse genuinely drops to zero with only D2 on.
    _, after = registers
    r03 = {r.id: r for r in after}["R-03"]
    assert r03.control == "D3" and r03.successes == 0


def test_evidence_does_not_credit_d3_for_d2s_side_effect(registers):
    _, after = registers
    res = evidence.results_from_register(after, ["D2"])
    assert res["D2"].startswith("Pass: 0/78 V_read_restricted, 0/78 V_exfiltration")
    assert res["D3"].startswith("Not yet run (D3 off)")
    assert "side effect of D2" in res["D3"]

    rows = {e.control: e.result for e in evidence.build_register(after, "RC-002", results=res)}
    assert rows["D3"].startswith("Not yet run")


def test_evidence_with_no_defences_and_a_failing_control(registers):
    before, _ = registers
    assert evidence.results_from_register(before, []) == {"D2": "Not yet run (D2 off)",
                                                         "D3": "Not yet run (D3 off)"}
    assert evidence.results_from_register(before, ["D2"])["D2"].startswith("Fail:")


def test_summary_reports_the_live_versions_attack_rate():
    events = [
        {"event": "decision", "candidate_id": "RC-001", "decision": "BLOCK", "reviewer": "sarah"},
        {"event": "decision", "candidate_id": "RC-002", "decision": "APPROVE", "reviewer": "sarah"},
    ]
    s = summary_page.build_summary([], events, asr_by_candidate={"RC-001": 0.423, "RC-002": 0.0})
    assert s.live_version == "RC-002" and s.current_asr == 0.0
    assert "**Current attack rate (RC-002):** 0.0%" in summary_page.to_markdown(s)


def test_summary_with_nothing_live_has_no_attack_rate():
    s = summary_page.build_summary([], [], asr_by_candidate={"RC-001": 0.423})
    assert s.live_version is None and s.current_asr is None
    assert "**Current attack rate:** n/a" in summary_page.to_markdown(s)


def test_proof_file_points_at_the_folder_the_runs_were_written_to(registers):
    _, after = registers
    rows = evidence.build_register(after, "RC-002", proof_dir="governance_out")
    assert {r.proof_file for r in rows} == {"governance_out/RC-002/runs.csv"}
    assert evidence.build_register(after, "RC-002")[0].proof_file == "results/RC-002/runs.csv"  # default kept


def test_summary_labels_whose_open_risks_it_lists(registers):
    before, _ = registers
    s = summary_page.build_summary(before, [], register_candidate="RC-001")
    assert "**Open risks in RC-001 (3):**" in summary_page.to_markdown(s)


def test_last_decision_ignores_later_submissions_and_refusals():
    events = [
        {"event": "decision", "candidate_id": "RC-002", "decision": "APPROVE", "reviewer": "sarah",
         "time": "2026-10-17T10:00:00"},
        {"event": "decision_refused", "candidate_id": "RC-001", "action": "OVERRIDE", "reviewer": "sarah"},
        {"event": "submit", "candidate_id": "RC-003", "by": "tom"},
    ]
    s = summary_page.build_summary([], events)
    assert s.last_decision == "APPROVE on RC-002 by sarah (2026-10-17)"
