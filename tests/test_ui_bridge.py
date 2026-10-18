"""Tests for ui/bridge.py: the bridge between progress.jsonl and the pipeline screen.

No model, no network, no Ollama. Child processes are tiny fake scripts in a
temporary repo folder. Nothing here writes to the real ui/data/ folder.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BRIDGE_PATH = REPO / "ui" / "bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("ui_bridge", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ui_bridge"] = module
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()

UID = "0123456789ab"


def line(seq: int, type_: str = "stage_start", uid: str = UID, **fields) -> str:
    """One progress line as the emitter writes it (compact JSON, no newline)."""
    base = {"v": 1, "seq": seq, "t": "2026-01-01T00:00:00.%06d+00:00" % (seq * 1000),
            "uid": uid, "type": type_, "cand": None, "stage": None}
    base.update(fields)
    return json.dumps(base, sort_keys=True, separators=(",", ":"))


def append(path: Path, data) -> None:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    with open(path, "ab") as fh:
        fh.write(raw)


# --------------------------------------------------------------------------- #
# Step 1: tailing and parsing.
# --------------------------------------------------------------------------- #
def test_tailer_reports_missing_file(tmp_path):
    tail = bridge.Tailer(tmp_path / "progress.jsonl")
    res = tail.poll()
    assert res.missing is True
    assert res.lines == []
    assert res.reset is False


def test_tailer_reads_only_complete_lines(tmp_path):
    p = tmp_path / "progress.jsonl"
    tail = bridge.Tailer(p)
    append(p, line(1) + "\n" + line(2) + "\n" + line(3)[:20])
    res = tail.poll()
    assert res.missing is False
    assert len(res.lines) == 2
    assert json.loads(res.lines[1])["seq"] == 2
    # nothing new: the fragment is kept, not emitted
    assert tail.poll().lines == []


def test_tailer_keeps_fragment_until_its_newline_arrives(tmp_path):
    p = tmp_path / "progress.jsonl"
    tail = bridge.Tailer(p)
    whole = line(7)
    append(p, whole[:15])
    assert tail.poll().lines == []
    append(p, whole[15:])
    assert tail.poll().lines == []  # still no newline
    append(p, "\n")
    res = tail.poll()
    assert [json.loads(x)["seq"] for x in res.lines] == [7]


def test_tailer_reads_from_the_last_offset_only(tmp_path):
    p = tmp_path / "progress.jsonl"
    tail = bridge.Tailer(p)
    append(p, line(1) + "\n")
    assert len(tail.poll().lines) == 1
    append(p, line(2) + "\n" + line(3) + "\n")
    res = tail.poll()
    assert [json.loads(x)["seq"] for x in res.lines] == [2, 3]
    assert tail.poll().lines == []


def test_tailer_multibyte_character_split_across_polls(tmp_path):
    p = tmp_path / "progress.jsonl"
    tail = bridge.Tailer(p)
    raw = ('{"v":1,"type":"x","text":"café"}\n').encode("utf-8")
    cut = raw.index("é".encode("utf-8")) + 1  # between the two bytes of the e-acute
    append(p, raw[:cut])
    assert tail.poll().lines == []
    append(p, raw[cut:])
    res = tail.poll()
    assert res.lines == ['{"v":1,"type":"x","text":"café"}']


def test_tailer_decodes_invalid_bytes_with_replacement(tmp_path):
    p = tmp_path / "progress.jsonl"
    tail = bridge.Tailer(p)
    append(p, b'{"v":1,"type":"x","text":"a\xffb"}\n')
    res = tail.poll()
    assert len(res.lines) == 1
    assert "�" in res.lines[0]
    event, why = bridge.parse_line(res.lines[0])
    assert why is None
    assert event["text"] == "a�b"


def test_tailer_strips_carriage_returns(tmp_path):
    p = tmp_path / "progress.jsonl"
    tail = bridge.Tailer(p)
    append(p, (line(1) + "\r\n" + line(2) + "\r\n").encode("utf-8"))
    res = tail.poll()
    assert all(not x.endswith("\r") for x in res.lines)
    assert len(res.lines) == 2


def test_tailer_resets_when_the_file_shrinks(tmp_path):
    p = tmp_path / "progress.jsonl"
    tail = bridge.Tailer(p)
    append(p, line(1) + "\n" + line(2) + "\n" + line(3) + "\n")
    assert len(tail.poll().lines) == 3
    p.write_bytes((line(1, uid="ffffffffffff") + "\n").encode("utf-8"))  # truncated and rewritten
    res = tail.poll()
    assert res.reset is True
    assert [json.loads(x)["uid"] for x in res.lines] == ["ffffffffffff"]


def test_tailer_resets_when_the_file_is_replaced_by_a_bigger_one(tmp_path):
    p = tmp_path / "progress.jsonl"
    tail = bridge.Tailer(p)
    append(p, line(1) + "\n")
    tail.poll()
    p.rename(tmp_path / "progress.20260101T000000Z.jsonl")  # what the emitter does at the next run
    new = [line(i, uid="eeeeeeeeeeee") for i in range(1, 6)]
    p.write_bytes(("\n".join(new) + "\n").encode("utf-8"))
    res = tail.poll()
    assert res.reset is True
    assert len(res.lines) == 5


def test_tailer_missing_after_present_resets_then_rereads(tmp_path):
    p = tmp_path / "progress.jsonl"
    tail = bridge.Tailer(p)
    append(p, line(1) + "\n")
    tail.poll()
    p.unlink()
    res = tail.poll()
    assert res.missing is True
    assert res.reset is True
    append(p, line(1, uid="dddddddddddd") + "\n")
    res = tail.poll()
    assert res.missing is False
    assert [json.loads(x)["uid"] for x in res.lines] == ["dddddddddddd"]


def test_tailer_drops_a_runaway_line_and_resumes(tmp_path, monkeypatch):
    assert len(line(1)) < 300
    monkeypatch.setattr(bridge, "MAX_LINE_BYTES", 300)
    p = tmp_path / "progress.jsonl"
    tail = bridge.Tailer(p)
    append(p, b"x" * 500)                      # no newline, over the cap
    res = tail.poll()
    assert res.lines == []
    assert res.oversize == 1
    append(p, b"y" * 50 + b"\n" + line(1).encode("utf-8") + b"\n")
    res = tail.poll()                          # the rest of the runaway line is discarded
    assert [json.loads(x)["seq"] for x in res.lines] == [1]
    assert res.oversize == 0


@pytest.mark.parametrize("text, ok", [
    (line(1), True),
    ("   " + line(1) + "  ", True),
    ("", None),                                 # blank: ignored, not malformed
    ("   ", None),
    ("not json", False),
    ('{"type": "x"', False),                    # cut off
    ("[1, 2, 3]", False),
    ('"text"', False),
    ("123", False),
    ('{"v":1,"seq":1}', False),                 # no type
    ('{"v":1,"type":""}', False),
    ('{"v":1,"type":7}', False),
    ('{"v":2,"type":"x"}', False),              # unknown version
    ('{"v":1,"type":"x","a":NaN}', False),
    ('{"v":1,"type":"x","a":Infinity}', False),
    ('{"type":"x"}', True),                     # v missing is accepted
])
def test_parse_line(text, ok):
    event, why = bridge.parse_line(text)
    if ok is True:
        assert isinstance(event, dict) and why is None
    elif ok is None:
        assert event is None and why is None
    else:
        assert event is None and isinstance(why, str) and why


def test_reader_skips_and_counts_malformed_lines(tmp_path):
    p = tmp_path / "progress.jsonl"
    reader = bridge.LogReader(p)
    append(p, line(1) + "\nthis is not json\n\n" + line(2) + "\n[]\n" + line(3) + "\n")
    res = reader.read()
    assert [e["seq"] for e in res.events] == [1, 2, 3]
    assert res.malformed == 2
    assert res.missing is False
    append(p, "oops\n")
    assert reader.read().malformed == 3        # the count is cumulative until a reset


def test_reader_waiting_when_file_missing(tmp_path):
    reader = bridge.LogReader(tmp_path / "nope" / "progress.jsonl")
    res = reader.read()
    assert res.missing is True and res.events == []


def test_reader_resets_on_uid_change(tmp_path):
    p = tmp_path / "progress.jsonl"
    reader = bridge.LogReader(p)
    append(p, line(1) + "\n" + line(2) + "\n")
    first = reader.read()
    assert first.reset is False and reader.uid == UID
    # the same file now carries a different run (a copy over it, or a replay)
    append(p, line(3) + "\n" + line(1, uid="bbbbbbbbbbbb") + "\n" + line(2, uid="bbbbbbbbbbbb") + "\n")
    res = reader.read()
    assert res.reset is True
    assert [e["uid"] for e in res.events] == ["bbbbbbbbbbbb", "bbbbbbbbbbbb"]
    assert reader.uid == "bbbbbbbbbbbb"
    assert res.malformed == 0


def test_reader_reset_clears_the_malformed_count(tmp_path):
    p = tmp_path / "progress.jsonl"
    reader = bridge.LogReader(p)
    append(p, "junk\n" + line(1) + "\n")
    assert reader.read().malformed == 1
    p.write_bytes((line(1, uid="cccccccccccc") + "\n").encode("utf-8"))   # shrink: new run
    res = reader.read()
    assert res.reset is True and res.malformed == 0


def test_reader_treats_events_without_uid_as_part_of_the_current_run(tmp_path):
    p = tmp_path / "progress.jsonl"
    reader = bridge.LogReader(p)
    append(p, line(1) + "\n" + '{"v":1,"type":"artifact","seq":2}\n')
    res = reader.read()
    assert res.reset is False and len(res.events) == 2


# --------------------------------------------------------------------------- #
# Step 2: state builder (event -> node mapping) and the live.js writer.
# --------------------------------------------------------------------------- #
import copy  # noqa: E402
import getpass  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402

SPEC_NODES = [
    "rt.attack", "rt.record", "rt.check", "rt.rate", "rt.fixes",
    "sl.fingerprint", "sl.gate", "sl.review", "sl.log", "fix.ticket",
    "ll.risks", "ll.controls", "ll.evidence", "ll.trace", "ll.summary",
]
DOT = "\u00b7"
NULLISH = re.compile(r"\b(None|nan|NaN|null)\b")


def lane_a_fixture_lines():
    """Lane A's scripted log: the working-tree file once merged, else `git show lane/A:...`; else skip."""
    path = REPO / "tests" / "fixtures" / "progress_scripted.jsonl"
    if path.is_file():
        return path.read_text(encoding="utf-8").splitlines()
    try:
        done = subprocess.run(["git", "show", "lane/A:tests/fixtures/progress_scripted.jsonl"],
                              cwd=str(REPO), capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is not available, so lane A's fixture cannot be read")
    if done.returncode != 0:
        pytest.skip("lane A's tests/fixtures/progress_scripted.jsonl is not available")
    return done.stdout.decode("utf-8").splitlines()


def lane_a_events():
    return [json.loads(x) for x in lane_a_fixture_lines() if x.strip()]


class Run:
    """A small pipeline run with the event shapes lane A's emitter writes."""

    def __init__(self, mode="SCRIPTED", cands=(("RC-001", []), ("RC-002", ["D2"])), n_runs=4, uid=UID):
        self.mode, self.cands, self.n_runs, self.uid = mode, list(cands), n_runs, uid
        self.events = []
        self.seq = 0

    def ev(self, type_, cand=None, stage=None, **fields):
        self.seq += 1
        e = {"v": 1, "seq": self.seq, "t": "2026-01-01T00:00:00.%06d+00:00" % (self.seq * 1000),
             "uid": self.uid, "type": type_, "cand": cand, "stage": stage}
        e.update(fields)
        self.events.append(e)
        return e

    def head(self):
        self.ev("pipeline_start", mode=self.mode, argv=["--out", "<outside repo>/demo"],
                out="<outside repo>/demo", platform="linux", git_commit="0" * 40)
        self.ev("stage_start", stage="setup", title="Setup")
        self.ev("meta", stage="setup", model="qwen2.5:7b-instruct", digest12="placeholder (DEMO)",
                policy_version="1.0", reps=1, sample=None,
                test_set={"records": 4, "attacks": 2, "benign": 2, "sha12": "000000000001"},
                candidates=[{"id": c, "defences": d, "expected_runs": self.n_runs} for c, d in self.cands])
        self.ev("stage_done", stage="setup", title="Setup", status="ok", dur_s=0.0)

    def run_events(self, cand, violated_pattern=(True, False), with_start=False):
        """n_runs run_done events: attacks first (cycling the pattern), then two benign runs."""
        ar = av = bn = bok = 0
        running = None
        for i in range(1, self.n_runs + 1):
            benign = i > self.n_runs - 2
            kind = "benign" if benign else "attack"
            viol = False if benign else violated_pattern[(i - 1) % len(violated_pattern)]
            if with_start:
                self.ev("run_start", cand, f"{cand}:runs", i=i, n=self.n_runs, test_id=f"T-{i:04d}", rep=1,
                        kind=kind, technique="T1_plain")
            if benign:
                bn += 1
                bok += 1
            else:
                ar += 1
                av += 1 if viol else 0
            asr = None if ar == 0 else av / ar
            running = {"attack_runs": ar, "attack_violations": av, "asr": asr,
                       "asr_lo": None if asr is None else asr / 2,
                       "asr_hi": None if asr is None else min(1.0, asr + 0.2),
                       "benign_n": bn, "benign_rate": None if bn == 0 else bok / bn, "errors": 0, "runs": i}
            self.ev("run_done", cand, f"{cand}:runs", i=i, n=self.n_runs, test_id=f"T-{i:04d}", rep=1, kind=kind,
                    technique="T1_plain", goal=None if benign else "G1", violated=viol,
                    classes=["V_read_restricted"] if viol else [], attempted=not benign, calls=2, blocked=0,
                    tool_calls="read_doc(a.md)", error=None, benign_success=True if benign else None,
                    duration_s=None, running=running)
        return running

    def candidate(self, cand, verdict, decision):
        stage = f"{cand}:runs"
        self.ev("stage_start", cand, stage, title=f"{cand} runs")
        final = self.run_events(cand)
        summary = dict(final, attempted_count=1, attempted_rate=0.5, attempted_runs=final["attack_runs"],
                       errors_with_violation=0)
        self.ev("runs_done", cand, stage, run_id=None, runs_csv="<outside repo>/runs.csv", summary=summary)
        self.ev("artifact", cand, stage, kind="fingerprint", name=f"{cand}.json", path=f"<outside repo>/{cand}.json")
        self.ev("fingerprint", cand, stage, identity12=("%012d" % self.seq),
                changed_vs_prev=None if cand == "RC-001" else True)
        self.ev("stage_done", cand, stage, title=f"{cand} runs", status="ok", dur_s=0.0)
        gstage = f"{cand}:gate"
        self.ev("stage_start", cand, gstage, title="Gate")
        bad = verdict == "FAIL"
        checks = [{"name": "attack_success_rate", "ok": not bad, "value": 0.4230769230769231 if bad else 0.0,
                   "limit": 0.05, "note": ""},
                  {"name": "min_attack_runs", "ok": True, "value": 78, "limit": 50, "note": ""}]
        self.ev("gate", cand, gstage, verdict=verdict, reasons=["attack_success_rate"] if bad else [],
                checks=checks, lines=["x"], file="<outside repo>/gate.txt")
        self.ev("stage_done", cand, gstage, title="Gate", status="ok", dur_s=0.0)
        dstage = f"{cand}:decide"
        self.ev("stage_start", cand, dstage, title="Decide")
        self.ev("audit", cand, dstage, entry=1, event="submit", by="tom", reviewer=None, verdict=None,
                decision=None, reason=None, scripted=True, hash12="00000000000a")
        self.ev("audit", cand, dstage, entry=2, event="verdict", by=None, reviewer=None, verdict=verdict,
                decision=None, reason=None, scripted=True, hash12="00000000000b")
        self.ev("audit", cand, dstage, entry=3, event="decision", by=None, reviewer="sarah", verdict=verdict,
                decision=decision, reason="Needs more defences." if decision == "BLOCK" else None,
                scripted=True, hash12="00000000000c")
        self.ev("stage_done", cand, dstage, title="Decide", status="ok", dur_s=0.0)
        astage = f"{cand}:assure"
        self.ev("stage_start", cand, astage, title="Assure")
        rows = [{"id": "R-01", "status": "Open"}, {"id": "R-02", "status": "Open"},
                {"id": "R-03", "status": "Mitigated"}]
        self.ev("risks", cand, astage, rows=rows, controls_used=["D2", "D3"])
        if cand == "RC-001":
            self.ev("tickets", cand, astage, rows=[{"id": "FIX-01", "status": "Open"},
                                                   {"id": "FIX-02", "status": "Open"}],
                    controls_used=["D2", "D3"])
        else:
            self.ev("evidence", cand, astage, rows=[{"control": "D2"}], controls_used=["D2", "D3"])
        self.ev("stage_done", cand, astage, title="Assure", status="ok", dur_s=0.0)

    def tail(self, finding="ATK-0021", tamper=True):
        self.ev("stage_start", stage="trace", title="Trace")
        self.ev("trace", stage="trace", finding=finding, steps_n=12 if finding else None)
        self.ev("stage_done", stage="trace", title="Trace", status="ok", dur_s=0.0)
        self.ev("stage_start", stage="summary", title="Summary")
        self.ev("summary", stage="summary", bullets=["one", "two", "three"])
        self.ev("stage_done", stage="summary", title="Summary", status="ok", dur_s=0.0)
        self.ev("stage_start", stage="chain", title="Chain")
        self.ev("chain", stage="chain", ok=True, entries=6, head12="0000000000ff",
                detail="6 entries, chain intact (head 0000000000ff)", tamper=None)
        if tamper:
            self.ev("chain", stage="chain", ok=True, entries=6, head12="0000000000ff",
                    detail="6 entries, chain intact (head 0000000000ff)",
                    tamper={"ok": False, "bad_seq": 4, "kind": "edited"})
        self.ev("stage_done", stage="chain", title="Chain", status="ok", dur_s=0.0)
        self.ev("stage_start", stage="done", title="Done")
        self.ev("stage_done", stage="done", title="Done", status="ok", dur_s=0.0)

    def finish(self, status="ok", exit_code=0, dur_s=2.5, error=None, verdicts=("FAIL", "PASS")):
        cands = [{"id": c, "verdict": v, "asr": 0.5, "asr_lo": 0.1, "asr_hi": 0.9, "attack_runs": 2,
                  "attack_violations": 1, "benign_n": 2, "benign_rate": 1.0}
                 for (c, _), v in zip(self.cands, verdicts)]
        self.ev("pipeline_done", status=status, exit_code=exit_code, dur_s=dur_s,
                headline={"cands": cands}, error=error)

    def full(self):
        self.head()
        self.candidate("RC-001", "FAIL", "BLOCK")
        self.candidate("RC-002", "PASS", "APPROVE")
        self.tail()
        self.finish()
        return self.events


def build(events, view=None):
    """Apply events to a fresh model and return (model, ui-live snapshot)."""
    model = bridge.LiveModel()
    for e in events:
        model.apply(e)
    return model, bridge.build_live(model, view or bridge.View())


def states(snap):
    return {k: v["state"] for k, v in snap["nodes"].items()}


def attack_row(cand, i, n, test_id, rep, kind, violated, classes, error, benign_success, running):
    """One run_done event written by hand (for the tile tests)."""
    return dict(cand=cand, stage=f"{cand}:runs", i=i, n=n, test_id=test_id, rep=rep, kind=kind, technique="T",
                goal="G", violated=violated, classes=classes, attempted=True, calls=1, blocked=0, tool_calls="",
                error=error, benign_success=benign_success, duration_s=None, running=running)


def stats(ar, av, asr, lo, hi, bn, br, errors, runs):
    return {"attack_runs": ar, "attack_violations": av, "asr": asr, "asr_lo": lo, "asr_hi": hi,
            "benign_n": bn, "benign_rate": br, "errors": errors, "runs": runs}


# ---- shape -------------------------------------------------------------- #
def test_node_ids_are_the_fifteen_from_the_spec():
    assert sorted(bridge.NODE_IDS) == sorted(SPEC_NODES)
    assert len(set(bridge.NODE_IDS)) == 15


def test_edges_only_join_known_nodes():
    assert bridge.EDGES
    for key in bridge.EDGES:
        a, b = key.split(">")
        assert a in SPEC_NODES and b in SPEC_NODES


def test_waiting_snapshot_has_the_ui_live_1_shape():
    snap = bridge.build_live(bridge.LiveModel(), bridge.View())
    assert list(snap) == ["schema", "rev", "written_at", "source", "label", "mode", "model", "digest", "state",
                          "active", "prev_active", "nodes", "edges", "rail", "tiles", "events_tail", "warnings",
                          "done", "batch"]
    assert snap["schema"] == "ui-live/1"
    assert snap["state"] == "waiting"
    assert set(snap["nodes"]) == set(SPEC_NODES)
    assert all(set(n) == {"state", "fact", "fact2", "status", "tag"} for n in snap["nodes"].values())
    assert set(states(snap).values()) == {"idle"}
    assert set(snap["edges"]) == set(bridge.EDGES) and set(snap["edges"].values()) == {"idle"}
    assert set(snap["source"]) == {"kind", "log", "uid", "speed", "paced"}
    assert set(snap["label"]) == {"word", "text", "status"}
    assert snap["rail"] == [] and snap["events_tail"] == [] and snap["warnings"] == []
    assert snap["tiles"] == {"cand": None, "n": 0, "list": []}
    assert snap["done"] is None and snap["batch"] is None
    assert snap["active"] is None and snap["prev_active"] is None
    json.dumps(snap, allow_nan=False)


# ---- header, rail, label ------------------------------------------------ #
def test_start_and_meta_fill_the_header_and_the_rail():
    run = Run()
    run.head()
    model, snap = build(run.events)
    assert snap["mode"] == "SCRIPTED" and snap["model"] == "qwen2.5:7b-instruct"
    assert snap["digest"] == "placeholder (DEMO)"
    assert snap["state"] == "running"
    assert [r["id"] for r in snap["rail"]] == ["RC-001", "RC-002"]
    assert snap["rail"][0]["text"] == "RC-001 " + DOT + " no defences"
    assert snap["rail"][1]["text"] == "RC-002 " + DOT + " defences D2"
    assert all(r["state"] == "idle" and r["verdict"] == "" and r["status"] == "SAMPLE" for r in snap["rail"])
    assert snap["tiles"]["cand"] == "RC-001" and snap["tiles"]["n"] == 4
    assert snap["tiles"]["list"] == [["#1", "idle"], ["#2", "idle"], ["#3", "idle"], ["#4", "idle"]]
    assert set(states(snap).values()) == {"idle"}
    assert snap["label"] == {"word": "SCRIPTED", "text": "SCRIPTED " + DOT + " paced for display", "status": "SAMPLE"}
    assert snap["source"]["uid"] == UID


@pytest.mark.parametrize("mode, expect_status", [("SCRIPTED", "SAMPLE"), ("LIVE", "REAL"), ("SAVED", "REAL")])
def test_status_follows_the_mode(mode, expect_status):
    run = Run(mode=mode)
    run.head()
    _, snap = build(run.events)
    assert snap["rail"][0]["status"] == expect_status
    assert snap["nodes"]["rt.attack"]["status"] == expect_status


def test_label_rules():
    live = Run(mode="LIVE")
    live.head()
    model, _ = build(live.events)

    def label(**kw):
        return bridge.build_live(model, bridge.View(**kw))["label"]

    growing = label(kind="follow", child_alive=True, last_event_age=500.0)
    assert growing["word"] == "LIVE" and growing["status"] == "REAL"
    assert "qwen2.5:7b-instruct" in growing["text"]
    assert label(kind="follow", child_alive=None, last_event_age=59.0)["word"] == "LIVE"
    assert label(kind="follow", child_alive=False, last_event_age=61.0)["word"] == "REPLAY"
    assert label(kind="follow", child_alive=None, last_event_age=61.0)["word"] == "REPLAY"
    old = label(kind="follow", child_alive=False, last_event_age=300.0)
    assert old["text"].startswith("REPLAY " + DOT + " real-model run of 2026-01-01 00:00 UTC")
    rep = label(kind="replay", speed=4.0, child_alive=None, last_event_age=0.0)
    assert rep["word"] == "REPLAY" and rep["status"] == "REAL"
    assert rep["text"] == ("REPLAY " + DOT + " real-model run of 2026-01-01 00:00 UTC " + DOT + " 4\u00d7 speed "
                           + DOT + " long gaps shortened")
    saved = Run(mode="SAVED")
    saved.head()
    m2, _ = build(saved.events)
    lab = bridge.build_live(m2, bridge.View())["label"]
    assert lab["word"] == "SAVED" and "paced for display" in lab["text"] and lab["status"] == "REAL"
    scripted = Run()
    scripted.head()
    m3, _ = build(scripted.events)
    assert bridge.build_live(m3, bridge.View(kind="replay", speed=2.0))["label"]["word"] == "SCRIPTED"


def test_source_block_says_what_is_being_shown():
    run = Run(mode="LIVE")
    run.head()
    model, _ = build(run.events)
    src = bridge.build_live(model, bridge.View(kind="replay", log="results/x/progress.jsonl", speed=4.0))["source"]
    assert src == {"kind": "replay", "log": "results/x/progress.jsonl", "uid": UID, "speed": 4.0, "paced": True}
    src = bridge.build_live(model, bridge.View(kind="follow", log="a/progress.jsonl", last_event_age=1.0))["source"]
    assert src["kind"] == "follow" and src["speed"] is None and src["paced"] is False
    scripted = Run()
    scripted.head()
    m2, _ = build(scripted.events)
    assert bridge.build_live(m2, bridge.View(kind="follow"))["source"]["paced"] is True


# ---- the red-team row --------------------------------------------------- #
def test_runs_stage_starts_the_attack_and_runs_wave_across_the_row():
    run = Run(n_runs=6)
    run.head()
    run.ev("stage_start", "RC-001", "RC-001:runs", title="runs")
    model = bridge.LiveModel()
    for e in run.events:
        model.apply(e)
    snap = bridge.build_live(model, bridge.View())
    assert snap["nodes"]["rt.attack"]["state"] == "running"
    assert snap["nodes"]["rt.attack"]["fact"] == "6 runs planned"
    assert snap["active"] == "rt.attack" and snap["prev_active"] is None
    assert snap["rail"][0]["state"] == "running"
    seen = []
    run.events.clear()
    run.run_events("RC-001")
    for e in run.events:
        beat = model.apply(e)
        s = bridge.build_live(model, bridge.View())
        seen.append((beat, s["active"], s["prev_active"], states(s)))
    # run 1 starts the record node, run 2 the check node, run 3 the rate node; later runs start nothing
    assert [b for b, *_ in seen] == [True, True, True, False, False, False]
    assert seen[0][1:3] == ("rt.record", "rt.attack") and seen[0][3]["rt.record"] == "running"
    assert seen[1][1:3] == ("rt.check", "rt.record") and seen[1][3]["rt.check"] == "running"
    assert seen[2][1:3] == ("rt.rate", "rt.check") and seen[2][3]["rt.rate"] == "running"
    final = bridge.build_live(model, bridge.View())
    assert final["edges"]["rt.attack>rt.record"] == "done"
    assert final["edges"]["rt.record>rt.check"] == "done"
    assert final["edges"]["rt.check>rt.rate"] == "done"
    assert final["edges"]["rt.rate>sl.fingerprint"] == "idle"
    assert final["nodes"]["rt.attack"]["fact"] == f"run 6 of 6 {DOT} T-0006 rep 1"
    assert final["nodes"]["rt.record"]["fact"] == f"12 tool calls {DOT} 0 blocked"


def test_run_facts_come_from_backend_values():
    run = Run(n_runs=4)
    run.head()
    run.ev("stage_start", "RC-001", "RC-001:runs", title="runs")
    run.run_events("RC-001", violated_pattern=(False,))        # no attack ever lands
    _, snap = build(run.events)
    rate = snap["nodes"]["rt.rate"]
    assert rate["fact"] == f"0 of 2 {DOT} 0.0% (0.0% to 20.0%)"
    assert rate["fact2"] == "normal requests handled 100.0% of 2"
    assert snap["nodes"]["rt.check"]["fact"] == "no rule broken yet"


def test_a_rate_with_nothing_behind_it_is_not_shown_as_zero():
    run = Run(n_runs=4)
    run.head()
    run.ev("stage_start", "RC-001", "RC-001:runs", title="runs")
    for i in range(1, 4):
        run.ev("run_done", **attack_row("RC-001", i, 4, f"B-{i}", 1, "benign", False, [], None, True,
                                        stats(0, 0, None, None, None, i, 1.0, 0, i)))
    _, snap = build(run.events)
    assert snap["nodes"]["rt.rate"]["fact"] == "no attack runs yet"
    assert "0.0%" not in snap["nodes"]["rt.rate"]["fact"]
    run3 = Run(n_runs=4)
    run3.head()
    run3.ev("stage_start", "RC-001", "RC-001:runs", title="runs")
    for i in range(1, 4):
        run3.ev("run_done", **attack_row("RC-001", i, 4, f"A-{i}", 1, "attack", False, [], None, None,
                                         stats(i, 0, 0.0, 0.0, 0.5, 0, None, 0, i)))
    _, snap3 = build(run3.events)
    assert snap3["nodes"]["rt.rate"]["fact"] == f"0 of 3 {DOT} 0.0% (0.0% to 50.0%)"
    assert snap3["nodes"]["rt.rate"]["fact2"] == "no normal requests run yet"


def test_class_counts_show_the_top_two():
    run = Run(n_runs=6)
    run.head()
    run.ev("stage_start", "RC-001", "RC-001:runs", title="runs")
    for i, classes in enumerate([["V_a"], ["V_a"], ["V_b"], ["V_a", "V_c"], ["V_c"]], start=1):
        row = attack_row("RC-001", i, 6, f"A-{i}", 1, "attack", True, classes, None, None,
                         stats(i, i, 1.0, 0.5, 1.0, 0, None, 0, i))
        row["blocked"] = 1
        run.ev("run_done", **row)
    _, snap = build(run.events)
    check = snap["nodes"]["rt.check"]
    assert check["fact"] == f"V_a 3 {DOT} V_c 2 {DOT} +1 more"
    assert check["fact2"] == "V_a 3, V_c 2, V_b 1"
    assert snap["nodes"]["rt.record"]["fact"] == f"5 tool calls {DOT} 5 blocked"


def test_live_run_start_marks_the_tile_running_and_names_the_test():
    run = Run(n_runs=6, mode="LIVE")
    run.head()
    run.ev("stage_start", "RC-001", "RC-001:runs", title="runs")
    run.ev("run_start", "RC-001", "RC-001:runs", i=1, n=6, test_id="ATK-0002", rep=1, kind="attack", technique="T")
    _, snap = build(run.events)
    assert snap["tiles"]["list"][0] == ["ATK-0002:1", "running"]
    assert snap["nodes"]["rt.attack"]["fact"] == f"run 1 of 6 {DOT} ATK-0002 rep 1"


def test_tiles_show_each_run_with_its_key_and_state():
    run = Run(n_runs=6)
    run.head()
    run.ev("stage_start", "RC-001", "RC-001:runs", title="runs")
    run.ev("run_done", **attack_row("RC-001", 1, 6, "A-1", 1, "attack", True, ["V_x"], None, None,
                                    stats(1, 1, 1.0, 0.2, 1.0, 0, None, 0, 1)))
    run.ev("run_done", **attack_row("RC-001", 2, 6, "A-2", 2, "attack", False, [], None, None,
                                    stats(2, 1, 0.5, 0.1, 0.9, 0, None, 0, 2)))
    run.ev("run_done", **attack_row("RC-001", 3, 6, "B-1", 1, "benign", False, [], None, True,
                                    stats(2, 1, 0.5, 0.1, 0.9, 1, 1.0, 0, 3)))
    run.ev("run_done", **attack_row("RC-001", 4, 6, "B-2", 1, "benign", False, [], None, False,
                                    stats(2, 1, 0.5, 0.1, 0.9, 2, 0.5, 0, 4)))
    run.ev("run_done", **attack_row("RC-001", 5, 6, "A-3", 1, "attack", False, [], "model error: timeout", None,
                                    stats(2, 1, 0.5, 0.1, 0.9, 2, 0.5, 1, 5)))
    _, snap = build(run.events)
    assert snap["tiles"]["cand"] == "RC-001" and snap["tiles"]["n"] == 6
    assert snap["tiles"]["list"] == [["A-1:1", "violated"], ["A-2:2", "held"], ["B-1:1", "ok"],
                                      ["B-2:1", "missed"], ["A-3:1", "error"], ["#6", "idle"]]


def test_runs_done_finishes_the_row_with_the_final_rate():
    run = Run()
    run.head()
    run.ev("stage_start", "RC-001", "RC-001:runs", title="runs")
    run.run_events("RC-001")
    summary = {"attack_runs": 78, "attack_violations": 33, "asr": 0.4230769230769231, "asr_lo": 0.31959060348175483,
               "asr_hi": 0.533784444166275, "benign_n": 60, "benign_rate": 1.0, "errors": 0, "runs": 138,
               "attempted_count": 33, "attempted_rate": 0.4230769230769231, "attempted_runs": 78,
               "errors_with_violation": 0}
    run.ev("runs_done", "RC-001", "RC-001:runs", run_id=None, runs_csv="<outside repo>/runs.csv", summary=summary)
    _, snap = build(run.events)
    for nid in ("rt.attack", "rt.record", "rt.check", "rt.rate"):
        assert snap["nodes"][nid]["state"] == "done", nid
    assert snap["nodes"]["rt.rate"]["fact"] == f"33 of 78 {DOT} 42.3% (32.0% to 53.4%)"
    assert snap["nodes"]["rt.rate"]["fact2"] == "normal requests handled 100.0% of 60"
    assert snap["nodes"]["rt.attack"]["fact"] == "138 runs"


# ---- the short loop ----------------------------------------------------- #
def test_fingerprint_runs_until_the_next_event_then_is_done():
    run = Run()
    run.head()
    run.ev("stage_start", "RC-001", "RC-001:runs", title="runs")
    final = run.run_events("RC-001")
    run.ev("runs_done", "RC-001", "RC-001:runs", run_id=None, runs_csv="x", summary=final)
    model = bridge.LiveModel()
    for e in run.events:
        model.apply(e)
    beat = model.apply(run.ev("fingerprint", "RC-001", "RC-001:runs", identity12="000000000002",
                              changed_vs_prev=None))
    snap = bridge.build_live(model, bridge.View())
    assert beat is True
    assert snap["nodes"]["sl.fingerprint"]["state"] == "running"
    assert snap["nodes"]["sl.fingerprint"]["fact"] == f"identity 000000000002 {DOT} first version"
    assert snap["edges"]["rt.rate>sl.fingerprint"] == "done"
    assert snap["active"] == "sl.fingerprint" and snap["prev_active"] == "rt.rate"
    model.apply(run.ev("stage_done", "RC-001", "RC-001:runs", title="runs", status="ok", dur_s=0.0))
    assert bridge.build_live(model, bridge.View())["nodes"]["sl.fingerprint"]["state"] == "done"


def test_changed_and_unchanged_fingerprints():
    for flag, word in ((True, "changed"), (False, "unchanged")):
        run = Run()
        run.head()
        run.ev("fingerprint", "RC-001", "RC-001:runs", identity12="0000000000aa", changed_vs_prev=flag)
        _, snap = build(run.events)
        assert snap["nodes"]["sl.fingerprint"]["fact"] == f"identity 0000000000aa {DOT} {word}"


def test_gate_sets_fact_tag_and_the_rail_verdict():
    run = Run()
    run.head()
    run.candidate("RC-001", "FAIL", "BLOCK")
    _, snap = build(run.events)
    gate = snap["nodes"]["sl.gate"]
    assert gate["state"] == "done" and gate["tag"] == "FAIL"
    assert gate["fact"] == f"FAIL {DOT} 1 of 2 checks failed"
    assert gate["fact2"] == "attack_success_rate 42.3% (limit 5.0%)"
    assert snap["rail"][0]["verdict"] == "FAIL"
    run = Run()
    run.head()
    run.candidate("RC-001", "PASS", "APPROVE")
    _, snap = build(run.events)
    assert snap["nodes"]["sl.gate"]["fact"] == f"PASS {DOT} all 2 checks passed"
    assert snap["nodes"]["sl.gate"]["tag"] == "PASS" and snap["nodes"]["sl.gate"]["fact2"] == ""
    assert snap["rail"][0]["verdict"] == "PASS"


def test_gate_value_formats_follow_the_backend_rule():
    assert bridge.fmt_check_value(0.4230769230769231) == "42.3%"
    assert bridge.fmt_check_value(0.0) == "0.0%"
    assert bridge.fmt_check_value(78) == "78"
    assert bridge.fmt_check_value(50) == "50"
    assert bridge.fmt_check_value(None) == "n/a"


def test_decision_marks_the_reviewer_as_scripted_and_logs_the_entry():
    run = Run()
    run.head()
    run.candidate("RC-001", "FAIL", "BLOCK")
    _, snap = build(run.events)
    review = snap["nodes"]["sl.review"]
    assert review["state"] == "done"
    assert review["fact"] == "BLOCK by sarah" and review["tag"] == "scripted by the CLI"
    assert review["fact2"] == "Needs more defences."
    log = snap["nodes"]["sl.log"]
    assert log["state"] == "done" and log["fact"] == f"entry 3 {DOT} decision {DOT} 00000000000c"
    assert snap["edges"]["sl.gate>sl.review"] == "done" and snap["edges"]["sl.review>sl.log"] == "done"


def test_a_live_decision_is_marked_scripted_when_the_event_says_so():
    run = Run(mode="LIVE")
    run.head()
    run.candidate("RC-001", "FAIL", "BLOCK")
    _, snap = build(run.events)
    assert snap["nodes"]["sl.review"]["tag"] == "scripted by the CLI"
    for e in run.events:
        if e["type"] == "audit":
            e["scripted"] = False
    _, snap2 = build(run.events)
    assert snap2["nodes"]["sl.review"]["tag"] == ""


def test_refused_review_is_shown_and_holds_the_schedule():
    run = Run()
    run.head()
    run.ev("stage_start", "RC-001", "RC-001:decide", title="Decide")
    model = bridge.LiveModel()
    for e in run.events:
        model.apply(e)
    beat = model.apply(run.ev("audit", "RC-001", "RC-001:decide", entry=3, event="decision_refused", by=None,
                              reviewer="tom", verdict=None, decision=None, scripted=True, hash12="00000000000c",
                              reason="REFUSED: tom submitted this candidate and cannot review it"))
    snap = bridge.build_live(model, bridge.View())
    assert beat is True
    assert snap["nodes"]["sl.review"]["fact"] == "refused: tom"
    assert snap["nodes"]["sl.review"]["fact2"] == "REFUSED: tom submitted this candidate and cannot review it"


# ---- assurance and the long loop ---------------------------------------- #
def test_assure_events_fill_the_long_loop_and_the_fixes_node():
    run = Run()
    run.head()
    run.candidate("RC-001", "FAIL", "BLOCK")
    _, snap = build(run.events)
    assert snap["nodes"]["ll.risks"]["state"] == "done"
    assert snap["nodes"]["ll.risks"]["fact"] == f"3 risks {DOT} 2 open"
    assert snap["nodes"]["ll.controls"]["state"] == "done"
    assert snap["nodes"]["ll.controls"]["fact"] == "controls used: D2, D3"
    assert snap["nodes"]["rt.fixes"]["state"] == "done"
    assert snap["nodes"]["rt.fixes"]["fact"] == f"2 fix tickets {DOT} 2 open risks"
    assert snap["nodes"]["ll.evidence"]["state"] == "idle"
    assert snap["edges"]["rt.attack>ll.risks"] == "done"
    assert snap["edges"]["ll.risks>ll.controls"] == "done"
    assert snap["edges"]["rt.rate>rt.fixes"] == "done"


def test_evidence_event_fills_the_evidence_node():
    run = Run()
    run.head()
    run.candidate("RC-001", "FAIL", "BLOCK")
    run.candidate("RC-002", "PASS", "APPROVE")
    _, snap = build(run.events)
    assert snap["nodes"]["ll.evidence"]["state"] == "done"
    assert snap["nodes"]["ll.evidence"]["fact"] == "1 evidence row"


def test_a_second_candidate_starts_a_new_pass_with_a_fix_ticket_first():
    run = Run()
    run.head()
    run.candidate("RC-001", "FAIL", "BLOCK")
    model = bridge.LiveModel()
    for e in run.events:
        model.apply(e)
    before = bridge.build_live(model, bridge.View())
    assert before["nodes"]["rt.fixes"]["state"] == "done" and before["nodes"]["ll.risks"]["state"] == "done"
    beat = model.apply(run.ev("stage_start", "RC-002", "RC-002:runs", title="RC-002 runs"))
    snap = bridge.build_live(model, bridge.View())
    assert beat is True
    for nid in ("rt.attack", "rt.record", "rt.check", "rt.rate", "sl.fingerprint", "sl.gate", "sl.review", "sl.log"):
        assert snap["nodes"][nid]["state"] == "idle" and snap["nodes"][nid]["fact"] == "", nid
    assert snap["nodes"]["fix.ticket"]["state"] == "running"
    assert snap["nodes"]["fix.ticket"]["fact"] == "adds D2"
    assert snap["nodes"]["fix.ticket"]["fact2"] == "RC-002 enforces D2"
    assert snap["active"] == "fix.ticket"
    # the persistent nodes keep what they had
    assert snap["nodes"]["rt.fixes"]["state"] == "done" and snap["nodes"]["ll.risks"]["state"] == "done"
    assert snap["rail"][0]["state"] == "done" and snap["rail"][1]["state"] == "running"
    assert snap["tiles"]["cand"] == "RC-002"
    assert all(v == "idle" for k, v in snap["edges"].items()
               if k.startswith(("rt.attack>rt.", "sl.")) and k != "sl.log>fix.ticket")
    assert snap["edges"]["sl.log>fix.ticket"] == "done"        # the last pass's log leads to the fix
    # the next event hands over: fix ticket done, the attack starts, the "test again" edge lights
    model.apply(run.ev("artifact", "RC-002", "RC-002:runs", kind="fingerprint", name="RC-002.json", path="x"))
    snap = bridge.build_live(model, bridge.View())
    assert snap["nodes"]["fix.ticket"]["state"] == "done"
    assert snap["nodes"]["rt.attack"]["state"] == "running"
    assert snap["active"] == "rt.attack" and snap["prev_active"] == "fix.ticket"
    assert snap["edges"]["fix.ticket>rt.attack"] == "done"


def test_third_candidate_names_only_the_new_defence():
    run = Run(cands=(("RC-001", []), ("RC-002", ["D2"]), ("RC-003", ["D2", "D3"])))
    run.head()
    run.candidate("RC-001", "FAIL", "BLOCK")
    run.candidate("RC-002", "FAIL", "BLOCK")
    run.ev("stage_start", "RC-003", "RC-003:runs", title="runs")
    _, snap = build(run.events)
    assert snap["nodes"]["fix.ticket"]["fact"] == "adds D3"
    assert snap["nodes"]["fix.ticket"]["fact2"] == "RC-003 enforces D2, D3"


def test_trace_summary_and_chain_facts():
    run = Run()
    run.head()
    run.tail()
    _, snap = build(run.events)
    assert snap["nodes"]["ll.trace"]["state"] == "done"
    assert snap["nodes"]["ll.trace"]["fact"] == f"ATK-0021 {DOT} 12 steps"
    assert snap["nodes"]["ll.summary"]["fact"] == "3 summary lines" and snap["nodes"]["ll.summary"]["fact2"] == "one"
    log = snap["nodes"]["sl.log"]
    assert log["state"] == "done" and log["tag"] == "intact"
    assert log["fact"] == f"6 entries {DOT} chain intact {DOT} head 0000000000ff"
    assert log["fact2"] == "tamper check on a copy: caught (edited at entry 4)"
    run = Run()
    run.head()
    run.tail(finding=None, tamper=False)
    _, snap = build(run.events)
    assert snap["nodes"]["ll.trace"]["fact"] == "nothing to trace"
    assert snap["nodes"]["sl.log"]["fact2"] == ""


def test_the_events_tail_tells_the_chain_check_from_the_tamper_check():
    run = Run()
    run.head()
    run.tail()
    _, snap = build(run.events)
    lines = [x for x in snap["events_tail"] if "chain" in x.lower() or "tamper" in x.lower()]
    assert "Audit chain checked: intact" in lines
    assert "Tamper check on a copy: caught" in lines


def test_a_tamper_check_that_is_not_caught_is_said_plainly():
    run = Run()
    run.head()
    run.tail()
    for e in run.events:
        if e["type"] == "chain" and e["tamper"]:
            e["tamper"] = {"ok": True, "bad_seq": None, "kind": None}
    _, snap = build(run.events)
    assert snap["nodes"]["sl.log"]["fact2"] == "tamper check on a copy: NOT caught"


def test_a_broken_chain_is_marked_broken():
    run = Run()
    run.head()
    run.tail(tamper=False)
    for e in run.events:
        if e["type"] == "chain":
            e["ok"] = False
            e["detail"] = "entry 4 does not match its hash"
    _, snap = build(run.events)
    log = snap["nodes"]["sl.log"]
    assert log["tag"] == "broken" and "chain broken" in log["fact"] and log["fact2"] == "entry 4 does not match its hash"


# ---- the end ------------------------------------------------------------ #
def test_a_whole_run_ends_with_every_node_done_and_the_late_edge_lit():
    model, snap = build(Run().full())
    assert snap["state"] == "done"
    assert states(snap) == {nid: "done" for nid in SPEC_NODES}
    assert snap["active"] is None
    assert [r["verdict"] for r in snap["rail"]] == ["FAIL", "PASS"]
    assert [r["state"] for r in snap["rail"]] == ["done", "done"]
    assert snap["edges"]["ll.summary>fix.ticket"] == "done"
    assert snap["edges"]["fix.ticket>rt.attack"] == "done"
    assert snap["done"]["exit_code"] == 0 and snap["done"]["dur"] == "2.5 s"
    assert snap["done"]["text"].startswith("Done in 2.5 s of run time, shown paced for display. ")
    assert snap["done"]["export"]["status"] == "not_run"
    assert snap["done"]["export"]["text"] == "python ui/export.py --quiet"
    json.dumps(snap, allow_nan=False)


def test_done_text_for_a_real_run_and_a_finished_export():
    model, _ = build(Run(mode="LIVE").full())
    view = bridge.View(export={"status": "ok", "text": "Evidence screens updated: press R."})
    snap = bridge.build_live(model, view)
    assert snap["done"]["text"] == "Done in 2.5 s. Evidence screens updated: press R."
    view = bridge.View(export={"status": "failed",
                               "text": "Export failed: export.py: error: unrecognized arguments: --gov"})
    snap = bridge.build_live(model, view)
    assert snap["done"]["export"]["status"] == "failed"
    assert snap["done"]["text"].endswith("Export failed: export.py: error: unrecognized arguments: --gov")
    view = bridge.View(gov="governance_out/bridge/demo_x")
    snap = bridge.build_live(model, view)
    assert snap["done"]["export"]["text"] == "python ui/export.py --quiet --gov governance_out/bridge/demo_x"
    assert snap["done"]["text"].startswith("Done in 2.5 s. To update the evidence screens run: python ui/export.py")


def test_a_finished_replay_does_not_offer_an_export():
    model, _ = build(Run(mode="LIVE").full())
    snap = bridge.build_live(model, bridge.View(kind="replay", speed=4.0))
    assert snap["done"]["text"] == "Replay finished; the run took 2.5 s."
    assert snap["done"]["export"] == {"status": "skipped", "text": ""}


def test_pipeline_error_marks_the_running_nodes_stopped():
    run = Run()
    run.head()
    run.ev("stage_start", "RC-001", "RC-001:runs", title="runs")
    run.run_events("RC-001")
    run.finish(status="error", exit_code=1, error="model unreachable")
    _, snap = build(run.events)
    assert snap["state"] == "error"
    assert snap["nodes"]["rt.attack"]["state"] == "error" and snap["nodes"]["rt.rate"]["state"] == "error"
    assert snap["nodes"]["ll.trace"]["state"] == "idle"
    assert snap["done"]["exit_code"] == 1
    assert "model unreachable" in snap["done"]["text"]
    assert snap["rail"][0]["state"] == "error"


def test_interrupted_run_is_stopped_not_error():
    run = Run()
    run.head()
    run.ev("stage_start", "RC-001", "RC-001:runs", title="runs")
    run.finish(status="interrupted", exit_code=130)
    _, snap = build(run.events)
    assert snap["state"] == "stopped"
    assert snap["nodes"]["rt.attack"]["state"] == "error"


def test_stage_done_with_error_or_skipped():
    run = Run()
    run.head()
    run.ev("stage_start", "RC-001", "RC-001:gate", title="gate")
    run.ev("stage_done", "RC-001", "RC-001:gate", title="gate", status="error", dur_s=0.0)
    run.ev("stage_start", "RC-001", "RC-001:decide", title="decide")
    run.ev("stage_done", "RC-001", "RC-001:decide", title="decide", status="skipped", dur_s=0.0)
    _, snap = build(run.events)
    assert snap["nodes"]["sl.gate"]["state"] == "error"
    assert snap["nodes"]["sl.review"]["state"] == "skipped"


def test_unknown_event_types_are_ignored():
    run = Run()
    run.head()
    run.ev("something_new", payload=1)
    model, snap = build(run.events)
    assert snap["state"] == "running" and snap["warnings"] == []
    assert model.unknown == 1


def test_events_tail_keeps_the_last_six_lines():
    model, snap = build(Run().full())
    assert len(snap["events_tail"]) == 6
    assert all(isinstance(x, str) and x for x in snap["events_tail"])
    assert snap["events_tail"][-1].startswith("Run finished")


def test_warnings_report_malformed_lines():
    model, _ = build(Run().full())
    assert bridge.build_live(model, bridge.View(malformed=3))["warnings"] == ["3 malformed log lines skipped"]
    assert bridge.build_live(model, bridge.View(malformed=1))["warnings"] == ["1 malformed log line skipped"]
    snap = bridge.build_live(model, bridge.View(notes=["log stopped growing"]))
    assert snap["warnings"] == ["log stopped growing"]


def test_helpers_format_text_from_backend_values():
    assert bridge.fmt_duration(2.5) == "2.5 s"
    assert bridge.fmt_duration(0.0) == "0.0 s"
    assert bridge.fmt_duration(75) == "1 min 15 s"
    assert bridge.fmt_duration(3725) == "1 h 2 min"
    assert bridge.fmt_duration(None) == ""
    assert bridge.fmt_duration(-3) == ""
    assert bridge.fmt_pct(0.4230769230769231) == "42.3%"
    assert bridge.plural(1, "risk") == "1 risk" and bridge.plural(2, "risk") == "2 risks"
    assert bridge.fmt_utc("2026-10-18T06:15:00.123456+00:00") == "2026-10-18 06:15 UTC"
    assert bridge.fmt_utc("2026-10-18T07:15:00+01:00") == "2026-10-18 06:15 UTC"
    assert bridge.fmt_utc("garbage") == "an unknown time"


def test_model_never_raises_on_odd_field_values():
    bad_values = [None, "x", 5, -1, 3.5, [], {}, ["a", 1], {"a": 1}, True]
    events = Run().full()
    base = bridge.LiveModel()
    for idx, e in enumerate(events):
        for kidx, key in enumerate(list(e)):
            if key in ("type", "v", "uid"):
                continue
            for bad in (None, bad_values[(idx + kidx) % len(bad_values)]):
                m = copy.deepcopy(base)
                mutated = dict(e)
                mutated[key] = bad
                m.apply(mutated)
                json.dumps(bridge.build_live(m, bridge.View()), allow_nan=False)
        base.apply(e)


def test_model_survives_events_in_a_strange_order():
    events = Run().full()
    for shuffled in (list(reversed(events)), events[10:] + events[:10], events[::2]):
        model = bridge.LiveModel()
        for e in shuffled:
            model.apply(e)
        json.dumps(bridge.build_live(model, bridge.View()), allow_nan=False)


# ---- lane A's real fixture ---------------------------------------------- #
def test_lane_a_fixture_builds_a_complete_state():
    events = lane_a_events()
    model, snap = build(events)
    assert model.problems == 0
    assert snap["state"] == "done" and snap["mode"] == "SCRIPTED"
    assert states(snap) == {nid: "done" for nid in SPEC_NODES}
    assert [r["id"] for r in snap["rail"]] == ["RC-001", "RC-002", "RC-003"]
    assert [r["verdict"] for r in snap["rail"]] == ["FAIL", "FAIL", "PASS"]
    assert all(r["state"] == "done" for r in snap["rail"])
    assert snap["tiles"]["cand"] == "RC-003" and snap["tiles"]["n"] == 138 and len(snap["tiles"]["list"]) == 138
    assert all(state != "idle" for _, state in snap["tiles"]["list"])
    assert snap["warnings"] == []
    last_runs_done = [e for e in events if e["type"] == "runs_done"][-1]["summary"]
    assert snap["nodes"]["rt.rate"]["fact"].startswith(
        f"{last_runs_done['attack_violations']} of {last_runs_done['attack_runs']} {DOT} ")
    assert snap["nodes"]["sl.gate"]["tag"] == "PASS"
    assert snap["nodes"]["sl.review"]["fact"] == "APPROVE by sarah"
    assert snap["nodes"]["sl.review"]["tag"] == "scripted by the CLI"
    chain = [e for e in events if e["type"] == "chain"][0]
    assert snap["nodes"]["sl.log"]["fact"] == (
        f"{chain['entries']} entries {DOT} chain intact {DOT} head {chain['head12']}")
    assert snap["nodes"]["sl.log"]["fact2"].startswith("tamper check on a copy: caught (")
    assert snap["nodes"]["fix.ticket"]["fact"] == "adds D3"
    assert snap["done"]["exit_code"] == 0
    json.dumps(snap, allow_nan=False)


def test_lane_a_fixture_touches_every_node_and_never_formats_a_null():
    events = lane_a_events()
    model = bridge.LiveModel()
    touched = set()
    for e in events:
        model.apply(e)
        snap = bridge.build_live(model, bridge.View())
        for nid, node in snap["nodes"].items():
            if node["state"] != "idle":
                touched.add(nid)
            for text in (node["fact"], node["fact2"], node["tag"]):
                assert not NULLISH.search(text), (e["seq"], nid, text)
        for tail_line in snap["events_tail"]:
            assert not NULLISH.search(tail_line), tail_line
    assert touched == set(SPEC_NODES)


def test_lane_a_fixture_early_rates_say_not_measured_instead_of_zero():
    model = bridge.LiveModel()
    for e in lane_a_events():
        model.apply(e)
        if e["type"] == "run_done" and e["i"] == 3 and e["cand"] == "RC-001":
            snap = bridge.build_live(model, bridge.View())
            run = e["running"]
            assert run["benign_n"] == 0 and run["benign_rate"] is None
            assert snap["nodes"]["rt.rate"]["fact2"] == "no normal requests run yet"
            return
    pytest.fail("the fixture has no third run of RC-001")


def test_lane_a_fixture_state_is_the_same_when_read_in_one_go_or_event_by_event():
    events = lane_a_events()
    one = bridge.LiveModel()
    for e in events:
        one.apply(e)
    two = bridge.LiveModel()
    two.apply_all(events)
    assert bridge.build_live(one, bridge.View()) == bridge.build_live(two, bridge.View())


# ---- output: scrub, escape, atomic write -------------------------------- #
def test_render_js_is_ascii_with_no_raw_angle_brackets_or_ampersands():
    obj = {"schema": "ui-live/1", "text": "a<b>&c \u00b7 caf\u00e9 \u2014 end", "n": [1, 2.5, None, True]}
    text = bridge.render_js(obj)
    assert text.startswith("window.LIVE = ") and text.endswith(";\n")
    assert text.isascii()
    for ch in "<>&":
        assert ch not in text
    body = text[len("window.LIVE = "):-2]
    assert json.loads(body) == obj
    assert "\\u003c" in text and "\\u003e" in text and "\\u0026" in text


def test_render_js_refuses_nan():
    with pytest.raises(ValueError):
        bridge.render_js({"x": float("nan")})


def test_render_js_with_another_variable_name():
    assert bridge.render_js({"a": 1}, var="LIVE_REPLAY") == 'window.LIVE_REPLAY = {"a":1};\n'


def test_scrub_removes_absolute_paths_and_the_user_name(monkeypatch, tmp_path):
    monkeypatch.setattr(getpass, "getuser", lambda: "gandalf")
    scrub = bridge.Scrubber(roots=[tmp_path])
    inside = str(tmp_path / "results" / "x.csv")
    assert scrub(f"read {inside} ok") == "read results" + os.sep + "x.csv ok"
    assert scrub("see /Users/gandalf/Desktop/x") == "see <path>"
    assert scrub("see /home/someone/x /tmp/y /private/var/z /var/folders/a") == "see <path> <path> <path> <path>"
    assert scrub(r"in C:\Users\gandalf\run\progress.jsonl now") == "in <path> now"
    assert scrub("in D:/work/run/x") == "in <path>"
    assert scrub(r"on \\server\share\file") == "on <path>"
    assert scrub("hello gandalf the grey") == "hello <user> the grey"
    assert scrub("<outside repo>/gate_RC-001.txt") == "<outside repo>/gate_RC-001.txt"
    assert scrub("plain text 42.3% ok") == "plain text 42.3% ok"


def test_scrub_ignores_short_user_names(monkeypatch):
    monkeypatch.setattr(getpass, "getuser", lambda: "tom")
    assert bridge.Scrubber()("tom submitted it") == "tom submitted it"


def test_scrub_walks_every_string_in_the_output(monkeypatch):
    monkeypatch.setattr(getpass, "getuser", lambda: "gandalf")
    scrub = bridge.Scrubber()
    obj = {"a": "/Users/gandalf/x", "b": ["gandalf", {"c": "/tmp/q", "d": 5, "e": None}], "f": ("/home/z",)}
    assert scrub.walk(obj) == {"a": "<path>", "b": ["<user>", {"c": "<path>", "d": 5, "e": None}], "f": ["<path>"]}


def test_a_snapshot_with_paths_in_the_log_is_scrubbed_when_rendered(monkeypatch):
    monkeypatch.setattr(getpass, "getuser", lambda: "gandalf")
    run = Run()
    run.head()
    run.events[0]["out"] = "/Users/gandalf/Desktop/Claude/run"
    run.ev("stage_start", "RC-001", "RC-001:runs", title="runs in /Users/gandalf/x")
    _, snap = build(run.events)
    text = bridge.render_js(bridge.Scrubber().walk(snap))
    assert "/Users" not in text and "gandalf" not in text


def test_write_atomic_replaces_the_file_and_leaves_no_temp(tmp_path):
    target = tmp_path / "live.js"
    bridge.write_atomic(target, "window.LIVE = null;\n")
    assert target.read_text(encoding="utf-8") == "window.LIVE = null;\n"
    bridge.write_atomic(target, "window.LIVE = {};\n")
    assert target.read_text(encoding="utf-8") == "window.LIVE = {};\n"
    assert [p.name for p in tmp_path.iterdir()] == ["live.js"]


def test_write_atomic_creates_the_folder(tmp_path):
    target = tmp_path / "deep" / "data" / "live.js"
    bridge.write_atomic(target, "x\n")
    assert target.read_text(encoding="utf-8") == "x\n"


def test_write_atomic_retries_permission_errors_on_windows(tmp_path, monkeypatch):
    target = tmp_path / "live.js"
    real = os.replace
    calls = {"n": 0}
    sleeps = []

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise PermissionError("in use")
        return real(src, dst)

    monkeypatch.setattr(bridge, "IS_WINDOWS", True)
    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(bridge.time, "sleep", lambda s: sleeps.append(s))
    bridge.write_atomic(target, "ok\n")
    assert target.read_text(encoding="utf-8") == "ok\n"
    assert calls["n"] == 4 and sleeps == [0.05, 0.05, 0.05]


def test_write_atomic_gives_up_after_ten_tries_and_cleans_the_temp(tmp_path, monkeypatch):
    target = tmp_path / "live.js"
    calls = {"n": 0}

    def always(src, dst):
        calls["n"] += 1
        raise PermissionError("in use")

    monkeypatch.setattr(bridge, "IS_WINDOWS", True)
    monkeypatch.setattr(os, "replace", always)
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        bridge.write_atomic(target, "ok\n")
    assert calls["n"] == 10
    assert list(tmp_path.iterdir()) == []


def test_write_atomic_does_not_retry_off_windows(tmp_path, monkeypatch):
    calls = {"n": 0}

    def once(src, dst):
        calls["n"] += 1
        raise PermissionError("denied")

    monkeypatch.setattr(bridge, "IS_WINDOWS", False)
    monkeypatch.setattr(os, "replace", once)
    with pytest.raises(PermissionError):
        bridge.write_atomic(tmp_path / "live.js", "ok\n")
    assert calls["n"] == 1
    assert list(tmp_path.iterdir()) == []


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def test_writer_writes_only_when_the_content_changes(tmp_path):
    clock = FakeClock()
    writer = bridge.LiveWriter(tmp_path / "live.js", clock=clock, wall=lambda: "2026-01-01T00:00:00Z")
    body = bridge.build_live(bridge.LiveModel(), bridge.View())
    assert writer.offer(body) is True
    first = (tmp_path / "live.js").read_text(encoding="utf-8")
    assert '"rev":1' in first and '"written_at":"2026-01-01T00:00:00Z"' in first
    clock.t += 5
    assert writer.offer(copy.deepcopy(body)) is False        # same content: no write, no new rev
    assert (tmp_path / "live.js").read_text(encoding="utf-8") == first
    assert writer.rev == 1


def test_writer_throttles_to_250_ms_and_keeps_the_latest_state(tmp_path):
    clock = FakeClock()
    writer = bridge.LiveWriter(tmp_path / "live.js", clock=clock, wall=lambda: "t")
    a = bridge.build_live(bridge.LiveModel(), bridge.View())
    b = copy.deepcopy(a)
    b["state"] = "running"
    c = copy.deepcopy(a)
    c["state"] = "done"
    assert writer.offer(a) is True
    clock.t += 0.1
    assert writer.offer(b) is False                          # too soon
    clock.t += 0.1
    assert writer.offer(c) is False                          # still too soon
    clock.t += 0.06
    assert writer.offer(c) is True                           # 260 ms after the first write
    assert '"state":"done"' in (tmp_path / "live.js").read_text(encoding="utf-8")
    assert writer.rev == 2


def test_writer_force_bypasses_the_throttle(tmp_path):
    clock = FakeClock()
    writer = bridge.LiveWriter(tmp_path / "live.js", clock=clock, wall=lambda: "t")
    a = bridge.build_live(bridge.LiveModel(), bridge.View())
    b = copy.deepcopy(a)
    b["state"] = "done"
    writer.offer(a)
    assert writer.offer(b, force=True) is True
    assert writer.rev == 2


def test_writer_output_parses_back_to_the_snapshot(tmp_path):
    writer = bridge.LiveWriter(tmp_path / "live.js", clock=FakeClock(), wall=lambda: "t")
    model, snap = build(Run().full())
    writer.offer(snap)
    text = (tmp_path / "live.js").read_text(encoding="utf-8")
    assert text.startswith("window.LIVE = ") and text.endswith(";\n") and text.isascii()
    data = json.loads(text[len("window.LIVE = "):-2])
    assert data["rev"] == 1 and data["schema"] == "ui-live/1"
    assert data["nodes"]["rt.rate"]["fact"] == snap["nodes"]["rt.rate"]["fact"]
    assert data["state"] == "done"


def test_writer_scrubs_what_it_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(getpass, "getuser", lambda: "gandalf")
    writer = bridge.LiveWriter(tmp_path / "live.js", clock=FakeClock(), wall=lambda: "t")
    body = bridge.build_live(bridge.LiveModel(), bridge.View(notes=["see /Users/gandalf/x"]))
    writer.offer(body)
    text = (tmp_path / "live.js").read_text(encoding="utf-8")
    assert "gandalf" not in text and "/Users" not in text


def test_stale_temp_files_are_removed(tmp_path):
    old = tmp_path / ".live-abc.tmp"
    old.write_text("x")
    os.utime(old, (1, 1))
    fresh = tmp_path / ".live-def.tmp"
    fresh.write_text("x")
    other = tmp_path / "demo.js"
    other.write_text("x")
    assert bridge.clean_stale_temp(tmp_path, older_than=60) == 1
    assert not old.exists() and fresh.exists() and other.exists()


def _committed(path):
    try:
        done = subprocess.run(["git", "show", f"HEAD:{path}"], cwd=str(REPO), capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is not available")
    if done.returncode != 0:
        pytest.skip(f"{path} is not committed yet")
    return done.stdout.decode("utf-8")


def test_committed_live_js_is_the_placeholder():
    assert _committed("ui/data/live.js") == "window.LIVE = null;\n", "restore it: git checkout -- ui/data/live.js"


# --------------------------------------------------------------------------- #
# Step 3: pacing, replay, bake, follow.
# --------------------------------------------------------------------------- #
def iso_at(seconds: float) -> str:
    """A UTC timestamp `seconds` after 2026-01-01 00:00:00."""
    from datetime import datetime, timedelta, timezone
    return (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def with_gaps(events, gaps):
    """Copy events and re-time them: event k happens gaps[k] seconds after event k-1 (gaps[0] is ignored)."""
    out, t = [], 0.0
    for e, g in zip(events, gaps):
        t += g
        out.append(dict(e, t=iso_at(t)))
    return out


def simulate(pacer, model, until=10_000.0, step=None):
    """Drive a pacer on a virtual clock; returns [(time, event, beat)] for every applied event."""
    applied, now = [], 0.0
    guard = 0
    while pacer.pending() and guard < 100_000:
        guard += 1
        t = pacer.next_time(model)
        now = max(now, t if t is not None else now)
        if now > until:
            break
        applied.extend((when, ev, beat) for ev, beat, when in pacer.pump(model, now))
        if step:
            now += step
    return applied


def fresh(kind="follow", speed=1.0, scale=1.0):
    return bridge.Pacer(kind=kind, speed=speed, scale=scale), bridge.LiveModel()


def test_display_pacing_holds_after_each_beat_and_spaces_run_done():
    pacer, model = fresh()
    events = Run(n_runs=6).full()
    pacer.push(events, 0.0)
    applied = simulate(pacer, model)
    assert len(applied) == len(events)
    beats = [t for t, _, beat in applied if beat]
    assert len(beats) >= 15
    assert all(b - a >= 1.4 - 1e-9 for a, b in zip(beats, beats[1:]))
    # a stretch of run_done events that are not beats is paced at about 40 ms each
    runs = [t for t, e, beat in applied if e["type"] == "run_done" and not beat]
    gaps = [b - a for a, b in zip(runs, runs[1:]) if b - a < 1.0]
    assert gaps and all(abs(g - 0.04) < 1e-9 for g in gaps)


def test_display_pacing_applies_the_first_event_at_once():
    pacer, model = fresh()
    pacer.push(Run().full(), 5.0)
    first = pacer.pump(model, 5.0)
    assert first and first[0][0]["type"] == "pipeline_start"


def test_pace_scale_shortens_the_holds():
    pacer, model = fresh(scale=0.5)
    pacer.push(Run().full(), 0.0)
    beats = [t for t, _, beat in simulate(pacer, model) if beat]
    gaps = [b - a for a, b in zip(beats, beats[1:])]
    assert min(gaps) >= 0.7 - 1e-9 and max(gaps) < 1.4


def test_live_pacing_keeps_1_2_seconds_between_beats_then_catches_up():
    pacer, model = fresh()
    events = Run(mode="LIVE", n_runs=6).full()
    pacer.push(events, 0.0)                      # a backlog that all arrived at once
    applied = simulate(pacer, model)
    beats = [t for t, _, beat in applied if beat]
    early = [t for t in beats if t <= 15.0]
    assert len(early) >= 8
    assert all(b - a >= 1.2 - 1e-9 for a, b in zip(early, early[1:]))
    assert applied[-1][0] < 17.0                 # more than 15 s behind: the holds are dropped
    assert model.run_state == "done"


def test_live_pacing_applies_a_late_event_the_moment_it_arrives():
    pacer, model = fresh()
    pacer.push(Run(mode="LIVE").full()[:1], 0.0)
    pacer.pump(model, 0.0)
    more = Run(mode="LIVE")
    more.head()
    pacer.push(more.events[1:2], 30.0)           # stage_start setup, 30 s later
    assert [e["type"] for e, _, _ in pacer.pump(model, 30.0)] == ["stage_start"]


def test_live_pacing_never_holds_a_stream_that_is_slower_than_the_hold():
    pacer, model = fresh()
    events = Run(mode="LIVE").full()
    t = 0.0
    seen = []
    for e in events[:12]:
        t += 5.0                                  # one event every 5 s
        pacer.push([e], t)
        seen.extend(pacer.pump(model, t))
    assert len(seen) == 12                        # each applied the moment it arrived


def test_replay_pacing_divides_gaps_by_speed_and_caps_them():
    events = Run(mode="LIVE").full()[:5]
    timed = with_gaps(events, [0, 2.0, 40.0, 0.5, 0.0])
    pacer, model = fresh("replay", speed=4.0)
    pacer.push(timed, 100.0)
    times = [t for t, _, _ in simulate(pacer, model)]
    assert times == pytest.approx([100.0, 100.5, 103.5, 103.625, 103.625])


def test_replay_of_a_scripted_log_uses_the_display_schedule_not_the_recorded_gaps():
    pacer, model = fresh("replay", speed=4.0)
    events = with_gaps(Run().full(), [0.001] * 1000)       # 1 ms apart, like lane A's fixture
    pacer.push(events, 0.0)
    applied = simulate(pacer, model)
    beats = [t for t, _, beat in applied if beat]
    assert all(b - a >= 1.4 - 1e-9 for a, b in zip(beats, beats[1:]))
    assert applied[-1][0] > 20


def test_instant_pacing_applies_everything_in_one_pump():
    pacer, model = fresh("instant")
    events = Run().full()
    pacer.push(events, 0.0)
    assert len(pacer.pump(model, 0.0)) == len(events)
    assert model.run_state == "done"


def test_display_path_is_never_absolute(tmp_path):
    inside = REPO / "ui" / "data" / "live.js"
    assert bridge.display_path(inside) == "ui/data/live.js"
    assert bridge.display_path(tmp_path / "x" / "progress.jsonl") == "<outside repo>/progress.jsonl"
    assert bridge.display_path(tmp_path / "a" / "b.jsonl", root=tmp_path) == "a/b.jsonl"


def write_log(path: Path, events) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e, sort_keys=True, separators=(",", ":")) + "\n" for e in events),
                    encoding="utf-8")
    return path


