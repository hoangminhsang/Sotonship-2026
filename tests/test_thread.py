"""Traceability: one finding from the failing run, through its fix, to the re-test."""
from __future__ import annotations

import pytest

from lab import corpus, defences, demo, metrics, world
from lab.assurance import load_control_map, risks, thread
from lab.governance import load_policy


@pytest.fixture
def study():
    """Runs with no defences (RC-001), the D2 re-test (RC-002), and its register."""
    # merge: read the committed split; tests never rewrite the tracked corpus (B-31)
    tests = corpus.read_jsonl(corpus.TEST_PATH)
    by_id, rules = {t["id"]: t for t in tests}, world.load_rules()
    policy, cmap = load_policy(), load_control_map()
    base = demo.demo_traces(tests)
    before = metrics.build_runs_frame(base, rules, by_id)
    after = metrics.build_runs_frame(defences.replay(base, rules, ["D2"]), rules, by_id)
    reg_before = risks.build_register(before, policy, cmap)
    reg_after = risks.build_register(after, policy, cmap, prior=risks.prior_from(reg_before))
    finding = before[(before["kind"] == "attack") & before["violated"]].iloc[0]["test_id"]
    return before, after, reg_after, finding


APPROVAL = [{"event": "decision", "seq": 5, "candidate_id": "RC-002", "decision": "APPROVE",
             "reviewer": "sarah"}]


def test_fixed_finding_is_traced_all_the_way_to_closure(study):
    before, after, reg, finding = study
    tr = thread.build_thread(finding, before, reg, risks.fix_tickets(reg), APPROVAL,
                             retest_candidate="RC-002", retest_runs=after)
    assert tr.chain.startswith(f"{finding} -> R-")
    assert "-> D2 ->" in tr.chain and "-> RC-002 -> entry #5 -> Mitigated" in tr.chain
    assert any(f"{finding} 0/3 runs violated" in s for s in tr.steps)
    assert "violation recorded" not in thread.to_markdown(tr)


def test_tracing_from_the_fixed_run_says_not_violated(study):
    # The old app bug: the re-test's runs were passed as the "before" runs, so the
    # chain stopped at the finding and still claimed "violation recorded".
    _, after, reg, finding = study
    tr = thread.build_thread(finding, after, reg, risks.fix_tickets(reg), APPROVAL,
                             retest_candidate="RC-002")
    assert tr.chain == f"{finding} -> not violated"
    text = thread.to_markdown(tr)
    assert "not violated in these runs (0/3 runs violated)" in text
    assert "violation recorded" not in text


def test_retest_without_this_finding_says_so(study):
    before, after, reg, finding = study
    tr = thread.build_thread(finding, before, reg, [], [], retest_candidate="RC-002",
                             retest_runs=after[after["test_id"] != finding])
    assert any("not found in the re-test" in s for s in tr.steps)


# ---------------------------------------------------------------------------
# trace_finding: which runs the story starts from, and the evidence warnings.
# ---------------------------------------------------------------------------

OLDER, NEWER = "2026-10-17T09:00:00+00:00", "2026-10-17T10:00:00+00:00"


def _rec(defences, created_at, mode="DEMO"):
    rec = {"submitted_by": "tom", "defences": defences, "created_at": created_at, "decision": None}
    if mode is not None:
        rec["mode"] = mode
    return rec


@pytest.fixture
def runs_by_defence():
    """Demo runs scored with no defences, D2 replayed, and D3 replayed."""
    # merge: read the committed split; tests never rewrite the tracked corpus (B-31)
    tests = corpus.read_jsonl(corpus.TEST_PATH)
    by_id, rules = {t["id"]: t for t in tests}, world.load_rules()
    base = demo.demo_traces(tests)
    frame = lambda traces: metrics.build_runs_frame(traces, rules, by_id)  # noqa: E731
    out = {"none": frame(base), "D2": frame(defences.replay(base, rules, ["D2"])),
           "D3": frame(defences.replay(base, rules, ["D3"]))}
    out["D2_1rep"] = frame(defences.replay(demo.demo_traces(tests, reps=1), rules, ["D2"]))
    return out, tests


def _read_restricted_finding(runs):
    """A finding that landed *only* as V_read_restricted with no defences."""
    hit = runs[(runs["kind"] == "attack") & runs["violated"] & (runs["violation_classes"] == "V_read_restricted")]
    return hit.iloc[0]["test_id"]


def _trace(finding, cand_id, cand_runs, cand, prior_id=None, prior_runs=None, prior=None):
    return thread.trace_finding(
        finding, candidate_id=cand_id, candidate_runs=cand_runs, candidate=cand,
        policy=load_policy(), control_map=load_control_map(), audit_events=[],
        prior_id=prior_id, prior_runs=prior_runs, prior=prior)


def _warnings(tr):
    return [s for s in tr.steps if s.startswith(("Warning:", "Note:"))]


def test_trace_fix_works_ends_mitigated_without_warnings(runs_by_defence):
    runs, _ = runs_by_defence
    f = _read_restricted_finding(runs["none"])
    tr = _trace(f, "RC-002", runs["D2"], _rec(["D2"], NEWER), "RC-001", runs["none"], _rec([], OLDER))
    assert tr.chain == f"{f} -> R-01 -> D2 -> FIX-01 -> RC-002 -> Mitigated"
    assert _warnings(tr) == []


