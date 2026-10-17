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


# Red team -> new candidate RC-001, submitted by engineer #1 (tom), no defences, DEMO.
NEW_DEMO_CANDIDATE = ["1", "1", "", "1", "", "1", ""]


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
        "7",                     # back
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

    # Manager summary (nothing approved yet: the live rate is n/a, the viewed
    # candidate's own numbers are labelled) and inventory card.
    # With nothing approved the summary says so; the merged summary_page prints the
    # viewed candidate's own labelled rate ("... for RC-001, mode SCRIPTED") where the
    # app alone printed "n/a". Either form is honest; both must say "no approved version".
    rate = _lines_with(out, "Current attack rate")
    assert rate and all(
        "no approved version" in ln and ("n/a" in ln or "mode SCRIPTED" in ln or "[SCRIPTED]" in ln)
        for ln in rate
    )
    assert all("42.3%" in ln and "[SCRIPTED]" in ln for ln in _lines_with(out, "Viewed candidate"))
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
    out = run_app(monkeypatch, capsys, work, ["2", "4", "", "7", "4"])

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
NEW_LIVE_CANDIDATE = ["1", "1", "", "1", "", "2", "2"]


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


# --------------------------------------------------------------------------- #
# Item 3: robustness -- B-09, B-22/D-09, D-03, X-13.
# --------------------------------------------------------------------------- #

def test_eof_at_main_menu_exits_cleanly(tmp_path, monkeypatch, capsys):
    out = run_app(monkeypatch, capsys, tmp_path / "governance_out", [EOFError])
    assert "bye." in out


def test_ctrl_c_at_main_menu_exits_cleanly(tmp_path, monkeypatch, capsys):
    out = run_app(monkeypatch, capsys, tmp_path / "governance_out", [KeyboardInterrupt])
    assert "bye." in out


def test_ctrl_c_inside_an_action_returns_to_the_menu(tmp_path, monkeypatch, capsys):
    work = tmp_path / "governance_out"
    out = run_app(monkeypatch, capsys, work, ["1", "1", KeyboardInterrupt, "3", "4"])
    assert "(cancelled)" in out and "bye." in out
    assert not (work / "candidates.json").exists()


def test_non_integer_sample_size_reprompts(tmp_path, monkeypatch, capsys):
    from lab import demo
    _stub_runner(monkeypatch, lambda subset, reps, defences: demo.demo_traces(subset, reps))
    work = tmp_path / "governance_out"
    out = run_app(monkeypatch, capsys, work,
                  ["1", "1", "", "1", "", "2", "eight", "0", "2", "", "3", "4"])
    assert out.count("Sample size per split") == 3
    assert json.loads((work / "candidates.json").read_text())["RC-001"]["mode"] == "LIVE"


def test_ctrl_c_during_live_sweep_aborts_and_returns_to_menu(tmp_path, monkeypatch, capsys):
    from lab import demo

    def interrupted(subset, reps, defences):
        traces = demo.demo_traces(subset, reps)
        yield traces[0]
        raise KeyboardInterrupt

    _stub_runner(monkeypatch, interrupted)
    work = tmp_path / "governance_out"
    out = run_app(monkeypatch, capsys, work, [*NEW_LIVE_CANDIDATE, "", "3", "4"])
    assert "aborted" in out and "no candidate saved" in out and "bye." in out
    assert not (work / "candidates.json").exists()
    assert not [e for e in audit.read_log(work / "audit_log.jsonl") if e["event"] == "submit"]


def test_empty_subset_shows_not_measured(tmp_path, monkeypatch, capsys):
    _stub_runner(monkeypatch, lambda subset, reps, defences: [])
    work = tmp_path / "governance_out"
    out = run_app(monkeypatch, capsys, work, [*NEW_LIVE_CANDIDATE, "", "3", "4"])
    assert "not measured" in out and "no candidate saved" in out and "bye." in out
    assert not (work / "candidates.json").exists()


