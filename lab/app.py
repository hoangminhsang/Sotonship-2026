"""Agent Release Gate -- one runnable, menu-driven program.

Ties the three parts together behind a text menu:

    1. Red team      -- test a candidate agent, produce the evidence
    2. Governance    -- fingerprint, gate verdict, decisions, roles, audit log
    3. Assurance     -- risk register, controls, evidence, traceability, summary

State lives under ``governance_out/`` so candidates, decisions, role edits and
the hash-chained audit log survive between actions and between runs. Everything
reuses the tested ``lab`` modules; nothing new is scored here.

Every candidate records its **run mode**: ``SCRIPTED`` (the offline demo, whose
traces are scripted to the rehearsal rates and whose benign leg was never
measured) or ``LIVE`` (a real model run). Every screen that shows a number
repeats that label, so a scripted figure can never pass for a measured one.

Run it:  ``.venv/bin/python -m lab.app``
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from lab import agent, assurance, corpus, demo, defences, metrics, world
from lab.assurance import risks, inventory, evidence, thread, summary_page
from lab.governance import candidate, gate, decision, roles, load_policy, audit

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "governance_out"      # default workspace; GovState(work=...) overrides it

# Run modes (review finding D-01). The scripted source text states what the
# demo numbers rest on: results/summary.csv holds 24 attack runs and 0 benign.
SCRIPTED = "SCRIPTED"
LIVE = "LIVE"
SCRIPTED_SOURCE = "scripted: calibrated to the 24-run rehearsal; benign not measured"
SCRIPTED_DIGEST = "none (scripted demo)"      # no model ran, so there is no digest
TOOLS = ["read_doc", "query_assets", "create_ticket"]
OLLAMA_DOWN = "Ollama unreachable or model missing — no candidate saved"


# ---------------------------------------------------------------------------
# Tiny input helpers (robust to Ctrl-C / EOF).
# ---------------------------------------------------------------------------

def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        got = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n(cancelled)")
        return default
    return got or default


def choose(prompt: str, options: list[str]) -> int:
    print(prompt)
    for i, opt in enumerate(options, start=1):
        print(f"  {i}. {opt}")
    raw = ask("Choose", "")
    try:
        n = int(raw)
        if 1 <= n <= len(options):
            return n
    except ValueError:
        pass
    print("  (not a valid choice)")
    return -1


def pause() -> None:
    try:
        input("\n[enter] to continue ")
    except (EOFError, KeyboardInterrupt):
        pass


# ---------------------------------------------------------------------------
# Run-mode labels (D-01): every number on screen says where it came from.
# ---------------------------------------------------------------------------

def mode_of(rec: dict) -> str:
    return rec.get("mode") or "UNKNOWN"


def mode_label(rec: dict) -> str:
    return f"[{mode_of(rec)}]"


def tag_numbers(text: str, rec: dict, every: bool = False) -> str:
    """Append the run-mode label to every line of ``text`` that shows a number.

    ``every=True`` labels all non-blank lines (for short cards where even a
    "none" is a claim about what ran).
    """
    tag = mode_label(rec)
    return "\n".join(
        f"{ln}  {tag}" if (every and ln.strip()) or re.search(r"\d", ln) else ln
        for ln in text.splitlines()
    )


def source_line(rec: dict) -> str:
    return f"  source: {rec.get('source') or 'unknown (record predates run-mode labels)'}"


def result_line(cid: str, rec: dict) -> str:
    """One line of numbers for a candidate, always labelled with its mode."""
    sm = rec["summary"]
    return (f"{cid} {mode_label(rec)}: ASR {sm['asr']:.1%} "
            f"({sm['attack_violations']}/{sm['attack_runs']}), "
            f"95% CI [{sm['asr_lo']:.1%}, {sm['asr_hi']:.1%}], "
            f"benign {sm['benign_rate']:.0%} ({sm['benign_n']} runs), "
            f"errors {sm.get('errors', 0)}  {mode_label(rec)}")


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------------------
# Persistent state.
# ---------------------------------------------------------------------------

class GovState:
    """Loads/saves the whole workspace: policy+roles, candidates, audit log."""

    def __init__(self, work: Path | None = None) -> None:
        self.work = Path(work) if work is not None else WORK
        self.work.mkdir(parents=True, exist_ok=True)
        self.cand_file = self.work / "candidates.json"
        self.roles_file = self.work / "roles_overlay.json"
        self.audit_file = self.work / "audit_log.jsonl"

        self.rules = world.load_rules()
        self.control_map = assurance.load_control_map()
        self.policy = load_policy()
        self._apply_roles_overlay()

        corpus.build_and_write()
        self.test = corpus.read_jsonl(corpus.TEST_PATH)
        self.tests_by_id = {t["id"]: t for t in self.test}

        self.candidates: dict[str, dict] = (
            json.loads(self.cand_file.read_text()) if self.cand_file.exists() else {}
        )

    # -- roles overlay (so edits survive without clobbering the commented yaml)
    def _apply_roles_overlay(self) -> None:
        if self.roles_file.exists():
            self.policy["roles"].update(json.loads(self.roles_file.read_text()))

    def save_roles(self) -> None:
        self.roles_file.write_text(json.dumps(self.policy["roles"], indent=2))

    def save_candidates(self) -> None:
        self.cand_file.write_text(json.dumps(self.candidates, indent=2))

    # -- candidate helpers
    def next_candidate_id(self) -> str:
        n = len(self.candidates) + 1
        while f"RC-{n:03d}" in self.candidates:
            n += 1
        return f"RC-{n:03d}"

    def traces_path(self, cid: str) -> Path:
        return self.work / cid / "traces.json"

    def fingerprint_path(self, cid: str) -> Path:
        return self.work / "candidates" / f"{cid}.json"

    def load_runs(self, cid: str) -> pd.DataFrame | None:
        """Rebuild a candidate's runs frame from its stored traces (correct dtypes)."""
        p = self.traces_path(cid)
        if not p.exists():
            return None
        traces = json.loads(p.read_text())
        return metrics.build_runs_frame(traces, self.rules, self.tests_by_id)

    def last_approved(self) -> tuple[str, float] | None:
        """Most recent approved candidate and its ASR, for the regression check."""
        for cid in reversed(list(self.candidates)):
            rec = self.candidates[cid]
            if rec.get("decision") == decision.APPROVE and rec.get("summary"):
                return cid, rec["summary"]["asr"]
        return None

    def log(self, event: dict) -> dict:
        return audit.append(self.audit_file, event)

    def events(self) -> list[dict]:
        return audit.read_log(self.audit_file)


