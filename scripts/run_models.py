#!/usr/bin/env python3
"""Run several Ollama models through the Redline governance pipeline, in one batch.

For each model listed in ``config/models.txt`` the batch does, in order:

    1. (--pull)   ollama pull <tag>
    2. preflight  scripts/preflight.py
    3. smoke      scripts/run_governance.py --live --sample 1 --reps 1
                  --out results/governance/<STAMP>_<slug>_smoke
    4. tool-support check on the smoke's new run folders (see below)
    5. full       scripts/run_governance.py --live --sample 26 --reps 3
                  --out results/governance/<STAMP>_<slug>

A model without tool support is skipped after the smoke run, and says why. Without
that check, a model that cannot call tools would read as a "safe" 0% attack rate.

Tool support is "yes" only when ALL of these hold for the smoke's run folders:
    * at least one trace holds a structured tool call,
    * no ``final_text`` contains the text ``<tool_call>`` (the model wrote its calls as text),
    * not every run errored.
Otherwise it is "no", with the reason. SCRIPTED runs read "not checked (scripted)".

Usage (from the repo root; on Windows PowerShell use .venv\\Scripts\\python.exe):

    python scripts/run_models.py --dry-run                # print the plan, run nothing
    python scripts/run_models.py --smoke-only             # preflight + smoke for every model
    python scripts/run_models.py --parallel 2             # smoke, check, then the full run
    python scripts/run_models.py --only qwen2.5-7b-instruct --pull
    python scripts/run_models.py --scripted --parallel 2  # DEMO rehearsal: no model, no Ollama

Outputs (``<STAMP>`` is the UTC start time, ``<slug>`` is the model tag as lab.run_folder._safe writes it):

    results/models/<STAMP>/plan.json         the plan: models, commands, paths (no absolute paths)
    results/models/<STAMP>/status.jsonl      one JSON line per state change (events below)
    results/models/<STAMP>/<slug>.<step>.log output of each child (pull, preflight, smoke, full)
    results/governance/<STAMP>_<slug>[_smoke]  the governance output of each child
    results/runs/<run_id>/                   the locked run folders (written by the child)

status.jsonl: every line has ``ts`` (UTC), ``event``, ``slug``, ``tag``, plus these fields.
``out``, ``log`` and ``run_dirs`` are relative to the repo (``run_dirs`` are names inside results/runs).

    queued               position
    pull_start           log
    pull_done            exit_code (None: ollama could not start, see error)
    preflight_start      log
    preflight_ok         exit_code
    preflight_fail       exit_code, detail (the FAIL lines of preflight.py)
    preflight_skipped    reason ("scripted")
    smoke_start          out, log
    smoke_done           exit_code, tools (yes | no | not checked), reason, text, out, run_dirs, detail
                         text = "<tools> (<reason>)", e.g. "no (every run errored)" or "not checked (scripted)"
    full_start           out, log
    full_done            exit_code, out, run_dirs (+ reason "interrupted")
    skipped              reason. The full run will not happen. It is the last line of every model
                         that did not reach full_done (no tool support, a failed step, --smoke-only, Ctrl-C).

Concurrency: ``--parallel N`` (default 2, at most 4) runs N models at once on a thread pool, one child
process at a time per model. The same model never runs twice at once. N is forced to 1 until the corpus
race fix is in scripts/run_governance.py. Without the fix every run rewrites corpus/*.jsonl at its start
(``corpus.build_and_write()``), and two runs at once would write the same files at the same time. The fix
replaces that call by a read-only check (``corpus.check_matches_generator()``). The script reads the
file with the ``ast`` module and looks at calls only: the fix is there when the code calls
check_matches_generator and does not call build_and_write. A comment, a docstring or a string that names
build_and_write does not count. A file that cannot be read or parsed counts as "no fix", with a warning.

Stopping: Ctrl-C or SIGTERM stops the batch. Each running child gets exactly one interrupt, so its run
folders still lock, and the queued models are skipped. A second Ctrl-C, or 60 s without an exit, ends the
children at once; any run folder left unlocked is listed. Each child has its own process group (Windows)
or session (POSIX), so a Ctrl-C typed in the terminal reaches only this script, which passes it on once.

Exit code: 0 all went as planned (a model skipped for missing tool support is a result, not a failure);
1 a step failed (preflight, pull, smoke or full); 2 bad command line or setup; 130 stopped.

Not verified: how OLLAMA_NUM_PARALLEL and OLLAMA_MAX_LOADED_MODELS behave when two models run. Check
with ``ollama ps`` while the batch runs. Not verified on Windows: CTRL_BREAK_EVENT as the stop signal,
taskkill for the hard stop. A child ends cleanly on a break only if it handles SIGBREAK.

Standard library only; Python 3.9 or newer; no shell is ever used (argument lists, ``shell=False``).
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS = "config/models.txt"

PLAN_SCHEMA = "run-models/1"
SMOKE_SAMPLE, SMOKE_REPS = 1, 1       # the smoke run: 1 attack + 1 benign test, 1 rep
FULL_SAMPLE, FULL_REPS = 26, 3        # the full run (overridable with --sample / --reps)
DEFAULT_PARALLEL, MAX_PARALLEL = 2, 4
# The corpus race: while run_governance.py calls build_and_write, every run rewrites corpus/*.jsonl at its
# start, so two runs at once are not safe. The fix replaces that call by the read-only check_matches_generator.
RACE_REWRITE_CALL = "build_and_write"
RACE_CHECK_CALL = "check_matches_generator"
OLLAMA_CMD = ["ollama"]               # tests point this at a stand-in
GRACE_S = 60.0                        # after Ctrl-C: how long a child gets to lock its run folders
TEXT_TOOL_CALL = "<tool_call>"        # a model that writes its calls as text puts this in final_text

# An Ollama reference: [host/][namespace/]name[:tag][@digest]. ASCII only, and it must start with a
# letter or digit, so a tag can never be read as a command-line option.
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]*$")


class ModelsFileError(ValueError):
    """config/models.txt is missing, empty, or has a bad line."""


@dataclass(frozen=True)
class Model:
    tag: str    # the Ollama tag, exactly as listed (it becomes LAB_MODEL)
    slug: str   # the filesystem-safe form used in folder and file names


def safe_slug(text: str) -> str:
    """The slug the run folders use: a copy of ``lab.run_folder._safe``.

    Copied, not imported, so this script needs nothing but the standard library (``--dry-run`` works in
    any Python 3.9+). tests/test_run_models.py pins it to the original.
    """
    return "".join(c if (c.isalnum() or c in "._-") else "-" for c in str(text)) or "x"


def read_models(path: Path) -> List[Model]:
    """Read the models file: one tag per line, ``#`` starts a comment, blank lines are fine.

    Raises :class:`ModelsFileError` for a missing or empty file, a bad tag, a duplicate tag, or two tags
    that would share one slug (they would write to the same folders).
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")  # -sig: drop the BOM that Windows editors add
    except FileNotFoundError:
        raise ModelsFileError(f"models file not found: {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ModelsFileError(f"cannot read models file {path}: {exc}") from None

    models: List[Model] = []
    first_line = {}   # tag -> line number
    slug_owner = {}   # slug -> (tag, line number)
    for number, raw in enumerate(text.splitlines(), start=1):
        tag = raw.split("#", 1)[0].strip()
        if not tag:
            continue
        if not _TAG_RE.match(tag):
            raise ModelsFileError(
                f"{path.name}: line {number}: {tag!r} is not a valid Ollama tag "
                "(one tag per line; letters, digits and . _ - : / @ only)"
            )
        if tag in first_line:
            raise ModelsFileError(
                f"{path.name}: line {number}: duplicate tag {tag!r} (first listed on line {first_line[tag]})"
            )
        slug = safe_slug(tag)
        if slug in slug_owner:
            other, other_line = slug_owner[slug]
            raise ModelsFileError(
                f"{path.name}: line {number}: tag {tag!r} gives the same slug {slug!r} as {other!r} "
                f"on line {other_line}; both would write to the same folders"
            )
        first_line[tag] = number
        slug_owner[slug] = (tag, number)
        models.append(Model(tag=tag, slug=slug))
    if not models:
        raise ModelsFileError(f"{path.name}: no models listed (add one Ollama tag per line)")
    return models


class UsageError(Exception):
    """A bad command line or setup (exit code 2); the message says what to change."""


def select_models(models: Sequence[Model], only: Sequence[str]) -> List[Model]:
    """Keep the models named by ``--only`` (by slug or by tag), in file order."""
    if not only:
        return list(models)
    chosen = [m for m in models if m.slug in only or m.tag in only]
    known = {x for m in chosen for x in (m.slug, m.tag)}
    missing = [w for w in only if w not in known]
    if missing:
        raise UsageError(
            f"--only {', '.join(repr(w) for w in missing)}: not in the models file. "
            f"Choose from: {', '.join(m.slug for m in models)}"
        )
    return chosen


# --------------------------------------------------------------------------- #
# Command line, paths, and the plan.
# --------------------------------------------------------------------------- #
def _positive_int(text: str) -> int:
    try:
        number = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def _parallel_int(text: str) -> int:
    number = _positive_int(text)
    if number > MAX_PARALLEL:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_PARALLEL}")
    return number


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_models.py",
        description="Run several Ollama models through the Redline governance pipeline "
                    "(preflight, smoke run, tool-support check, full run). See the top of this file.",
    )
    p.add_argument("--models", default=DEFAULT_MODELS, metavar="FILE",
                   help="one Ollama tag per line, # for comments; a relative path is relative to the repo "
                        "(default: %(default)s)")
    p.add_argument("--parallel", type=_parallel_int, default=DEFAULT_PARALLEL, metavar="N",
                   help=f"models run at once, 1 to {MAX_PARALLEL} (default: %(default)s; forced to 1 while "
                        "the corpus race fix is absent)")
    p.add_argument("--scripted", action="store_true",
                   help="DEMO: run without --live (no model, no Ollama, no preflight); for rehearsal and tests")
    p.add_argument("--smoke-only", action="store_true", help="stop after the smoke run and the tool-support check")
    p.add_argument("--only", action="append", default=[], metavar="SLUG",
                   help="run only this model (slug or tag); repeat the flag for more")
    p.add_argument("--dry-run", action="store_true", help="print the plan, run nothing, write nothing")
    p.add_argument("--pull", action="store_true", help="run 'ollama pull <tag>' for each model first")
    p.add_argument("--sample", type=_positive_int, default=FULL_SAMPLE, metavar="N",
                   help="full run: tests per kind (default: %(default)s); the smoke run always uses "
                        f"{SMOKE_SAMPLE}")
    p.add_argument("--reps", type=_positive_int, default=FULL_REPS, metavar="N",
                   help="full run: repetitions per test (default: %(default)s); the smoke run always uses "
                        f"{SMOKE_REPS}")
    p.add_argument("--python", default=None, metavar="PATH",
                   help="interpreter for the child scripts (default: the one running this script)")
    return p


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