def test_action_error_prints_one_line_and_keeps_the_menu(tmp_path, monkeypatch, capsys):
    work = tmp_path / "governance_out"

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(app.gate, "report_text", boom)
    out = run_app(monkeypatch, capsys, work, [*NEW_DEMO_CANDIDATE, "3", "2", "1", "", "7", "4"])
    assert "error: RuntimeError: boom" in out
    assert "Traceback" not in out and "bye." in out


def test_live_sample_takes_first_n_attacks_plus_first_n_benign(tmp_path, monkeypatch, capsys):
    """X-05: never tests[:N]; a sample of N is N attacks + N benign, no duplicates."""
    from lab import corpus, demo
    seen: list[list[dict]] = []

    def record(subset, reps, defences):
        seen.append(list(subset))
        return demo.demo_traces(subset, reps)

    _stub_runner(monkeypatch, record)
    tests = corpus.read_jsonl(corpus.TEST_PATH)
    attacks = [t["id"] for t in tests if t["kind"] == "attack"]
    benign = [t["id"] for t in tests if t["kind"] == "benign"]

    out = run_app(monkeypatch, capsys, tmp_path / "governance_out",
                  ["1", "1", "", "1", "", "2", "3", "",           # RC-001, sample 3
                   "1", "", "1", "", "2", "40", "",               # RC-002, sample 40 (> 26 attacks)
                   "3", "4"])

    ids = [t["id"] for t in seen[0]]
    assert ids == attacks[:3] + benign[:3]
    assert "3 attacks + 3 benign = 18 runs" in out

    ids = [t["id"] for t in seen[1]]
    assert ids == attacks + benign and len(set(ids)) == len(ids) == 46
    assert "26 attacks + 20 benign = 138 runs" in out


# --------------------------------------------------------------------------- #
# Item 4: ids and identity -- B-20/D-06, B-03, B-19, B-21.
# --------------------------------------------------------------------------- #

def test_candidate_ids_are_validated_and_never_reused(tmp_path, monkeypatch, capsys):
    work = tmp_path / "governance_out"
    out = run_app(monkeypatch, capsys, work, [
        "1", "1", "../../escaped", "rc-1", "", "1", "", "1", "",   # two bad ids, then RC-001
        "1", "RC-001", "", "1", "", "1", "",                       # RC-001 refused -> RC-002
        "3", "4",
    ])
    recs = json.loads((work / "candidates.json").read_text())
    assert list(recs) == ["RC-001", "RC-002"]
    assert "already exists" in out and "RC-002" in out
    assert not list(tmp_path.glob("**/escaped*")) and not (app.ROOT / "escaped.json").exists()
    assert recs["RC-001"]["decision"] is None and (work / "RC-001" / "traces.json").exists()


def test_submitter_is_chosen_from_engineers_and_normalised(tmp_path, monkeypatch, capsys):
    work = tmp_path / "governance_out"
    work.mkdir()
    (work / "roles_overlay.json").write_text(json.dumps(
        {"engineers": [" Alice ", "tom"], "reviewers": ["sarah", "alice"], "rule_owner": "security-lead"}))
    out = run_app(monkeypatch, capsys, work, [
        *NEW_DEMO_CANDIDATE,                       # engineer #1 = alice
        "3",
        "2", "2", "", "2", " Alice ", "found it", "",   # alice tries to BLOCK her own candidate
        "7", "4",
    ])
    rec = json.loads((work / "candidates.json").read_text())["RC-001"]
    assert rec["submitted_by"] == "alice"
    assert "1. alice" in out and "2. tom" in out
    assert "REFUSED" in out and "submitted this candidate" in out
    assert rec["decision"] is None


def test_defence_codes_validated_deduped_and_d1_warned(tmp_path, monkeypatch, capsys):
    work = tmp_path / "governance_out"
    out = run_app(monkeypatch, capsys, work, [
        "1", "1", "", "1", "d2, D9", "d2,d2", "1", "",   # D9 rejected, then D2 (deduped)
        "1", "", "1", "D1", "1", "",                      # D1 in DEMO: warned
        "3", "4",
    ])
    recs = json.loads((work / "candidates.json").read_text())
    assert recs["RC-001"]["defences"] == ["D2"]
    assert "unknown defence code" in out and "D9" in out
    assert recs["RC-002"]["defences"] == ["D1"]
    assert "D1" in out and "cannot be replayed" in out


