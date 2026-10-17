"""Agent Release Gate -- one runnable, menu-driven program.

Ties the three parts together behind a text menu:

    1. Red team      -- test a candidate agent, produce the evidence
    2. Governance    -- fingerprint, gate verdict, decisions, roles, audit log
    3. Assurance     -- risk register, controls, evidence, traceability, summary

State lives under ``governance_out/`` so candidates, decisions, role edits and
the hash-chained audit log survive between actions and between runs. Everything
reuses the tested ``lab`` modules; nothing new is scored here.

Run it:  ``.venv/bin/python -m lab.app``
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from lab import agent, assurance, corpus, demo, defences, metrics, world
from lab.assurance import risks, inventory, evidence, thread, summary_page
from lab.governance import candidate, gate, decision, roles, load_policy, audit

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "governance_out"
CAND_FILE = WORK / "candidates.json"
ROLES_FILE = WORK / "roles_overlay.json"
AUDIT_FILE = WORK / "audit_log.jsonl"


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
# Persistent state.
# ---------------------------------------------------------------------------

class GovState:
    """Loads/saves the whole workspace: policy+roles, candidates, audit log."""

    def __init__(self) -> None:
        WORK.mkdir(exist_ok=True)
        self.rules = world.load_rules()
        self.control_map = assurance.load_control_map()
        self.policy = load_policy()
        self._apply_roles_overlay()

        corpus.build_and_write()
        self.test = corpus.read_jsonl(corpus.TEST_PATH)
        self.tests_by_id = {t["id"]: t for t in self.test}

        self.candidates: dict[str, dict] = (
            json.loads(CAND_FILE.read_text()) if CAND_FILE.exists() else {}
        )

    # -- roles overlay (so edits survive without clobbering the commented yaml)
    def _apply_roles_overlay(self) -> None:
        if ROLES_FILE.exists():
            self.policy["roles"].update(json.loads(ROLES_FILE.read_text()))

    def save_roles(self) -> None:
        ROLES_FILE.write_text(json.dumps(self.policy["roles"], indent=2))

    def save_candidates(self) -> None:
        CAND_FILE.write_text(json.dumps(self.candidates, indent=2))

    # -- candidate helpers
    def next_candidate_id(self) -> str:
        n = len(self.candidates) + 1
        while f"RC-{n:03d}" in self.candidates:
            n += 1
        return f"RC-{n:03d}"

    def traces_path(self, cid: str) -> Path:
        return WORK / cid / "traces.json"

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
        return audit.append(AUDIT_FILE, event)


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


def redteam_run(s: GovState) -> None:
    cid = ask("Candidate id", s.next_candidate_id())
    submitted_by = ask("Submitted by (engineer)", "tom")
    raw = ask("Defences ON (comma, e.g. D2 or D1,D2,D3; blank = none)", "")
    codes = [d.strip().upper() for d in raw.split(",") if d.strip()]
    live = choose("Run mode", ["DEMO (offline, deterministic)", "LIVE (real Ollama model)"]) == 2

    print(f"\nRunning {cid}  defences={codes or 'none'}  mode={'LIVE' if live else 'DEMO'} ...")
    if live:
        from lab import runner
        sample = int(ask("Sample size per split (LIVE is slow)", "8") or "8")
        subset = s.test[:sample] + [t for t in s.test if t["kind"] == "benign"][:sample]
        traces = list(runner.run_split(subset, reps=3, defences=tuple(codes)))
        digest = runner.model_digest()
    else:
        traces = demo.demo_traces(s.test)
        if codes:
            traces = defences.replay(traces, s.rules, codes)
        digest = "sha256:845dbda0..."

    # Persist traces + a human-readable runs.csv, and compute the summary.
    runs = metrics.build_runs_frame(traces, s.rules, s.tests_by_id)
    (WORK / cid).mkdir(parents=True, exist_ok=True)
    s.traces_path(cid).write_text(json.dumps(traces, indent=2))
    runs.to_csv(WORK / cid / "runs.csv", index=False)
    summary = gate.summarise(runs)

    fp = candidate.fingerprint(
        cid, submitted_by=submitted_by, model=agent.MODEL, model_digest=digest,
        system_prompt=agent.SYSTEM_PROMPT,
        tools=["read_doc", "query_assets", "create_ticket"], defences=codes,
        rules_text=world.RULES_PATH.read_text(), test_set_text=corpus.TEST_PATH.read_text(),
    )
    candidate.save(fp, WORK / "candidates")

    s.candidates[cid] = {
        "submitted_by": submitted_by, "defences": codes,
        "identity_hash": fp.identity_hash(), "summary": summary,
        "decision": None, "created_at": datetime.now(timezone.utc).isoformat(),
    }
    s.save_candidates()
    s.log({"event": "submit", "candidate_id": cid, "by": submitted_by, "defences": codes})

    print(f"\n{cid}: ASR {summary['asr']:.1%} "
          f"({summary['attack_violations']}/{summary['attack_runs']}), "
          f"benign {summary['benign_rate']:.0%} ({summary['benign_n']} runs). "
          f"Evidence in governance_out/{cid}/")
    pause()


def list_candidates(s: GovState) -> None:
    if not s.candidates:
        print("  (no candidates yet -- run a red-team test first)")
        return
    print(f"\n  {'id':<8}{'by':<8}{'defences':<12}{'ASR':>7}  {'decision'}")
    for cid, rec in s.candidates.items():
        asr = f"{rec['summary']['asr']:.1%}" if rec.get("summary") else "-"
        print(f"  {cid:<8}{rec['submitted_by']:<8}"
              f"{(','.join(rec['defences']) or 'none'):<12}{asr:>7}  {rec.get('decision') or '(none)'}")


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
    print()
    print(gate.report_text(cid, _verdict_for(s, cid)))
    pause()


def gov_decide(s: GovState) -> None:
    cid = pick_candidate(s)
    if not cid:
        return pause()
    rec = s.candidates[cid]
    verdict = _verdict_for(s, cid)
    print(f"\n{cid} verdict: {verdict.status}  (submitted by {rec['submitted_by']})")

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

    s.log(ev)
    rec["decision"] = action
    s.save_candidates()
    print(f"\nLogged: {action} on {cid} by {reviewer}.")
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
    entries = audit.read_log(AUDIT_FILE)
    if not entries:
        print("  (audit log empty)")
        return pause()
    print()
    for e in entries:
        extra = e.get("decision") or e.get("action") or e.get("verdict") or ""
        who = e.get("reviewer") or e.get("by") or e.get("actor") or ""
        print(f"  #{e['seq']:<3}{e.get('time','')[:19]:<21}{e['event']:<17}"
              f"{e.get('candidate_id',''):<8}{extra:<9}{who}")
    pause()


def gov_verify_log(s: GovState) -> None:
    check = audit.verify_log(AUDIT_FILE)
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
        reg = _register_for(s, cid, runs)
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
            s_asr = s.candidates[cid]["summary"]["asr"]
            print("\n" + summary_page.to_markdown(
                summary_page.build_summary(reg, audit.read_log(AUDIT_FILE), current_asr=s_asr)))
        elif c == 7:
            fp = candidate.load(WORK / "candidates" / f"{cid}.json")
            print("\n" + inventory.to_markdown(inventory.build_card(fp, s.policy)))
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
    tr = thread.build_thread(finding, runs, reg, tickets, audit.read_log(AUDIT_FILE), retest_candidate=cid)
    print("\n" + thread.to_markdown(tr))


def write_reports(s: GovState, cid: str, runs: pd.DataFrame, reg) -> None:
    out = WORK / cid
    out.mkdir(parents=True, exist_ok=True)
    fp = candidate.load(WORK / "candidates" / f"{cid}.json")
    s_asr = s.candidates[cid]["summary"]["asr"]
    files = {
        "inventory.md": inventory.to_markdown(inventory.build_card(fp, s.policy)),
        "risk_register.md": risks.to_markdown(reg),
        "evidence_register.md": evidence.to_markdown(evidence.build_register(reg, retest_candidate=cid)),
        "summary.md": summary_page.to_markdown(
            summary_page.build_summary(reg, audit.read_log(AUDIT_FILE), current_asr=s_asr)),
    }
    for name, text in files.items():
        (out / name).write_text(text + "\n", encoding="utf-8")
    print(f"\nWrote {', '.join(files)} to governance_out/{cid}/")


# ---------------------------------------------------------------------------
# Main loop.
# ---------------------------------------------------------------------------

def main() -> None:
    s = GovState()
    print("=" * 64)
    print(" Agent Release Gate  --  sandbox lab, all data synthetic")
    print(f" workspace: governance_out/   test split: {len(s.test)} records")
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
