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
