"""A risk closes only when its control is on (review X-01, Mitigated guard).

Zero landings of a class whose control the candidate does not run is absence
of evidence, not a fix: R-03 (V_tool_misuse, control D3) must stay Open for a
D2-only candidate even at 0/78.
"""
from __future__ import annotations

import pytest

from lab import corpus, defences, demo, metrics, world
from lab.assurance import load_control_map, risks
from lab.governance import load_policy


@pytest.fixture
def policy() -> dict:
    return load_policy()


@pytest.fixture
def control_map() -> dict:
    return load_control_map()


def _landed_all(make_runs):
    return make_runs([
        {"violated": True, "violation_classes": ["V_read_restricted", "V_exfiltration"]},
        {"violated": True, "violation_classes": ["V_tool_misuse"]},
        {"violated": False, "violation_classes": []},
    ])


def _clean(make_runs, n: int = 78):
    return make_runs([{"violated": False, "violation_classes": []} for _ in range(n)])


def _statuses(register) -> dict[str, str]:
    return {r.id: r.status for r in register}


def test_zero_rate_stays_open_when_control_is_not_in_defences(make_runs, policy, control_map):
    """D2-only candidate, 0/78 V_tool_misuse: R-03 Open (D3 absent); R-01/R-02 Mitigated (D2 on)."""
    prior = risks.prior_from(risks.build_register(_landed_all(make_runs), policy, control_map))
    after = risks.build_register(_clean(make_runs), policy, control_map, prior=prior, defences=["D2"])
    by_id = {r.id: r for r in after}
    assert (by_id["R-03"].successes, by_id["R-03"].runs) == (0, 78)
    assert by_id["R-03"].control == "D3"
    assert by_id["R-03"].status == "Open"
    assert "D3" in by_id["R-03"].note
    assert _statuses(after)["R-01"] == "Mitigated"
    assert _statuses(after)["R-02"] == "Mitigated"


def test_zero_rate_is_mitigated_when_control_is_on(make_runs, policy, control_map):
    prior = risks.prior_from(risks.build_register(_landed_all(make_runs), policy, control_map))
    after = risks.build_register(_clean(make_runs), policy, control_map, prior=prior, defences=["D2", "D3"])
    assert _statuses(after) == {"R-01": "Mitigated", "R-02": "Mitigated", "R-03": "Mitigated"}


def test_no_defences_closes_nothing(make_runs, policy, control_map):
    prior = risks.prior_from(risks.build_register(_landed_all(make_runs), policy, control_map))
    after = risks.build_register(_clean(make_runs), policy, control_map, prior=prior, defences=[])
    assert _statuses(after) == {"R-01": "Open", "R-02": "Open", "R-03": "Open"}


def test_omitting_defences_keeps_the_old_behaviour(make_runs, policy, control_map):
    """Backward compatible: callers that pass no ``defences`` get the rate-and-prior rule only."""
    prior = risks.prior_from(risks.build_register(_landed_all(make_runs), policy, control_map))
    after = risks.build_register(_clean(make_runs), policy, control_map, prior=prior)
    assert _statuses(after) == {"R-01": "Mitigated", "R-02": "Mitigated", "R-03": "Mitigated"}


def test_guarded_open_risk_still_gets_a_fix_ticket(make_runs, policy, control_map):
    prior = risks.prior_from(risks.build_register(_landed_all(make_runs), policy, control_map))
    after = risks.build_register(_clean(make_runs), policy, control_map, prior=prior, defences=["D2"])
    assert [t.id for t in risks.fix_tickets(after)] == ["FIX-03"]


def test_demo_rc002_keeps_r03_open_and_rc003_closes_it(policy, control_map):
    """On the real DEMO traces: D2 closes R-01/R-02, leaves R-03 Open at 15/78; D2+D3 closes all."""
    tests = corpus.read_jsonl(corpus.TEST_PATH)
    by_id, rules = {t["id"]: t for t in tests}, world.load_rules()
    base = demo.demo_traces(tests)
    frame = lambda codes: metrics.build_runs_frame(defences.replay(base, rules, codes), rules, by_id)

    before = risks.build_register(frame([]), policy, control_map)
    assert _statuses(before) == {"R-01": "Open", "R-02": "Open", "R-03": "Open"}
    prior = risks.prior_from(before)

    rc002 = risks.build_register(frame(["D2"]), policy, control_map, prior=prior, defences=["D2"])
    r = {x.id: x for x in rc002}
    assert (r["R-01"].successes, r["R-02"].successes, r["R-03"].successes) == (0, 0, 15)
    assert _statuses(rc002) == {"R-01": "Mitigated", "R-02": "Mitigated", "R-03": "Open"}

    rc003 = risks.build_register(frame(["D2", "D3"]), policy, control_map, prior=prior, defences=["D2", "D3"])
    assert all(x.successes == 0 for x in rc003)
    assert _statuses(rc003) == {"R-01": "Mitigated", "R-02": "Mitigated", "R-03": "Mitigated"}
