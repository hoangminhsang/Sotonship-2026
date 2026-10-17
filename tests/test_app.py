"""Smoke tests for the menu app (``lab/app.py``). Fully offline, no model.

Every test drives :func:`lab.app.main` with a patched ``input()`` and points the
workspace at a temp folder, so nothing is written inside the repo.
"""
from __future__ import annotations

import json

import pytest

from lab import app
from lab.governance import audit


def run_app(monkeypatch, capsys, work, answers):
    """Run the app with ``answers`` fed to ``input()``; return captured stdout.

    An exception class in ``answers`` is raised at that prompt (to simulate
    EOF or Ctrl-C). Running out of answers raises, so a wrong script fails
    instead of looping forever.
    """
    monkeypatch.setattr(app, "WORK", work)
    it = iter(answers)

    def fake_input(prompt=""):
        try:
            a = next(it)
        except StopIteration:
            raise RuntimeError(f"test input exhausted at prompt {prompt!r}")
        if isinstance(a, type) and issubclass(a, BaseException):
            raise a()
        print(f"{prompt}{a}")
        return a

    monkeypatch.setattr("builtins.input", fake_input)
    app.main()
    return capsys.readouterr().out


# Red team -> new candidate RC-001, submitted by tom, no defences, DEMO.
NEW_DEMO_CANDIDATE = ["1", "1", "", "", "", "1", ""]


def _lines_with(out: str, needle: str) -> list[str]:
    return [ln for ln in out.splitlines() if needle in ln]


# --------------------------------------------------------------------------- #
# Item 1: D-01 run-mode labels, scripted digest, X-14 [scripted] log marker.
# --------------------------------------------------------------------------- #

def test_demo_candidate_records_mode_and_source(tmp_path, monkeypatch, capsys):
    work = tmp_path / "governance_out"
    run_app(monkeypatch, capsys, work, [*NEW_DEMO_CANDIDATE, "3", "4"])

    rec = json.loads((work / "candidates.json").read_text())["RC-001"]
    assert rec["mode"] == "SCRIPTED"
    assert rec["source"] == "scripted: calibrated to the 24-run rehearsal; benign not measured"

    fp = json.loads((work / "candidates" / "RC-001.json").read_text())
    assert fp["model_digest"] == "none (scripted demo)"
    assert "845dbda0" not in fp["model_digest"]

    submit = [e for e in audit.read_log(work / "audit_log.jsonl") if e["event"] == "submit"]
    assert submit and submit[0]["mode"] == "SCRIPTED" and submit[0]["scripted"] is True


def test_every_numbered_line_carries_the_mode_label(tmp_path, monkeypatch, capsys):
    work = tmp_path / "governance_out"
    out = run_app(monkeypatch, capsys, work, [
        *NEW_DEMO_CANDIDATE,
        "2", "",                 # list candidates
        "3",                     # back
        "2", "1", "", "",        # governance -> verdict RC-001
        "6",                     # back
        "3", "6", "", "", "",    # assurance -> manager summary (no prior)
        "7", "", "", "",         # inventory card
        "9", "4",                # back, quit
    ])

    # Candidate list: the ASR line names the mode and the next line the source.
    row = _lines_with(out, "42.3%")
    assert row and all("SCRIPTED" in ln for ln in row)
    assert "source: scripted: calibrated to the 24-run rehearsal" in out

    # Gate verdict: every check line and the verdict line are labelled.
    for key in ("attack_success_rate", "min_attack_runs", "min_benign_task_success", "VERDICT"):
        lines = _lines_with(out, key)
        assert lines and all("[SCRIPTED]" in ln for ln in lines), key

    # Manager summary and inventory card.
    assert all("[SCRIPTED]" in ln for ln in _lines_with(out, "Current attack rate"))
    digest_lines = _lines_with(out, "model digest")
    assert digest_lines and all("none (scripted demo)" in ln and "[SCRIPTED]" in ln for ln in digest_lines)
    assert "run mode:** SCRIPTED" in out


def test_view_log_marks_scripted_events(tmp_path, monkeypatch, capsys):
    work = tmp_path / "governance_out"
    work.mkdir()
    audit.append(work / "audit_log.jsonl", {"event": "decision", "candidate_id": "RC-009",
                                           "decision": "APPROVE", "reviewer": "sarah",
                                           "scripted": True})
    audit.append(work / "audit_log.jsonl", {"event": "decision", "candidate_id": "RC-008",
                                           "decision": "BLOCK", "reviewer": "sarah"})
    out = run_app(monkeypatch, capsys, work, ["2", "4", "", "6", "4"])

    assert all("[scripted]" in ln for ln in _lines_with(out, "RC-009"))
    assert all("[scripted]" not in ln for ln in _lines_with(out, "RC-008"))
