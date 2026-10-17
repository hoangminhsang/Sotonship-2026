#!/usr/bin/env python
"""Run the Northwind attack lab through PyRIT and print the results.

Delivers every corpus attack through a PyRIT ``PromptTarget`` that wraps the
insecure agent, scores each run with the deterministic oracle, writes the same
trace/CSV outputs as the native runner, and prints the ASR tables.

Usage (from the repo root, using the combined environment):

    .venv-pyrit/bin/python scripts/run_pyrit_attack.py                 # full test split, 3 reps
    .venv-pyrit/bin/python scripts/run_pyrit_attack.py --sample 12     # quick rehearsal
    .venv-pyrit/bin/python scripts/run_pyrit_attack.py --split dev --reps 1
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

# Quiet the per-call HTTP, PyRIT, and memory-DB-migration logs so results stand out.
logging.basicConfig(level=logging.ERROR)
for _noisy in ("httpx", "pyrit", "alembic", "sqlalchemy", "asyncio", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

# Make the repo importable no matter where the script is launched from.
import sys
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab import corpus, metrics, pyrit_integration as pyrit, runner, world  # noqa: E402

RESULTS = ROOT / "results"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--split", choices=["dev", "test"], default="test",
                   help="which corpus split to run (default: test)")
    p.add_argument("--reps", type=int, default=3, help="repetitions per test (default: 3)")
    p.add_argument("--sample", type=int, default=None,
                   help="run only the first N records (default: all)")
    p.add_argument("--temperature", type=float, default=0.2)
    return p.parse_args()


def _records(split: str) -> list[dict]:
    path = corpus.TEST_PATH if split == "test" else corpus.DEV_PATH
    if not path.exists():
        corpus.build_and_write()
    return corpus.read_jsonl(path)


async def _run(records: list[dict], reps: int, temperature: float) -> list[dict]:
    return await pyrit.run_split(records, reps=reps, temperature=temperature)


def main() -> None:
    args = parse_args()
    corpus.build_and_write()  # ensure splits exist and are current
    records = _records(args.split)
    if args.sample:
        records = records[: args.sample]
    tests_by_id = {r["id"]: r for r in records}

    print(f"PyRIT attack run | split={args.split} | tests={len(records)} | reps={args.reps}")
    print(f"model={pyrit.agent.MODEL} | digest={runner.model_digest() or '(n/a)'}")
    print("delivering payloads through PyRIT target ...\n")

    runner.clear_traces()
    traces = asyncio.run(_run(records, args.reps, args.temperature))

    rules = world.load_rules()
    runs = metrics.build_runs_frame(traces, rules, tests_by_id)
    metrics.write_outputs(runs, RESULTS / "summary.csv", RESULTS / "runs.csv")

    asr = metrics.attack_success_rate(runs)
    benign = metrics.benign_task_success(runs)
    errors = metrics.error_count(runs)

    print("=" * 60)
    print(f"Overall ASR         : {asr.rate:5.1%}  ({asr.count_str})  "
          f"95% CI [{asr.lo:.1%}, {asr.hi:.1%}]")
    if benign.n:
        print(f"Benign task success : {benign.rate:5.1%}  ({benign.count_str})  "
              f"95% CI [{benign.lo:.1%}, {benign.hi:.1%}]")
    print(f"Error runs          : {errors}")
    print("\nASR per technique (worst first):")
    tech = metrics.asr_per_technique(runs)
    for _, r in tech.iterrows():
        print(f"  {r['technique']:<20} {int(r['violations'])}/{int(r['runs'])}  "
              f"{r['asr']:5.0%}  [{r['ci_low']:.0%}, {r['ci_high']:.0%}]")
    print("=" * 60)
    print(f"\nwrote {RESULTS/'summary.csv'} and {RESULTS/'runs.csv'}")
    print(f"traces in {runner.TRACES}")


if __name__ == "__main__":
    main()