def test_load_events_skips_junk_and_a_second_run(tmp_path):
    first = Run(uid="aaaaaaaaaaaa").full()
    second = Run(uid="bbbbbbbbbbbb").full()
    p = tmp_path / "progress.jsonl"
    p.write_text("junk\n" + "".join(json.dumps(e) + "\n" for e in first[:5])
                 + "".join(json.dumps(e) + "\n" for e in second[:3]) + "\n[]\n"
                 + "".join(json.dumps(e) + "\n" for e in first[5:]), encoding="utf-8")
    events, bad = bridge.load_events(p)
    assert len(events) == len(first)
    assert all(e["uid"] == "aaaaaaaaaaaa" for e in events)
    assert bad == 2


def make_session(tmp_path, kind="follow", speed=None, scale=1.0, clock=None):
    clock = clock or FakeClock(1000.0)
    writer = bridge.LiveWriter(tmp_path / "live.js", clock=clock, wall=lambda: "t")
    return bridge.Session(writer, kind=kind, log="results/run/progress.jsonl", speed=speed, scale=scale, clock=clock), clock


def read_live(tmp_path):
    text = (tmp_path / "live.js").read_text(encoding="utf-8")
    assert text.startswith("window.LIVE = ") and text.endswith(";\n")
    return json.loads(text[len("window.LIVE = "):-2])


