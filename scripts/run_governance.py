#!/usr/bin/env python3
"""Run the governance + assurance pipeline as a standalone program.

This is the command-line twin of ``notebooks/release_gate_demo.ipynb``. It turns
the red team's numbers into a release decision and a permanent record, the same
way the notebook does, but without Jupyter: it prints a readable report and
writes every artifact to an output directory you can hand to a reviewer.

The whole flow, end to end:

  RC-001  the insecure agent (no defences)
    -> fingerprint it (an exact ID card of what was tested)
    -> gate it against policy.yaml                     -> FAIL
    -> record submit + verdict in the hash-chained audit log
    -> the engineer who built it is refused review; a reviewer BLOCKs it
    -> open the risk register (likelihood = the *measured* attack rate)
    -> draft fix tickets, build the AI inventory card and evidence register
  RC-002  the same agent with defence D2 enforced
    -> fingerprint it (the fingerprint changes, so the old decision is void)
    -> gate it again: leaks stop, unrequested P1 tickets still land -> FAIL
    -> the reviewer BLOCKs it again; only the risks D2 covers close
  RC-003  D2 + D3 enforced
    -> fingerprint, gate                               -> PASS
    -> record submit + verdict; the reviewer APPROVEs it
    -> re-test closes the remaining risk (an honest "Mitigated")
    -> stitch one finding start-to-finish (traceability)
    -> build the one-page manager summary
    -> prove the audit log is tamper-evident

Modes:
  DEMO (default)  offline and deterministic. Candidate runs come from traces
                  calibrated to the measured per-technique attack rates, so the
                  governance story is reproducible with no model and no network.
  LIVE (--live)   drives the real Ollama agent via ``lab.runner`` for the
                  candidate runs. Needs Ollama up with the model pulled. Each
                  candidate's run is saved to its own locked
                  ``results/runs/<run_id>/`` folder.
  SAVED (--run)   gates a saved, locked run folder as RC-001 (hash-verified,
                  model + digest from its manifest). RC-002 is a saved D2 run
                  given with ``--run-after``, or else a deterministic D2 replay
                  of the same traces (labelled as a replay).

Usage (from the repo root):
    .venv-pyrit/bin/python scripts/run_governance.py                 # demo
    .venv-pyrit/bin/python scripts/run_governance.py --live --sample 12
    .venv-pyrit/bin/python scripts/run_governance.py --out /tmp/gov_run   # default: governance_out/cli_demo
    .venv-pyrit/bin/python scripts/run_governance.py --run results/runs/<id> [--run-after results/runs/<id2>]

The governance and assurance logic itself lives in ``lab/governance`` and
``lab/assurance``; this script only orchestrates it and writes the outputs.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import pandas as pd  # noqa: E402

from lab import agent, corpus, defences, metrics, progress, run_folder, world  # noqa: E402
from lab.demo import demo_traces  # noqa: E402  (the one offline run generator; no local copy)
from lab.assurance import (  # noqa: E402
    evidence,
    inventory,
    load_control_map,
    risks,
    summary_page,
    thread,
)
from lab.governance import (  # noqa: E402
    audit,
    candidate,
    decision,
    gate,
    load_policy,
)

# --------------------------------------------------------------------------- #
# Candidate runs. DEMO traces come from ``lab.demo`` (deterministic, calibrated
# to the measured per-technique ASR); LIVE runs come from ``lab.runner`` and
# are saved to locked run folders; SAVED runs are loaded from such a folder.
# --------------------------------------------------------------------------- #
def live_subset(tests: list[dict], sample: int) -> list[dict]:
    """First ``sample`` attacks + first ``sample`` benign records, no duplicates."""
    attacks = [t for t in tests if t["kind"] == "attack"][:sample]
    benign = [t for t in tests if t["kind"] == "benign"][:sample]
    return attacks + benign


def live_test_set_text(tests: list[dict], sample: int) -> str:
    """The JSONL text of the records a --live run actually uses (A-04).

    The fingerprint hashes this, not the whole corpus/test.jsonl, so the ID
    card names the test set that was run; it equals the run folder's
    records.jsonl byte for byte.
    """
    return corpus.jsonl_text(live_subset(tests, sample))


def load_saved_run(path: str | Path, expect_defences: tuple[str, ...]) -> tuple[dict, list[dict], list[dict]]:
    """Load and hash-verify a locked run folder; check it is the kind of run expected."""
    manifest, records, traces = run_folder.load_run(path)
    got = sorted(manifest.get("defences") or [])
    if got != sorted(expect_defences):
        raise ValueError(
            f"run folder {path} was run with defences {got or 'none'}; "
            f"expected {sorted(expect_defences) or 'none'}"
        )
    if not traces:
        raise ValueError(f"run folder {path} holds no traces")
    return manifest, records, traces


def drain_live(
    sweep,
    records: list[dict],
    reps: int,
    rules: dict,
    tests_by_id: dict[str, dict],
    prog: progress.Progress | None = None,
) -> list[dict]:
    """Collect a ``runner.run_split`` sweep one run at a time, telling the progress log about each.

    ``run_start`` goes out before the model is called for a run, ``run_done`` after its trace is
    written, scored with ``metrics.build_runs_frame([trace], ...)`` and carrying the running counters.
    The order is records x reps, as ``run_split`` runs them. The sweep is closed in ``finally``, so
    the run folder is locked also when a Ctrl-C lands between two runs; a Ctrl-C inside a run
    reaches the generator's own ``finally`` first. Without ``prog`` this is ``list(sweep)``.
    """
    if prog is None:
        return list(sweep)
    schedule = [(test, rep) for test in records for rep in range(1, reps + 1)]
    n = len(schedule)
    stats = progress.RunningStats()
    traces: list[dict] = []
    try:
        for i, (test, rep) in enumerate(schedule, start=1):
            prog.emit_lazy("run_start", lambda: {
                "i": i, "n": n, "test_id": test["id"], "rep": rep,
                "kind": test["kind"], "technique": test.get("technique", "none"),
            })
            try:
                trace = next(sweep)
            except StopIteration:
                break
            traces.append(trace)
            prog.emit_lazy("run_done", lambda: progress.run_fields(
                i, n, trace, metrics.build_runs_frame([trace], rules, tests_by_id).iloc[0], stats))
        traces.extend(sweep)  # nothing more is expected; this lets the sweep finish (and lock) itself
    finally:
        sweep.close()
    return traces


def run_candidate(
    tests: list[dict],
    tests_by_id: dict[str, dict],
    rules: dict,
    *,
    live: bool,
    sample: int,
    reps: int,
    temperature: float,
    defence_codes: tuple[str, ...] = (),
    candidate_id: str = "RC-001",
    saved: tuple[dict, list[dict], list[dict]] | None = None,
    saved_dir: Path | None = None,
    replay_traces: list[dict] | None = None,
    out_dir: Path | None = None,
    prog: progress.Progress | None = None,
) -> tuple[pd.DataFrame, Path | None]:
    """Return ``(runs, proof_file)`` for one candidate.

    ``proof_file`` is the real ``runs.csv`` the evidence register points at: the
    locked run folder's file for LIVE and SAVED runs, otherwise a copy written
    under ``out_dir/<candidate_id>/``. ``prog`` (optional) receives a ``run_done``
    event for every run.
    """
    proof: Path | None = None
    if saved is not None:
        traces = saved[2]
        proof = Path(saved_dir) / "runs.csv" if saved_dir else None
    elif live:
        from lab import runner

        run_id = run_folder.make_run_id(agent.MODEL, candidate_id)
        records = live_subset(tests, sample)
        traces = drain_live(
            runner.run_split(
                records, reps=reps, temperature=temperature,
                defences=tuple(defence_codes), candidate=candidate_id, split="test", run_id=run_id,
            ),
            records, reps, rules, tests_by_id, prog,
        )
        proof = run_folder.RUNS_ROOT / run_id / "runs.csv"
    else:
        traces = replay_traces if replay_traces is not None else demo_traces(tests, reps=reps)
        if defence_codes:
            traces = defences.replay(traces, rules, list(defence_codes))
    runs = metrics.build_runs_frame(traces, rules, tests_by_id)
    if prog is not None and (saved is not None or not live):
        emit_scored_runs(prog, traces, runs)  # SCRIPTED / SAVED: the runs exist already, one run_done each
    if proof is None and out_dir is not None:
        proof = Path(out_dir) / candidate_id / "runs.csv"
        # Same writer as a run folder: sorted rows, summary.csv beside it (C-07).
        metrics.write_outputs(runs, proof.parent / "summary.csv", proof)
    return runs, proof


# --------------------------------------------------------------------------- #
# Small reporting helpers.
# --------------------------------------------------------------------------- #
class Report:
    """Collects the text report and the files written, for a tidy summary.

    Also tells the progress log (``prog``, optional) where the run is: a section
    with a ``stage`` opens that pipeline stage, a file written is an artifact.
    """

    def __init__(self, out_dir: Path, tag: str = "", note: str = "",
                 prog: progress.Progress | None = None) -> None:
        self.out_dir = out_dir
        self.written: list[Path] = []
        self.tag = tag      # SCRIPTED / LIVE / SAVED: stamped on every artifact written
        self.note = note    # one sentence on what the numbers are (scripted mode)
        self.prog = prog

    def section(self, title: str, stage: str | None = None) -> None:
        print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")
        if stage is not None:
            self.stage(stage, title)

    def stage(self, stage: str, title: str) -> None:
        """Open a pipeline stage in the progress log, closing the previous one. Prints nothing."""
        if self.prog is not None:
            self.prog.begin_stage(stage, title)

    def add_file(self, path: Path, kind: str) -> None:
        """Count a file this script wrote (not through :meth:`write`) as an artifact."""
        self.written.append(path)
        if self.prog is not None:
            self.prog.emit("artifact", name=Path(path).name, path=path, kind=kind)

    def say(self, text: str = "") -> None:
        print(text)

    def label(self, text: str, plain: bool = False) -> str:
        """Prefix ``text`` with the mode label so no artifact shows an unlabelled number."""
        if not self.tag:
            return text
        if plain:
            return f"[{self.tag}] {text}"
        head = f"**Mode: [{self.tag}]**" + (f" {self.note}" if self.note else "")
        return f"{head}\n\n{text}"

    def write(self, name: str, text: str) -> Path:
        path = self.out_dir / name
        path.write_text(self.label(text, plain=name.endswith(".txt")).rstrip() + "\n", encoding="utf-8")
        self.add_file(path, artifact_kind(name))
        return path


# Mode labels. SCRIPTED numbers come from lab.demo's calibrated traces, not a model.
SCRIPTED, LIVE, SAVED = "SCRIPTED", "LIVE", "SAVED"
SCRIPTED_NOTE = ("Scripted traces calibrated to the 24-run rehearsal; "
                 "benign 60/60 is scripted, not measured.")


def rotate_log(audit_path: Path) -> Path:
    """Rename an existing audit log to ``audit_log.<UTC timestamp>.jsonl``; never delete."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = audit_path.with_name(f"{audit_path.stem}.{stamp}{audit_path.suffix}")
    n = 1
    while target.exists():  # two runs in the same second
        n += 1
        target = audit_path.with_name(f"{audit_path.stem}.{stamp}-{n}{audit_path.suffix}")
    audit_path.rename(target)
    return target


