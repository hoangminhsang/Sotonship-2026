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

import copy
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from lab import agent, assurance, corpus, demo, defences, governance, metrics, world
from lab.assurance import risks, inventory, evidence, thread, summary_page
from lab.governance import candidate, gate, decision, roles, load_policy, audit

# Names the governance lane adds (hash-chain and policy validation errors).
# Resolved by name so this module runs before and after that merge.
AuditError = getattr(audit, "AuditError", None) or type("AuditError", (RuntimeError,), {})
PolicyError = getattr(governance, "PolicyError", None) or type("PolicyError", (ValueError,), {})

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
REPS = 3                                       # runs per test, LIVE and scripted


# ---------------------------------------------------------------------------
# Tiny input helpers. Ctrl-C or EOF at a prompt cancels the current action
# (at the main menu it quits); every menu action runs inside run_action, so
# one failure prints one line and the menu comes back (B-09, B-22, D-09).
# ---------------------------------------------------------------------------

class Cancelled(Exception):
    """Ctrl-C or end of input at a prompt."""


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        got = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n(cancelled)")
        raise Cancelled()
    return got or default


def ask_int(prompt: str, default: int, lo: int = 1) -> int:
    """Ask for a whole number >= ``lo``; re-prompt on anything else."""
    while True:
        raw = ask(prompt, str(default))
        try:
            n = int(raw)
        except ValueError:
            print(f"  (enter a whole number, e.g. {default})")
            continue
        if n < lo:
            print(f"  (enter a number of at least {lo})")
            continue
        return n


def run_action(fn, *args) -> None:
    """Run one menu action; any failure prints one line and returns to the menu."""
    try:
        fn(*args)
    except Cancelled:
        return
    except KeyboardInterrupt:
        print("\n(interrupted -- back to the menu)")
    except AuditError as e:
        print(f"\naudit log problem: {e}  (nothing was recorded; back to the menu)")
    except Exception as e:  # noqa: BLE001 - the menu must survive any one action
        print(f"\nerror: {type(e).__name__}: {e}  (back to the menu)")


def run_menu(s: "GovState", title: str, options: list[str], actions: dict) -> None:
    """Show ``options`` until Back/cancel; ``actions`` maps a choice to a function."""
    while True:
        try:
            c = choose(title, options)
        except Cancelled:
            return
        if c == -1:
            continue
        fn = actions.get(c)
        if fn is None:
            return
        run_action(fn, s)


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
        f"{ln}  {tag}" if tag not in ln and ((every and ln.strip()) or re.search(r"\d", ln)) else ln
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
        while self.candidate_exists(f"RC-{n:03d}"):
            n += 1
        return f"RC-{n:03d}"

    def candidate_exists(self, cid: str) -> bool:
        """Known in the record, or evidence already on disk under that id."""
        return (cid in self.candidates or (self.work / cid).exists()
                or self.fingerprint_path(cid).exists())

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
    run_menu(s, "\n== Red team ==", [
        "Test a NEW candidate (produce evidence)",
        "List candidates and their results",
        "Back",
    ], {1: redteam_run, 2: _list_and_pause})


def _list_and_pause(s: GovState) -> None:
    list_candidates(s)
    pause()


def _run_live(runner, subset: list[dict], codes: list[str], cid: str) -> list[dict]:
    """Run the model. Passes ``candidate``/``split`` when the runner accepts them."""
    try:
        return list(runner.run_split(subset, reps=REPS, defences=tuple(codes),
                                     candidate=cid, split="test"))
    except TypeError as e:
        if "unexpected keyword" not in str(e):
            raise
        return list(runner.run_split(subset, reps=REPS, defences=tuple(codes)))


CANDIDATE_ID_RE = re.compile(r"^RC-\d{3,}$")


def _norm(name: str) -> str:
    """One spelling per person: trimmed, single-spaced, lower-case (B-03)."""
    return " ".join(str(name).strip().split()).lower()


def ask_candidate_id(s: GovState) -> str:
    """A fresh id of the form RC-NNN; an existing id is refused (B-20, D-06)."""
    while True:
        cid = ask("Candidate id", s.next_candidate_id())
        if not CANDIDATE_ID_RE.match(cid):
            print(f"  ({cid!r} is not a candidate id; use the form RC-001)")
            continue
        if s.candidate_exists(cid):
            print(f"  ({cid} already exists and its evidence is kept; "
                  f"next free id is {s.next_candidate_id()})")
            continue
        return cid


