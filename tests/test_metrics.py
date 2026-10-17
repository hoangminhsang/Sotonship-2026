"""Tests for metrics and the Wilson confidence interval."""
from __future__ import annotations

import pandas as pd

from lab import metrics


def test_wilson_brackets_point_estimate():
    p = metrics.wilson(7, 10)
    assert p.rate == 0.7
    assert p.lo < 0.7 < p.hi
    assert 0.0 <= p.lo <= p.hi <= 1.0


def test_wilson_known_value():
    # Classic worked example: 0/10 successes, Wilson 95% upper bound ~0.278.
    p = metrics.wilson(0, 10)
    assert p.rate == 0.0
    assert p.lo == 0.0
    assert abs(p.hi - 0.2775) < 0.01


def test_wilson_zero_n():
    p = metrics.wilson(0, 0)
    assert (p.rate, p.lo, p.hi) == (0.0, 0.0, 0.0)


def _runs():
    rows = [
        # technique T1: 2/2 violated ; T2: 1/2 violated
        {"test_id": "A1", "kind": "attack", "technique": "T1", "rep": 1,
         "violated": True, "benign_success": False, "error": ""},
        {"test_id": "A1", "kind": "attack", "technique": "T1", "rep": 2,
         "violated": True, "benign_success": False, "error": ""},
        {"test_id": "A2", "kind": "attack", "technique": "T2", "rep": 1,
         "violated": True, "benign_success": False, "error": ""},
        {"test_id": "A2", "kind": "attack", "technique": "T2", "rep": 2,
         "violated": False, "benign_success": False, "error": ""},
        # one benign success, one benign failure
        {"test_id": "B1", "kind": "benign", "technique": "none", "rep": 1,
         "violated": False, "benign_success": True, "error": ""},
        {"test_id": "B2", "kind": "benign", "technique": "none", "rep": 1,
         "violated": False, "benign_success": False, "error": ""},
        # one error run, excluded from rates
        {"test_id": "A3", "kind": "attack", "technique": "T1", "rep": 1,
         "violated": False, "benign_success": False, "error": "boom"},
    ]
    return pd.DataFrame(rows)


def test_overall_asr_excludes_errors():
    runs = _runs()
    asr = metrics.attack_success_rate(runs)
    # 3 violations out of 4 non-error attack runs
    assert asr.successes == 3
    assert asr.n == 4
    assert abs(asr.rate - 0.75) < 1e-9


def test_per_technique_sums_to_overall():
    runs = _runs()
    table = metrics.asr_per_technique(runs)
    assert set(table["technique"]) == {"T1", "T2"}
    assert table.iloc[0]["technique"] == "T1"  # sorted by asr desc, T1=1.0
    assert int(table["violations"].sum()) == 3
    assert int(table["runs"].sum()) == 4


def test_benign_success_rate():
    runs = _runs()
    b = metrics.benign_task_success(runs)
    assert b.successes == 1
    assert b.n == 2


def test_error_count():
    assert metrics.error_count(_runs()) == 1


def test_ever_hijacked_rate():
    runs = _runs()
    ever = metrics.ever_hijacked_rate(runs)
    # A1 and A2 each broke at least once -> 2/2 tests ever hijacked
    assert ever.successes == 2
    assert ever.n == 2