def test_role_refusal_logged_and_change_logged_before_saved(tmp_path, monkeypatch, capsys):
    work = tmp_path / "governance_out"
    real_append = audit.append

    def flaky_append(path, event):
        if event.get("event") == "role_change" and event.get("person") == "bob":
            raise RuntimeError("disk full")
        return real_append(path, event)

    monkeypatch.setattr(app.audit, "append", flaky_append)
    out = run_app(monkeypatch, capsys, work, [
        "2", "3", "1", "tom", "reviewers", "mallory", "",          # not the owner: refused
        "3", "1", "", "reviewers", "bob",                           # log write fails: not saved
        "3", "1", " Security-Lead ", "Reviewers", "Carol", "",      # normalised: saved
        "7", "4",
    ])
    events = audit.read_log(work / "audit_log.jsonl")
    refused = [e for e in events if e["event"] == "role_refused"]
    assert refused and refused[0]["actor"] == "tom" and refused[0]["person"] == "mallory"
    assert "disk full" in out
    assert not [e for e in events if e["event"] == "role_change" and e["person"] == "bob"]
    overlay = json.loads((work / "roles_overlay.json").read_text())
    assert overlay["reviewers"] == ["carol", "sarah"]              # bob never persisted
    changed = [e for e in events if e["event"] == "role_change"]
    assert changed[-1]["person"] == "carol" and changed[-1]["actor"] == "security-lead"


# --------------------------------------------------------------------------- #
# Item 5: D-05 -- tamper demo on a copy; the real log is never edited.
# --------------------------------------------------------------------------- #

def test_tamper_demo_runs_on_a_copy_and_never_edits_the_real_log(tmp_path, monkeypatch, capsys):
    work = tmp_path / "governance_out"
    out = run_app(monkeypatch, capsys, work, [
        *NEW_DEMO_CANDIDATE, "3",
        "2", "2", "", "2", "sarah", "leaks data", "",     # BLOCK RC-001 by sarah (entry #2)
        "6", "",                                          # Tamper demo (on a copy)
        "5", "",                                          # Verify the real log
        "7", "4",
    ])
    real = work / "audit_log.jsonl"
    copy = work / "tamper_demo" / "audit_log_copy.jsonl"
    assert copy.exists()
    assert not audit.verify_log(copy).ok and audit.verify_log(copy).bad_seq == 2
    assert audit.verify_log(real).ok
    events = audit.read_log(real)
    assert [e["event"] for e in events] == ["submit", "decision"]      # nothing extra, nothing changed
    assert events[1]["decision"] == "BLOCK"
    assert "TAMPERING DETECTED at entry #2" in out
    assert "real log" in out and "intact" in out
    assert "BLOCK" in out and "APPROVE" in out                         # the one field changed is named


# --------------------------------------------------------------------------- #
# Item 6: X-07 manager summary uses the live version's own numbers;
#         X-06 traceability uses the prior candidate's runs and tickets.
# --------------------------------------------------------------------------- #

# RC-002 with D2,D3 scripted: 0/78 PASS (same before and after the faithful demo).
SECOND_D2D3 = ["1", "", "1", "D2,D3", "1", ""]
APPROVE_LAST_BY_SARAH = ["2", "2", "", "1", "sarah", ""]


def test_manager_summary_uses_the_live_versions_numbers(tmp_path, monkeypatch, capsys):
    work = tmp_path / "governance_out"
    out = run_app(monkeypatch, capsys, work, [
        *NEW_DEMO_CANDIDATE, *SECOND_D2D3, "3",       # RC-001 none, RC-002 D2,D3
        *APPROVE_LAST_BY_SARAH, "7",                   # RC-002 approved -> live
        "3", "6", "RC-001", "", "",                    # manager summary while VIEWING RC-001
        "9", "4",
    ])
    recs = json.loads((work / "candidates.json").read_text())
    assert recs["RC-002"]["decision"] == "APPROVE"
    assert recs["RC-001"]["summary"]["mode"] == "SCRIPTED"

    assert "Live version:** RC-002" in out
    rate = _lines_with(out, "Current attack rate")
    assert rate and all("0.0%" in ln and "42.3%" not in ln and "[SCRIPTED]" in ln for ln in rate)
    live_ev = _lines_with(out, "Live version evidence")
    assert live_ev and "RC-002" in live_ev[0] and "95% CI" in live_ev[0] and "0/78" in live_ev[0]
    viewed = _lines_with(out, "Viewed candidate")
    assert viewed and "RC-001" in viewed[0] and "42.3%" in viewed[0] and "[SCRIPTED]" in viewed[0]


