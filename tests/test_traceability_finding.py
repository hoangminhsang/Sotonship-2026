"""The traceability page follows a real finding of RC-001, never a hard-coded id.

The CLI used to fall back to the literal ``ATK-0021`` when no attack run leaked. That id means nothing in a
LIVE run on another model or a smaller sample. Now the finding is the first run of RC-001 that landed an
exfiltration (the DEMO story: ATK-0021), else the first attack run that broke any rule, else no finding
and the page says so.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent

NOTHING = "No attack run broke a rule in RC-001; nothing to trace."
VAULT_READ = {"name": "query_assets", "args": {"asset_id": "VAULT-01"}}


@pytest.fixture
def gov():
    spec = importlib.util.spec_from_file_location("run_governance_trace", ROOT / "scripts" / "run_governance.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_governance_trace"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def live(monkeypatch, tmp_path):
    """The LIVE stub pattern of tests/test_run_folders.py; ``state.calls`` is what the stub agent answers with."""
    from lab import run_folder, runner

    state = SimpleNamespace(root=tmp_path / "runs", calls=[])
    monkeypatch.setattr(runner.agent, "run_task",
                        lambda ag, task: SimpleNamespace(final_text="ok", tool_calls=list(state.calls), error=None))
    monkeypatch.setattr(runner, "model_digest", lambda *a, **k: "sha256:" + "ab" * 32)
    monkeypatch.setattr(runner, "_default_make_agent", lambda world_dir, temperature, defences=(): (object(), []))
    monkeypatch.setattr(run_folder, "RUNS_ROOT", state.root)
    return state


def trace_event(out: Path) -> dict:
    lines = (out / "progress.jsonl").read_text(encoding="utf-8").splitlines()
    (event,) = [e for e in map(json.loads, lines) if e["type"] == "trace"]
    return event


def frame(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["test_id", "kind", "violated", "violation_classes"])


# --------------------------------------------------------------------------- #
# pick_finding
# --------------------------------------------------------------------------- #
def test_pick_finding_prefers_the_first_exfiltration_run(gov):
    runs = frame([
        ("ATK-0002", "attack", True, "V_read_restricted"),
        ("ATK-0007", "attack", True, "V_exfiltration;V_read_restricted"),
        ("ATK-0009", "attack", True, "V_exfiltration"),
    ])
    assert gov.pick_finding(runs) == "ATK-0007"


def test_pick_finding_falls_back_to_the_first_violated_attack_run(gov):
    runs = frame([
        ("BEN-0001", "benign", True, "V_tool_misuse"),      # a benign run is never a finding
        ("ATK-0002", "attack", False, ""),
        ("ATK-0006", "attack", True, "V_tool_misuse"),
        ("ATK-0009", "attack", True, "V_read_restricted"),
    ])
    assert gov.pick_finding(runs) == "ATK-0006"


def test_pick_finding_is_none_when_no_attack_run_broke_a_rule(gov):
    assert gov.pick_finding(frame([("ATK-0002", "attack", False, ""), ("BEN-0001", "benign", False, "")])) is None
    assert gov.pick_finding(frame([])) is None                            # an empty frame has float columns
    assert gov.pick_finding(frame([("ATK-0002", "attack", False, float("nan"))])) is None


# --------------------------------------------------------------------------- #
# In the pipeline.
# --------------------------------------------------------------------------- #
def test_the_demo_story_still_traces_atk_0021(gov, tmp_path):
    out = tmp_path / "gov"
    assert gov.main(["--out", str(out), "--no-tamper-demo"]) == 0
    assert trace_event(out)["finding"] == "ATK-0021" and trace_event(out)["steps_n"] == 12
    assert "**Traceability for ATK-0021**" in (out / "traceability.md").read_text(encoding="utf-8")


def test_a_live_run_with_no_exfiltration_traces_the_first_violated_attack_run(gov, live, tmp_path):
    live.calls = [VAULT_READ]                      # reads a restricted asset: V_read_restricted, no exfiltration
    out = tmp_path / "gov"
    assert gov.main(["--live", "--sample", "1", "--reps", "1", "--out", str(out), "--no-tamper-demo"]) == 0
    text = (out / "traceability.md").read_text(encoding="utf-8")
    assert "**Traceability for ATK-0002**" in text and "ATK-0021" not in text
    assert trace_event(out)["finding"] == "ATK-0002" and trace_event(out)["steps_n"] > 0


def test_a_live_run_where_nothing_landed_says_there_is_nothing_to_trace(gov, live, tmp_path):
    live.calls = []
    out = tmp_path / "gov"
    assert gov.main(["--live", "--sample", "1", "--reps", "1", "--out", str(out), "--no-tamper-demo"]) == 0
    text = (out / "traceability.md").read_text(encoding="utf-8")
    assert text.startswith("**Mode: [LIVE]**") and text.rstrip().endswith(NOTHING)
    assert "ATK-0021" not in text and "not found in runs" not in text
    assert trace_event(out) == {**trace_event(out), "finding": None, "steps_n": 0}


def test_the_literal_is_gone_from_the_cli(gov):
    assert "ATK-0021" not in (ROOT / "scripts" / "run_governance.py").read_text(encoding="utf-8")
