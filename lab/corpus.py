"""Assemble the combined corpus and make a reproducible dev/test split.

The split is stratified by ``(kind, technique)`` so every technique and both
kinds appear in each half, and it is deterministic for a fixed seed. All
headline numbers come from the test split only; dev is for inspecting and
tuning.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from lab import attacks, benign

ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = ROOT / "corpus"
DEV_PATH = CORPUS_DIR / "dev.jsonl"
TEST_PATH = CORPUS_DIR / "test.jsonl"


def build_corpus(seed: int = 7, benign_count: int = 40) -> list[dict]:
    """Return the combined attack + benign corpus."""
    return attacks.generate(seed) + benign.generate(seed, benign_count)


def split(
    records: list[dict], seed: int = 7, test_frac: float = 0.5
) -> tuple[list[dict], list[dict]]:
    """Stratified dev/test split, deterministic for ``seed``.

    Each ``(kind, technique)`` stratum is shuffled with the seed and cut at
    ``test_frac``, so both splits cover every technique and both kinds.
    """
    rng = random.Random(seed)
    strata: dict[tuple[str, str], list[dict]] = {}
    for rec in records:
        key = (rec["kind"], rec.get("technique", "none"))
        strata.setdefault(key, []).append(rec)

    dev: list[dict] = []
    test: list[dict] = []
    for key in sorted(strata):
        group = sorted(strata[key], key=lambda r: r["id"])
        rng.shuffle(group)
        n_test = max(1, round(len(group) * test_frac)) if len(group) > 1 else 1
        test.extend(group[:n_test])
        dev.extend(group[n_test:])

    dev.sort(key=lambda r: r["id"])
    test.sort(key=lambda r: r["id"])
    return dev, test


def write_jsonl(records: list[dict], path: Path) -> None:
    """Write records as one JSON object per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL corpus file."""
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class CorpusMismatch(RuntimeError):
    """The corpus files on disk differ from what the generator produces."""


def _jsonl_bytes(records: list[dict]) -> bytes:
    return "".join(json.dumps(rec) + "\n" for rec in records).encode("utf-8")


def check_matches_generator(
    seed: int = 7,
    benign_count: int = 40,
    test_frac: float = 0.5,
    dev_path: Path | None = None,
    test_path: Path | None = None,
) -> dict[str, str]:
    """Verify ``corpus/*.jsonl`` equal a fresh generation, without writing anything.

    Returns ``{"dev.jsonl": sha256, "test.jsonl": sha256}`` of the files on disk
    when they match; raises :class:`CorpusMismatch` naming the differing file(s).
    """
    import hashlib

    dev_path = dev_path or DEV_PATH
    test_path = test_path or TEST_PATH
    dev, test = split(build_corpus(seed, benign_count), seed, test_frac)
    hashes: dict[str, str] = {}
    bad: list[str] = []
    for path, records in ((dev_path, dev), (test_path, test)):
        on_disk = path.read_bytes() if path.is_file() else None
        if on_disk != _jsonl_bytes(records):
            bad.append(path.name + ("" if on_disk is not None else " (missing)"))
        else:
            hashes[path.name] = "sha256:" + hashlib.sha256(on_disk).hexdigest()
    if bad:
        raise CorpusMismatch(
            f"corpus file(s) {bad} differ from the seed-{seed} generator output. "
            "Saved results point at the committed corpus, so it is not rewritten. "
            "Decide which corpus is canonical, then regenerate with "
            "`python -m lab.corpus` on purpose."
        )
    return hashes


def build_and_write(seed: int = 7, benign_count: int = 40, test_frac: float = 0.5) -> dict:
    """Build the corpus, split it, write both JSONL files, and return sizes."""
    records = build_corpus(seed, benign_count)
    dev, test = split(records, seed, test_frac)
    write_jsonl(dev, DEV_PATH)
    write_jsonl(test, TEST_PATH)
    return {
        "total": len(records),
        "attacks": sum(r["kind"] == "attack" for r in records),
        "benign": sum(r["kind"] == "benign" for r in records),
        "dev": len(dev),
        "test": len(test),
        "seed": seed,
    }


if __name__ == "__main__":  # pragma: no cover
    print(build_and_write())