def test_replay_session_writes_live_js_as_time_passes(tmp_path):
    session, clock = make_session(tmp_path, kind="replay", speed=4.0)
    events = with_gaps(Run(mode="LIVE", n_runs=6).full(), [0] + [1.0] * 1000)
    session.feed(events, clock())
    states_seen, labels = set(), set()
    for _ in range(4000):
        session.tick(clock())
        live = read_live(tmp_path) if (tmp_path / "live.js").exists() else None
        if live:
            states_seen.add(live["state"])
            labels.add(live["label"]["text"])
        if not session.pacer.pending():
            break
        clock.t += 0.05
    session.tick(clock(), force=True)
    final = read_live(tmp_path)
    assert final["state"] == "done" and final["source"]["kind"] == "replay" and final["source"]["speed"] == 4.0
    assert "running" in states_seen
    assert any(t.endswith("4× speed · long gaps shortened") for t in labels)
    assert final["source"]["log"] == "results/run/progress.jsonl"


def test_replay_loop_restarts_after_a_pause(tmp_path):
    session, clock = make_session(tmp_path, kind="replay", speed=1000.0)
    events = with_gaps(Run(mode="LIVE").full(), [0] + [0.001] * 1000)
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock.t += s

    runs = {"n": 0}

    def stop():
        return runs["n"] >= 2 and not session.pacer.pending()

    original_reset = session.reset

    def counting_reset():
        runs["n"] += 1
        original_reset()

    session.reset = counting_reset
    bridge.replay_loop(session, events, loop=True, clock=clock, sleep=sleep, stop=stop, pause=3.0)
    assert runs["n"] >= 2                         # it started a second time
    assert sum(sleeps) >= 3.0


