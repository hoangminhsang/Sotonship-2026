"""scripts/run_governance.py never deletes an audit log, logs refusals, and documents its defaults."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from lab import corpus
from lab.governance import audit

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def gov():
    spec = importlib.util.spec_from_file_location("run_governance_cli", ROOT / "scripts" / "run_governance.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_governance_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


def _demo(gov, out: Path, *extra: str) -> int:
    return gov.main(["--out", str(out), "--no-tamper-demo", *extra])


def test_default_out_is_the_cli_demo_folder(gov):
    default = Path(gov.build_parser().get_default("out"))
    assert default == ROOT / "governance_out" / "cli_demo"
    assert default != ROOT / "governance_out"  # the menu app's workspace is never the CLI's --out


def test_existing_audit_log_is_rotated_never_deleted(gov, tmp_path, capsys):
    out = tmp_path / "gov"
    assert _demo(gov, out) == 0
    log = out / "audit_log.jsonl"
    first = log.read_bytes()
    assert audit.verify_log(log).ok

    assert _demo(gov, out) == 0
    rotated = sorted(p for p in out.glob("audit_log.*.jsonl") if p != log)
    assert len(rotated) == 1, list(out.iterdir())
    assert rotated[0].read_bytes() == first          # old log intact, byte for byte
    assert audit.verify_log(rotated[0]).ok           # and still verifies
    assert audit.verify_log(log).ok                  # the new log is a fresh chain
    assert audit.read_log(log)[0]["seq"] == 1
    assert rotated[0].name in capsys.readouterr().out  # the script says what it did


def test_keep_log_appends(gov, tmp_path):
    out = tmp_path / "gov"
    assert _demo(gov, out) == 0
    log = out / "audit_log.jsonl"
    first = log.read_bytes()
    n = len(audit.read_log(log))

    assert _demo(gov, out, "--keep-log") == 0
    assert log.read_bytes().startswith(first)        # nothing rewritten
    assert len(audit.read_log(log)) == 2 * n
    assert audit.verify_log(log).ok
    assert not [p for p in out.glob("audit_log.*.jsonl") if p != log]  # no rotation


def test_self_review_refusal_is_logged(gov, tmp_path):
    out = tmp_path / "gov"
    assert _demo(gov, out) == 0
    events = audit.read_log(out / "audit_log.jsonl")
    refused = [e for e in events if e.get("event") == "decision_refused"]
    assert len(refused) == 1
    assert refused[0]["candidate_id"] == "RC-001"
    assert refused[0]["reviewer"] == "tom" and refused[0]["action"] == "APPROVE"
    assert refused[0]["detail"]


def _gate_counts(text: str) -> dict:
    """Pull 'x/y' counts and the verdict out of a gate report."""
    import re

    asr = re.search(r"attack_success_rate.*?\((\d+)/(\d+)", text)
    benign = re.search(r"min_benign_task_success.*?\((\d+)/(\d+)", text)
    verdict = re.search(r"VERDICT: (\w+)", text).group(1)
    return {"asr": f"{asr.group(1)}/{asr.group(2)}", "benign": f"{benign.group(1)}/{benign.group(2)}",
            "verdict": verdict}


def test_demo_three_step_story_numbers(gov, tmp_path, capsys):
    out = tmp_path / "gov"
    assert _demo(gov, out) == 0
    got = {cid: _gate_counts((out / f"gate_{cid}.txt").read_text()) for cid in ("RC-001", "RC-002", "RC-003")}
    assert got["RC-001"] == {"asr": "33/78", "benign": "60/60", "verdict": "FAIL"}
    assert got["RC-002"] == {"asr": "15/78", "benign": "60/60", "verdict": "FAIL"}   # D2: leaks stop, P1 misuse lands
    assert got["RC-003"] == {"asr": "0/78", "benign": "60/60", "verdict": "PASS"}    # D2+D3
    stdout = capsys.readouterr().out
    assert "attack success 42.3% -> 19.2% (D2) -> 0.0% (D2+D3)" in stdout
    assert stdout.count("TAMPERING DETECTED") == 0  # --no-tamper-demo honoured; the demo runs once otherwise


def test_demo_evidence_registers_name_what_each_control_proves(gov, tmp_path):
    out = tmp_path / "gov"
    assert _demo(gov, out) == 0
    rc2 = (out / "evidence_register_RC-002.md").read_text()
    assert "0/78 runs with V_read_restricted or V_exfiltration on the test set" in rc2
    assert "not enforced in this candidate (defences: D2): 15/78 runs with V_tool_misuse" in rc2
    rc3 = (out / "evidence_register_RC-003.md").read_text()
    assert "0/78 runs with V_tool_misuse on the test set" in rc3
    assert (out / "evidence_register.md").read_text() == rc3   # the final register keeps its old name
    for cid in ("RC-002", "RC-003"):
        assert str(out / cid / "runs.csv") in (out / f"evidence_register_{cid}.md").read_text()


def test_demo_decisions_and_risk_status_per_candidate(gov, tmp_path):
    out = tmp_path / "gov"
    assert _demo(gov, out) == 0
    events = audit.read_log(out / "audit_log.jsonl")
    decisions = [(e["candidate_id"], e["decision"]) for e in events if e.get("decision")]
    assert decisions == [("RC-001", "BLOCK"), ("RC-002", "BLOCK"), ("RC-003", "APPROVE")]
    assert audit.verify_log(out / "audit_log.jsonl").ok
    rc2 = (out / "risk_register_RC-002.md").read_text()
    assert "R-03" in rc2 and "Open" in rc2.split("R-03")[1].split("\n")[0]   # tool misuse still open under D2
    rc3 = (out / "risk_register_RC-003.md").read_text()
    assert "Open" not in rc3 and "Re-opened" not in rc3
    assert (out / "risk_register_after.md").read_text() == rc3
    tamper = tmp_path / "gov2"
    assert gov.main(["--out", str(tamper)]) == 0
    assert (tamper / "audit_log_tampered_demo.jsonl").is_file()


SCRIPTED_SENTENCE = "benign 60/60 is scripted, not measured"


def test_demo_artifacts_carry_the_scripted_label(gov, tmp_path, capsys):
    out = tmp_path / "gov"
    assert _demo(gov, out) == 0
    stdout = capsys.readouterr().out
    # Header and every summary/headline line say the numbers are scripted.
    assert "Scripted traces calibrated to the 24-run rehearsal; " + SCRIPTED_SENTENCE in stdout
    assert "[SCRIPTED] ASR 42.3%" in stdout
    assert "[SCRIPTED] Headline: attack success 42.3%" in stdout
    for cid in ("RC-001", "RC-002", "RC-003"):
        assert (out / f"gate_{cid}.txt").read_text().startswith(f"[SCRIPTED] Candidate {cid}")
    for name in ("evidence_register_RC-002.md", "evidence_register.md", "inventory_card.md",
                 "risk_register_before.md", "risk_register_RC-003.md", "traceability.md", "fix_tickets.md"):
        assert (out / name).read_text().startswith("**Mode: [SCRIPTED]**"), name
    ms = (out / "manager_summary.md").read_text()
    assert ms.startswith("**Mode: [SCRIPTED]**") and SCRIPTED_SENTENCE in ms
    assert "mode SCRIPTED" in ms and "for RC-003" in ms and "95% CI" in ms
    assert "**Audit log:** 10 entries, head sha256:" in ms or "**Audit log:** 10 entries, head " in ms


def test_cli_audit_events_are_marked_scripted(gov, tmp_path):
    out = tmp_path / "gov"
    assert _demo(gov, out) == 0
    events = audit.read_log(out / "audit_log.jsonl")
    assert len(events) == 10
    assert all(e.get("scripted") is True for e in events), [e["event"] for e in events if not e.get("scripted")]
    assert {e["event"] for e in events} == {"submit", "verdict", "decision_refused", "decision"}


def test_regression_baseline_compares_like_with_like(gov):
    """The no-regression ceiling applies only to the same test set, reps and mode (A-03)."""
    approved = {"asr": 0.0, "test_set_sha256": "sha256:a", "reps": 3, "mode": "SCRIPTED"}
    same = dict(approved)
    assert gov.regression_baseline(approved, same) == (0.0, None)
    assert gov.regression_baseline(None, same) == (None, None)
    for change in ({"test_set_sha256": "sha256:b"}, {"reps": 1}, {"mode": "LIVE"}):
        asr, reason = gov.regression_baseline(approved, {**same, **change})
        assert asr is None and reason and "not comparable" in reason, change


def test_demo_regression_check_uses_last_approved_when_comparable(gov, tmp_path, capsys):
    out = tmp_path / "gov"
    assert _demo(gov, out) == 0
    # RC-003 is the first approval; before it nothing is comparable and the report says so.
    stdout = capsys.readouterr().out
    assert "no approved candidate to compare against" in stdout


def test_attempted_figure_shows_its_wilson_ci(gov, tmp_path, capsys):
    """Attempted (incl. blocked) carries n and a 95% CI wherever it is shown (A-08)."""
    import re

    from lab import metrics

    out = tmp_path / "gov"
    assert _demo(gov, out) == 0
    ci = metrics.wilson(33, 78)
    expect = f"33/78 95% CI [{ci.lo:.1%}, {ci.hi:.1%}]"
    for cid in ("RC-001", "RC-002", "RC-003"):   # scripted attempts are 33/78 in all three
        text = (out / f"gate_{cid}.txt").read_text()
        line = next(ln for ln in text.splitlines() if "attempted" in ln.lower() and "not a limit" in ln)
        assert expect in line, line
        assert text.rstrip().endswith(("VERDICT: FAIL (1 reason)", "VERDICT: PASS"))
    stdout = capsys.readouterr().out
    assert re.search(r"Attempted \(incl\. blocked\): 42\.3% \[32\.0%, 53\.4%\] -> 42\.3% \[32\.0%, 53\.4%\] \(D2\) -> 42\.3% \[32\.0%, 53\.4%\] \(D2\+D3\)", stdout)
    assert "[SCRIPTED] ASR 42.3% (33/78) 95% CI [32.0%, 53.4%]" in stdout
    assert "attempted 33/78 (42.3%) 95% CI [32.0%, 53.4%]" in stdout


def test_demo_runs_csv_is_written_sorted_with_its_summary(gov, tmp_path):
    """DEMO/replay runs.csv goes through metrics.write_outputs: sorted rows, summary beside it (C-07)."""
    import pandas as pd

    out = tmp_path / "gov"
    assert _demo(gov, out) == 0
    for cid in ("RC-001", "RC-002", "RC-003"):
        runs = pd.read_csv(out / cid / "runs.csv", keep_default_na=False)
        expected = runs.sort_values(["kind", "technique", "violated", "test_id", "rep"],
                                    ascending=[True, True, False, True, True], kind="stable")
        assert list(runs.index) == list(expected.index), cid
        assert (runs["trace_path"] == "").all()
        summary = pd.read_csv(out / cid / "summary.csv").set_index("metric")
        assert "attack_success_rate" in summary.index and "attempted_rate" in summary.index


def test_help_text_states_real_defaults(gov):
    import re

    text = gov.build_parser().format_help()
    assert "(default: True)" not in text
    assert "--no-tamper-demo" in text
    joined = re.sub(r"\n\s+", "", text)  # argparse wraps the long absolute default path
    assert "governance_out/cli_demo" in joined


# --------------------------------------------------------------------------- #
# Race fix: a DEMO run reads the corpus, it never rewrites it.
# --------------------------------------------------------------------------- #
def _corpus_state() -> dict:
    return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (corpus.DEV_PATH, corpus.TEST_PATH)}


def test_demo_run_leaves_the_corpus_untouched(gov, tmp_path):
    """Two runs at once must not rewrite corpus/*.jsonl under each other (bytes AND mtime)."""
    before = _corpus_state()
    assert _demo(gov, tmp_path / "gov") == 0
    assert _corpus_state() == before


def test_corpus_mismatch_exits_2_with_one_line_and_writes_nothing(gov, tmp_path, monkeypatch, capsys):
    """A corpus that differs from the generator stops the run (exit 2); it is never rewritten."""
    def mismatch(*a, **k):
        raise corpus.CorpusMismatch("corpus file(s) ['test.jsonl'] differ from the seed-7 generator output.")

    def no_rewrite(*a, **k):
        raise AssertionError("the CLI must not rewrite the corpus")

    monkeypatch.setattr(corpus, "check_matches_generator", mismatch)
    monkeypatch.setattr(corpus, "build_and_write", no_rewrite)
    before = _corpus_state()
    out = tmp_path / "gov"
    assert _demo(gov, out) == 2
    captured = capsys.readouterr()
    lines = [ln for ln in captured.err.splitlines() if ln.strip()]
    assert len(lines) == 1 and "test.jsonl" in lines[0] and lines[0].startswith("error:"), captured.err
    assert not (out / "audit_log.jsonl").exists()      # stopped before any artifact
    assert _corpus_state() == before


# --------------------------------------------------------------------------- #
# main(): line-buffered stdout, and SIGTERM / SIGBREAK stop the run like Ctrl-C.
# --------------------------------------------------------------------------- #
def test_main_line_buffers_stdout_while_it_runs_and_puts_it_back(gov, monkeypatch):
    import io

    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    assert stream.line_buffering is False
    seen = {}

    def fake_run(args, argv=None):
        seen["during"] = sys.stdout.line_buffering
        return 0

    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(gov, "run", fake_run)
    assert gov.main(["--no-tamper-demo"]) == 0
    assert seen["during"] is True          # a viewer reading the pipe sees each line at once
    assert stream.line_buffering is False  # an in-process caller's stream is left as it was


def test_main_copes_with_a_stdout_that_cannot_be_reconfigured(gov, monkeypatch):
    import io

    monkeypatch.setattr(sys, "stdout", io.StringIO())            # no reconfigure()
    monkeypatch.setattr(gov, "run", lambda args, argv=None: 0)
    assert gov.main(["--no-tamper-demo"]) == 0


def test_sigterm_and_sigbreak_raise_keyboard_interrupt_only_while_main_runs(gov, monkeypatch):
    import signal

    names = [n for n in ("SIGTERM", "SIGBREAK") if hasattr(signal, n)]    # SIGBREAK: Windows only
    assert "SIGTERM" in names
    before = {n: signal.getsignal(getattr(signal, n)) for n in names}
    seen = {}

    def fake_run(args, argv=None):
        seen.update({n: signal.getsignal(getattr(signal, n)) for n in names})
        return 0

    monkeypatch.setattr(gov, "run", fake_run)
    assert gov.main(["--no-tamper-demo"]) == 0
    for n in names:
        handler = seen[n]
        assert callable(handler) and handler != before[n], n
        with pytest.raises(KeyboardInterrupt):                    # what Ctrl-C raises, so the folder gets locked
            handler(getattr(signal, n), None)
        assert signal.getsignal(getattr(signal, n)) == before[n], n   # put back after the run


def test_main_off_the_main_thread_still_runs(gov, monkeypatch):
    """signal.signal only works on the main thread; a caller on another thread must not crash."""
    import threading

    monkeypatch.setattr(gov, "run", lambda args, argv=None: 7)
    result = {}
    thread = threading.Thread(target=lambda: result.update(code=gov.main(["--no-tamper-demo"])))
    thread.start()
    thread.join(10)
    assert result == {"code": 7}