def pick_engineer(s: GovState) -> str | None:
    """The submitter is one of ``roles.engineers`` (B-03), never free text."""
    engineers = sorted({_norm(e) for e in s.policy["roles"].get("engineers", []) if _norm(e)})
    if not engineers:
        print("  (no engineers in roles -- add one under Governance > Manage roles)")
        return None
    while True:
        c = choose("Submitted by (engineer)", engineers)
        if c != -1:
            return engineers[c - 1]


def ask_defences() -> list[str]:
    """Only known defence codes, de-duplicated and sorted (B-19)."""
    valid = list(defences.ALL_DEFENCES)
    while True:
        raw = ask("Defences ON (comma, e.g. D2 or D1,D2,D3; blank = none)", "")
        codes = sorted({d.strip().upper() for d in raw.split(",") if d.strip()})
        bad = [c for c in codes if c not in valid]
        if bad:
            print(f"  (unknown defence code(s): {', '.join(bad)}; valid: {', '.join(valid)})")
            continue
        return codes


def redteam_run(s: GovState) -> None:
    cid = ask_candidate_id(s)
    submitted_by = pick_engineer(s)
    if submitted_by is None:
        return pause()
    codes = ask_defences()
    live = choose("Run mode", ["DEMO (offline, scripted traces)", "LIVE (real Ollama model)"]) == 2
    if not live and "D1" in codes:
        print("  WARNING: D1 (trust labels) cannot be replayed offline -- the SCRIPTED numbers "
              "ignore it; run LIVE to measure D1")

    print(f"\nRunning {cid}  defences={codes or 'none'}  mode={LIVE if live else SCRIPTED} ...")
    if live:
        from lab import runner
        sample = ask_int("Sample size per split (LIVE is slow)", 8, lo=1)
        # X-05: the first N attacks PLUS the first N benign records, never tests[:N].
        attacks = [t for t in s.test if t["kind"] == "attack"][:sample]
        benign = [t for t in s.test if t["kind"] == "benign"][:sample]
        subset = attacks + benign
        print(f"  LIVE sample: {len(attacks)} attacks + {len(benign)} benign = "
              f"{len(subset) * REPS} runs ({REPS} reps each)")
        print("  (Ctrl-C aborts the sweep and returns to the menu)")
        try:
            traces = _run_live(runner, subset, codes, cid)
        except KeyboardInterrupt:
            print("\nLIVE sweep aborted -- no candidate saved.")
            return pause()
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
                  f"{len(attacks)} attack + {len(benign)} benign tests x {REPS} reps; "
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
    summary["mode"] = mode                 # the manager summary labels each candidate by this

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
    run_menu(s, "\n== Governance (release gate) ==", [
        "Show gate verdict (PASS/FAIL) for a candidate",
        "Make a decision (Approve / Block / Override)",
        "Manage roles (who can do what)",
        "View audit log",
        "Verify audit log (tamper check)",
        "Tamper demo (on a copy)",
        "Back",
    ], {1: gov_verdict, 2: gov_decide, 3: gov_roles, 4: gov_view_log, 5: gov_verify_log,
        6: gov_tamper_demo})


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
    reviewer = _norm(ask("Reviewer name", "sarah"))
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
    action = "add" if act == 1 else "remove"
    actor = _norm(ask("Who is making this change? (must be the rule owner)",
                      s.policy["roles"].get("rule_owner", "")))
    role = _norm(ask("Role group (engineers / reviewers)", "reviewers"))
    person = _norm(ask("Person"))
    if not person:
        return pause()
    before = copy.deepcopy(s.policy["roles"])
    try:
        ev = roles.change_role(s.policy, actor=actor, person=person, role=role, action=action)
    except roles.RoleError as e:
        print(f"\n{e}")
        s.log({"event": "role_refused", "actor": actor, "person": person, "role": role,
               "action": action, "detail": str(e)})
        return pause()
    try:
        s.log(ev)                      # record first (B-21) ...
    except BaseException:
        s.policy["roles"] = before     # ... and undo the unlogged change if that failed
        raise
    s.save_roles()                     # ... then persist
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
        if e.get("malformed"):
            marker += f"  [MALFORMED LINE: {e.get('error', '')}]"
        print(f"  #{e.get('seq', '?'):<3}{str(e.get('time', ''))[:19]:<21}{e.get('event', '?'):<17}"
              f"{e.get('candidate_id', ''):<8}{extra:<9}{who}{marker}")
    pause()