@dataclass(frozen=True)
class Layout:
    """Where things are. Tests point ``results`` at a tmp folder; normally it is ``<root>/results``."""

    root: Path      # the repo: scripts/ and config/ live here; children run with it as their cwd
    results: Path   # governance and batch output go under here

    @property
    def models_dir(self) -> Path:
        return self.results / "models"

    @property
    def gov_dir(self) -> Path:
        return self.results / "governance"

    @property
    def runs_dir(self) -> Path:
        """Where the child writes its locked run folders (lab/run_folder.py: <repo>/results/runs)."""
        return self.root / "results" / "runs"

    def rel(self, path: Path) -> str:
        """``path`` as written in commands and in the plan: repo-relative with forward slashes."""
        try:
            return Path(path).relative_to(self.root).as_posix()
        except ValueError:
            return Path(path).as_posix()


def utc_stamp(when: Optional[datetime] = None) -> str:
    """``YYYYMMDDTHHMMSSZ`` in UTC: the same form as the run ids of lab.run_folder."""
    return (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


@dataclass(frozen=True)
class RaceCheck:
    """Is the corpus race fixed in scripts/run_governance.py?"""

    fixed: bool
    why: str            # the verdict in one sentence; no absolute paths (it goes into plan.json)
    problem: str = ""   # set when the file could not be read or parsed. The verdict is then "not fixed".


def _called_names(tree: ast.AST) -> Set[str]:
    """Names of everything the code calls: ``f()`` gives "f", ``a.b.f()`` gives "f".

    Comments are not in the tree, and docstrings, strings, definitions and plain references are not calls.
    """
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def race_fix_in_source(source) -> RaceCheck:
    """Judge the source of run_governance.py (text or bytes) by its CALLS only.

    The fix is there when the code calls ``check_matches_generator`` and does not call ``build_and_write``.
    Neither call counts as "not fixed": the read-only check must be there. A comment or a docstring that
    names build_and_write does not count (the fixed file has one). A source that does not parse counts as
    "not fixed", and ``problem`` says why.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")    # e.g. SyntaxWarning for an invalid escape in a string
            tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError) as exc:   # ValueError: a null byte, in older Pythons
        if isinstance(exc, SyntaxError):
            detail = f"SyntaxError: {exc.msg}" + (f", line {exc.lineno}" if exc.lineno else "")
        else:
            detail = f"{type(exc).__name__}: {exc}"
        problem = f"cannot parse scripts/run_governance.py ({detail})"
        return RaceCheck(False, f"{problem}, so the corpus race fix is not confirmed", problem)
    names = _called_names(tree)
    if RACE_REWRITE_CALL in names:
        return RaceCheck(False, f"scripts/run_governance.py still calls {RACE_REWRITE_CALL}(), which rewrites "
                                "corpus/*.jsonl at the start of every run")
    if RACE_CHECK_CALL not in names:
        return RaceCheck(False, f"scripts/run_governance.py does not call {RACE_CHECK_CALL}(), "
                                "the read-only corpus check")
    return RaceCheck(True, f"scripts/run_governance.py calls {RACE_CHECK_CALL}() and not {RACE_REWRITE_CALL}()")


def race_fix_check(root: Path) -> RaceCheck:
    """Read ``<root>/scripts/run_governance.py`` and judge it. Unreadable or unparseable counts as "not fixed"."""
    try:
        source = (Path(root) / "scripts" / "run_governance.py").read_bytes()
    except OSError as exc:
        problem = f"cannot read scripts/run_governance.py ({type(exc).__name__}: {exc.strerror or 'error'})"
        return RaceCheck(False, f"{problem}, so the corpus race fix is not confirmed", problem)
    return race_fix_in_source(source)


def corpus_race_fixed(root: Path) -> bool:
    return race_fix_check(root).fixed


def effective_parallel(requested: int, n_models: int, race_fixed: bool,
                       why: str = "") -> Tuple[int, Optional[str]]:
    """The number of models to run at once, and the note to print when it differs from the request."""
    count = max(1, min(requested, n_models))   # more workers than models would only sit idle
    if count > 1 and not race_fixed:
        reason = why or (f"the corpus race fix is not in scripts/run_governance.py (it must call "
                         f"{RACE_CHECK_CALL}() and not {RACE_REWRITE_CALL}())")
        note = f"parallel {count} forced to 1: {reason}. Two runs at once could write corpus/*.jsonl at the same time"
        return 1, note
    return count, None


def child_env(tag: str, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The environment of every child: the model tag, unbuffered output, UTF-8 (Windows code pages)."""
    env = dict(os.environ if base is None else base)
    env["LAB_MODEL"] = tag
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def build_plan(opts: argparse.Namespace, layout: Layout, models: Sequence[Model], stamp: str,
               *, race_fixed: bool, race_why: str = "") -> dict:
    """The whole batch as data: what runs, with which arguments, writing where. No absolute paths.

    Commands are argument lists with ``python`` (and ``ollama``) as the first item; the real interpreter
    replaces it when the command runs. ``race_fixed`` and ``race_why`` come from :func:`race_fix_check`.
    """
    scripted = bool(opts.scripted)
    parallel, note = effective_parallel(opts.parallel, len(models), race_fixed, race_why)
    batch_dir = layout.rel(layout.models_dir / stamp)
    live = [] if scripted else ["--live"]
    rows = []
    for m in models:
        smoke_out = layout.rel(layout.gov_dir / f"{stamp}_{m.slug}_smoke")
        full_out = layout.rel(layout.gov_dir / f"{stamp}_{m.slug}")
        commands = {
            "pull": ["ollama", "pull", m.tag] if opts.pull else None,
            "preflight": None if scripted else ["python", "scripts/preflight.py"],
            "smoke": ["python", "scripts/run_governance.py", *live,
                      "--sample", str(SMOKE_SAMPLE), "--reps", str(SMOKE_REPS), "--out", smoke_out],
            "full": ["python", "scripts/run_governance.py", *live,
                     "--sample", str(opts.sample), "--reps", str(opts.reps), "--out", full_out],
        }
        rows.append({
            "tag": m.tag,
            "slug": m.slug,
            "env": {"LAB_MODEL": m.tag},
            "smoke_out": smoke_out,
            "full_out": full_out,
            "logs": {step: f"{batch_dir}/{m.slug}.{step}.log" for step, cmd in commands.items() if cmd},
            "commands": commands,
        })
    models_file = Path(opts.models)
    return {
        "schema": PLAN_SCHEMA,
        "stamp": stamp,
        "mode": "scripted" if scripted else "live",
        "smoke_only": bool(opts.smoke_only),
        "pull": bool(opts.pull),
        "parallel": parallel,
        "parallel_requested": opts.parallel,
        "parallel_note": note,
        "models_file": models_file.name if models_file.is_absolute() else models_file.as_posix(),
        "batch_dir": batch_dir,
        "smoke": {"sample": SMOKE_SAMPLE, "reps": SMOKE_REPS},
        "full": {"sample": opts.sample, "reps": opts.reps},
        "env": {"PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"},
        "models": rows,
    }


TOOL_CHECK_TEXT = (
    'yes needs a structured tool call, no "<tool_call>" text in any final_text, and not every run '
    "errored; otherwise the full run is skipped"
)


def render_plan(plan: dict, *, dry_run: bool) -> str:
    """The plan as text, for --dry-run and for the top of a real run."""
    live = plan["mode"] == "live"
    lines = [
        "Redline model batch -- DRY RUN: nothing is run and nothing is written" if dry_run else "Redline model batch",
        "Mode      : " + ("LIVE (real models through Ollama)" if live else "SCRIPTED (DEMO traces: no model, no Ollama)"),
        f"Batch     : {plan['batch_dir']}",
        f"Models    : {len(plan['models'])} from {plan['models_file']}",
        f"Parallel  : {plan['parallel']}",
    ]
    if plan["parallel_note"]:
        lines.append(f"Note      : {plan['parallel_note']}")
    if plan["smoke_only"]:
        lines.append("Scope     : smoke only (no full runs)")
    for number, row in enumerate(plan["models"], start=1):
        cmds = row["commands"]
        lines.append("")
        lines.append(f"{number}. {row['tag']}  (slug {row['slug']})")
        lines.append(f"   env        : LAB_MODEL={row['tag']} PYTHONUNBUFFERED=1 PYTHONUTF8=1")
        if cmds["pull"]:
            lines.append(f"   pull       : {' '.join(cmds['pull'])}")
        lines.append("   preflight  : " + (" ".join(cmds["preflight"]) if cmds["preflight"] else "skipped (scripted)"))
        lines.append(f"   smoke      : {' '.join(cmds['smoke'])}")
        lines.append("   tool check : " + (TOOL_CHECK_TEXT if live else "not checked (scripted)"))
        lines.append("   full       : " + ("skipped (--smoke-only)" if plan["smoke_only"] else " ".join(cmds["full"])))
    return "\n".join(lines)


_SAY_LOCK = threading.Lock()


def say(text: str = "") -> None:
    """Print one line. One write under a lock (lines from two models never mix), and it cannot crash on a
    console code page (Windows cp1252): characters the console cannot show become '?'."""
    with _SAY_LOCK:
        try:
            sys.stdout.write(text + "\n")
        except UnicodeEncodeError:
            sys.stdout.write(text.encode("ascii", "replace").decode("ascii") + "\n")
        sys.stdout.flush()


def _short(text: object, limit: int = 200) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --------------------------------------------------------------------------- #
# Run folders and the tool-support check.
# --------------------------------------------------------------------------- #
def _read_json(path: Path):
    """The parsed JSON file, or None when it is missing or damaged."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_locked(run_dir: Path) -> bool:
    """True when the run folder's manifest says ``locked`` (lab/run_folder.py does that when a run ends)."""
    manifest = _read_json(Path(run_dir) / "manifest.json")
    return isinstance(manifest, dict) and bool(manifest.get("locked"))


def snapshot_runs(runs_dir: Path) -> Set[str]:
    """Names of the run folders that exist now. Taken before a step, to tell its new folders apart."""
    try:
        return {p.name for p in Path(runs_dir).iterdir() if p.is_dir()}
    except OSError:
        return set()


def runs_for_model(runs_dir: Path, tag: str, before: Set[str]) -> List[Path]:
    """Run folders made since ``before`` whose manifest says they ran ``tag``, oldest first.

    The manifest is the key, not the folder name: two different tags can have names that start alike.
    """
    found = []
    for name in sorted(snapshot_runs(runs_dir) - before):
        manifest = _read_json(Path(runs_dir) / name / "manifest.json")
        if isinstance(manifest, dict) and manifest.get("model") == tag:
            found.append(Path(runs_dir) / name)
    return found


def read_traces(run_dirs: Sequence[Path]) -> Tuple[List[dict], int]:
    """Every trace of the given run folders, and how many trace files could not be read."""
    traces: List[dict] = []
    unreadable = 0
    for run_dir in run_dirs:
        for path in sorted((Path(run_dir) / "traces").glob("*.json")):
            data = _read_json(path)
            if isinstance(data, dict):
                traces.append(data)
            else:
                unreadable += 1
    return traces, unreadable


@dataclass(frozen=True)
class ToolCheck:
    tools: str            # "yes" | "no" | "not checked"
    reason: str
    detail: str = ""      # more for a reader of status.jsonl: the first error text, a count

    @property
    def text(self) -> str:
        """One line for a screen or the console, e.g. ``no (every run errored)``."""
        return f"{self.tools} ({self.reason})" if self.reason else self.tools


def _has_structured_call(trace: dict) -> bool:
    calls = trace.get("tool_calls")
    return isinstance(calls, list) and any(isinstance(c, dict) and c.get("name") for c in calls)


def classify_tool_support(traces: Sequence[dict]) -> ToolCheck:
    """Does the model call tools properly? "yes" needs ALL of:

    * at least one trace holds a structured tool call (blocked calls count: the model made them),
    * no ``final_text`` contains ``<tool_call>`` (the model wrote its calls as text),
    * not every run errored.

    Anything else is "no", with the reason. Such a model gets no full run: with no working tools it
    cannot be attacked, so it would read as a safe 0% attack rate.
    """
    total = len(traces)
    if total == 0:
        return ToolCheck("no", "no traces were written by the smoke run")
    errors = [str(t["error"]) for t in traces if t.get("error")]
    if len(errors) == total:
        return ToolCheck("no", "every run errored", _short(errors[0]))
    as_text = sum(1 for t in traces if TEXT_TOOL_CALL in str(t.get("final_text") or ""))
    if as_text:
        return ToolCheck("no", f"the model wrote tool calls as text ({TEXT_TOOL_CALL} in final_text)",
                         f"in {as_text} of {total} runs")
    made = sum(1 for t in traces if _has_structured_call(t))
    if made == 0:
        return ToolCheck("no", "no structured tool call in any run", f"{total} runs checked")
    return ToolCheck("yes", f"{made} of {total} runs made a structured tool call",
                     f"{len(errors)} of {total} runs errored" if errors else "")


def tool_support_of(run_dirs: Sequence[Path]) -> ToolCheck:
    """The tool-support check on the run folders of one smoke run."""
    traces, unreadable = read_traces(run_dirs)
    check = classify_tool_support(traces)
    if unreadable:
        note = f"{unreadable} trace files unreadable"
        check = ToolCheck(check.tools, check.reason, "; ".join(x for x in (check.detail, note) if x))
    return check


# --------------------------------------------------------------------------- #
# status.jsonl and the child processes.
# --------------------------------------------------------------------------- #
def _console_line(record: dict) -> str:
    parts = []
    for key in ("tools", "exit_code", "reason", "out", "detail"):
        value = record.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    return f"[{record['ts'][11:19]}] {record['slug']:<26} {record['event']:<17} " + " ".join(parts)


class StatusLog:
    """status.jsonl: one JSON line per state change, also echoed to the console.

    Safe to call from several threads. A line is one write, so a reader that follows the file never sees
    half a line. A write error is reported once and never raised: the batch goes on.
    """

    def __init__(self, path: Path, echo: bool = True) -> None:
        self.path = Path(path)
        self.echo = echo
        self._lock = threading.Lock()
        self._warned = False

    def emit(self, event: str, model: Model, **fields) -> dict:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event,
                  "slug": model.slug, "tag": model.tag}
        record.update(fields)
        line = json.dumps(record)
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                if not self._warned:
                    self._warned = True
                    print(f"warning: cannot write {self.path}: {exc}", file=sys.stderr)
            if self.echo:
                say(_console_line(record))
        return record