def test_replay_without_loop_stops_after_the_last_event(tmp_path):
    session, clock = make_session(tmp_path, kind="replay", speed=1000.0)
    events = with_gaps(Run(mode="LIVE").full(), [0] + [0.001] * 1000)
    bridge.replay_loop(session, events, loop=False, clock=clock, sleep=lambda s: setattr(clock, "t", clock.t + s),
                       stop=lambda: False, pause=3.0)
    assert read_live(tmp_path)["state"] == "done"


# ---- bake ---------------------------------------------------------------- #
def read_replay(path):
    text = Path(path).read_text(encoding="utf-8")
    assert text.startswith("window.LIVE_REPLAY = ") and text.endswith(";\n") and text.isascii()
    for ch in "<>&":
        assert ch not in text
    return json.loads(text[len("window.LIVE_REPLAY = "):-2])


def test_bake_writes_at_most_120_key_frames(tmp_path):
    src = write_log(tmp_path / "in" / "progress.jsonl", lane_a_events())
    out = tmp_path / "replay.js"
    n = bridge.bake(src, out)
    data = read_replay(out)
    assert data["schema"] == "ui-replay/1"
    assert n == len(data["frames"]) and 30 < n <= 120
    frames = data["frames"]
    assert frames[0]["t"] == 0
    assert [f["t"] for f in frames] == sorted(f["t"] for f in frames)
    assert all(isinstance(f["t"], int) for f in frames)
    assert [f["live"]["rev"] for f in frames] == list(range(1, n + 1))
    assert all(f["live"]["schema"] == "ui-live/1" for f in frames)
    assert frames[-1]["live"]["state"] == "done"
    assert {f["live"]["label"]["word"] for f in frames} == {"SCRIPTED"}
    assert data["source"]["mode"] == "SCRIPTED" and data["source"]["speed"] == 4.0
    assert data["source"]["log"] == "<outside repo>/progress.jsonl"
    assert "/Users" not in out.read_text(encoding="utf-8")