# ---------------------------------------------------------------------------
# 1. Red team.
# ---------------------------------------------------------------------------

def menu_redteam(s: GovState) -> None:
    while True:
        c = choose("\n== Red team ==", [
            "Test a NEW candidate (produce evidence)",
            "List candidates and their results",
            "Back",
        ])
        if c == 1:
            redteam_run(s)
        elif c == 2:
            list_candidates(s)
            pause()
        else:
            return


def _run_live(runner, subset: list[dict], codes: list[str], cid: str) -> list[dict]:
    """Run the model. Passes ``candidate``/``split`` when the runner accepts them."""
    try:
        return list(runner.run_split(subset, reps=3, defences=tuple(codes),
                                     candidate=cid, split="test"))
    except TypeError as e:
        if "unexpected keyword" not in str(e):
            raise
        return list(runner.run_split(subset, reps=3, defences=tuple(codes)))


def redteam_run(s: GovState) -> None:
    cid = ask("Candidate id", s.next_candidate_id())
    submitted_by = ask("Submitted by (engineer)", "tom")
    raw = ask("Defences ON (comma, e.g. D2 or D1,D2,D3; blank = none)", "")
    codes = [d.strip().upper() for d in raw.split(",") if d.strip()]
    live = choose("Run mode", ["DEMO (offline, scripted traces)", "LIVE (real Ollama model)"]) == 2

    print(f"\nRunning {cid}  defences={codes or 'none'}  mode={LIVE if live else SCRIPTED} ...")
    if live:
        from lab import runner
        sample = int(ask("Sample size per split (LIVE is slow)", "8") or "8")
        attacks = [t for t in s.test if t["kind"] == "attack"][:sample]
        benign = [t for t in s.test if t["kind"] == "benign"][:sample]
        try:
            traces = _run_live(runner, attacks + benign, codes, cid)
        except (ConnectionError, OSError) as e:
            print(f"\n{type(e).__name__}: {e}")
            print(OLLAMA_DOWN)
            return pause()
        errors = sum(1 for t in traces if t.get("error"))
        print(f"\nLIVE: {len(traces)} runs, {errors} errored")
        if not traces:
            print("no runs -- not measured; no candidate saved")
            return pause()
        if errors == len(traces):
            print(OLLAMA_DOWN)
            return pause()
        digest = runner.model_digest()
        mode = LIVE
        source = (f"live: {agent.MODEL} {digest or '(digest unknown)'}; "
                  f"{len(attacks)} attack + {len(benign)} benign tests x 3 reps; "
                  f"{errors} errored runs excluded from every rate")
    else:
        traces = demo.demo_traces(s.test)
        if codes:
            traces = defences.replay(traces, s.rules, codes)
        digest = SCRIPTED_DIGEST
        mode, source = SCRIPTED, SCRIPTED_SOURCE

    _save_candidate(s, cid, submitted_by, codes, traces, digest, mode, source)
    rec = s.candidates[cid]
    print(f"\n{result_line(cid, rec)}")
    print(source_line(rec))
    print(f"  evidence in {_rel(s.work / cid)}/")
    pause()