def _popen_kwargs() -> dict:
    """Every child gets its own process group (Windows) or session (POSIX). A Ctrl-C typed in the terminal
    then reaches this script only, and this script passes it on exactly once: a second interrupt could
    cut short the child's cleanup, where it locks its run folders."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _send_interrupt(proc: "subprocess.Popen") -> None:
    """Stop a child the way Ctrl-C would, so its ``finally`` blocks run and its run folders lock."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            # CTRL_BREAK_EVENT to the child's process group: not verified on Windows.
            # The child sits in its own process group, which switches Ctrl-C off for it, so a break is
            # the one console signal that can reach it. Python turns a break into KeyboardInterrupt only
            # in a process that installs a SIGBREAK handler. Without one the child ends at once and its
            # run folders stay unlocked (the end-of-batch report lists any such folder).
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            proc.send_signal(signal.SIGINT)
    except OSError:
        pass


def _end_process(proc: "subprocess.Popen") -> None:
    """End a child now (no cleanup: its run folders may stay unlocked)."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            # taskkill /T ends the whole tree: not verified on Windows. A venv's python.exe starts the real
            # interpreter as its child, and TerminateProcess on the launcher alone could leave that child running.
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], check=False,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc.terminate()
    except OSError:
        with contextlib.suppress(OSError):
            proc.terminate()


def _exit_code(returncode: int) -> int:
    """POSIX: a child killed by signal N reports -N. Show it the way a shell does (128 + N)."""
    return 128 - returncode if returncode < 0 else returncode


class ChildSet:
    """The running child processes, so that a stop request reaches each of them exactly once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: Dict[int, "subprocess.Popen"] = {}
        self._interrupted: Set[int] = set()
        self.stopping = False

    def start(self, argv: Sequence[str], **kwargs) -> Optional["subprocess.Popen"]:
        """Start a child, or return None when a stop was requested already."""
        with self._lock:
            if self.stopping:
                return None
            proc = subprocess.Popen(list(argv), **kwargs)
            self._procs[proc.pid] = proc
            return proc

    def finished(self, proc: "subprocess.Popen") -> None:
        with self._lock:
            self._procs.pop(proc.pid, None)

    def interrupt(self) -> None:
        """Ask every running child to stop like Ctrl-C does. A child is asked once, however often this is called."""
        with self._lock:
            self.stopping = True
            todo = [p for pid, p in self._procs.items() if pid not in self._interrupted]
            self._interrupted.update(p.pid for p in todo)
        for proc in todo:
            _send_interrupt(proc)

    def terminate(self) -> None:
        """End every running child now. Its run folders may stay unlocked."""
        with self._lock:
            self.stopping = True
            todo = list(self._procs.values())
        for proc in todo:
            _end_process(proc)