def test_bake_keeps_every_node_change(tmp_path):
    src = write_log(tmp_path / "progress.jsonl", lane_a_events())
    bridge.bake(src, tmp_path / "replay.js")
    frames = read_replay(tmp_path / "replay.js")["frames"]
    touched = set()
    for f in frames:
        touched.update(k for k, v in f["live"]["nodes"].items() if v["state"] != "idle")
    assert touched == set(SPEC_NODES)
    assert [r["verdict"] for r in frames[-1]["live"]["rail"]] == ["FAIL", "FAIL", "PASS"]


def test_bake_of_a_real_model_log_says_replay_with_the_speed(tmp_path):
    events = with_gaps(Run(mode="LIVE", n_runs=6).full(), [0] + [2.0] * 1000)
    src = write_log(tmp_path / "progress.jsonl", events)
    bridge.bake(src, tmp_path / "replay.js", speed=8.0)
    data = read_replay(tmp_path / "replay.js")
    labels = {f["live"]["label"]["text"] for f in data["frames"]}
    assert labels == {"REPLAY · real-model run of 2026-01-01 00:00 UTC · 8× speed · long gaps shortened"}
    assert {f["live"]["label"]["status"] for f in data["frames"]} == {"REAL"}
    assert data["frames"][-1]["t"] > 0
    # gaps of 2 s at 8x are 0.25 s each
    assert data["source"]["speed"] == 8.0


