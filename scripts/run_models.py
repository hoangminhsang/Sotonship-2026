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

status.jsonl: every line has ``ts`` (UTC), ``event``, ``slug``, ``tag``; some add fields.

    queued          position
    pull_start / pull_done                  exit_code (pull_done)
    preflight_start / preflight_ok / preflight_fail / preflight_skipped
    smoke_start     out, log
    smoke_done      exit_code, tools (yes | no | not checked), reason, text, out, run_dirs
    full_start      out, log
    full_done       exit_code, out, run_dirs (+ reason when interrupted)
    skipped         reason            (the full run will not happen; always the last line of a model
                                       that did not reach full_done)

Concurrency: ``--parallel N`` (default 2, at most 4) runs N models at once on a thread pool, one child
process per model. The same model never runs twice at once. N is forced to 1 while
scripts/run_governance.py still calls ``corpus.build_and_write(``, because two runs would rewrite
corpus/*.jsonl at the same time (the corpus race; the fix makes the run read-only on the corpus).

Stopping: Ctrl-C (or SIGTERM) stops the batch. Each running child gets exactly one interrupt, so its run
folder still locks, and queued models are skipped. A second Ctrl-C ends the children at once.

Not verified: how OLLAMA_NUM_PARALLEL and OLLAMA_MAX_LOADED_MODELS behave when two models run. Check
with ``ollama ps`` while the batch runs.

Standard library only; Python 3.9 or newer; no shell is ever used (argument lists, ``shell=False``).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS = "config/models.txt"

PLAN_SCHEMA = "run-models/1"
SMOKE_SAMPLE, SMOKE_REPS = 1, 1       # the smoke run: 1 attack + 1 benign test, 1 rep
FULL_SAMPLE, FULL_REPS = 26, 3        # the full run (overridable with --sample / --reps)
DEFAULT_PARALLEL, MAX_PARALLEL = 2, 4
# The corpus race: while run_governance.py still calls corpus.build_and_write(, every run rewrites
# corpus/*.jsonl at start, so two runs at once are not safe. The fix replaces the call by a read-only check.
RACE_MARKER = "build_and_write("
OLLAMA_CMD = ["ollama"]               # tests point this at a stand-in

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


def corpus_race_fixed(root: Path) -> bool:
    """False while scripts/run_governance.py still calls ``corpus.build_and_write(``.

    Plain text search, as agreed: it does not parse the file, so keep the string out of comments too.
    A file that cannot be read counts as "not fixed" (the safe answer).
    """
    try:
        text = (Path(root) / "scripts" / "run_governance.py").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return RACE_MARKER not in text


def effective_parallel(requested: int, n_models: int, race_fixed: bool) -> Tuple[int, Optional[str]]:
    """The number of models to run at once, and the note to print when it differs from the request."""
    count = max(1, min(requested, n_models))   # more workers than models would only sit idle
    if count > 1 and not race_fixed:
        note = (
            f"parallel {count} forced to 1: scripts/run_governance.py still calls "
            f"corpus.{RACE_MARKER}) at the start of every run, and two runs at once would rewrite "
            "corpus/*.jsonl at the same time"
        )
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
               *, race_fixed: bool) -> dict:
    """The whole batch as data: what runs, with which arguments, writing where. No absolute paths.

    Commands are argument lists with ``python`` (and ``ollama``) as the first item; the real interpreter
    replaces it when the command runs.
    """
    scripted = bool(opts.scripted)
    parallel, note = effective_parallel(opts.parallel, len(models), race_fixed)
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
    'tool support is "yes" only with a structured tool call in the smoke traces, no "<tool_call>" text in any '
    "final_text, and not every run errored; otherwise the full run is skipped"
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


def say(text: str = "") -> None:
    """print() that cannot crash on a console code page (Windows cp1252): bad characters become '?'."""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


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
    if opts.python and not (Path(opts.python).is_file() or shutil.which(opts.python)):
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
    stamp = stamp or utc_stamp()
    plan = build_plan(opts, layout, models, stamp, race_fixed=corpus_race_fixed(layout.root))
    if opts.dry_run:
        say(render_plan(plan, dry_run=True))
        return 0
    print("error: running a batch is not implemented in this commit", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