def test_trace_reversed_order_traces_from_candidate_and_warns(runs_by_defence):
    runs, _ = runs_by_defence
    f = _read_restricted_finding(runs["none"])
    tr = _trace(f, "RC-001", runs["none"], _rec([], OLDER), "RC-002", runs["D2"], _rec(["D2"], NEWER))
    assert tr.chain == f"{f} -> R-01 -> D2 -> FIX-01 -> RC-001 -> Open"
    assert any(s.startswith("Landed in **RC-001**") for s in tr.steps)
    assert ("Warning: RC-002 is newer than RC-001. The prior should be the older candidate; "
            "did you mean to swap them?") in tr.steps


def test_trace_wrong_control_warns_but_keeps_the_computed_status(runs_by_defence):
    runs, _ = runs_by_defence
    f = _read_restricted_finding(runs["none"])
    # D2-blocked results, but the record says only D3 was on.
    tr = _trace(f, "RC-003", runs["D2"], _rec(["D3"], NEWER), "RC-001", runs["none"], _rec([], OLDER))
    expected = {r.id: r.status for r in risks.build_register(
        runs["D2"], load_policy(), load_control_map(),
        prior=risks.prior_from(risks.build_register(runs["none"], load_policy(), load_control_map())))}
    assert tr.chain.endswith(f"-> {expected['R-01']}")  # Mitigated, unchanged by the warning
    steps = tr.steps
    retest = next(i for i, s in enumerate(steps) if s.startswith("Re-tested as"))
    assert steps[retest + 1] == "Warning: D2 was not enabled in RC-003, so this drop is not evidence that D2 works."


def test_trace_real_d3_rerun_still_lands_so_no_false_warning(runs_by_defence):
    # Under D3 the restricted read still happens: no drop, status Open, nothing to warn about.
    runs, _ = runs_by_defence
    f = _read_restricted_finding(runs["none"])
    tr = _trace(f, "RC-003", runs["D3"], _rec(["D3"], NEWER), "RC-001", runs["none"], _rec([], OLDER))
    assert tr.chain.endswith("-> RC-003 -> Open") and _warnings(tr) == []


def test_trace_mode_mismatch_and_thin_evidence(runs_by_defence):
    runs, _ = runs_by_defence
    f = _read_restricted_finding(runs["none"])
    tr = _trace(f, "RC-005", runs["D2_1rep"], _rec(["D2"], NEWER, mode="LIVE"),
                "RC-003", runs["none"], _rec([], OLDER))
    assert ("Warning: RC-003 ran in DEMO and RC-005 in LIVE; the results are not directly comparable."
            in tr.steps)
    retest = next(s for s in tr.steps if s.startswith("Re-tested as"))
    assert f"0/1 runs violated (small sample; only 1 run(s), fewer than {thread.MIN_RUNS_PER_FINDING}; " \
           "LIVE results vary between runs)" in retest


def test_trace_legacy_record_without_mode_is_noted_once(runs_by_defence):
    runs, _ = runs_by_defence
    f = _read_restricted_finding(runs["none"])
    tr = _trace(f, "RC-002", runs["D2"], _rec(["D2"], NEWER, mode=None), "RC-001", runs["none"], _rec([], OLDER))
    notes = [s for s in tr.steps if "mode not recorded" in s]
    assert len(notes) == 1 and "mode not recorded for RC-002" in notes[0]
    assert tr.chain.endswith("-> Mitigated")


def test_trace_landed_nowhere_has_nothing_to_trace(runs_by_defence):
    runs, _ = runs_by_defence
    f = _read_restricted_finding(runs["none"])
    tr = _trace(f, "RC-004", runs["D2"], _rec(["D2"], NEWER), "RC-002", runs["D2"], _rec(["D2"], OLDER))
    assert tr.chain == f"{f} -> not violated"
    assert "nothing to trace" in tr.steps[0] and "RC-002 or RC-004" in tr.steps[0]


def test_trace_without_prior_traces_from_candidate(runs_by_defence):
    runs, _ = runs_by_defence
    f = _read_restricted_finding(runs["none"])
    tr = _trace(f, "RC-001", runs["none"], _rec([], OLDER))
    assert tr.chain == f"{f} -> R-01 -> D2 -> FIX-01 -> RC-001 -> Open" and _warnings(tr) == []


def test_thin_evidence_note_uses_the_threshold():
    m = thread.MIN_RUNS_PER_FINDING
    assert thread.thin_evidence_note(m, "DEMO") == ""
    assert "fewer than" in thread.thin_evidence_note(m - 1, "DEMO")
    assert "LIVE results vary" in thread.thin_evidence_note(m, "LIVE")
    assert thread.thin_evidence_note(m, None) == ""


def test_trace_uses_supplied_tickets_with_owners(runs_by_defence):
    runs, _ = runs_by_defence
    f = _read_restricted_finding(runs["none"])
    owned = risks.fix_tickets(risks.build_register(runs["none"], load_policy(), load_control_map()),
                              owners={"R-01": "tom"})
    tr = thread.trace_finding(
        f, candidate_id="RC-002", candidate_runs=runs["D2"], candidate=_rec(["D2"], NEWER),
        policy=load_policy(), control_map=load_control_map(), audit_events=[],
        prior_id="RC-001", prior_runs=runs["none"], prior=_rec([], OLDER), tickets=owned)
    assert any("FIX-01" in s and "(owner tom)" in s for s in tr.steps)