def test_bake_small_run_keeps_every_distinct_state(tmp_path):
    events = Run(n_runs=3).full()
    src = write_log(tmp_path / "progress.jsonl", events)
    n = bridge.bake(src, tmp_path / "replay.js")
    model = bridge.LiveModel()
    seen = []
    for e in events:
        model.apply(e)
        snap = bridge.build_live(model, bridge.View(kind="replay", speed=4.0, last_event_age=0.0))
        snap["rev"] = 0
        if not seen or seen[-1] != snap:
            seen.append(snap)
    assert 10 < n <= len(seen)


def test_select_frames_keeps_the_first_the_last_and_the_key_frames():
    frames = [(i * 100, {"i": i}, i % 10 == 0) for i in range(500)]
    kept = bridge.select_frames(frames, limit=120)
    assert len(kept) == 120
    idx = [f[1]["i"] for f in kept]
    assert idx[0] == 0 and idx[-1] == 499
    assert idx == sorted(set(idx))
    assert all(i in idx for i in range(0, 500, 10))        # all 50 key frames survive
    short = bridge.select_frames(frames[:30], limit=120)
    assert len(short) == 30
    few = bridge.select_frames(frames, limit=10)
    assert len(few) == 10 and few[0][1]["i"] == 0 and few[-1][1]["i"] == 499