def _raise_keyboard_interrupt(signum, frame) -> None:  # pragma: no cover - runs in a signal handler
    raise KeyboardInterrupt


@contextlib.contextmanager
def _stop_signals():
    """While the batch runs, SIGTERM (and SIGBREAK on Windows) stop it the same way Ctrl-C does."""
    saved = {}
    for name in ("SIGTERM", "SIGBREAK"):   # SIGBREAK exists on Windows only. Not verified there.
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            saved[sig] = signal.signal(sig, _raise_keyboard_interrupt)
        except (ValueError, OSError):      # not the main thread: leave the signal alone
            pass
    try:
        yield
    finally:
        for sig, old in saved.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, old)


@dataclass
class StepResult:
    exit_code: Optional[int]      # None: the child could not be started
    interrupted: bool = False     # a stop was requested and the child did not end cleanly
    error: str = ""


@dataclass
class ModelResult:
    outcome: str = "pending"      # ok | no_tools | failed | interrupted
    tools: str = "not checked"
    tools_text: str = "not checked"
    note: str = ""                # why the model has no full run, or how it ended
    out: str = ""                 # the full run's output folder, when the full run started


def _exit_text(step: StepResult) -> str:
    return step.error if step.exit_code is None else f"exit code {step.exit_code}"


class Batch:
    """One run of the plan: a thread pool with one child process per model at a time."""

    def __init__(self, plan: dict, layout: Layout, python: str, *, echo: bool = True) -> None:
        self.plan = plan
        self.layout = layout
        self.python = python
        self.scripted = plan["mode"] == "scripted"
        self.batch_dir = layout.models_dir / plan["stamp"]
        self.rows = {row["slug"]: row for row in plan["models"]}
        self.models = [Model(tag=row["tag"], slug=row["slug"]) for row in plan["models"]]
        self.status = StatusLog(self.batch_dir / "status.jsonl", echo=echo)
        self.children = ChildSet()
        self.results: Dict[str, ModelResult] = {}
        self.run_dirs: Set[Path] = set()          # every run folder this batch made (for the unlocked report)
        self._stop_asked_at: Optional[float] = None
        self._terminated = False

    # -- setup ----------------------------------------------------------------
    def prepare(self) -> None:
        """Create the batch folder (never reuse one) and write plan.json."""
        self.batch_dir.mkdir(parents=True, exist_ok=False)
        plan = dict(self.plan, created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        tmp = self.batch_dir / "plan.json.tmp"
        tmp.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.batch_dir / "plan.json")

    # -- running --------------------------------------------------------------
    def run(self) -> int:
        say(render_plan(self.plan, dry_run=False))
        say(f"Logs      : {self.plan['batch_dir']}/<slug>.<step>.log")
        say()
        for position, model in enumerate(self.models, start=1):
            self.status.emit("queued", model, position=position)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.plan["parallel"], thread_name_prefix="model")
        try:
            with _stop_signals():
                futures = [pool.submit(self._worker, model) for model in self.models]
                self._wait(futures)
        except KeyboardInterrupt:
            # Only reached for an interrupt outside the wait loop, or a second one while the first was being
            # handled. The first one asks the children to stop; the second ends them.
            if self.children.stopping:
                self.children.terminate()
            else:
                self.children.interrupt()
        finally:
            pool.shutdown(wait=True)
        return self._finish()

    def _wait(self, futures: Sequence["concurrent.futures.Future"]) -> None:
        """Wait for every model. The wait times out every 0.25 s so that Ctrl-C is noticed on Windows too."""
        pending = set(futures)
        while pending:
            try:
                _, pending = concurrent.futures.wait(pending, timeout=0.25)
                self._end_stuck_children()
            except KeyboardInterrupt:
                self._stop()

    def _stop(self) -> None:
        """First interrupt: ask the children to stop (they lock their folders). Second: end them."""
        if not self.children.stopping:
            self._stop_asked_at = time.monotonic()
            self.children.interrupt()
            say("Interrupted: stopping the running models so that their run folders lock. "
                "Press Ctrl-C again to end them at once.")
        elif not self._terminated:
            self._terminated = True
            self.children.terminate()
            say("Interrupted again: ending the running models now; their run folders may stay unlocked.")

    def _end_stuck_children(self) -> None:
        if (self.children.stopping and not self._terminated and self._stop_asked_at is not None
                and time.monotonic() - self._stop_asked_at > GRACE_S):
            self._terminated = True
            say(f"The running models did not stop within {GRACE_S:.0f} s; ending them now.")
            self.children.terminate()

    def _worker(self, model: Model) -> ModelResult:
        try:
            result = self._pipeline(model)
        except Exception as exc:  # noqa: BLE001 - a bug here must not take the other models down
            result = ModelResult("failed", note=f"internal error: {type(exc).__name__}: {exc}")
            self.status.emit("skipped", model, reason=result.note)
        self.results[model.slug] = result
        return result

    # -- one model ------------------------------------------------------------
    def _log_rel(self, model: Model, step: str) -> str:
        return f"{self.plan['batch_dir']}/{model.slug}.{step}.log"

    def _exec_argv(self, display: Sequence[str]) -> List[str]:
        head, rest = display[0], list(display[1:])
        if head == "python":
            return [self.python] + rest
        if head == "ollama":
            return list(OLLAMA_CMD) + rest
        raise ValueError(f"unknown command {head!r}")

    def _run_step(self, model: Model, step: str, display: Sequence[str]) -> StepResult:
        """Run one command with its output in ``<slug>.<step>.log``; wait for it."""
        argv = self._exec_argv(display)
        try:
            with open(self.batch_dir / f"{model.slug}.{step}.log", "wb") as log:
                log.write(("$ " + " ".join(display) + "\n").encode("utf-8"))
                log.flush()
                proc = self.children.start(
                    argv, cwd=str(self.layout.root), env=child_env(model.tag), stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, shell=False, **_popen_kwargs(),
                )
                if proc is None:
                    return StepResult(None, interrupted=True)
                try:
                    code = proc.wait()
                finally:
                    self.children.finished(proc)
        except OSError as exc:
            return StepResult(None, error=f"could not start {display[0]}: {exc}")
        code = _exit_code(code)
        return StepResult(code, interrupted=self.children.stopping and code != 0)

    def _preflight_detail(self, model: Model, step: StepResult) -> str:
        if step.error:
            return step.error
        try:
            lines = (self.batch_dir / f"{model.slug}.preflight.log").read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        fails = [ln.strip() for ln in lines if ln.startswith("FAIL")]
        if fails:
            return _short("; ".join(fails[:5]), 400)
        tail = [ln.strip() for ln in lines if ln.strip() and not ln.startswith("$ ")]
        return _short(tail[-1], 400) if tail else ""

    def _pipeline(self, model: Model) -> ModelResult:
        """pull -> preflight -> smoke -> tool-support check -> full, for one model."""
        row = self.rows[model.slug]
        cmds = row["commands"]
        res = ModelResult()

        def emit(event: str, **fields) -> None:
            self.status.emit(event, model, **fields)

        def skip(outcome: str, reason: str) -> ModelResult:
            res.outcome, res.note = outcome, reason
            emit("skipped", reason=reason)
            return res

        if self.children.stopping:
            return skip("interrupted", "interrupted before it started")

        if cmds["pull"]:
            emit("pull_start", log=self._log_rel(model, "pull"))
            step = self._run_step(model, "pull", cmds["pull"])
            emit("pull_done", exit_code=step.exit_code, **({"error": step.error} if step.error else {}))
            if step.interrupted:
                return skip("interrupted", "interrupted")
            if step.exit_code is None:
                return skip("failed", f"ollama pull could not run ({step.error})")
            if step.exit_code != 0:
                return skip("failed", f"ollama pull failed ({_exit_text(step)}); check the tag with: "
                                      f"ollama pull {model.tag}")

        if cmds["preflight"]:
            emit("preflight_start", log=self._log_rel(model, "preflight"))
            step = self._run_step(model, "preflight", cmds["preflight"])
            if step.interrupted:
                emit("preflight_fail", exit_code=step.exit_code, detail="interrupted")
                return skip("interrupted", "interrupted")
            if step.exit_code == 0:
                emit("preflight_ok", exit_code=0)
            else:
                emit("preflight_fail", exit_code=step.exit_code, detail=self._preflight_detail(model, step))
                return skip("failed", f"preflight failed ({_exit_text(step)}); see {self._log_rel(model, 'preflight')}")
        else:
            emit("preflight_skipped", reason="scripted")

        # Smoke run, then the tool-support check on the run folders it made.
        before = snapshot_runs(self.layout.runs_dir)
        emit("smoke_start", out=row["smoke_out"], log=self._log_rel(model, "smoke"))
        step = self._run_step(model, "smoke", cmds["smoke"])
        smoke_dirs = runs_for_model(self.layout.runs_dir, model.tag, before)
        self.run_dirs.update(smoke_dirs)
        if step.interrupted:
            check = ToolCheck("not checked", "interrupted")
        elif step.exit_code is None:
            check = ToolCheck("not checked", step.error)
        elif self.scripted:
            check = ToolCheck("not checked", "scripted")
        else:
            check = tool_support_of(smoke_dirs)
        res.tools, res.tools_text = check.tools, check.text
        fields = {"exit_code": step.exit_code, "tools": check.tools, "reason": check.reason, "text": check.text,
                  "out": row["smoke_out"], "run_dirs": [p.name for p in smoke_dirs]}
        if check.detail:
            fields["detail"] = check.detail
        emit("smoke_done", **fields)

        if step.interrupted:
            return skip("interrupted", "interrupted")
        if step.exit_code != 0:
            return skip("failed", f"smoke run failed ({_exit_text(step)}); see {self._log_rel(model, 'smoke')}")
        if not self.scripted and check.tools != "yes":
            return skip("no_tools", f"no tool support: {check.reason}")
        if self.plan["smoke_only"]:
            return skip("ok", "smoke-only: the full run was not requested")
        if self.children.stopping:
            return skip("interrupted", "interrupted")

        # Full run.
        before = snapshot_runs(self.layout.runs_dir)
        emit("full_start", out=row["full_out"], log=self._log_rel(model, "full"))
        step = self._run_step(model, "full", cmds["full"])
        full_dirs = runs_for_model(self.layout.runs_dir, model.tag, before)
        self.run_dirs.update(full_dirs)
        fields = {"exit_code": step.exit_code, "out": row["full_out"], "run_dirs": [p.name for p in full_dirs]}
        if step.interrupted:
            fields["reason"] = "interrupted"
        if step.error:
            fields["error"] = step.error
        emit("full_done", **fields)
        res.out = row["full_out"]
        if step.interrupted:
            res.outcome, res.note = "interrupted", "interrupted during the full run"
        elif step.exit_code == 0:
            res.outcome = "ok"
        else:
            res.outcome, res.note = "failed", f"full run failed ({_exit_text(step)}); see {self._log_rel(model, 'full')}"
        return res

    # -- the end --------------------------------------------------------------
    def _finish(self) -> int:
        say()
        say("Summary")
        for model in self.models:
            res = self.results.get(model.slug) or ModelResult("failed", note="no result")
            if res.out:
                tail = f"full run {'done' if res.outcome == 'ok' else res.outcome}: {res.out}"
                if res.note and res.outcome != "ok":
                    tail += f"  ({res.note})"
            elif res.outcome == "no_tools":
                tail = "full run skipped"          # the reason is already in the tools column
            else:
                tail = f"full run skipped: {res.note}"
            say(f"  {model.slug:<26} tools: {res.tools_text}  |  {tail}")
        if self.children.stopping:
            unlocked = [p for p in sorted(self.run_dirs) if not _is_locked(p)]
            if unlocked:
                say("These run folders are NOT locked (their run did not end cleanly). Nothing was deleted:")
                for path in unlocked:
                    say(f"  {self.layout.rel(path)}")
            say("Interrupted: the batch stopped early.")
            return 130
        return 1 if any(r.outcome == "failed" for r in self.results.values()) else 0


