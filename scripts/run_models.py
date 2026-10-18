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

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS = "config/models.txt"

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
