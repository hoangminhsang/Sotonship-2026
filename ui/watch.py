#!/usr/bin/env python3
"""ui/watch.py - show the backend working, as clean lines, next to the dashboard.

Standard library only (Python 3.9 or newer).

Use it in a real terminal beside the browser while ui/bridge.py runs:

    python ui/watch.py                  follow ui/data/live.js until Ctrl-C
    python ui/watch.py --once           print the current state once and exit
    python ui/watch.py --file PATH      read another live.js
    python ui/watch.py --no-color       plain text (also: NO_COLOR set, or stdout not a terminal)

It reads ui/data/live.js (window.LIVE, schema ui-live/1, written by ui/bridge.py) every 0.3 s
and prints, in step with the page:
  * a header line for each new run (time, SCRIPTED / LIVE / REPLAY / SAVED, model);
  * "> node" when the active node changes, "v node  fact" when a node is done;
  * the pinned example run (LIVE.feed_pin) once, as a block revealed line by line;
  * each new item of LIVE.feed once (run, fingerprint, gate, audit, chain, tickets);
  * the closing line (LIVE.done.text).
It never invents text: a field that is null or missing is skipped. An old live.js without
the feed still gives the header, the node lines and the closing line. It only reads the file.
Windows is not verified.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

POLL_S = 0.3
PIN_DELAY_S = 0.6        # between the lines of the pinned example
RUN_DELAY_S = 0.12       # after a run line, so about 8 run lines a second at most
MAX_RUN_LINES = 8        # run lines shown per poll; older new runs are counted instead
MAX_WIDTH = 100

NODE_NAMES = {
    "rt.attack": "Attack the agent",
    "rt.record": "Watch every tool call",
    "rt.check": "Check the rules",
    "rt.rate": "Rate the attack success",
    "rt.fixes": "Raise fix tickets",
    "sl.fingerprint": "Fingerprint the update",
    "sl.gate": "Release gate",
    "sl.review": "A person decides",
    "sl.log": "Tamper-evident log",
    "fix.ticket": "Fix and test again",
    "ll.risks": "Risks under control?",
    "ll.controls": "Controls in use",
    "ll.evidence": "Evidence",
    "ll.trace": "Trace one attack",
    "ll.summary": "Summary",
}

ANSI = {"red": "31", "green": "32", "cyan": "36", "yellow": "33", "dim": "2", "bold": "1"}

# A printed line: (text, colour name or None, seconds to wait after it).
Line = Tuple[str, Optional[str], float]


def clip(text: str, width: int = MAX_WIDTH) -> str:
    text = str(text).replace("\r", " ").replace("\n", " ").replace("\t", " ")     # one line
    return text if len(text) <= width else text[: width - 1] + "…"


def read_live(path: Path) -> Tuple[bool, Optional[dict]]:
    """Read live.js. Returns (ok, live). ok is False for a missing or half-written file.
    live is None when the file says `window.LIVE = null;` (no run yet)."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, None
    body = raw.strip()
    if body.startswith("window"):
        body = body.partition("=")[2].strip()
    if body.endswith(";"):
        body = body[:-1].rstrip()
    try:
        data = json.loads(body)
    except ValueError:
        return False, None
    if data is None:
        return True, None
    return (True, data) if isinstance(data, dict) else (False, None)