def test_traceability_uses_prior_candidates_runs_and_tickets(tmp_path, monkeypatch, capsys):
    work = tmp_path / "governance_out"
    out = run_app(monkeypatch, capsys, work, [
        *NEW_DEMO_CANDIDATE, *SECOND_D2D3, "3",
        *APPROVE_LAST_BY_SARAH, "7",
        "3", "5", "", "RC-001", "", "",               # traceability for RC-002, prior RC-001, default finding
        "9", "4",
    ])
    assert "Broke `V_exfiltration` -> risk **R-02**" in out
    assert "Control **D2**" in out
    assert "Fix ticket **FIX-02**" in out
    assert "Re-tested as **RC-002**" in out
    assert "Approved by sarah -> audit entry #" in out
    assert "Final status: **Mitigated**" in out


def test_evidence_register_reports_a_measured_result_and_real_proof_file(tmp_path, monkeypatch, capsys):
    work = tmp_path / "governance_out"
    out = run_app(monkeypatch, capsys, work, [
        *NEW_DEMO_CANDIDATE, *SECOND_D2D3, "3",
        "3", "4", "", "", "",                          # evidence register for RC-002 (no prior)
        "8", "", "", "",                               # write all reports for RC-002
        "9", "4",
    ])
    # The measured text comes from evidence.control_result ("0/78 runs with ...");
    # the app's own earlier wording was "0 of 78". Accept both.
    measured = ("0 of 78", "0/78 runs")
    d2 = [ln for ln in out.splitlines() if ln.startswith("| D2 |")]
    assert d2 and "Not yet run" not in d2[0] and any(s in d2[0] for s in measured)
    md = (work / "RC-002" / "evidence_register.md").read_text()
    assert "Not yet run" not in md and any(s in md for s in measured)
    assert "results/RC-002/runs.csv" not in md                   # X-03: no phantom path


# --------------------------------------------------------------------------- #
# Item 7: B-31 / D-21 -- the app reads the corpus; it builds only if missing.
# --------------------------------------------------------------------------- #

def test_app_start_reads_the_corpus_and_never_rewrites_it(tmp_path, monkeypatch, capsys):
    from lab import corpus
    calls: list[int] = []
    monkeypatch.setattr(corpus, "build_and_write", lambda *a, **k: calls.append(1))
    stamp = lambda: [(p.read_bytes(), p.stat().st_mtime_ns) for p in (corpus.DEV_PATH, corpus.TEST_PATH)]
    before = stamp()
    out = run_app(monkeypatch, capsys, tmp_path / "governance_out", ["4"])
    assert calls == []
    assert stamp() == before
    assert "test split: 46 records" in out


def test_app_start_builds_the_corpus_only_when_missing(tmp_path, monkeypatch, capsys):
    from lab import corpus
    monkeypatch.setattr(corpus, "CORPUS_DIR", tmp_path / "corpus")
    monkeypatch.setattr(corpus, "DEV_PATH", tmp_path / "corpus" / "dev.jsonl")
    monkeypatch.setattr(corpus, "TEST_PATH", tmp_path / "corpus" / "test.jsonl")
    run_app(monkeypatch, capsys, tmp_path / "governance_out", ["4"])
    assert corpus.DEV_PATH.exists() and corpus.TEST_PATH.exists()
    assert len(corpus.read_jsonl(corpus.TEST_PATH)) == 46


# --------------------------------------------------------------------------- #
# Item 8: D-14 smoke tests -- the whole DEMO flow writes only under its
#         workspace; a LIVE run whose runner raises saves nothing.
# --------------------------------------------------------------------------- #

