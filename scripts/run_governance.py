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
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import pandas as pd  # noqa: E402

from lab import agent, corpus, defences, metrics, run_folder, world  # noqa: E402
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
) -> tuple[pd.DataFrame, Path | None]:
    """Return ``(runs, proof_file)`` for one candidate.

    ``proof_file`` is the real ``runs.csv`` the evidence register points at: the
    locked run folder's file for LIVE and SAVED runs, otherwise a copy written
    under ``out_dir/<candidate_id>/``.
    """
    proof: Path | None = None
    if saved is not None:
        traces = saved[2]
        proof = Path(saved_dir) / "runs.csv" if saved_dir else None
    elif live:
        from lab import runner

        run_id = run_folder.make_run_id(agent.MODEL, candidate_id)
        traces = list(
            runner.run_split(
                live_subset(tests, sample), reps=reps, temperature=temperature,
                defences=tuple(defence_codes), candidate=candidate_id, split="test", run_id=run_id,
            )
        )
        proof = run_folder.RUNS_ROOT / run_id / "runs.csv"
    else:
        traces = replay_traces if replay_traces is not None else demo_traces(tests, reps=reps)
        if defence_codes:
            traces = defences.replay(traces, rules, list(defence_codes))
    runs = metrics.build_runs_frame(traces, rules, tests_by_id)
    if proof is None and out_dir is not None:
        proof = Path(out_dir) / candidate_id / "runs.csv"
        proof.parent.mkdir(parents=True, exist_ok=True)
        runs.to_csv(proof, index=False)
    return runs, proof


# --------------------------------------------------------------------------- #
# Small reporting helpers.
# --------------------------------------------------------------------------- #
class Report:
    """Collects the text report and the files written, for a tidy summary."""

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.written: list[Path] = []

    def section(self, title: str) -> None:
        print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")

    def say(self, text: str = "") -> None:
        print(text)

    def write(self, name: str, text: str) -> Path:
        path = self.out_dir / name
        path.write_text(text.rstrip() + "\n", encoding="utf-8")
        self.written.append(path)
        return path


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


def _fmt_summary(s: dict) -> str:
    # ``errors_with_violation`` / ``attempted_*`` arrive with the error-scoring
    # change (fix 3); read with .get so this line works before and after it.
    attempted = (
        f" · attempted {s.get('attempted_count')}/{s.get('attempted_runs')}"
        if "attempted_runs" in s else ""
    )
    return (
        f"ASR {s['asr']:.1%} ({s['attack_violations']}/{s['attack_runs']}) "
        f"95% CI [{s['asr_lo']:.1%}, {s['asr_hi']:.1%}]  |  "
        f"benign {s['benign_rate']:.0%} (n={s['benign_n']})  |  "
        f"errors {s['errors']} ({s.get('errors_with_violation', 0)} broke a rule, counted){attempted}"
    )