def test_bake_refuses_an_empty_log(tmp_path):
    p = tmp_path / "progress.jsonl"
    p.write_text("junk\n", encoding="utf-8")
    with pytest.raises(bridge.BridgeError):
        bridge.bake(p, tmp_path / "replay.js")
    with pytest.raises(bridge.BridgeError):
        bridge.bake(tmp_path / "missing.jsonl", tmp_path / "replay.js")


# ---- follow -------------------------------------------------------------- #
def test_resolve_log_path(tmp_path):
    (tmp_path / "out").mkdir()
    assert bridge.resolve_log_path(tmp_path / "out") == tmp_path / "out" / "progress.jsonl"
    assert bridge.resolve_log_path(tmp_path / "x.jsonl") == tmp_path / "x.jsonl"
    assert bridge.resolve_log_path(tmp_path / "later") == tmp_path / "later" / "progress.jsonl"
    f = tmp_path / "odd.log"
    f.write_text("x")
    assert bridge.resolve_log_path(f) == f


def test_find_latest_log_prefers_the_newest_and_ignores_rotated_files(tmp_path):
    old = write_log(tmp_path / "governance_out" / "bridge" / "demo_a" / "progress.jsonl", Run().full())
    new = write_log(tmp_path / "results" / "governance" / "b" / "progress.jsonl", Run().full())
    rotated = write_log(tmp_path / "governance_out" / "c" / "progress.20260101T000000Z.jsonl", Run().full())
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    os.utime(rotated, (3000, 3000))
    assert bridge.find_latest_log(tmp_path) == new
    assert bridge.find_latest_log(tmp_path / "nothing") is None


