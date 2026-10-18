#!/usr/bin/env python3
"""ui/bridge.py - follow a pipeline run's progress.jsonl and feed the pipeline screen.

Standard library only (Python 3.9 or newer), cross-platform paths.

The pipeline run (scripts/run_governance.py) appends one JSON object per line to
<out>/progress.jsonl. This bridge reads those lines, keeps a small state, and
rewrites ui/data/live.js (window.LIVE, schema ui-live/1) so the page can draw it.
The page never starts a command and never computes a number: every number in
live.js is formatted here from a backend value.

EVENT -> NODE TABLE
===================
One table says what each progress event does on the screen. "pass" means one
candidate's trip round the loop (RC-001, then RC-002, ...). A node shows
"running" until the NEXT event or its stage_done, whichever comes first, so a
one-shot event is still visible as "running" for the display hold.

    trigger (event type / stage id)     node            state after         fact text (formatted here)
    ----------------------------------- --------------- ------------------- --------------------------------------
    pipeline_start                      (all)           idle                label, mode, run status (REAL|SAMPLE)
    meta                                (rail, tiles)   -                   rail "RC-002 . defences D2"; tiles n
    stage_start RC-00N:runs, pass 1     rt.attack       running             "138 runs planned"
    stage_start RC-00N:runs, pass 2+    fix.ticket      running -> done     "adds D3" / fact2 "RC-003 enforces D2, D3"
      (the short-loop row is reset)     rt.attack       running (next event) "138 runs planned"; edge "test again" lit
    run_start (LIVE)                    rt.attack       running             "run 3 of 12 . ATK-0021 rep 1"; tile running
    run_done                            rt.attack       running             "run 3 of 12 . ATK-0021 rep 1"; tile state
      1st / 2nd / 3rd run_done of pass  rt.record       running             "40 tool calls . 3 blocked"
                                        rt.check        running             "V_read_restricted 12 . V_x 9 . +1 more"
                                        rt.rate         running             "33 of 78 . 42.3% (32.0% to 53.4%)"
    runs_done                           rt.attack       done                "138 runs"
                                        rt.record       done                final calls and blocked
                                        rt.check        done                final class counts
                                        rt.rate         done                final rate with its range; fact2 normal
                                                                            requests "handled 100.0% of 60"
    fingerprint                         sl.fingerprint  running (-> done)   "identity 000000000007 . changed"
    stage_start :gate, gate, stage_done sl.gate         running -> done     "FAIL . 1 of 4 checks failed"; tag FAIL|PASS
    stage_start :decide                 sl.review       running             -
    audit (submit, verdict, ...)        sl.log          running             "entry 3 . decision . 00000000000c"
    audit decision                      sl.review       running             "BLOCK by sarah"; tag "scripted by the CLI"
    audit decision_refused              sl.review       running (a beat)    "refused: tom"; fact2 the backend reason
    stage_done :decide                  sl.review/log   done                -
    stage_start :assure                 ll.risks        running             -
    risks                               ll.risks        done                "9 risks . 3 open"
                                        ll.controls     running (-> done)   "controls used: D2, D3"
    tickets                             rt.fixes        running (-> done)   "3 fix tickets . 3 open risks"
    evidence                            ll.evidence     running (-> done)   "2 evidence rows"
    stage_start trace, trace            ll.trace        running -> done     "ATK-0021 . 12 steps" / "nothing to trace"
    stage_start summary, summary        ll.summary      running -> done     "6 summary lines"; fact2 first line
    stage_start chain, chain            sl.log          running -> done     "10 entries . chain intact . head ..."
                                                                            tag intact|broken; fact2 tamper check
    stage_done status error|skipped     the stage's     error | skipped     -
                                        nodes
    pipeline_done                       all running     done | error        label, rail verdicts, "Done in 2.5 s"
    artifact, anything else             -               -                   only the events_tail line

Rules that apply to every row:
  * A value with nothing behind it (null) is never shown as 0: "no attack runs yet",
    "no normal requests run yet".
  * Percentages use the backend's format, f"{v:.1%}". Counts are "x of n".
  * rt.fixes and the ll.* nodes keep their state from pass to pass; the other
    nine nodes are reset at the start of each pass.
  * `active` is the node that most recently started; `prev_active` the one before.
    The page animates the edge prev_active>active.
  * A node whose event says nothing new keeps its last fact; facts are short
    (about 50 characters) because the node box has two 22 px lines.

This file grows in steps. Step 2 (this one): the state builder, the live.js writer.
"""
from __future__ import annotations

import getpass
import json
import math
import os
import re
import tempfile
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_LIVE = "ui-live/1"
SCHEMA_REPLAY = "ui-replay/1"
LOG_NAME = "progress.jsonl"

UI_DIR = Path(__file__).resolve().parent
REPO_ROOT = UI_DIR.parent
DATA_DIR = UI_DIR / "data"
IS_WINDOWS = os.name == "nt"

# A progress line is a few hundred bytes (the largest, a gate or a risk register,
# a few kilobytes). A "line" longer than this is a corrupt file, not an event.
MAX_LINE_BYTES = 1_000_000
# Bytes read from the file per poll. A bigger backlog is read over several polls.
MAX_READ_BYTES = 4_000_000
# The first bytes of the file, kept to notice that a different file took its place.
HEAD_BYTES = 64

THROTTLE_S = 0.25          # live.js is rewritten at most this often
LIVE_WINDOW_S = 60.0       # a LIVE log counts as growing while its last event is younger than this
REPLACE_TRIES = 10         # os.replace attempts on Windows when the page holds the file open
REPLACE_WAIT_S = 0.05
TAIL_LINES = 6
MAX_TILES = 600


# --------------------------------------------------------------------------- #
# Tailing: read the new complete lines of a growing file.
# --------------------------------------------------------------------------- #
class TailResult:
    """What one poll of the file found."""

    __slots__ = ("lines", "reset", "missing", "oversize")

    def __init__(self, lines: Optional[List[str]] = None, reset: bool = False,
                 missing: bool = False, oversize: int = 0) -> None:
        self.lines = lines if lines is not None else []
        self.reset = reset          # the file is a different one: forget everything read before
        self.missing = missing      # the file does not exist (yet)
        self.oversize = oversize    # runaway lines dropped in this poll


