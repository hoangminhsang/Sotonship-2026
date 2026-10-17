"""Every run is saved to its own locked ``results/runs/<run_id>/`` folder.

All offline: the agent is a stub, the digest is given, and ``runner.model_digest``
is patched to fail loudly if anything tries to probe Ollama.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lab import corpus, run_folder, runner, world
from lab.assurance import evidence
from lab.assurance.risks import Risk
from lab.governance import POLICY_PATH

ROOT = Path(__file__).resolve().parent.parent

VAULT_CALL = [{"name": "query_assets", "args": {"asset_id": "VAULT-01"}}]


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _records() -> list[dict]:
    tests = corpus.read_jsonl(corpus.TEST_PATH)
    attacks = [t for t in tests if t["kind"] == "attack"][:2]
    benign = [t for t in tests if t["kind"] == "benign"][:2]
    return attacks + benign


@pytest.fixture
def stub(monkeypatch, tmp_path):
    """Stub agent + loud failures for anything that would touch Ollama or wipe traces."""
    calls: list[str] = []

    def fake_run_task(ag, task):
        calls.append(task)
        return SimpleNamespace(final_text="ok", tool_calls=list(VAULT_CALL), error=None)

    def boom(*a, **k):
        raise AssertionError("must not be called on the run path")

    monkeypatch.setattr(runner.agent, "run_task", fake_run_task)
    monkeypatch.setattr(runner, "model_digest", boom)
    monkeypatch.setattr(runner, "clear_traces", boom)
    monkeypatch.setattr(runner, "TRACES", tmp_path / "legacy-traces")
    return SimpleNamespace(calls=calls, make_agent=lambda w, t: (object(), []), root=tmp_path / "runs")


def _run(stub, records, reps=2, **kw):
    return list(
        runner.run_split(
            records, reps=reps, digest=kw.pop("digest", ""), make_agent=stub.make_agent,
            runs_root=stub.root, **kw,
        )
    )


def _only_run_dir(root: Path) -> Path:
    dirs = [p for p in root.iterdir() if p.is_dir()]
    assert len(dirs) == 1, dirs
    return dirs[0]


# --------------------------------------------------------------------------- #
# Layout, manifest, lock.
# --------------------------------------------------------------------------- #
def test_run_writes_locked_folder(stub):
    records = _records()
    traces = _run(stub, records, reps=2, candidate="RC-001", split="test")
    assert len(traces) == 8 and len(stub.calls) == 8

    run_dir = _only_run_dir(stub.root)
    assert run_dir.name.endswith("_RC-001")
    assert all(c.isalnum() or c in "._-" for c in run_dir.name)
    assert traces[0]["run_id"] == run_dir.name and traces[0]["driver"] == "native"

    for name in ("manifest.json", "runs.csv", "summary.csv", "records.jsonl"):
        assert (run_dir / name).is_file(), name
    assert sorted(p.name for p in (run_dir / "traces").iterdir()) == sorted(
        f"{t['id']}__rep{k}.json" for t in records for k in (1, 2)
    )
    assert not (stub.root.parent / "legacy-traces").exists()

    m = json.loads((run_dir / "manifest.json").read_text())
    for key in (
        "run_id", "candidate", "driver", "model", "model_digest", "defences", "temperature",
        "seed", "reps", "split", "n_records", "expected_runs", "records_sha256",
        "corpus_sha256", "rules_sha256", "policy_sha256", "git_commit", "started_at",
        "ended_at", "n_runs", "complete", "seconds_per_run", "files", "locked", "locked_at",
    ):
        assert key in m, key
    assert m["locked"] is True and m["complete"] is True
    assert m["n_runs"] == 8 == m["expected_runs"]
    assert m["driver"] == "native" and m["split"] == "test" and m["reps"] == 2
    assert m["rules_sha256"] == _sha(world.RULES_PATH)
    assert m["policy_sha256"] == _sha(POLICY_PATH)
    assert m["corpus_sha256"]["test.jsonl"] == _sha(corpus.TEST_PATH)
    assert m["records_sha256"] == _sha(run_dir / "records.jsonl")
    assert m["seconds_per_run"]["n"] == 8

    # Every file in the folder is hashed in the manifest, and the hashes match.
    on_disk = {
        p.relative_to(run_dir).as_posix(): _sha(p)
        for p in run_dir.rglob("*") if p.is_file() and p.name != "manifest.json"
    }
    assert m["files"] == on_disk

    # Everything is read-only.
    for p in run_dir.rglob("*"):
        if p.is_file():
            assert not (p.stat().st_mode & 0o222), p

    # runs.csv rows point at trace files that exist, relative to the run folder.
    import pandas as pd

    runs = pd.read_csv(run_dir / "runs.csv")
    assert len(runs) == 8
    assert all((run_dir / tp).is_file() for tp in runs["trace_path"])
    assert bool(runs[runs["kind"] == "attack"]["violated"].all())


def test_existing_run_folder_is_never_written_again(stub):
    records = _records()
    _run(stub, records, reps=1, run_id="fixed-id")
    run_dir = stub.root / "fixed-id"
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in run_dir.rglob("*") if p.is_file()}
    n_calls = len(stub.calls)

    with pytest.raises(run_folder.RunFolderError, match="fixed-id"):
        _run(stub, records, reps=1, run_id="fixed-id")
    assert len(stub.calls) == n_calls  # refused before any agent call

    with pytest.raises(run_folder.RunFolderError, match="locked"):
        run_folder.write_trace(run_dir, {"test_id": "X", "rep": 1})

    assert run_folder.lock_run(run_dir)["locked"] is True  # idempotent
    run_folder.verify_run(run_dir)
    after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in run_dir.rglob("*") if p.is_file()}
    assert before == after


def test_tampered_trace_is_detected(stub):
    _run(stub, _records(), reps=1, run_id="t")
    run_dir = stub.root / "t"
    victim = next((run_dir / "traces").iterdir())
    os.chmod(victim, 0o644)
    victim.write_text(victim.read_text().replace("VAULT-01", "NW-LAP-1001"))
    with pytest.raises(run_folder.RunFolderError, match=victim.name):
        run_folder.verify_run(run_dir)
    with pytest.raises(run_folder.RunFolderError):
        run_folder.load_run(run_dir)


def test_interrupted_run_is_still_locked(stub):
    gen = runner.run_split(_records(), reps=2, digest="", make_agent=stub.make_agent,
                           runs_root=stub.root, run_id="partial")
    next(gen)
    gen.close()
    m = json.loads((stub.root / "partial" / "manifest.json").read_text())
    assert m["locked"] is True and m["complete"] is False and m["n_runs"] == 1
    assert (stub.root / "partial" / "runs.csv").is_file()


def test_duplicate_test_ids_refused_before_running(stub):
    records = _records()
    with pytest.raises(run_folder.RunFolderError, match="duplicate"):
        _run(stub, records + records[:1], reps=1, run_id="dup")
    assert stub.calls == [] and not (stub.root / "dup").exists()


def test_lock_run_exposes_digest_before_model_is_deleted(stub):
    """The workflow run -> lock -> delete model: the digest is in the manifest at start."""
    gen = runner.run_split(_records()[:1], reps=1, digest="sha256:abc", make_agent=stub.make_agent,
                           runs_root=stub.root, run_id="early")
    next(gen)  # the folder and manifest exist after the first run, before the end
    m = json.loads((stub.root / "early" / "manifest.json").read_text())
    assert m["model_digest"] == "sha256:abc" and m["locked"] is False
    gen.close()
    assert run_folder.load_run(stub.root / "early")[0]["model_digest"] == "sha256:abc"


# --------------------------------------------------------------------------- #
# Corpus check (never writes).
# --------------------------------------------------------------------------- #
def test_corpus_check_passes_on_committed_corpus(monkeypatch):
    monkeypatch.setattr(corpus, "write_jsonl", lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrote")))
    before = (corpus.DEV_PATH.read_bytes(), corpus.TEST_PATH.read_bytes())
    hashes = corpus.check_matches_generator()
    assert set(hashes) == {"dev.jsonl", "test.jsonl"}
    assert hashes["test.jsonl"] == _sha(corpus.TEST_PATH)
    assert (corpus.DEV_PATH.read_bytes(), corpus.TEST_PATH.read_bytes()) == before


def test_corpus_check_refuses_changed_corpus(tmp_path):
    dev = tmp_path / "dev.jsonl"
    test = tmp_path / "test.jsonl"
    dev.write_bytes(corpus.DEV_PATH.read_bytes())
    test.write_bytes(corpus.TEST_PATH.read_bytes() + b'{"id": "ATK-9999"}\n')
    with pytest.raises(corpus.CorpusMismatch, match="test.jsonl"):
        corpus.check_matches_generator(dev_path=dev, test_path=test)


def test_pyrit_script_checks_instead_of_rewriting():
    script = (ROOT / "scripts" / "run_pyrit_attack.py").read_text()
    assert "check_matches_generator(" in script
    assert "build_and_write(" not in script
    assert "clear_traces(" not in script
    assert "RESULTS" not in script  # no results/summary.csv or results/runs.csv overwrite
    driver = (ROOT / "lab" / "pyrit_integration.py").read_text()
    assert "runner.TRACES" not in driver
    assert "run_folder" in driver


# --------------------------------------------------------------------------- #
# Gate and assurance from a run folder.
# --------------------------------------------------------------------------- #
def _load_governance():
    spec = importlib.util.spec_from_file_location("run_governance", ROOT / "scripts" / "run_governance.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_governance"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_governance_accepts_run_folder(stub, tmp_path, monkeypatch):
    _run(stub, _records(), reps=2, digest="sha256:stub", candidate="RC-001", split="test",
         run_id="before")
    before = stub.root / "before"
    monkeypatch.setattr(corpus, "build_and_write", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rewrote corpus")))

    gov = _load_governance()
    out = tmp_path / "gov"
    assert gov.main(["--run", str(before), "--out", str(out), "--no-tamper-demo"]) == 0

    import pandas as pd

    summary = pd.read_csv(before / "summary.csv").set_index("metric")
    asr = summary.loc["attack_success_rate"]
    gate_txt = (out / "gate_RC-001.txt").read_text()
    assert f"{int(asr['successes'])}/{int(asr['n'])}" in gate_txt
    fp = json.loads((out / "RC-001.json").read_text())
    assert fp["model_digest"] == "sha256:stub"
    assert fp["test_set_sha256"] == _sha(before / "records.jsonl")
    ev = (out / "evidence_register.md").read_text()
    assert "results/RC-002/runs.csv" not in ev
    assert (out / "RC-002" / "runs.csv").is_file()
    assert str(out / "RC-002" / "runs.csv") in ev


def test_governance_uses_saved_after_run(stub, tmp_path):
    _run(stub, _records(), reps=1, candidate="RC-001", split="test", run_id="b")
    _run(stub, _records(), reps=1, candidate="RC-002", split="test", run_id="a", defences=("D2",))
    gov = _load_governance()
    out = tmp_path / "gov"
    assert gov.main(["--run", str(stub.root / "b"), "--run-after", str(stub.root / "a"),
                     "--out", str(out), "--no-tamper-demo"]) == 0
    ev = (out / "evidence_register.md").read_text()
    assert str(stub.root / "a" / "runs.csv") in ev


def test_governance_run_with_live_is_refused(tmp_path, capsys):
    gov = _load_governance()
    assert gov.main(["--run", str(tmp_path), "--live", "--out", str(tmp_path / "o")]) == 2
    assert "error" in capsys.readouterr().err


def test_governance_reviewer_must_be_a_reviewer_and_not_the_engineer(tmp_path, capsys):
    gov = _load_governance()
    out = tmp_path / "o"
    assert gov.main(["--reviewer", "tom", "--out", str(out), "--no-tamper-demo"]) == 2
    assert "error" in capsys.readouterr().err
    assert gov.main(["--reviewer", "nobody", "--out", str(out), "--no-tamper-demo"]) == 2
    assert not (out / "audit_log.jsonl").exists()


def test_governance_live_subset_has_no_duplicates():
    gov = _load_governance()
    tests = corpus.read_jsonl(corpus.TEST_PATH)
    subset = gov.live_subset(tests, 30)
    ids = [t["id"] for t in subset]
    assert len(ids) == len(set(ids))
    assert sum(t["kind"] == "attack" for t in subset) == min(30, sum(t["kind"] == "attack" for t in tests))


def test_evidence_register_takes_a_proof_file():
    risk = Risk(id="R-02", title="x", violation_class="V_exfiltration", successes=1, runs=2,
                rate=0.5, ci_low=0, ci_high=1, likelihood="High", impact="High",
                control="D2", control_name="D2")
    rows = evidence.build_register([risk], retest_candidate="RC-002", proof_file="/tmp/x/runs.csv")
    assert rows[0].proof_file == "/tmp/x/runs.csv"
    default = evidence.build_register([risk], retest_candidate="RC-002")
    assert default[0].proof_file == "results/RC-002/runs.csv"
