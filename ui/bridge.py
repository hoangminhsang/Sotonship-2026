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
  * "." in the table stands for the middle dot (U+00B7) that separates the parts of a fact.

ui-live/1 FIELDS (where the spec leaves a choice; keys are exactly the A3 list, plus "schema",
done.dur and done.text)
  nodes[id]  state idle|running|done|error|skipped; status REAL|SAMPLE (the run's status, "" while waiting);
             tag: sl.gate PASS|FAIL, sl.review "scripted by the CLI" when the event says so, sl.log intact|broken
  edges      16 keys "a>b", idle|done; "ll.summary>fix.ticket" is lit only when the run ends
  rail[]     id, text, verdict FAIL|PASS|"" (the gate's), state idle|running|done|error, status
  tiles      cand, n, list: n entries [key, state] for the candidate on show; key "<test_id>:<rep>" or "#<i>" before
             that run starts; state idle|running|violated|held|ok|missed|error|unscored
  label      word SCRIPTED|LIVE|REPLAY|SAVED ("" while waiting), text, status REAL|SAMPLE
  state      waiting|running|done|error|stopped; active = node that started last (null unless running)
  done       null until the run ends: exit_code, dur ("2.5 s"), text (the whole status line), export{status, text}
             export.status ok|failed|not_run|running|skipped; text is the command to run when it did not run
  batch      null, or {"models": [{slug, tag, state, tools, text}]} sorted by tag
  events_tail up to 6 short strings; warnings: strings

LABELS AND PACING
  LIVE only while a LIVE-mode log is still growing (the child is alive, or the last event is under 60 s old);
  a recorded LIVE log is REPLAY ("REPLAY . real-model run of <UTC> . N x speed . long gaps shortened");
  SCRIPTED shows "SCRIPTED . paced for display": about 40 ms per run_done, at least 1.4 s per node transition.
  A log that stops growing for 180 s with no child is shown as "stopped".

COMMANDS: see --help. Exit codes: 0 fine or stopped by you, 2 refused or bad arguments, 130 run stopped by
Ctrl-C, otherwise the run's own exit code. Not verified on Windows: CREATE_NEW_PROCESS_GROUP + CTRL_BREAK_EVENT.

PAGE-STARTED RUNS (--serve alone, or --serve --run KEY): the page may start the same fixed runs through a small
JSON API on 127.0.0.1. One run at a time. The page never sends a command: it names a "kind" from a fixed list
and a model tag from config/models.txt, and the bridge maps that to the fixed argument list (shell=False).
    GET  /api/session  -> {"token": "<32 hex>"}   (same-origin only: no CORS headers)
    GET  /api/status   -> {running, kind, model, started, last, models, kinds}
    POST /api/run      -> 202 {"ok": true} | 400 | 403 | 409   headers: Content-Type: application/json,
                          X-Redline-Token; body {"kind", "model", "parallel"}
    POST /api/stop     -> 202 {"ok": true} | 403 | 409          header:  X-Redline-Token
Refused: a wrong or missing token (403), a Host or Origin that is not this server (403), a body that is not
JSON or is over 4096 bytes (400), an unknown kind or a model not in config/models.txt (400), a second run (409).
The token is compared in constant time. OPTIONS and every other method are 405 with no CORS headers.
"""
from __future__ import annotations

import argparse
import getpass
import hmac
import json
import math
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

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


def parse_line(text: str, key: str = "type") -> Tuple[Optional[dict], Optional[str]]:
    """(event, None) for a good line, (None, None) for a blank one, (None, reason) for a bad one.

    `key` names the field that tells the kind of line: "type" in progress.jsonl, "event" in a batch's status.jsonl.
    """
    text = text.strip()
    if not text:
        return None, None
    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None, "not valid JSON"
    if not isinstance(obj, dict):
        return None, "not a JSON object"
    kind = obj.get(key)
    if not isinstance(kind, str) or not kind:
        return None, f"no event {key}"
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
                 notes=(), state: Optional[str] = None, stalled: bool = False,
                 refusal: Optional[str] = None) -> None:
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
        self.refusal = refusal              # why a run was not started (preflight failed, ...)


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
        if view.refusal:
            return {"exit_code": 2, "dur": "", "text": f"Not started: {view.refusal}",
                    "export": {"status": "skipped", "text": ""}}
        return None
    cmd = "python ui/export.py --quiet" + (f" --gov {view.gov}" if view.gov else "")
    dur = d["dur"]
    if view.kind == "replay":
        # A replay shows an old run; nothing is exported from it.
        text = "Replay finished" + (f"; the run took {dur}" if dur else "") + "."
        return {"exit_code": d["exit_code"], "dur": dur, "text": text, "export": {"status": "skipped", "text": ""}}
    export = dict(view.export) if view.export else {"status": "not_run", "text": cmd}
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


# --------------------------------------------------------------------------- #
# Pacing: when does each event reach the screen?
#   SCRIPTED / SAVED  "display": about 40 ms per run_done, at least 1.4 s per node transition
#   LIVE              real time, at least 1.2 s per node transition; the holds are dropped
#                     when the screen is more than 15 s behind the log
#   replay (LIVE log) the recorded gap divided by the speed, at most 3 s
# --------------------------------------------------------------------------- #
DISPLAY_HOLD_S = 1.4
LIVE_HOLD_S = 1.2
RUN_COST_S = 0.04
CATCH_UP_S = 15.0
REPLAY_GAP_CAP_S = 3.0
STALL_S = 180.0            # a log that stops growing this long, with nothing running it, is "stopped"
LOOP_PAUSE_S = 3.0
LATEST_RESCAN_S = 1.0
POLL_S = 0.05
MAX_FRAMES = 120
DEFAULT_SPEED = 4.0
LATEST_PATTERNS = ("governance_out/bridge/*/" + LOG_NAME, "governance_out/*/" + LOG_NAME,
                   "results/governance/*/" + LOG_NAME)


class BridgeError(Exception):
    """A problem to tell the user in one line, without a traceback."""


def display_path(path, root=REPO_ROOT) -> str:
    """A repo-relative POSIX path, or <outside repo>/<name>; never an absolute path."""
    p = Path(path)
    try:
        return p.resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return f"<outside repo>/{p.name}"


def _event_ts(ev: dict) -> Optional[float]:
    dt = parse_time(ev.get("t"))
    return dt.timestamp() if dt else None


class Pacer:
    """Holds events back and releases them to the model on the display schedule."""

    def __init__(self, kind: str = "follow", speed: float = 1.0, scale: float = 1.0) -> None:
        self.kind = kind                    # follow | replay | instant
        self.speed = speed if speed and speed > 0 else 1.0
        self.scale = max(0.0, float(scale))   # the hidden --pace-scale: 1 is the spec, 0 has no holds
        self.queue: deque = deque()         # (arrival time, due time or None, event)
        self.next_free = 0.0
        self._prev_ts: Optional[float] = None
        self._due: Optional[float] = None

    def pending(self) -> int:
        return len(self.queue)

    def push(self, events, now: float) -> None:
        for ev in events:
            due = None
            if self.kind == "replay":
                ts = _event_ts(ev)
                gap = 0.0
                if ts is not None and self._prev_ts is not None:
                    gap = max(0.0, ts - self._prev_ts)
                if ts is not None:
                    self._prev_ts = ts
                base = now if self._due is None else self._due
                self._due = base + min(gap / self.speed, REPLAY_GAP_CAP_S)
                due = self._due
            self.queue.append((now, due, ev))

    def mode_for(self, model: LiveModel) -> str:
        if self.kind == "instant":
            return "instant"
        if model.mode in ("SCRIPTED", "SAVED"):
            return "display"
        return "replay" if self.kind == "replay" else "live"

    def next_time(self, model: LiveModel) -> Optional[float]:
        """When the first queued event may be applied (None if the queue is empty)."""
        if not self.queue:
            return None
        mode = self.mode_for(model)
        if mode == "instant":
            return 0.0
        if mode == "replay":
            due = self.queue[0][1]
            return self.next_free if due is None else due
        return self.next_free

    def pump(self, model: LiveModel, now: float) -> List[Tuple[dict, bool, float]]:
        """Apply every event whose time has come; returns [(event, beat, now)]."""
        applied: List[Tuple[dict, bool, float]] = []
        while self.queue:
            arrival, due, ev = self.queue[0]
            mode = self.mode_for(model)
            if mode == "replay":
                if due is not None and now < due:
                    break
            elif mode != "instant" and now < self.next_free:
                break
            self.queue.popleft()
            beat = model.apply(ev)
            applied.append((ev, beat, now))
            if mode == "display":
                if beat:
                    self.next_free = now + DISPLAY_HOLD_S * self.scale
                elif ev.get("type") == "run_done":
                    self.next_free = max(self.next_free, now - 0.1) + RUN_COST_S * self.scale
            elif mode == "live" and beat:
                hold = 0.0 if now - arrival > CATCH_UP_S else LIVE_HOLD_S * self.scale
                self.next_free = now + hold
        return applied


# --------------------------------------------------------------------------- #
# A session ties a model, its pacer and the writer together.
# --------------------------------------------------------------------------- #
class Session:
    def __init__(self, writer: LiveWriter, kind: str = "follow", log: Optional[str] = None,
                 speed: Optional[float] = None, scale: float = 1.0, gov: Optional[str] = None,
                 clock=time.monotonic) -> None:
        self.writer = writer
        self.kind = kind
        self.log = log
        self.speed = speed
        self.scale = scale
        self.gov = gov
        self.clock = clock
        self.child_alive: Optional[bool] = None
        self.export: Optional[dict] = None
        self.batch: Optional[dict] = None
        self.notes: List[str] = []
        self.forced_state: Optional[str] = None
        self.refusal: Optional[str] = None
        self.malformed = 0
        self._new_run()

    def _new_run(self) -> None:
        self.model = LiveModel()
        self.pacer = Pacer("replay" if self.kind == "replay" else "follow", self.speed or 1.0, self.scale)
        self.last_arrival: Optional[float] = None

    def reset(self) -> None:
        self._new_run()

    def feed(self, events, now: float) -> None:
        if events:
            self.pacer.push(events, now)
            self.last_arrival = now

    def view(self, now: float) -> View:
        age = None if self.last_arrival is None else max(0.0, now - self.last_arrival)
        stalled = (self.kind != "replay" and self.model.run_state == "running" and age is not None
                   and age > STALL_S and self.child_alive is not True and self.pacer.pending() == 0)
        notes = list(self.notes) + (["log stopped growing"] if stalled else [])
        return View(kind=self.kind, log=self.log, speed=self.speed if self.kind == "replay" else None,
                    child_alive=self.child_alive, last_event_age=age, malformed=self.malformed,
                    export=self.export, gov=self.gov, batch=self.batch, notes=notes,
                    state=self.forced_state, stalled=stalled, refusal=self.refusal)

    def snapshot(self, now: float) -> dict:
        return build_live(self.model, self.view(now))

    def tick(self, now: float, force: bool = False) -> bool:
        """Release due events, then offer the snapshot to the writer; True if live.js was rewritten."""
        self.pacer.pump(self.model, now)
        return self.writer.offer(self.snapshot(now), force=force)


# --------------------------------------------------------------------------- #
# Reading logs: one file at a time, or the newest one.
# --------------------------------------------------------------------------- #
def resolve_log_path(arg) -> Path:
    """PATH may be the progress.jsonl file itself or the run folder that holds it."""
    p = Path(arg)
    if p.is_dir():
        return p / LOG_NAME
    if p.suffix.lower() == ".jsonl" or p.exists():
        return p
    return p / LOG_NAME          # a run folder that does not exist yet


def find_latest_log(root=REPO_ROOT) -> Optional[Path]:
    """The progress.jsonl that was written most recently under the usual run folders."""
    best, best_m = None, -1.0
    for pattern in LATEST_PATTERNS:
        for p in Path(root).glob(pattern):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m > best_m:
                best, best_m = p, m
    return best


def load_events(path) -> Tuple[List[dict], int]:
    """All events of one run in a finished log, and how many lines were not readable."""
    data = Path(path).read_bytes()
    events: List[dict] = []
    bad = 0
    uid = None
    for raw in data.split(b"\n"):
        event, why = parse_line(raw.rstrip(b"\r").decode("utf-8", errors="replace"))
        if event is None:
            bad += 1 if why else 0
            continue
        u = event.get("uid")
        if isinstance(u, str) and u:
            if uid is None:
                uid = u
            elif u != uid:
                continue            # a second run in the same file is not part of this one
        events.append(event)
    return events, bad


class Follower:
    """Follows one progress.jsonl as it grows (or, with latest_root, the newest one)."""

    def __init__(self, session: Session, log=None, latest_root=None, root=REPO_ROOT) -> None:
        self.session = session
        self.root = Path(root)
        self.latest_root = Path(latest_root) if latest_root is not None else None
        self.reader: Optional[LogReader] = None
        self.path: Optional[Path] = None
        self._last_scan: Optional[float] = None
        if log is not None:
            self._open(resolve_log_path(log))

    def _open(self, path: Path) -> None:
        self.path = Path(path)
        self.reader = LogReader(self.path)
        self.session.log = display_path(self.path, self.root)

    def _rescan(self, now: float) -> None:
        if self.latest_root is None:
            return
        if self._last_scan is not None and now - self._last_scan < LATEST_RESCAN_S:
            return
        self._last_scan = now
        newest = find_latest_log(self.latest_root)
        if newest is None or newest == self.path:
            return
        s = self.session
        quiet = s.last_arrival is None or now - s.last_arrival > 3.0
        if self.path is None or quiet or s.model.run_state in ("done", "error", "stopped"):
            s.reset()
            self._open(newest)

    def step(self, now: float, force: bool = False) -> bool:
        self._rescan(now)
        s = self.session
        if self.reader is not None:
            res = self.reader.read()
            if res.reset:
                s.reset()
            s.malformed = res.malformed
            s.feed(res.events, now)
        return s.tick(now, force=force)


def replay_loop(session: Session, events: List[dict], loop: bool = False, clock=time.monotonic,
                sleep=time.sleep, stop=lambda: False, pause: float = LOOP_PAUSE_S) -> None:
    """Play a finished log through the session; with `loop`, again and again."""
    while True:
        session.reset()
        session.feed(events, clock())
        while session.pacer.pending():
            if stop():
                return
            session.tick(clock())
            sleep(POLL_S)
        session.tick(clock(), force=True)
        if not loop:
            return
        until = clock() + pause
        while clock() < until:
            if stop():
                return
            sleep(POLL_S)


# --------------------------------------------------------------------------- #
# Baking a replay for file:// (no bridge running): ui/data/replay.js, ui-replay/1.
# --------------------------------------------------------------------------- #
def _key_signature(snap: dict) -> tuple:
    return (snap["state"], snap["active"], tuple(n["state"] for n in snap["nodes"].values()),
            tuple(snap["edges"].values()), tuple((r["state"], r["verdict"]) for r in snap["rail"]),
            snap["done"] is not None)


def simulate_frames(events: List[dict], speed: float, scale: float, log: str) -> List[tuple]:
    """Run the events through the replay schedule on a virtual clock: [(t_ms, snapshot, is_key)]."""
    model = LiveModel()
    pacer = Pacer("replay", speed, scale)
    pacer.push(events, 0.0)
    view = View(kind="replay", log=log, speed=speed, last_event_age=0.0)
    frames: List[tuple] = []
    prev_body = prev_key = None
    now = 0.0
    guard = 0
    while pacer.pending() and guard < 4 * len(events) + 100:
        guard += 1
        t = pacer.next_time(model)
        now = max(now, 0.0 if t is None else t)
        pacer.pump(model, now)
        snap = build_live(model, view)
        body = json.dumps(snap, sort_keys=True, default=str)
        if body == prev_body:
            continue
        key = _key_signature(snap)
        frames.append((int(round(now * 1000)), snap, key != prev_key))
        prev_body, prev_key = body, key
    return frames


def _spread(items: List[int], k: int) -> List[int]:
    if k <= 0:
        return []
    if len(items) <= k:
        return list(items)
    return [items[int(j * len(items) / k)] for j in range(k)]


def select_frames(frames: List[tuple], limit: int = MAX_FRAMES) -> List[tuple]:
    """At most `limit` frames: the first, the last and every frame where a node changed come first."""
    n = len(frames)
    if n <= limit:
        return list(frames)
    keep = {0, n - 1}
    keys = [i for i, f in enumerate(frames) if f[2]]
    if len(keep | set(keys)) <= limit:
        keep |= set(keys)
        rest = [i for i in range(n) if i not in keep]
        keep |= set(_spread(rest, limit - len(keep)))
    else:
        pool = [i for i in keys if i not in keep]
        keep |= set(_spread(pool, limit - len(keep)))
    return [frames[i] for i in sorted(keep)]


def bake(src, out_path, speed: float = DEFAULT_SPEED, scale: float = 1.0, limit: int = MAX_FRAMES) -> int:
    """Write the replay of a finished log to `out_path` (window.LIVE_REPLAY); returns the frame count."""
    path = resolve_log_path(src)
    if not path.is_file():
        raise BridgeError(f"log not found: {display_path(path)}")
    events, _bad = load_events(path)
    if not events:
        raise BridgeError("the log has no readable events")
    log = display_path(path)
    chosen = select_frames(simulate_frames(events, speed, scale, log), limit)
    frames = []
    for i, (t, snap, _key) in enumerate(chosen, start=1):
        live = dict(snap)
        live["rev"] = i
        frames.append({"t": t, "live": live})
    last = chosen[-1][1]
    obj = {"schema": SCHEMA_REPLAY, "baked_at": utc_now_iso(),
           "source": {"log": log, "mode": last["mode"], "uid": last["source"]["uid"], "speed": speed},
           "frames": frames}
    write_atomic(out_path, render_js(Scrubber().walk(obj), var="LIVE_REPLAY"))
    return len(frames)


# --------------------------------------------------------------------------- #
# --serve: the ui/ folder on http://127.0.0.1:<port>/ (this computer only).
#   GET and HEAD only for files; files under ui/ only (the resolved path is checked, so a
#   symbolic link out of ui/ is refused); no directory listing; every answer says
#   Cache-Control: no-store and X-Content-Type-Options: nosniff; no CORS headers;
#   a Host header that is not this server is refused (DNS rebinding).
#   The only POSTs are /api/run and /api/stop (see "PAGE-STARTED RUNS" above), and only
#   when the server was made with a RunController.
# --------------------------------------------------------------------------- #
SERVE_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
}   # a fixed table: the Windows registry can map .js to text/plain


# The page's JSON API: path -> the one method it answers.
API_ROUTES = {"/api/session": "GET", "/api/status": "GET", "/api/run": "POST", "/api/stop": "POST"}
API_BODY_LIMIT = 4096          # bytes of a POST body


class UiHandler(BaseHTTPRequestHandler):
    server_version = "RedlineBridge"
    sys_version = ""
    ui_root: Path = UI_DIR.resolve()
    timeout = 15                # a client that sends less body than it promised cannot hold a thread for ever

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, format, *args) -> None:  # noqa: A002 - quiet: no request log on the console
        return

    def _reply(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8", extra=None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return True
        port = self.server.server_address[1]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost")

    def _target(self) -> Optional[Path]:
        """The file a request path names, or None. Anything odd is None (a 404)."""
        raw = self.path.split("?", 1)[0].split("#", 1)[0]
        if not raw.startswith("/"):
            return None
        try:
            decoded = unquote(raw, errors="strict")
        except UnicodeDecodeError:
            return None
        if "\x00" in decoded or "\\" in decoded:
            return None
        parts = [p for p in decoded.split("/") if p]
        if parts and decoded.endswith("/"):
            return None                     # a folder (or a file written as one): no listing
        if any(p in (".", "..") or p.startswith(".") for p in parts):
            return None
        root = self.ui_root
        candidate = root.joinpath(*parts) if parts else root / "index.html"
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError, RuntimeError):
            return None
        if not resolved.is_file() or resolved.suffix.lower() not in CONTENT_TYPES:
            return None
        return resolved

    def _serve(self) -> None:
        if not self._host_ok():
            self._reply(403, b"forbidden\n")
            return
        target = self._target()
        if target is None:
            self._reply(404, b"not found\n")
            return
        try:
            data = target.read_bytes()
        except OSError:
            self._reply(404, b"not found\n")
            return
        self._reply(200, data, CONTENT_TYPES[target.suffix.lower()])

    # ---- the JSON API (only when the server has a RunController) ----
    def _api_route(self) -> Optional[str]:
        """The path if it is in the /api namespace of a server that has a controller, else None."""
        if getattr(self.server, "controller", None) is None:
            return None
        raw = self.path.split("?", 1)[0].split("#", 1)[0]
        return raw if raw == "/api" or raw.startswith("/api/") else None

    def _json(self, code: int, obj) -> None:
        body = (json.dumps(obj, separators=(",", ":")) + "\n").encode("ascii")
        self._reply(code, body, "application/json; charset=utf-8")

    def _drain(self) -> None:
        """Read and drop a request body we will not use (up to 1 MB), so the client sees our answer and not a reset."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 1_000_000:
            self.close_connection = True
            return
        try:
            while length > 0:                       # read and drop it, a piece at a time
                piece = self.rfile.read(min(length, 65536))
                if not piece:
                    break
                length -= len(piece)
        except OSError:
            self.close_connection = True

    def _origin_ok(self) -> bool:
        """No Origin header is fine (a same-origin GET has none). A present one must be this server."""
        origins = self.headers.get_all("Origin") or []
        if not origins:
            return True
        port = self.server.server_address[1]
        return len(origins) == 1 and origins[0].strip() in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def _api_guard(self) -> bool:
        if self._host_ok() and self._origin_ok():
            return True
        self._drain()
        self._json(403, {"error": "forbidden: this request does not come from the page"})
        return False

    def _read_json_body(self) -> Optional[dict]:
        """The JSON object of a POST body, or None after answering 400."""
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self._drain()
            self._json(400, {"error": "Content-Type must be application/json"})
            return None
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            self._json(400, {"error": "Content-Length is required"})
            return None
        if length > API_BODY_LIMIT:
            self._drain()
            self._json(400, {"error": f"the request body is over {API_BODY_LIMIT} bytes"})
            return None
        try:
            raw = self.rfile.read(length)
        except OSError:
            self.close_connection = True
            return None
        try:
            if len(raw) != length:
                raise ValueError("short body")
            obj = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        except (ValueError, RecursionError):        # UnicodeDecodeError is a ValueError
            self._json(400, {"error": "the body is not valid JSON"})
            return None
        if not isinstance(obj, dict):
            self._json(400, {"error": "the body must be a JSON object"})
            return None
        return obj

    def _api_post(self, route: str) -> None:
        controller = self.server.controller
        if not controller.token_ok(self.headers.get("X-Redline-Token")):
            self._drain()
            self._json(403, {"error": "missing or wrong token"})
            return
        try:
            if route == "/api/stop":
                self._drain()
                controller.stop()
            else:
                payload = self._read_json_body()
                if payload is None:
                    return
                controller.start(payload)
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
            return
        self._json(202, {"ok": True})

    def _allow(self) -> str:
        route = self._api_route()
        return API_ROUTES.get(route, "GET, HEAD") if route is not None else "GET, HEAD"

    def do_GET(self) -> None:  # noqa: N802
        route = self._api_route()
        if route is None:
            self._serve()
            return
        if not self._api_guard():
            return
        want = API_ROUTES.get(route)
        if want is None:
            self._json(404, {"error": "not found"})
        elif want != "GET":
            self._not_allowed()
        elif route == "/api/session":
            self._json(200, {"token": self.server.controller.token})
        else:
            self._json(200, self.server.controller.status())

    def do_HEAD(self) -> None:  # noqa: N802
        if self._api_route() is not None:
            self._not_allowed()
        else:
            self._serve()

    def do_POST(self) -> None:  # noqa: N802
        route = self._api_route()
        if route is None:
            self._not_allowed()
            return
        if not self._api_guard():
            return
        want = API_ROUTES.get(route)
        if want is None:
            self._drain()
            self._json(404, {"error": "not found"})
        elif want != "POST":
            self._not_allowed()
        else:
            self._api_post(route)

    def _not_allowed(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if 0 < length <= 1_000_000:
            self.rfile.read(length)         # read the body so the client sees the answer, not a reset
        self._reply(405, b"method not allowed\n", extra={"Allow": self._allow()})

    def __getattr__(self, name: str):
        if name.startswith("do_"):
            return self._not_allowed          # PUT, DELETE, OPTIONS, anything else
        raise AttributeError(name)


class UiServer(ThreadingHTTPServer):
    allow_reuse_address = not IS_WINDOWS     # on Windows, reuse would let another program share the port
    daemon_threads = True
    controller = None                        # a RunController, or None: then there is no /api


def make_server(port: int = DEFAULT_PORT, root=UI_DIR, controller=None) -> UiServer:
    """A web server for the ui/ folder on 127.0.0.1 (never any other address).

    With a RunController the page also gets the /api routes (and may start runs); without one there is no /api."""
    handler = type("BoundUiHandler", (UiHandler,), {"ui_root": Path(root).resolve()})
    try:
        server = UiServer((SERVE_HOST, port), handler)
    except OSError as exc:
        raise BridgeError(f"port {port} is already in use or not allowed ({exc.strerror or exc}); "
                          f"try another with --port") from exc
    server.controller = controller
    return server


def serve_in_thread(server: UiServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    return thread


# --------------------------------------------------------------------------- #
# --run: start a pipeline run from a fixed list (the page never starts anything).
#   * fixed argument lists, shell=False, PYTHONUNBUFFERED=1 and PYTHONUTF8=1
#   * the LIVE keys run scripts/preflight.py first and refuse to start if it fails
#   * --model must be listed in config/models.txt; it reaches the run as LAB_MODEL
#   * interpreter: .venv/bin/python, then .venv\Scripts\python.exe, then --python
#   * after the child exits: ui/export.py --quiet --gov <out>, unless --no-export
#   * Windows: the child gets its own process group and is stopped with CTRL_BREAK_EVENT
#     (NOT VERIFIED ON WINDOWS)
# --------------------------------------------------------------------------- #
RUN_KEYS = ("demo", "live-short", "live-smoke", "live-full", "models")
LIVE_KEYS = ("live-short", "live-smoke", "live-full")
RE_SLUG = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
PREFLIGHT_TIMEOUT_S = 90
EXPORT_TIMEOUT_S = 180
STOP_GRACE_S = 15.0
STOP_TERM_S = 5.0
EXPORT_OK_TEXT = "Evidence screens updated: press R."
BATCH_SCAN_S = 0.3


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def model_slug(tag: str) -> str:
    """A model tag as a folder-name part: letters, digits and . _ - only ("qwen2.5:7b-instruct" -> "qwen2.5-7b-instruct").
    Made here; it is not read from scripts/run_models.py."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", tag).strip("-._")[:60].strip("-._")
    return slug or "model"


def run_argv(key: str, py: str, stamp: str, parallel: int = 2,
             model: Optional[str] = None) -> Tuple[List[str], Optional[str]]:
    """The fixed argument list for a run key, and the run folder it writes (None for `models`).

    `model` is used only to name the folder of `live-full`; it reaches the run as LAB_MODEL, not as an argument."""
    if key == "live-full":
        if not model:
            raise BridgeError("live-full needs a model: pass --model TAG")
        out = f"results/governance/{stamp}_{model_slug(model)}"
        return [py, "scripts/run_governance.py", "--live", "--sample", "26", "--reps", "3", "--out", out], out
    if key == "demo":
        out = f"governance_out/bridge/demo_{stamp}"
        return [py, "scripts/run_governance.py", "--out", out], out
    if key == "live-short":
        out = f"governance_out/bridge/live_{stamp}"
        return [py, "scripts/run_governance.py", "--live", "--sample", "2", "--reps", "1", "--out", out], out
    if key == "live-smoke":
        out = f"governance_out/bridge/smoke_{stamp}"
        return [py, "scripts/run_governance.py", "--live", "--sample", "1", "--reps", "3", "--out", out], out
    if key == "models":
        if not isinstance(parallel, int) or not 1 <= parallel <= 4:
            raise BridgeError("--parallel must be from 1 to 4")
        return [py, "scripts/run_models.py", "--models", "config/models.txt", "--parallel", str(parallel)], None
    raise BridgeError(f"unknown run key {key!r}; the choices are {', '.join(RUN_KEYS)}")


def read_models(root) -> List[str]:
    """The Ollama tags in config/models.txt: one per line, blank lines and # comments ignored."""
    path = Path(root) / "config" / "models.txt"
    if not path.is_file():
        raise BridgeError("config/models.txt not found")
    tags = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        if s and not s.startswith("#"):
            tags.append(s)
    return tags


def check_model(root, tag: str) -> None:
    if tag not in read_models(root):
        raise BridgeError(f"model {tag!r} is not listed in config/models.txt")


def resolve_python(root, override: Optional[str] = None) -> str:
    """.venv/bin/python, then .venv/Scripts/python.exe, then --python. (A venv's python is not resolved.)"""
    base = Path(root)
    for cand in (base / ".venv" / "bin" / "python", base / ".venv" / "Scripts" / "python.exe"):
        if cand.is_file():
            return str(cand)
    if override:
        p = Path(override)
        if p.is_file():
            return str(p)
        raise BridgeError(f"--python {display_path(p)}: file not found")
    raise BridgeError("no Python found: create .venv in the repo folder or pass --python PATH")


def child_env(model: Optional[str] = None) -> Dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    if model:
        env["LAB_MODEL"] = model
    return env


def _last_line(text: Optional[str]) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def run_preflight(py: str, root, env: Dict[str, str], model: Optional[str] = None) -> Tuple[bool, str]:
    """scripts/preflight.py; (True, "") if it passes, else (False, the first FAIL line)."""
    argv = [py, "scripts/preflight.py"] + (["--model", model] if model else [])
    try:
        done = subprocess.run(argv, cwd=str(root), env=env, shell=False, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=PREFLIGHT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, "preflight did not answer in time"
    except OSError as exc:
        return False, f"preflight could not start ({exc.strerror or exc})"
    if done.returncode == 0:
        return True, ""
    fails = [ln.strip() for ln in (done.stdout or "").splitlines() if ln.strip().startswith("FAIL")]
    text = fails[0] if fails else (_last_line(done.stdout) or _last_line(done.stderr) or f"exit code {done.returncode}")
    if len(fails) > 1:
        text += f" (+{len(fails) - 1} more)"
    return False, clip(text, 220)


def run_export(py: str, root, env: Dict[str, str], gov: Optional[str]) -> dict:
    """ui/export.py --quiet [--gov <run folder>]; never raises. If export.py lacks a flag, its exact error is the text."""
    if not (Path(root) / "ui" / "export.py").is_file():
        return {"status": "failed", "text": "Export failed: ui/export.py not found."}
    argv = [py, "ui/export.py", "--quiet"] + (["--gov", gov] if gov else [])
    try:
        done = subprocess.run(argv, cwd=str(root), env=env, shell=False, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=EXPORT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"status": "failed", "text": "Export failed: it did not finish in time."}
    except OSError as exc:
        return {"status": "failed", "text": f"Export failed: could not start ({exc.strerror or exc})."}
    if done.returncode == 0:
        return {"status": "ok", "text": EXPORT_OK_TEXT}
    message = _last_line(done.stderr) or _last_line(done.stdout) or f"exit code {done.returncode}"
    return {"status": "failed", "text": "Export failed: " + clip(Scrubber([root])(message), 240)}


class Child:
    """One child process: its own process group, never a shell, stopped in steps."""

    def __init__(self, argv: List[str], cwd, env: Dict[str, str]) -> None:
        self.argv = list(argv)
        self.cwd = str(cwd)
        self.env = env
        self.proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        kwargs: Dict[str, Any] = dict(cwd=self.cwd, env=self.env, shell=False, stdin=subprocess.DEVNULL)
        if IS_WINDOWS:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        else:
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(self.argv, **kwargs)
        except OSError as exc:
            raise BridgeError(f"could not start the run ({exc.strerror or exc})") from exc

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def returncode(self) -> Optional[int]:
        return None if self.proc is None else self.proc.poll()

    def _signal(self, kind: str) -> None:
        p = self.proc
        if p is None or p.poll() is not None:
            return
        try:
            if IS_WINDOWS:
                if kind == "interrupt":
                    p.send_signal(signal.CTRL_BREAK_EVENT)      # NOT VERIFIED ON WINDOWS
                elif kind == "terminate":
                    p.terminate()
                else:
                    p.kill()
            else:
                sig = {"interrupt": signal.SIGINT, "terminate": signal.SIGTERM, "kill": signal.SIGKILL}[kind]
                os.killpg(p.pid, sig)       # the child leads its own group, like a Ctrl-C typed at it
        except (OSError, ValueError):
            pass

    def _wait(self, seconds: float) -> bool:
        try:
            self.proc.wait(timeout=seconds)
            return True
        except subprocess.TimeoutExpired:
            return False
        except KeyboardInterrupt:
            return False

    def stop(self, grace: float = STOP_GRACE_S, term_wait: float = STOP_TERM_S) -> None:
        """One interrupt first (so the run can lock its folder), then terminate, then kill."""
        if self.proc is None or self.proc.poll() is not None:
            return
        self._signal("interrupt")
        if self._wait(grace):
            return
        self._signal("terminate")
        if self._wait(term_wait):
            return
        self._signal("kill")
        self._wait(2.0)


# ---- batches (scripts/run_models.py): results/models/<STAMP>/plan.json and status.jsonl ----
def parse_status_line(text: str) -> Optional[dict]:
    event, _why = parse_line(text, key="event")
    return event


class BatchWatcher:
    """Reads a model batch's plan.json and status.jsonl into one row per model."""

    PHASE = {"queued": "queued", "pull_start": "checking", "pull_done": "checking", "preflight_start": "checking",
             "preflight_ok": "checking", "preflight_skipped": "checking", "preflight_fail": "failed",
             "smoke_start": "smoke", "smoke_done": "smoke", "full_start": "running", "skipped": "skipped"}

    def __init__(self, batch_dir, root=REPO_ROOT) -> None:
        self.dir = Path(batch_dir)
        self.root = Path(root)
        self.tail = Tailer(self.dir / "status.jsonl")
        self.rec: Dict[str, dict] = {}
        self._plan_read = False

    def _row(self, slug: str, tag: Optional[str] = None) -> dict:
        row = self.rec.get(slug)
        if row is None:
            row = {"slug": slug, "tag": tag or slug, "state": "queued", "tools": "not checked", "tools_text": "",
                   "reason": "", "smoke_out": None, "full_out": None}
            self.rec[slug] = row
        elif tag:
            row["tag"] = tag
        return row

    def _read_plan(self) -> None:
        if self._plan_read:
            return
        try:
            plan = json.loads((self.dir / "plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._plan_read = True
        for m in _dicts(plan.get("models") if isinstance(plan, dict) else None):
            slug, tag = m.get("slug"), m.get("tag")
            if isinstance(slug, str) and RE_SLUG.match(slug):
                row = self._row(slug, tag if isinstance(tag, str) else None)
                row["smoke_out"] = row["smoke_out"] or str_or_none(m.get("smoke_out"))
                row["full_out"] = row["full_out"] or str_or_none(m.get("full_out"))

    def poll(self) -> None:
        self._read_plan()
        res = self.tail.poll()
        if res.reset:
            self.rec = {}
            self._plan_read = False
            self._read_plan()
        for text in res.lines:
            ev = parse_status_line(text)
            if ev is None:
                continue
            slug, kind = ev.get("slug"), ev.get("event")
            if not (isinstance(slug, str) and RE_SLUG.match(slug)) or not isinstance(kind, str):
                continue
            row = self._row(slug, str_or_none(ev.get("tag")))
            if kind in self.PHASE:
                row["state"] = self.PHASE[kind]
            if kind == "preflight_fail":
                row["reason"] = clip(ev.get("detail") or "preflight failed", 160)
            elif kind == "skipped":
                row["reason"] = clip(ev.get("reason") or "", 160)
            elif kind == "smoke_start":
                row["smoke_out"] = str_or_none(ev.get("out")) or row["smoke_out"]
            elif kind == "smoke_done":
                tools = str_or_none(ev.get("tools"))
                row["tools"] = tools if tools in ("yes", "no", "not checked") else "not checked"
                row["tools_text"] = clip(ev.get("text") or "", 120)
            elif kind == "full_start":
                row["full_out"] = str_or_none(ev.get("out")) or row["full_out"]
            elif kind == "full_done":
                row["state"] = "done" if to_int(ev.get("exit_code")) == 0 else "failed"
                if row["state"] == "failed":
                    row["reason"] = f"exit code {to_int(ev.get('exit_code'))}"

    @staticmethod
    def _text(row: dict) -> str:
        state, tools = row["state"], row["tools_text"]
        if state == "queued":
            return "waiting for its turn"
        if state == "checking":
            return "checking the model"
        if state == "smoke":
            return "smoke run" + (f"; tool calls: {tools}" if tools else "")
        if state == "running":
            return "full run in progress" + (f"; tool calls: {tools}" if tools else "")
        if state == "done":
            return "full run finished" + (f"; tool calls: {tools}" if tools else "")
        if state == "failed":
            return "a step failed" + (f": {row['reason']}" if row["reason"] else "")
        return "skipped" + (f": {row['reason']}" if row["reason"] else "") + (f"; tool calls: {tools}" if tools else "")

    def models(self) -> List[dict]:
        rows = sorted(self.rec.values(), key=lambda r: (r["tag"], r["slug"]))
        return [{"slug": r["slug"], "tag": r["tag"], "state": r["state"], "tools": r["tools"],
                 "text": self._text(r)} for r in rows]

    def auto_pick(self) -> Optional[str]:
        """The model to show when none was named: one that is running, else the last one with a log."""
        rows = sorted(self.rec.values(), key=lambda r: (r["tag"], r["slug"]))
        for state in ("running", "smoke", "checking"):
            for r in rows:
                if r["state"] == state:
                    return r["slug"]
        with_log = [r for r in rows if r["full_out"] or r["smoke_out"]]
        return with_log[-1]["slug"] if with_log else (rows[0]["slug"] if rows else None)

    def log_path(self, slug: Optional[str]) -> Optional[Path]:
        row = self.rec.get(slug) if slug else None
        if row is None:
            return None
        out = row["full_out"] if row["state"] in ("running", "done", "failed") and row["full_out"] else \
            (row["smoke_out"] or row["full_out"])
        if not out:
            return None
        p = Path(out)
        if p.is_absolute() or ".." in p.parts:
            return None
        return self.root / p / LOG_NAME


def find_batch_dir(root, not_before: Optional[str] = None) -> Optional[Path]:
    """The newest results/models/<STAMP> folder that has a plan (not older than `not_before`, a stamp)."""
    base = Path(root) / "results" / "models"
    best = None
    try:
        names = sorted(p.name for p in base.iterdir() if p.is_dir())
    except OSError:
        return None
    for name in names:
        if not_before and name < not_before:
            continue
        if (base / name / "plan.json").is_file() or (base / name / "status.jsonl").is_file():
            best = base / name
    return best


class BatchFollower:
    """Follows a model batch: the list of models, and the progress log of the one on show."""

    def __init__(self, session: Session, root, batch_dir=None, follow_model: Optional[str] = None,
                 not_before: Optional[str] = None) -> None:
        self.session = session
        self.root = Path(root)
        self.batch_dir = Path(batch_dir) if batch_dir is not None else None
        self.follow_model = follow_model
        self.not_before = not_before
        self.watcher: Optional[BatchWatcher] = None
        self.follower = Follower(session, None, root=root)
        self._scan_at: Optional[float] = None

    def refresh(self) -> None:
        """Look for the batch folder again at the next step (the run has ended; nothing more will appear)."""
        self._scan_at = None

    def step(self, now: float, force: bool = False) -> bool:
        if self.batch_dir is None and (self._scan_at is None or now - self._scan_at >= BATCH_SCAN_S):
            self._scan_at = now
            self.batch_dir = find_batch_dir(self.root, self.not_before)
        if self.batch_dir is not None and self.watcher is None:
            self.watcher = BatchWatcher(self.batch_dir, self.root)
        if self.watcher is not None:
            self.watcher.poll()
            self.session.batch = {"models": self.watcher.models()}
            slug = self.follow_model or self.watcher.auto_pick()
            path = self.watcher.log_path(slug)
            if path is not None and path != self.follower.path:
                self.session.reset()
                self.follower._open(path)
        return self.follower.step(now, force=force)


class _StopRequested(Exception):
    """The page asked to stop the run (on the command line, Ctrl-C does this)."""


class RunOutcome:
    """What a run came to. `refused` is why it never started; `interrupted` is a stop (Ctrl-C or the page)."""

    def __init__(self, code: int, interrupted: bool = False, refused: Optional[str] = None,
                 out: Optional[str] = None, export: Optional[dict] = None, text: str = "") -> None:
        self.code = code
        self.interrupted = interrupted
        self.refused = refused
        self.out = out
        self.export = export
        self.text = text


def execute_run(root, py: str, key: str, model: Optional[str], parallel: int, no_export: bool, data_dir, scale: float,
                follow_model: Optional[str] = None, stop: Optional[threading.Event] = None,
                fresh_screen: bool = False, on_child=None, stop_hint: str = "Ctrl-C stops the run.") -> RunOutcome:
    """One run, start to finish: (preflight), start the child, follow it, export. Shared by --run and the page.

    `stop` is an Event the page sets to stop the run; the command line stops with Ctrl-C (KeyboardInterrupt) instead.
    `fresh_screen` rewrites live.js to "waiting" first, so the page does not show the previous run during the preflight.
    Raises BridgeError for a run that cannot start (a missing script, a child that will not launch)."""
    root = Path(root)
    env = child_env(model)
    stamp = utc_stamp()
    argv, out_rel = run_argv(key, py, stamp, parallel, model)
    if not (root / argv[1]).is_file():
        raise BridgeError(f"{argv[1]} not found")
    if key == "models" and not read_models(root):
        raise BridgeError("config/models.txt lists no models")
    writer = LiveWriter(Path(data_dir) / "live.js")
    session = Session(writer, kind="run", scale=scale, gov=out_rel)
    if fresh_screen:
        session.tick(time.monotonic(), force=True)
    if key in LIVE_KEYS:
        _say("bridge: running the preflight checks first ...")
        ok, why = run_preflight(py, root, env, model)
        if not ok:
            session.refusal = f"preflight failed. {why}"
            session.forced_state = "error"
            session.tick(time.monotonic(), force=True)
            return RunOutcome(2, refused=session.refusal, out=None)
    if stop is not None and stop.is_set():           # stopped during the preflight: nothing was started
        session.forced_state = "stopped"
        session.notes.append("stopped by you")
        session.export = {"status": "skipped", "text": "The run was stopped, so the evidence screens were not updated."}
        session.tick(time.monotonic(), force=True)
        return RunOutcome(130, interrupted=True, out=None, export=session.export, text="stopped before it started")
    if key == "models":
        follower: Any = BatchFollower(session, root, follow_model=follow_model, not_before=stamp)
    else:
        follower = Follower(session, root / out_rel / LOG_NAME, root=root)
    child = Child(argv, root, env)
    child.start()
    if on_child is not None:
        on_child(child)
    session.child_alive = True
    _say(f"bridge: started {key}" + (f" (model {model})" if model else "")
         + (f", writing {out_rel}" if out_rel else "") + f". {stop_hint}")
    interrupted = False
    exporter: Optional[threading.Thread] = None
    try:
        while True:
            if stop is not None and stop.is_set():
                raise _StopRequested()
            alive = child.alive()
            session.child_alive = alive
            follower.step(time.monotonic())
            if not alive:
                break
            time.sleep(POLL_S)
        if isinstance(follower, BatchFollower):
            follower.refresh()
        follower.step(time.monotonic())
        code = child.returncode()
        if code != 0:
            session.export = {"status": "skipped", "text": "The run did not finish, so the evidence screens were not updated."}
        elif no_export:
            session.export = None           # the default text is the command to run
        else:
            session.export = {"status": "running", "text": "Updating the evidence screens ..."}

            def work() -> None:
                session.export = run_export(py, root, env, out_rel)

            exporter = threading.Thread(target=work, daemon=True)
            exporter.start()
        while session.pacer.pending() or (exporter is not None and exporter.is_alive()):
            follower.step(time.monotonic())
            time.sleep(POLL_S)
        follower.step(time.monotonic())
    except (KeyboardInterrupt, _StopRequested):
        interrupted = True
        _say("bridge: stopping the run; it keeps its folder")
    finally:
        if child.alive():
            child.stop()
    code = child.returncode()
    if interrupted:
        session.pacer.kind = "instant"
        session.child_alive = False
        follower.step(time.monotonic())
        session.forced_state = session.forced_state or ("stopped" if session.model.run_state == "running" else None)
        session.notes.append("stopped by you")
        session.export = {"status": "skipped", "text": "The run was stopped, so the evidence screens were not updated."}
    elif code not in (0, None) and session.model.run_state == "running":
        session.forced_state = "error"
        session.notes.append(f"the run ended with exit code {code} before it finished")
    session.child_alive = False
    follower.step(time.monotonic(), force=True)
    done = session.snapshot(time.monotonic())["done"]
    text = done["text"] if done else f"the run ended with exit code {code if code is not None else 'unknown'}"
    _say("bridge: " + text)
    export = (done or {}).get("export") or session.export or {"status": "not_run", "text": ""}
    return RunOutcome(130 if interrupted else (code if code is not None else 0), interrupted=interrupted,
                      out=out_rel, export=export, text=text)


# --------------------------------------------------------------------------- #
# Page-started runs: one at a time, only from the fixed list (see "PAGE-STARTED RUNS" above).
# --------------------------------------------------------------------------- #
API_KINDS = ("demo", "live-short", "live-full", "models")      # the page's runs; live-smoke stays command-line only
API_PARALLEL = (1, 2)
SHUTDOWN_WAIT_S = STOP_GRACE_S + STOP_TERM_S + 10.0


class ApiError(Exception):
    """A refused API request: an HTTP status and one line for the page."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class RunController:
    """Starts, tracks and stops the page's runs. The server holds one; the token is made once per server."""

    def __init__(self, root=REPO_ROOT, data_dir=DATA_DIR, python: Optional[str] = None, no_export: bool = False,
                 scale: float = 1.0) -> None:
        self.root = Path(root)
        self.data_dir = Path(data_dir)
        self.python = python
        self.no_export = no_export
        self.scale = scale
        self.token = secrets.token_hex(16)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._running = False
        self._external = False              # the run belongs to --run on the command line, not to the page
        self._current: Optional[dict] = None
        self._last: Optional[dict] = None
        self._thread: Optional[threading.Thread] = None
        self._child: Optional[Child] = None
        self._scrub = Scrubber([self.root])

    # ---- what the page reads ----
    def token_ok(self, supplied) -> bool:
        """Constant-time comparison; anything that is not text counts as empty."""
        text = supplied if isinstance(supplied, str) else ""
        return hmac.compare_digest(text.encode("utf-8", "replace"), self.token.encode("ascii"))

    def status(self) -> dict:
        with self._lock:
            cur = dict(self._current) if self._current else {}
            last = dict(self._last) if self._last else None
            running = self._running
        try:
            models = read_models(self.root)
        except BridgeError:
            models = []
        return {"running": running, "kind": cur.get("kind"), "model": cur.get("model"),
                "started": cur.get("started"), "last": last, "models": models, "kinds": list(API_KINDS)}

    # ---- what the page asks ----
    def start(self, payload: dict) -> None:
        """Validate the request and start the run in a thread; raise ApiError (400 or 409) if it cannot."""
        kind = payload.get("kind")
        if not isinstance(kind, str) or kind not in API_KINDS:
            raise ApiError(400, "kind must be one of: " + ", ".join(API_KINDS))
        model = payload.get("model")
        if model is not None and (not isinstance(model, str) or not model):
            raise ApiError(400, "model must be a model tag (text)")
        if kind == "models" and model is not None:
            raise ApiError(400, "model does not apply to the models run")
        if kind in LIVE_KEYS and model is None:
            raise ApiError(400, f"{kind} needs a model")
        parallel = 2
        if payload.get("parallel") is not None:
            value = payload["parallel"]
            if isinstance(value, bool) or not isinstance(value, int) or value not in API_PARALLEL:
                raise ApiError(400, "parallel must be 1 or 2")
            parallel = value
        try:
            if model is not None:
                check_model(self.root, model)
            if kind == "models" and not read_models(self.root):
                raise BridgeError("config/models.txt lists no models")
            py = resolve_python(self.root, self.python)
            argv, _out = run_argv(kind, py, utc_stamp(), parallel, model)
            if not (self.root / argv[1]).is_file():
                raise BridgeError(f"{argv[1]} not found")
        except BridgeError as exc:
            raise ApiError(400, self._scrub(str(exc))) from exc
        with self._lock:
            if self._running:
                raise ApiError(409, "a run is in progress")
            self._running = True
            self._external = False
            self._current = {"kind": kind, "model": model, "started": utc_now_iso()}
            self._stop.clear()
            thread = threading.Thread(target=self._work, args=(kind, model, parallel, py), daemon=True,
                                      name="redline-run")
            self._thread = thread
        try:
            thread.start()
        except RuntimeError as exc:                 # no thread could be made: do not stay "running"
            self._finish(kind, model, None, f"could not start the run ({exc})")
            raise ApiError(400, "could not start the run") from exc

    def stop(self) -> None:
        """Ask the running run to stop (an interrupt first, then terminate, then kill); raise ApiError 409 if none."""
        with self._lock:
            if not self._running:
                raise ApiError(409, "nothing is running")
            if self._external:
                raise ApiError(409, "this run was started from the command line; stop it there with Ctrl-C")
            self._stop.set()

    # ---- a run started by --run while the page is served ----
    def begin_external(self, kind: str, model: Optional[str]) -> None:
        with self._lock:
            if self._running:
                raise BridgeError("a run started from the page is already in progress")
            self._running = True
            self._external = True
            self._current = {"kind": kind, "model": model, "started": utc_now_iso()}

    def end_external(self, kind: str, model: Optional[str], outcome: Optional[RunOutcome], error: Optional[str]) -> None:
        self._finish(kind, model, outcome, error)

    # ---- the worker ----
    def _set_child(self, child: Child) -> None:
        self._child = child

    def _work(self, kind: str, model: Optional[str], parallel: int, py: str) -> None:
        outcome: Optional[RunOutcome] = None
        error: Optional[str] = None
        try:
            _say(f"bridge: run started from the page: {kind}" + (f" (model {model})" if model else ""))
            outcome = execute_run(self.root, py, kind, model, parallel, self.no_export, self.data_dir, self.scale,
                                  stop=self._stop, fresh_screen=True, on_child=self._set_child,
                                  stop_hint="The Stop button on the page stops the run.")
        except BridgeError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - whatever happens, the controller must not stay "running"
            error = f"the run failed ({exc.__class__.__name__})"
        self._finish(kind, model, outcome, error)

    def _finish(self, kind: str, model: Optional[str], outcome: Optional[RunOutcome], error: Optional[str]) -> None:
        last = self._last_record(kind, model, outcome, error)
        with self._lock:
            self._last = last
            self._running = False
            self._external = False
            self._current = None
            self._child = None

    def _last_record(self, kind: str, model: Optional[str], outcome: Optional[RunOutcome], error: Optional[str]) -> dict:
        if outcome is None:
            return {"kind": kind, "model": model, "exit_code": 2, "export_status": "skipped", "export_text": "",
                    "out": None, "error": self._scrub(error or "the run did not start")}
        export = dict(outcome.export or {"status": "skipped", "text": ""})
        err: Optional[str] = None
        if outcome.refused:
            err = outcome.refused
            export = {"status": "skipped", "text": "Not started: " + outcome.refused}
        elif outcome.interrupted:
            err = "stopped by you"
        elif outcome.code != 0:
            err = f"the run ended with exit code {outcome.code}"
        return {"kind": kind, "model": model, "exit_code": outcome.code, "export_status": export.get("status"),
                "export_text": self._scrub(export.get("text") or ""), "out": outcome.out,
                "error": self._scrub(err) if err else None}

    def shutdown(self, timeout: float = SHUTDOWN_WAIT_S) -> None:
        """Stop a page-started run and wait for it, so no child is left running when the bridge exits."""
        with self._lock:
            thread = self._thread if self._running and not self._external else None
            if thread is not None:
                self._stop.set()
        if thread is None:
            return
        try:
            thread.join(timeout)
        except KeyboardInterrupt:
            pass
        child = self._child
        if child is not None and child.alive():
            child.stop(grace=0.0, term_wait=2.0)        # the wait ran out (or a second Ctrl-C): do not leave it


# --------------------------------------------------------------------------- #
# Command line.
# --------------------------------------------------------------------------- #
USAGE_TEXT = """\
macOS / Linux (from the repo folder):
  python3 ui/bridge.py --serve                                   # open http://127.0.0.1:8765/ ; the page can start runs
  python3 ui/bridge.py --run demo --serve                        # scripted run, shown paced for display
  python3 ui/bridge.py --run live-short --model qwen2.5:7b-instruct --serve
  python3 ui/bridge.py --run models --parallel 2 --follow-model qwen2.5-7b-instruct --serve
  python3 ui/bridge.py --batch results/models/<STAMP> --follow-model <slug> --serve
  python3 ui/bridge.py --replay results/governance/<run>/progress.jsonl --speed 4 --loop
  python3 ui/bridge.py --bake results/governance/<run>/progress.jsonl      # writes ui/data/replay.js
  python3 ui/bridge.py --follow governance_out/cli_demo | --follow-latest

Windows PowerShell (from the repo folder; the repo's .venv is found by itself, else add --python PATH):
  py ui\\bridge.py --serve
  py ui\\bridge.py --run demo --serve
  py ui\\bridge.py --run live-short --model qwen2.5:7b-instruct --serve
  py ui\\bridge.py --run models --parallel 2 --serve
  py ui\\bridge.py --replay results\\governance\\<run>\\progress.jsonl --speed 4 --loop
  py ui\\bridge.py --bake results\\governance\\<run>\\progress.jsonl
  (Stopping a run with Ctrl-C uses CTRL_BREAK_EVENT: not verified on Windows.)

Run keys: demo | live-short (2 tests per split, 1 rep) | live-smoke (1 test, 3 reps) | live-full (26 tests, 3 reps; needs --model) | models.
After a run the bridge calls ui/export.py --quiet --gov <run folder> (--no-export skips it).
"""


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number")
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def _non_negative_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number")
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def _parallel(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number")
    if not 1 <= value <= 4:
        raise argparse.ArgumentTypeError("must be from 1 to 4")
    return value


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a port number")
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError("a port is a number from 0 to 65535")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bridge.py", description="Feed the pipeline screen from a run's progress.jsonl "
        "(writes ui/data/live.js, or ui/data/replay.js with --bake).",
        epilog=USAGE_TEXT, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--follow", metavar="PATH|OUT_DIR", help="follow a log (or a run folder) as it grows")
    mode.add_argument("--follow-latest", action="store_true", help="follow the newest log under the usual run folders")
    mode.add_argument("--replay", metavar="PATH", help="play a finished log back")
    mode.add_argument("--bake", metavar="PATH", help="write ui/data/replay.js (at most 120 key frames) for file://")
    mode.add_argument("--run", choices=RUN_KEYS, metavar="KEY",
                      help="start a run from the fixed list: " + ", ".join(RUN_KEYS))
    mode.add_argument("--batch", metavar="DIR", help="follow a model batch folder (results/models/<STAMP>)")
    p.add_argument("--model", metavar="TAG", default=None,
                   help="with --run demo|live-short|live-smoke|live-full: a tag listed in config/models.txt")
    p.add_argument("--parallel", type=_parallel, default=None, metavar="N", help="with --run models: 1 to 4 (default 2)")
    p.add_argument("--follow-model", metavar="SLUG", default=None,
                   help="with --run models or --batch: the model whose pipeline is shown")
    p.add_argument("--no-export", action="store_true", help="with --run or --serve: do not run ui/export.py afterwards")
    p.add_argument("--python", metavar="PATH", default=None,
                   help="with --run or --serve: the interpreter to use when the repo has no .venv")
    p.add_argument("--root", metavar="DIR", default=None, help=argparse.SUPPRESS)
    p.add_argument("--speed", type=_positive_float, default=DEFAULT_SPEED, metavar="N",
                   help="replay speed: recorded gaps divided by N, at most 3 s each (default 4)")
    p.add_argument("--loop", action="store_true", help="with --replay: start again after a pause")
    p.add_argument("--serve", action="store_true",
                   help="also serve the ui/ folder on http://127.0.0.1:PORT/ (this computer only)")
    p.add_argument("--port", type=_port, default=None, metavar="PORT", help="with --serve (default 8765)")
    p.add_argument("--data-dir", metavar="DIR", default=None, help="folder for live.js and replay.js (default ui/data)")
    p.add_argument("--pace-scale", type=_non_negative_float, default=1.0, help=argparse.SUPPRESS)
    return p


def _say(text: str) -> None:
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def _run_follow(args, data_dir: Path) -> int:
    writer = LiveWriter(data_dir / "live.js")
    session = Session(writer, kind="follow", scale=args.pace_scale)
    follower = Follower(session, args.follow, latest_root=REPO_ROOT if args.follow_latest else None)
    _say("bridge: following " + (session.log or "the newest log under governance_out and results/governance")
         + ". Ctrl-C stops.")
    last = None
    try:
        while True:
            follower.step(time.monotonic())
            state = session.snapshot(time.monotonic())["state"]
            if state != last:
                _say(f"bridge: {state}")
                last = state
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        follower.step(time.monotonic(), force=True)
        _say("bridge: stopped")
    return 0


def _root_of(args) -> Path:
    return Path(args.root) if args.root else REPO_ROOT


def _run_flow(args, data_dir: Path, controller: Optional[RunController] = None) -> int:
    """--run KEY: check, (preflight), start the child, follow it, export, report."""
    root = _root_of(args)
    key = args.run
    if args.model:
        if key == "models":
            raise BridgeError("--model does not apply to --run models; use --follow-model SLUG to choose what to watch")
        check_model(root, args.model)
    py = resolve_python(root, args.python)
    parallel = args.parallel if args.parallel is not None else 2
    outcome: Optional[RunOutcome] = None
    error: Optional[str] = None
    if controller is not None:
        controller.begin_external(key, args.model)
    try:
        outcome = execute_run(root, py, key, args.model, parallel, args.no_export, data_dir, args.pace_scale,
                              follow_model=args.follow_model)
    except BridgeError as exc:
        error = str(exc)
        raise
    finally:
        if controller is not None:
            controller.end_external(key, args.model, outcome, error)
    if outcome.refused:
        print(f"bridge: not started: {outcome.refused}", file=sys.stderr)
    return outcome.code


def _run_batch_follow(args, data_dir: Path) -> int:
    root = _root_of(args)
    path = Path(args.batch)
    if not path.is_dir() and (root / path).is_dir():
        path = root / path
    if not path.is_dir():
        raise BridgeError(f"batch folder not found: {display_path(path, root)}")
    writer = LiveWriter(data_dir / "live.js")
    session = Session(writer, kind="batch", scale=args.pace_scale)
    follower = BatchFollower(session, root, batch_dir=path, follow_model=args.follow_model)
    _say(f"bridge: following the batch {display_path(path, root)}. Ctrl-C stops.")
    try:
        while True:
            follower.step(time.monotonic())
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        follower.step(time.monotonic(), force=True)
        _say("bridge: stopped")
    return 0


def _run_replay(args, data_dir: Path) -> int:
    path = resolve_log_path(args.replay)
    if not path.is_file():
        raise BridgeError(f"log not found: {display_path(path)}")
    events, bad = load_events(path)
    if not events:
        raise BridgeError("the log has no readable events")
    writer = LiveWriter(data_dir / "live.js")
    session = Session(writer, kind="replay", log=display_path(path), speed=args.speed, scale=args.pace_scale)
    session.malformed = bad
    _say(f"bridge: replaying {session.log} at {args.speed:g}x" + (", looping" if args.loop else "") + ". Ctrl-C stops.")
    try:
        replay_loop(session, events, loop=args.loop)
    except KeyboardInterrupt:
        _say("bridge: stopped")
    return 0


def _wait_for_ctrl_c() -> None:
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        _say("bridge: stopped")


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not (args.follow or args.follow_latest or args.replay or args.bake or args.run or args.batch or args.serve):
        parser.error("choose a mode: --follow, --follow-latest, --replay, --bake, --run, --batch or --serve")
    if args.loop and not args.replay:
        parser.error("--loop needs --replay")
    if args.port is not None and not args.serve:
        parser.error("--port needs --serve")
    if args.model and not args.run:
        parser.error("--model needs --run")
    if args.parallel is not None and args.run != "models":
        parser.error("--parallel needs --run models")
    if args.follow_model and not (args.run == "models" or args.batch):
        parser.error("--follow-model needs --run models or --batch")
    if args.follow_model and not RE_SLUG.match(args.follow_model):
        parser.error("--follow-model: that is not a model slug (letters, digits, . _ - only)")
    if (args.no_export or args.python) and not (args.run or args.serve):
        parser.error("--no-export and --python need --run or --serve")
    data_dir = Path(args.data_dir) if args.data_dir else DATA_DIR
    server = None
    controller: Optional[RunController] = None
    try:
        clean_stale_temp(data_dir)
        if args.serve:
            # The page may start runs when nothing else owns live.js: --serve alone, or with --run or --bake.
            if not (args.follow or args.follow_latest or args.replay or args.batch):
                controller = RunController(root=_root_of(args), data_dir=data_dir, python=args.python,
                                           no_export=args.no_export, scale=args.pace_scale)
            server = make_server(DEFAULT_PORT if args.port is None else args.port, controller=controller)
            serve_in_thread(server)
            _say(f"bridge: serving ui/ at http://{SERVE_HOST}:{server.server_address[1]}/ "
                 "(this computer only). Ctrl-C stops."
                 + (" The page can start runs." if controller is not None else ""))
        rc = 0
        if args.bake:
            out = data_dir / "replay.js"
            frames = bake(args.bake, out, speed=args.speed, scale=args.pace_scale)
            _say(f"bridge: wrote {display_path(out)} ({frames} frames)")
        elif args.run:
            rc = _run_flow(args, data_dir, controller)
        elif args.batch:
            rc = _run_batch_follow(args, data_dir)
        elif args.replay:
            rc = _run_replay(args, data_dir)
        elif args.follow or args.follow_latest:
            rc = _run_follow(args, data_dir)
        if server is not None and rc != 130 and not (args.follow or args.follow_latest or args.batch):
            _wait_for_ctrl_c()              # bake, replay and run end by themselves; keep the page served
        return rc
    except BridgeError as exc:
        print(f"bridge: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("bridge: stopped", file=sys.stderr)
        return 0
    finally:
        if controller is not None:
            controller.shutdown()           # a run started from the page must not outlive the bridge
        if server is not None:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