class Tailer:
    """Reads the complete lines appended to a file since the last poll.

    * Bytes are read from the last offset and split on b"\\n"; a trailing
      fragment waits (as bytes, so a split multi-byte character is safe) until
      its newline arrives.
    * Lines are decoded with errors="replace".
    * The file is opened and closed on every poll. A handle held open would stop
      the emitter from renaming the old log at the start of the next run on
      Windows.
    * The state resets when the file is missing, shrinks, changes identity, or
      starts with different bytes than before.
    """

    def __init__(self, path) -> None:
        self.path = Path(path)
        self._fresh()
        self.present = False

    def _fresh(self) -> None:
        self.offset = 0
        self.fragment = b""
        self.ident = None            # (st_dev, st_ino) of the file being read
        self.head = b""              # first bytes of that file
        self.skipping = False        # discarding the rest of a runaway line

    def poll(self) -> TailResult:
        try:
            st = self.path.stat()
        except OSError:
            had = self.present
            self.present = False
            self._fresh()
            return TailResult(missing=True, reset=had)

        reset = False
        ident = (st.st_dev, st.st_ino)
        if self.present and self.ident is not None:
            if st.st_size < self.offset or ident != self.ident or not self._head_matches():
                self._fresh()
                reset = True
        elif not self.present and (self.offset or self.ident is not None):
            self._fresh()
            reset = True
        self.present = True
        self.ident = ident

        if st.st_size <= self.offset:
            return TailResult(reset=reset)
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                data = fh.read(min(st.st_size - self.offset, MAX_READ_BYTES))
        except OSError:
            return TailResult(reset=reset)
        if self.offset == 0 and data:
            self.head = data[:HEAD_BYTES]
        self.offset += len(data)

        lines: List[str] = []
        oversize = 0
        if self.skipping:
            cut = data.find(b"\n")
            if cut < 0:
                return TailResult(reset=reset)
            data = data[cut + 1:]
            self.skipping = False
        buf = self.fragment + data
        parts = buf.split(b"\n")
        self.fragment = parts.pop()
        for raw in parts:
            if len(raw) > MAX_LINE_BYTES:
                oversize += 1
                continue
            lines.append(raw.rstrip(b"\r").decode("utf-8", errors="replace"))
        if len(self.fragment) > MAX_LINE_BYTES:
            self.fragment = b""
            self.skipping = True
            oversize += 1
        return TailResult(lines=lines, reset=reset, oversize=oversize)

    def _head_matches(self) -> bool:
        if not self.head:
            return True
        try:
            with open(self.path, "rb") as fh:
                return fh.read(len(self.head)) == self.head
        except OSError:
            return True


# --------------------------------------------------------------------------- #
# Parsing: one line -> one event dict.
# --------------------------------------------------------------------------- #
def _reject_constant(name: str):
    raise ValueError("non-finite number: " + name)


def parse_line(text: str) -> Tuple[Optional[dict], Optional[str]]:
    """(event, None) for a good line, (None, None) for a blank one, (None, reason) for a bad one."""
    text = text.strip()
    if not text:
        return None, None
    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None, "not valid JSON"
    if not isinstance(obj, dict):
        return None, "not a JSON object"
    kind = obj.get("type")
    if not isinstance(kind, str) or not kind:
        return None, "no event type"
    version = obj.get("v", 1)
    if isinstance(version, bool) or not isinstance(version, (int, float)) or version != 1:
        return None, "unsupported version"
    return obj, None


class ReadResult:
    """New events from the log, plus what the consumer must know about the file."""

    __slots__ = ("events", "reset", "missing", "malformed")

    def __init__(self, events: List[dict], reset: bool, missing: bool, malformed: int) -> None:
        self.events = events
        self.reset = reset          # throw away the state built so far; `events` start a new run
        self.missing = missing      # no file: the state is "waiting"
        self.malformed = malformed  # bad complete lines skipped since the last reset


class LogReader:
    """Tailer + parser + run identity (the `uid` that every event carries)."""

    def __init__(self, path) -> None:
        self.tailer = Tailer(path)
        self.uid: Optional[str] = None
        self.malformed = 0

    @property
    def path(self) -> Path:
        return self.tailer.path

    def read(self) -> ReadResult:
        tail = self.tailer.poll()
        reset = tail.reset
        if reset:
            self.uid = None
            self.malformed = 0
        events: List[dict] = []
        self.malformed += tail.oversize
        for text in tail.lines:
            event, why = parse_line(text)
            if event is None:
                if why:
                    self.malformed += 1
                continue
            uid = event.get("uid")
            if isinstance(uid, str) and uid:
                if self.uid is None:
                    self.uid = uid
                elif uid != self.uid:
                    # A different run is in this file now: start over with it.
                    reset = True
                    events = []
                    self.malformed = 0
                    self.uid = uid
            events.append(event)
        return ReadResult(events, reset, tail.missing, self.malformed)


# --------------------------------------------------------------------------- #
# Formatting helpers. Every number the page shows is made here.
# --------------------------------------------------------------------------- #
DOT = " · "            # " . " between the parts of a fact line
TIMES = "×"


def to_int(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value == int(value):
        return int(value)
    return None


def to_float(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def str_or_none(value) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def clip(text, limit: int) -> str:
    s = "" if text is None else str(text)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: max(0, limit - 3)].rstrip() + "..."


def fmt_pct(value) -> Optional[str]:
    """The backend's percentage format (gate.report_text): f"{v:.1%}"."""
    f = to_float(value)
    return None if f is None else f"{f:.1%}"


def fmt_check_value(value) -> str:
    """A gate check's value or limit, in the backend's rule: floats are percentages, the rest as written."""
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.1%}" if math.isfinite(value) else "n/a"
    return clip(value, 24)


def noun(n: int, word: str) -> str:
    if n == 1:
        return word
    if len(word) > 1 and word.endswith("y") and word[-2] not in "aeiou":
        return word[:-1] + "ies"      # entry -> entries
    return word + "s"


def plural(n: int, word: str) -> str:
    return f"{n} {noun(n, word)}"


def fmt_duration(seconds) -> str:
    s = to_float(seconds)
    if s is None or s < 0:
        return ""
    if s < 60:
        return f"{s:.1f} s"
    minutes, sec = divmod(int(round(s)), 60)
    if minutes < 60:
        return f"{minutes} min {sec} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def parse_time(text) -> Optional[datetime]:
    if not isinstance(text, str) or not text:
        return None
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_utc(text) -> str:
    dt = parse_time(text)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "an unknown time"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# The page's picture: 15 nodes, their edges, the rail, the tiles.