def _save_candidate(s: GovState, cid: str, submitted_by: str, codes: list[str],
                    traces: list[dict], digest: str, mode: str, source: str) -> None:
    """Persist traces + runs.csv, fingerprint and summary; log the submit."""
    runs = metrics.build_runs_frame(traces, s.rules, s.tests_by_id)
    (s.work / cid).mkdir(parents=True, exist_ok=True)
    s.traces_path(cid).write_text(json.dumps(traces, indent=2))
    runs.to_csv(s.work / cid / "runs.csv", index=False)
    summary = gate.summarise(runs)

    fp = candidate.fingerprint(
        cid, submitted_by=submitted_by, model=agent.MODEL, model_digest=digest,
        system_prompt=agent.SYSTEM_PROMPT, tools=TOOLS, defences=codes,
        rules_text=world.RULES_PATH.read_text(), test_set_text=corpus.TEST_PATH.read_text(),
    )
    candidate.save(fp, s.work / "candidates")

    s.candidates[cid] = {
        "submitted_by": submitted_by, "defences": codes, "mode": mode, "source": source,
        "identity_hash": fp.identity_hash(), "summary": summary,
        "decision": None, "created_at": datetime.now(timezone.utc).isoformat(),
    }
    s.save_candidates()
    event = {"event": "submit", "candidate_id": cid, "by": submitted_by, "defences": codes,
             "mode": mode, "source": source, "identity_hash": fp.identity_hash()}
    if mode == SCRIPTED:
        event["scripted"] = True
    s.log(event)


def list_candidates(s: GovState) -> None:
    if not s.candidates:
        print("  (no candidates yet -- run a red-team test first)")
        return
    print(f"\n  {'id':<8}{'by':<8}{'defences':<12}{'mode':<10}{'ASR':>7}  decision")
    for cid, rec in s.candidates.items():
        asr = f"{rec['summary']['asr']:.1%}" if rec.get("summary") else "-"
        print(f"  {cid:<8}{rec['submitted_by']:<8}"
              f"{(','.join(rec['defences']) or 'none'):<12}{mode_of(rec):<10}{asr:>7}  "
              f"{rec.get('decision') or '(none)'}")
        print(f"  {'':<8}{source_line(rec).strip()}")


def pick_candidate(s: GovState, prompt: str = "Candidate id") -> str | None:
    list_candidates(s)
    if not s.candidates:
        return None
    cid = ask(prompt, next(reversed(s.candidates)))
    if cid not in s.candidates:
        print(f"  no such candidate {cid!r}")
        return None
    return cid


# ---------------------------------------------------------------------------
# 2. Governance.
# ---------------------------------------------------------------------------

def menu_governance(s: GovState) -> None:
    while True:
        c = choose("\n== Governance (release gate) ==", [
            "Show gate verdict (PASS/FAIL) for a candidate",
            "Make a decision (Approve / Block / Override)",
            "Manage roles (who can do what)",
            "View audit log",
            "Verify audit log (tamper check)",
            "Back",
        ])
        if c == 1:
            gov_verdict(s)
        elif c == 2:
            gov_decide(s)
        elif c == 3:
            gov_roles(s)
        elif c == 4:
            gov_view_log(s)
        elif c == 5:
            gov_verify_log(s)
        else:
            return


def _verdict_for(s: GovState, cid: str):
    summary = s.candidates[cid]["summary"]
    prior = s.last_approved()
    last_asr = prior[1] if prior and prior[0] != cid else None
    return gate.evaluate(summary, s.policy, last_approved_asr=last_asr)


def gov_verdict(s: GovState) -> None:
    cid = pick_candidate(s)
    if not cid:
        return pause()
    rec = s.candidates[cid]
    print()
    print(tag_numbers(gate.report_text(cid, _verdict_for(s, cid)), rec))
    print(source_line(rec))
    pause()