# --------------------------------------------------------------------------- #
# Main pipeline.
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> int:
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
                    saved_retests[cid] = (saved, path)
        except (run_folder.RunFolderError, ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rep = Report(out_dir)

    rules = world.load_rules()
    policy = load_policy()
    control_map = load_control_map()

    # The held-out TEST split is the only source of headline numbers.
    if saved_before is not None:
        tests = saved_before[1]  # the exact records the saved run used
    else:
        corpus.build_and_write()
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
    rep.section("Agent Release Gate — Governance & Assurance")
    rep.say(f"Mode     : {mode}")
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

        test_set_text = corpus.TEST_PATH.read_text()
        model_name = agent.MODEL
        model_digest = runner.model_digest()
    else:
        test_set_text = corpus.TEST_PATH.read_text()
        model_name = agent.MODEL
        model_digest = "sha256:845dbda0..."  # best-effort provenance for the demo

    # Fresh audit log unless asked to keep it. An existing log is never deleted:
    # it is rotated to audit_log.<UTC timestamp>.jsonl next to the new one.
    audit_path = out_dir / "audit_log.jsonl"
    if audit_path.exists() and not args.keep_log:
        rotated = rotate_log(audit_path)
        rep.say(f"Audit log: existing {audit_path.name} kept as {rotated.name}; starting a fresh log")

    # ----------------------------------------------------------------------- #
    # RC-001: the insecure agent.
    # ----------------------------------------------------------------------- #
    rep.section("1. RC-001 — the insecure agent (no defences)")
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
    )
    summary_before = gate.summarise(runs_before)
    rep.say(_fmt_summary(summary_before))
    rep.say(f"Runs file: {proof_before}")

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
    rep.written.append(out_dir / "RC-001.json")
    rep.say(f"\nFingerprint RC-001 identity: {fp_001.identity_hash()}")

    rep.section("2. Gate verdict for RC-001")
    verdict_001 = gate.evaluate(summary_before, policy)
    report_001 = gate.report_text("RC-001", verdict_001)
    rep.say(report_001)
    rep.write("gate_RC-001.txt", report_001)

    rep.section("3. Audit log + human decision for RC-001")
    audit.append(audit_path, {"event": "submit", "candidate_id": "RC-001", "by": args.engineer, "defences": []})
    audit.append(audit_path, {"event": "verdict", "candidate_id": "RC-001", "verdict": verdict_001.status})

    # The engineer who submitted cannot review their own work.
    try:
        decision.decide(
            candidate_id="RC-001", submitted_by=args.engineer, verdict_passed=verdict_001.passed,
            policy=policy, reviewer=args.engineer, action="APPROVE",
        )
    except decision.DecisionError as e:
        rep.say(f"Self-review refused: {e}")
        audit.append(audit_path, {  # the refusal is part of the record, as in the menu app
            "event": "decision_refused", "candidate_id": "RC-001", "reviewer": args.engineer,
            "action": "APPROVE", "detail": str(e),
        })

    # The reviewer blocks it with a reason (a FAIL cannot be approved).
    ev_block = decision.decide(
        candidate_id="RC-001", submitted_by=args.engineer, verdict_passed=verdict_001.passed,
        policy=policy, reviewer=args.reviewer, action="BLOCK",
        reason="Leaks data in a large share of attacks. Build D2 first.",
    )
    audit.append(audit_path, ev_block)
    rep.say(f"Logged decision: {ev_block['decision']} by {ev_block['reviewer']} — \"{ev_block['reason']}\"")

    # ----------------------------------------------------------------------- #
    # Assurance for RC-001: inventory, risks, fixes, evidence.
    # ----------------------------------------------------------------------- #
    rep.section("4. Assurance — inventory, risk register, fix tickets")
    card = inventory.build_card(fp_001, policy)
    card_md = inventory.to_markdown(card)
    rep.write("inventory_card.md", card_md)
    rep.say(card_md)

    register_before = risks.build_register(runs_before, policy, control_map)
    reg_before_md = risks.to_markdown(register_before)
    rep.write("risk_register_before.md", reg_before_md)
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
    rep.say("\nFix tickets:")
    for line in ticket_lines:
        rep.say(f"  {line}")

    # ----------------------------------------------------------------------- #
    # Re-tests: RC-002 (D2) then RC-003 (D2 + D3). Each one is fingerprinted,
    # gated, decided by the reviewer, and closes only the risks its own
    # controls cover -- D2 alone never proves D3.
    # ----------------------------------------------------------------------- #
    retests = [
        ("RC-002", ("D2",), "--run-after",
         "D2 stops the leaks, but unrequested P1 tickets still land. Add D3."),
        ("RC-003", ("D2", "D3"), "--run-after-d3",
         "Still above the policy limit with D2 + D3; needs more work."),
    ]
    summaries = {"RC-001": summary_before}
    verdicts = {"RC-001": verdict_001}
    proofs = {"RC-001": proof_before}
    prev_cid, prev_fp, prev_summary, prev_register = "RC-001", fp_001, summary_before, register_before
    register_final, ev_md_final, last_cid = register_before, "", "RC-001"
    last_approved_asr: float | None = None
    section = 5
    for cid, codes, flag, block_reason in retests:
        label = "+".join(codes)
        rep.section(f"{section}. {cid} — defences {label} enforced (re-test)")
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
        rep.written.append(out_dir / f"{cid}.json")
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
        )
        summary = gate.summarise(runs)
        rep.say("\n" + _fmt_summary(summary))
        if saved_before is not None and saved is None:
            rep.say(f"({cid} is a deterministic {label} replay of the saved RC-001 traces, not a measured "
                    f"run; pass {flag} <folder> to gate a saved {label} run)")
        rep.say(f"Runs file: {proof}")
        rep.say(
            f"\nAttack success: {prev_cid} {prev_summary['asr']:.1%} -> {cid} {summary['asr']:.1%}"
            f"   |   benign: {prev_summary['benign_rate']:.0%} -> {summary['benign_rate']:.0%}"
        )

        section += 1
        rep.section(f"{section}. Gate verdict for {cid}, then the reviewer decides")
        verdict = gate.evaluate(summary, policy, last_approved_asr=last_approved_asr)
        report = gate.report_text(cid, verdict)
        rep.say(report)
        rep.write(f"gate_{cid}.txt", report)

        audit.append(audit_path, {"event": "submit", "candidate_id": cid, "by": args.engineer, "defences": list(codes)})
        audit.append(audit_path, {"event": "verdict", "candidate_id": cid, "verdict": verdict.status})
        if verdict.passed:
            ev = decision.decide(
                candidate_id=cid, submitted_by=args.engineer, verdict_passed=True,
                policy=policy, reviewer=args.reviewer, action="APPROVE",
            )
            last_approved_asr = summary["asr"]
            rep.say(f"\nLogged decision: {ev['decision']} {cid} by {ev['reviewer']}")
        else:
            ev = decision.decide(
                candidate_id=cid, submitted_by=args.engineer, verdict_passed=False,
                policy=policy, reviewer=args.reviewer, action="BLOCK", reason=block_reason,
            )
            rep.say(f"\nLogged decision: {ev['decision']} {cid} by {ev['reviewer']} — \"{block_reason}\"")
        audit.append(audit_path, ev)

        section += 1
        rep.section(f"{section}. Assurance after {cid} — risk register, evidence register")
        prior = risks.prior_from(prev_register)  # carries whether each risk had landed
        register = risks.build_register(runs, policy, control_map, prior=prior, defences=codes)
        reg_md = risks.to_markdown(register)
        rep.write(f"risk_register_{cid}.md", reg_md)
        rep.say(reg_md)

        measured = evidence.control_results(runs, [r.control for r in register if r.control], defences=codes)
        ev_rows = evidence.build_register(register, retest_candidate=cid, results=measured, proof_file=str(proof))
        ev_md = evidence.to_markdown(ev_rows)
        rep.write(f"evidence_register_{cid}.md", ev_md)
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
    rep.section(f"{section}. Traceability — one finding, start to finish")
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
    rep.say(tr_md)

    # ----------------------------------------------------------------------- #
    # Manager summary.
    # ----------------------------------------------------------------------- #
    section += 1
    rep.section(f"{section}. Manager summary")
    s = summary_page.build_summary(
        register_final, audit.read_log(audit_path), current_asr=summary_final["asr"]
    )
    s_md = summary_page.to_markdown(s)
    rep.write("manager_summary.md", s_md)
    rep.say(s_md)

    # ----------------------------------------------------------------------- #
    # Tamper-evidence: verify the real log, then prove a forged copy is caught.
    # The real audit_log.jsonl is left intact and verifiable.
    # ----------------------------------------------------------------------- #
    section += 1
    rep.section(f"{section}. The audit log is tamper-evident")
    real = audit.verify_log(audit_path)
    rep.say(f"Real log      : {real}")

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
            rep.written.append(tampered)
            check = audit.verify_log(tampered)
            rep.say(f"Forged copy   : {check}")
            if not check.ok:
                rep.say(f">>> TAMPERING DETECTED at entry #{check.bad_seq} (real log untouched) <<<")

    # ----------------------------------------------------------------------- #
    # Final summary of artifacts + the headline verdicts.
    # ----------------------------------------------------------------------- #
    rep.section("Done — artifacts written")
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
        + [f"{summaries[cid]['asr']:.1%} ({'+'.join(codes)})" for cid, codes, _, _ in retests]
    )
    benign_chain = " -> ".join(f"{summaries[cid]['benign_rate']:.0%}" for cid in summaries)
    rep.say(f"Headline: attack success {asr_chain}, benign {benign_chain}")
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
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
