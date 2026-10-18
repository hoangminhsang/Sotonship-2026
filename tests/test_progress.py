"""lab/progress.py -- the progress emitter behind ``<out>/progress.jsonl``.

Unit tests for the file format and the safety rules (never raises, never deletes,
canonical JSON, one write + flush per line, repo-relative paths), then the CLI
hooks in scripts/run_governance.py (stage order, one start and one done, the
failure paths, the committed scripted fixture, the LIVE per-run events).
"""
from __future__ import annotations

import importlib.util
import io
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from lab import corpus, progress
from lab.governance import audit

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "progress_scripted.jsonl"
HEX12 = re.compile(r"^[0-9a-f]{12}$")
ISO_US = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00$")


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Spy(io.StringIO):
    """A file object that records every call, to prove one write() and one flush() per line."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []

    def write(self, text):
        self.calls.append(("write", text))
        return super().write(text)

    def flush(self):
        self.calls.append(("flush",))
        return super().flush()

    def close(self):
        self.calls.append(("close",))
        # keep the buffer readable after the emitter closes it


class Failing:
    """A file object whose write fails with ``exc`` (an OSError, or a KeyboardInterrupt)."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def write(self, text):
        raise self.exc

    def flush(self):
        pass

    def close(self):
        pass


# --------------------------------------------------------------------------- #
# Format and common keys.
# --------------------------------------------------------------------------- #
def test_lines_are_canonical_json(tmp_path):
    np = pytest.importorskip("numpy")
    prog = progress.Progress(tmp_path)
    prog.emit(
        "demo",
        text="caf\u00e9 \u2014 dash\nnew line",
        nan=float("nan"), inf=float("inf"), ninf=-math.inf, npnan=np.float64("nan"),
        npint=np.int64(7), npbool=np.bool_(True), npfloat=np.float64(0.25),
        nested={"b": [1, (2, 3), {"z": None, "a": True}], "a": {4, 2, 3}},
    )
    prog.close()
    raw = (tmp_path / "progress.jsonl").read_text(encoding="utf-8")
    assert raw.endswith("\n") and raw.count("\n") == 1
    assert raw.isascii() and "\\u00e9" in raw and "\\u2014" in raw      # ensure_ascii=True
    assert "NaN" not in raw and "Infinity" not in raw                    # allow_nan=False
    ev = json.loads(raw)
    assert raw.rstrip("\n") == canonical(ev)                              # sorted keys, compact separators
    assert ev["text"] == "caf\u00e9 \u2014 dash\nnew line"
    assert ev["nan"] is None and ev["inf"] is None and ev["ninf"] is None and ev["npnan"] is None
    assert ev["npint"] == 7 and ev["npbool"] is True and ev["npfloat"] == 0.25
    assert ev["nested"] == {"a": [2, 3, 4], "b": [1, [2, 3], {"a": True, "z": None}]}


def test_common_keys_and_sequence(tmp_path):
    prog = progress.Progress(tmp_path)
    assert HEX12.match(prog.uid)
    prog.emit("one")
    prog.emit("two", cand="RC-001", stage="RC-001:runs", extra=1)
    prog.close()
    first, second = read_events(tmp_path / "progress.jsonl")
    for seq, ev in enumerate((first, second), start=1):
        assert ev["v"] == 1 and ev["seq"] == seq and ev["uid"] == prog.uid
        assert ISO_US.match(ev["t"]), ev["t"]                            # UTC, microseconds
        assert {"v", "seq", "t", "uid", "type", "cand", "stage"} <= set(ev)
    assert first["type"] == "one" and first["cand"] is None and first["stage"] is None
    assert second["type"] == "two" and second["cand"] == "RC-001" and second["stage"] == "RC-001:runs"
    assert second["extra"] == 1
    other = progress.Progress(tmp_path / "other")
    assert other.uid != prog.uid and HEX12.match(other.uid)


def test_extra_fields_cannot_overwrite_the_common_keys(tmp_path):
    prog = progress.Progress(tmp_path, uid="abcdef012345")
    prog.emit("x", v=99, seq=99, uid="no", t="no")  # the common keys always win
    prog.close()
    (ev,) = read_events(tmp_path / "progress.jsonl")
    assert ev["v"] == 1 and ev["seq"] == 1 and ev["uid"] == "abcdef012345" and ev["t"] != "no"
    assert ev["type"] == "x"


def test_one_write_and_one_flush_per_line(tmp_path):
    spy = Spy()
    prog = progress.Progress(tmp_path, opener=lambda path: spy)
    for n in range(3):
        prog.emit("tick", n=n)
    calls = [c for c in spy.calls if c[0] in ("write", "flush")]
    assert [c[0] for c in calls] == ["write", "flush"] * 3
    for kind, *rest in calls:
        if kind == "write":
            text = rest[0]
            assert text.endswith("\n") and text.count("\n") == 1          # the whole line in one write()
            assert json.loads(text)["type"] == "tick"


# --------------------------------------------------------------------------- #
# Safety: never raises, never catches BaseException, never deletes.
# --------------------------------------------------------------------------- #
def test_write_failure_is_swallowed_disables_and_prints_one_line(tmp_path, capsys):
    prog = progress.Progress(tmp_path, opener=lambda path: Failing(OSError("disk full")))
    prog.emit("a")                               # must not raise
    assert prog.disabled
    for _ in range(5):
        prog.emit("b")                           # silent no-ops now
    prog.begin_stage("RC-001:runs", "title")
    prog.end_stage()
    prog.close()
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1 and lines[0].startswith("progress:") and "disk full" in lines[0], err


def test_unserialisable_value_is_swallowed(tmp_path, capsys):
    class Bad:
        def __str__(self):
            raise RuntimeError("cannot render")

    prog = progress.Progress(tmp_path)
    prog.emit("a", bad=Bad())
    assert prog.disabled
    prog.emit("b")
    assert len([ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]) == 1


def test_open_failure_is_swallowed(tmp_path, capsys):
    def boom(path):
        raise PermissionError("read-only folder")

    prog = progress.Progress(tmp_path, opener=boom)
    prog.emit("a")
    assert prog.disabled
    assert capsys.readouterr().err.count("\n") == 1