def _s(value: Any) -> Optional[str]:
    """A non-empty string, or None."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


class Watcher:
    """Turns successive LIVE snapshots into lines. No printing and no sleeping here."""

    def __init__(self, clock=time.localtime) -> None:
        self.clock = clock
        self.uid: Any = None
        self.prev_state: Optional[str] = None
        self.started = False        # a header was printed for the current run
        self.ended = False
        self.node_state: Dict[str, str] = {}
        self.last_active: Optional[str] = None
        self.seen_seq: set = set()
        self.pin_done = False
        self.pin_key: Optional[tuple] = None
        self.printed_any = False

    # -- one snapshot -> lines --------------------------------------------------------
    def step(self, live: Optional[dict]) -> List[Line]:
        if not isinstance(live, dict):
            return []
        out: List[Line] = []
        state = _s(live.get("state")) or "waiting"
        uid = _dict(live.get("source")).get("uid")
        new_run = False
        if state != "waiting" and (
            not self.started
            or (uid is not None and uid != self.uid)
            or (state == "running" and self.prev_state in ("done", "error", "stopped"))
        ):
            new_run = True
        if new_run:
            self._reset()
            out += self._header(live)
            self.started = True
        if uid is not None:
            self.uid = uid
        self.prev_state = state
        if state == "waiting":
            return out

        out += self._nodes(live)
        out += self._pin(live)
        out += self._feed(live)
        if state in ("done", "error", "stopped") and not self.ended:
            self.ended = True
            end = _s(_dict(live.get("done")).get("text"))
            if end is None:
                end = "run " + state
            out.append((clip(end), "green" if state == "done" else "red", 0.0))
        return out

    def _reset(self) -> None:
        self.node_state = {}
        self.last_active = None
        self.seen_seq = set()
        self.pin_done = False
        self.pin_key = None
        self.ended = False

    # -- parts ------------------------------------------------------------------------
    def _header(self, live: dict) -> List[Line]:
        label = _dict(live.get("label"))
        word = _s(label.get("word"))
        parts = [time.strftime("%H:%M:%S", self.clock()), "run"]
        if word:
            parts.append(word)
        if word == "SCRIPTED":
            parts.append("no model called")      # the model name in a scripted run is only a setting
        elif _s(live.get("model")):
            parts.append(_s(live.get("model")))
        out: List[Line] = []
        if self.printed_any:
            out.append(("", None, 0.0))
        out.append((clip("  ".join(parts)), "bold", 0.0))
        return out

    def _nodes(self, live: dict) -> List[Line]:
        out: List[Line] = []
        nodes = _dict(live.get("nodes"))
        for nid, node in nodes.items():
            node = _dict(node)
            st = _s(node.get("state")) or "idle"
            before = self.node_state.get(nid, "idle")
            self.node_state[nid] = st
            if st == before or st not in ("done", "error"):
                continue
            name = NODE_NAMES.get(nid, nid)
            fact = _s(node.get("fact"))
            mark, colour = ("✓", "green") if st == "done" else ("✗", "red")
            out.append((clip(f"{mark} {name}" + (f"  {fact}" if fact else "")), colour, 0.0))
        active = _s(live.get("active"))
        if active != self.last_active:
            self.last_active = active
            if active and self.node_state.get(active) not in ("done", "error"):
                out.append((clip("▶ " + NODE_NAMES.get(active, active)), "yellow", 0.0))
        return out

    def _pin(self, live: dict) -> List[Line]:
        pin = live.get("feed_pin")
        if self.pin_done or not isinstance(pin, dict):
            return []
        self.pin_done = True
        self.pin_key = (pin.get("test_id"), pin.get("rep"), pin.get("cand"))
        if isinstance(pin.get("seq"), int):
            self.seen_seq.add(pin["seq"])
        scripted = _s(_dict(live.get("label")).get("word")) == "SCRIPTED"
        who = "scripted agent" if scripted else (_s(live.get("model")) or "agent")
        block: List[Line] = []

        def add(key: str, value: str, colour: Optional[str] = None) -> None:
            block.append((clip(f"{key:<9}{value}"), colour, PIN_DELAY_S))

        task, doc, hidden, calls = (_s(pin.get(k)) for k in ("user_task", "doc", "hidden", "tool_calls"))
        if task:
            add("task", f'"{task}"')
        if doc:
            add("reads", doc)
        if hidden:
            add("hidden", f'"{hidden}"', "red")
        if calls:
            add("agent", f"{who}: {calls}")
        if isinstance(pin.get("violated"), bool):
            if pin["violated"]:
                classes = ", ".join(str(c) for c in pin.get("classes") or [] if _s(c))
                add("verdict", "CAUGHT" + (f": {classes}" if classes else ""), "red")
            else:
                add("verdict", "HELD: did only the task", "green")
        if not block:
            return []
        block.insert(0, ("", None, 0.0))
        block[-1] = (block[-1][0], block[-1][1], 0.0)
        return block

    def _feed(self, live: dict) -> List[Line]:
        feed = live.get("feed")
        if not isinstance(feed, list):
            return []
        new = []
        for item in feed:
            if not isinstance(item, dict) or not isinstance(item.get("seq"), int):
                continue
            if item["seq"] in self.seen_seq:
                continue
            self.seen_seq.add(item["seq"])
            if (item.get("type") == "run" and self.pin_key is not None
                    and (item.get("test_id"), item.get("rep"), item.get("cand")) == self.pin_key):
                continue        # the pinned example is already shown
            new.append(item)
        new.sort(key=lambda it: it["seq"])
        runs = [it for it in new if it.get("type") == "run"]
        keep = {id(it) for it in runs[-MAX_RUN_LINES:]}
        skipped = len(runs) - len(keep)
        out: List[Line] = []
        said_skipped = False
        for item in new:
            if item.get("type") == "run":
                if id(item) not in keep:
                    continue
                if skipped and not said_skipped:
                    said_skipped = True
                    out.append((f"… {skipped} more runs", "dim", 0.0))
                line = self._run_line(item)
                if line:
                    out.append((line[0], line[1], RUN_DELAY_S))
            else:
                line = self._item_line(item)
                if line:
                    out.append((line[0], line[1], 0.0))
        return out

    @staticmethod
    def _run_line(item: dict) -> Optional[Tuple[str, Optional[str]]]:
        parts = [p for p in (_s(item.get("test_id")), _s(item.get("technique"))) if p]
        violated = item.get("violated")
        if item.get("kind") == "benign":
            word, colour = ("benign violated", "red") if violated is True else ("benign ok", "green")
        elif violated is True:
            word, colour = "CAUGHT", "red"
        elif violated is False:
            word, colour = "held", "green"
        else:
            word, colour = None, None
        if word:
            parts.append(word)
        return (clip("  ".join(parts)), colour) if parts else None

    @staticmethod
    def _item_line(item: dict) -> Optional[Tuple[str, Optional[str]]]:
        kind = item.get("type")
        if kind == "fingerprint":
            ident = _s(item.get("identity12"))
            if not ident:
                return None
            tail = "  changed" if item.get("changed_vs_prev") is True else ""
            return clip(f"fingerprint  {ident}{tail}"), "cyan"
        if kind == "gate":
            verdict, reason = _s(item.get("verdict")), _s(item.get("reason"))
            if not verdict:
                return None
            colour = "red" if verdict == "FAIL" else "green" if verdict == "PASS" else None
            return clip(f"gate  {verdict}" + (f"  {reason}" if reason else "")), colour
        if kind == "audit":
            parts = ["logged"]
            if item.get("entry") is not None:
                parts.append(f"entry {item['entry']}")
            if _s(item.get("hash12")):
                parts.append(f"hash {item['hash12']}")
            if item.get("scripted") is True:
                parts.append("scripted reviewer")
            return (clip("  ".join(parts)), None) if len(parts) > 1 else None
        if kind == "chain":
            if not isinstance(item.get("ok"), bool):
                return None
            n = item.get("entries")
            text = "chain  " + ("intact" if item["ok"] else "broken") + (f"  {n} entries" if n is not None else "")
            return clip(text), "green" if item["ok"] else "red"
        if kind == "tickets":
            title = _s(item.get("title"))
            return (clip(f"fix raised  {title}"), "yellow") if title else None
        return None


def paint(text: str, colour: Optional[str], use_color: bool) -> str:
    if not use_color or not colour:
        return text
    return f"\033[{ANSI[colour]}m{text}\033[0m"


def want_color(no_color_flag: bool, stream=None) -> bool:
    stream = stream if stream is not None else sys.stdout
    if no_color_flag or os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def emit(lines: List[Line], use_color: bool, pace: bool) -> None:
    for text, colour, delay in lines:
        print(paint(text, colour, use_color), flush=True)
        if pace and delay > 0:
            time.sleep(delay)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Show the backend working, as clean lines, beside the dashboard.")
    ap.add_argument("--file", default=str(Path(__file__).resolve().parent / "data" / "live.js"),
                    help="the live.js to follow (default: ui/data/live.js next to this script)")
    ap.add_argument("--once", action="store_true", help="print the current state once and exit")
    ap.add_argument("--no-color", action="store_true", help="plain text, no colours")
    args = ap.parse_args(argv)

    use_color = want_color(args.no_color)
    if os.name == "nt":
        os.system("")       # constant empty command (no input): turns on VT (ANSI) processing in the Windows console
    for stream in (sys.stdout,):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    path = Path(args.file)
    watcher = Watcher()
    try:
        if args.once:
            ok, live = read_live(path)
            lines = watcher.step(live) if ok else []
            emit(lines, use_color, pace=False)
            if not lines:
                print("waiting for a run", flush=True)
            return 0
        print(paint(clip(f"watching {path}"), "dim", use_color), flush=True)
        announced = False
        while True:
            ok, live = read_live(path)
            lines = watcher.step(live) if ok else []
            if lines:
                watcher.printed_any = True
                emit(lines, use_color, pace=True)
            elif not announced and ok and not watcher.started:
                announced = True
                print("waiting for a run", flush=True)
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