def _print_check(check, label: str) -> None:
    """Print one verify result; names the kind of tampering when the lane provides it."""
    entries = getattr(check, "entries", None)
    print(f"{label}: {check.detail}")
    if check.ok:
        if entries == 0:
            print("  WARNING: 0 entries -- an empty or deleted log also verifies; nothing is proven")
        else:
            print("  Chain intact. ✔")
    else:
        kind = getattr(check, "kind", None)
        print(f"  >>> TAMPERING DETECTED at entry #{check.bad_seq}{f' ({kind})' if kind else ''} <<<")


def gov_verify_log(s: GovState) -> None:
    check = audit.verify_log(s.audit_file)
    print()
    if getattr(check, "entries", None) is None and check.ok and not s.events():
        print(f"real log: {check.detail}")
        print("  WARNING: 0 entries -- an empty or deleted log also verifies; nothing is proven")
    else:
        _print_check(check, "real log")
    pause()


def gov_tamper_demo(s: GovState) -> None:
    """D-05: show tamper detection on a COPY of the log. The real log is never edited."""
    entries = s.events()
    if not entries:
        print("  (audit log empty -- run a candidate first)")
        return pause()
    real_before = s.audit_file.read_bytes()
    copy_path = s.work / "tamper_demo" / "audit_log_copy.jsonl"
    copy_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(s.audit_file, copy_path)

    # Forge the most recent decision (the most tempting target), else entry 1.
    idx = next((i for i in range(len(entries) - 1, -1, -1)
                if entries[i].get("event") == "decision"), 0)
    lines = [ln for ln in copy_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    rec = json.loads(lines[idx])
    if "decision" in rec:
        field, new = "decision", ("APPROVE" if rec["decision"] != "APPROVE" else "BLOCK")
    elif "by" in rec:
        field, new = "by", "mallory"
    else:
        field, new = "event", "forged"
    old = rec[field]
    rec[field] = new
    lines[idx] = json.dumps(rec, sort_keys=True)
    copy_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\nCopied the log to {_rel(copy_path)} and changed entry #{rec.get('seq', idx + 1)} "
          f"field {field!r}: {old!r} -> {new!r}")
    _print_check(audit.verify_log(copy_path), "copy")
    _print_check(audit.verify_log(s.audit_file), "real log")
    if s.audit_file.read_bytes() == real_before:
        print("The real log was not touched (bytes identical). ✔")
    else:  # pragma: no cover - would be a bug in this function
        print(">>> BUG: the real log changed during the demo <<<")
    pause()


# ---------------------------------------------------------------------------
# 3. Assurance.
# ---------------------------------------------------------------------------

ASSURANCE_OPTIONS = [
    "Risk register (optionally vs a prior candidate)",
    "Fix tickets",
    "Control mapping (OWASP / ATLAS / NIST)",
    "Evidence register",
    "Traceability (one finding, start to finish)",
    "Manager summary",
    "AI inventory card",
    "Write ALL reports to markdown files",
    "Back",
]


def menu_assurance(s: GovState) -> None:
    run_menu(s, "\n== Assurance (risk & evidence) ==", ASSURANCE_OPTIONS,
             {c: (lambda s, c=c: assurance_action(s, c)) for c in range(1, 9)})


def assurance_action(s: GovState, c: int) -> None:
    cid = pick_candidate(s)
    if not cid:
        return pause()
    runs = s.load_runs(cid)
    if runs is None:
        print("  (no stored runs for that candidate)")
        return pause()
    rec = s.candidates[cid]
    reg, prior = _register_for(s, cid, runs)
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
        print("\n" + evidence.to_markdown(_evidence_rows(s, cid, runs, reg)))
    elif c == 5:
        assurance_thread(s, cid, runs, reg, prior)
    elif c == 6:
        print("\n" + manager_summary_text(s, cid, reg))
    elif c == 7:
        print("\n" + inventory_text(s, cid))
    elif c == 8:
        write_reports(s, cid, runs, reg)
    pause()


