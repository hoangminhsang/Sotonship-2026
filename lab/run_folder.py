"""Saved, locked run folders: ``results/runs/<run_id>/``.

Every run (native runner or PyRIT driver) writes to its own folder and never
touches another one:

    results/runs/<run_id>/
        manifest.json    what was run, with what, hashes of every file, locked flag
        records.jsonl    the exact corpus records that were run (same format as corpus/)
        traces/          one JSON per (test, rep), written once, never overwritten
        runs.csv         one row per run (tool calls + verdict), built at lock time
        summary.csv      aggregated metrics, built at lock time

The manifest is written at the *start* of a run with the model tag and digest,
so the digest is on disk before anything could delete the model. When the run
ends (normally or interrupted) :func:`lock_run` builds the CSVs, hashes every
file, writes ``locked: true`` and makes every file read-only. Nothing writes
into a locked folder; a folder that already exists is never reused.

Nothing here deletes anything.
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from lab import corpus, metrics, world
from lab.governance import POLICY_PATH

ROOT = Path(__file__).resolve().parent.parent
RUNS_ROOT = ROOT / "results" / "runs"
MANIFEST = "manifest.json"
RECORDS = "records.jsonl"
TRACES = "traces"
SCHEMA = 1


class RunFolderError(RuntimeError):
    """A write into a saved run, a reused run id, or a run whose files changed."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(text: str) -> str:
    return "".join(c if (c.isalnum() or c in "._-") else "-" for c in str(text)) or "x"


def make_run_id(model: str, candidate: str = "adhoc", when: datetime | None = None) -> str:
    """``<UTC time>_<model tag>_<candidate>``, filesystem-safe."""
    when = when or datetime.now(timezone.utc)
    return f"{when.strftime('%Y%m%dT%H%M%SZ')}_{_safe(model)}_{_safe(candidate)}"


