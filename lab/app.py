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
import sys
import unicodedata
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


def clean_input(text: str) -> tuple[str, int]:
    """Drop characters nobody meant to type; return (clean text, how many were dropped).

    A keyboard IME (e.g. Vietnamese Telex turning "es" into "é" and back) can leave
    half of a UTF-8 character on stdin. Python surfaces that byte as a lone
    surrogate such as '\\udcc3', invisible on screen, so "es-003" stops matching a
    real candidate id. Surrogates, control and invisible format characters (e.g.
    zero-width spaces) and U+FFFD are removed; real letters such as "é" are kept.
    """
    kept = [ch for ch in text
            if ch != "�" and unicodedata.category(ch) not in ("Cs", "Cc", "Cf")]
    return unicodedata.normalize("NFC", "".join(kept)).strip(), len(text) - len(kept)


def ask(prompt: str, default: str = "") -> str:
    """One prompt. Ctrl-C or EOF raises :class:`Cancelled`, never a default.

    A closed stdin (Ctrl-D, end of piped input) cancels the current action;
    every later prompt hits EOF too, so the menus unwind and the app ends with
    "bye." instead of looping on "(not a valid choice)".
    """
    suffix = f" [{default}]" if default else ""
    try:
        got, dropped = clean_input(input(f"{prompt}{suffix}: "))
    except (EOFError, KeyboardInterrupt):
        print("\n(cancelled)")
        raise Cancelled()
    if dropped:
        print(f"  (ignored {dropped} unreadable character(s) in your input; read it as {got!r})")
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

def ensure_corpus() -> None:
    """Check, read-only, that the committed split equals the generator (B-31, D-21).

    The app never writes corpus/*.jsonl, not even when a file is missing:
    rewriting the tracked corpus dirtied the checkout, raced a CLI run started
    at the same time, and on a different Python could silently change every
    fingerprint's test_set hash. Raises :class:`corpus.CorpusMismatch` (a
    missing file counts as a mismatch); :func:`main` shows it and stops.
    Regenerating is a deliberate ``python -m lab.corpus``.
    """
    corpus.check_matches_generator()


def _load_state_json(path: Path) -> dict:
    """Read a workspace JSON file; on corruption, stop with a clear message.

    Never repairs or replaces the file: it may be evidence, so a person decides.
    """
    folder = path.parent.name
    name = f"{folder}/{path.name}"
    fix = f"Restore it from a backup, or delete {folder}/ to start a fresh workspace."
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise SystemExit(f"ERROR: {name} is not valid JSON ({e}).\n{fix}") from None
    if not isinstance(data, dict):
        raise SystemExit(f"ERROR: {name} should hold a JSON object, not {type(data).__name__}.\n{fix}")
    return data