def regression_baseline(last_approved: dict | None, current: dict) -> tuple[float | None, str | None]:
    """The ASR to apply the no-regression ceiling against, or why not (A-03).

    Like with like only: the previous approved candidate must have the same
    fingerprint ``test_set_sha256``, the same ``reps`` and the same mode.
    Otherwise returns ``(None, reason)`` and the caller skips the ceiling and
    says so; it never passes silently.
    """
    if not last_approved:
        return None, None
    differences = [k for k in ("test_set_sha256", "reps", "mode") if last_approved.get(k) != current.get(k)]
    if differences:
        return None, ("not comparable (different test set / reps / mode): "
                      + ", ".join(differences) + f" differ from {last_approved.get('candidate_id', 'last approved')}")
    return last_approved.get("asr"), None


def _attempted_ci(s: dict):
    """Wilson interval for the attempted figure (attempts incl. calls blocked by defences)."""
    return metrics.wilson(int(s.get("attempted_count") or 0), int(s.get("attempted_runs") or 0))


def _ci_text(p) -> str:
    return f"[{p.lo:.1%}, {p.hi:.1%}]"


def gate_report(cid: str, verdict, summary: dict) -> str:
    """The gate's report plus the attempted figure with its CI, shown but never a limit (A-08)."""
    lines = gate.report_text(cid, verdict).splitlines()
    att = _attempted_ci(summary)
    lines.insert(
        len(lines) - 1,
        f"  info  attempted_incl_blocked         {att.rate:>7.1%}  not a limit  "
        f"({att.count_str} 95% CI {_ci_text(att)})",
    )
    return "\n".join(lines)


