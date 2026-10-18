"""Progress events for one pipeline run: ``<out>/progress.jsonl``.

``scripts/run_governance.py`` writes one JSON object per line while it runs, so a
viewer (``ui/bridge.py``, the pipeline screen) can follow the run as it happens.
This module is only the writer plus a few helpers; the event types and the
fields of each one are listed below and are built where the events happen.

File format
-----------
* One object per line, written with ``json.dumps(ensure_ascii=True,
  sort_keys=True, separators=(",", ":"), allow_nan=False)``. NaN and infinity
  become ``null``. Each line goes out in ONE ``write()`` followed by ``flush()``.
* Common keys on every line: ``v`` (1), ``seq`` (1, 2, 3 ... per file),
  ``t`` (UTC ISO 8601 with microseconds), ``uid`` (12 hex characters, one per
  pipeline run), ``type``, ``cand`` (``RC-001`` ... or null) and ``stage``
  (the open stage id or null).
* Stage ids: ``setup``; then for each RC-00N ``RC-00N:runs``, ``:gate``,
  ``:decide``, ``:assure``; then ``trace``, ``summary``, ``chain``, ``done``.
* Paths are repo-relative POSIX (``results/runs/x/runs.csv``) or
  ``<outside repo>/<name>``. An absolute path never appears.

Safety (a progress file must never hurt the run it describes)
-------------------------------------------------------------
* **Never raises.** Any ``Exception`` disables the emitter, prints ONE line to
  stderr and the run carries on. ``BaseException`` (Ctrl-C, SIGTERM turned into
  KeyboardInterrupt) is NOT caught, so an interrupted run still locks its folder.
* **Never deletes.** An existing ``progress.jsonl`` is renamed to
  ``progress.<UTC>.jsonl`` first; the new file is created exclusively.
* It does no work that can block: no network, one ``git rev-parse`` at the start.

Event types (fields besides the common keys)
--------------------------------------------
pipeline_start  mode, argv, out, platform, git_commit
meta            model, digest12, policy_version, test_set{records, attacks, benign, sha12},
                reps, sample, candidates[{id, defences, expected_runs}]
stage_start     stage, title
stage_done      stage, title, status (ok|error|skipped), dur_s
run_start       i, n, test_id, rep, kind, technique                     (LIVE only)
run_done        i, n, test_id, rep, kind, technique, goal, violated, classes, attempted,
                calls, blocked, tool_calls, error, benign_success, duration_s,
                running{attack_runs, attack_violations, asr, asr_lo, asr_hi,
                        benign_n, benign_rate, errors, runs}
runs_done       run_id, runs_csv, summary{...gate.summarise...}
fingerprint     identity12, changed_vs_prev
gate            verdict, reasons, checks[{name, ok, value, limit, note}], lines, file
audit           entry (the audit log's own entry number), event, by, reviewer, verdict,
                decision, reason, scripted, hash12
risks|tickets|evidence   rows, controls_used
artifact        name, path, kind
trace           finding, steps_n
summary         bullets
chain           ok, entries, head12, detail, tamper{ok, bad_seq, kind}|null
pipeline_done   status (ok|error|interrupted), exit_code, dur_s, headline{cands[...]}, error
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import IO, Any, Callable, Iterator, Mapping

REPO = Path(__file__).resolve().parent.parent
SCHEMA = 1
FILENAME = "progress.jsonl"
OUTSIDE = "<outside repo>"
MAX_PENDING = 1000  # a deferred emitter that is never activated must not grow without bound

# Options of the governance CLI whose value is a path (so argv can be relativised).
PATH_OPTIONS = ("--out", "--run", "--run-after", "--run-after-d3")


# --------------------------------------------------------------------------- #
# Small helpers (pure; used by the emitter and by the CLI hooks).
# --------------------------------------------------------------------------- #
def new_uid() -> str:
    """12 hex characters naming one pipeline run."""
    return uuid.uuid4().hex[:12]


def utc_now() -> str:
    """UTC ISO 8601 with microseconds, e.g. ``2026-10-18T06:15:00.123456+00:00``."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _stamp() -> str:
    """UTC stamp for a rotated file name (a function so tests can fix it)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def short(value: str | None) -> str | None:
    """First 12 hex characters of a hash, with or without a ``sha256:`` prefix."""
    if not value:
        return None
    return str(value).split(":", 1)[-1][:12]


def display_path(path: str | os.PathLike | None, repo: Path | str = REPO) -> str | None:
    """Repo-relative POSIX path, or ``<outside repo>/<name>``; never an absolute path."""
    if path is None or str(path) == "":
        return None
    try:
        resolved = Path(path).resolve()
        try:
            return resolved.relative_to(Path(repo).resolve()).as_posix()
        except ValueError:
            return f"{OUTSIDE}/{resolved.name or str(resolved)}"
    except Exception:  # noqa: BLE001 - a path that cannot be resolved still must not leak
        name = os.path.basename(str(path).rstrip("/\\"))
        return f"{OUTSIDE}/{name}"


def relativise_argv(argv: list[str], repo: Path | str = REPO) -> list[str]:
    """The command line with every path argument made repo-relative or ``<outside repo>/<name>``.

    A token is a path when it follows (or is attached with ``=`` to) one of
    :data:`PATH_OPTIONS`, or when it is an absolute path on its own.
    """
    out: list[str] = []
    take_path = False
    for tok in argv:
        tok = str(tok)
        if take_path:
            out.append(display_path(tok, repo) or tok)
            take_path = False
            continue
        opt, eq, val = tok.partition("=")
        if eq and opt in PATH_OPTIONS:
            out.append(f"{opt}={display_path(val, repo) or val}")
        elif tok in PATH_OPTIONS:
            out.append(tok)
            take_path = True
        elif not tok.startswith("-") and os.path.isabs(tok):
            out.append(display_path(tok, repo) or tok)
        else:
            out.append(tok)
    return out


def scrub(text: object, limit: int = 200, repo: Path | str = REPO) -> str:
    """``text`` with the repo folder removed and the home folder shortened to ``~``, cut to ``limit``."""
    s = str(text)
    try:
        root = str(Path(repo).resolve())
        s = s.replace(root + os.sep, "").replace(root, "<repo>")
        home = str(Path.home())
        if home and home not in ("/", os.sep):
            s = s.replace(home, "~")
    except Exception:  # noqa: BLE001
        pass
    return s[:limit]


def git_commit(repo: Path | str = REPO, timeout: float = 5.0) -> str | None:
    """``git rev-parse HEAD`` for ``repo``, or None when git is missing or fails (best effort)."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, timeout=timeout,
        )
        if done.returncode == 0:
            return done.stdout.strip() or None
    except Exception:  # noqa: BLE001 - provenance is best effort
        pass
    return None