# --------------------------------------------------------------------------- #
# Entry point.
# --------------------------------------------------------------------------- #
def _models_path(arg: str, layout: Layout) -> Path:
    """A relative --models path is relative to the repo (never to the current folder), so the default always works."""
    path = Path(arg)
    return path if path.is_absolute() else layout.root / path


def _check_setup(opts: argparse.Namespace, layout: Layout) -> str:
    """Refuse a setup that cannot work; return the interpreter for the children."""
    if opts.pull and opts.scripted:
        raise UsageError("--pull needs a live run; it cannot be combined with --scripted")
    python = opts.python or sys.executable
    if not python:
        raise UsageError("cannot tell which Python to run the child scripts with; pass --python PATH")
    if opts.python:
        if Path(opts.python).is_file():
            # Absolute, because the children run in the repo folder. abspath, not resolve(): a venv's
            # python is a symlink, and following it would leave the venv.
            python = os.path.abspath(opts.python)
        elif not shutil.which(opts.python):
            raise UsageError(f"--python {opts.python!r} is not a file or a command on PATH")
    needed = ["run_governance.py"] + ([] if opts.scripted else ["preflight.py"])
    for name in needed:
        if not (layout.root / "scripts" / name).is_file():
            raise UsageError(f"scripts/{name} not found under {layout.root}")
    return python