def test_keyboard_interrupt_is_not_swallowed(tmp_path):
    """Ctrl-C must reach the pipeline so a partial run folder still gets locked."""
    prog = progress.Progress(tmp_path, opener=lambda path: Failing(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        prog.emit("a")
    assert not prog.disabled


def test_existing_file_is_rotated_never_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(progress, "_stamp", lambda: "20261018T061500Z")
    old = tmp_path / "progress.jsonl"
    old.write_bytes(b'{"old":1}\n')

    first = progress.Progress(tmp_path)
    first.emit("a")
    first.close()
    rotated = tmp_path / "progress.20261018T061500Z.jsonl"
    assert rotated.read_bytes() == b'{"old":1}\n'                         # old file intact, byte for byte
    assert [e["type"] for e in read_events(old)] == ["a"] and read_events(old)[0]["seq"] == 1

    second = progress.Progress(tmp_path)                                   # same second: no overwrite
    second.emit("b")
    second.close()
    assert rotated.read_bytes() == b'{"old":1}\n'
    again = tmp_path / "progress.20261018T061500Z-2.jsonl"
    assert [e["type"] for e in read_events(again)] == ["a"]               # the first run's file, kept
    assert [e["type"] for e in read_events(old)] == ["b"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "progress.20261018T061500Z-2.jsonl", "progress.20261018T061500Z.jsonl", "progress.jsonl",
    ]


def test_creates_the_folder_when_not_deferred(tmp_path):
    out = tmp_path / "a" / "b"
    prog = progress.Progress(out)
    prog.emit("a")
    prog.close()
    assert (out / "progress.jsonl").is_file()


def test_deferred_emitter_touches_nothing_until_activated(tmp_path):
    out = tmp_path / "later"
    prog = progress.Progress(out, deferred=True)
    prog.emit("first")
    prog.emit("second")
    assert not out.exists()                                                # no folder, no file
    prog.close()
    assert not out.exists()                                                # dropped, never written

    prog2 = progress.Progress(out, deferred=True)
    prog2.emit("first")
    out.mkdir()
    (out / "progress.jsonl").write_text("old\n")                           # the caller made the folder
    prog2.activate()                                                       # rotate, then flush what was queued
    prog2.emit("second")
    prog2.close()
    events = read_events(out / "progress.jsonl")
    assert [(e["seq"], e["type"]) for e in events] == [(1, "first"), (2, "second")]
    assert len(list(out.glob("progress.*.jsonl"))) == 1


# --------------------------------------------------------------------------- #
# Paths.
# --------------------------------------------------------------------------- #
def test_display_path_is_repo_relative_or_outside(tmp_path):
    repo = tmp_path / "repo"
    (repo / "results" / "x").mkdir(parents=True)
    assert progress.display_path(repo / "results" / "x" / "runs.csv", repo) == "results/x/runs.csv"
    assert progress.display_path(str(repo / "governance_out"), repo) == "governance_out"
    outside = tmp_path / "elsewhere" / "gov" / "gate_RC-001.txt"
    assert progress.display_path(outside, repo) == "<outside repo>/gate_RC-001.txt"
    assert progress.display_path(tmp_path, repo) == f"<outside repo>/{tmp_path.name}"
    assert progress.display_path(None, repo) is None and progress.display_path("", repo) is None
    assert "\\" not in progress.display_path(repo / "a" / "b", repo)       # POSIX separators only


def test_path_values_in_events_never_leak_an_absolute_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    prog = progress.Progress(tmp_path / "out", repo=repo)
    prog.emit("artifact", path=repo / "results" / "a.csv", other=tmp_path / "secret" / "b.csv",
              many=[repo / "x", tmp_path / "y" / "z"])
    prog.close()
    (ev,) = read_events(tmp_path / "out" / "progress.jsonl")
    assert ev["path"] == "results/a.csv" and ev["other"] == "<outside repo>/b.csv"
    assert ev["many"] == ["x", "<outside repo>/z"]
    assert str(tmp_path) not in json.dumps(ev)


def test_relativise_argv():
    repo = ROOT
    argv = ["--out", str(repo / "governance_out" / "x"), "--run=/somewhere/else/run1",
            "--reps", "3", "--engineer", "tom", str(Path("/abs/stray").resolve())]
    got = progress.relativise_argv(argv, repo)
    assert got == ["--out", "governance_out/x", "--run=<outside repo>/run1",
                   "--reps", "3", "--engineer", "tom", "<outside repo>/stray"]


def test_error_text_is_scrubbed_of_the_home_folder_and_repo():
    text = f"[Errno 2] No such file: '{ROOT}/results/x' and '{Path.home()}/secret.txt'"
    got = progress.scrub(text)
    assert str(ROOT) not in got and str(Path.home()) not in got
    assert "results/x" in got and "~/secret.txt" in got
    assert len(progress.scrub("x" * 500, limit=200)) == 200


# --------------------------------------------------------------------------- #
# Stages.
# --------------------------------------------------------------------------- #
def test_stages_close_the_previous_one_and_carry_cand_and_stage(tmp_path):
    prog = progress.Progress(tmp_path)
    prog.begin_stage("setup", "Setup")
    prog.emit("meta")
    prog.begin_stage("RC-001:runs", "1. RC-001")
    prog.emit("runs_done")
    prog.begin_stage("trace", "Traceability")
    prog.end_stage("error")
    prog.end_stage("ok")                          # nothing open: no event
    prog.close()
    evs = read_events(tmp_path / "progress.jsonl")
    got = [(e["type"], e["stage"], e["cand"], e.get("status")) for e in evs]
    assert got == [
        ("stage_start", "setup", None, None),
        ("meta", "setup", None, None),
        ("stage_done", "setup", None, "ok"),
        ("stage_start", "RC-001:runs", "RC-001", None),
        ("runs_done", "RC-001:runs", "RC-001", None),
        ("stage_done", "RC-001:runs", "RC-001", "ok"),
        ("stage_start", "trace", None, None),
        ("stage_done", "trace", None, "error"),
    ]
    done = [e for e in evs if e["type"] == "stage_done"]
    assert all(isinstance(e["dur_s"], float) and e["dur_s"] >= 0 for e in done)
    assert evs[0]["title"] == "Setup" and done[1]["title"] == "1. RC-001"


# --------------------------------------------------------------------------- #
# Best-effort git commit.
# --------------------------------------------------------------------------- #
def test_git_commit_is_best_effort(monkeypatch):
    assert progress.git_commit(ROOT) is None or re.fullmatch(r"[0-9a-f]{40}", progress.git_commit(ROOT))

    def boom(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(progress.subprocess, "run", boom)
    assert progress.git_commit(ROOT) is None


def test_short_hash_helper():
    assert progress.short("sha256:" + "ab" * 32) == "ab" * 6
    assert progress.short("ab" * 32) == "ab" * 6
    assert progress.short(None) is None and progress.short("") is None


# --------------------------------------------------------------------------- #
# The running counters behind run_done.
# --------------------------------------------------------------------------- #
def _demo_scored():
    from lab import corpus, metrics, world
    from lab.demo import demo_traces

    tests = corpus.read_jsonl(corpus.TEST_PATH)
    traces = demo_traces(tests, reps=3)
    runs = metrics.build_runs_frame(traces, world.load_rules(), {t["id"]: t for t in tests})
    return traces, runs


def test_running_stats_equal_gate_summarise_at_every_checked_prefix():
    from lab.governance import gate

    traces, runs = _demo_scored()
    stats = progress.RunningStats()
    n = len(runs)
    for i, (tr, row) in enumerate(zip(traces, runs.to_dict("records")), start=1):
        running = progress.run_fields(i, n, tr, row, stats)["running"]
        if i <= 3 or i % 37 == 0 or i == n:
            want = gate.summarise(runs.iloc[:i])
            for key in ("attack_runs", "attack_violations", "benign_n", "errors", "runs"):
                assert running[key] == want[key], (i, key)
            for key in ("asr", "asr_lo", "asr_hi"):
                assert running[key] == pytest.approx(want[key]), (i, key)
            if want["benign_n"]:
                assert running["benign_rate"] == pytest.approx(want["benign_rate"]), i
            else:
                assert running["benign_rate"] is None                    # not measured, not a clean 0%
    assert running["runs"] == n


def test_running_stats_follow_the_error_counting_rule():
    """An error run counts only if it broke a rule; every error run is in `errors`; a benign error is never a success."""
    import pandas as pd

    from lab.governance import gate

    steps = [  # kind, violated, error, benign_success
        ("attack", True, "", False), ("attack", False, "", False),
        ("attack", False, "model timed out", False),      # error, no violation: not counted
        ("attack", True, "model timed out", False),       # error that broke a rule: counted
        ("benign", False, "", True), ("benign", False, "boom", False),   # benign error: not counted
        ("benign", False, "", False),
    ]
    stats = progress.RunningStats()
    rows = []
    for i, (kind, violated, error, ok) in enumerate(steps, start=1):
        rows.append({"kind": kind, "violated": violated, "error": error, "benign_success": ok, "attempted": violated})
        got = stats.add(kind, violated, error, ok)
        want = gate.summarise(pd.DataFrame(rows))
        for key in ("attack_runs", "attack_violations", "benign_n", "errors", "runs"):
            assert got[key] == want[key], (i, key)
        if want["benign_n"]:
            assert got["benign_rate"] == pytest.approx(want["benign_rate"])
    assert (got["attack_runs"], got["attack_violations"], got["benign_n"], got["errors"]) == (3, 2, 2, 3)

    empty = progress.RunningStats().add("benign", False, "", True)       # no attack run yet
    assert empty["asr"] is None and empty["asr_lo"] is None and empty["asr_hi"] is None
    assert empty["attack_runs"] == 0 and empty["benign_rate"] == 1.0


def test_run_fields_shape_limits_and_json_safety():
    np = pytest.importorskip("numpy")
    trace = {
        "tool_calls": [{"name": "query_assets", "args": {"asset_id": "VAULT-01"}, "blocked": True},
                       {"name": "read_doc", "args": {"name": "x.md"}}],
        "duration_s": 1.25,
    }
    row = {
        "test_id": "ATK-0001", "kind": "attack", "goal": float("nan"), "technique": "T1_plain",
        "rep": np.int64(2), "violated": np.bool_(True), "violation_classes": "V_a;V_b", "attempted": np.bool_(True),
        "tool_calls": "x" * 400, "error": "e" * 300, "benign_success": np.bool_(False),
    }
    got = progress.run_fields(3, 10, trace, row, progress.RunningStats())
    assert got["i"] == 3 and got["n"] == 10 and got["rep"] == 2 and got["classes"] == ["V_a", "V_b"]
    assert got["calls"] == 2 and got["blocked"] == 1 and got["duration_s"] == 1.25
    assert len(got["tool_calls"]) == 300 and len(got["error"]) == 200
    assert got["benign_success"] is None                                  # only benign runs have one
    ev = json.loads(progress.dumps(got))
    assert ev["goal"] is None and ev["violated"] is True and ev["attempted"] is True
    quiet = progress.run_fields(1, 1, {"tool_calls": []}, {**row, "error": "", "violation_classes": "",
                                                           "kind": "benign", "benign_success": True},
                                progress.RunningStats())
    assert quiet["error"] is None and quiet["classes"] == [] and quiet["benign_success"] is True
    assert quiet["duration_s"] is None and quiet["calls"] == 0 and quiet["blocked"] == 0


# =========================================================================== #
# The CLI hooks: scripts/run_governance.py writes <out>/progress.jsonl.
# =========================================================================== #
CANDS = ("RC-001", "RC-002", "RC-003")
EXPECTED_STAGES = [
    "setup",
    "RC-001:runs", "RC-001:gate", "RC-001:decide", "RC-001:assure",
    "RC-002:runs", "RC-002:gate", "RC-002:decide", "RC-002:assure",
    "RC-003:runs", "RC-003:gate", "RC-003:decide", "RC-003:assure",
    "trace", "summary", "chain", "done",
]


@pytest.fixture(scope="module")
def gov():
    spec = importlib.util.spec_from_file_location("run_governance_progress", ROOT / "scripts" / "run_governance.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_governance_progress"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def demo(gov, tmp_path_factory):
    """One full DEMO run (tamper demo on), shared by the read-only checks below."""
    out = tmp_path_factory.mktemp("progress") / "demo"      # fixed leaf name: paths in events are deterministic
    assert gov.main(["--out", str(out)]) == 0
    return SimpleNamespace(out=out, events=read_events(out / "progress.jsonl"))


def of_type(events, type_):
    return [e for e in events if e["type"] == type_]


def gate_counts(text: str) -> tuple[int, int]:
    m = re.search(r"attack_success_rate.*?\((\d+)/(\d+)", text)
    return int(m.group(1)), int(m.group(2))


def test_demo_has_one_start_one_done_and_the_stage_order(demo):
    ev = demo.events
    assert [e["type"] for e in ev if e["type"] in ("pipeline_start", "pipeline_done")] == [
        "pipeline_start", "pipeline_done"]
    assert ev[0]["type"] == "pipeline_start" and ev[-1]["type"] == "pipeline_done"
    assert ev[-1]["status"] == "ok" and ev[-1]["exit_code"] == 0 and ev[-1]["error"] is None
    assert [e["stage"] for e in of_type(ev, "stage_start")] == EXPECTED_STAGES
    assert [(e["stage"], e["status"]) for e in of_type(ev, "stage_done")] == [(s, "ok") for s in EXPECTED_STAGES]

    # Stages never overlap, and every event between a start and a done carries that stage.
    open_stage = None
    for e in ev:
        if e["type"] == "stage_start":
            assert open_stage is None, e
            open_stage = e["stage"]
        elif e["type"] == "stage_done":
            assert e["stage"] == open_stage, e
            open_stage = None
        elif e["type"] in ("pipeline_start", "pipeline_done"):
            assert e["stage"] is None
        else:
            assert e["stage"] == open_stage, e
        assert e["cand"] == (e["stage"].split(":")[0] if e["stage"] and ":" in e["stage"] else e["cand"]), e
    assert open_stage is None


def test_demo_common_keys_are_consistent(demo):
    ev = demo.events
    assert [e["seq"] for e in ev] == list(range(1, len(ev) + 1))
    assert {e["uid"] for e in ev} == {ev[0]["uid"]} and HEX12.match(ev[0]["uid"])
    assert all(e["v"] == 1 and ISO_US.match(e["t"]) for e in ev)
    stamps = [datetime.fromisoformat(e["t"]) for e in ev]
    assert stamps == sorted(stamps) and all(s.utcoffset() == timedelta(0) for s in stamps)
    raw = (demo.out / "progress.jsonl").read_text(encoding="utf-8")
    assert raw.isascii() and "\r" not in raw and "NaN" not in raw
    for line in raw.splitlines():
        assert line == canonical(json.loads(line))


def test_demo_event_counts_and_per_candidate_order(demo):
    ev = demo.events
    by = Counter(e["type"] for e in ev)
    n_runs = len(corpus.read_jsonl(corpus.TEST_PATH)) * 3
    assert by["run_done"] == 3 * n_runs and by["run_start"] == 0           # run_start is LIVE only
    assert (by["meta"], by["runs_done"], by["fingerprint"], by["gate"]) == (1, 3, 3, 3)
    assert (by["audit"], by["risks"], by["tickets"], by["evidence"]) == (10, 3, 1, 2)
    assert (by["trace"], by["summary"], by["chain"]) == (1, 1, 2)          # chain: the real log, then the forged copy
    for cid in CANDS:
        order = [e["type"] for e in ev if e["cand"] == cid and e["type"] in ("runs_done", "fingerprint", "gate")]
        assert order == ["runs_done", "fingerprint", "gate"], cid


def test_pipeline_start_and_meta(demo):
    start = demo.events[0]
    assert start["mode"] == "SCRIPTED" and start["platform"] == sys.platform
    assert start["argv"] == ["--out", "<outside repo>/demo"] and start["out"] == "<outside repo>/demo"
    assert start["git_commit"] is None or re.fullmatch(r"[0-9a-f]{40}", start["git_commit"])
    assert str(demo.out.parent) not in json.dumps(demo.events)             # no absolute path anywhere

    (meta,) = of_type(demo.events, "meta")
    tests = corpus.read_jsonl(corpus.TEST_PATH)
    assert meta["stage"] == "setup" and meta["digest12"] == "placeholder (DEMO)"
    assert meta["test_set"]["records"] == len(tests)
    assert meta["test_set"]["attacks"] == sum(t["kind"] == "attack" for t in tests)
    assert meta["test_set"]["benign"] == sum(t["kind"] == "benign" for t in tests)
    assert HEX12.match(meta["test_set"]["sha12"]) and meta["reps"] == 3 and meta["sample"] is None
    assert meta["model"] and meta["policy_version"] is not None
    assert [(c["id"], c["defences"], c["expected_runs"]) for c in meta["candidates"]] == [
        ("RC-001", [], len(tests) * 3), ("RC-002", ["D2"], len(tests) * 3), ("RC-003", ["D2", "D3"], len(tests) * 3)]


def test_run_done_events_carry_running_counters_that_match_runs_done(demo):
    n_runs = len(corpus.read_jsonl(corpus.TEST_PATH)) * 3
    summaries = {e["cand"]: e["summary"] for e in of_type(demo.events, "runs_done")}
    for cid in CANDS:
        runs = [e for e in of_type(demo.events, "run_done") if e["cand"] == cid]
        assert [e["i"] for e in runs] == list(range(1, n_runs + 1)) and {e["n"] for e in runs} == {n_runs}
        assert {e["stage"] for e in runs} == {f"{cid}:runs"}
        keys = ("attack_runs", "attack_violations", "benign_n", "errors", "runs")
        for a, b in zip(runs, runs[1:]):                                   # counters only ever grow
            assert all(b["running"][k] >= a["running"][k] for k in keys)
        last = runs[-1]["running"]
        assert all(last[k] == summaries[cid][k] for k in keys)
        for k in ("asr", "asr_lo", "asr_hi", "benign_rate"):
            assert last[k] == pytest.approx(summaries[cid][k])
    first = of_type(demo.events, "run_done")[0]
    assert first["kind"] in ("attack", "benign") and first["test_id"] and first["rep"] == 1
    assert isinstance(first["violated"], bool) and isinstance(first["classes"], list)
    assert first["calls"] >= 1 and first["blocked"] == 0 and first["error"] is None


def test_runs_done_and_fingerprint_events(demo):
    for e in of_type(demo.events, "runs_done"):
        assert e["run_id"] is None and e["runs_csv"] == "<outside repo>/runs.csv"      # DEMO: no run folder
        text = (demo.out / f"gate_{e['cand']}.txt").read_text()
        assert (e["summary"]["attack_violations"], e["summary"]["attack_runs"]) == gate_counts(text)
    fps = of_type(demo.events, "fingerprint")
    assert [e["changed_vs_prev"] for e in fps] == [None, True, True]
    for e in fps:
        card = json.loads((demo.out / f"{e['cand']}.json").read_text())
        assert e["identity12"] == card["identity_hash"].split(":", 1)[1][:12]


def test_gate_events_match_the_gate_files(demo):
    gates = {e["cand"]: e for e in of_type(demo.events, "gate")}
    assert [gates[c]["verdict"] for c in CANDS] == ["FAIL", "FAIL", "PASS"]
    for cid, g in gates.items():
        text = (demo.out / f"gate_{cid}.txt").read_text()
        assert text == "[SCRIPTED] " + "\n".join(g["lines"]) + "\n"        # the very lines the reader sees
        assert g["file"] == f"<outside repo>/gate_{cid}.txt"
        assert [c["name"] for c in g["checks"] if not c["ok"]] == g["reasons"]
        assert {"name", "ok", "value", "limit", "note"} == set(g["checks"][0])
        assert g["stage"] == f"{cid}:gate" and bool(g["reasons"]) == (g["verdict"] == "FAIL")
    assert gates["RC-001"]["reasons"] == ["attack_success_rate"] and gates["RC-003"]["reasons"] == []
    asr = next(c for c in gates["RC-001"]["checks"] if c["name"] == "attack_success_rate")
    assert asr["value"] > asr["limit"] and isinstance(asr["limit"], float)


def test_audit_events_mirror_the_log(demo):
    log = audit.read_log(demo.out / "audit_log.jsonl")
    evs = of_type(demo.events, "audit")
    assert [e["entry"] for e in evs] == [r["seq"] for r in log] == list(range(1, 11))
    assert [e["hash12"] for e in evs] == [r["hash"][:12] for r in log]
    assert [e["event"] for e in evs] == [r["event"] for r in log]
    assert [e["cand"] for e in evs] == [r["candidate_id"] for r in log]
    assert all(e["scripted"] is True for e in evs)
    assert {e["stage"] for e in evs} == {"RC-001:decide", "RC-002:decide", "RC-003:decide"}
    assert [(e["cand"], e["decision"], e["reviewer"]) for e in evs if e["event"] == "decision"] == [
        ("RC-001", "BLOCK", "sarah"), ("RC-002", "BLOCK", "sarah"), ("RC-003", "APPROVE", "sarah")]
    (refused,) = [e for e in evs if e["event"] == "decision_refused"]
    assert refused["reviewer"] == "tom" and refused["reason"] and refused["decision"] is None
    assert {e["by"] for e in evs if e["event"] == "submit"} == {"tom"}
    assert [e["verdict"] for e in evs if e["event"] == "verdict"] == ["FAIL", "FAIL", "PASS"]


def test_assurance_events(demo):
    risks = of_type(demo.events, "risks")
    assert [e["cand"] for e in risks] == list(CANDS) and all(e["rows"] and "controls_used" in e for e in risks)
    row = risks[0]["rows"][0]
    assert {"id", "title", "violation_class", "successes", "runs", "rate", "likelihood", "impact", "control",
            "owner", "status"} <= set(row)
    assert risks[0]["rows"][0]["status"] == "Open" and risks[0]["controls_used"] == ["D2", "D3"]
    (tickets,) = of_type(demo.events, "tickets")
    assert tickets["cand"] == "RC-001" and [t["id"] for t in tickets["rows"]] == ["FIX-01", "FIX-02", "FIX-03"]
    ev = of_type(demo.events, "evidence")
    assert [e["cand"] for e in ev] == ["RC-002", "RC-003"]
    assert all(r["proof_file"] == "<outside repo>/runs.csv" for e in ev for r in e["rows"])
    assert str(demo.out) not in json.dumps(ev)


def test_artifact_events_name_every_file_written(demo):
    arts = of_type(demo.events, "artifact")
    names = [a["name"] for a in arts]
    assert len(names) == len(set(names))
    assert all((demo.out / n).is_file() for n in names)
    assert {a["path"] for a in arts} == {f"<outside repo>/{n}" for n in names}
    expected = {"RC-001.json", "RC-002.json", "RC-003.json", "gate_RC-001.txt", "gate_RC-002.txt", "gate_RC-003.txt",
                "inventory_card.md", "risk_register_before.md", "fix_tickets.md", "risk_register_RC-002.md",
                "evidence_register_RC-002.md", "risk_register_RC-003.md", "evidence_register_RC-003.md",
                "risk_register_after.md", "evidence_register.md", "traceability.md", "manager_summary.md",
                "audit_log_tampered_demo.jsonl"}
    assert set(names) == expected
    kinds = {a["name"]: a["kind"] for a in arts}
    assert kinds["RC-001.json"] == "fingerprint" and kinds["gate_RC-002.txt"] == "gate"
    assert kinds["manager_summary.md"] == "summary" and kinds["audit_log_tampered_demo.jsonl"] == "tamper_demo"
    assert kinds["risk_register_after.md"] == "risk_register" and kinds["evidence_register.md"] == "evidence"


def test_trace_summary_and_chain_events(demo):
    (trace,) = of_type(demo.events, "trace")
    text = (demo.out / "traceability.md").read_text()
    assert f"**Traceability for {trace['finding']}**" in text
    assert trace["steps_n"] == len(re.findall(r"^\d+\. ", text, flags=re.M)) > 0
    (summary,) = of_type(demo.events, "summary")
    md = (demo.out / "manager_summary.md").read_text()
    assert summary["bullets"] == [ln[2:].replace("**", "") for ln in md.splitlines() if ln.startswith("- ")]
    real = audit.verify_log(demo.out / "audit_log.jsonl")
    forged = audit.verify_log(demo.out / "audit_log_tampered_demo.jsonl")
    first, second = of_type(demo.events, "chain")
    for c in (first, second):
        assert (c["ok"], c["entries"], c["head12"], c["detail"]) == (True, 10, real.last_hash[:12], real.detail)
    assert first["tamper"] is None
    assert second["tamper"] == {"ok": False, "bad_seq": forged.bad_seq, "kind": forged.kind}
    assert first["stage"] == second["stage"] == "chain"


def test_pipeline_done_headline_matches_the_verdicts(demo):
    done = demo.events[-1]
    cands = done["headline"]["cands"]
    assert [(c["id"], c["verdict"]) for c in cands] == [("RC-001", "FAIL"), ("RC-002", "FAIL"), ("RC-003", "PASS")]
    summaries = {e["cand"]: e["summary"] for e in of_type(demo.events, "runs_done")}
    for c in cands:
        s = summaries[c["id"]]
        for k in ("asr", "asr_lo", "asr_hi", "attack_runs", "attack_violations", "benign_rate", "benign_n"):
            assert c[k] == pytest.approx(s[k]), (c["id"], k)
    assert isinstance(done["dur_s"], float) and done["dur_s"] >= 0


# --------------------------------------------------------------------------- #
# Folders, rotation, refusals and failures.
# --------------------------------------------------------------------------- #
def test_second_run_in_the_same_folder_rotates_the_progress_file(gov, tmp_path):
    out = tmp_path / "gov"
    assert gov.main(["--out", str(out), "--no-tamper-demo"]) == 0
    first = (out / "progress.jsonl").read_bytes()
    assert gov.main(["--out", str(out), "--no-tamper-demo"]) == 0
    (rotated,) = [p for p in out.glob("progress.*.jsonl")]
    assert rotated.read_bytes() == first                                   # the old file, byte for byte
    old, new = read_events(rotated), read_events(out / "progress.jsonl")
    assert old[0]["uid"] != new[0]["uid"] and new[0]["seq"] == 1
    assert [e["type"] for e in new].count("pipeline_start") == 1 == [e["type"] for e in new].count("pipeline_done")


def test_a_refused_run_leaves_no_folder_and_no_progress_file(gov, tmp_path, capsys):
    out = tmp_path / "o"
    assert gov.main(["--run", str(tmp_path / "missing"), "--out", str(out)]) == 2
    assert "error" in capsys.readouterr().err and not out.exists()
    existing = tmp_path / "keep"
    existing.mkdir()
    (existing / "progress.jsonl").write_text("previous run\n")
    assert gov.main(["--run", str(tmp_path / "missing"), "--out", str(existing)]) == 2
    assert [p.name for p in existing.iterdir()] == ["progress.jsonl"]      # not rotated by a refused run
    assert (existing / "progress.jsonl").read_text() == "previous run\n"


def test_corpus_mismatch_is_recorded_as_an_error(gov, tmp_path, monkeypatch):
    def mismatch(*a, **k):
        raise corpus.CorpusMismatch("corpus file(s) ['test.jsonl'] differ from the seed-7 generator output.")

    monkeypatch.setattr(corpus, "check_matches_generator", mismatch)
    out = tmp_path / "gov"
    assert gov.main(["--out", str(out), "--no-tamper-demo"]) == 2
    ev = read_events(out / "progress.jsonl")
    assert [e["type"] for e in ev] == ["pipeline_start", "pipeline_done"]
    assert ev[-1]["status"] == "error" and ev[-1]["exit_code"] == 2 and "test.jsonl" in ev[-1]["error"]


def test_an_exception_is_recorded_and_still_raised(gov, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom in the register")

    monkeypatch.setattr(gov.risks, "build_register", boom)
    out = tmp_path / "gov"
    with pytest.raises(RuntimeError, match="boom in the register"):
        gov.main(["--out", str(out), "--no-tamper-demo"])
    ev = read_events(out / "progress.jsonl")
    assert ev[-2]["type"] == "stage_done" and ev[-2]["stage"] == "RC-001:assure" and ev[-2]["status"] == "error"
    done = ev[-1]
    assert done["type"] == "pipeline_done" and done["status"] == "error" and done["exit_code"] == 1
    assert "RuntimeError" in done["error"] and "boom in the register" in done["error"]
    assert [c["id"] for c in done["headline"]["cands"]] == ["RC-001"]       # what was gated before it stopped
    assert not [e for e in ev if e["type"] == "pipeline_start"][1:]


def test_ctrl_c_is_recorded_and_still_raised(gov, tmp_path, monkeypatch):
    def interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(gov.inventory, "build_card", interrupt)
    out = tmp_path / "gov"
    with pytest.raises(KeyboardInterrupt):
        gov.main(["--out", str(out), "--no-tamper-demo"])
    ev = read_events(out / "progress.jsonl")
    assert ev[-2]["type"] == "stage_done" and ev[-2]["status"] == "error"
    assert ev[-1]["type"] == "pipeline_done" and ev[-1]["status"] == "interrupted" and ev[-1]["exit_code"] == 130


def test_a_broken_emitter_never_changes_the_run(gov, tmp_path, monkeypatch, capsys):
    def no_access(path):
        raise PermissionError("no write access")

    monkeypatch.setattr(progress, "_open_new", no_access)
    out = tmp_path / "gov"
    assert gov.main(["--out", str(out), "--no-tamper-demo"]) == 0
    captured = capsys.readouterr()
    err_lines = [ln for ln in captured.err.splitlines() if ln.strip()]
    assert len(err_lines) == 1 and err_lines[0].startswith("progress: disabled")
    assert not (out / "progress.jsonl").exists()
    assert audit.verify_log(out / "audit_log.jsonl").ok and (out / "manager_summary.md").is_file()
    assert "[SCRIPTED] Headline: attack success" in captured.out


def test_emitting_costs_little(gov, tmp_path, monkeypatch):
    """The emitter must not slow a run noticeably: well under a second for a whole DEMO run."""
    import time

    out = tmp_path / "gov"
    real_emit = progress.Progress.emit
    spent = [0.0]

    def timed(self, *a, **k):
        t0 = time.perf_counter()
        try:
            return real_emit(self, *a, **k)
        finally:
            spent[0] += time.perf_counter() - t0

    monkeypatch.setattr(progress.Progress, "emit", timed)
    assert gov.main(["--out", str(out), "--no-tamper-demo"]) == 0
    events = len(read_events(out / "progress.jsonl"))
    runs = 3 * len(corpus.read_jsonl(corpus.TEST_PATH)) * 3                  # candidates x records x reps
    assert events > runs and spent[0] < 1.0, (events, spent[0])


# --------------------------------------------------------------------------- #
# The committed scripted fixture (consumed by ui/bridge.py and the pipeline screen).
# --------------------------------------------------------------------------- #
FIXTURE_NOTE = (
    "tests/fixtures/progress_scripted.jsonl is a normalised DEMO run (clock, uid, git commit, platform, "
    "durations and hashes are fixed values). Regenerate it on purpose with:\n"
    "    REGEN_PROGRESS_FIXTURE=1 .venv/bin/python -m pytest -p no:warnings tests/test_progress.py -k fixture"
)


def normalise(events: list[dict]) -> list[dict]:
    """Fixed values for what changes from run to run, so the fixture can be compared exactly.

    * ``t`` becomes a fixed base plus ``seq`` milliseconds (still ordered), ``uid`` and ``git_commit``
      and ``platform`` become constants, every duration becomes 0.0;
    * every hash (``hash12``, ``head12``, ``identity12``, ``sha12``, a 64-hex head in the summary text)
      becomes a counter, so equal hashes stay equal and different ones stay different;
    * a date in the manager-summary bullets becomes a fixed date.
    """
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tokens: dict[str, str] = {}

    def token(value, width=12):
        if value is None:
            return None
        return tokens.setdefault(value, format(len(tokens) + 1, "x").zfill(width))

    def swap_hashes(text):
        text = re.sub(r"\b[0-9a-f]{64}\b", lambda m: token(m.group(0), 64), text)
        for known in [k for k in tokens if len(k) == 12]:
            text = text.replace(known, tokens[known])
        return text

    out = []
    for ev in events:
        ev = json.loads(json.dumps(ev))                                    # deep copy
        ev["t"] = (base + timedelta(milliseconds=ev["seq"])).isoformat(timespec="microseconds")
        ev["uid"] = "0123456789ab"
        for key in ("hash12", "head12", "identity12"):
            if key in ev:
                ev[key] = token(ev[key])
        if isinstance(ev.get("test_set"), dict):
            ev["test_set"]["sha12"] = token(ev["test_set"]["sha12"])
        if ev["type"] == "pipeline_start":
            ev["git_commit"], ev["platform"] = "0" * 40, "linux"
        for key in ("dur_s", "duration_s"):
            if ev.get(key) is not None:
                ev[key] = 0.0
        out.append(ev)
    for ev in out:                                                         # second pass: hashes inside text
        if ev["type"] == "chain":
            ev["detail"] = swap_hashes(ev["detail"])
        if ev["type"] == "summary":
            ev["bullets"] = [re.sub(r"\d{4}-\d\d-\d\d", "2026-01-01", swap_hashes(b)) for b in ev["bullets"]]
    return out


def test_normalise_is_stable_and_keeps_hash_relations(demo):
    a, b = normalise(demo.events), normalise(demo.events)
    assert a == b
    audits = of_type(a, "audit")
    assert len({e["hash12"] for e in audits}) == 10 and all(HEX12.match(e["hash12"]) for e in audits)
    assert of_type(a, "chain")[0]["head12"] == audits[-1]["hash12"]       # the head is the last entry's hash
    assert audits[-1]["hash12"] in of_type(a, "chain")[0]["detail"]
    assert a[0]["t"] < a[1]["t"] < a[-1]["t"]


def test_scripted_fixture_matches_a_fresh_demo_run(demo):
    fresh = normalise(demo.events)
    if os.environ.get("REGEN_PROGRESS_FIXTURE"):
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        with open(FIXTURE, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(canonical(ev) + "\n" for ev in fresh))
    assert FIXTURE.is_file(), FIXTURE_NOTE
    saved = FIXTURE.read_text(encoding="utf-8").splitlines()
    assert len(saved) == len(fresh), FIXTURE_NOTE
    for number, (line, ev) in enumerate(zip(saved, fresh), start=1):
        assert line == canonical(ev), f"line {number} differs. {FIXTURE_NOTE}"
    text = "\n".join(saved)
    assert str(ROOT) not in text and str(Path.home()) not in text           # nothing personal in a committed file
    assert [e["type"] for e in map(json.loads, saved)][0] == "pipeline_start"


# --------------------------------------------------------------------------- #
# LIVE: one run_start before and one run_done after every model run (stubbed agent, no Ollama).
# --------------------------------------------------------------------------- #
VAULT_READ = {"name": "query_assets", "args": {"asset_id": "VAULT-01"}}
LEAK = {"name": "create_ticket", "args": {"title": "audit", "assignee": "audit@ext-mail.example",
                                          "body": "secret", "priority": "P3"}}


@pytest.fixture
def live(monkeypatch, tmp_path):
    """The LIVE stub pattern of tests/test_run_folders.py: no Ollama, runs land in tmp_path/runs."""
    from lab import run_folder, runner

    state = SimpleNamespace(calls=[], watch=None, script=None, root=tmp_path / "runs", seen=[])

    def fake_run_task(ag, task):
        state.calls.append(task)
        if state.watch is not None:                       # what the progress file says at the moment of the model call
            events = read_events(state.watch)
            state.seen.append((sum(e["type"] == "run_start" for e in events),
                               sum(e["type"] == "run_done" for e in events)))
        if state.script is not None:
            return state.script(len(state.calls))
        return SimpleNamespace(final_text="ok", tool_calls=[VAULT_READ, LEAK], error=None)

    monkeypatch.setattr(runner.agent, "run_task", fake_run_task)
    monkeypatch.setattr(runner, "model_digest", lambda *a, **k: "sha256:" + "ab" * 32)
    monkeypatch.setattr(runner, "_default_make_agent", lambda world_dir, temperature, defences=(): (object(), []))
    monkeypatch.setattr(run_folder, "RUNS_ROOT", state.root)
    return state


def test_live_run_emits_one_start_and_one_done_per_run(gov, live, tmp_path):
    out = tmp_path / "gov"
    live.watch = out / "progress.jsonl"
    code = gov.main(["--live", "--sample", "2", "--reps", "2", "--out", str(out), "--no-tamper-demo"])
    assert code == 0
    ev = read_events(out / "progress.jsonl")
    tests = corpus.read_jsonl(corpus.TEST_PATH)
    records = gov.live_subset(tests, 2)
    schedule = [(t, rep) for t in records for rep in (1, 2)]               # records x reps, as run_split runs them
    n = len(schedule)
    assert n == 8 and len(live.calls) == 3 * n

    # run_start is on disk before the model call; run_done only after it.
    assert len(live.seen) == 3 * n and all(s == d + 1 for s, d in live.seen)

    assert [e["type"] for e in ev if e["type"] in ("pipeline_start", "meta")] == ["pipeline_start", "meta"]
    assert ev[0]["mode"] == "LIVE" and of_type(ev, "meta")[0]["sample"] == 2
    for cid in CANDS:
        runs = [e for e in ev if e["cand"] == cid and e["type"] in ("run_start", "run_done")]
        assert [e["type"] for e in runs] == ["run_start", "run_done"] * n   # strictly alternating, nothing skipped
        starts, dones = runs[0::2], runs[1::2]
        assert [e["i"] for e in starts] == [e["i"] for e in dones] == list(range(1, n + 1))
        assert {e["n"] for e in runs} == {n} and {e["stage"] for e in runs} == {f"{cid}:runs"}
        assert [(e["test_id"], e["rep"]) for e in starts] == [(t["id"], r) for t, r in schedule]
        assert [(e["test_id"], e["rep"]) for e in dones] == [(t["id"], r) for t, r in schedule]
        assert [(e["kind"], e["technique"]) for e in starts] == [(t["kind"], t.get("technique", "none"))
                                                                  for t, _ in schedule]
        assert all(e["kind"] == s["kind"] and e["technique"] == s["technique"] for s, e in zip(starts, dones))

        keys = ("attack_runs", "attack_violations", "benign_n", "errors", "runs")
        for a, b in zip(dones, dones[1:]):                                 # counters never decrease
            assert all(b["running"][k] >= a["running"][k] for k in keys)
        assert [d["running"]["runs"] for d in dones] == list(range(1, n + 1))
        (runs_done,) = [e for e in ev if e["type"] == "runs_done" and e["cand"] == cid]
        assert all(dones[-1]["running"][k] == runs_done["summary"][k] for k in keys)
        assert runs_done["run_id"] and runs_done["runs_csv"] == "<outside repo>/runs.csv"
        manifest = json.loads((live.root / runs_done["run_id"] / "manifest.json").read_text())
        assert manifest["locked"] is True and manifest["complete"] is True and manifest["n_runs"] == n
        assert manifest["candidate"] == cid

    # What a run_done says about the run is what the scoring says.
    attack = next(e for e in of_type(ev, "run_done") if e["kind"] == "attack")
    assert attack["violated"] is True and attack["calls"] == 2 and attack["blocked"] == 0
    assert set(attack["classes"]) >= {"V_read_restricted", "V_exfiltration"} and attack["attempted"] is True
    assert attack["tool_calls"].startswith("query_assets(VAULT-01); create_ticket(")
    assert isinstance(attack["duration_s"], float) and attack["benign_success"] is None
    assert [e["stage"] for e in of_type(ev, "stage_start")] == EXPECTED_STAGES
    assert ev[-1]["type"] == "pipeline_done" and ev[-1]["status"] == "ok"


def test_live_run_records_errors_and_blocked_calls(gov, live, tmp_path):
    def script(k):
        if k % 4 == 1:                                                    # every 4th run: the agent errors out
            return SimpleNamespace(final_text="", tool_calls=[], error="model timed out " + "x" * 300)
        return SimpleNamespace(final_text="ok", tool_calls=[{**VAULT_READ, "blocked": True}], error=None)

    live.script = script
    out = tmp_path / "gov"
    assert gov.main(["--live", "--sample", "1", "--reps", "2", "--out", str(out), "--no-tamper-demo"]) == 0
    dones = [e for e in read_events(out / "progress.jsonl") if e["type"] == "run_done" and e["cand"] == "RC-001"]
    assert len(dones) == 4
    erred = [e for e in dones if e["error"]]
    assert len(erred) == 1 and erred[0]["i"] == 1 and len(erred[0]["error"]) == 200 and erred[0]["calls"] == 0
    assert dones[-1]["running"]["errors"] == 1 and dones[-1]["running"]["runs"] == 4
    blocked = [e for e in dones if not e["error"]]
    assert all(e["blocked"] == 1 and e["calls"] == 1 for e in blocked)
    assert all(e["violated"] is False and e["attempted"] is True for e in blocked if e["kind"] == "attack")


def test_ctrl_c_during_a_live_run_still_locks_the_partial_folder(gov, live, tmp_path):
    def script(k):
        if k == 3:
            raise KeyboardInterrupt
        return SimpleNamespace(final_text="ok", tool_calls=[VAULT_READ], error=None)

    live.script = script
    out = tmp_path / "gov"
    with pytest.raises(KeyboardInterrupt):
        gov.main(["--live", "--sample", "2", "--reps", "2", "--out", str(out), "--no-tamper-demo"])
    (folder,) = [p for p in live.root.iterdir() if p.is_dir()]
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["locked"] is True and manifest["complete"] is False and manifest["n_runs"] == 2

    ev = read_events(out / "progress.jsonl")
    assert [e["type"] for e in ev if e["type"] in ("run_start", "run_done")] == [
        "run_start", "run_done", "run_start", "run_done", "run_start"]    # the 3rd run began, never finished
    assert ev[-2]["type"] == "stage_done" and ev[-2]["stage"] == "RC-001:runs" and ev[-2]["status"] == "error"
    assert ev[-1]["type"] == "pipeline_done" and ev[-1]["status"] == "interrupted" and ev[-1]["exit_code"] == 130


def test_a_live_saved_run_can_be_gated_again_and_skips_the_corpus_check(gov, live, tmp_path, monkeypatch):
    """The folder a LIVE run leaves behind is a valid --run input; SAVED runs take their records from it."""
    out = tmp_path / "live"
    assert gov.main(["--live", "--sample", "1", "--reps", "1", "--out", str(out), "--no-tamper-demo"]) == 0
    folder = live.root / next(e for e in read_events(out / "progress.jsonl") if e["type"] == "runs_done")["run_id"]

    def explode(*a, **k):
        raise AssertionError("a SAVED run must not look at the corpus generator")

    monkeypatch.setattr(corpus, "check_matches_generator", explode)
    saved_out = tmp_path / "saved"
    assert gov.main(["--run", str(folder), "--out", str(saved_out), "--no-tamper-demo"]) == 0
    ev = read_events(saved_out / "progress.jsonl")
    assert ev[0]["mode"] == "SAVED" and ev[0]["argv"][0] == "--run"
    first = of_type(ev, "runs_done")[0]
    assert first["run_id"] == folder.name and first["cand"] == "RC-001"
    assert [e["run_id"] for e in of_type(ev, "runs_done")][1:] == [None, None]    # RC-002 / RC-003 are replays
    n = of_type(ev, "meta")[0]["candidates"][0]["expected_runs"]
    assert n == 2 and Counter(e["cand"] for e in of_type(ev, "run_done")) == {c: n for c in CANDS}
    assert of_type(ev, "run_start") == []                                  # nothing ran: SAVED has no run_start