def _snapshot(root):
    """(size, mtime) of every file under ``root`` except caches and git metadata."""
    skip = {"__pycache__", ".pytest_cache", ".git", ".venv", "governance_out"}
    out = {}
    for p in root.rglob("*"):
        if p.is_file() and not any(part in skip for part in p.parts):
            st = p.stat()
            out[str(p)] = (st.st_size, st.st_mtime_ns)
    return out


def test_full_demo_flow_writes_only_under_its_workspace(tmp_path, monkeypatch, capsys):
    before = _snapshot(app.ROOT)
    work = tmp_path / "governance_out"
    out = run_app(monkeypatch, capsys, work, [
        *NEW_DEMO_CANDIDATE, "3",                        # new candidate RC-001 (DEMO)
        "2", "1", "", "",                                # verdict
        "2", "", "2", "sarah", "leaks data", "",         # decide: BLOCK
        "5", "",                                         # verify the real log
        "6", "",                                         # tamper demo on a copy
        "7", "4",                                        # back, quit
    ])
    assert _snapshot(app.ROOT) == before                 # nothing in the repo changed
    assert "VERDICT: FAIL" in out and "[SCRIPTED]" in out
    assert "Logged: BLOCK on RC-001" in out
    assert "Chain intact" in out and "TAMPERING DETECTED at entry #2" in out
    assert "bye." in out and "Traceback" not in out
    assert sorted(p.name for p in work.iterdir()) == [
        "RC-001", "audit_log.jsonl", "candidates", "candidates.json", "tamper_demo"]
    assert audit.verify_log(work / "audit_log.jsonl").ok
    assert json.loads((work / "candidates.json").read_text())["RC-001"]["decision"] == "BLOCK"


def test_live_runner_raising_connection_error_saves_nothing(tmp_path, monkeypatch, capsys):
    from lab import runner

    def dead(*args, **kwargs):
        raise ConnectionError("[Errno 61] Connection refused")

    monkeypatch.setattr(runner, "run_split", dead)
    monkeypatch.setattr(runner, "model_digest", lambda *a, **k: "")
    before = _snapshot(app.ROOT)
    work = tmp_path / "governance_out"
    out = run_app(monkeypatch, capsys, work, [*NEW_LIVE_CANDIDATE, "", "3", "4"])

    assert "Ollama unreachable or model missing" in out and "no candidate saved" in out
    assert "bye." in out and "Traceback" not in out
    assert not (work / "candidates.json").exists() and not (work / "RC-001").exists()
    assert not (work / "audit_log.jsonl").exists()
    assert _snapshot(app.ROOT) == before


# --------------------------------------------------------------------------- #
# Item 6 follow-up (lane A): the no-regression ceiling applies only when the
# last approved candidate is comparable (same test set, reps and mode).
# --------------------------------------------------------------------------- #

def test_no_regression_check_only_against_a_comparable_approval(tmp_path, monkeypatch, capsys):
    from lab import defences, demo, world
    rules = world.load_rules()

    def live(subset, reps, defs):
        traces = demo.demo_traces(subset, reps)
        return defences.replay(traces, rules, list(defs)) if defs else traces

    _stub_runner(monkeypatch, live)
    work = tmp_path / "governance_out"
    out = run_app(monkeypatch, capsys, work, [
        "1", "1", "", "1", "D2,D3", "2", "17", "", "3",   # RC-001 LIVE D2,D3 sample 17 -> PASS
        "2", "2", "", "1", "sarah", "", "7",              # approve RC-001
        "1", "1", "", "1", "", "1", "", "3",              # RC-002 SCRIPTED none
        "2", "1", "", "", "7",                            # verdict RC-002: LIVE approval not comparable
        "1", "1", "", "1", "D2,D3", "1", "", "3",         # RC-003 SCRIPTED D2,D3 -> PASS
        "2", "2", "", "1", "sarah", "", "7",              # approve RC-003
        "1", "1", "", "1", "", "1", "", "3",              # RC-004 SCRIPTED none
        "2", "1", "", "", "7", "4",                       # verdict RC-004: comparable -> regression FAIL
    ])
    recs = json.loads((work / "candidates.json").read_text())
    assert recs["RC-001"]["mode"] == "LIVE" and recs["RC-001"]["decision"] == "APPROVE"
    assert recs["RC-003"]["mode"] == "SCRIPTED" and recs["RC-003"]["decision"] == "APPROVE"
    assert recs["RC-001"]["test_set_sha256"] != recs["RC-002"]["test_set_sha256"]   # a 17-sample is not the full set
    assert recs["RC-001"]["reps"] == recs["RC-002"]["reps"] == 3

    v2 = out.split("Candidate RC-002")[1].split("[enter]")[0]
    assert "not comparable (different" in v2 and "mode)" in v2 and "RC-001" in v2
    assert "no_regression_vs_last_approved" not in v2

    v4 = out.split("Candidate RC-004")[1].split("[enter]")[0]
    assert "no_regression_vs_last_approved" in v4 and "RC-003" in v4
    assert any(ln.startswith("  FAIL") and "no_regression" in ln for ln in v4.splitlines())