def _kw_unsupported(e: TypeError) -> bool:
    return "unexpected keyword" in str(e)


def _build_register(s: GovState, runs: pd.DataFrame, codes: list[str], prior=None):
    """Risk register; passes the candidate's defences when the assurance lane accepts them."""
    try:
        return risks.build_register(runs, s.policy, s.control_map, prior=prior, defences=codes)
    except TypeError as e:
        if not _kw_unsupported(e):
            raise
        return risks.build_register(runs, s.policy, s.control_map, prior=prior)


def _register_for(s: GovState, cid: str, runs: pd.DataFrame):
    """Build the register, using a chosen prior candidate to close/reopen risks.

    Returns ``(register, prior)``; ``prior`` is ``None`` or a dict with the
    prior candidate's ``id``, ``runs`` and ``register`` (the "before" story).
    """
    prior_id = ask("Compare against prior candidate for status? (id or blank)", "")
    prior = None
    if prior_id:
        prior_runs = s.load_runs(prior_id) if prior_id in s.candidates else None
        if prior_runs is None:
            print(f"  (no stored runs for {prior_id!r}; building without a prior)")
        else:
            prior_reg = _build_register(s, prior_runs, s.candidates[prior_id].get("defences", []))
            prior = {"id": prior_id, "runs": prior_runs, "register": prior_reg}
    reg = _build_register(s, runs, s.candidates[cid].get("defences", []),
                          prior=risks.prior_from(prior["register"]) if prior else None)
    return reg, prior


def assurance_thread(s: GovState, cid: str, runs: pd.DataFrame, reg, prior=None) -> None:
    """X-06: the story starts at the PRIOR candidate's runs and tickets, as the CLI does."""
    before_runs = prior["runs"] if prior else runs
    tickets = risks.fix_tickets(prior["register"] if prior else reg)
    attacks = before_runs[(before_runs["kind"] == "attack") & before_runs["violated"]]
    leaked = attacks[attacks["violation_classes"].str.contains("V_exfiltration", na=False)]
    pick = leaked if not leaked.empty else attacks          # the CLI's choice: a leak first
    default = pick.iloc[0]["test_id"] if not pick.empty else "ATK-0021"
    finding = ask("Finding id", default)
    if prior:
        print(f"  (before: {prior['id']}'s runs and fix tickets; re-test: {cid})")
    else:
        print("  (no prior chosen: the story starts and ends at this candidate; "
              "choose a prior to see the finding, the fix ticket and the re-test)")
    tr = thread.build_thread(finding, before_runs, reg, tickets, s.events(), retest_candidate=cid)
    print("\n" + thread.to_markdown(tr))


def _live_version(events: list[dict]) -> str | None:
    """The candidate the record says is live (the governance lane's rule when present)."""
    fn = getattr(summary_page, "live_version", None)
    if fn is not None:
        return fn(events)
    approvals = [e for e in events if e.get("decision") in (decision.APPROVE, decision.OVERRIDE)]
    return approvals[-1]["candidate_id"] if approvals else None


def _log_head(s: GovState) -> tuple[int, str | None]:
    fn = getattr(audit, "head", None)
    if fn is not None:
        return fn(s.audit_file)
    entries = s.events()
    return len(entries), (entries[-1].get("hash") if entries else None)


def _evidence_line(cid: str, rec: dict) -> str:
    sm = rec.get("summary") or {}
    if not sm:
        return f"{cid}: no numbers recorded"
    return (f"{cid} ASR {sm['asr']:.1%} ({sm['attack_violations']}/{sm['attack_runs']}), "
            f"95% CI [{sm['asr_lo']:.1%}, {sm['asr_hi']:.1%}], benign {sm['benign_rate']:.0%} "
            f"({sm['benign_n']} runs) {mode_label(rec)} -- {rec.get('source') or 'source unknown'}")