def _evidence_exists(work: Path, candidates: dict, cid: str) -> bool:
    """``cid`` is in the record, or evidence is already on disk under that id."""
    return (cid in candidates or (work / cid).exists()
            or (work / "candidates" / f"{cid}.json").exists())


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

        ensure_corpus()
        self.test = corpus.read_jsonl(corpus.TEST_PATH)
        self.tests_by_id = {t["id"]: t for t in self.test}

        self.candidates: dict[str, dict] = (
            _load_state_json(self.cand_file) if self.cand_file.exists() else {}
        )
        self._refresh_legacy_summaries()

    # Keys the current gate reads that older records (before fix 3) lack.
    SUMMARY_KEYS = ("runs", "attempted_rate")

    def _refresh_legacy_summaries(self) -> None:
        """C-09: re-summarise old records from their traces so they gate on today's keys."""
        changed = False
        for cid, rec in self.candidates.items():
            sm = rec.get("summary") or {}
            if all(k in sm for k in self.SUMMARY_KEYS):
                continue
            runs = self.load_runs(cid)
            if runs is None or runs.empty:
                rec["legacy"] = "no traces on disk to re-summarise; record predates the current gate keys"
                continue
            fresh = gate.summarise(runs)
            fresh["mode"] = mode_of(rec)
            rec["summary"] = fresh
            rec.pop("legacy", None)
            rec["resummarised_at"] = datetime.now(timezone.utc).isoformat()
            print(f"  (re-summarised {cid} from its stored traces: the record predates the current gate keys)")
            changed = True
        if changed:
            self.save_candidates()

    # -- roles overlay (so edits survive without clobbering the commented yaml)
    def _apply_roles_overlay(self) -> None:
        if self.roles_file.exists():
            self.policy["roles"].update(_load_state_json(self.roles_file))

    def save_roles(self) -> None:
        self.roles_file.write_text(json.dumps(self.policy["roles"], indent=2, sort_keys=True))

    def save_candidates(self) -> None:
        self.cand_file.write_text(json.dumps(self.candidates, indent=2, sort_keys=True))

    # -- candidate helpers
    def next_candidate_id(self) -> str:
        n = len(self.candidates) + 1
        while self.candidate_exists(f"RC-{n:03d}"):
            n += 1
        return f"RC-{n:03d}"

    def candidate_exists(self, cid: str) -> bool:
        """Known in the record, or evidence already on disk under that id."""
        return _evidence_exists(self.work, self.candidates, cid)

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

    def require_intact_log(self) -> None:
        """Raise :class:`AuditError` now if the audit log is damaged, before an
        action writes anything it could then not log (no unlogged submissions,
        decisions or role changes). Checks the chain and its head anchor."""
        check = audit.verify_log(self.audit_file)
        if not check.ok:
            kind = getattr(check, "kind", None) or "damaged"
            where = f" at entry #{check.bad_seq}" if check.bad_seq is not None else ""
            raise AuditError(
                f"{_rel(self.audit_file)} is not intact ({kind}{where}: {check.detail}). "
                "Nothing more can be logged until it is restored; "
                "Governance > Verify audit log shows the damage"
            )


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


def _id_problem(s: GovState, cid: str) -> str | None:
    """Why ``cid`` cannot be a new candidate id, or ``None`` (B-20, D-06)."""
    if not CANDIDATE_ID_RE.match(cid):
        return f"{cid!r} is not a candidate id; use the form RC-001"
    if _evidence_exists(s.work, s.candidates, cid):
        return f"{cid} already exists and its evidence is kept; next free id is {s.next_candidate_id()}"
    return None


def _defence_problem(codes: list[str]) -> str | None:
    """Why ``codes`` are not all known defence codes, or ``None`` (B-19)."""
    valid = list(defences.ALL_DEFENCES)
    bad = [c for c in codes if c not in valid]
    if bad:
        return f"unknown defence code(s): {', '.join(bad)}; valid: {', '.join(valid)}"
    return None


def ask_candidate_id(s: GovState) -> str:
    """A fresh id of the form RC-NNN; an existing id is refused (B-20, D-06)."""
    while True:
        cid = ask("Candidate id", s.next_candidate_id())
        problem = _id_problem(s, cid)
        if problem:
            print(f"  ({problem})")
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
    while True:
        raw = ask("Defences ON (comma, e.g. D2 or D1,D2,D3; blank = none)", "")
        codes = sorted({d.strip().upper() for d in raw.split(",") if d.strip()})
        problem = _defence_problem(codes)
        if problem:
            print(f"  ({problem})")
            continue
        return codes


LIVE_DEFAULT_SAMPLE = 8


def parse_sample(raw: str, default: int = LIVE_DEFAULT_SAMPLE) -> tuple[int, str]:
    """LIVE sample size from user text -> (n, note). Bad input falls back to ``default``.

    A pure helper (from PR #2). The menu itself re-asks on bad input
    (:func:`ask_int`), so a typo never silently starts a slow LIVE sweep.
    """
    try:
        n = int(raw)
    except ValueError:
        return default, f"  ({raw!r} is not a whole number; using {default})"
    if n < 1:
        return default, f"  (sample size must be at least 1; using {default})"
    return n, ""


def live_subset(tests: list[dict], sample: int) -> list[dict]:
    """The first ``sample`` attacks PLUS the first ``sample`` benign tests (X-05).

    Never ``tests[:N]``, and no test id twice even when ``sample`` is larger
    than either list (PR #2: a sample above 26 duplicated benign tests).
    """
    attacks = [t for t in tests if t["kind"] == "attack"]
    benign = [t for t in tests if t["kind"] == "benign"]
    return attacks[:sample] + benign[:sample]