def _fmt_summary(s: dict, tag: str = "") -> str:
    # ``errors_with_violation`` / ``attempted_*`` arrive with the error-scoring
    # change (fix 3); read with .get so this line works before and after it.
    if "attempted_runs" in s:
        att = _attempted_ci(s)
        attempted = f" · attempted {att.count_str} ({att.rate:.1%}) 95% CI {_ci_text(att)}"
    else:
        attempted = ""
    return (
        (f"[{tag}] " if tag else "")
        + f"ASR {s['asr']:.1%} ({s['attack_violations']}/{s['attack_runs']}) "
        f"95% CI [{s['asr_lo']:.1%}, {s['asr_hi']:.1%}]  |  "
        f"benign {s['benign_rate']:.0%} (n={s['benign_n']})  |  "
        f"errors {s['errors']} ({s.get('errors_with_violation', 0)} broke a rule, counted){attempted}"
    )


# --------------------------------------------------------------------------- #
# Progress log: the field builders for the events (the format is in lab/progress.py).
# Every hook goes through ``prog.emit_lazy`` / ``prog.guard`` so that a surprise
# while building an event can only switch the log off, never stop the run.
# --------------------------------------------------------------------------- #
ARTIFACT_KINDS = (
    ("gate_", "gate"), ("inventory_card", "inventory"), ("risk_register", "risk_register"),
    ("fix_tickets", "tickets"), ("evidence_register", "evidence"), ("traceability", "trace"),
    ("manager_summary", "summary"), ("audit_log", "audit_log"),
)

# The re-tests after RC-001: id, defences enforced, the flag that supplies a saved run.
# The reviewer's BLOCK reason is not kept here: it is built from the gate's checks (block_reason).
RETESTS = [
    ("RC-002", ("D2",), "--run-after"),
    ("RC-003", ("D2", "D3"), "--run-after-d3"),
]


def artifact_kind(name: str) -> str:
    """A short word for what kind of file ``name`` is, for the ``artifact`` event."""
    for prefix, kind in ARTIFACT_KINDS:
        if name.startswith(prefix):
            return kind
    return Path(name).suffix.lstrip(".") or "file"


def _check_value(value) -> str:
    """A check's value or limit as the gate report prints it (``n/a`` when it was not measured)."""
    if value is None:
        return "n/a"
    return f"{value:.1%}" if isinstance(value, float) else str(value)


def landed_classes(runs: pd.DataFrame) -> dict[str, int]:
    """Violation class -> number of attack runs where it landed (a violated attack run), sorted by class."""
    counts: dict[str, int] = {}
    if runs is None or runs.empty:
        return counts
    attacks = runs[(runs["kind"] == "attack") & runs["violated"].astype(bool)]
    for cell in attacks["violation_classes"]:
        if not isinstance(cell, str):  # a missing cell
            continue
        for cls in (c for c in cell.split(";") if c):
            counts[cls] = counts.get(cls, 0) + 1
    return dict(sorted(counts.items()))


def block_reason(verdict, runs: pd.DataFrame) -> str:
    """The scripted reviewer's BLOCK reason, built from the gate's own checks and the scored runs.

    It names every failed check with its value and limit, lists the violation classes that landed in
    the attack runs, and says so when ``min_attack_runs`` failed ("not enough proof"). There is no fixed
    text about a candidate: the data decides what is true (the old canned reasons contradicted it).
    """
    failed = verdict.failures()
    if not failed:
        return "No gate check failed."
    checks = "; ".join(f"{c.name} {_check_value(c.value)} (limit {_check_value(c.limit)})" for c in failed)
    landed = landed_classes(runs)
    classes = ", ".join(f"{cls} ({n} runs)" for cls, n in landed.items()) or "none"
    reason = f"Failed checks: {checks}. Violation classes that landed: {classes}."
    if any(c.name == "min_attack_runs" for c in failed):
        reason += " Not enough proof: run more attacks."
    return reason


def emit_scored_runs(prog: progress.Progress, traces: list[dict], runs: pd.DataFrame) -> None:
    """One ``run_done`` event per run that already exists (SCRIPTED, SAVED and replayed candidates)."""
    if prog.disabled:
        return
    with prog.guard():
        stats = progress.RunningStats()
        n = len(runs)
        for i, (trace, row) in enumerate(zip(traces, runs.to_dict("records")), start=1):
            prog.emit_lazy("run_done", lambda: progress.run_fields(i, n, trace, row, stats))


def audit_fields(record: dict) -> dict:
    """The ``audit`` event for one entry just appended to the log (``entry`` is the log's own number)."""
    return {
        "cand": record.get("candidate_id"),
        "entry": record.get("seq"),
        "event": record.get("event"),
        "by": record.get("by"),
        "reviewer": record.get("reviewer"),
        "verdict": record.get("verdict"),
        "decision": record.get("decision"),
        "reason": record.get("reason") or record.get("detail") or None,
        "scripted": bool(record.get("scripted")),
        "hash12": progress.short(record.get("hash")),
    }


def gate_fields(cid: str, verdict, report: str, path: Path) -> dict:
    return {
        "cand": cid,
        "verdict": verdict.status,
        "reasons": [c.name for c in verdict.failures()],
        "checks": [{"name": c.name, "ok": c.ok, "value": c.value, "limit": c.limit, "note": c.note}
                   for c in verdict.checks],
        "lines": report.splitlines(),
        "file": path,
    }


def headline_entry(cid: str, verdict, summary: dict) -> dict:
    """One candidate in ``pipeline_done.headline``; a rate with nothing behind it is null, not 0%."""
    measured_attacks, measured_benign = summary["attack_runs"] > 0, summary["benign_n"] > 0
    return {
        "id": cid,
        "verdict": verdict.status,
        "asr": summary["asr"] if measured_attacks else None,
        "asr_lo": summary["asr_lo"] if measured_attacks else None,
        "asr_hi": summary["asr_hi"] if measured_attacks else None,
        "attack_runs": summary["attack_runs"],
        "attack_violations": summary["attack_violations"],
        "benign_rate": summary["benign_rate"] if measured_benign else None,
        "benign_n": summary["benign_n"],
    }