def manager_summary_text(s: GovState, cid: str, reg) -> str:
    """The one-page view. The attack rate is the LIVE version's own (X-07), the
    open risks are the viewed candidate's, and every number carries its mode."""
    rec = s.candidates[cid]
    events = s.events()
    live = _live_version(events)
    live_rec = s.candidates.get(live) if live else None
    live_asr = live_rec["summary"]["asr"] if live_rec and live_rec.get("summary") else None
    summaries = {c: dict(r["summary"], mode=mode_of(r))
                 for c, r in s.candidates.items() if r.get("summary")}
    try:
        sm = summary_page.build_summary(reg, events, current_asr=live_asr, summaries=summaries,
                                        current_candidate=cid, log_head=_log_head(s))
    except TypeError as e:
        if not _kw_unsupported(e):
            raise
        sm = summary_page.build_summary(reg, events, current_asr=live_asr)

    lines = []
    for ln in summary_page.to_markdown(sm).splitlines():
        if "attack rate" in ln.lower():
            lines.append(tag_numbers(ln, live_rec) if live_rec else f"{ln}  (no approved version)")
        else:
            lines.append(tag_numbers(ln, rec))
    if live_rec:
        lines.append(f"- **Live version evidence:** {_evidence_line(live, live_rec)}")
    elif live:
        lines.append(f"- **Live version evidence:** {live} has no record in this workspace -- rate n/a")
    else:
        lines.append("- **Live version evidence:** none approved")
    lines.append(f"- **Viewed candidate:** {_evidence_line(cid, rec)}")
    return "\n".join(lines)


def _control_results(s: GovState, runs: pd.DataFrame, reg, codes: list[str]) -> dict[str, str]:
    """Measured result per control named in the register (never 'assumed proven')."""
    controls = sorted({r.control for r in reg if r.control})
    fn = getattr(evidence, "control_results", None)
    if fn is not None:
        try:
            return fn(runs, controls, defences=codes)
        except TypeError as e:
            if not _kw_unsupported(e):
                raise
            return fn(runs, controls)
    attacks = runs[(runs["kind"] == "attack") & (~runs["error"].astype(bool))]
    out: dict[str, str] = {}
    for code in controls:
        classes = [cls for cls in risks.RISK_CATALOG if risks.control_for_class(cls) == code]
        hits = int(attacks["violation_classes"].apply(
            lambda cell: bool(set(str(cell).split(";")) & set(classes))).sum())
        state = "control ON" if code in codes else "control NOT in this candidate's defences"
        out[code] = f"{hits} of {len(attacks)} attack runs landed {'/'.join(classes)} ({state})"
    return out


def _evidence_rows(s: GovState, cid: str, runs: pd.DataFrame, reg):
    res = _control_results(s, runs, reg, s.candidates[cid].get("defences", []))
    proof = _rel(s.work / cid / "runs.csv")
    try:
        return evidence.build_register(reg, retest_candidate=cid, results=res, proof_file=proof)
    except TypeError as e:
        if not _kw_unsupported(e):
            raise
        rows = evidence.build_register(reg, retest_candidate=cid, results=res)
        for row in rows:
            row.proof_file = proof          # X-03: point at the file the app really writes
        return rows


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
        "evidence_register.md": evidence.to_markdown(_evidence_rows(s, cid, runs, reg)),
        "summary.md": manager_summary_text(s, cid, reg),
    }
    for name, text in files.items():
        (out / name).write_text(banner + text + "\n", encoding="utf-8")
    print(f"\nWrote {', '.join(files)} to {_rel(out)}/")


# ---------------------------------------------------------------------------
# Main loop.
# ---------------------------------------------------------------------------

def main(work: Path | None = None) -> None:
    try:
        s = GovState(work)
    except PolicyError as e:
        print(f"cannot start: policy.yaml is invalid -- {e}")
        return
    print("=" * 64)
    print(" Agent Release Gate  --  sandbox lab, all data synthetic")
    print(f" workspace: {_rel(s.work)}/   test split: {len(s.test)} records")
    print("=" * 64)
    while True:
        try:
            c = choose("\n== MAIN MENU ==", [
                "Red team  (test a candidate agent)",
                "Governance  (gate, decisions, roles, audit log)",
                "Assurance  (risks, controls, evidence, traceability)",
                "Quit",
            ])
        except Cancelled:
            print("bye.")
            return
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
