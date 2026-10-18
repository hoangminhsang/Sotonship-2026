"""Tests for ui/watch.py: the terminal view that follows ui/data/live.js.

No model, no network, no sleeping. Fixtures are written in tmp_path; the real ui/data is not touched.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("ui_watch", REPO / "ui" / "watch.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ui_watch"] = module
    spec.loader.exec_module(module)
    return module


watch = _load()


def _atk2() -> dict:
    for line in (REPO / "corpus" / "test.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["id"] == "ATK-0002":
            return row
    raise AssertionError("ATK-0002 not in corpus/test.jsonl")


ROW = _atk2()


def _pin(seq: int = 1) -> dict:
    return {
        "seq": seq, "type": "run", "i": 2, "n": 12, "cand": "RC-001", "test_id": "ATK-0002", "rep": 1,
        "kind": "attack", "technique": ROW["technique"], "goal": ROW["goal"], "violated": True,
        "classes": ["V_read_restricted"], "calls": 2, "blocked": 0,
        "tool_calls": "read_doc, query_assets(VAULT-01)", "duration_s": 0.1,
        "user_task": ROW["user_task"], "doc": ROW["inject_target"]["doc"],
        "hidden": ROW["poison"],
    }


def _live(**over) -> dict:
    live = {
        "schema": "ui-live/1", "state": "running", "source": {"uid": "u1"},
        "label": {"word": "SCRIPTED", "text": "SCRIPTED · paced for display", "status": "SAMPLE"},
        "model": None, "active": "rt.record",
        "nodes": {"rt.attack": {"state": "done", "fact": "138 runs"},
                  "rt.record": {"state": "running", "fact": "40 tool calls"}},
        "done": None,
        "feed": [_pin(1),
                 {"seq": 2, "type": "run", "test_id": "ATK-0003", "technique": "T2_polite", "kind": "attack",
                  "violated": False, "cand": "RC-001", "rep": 1},
                 {"seq": 3, "type": "run", "test_id": "BEN-0001", "technique": None, "kind": "benign",
                  "violated": False, "cand": "RC-001", "rep": 1},
                 {"seq": 4, "type": "fingerprint", "identity12": "000000000007", "changed_vs_prev": True},
                 {"seq": 5, "type": "gate", "cand": "RC-001", "verdict": "FAIL", "reason": "1 of 4 checks failed"},
                 {"seq": 6, "type": "audit", "entry": 3, "event": "decision", "verdict": "BLOCK",
                  "decision": "BLOCK", "scripted": True, "hash12": "00000000000c", "by": "sarah"},
                 {"seq": 7, "type": "chain", "ok": True, "entries": 10, "head12": "abc"},
                 {"seq": 8, "type": "tickets", "rows": 3, "title": "3 fix tickets"}],
        "feed_pin": _pin(1),
    }
    live.update(over)
    return live


def _texts(lines):
    return [t for t, _c, _d in lines]


def _write(path: Path, live) -> None:
    path.write_text("window.LIVE = " + json.dumps(live) + ";\n", encoding="utf-8")


# ---------------------------------------------------------------------------------- reading
def test_read_live_parses_prefix_and_semicolon(tmp_path):
    p = tmp_path / "live.js"
    _write(p, {"state": "running"})
    assert watch.read_live(p) == (True, {"state": "running"})


def test_read_live_null_is_ok_but_empty(tmp_path):
    p = tmp_path / "live.js"
    p.write_text("window.LIVE = null;\n", encoding="utf-8")
    assert watch.read_live(p) == (True, None)


def test_read_live_tolerates_missing_and_half_written(tmp_path):
    assert watch.read_live(tmp_path / "nope.js") == (False, None)
    p = tmp_path / "live.js"
    p.write_text('window.LIVE = {"state": "run', encoding="utf-8")
    assert watch.read_live(p) == (False, None)
    p.write_text("", encoding="utf-8")
    assert watch.read_live(p) == (False, None)


def test_step_ignores_none_and_non_dict():
    w = watch.Watcher()
    assert w.step(None) == []
    assert w.step([1, 2]) == []
    assert w.step({"state": "waiting"}) == []


# ---------------------------------------------------------------------------------- header and nodes
def test_header_scripted_says_no_model_called():
    header = _texts(watch.Watcher().step(_live()))[0]
    assert "run" in header and "SCRIPTED" in header and "no model called" in header


def test_header_scripted_hides_the_configured_model_name():
    header = _texts(watch.Watcher().step(_live(model="qwen2.5:7b-instruct")))[0]
    assert "qwen" not in header and "no model called" in header


def test_header_live_shows_model_not_scripted_note():
    live = _live(label={"word": "LIVE", "text": "LIVE", "status": "REAL"}, model="qwen3:8b")
    header = _texts(watch.Watcher().step(live))[0]
    assert "LIVE" in header and "qwen3:8b" in header and "no model called" not in header


def test_node_lines_active_and_done_once():
    w = watch.Watcher()
    first = _texts(w.step(_live()))
    assert "✓ Attack the agent  138 runs" in first
    assert "▶ Watch every tool call" in first
    again = _texts(w.step(_live()))
    assert not any(t.startswith(("✓", "▶")) for t in again)
    nxt = _live(active="rt.check")
    nxt["nodes"]["rt.record"] = {"state": "done", "fact": "40 tool calls"}
    nxt["nodes"]["rt.check"] = {"state": "running", "fact": ""}
    got = _texts(w.step(nxt))
    assert "✓ Watch every tool call  40 tool calls" in got
    assert "▶ Check the rules" in got


def test_end_line_once_with_done_text():
    w = watch.Watcher()
    w.step(_live())
    done = _live(state="done", active=None, done={"exit_code": 0, "dur": "2.5 s", "text": "Done in 2.5 s."})
    assert _texts(w.step(done))[-1] == "Done in 2.5 s."
    assert "Done in 2.5 s." not in _texts(w.step(done))


def test_new_uid_starts_new_run_and_resets_seq():
    w = watch.Watcher()
    w.step(_live())
    second = _texts(w.step(_live(source={"uid": "u2"})))
    assert any("no model called" in t for t in second)
    assert any(t.startswith("ATK-0003") for t in second)       # seq 2 shown again in the new run


def test_old_live_js_without_feed_still_prints_header_nodes_end():
    old = _live(state="done", active=None, done={"text": "Done in 1 s."})
    for key in ("feed", "feed_pin"):
        del old[key]
    got = _texts(watch.Watcher().step(old))
    assert any("SCRIPTED" in t for t in got)
    assert any(t.startswith("✓") for t in got)
    assert got[-1] == "Done in 1 s."


# ---------------------------------------------------------------------------------- pin
def test_pin_block_uses_real_atk0002_text_and_prints_once():
    w = watch.Watcher()
    lines = w.step(_live())
    texts = _texts(lines)
    assert f'task     "{ROW["user_task"]}"' in texts
    assert "reads    " + ROW["inject_target"]["doc"] in texts
    assert f'hidden   "{ROW["poison"]}"' in texts
    assert any(t.startswith("agent    scripted agent: read_doc") for t in texts)
    assert "verdict  CAUGHT: V_read_restricted" in texts
    colours = {t.split()[0]: c for t, c, _d in lines if t.startswith(("hidden", "verdict"))}
    assert colours == {"hidden": "red", "verdict": "red"}
    pin_delays = [d for t, _c, d in lines if t.startswith(("task", "reads", "hidden", "agent"))]
    assert pin_delays and all(d == watch.PIN_DELAY_S for d in pin_delays)
    again = _texts(w.step(_live()))
    assert not any(t.startswith(("task ", "hidden ", "verdict ")) for t in again)


def test_pin_not_repeated_as_run_line():
    texts = _texts(watch.Watcher().step(_live()))
    assert not any(t.startswith("ATK-0002") for t in texts)


def test_pin_held_and_null_fields_skipped():
    pin = _pin()
    pin.update(violated=False, hidden=None, classes=[])
    texts = _texts(watch.Watcher().step(_live(feed=[], feed_pin=pin)))
    assert "verdict  HELD: did only the task" in texts
    assert not any(t.startswith("hidden") for t in texts)


def test_pin_agent_uses_model_when_present():
    live = _live(model="qwen3:8b", label={"word": "LIVE"})
    texts = _texts(watch.Watcher().step(live))
    assert any(t.startswith("agent    qwen3:8b: ") for t in texts)


# ---------------------------------------------------------------------------------- feed
def test_feed_lines_and_seq_dedupe():
    w = watch.Watcher()
    texts = _texts(w.step(_live()))
    assert "ATK-0003  T2_polite  held" in texts
    assert "BEN-0001  benign ok" in texts
    assert "fingerprint  000000000007  changed" in texts
    assert "gate  FAIL  1 of 4 checks failed" in texts
    assert "logged  entry 3  hash 00000000000c  scripted reviewer" in texts
    assert "chain  intact  10 entries" in texts
    assert "fix raised  3 fix tickets" in texts
    live = _live()
    live["feed"].append({"seq": 9, "type": "gate", "verdict": "PASS", "reason": "all checks passed"})
    second = _texts(w.step(live))
    assert second == ["gate  PASS  all checks passed"]


def test_run_line_caught_and_broken_chain():
    assert watch.Watcher._run_line({"test_id": "ATK-1", "technique": "T1", "kind": "attack",
                                    "violated": True}) == ("ATK-1  T1  CAUGHT", "red")
    line = watch.Watcher._item_line({"type": "chain", "ok": False, "entries": 4})
    assert line == ("chain  broken  4 entries", "red")


def test_backlog_is_counted_not_printed():
    runs = [{"seq": i, "type": "run", "test_id": f"ATK-{i:04d}", "technique": "T1", "kind": "attack",
             "violated": i % 2 == 0, "cand": "RC-001", "rep": 1} for i in range(1, 31)]
    texts = _texts(watch.Watcher().step(_live(feed=runs, feed_pin=None)))
    assert "… 22 more runs" in texts
    assert sum(t.startswith("ATK-") for t in texts) == 8
    assert texts.index("… 22 more runs") < texts.index("ATK-0023  T1  held")


def test_lines_are_clipped():
    pin = _pin()
    pin["hidden"] = "x" * 400
    for text in _texts(watch.Watcher().step(_live(feed_pin=pin))):
        assert len(text) <= watch.MAX_WIDTH
    assert watch.clip("y" * 300).endswith("…")


# ---------------------------------------------------------------------------------- colour and --once
def test_color_off_when_not_a_tty(monkeypatch):
    class Stream:
        def isatty(self):
            return False

    class Tty:
        def isatty(self):
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert watch.want_color(False, Stream()) is False
    assert watch.want_color(False, Tty()) is True
    assert watch.want_color(True, Tty()) is False
    monkeypatch.setenv("NO_COLOR", "1")
    assert watch.want_color(False, Tty()) is False


def test_paint_only_when_enabled():
    assert watch.paint("x", "red", False) == "x"
    assert watch.paint("x", "red", True) == "\033[31mx\033[0m"
    assert watch.paint("x", None, True) == "x"


def test_once_prints_fixture_and_exits_zero(tmp_path, capsys):
    p = tmp_path / "live.js"
    _write(p, _live(state="done", active=None, done={"text": "Done in 2.5 s."}))
    assert watch.main(["--file", str(p), "--once"]) == 0
    out = capsys.readouterr().out
    assert "\033[" not in out                       # captured stdout is not a tty
    assert "SCRIPTED" in out and "no model called" in out
    assert f'task     "{ROW["user_task"]}"' in out
    assert "verdict  CAUGHT: V_read_restricted" in out
    assert "gate  FAIL  1 of 4 checks failed" in out
    assert out.rstrip().endswith("Done in 2.5 s.")
    assert all(len(line) <= watch.MAX_WIDTH for line in out.splitlines())


def test_once_with_null_missing_or_half_written_file(tmp_path, capsys):
    p = tmp_path / "live.js"
    p.write_text("window.LIVE = null;", encoding="utf-8")
    assert watch.main(["--file", str(p), "--once"]) == 0
    assert "waiting for a run" in capsys.readouterr().out
    assert watch.main(["--file", str(tmp_path / "missing.js"), "--once"]) == 0
    p.write_text('window.LIVE = {"state"', encoding="utf-8")
    assert watch.main(["--file", str(p), "--once", "--no-color"]) == 0
    assert "waiting for a run" in capsys.readouterr().out
