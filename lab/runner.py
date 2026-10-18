"""Experiment runner and trace logging.

For each test in a split, the runner builds an isolated poisoned copy of the
world, runs the agent ``reps`` times, and writes one trace per run. Traces are
the single source of truth for scoring -- the oracle and metrics read traces,
never re-run the agent.

The shared ``data/`` fixtures are never mutated: each run gets a fresh temp
directory, and the in-process tools (:func:`lab.agent.make_local_tools`) are
pointed at it.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from lab import agent, run_folder, world

ROOT = Path(__file__).resolve().parent.parent
# Legacy flat trace folder. Runs no longer write here (see lab.run_folder);
# kept only for clear_traces()/load_traces() used by old notebooks.
TRACES = ROOT / "results" / "traces"


def model_digest(model: str = agent.MODEL, host: str = agent.OLLAMA) -> str:
    """Return the Ollama digest for ``model`` (from /api/tags), or ''.

    The digest is best-effort provenance metadata; a failure to read it never
    stops a run. Matches the model exactly, then by base name (ignoring a
    ``:latest`` suffix) so ``qwen2.5:7b-instruct`` resolves cleanly.
    """
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=10) as resp:
            models = json.load(resp).get("models", [])
    except Exception:  # noqa: BLE001 - digest is best-effort metadata
        return ""

    for record in models:
        if record.get("name") == model:
            digest = record.get("digest", "")
            return digest if digest.startswith("sha256:") or not digest else f"sha256:{digest}"
    want = model.split(":")[0]
    for record in models:
        if record.get("name", "").split(":")[0] == want:
            digest = record.get("digest", "")
            return digest if digest.startswith("sha256:") or not digest else f"sha256:{digest}"
    return ""


def build_world_dir(test: dict, dest: Path) -> Path:
    """Write an isolated, poisoned copy of the world into ``dest``.

    Starts from the clean fixtures and applies the test's ``inject_target``.
    Benign tests get a clean copy. Returns ``dest``.
    """
    (dest / "docs").mkdir(parents=True, exist_ok=True)
    for name, text in world.load().docs.items():
        (dest / "docs" / name).write_text(text, encoding="utf-8")
    assets = json.loads(world.ASSETS_PATH.read_text(encoding="utf-8"))

    target = test.get("inject_target")
    if target:
        _apply_injection(target, dest, assets)

    (dest / "assets.json").write_text(json.dumps(assets, indent=2), encoding="utf-8")
    return dest


def _apply_injection(target: dict, dest: Path, assets: list[dict]) -> None:
    """Apply one inject_target to the per-run world (docs on disk, assets in place)."""
    kind = target["type"]
    if kind in ("doc_body", "doc_comment"):
        path = dest / "docs" / target["doc"]
        base = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(base + "\n\n" + target["payload"] + "\n", encoding="utf-8")
    elif kind == "doc_split":
        for frag in target["docs"]:
            path = dest / "docs" / frag["doc"]
            base = path.read_text(encoding="utf-8") if path.exists() else ""
            path.write_text(base + "\n\n" + frag["text"] + "\n", encoding="utf-8")
    elif kind == "asset_notes":
        for record in assets:
            if record["asset_id"] == target["asset_id"]:
                record["notes"] = f"{record['notes']}. {target['payload']}"
                break
    else:  # pragma: no cover - guards against a malformed corpus
        raise ValueError(f"unknown inject_target type {kind!r}")


def run_split(
    split_records: list[dict],
    reps: int = 3,
    seed: int = 7,
    temperature: float = 0.2,
    digest: str | None = None,
    make_agent: Callable | None = None,
    defences: tuple[str, ...] = (),
    *,
    candidate: str = "adhoc",
    split: str | None = None,
    runs_root: Path | str | None = None,
    run_id: str | None = None,
) -> Iterator[dict]:
    """Run every test in a split ``reps`` times, yielding one trace per run.

    A generator so the notebook can render live progress and a running ASR.
    Each run is isolated; an agent error is recorded in the trace (with empty
    ``tool_calls``) and the sweep continues.

    ``defences`` (e.g. ``("D2",)``) builds the agent over guarded tools for an
    *after*-study run; empty is the unguarded *before* study.

    Every call writes into its own folder ``results/runs/<run_id>/`` (see
    :mod:`lab.run_folder`): the manifest with the model digest goes down before
    the first model call, each trace is written once, and the folder is locked
    (CSVs built, hashes recorded, files read-only) when the sweep ends -- also
    when it is interrupted. Nothing under ``results/`` is ever deleted or
    overwritten. ``candidate`` and ``split`` only label the folder.
    """
    digest = model_digest() if digest is None else digest
    build_agent = make_agent or (
        lambda world_dir, temp: _default_make_agent(world_dir, temp, defences)
    )
    run_dir = run_folder.create(
        list(split_records), model=agent.MODEL, model_digest=digest, driver="native",
        reps=reps, temperature=temperature, seed=seed, candidate=candidate,
        defences=defences, split=split, runs_root=runs_root, run_id=run_id,
    )

    try:
        for test in split_records:
            for rep in range(1, reps + 1):
                with tempfile.TemporaryDirectory(prefix="lab-world-") as tmp:
                    world_dir = build_world_dir(test, Path(tmp))
                    ag, _store = build_agent(world_dir, temperature)
                    t0 = time.time()
                    started = datetime.now(timezone.utc).isoformat()
                    result = agent.run_task(ag, test["user_task"])
                    trace = {
                        "test_id": test["id"],
                        "kind": test["kind"],
                        "goal": test.get("goal"),
                        "technique": test.get("technique", "none"),
                        "place": test.get("place", "none"),
                        "rep": rep,
                        "run_id": run_dir.name,
                        "driver": "native",
                        "model": agent.MODEL,
                        "model_digest": digest,
                        "temperature": temperature,
                        "seed": seed,
                        "user_task": test["user_task"],
                        "expect_ticket": test.get("expect_ticket", False),
                        "expect_asset": test.get("expect_asset"),
                        "defences": list(defences),
                        "tool_calls": result.tool_calls,
                        "final_text": result.final_text,
                        "error": result.error,
                        "started_at": started,
                        "duration_s": round(time.time() - t0, 2),
                    }
                    run_folder.write_trace(run_dir, trace)
                    yield trace
    finally:
        run_folder.lock_run(run_dir)


def _default_make_agent(world_dir: Path, temperature: float, defences: tuple[str, ...] = ()):
    """Build a fresh agent over in-process tools pointed at ``world_dir``."""
    tools, store = agent.make_local_tools(world_dir, defences=defences)
    return agent.build_agent(tools, temperature=temperature), store


def clear_traces() -> None:
    """Legacy: empty the old flat ``results/traces/`` folder.

    Not on any run path any more -- runs write to ``results/runs/<run_id>/``
    and saved runs are never deleted. Only touches the legacy folder.
    """
    if TRACES.exists():
        shutil.rmtree(TRACES)
    TRACES.mkdir(parents=True, exist_ok=True)


def load_traces() -> list[dict]:
    """Legacy: load every trace JSON from the old flat folder into a list."""
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(TRACES.glob("*.json"))
    ]