def _git_commit() -> tuple[str, bool | None]:
    """Best-effort ``(commit, dirty)``; ``("", None)`` when git is unavailable."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        if commit.returncode != 0:
            return "", None
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
        return commit.stdout.strip(), dirty
    except Exception:  # noqa: BLE001 - provenance is best-effort
        return "", None


def _read_manifest(run_dir: Path) -> dict:
    path = Path(run_dir) / MANIFEST
    if not path.is_file():
        raise RunFolderError(f"{run_dir} is not a run folder (no {MANIFEST})")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_manifest(run_dir: Path, manifest: dict) -> None:
    path = Path(run_dir) / MANIFEST
    if path.exists():
        os.chmod(path, 0o644)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def _file_list(run_dir: Path) -> list[Path]:
    """Every regular file under the folder except the manifest, dotfiles ignored."""
    out = []
    for p in sorted(Path(run_dir).rglob("*")):
        rel = p.relative_to(run_dir)
        if p.is_file() and p.name != MANIFEST and not any(part.startswith(".") for part in rel.parts):
            out.append(p)
    return out


def create(
    records: list[dict],
    *,
    model: str,
    model_digest: str,
    driver: str,
    reps: int,
    temperature: float,
    seed: int | None = None,
    candidate: str = "adhoc",
    defences: tuple[str, ...] | list[str] = (),
    split: str | None = None,
    runs_root: Path | str | None = None,
    run_id: str | None = None,
) -> Path:
    """Create a fresh run folder with its records and an unlocked manifest.

    Refuses duplicate test ids (they would collide on trace names) and refuses
    an existing folder: a saved run is never written into again.
    """
    ids = [r["id"] for r in records]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise RunFolderError(f"duplicate test ids in the records to run: {dupes}")

    root = Path(runs_root) if runs_root else RUNS_ROOT
    run_id = run_id or make_run_id(model, candidate)
    run_dir = root / run_id
    if run_dir.exists():
        raise RunFolderError(
            f"run folder {run_dir} already exists; a saved run is never overwritten "
            "(pick a new run_id)"
        )
    root.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(exist_ok=False)
    (run_dir / TRACES).mkdir()

    records_path = run_dir / RECORDS
    corpus.write_jsonl(records, records_path)

    commit, dirty = _git_commit()
    manifest = {
        "schema": SCHEMA,
        "run_id": run_id,
        "candidate": candidate,
        "driver": driver,
        "model": model,
        "model_digest": model_digest or "",
        "defences": list(defences or ()),
        "temperature": temperature,
        "seed": seed,
        "reps": reps,
        "split": split,
        "n_records": len(records),
        "expected_runs": len(records) * reps,
        "records_sha256": sha256_file(records_path),
        "corpus_sha256": {
            p.name: sha256_file(p) for p in (corpus.DEV_PATH, corpus.TEST_PATH) if p.is_file()
        },
        "rules_sha256": sha256_file(world.RULES_PATH),
        "policy_sha256": sha256_file(POLICY_PATH),
        "git_commit": commit,
        "git_dirty": dirty,
        "started_at": _now(),
        "ended_at": None,
        "n_runs": 0,
        "complete": None,
        "seconds_per_run": None,
        "files": {},
        "locked": False,
        "locked_at": None,
    }
    _write_manifest(run_dir, manifest)
    return run_dir


def trace_name(test_id: str, rep: int) -> str:
    return f"{test_id}__rep{rep}.json"


def write_trace(run_dir: Path | str, trace: dict) -> Path:
    """Write one trace into an unlocked run folder; never overwrites."""
    run_dir = Path(run_dir)
    if _read_manifest(run_dir).get("locked"):
        raise RunFolderError(f"run folder {run_dir} is locked; nothing may be written into it")
    out = run_dir / TRACES / trace_name(trace["test_id"], trace.get("rep"))
    try:
        with out.open("x", encoding="utf-8") as fh:
            json.dump(trace, fh, indent=2)
    except FileExistsError as exc:
        raise RunFolderError(f"trace {out} already exists; traces are never overwritten") from exc
    return out


def read_traces(run_dir: Path | str) -> list[dict]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((Path(run_dir) / TRACES).glob("*.json"))
    ]


def read_records(run_dir: Path | str) -> list[dict]:
    return corpus.read_jsonl(Path(run_dir) / RECORDS)


def _seconds(traces: list[dict]) -> dict:
    secs = [float(t["duration_s"]) for t in traces if t.get("duration_s") is not None]
    if not secs:
        return {"n": 0, "total": 0.0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "n": len(secs),
        "total": round(sum(secs), 3),
        "mean": round(statistics.fmean(secs), 3),
        "median": round(statistics.median(secs), 3),
        "min": min(secs),
        "max": max(secs),
    }


def lock_run(run_dir: Path | str) -> dict:
    """Finish a run: build the CSVs, hash every file, mark locked, make read-only.

    This is the hook for a testing workflow (run -> lock -> save -> extract ->
    delete model). Idempotent: on an already-locked folder it only verifies.
    """
    run_dir = Path(run_dir)
    manifest = _read_manifest(run_dir)
    if manifest.get("locked"):
        return verify_run(run_dir)

    traces = read_traces(run_dir)
    if traces:
        records = read_records(run_dir)
        by_id = {r["id"]: r for r in records}
        runs = metrics.build_runs_frame(traces, world.load_rules(), by_id)
        runs["trace_path"] = [
            f"{TRACES}/{trace_name(tid, rep)}" for tid, rep in zip(runs["test_id"], runs["rep"])
        ]
        metrics.write_outputs(runs, run_dir / "summary.csv", run_dir / "runs.csv")

    manifest.update(
        {
            "ended_at": _now(),
            "n_runs": len(traces),
            "complete": len(traces) == manifest.get("expected_runs"),
            "seconds_per_run": _seconds(traces),
            "files": {p.relative_to(run_dir).as_posix(): sha256_file(p) for p in _file_list(run_dir)},
            "locked": True,
            "locked_at": _now(),
        }
    )
    manifest["manifest_sha256"] = manifest_digest(manifest)
    _write_manifest(run_dir, manifest)
    for p in [*_file_list(run_dir), run_dir / MANIFEST]:
        os.chmod(p, 0o444)
    return manifest


def manifest_digest(manifest: dict) -> str:
    """sha256 of the manifest body (every field except ``manifest_sha256`` itself).

    Written at lock time and checked by :func:`verify_run`, so an edit to a
    locked manifest (model, model_digest, defences, ...) is detected (C-12).
    """
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_run(run_dir: Path | str) -> dict:
    """Check a locked run folder against its manifest hashes; raise on any change."""
    run_dir = Path(run_dir)
    manifest = _read_manifest(run_dir)
    if not manifest.get("locked"):
        raise RunFolderError(f"run folder {run_dir} is not locked (run did not finish?)")
    recorded = manifest.get("manifest_sha256")
    if recorded != manifest_digest(manifest):
        raise RunFolderError(
            f"run folder {run_dir}: manifest.json was changed after lock "
            f"(manifest_sha256 {recorded!r} does not match its body)"
        )
    expected = manifest.get("files", {})
    actual = {p.relative_to(run_dir).as_posix(): sha256_file(p) for p in _file_list(run_dir)}
    changed = sorted(k for k in expected if k in actual and actual[k] != expected[k])
    missing = sorted(k for k in expected if k not in actual)
    extra = sorted(k for k in actual if k not in expected)
    if changed or missing or extra:
        raise RunFolderError(
            f"run folder {run_dir} does not match its manifest: "
            f"changed={changed} missing={missing} extra={extra}"
        )
    return manifest


def load_run(run_dir: Path | str) -> tuple[dict, list[dict], list[dict]]:
    """Return ``(manifest, records, traces)`` of a verified locked run."""
    run_dir = Path(run_dir)
    manifest = verify_run(run_dir)
    return manifest, read_records(run_dir), read_traces(run_dir)