# --------------------------------------------------------------------------- #
# C-09: candidates saved before fix 3 have no `runs` in their summary; the app
#       re-summarises them from <cid>/traces.json instead of gating 0/0.
# --------------------------------------------------------------------------- #

OLD_SUMMARY = {"asr": 0.4230769230769231, "asr_lo": 0.32, "asr_hi": 0.534, "attack_runs": 78,
               "attack_violations": 33, "benign_rate": 1.0, "benign_n": 60, "errors": 0}


def test_legacy_summary_is_resummarised_from_traces(tmp_path, monkeypatch, capsys):
    from lab import corpus, demo
    work = tmp_path / "governance_out"
    (work / "RC-001").mkdir(parents=True)
    (work / "RC-001" / "traces.json").write_text(
        json.dumps(demo.demo_traces(corpus.read_jsonl(corpus.TEST_PATH))))
    old = {"submitted_by": "tom", "defences": [], "mode": "SCRIPTED", "source": "old record",
           "identity_hash": "sha256:x", "summary": dict(OLD_SUMMARY), "decision": None,
           "created_at": "2026-10-17T00:00:00+00:00"}
    (work / "candidates.json").write_text(json.dumps({"RC-001": old, "RC-002": dict(old)}))

    out = run_app(monkeypatch, capsys, work, ["2", "1", "RC-001", "", "1", "RC-002", "", "7", "4"])

    recs = json.loads((work / "candidates.json").read_text())
    s1 = recs["RC-001"]["summary"]
    assert s1["runs"] == 138 and "attempted_rate" in s1 and s1["mode"] == "SCRIPTED"
    assert abs(s1["asr"] - OLD_SUMMARY["asr"]) < 1e-9
    v1 = out.split("Candidate RC-001")[1].split("[enter]")[0]
    assert "not measured (0 runs)" not in v1 and "re-summarised" in out

    # RC-002 has no traces: say so on screen, never crash, never show a clean 0/0.
    v2 = out.split("Candidate RC-002")[1].split("[enter]")[0]
    assert "no traces" in v2 and "not comparable" in v2
    assert "bye." in out and "Traceback" not in out


# --------------------------------------------------------------------------- #
# A-03 follow-up: the expected run count (tests x reps) is part of "comparable".
# --------------------------------------------------------------------------- #

def test_comparability_flags_test_set_reps_run_count_and_mode(tmp_path):
    s = app.GovState(tmp_path / "governance_out")
    base = {"mode": "LIVE", "reps": 3, "test_set_sha256": "sha256:a", "tests_run": 17, "summary": {}}
    s.candidates = {"RC-001": dict(base), "RC-002": dict(base, tests_run=20)}
    assert app._comparable(s, "RC-002", "RC-001") == ["run count"]
    s.candidates["RC-003"] = dict(base, mode="SCRIPTED", reps=1)
    assert app._comparable(s, "RC-003", "RC-001") == ["reps", "run count", "mode"]
    s.candidates["RC-004"] = dict(base, test_set_sha256="sha256:b")
    assert app._comparable(s, "RC-004", "RC-001") == ["test set"]
    assert app._comparable(s, "RC-001", "RC-001") == []
