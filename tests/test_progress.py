"""lab/progress.py -- the progress emitter behind ``<out>/progress.jsonl``.

Unit tests for the file format and the safety rules (never raises, never deletes,
canonical JSON, one write + flush per line, repo-relative paths), then the CLI
hooks in scripts/run_governance.py (stage order, one start and one done, the
failure paths, the committed scripted fixture, the LIVE per-run events).
"""
from __future__ import annotations

import io
import json
import math
import re
from pathlib import Path

import pytest

from lab import progress

ROOT = Path(__file__).resolve().parent.parent
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
