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


# --------------------------------------------------------------------------- #
# Item 2: D-02-lite -- LIVE error count; all-errored run saves nothing.
# --------------------------------------------------------------------------- #

def _error_traces(subset, reps, defences, error="ConnectError: [Errno 61] Connection refused"):
    return [{
        "test_id": t["id"], "kind": t["kind"], "goal": t.get("goal"),
        "technique": t.get("technique", "none"), "place": t.get("place", "none"),
        "rep": r, "user_task": t["user_task"], "expect_ticket": t.get("expect_ticket", False),
        "expect_asset": t.get("expect_asset"), "defences": list(defences),
        "tool_calls": [], "error": error,
    } for t in subset for r in range(1, reps + 1)]


def _stub_runner(monkeypatch, make_traces):
    """Replace the live runner with ``make_traces(subset, reps, defences)``; no Ollama."""
    from lab import runner

    def fake_run_split(subset, reps=3, defences=(), **kwargs):
        yield from make_traces(subset, reps, defences)

    monkeypatch.setattr(runner, "run_split", fake_run_split)
    monkeypatch.setattr(runner, "model_digest", lambda *a, **k: "sha256:stub")


# Red team -> new candidate, by tom, no defences, LIVE, sample size 2.
NEW_LIVE_CANDIDATE = ["1", "1", "", "", "", "2", "2"]


def test_live_all_errored_saves_nothing(tmp_path, monkeypatch, capsys):
    _stub_runner(monkeypatch, _error_traces)
    work = tmp_path / "governance_out"
    out = run_app(monkeypatch, capsys, work, [*NEW_LIVE_CANDIDATE, "", "3", "4"])

    assert "12 runs, 12 errored" in out
    assert "Ollama unreachable or model missing" in out and "no candidate saved" in out
    assert not (work / "candidates.json").exists() or "RC-001" not in json.loads((work / "candidates.json").read_text())
    assert not (work / "RC-001").exists()
    assert not [e for e in audit.read_log(work / "audit_log.jsonl") if e["event"] == "submit"]


def test_live_partial_errors_prints_count_and_saves(tmp_path, monkeypatch, capsys):
    from lab import demo

    def half_errored(subset, reps, defences):
        traces = demo.demo_traces(subset, reps)
        for tr in traces[:5]:
            tr["error"] = "ConnectError: timed out"
            tr["tool_calls"] = []
        return traces

    _stub_runner(monkeypatch, half_errored)
    work = tmp_path / "governance_out"
    out = run_app(monkeypatch, capsys, work, [*NEW_LIVE_CANDIDATE, "", "3", "4"])

    assert "12 runs, 5 errored" in out
    rec = json.loads((work / "candidates.json").read_text())["RC-001"]
    assert rec["mode"] == "LIVE" and rec["summary"]["errors"] == 5
    assert rec["source"].startswith("live:")
    assert all("[LIVE]" in ln for ln in _lines_with(out, "errors 5"))


def test_live_runner_without_new_kwargs_still_runs(tmp_path, monkeypatch, capsys):
    """Before fix 1 merges, runner.run_split has no candidate=/split= kwargs."""
    from lab import demo, runner

    def old_run_split(subset, reps=3, defences=()):
        yield from demo.demo_traces(subset, reps)

    monkeypatch.setattr(runner, "run_split", old_run_split)
    monkeypatch.setattr(runner, "model_digest", lambda *a, **k: "")
    work = tmp_path / "governance_out"
    run_app(monkeypatch, capsys, work, [*NEW_LIVE_CANDIDATE, "", "3", "4"])
    assert json.loads((work / "candidates.json").read_text())["RC-001"]["mode"] == "LIVE"


# --------------------------------------------------------------------------- #
# Item 2: scripts/preflight.py (stdlib only; network monkeypatched).
# --------------------------------------------------------------------------- #

def _load_preflight():
    import importlib.util
    from pathlib import Path
    path = Path(app.ROOT) / "scripts" / "preflight.py"
    spec = importlib.util.spec_from_file_location("preflight", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TAGS = {"models": [{"name": "qwen2.5:7b-instruct", "digest": "sha256:" + "ab" * 32}]}
CLEAN_ENV = {"OLLAMA_HOST": "http://localhost:11434", "LAB_MODEL": "qwen2.5:7b-instruct"}


def test_preflight_passes_on_a_ready_machine(tmp_path, capsys):
    pf = _load_preflight()
    log = tmp_path / "audit_log.jsonl"
    audit.append(log, {"event": "submit", "candidate_id": "RC-001"})
    rc = pf.main(["--log", str(log), "--digest", "sha256:" + "ab" * 32],
                 fetch=lambda host, timeout: TAGS, env=CLEAN_ENV)
    out = capsys.readouterr().out
    assert rc == 0
    assert "sha256:" + "ab" * 32 in out          # the digest is printed
    assert "FAIL" not in out


def test_preflight_fails_one_line_per_problem(tmp_path, capsys):
    pf = _load_preflight()
    log = tmp_path / "audit_log.jsonl"
    audit.append(log, {"event": "submit", "candidate_id": "RC-001"})
    audit.append(log, {"event": "verdict", "candidate_id": "RC-001"})
    lines = log.read_text().splitlines()
    lines[0] = lines[0].replace("RC-001", "RC-002")        # tamper entry 1
    log.write_text("\n".join(lines) + "\n")

    def down(host, timeout):
        raise OSError("connection refused")

    env = {**CLEAN_ENV, "OLLAMA_HOST": "http://10.0.0.5:11434",
           "LANGCHAIN_TRACING_V2": "true", "LANGSMITH_API_KEY": "ls-secret"}
    rc = pf.main(["--log", str(log)], fetch=down, env=env)
    out = capsys.readouterr().out
    fails = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
    assert rc != 0
    assert any("ollama" in ln for ln in fails)
    assert any("audit" in ln and "1" in ln for ln in fails)
    assert any("host" in ln for ln in fails)
    assert any("LANGCHAIN_TRACING_V2" in ln for ln in fails)
    assert any("LANGSMITH_API_KEY" in ln for ln in fails)
    assert "ls-secret" not in out                           # never print a secret


def test_preflight_model_missing_and_digest_mismatch(tmp_path, capsys):
    pf = _load_preflight()
    rc = pf.main(["--log", str(tmp_path / "none.jsonl")],
                 fetch=lambda host, timeout: {"models": [{"name": "llama3:8b", "digest": "sha256:ff"}]},
                 env=CLEAN_ENV)
    out = capsys.readouterr().out
    assert rc != 0 and any(ln.startswith("FAIL") and "model" in ln for ln in out.splitlines())

    rc = pf.main(["--log", str(tmp_path / "none.jsonl"), "--digest", "sha256:other"],
                 fetch=lambda host, timeout: TAGS, env=CLEAN_ENV)
    out = capsys.readouterr().out
    assert rc != 0 and any(ln.startswith("FAIL") and "digest" in ln for ln in out.splitlines())
