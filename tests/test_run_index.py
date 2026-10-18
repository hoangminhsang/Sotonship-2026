"""``<out>/audit_verify.json`` ("audit-verify/1") and ``<out>/run_index.json`` ("run-index/1").

Two small raw-JSON files the dashboard export reads (lane D). They are not reports: nothing is stamped
onto them, they are not written through ``Report.write``, and they carry the same ``uid`` as
``progress.jsonl``. All offline: DEMO runs and the LIVE stub pattern of tests/test_run_folders.py.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from lab import corpus
from lab.governance import audit

ROOT = Path(__file__).resolve().parent.parent
CANDS = ("RC-001", "RC-002", "RC-003")

VERIFY_KEYS = {"schema", "ok", "bad_seq", "kind", "detail", "entries", "last_hash", "verified_at", "mode",
               "tamper_demo"}
INDEX_KEYS = {"schema", "mode", "uid", "started", "ended", "complete", "model", "digest", "git_commit", "reps",
              "sample", "candidates"}
CANDIDATE_KEYS = {"id", "defences", "run_id", "runs_csv", "summary_csv", "gate", "fingerprint", "verdict"}


@pytest.fixture
def gov():
    spec = importlib.util.spec_from_file_location("run_governance_run_index", ROOT / "scripts" / "run_governance.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_governance_run_index"] = mod
    spec.loader.exec_module(mod)
    return mod


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def events(out: Path) -> list[dict]:
    return [json.loads(x) for x in (out / "progress.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


def of_type(evs, name):
    return [e for e in evs if e["type"] == name]


def parse_utc(text: str) -> datetime:
    when = datetime.fromisoformat(text)
    assert when.utcoffset() == timedelta(0), text
    return when


@pytest.fixture
def demo(gov, tmp_path):
    out = tmp_path / "demo"
    assert gov.main(["--out", str(out)]) == 0
    return out


@pytest.fixture
def live(monkeypatch, tmp_path):
    """The LIVE stub pattern of tests/test_run_folders.py: no Ollama, runs land in tmp_path/runs."""
    from lab import run_folder, runner

    state = SimpleNamespace(root=tmp_path / "runs")
    monkeypatch.setattr(runner.agent, "run_task",
                        lambda ag, task: SimpleNamespace(final_text="ok", tool_calls=[], error=None))
    monkeypatch.setattr(runner, "model_digest", lambda *a, **k: "sha256:" + "ab" * 32)
    monkeypatch.setattr(runner, "_default_make_agent", lambda world_dir, temperature, defences=(): (object(), []))
    monkeypatch.setattr(run_folder, "RUNS_ROOT", state.root)
    return state


# --------------------------------------------------------------------------- #
# audit_verify.json
# --------------------------------------------------------------------------- #
def test_audit_verify_json_has_exactly_the_agreed_keys_and_the_real_log_result(demo):
    text = (demo / "audit_verify.json").read_text(encoding="utf-8")
    assert text.lstrip().startswith("{") and "Mode:" not in text       # raw JSON: no mode label stamped on it
    doc = json.loads(text)
    assert set(doc) == VERIFY_KEYS and doc["schema"] == "audit-verify/1"
    real = audit.verify_log(demo / "audit_log.jsonl")
    assert doc["ok"] is True and doc["bad_seq"] is None and doc["kind"] is None
    assert doc["entries"] == real.entries == 10 and doc["last_hash"] == real.last_hash
    assert re.fullmatch(r"[0-9a-f]{64}", doc["last_hash"]) and doc["detail"] == real.detail
    assert doc["mode"] == "SCRIPTED"
    parse_utc(doc["verified_at"])


def test_audit_verify_json_records_the_forged_copy_when_the_tamper_demo_ran(demo):
    doc = load(demo / "audit_verify.json")
    forged = audit.verify_log(demo / "audit_log_tampered_demo.jsonl")
    assert not forged.ok
    assert doc["tamper_demo"] == {"ok": False, "bad_seq": forged.bad_seq, "kind": forged.kind,
                                  "detail": forged.detail}
    assert doc["ok"] is True                                            # the real log is still intact


def test_audit_verify_json_has_a_null_tamper_demo_without_the_demo(gov, tmp_path):
    out = tmp_path / "gov"
    assert gov.main(["--out", str(out), "--no-tamper-demo"]) == 0
    doc = load(out / "audit_verify.json")
    assert set(doc) == VERIFY_KEYS and doc["tamper_demo"] is None and doc["ok"] is True


def test_audit_verify_json_names_the_live_mode(gov, live, tmp_path):
    out = tmp_path / "gov"
    assert gov.main(["--live", "--sample", "1", "--reps", "1", "--out", str(out), "--no-tamper-demo"]) == 0
    assert load(out / "audit_verify.json")["mode"] == "LIVE"


def test_audit_verify_json_is_not_listed_as_a_report_artifact_and_leaves_no_temp_file(demo):
    names = {e["name"] for e in of_type(events(demo), "artifact")}
    assert "audit_verify.json" not in names and "run_index.json" not in names
    assert not list(demo.glob("*.tmp"))


def test_a_stale_audit_verify_json_is_rotated_not_left_describing_an_old_log(gov, tmp_path, monkeypatch):
    out = tmp_path / "gov"
    out.mkdir()
    (out / "audit_verify.json").write_text('{"stale": true}\n', encoding="utf-8")

    def boom(*a, **k):
        raise RuntimeError("boom before the chain stage")

    monkeypatch.setattr(gov.risks, "build_register", boom)
    with pytest.raises(RuntimeError):
        gov.main(["--out", str(out), "--no-tamper-demo"])
    assert not (out / "audit_verify.json").exists()                      # nothing claims a chain that was not checked
    (old,) = out.glob("audit_verify.*.json")
    assert old.read_text(encoding="utf-8") == '{"stale": true}\n'         # kept, never deleted


# --------------------------------------------------------------------------- #
# run_index.json
# --------------------------------------------------------------------------- #
def test_run_index_of_a_demo_run(demo):
    doc = load(demo / "run_index.json")
    evs = events(demo)
    assert set(doc) == INDEX_KEYS and doc["schema"] == "run-index/1"
    assert doc["mode"] == "SCRIPTED" and doc["complete"] is True
    assert doc["uid"] == evs[0]["uid"] and re.fullmatch(r"[0-9a-f]{12}", doc["uid"])     # same uid as progress.jsonl
    assert parse_utc(doc["started"]) <= parse_utc(doc["ended"])
    (meta,) = of_type(evs, "meta")
    assert doc["model"] == meta["model"] and doc["digest"] == "placeholder (DEMO)"
    assert doc["git_commit"] == evs[0]["git_commit"]
    assert doc["reps"] == 3 and doc["sample"] is None
    assert [c["id"] for c in doc["candidates"]] == list(CANDS)
    assert [c["defences"] for c in doc["candidates"]] == [[], ["D2"], ["D2", "D3"]]
    gates = {e["cand"]: e["verdict"] for e in of_type(evs, "gate")}
    for c in doc["candidates"]:
        assert set(c) == CANDIDATE_KEYS
        assert c["verdict"] == gates[c["id"]]
        assert c["run_id"] is None                                       # DEMO: there is no run folder
    assert [c["verdict"] for c in doc["candidates"]] == ["FAIL", "FAIL", "PASS"]


def test_run_index_paths_inside_the_repo_are_repo_relative_posix_and_exist(gov):
    out = ROOT / "governance_out" / ("test_run_index_" + uuid.uuid4().hex[:8])
    try:
        assert gov.main(["--out", str(out), "--no-tamper-demo"]) == 0
        doc = load(out / "run_index.json")
        rel = out.relative_to(ROOT).as_posix()
        for c in doc["candidates"]:
            cid = c["id"]
            assert c["runs_csv"] == f"{rel}/{cid}/runs.csv" and c["summary_csv"] == f"{rel}/{cid}/summary.csv"
            assert c["gate"] == f"{rel}/gate_{cid}.txt" and c["fingerprint"] == f"{rel}/{cid}.json"
            for key in ("runs_csv", "summary_csv", "gate", "fingerprint"):
                assert "\\" not in c[key] and not c[key].startswith("/") and (ROOT / c[key]).is_file(), (cid, key)
        assert str(ROOT) not in (out / "run_index.json").read_text(encoding="utf-8")
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_run_index_paths_outside_the_repo_never_leak_an_absolute_path(demo):
    text = (demo / "run_index.json").read_text(encoding="utf-8")
    assert str(demo.parent) not in text and str(Path.home()) not in text
    c = load(demo / "run_index.json")["candidates"][0]
    assert c["runs_csv"] == "<outside repo>/runs.csv" and c["gate"] == "<outside repo>/gate_RC-001.txt"


def test_run_index_of_a_live_run_names_the_run_folders_digest_and_sample(gov, live, tmp_path):
    out = tmp_path / "gov"
    assert gov.main(["--live", "--sample", "2", "--reps", "1", "--out", str(out), "--no-tamper-demo"]) == 0
    doc = load(out / "run_index.json")
    evs = events(out)
    assert doc["mode"] == "LIVE" and doc["complete"] is True and doc["uid"] == evs[0]["uid"]
    assert doc["sample"] == 2 and doc["reps"] == 1 and doc["digest"] == "sha256:" + "ab" * 32
    assert doc["model"] == of_type(evs, "meta")[0]["model"]
    ids = [e["run_id"] for e in of_type(evs, "runs_done")]
    assert [c["run_id"] for c in doc["candidates"]] == ids and all(ids)
    for c in doc["candidates"]:
        assert (live.root / c["run_id"] / "runs.csv").is_file() and (live.root / c["run_id"] / "summary.csv").is_file()


def test_run_index_of_a_saved_run_takes_model_and_digest_from_its_manifest(gov, live, tmp_path):
    first = tmp_path / "live"
    assert gov.main(["--live", "--sample", "1", "--reps", "1", "--out", str(first), "--no-tamper-demo"]) == 0
    folder = live.root / of_type(events(first), "runs_done")[0]["run_id"]
    out = tmp_path / "saved"
    assert gov.main(["--run", str(folder), "--out", str(out), "--no-tamper-demo"]) == 0
    doc = load(out / "run_index.json")
    manifest = json.loads((folder / "manifest.json").read_text())
    assert doc["mode"] == "SAVED" and doc["complete"] is True and doc["sample"] is None
    assert doc["model"] == manifest["model"] and doc["digest"] == manifest["model_digest"]
    assert [c["run_id"] for c in doc["candidates"]] == [folder.name, None, None]   # RC-002 / RC-003 are replays


def test_a_failed_run_leaves_a_partial_index(gov, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom in the register")

    monkeypatch.setattr(gov.risks, "build_register", boom)
    out = tmp_path / "gov"
    with pytest.raises(RuntimeError, match="boom in the register"):
        gov.main(["--out", str(out), "--no-tamper-demo"])
    doc = load(out / "run_index.json")
    assert set(doc) == INDEX_KEYS and doc["complete"] is False and doc["uid"] == events(out)[0]["uid"]
    assert parse_utc(doc["started"]) <= parse_utc(doc["ended"])
    (c,) = doc["candidates"]                                             # what was reached before the failure
    assert set(c) == CANDIDATE_KEYS and c["id"] == "RC-001" and c["verdict"] == "FAIL"
    assert c["gate"] and c["fingerprint"] and c["runs_csv"] and c["summary_csv"]
    assert not (out / "audit_verify.json").exists()                      # the chain stage was never reached


def test_an_interrupted_run_leaves_a_partial_index_too(gov, tmp_path, monkeypatch):
    def interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(gov.inventory, "build_card", interrupt)
    out = tmp_path / "gov"
    with pytest.raises(KeyboardInterrupt):
        gov.main(["--out", str(out), "--no-tamper-demo"])
    doc = load(out / "run_index.json")
    assert doc["complete"] is False and doc["mode"] == "SCRIPTED"


def test_a_corpus_mismatch_leaves_a_partial_index_with_no_candidate(gov, tmp_path, monkeypatch):
    def mismatch(*a, **k):
        raise corpus.CorpusMismatch("corpus file(s) ['test.jsonl'] differ from the seed-7 generator output.")

    monkeypatch.setattr(corpus, "check_matches_generator", mismatch)
    out = tmp_path / "gov"
    assert gov.main(["--out", str(out), "--no-tamper-demo"]) == 2
    doc = load(out / "run_index.json")
    assert doc["complete"] is False and doc["candidates"] == [] and doc["model"] is None


def test_a_refused_run_writes_neither_file(gov, tmp_path):
    out = tmp_path / "o"
    assert gov.main(["--run", str(tmp_path / "missing"), "--out", str(out)]) == 2
    assert not out.exists()


def test_a_second_run_in_the_same_folder_overwrites_the_index_with_its_own_uid(gov, tmp_path):
    out = tmp_path / "gov"
    assert gov.main(["--out", str(out), "--no-tamper-demo"]) == 0
    first = load(out / "run_index.json")
    assert gov.main(["--out", str(out), "--no-tamper-demo"]) == 0
    second = load(out / "run_index.json")
    assert second["uid"] != first["uid"] and second["uid"] == events(out)[0]["uid"]


def test_a_failure_to_write_the_index_is_a_warning_not_a_failed_run(gov, tmp_path, monkeypatch, capsys):
    real = gov.write_json

    def refuse(path, document):
        if Path(path).name == "run_index.json":
            raise PermissionError("no write access")
        return real(path, document)

    monkeypatch.setattr(gov, "write_json", refuse)
    out = tmp_path / "gov"
    assert gov.main(["--out", str(out), "--no-tamper-demo"]) == 0
    assert not (out / "run_index.json").exists() and (out / "audit_verify.json").is_file()
    err = capsys.readouterr().err
    assert "run_index.json" in err and "no write access" in err and "Traceback" not in err