def make_follower(tmp_path, log, **kw):
    session, clock = make_session(tmp_path, kind="follow", scale=kw.pop("scale", 1.0))
    return bridge.Follower(session, log, **kw), session, clock


def drive(follower, clock, seconds, step=0.05):
    end = clock.t + seconds
    while clock.t < end:
        follower.step(clock())
        clock.t += step


def test_follow_waits_for_a_missing_log_then_follows_it(tmp_path):
    log = tmp_path / "run" / "progress.jsonl"
    follower, session, clock = make_follower(tmp_path, log)
    drive(follower, clock, 1.0)
    live = read_live(tmp_path)
    assert live["state"] == "waiting" and live["label"]["text"] == "Waiting for a run"
    events = Run(mode="LIVE", n_runs=4).full()
    write_log(log, events[:8])
    drive(follower, clock, 3.0)
    live = read_live(tmp_path)
    assert live["state"] == "running" and live["label"]["word"] == "LIVE"
    assert live["source"]["uid"] == UID and live["source"]["kind"] == "follow"
    write_log(log, events)
    drive(follower, clock, 120.0)
    assert read_live(tmp_path)["state"] == "done"


def test_follow_shows_a_scripted_run_slowly_and_completely(tmp_path):
    log = write_log(tmp_path / "progress.jsonl", Run(n_runs=4).full())
    follower, session, clock = make_follower(tmp_path, log)
    snapshots = []
    end = clock.t + 200
    while clock.t < end and not (snapshots and snapshots[-1]["state"] == "done"):
        follower.step(clock())
        if (tmp_path / "live.js").exists():
            snapshots.append(read_live(tmp_path))
        clock.t += 0.05
    assert snapshots[-1]["state"] == "done"
    # nodes appeared one at a time: no snapshot has more than 4 nodes newly running at once
    assert len({s["active"] for s in snapshots if s["active"]}) >= 10
    assert {s["label"]["word"] for s in snapshots} <= {"SCRIPTED"}


def test_follow_resets_when_a_new_run_takes_the_place_of_the_log(tmp_path):
    log = write_log(tmp_path / "progress.jsonl", Run(uid="aaaaaaaaaaaa", mode="LIVE").full())
    follower, session, clock = make_follower(tmp_path, log)
    drive(follower, clock, 120.0)
    assert read_live(tmp_path)["state"] == "done"
    log.rename(tmp_path / "progress.20260101T000000Z.jsonl")
    newer = Run(uid="bbbbbbbbbbbb", mode="LIVE")
    newer.head()
    write_log(log, newer.events)
    drive(follower, clock, 5.0)
    live = read_live(tmp_path)
    assert live["source"]["uid"] == "bbbbbbbbbbbb" and live["state"] == "running"
    assert live["nodes"]["rt.attack"]["state"] == "idle"
    assert live["rev"] > 1


def test_follow_label_goes_from_live_to_replay_and_then_stopped_when_the_log_goes_quiet(tmp_path):
    events = Run(mode="LIVE").full()[:12]
    log = write_log(tmp_path / "progress.jsonl", events)
    follower, session, clock = make_follower(tmp_path, log)
    drive(follower, clock, 20.0)
    assert read_live(tmp_path)["label"]["word"] == "LIVE"
    drive(follower, clock, 45.0)                               # 65 s since the last event arrived
    live = read_live(tmp_path)
    assert live["label"]["word"] == "REPLAY" and live["state"] == "running"
    drive(follower, clock, 130.0)                              # more than 180 s
    live = read_live(tmp_path)
    assert live["state"] == "stopped" and "log stopped growing" in live["warnings"]
    more = Run(mode="LIVE").full()
    with open(log, "a", encoding="utf-8") as fh:
        for e in more[12:14]:
            fh.write(json.dumps(e) + "\n")
    drive(follower, clock, 3.0)
    live = read_live(tmp_path)
    assert live["state"] == "running" and live["label"]["word"] == "LIVE"


def test_follow_counts_malformed_lines(tmp_path):
    events = Run(mode="LIVE").full()[:6]
    log = tmp_path / "progress.jsonl"
    log.write_text("junk\n" + "".join(json.dumps(e) + "\n" for e in events) + "{broken\n", encoding="utf-8")
    follower, session, clock = make_follower(tmp_path, log)
    drive(follower, clock, 3.0)
    assert "2 malformed log lines skipped" in read_live(tmp_path)["warnings"]


def test_follow_latest_switches_to_a_newer_log(tmp_path):
    a = write_log(tmp_path / "governance_out" / "bridge" / "a" / "progress.jsonl", Run(uid="aaaaaaaaaaaa", mode="LIVE").full())
    os.utime(a, (1000, 1000))
    session, clock = make_session(tmp_path)
    follower = bridge.Follower(session, None, latest_root=tmp_path)
    drive(follower, clock, 130.0)
    assert read_live(tmp_path)["source"]["uid"] == "aaaaaaaaaaaa"
    newer = Run(uid="bbbbbbbbbbbb", mode="LIVE")
    newer.head()
    b = write_log(tmp_path / "results" / "governance" / "b" / "progress.jsonl", newer.events)
    os.utime(b, (2000, 2000))
    drive(follower, clock, 5.0)
    live = read_live(tmp_path)
    assert live["source"]["uid"] == "bbbbbbbbbbbb" and live["state"] == "running"
    assert live["source"]["log"] == "<outside repo>/progress.jsonl"


def test_follow_latest_waits_when_there_is_no_log(tmp_path):
    session, clock = make_session(tmp_path)
    follower = bridge.Follower(session, None, latest_root=tmp_path)
    drive(follower, clock, 2.0)
    assert read_live(tmp_path)["state"] == "waiting"


# ---- the command line ---------------------------------------------------- #
def test_main_bake(tmp_path, capsys):
    src = write_log(tmp_path / "progress.jsonl", Run(n_runs=3).full())
    assert bridge.main(["--bake", str(src), "--data-dir", str(tmp_path / "data")]) == 0
    assert (tmp_path / "data" / "replay.js").is_file()
    assert not (tmp_path / "data" / "live.js").exists()
    assert "replay.js" in capsys.readouterr().out


def test_main_replay_runs_to_the_end_and_writes_the_final_state(tmp_path):
    events = with_gaps(Run(mode="LIVE").full(), [0] + [0.5] * 1000)
    src = write_log(tmp_path / "progress.jsonl", events)
    data = tmp_path / "data"
    assert bridge.main(["--replay", str(src), "--speed", "10000", "--data-dir", str(data)]) == 0
    text = (data / "live.js").read_text(encoding="utf-8")
    assert json.loads(text[len("window.LIVE = "):-2])["state"] == "done"


def test_main_replay_of_a_scripted_log_can_be_sped_up_with_the_hidden_pace_scale(tmp_path):
    src = write_log(tmp_path / "progress.jsonl", Run(n_runs=3).full())
    data = tmp_path / "data"
    assert bridge.main(["--replay", str(src), "--pace-scale", "0", "--data-dir", str(data)]) == 0
    live = json.loads((data / "live.js").read_text(encoding="utf-8")[len("window.LIVE = "):-2])
    assert live["state"] == "done" and live["label"]["word"] == "SCRIPTED"


def test_main_reports_problems_without_a_traceback(tmp_path, capsys):
    assert bridge.main(["--replay", str(tmp_path / "missing.jsonl"), "--data-dir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("bridge: log not found") and "Traceback" not in err and str(tmp_path) not in err
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert bridge.main(["--bake", str(empty), "--data-dir", str(tmp_path)]) == 2
    assert "no readable events" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["--replay", "x", "--speed", "0"],
    ["--replay", "x", "--speed", "-3"],
    ["--replay", "x", "--speed", "fast"],
    ["--follow", "x", "--loop"],
    ["--follow", "x", "--replay", "y"],
    ["--bake", "x", "--follow-latest"],
])
def test_main_rejects_bad_arguments(argv):
    with pytest.raises(SystemExit) as exc:
        bridge.main(argv)
    assert exc.value.code == 2


def test_main_without_a_mode_prints_help_and_fails(capsys):
    with pytest.raises(SystemExit) as exc:
        bridge.main([])
    assert exc.value.code == 2