RISK_FIELDS = ("id", "title", "violation_class", "successes", "runs", "rate", "ci_low", "ci_high",
               "likelihood", "impact", "control", "owner", "status", "note")


def risk_rows(register) -> list[dict]:
    return [{k: getattr(r, k) for k in RISK_FIELDS} for r in register]


def ticket_rows(tickets) -> list[dict]:
    return [{k: getattr(t, k) for k in ("id", "title", "control", "risk_id", "owner", "due", "status")}
            for t in tickets]


def evidence_rows(rows) -> list[dict]:
    return [{"control": e.control, "proof_needed": e.proof_needed,
             "proof_file": progress.display_path(e.proof_file, REPO), "result": e.result} for e in rows]


def controls_used(rows) -> list[str]:
    return sorted({r.control for r in rows if r.control})


def chain_fields(real, forged=None) -> dict:
    """The ``chain`` event: the real log's check, and (once the demo ran) the forged copy's."""
    return {
        "ok": real.ok,
        "entries": real.entries,
        "head12": progress.short(real.last_hash),
        "detail": real.detail,
        "tamper": None if forged is None else {"ok": forged.ok, "bad_seq": forged.bad_seq, "kind": forged.kind},
    }


def run_folder_id(saved, proof, live: bool) -> str | None:
    """The locked run folder of a LIVE or SAVED candidate; None for DEMO runs and replays (no folder)."""
    if saved is not None:
        return saved[0].get("run_id")
    if live and proof is not None:
        return Path(proof).parent.name
    return None


# --------------------------------------------------------------------------- #
# Main pipeline.
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace, argv: list[str] | None = None) -> int:
    """Run the pipeline and record it in ``<out>/progress.jsonl`` (see :mod:`lab.progress`).

    Emits ``pipeline_start`` first and ``pipeline_done`` always, also when the body
    raises or is interrupted; the exception then goes on unchanged, so Ctrl-C still
    locks a partial run folder. Writing the log can never fail the run.
    """
    prog = progress.Progress(args.out, repo=REPO, deferred=True)  # writes nothing until _run is past its checks
    started = time.monotonic()
    prog.emit_lazy("pipeline_start", lambda: {
        "mode": SAVED if args.run else (LIVE if args.live else SCRIPTED),
        "argv": progress.relativise_argv(list(argv or []), REPO),
        "out": progress.display_path(args.out, REPO),
        "platform": sys.platform,
        "git_commit": progress.git_commit(REPO),
    })
    status, code = "ok", 0
    try:
        code = _run(args, prog)
        status = "ok" if code == 0 else "error"
        return code
    except KeyboardInterrupt:
        status, code = "interrupted", 130
        raise
    except Exception as exc:
        status, code = "error", 1
        prog.error = f"{type(exc).__name__}: {progress.scrub(exc)}"
        raise
    finally:
        prog.end_stage("ok" if status == "ok" else "error")
        prog.emit("pipeline_done", status=status, exit_code=code, dur_s=round(time.monotonic() - started, 3),
                  headline={"cands": list(prog.headline)}, error=prog.error)
        prog.close()