def _refuse_new_candidate(s: GovState, cid: str, submitted_by: str, codes: list[str]) -> str | None:
    """Why a new candidate submission is refused, or ``None`` if it may run.

    The last check before a run, on the same rules the prompts re-ask on: a
    fresh RC-NNN id, a submitter from ``roles.engineers``, known defence codes.
    """
    problem = _id_problem(s, cid)
    if problem:
        return f"REFUSED: {problem}"
    engineers = sorted({_norm(e) for e in s.policy["roles"].get("engineers", []) if _norm(e)})
    if _norm(submitted_by) not in engineers:
        return f"REFUSED: {submitted_by} is not an engineer (engineers: {', '.join(engineers) or 'none'})"
    problem = _defence_problem(codes)
    if problem:
        return f"REFUSED: {problem}"
    return None


def redteam_run(s: GovState) -> None:
    cid = ask_candidate_id(s)
    submitted_by = pick_engineer(s)
    if submitted_by is None:
        return pause()
    codes = ask_defences()
    refusal = _refuse_new_candidate(s, cid, submitted_by, codes)
    if refusal:
        print(f"\n{refusal}")
        return pause()
    s.require_intact_log()  # a damaged log blocks the submit before anything is run or written

    mode = -1
    while mode == -1:
        mode = choose("Run mode", ["DEMO (offline, scripted traces)", "LIVE (real Ollama model)"])
    live = mode == 2
    if not live and "D1" in codes:
        print("  WARNING: D1 (trust labels) cannot be replayed offline -- the SCRIPTED numbers "
              "ignore it; run LIVE to measure D1")

    print(f"\nRunning {cid}  defences={codes or 'none'}  mode={LIVE if live else SCRIPTED} ...")
    if live:
        from lab import runner
        min_runs = s.policy["release_limits"]["min_attack_runs"]
        need = -(-min_runs // REPS)  # ceil
        print(f"  (the gate needs {min_runs} attack runs: use a sample of {need} or more, "
              f"at {REPS} reps each)")
        sample = ask_int("Sample size per split (LIVE is slow)", LIVE_DEFAULT_SAMPLE, lo=1)
        subset = live_subset(s.test, sample)
        attacks = [t for t in subset if t["kind"] == "attack"]
        benign = [t for t in subset if t["kind"] == "benign"]
        if len(attacks) * REPS < min_runs:
            print(f"  (note: {len(attacks) * REPS} attack runs is under {min_runs}; "
                  f"this run will fail the min_attack_runs check)")
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
        # The fingerprint covers what was actually run: this sample, not the whole file.
        test_set_text = "".join(json.dumps(t) + "\n" for t in subset)
    else:
        traces = demo.demo_traces(s.test, reps=REPS)
        if codes:
            traces = defences.replay(traces, s.rules, codes)
        digest = SCRIPTED_DIGEST
        mode, source = SCRIPTED, SCRIPTED_SOURCE
        test_set_text = corpus.TEST_PATH.read_text()

    _save_candidate(s, cid, submitted_by, codes, traces, digest, mode, source, test_set_text)
    rec = s.candidates[cid]
    print(f"\n{result_line(cid, rec)}")
    print(source_line(rec))
    print(f"  evidence in {_rel(s.work / cid)}/")
    pause()


def _save_candidate(s: GovState, cid: str, submitted_by: str, codes: list[str],
                    traces: list[dict], digest: str, mode: str, source: str,
                    test_set_text: str) -> None:
    """Persist traces + runs.csv, fingerprint and summary; log the submit."""
    runs = metrics.build_runs_frame(traces, s.rules, s.tests_by_id)
    # C-07: no phantom results/traces/... path; a trace's own path only if it has one.
    runs["trace_path"] = [tr.get("trace_path", "") or "" for tr in traces]
    (s.work / cid).mkdir(parents=True, exist_ok=True)
    s.traces_path(cid).write_text(json.dumps(traces, indent=2, sort_keys=True))
    metrics.write_outputs(runs, s.work / cid / "summary.csv", s.work / cid / "runs.csv")  # sorted
    summary = gate.summarise(runs)
    summary["mode"] = mode                 # the manager summary labels each candidate by this

    fp = candidate.fingerprint(
        cid, submitted_by=submitted_by, model=agent.MODEL, model_digest=digest,
        system_prompt=agent.SYSTEM_PROMPT, tools=TOOLS, defences=codes,
        rules_text=world.RULES_PATH.read_text(), test_set_text=test_set_text,
    )
    candidate.save(fp, s.work / "candidates")

    s.candidates[cid] = {
        "submitted_by": submitted_by, "defences": codes, "mode": mode, "source": source,
        "identity_hash": fp.identity_hash(), "test_set_sha256": fp.test_set_sha256,
        "reps": REPS, "tests_run": len({t["test_id"] for t in traces}), "summary": summary,
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


def _test_set_sha(s: GovState, cid: str) -> str | None:
    rec = s.candidates[cid]
    if rec.get("test_set_sha256"):
        return rec["test_set_sha256"]
    p = s.fingerprint_path(cid)                 # records written before this field existed
    return candidate.load(p).test_set_sha256 if p.exists() else None


def _comparable(s: GovState, cid: str, prior_cid: str) -> list[str]:
    """What differs between two candidates for the no-regression check (empty = comparable)."""
    a, b = s.candidates[cid], s.candidates[prior_cid]
    diffs = []
    if a.get("legacy") or b.get("legacy"):
        diffs.append("legacy record without traces")
    if _test_set_sha(s, cid) is None or _test_set_sha(s, cid) != _test_set_sha(s, prior_cid):
        diffs.append("test set")
    if a.get("reps") is None or a.get("reps") != b.get("reps"):
        diffs.append("reps")
    expected = lambda r: (r.get("tests_run") or 0) * (r.get("reps") or 0)   # noqa: E731
    if not expected(a) or expected(a) != expected(b):
        diffs.append("run count")
    if mode_of(a) != mode_of(b):
        diffs.append("mode")
    return diffs


def _verdict_for(s: GovState, cid: str):
    """Gate verdict plus a one-line note on the no-regression comparison.

    The ceiling applies only when the last approved candidate ran the same
    test set, the same reps and the same mode; otherwise it is skipped and
    said so on screen (never a silent pass).
    """
    rec = s.candidates[cid]
    summary = rec["summary"]
    prior = s.last_approved()
    last_asr, note = None, "no-regression check: no previously approved candidate"
    if rec.get("legacy"):
        # Older record, no traces: gate on safe defaults (an absent count is 0,
        # which the gate reports as "not measured"), and say it is not comparable.
        summary = {"runs": 0, "attempted_runs": 0, "errors": 0, **summary}
        note = (f"legacy record: {rec['legacy']} -- verdict not comparable to current "
                f"candidates; re-run it to get measured numbers")
    elif prior and prior[0] != cid:
        diffs = _comparable(s, cid, prior[0])
        if diffs:
            note = (f"no-regression check SKIPPED: last approved {prior[0]} "
                    f"{mode_label(s.candidates[prior[0]])} is not comparable "
                    f"(different {' / '.join(diffs)})")
        else:
            last_asr = prior[1]
            note = (f"no-regression check against last approved {prior[0]} "
                    f"{mode_label(s.candidates[prior[0]])} (same test set, reps and mode)")
    return gate.evaluate(summary, s.policy, last_approved_asr=last_asr), note


def gov_verdict(s: GovState) -> None:
    cid = pick_candidate(s)
    if not cid:
        return pause()
    rec = s.candidates[cid]
    verdict, note = _verdict_for(s, cid)
    print()
    print(tag_numbers(gate.report_text(cid, verdict), rec))
    print(f"  {note}")
    print(source_line(rec))
    pause()


def gov_decide(s: GovState) -> None:
    s.require_intact_log()
    cid = pick_candidate(s)
    if not cid:
        return pause()
    rec = s.candidates[cid]
    verdict, note = _verdict_for(s, cid)
    print(f"\n{cid} verdict: {verdict.status} {mode_label(rec)}  (submitted by {rec['submitted_by']})")
    print(f"  {note}")
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
    s.require_intact_log()
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
        truncated = getattr(check, "truncated_after", None)
        if truncated is not None:
            where = f": log truncated after entry {truncated}"
        elif check.bad_seq is not None:
            where = f" at entry #{check.bad_seq}"
        else:
            where = ""
        print(f"  >>> TAMPERING DETECTED{where}{f' ({kind})' if kind else ''} <<<")


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
    An id that is not a candidate is asked again (a typo must not silently
    drop the comparison); a prior newer than ``cid`` is warned about.
    """
    prior = None
    while True:
        prior_id = ask("Compare against prior candidate for status? (id or blank)", "")
        if prior_id and prior_id not in s.candidates:
            print(f"  no such candidate {prior_id!r} -- enter a listed id, or leave blank for no comparison")
            continue
        break
    if prior_id:
        prior_runs = s.load_runs(prior_id)
        if prior_runs is None:
            print(f"  (no stored runs for {prior_id!r}; building without a prior)")
        else:
            prior_reg = _build_register(s, prior_runs, s.candidates[prior_id].get("defences", []))
            prior = {"id": prior_id, "runs": prior_runs, "register": prior_reg}
            newer = thread.newer_prior_warning(prior_id, s.candidates[prior_id], cid, s.candidates[cid])
            if newer:
                print(f"  {newer}")
    reg = _build_register(s, runs, s.candidates[cid].get("defences", []),
                          prior=risks.prior_from(prior["register"]) if prior else None)
    return reg, prior


def _default_finding(runs: pd.DataFrame | None) -> str | None:
    """The first landed attack in ``runs``, a leak first (the CLI's choice)."""
    if runs is None:
        return None
    attacks = runs[(runs["kind"] == "attack") & runs["violated"]]
    leaked = attacks[attacks["violation_classes"].str.contains("V_exfiltration", na=False)]
    pick = leaked if not leaked.empty else attacks
    return pick.iloc[0]["test_id"] if not pick.empty else None


def assurance_thread(s: GovState, cid: str, runs: pd.DataFrame, reg, prior=None) -> None:
    """X-06: the story starts where the finding landed -- the PRIOR candidate's
    runs and tickets when it landed there, as the CLI does -- and ends at this
    candidate's re-test (:func:`thread.trace_finding`, with its evidence warnings).

    Status comes from ``reg``, the same register the risk screen shows (built
    with this candidate's defences and the prior).
    """
    before_runs = prior["runs"] if prior else None
    default = _default_finding(before_runs) or _default_finding(runs)
    if default is None:
        print(f"  (no attack landed in {prior['id'] + ' or ' if prior else ''}{cid})")
    finding = ask("Finding id", default or "ATK-0021")
    if prior:
        print(f"  (before: {prior['id']}'s runs and fix tickets; re-test: {cid})")
    else:
        print("  (no prior chosen: the story starts and ends at this candidate; "
              "choose a prior to see the finding, the fix ticket and the re-test)")
    tr = thread.trace_finding(
        finding, candidate_id=cid, candidate_runs=runs, candidate=s.candidates[cid],
        policy=s.policy, control_map=s.control_map, audit_events=s.events(),
        prior_id=prior["id"] if prior else None, prior_runs=before_runs,
        prior=s.candidates.get(prior["id"]) if prior else None,
        register=reg, prior_register=prior["register"] if prior else None,
    )
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
        # The current rate is the live version's, so it is labelled with the live
        # id and CI (PR #2), never the viewed candidate's; the open risks are cid's.
        sm = summary_page.build_summary(reg, events, current_asr=live_asr, summaries=summaries,
                                        current_candidate=live if live_asr is not None else None,
                                        register_candidate=cid, log_head=_log_head(s))
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

def _windows_safe_stdout() -> None:
    """Never crash on a character the console cannot show (e.g. the check mark
    on a cp1252 Windows console when output is piped or redirected)."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):  # a stream that cannot be reconfigured
            pass


def main(work: Path | None = None) -> None:
    _windows_safe_stdout()
    try:
        s = GovState(work)
    except PolicyError as e:
        print(f"cannot start: policy.yaml is invalid -- {e}")
        return
    except corpus.CorpusMismatch as e:
        print(f"cannot start: {' '.join(str(e).split())}")
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