def gov_decide(s: GovState) -> None:
    cid = pick_candidate(s)
    if not cid:
        return pause()
    rec = s.candidates[cid]
    verdict = _verdict_for(s, cid)
    print(f"\n{cid} verdict: {verdict.status} {mode_label(rec)}  (submitted by {rec['submitted_by']})")
    print(source_line(rec))

    act = choose("Decision", ["Approve", "Block", "Override"])
    action = {1: decision.APPROVE, 2: decision.BLOCK, 3: decision.OVERRIDE}.get(act)
    if not action:
        return pause()
    reviewer = ask("Reviewer name", "sarah")
    reason = ask("Reason", "") if action != decision.APPROVE else ""
    expiry = ask("Override expiry (YYYY-MM-DD)", "") if action == decision.OVERRIDE else None

    try:
        ev = decision.decide(candidate_id=cid, submitted_by=rec["submitted_by"],
                             verdict_passed=verdict.passed, policy=s.policy,
                             reviewer=reviewer, action=action, reason=reason, expiry=expiry)
    except decision.DecisionError as e:
        print(f"\n{e}")
        s.log({"event": "decision_refused", "candidate_id": cid, "reviewer": reviewer,
               "action": action, "detail": str(e)})
        return pause()

    ev["mode"] = mode_of(rec)
    s.log(ev)
    rec["decision"] = action
    s.save_candidates()
    print(f"\nLogged: {action} on {cid} {mode_label(rec)} by {reviewer}.")
    pause()


def gov_roles(s: GovState) -> None:
    print("\nCurrent roles:")
    for k, v in s.policy["roles"].items():
        print(f"  {k}: {v}")
    act = choose("Change roles", ["Add a person to a role", "Remove a person from a role", "Back"])
    if act not in (1, 2):
        return
    actor = ask("Who is making this change? (must be the rule owner)", s.policy["roles"].get("rule_owner", ""))
    role = ask("Role group (engineers / reviewers)", "reviewers")
    person = ask("Person")
    if not person:
        return pause()
    try:
        ev = roles.change_role(s.policy, actor=actor, person=person, role=role,
                               action="add" if act == 1 else "remove")
    except roles.RoleError as e:
        print(f"\n{e}")
        return pause()
    s.save_roles()
    s.log(ev)
    print(f"\nDone. {role} is now: {s.policy['roles'][role]}  (logged in the audit trail)")
    pause()


def gov_view_log(s: GovState) -> None:
    entries = s.events()
    if not entries:
        print("  (audit log empty)")
        return pause()
    print()
    for e in entries:
        extra = e.get("decision") or e.get("action") or e.get("verdict") or ""
        who = e.get("reviewer") or e.get("by") or e.get("actor") or ""
        marker = "  [scripted]" if e.get("scripted") else ""
        print(f"  #{e['seq']:<3}{e.get('time','')[:19]:<21}{e['event']:<17}"
              f"{e.get('candidate_id',''):<8}{extra:<9}{who}{marker}")
    pause()


def gov_verify_log(s: GovState) -> None:
    check = audit.verify_log(s.audit_file)
    print(f"\n{check.detail}")
    if check.ok:
        print("Chain intact. ✔")
    else:
        print(f">>> TAMPERING DETECTED at entry #{check.bad_seq} <<<")
    pause()


# ---------------------------------------------------------------------------
# 3. Assurance.
# ---------------------------------------------------------------------------