def _run(args: argparse.Namespace, prog: progress.Progress) -> int:
    # Saved runs are loaded and hash-verified before anything is written.
    saved_before = None
    saved_retests: dict[str, tuple] = {"RC-002": (None, None), "RC-003": (None, None)}
    if args.run:
        try:
            saved_before = load_saved_run(args.run, ())
            for cid, codes, flag, path in (
                ("RC-002", ("D2",), "--run-after", args.run_after),
                ("RC-003", ("D2", "D3"), "--run-after-d3", args.run_after_d3),
            ):
                if path:
                    saved = load_saved_run(path, codes)
                    if saved[0].get("records_sha256") != saved_before[0].get("records_sha256"):
                        raise ValueError(f"--run and {flag} were not run on the same records")
                    for key in ("reps", "expected_runs"):  # same size of evidence, or not comparable (A-03)
                        if saved[0].get(key) != saved_before[0].get(key):
                            raise ValueError(
                                f"--run and {flag} differ in {key} ({saved_before[0].get(key)} vs "
                                f"{saved[0].get(key)}); the candidates would not be comparable"
                            )
                    saved_retests[cid] = (saved, path)
        except (run_folder.RunFolderError, ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            prog.error = progress.scrub(exc)
            return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    prog.activate()  # the run is past its checks: progress.jsonl starts here (an old one is rotated)
    tag = SAVED if saved_before is not None else (LIVE if args.live else SCRIPTED)
    rep = Report(out_dir, tag=tag, note=SCRIPTED_NOTE if tag == SCRIPTED else "", prog=prog)

    rules = world.load_rules()
    policy = load_policy()
    control_map = load_control_map()

    # The held-out TEST split is the only source of headline numbers.
    if saved_before is not None:
        tests = saved_before[1]  # the exact records the saved run used
    else:
        # Read-only check. The old build_and_write() rewrote corpus/*.jsonl on every
        # run, so two runs started together raced each other on the same files.
        try:
            corpus.check_matches_generator()
        except corpus.CorpusMismatch as exc:
            message = " ".join(str(exc).split())
            print(f"error: {message}", file=sys.stderr)
            prog.error = progress.scrub(message)
            return 2
        tests = corpus.read_jsonl(corpus.TEST_PATH)
    tests_by_id = {t["id"]: t for t in tests}
    n_attack = sum(t["kind"] == "attack" for t in tests)
    n_benign = sum(t["kind"] == "benign" for t in tests)

    if saved_before is not None:
        mode = f"SAVED RUN ({saved_before[0]['run_id']}, hash-verified)"
    elif args.live:
        mode = "LIVE (real model; each candidate saved to results/runs/<run_id>/)"
    else:
        mode = "DEMO (offline, deterministic)"
    rep.section("Agent Release Gate — Governance & Assurance", stage="setup")
    rep.say(f"Mode     : [{tag}] {mode}")
    if rep.note:
        rep.say(f"           {rep.note}")
    rep.say(f"Policy   : v{policy.get('policy_version')} (owner: {policy.get('policy_owner')})")
    rep.say(f"Test set : {len(tests)} records = {n_attack} attacks + {n_benign} benign")
    rep.say(f"Output   : {out_dir}")

    # Shared fingerprint inputs.
    prompt_text = agent.SYSTEM_PROMPT
    rules_text = world.RULES_PATH.read_text()
    tools = ["read_doc", "query_assets", "create_ticket"]

    if saved_before is not None:
        test_set_text = (Path(args.run) / run_folder.RECORDS).read_text()
        model_name = saved_before[0].get("model") or agent.MODEL
        model_digest = saved_before[0].get("model_digest") or ""
    elif args.live:
        from lab import runner

        test_set_text = live_test_set_text(tests, args.sample)  # the subset actually run
        subset_n = len(live_subset(tests, args.sample))
        rep.say(f"Live subset: {subset_n} records (first {args.sample} attacks + first {args.sample} benign); "
                "the fingerprint hashes these records")
        model_name = agent.MODEL
        model_digest = runner.model_digest()
    else:
        test_set_text = corpus.TEST_PATH.read_text()
        model_name = agent.MODEL
        model_digest = "sha256:845dbda0..."  # best-effort provenance for the demo

    reps = saved_before[0].get("reps") if saved_before is not None else args.reps
    with prog.guard():
        used = live_subset(tests, args.sample) if tag == LIVE else tests  # the records the candidates run
        expected = (saved_before[0].get("expected_runs") or len(saved_before[2])) if saved_before is not None \
            else len(used) * args.reps
        prog.emit(
            "meta",
            model=model_name,
            digest12="placeholder (DEMO)" if tag == SCRIPTED else progress.short(model_digest),
            policy_version=policy.get("policy_version"),
            test_set={
                "records": len(used),
                "attacks": sum(t["kind"] == "attack" for t in used),
                "benign": sum(t["kind"] == "benign" for t in used),
                "sha12": progress.short(candidate.sha256_text(test_set_text)),
            },
            reps=reps,
            sample=args.sample if tag == LIVE else None,
            candidates=[{"id": cid, "defences": list(codes), "expected_runs": expected}
                        for cid, codes in [("RC-001", ())] + [(c, d) for c, d, _flag in RETESTS]],
        )

    # Fresh audit log unless asked to keep it. An existing log is never deleted:
    # it is rotated to audit_log.<UTC timestamp>.jsonl next to the new one.
    audit_path = out_dir / "audit_log.jsonl"
    if audit_path.exists() and not args.keep_log:
        rotated = rotate_log(audit_path)
        rep.say(f"Audit log: existing {audit_path.name} kept as {rotated.name}; starting a fresh log")

    def log(event: dict) -> dict:
        """Append to the audit log, marking what this script wrote on a person's behalf.

        In SCRIPTED mode every event is scripted. In LIVE / SAVED mode the runs
        are measured but the reviewer's decisions (and the refused self-review)
        are still scripted by the CLI, so those carry the marker too (C-14).
        """
        if tag == SCRIPTED or event.get("event") in ("decision", "decision_refused"):
            event = {**event, "scripted": True}
        record = audit.append(audit_path, event)
        prog.emit_lazy("audit", lambda: audit_fields(record))
        return record

    def gated(cid: str, verdict, summary: dict, report: str, path: Path) -> None:
        """Tell the progress log the gate's verdict for ``cid`` (and keep it for the final headline)."""
        prog.emit_lazy("gate", lambda: gate_fields(cid, verdict, report, path))
        with prog.guard():
            prog.headline.append(headline_entry(cid, verdict, summary))

    # ----------------------------------------------------------------------- #
    # RC-001: the insecure agent.
    # ----------------------------------------------------------------------- #
    rep.section("1. RC-001 — the insecure agent (no defences)", stage="RC-001:runs")
    runs_before, proof_before = run_candidate(
        tests,
        tests_by_id,
        rules,
        live=args.live,
        sample=args.sample,
        reps=args.reps,
        temperature=args.temperature,
        defence_codes=(),
        candidate_id="RC-001",
        saved=saved_before,
        saved_dir=Path(args.run) if args.run else None,
        out_dir=out_dir,
        prog=prog,
    )
    summary_before = gate.summarise(runs_before)
    rep.say(_fmt_summary(summary_before, tag))
    rep.say(f"Runs file: {proof_before}")
    prog.emit_lazy("runs_done", lambda: {
        "run_id": run_folder_id(saved_before, proof_before, args.live),
        "runs_csv": proof_before,
        "summary": summary_before,
    })

    fp_001 = candidate.fingerprint(
        "RC-001",
        submitted_by=args.engineer,
        model=model_name,
        model_digest=model_digest,
        system_prompt=prompt_text,
        tools=tools,
        defences=[],
        rules_text=rules_text,
        test_set_text=test_set_text,
    )
    candidate.save(fp_001, out_dir)
    rep.add_file(out_dir / "RC-001.json", "fingerprint")
    rep.say(f"\nFingerprint RC-001 identity: {fp_001.identity_hash()}")
    prog.emit_lazy("fingerprint", lambda: {"identity12": progress.short(fp_001.identity_hash()),
                                           "changed_vs_prev": None})

    rep.section("2. Gate verdict for RC-001", stage="RC-001:gate")
    verdict_001 = gate.evaluate(summary_before, policy)
    report_001 = gate_report("RC-001", verdict_001, summary_before)
    rep.say(report_001)
    gate_file_001 = rep.write("gate_RC-001.txt", report_001)
    gated("RC-001", verdict_001, summary_before, report_001, gate_file_001)

    rep.section("3. Audit log + human decision for RC-001", stage="RC-001:decide")
    log({"event": "submit", "candidate_id": "RC-001", "by": args.engineer, "defences": []})
    log({"event": "verdict", "candidate_id": "RC-001", "verdict": verdict_001.status})

    # The engineer who submitted cannot review their own work.
    try:
        decision.decide(
            candidate_id="RC-001", submitted_by=args.engineer, verdict_passed=verdict_001.passed,
            policy=policy, reviewer=args.engineer, action="APPROVE",
        )
    except decision.DecisionError as e:
        rep.say(f"Self-review refused: {e}")
        log({  # the refusal is part of the record, as in the menu app
            "event": "decision_refused", "candidate_id": "RC-001", "reviewer": args.engineer,
            "action": "APPROVE", "detail": str(e),
        })

    # The reviewer blocks it with a reason (a FAIL cannot be approved).
    reason_001 = block_reason(verdict_001, runs_before)
    if verdict_001.passed:  # the script blocks the baseline anyway, so that the re-tests run; say so
        reason_001 += " The CLI script blocks the baseline so that the re-tests run."
    ev_block = decision.decide(
        candidate_id="RC-001", submitted_by=args.engineer, verdict_passed=verdict_001.passed,
        policy=policy, reviewer=args.reviewer, action="BLOCK", reason=reason_001,
    )
    log(ev_block)
    rep.say(f"Logged decision: {ev_block['decision']} by {ev_block['reviewer']} — \"{ev_block['reason']}\"")

    # ----------------------------------------------------------------------- #
    # Assurance for RC-001: inventory, risks, fixes, evidence.
    # ----------------------------------------------------------------------- #
    rep.section("4. Assurance — inventory, risk register, fix tickets", stage="RC-001:assure")
    card = inventory.build_card(fp_001, policy)
    card_md = inventory.to_markdown(card)
    rep.write("inventory_card.md", card_md)
    rep.say(card_md)

    register_before = risks.build_register(runs_before, policy, control_map)
    reg_before_md = risks.to_markdown(register_before)
    rep.write("risk_register_before.md", reg_before_md)
    prog.emit_lazy("risks", lambda: {"rows": risk_rows(register_before),
                                     "controls_used": controls_used(register_before)})
    rep.say("\n" + reg_before_md)

    tickets = risks.fix_tickets(
        register_before,
        owners={"R-02": args.engineer, "R-01": args.engineer},
        due=args.due,
    )
    ticket_lines = [
        f"{t.id}: {t.title}  (owner {t.owner}, due {t.due}, risk {t.risk_id})" for t in tickets
    ]
    rep.write("fix_tickets.md", "**Fix tickets**\n\n" + "\n".join(f"- {line}" for line in ticket_lines))
    prog.emit_lazy("tickets", lambda: {"rows": ticket_rows(tickets), "controls_used": controls_used(tickets)})
    rep.say("\nFix tickets:")
    for line in ticket_lines:
        rep.say(f"  {line}")

    # ----------------------------------------------------------------------- #
    # Re-tests: RC-002 (D2) then RC-003 (D2 + D3). Each one is fingerprinted,
    # gated, decided by the reviewer, and closes only the risks its own
    # controls cover -- D2 alone never proves D3.
    # ----------------------------------------------------------------------- #
    retests = RETESTS
    summaries = {"RC-001": summary_before}
    verdicts = {"RC-001": verdict_001}
    proofs = {"RC-001": proof_before}
    prev_cid, prev_fp, prev_summary, prev_register = "RC-001", fp_001, summary_before, register_before
    register_final, ev_md_final, last_cid = register_before, "", "RC-001"
    last_approved: dict | None = None  # asr + what it was measured on, for the regression check
    section = 5
    for cid, codes, flag in retests:
        label = "+".join(codes)
        rep.section(f"{section}. {cid} — defences {label} enforced (re-test)", stage=f"{cid}:runs")
        fp = candidate.fingerprint(
            cid,
            submitted_by=args.engineer,
            model=fp_001.model,
            model_digest=fp_001.model_digest,
            system_prompt=prompt_text,
            tools=tools,
            defences=list(codes),
            rules_text=rules_text,
            test_set_text=test_set_text,
        )
        candidate.save(fp, out_dir)
        rep.add_file(out_dir / f"{cid}.json", "fingerprint")
        rep.say(f"Fingerprint changed vs {prev_cid}: {prev_fp.identity_hash() != fp.identity_hash()}")
        rep.say(f"(so {prev_cid}'s decision does not carry over — the re-test rule forces a fresh review)")

        saved, saved_path = saved_retests[cid]
        runs, proof = run_candidate(
            tests,
            tests_by_id,
            rules,
            live=args.live,
            sample=args.sample,
            reps=args.reps,
            temperature=args.temperature,
            defence_codes=codes,
            candidate_id=cid,
            saved=saved,
            saved_dir=Path(saved_path) if saved_path else None,
            # A saved RC-001 without a saved re-test run: replay the defences over its traces.
            replay_traces=saved_before[2] if (saved_before is not None and saved is None) else None,
            out_dir=out_dir,
            prog=prog,
        )
        summary = gate.summarise(runs)
        prog.emit_lazy("runs_done", lambda: {
            "run_id": run_folder_id(saved, proof, args.live),
            "runs_csv": proof,
            "summary": summary,
        })
        # Same order as RC-001 in the log (runs, then fingerprint), though the file was written first.
        prog.emit_lazy("fingerprint", lambda: {
            "identity12": progress.short(fp.identity_hash()),
            "changed_vs_prev": prev_fp.identity_hash() != fp.identity_hash(),
        })
        rep.say("\n" + _fmt_summary(summary, tag))
        if saved_before is not None and saved is None:
            rep.say(f"({cid} is a deterministic {label} replay of the saved RC-001 traces, not a measured "
                    f"run; pass {flag} <folder> to gate a saved {label} run)")
        rep.say(f"Runs file: {proof}")
        rep.say(
            f"\nAttack success: {prev_cid} {prev_summary['asr']:.1%} -> {cid} {summary['asr']:.1%}"
            f"   |   benign: {prev_summary['benign_rate']:.0%} -> {summary['benign_rate']:.0%}"
        )

        section += 1
        rep.section(f"{section}. Gate verdict for {cid}, then the reviewer decides", stage=f"{cid}:gate")
        current = {"candidate_id": cid, "test_set_sha256": fp.test_set_sha256, "reps": reps, "mode": tag}
        baseline_asr, why_not = regression_baseline(last_approved, current)
        if why_not:
            rep.say(f"No-regression check skipped: {why_not}")
        elif baseline_asr is None:
            rep.say("No-regression check: no approved candidate to compare against yet")
        else:
            rep.say(f"No-regression check against {last_approved['candidate_id']} "
                    f"(ASR {baseline_asr:.1%}, same test set, reps and mode)")
        verdict = gate.evaluate(summary, policy, last_approved_asr=baseline_asr)
        report = gate_report(cid, verdict, summary)
        rep.say(report)
        gate_file = rep.write(f"gate_{cid}.txt", report)
        gated(cid, verdict, summary, report, gate_file)

        # The gate and the reviewer's decision share one printed section; the log gives the decision its own stage.
        rep.stage(f"{cid}:decide", f"Reviewer decision for {cid}")
        log({"event": "submit", "candidate_id": cid, "by": args.engineer, "defences": list(codes)})
        log({"event": "verdict", "candidate_id": cid, "verdict": verdict.status})
        if verdict.passed:
            ev = decision.decide(
                candidate_id=cid, submitted_by=args.engineer, verdict_passed=True,
                policy=policy, reviewer=args.reviewer, action="APPROVE",
            )
            last_approved = {**current, "asr": summary["asr"]}
            rep.say(f"\nLogged decision: {ev['decision']} {cid} by {ev['reviewer']}")
        else:
            ev = decision.decide(
                candidate_id=cid, submitted_by=args.engineer, verdict_passed=False,
                policy=policy, reviewer=args.reviewer, action="BLOCK", reason=block_reason(verdict, runs),
            )
            rep.say(f"\nLogged decision: {ev['decision']} {cid} by {ev['reviewer']} — \"{ev['reason']}\"")
        log(ev)

        section += 1
        rep.section(f"{section}. Assurance after {cid} — risk register, evidence register", stage=f"{cid}:assure")
        prior = risks.prior_from(prev_register)  # carries whether each risk had landed
        register = risks.build_register(runs, policy, control_map, prior=prior, defences=codes)
        reg_md = risks.to_markdown(register)
        rep.write(f"risk_register_{cid}.md", reg_md)
        prog.emit_lazy("risks", lambda: {"rows": risk_rows(register), "controls_used": controls_used(register)})
        rep.say(reg_md)

        measured = evidence.control_results(runs, [r.control for r in register if r.control], defences=codes)
        ev_rows = evidence.build_register(register, retest_candidate=cid, results=measured, proof_file=str(proof))
        ev_md = evidence.to_markdown(ev_rows)
        rep.write(f"evidence_register_{cid}.md", ev_md)
        prog.emit_lazy("evidence", lambda: {"rows": evidence_rows(ev_rows), "controls_used": controls_used(ev_rows)})
        rep.say("\nEvidence register:\n" + ev_md)

        summaries[cid], verdicts[cid], proofs[cid] = summary, verdict, proof
        prev_cid, prev_fp, prev_summary, prev_register = cid, fp, summary, register
        register_final, ev_md_final, last_cid = register, ev_md, cid
        section += 1

    # The final state keeps the file names earlier readers expect.
    rep.write("risk_register_after.md", risks.to_markdown(register_final))
    rep.write("evidence_register.md", ev_md_final)
    summary_final = summaries[last_cid]

    # ----------------------------------------------------------------------- #
    # Traceability: one finding, start to finish.
    # ----------------------------------------------------------------------- #
    rep.section(f"{section}. Traceability — one finding, start to finish", stage="trace")
    leaked = runs_before[
        (runs_before["kind"] == "attack")
        & runs_before["violation_classes"].str.contains("V_exfiltration", na=False)
    ]
    finding_id = leaked.iloc[0]["test_id"] if not leaked.empty else "ATK-0021"
    tr = thread.build_thread(
        finding_id, runs_before, register_final, tickets, audit.read_log(audit_path),
        retest_candidate=last_cid,
    )
    tr_md = thread.to_markdown(tr)
    rep.write("traceability.md", tr_md)
    prog.emit_lazy("trace", lambda: {"finding": finding_id, "steps_n": len(tr.steps)})
    rep.say(tr_md)

    # ----------------------------------------------------------------------- #
    # Manager summary.
    # ----------------------------------------------------------------------- #
    section += 1
    rep.section(f"{section}. Manager summary", stage="summary")
    s = summary_page.build_summary(
        register_final,
        audit.read_log(audit_path),
        current_asr=summary_final["asr"],
        summaries={cid: {**summ, "mode": tag} for cid, summ in summaries.items()},
        current_candidate=last_cid,
        log_head=audit.head(audit_path),
    )
    s_md = summary_page.to_markdown(s)
    if rep.note:
        s_md += f"\n- **Numbers:** {rep.note}"
    rep.write("manager_summary.md", s_md)
    prog.emit_lazy("summary", lambda: {
        "bullets": [ln[2:].replace("**", "") for ln in s_md.splitlines() if ln.startswith("- ")],
    })
    rep.say(rep.label(s_md))

    # ----------------------------------------------------------------------- #
    # Tamper-evidence: verify the real log, then prove a forged copy is caught.
    # The real audit_log.jsonl is left intact and verifiable.
    # ----------------------------------------------------------------------- #
    section += 1
    rep.section(f"{section}. The audit log is tamper-evident", stage="chain")
    real = audit.verify_log(audit_path)
    rep.say(f"Real log      : {real}")
    prog.emit_lazy("chain", lambda: chain_fields(real))

    if args.tamper_demo:
        tampered = out_dir / "audit_log_tampered_demo.jsonl"
        lines = audit_path.read_text().splitlines()
        # Forge the reviewer's BLOCK of RC-001 into an approval.
        idx = next((i for i, line in enumerate(lines)
                    if json.loads(line).get("decision") == "BLOCK"), None)
        if idx is not None:
            rec = json.loads(lines[idx])
            rec["decision"] = "APPROVE"  # forge an approval
            lines[idx] = json.dumps(rec, sort_keys=True)
            tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")
            rep.add_file(tampered, "tamper_demo")
            check = audit.verify_log(tampered)
            rep.say(f"Forged copy   : {check}")
            if not check.ok:
                rep.say(f">>> TAMPERING DETECTED at entry #{check.bad_seq} (real log untouched) <<<")
            prog.emit_lazy("chain", lambda: chain_fields(real, check))

    # ----------------------------------------------------------------------- #
    # Final summary of artifacts + the headline verdicts.
    # ----------------------------------------------------------------------- #
    rep.section("Done — artifacts written", stage="done")
    for p in rep.written:
        rep.say(f"  {p}")
    rep.say(f"  {audit_path}")
    for cid, proof in proofs.items():
        rep.say(f"  {proof}  ({cid} runs)")

    rep.say("")
    for cid, verdict in verdicts.items():
        n_fail = len(verdict.failures())
        rep.say(f"{cid} verdict: {verdict.status}" + (f"  ({n_fail} reason(s))" if n_fail else ""))
    asr_chain = " -> ".join(
        [f"{summary_before['asr']:.1%}"]
        + [f"{summaries[cid]['asr']:.1%} ({'+'.join(codes)})" for cid, codes, _ in retests]
    )
    benign_chain = " -> ".join(f"{summaries[cid]['benign_rate']:.0%}" for cid in summaries)
    rep.say(f"[{tag}] Headline: attack success {asr_chain}, benign {benign_chain}")
    attempted_chain = " -> ".join(
        [f"{_attempted_ci(summary_before).rate:.1%} {_ci_text(_attempted_ci(summary_before))}"]
        + [f"{_attempted_ci(summaries[cid]).rate:.1%} {_ci_text(_attempted_ci(summaries[cid]))} ({'+'.join(codes)})"
           for cid, codes, _ in retests]
    )
    rep.say(f"[{tag}] Attempted (incl. blocked): {attempted_chain}")
    if rep.note:
        rep.say(f"[{tag}] {rep.note}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Run the governance + release-gate and assurance pipeline end to end "
            "(the command-line twin of notebooks/release_gate_demo.ipynb). Prints a "
            "report and writes every artifact to --out."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--live", action="store_true",
                   help="drive the real Ollama agent for candidate runs (needs Ollama + model)")
    p.add_argument("--sample", type=int, default=12,
                   help="LIVE only: first N tests per split for a quick run")
    p.add_argument("--reps", type=int, default=3, help="repetitions per test")
    p.add_argument("--temperature", type=float, default=0.2, help="LIVE only: model temperature")
    p.add_argument("--out", default=str(REPO / "governance_out" / "cli_demo"),
                   help="directory for the audit log and artifact files (an existing audit log "
                        "there is rotated, never deleted; the menu app's governance_out/ is not touched)")
    p.add_argument("--engineer", default="tom", help="candidate submitter (an 'engineers' role in policy.yaml)")
    p.add_argument("--reviewer", default="sarah", help="decision reviewer (a 'reviewers' role in policy.yaml)")
    p.add_argument("--due", default="2026-10-24", help="due date stamped on fix tickets (YYYY-MM-DD)")
    p.add_argument("--keep-log", action="store_true",
                   help="append to an existing audit log instead of starting fresh")
    p.add_argument("--no-tamper-demo", dest="tamper_demo", action="store_false",
                   default=argparse.SUPPRESS,  # keeps the formatter from printing "(default: True)"
                   help="skip the tamper-evidence demonstration (it runs unless this flag is given)")
    p.add_argument("--run", default=None, metavar="RUN_DIR",
                   help="gate a saved, locked run folder (results/runs/<id>) as RC-001 instead of DEMO/LIVE")
    p.add_argument("--run-after", default=None, metavar="RUN_DIR",
                   help="with --run: a saved D2 run folder to use as RC-002; without it RC-002 is a "
                        "D2 replay of --run")
    p.add_argument("--run-after-d3", default=None, metavar="RUN_DIR",
                   help="with --run: a saved D2+D3 run folder to use as RC-003; without it RC-003 is a "
                        "D2+D3 replay of --run")
    return p


def _stop(signum, frame):
    """SIGTERM / SIGBREAK handler: stop exactly like Ctrl-C, so a partial run folder is still locked."""
    raise KeyboardInterrupt


def _stop_on_sigterm() -> list[tuple[int, object]]:
    """Make SIGTERM (and SIGBREAK, which only Windows has) raise KeyboardInterrupt; returns what to put back."""
    saved: list[tuple[int, object]] = []
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            saved.append((sig, signal.signal(sig, _stop)))
        except (ValueError, OSError):  # not the main thread, or no such signal here
            pass
    return saved


def _line_buffer_stdout():
    """Flush stdout at every newline, so a viewer reading the pipe sees each line at once. Returns the undo."""
    stream = sys.stdout
    if not hasattr(stream, "reconfigure"):  # a notebook's stream, a StringIO ...
        return lambda: None
    was = getattr(stream, "line_buffering", False)
    try:
        stream.reconfigure(line_buffering=True)
    except (OSError, ValueError):  # closed or detached
        return lambda: None

    def undo() -> None:
        try:
            stream.reconfigure(line_buffering=was)
        except (OSError, ValueError):
            pass

    return undo


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not hasattr(args, "tamper_demo"):  # SUPPRESS default: the flag's absence means "run it"
        args.tamper_demo = True
    try:
        date.fromisoformat(args.due)
    except ValueError:
        print(f"error: --due {args.due!r} is not a YYYY-MM-DD date", file=sys.stderr)
        return 2

    # The reviewer must hold the reviewer role and must not be the engineer who submits.
    policy = load_policy()
    reviewers = {str(r).strip().casefold() for r in (policy.get("roles") or {}).get("reviewers") or []}
    reviewer = args.reviewer.strip().casefold()
    if reviewer not in reviewers:
        print(f"error: --reviewer {args.reviewer!r} is not in policy roles.reviewers "
              f"{sorted(reviewers)}", file=sys.stderr)
        return 2
    if reviewer == args.engineer.strip().casefold():
        print(f"error: --reviewer {args.reviewer!r} is the same person as --engineer; "
              "a submitter never reviews their own candidate", file=sys.stderr)
        return 2

    if args.run and args.live:
        print("error: --run gates a saved run; it cannot be combined with --live", file=sys.stderr)
        return 2
    if (args.run_after or args.run_after_d3) and not args.run:
        print("error: --run-after / --run-after-d3 need --run", file=sys.stderr)
        return 2

    undo_buffering = _line_buffer_stdout()
    saved_handlers = _stop_on_sigterm()
    try:
        return run(args, list(sys.argv[1:] if argv is None else argv))
    finally:  # an in-process caller (a test, a notebook) gets its stream and signal handlers back
        for sig, previous in saved_handlers:
            if previous is not None:
                try:
                    signal.signal(sig, previous)
                except (ValueError, OSError, TypeError):
                    pass
        undo_buffering()


if __name__ == "__main__":
    raise SystemExit(main())