# --------------------------------------------------------------------------- #
NODE_IDS = (
    "rt.attack", "rt.record", "rt.check", "rt.rate", "rt.fixes",
    "sl.fingerprint", "sl.gate", "sl.review", "sl.log", "fix.ticket",
    "ll.risks", "ll.controls", "ll.evidence", "ll.trace", "ll.summary",
)
# Orthogonal edges of the diagram; "a>b" is lit when b starts and a has started.
EDGES = (
    "rt.attack>rt.record", "rt.record>rt.check", "rt.check>rt.rate", "rt.rate>rt.fixes",
    "sl.fingerprint>sl.gate", "sl.gate>sl.review", "sl.review>sl.log", "sl.log>fix.ticket",
    "ll.risks>ll.controls", "ll.controls>ll.evidence", "ll.evidence>ll.trace", "ll.trace>ll.summary",
    "rt.rate>sl.fingerprint",   # "every change"
    "fix.ticket>rt.attack",     # "test again"
    "rt.attack>ll.risks",       # "same test data" (the left trunk)
    "ll.summary>fix.ticket",    # "open risk" (lit at the end of the run)
)
# The nine nodes that start again for every candidate; the other six keep their state.
PASS_NODES = ("rt.attack", "rt.record", "rt.check", "rt.rate",
              "sl.fingerprint", "sl.gate", "sl.review", "sl.log", "fix.ticket")
RUN_NODES = ("rt.attack", "rt.record", "rt.check", "rt.rate")
LATE_EDGES = ("ll.summary>fix.ticket",)
STAGE_NODES = {
    "runs": RUN_NODES,
    "gate": ("sl.gate",),
    "decide": ("sl.review", "sl.log"),
    "assure": ("ll.risks", "ll.controls", "ll.evidence", "rt.fixes"),
    "trace": ("ll.trace",),
    "summary": ("ll.summary",),
    "chain": ("sl.log",),
}
WAVE = {1: "rt.record", 2: "rt.check", 3: "rt.rate"}
MODES = ("SCRIPTED", "LIVE", "SAVED")
STATUS_OF_MODE = {"SCRIPTED": "SAMPLE", "LIVE": "REAL", "SAVED": "REAL"}
KNOWN_EVENTS = (
    "pipeline_start", "meta", "stage_start", "stage_done", "run_start", "run_done", "runs_done", "fingerprint",
    "gate", "audit", "risks", "tickets", "evidence", "artifact", "trace", "summary", "chain", "pipeline_done",
)
PASS_EDGES = tuple(k for k in EDGES
                   if all(end in PASS_NODES for end in k.split(">")))
EDGES_INTO: Dict[str, List[str]] = {nid: [k for k in EDGES if k.split(">")[1] == nid] for nid in NODE_IDS}
RE_CAND = re.compile(r"^[A-Za-z0-9_.-]{1,24}$")


def _blank_node() -> dict:
    return {"state": "idle", "fact": "", "fact2": "", "status": "", "tag": ""}


def _cand_of(ev: dict) -> Optional[str]:
    c = ev.get("cand")
    if isinstance(c, str) and RE_CAND.match(c):
        return c
    stage = ev.get("stage")
    if isinstance(stage, str) and ":" in stage:
        head = stage.partition(":")[0]
        if RE_CAND.match(head):
            return head
    return None


def _cand_no(cand: Optional[str]) -> Optional[int]:
    m = re.search(r"(\d+)$", cand or "")
    return int(m.group(1)) if m else None


def _stage_kind(stage) -> str:
    if not isinstance(stage, str):
        return ""
    return stage.partition(":")[2] if ":" in stage else stage


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _dicts(value) -> List[dict]:
    return [x for x in _list(value) if isinstance(x, dict)]