def menu_assurance(s: GovState) -> None:
    while True:
        c = choose("\n== Assurance (risk & evidence) ==", [
            "Risk register (optionally vs a prior candidate)",
            "Fix tickets",
            "Control mapping (OWASP / ATLAS / NIST)",
            "Evidence register",
            "Traceability (one finding, start to finish)",
            "Manager summary",
            "AI inventory card",
            "Write ALL reports to markdown files",
            "Back",
        ])
        if c == 9 or c == -1:
            return
        cid = pick_candidate(s)
        if not cid:
            pause()
            continue
        runs = s.load_runs(cid)
        if runs is None:
            print("  (no stored runs for that candidate)")
            pause()
            continue
        rec = s.candidates[cid]
        reg = _register_for(s, cid, runs)
        print(f"\n{cid} {mode_label(rec)} -- {rec.get('source') or 'source unknown'}")
        if c == 1:
            print("\n" + risks.to_markdown(reg))
        elif c == 2:
            for t in risks.fix_tickets(reg, due="2026-10-24"):
                print(f"  {t.id}: {t.title} (owner {t.owner}, risk {t.risk_id})")
        elif c == 3:
            for r in reg:
                if r.control:
                    print(f"  {r.id} {r.violation_class:<20} -> {r.control}  "
                          f"OWASP {r.owasp} | ATLAS {r.mitre_atlas} | NIST {r.nist_ai_rmf}")
        elif c == 4:
            res = {"D2": f"{int((runs['violation_classes'].str.contains('V_exfiltration', na=False)).sum())} exfiltration runs"}
            print("\n" + evidence.to_markdown(evidence.build_register(reg, retest_candidate=cid, results=res)))
        elif c == 5:
            assurance_thread(s, cid, runs, reg)
        elif c == 6:
            print("\n" + manager_summary_text(s, cid, reg))
        elif c == 7:
            print("\n" + inventory_text(s, cid))
        elif c == 8:
            write_reports(s, cid, runs, reg)
        pause()


def _register_for(s: GovState, cid: str, runs: pd.DataFrame):
    """Build the register, using a chosen prior candidate to close/reopen risks."""
    prior_id = ask("Compare against prior candidate for status? (id or blank)", "")
    prior = None
    if prior_id and prior_id in s.candidates:
        prior_runs = s.load_runs(prior_id)
        if prior_runs is not None:
            prior = risks.prior_from(risks.build_register(prior_runs, s.policy, s.control_map))
    return risks.build_register(runs, s.policy, s.control_map, prior=prior)


def assurance_thread(s: GovState, cid: str, runs: pd.DataFrame, reg) -> None:
    leaked = runs[(runs["kind"] == "attack") & runs["violated"]]
    default = leaked.iloc[0]["test_id"] if not leaked.empty else "ATK-0021"
    finding = ask("Finding id", default)
    tickets = risks.fix_tickets(reg)
    tr = thread.build_thread(finding, runs, reg, tickets, s.events(), retest_candidate=cid)
    print("\n" + thread.to_markdown(tr))


def manager_summary_text(s: GovState, cid: str, reg) -> str:
    """The one-page view, every number labelled with the candidate's run mode."""
    rec = s.candidates[cid]
    md = summary_page.to_markdown(
        summary_page.build_summary(reg, s.events(), current_asr=rec["summary"]["asr"]))
    return tag_numbers(md, rec) + "\n" + source_line(rec)


def inventory_text(s: GovState, cid: str) -> str:
    rec = s.candidates[cid]
    fp = candidate.load(s.fingerprint_path(cid))
    card = inventory.build_card(fp, s.policy)
    card["run_mode"] = mode_of(rec)
    card["source"] = rec.get("source") or "unknown"
    return tag_numbers(inventory.to_markdown(card), rec, every=True)


def write_reports(s: GovState, cid: str, runs: pd.DataFrame, reg) -> None:
    out = s.work / cid
    out.mkdir(parents=True, exist_ok=True)
    rec = s.candidates[cid]
    banner = f"_Run mode: {mode_of(rec)} -- {rec.get('source') or 'source unknown'}_\n\n"
    files = {
        "inventory.md": inventory_text(s, cid),
        "risk_register.md": risks.to_markdown(reg),
        "evidence_register.md": evidence.to_markdown(evidence.build_register(reg, retest_candidate=cid)),
        "summary.md": manager_summary_text(s, cid, reg),
    }
    for name, text in files.items():
        (out / name).write_text(banner + text + "\n", encoding="utf-8")
    print(f"\nWrote {', '.join(files)} to {_rel(out)}/")


# ---------------------------------------------------------------------------
# Main loop.
# ---------------------------------------------------------------------------

def main() -> None:
    s = GovState()
    print("=" * 64)
    print(" Agent Release Gate  --  sandbox lab, all data synthetic")
    print(f" workspace: {_rel(s.work)}/   test split: {len(s.test)} records")
    print("=" * 64)
    while True:
        c = choose("\n== MAIN MENU ==", [
            "Red team  (test a candidate agent)",
            "Governance  (gate, decisions, roles, audit log)",
            "Assurance  (risks, controls, evidence, traceability)",
            "Quit",
        ])
        if c == 1:
            menu_redteam(s)
        elif c == 2:
            menu_governance(s)
        elif c == 3:
            menu_assurance(s)
        elif c == 4:
            print("bye.")
            return


if __name__ == "__main__":
    main()
