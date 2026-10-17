"""Old runs.csv files (written before the ``attempted`` column) can still be gated."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from lab import metrics
from lab.governance import gate

ROOT = Path(__file__).resolve().parent.parent


def _committed_rehearsal() -> pd.DataFrame:
    return pd.read_csv(ROOT / "results" / "runs.csv", keep_default_na=False)


def test_committed_rehearsal_has_no_attempted_column():
    assert "attempted" not in _committed_rehearsal().columns  # the premise of this file


def test_attempted_rate_falls_back_to_violated():
    runs = _committed_rehearsal()
    attempted = metrics.attempted_rate(runs)
    landed = metrics.attack_success_rate(runs)
    assert (attempted.successes, attempted.n) == (landed.successes, landed.n) == (12, 24)


def test_gate_summarise_and_summary_frame_on_old_csv():
    runs = _committed_rehearsal()
    s = gate.summarise(runs)
    assert s["attack_violations"] == 12 and s["attack_runs"] == 24
    assert s["attempted_count"] == 12 and s["attempted_runs"] == 24
    frame = metrics.summary_frame(runs).set_index("metric")
    assert int(frame.loc["attempted_rate", "successes"]) == 12