def clean(value: Any, repo: Path | str = REPO) -> Any:
    """``value`` as plain JSON types: NaN/inf -> None, numpy scalars unwrapped, paths made safe."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, PurePath):
        return display_path(value, repo)
    if isinstance(value, Mapping):
        return {str(k): clean(v, repo) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v, repo) for v in value]
    if isinstance(value, (set, frozenset)):
        return [clean(v, repo) for v in sorted(value, key=str)]
    if type(value).__name__ in ("NAType", "NaTType"):  # pandas missing values, without importing pandas
        return None
    item = getattr(value, "item", None)  # numpy scalars (np.int64, np.bool_, ...)
    if callable(item):
        try:
            return clean(item(), repo)
        except Exception:  # noqa: BLE001
            pass
    return str(value)


def dumps(event: Mapping[str, Any], repo: Path | str = REPO) -> str:
    """The canonical one-line JSON for ``event`` (no trailing newline)."""
    return json.dumps(
        clean(event, repo), ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _cand_of(stage: str | None) -> str | None:
    """``RC-001:gate`` -> ``RC-001``; a stage without a candidate (``trace``) -> None."""
    if stage and ":" in stage:
        return stage.partition(":")[0]
    return None


def _open_new(path: Path) -> IO[str]:
    """Create ``path`` exclusively (never truncate, never append to someone else's file); LF line ends."""
    return open(path, "x", encoding="utf-8", newline="\n")


def rotate_existing(path: Path) -> Path | None:
    """Rename an existing ``path`` to ``<stem>.<UTC stamp><suffix>``; never deletes. Returns the new name."""
    if not path.exists():
        return None
    stamp = _stamp()
    target = path.with_name(f"{path.stem}.{stamp}{path.suffix}")
    n = 1
    while target.exists():  # two runs in the same second
        n += 1
        target = path.with_name(f"{path.stem}.{stamp}-{n}{path.suffix}")
    path.rename(target)
    return target


# --------------------------------------------------------------------------- #
# Running counters for the run_done events.
# --------------------------------------------------------------------------- #
class RunningStats:
    """The live counters shown while runs finish: an O(1)-per-run twin of ``gate.summarise``.

    Same counting rule as ``metrics.counted``: an error run enters the rates only
    if it broke a rule; every run enters ``runs``, every error run enters
    ``errors``. A rate with nothing behind it (0 attack runs, 0 benign runs) is
    ``None``, never a clean 0% (the gate calls that "not measured").
    """

    def __init__(self) -> None:
        self.runs = self.errors = 0
        self.attack_runs = self.attack_violations = 0
        self.benign_n = self.benign_ok = 0

    def add(self, kind: str, violated: bool, error: object, benign_success: bool) -> dict:
        from lab import metrics  # local: keeps `import lab.progress` light

        self.runs += 1
        failed = bool(error)
        self.errors += failed
        if not failed or violated:  # metrics.counted
            if kind == "attack":
                self.attack_runs += 1
                self.attack_violations += bool(violated)
            elif kind == "benign":
                self.benign_n += 1
                self.benign_ok += bool(benign_success)
        asr = metrics.wilson(self.attack_violations, self.attack_runs)
        benign = metrics.wilson(self.benign_ok, self.benign_n)
        return {
            "attack_runs": self.attack_runs,
            "attack_violations": self.attack_violations,
            "asr": asr.rate if self.attack_runs else None,
            "asr_lo": asr.lo if self.attack_runs else None,
            "asr_hi": asr.hi if self.attack_runs else None,
            "benign_n": self.benign_n,
            "benign_rate": benign.rate if self.benign_n else None,
            "errors": self.errors,
            "runs": self.runs,
        }


def _text(value: object) -> str:
    """``value`` as text; None and NaN (a missing cell) are the empty string."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value)


def run_fields(i: int, n: int, trace: Mapping, row: Mapping, stats: RunningStats) -> dict:
    """The fields of a ``run_done`` event for run ``i`` of ``n``.

    ``trace`` is the run's trace; ``row`` is its scored row from
    ``metrics.build_runs_frame`` (a dict or a pandas Series).
    """
    calls = trace.get("tool_calls") or []
    kind = str(row["kind"])
    violated = bool(row["violated"])
    error = _text(row["error"])
    benign_success = bool(row["benign_success"])
    classes = [c for c in _text(row["violation_classes"]).split(";") if c]
    return {
        "i": i,
        "n": n,
        "test_id": str(row["test_id"]),
        "rep": row["rep"],
        "kind": kind,
        "technique": row["technique"],
        "goal": row["goal"],
        "violated": violated,
        "classes": classes,
        "attempted": bool(row["attempted"]),
        "calls": len(calls),
        "blocked": sum(1 for c in calls if isinstance(c, Mapping) and c.get("blocked")),
        "tool_calls": _text(row["tool_calls"])[:300],
        "error": scrub(error, 200) if error else None,
        "benign_success": benign_success if kind == "benign" else None,
        "duration_s": trace.get("duration_s"),
        "running": stats.add(kind, violated, error, benign_success),
    }


# --------------------------------------------------------------------------- #
# The emitter.
# --------------------------------------------------------------------------- #
class Progress:
    """Writes ``<out_dir>/progress.jsonl``. Safe to call from anywhere: it never raises.

    ``deferred=True`` holds events in memory until :meth:`activate` is called, so
    a run that is refused before it writes anything (a bad ``--run`` folder)
    leaves no file and no folder behind. Otherwise the folder is created and an
    existing file rotated at the first event.
    """

    def __init__(
        self,
        out_dir: str | os.PathLike,
        *,
        uid: str | None = None,
        repo: Path | str = REPO,
        deferred: bool = False,
        opener: Callable[[Path], IO[str]] | None = None,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.path = self.out_dir / FILENAME
        self.uid = uid or new_uid()
        self.repo = Path(repo)
        self.seq = 0
        self.disabled = False
        self.rotated_to: Path | None = None
        self.error: str | None = None             # set by the pipeline for pipeline_done
        self.headline: list[dict] = []            # candidates gated so far, for pipeline_done
        self._opener = opener
        self._fh: IO[str] | None = None
        self._active = not deferred
        self._pending: list[str] = []
        self._stage: str | None = None
        self._stage_title: str | None = None
        self._stage_t0 = 0.0

    # -- plumbing ----------------------------------------------------------- #
    def _fail(self, exc: Exception) -> None:
        """Disable the emitter and say so once. Never raises."""
        if self.disabled:
            return
        self.disabled = True
        self._pending.clear()
        try:
            print(f"progress: disabled, {type(exc).__name__}: {exc}", file=sys.stderr)
        except Exception:  # noqa: BLE001 - stderr itself may be gone
            pass
        self._close_quietly()

    def _close_quietly(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass

    def _open(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.rotated_to = rotate_existing(self.path)
        self._fh = (self._opener or _open_new)(self.path)

    def _write(self, line: str) -> None:
        if self._fh is None:
            self._open()
        assert self._fh is not None
        self._fh.write(line)   # the whole line, newline included, in one call
        self._fh.flush()

    def activate(self) -> None:
        """Start writing (deferred emitters): rotate any old file, then write what was queued."""
        if self.disabled or self._active:
            return
        try:
            self._active = True
            pending, self._pending = self._pending, []
            for line in pending:
                self._write(line)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    # -- events ------------------------------------------------------------- #
    def emit(self, type: str, **fields: Any) -> None:  # noqa: A002 - "type" is the event key
        """Append one event. ``stage`` and ``cand`` default to the open stage's."""
        if self.disabled:
            return
        try:
            stage = fields.pop("stage", self._stage)
            cand = fields.pop("cand", _cand_of(stage))
            self.seq += 1
            event = {
                **fields,
                "v": SCHEMA, "seq": self.seq, "t": utc_now(), "uid": self.uid,
                "type": type, "cand": cand, "stage": stage,
            }
            line = dumps(event, self.repo) + "\n"
            if self._active:
                self._write(line)
            elif len(self._pending) < MAX_PENDING:
                self._pending.append(line)
        except Exception as exc:  # noqa: BLE001 - never BaseException: Ctrl-C must get through
            self._fail(exc)

    def emit_lazy(self, type: str, build: Callable[[], Mapping[str, Any]], **fields: Any) -> None:  # noqa: A002
        """Like :meth:`emit`, but the fields come from ``build()``.

        ``build`` runs only while the emitter is enabled, and an ``Exception`` in
        it (a surprise in the data) disables the emitter instead of the run.
        """
        if self.disabled:
            return
        try:
            data = dict(build())
        except Exception as exc:  # noqa: BLE001 - never BaseException
            self._fail(exc)
            return
        self.emit(type, **{**data, **fields})

    @contextmanager
    def guard(self) -> Iterator[None]:
        """``with prog.guard():`` runs a block whose ``Exception`` disables the emitter, not the run."""
        try:
            yield
        except Exception as exc:  # noqa: BLE001 - never BaseException
            self._fail(exc)

    def begin_stage(self, stage: str, title: str) -> None:
        """Close the open stage (status ok) and open ``stage``."""
        self.end_stage("ok")
        try:
            self._stage, self._stage_title, self._stage_t0 = stage, title, time.monotonic()
        except Exception:  # noqa: BLE001
            return
        self.emit("stage_start", stage=stage, title=title)

    def end_stage(self, status: str = "ok") -> None:
        """Close the open stage with ``status`` (ok|error|skipped); a no-op when none is open."""
        stage = self._stage
        if stage is None:
            return
        title, t0 = self._stage_title, self._stage_t0
        self._stage = self._stage_title = None
        self.emit("stage_done", stage=stage, title=title, status=status,
                  dur_s=round(time.monotonic() - t0, 3))

    def close(self) -> None:
        """Close the file. Safe to call twice."""
        self._close_quietly()
