#!/usr/bin/env python3
"""ui/bridge.py - follow a pipeline run's progress.jsonl and feed the pipeline screen.

Standard library only (Python 3.9 or newer), cross-platform paths.

The pipeline run (scripts/run_governance.py) appends one JSON object per line to
<out>/progress.jsonl. This bridge reads those lines, keeps a small state, and
rewrites ui/data/live.js (window.LIVE, schema ui-live/1) so the page can draw it.
The page never starts a command and never computes a number: every number in
live.js is formatted here from a backend value.

This file grows in steps. Step 1 (this one): tailing and parsing the log.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional, Tuple

SCHEMA_LIVE = "ui-live/1"
SCHEMA_REPLAY = "ui-replay/1"
LOG_NAME = "progress.jsonl"

# A progress line is a few hundred bytes (the largest, a gate or a risk register,
# a few kilobytes). A "line" longer than this is a corrupt file, not an event.
MAX_LINE_BYTES = 1_000_000
# Bytes read from the file per poll. A bigger backlog is read over several polls.
MAX_READ_BYTES = 4_000_000
# The first bytes of the file, kept to notice that a different file took its place.
HEAD_BYTES = 64


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


if __name__ == "__main__":  # pragma: no cover - the command line arrives in a later step
    raise SystemExit("ui/bridge.py: command line not built yet")