def main(argv: Optional[Sequence[str]] = None, *, root: Optional[Path] = None,
         results_root: Optional[Path] = None, stamp: Optional[str] = None) -> int:
    """Run the batch. ``root``, ``results_root`` and ``stamp`` exist for tests; normally all three default."""
    opts = parse_args(argv)
    repo = Path(root) if root is not None else ROOT
    layout = Layout(root=repo, results=Path(results_root) if results_root is not None else repo / "results")
    try:
        models = select_models(read_models(_models_path(opts.models, layout)), opts.only)
        python = _check_setup(opts, layout)
    except (ModelsFileError, UsageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if stamp is None:
        stamp = utc_stamp()
        while not opts.dry_run and (layout.models_dir / stamp).exists():   # another batch began this second
            time.sleep(1.0)
            stamp = utc_stamp()
    race = race_fix_check(layout.root)
    if race.problem:   # say so even when nothing is forced (one model, or --parallel 1)
        print(f"warning: {race.problem}; treating the corpus race fix as absent", file=sys.stderr)
    plan = build_plan(opts, layout, models, stamp, race_fixed=race.fixed, race_why=race.why)
    if opts.dry_run:
        say(render_plan(plan, dry_run=True))
        return 0
    batch = Batch(plan, layout, python)
    try:
        batch.prepare()
    except FileExistsError:
        print(f"error: {plan['batch_dir']} already exists; a batch folder is never reused", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot create {plan['batch_dir']}: {exc}", file=sys.stderr)
        return 2
    try:
        return batch.run()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
