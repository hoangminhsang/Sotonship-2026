"""Tests for the deterministic release gate."""
from __future__ import annotations

import pytest

from lab.governance import gate, load_policy


@pytest.fixture
def policy() -> dict:
    return load_policy()


def _summary(asr, attack_runs, benign_rate, benign_n):
    return {
        "asr": asr, "asr_lo": 0.0, "asr_hi": 1.0,
        "attack_runs": attack_runs, "attack_violations": round(asr * attack_runs),
        "benign_rate": benign_rate, "benign_n": benign_n, "errors": 0,
    }


def test_clean_candidate_passes(policy):
    verdict = gate.evaluate(_summary(0.0, 60, 1.0, 40), policy)
    assert verdict.passed
    assert verdict.status == "PASS"


def test_high_asr_fails(policy):
    verdict = gate.evaluate(_summary(0.5, 60, 1.0, 40), policy)
    assert not verdict.passed
    assert any(c.name == "attack_success_rate" and not c.ok for c in verdict.checks)


def test_too_few_runs_always_fails(policy):
    # Even a perfect 0% ASR fails if there are not enough runs to trust it.
    verdict = gate.evaluate(_summary(0.0, 24, 1.0, 40), policy)
    assert not verdict.passed
    assert any(c.name == "min_attack_runs" and not c.ok for c in verdict.checks)


def test_benign_not_measured_fails(policy):
    verdict = gate.evaluate(_summary(0.0, 60, 0.0, 0), policy)
    assert not verdict.passed
    benign = next(c for c in verdict.checks if c.name == "min_benign_task_success")
    assert not benign.ok and "not measured" in benign.note


def test_low_benign_fails(policy):
    verdict = gate.evaluate(_summary(0.0, 60, 0.5, 40), policy)
    assert any(c.name == "min_benign_task_success" and not c.ok for c in verdict.checks)


def test_regression_against_last_approved_fails(policy):
    # New ASR (2%) is under the 5% limit but worse than the last approved (0%).
    verdict = gate.evaluate(_summary(0.02, 60, 1.0, 40), policy, last_approved_asr=0.0)
    assert not verdict.passed
    assert any(c.name == "no_regression_vs_last_approved" and not c.ok for c in verdict.checks)


def test_report_text_lists_verdict(policy):
    verdict = gate.evaluate(_summary(0.5, 24, 0.0, 0), policy)
    text = gate.report_text("RC-001", verdict)
    assert "RC-001" in text and "VERDICT: FAIL" in text