class LiveModel:
    """The state of the screen, built one progress event at a time.

    apply(event) returns True when the event was a "beat": a node changed state,
    or something worth a pause (a refused review) was shown. The pacer in a later
    step holds the schedule after a beat so the audience can read it.
    """

    def __init__(self) -> None:
        self.reset()

    # -- state ------------------------------------------------------------ #
    def reset(self) -> None:
        self.uid: Optional[str] = None
        self.mode: Optional[str] = None
        self.status = ""
        self.started_t: Optional[str] = None
        self.model: Optional[str] = None
        self.digest: Optional[str] = None
        self.run_state = "waiting"
        self.nodes = {nid: _blank_node() for nid in NODE_IDS}
        self.edges = {key: "idle" for key in EDGES}
        self.rail: List[dict] = []
        self.expected: Dict[str, Optional[int]] = {}
        self.defences: Dict[str, List[str]] = {}
        self.cand: Optional[str] = None
        self.prev_cand: Optional[str] = None
        self.pass_no = 0
        self.active: Optional[str] = None
        self.prev_active: Optional[str] = None
        self.tail = deque(maxlen=TAIL_LINES)
        self.done: Optional[dict] = None
        self.events_seen = 0
        self.unknown = 0
        self.problems = 0
        self.open_risks: Optional[int] = None
        self._log_was_done = False
        self._deferred: List[tuple] = []
        self._beat = False
        self._tiles_reset(None, 0)
        self._acc_reset()

    def _acc_reset(self) -> None:
        """Counters for one pass (they restart for every candidate)."""
        self.runs_seen = 0
        self.calls_total = 0
        self.calls_known = False
        self.blocked_total = 0
        self.class_counts: Dict[str, int] = {}
        self.stats: Optional[dict] = None
        self.summary: Optional[dict] = None

    def _tiles_reset(self, cand: Optional[str], n: int) -> None:
        self.tile_cand = cand
        self.tile_n = n or 0
        self.tiles: Dict[int, List[str]] = {}

    # -- applying events -------------------------------------------------- #
    def apply(self, ev: dict) -> bool:
        """Fold one progress event into the state; True if it was a beat."""
        self._beat = False
        self.events_seen += 1
        if self.run_state == "waiting":
            self.run_state = "running"
        uid = ev.get("uid")
        if self.uid is None and isinstance(uid, str) and uid:
            self.uid = uid
        try:
            self._run_deferred()
        except Exception:  # noqa: BLE001 - a bad line must never stop the bridge
            self.problems += 1
        kind = ev.get("type")
        handler = getattr(self, "_on_" + kind, None) if kind in KNOWN_EVENTS else None
        if handler is None:
            self.unknown += 1
        else:
            try:
                handler(ev)
            except Exception:  # noqa: BLE001
                self.problems += 1
        try:
            text = self._tail_text(ev)
        except Exception:  # noqa: BLE001
            text = None
        if text:
            self.tail.append(text)
        return self._beat

    def apply_all(self, events) -> int:
        """Apply many events at once; returns how many were beats."""
        return sum(1 for e in events if self.apply(e))

    # -- node moves ------------------------------------------------------- #
    def _facts(self, node: dict, fact=None, fact2=None, tag=None) -> None:
        if fact is not None:
            node["fact"] = clip(fact, 90)
        if fact2 is not None:
            node["fact2"] = clip(fact2, 200)
        if tag is not None:
            node["tag"] = clip(tag, 40)

    def _light_edges(self, nid: str) -> None:
        for key in EDGES_INTO.get(nid, ()):
            if self.nodes[key.split(">")[0]]["state"] != "idle":
                self.edges[key] = "done"

    def _start(self, nid: str, fact=None, fact2=None, tag=None) -> None:
        node = self.nodes[nid]
        if node["state"] != "running":
            node["state"] = "running"
            self._beat = True
            if self.active != nid:
                self.prev_active, self.active = self.active, nid
            self._light_edges(nid)
        self._facts(node, fact, fact2, tag)

    def _finish(self, nid: str, state: str = "done", fact=None, fact2=None, tag=None) -> None:
        node = self.nodes[nid]
        if node["state"] != state:
            node["state"] = state
            self._beat = True
            if state == "done":
                self._light_edges(nid)
        self._facts(node, fact, fact2, tag)

    def _defer(self, *op) -> None:
        self._deferred.append(tuple(op))

    def _run_deferred(self) -> None:
        ops, self._deferred = self._deferred, []
        for op in ops:
            if op[0] == "finish":
                if self.nodes[op[1]]["state"] == "running":
                    self._finish(op[1])
            elif op[0] == "start":
                self._start(op[1], fact=op[2] if len(op) > 2 else None)

    def _show(self, nid: str, fact=None, fact2=None, tag=None) -> None:
        """Put facts on a node: in place if it is running, else run it until the next event."""
        if self.nodes[nid]["state"] == "running":
            self._facts(self.nodes[nid], fact, fact2, tag)
        else:
            self._start(nid, fact, fact2, tag)
            self._defer("finish", nid)

    # -- passes, rail, tiles ---------------------------------------------- #
    def _rail_entry(self, cid: str) -> dict:
        for entry in self.rail:
            if entry["id"] == cid:
                return entry
        entry = {"id": cid, "text": cid, "verdict": "", "state": "idle", "status": self.status}
        self.rail.append(entry)
        return entry

    @staticmethod
    def _rail_text(cid: str, defences: List[str]) -> str:
        return f"{cid}{DOT}no defences" if not defences else f"{cid}{DOT}defences {', '.join(defences)}"

    def _enter_pass(self, cand: Optional[str]) -> bool:
        """A new candidate begins: reset the short loop. True if a new pass started."""
        if not cand or cand == self.cand:
            return False
        old_no, new_no = _cand_no(self.cand), _cand_no(cand)
        if self.cand is not None and old_no is not None and new_no is not None and new_no < old_no:
            return False
        if self.cand is not None:
            old = self._rail_entry(self.cand)
            if old["state"] in ("idle", "running"):
                old["state"] = "done"
        self.prev_cand, self.cand = self.cand, cand
        self.pass_no += 1
        self._rail_entry(cand)["state"] = "running"
        self._log_was_done = self.nodes["sl.log"]["state"] == "done"
        for nid in PASS_NODES:
            node = self.nodes[nid]
            if node["state"] != "idle":
                self._beat = True
            node.update(state="idle", fact="", fact2="", tag="")
        for key in PASS_EDGES:
            self.edges[key] = "idle"
        if self.active in PASS_NODES:
            self.prev_active, self.active = self.active, None
        self._tiles_reset(cand, self.expected.get(cand) or 0)
        self._acc_reset()
        return True

    def _planned_fact(self) -> str:
        n = self.expected.get(self.cand) if self.cand else None
        return f"{n} runs planned" if n else ""

    def _begin_retest(self, cand: str) -> None:
        defs = self.defences.get(cand) or []
        prev = self.defences.get(self.prev_cand) or [] if self.prev_cand else []
        delta = [d for d in defs if d not in prev]
        fact = ("adds " + ", ".join(delta)) if delta else "re-test"
        fact2 = f"{cand} enforces {', '.join(defs)}" if defs else ""
        self._start("fix.ticket", fact=fact, fact2=fact2)
        if getattr(self, "_log_was_done", False):
            self.edges["sl.log>fix.ticket"] = "done"       # the last pass's log was written; now the fix
        self._defer("finish", "fix.ticket")
        self._defer("start", "rt.attack", self._planned_fact())

    def _tile_state(self, ev: dict) -> str:
        err = ev.get("error")
        errored = err not in (None, "", False)
        kind = ev.get("kind")
        if kind == "benign":
            if errored:
                return "error"
            ok = ev.get("benign_success")
            return "ok" if ok is True else ("missed" if ok is False else "unscored")
        if ev.get("violated") is True:
            return "violated"
        if errored:
            return "error"
        return "held" if kind == "attack" else "unscored"

    def _tile_set(self, ev: dict, state: str) -> None:
        i = to_int(ev.get("i"))
        n = to_int(ev.get("n"))
        if n and n > 0 and not self.tile_n:
            self.tile_n = n
        elif n and n > 0 and n != self.tile_n:
            self.tile_n = n
        if i is None or i < 1:
            i = len(self.tiles) + 1
        test_id = str_or_none(ev.get("test_id")) or f"run {i}"
        rep = to_int(ev.get("rep"))
        key = f"{test_id}:{rep}" if rep is not None else test_id
        if len(self.tiles) < MAX_TILES or i in self.tiles:
            self.tiles[i] = [clip(key, 40), state]

    def tiles_obj(self) -> dict:
        n = self.tile_n or (max(self.tiles) if self.tiles else 0)
        if 0 < n <= MAX_TILES:
            lst = [list(self.tiles.get(i) or [f"#{i}", "idle"]) for i in range(1, n + 1)]
        else:
            lst = [list(self.tiles[i]) for i in sorted(self.tiles)][:MAX_TILES]
        return {"cand": self.tile_cand, "n": n, "list": lst}

    # -- run facts -------------------------------------------------------- #
    def _fact_record(self) -> str:
        if not self.calls_known:
            return ""
        return f"{plural(self.calls_total, 'tool call')}{DOT}{self.blocked_total} blocked"

    def _fact_check(self, final: bool) -> Tuple[str, str]:
        items = sorted(self.class_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if not items:
            return ("no rule broken" if final else "no rule broken yet"), ""
        shown = [f"{name} {count}" for name, count in items[:2]]
        if len(items) > 2:
            shown.append(f"+{len(items) - 2} more")
        full = ", ".join(f"{name} {count}" for name, count in items) if len(items) > 2 else ""
        return DOT.join(shown), full

    def _fact_rate(self, final: bool) -> Tuple[str, str]:
        s = (self.summary if final and self.summary else self.stats) or {}
        runs = to_int(s.get("attack_runs"))
        viol = to_int(s.get("attack_violations"))
        asr = to_float(s.get("asr"))
        lo, hi = to_float(s.get("asr_lo")), to_float(s.get("asr_hi"))
        if not runs or asr is None:
            fact = "no attack runs" if final else "no attack runs yet"
        else:
            head = f"{viol} of {runs}{DOT}{fmt_pct(asr)}" if viol is not None else f"{fmt_pct(asr)} of {runs} runs"
            fact = head + (f" ({fmt_pct(lo)} to {fmt_pct(hi)})" if lo is not None and hi is not None else "")
        parts = []
        bn, br = to_int(s.get("benign_n")), to_float(s.get("benign_rate"))
        if bn and br is not None:
            parts.append(f"normal requests handled {fmt_pct(br)} of {bn}")
        else:
            parts.append("no normal requests were run" if final else "no normal requests run yet")
        errors = to_int(s.get("errors"))
        if errors:
            parts.append(plural(errors, "error"))
        return fact, "; ".join(parts)

    def _refresh_run_facts(self) -> None:
        if self.nodes["rt.record"]["state"] == "running":
            self._facts(self.nodes["rt.record"], self._fact_record())
        if self.nodes["rt.check"]["state"] == "running":
            fact, fact2 = self._fact_check(False)
            self._facts(self.nodes["rt.check"], fact, fact2)
        if self.nodes["rt.rate"]["state"] == "running":
            fact, fact2 = self._fact_rate(False)
            self._facts(self.nodes["rt.rate"], fact, fact2)

    def _finish_runs(self, state: str = "done") -> None:
        s = self.summary or self.stats or {}
        for nid in RUN_NODES:
            if self.nodes[nid]["state"] not in ("idle", "running"):
                continue
            if nid == "rt.attack":
                runs = to_int(s.get("runs")) or self.runs_seen
                fact = f"{runs} runs" if runs else "runs done"
                errors = to_int(s.get("errors"))
                fact2 = f"{plural(errors, 'run')} errored" if errors else ""
            elif nid == "rt.record":
                fact, fact2 = self._fact_record(), ""
            elif nid == "rt.check":
                fact, fact2 = self._fact_check(True)
            else:
                fact, fact2 = self._fact_rate(True)
            self._finish(nid, state, fact, fact2)

    # -- event handlers --------------------------------------------------- #
    def _sync_status(self) -> None:
        for node in self.nodes.values():
            node["status"] = self.status
        for entry in self.rail:
            entry["status"] = self.status

    def _on_pipeline_start(self, ev: dict) -> None:
        seen, uid = self.events_seen, ev.get("uid")
        self.reset()
        self.events_seen = seen
        self.run_state = "running"
        self.uid = uid if isinstance(uid, str) and uid else None
        mode = str(ev.get("mode") or "").upper()
        if mode == "DEMO":
            mode = "SCRIPTED"
        self.mode = mode if mode in MODES else None
        self.status = STATUS_OF_MODE.get(self.mode, "")
        self.started_t = str_or_none(ev.get("t"))
        self._sync_status()

    def _on_meta(self, ev: dict) -> None:
        self.model = str_or_none(ev.get("model"))
        self.digest = str_or_none(ev.get("digest12"))
        for c in _dicts(ev.get("candidates")):
            cid = c.get("id")
            if not isinstance(cid, str) or not RE_CAND.match(cid):
                continue
            defs = [str(d) for d in _list(c.get("defences")) if d is not None]
            self.defences[cid] = defs
            self.expected[cid] = to_int(c.get("expected_runs"))
            entry = self._rail_entry(cid)
            entry["text"] = self._rail_text(cid, defs)
        if self.cand is None and self.rail:
            first = self.rail[0]["id"]
            self._tiles_reset(first, self.expected.get(first) or 0)

    def _on_stage_start(self, ev: dict) -> None:
        kind = _stage_kind(ev.get("stage"))
        cand = _cand_of(ev)
        new_pass = self._enter_pass(cand)
        if kind == "runs":
            if new_pass and self.pass_no >= 2:
                self._begin_retest(cand)
            else:
                self._start("rt.attack", fact=self._planned_fact())
        elif kind == "gate":
            self._start("sl.gate")
        elif kind == "decide":
            self._start("sl.review")
        elif kind == "assure":
            self._start("ll.risks")
        elif kind == "trace":
            self._start("ll.trace")
        elif kind == "summary":
            self._start("ll.summary")
        elif kind == "chain":
            self._start("sl.log", fact="checking the chain")

    def _on_stage_done(self, ev: dict) -> None:
        kind = _stage_kind(ev.get("stage"))
        status = str(ev.get("status") or "ok").lower()
        nodes = STAGE_NODES.get(kind, ())
        if status == "error":
            for nid in nodes:
                if self.nodes[nid]["state"] == "running":
                    self._finish(nid, "error")
        elif status == "skipped":
            for nid in nodes:
                if self.nodes[nid]["state"] in ("idle", "running"):
                    self._finish(nid, "skipped")
        elif kind == "runs":
            self._finish_runs()
        else:
            for nid in nodes:
                if self.nodes[nid]["state"] == "running":
                    self._finish(nid)

    def _ensure_attack(self) -> None:
        if self.nodes["rt.attack"]["state"] == "idle":
            self._start("rt.attack")

    def _on_run_start(self, ev: dict) -> None:
        self._enter_pass(_cand_of(ev))
        self._ensure_attack()
        self._tile_set(ev, "running")
        self._facts(self.nodes["rt.attack"], self._attack_fact(ev))

    @staticmethod
    def _attack_fact(ev: dict) -> str:
        i, n = to_int(ev.get("i")), to_int(ev.get("n"))
        head = f"run {i} of {n}" if i is not None and n else (f"run {i}" if i is not None else "run")
        test_id, rep = str_or_none(ev.get("test_id")), to_int(ev.get("rep"))
        tail = ""
        if test_id:
            tail = f"{DOT}{test_id}" + (f" rep {rep}" if rep is not None else "")
        return head + tail

    def _on_run_done(self, ev: dict) -> None:
        self._enter_pass(_cand_of(ev))
        self._ensure_attack()
        self.runs_seen += 1
        calls, blocked = to_int(ev.get("calls")), to_int(ev.get("blocked"))
        if calls is not None:
            self.calls_known = True
            self.calls_total += calls
        if blocked is not None:
            self.blocked_total += blocked
        if ev.get("violated") is True:
            for name in _list(ev.get("classes")):
                if isinstance(name, str) and name:
                    key = clip(name, 40)
                    self.class_counts[key] = self.class_counts.get(key, 0) + 1
        running = ev.get("running")
        if isinstance(running, dict):
            self.stats = running
        self._tile_set(ev, self._tile_state(ev))
        self._facts(self.nodes["rt.attack"], self._attack_fact(ev))
        wave = WAVE.get(self.runs_seen)
        if wave and self.nodes[wave]["state"] == "idle":
            self._start(wave)
        self._refresh_run_facts()

    def _on_runs_done(self, ev: dict) -> None:
        self._enter_pass(_cand_of(ev))
        summary = ev.get("summary")
        self.summary = summary if isinstance(summary, dict) else None
        self._finish_runs()

    def _on_fingerprint(self, ev: dict) -> None:
        self._enter_pass(_cand_of(ev))
        ident = str_or_none(ev.get("identity12")) or "unknown"
        changed = ev.get("changed_vs_prev")
        word = "changed" if changed is True else ("unchanged" if changed is False else "first version")
        self._show("sl.fingerprint", f"identity {ident}{DOT}{word}", "")

    def _on_gate(self, ev: dict) -> None:
        cand = _cand_of(ev)
        self._enter_pass(cand)
        verdict = (str_or_none(ev.get("verdict")) or "").upper()
        checks = _dicts(ev.get("checks"))
        failed = [c for c in checks if c.get("ok") is False]
        total = len(checks)
        if verdict == "PASS":
            fact = f"PASS{DOT}all {plural(total, 'check')} passed" if total else "PASS"
        elif verdict == "FAIL":
            fact = f"FAIL{DOT}{len(failed)} of {plural(total, 'check')} failed" if total else "FAIL"
        else:
            fact = verdict
        fact2 = ""
        if failed:
            c = failed[0]
            fact2 = (f"{clip(c.get('name'), 40)} {fmt_check_value(c.get('value'))} "
                     f"(limit {fmt_check_value(c.get('limit'))})")
            if len(failed) > 1:
                fact2 += f" +{len(failed) - 1} more"
        self._show("sl.gate", fact, fact2, verdict)
        if cand and verdict:
            self._rail_entry(cand)["verdict"] = verdict

    def _on_audit(self, ev: dict) -> None:
        self._enter_pass(_cand_of(ev))
        event = str_or_none(ev.get("event")) or ""
        entry, h = to_int(ev.get("entry")), str_or_none(ev.get("hash12"))
        parts = ([f"entry {entry}"] if entry is not None else []) + ([event] if event else []) + ([h] if h else [])
        log = self.nodes["sl.log"]
        if log["state"] != "running":
            self._start("sl.log", DOT.join(parts), "")
        else:
            self._facts(log, DOT.join(parts), "")
        reviewer = str_or_none(ev.get("reviewer"))
        reason = str_or_none(ev.get("reason")) or ""
        if event == "decision":
            decision = str_or_none(ev.get("decision")) or ""
            fact = f"{decision} by {reviewer}" if reviewer and decision else (decision or "decided")
            self._show("sl.review", fact, reason, "scripted by the CLI" if ev.get("scripted") is True else "")
        elif event == "decision_refused":
            self._show("sl.review", f"refused: {reviewer}" if reviewer else "refused", reason)
            self._beat = True

    def _on_risks(self, ev: dict) -> None:
        self._enter_pass(_cand_of(ev))
        rows = _dicts(ev.get("rows"))
        open_n = sum(1 for r in rows if str(r.get("status") or "").strip().lower() == "open")
        self.open_risks = open_n
        fact = f"{plural(len(rows), 'risk')}{DOT}{open_n} open"
        if self.nodes["ll.risks"]["state"] != "running":
            self._start("ll.risks")
        self._finish("ll.risks", "done", fact, "")
        self._controls(ev)

    def _controls(self, ev: dict) -> None:
        if not isinstance(ev.get("controls_used"), list):
            return
        names = [str(c) for c in ev["controls_used"] if c is not None]
        self._show("ll.controls", "controls used: " + ", ".join(names) if names else "no controls used", "")

    def _on_tickets(self, ev: dict) -> None:
        self._enter_pass(_cand_of(ev))
        rows = _dicts(ev.get("rows"))
        fact = plural(len(rows), "fix ticket")
        if self.open_risks is not None:
            fact += f"{DOT}{plural(self.open_risks, 'open risk')}"
        self._show("rt.fixes", fact, "")

    def _on_evidence(self, ev: dict) -> None:
        self._enter_pass(_cand_of(ev))
        self._show("ll.evidence", plural(len(_dicts(ev.get("rows"))), "evidence row"), "")

    def _on_artifact(self, ev: dict) -> None:
        self._enter_pass(_cand_of(ev))

    def _on_trace(self, ev: dict) -> None:
        finding, steps = str_or_none(ev.get("finding")), to_int(ev.get("steps_n"))
        if finding:
            fact = f"{finding}{DOT}{plural(steps, 'step')}" if steps is not None else finding
        else:
            fact = "nothing to trace"
        self._show("ll.trace", fact, "")

    def _on_summary(self, ev: dict) -> None:
        bullets = [str(b) for b in _list(ev.get("bullets")) if b is not None]
        self._show("ll.summary", plural(len(bullets), "summary line"), bullets[0] if bullets else "")

    def _on_chain(self, ev: dict) -> None:
        ok = ev.get("ok") is True
        entries, head = to_int(ev.get("entries")), str_or_none(ev.get("head12"))
        parts = ([plural(entries, "entry")] if entries is not None else []) \
            + [f"chain {'intact' if ok else 'broken'}"] + ([f"head {head}"] if head else [])
        fact2 = "" if ok else (str_or_none(ev.get("detail")) or "")
        tamper = ev.get("tamper")
        if isinstance(tamper, dict):
            if tamper.get("ok") is False:
                kind, seq = str_or_none(tamper.get("kind")), to_int(tamper.get("bad_seq"))
                where = f"{kind or 'changed'} at entry {seq}" if seq is not None else (kind or "changed")
                fact2 = f"tamper check on a copy: caught ({where})"
            elif tamper.get("ok") is True:
                fact2 = "tamper check on a copy: NOT caught"
        self._show("sl.log", DOT.join(parts), fact2, "intact" if ok else "broken")

    def _on_pipeline_done(self, ev: dict) -> None:
        status = str(ev.get("status") or "").lower()
        node_state = "done" if status == "ok" else "error"
        for nid in NODE_IDS:
            if self.nodes[nid]["state"] == "running":
                self._finish(nid, node_state)
        for cand_row in _dicts((ev.get("headline") or {}).get("cands") if isinstance(ev.get("headline"), dict) else None):
            cid, verdict = cand_row.get("id"), str_or_none(cand_row.get("verdict"))
            if isinstance(cid, str) and RE_CAND.match(cid) and verdict:
                self._rail_entry(cid)["verdict"] = verdict.upper()
        for entry in self.rail:
            if entry["state"] == "running":
                entry["state"] = "done" if status == "ok" else "error"
            elif entry["state"] == "idle" and status == "ok" and entry["verdict"]:
                entry["state"] = "done"
        if status == "ok":
            for key in LATE_EDGES:
                a, b = key.split(">")
                if self.nodes[a]["state"] == "done" and self.nodes[b]["state"] == "done":
                    self.edges[key] = "done"
        self.run_state = "done" if status == "ok" else ("stopped" if status == "interrupted" else "error")
        self.prev_active = self.active or self.prev_active
        self.active = None
        error = str_or_none(ev.get("error"))
        self.done = {"status": status, "exit_code": to_int(ev.get("exit_code")),
                     "dur": fmt_duration(ev.get("dur_s")), "error": clip(error, 200) if error else None}

    # -- the events_tail line --------------------------------------------- #
    def _tail_text(self, ev: dict) -> Optional[str]:
        kind = ev.get("type")
        cand = _cand_of(ev) or ""
        lead = f"{cand} " if cand else ""
        if kind == "pipeline_start":
            return f"Run started ({self.mode or 'unknown mode'})"
        if kind == "stage_start":
            title = clip(ev.get("title"), 90)
            return f"Started: {title}" if title else None
        if kind == "stage_done":
            status = str(ev.get("status") or "ok").lower()
            return None if status == "ok" else f"Stage {clip(ev.get('stage'), 30)} {status}"
        if kind == "run_done":
            err = ev.get("error")
            return f"Run {to_int(ev.get('i'))} errored: {clip(err, 80)}" if err not in (None, "", False) else None
        if kind == "gate":
            return f"{lead}gate: {str_or_none(ev.get('verdict')) or 'no verdict'}"
        if kind == "audit":
            bits = [str_or_none(ev.get("event")) or "event", str_or_none(ev.get("decision")) or ""]
            entry = to_int(ev.get("entry"))
            return f"Audit entry {entry}: " + " ".join(b for b in bits if b) if entry is not None else \
                "Audit: " + " ".join(b for b in bits if b)
        if kind == "fingerprint":
            return f"{lead}fingerprint {str_or_none(ev.get('identity12')) or 'unknown'}"
        if kind == "artifact":
            name = str_or_none(ev.get("name"))
            return f"Wrote {clip(name, 60)}" if name else None
        if kind in ("risks", "tickets", "evidence"):
            return f"{lead}{kind}: {plural(len(_dicts(ev.get('rows'))), 'row')}"
        if kind == "trace":
            return "Trace written"
        if kind == "summary":
            return "Summary written"
        if kind == "chain":
            tamper = ev.get("tamper")
            if isinstance(tamper, dict):
                return "Tamper check on a copy: " + ("caught" if tamper.get("ok") is False else "NOT caught")
            return "Audit chain checked: " + ("intact" if ev.get("ok") is True else "broken")
        if kind == "pipeline_done":
            return f"Run finished: {str_or_none(ev.get('status')) or 'unknown'}"
        return None


# --------------------------------------------------------------------------- #
# What the page is given: a ui-live/1 snapshot built from the model and a view.
# --------------------------------------------------------------------------- #
class View:
    """What the model cannot know: where the data comes from, and the clock."""

    def __init__(self, kind: str = "follow", log: Optional[str] = None, speed: Optional[float] = None,
                 child_alive: Optional[bool] = None, last_event_age: Optional[float] = None, malformed: int = 0,
                 export: Optional[dict] = None, gov: Optional[str] = None, batch: Optional[dict] = None,
                 notes=(), state: Optional[str] = None, stalled: bool = False) -> None:
        self.kind = kind                    # follow | replay | run | batch
        self.log = log                      # the log's display path (never absolute)
        self.speed = speed                  # replay speed
        self.child_alive = child_alive      # a child run is alive (None: no child)
        self.last_event_age = last_event_age  # seconds since the last event arrived
        self.malformed = malformed
        self.export = export                # {"status", "text"} once the export has been tried
        self.gov = gov                      # the run folder, for the export command
        self.batch = batch
        self.notes = list(notes)
        self.state = state                  # force a state (a refused or crashed run)
        self.stalled = stalled              # the log stopped growing and nothing is running it


def is_paced(model: LiveModel, view: View) -> bool:
    return model.mode in ("SCRIPTED", "SAVED") or (model.mode == "LIVE" and view.kind == "replay")


def make_label(model: LiveModel, view: View) -> dict:
    if not model.events_seen:
        return {"word": "", "text": "Waiting for a run", "status": ""}
    mode = model.mode
    status = STATUS_OF_MODE.get(mode, "")
    if mode == "SCRIPTED":
        return {"word": "SCRIPTED", "text": "SCRIPTED · paced for display", "status": status}
    if mode == "SAVED":
        return {"word": "SAVED", "text": "SAVED · gated from a saved run folder · paced for display",
                "status": status}
    if mode == "LIVE":
        growing = view.kind != "replay" and (
            view.child_alive is True
            or (view.last_event_age is not None and view.last_event_age < LIVE_WINDOW_S))
        if growing:
            who = f"{model.model} · " if model.model else ""
            return {"word": "LIVE", "text": f"LIVE · {who}running now", "status": status}
        text = f"REPLAY · real-model run of {fmt_utc(model.started_t)}"
        if view.kind == "replay" and view.speed:
            text += f" · {view.speed:g}{TIMES} speed · long gaps shortened"
        else:
            text += " · recorded log"
        return {"word": "REPLAY", "text": text, "status": status}
    return {"word": "", "text": "Run of unknown mode", "status": ""}


def make_done(model: LiveModel, view: View) -> Optional[dict]:
    d = model.done
    if d is None:
        return None
    cmd = "python ui/export.py --quiet" + (f" --gov {view.gov}" if view.gov else "")
    export = dict(view.export) if view.export else {"status": "not_run", "text": cmd}
    dur = d["dur"]
    if d["status"] == "ok":
        lead = f"Done in {dur}" if dur else "Done"
        if is_paced(model, view):
            lead += " of run time, shown paced for display"
        if export.get("status") == "not_run":
            rest = f"To update the evidence screens run: {export.get('text') or cmd}"
        else:
            rest = str(export.get("text") or "")
        text = f"{lead}. {rest}".rstrip()
    else:
        lead = f"Stopped after {dur}" if dur else "Stopped"
        text = f"{lead}: {d['error']}" if d.get("error") else lead
    return {"exit_code": d["exit_code"], "dur": dur, "text": text, "export": export}


def build_live(model: LiveModel, view: View) -> dict:
    """The ui-live/1 object (rev and written_at are placeholders; the writer fills them)."""
    state = view.state or ("waiting" if not model.events_seen else model.run_state)
    if state == "running" and view.stalled:
        state = "stopped"
    warnings: List[str] = []
    if view.malformed:
        warnings.append(f"{view.malformed} malformed log {noun(view.malformed, 'line')} skipped")
    if model.problems:
        warnings.append(f"{model.problems} {noun(model.problems, 'event')} could not be read")
    warnings.extend(str(n) for n in view.notes)
    return {
        "schema": SCHEMA_LIVE,
        "rev": 0,
        "written_at": "",
        "source": {"kind": view.kind, "log": view.log, "uid": model.uid,
                   "speed": view.speed if view.kind == "replay" else None, "paced": is_paced(model, view)},
        "label": make_label(model, view),
        "mode": model.mode,
        "model": model.model,
        "digest": model.digest,
        "state": state,
        "active": model.active if state == "running" else None,
        "prev_active": model.prev_active,
        "nodes": {nid: dict(model.nodes[nid]) for nid in NODE_IDS},
        "edges": dict(model.edges),
        "rail": [dict(r) for r in model.rail],
        "tiles": model.tiles_obj(),
        "events_tail": list(model.tail),
        "warnings": warnings,
        "done": make_done(model, view),
        "batch": view.batch,
    }


# --------------------------------------------------------------------------- #
# Writing live.js: scrub, escape, replace atomically.
# --------------------------------------------------------------------------- #
RE_ABS_UNIX = re.compile(r"(?:/Users|/private|/home|/var|/tmp)/[^\s'\"]*")
RE_ABS_WIN = re.compile(r"[A-Za-z]:[\\/][^\s'\"]*")
RE_ABS_UNC = re.compile(r"\\\\[^\s\\'\"]+\\[^\s'\"]*")


class Scrubber:
    """Removes absolute paths and the local user name from text.

    These are the rules of ui/export.py `scrub` (copied, not imported), plus
    Windows drive and network paths.
    """

    def __init__(self, roots=()) -> None:
        found = {str(REPO_ROOT), str(UI_DIR)}
        try:
            found.add(str(Path.home()))
        except (RuntimeError, OSError):
            pass
        found.update(str(r) for r in roots)
        self.roots = sorted((p for p in found if p and len(p) > 1), key=len, reverse=True)
        try:
            user = getpass.getuser()
        except Exception:  # noqa: BLE001
            user = ""
        self.user = user if len(user) >= 4 else ""

    def __call__(self, text) -> str:
        s = str(text)
        for p in self.roots:
            s = s.replace(p + os.sep, "").replace(p, "<dir>")
        s = RE_ABS_UNIX.sub("<path>", s)
        s = RE_ABS_WIN.sub("<path>", s)
        s = RE_ABS_UNC.sub("<path>", s)
        if self.user:
            s = s.replace(self.user, "<user>")
        return s

    def walk(self, obj):
        if isinstance(obj, str):
            return self(obj)
        if isinstance(obj, dict):
            return {k: self.walk(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.walk(v) for v in obj]
        return obj


def render_js(obj, var: str = "LIVE") -> str:
    """`window.<var> = {...};` as pure ASCII, with < > & escaped so no tag can form."""
    body = json.dumps(obj, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    body = body.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f"window.{var} = {body};\n"


def write_atomic(path, text: str) -> None:
    """Write `text` to `path` through a temporary file in the same folder, then os.replace.

    The page may be reading the file while it is replaced; a reader sees the
    old or the new file, never half of one. On Windows os.replace can fail with
    PermissionError while the browser holds the file open; retry for a moment.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".live-", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(tmp, str(target))
                return
            except PermissionError:
                if not IS_WINDOWS or attempt == REPLACE_TRIES - 1:
                    raise
                time.sleep(REPLACE_WAIT_S)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clean_stale_temp(folder, older_than: float = 60.0) -> int:
    """Remove temporary files a crashed bridge left behind; returns how many."""
    removed = 0
    now = time.time()
    try:
        names = list(Path(folder).glob(".live-*.tmp"))
    except OSError:
        return 0
    for p in names:
        try:
            if now - p.stat().st_mtime > older_than:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


class LiveWriter:
    """Writes snapshots to live.js: only when the content changed, at most every 250 ms."""

    def __init__(self, path, throttle: float = THROTTLE_S, clock=time.monotonic, wall=utc_now_iso,
                 var: str = "LIVE", scrubber: Optional[Scrubber] = None) -> None:
        self.path = Path(path)
        self.throttle = throttle
        self.clock = clock
        self.wall = wall
        self.var = var
        self.scrub = scrubber or Scrubber()
        self.rev = 0
        self._last_body: Optional[str] = None
        self._last_write: Optional[float] = None

    @staticmethod
    def _key(body: dict) -> str:
        return json.dumps({k: v for k, v in body.items() if k not in ("rev", "written_at")},
                          sort_keys=True, separators=(",", ":"), default=str)

    def offer(self, body: dict, force: bool = False) -> bool:
        """Write `body` if it differs from the last write and the throttle allows; True if written."""
        key = self._key(body)
        if key == self._last_body:
            return False
        now = self.clock()
        if not force and self._last_write is not None and now - self._last_write < self.throttle:
            return False
        out = dict(body)
        out["rev"] = self.rev + 1
        out["written_at"] = self.wall()
        write_atomic(self.path, render_js(self.scrub.walk(out), self.var))
        self.rev += 1
        self._last_body = key
        self._last_write = now
        return True


if __name__ == "__main__":  # pragma: no cover - the command line arrives in a later step
    raise SystemExit("ui/bridge.py: command line not built yet")
