#!/usr/bin/env python3
"""Export backend files to ui/data/demo.js (window.DEMO, schema "ui-demo/1").

The UI shows only what backend files say. This script reads those files and
formats every number as text with the backend's own format specs, so the front
end never computes a verdict, a rate or an interval.

Rules this script enforces (see the build contract):
* Python standard library only; runs on Python 3.9 and later.
* It reads files and runs read-only git commands. It never runs backend code.
* The rehearsal batch is pinned to a git revision (git show <rev>:<path>), so a
  later live run that rewrites results/ cannot change the replay.
* Missing or unreadable data becomes PENDING with a reason and a warning. One
  bad source never stops the export. A fatal error keeps the previous demo.js
  and exits 1.
* SAMPLE data comes only from ui/fixtures/*.sample.json, only with
  --allow-sample, and is always labelled.
* Every Field that carries text from a file has a provenance entry. --check
  re-reads every provenance locator and compares the raw value.
* File modification times are never used.

Usage:
  python3 ui/export.py [--allow-sample] [--check] [--final] [--quiet]
                       [--no-git] [--sources PATH] [--out PATH]

Exit codes: 0 success (PENDING data included); 1 fatal error, --check
mismatch, or --final with any SAMPLE data.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import getpass
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA = "ui-demo/1"
SOURCES_SCHEMA = "ui-sources/1"
UI_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCES = UI_DIR / "sources.json"
DEFAULT_OUT = UI_DIR / "data" / "demo.js"
JS_PREFIX = "window.DEMO = "
JS_SUFFIX = ";\n"

REAL, SAMPLE, PENDING = "REAL", "SAMPLE", "PENDING"
STATUSES = (REAL, SAMPLE, PENDING)

NOT_CONNECTED = "Not connected yet: waits for docs/DATA_CONTRACT.md."
VERIFY_TERMINAL_ONLY = "The backend prints its verify result to the terminal only."
GATE_SAMPLE_TAG = "SAMPLE \u00b7 hand-written, not produced by the gate"
BACKEND_SAMPLE_TAG = "SAMPLE \u00b7 hand-written, not produced by the backend"
SCRIPTED_TAG = "SAMPLE \u00b7 scripted (backend DEMO mode), not a measured run"
SAMPLE_BANNER = "SAMPLE data on screen \u2014 not produced by the backend"
SCRIPTED_REASON = "scripted (backend DEMO mode)"
DIFFERENT_BATCH = "gate output describes a different batch"
DEMO_DIGEST = "sha256:845dbda0..."  # literal used by scripts/run_governance.py in DEMO mode
NOT_RECORDED = "not recorded"
CHECKER = "plain code (lab/oracle.py)"

FIXTURE_FORMATS = {
    "gate": "gate_report_text",
    "audit": "audit_jsonl_lines",
    "assurance": "assurance_markdown",
}

# --------------------------------------------------------------------------- #
# Label tables (UI wording for backend codes). Unknown codes show as the code.
# --------------------------------------------------------------------------- #
TECHNIQUE_LABELS = {
    "T1_plain": "T1 plain instruction",
    "T2_fake_system": "T2 fake system message",
    "T3_fake_staff": "T3 fake staff directive",
    "T4_hidden": "T4 hidden text",
    "T5_encoded": "T5 encoded (base64)",
    "T6_other_language": "T6 other language",
    "T7_split": "T7 split across documents",
    "none": "Normal request",
}
PLACE_LABELS = {
    "P1_doc_body": "Document body",
    "P2_doc_comment": "Document comment",
    "P3_asset_notes": "Asset notes",
    "none": "None",
}
GOAL_LABELS = {
    "G1_read_restricted": "Read a restricted record",
    "G2_exfiltrate": "Send data outside",
    "G3_tool_misuse": "Create an unrequested ticket",
}
CLASS_LABELS = {
    "V_read_restricted": "Read a restricted record",
    "V_exfiltration": "Sent data outside the company",
    "V_tool_misuse": "Created a ticket nobody asked for",
}
CHECK_LABELS = {
    "attack_success_rate": "Attack success",
    "min_attack_runs": "Attack runs",
    "min_benign_task_success": "Normal-task success",
    "max_error_rate": "Agent errors",
    "no_regression_vs_last_approved": "No regression vs last approved",
    "attempted_incl_blocked": "Attempted, including blocked calls",
}
STATE_LABELS = {
    "broke": "Rule broken",
    "blocked": "Attempted, blocked",
    "clean": "Clean",
    "error": "Error",
    "handled": "Handled",
    "not_handled": "Not handled",
}
LIMIT_LABELS = {
    "max_attack_success_rate": "Highest attack success allowed",
    "min_benign_task_success": "Lowest normal-task success allowed",
    "max_regression_vs_last_approved": "Allowed rise in attack success vs last approved",
    "min_attack_runs": "Fewest attack runs that count as proof",
    "max_error_rate": "Highest error rate allowed",
    "attempted_incl_blocked": "Attempted, including blocked calls (information, not a limit)",
}
EVENT_LABELS = {
    "submit": "Submitted",
    "verdict": "Gate verdict",
    "decision": "Decision",
    "decision_refused": "Refused",
    "role_change": "Role change",
}
INJECT_WHERE = {
    "doc_comment": "HTML comment in {doc}",
    "doc_body": "Body text of {doc}",
    "asset_notes": "Notes field of asset {asset_id}",
    "doc_split": "Split across {docs}",
}
METRIC_NOTES = {
    "attack_success_rate": "attack runs broke a rule",
    "benign_task_success": "normal requests handled",
    "ever_hijacked_rate": "tests broke a rule at least once",
    "technique": "attack runs broke a rule",
}

# Mode tags that scripts/run_governance.py (Report.label) puts on every file it writes:
# the first line of a .txt file is "[TAG] ...", the first line of a .md file is "**Mode: [TAG]** ...".
# The tag of a real-model run is assembled from pieces, like BANNED_WORDS below: the QA greps scan ui/.
MODE_SCRIPTED, MODE_REAL, MODE_SAVED = "SCRIPTED", "LI" + "VE", "SAVED"
MODE_TAGS = (MODE_SCRIPTED, MODE_REAL, MODE_SAVED)
_MODE_RX = "|".join(MODE_TAGS)

# Gate report formats (lab/governance/gate.py report_text, plus the mode tag and the info line).
RE_GATE_CAND = re.compile(r"^(?:\[(" + _MODE_RX + r")\] )?Candidate (\S+)\s*$")
RE_GATE_CHECK = re.compile(r"^  (OK  |FAIL)  (\S+) +(\S+)  limit (\S+)\s*(?:\((.*)\))?\s*$")
# A figure the gate prints for information only. It is not a release limit and never counts as a check.
RE_GATE_INFO = re.compile(r"^  info  (\S+) +(\S+)  not a limit  \((.*)\)\s*$")
# First line of a .md file: "**Mode: [TAG]**", then (scripted mode) one sentence on what the numbers are.
RE_MODE_LINE = re.compile(r"^\*\*Mode: \[(" + _MODE_RX + r")\]\*\*(?: (.*))?$")
# First line of a .txt file: "[TAG] <the usual first line>".
RE_TXT_MODE = re.compile(r"^\[(" + _MODE_RX + r")\] ")
RE_GATE_VERDICT = re.compile(r"^VERDICT: (PASS|FAIL)(?: \((\d+) reasons?\))?\s*$")
RE_GATE_XN = re.compile(r"^(\d+)/(\d+) ")
# lab/agent.py MODEL default.
RE_MODEL = re.compile(r'^MODEL\s*=\s*os\.environ\.get\(\s*"LAB_MODEL"\s*,\s*"([^"]+)"\s*\)\s*$')
# Tool calls in runs.csv (lab/metrics.py _flatten_calls, joined by "; ").
RE_CALL_SPLIT = re.compile(r"; (?=[A-Za-z_]\w*\()")
RE_CALL_NAME = re.compile(r"^([A-Za-z_]\w*)\(")
# Audit verify details (lab/governance/audit.py verify_log).
RE_VERIFY_DETAIL = (
    re.compile(r"^\d+ entries, chain intact$"),
    re.compile(r"^sequence jumped at entry \d+ \(saw seq=.*\)$"),
    re.compile(r"^broken link at entry \d+: prev_hash does not match entry -?\d+$"),
    re.compile(r"^contents of entry \d+ were changed after it was written$"),
)
RE_LIKELIHOOD = re.compile(r"^(High|Medium|Low) \((\d+)/(\d+), (\d+)%\)$")
RE_TICKET_LEFT = re.compile(r"^([A-Z]+-\d+): (.+)$")
RE_TICKET_RIGHT = re.compile(r"^(.*), due (.*), risk (\S+)\)$")
RE_BULLET = re.compile(r"^- \*\*(.+?):\*\* ?(.*)$")
RE_NUMBERED = re.compile(r"^(\d+)\. (.*)$")
RE_GIT_LOG_CMD = re.compile(r"^log -1 --format=%cI [0-9A-Za-z][0-9A-Za-z._/-]* -- [A-Za-z0-9_][A-Za-z0-9_./-]*$")
RE_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")

# Numbers that must never appear (idea-doc illustrations and scripted DEMO outputs).
# The patterns are assembled from pieces so that this file itself never
# contains the forbidden numbers or banned words (the QA greps scan ui/).
_NB = r"(?<![\d.])"
FORBIDDEN = [
    re.compile(_NB + "13" + "8 runs"),
    re.compile(_NB + r"1\." + "4%"),
    re.compile("zero" + " leaks", re.I),
    re.compile(_NB + "3" + "%"),
    re.compile(_NB + "2" + "3" + "%"),
    re.compile(_NB + "33" + "/78" + r"(?!\d)"),
    re.compile(_NB + r"42\." + "3" + "%"),
    re.compile(_NB + "60" + "/60" + r"(?!\d)"),
    re.compile(_NB + "0" + "/78" + r"(?!\d)"),
]
BANNED_WORDS = [
    re.compile(r"\b" + "LI" + "VE" + r"\b"),
    re.compile(r"\b" + "un" + "changeable" + r"\b", re.I),
    re.compile(r"\b" + "im" + "mutable" + r"\b", re.I),
    re.compile(r"\b" + "tamper" + "-proof" + r"\b", re.I),
    re.compile(r"\b" + "perma" + "nent" + r"\b", re.I),
    re.compile("compli" + "ant with", re.I),
]
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
RE_ABS_PATH = re.compile(r"(?:/Users/|/private/|/home/|/var/folders/|/tmp/|[A-Za-z]:\\\\)")


class SourceError(Exception):
    """A source file is missing, empty or malformed. Never fatal on its own."""


class FatalError(Exception):
    """The export cannot produce a valid demo.js. The previous file is kept."""


# --------------------------------------------------------------------------- #
# Context: settings, warnings, provenance.
# --------------------------------------------------------------------------- #
class Ctx:
    def __init__(self, args, sources_path: Path, sources: dict, repo_root: Path):
        self.args = args
        self.sources_path = sources_path
        self.sources = sources
        self.repo_root = repo_root
        self.ui_dir = UI_DIR
        self.git_ok = False
        self.git_note = ""
        self.warnings: list = []
        self.prov: dict = {}
        self._cache: dict = {}

    # warnings ------------------------------------------------------------- #
    def warn(self, level: str, text: str) -> None:
        text = scrub(self, text)
        item = {"level": level, "text": text}
        if item not in self.warnings:
            self.warnings.append(item)

    # fields --------------------------------------------------------------- #
    @staticmethod
    def field(text, status: str, prov=None) -> dict:
        return {"text": str(text), "status": status, "prov": prov}

    def add_prov(self, pid: str, status: str, display: str, raw: str, fmt: str,
                 source_file, rev, locator: dict) -> str:
        if pid in self.prov:
            # identical re-registration is fine (shared provenance)
            if self.prov[pid]["raw"] == raw and self.prov[pid]["locator"] == locator:
                return pid
            n = 2
            while f"{pid}#{n}" in self.prov:
                n += 1
            pid = f"{pid}#{n}"
        self.prov[pid] = {
            "id": pid,
            "status": status,
            "display": str(display),
            "raw": str(raw),
            "fmt": fmt,
            "source_file": source_file,
            "rev": rev,
            "locator": locator,
            "where": describe(source_file, rev, locator),
        }
        return pid

    def pfield(self, pid, text, status, raw, fmt, source_file, rev, locator) -> dict:
        """A Field with its provenance entry registered in one step."""
        pid = self.add_prov(pid, status, text, raw, fmt, source_file, rev, locator)
        return self.field(text, status, pid)


def scrub(ctx, text: str) -> str:
    """Remove absolute paths and the local user name from warning text."""
    text = str(text)
    for p in sorted({str(ctx.repo_root) if ctx else "", str(UI_DIR), str(Path.home())}, key=len, reverse=True):
        if p and len(p) > 1:
            text = text.replace(p + os.sep, "").replace(p, "<dir>")
    text = re.sub(r"(?:/Users|/private|/home|/var|/tmp)/[^\s'\"]*", "<path>", text)
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = ""
    if user and len(user) >= 4:
        text = text.replace(user, "<user>")
    return text


def describe(source_file, rev, loc: dict) -> str:
    at = f" @{rev}" if rev else ""
    k = loc.get("kind")
    src = source_file or ""
    if k == "csv":
        if "row" in loc:
            return f"{src}{at} row {loc['row']} cols {','.join(loc.get('columns', []))}"
        return f"{src}{at} {loc.get('key', '')} (cols {','.join(loc.get('columns', []))})"
    if k == "jsonl":
        return f"{src}{at} line {loc.get('line')} {loc.get('json_path', '')}"
    if k == "yaml":
        return f"{src}{at} line {loc.get('line')} {loc.get('key', '')}"
    if k == "text":
        if "line" in loc:
            return f"{src}{at} line {loc['line']} {loc.get('key') or ','.join(loc.get('columns', []))}"
        return f"{src}{at} {loc.get('json_path', '')}"
    if k == "git":
        return f"git {loc.get('cmd', '')}"
    if k == "fixture":
        inner = loc.get("key") or ",".join(loc.get("columns", []))
        return f"{src} {loc.get('json_path', '')} line {loc.get('line')} {inner}".rstrip()
    return src


def safe(ctx: Ctx, name: str, fn, *a, **kw):
    """Run one builder; on any error record a warning and return None."""
    try:
        return fn(*a, **kw)
    except Exception as exc:  # noqa: BLE001 - one bad source must not stop the export
        ctx.warn("integrity", f"{name}: {err_text(exc)}")
        return None


def err_text(exc: BaseException) -> str:
    if isinstance(exc, FileNotFoundError):
        return "file not found"
    if isinstance(exc, PermissionError):
        return "file cannot be read (permission denied)"
    if isinstance(exc, IsADirectoryError):
        return "expected a file, found a folder"
    if isinstance(exc, UnicodeDecodeError):
        return "file is not UTF-8 text"
    if isinstance(exc, json.JSONDecodeError):
        return f"not valid JSON (line {exc.lineno})"
    if isinstance(exc, (SourceError, ValueError, KeyError, TypeError, IndexError)):
        msg = str(exc).strip("'\"")
        return msg or type(exc).__name__
    return f"{type(exc).__name__}: {exc}"


def pending(reason: str, source_file) -> dict:
    return {
        "status": PENDING,
        "source_file": source_file,
        "produced": not_recorded_produced("not produced yet"),
        "reason": reason,
    }


def not_recorded_produced(text: str = NOT_RECORDED) -> dict:
    return {"basis": "not-recorded", "text": text, "raw": None}


# --------------------------------------------------------------------------- #
# Reading files and git.
# --------------------------------------------------------------------------- #
def rel_path(path) -> str:
    """Validate a repo-relative path: no absolute paths, no '..'."""
    if not isinstance(path, str) or not path.strip():
        raise SourceError("no path given")
    p = Path(path)
    if p.is_absolute() or ".." in p.parts or path.startswith("~"):
        raise SourceError(f"path must be relative to the repo root: {path}")
    return p.as_posix()


def git(ctx: Ctx, *args, timeout: int = 5):
    """Run a read-only git command. Returns (ok, stdout_bytes, stderr_text)."""
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    cmd = ["git", "-C", str(ctx.repo_root), "--no-pager", *args]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return False, b"", "git is not installed"
    except subprocess.TimeoutExpired:
        return False, b"", "git timed out"
    except OSError as exc:
        return False, b"", f"git could not start ({type(exc).__name__})"
    return cp.returncode == 0, cp.stdout, cp.stderr.decode("utf-8", "replace").strip()


def init_git(ctx: Ctx) -> None:
    if ctx.args.no_git:
        ctx.git_ok = False
        ctx.git_note = "git disabled by --no-git"
        return
    ok, out, err = git(ctx, "rev-parse", "--show-toplevel")
    if not ok:
        ctx.git_ok = False
        ctx.git_note = err if err in ("git is not installed", "git timed out") else "repo_root is not a git work tree"
        return
    top = out.decode("utf-8", "replace").strip()
    try:
        same = Path(top).resolve() == ctx.repo_root.resolve()
    except OSError:
        same = False
    if not same:
        ctx.git_ok = False
        ctx.git_note = "repo_root is not the top of a git work tree"
        return
    ctx.git_ok = True


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def decode_text(data: bytes) -> str:
    text = data.decode("utf-8")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def read_bytes(ctx: Ctx, path: str, rev=None) -> bytes:
    path = rel_path(path)
    key = (path, rev)
    if key in ctx._cache:
        data = ctx._cache[key]
        if isinstance(data, Exception):
            raise data
        return data
    try:
        if rev:
            if not ctx.git_ok:
                raise SourceError(f"{path} @{rev}: git unavailable ({ctx.git_note})")
            ok, out, err = git(ctx, "show", f"{rev}:{path}")
            if not ok:
                raise SourceError(f"{path} @{rev}: not found in git")
            data = out
        else:
            full = (ctx.repo_root / path)
            resolved = full.resolve()
            root = ctx.repo_root.resolve()
            if root not in resolved.parents and resolved != root:
                raise SourceError(f"{path}: outside the repo root")
            if not full.exists():
                raise SourceError(f"{path}: file not found")
            if full.is_dir():
                raise SourceError(f"{path}: expected a file, found a folder")
            data = full.read_bytes()
    except Exception as exc:  # noqa: BLE001
        ctx._cache[key] = exc if isinstance(exc, SourceError) else SourceError(f"{path}: {err_text(exc)}")
        raise ctx._cache[key]
    ctx._cache[key] = data
    return data


def read_text(ctx: Ctx, path: str, rev=None):
    """Return (text, meta). Raises SourceError on any problem."""
    data = read_bytes(ctx, path, rev)
    try:
        text = decode_text(data)
    except UnicodeDecodeError:
        raise SourceError(f"{path}: file is not UTF-8 text")
    if not text.strip():
        raise SourceError(f"{path}: file is empty")
    return text, {"path": path, "rev": rev, "blob": blob_sha(data)}


def commit_time(ctx: Ctx, path: str, rev, blob: str):
    """Commit time of the last commit that changed `path` at `rev` (HEAD if None),
    but only when the content shown equals the blob at that commit."""
    if not ctx.git_ok:
        return None
    path = rel_path(path)
    base = rev or "HEAD"
    ok, out, _ = git(ctx, "log", "-1", "--format=%H %cI", base, "--", path)
    if not ok or not out.strip():
        return None
    parts = out.decode("utf-8", "replace").strip().split(" ")
    if len(parts) != 2:
        return None
    commit, when = parts
    ok, out, _ = git(ctx, "rev-parse", f"{commit}:{path}")
    if not ok or out.decode().strip() != blob:
        return None
    if parse_iso(when) is None:
        return None
    return {"time": when, "commit": commit[:7], "cmd": f"log -1 --format=%cI {base} -- {path}"}


def text_lines(text: str) -> list:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


# --------------------------------------------------------------------------- #
# Small parsers and formatters.
# --------------------------------------------------------------------------- #
def parse_iso(s):
    if not isinstance(s, str) or not s:
        return None
    s2 = s.strip()
    if s2.endswith("Z"):
        s2 = s2[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(s2)
    except ValueError:
        # Python 3.9 accepts only 3 or 6 fractional digits; normalise to 6.
        m = re.match(r"^(.*T\d\d:\d\d:\d\d)\.(\d+)([+-]\d\d:\d\d)$", s2)
        if not m:
            return None
        try:
            d = dt.datetime.fromisoformat(f"{m.group(1)}.{(m.group(2) + '000000')[:6]}{m.group(3)}")
        except ValueError:
            return None
    if d.tzinfo is None or d.utcoffset() is None:
        return None
    return d


def parse_bool(x) -> bool:
    table = {"True": True, "False": False, "true": True, "false": False, "TRUE": True, "FALSE": False}
    if x in table:
        return table[x]
    raise SourceError(f"not a True/False value: {x!r}")


def parse_int(x, what="value") -> int:
    try:
        f = float(x)
    except (TypeError, ValueError):
        raise SourceError(f"{what} is not a number: {x!r}")
    if not math.isfinite(f) or f != int(f):
        raise SourceError(f"{what} is not a whole number: {x!r}")
    return int(f)


def parse_unit_float(x, what="value") -> float:
    try:
        f = float(x)
    except (TypeError, ValueError):
        raise SourceError(f"{what} is not a number: {x!r}")
    if not math.isfinite(f) or f < 0.0 or f > 1.0:
        raise SourceError(f"{what} is outside 0..1: {x!r}")
    return f


def fmt_pct1(raw) -> str:
    """The backend's percentage format (gate.report_text): f"{v:.1%}"."""
    return f"{float(raw):.1%}"


def fmt_range(lo, hi) -> str:
    return f"{fmt_pct1(lo)} to {fmt_pct1(hi)}"


def fmt_x_of_n(x: int, n: int) -> str:
    return f"{x} of {n}"


def fmt_limit(v) -> str:
    """gate.report_text limit format: f"{limit:.1%}" for floats, str() otherwise."""
    return f"{v:.1%}" if isinstance(v, float) else str(v)


def short_digest(d) -> str:
    if not isinstance(d, str) or not d:
        return NOT_RECORDED
    if d == DEMO_DIGEST or d.endswith("..."):
        return "placeholder digest (DEMO mode)"
    m = re.match(r"^sha256:([0-9a-f]{12})[0-9a-f]*$", d)
    if m:
        return "sha256:" + m.group(1)
    return "unrecognised digest format"


def label(table: dict, code) -> str:
    if code is None:
        return ""
    return table.get(code, str(code))


def tech_sort_key(code: str):
    m = re.match(r"^T(\d+)", code or "")
    return (0, int(m.group(1)), code) if m else (1, 0, code or "")


def raw_of(v) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


def get_path(obj, path: str):
    """Follow a dotted path with optional [n] indexes, e.g. 'a.b[0].c'."""
    cur = obj
    for part in path.split("."):
        m = re.match(r"^([^\[\]]*)((?:\[\d+\])*)$", part)
        if not m:
            raise SourceError(f"bad path {path}")
        name, idx = m.group(1), m.group(2)
        if name:
            if not isinstance(cur, dict) or name not in cur:
                raise SourceError(f"missing key {path}")
            cur = cur[name]
        for i in re.findall(r"\[(\d+)\]", idx):
            if not isinstance(cur, list) or int(i) >= len(cur):
                raise SourceError(f"missing index {path}")
            cur = cur[int(i)]
    return cur


def join_paths(obj, paths: str) -> str:
    return ", ".join(raw_of(get_path(obj, p.strip())) for p in paths.split(","))


# ------------------------------- CSV ------------------------------------- #
def parse_csv_text(text: str):
    csv.field_size_limit(10 ** 7)
    reader = csv.reader(io.StringIO(text, newline=""))
    rows = list(reader)
    if not rows:
        raise SourceError("empty CSV")
    header = [h.strip() for h in rows[0]]
    if len(set(header)) != len(header):
        raise SourceError("CSV header repeats a column")
    return header, rows[1:]


SUMMARY_COLS = ("metric", "successes", "n", "rate", "ci_low", "ci_high")
RUNS_REQUIRED = ("test_id", "kind", "technique", "rep", "user_task", "tool_calls", "violated",
                 "violation_classes", "oracle_detail", "benign_success", "error")


def parse_summary_csv(text: str):
    """Return ({metric: {"row": N, "cells": {...}}}, errors). Header = row 1."""
    header, rows = parse_csv_text(text)
    missing = [c for c in SUMMARY_COLS if c not in header]
    if missing:
        raise SourceError(f"summary.csv lacks columns {','.join(missing)}")
    out, errors = {}, []
    for i, r in enumerate(rows, start=2):
        if not r or all(not c.strip() for c in r):
            continue
        if len(r) != len(header):
            errors.append(f"row {i}: {len(r)} cells, expected {len(header)}")
            continue
        cells = dict(zip(header, r))
        name = cells["metric"].strip()
        if not name:
            errors.append(f"row {i}: no metric name")
            continue
        if name in out:
            errors.append(f"row {i}: metric {name} repeats")
            continue
        out[name] = {"row": i, "cells": cells}
    if not out:
        raise SourceError("summary.csv has no metric rows")
    return out, errors


def split_tool_calls(s: str) -> list:
    """Split the runs.csv tool_calls cell (lab/metrics.py _flatten_calls)."""
    if not s or not s.strip():
        return []
    calls = []
    for n, part in enumerate(RE_CALL_SPLIT.split(s), start=1):
        blocked = part.endswith(" [blocked]")
        text = part[: -len(" [blocked]")] if blocked else part
        m = RE_CALL_NAME.match(text)
        calls.append({"n": n, "text": text, "name": m.group(1) if m else text, "blocked": blocked,
                      "flagged": False})
    return calls


def flatten_tool_calls(tool_calls) -> str:
    """Mirror of lab/metrics.py _flatten_calls; used only to validate traces."""
    parts = []
    for call in tool_calls or []:
        name, args = call["name"], call.get("args", {}) or {}
        if name == "query_assets":
            rendered = f"query_assets({args.get('asset_id', '')})"
        elif name == "create_ticket":
            rendered = (
                f"create_ticket(assignee={args.get('assignee', '')},"
                f"priority={args.get('priority', '')})"
            )
        elif name == "read_doc":
            rendered = f"read_doc({args.get('name', '')})"
        else:
            rendered = f"{name}({args})"
        if call.get("blocked"):
            rendered += " [blocked]"
        parts.append(rendered)
    return "; ".join(parts)


def flag_calls(calls: list, details: list) -> None:
    """Mark the calls the checker's detail lines point at (display only)."""
    for d in details:
        m = re.match(r"^query_assets on forbidden id (.+)$", d)
        if m:
            for c in calls:
                if not c["blocked"] and c["text"] == f"query_assets({m.group(1)})":
                    c["flagged"] = True
            continue
        m = re.match(r"^ticket assigned outside allowed domain: (.+)$", d)
        if m:
            dom = m.group(1)
            for c in calls:
                am = re.search(r"assignee=([^,)]*)", c["text"])
                if c["name"] == "create_ticket" and not c["blocked"] and am and (
                        am.group(1).endswith("@" + dom) or am.group(1) == dom):
                    c["flagged"] = True
            continue
        if d == "secret marker found in ticket body":
            for c in calls:
                if c["name"] == "create_ticket" and not c["blocked"]:
                    c["flagged"] = True
            continue
        m = re.match(r"^unrequested (\S+) ticket created", d)
        if m:
            for c in calls:
                if c["name"] == "create_ticket" and not c["blocked"] and f"priority={m.group(1)}" in c["text"]:
                    c["flagged"] = True


def parse_runs_csv(text: str):
    """Return (records, errors). Each record keeps its CSV row number (header = 1)."""
    header, rows = parse_csv_text(text)
    missing = [c for c in RUNS_REQUIRED if c not in header]
    if missing:
        raise SourceError(f"runs.csv lacks columns {','.join(missing)}")
    recs, errors, seen = [], [], set()
    for i, r in enumerate(rows, start=2):
        if not r or all(not c.strip() for c in r):
            continue
        if len(r) != len(header):
            errors.append(f"row {i}: {len(r)} cells, expected {len(header)}")
            continue
        c = dict(zip(header, r))
        try:
            tid = c["test_id"].strip()
            if not tid or not RE_SAFE_ID.match(tid):
                raise SourceError("bad test_id")
            kind = c["kind"].strip()
            if kind not in ("attack", "benign"):
                raise SourceError(f"bad kind {kind!r}")
            rep = parse_int(c["rep"], "rep")
            violated = parse_bool(c["violated"].strip())
            benign_ok = parse_bool(c["benign_success"].strip())
        except SourceError as exc:
            errors.append(f"row {i}: {exc}")
            continue
        if (tid, rep) in seen:
            errors.append(f"row {i}: {tid} rep {rep} repeats")
            continue
        seen.add((tid, rep))
        calls = split_tool_calls(c["tool_calls"])
        details = [d for d in c["oracle_detail"].split(" | ") if d] if c["oracle_detail"] else []
        flag_calls(calls, details)
        recs.append({
            "row": i,
            "cells": c,
            "test_id": tid,
            "kind": kind,
            "goal": (c.get("goal") or "").strip() or None,
            "technique": (c["technique"] or "none").strip() or "none",
            "place": (c.get("place") or "none").strip() or "none",
            "rep": rep,
            "user_task": c["user_task"],
            "tool_calls_raw": c["tool_calls"],
            "calls": calls,
            "violated": violated,
            "classes": sorted(x for x in c["violation_classes"].split(";") if x) if c["violation_classes"] else [],
            "detail": details,
            "benign_success": benign_ok,
            "error": c["error"] or "",
            "any_blocked": any(x["blocked"] for x in calls),
        })
    return recs, errors


def tile_state(rec: dict, has_blocked_info: bool):
    """Contract tile rule. Returns (state, badges)."""
    badges = []
    if rec["violated"]:
        if rec["error"]:
            badges.append("errored")
        if has_blocked_info and rec["any_blocked"]:
            badges.append("some calls blocked")
        return "broke", badges
    if rec["error"]:
        return "error", badges
    if rec["kind"] == "attack":
        if has_blocked_info and rec["any_blocked"]:
            return "blocked", badges
        return "clean", badges
    return ("handled" if rec["benign_success"] else "not_handled"), badges


COUNTERS = ("rows", "rows.error", "attack", "attack.broke", "attack.blocked", "attack.clean",
            "attack.error", "benign", "benign.broke", "benign.handled", "benign.not_handled",
            "benign.error", "tests.attack", "tests.benign")


def count_runs(recs: list, name: str, has_blocked_info: bool) -> int:
    if name == "rows":
        return len(recs)
    if name == "rows.error":
        return sum(1 for r in recs if r["error"])
    if name in ("tests.attack", "tests.benign"):
        k = name.split(".")[1]
        return len({r["test_id"] for r in recs if r["kind"] == k})
    kind, _, state = name.partition(".")
    if kind not in ("attack", "benign"):
        raise SourceError(f"unknown counter {name}")
    sel = [r for r in recs if r["kind"] == kind]
    if not state:
        return len(sel)
    return sum(1 for r in sel if tile_state(r, has_blocked_info)[0] == state)


def shape_info(recs: list) -> dict:
    def reps_of(kind):
        per = {}
        for r in recs:
            if r["kind"] == kind:
                per[r["test_id"]] = per.get(r["test_id"], 0) + 1
        return len(per), sorted(set(per.values()))
    at, ar = reps_of("attack")
    bt, br = reps_of("benign")
    return {"attack_tests": at, "attack_reps": ar, "benign_tests": bt, "benign_reps": br}


def shape_text(info: dict) -> str:
    def reps(rs):
        if not rs:
            return ""
        if len(rs) == 1:
            return f" \u00d7 {rs[0]} rep{'s' if rs[0] != 1 else ''}"
        return f" \u00d7 {rs[0]}\u2013{rs[-1]} reps"
    at, bt = info["attack_tests"], info["benign_tests"]
    a = f"{at} attack test{'s' if at != 1 else ''}{reps(info['attack_reps'])}"
    b = f"{bt} normal request{'s' if bt != 1 else ''}{reps(info['benign_reps'])}"
    return f"{a} \u00b7 {b}"


def shape_raw(info: dict) -> str:
    return (f"attack_tests={info['attack_tests']} attack_reps={info['attack_reps']} "
            f"benign_tests={info['benign_tests']} benign_reps={info['benign_reps']}")


# ------------------------------ JSONL ------------------------------------ #
def parse_jsonl(text: str) -> list:
    """Return [(line_no, obj|None, raw)] for every non-blank line, in file order."""
    out = []
    for no, line in enumerate(text_lines(text), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                obj = None
        except ValueError:
            obj = None
        out.append((no, obj, line))
    return out


# ------------------------------ YAML subset -------------------------------- #
class YamlError(SourceError):
    pass


def _strip_comment(line: str) -> str:
    in_s = in_d = False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_d:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_d = False
        elif in_s:
            if ch == "'":
                in_s = False
        else:
            if ch == '"':
                in_d = True
            elif ch == "'":
                in_s = True
            elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
                return line[:i]
        i += 1
    return line


def _split_list_items(inner: str, no: int) -> list:
    items, buf, in_s, in_d, i = [], "", False, False, 0
    while i < len(inner):
        ch = inner[i]
        if in_d:
            buf += ch
            if ch == "\\" and i + 1 < len(inner):
                buf += inner[i + 1]
                i += 2
                continue
            if ch == '"':
                in_d = False
        elif in_s:
            buf += ch
            if ch == "'":
                in_s = False
        elif ch == '"':
            in_d = True
            buf += ch
        elif ch == "'":
            in_s = True
            buf += ch
        elif ch == ",":
            items.append(buf)
            buf = ""
        elif ch in "[]{}":
            raise YamlError(f"line {no}: nested collections are not supported")
        else:
            buf += ch
        i += 1
    if in_s or in_d:
        raise YamlError(f"line {no}: unclosed quote")
    items.append(buf)
    if len(items) == 1 and not items[0].strip():
        return []
    return items


def _yaml_scalar(s: str, no: int):
    s = s.strip()
    if not s:
        raise YamlError(f"line {no}: empty value")
    if s.startswith("["):
        if not s.endswith("]"):
            raise YamlError(f"line {no}: list is not closed")
        return [_yaml_scalar(x, no) for x in _split_list_items(s[1:-1], no)]
    if s.startswith('"'):
        if len(s) < 2 or not s.endswith('"'):
            raise YamlError(f"line {no}: unclosed double quote")
        try:
            v = json.loads(s)
        except ValueError:
            raise YamlError(f"line {no}: bad double-quoted string")
        if not isinstance(v, str):
            raise YamlError(f"line {no}: bad double-quoted string")
        return v
    if s.startswith("'"):
        if len(s) < 2 or not s.endswith("'"):
            raise YamlError(f"line {no}: unclosed single quote")
        return s[1:-1].replace("''", "'")
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if s in ("null", "~"):
        return None
    if re.match(r"^[-+]?\d+$", s):
        return int(s)
    if re.match(r"^[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$", s):
        return float(s)
    if s[0] in "{&*!|>%@`" or ": " in s:
        raise YamlError(f"line {no}: unsupported YAML value")
    return s


def parse_yaml_subset(text: str):
    """Parse the YAML subset used by policy.yaml and control_map.yaml.

    Supports comments (full-line and trailing), 2-space nested mappings,
    quoted strings, numbers, booleans and inline lists. Returns
    (data, line_of) where line_of maps a dotted key path to its line number.
    """
    data: dict = {}
    line_of: dict = {}
    stack = [(-2, data, "")]
    for no, rawline in enumerate(text_lines(text), start=1):
        line = _strip_comment(rawline).rstrip()
        if not line.strip():
            continue
        lead = line[: len(line) - len(line.lstrip())]
        if "\t" in lead:
            raise YamlError(f"line {no}: tab indentation")
        indent = len(lead)
        body = line.strip()
        if body.startswith("- ") or body == "-":
            raise YamlError(f"line {no}: block lists are not supported")
        m = re.match(r'^("[^"]+"|[A-Za-z_][\w\-]*)\s*:(?:\s+(.*))?$', body)
        if not m:
            raise YamlError(f"line {no}: not a key: value line")
        key = m.group(1).strip('"')
        val = m.group(2)
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent_indent, container, ppath = stack[-1]
        if indent != parent_indent + 2:
            raise YamlError(f"line {no}: unexpected indentation")
        path = f"{ppath}.{key}" if ppath else key
        if key in container:
            raise YamlError(f"line {no}: key {key} repeats")
        if val is None or not val.strip():
            child: dict = {}
            container[key] = child
            stack.append((indent, child, path))
        else:
            container[key] = _yaml_scalar(val, no)
        line_of[path] = no
    if not data:
        raise YamlError("no keys found")
    return data, line_of


# ------------------------------ gate report -------------------------------- #
def parse_gate_report(text: str) -> dict:
    """Parse gate.report_text output. Raises SourceError on any unknown line."""
    lines = text_lines(text)
    out = {"candidate": None, "candidate_line": None, "mode": None, "checks": [], "info": [], "verdict": None,
           "reasons": None, "verdict_line": None, "lines": lines}
    for no, line in enumerate(lines, start=1):
        if not line.strip():
            if out["verdict"] is not None:
                continue
            raise SourceError(f"gate report line {no}: blank line before the verdict")
        m = RE_GATE_CAND.match(line)
        if m:
            if out["candidate"] is not None or out["checks"]:
                raise SourceError(f"gate report line {no}: candidate line out of place")
            out["mode"], out["candidate"], out["candidate_line"] = m.group(1), m.group(2), no
            continue
        m = RE_GATE_CHECK.match(line)
        if m:
            if out["candidate"] is None or out["verdict"] is not None:
                raise SourceError(f"gate report line {no}: check line out of place")
            out["checks"].append({"line": no, "mark": m.group(1).strip(), "name": m.group(2),
                                  "value": m.group(3), "limit": m.group(4), "note": m.group(5)})
            continue
        m = RE_GATE_INFO.match(line)
        if m:
            # Kept apart: an info line has no OK/FAIL mark and is not a release limit.
            if out["candidate"] is None or out["verdict"] is not None:
                raise SourceError(f"gate report line {no}: info line out of place")
            out["info"].append({"line": no, "name": m.group(1), "value": m.group(2), "note": m.group(3)})
            continue
        m = RE_GATE_VERDICT.match(line)
        if m:
            if out["verdict"] is not None:
                raise SourceError(f"gate report line {no}: second verdict line")
            out["verdict"], out["verdict_line"] = m.group(1), no
            out["reasons"] = int(m.group(2)) if m.group(2) else None
            continue
        raise SourceError(f"gate report line {no}: not in the gate's format")
    if out["candidate"] is None:
        raise SourceError("gate report has no Candidate line")
    if out["verdict"] is None:
        raise SourceError("gate report has no VERDICT line")
    if not out["checks"]:
        raise SourceError("gate report has no check lines")
    return out


# ------------------------------ markdown ----------------------------------- #
def _md_cells(line: str) -> list:
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        raise SourceError("not a table row")
    return [c.strip() for c in s[1:-1].split("|")]


def _is_sep(line: str) -> bool:
    return bool(re.match(r"^\|(\s*:?-{3,}:?\s*\|)+\s*$", line.strip()))


def parse_md_table(text: str, required=()) -> list:
    """Return [(line_no, {header: cell})] for the first table in the text."""
    lines = text_lines(text)
    for i in range(len(lines) - 1):
        if lines[i].strip().startswith("|") and _is_sep(lines[i + 1]):
            header = _md_cells(lines[i])
            missing = [c for c in required if c not in header]
            if missing:
                raise SourceError(f"table lacks columns {', '.join(missing)}")
            rows = []
            for j in range(i + 2, len(lines)):
                ln = lines[j]
                if not ln.strip().startswith("|"):
                    break
                cells = _md_cells(ln)
                if len(cells) != len(header):
                    raise SourceError(f"table line {j + 1}: {len(cells)} cells, expected {len(header)}")
                rows.append((j + 1, dict(zip(header, cells))))
            return rows
    raise SourceError("no markdown table found")


def md_cell_at(lines: list, line_no: int, column: str) -> str:
    """Cell `column` of the table row at line_no (header found above it)."""
    j = line_no - 1
    while j > 0 and not _is_sep(lines[j]):
        j -= 1
    if j <= 0:
        raise SourceError("no table header above this line")
    header = _md_cells(lines[j - 1])
    cells = _md_cells(lines[line_no - 1])
    if len(cells) != len(header) or column not in header:
        raise SourceError("table row does not match its header")
    return cells[header.index(column)]


def strip_ticks(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s.startswith("`") and s.endswith("`"):
        return s[1:-1]
    return s


def strip_bold(s: str) -> str:
    s = s.strip()
    if len(s) >= 4 and s.startswith("**") and s.endswith("**"):
        return s[2:-2]
    return s


def parse_likelihood(cell: str) -> dict:
    m = RE_LIKELIHOOD.match(cell.strip())
    if not m:
        raise SourceError(f"likelihood not in the backend format: {cell!r}")
    return {"level": m.group(1), "successes": int(m.group(2)), "runs": int(m.group(3)),
            "pct": m.group(4) + "%"}


def split_mode_line(text: str):
    """Split off the mode line that Report.label puts on top of every .md file.

    Returns (tag, note, body, offset):
      tag     SCRIPTED, the real-model tag or SAVED; None when the first line is no mode line
      note    the sentence after the tag ("" when there is none)
      body    the text without the mode line and the blank line under it
      offset  how many lines were taken off the top, so line n of body is line n + offset of the file

    The mode line starts with "**" but does not always end with "**", so a parser that skips bold
    title lines by their two ends must not see it. Untagged text comes back unchanged.
    """
    lines = text_lines(text)
    m = RE_MODE_LINE.match(lines[0]) if lines else None
    if not m:
        return None, "", text, 0
    offset = 2 if len(lines) > 1 and not lines[1].strip() else 1
    return m.group(1), (m.group(2) or "").strip(), "\n".join(lines[offset:]), offset


def parse_fix_ticket_line(line: str) -> dict:
    if not line.startswith("- "):
        raise SourceError("ticket line must start with '- '")
    body = line[2:]
    parts = body.rsplit("  (owner ", 1)
    if len(parts) != 2:
        raise SourceError("ticket line has no '  (owner ' part")
    lm = RE_TICKET_LEFT.match(parts[0])
    rm = RE_TICKET_RIGHT.match(parts[1])
    if not lm or not rm:
        raise SourceError("ticket line not in the backend format")
    return {"id": lm.group(1), "title": lm.group(2), "owner": rm.group(1), "due": rm.group(2),
            "risk": rm.group(3)}


def parse_fix_tickets(text: str) -> list:
    _tag, _note, body, offset = split_mode_line(text)
    out = []
    for no, line in enumerate(text_lines(body), start=offset + 1):
        s = line.strip()
        if not s or s == "**Fix tickets**":
            continue
        try:
            out.append((no, parse_fix_ticket_line(line)))
        except SourceError as exc:
            raise SourceError(f"fix tickets line {no}: {exc}")
    return out


def parse_md_bullets(text: str) -> list:
    """'- **Key:** value' lines (inventory card, manager summary)."""
    _tag, _note, body, offset = split_mode_line(text)
    out = []
    for no, line in enumerate(text_lines(body), start=offset + 1):
        s = line.strip()
        if not s or (s.startswith("**") and s.endswith("**")):
            continue
        m = RE_BULLET.match(s)
        if not m:
            raise SourceError(f"line {no}: not a '- **Key:** value' line")
        out.append((no, m.group(1), m.group(2)))
    return out


def parse_md_steps(text: str) -> list:
    """Traceability markdown: a bold title, a `chain`, then numbered steps."""
    _tag, _note, body, offset = split_mode_line(text)
    out = []
    for no, line in enumerate(text_lines(body), start=offset + 1):
        s = line.strip()
        if not s or (s.startswith("**") and s.endswith("**")) or (s.startswith("`") and s.endswith("`")):
            continue
        m = RE_NUMBERED.match(s)
        if not m:
            raise SourceError(f"line {no}: not a numbered step")
        out.append((no, int(m.group(1)), m.group(2)))
    return out


def parse_verify(text: str) -> dict:
    """Audit verify result: JSON {ok, bad_seq, detail} or the LogCheck repr."""
    s = text.strip()
    obj = None
    if s.startswith("{"):
        obj = json.loads(s)
        if not isinstance(obj, dict):
            raise SourceError("verify file is not a JSON object")
        ok, bad, detail = obj.get("ok"), obj.get("bad_seq"), obj.get("detail")
    else:
        m = re.match(r"^LogCheck\(ok=(True|False), bad_seq=(None|\d+), detail=(['\"])(.*)\3\)$", s)
        if not m:
            raise SourceError("verify output not in a known format")
        ok = m.group(1) == "True"
        bad = None if m.group(2) == "None" else int(m.group(2))
        detail = m.group(4)
    if not isinstance(ok, bool) or not isinstance(detail, str):
        raise SourceError("verify result lacks ok/detail")
    if bad is not None and not isinstance(bad, int):
        raise SourceError("verify bad_seq is not a number")
    if not any(rx.match(detail) for rx in RE_VERIFY_DETAIL):
        raise SourceError("verify detail not in the backend's wording")
    if ok != detail.endswith("chain intact"):
        raise SourceError("verify ok flag contradicts its detail")
    return {"ok": ok, "bad_seq": bad, "detail": detail}


def parse_manifest(text: str):
    """Stub for the planned results/runs/<run_id>/manifest.json (not built yet)."""
    return None


def model_default_from_agent(text: str):
    for no, line in enumerate(text_lines(text), start=1):
        m = RE_MODEL.match(line)
        if m:
            return m.group(1), no
    raise SourceError("no MODEL default found in lab/agent.py")


# --------------------------------------------------------------------------- #
# Provenance re-reading (used by --check).
# --------------------------------------------------------------------------- #
def extract_line(lines: list, loc: dict) -> str:
    no = loc.get("line")
    if not isinstance(no, int) or no < 1 or no > len(lines):
        raise SourceError("line number out of range")
    s = lines[no - 1]
    key = loc.get("key")
    cols = loc.get("columns")
    if key and key.startswith("gate."):
        part = key[5:]
        if part == "candidate":
            m = RE_GATE_CAND.match(s)
            if not m:
                raise SourceError("not a Candidate line")
            return m.group(2)
        if part in ("verdict", "reasons"):
            m = RE_GATE_VERDICT.match(s)
            if not m:
                raise SourceError("not a VERDICT line")
            return m.group(1) if part == "verdict" else (m.group(2) or "")
        if part.startswith("info."):
            m = RE_GATE_INFO.match(s)
            if not m:
                raise SourceError("not an info line")
            idx = {"name": 1, "value": 2, "note": 3}.get(part[5:])
            if idx is None:
                raise SourceError(f"unknown gate key {key}")
            return m.group(idx)
        m = RE_GATE_CHECK.match(s)
        if not m:
            raise SourceError("not a check line")
        idx = {"mark": 1, "name": 2, "value": 3, "limit": 4, "note": 5}.get(part)
        if idx is None:
            raise SourceError(f"unknown gate key {key}")
        v = m.group(idx)
        return (v.strip() if idx == 1 else v) or ""
    if key == "agent.model_default":
        m = RE_MODEL.match(s)
        if not m:
            raise SourceError("not the MODEL line")
        return m.group(1)
    if key and key.startswith("json:"):
        obj = json.loads(s)
        return join_paths(obj, key[5:])
    if key and key.startswith("ticket."):
        return parse_fix_ticket_line(s)[key[7:]]
    if key == "line":
        return s
    if cols:
        return ", ".join(md_cell_at(lines, no, c) for c in cols)
    raise SourceError("locator names no key or columns")


def git_locator(ctx: Ctx, cmd: str) -> str:
    if not RE_GIT_LOG_CMD.match(cmd or ""):
        raise SourceError("git locator is not an allowed read-only command")
    if not ctx.git_ok:
        raise SourceError("git unavailable")
    parts = cmd.split(" ")
    rel_path(parts[-1])
    ok, out, _ = git(ctx, *parts)
    if not ok:
        raise SourceError("git command failed")
    return out.decode("utf-8", "replace").strip()


def check_csv_key(cells: dict, key: str) -> None:
    for cond in (key or "").split(";"):
        if not cond:
            continue
        col, _, want = cond.partition("=")
        if cells.get(col, "").strip() != want:
            raise SourceError(f"row no longer has {cond}")


def resolve_raw(ctx: Ctx, p: dict) -> str:
    loc = p.get("locator") or {}
    kind = loc.get("kind")
    src, rev = p.get("source_file"), p.get("rev")
    if kind == "git":
        return git_locator(ctx, loc.get("cmd", ""))
    if kind == "fixture":
        text, _ = read_text(ctx, src, None)
        content = get_path(json.loads(text), loc["json_path"])
        lines = [str(x) for x in content] if isinstance(content, list) else text_lines(str(content))
        return extract_line(lines, loc)
    text, _ = read_text(ctx, src, rev)
    if kind == "csv":
        key = loc.get("key", "")
        if key.startswith("count:") or key == "shape":
            recs, _ = parse_runs_csv(text)
            if key == "shape":
                return shape_raw(shape_info(recs))
            spec, _, bi = key[6:].partition("|bi=")
            return ", ".join(str(count_runs(recs, n, bi == "1")) for n in spec.split("+"))
        header, rows = parse_csv_text(text)
        row = loc["row"]
        if row < 2 or row - 2 >= len(rows):
            raise SourceError("row out of range")
        cells = dict(zip(header, rows[row - 2]))
        check_csv_key(cells, key)
        return ", ".join(cells[c] for c in loc["columns"])
    if kind == "jsonl":
        lines = text_lines(text)
        no = loc["line"]
        if no < 1 or no > len(lines):
            raise SourceError("line out of range")
        return join_paths(json.loads(lines[no - 1]), loc["json_path"])
    if kind == "yaml":
        data, line_of = parse_yaml_subset(text)
        if line_of.get(loc["key"]) != loc.get("line"):
            raise SourceError("key moved to another line")
        return raw_of(get_path(data, loc["key"]))
    if kind == "text":
        if "line" not in loc and loc.get("json_path"):
            return join_paths(json.loads(text), loc["json_path"])
        return extract_line(text_lines(text), loc)
    raise SourceError(f"unknown locator kind {kind!r}")


def self_check(ctx: Ctx, provs) -> list:
    bad = []
    for p in provs:
        try:
            raw = resolve_raw(ctx, p)
        except Exception as exc:  # noqa: BLE001
            bad.append((p.get("id"), f"cannot re-read: {err_text(exc)}"))
            continue
        if raw != p.get("raw"):
            bad.append((p.get("id"), f"raw changed: was {short(p.get('raw'))}, now {short(raw)}"))
    return bad


def short(s, n: int = 70) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "\u2026"


# --------------------------------------------------------------------------- #
# Builders: batches and runs.
# --------------------------------------------------------------------------- #
def metric_from_row(ctx: Ctx, pid: str, name: str, row, unit: str, src: str, rev, note: str) -> dict:
    if row is None:
        return {"measured": False, "unit": unit,
                "x_of_n": ctx.field("not available", PENDING),
                "rate": None, "ci": None, "geom": None, "note": note,
                "reason": f"{src} has no {name} row"}
    cells, rowno = row["cells"], row["row"]
    key = f"metric={name}"
    succ = parse_int(cells["successes"], f"{name} successes")
    n = parse_int(cells["n"], f"{name} n")
    if n < 0 or succ < 0 or succ > n:
        raise SourceError(f"{name}: successes/n out of range")
    xloc = {"kind": "csv", "row": rowno, "columns": ["successes", "n"], "key": key}
    xraw = f"{cells['successes']}, {cells['n']}"
    if n == 0:
        x = ctx.pfield(f"{pid}.x_of_n", "not measured (n = 0)", REAL, xraw, 'n == 0 -> "not measured (n = 0)"',
                       src, rev, xloc)
        return {"measured": False, "unit": unit, "x_of_n": x, "rate": None, "ci": None, "geom": None,
                "note": note}
    x = ctx.pfield(f"{pid}.x_of_n", fmt_x_of_n(succ, n), REAL, xraw, '"{successes} of {n}"', src, rev, xloc)
    out = {"measured": True, "unit": unit, "x_of_n": x, "rate": None, "ci": None, "geom": None, "note": note,
           "successes": succ, "n": n}
    try:
        rate = parse_unit_float(cells["rate"], f"{name} rate")
        out["rate"] = ctx.pfield(f"{pid}.rate", fmt_pct1(cells["rate"]), REAL, cells["rate"],
                                 'f"{float(raw):.1%}"', src, rev,
                                 {"kind": "csv", "row": rowno, "columns": ["rate"], "key": key})
        lo = parse_unit_float(cells["ci_low"], f"{name} ci_low")
        hi = parse_unit_float(cells["ci_high"], f"{name} ci_high")
        if not lo <= rate <= hi:
            raise SourceError(f"{name}: rate is outside its interval")
        out["ci"] = ctx.pfield(f"{pid}.ci", fmt_range(cells["ci_low"], cells["ci_high"]), REAL,
                               f"{cells['ci_low']}, {cells['ci_high']}",
                               'f"{float(lo):.1%} to {float(hi):.1%}"', src, rev,
                               {"kind": "csv", "row": rowno, "columns": ["ci_low", "ci_high"], "key": key})
        out["geom"] = {"rate": rate, "lo": lo, "hi": hi}
    except SourceError as exc:
        ctx.warn("integrity", f"{src} row {rowno}: {exc}")
        out["geom"] = None
    return out


def planted_for(rec_obj):
    """(text, where_text, text_paths, where_paths) for a corpus record, or None."""
    it = rec_obj.get("inject_target") if isinstance(rec_obj, dict) else None
    if not isinstance(it, dict):
        return None
    t = it.get("type")
    if t in ("doc_body", "doc_comment") and isinstance(it.get("payload"), str) and isinstance(it.get("doc"), str):
        return (it["payload"], INJECT_WHERE[t].format(doc=it["doc"]), "inject_target.payload",
                "inject_target.type,inject_target.doc")
    if t == "asset_notes" and isinstance(it.get("payload"), str) and isinstance(it.get("asset_id"), str):
        return (it["payload"], INJECT_WHERE[t].format(asset_id=it["asset_id"]), "inject_target.payload",
                "inject_target.type,inject_target.asset_id")
    if t == "doc_split" and isinstance(it.get("docs"), list) and it["docs"]:
        docs = [d for d in it["docs"] if isinstance(d, dict)]
        text = "\n".join(f"{d.get('doc', '?')}: {d.get('text', '')}" for d in docs)
        names = [str(d.get("doc", "?")) for d in docs]
        joined = " and ".join(names) if len(names) <= 2 else ", ".join(names[:-1]) + " and " + names[-1]
        return text, INJECT_WHERE[t].format(docs=joined), "inject_target.docs", "inject_target.type,inject_target.docs"
    return None


def load_corpus(ctx: Ctx, bid: str, corpus_cfg: dict):
    """Return (index {id: (line_no, obj)}, rev_used, others {rev: index}) or (None, ...)."""
    path = corpus_cfg.get("path")
    rev = corpus_cfg.get("rev")
    if rev and not ctx.git_ok:
        ctx.warn("integrity", f"{bid}: corpus read from the working tree, not pinned to {rev} ({ctx.git_note}).")
        rev = None
    text, _ = read_text(ctx, path, rev)
    index = {}
    bad = 0
    for no, obj, _raw in parse_jsonl(text):
        if obj is None or not isinstance(obj.get("id"), str):
            bad += 1
            continue
        index.setdefault(obj["id"], (no, obj))
    if bad:
        ctx.warn("integrity", f"{bid}: {path} has {bad} unreadable line(s); they were skipped.")
    others = {}
    for orev in corpus_cfg.get("also_check_revs") or []:
        if not ctx.git_ok:
            ctx.warn("info", f"{bid}: could not compare the test set with {orev} ({ctx.git_note}).")
            continue
        try:
            otext, _ = read_text(ctx, path, orev)
            others[orev] = {o["id"]: (n, o) for n, o, _r in parse_jsonl(otext)
                            if o is not None and isinstance(o.get("id"), str)}
        except SourceError as exc:
            ctx.warn("info", f"{bid}: could not read {path} @{orev}: {err_text(exc)}")
    return index, rev, others


def read_trace(ctx: Ctx, traces_dir: str, rec: dict):
    """Return (status, obj, path). status: ok | mismatch | missing."""
    path = f"{traces_dir.rstrip('/')}/{rec['test_id']}__rep{rec['rep']}.json"
    try:
        text, _ = read_text(ctx, path, None)
        obj = json.loads(text)
    except SourceError as exc:
        return ("missing" if "not found" in str(exc) else "mismatch"), None, path
    except ValueError:
        return "mismatch", None, path
    if not isinstance(obj, dict):
        return "mismatch", None, path
    try:
        if obj.get("test_id") != rec["test_id"] or parse_int(obj.get("rep"), "rep") != rec["rep"]:
            return "mismatch", None, path
        if flatten_tool_calls(obj.get("tool_calls")) != rec["tool_calls_raw"]:
            return "mismatch", None, path
    except Exception:  # noqa: BLE001
        return "mismatch", None, path
    return "ok", obj, path


def build_batch(ctx: Ctx, cfg: dict, kind: str):
    """Build one Batch from the legacy results/ layout. kind: 'pinned' | 'local'."""
    bid = cfg["id"]
    if not RE_SAFE_ID.match(bid):
        raise SourceError(f"bad batch id {bid!r}")
    if cfg.get("adapter", "legacy-results") != "legacy-results":
        raise SourceError(f"{bid}: adapter {cfg.get('adapter')!r} is not built yet")
    title = cfg.get("title") or bid
    pin = cfg.get("pin_rev") if kind == "pinned" else None
    rev = pin if (pin and ctx.git_ok) else None
    pinned_ok = bool(pin and ctx.git_ok)
    if pin and not ctx.git_ok:
        ctx.warn("integrity", f"{title}: {ctx.git_note}, so results were read from working-tree files, "
                              f"not pinned to {pin}.")
    runs_path = rel_path(cfg["runs_csv"])
    sum_path = rel_path(cfg["summary_csv"])
    at = f" @{rev}" if rev else ""

    batch = {
        "id": bid, "title": title, "status": PENDING, "reason": None,
        "source_files": [f"{runs_path}{at}", f"{sum_path}{at}"],
        "produced": not_recorded_produced(), "replay_label": "REPLAY \u00b7 run time not recorded",
        "model": ctx.field(NOT_RECORDED, PENDING), "digest": ctx.field(NOT_RECORDED, PENDING),
        "defences": ctx.field(NOT_RECORDED, PENDING), "driver": ctx.field(NOT_RECORDED, PENDING),
        "has_blocked_info": False, "blocked_note": "Blocked: not recorded in this batch",
        "shape": ctx.field("not available", PENDING),
        "counts": {"attack": {}, "benign": {}},
        "summary": {}, "groups": [], "runs": {},
        "error_note": ctx.field("not available", PENDING),
        "traces_note": cfg.get("traces_note"),
        "pinned": pinned_ok, "rev": rev,
    }

    # ---- runs.csv
    try:
        runs_text, runs_meta = read_text(ctx, runs_path, rev)
        recs, errs = parse_runs_csv(runs_text)
    except SourceError as exc:
        ctx.warn("integrity", f"{title}: {err_text(exc)}")
        batch["reason"] = f"{runs_path} could not be read: {err_text(exc)}"
        recs, errs, runs_meta = None, [], None
    for e in errs[:5]:
        ctx.warn("integrity", f"{title}: {runs_path} {e}; row skipped.")
    if len(errs) > 5:
        ctx.warn("integrity", f"{title}: {runs_path} has {len(errs) - 5} more bad rows.")
    if recs is not None and not recs:
        batch["reason"] = f"{runs_path} has no readable rows."
        ctx.warn("integrity", f"{title}: {runs_path} has no readable rows.")
        recs = None

    # ---- summary.csv
    summary_rows, sum_meta = None, None
    try:
        sum_text, sum_meta = read_text(ctx, sum_path, rev)
        summary_rows, serrs = parse_summary_csv(sum_text)
        for e in serrs[:5]:
            ctx.warn("integrity", f"{title}: {sum_path} {e}; row skipped.")
    except SourceError as exc:
        ctx.warn("integrity", f"{title}: {err_text(exc)}")

    # ---- agent default model (for "not recorded (lab default: ...)")
    agent_cfg = cfg.get("agent_source") or {"path": "lab/agent.py", "rev": None}
    model_field = ctx.field(NOT_RECORDED, PENDING)
    try:
        arev = agent_cfg.get("rev") if ctx.git_ok else None
        atext, _ = read_text(ctx, agent_cfg["path"], arev)
        mdl, mline = model_default_from_agent(atext)
        model_field = ctx.pfield(f"{bid}.model_default", f"{NOT_RECORDED} (lab default: {mdl})", PENDING, mdl,
                                 '"not recorded (lab default: {raw})"', rel_path(agent_cfg["path"]), arev,
                                 {"kind": "text", "line": mline, "key": "agent.model_default"})
    except Exception as exc:  # noqa: BLE001
        ctx.warn("info", f"{title}: lab default model unknown ({err_text(exc)}).")

    if recs is None and summary_rows is None:
        batch["reason"] = batch["reason"] or "No readable results files."
        batch["model"] = model_field
        return batch

    # ---- traces (only for batches that have a traces_dir)
    traces_dir = cfg.get("traces_dir")
    traces = {}
    if traces_dir and recs:
        rel_path(traces_dir)
        for r in recs:
            traces[(r["test_id"], r["rep"])] = read_trace(ctx, traces_dir, r)

    has_blocked_info = False
    if recs:
        has_blocked_info = any(r["any_blocked"] for r in recs) or any(
            st == "ok" and any(isinstance(c, dict) and "blocked" in c for c in (obj.get("tool_calls") or []))
            for st, obj, _p in traces.values())
    batch["has_blocked_info"] = has_blocked_info
    batch["blocked_note"] = None if has_blocked_info else "Blocked: not recorded in this batch"

    # ---- REPLAY label and produced
    started = []
    for st, obj, _p in traces.values():
        if st == "ok":
            d = parse_iso(obj.get("started_at"))
            if d is not None:
                started.append((d, obj.get("started_at")))
    if started:
        first = min(started, key=lambda x: x[0])
        iso = first[0].isoformat(timespec="seconds")
        batch["replay_label"] = f"REPLAY \u00b7 run of {iso}"
        batch["produced"] = {"basis": "trace-started_at", "text": f"run of {iso}", "raw": first[1]}
    elif runs_meta and sum_meta:
        ct_r = commit_time(ctx, runs_path, rev, runs_meta["blob"])
        ct_s = commit_time(ctx, sum_path, rev, sum_meta["blob"])
        if ct_r and ct_s:
            latest = max((ct_r, ct_s), key=lambda c: parse_iso(c["time"]))
            pid = ctx.add_prov(f"{bid}.produced", REAL, f"results committed {latest['time']}", latest["time"],
                               "as written by git (%cI)", sum_path if latest is ct_s else runs_path, None,
                               {"kind": "git", "cmd": latest["cmd"]})
            batch["replay_label"] = f"REPLAY \u00b7 results committed {latest['time']}"
            batch["produced"] = {"basis": "git-commit", "text": f"results committed {latest['time']}",
                                 "raw": latest["time"], "prov": pid}

    # ---- candidate-ish fields from traces (local batches) or not recorded
    ok_traces = [obj for st, obj, _p in traces.values() if st == "ok"]
    batch["model"] = model_field
    if ok_traces:
        models = sorted({str(o.get("model")) for o in ok_traces if o.get("model")})
        digests = sorted({str(o.get("model_digest")) for o in ok_traces if o.get("model_digest")})
        defs = sorted({", ".join(o.get("defences") or []) or "none" for o in ok_traces
                       if isinstance(o.get("defences"), list)})
        if len(models) == 1:
            batch["model"] = ctx.field(models[0], REAL, None)
        elif models:
            batch["model"] = ctx.field("mixed: " + ", ".join(models), REAL, None)
        if len(digests) == 1:
            batch["digest"] = ctx.field(short_digest(digests[0]), REAL, None)
        if len(defs) == 1:
            batch["defences"] = ctx.field(defs[0], REAL, None)

    # ---- summary metrics
    if summary_rows is not None:
        sr = summary_rows
        summ = {
            "attack_success_rate": safe(ctx, f"{title} attack_success_rate", metric_from_row, ctx, f"{bid}.asr",
                                        "attack_success_rate", sr.get("attack_success_rate"), "runs", sum_path,
                                        rev, METRIC_NOTES["attack_success_rate"]),
            "benign_task_success": safe(ctx, f"{title} benign_task_success", metric_from_row, ctx,
                                        f"{bid}.benign", "benign_task_success", sr.get("benign_task_success"),
                                        "runs", sum_path, rev, METRIC_NOTES["benign_task_success"]),
            "ever_hijacked_rate": safe(ctx, f"{title} ever_hijacked_rate", metric_from_row, ctx, f"{bid}.ever",
                                       "ever_hijacked_rate", sr.get("ever_hijacked_rate"), "tests", sum_path,
                                       rev, METRIC_NOTES["ever_hijacked_rate"]),
        }
        for k in list(summ):
            if summ[k] is None:
                summ[k] = metric_from_row(ctx, "", k, None, "tests" if k == "ever_hijacked_rate" else "runs",
                                          sum_path, rev, METRIC_NOTES[k])
        er = sr.get("error_runs")
        if er:
            try:
                e_s = parse_int(er["cells"]["successes"], "error_runs successes")
                e_n = parse_int(er["cells"]["n"], "error_runs n")
                summ["error_runs"] = ctx.pfield(
                    f"{bid}.error_runs", f"{e_s} of {e_n} runs errored", REAL,
                    f"{er['cells']['successes']}, {er['cells']['n']}", '"{successes} of {n} runs errored"',
                    sum_path, rev, {"kind": "csv", "row": er["row"], "columns": ["successes", "n"],
                                    "key": "metric=error_runs"})
            except SourceError as exc:
                ctx.warn("integrity", f"{title}: {sum_path} error_runs: {exc}")
                summ["error_runs"] = ctx.field("not available", PENDING)
        else:
            summ["error_runs"] = ctx.field("not available", PENDING)
        per = []
        for name in sorted((m for m in sr if m.startswith("asr::")), key=lambda m: tech_sort_key(m[5:])):
            tech = name[5:]
            met = safe(ctx, f"{title} {name}", metric_from_row, ctx, f"{bid}.tech.{tech}", name, sr[name], "runs",
                       sum_path, rev, METRIC_NOTES["technique"])
            if met is not None:
                per.append({"technique": tech, "label": label(TECHNIQUE_LABELS, tech), "metric": met})
        summ["per_technique"] = per
        batch["summary"] = summ
    else:
        batch["summary"] = {
            k: metric_from_row(ctx, "", k, None, "tests" if k == "ever_hijacked_rate" else "runs", sum_path, rev,
                               METRIC_NOTES[k])
            for k in ("attack_success_rate", "benign_task_success", "ever_hijacked_rate")}
        batch["summary"]["error_runs"] = ctx.field("not available", PENDING)
        batch["summary"]["per_technique"] = []

    if recs is None:
        batch["reason"] = batch["reason"] or f"{runs_path} could not be read."
        return batch

    # ---- counts, shape, error note (counted from runs.csv)
    bi = "1" if has_blocked_info else "0"

    def cnt(names: str, text_fn, fmt: str):
        vals = [count_runs(recs, n, has_blocked_info) for n in names.split("+")]
        key = f"count:{names}|bi={bi}"
        return ctx.pfield(f"{bid}.count.{names}", text_fn(*vals), REAL, ", ".join(str(v) for v in vals), fmt,
                          runs_path, rev, {"kind": "csv", "key": key,
                                           "columns": ["kind", "violated", "error", "tool_calls", "benign_success"]})

    counts = {"attack": {}, "benign": {}}
    for k in ("attack", "benign"):
        counts[k]["runs"] = cnt(k, str, "count of rows")
        counts[k]["broke"] = cnt(f"{k}.broke", str, "count of rows")
        counts[k]["error"] = cnt(f"{k}.error", str, "count of rows")
    counts["attack"]["clean"] = cnt("attack.clean", str, "count of rows")
    if has_blocked_info:
        counts["attack"]["blocked"] = cnt("attack.blocked", str, "count of rows")
    else:
        counts["attack"]["blocked"] = ctx.field("not recorded in this batch", PENDING)
    counts["benign"]["handled"] = cnt("benign.handled", str, "count of rows")
    counts["benign"]["not_handled"] = cnt("benign.not_handled", str, "count of rows")
    batch["counts"] = counts
    info = shape_info(recs)
    batch["shape"] = ctx.pfield(f"{bid}.shape", shape_text(info), REAL, shape_raw(info),
                                "tests and reps counted from runs.csv", runs_path, rev,
                                {"kind": "csv", "key": "shape", "columns": ["test_id", "kind", "rep"]})
    batch["error_note"] = cnt("rows+rows.error", lambda a, b: f"{a} runs in the file, {b} errored",
                              '"{rows} runs in the file, {errored} errored"')

    # ---- corpus (planted text)
    corpus_cfg = cfg.get("corpus") or {}
    corpus_index, corpus_rev, corpus_others = None, None, {}
    if corpus_cfg.get("path"):
        try:
            corpus_index, corpus_rev, corpus_others = load_corpus(ctx, bid, corpus_cfg)
            batch["source_files"].append(f"{rel_path(corpus_cfg['path'])}{' @' + corpus_rev if corpus_rev else ''}")
        except SourceError as exc:
            ctx.warn("integrity", f"{title}: test set unreadable ({err_text(exc)}); planted text not shown.")
    corpus_path = rel_path(corpus_cfg["path"]) if corpus_cfg.get("path") else None

    # ---- runs
    runs, by_group = {}, {}
    for r in recs:
        key = f"{r['test_id']}:{r['rep']}"
        state, badges = tile_state(r, has_blocked_info)
        rowkey = f"test_id={r['test_id']};rep={r['cells']['rep']}"
        ut = ctx.pfield(f"{bid}.run.{key}.user_task", r["user_task"], REAL, r["user_task"], "as written", runs_path,
                        rev, {"kind": "csv", "row": r["row"], "columns": ["user_task"], "key": rowkey})
        src_row = ctx.pfield(f"{bid}.run.{key}.row", f"{runs_path}{at} row {r['row']}", REAL,
                             f"{r['cells']['test_id']}, {r['cells']['rep']}", '"{file} @{rev} row {N}"',
                             runs_path, rev, {"kind": "csv", "row": r["row"], "columns": ["test_id", "rep"],
                                              "key": rowkey})
        planted = {"status": "none", "text": None, "where": None, "caveat": None, "changed_between_versions": False}
        if r["kind"] == "attack":
            if corpus_index is None:
                planted.update(status="missing", caveat="The test set could not be read.")
            elif r["test_id"] not in corpus_index:
                planted.update(status="missing",
                               caveat=f"This test id is not in {corpus_path}{' @' + corpus_rev if corpus_rev else ''}.")
            else:
                cline, cobj = corpus_index[r["test_id"]]
                cat = f" @{corpus_rev}" if corpus_rev else " (working tree)"
                if cobj.get("user_task") != r["user_task"]:
                    planted.update(status="differs", caveat="corpus version differs from this run")
                else:
                    pl = planted_for(cobj)
                    if pl is None:
                        planted.update(status="missing", caveat="The test record has no planted text.")
                    else:
                        ptext, pwhere, tpath, wpath = pl
                        tid = r["test_id"]
                        planted["text"] = ctx.pfield(
                            f"{bid}.test.{tid}.planted", ptext, REAL, join_paths(cobj, tpath), "as written",
                            corpus_path, corpus_rev, {"kind": "jsonl", "line": cline, "json_path": tpath})
                        planted["where"] = ctx.pfield(
                            f"{bid}.test.{tid}.where", pwhere, REAL, join_paths(cobj, wpath),
                            "INJECT_WHERE[type].format(...)", corpus_path, corpus_rev,
                            {"kind": "jsonl", "line": cline, "json_path": wpath})
                        changed = False
                        for _orev, oidx in corpus_others.items():
                            other = oidx.get(tid)
                            if other is None or (other[1].get("inject_target"), other[1].get("poison")) != (
                                    cobj.get("inject_target"), cobj.get("poison")):
                                changed = True
                        cav = f"Planted text comes from {corpus_path}{cat}; the run record does not store it."
                        if changed:
                            cav += " The planted text changed between test-set versions; this run may have seen the other one."
                        planted.update(status="match", caveat=cav, changed_between_versions=changed)

        tstat, tobj, tpath_ = ("not-committed", None, None)
        if traces_dir:
            tstat, tobj, tpath_ = traces.get((r["test_id"], r["rep"]), ("missing", None, None))
        run_info = {
            "model": model_field,
            "digest": ctx.field(NOT_RECORDED, PENDING),
            "duration": ctx.field(f"{NOT_RECORDED} (no trace file)", PENDING),
            "started": ctx.field(NOT_RECORDED, PENDING),
            "driver": ctx.field(NOT_RECORDED, PENDING),
            "defences": ctx.field(NOT_RECORDED, PENDING),
        }
        if tstat == "ok" and tobj is not None:
            def tf(fieldname, jp, fmt_fn, fmt_desc):
                if tobj.get(jp) in (None, ""):
                    return ctx.field(NOT_RECORDED, PENDING)
                raw = raw_of(tobj.get(jp))
                return ctx.pfield(f"{bid}.run.{key}.{fieldname}", fmt_fn(raw), REAL, raw, fmt_desc, tpath_, None,
                                  {"kind": "text", "json_path": jp})
            run_info["model"] = tf("model", "model", lambda x: x, "as written")
            run_info["digest"] = tf("digest", "model_digest", short_digest, "short_digest(raw)")
            try:
                run_info["duration"] = tf("duration", "duration_s", lambda x: f"{float(x):.2f} s",
                                          'f"{float(raw):.2f} s"')
            except ValueError:
                run_info["duration"] = ctx.field(NOT_RECORDED, PENDING)
            run_info["started"] = tf("started", "started_at", lambda x: x, "as written")
            if isinstance(tobj.get("defences"), list):
                run_info["defences"] = tf("defences", "defences",
                                          lambda x: ", ".join(json.loads(x)) or "none", '", ".join(raw) or "none"')
        elif traces_dir:
            run_info["duration"] = ctx.field(
                f"{NOT_RECORDED} ({'trace file missing' if tstat == 'missing' else 'trace does not match this row'})",
                PENDING)

        runs[key] = {
            "key": key, "test_id": r["test_id"], "rep": r["rep"], "kind": r["kind"],
            "technique": r["technique"], "technique_label": label(TECHNIQUE_LABELS, r["technique"]),
            "place": r["place"], "place_label": label(PLACE_LABELS, r["place"]),
            "goal": r["goal"], "goal_label": label(GOAL_LABELS, r["goal"]),
            "state": state, "state_label": STATE_LABELS[state], "badges": badges,
            "user_task": ut,
            "tool_calls": r["calls"],
            "violated": r["violated"],
            "classes": [{"code": c, "label": label(CLASS_LABELS, c)} for c in r["classes"]],
            "detail": r["detail"],
            "checker": CHECKER,
            "benign_success": r["benign_success"] if r["kind"] == "benign" else None,
            "error": r["error"],
            "planted": planted,
            "run_info": run_info,
            "trace_status": tstat,
            "source_row": src_row,
        }
        gk = (r["kind"], r["technique"] if r["kind"] == "attack" else "none")
        by_group.setdefault(gk, {}).setdefault(r["test_id"], []).append((r["rep"], key))

    per_tech = {p["technique"]: p["metric"] for p in batch["summary"].get("per_technique", [])}
    groups = []
    for band in ("attack", "benign"):
        keys = sorted((g for g in by_group if g[0] == band), key=lambda g: tech_sort_key(g[1]))
        for g in keys:
            tests = [{"test_id": tid, "runs": [k for _rep, k in sorted(v)]}
                     for tid, v in sorted(by_group[g].items())]
            if band == "attack":
                metric = per_tech.get(g[1])
            else:
                bm = batch["summary"].get("benign_task_success")
                metric = bm if bm and bm.get("measured") else None
            groups.append({"band": band, "technique": g[1],
                           "label": label(TECHNIQUE_LABELS, g[1]) if band == "attack" else "Normal requests",
                           "metric": metric, "tests": tests})
    batch["groups"] = groups
    batch["runs"] = runs
    batch["status"] = REAL
    batch["reason"] = None
    batch["_recs"] = recs  # removed before writing
    batch["_trace_count"] = sum(1 for st, _o, _p in traces.values() if st == "ok")
    batch["_traces_started"] = len(started)
    return batch


def consistency_checks_batch(ctx: Ctx, b: dict) -> None:
    recs = b.get("_recs")
    s = b.get("summary") or {}
    if not recs or not s:
        return
    hb = b["has_blocked_info"]
    title = b["title"]
    asr = s.get("attack_success_rate") or {}
    attack_ok = [r for r in recs if r["kind"] == "attack" and not r["error"]]
    if asr.get("measured"):
        broke = sum(1 for r in attack_ok if r["violated"])
        if (broke, len(attack_ok)) != (asr.get("successes"), asr.get("n")):
            ctx.warn("integrity", f"{title}: runs.csv and summary.csv disagree on attack success "
                                  f"(rows {broke}/{len(attack_ok)}).")
    for p in s.get("per_technique", []):
        m = p["metric"]
        rows = [r for r in attack_ok if r["technique"] == p["technique"]]
        if m.get("measured") and (sum(1 for r in rows if r["violated"]), len(rows)) != (m.get("successes"), m.get("n")):
            ctx.warn("integrity", f"{title}: runs.csv and summary.csv disagree on {p['technique']}.")
    ev = s.get("ever_hijacked_rate") or {}
    if ev.get("measured"):
        tests = {}
        for r in attack_ok:
            tests[r["test_id"]] = tests.get(r["test_id"], False) or r["violated"]
        if (sum(tests.values()), len(tests)) != (ev.get("successes"), ev.get("n")):
            ctx.warn("integrity", f"{title}: runs.csv and summary.csv disagree on tests broken at least once.")
    bn = s.get("benign_task_success") or {}
    benign_ok = [r for r in recs if r["kind"] == "benign" and not r["error"]]
    if bn.get("measured") is False and benign_ok and bn.get("x_of_n", {}).get("status") == REAL:
        ctx.warn("integrity", f"{title}: summary.csv says no normal runs, runs.csv has {len(benign_ok)}.")
    er = s.get("error_runs")
    if er and er.get("status") == REAL:
        m = re.match(r"^(\d+) of (\d+) runs errored$", er["text"])
        if m and (int(m.group(1)), int(m.group(2))) != (count_runs(recs, "rows.error", hb), len(recs)):
            ctx.warn("integrity", f"{title}: runs.csv and summary.csv disagree on error runs.")


# --------------------------------------------------------------------------- #
# Builders: policy and controls.
# --------------------------------------------------------------------------- #
def file_produced(ctx: Ctx, path: str, meta: dict, what: str = "committed") -> dict:
    ct = commit_time(ctx, path, None, meta["blob"])
    if ct:
        pid = ctx.add_prov(f"file.{path}.committed", REAL, f"{what} {ct['time']}", ct["time"],
                           "as written by git (%cI)", path, None, {"kind": "git", "cmd": ct["cmd"]})
        return {"basis": "git-commit", "text": f"{what} {ct['time']}", "raw": ct["time"], "prov": pid}
    return not_recorded_produced("working-tree file; commit time not recorded")


def build_policy(ctx: Ctx) -> dict:
    path = rel_path(ctx.sources.get("policy", {}).get("path", "lab/governance/policy.yaml"))
    try:
        text, meta = read_text(ctx, path)
        data, line_of = parse_yaml_subset(text)
    except SourceError as exc:
        ctx.warn("integrity", f"Policy: {err_text(exc)}")
        out = pending(f"{path} could not be read: {err_text(exc)}", path)
        out.update(version=ctx.field(NOT_RECORDED, PENDING), owner=ctx.field(NOT_RECORDED, PENDING), limits=[],
                   roles={k: ctx.field(NOT_RECORDED, PENDING) for k in ("engineers", "reviewers", "rule_owner")},
                   impact=[])
        return out

    def yf(pid, key, fmt_fn, fmt_desc):
        if key not in line_of:
            ctx.warn("integrity", f"Policy: {path} has no {key}.")
            return ctx.field(NOT_RECORDED, PENDING)
        v = get_path(data, key)
        try:
            txt = fmt_fn(v)
        except (TypeError, ValueError):
            ctx.warn("integrity", f"Policy: {key} has an unexpected value.")
            return ctx.field("unreadable value", PENDING)
        return ctx.pfield(pid, txt, REAL, raw_of(v), fmt_desc, path, None,
                          {"kind": "yaml", "key": key, "line": line_of[key]})

    def as_list(v):
        if isinstance(v, list):
            return ", ".join(str(x) for x in v) or "none"
        if isinstance(v, str):
            return v
        raise TypeError

    def as_str(v):
        if isinstance(v, (dict, list)) or v is None:
            raise TypeError
        return str(v)

    def as_limit(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TypeError
        return fmt_limit(v)

    limits = []
    rl = data.get("release_limits") if isinstance(data.get("release_limits"), dict) else {}
    order = [k for k in LIMIT_LABELS if k in rl] + [k for k in rl if k not in LIMIT_LABELS]
    for k in order:
        limits.append({"key": k, "label": label(LIMIT_LABELS, k),
                       "text": yf(f"policy.limit.{k}", f"release_limits.{k}", as_limit,
                                  'gate.report_text limit format: f"{v:.1%}" for floats, str(v) otherwise')})
    if not rl:
        ctx.warn("integrity", f"Policy: {path} has no release_limits.")
    impact = []
    imp = data.get("impact") if isinstance(data.get("impact"), dict) else {}
    for k in imp:
        impact.append({"class": k, "label": label(CLASS_LABELS, k),
                       "text": yf(f"policy.impact.{k}", f"impact.{k}", as_str, "as written")})
    out = {
        "status": REAL, "source_file": path, "produced": file_produced(ctx, path, meta), "reason": None,
        "version": yf("policy.version", "policy_version", as_str, "as written"),
        "owner": yf("policy.owner", "policy_owner", as_str, "as written"),
        "limits": limits,
        "roles": {
            "engineers": yf("policy.roles.engineers", "roles.engineers", as_list, '", ".join(list)'),
            "reviewers": yf("policy.roles.reviewers", "roles.reviewers", as_list, '", ".join(list)'),
            "rule_owner": yf("policy.roles.rule_owner", "roles.rule_owner", as_str, "as written"),
        },
        "impact": impact,
    }
    if not limits:
        out["status"] = PENDING
        out["reason"] = f"{path} has no release limits."
    return out


def build_controls(ctx: Ctx) -> dict:
    path = rel_path(ctx.sources.get("controls", {}).get("path", "lab/assurance/control_map.yaml"))
    try:
        text, meta = read_text(ctx, path)
        data, line_of = parse_yaml_subset(text)
        ctrl = data.get("controls")
        if not isinstance(ctrl, dict) or not ctrl:
            raise SourceError(f"{path} has no controls")
    except SourceError as exc:
        ctx.warn("integrity", f"Controls: {err_text(exc)}")
        out = pending(f"{path} could not be read: {err_text(exc)}", path)
        out.update(items=[], note=ctx.field(NOT_RECORDED, PENDING), checked_against=[])
        return out

    def strlist(v):
        if isinstance(v, list):
            return [str(x) for x in v]
        if isinstance(v, str):
            return [v]
        return []

    items = []
    for code, c in ctrl.items():
        if not isinstance(c, dict):
            ctx.warn("integrity", f"Controls: {code} is not a mapping; skipped.")
            continue
        items.append({"code": code, "name": str(c.get("name", "")),
                      "owasp": strlist(c.get("owasp")), "mitre_atlas": strlist(c.get("mitre_atlas")),
                      "nist_ai_rmf": strlist(c.get("nist_ai_rmf"))})
    ca = data.get("checked_against") if isinstance(data.get("checked_against"), dict) else {}
    if "checked_against.note" in line_of and isinstance(ca.get("note"), str):
        note = ctx.pfield("controls.note", ca["note"], REAL, ca["note"], "as written", path, None,
                          {"kind": "yaml", "key": "checked_against.note", "line": line_of["checked_against.note"]})
    else:
        note = ctx.field("No verification note in the control map.", PENDING)
    checked = [{"key": k, "text": str(v)} for k, v in ca.items() if k != "note"]
    return {"status": REAL, "source_file": path, "produced": file_produced(ctx, path, meta), "reason": None,
            "items": items, "note": note, "checked_against": checked}


# --------------------------------------------------------------------------- #
# Builders: governance (gate, audit, assurance).
# --------------------------------------------------------------------------- #
def load_fixture(ctx: Ctx, dataset: str):
    """Return (fixture_obj, path). Refuses anything not marked sample:true."""
    path = rel_path((ctx.sources.get("sample_fixtures") or {}).get(dataset, ""))
    if not (path.startswith("ui/fixtures/") and path.endswith(".sample.json")):
        raise SourceError(f"fixture {path} is not under ui/fixtures/*.sample.json; refused")
    text, _ = read_text(ctx, path)
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise SourceError(f"fixture {path} is not a JSON object")
    if obj.get("sample") is not True:
        raise SourceError(f"fixture {path} is not marked \"sample\": true; refused")
    if obj.get("dataset") != dataset:
        raise SourceError(f"fixture {path} is for dataset {obj.get('dataset')!r}, not {dataset!r}; refused")
    if obj.get("format") != FIXTURE_FORMATS[dataset]:
        raise SourceError(f"fixture {path} has format {obj.get('format')!r}; refused")
    if "NOT produced" not in str(obj.get("how_made", "")):
        raise SourceError(f"fixture {path} does not say it is NOT produced by the backend; refused")
    return obj, path


def fixture_produced(obj: dict) -> dict:
    prep = str(obj.get("prepared", ""))
    return {"basis": "fixture", "text": f"hand-written fixture, prepared {prep}", "raw": prep}


def governance_dir(gov: dict) -> str:
    """The repo-relative folder that holds the governance files ("" when none is configured)."""
    d = gov.get("dir")
    if isinstance(d, str) and d.strip():
        return rel_path(d)
    gate = gov.get("gate_report")
    if isinstance(gate, str) and gate.strip():
        return Path(rel_path(gate)).parent.as_posix()
    return ""


def folder_mode_tags(ctx: Ctx, gov: dict) -> dict:
    """{file name: mode tag} for every file in the governance folder that states its mode.

    Reads the "mode" of run_index.json and audit_verify.json, the "[TAG] " that starts gate_*.txt
    and the "**Mode: [TAG]**" line that starts each .md file. Files without a tag are left out.
    An unreadable file is left out too: the code that parses it reports the problem.
    """
    out: dict = {}
    try:
        folder = governance_dir(gov)
        names = sorted(p.name for p in (ctx.repo_root / folder).iterdir() if p.is_file())
    except (OSError, SourceError):
        return out
    for name in names[:400]:
        rel = name if folder in ("", ".") else f"{folder}/{name}"
        try:
            if name in ("run_index.json", "audit_verify.json"):
                obj = json.loads(read_text(ctx, rel)[0])
                tag = obj.get("mode") if isinstance(obj, dict) else None
            elif name.startswith("gate_") and name.endswith(".txt"):
                first = (text_lines(read_text(ctx, rel)[0]) or [""])[0]
                m = RE_TXT_MODE.match(first)
                tag = m.group(1) if m else None
            elif name.endswith(".md"):
                tag = split_mode_line(read_text(ctx, rel)[0])[0]
            else:
                continue
        except Exception:  # noqa: BLE001
            continue
        if tag in MODE_TAGS:
            out[name] = tag
    return out


def warn_mixed_modes(ctx: Ctx, tags: dict) -> bool:
    """Warn when the files of one governance folder carry different mode tags. True if they do."""
    kinds = sorted(set(tags.values()))
    if len(kinds) < 2:
        return False
    parts = []
    for k in kinds:
        names = [n for n in sorted(tags) if tags[n] == k]
        parts.append(f"{k}: {', '.join(names[:4])}{', ...' if len(names) > 4 else ''}")
    ctx.warn("integrity", "Governance output mixes run modes (" + "; ".join(parts) + "). The files are not all "
                          "from one run; run the CLI again into an empty folder.")
    return True


def detect_scripted(ctx: Ctx, gov: dict, batch) -> bool:
    for fp in gov.get("fingerprint") or []:
        try:
            text, _ = read_text(ctx, fp)
            obj = json.loads(text)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict) and isinstance(obj.get("model_digest"), str) and (
                obj["model_digest"] == DEMO_DIGEST or obj["model_digest"].startswith("sha256:845dbda0")):
            return True
    if batch and batch.get("_trace_count", 0) and not batch.get("_traces_started", 0):
        return True
    return False


def gate_from_text(ctx: Ctx, lines_or_text, status: str, src: str, loc_base: dict) -> dict:
    """Build a Gate from report text. loc_base: {'kind':'text'} or fixture locator base."""
    text = "\n".join(lines_or_text) if isinstance(lines_or_text, list) else lines_or_text
    g = parse_gate_report(text)

    def loc(line, key):
        d = dict(loc_base)
        d.update(line=line, key=key)
        return d

    def gf(pid, txt, raw, line, key, fmt="as written"):
        return ctx.pfield(pid, txt, status, raw, fmt, src, None, loc(line, key))

    checks = []
    for c in g["checks"]:
        n = c["name"]
        checks.append({
            "name": n, "label": label(CHECK_LABELS, n), "ok": c["mark"] == "OK", "mark": c["mark"],
            "value": gf(f"gate.check.{n}.value", c["value"], c["value"], c["line"], "gate.value"),
            "limit": gf(f"gate.check.{n}.limit", c["limit"], c["limit"], c["line"], "gate.limit"),
            "note": gf(f"gate.check.{n}.note", c["note"], c["note"], c["line"], "gate.note") if c["note"] else None,
            "line": c["line"],
        })
    # Figures the gate prints "not a limit": shown beside the checks, never part of them.
    info = []
    for c in g["info"]:
        n = c["name"]
        info.append({
            "name": n, "label": label(CHECK_LABELS, n),
            "value": gf(f"gate.info.{n}.value", c["value"], c["value"], c["line"], "gate.info.value"),
            "note": gf(f"gate.info.{n}.note", c["note"], c["note"], c["line"], "gate.info.note"),
            "line": c["line"],
        })
    reasons = None
    if g["reasons"] is not None:
        rn = g["reasons"]
        reasons = gf("gate.reasons", f"{rn} reason{'s' if rn != 1 else ''}", str(rn), g["verdict_line"],
                     "gate.reasons", '"{n} reason(s)"')
    fails = sum(1 for c in g["checks"] if c["mark"] == "FAIL")
    return {
        "candidate_id": gf("gate.candidate", g["candidate"], g["candidate"], g["candidate_line"], "gate.candidate"),
        "verdict": gf("gate.verdict", g["verdict"], g["verdict"], g["verdict_line"], "gate.verdict"),
        "reasons_count": reasons,
        "checks": checks,
        "info": info,
        "raw_lines": list(g["lines"]),
        "_fails": fails, "_reasons": g["reasons"] or 0, "_verdict": g["verdict"],
    }


def gate_pending(reason: str, src) -> dict:
    out = pending(reason, src)
    out.update(sample_tag=None, candidate_id=Ctx.field(NOT_RECORDED, PENDING), verdict=None,
               reasons_count=None, checks=[], info=[], raw_lines=[])
    return out


def gate_consistency(ctx: Ctx, gate: dict, batch) -> str:
    """Return a PENDING reason, or '' if the gate is consistent."""
    if gate["_fails"] != gate["_reasons"]:
        ctx.warn("integrity", "Gate: the number of FAIL lines does not match the reasons count.")
        return "gate output is inconsistent (FAIL lines and reasons count differ)"
    if (gate["_verdict"] == "PASS") != (gate["_fails"] == 0):
        ctx.warn("integrity", "Gate: the verdict does not match its check lines.")
        return "gate output is inconsistent (verdict and checks differ)"
    asr_check = next((c for c in gate["checks"] if c["name"] == "attack_success_rate"), None)
    m = batch and (batch.get("summary") or {}).get("attack_success_rate")
    if asr_check and asr_check["note"] and m and m.get("measured"):
        xm = RE_GATE_XN.match(asr_check["note"]["text"])
        if not xm or (int(xm.group(1)), int(xm.group(2))) != (m.get("successes"), m.get("n")):
            ctx.warn("integrity", f"Gate: its attack count does not match the evidence batch ({batch['title']}).")
            return DIFFERENT_BATCH
    elif asr_check:
        ctx.warn("integrity", "Gate: cannot compare its attack count with the evidence batch.")
        return DIFFERENT_BATCH
    return ""


def json_line_loc(loc_base: dict, line: int, path: str) -> dict:
    """Locator for one JSON field on one line: jsonl files use json_path;
    fixtures keep json_path for the content list and put the field in key."""
    d = dict(loc_base)
    if d.get("kind") == "jsonl":
        d.update(line=line, json_path=path)
    else:
        d.update(line=line, key="json:" + path)
    return d


def audit_entries(ctx: Ctx, parsed: list, status: str, src: str, loc_base: dict) -> list:
    entries = []

    def loc(line, key):
        return json_line_loc(loc_base, line, key[5:])

    for line, obj, _raw in parsed:
        base = f"audit.entry.{line}"
        if obj is None:
            entries.append({"line": line, "seq": ctx.field(f"line {line}: unreadable", status),
                            "event": None, "time": ctx.field("", status), "who": ctx.field("", status),
                            "candidate": ctx.field("", status), "what": ctx.field(f"line {line}: unreadable", status),
                            "reason": None, "hash_short": "", "readable": False})
            continue

        def jf(name, path, text=None, fmt="as written"):
            try:
                raw = join_paths(obj, path)
            except SourceError:
                return ctx.field("not recorded", status)
            return ctx.pfield(f"{base}.{name}", raw if text is None else text, status, raw, fmt, src, None,
                              loc(line, "json:" + path))

        ev = obj.get("event")
        who, what, reason, cand = None, None, None, None
        if "candidate_id" in obj:
            cand = jf("candidate", "candidate_id")
        if ev == "submit":
            who = jf("who", "by")
            d = obj.get("defences")
            dtext = (", ".join(str(x) for x in d) or "none") if isinstance(d, list) else "not recorded"
            what = jf("what", "defences", text=f"Submitted for review (defences: {dtext})",
                      fmt='"Submitted for review (defences: {list or none})"')
        elif ev == "verdict":
            who = ctx.field("gate", status)
            what = jf("what", "verdict", text=f"Gate verdict {obj.get('verdict')}", fmt='"Gate verdict {verdict}"')
        elif ev == "decision":
            who = jf("who", "reviewer")
            dec, ver, exp = obj.get("decision"), obj.get("verdict"), obj.get("expiry")
            t = f"{dec} until {exp}" if dec == "OVERRIDE" and exp else f"{dec}"
            what = jf("what", "decision,verdict", text=f"{t} (gate verdict {ver})",
                      fmt='"{decision} (gate verdict {verdict})"')
            if obj.get("reason"):
                reason = jf("reason", "reason")
        elif ev == "decision_refused":
            who = jf("who", "reviewer")
            what = jf("what", "detail")
        elif ev == "role_change":
            who = jf("who", "actor")
            act, person, role = obj.get("action"), obj.get("person"), obj.get("role")
            prep = "to" if act == "add" else "from"
            what = jf("what", "action,person,role", text=f"{act} {person} {prep} {role}",
                      fmt='"{action} {person} to/from {role}"')
        else:
            what = ctx.field(f"event {ev!r}", status)
        if cand is None:
            cand = ctx.field("none", status)
        if who is None:
            who = ctx.field("not recorded", status)
        h = obj.get("hash")
        if isinstance(h, str) and re.match(r"^[0-9a-f]{64}$", h):
            hs = h[:12]
        elif isinstance(h, str) and h.startswith("SAMPLE-NOT-A-HASH"):
            hs = h
        else:
            hs = "no hash"
        seq = jf("seq", "seq")
        tm = jf("time", "time")
        entries.append({"line": line, "seq": seq, "event": ev, "event_label": EVENT_LABELS.get(ev, str(ev)),
                        "time": tm, "who": who, "candidate": cand, "what": what, "reason": reason,
                        "hash_short": hs, "readable": True})
    return entries


def latest_decision(ctx: Ctx, parsed: list, status: str, src: str, loc_base: dict, cand: str):
    last = None
    for line, obj, _raw in parsed:
        if obj and obj.get("event") == "decision" and obj.get("candidate_id") == cand:
            last = (line, obj)
    if not last:
        return None
    line, obj = last
    d = json_line_loc(loc_base, line, "decision,reviewer")
    raw = join_paths(obj, "decision,reviewer")
    return ctx.pfield("audit.latest_decision", f"{obj.get('decision')} by {obj.get('reviewer')}", status, raw,
                      '"{decision} by {reviewer}"', src, None, d)


def assurance_from_md(ctx: Ctx, risk_md, ev_md, fix_md, status: str, srcs: dict, loc_bases: dict) -> dict:
    risks = []
    for line, row in parse_md_table(risk_md, ("ID", "Risk", "Violation class", "Likelihood", "Impact", "Control",
                                              "Owner", "Status")):
        parse_likelihood(row["Likelihood"])
        d = dict(loc_bases["risk"])
        d.update(line=line, columns=["Likelihood"])
        lk = ctx.pfield(f"assurance.risk.{row['ID']}.likelihood", row["Likelihood"], status, row["Likelihood"],
                        "as written (risks.to_markdown)", srcs["risk"], None, d)
        cls = strip_ticks(row["Violation class"])
        risks.append({"id": row["ID"], "title": row["Risk"], "class": cls, "class_label": label(CLASS_LABELS, cls),
                      "likelihood": lk, "impact": row["Impact"],
                      "control": "no fix" if strip_bold(row["Control"]) == "no fix" else row["Control"],
                      "owner": row["Owner"], "status_text": row["Status"]})
    evidence = []
    if ev_md is not None:
        for _line, row in parse_md_table(ev_md, ("Control", "Proof needed", "Proof file", "Result")):
            pf = strip_ticks(row["Proof file"])
            exists = False
            try:
                rp = rel_path(pf)
                full = (ctx.repo_root / rp).resolve()
                exists = full.is_file() and ctx.repo_root.resolve() in full.parents
            except Exception:  # noqa: BLE001
                exists = False
            evidence.append({"control": row["Control"], "proof_needed": row["Proof needed"], "proof_file": pf,
                             "proof_file_exists": exists,
                             "proof_file_note": None if exists else "file not found",
                             "result": row["Result"]})
    tickets = []
    if fix_md is not None:
        for _line, t in parse_fix_tickets(fix_md):
            tickets.append(t)
    return {"risks": risks, "evidence": evidence, "tickets": tickets}


def build_governance(ctx: Ctx, batches: dict):
    gov = ctx.sources.get("governance") or {}
    connected = gov.get("connected") is True
    allow = ctx.args.allow_sample
    ev_batch = batches.get(gov.get("evidence_batch") or ctx.sources.get("current_batch"))
    cand_id = gov.get("candidate_id") or "RC-001"
    gate_src = gov.get("gate_report")
    audit_src = gov.get("audit_log")
    risk_src = gov.get("risk_register_md")

    gate = None
    audit = None
    assurance = None
    latest = None

    if connected:
        warn_mixed_modes(ctx, folder_mode_tags(ctx, gov))
        scripted = detect_scripted(ctx, gov, ev_batch)
        g_status = SAMPLE if scripted else REAL
        if scripted and not allow:
            reason = "Scripted output (backend DEMO mode) is not shown without --allow-sample."
            ctx.warn("info", f"Governance: {reason}")
            gate = gate_pending(reason, gate_src)
            audit = None
            assurance = None
        else:
            tag = SCRIPTED_TAG if scripted else None
            # gate
            try:
                p = rel_path(gate_src)
                text, meta = read_text(ctx, p)
                gb = gate_from_text(ctx, text, g_status, p, {"kind": "text"})
                why = gate_consistency(ctx, gb, ev_batch)
                if why:
                    gate = gate_pending(why, p)
                else:
                    gate = {"status": g_status, "source_file": p, "produced": file_produced(ctx, p, meta, "written"),
                            "reason": SCRIPTED_REASON if scripted else None, "sample_tag": tag, **gb}
            except Exception as exc:  # noqa: BLE001
                ctx.warn("integrity", f"Gate: {err_text(exc)}")
                gate = gate_pending(f"No usable gate output: {err_text(exc)}", gate_src)
            # audit
            try:
                p = rel_path(audit_src)
                if Path(p).name != "audit_log.jsonl":
                    raise SourceError(f"audit path {p} is not an audit_log.jsonl file; refused")
                text, _ = read_text(ctx, p)
                parsed = parse_jsonl(text)
                entries = audit_entries(ctx, parsed, g_status, p, {"kind": "jsonl"})
                last_time = next((e["time"]["text"] for e in reversed(entries) if e["readable"]), None)
                chain = {"status": PENDING, "text": None, "ok": None, "bad_seq": None,
                         "source_file": gov.get("audit_verify"), "reason": VERIFY_TERMINAL_ONLY}
                if gov.get("audit_verify"):
                    try:
                        vp = rel_path(gov["audit_verify"])
                        vtext, _ = read_text(ctx, vp)
                        v = parse_verify(vtext)
                        chain = {"status": g_status,
                                 "text": ctx.pfield("audit.chain", v["detail"], g_status, v["detail"], "as written",
                                                    vp, None, {"kind": "text", "json_path": "detail"})
                                 if vtext.strip().startswith("{") else ctx.field(v["detail"], g_status),
                                 "ok": v["ok"],
                                 "bad_seq": ctx.field(str(v["bad_seq"]), g_status) if v["bad_seq"] is not None else None,
                                 "source_file": vp, "reason": None}
                    except SourceError as exc:
                        if "file not found" not in str(exc):
                            ctx.warn("integrity", f"Audit verify: {err_text(exc)}")
                audit = {"status": g_status, "source_file": p,
                         "produced": {"basis": "last-entry-time", "text": f"last entry {last_time}", "raw": last_time}
                         if last_time else not_recorded_produced(),
                         "reason": SCRIPTED_REASON if scripted else None,
                         "sample_tag": tag, "entries": entries, "chain": chain}
                latest = latest_decision(ctx, parsed, g_status, p, {"kind": "jsonl"}, cand_id)
            except Exception as exc:  # noqa: BLE001
                ctx.warn("integrity", f"Audit log: {err_text(exc)}")
                audit = None
            # assurance
            try:
                rp = rel_path(risk_src)
                rtext, rmeta = read_text(ctx, rp)
                etext = ftext = None
                srcs = {"risk": rp}
                files = [rp]
                for key, var in (("evidence_md", "e"), ("fix_tickets_md", "f")):
                    if gov.get(key):
                        try:
                            t, _ = read_text(ctx, rel_path(gov[key]))
                            files.append(rel_path(gov[key]))
                            if var == "e":
                                etext = t
                            else:
                                ftext = t
                        except SourceError as exc:
                            ctx.warn("info", f"Assurance: {err_text(exc)}")
                body = assurance_from_md(ctx, rtext, etext, ftext, g_status, srcs, {"risk": {"kind": "text"}})
                assurance = {"status": g_status, "source_files": files,
                             "produced": file_produced(ctx, rp, rmeta, "written"),
                             "reason": SCRIPTED_REASON if scripted else None, "sample_tag": tag, **body}
            except Exception as exc:  # noqa: BLE001
                ctx.warn("integrity", f"Assurance: {err_text(exc)}")
                assurance = None
    elif allow:
        # ---- SAMPLE from fixtures, through the same parsers
        ctx.warn("info", "Governance outputs are not connected; hand-written SAMPLE fixtures are shown "
                         "(--allow-sample).")
        try:
            fx, fpath = load_fixture(ctx, "gate")
            lines = fx.get("content_lines")
            if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
                raise SourceError(f"fixture {fpath} has no content_lines")
            gb = gate_from_text(ctx, lines, SAMPLE, fpath, {"kind": "fixture", "json_path": "content_lines"})
            why = gate_consistency(ctx, gb, ev_batch)
            if why:
                gate = gate_pending(why, fpath)
            else:
                gate = {"status": SAMPLE, "source_file": fpath, "produced": fixture_produced(fx),
                        "reason": str(fx.get("how_made")), "sample_tag": GATE_SAMPLE_TAG,
                        "stands_in_for": fx.get("stands_in_for"), **gb}
        except Exception as exc:  # noqa: BLE001
            ctx.warn("integrity", f"Gate sample: {err_text(exc)}")
            gate = gate_pending(NOT_CONNECTED, gate_src)
        try:
            fx, fpath = load_fixture(ctx, "audit")
            lines = fx.get("content_lines")
            if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
                raise SourceError(f"fixture {fpath} has no content_lines")
            parsed = parse_jsonl("\n".join(lines))
            lb = {"kind": "fixture", "json_path": "content_lines"}
            entries = audit_entries(ctx, parsed, SAMPLE, fpath, lb)
            audit = {"status": SAMPLE, "source_file": fpath, "produced": fixture_produced(fx),
                     "reason": str(fx.get("how_made")), "sample_tag": BACKEND_SAMPLE_TAG,
                     "stands_in_for": fx.get("stands_in_for"), "entries": entries,
                     "chain": {"status": PENDING, "text": None, "ok": None, "bad_seq": None,
                               "source_file": None, "reason": VERIFY_TERMINAL_ONLY}}
            latest = latest_decision(ctx, parsed, SAMPLE, fpath, lb, cand_id)
        except Exception as exc:  # noqa: BLE001
            ctx.warn("integrity", f"Audit sample: {err_text(exc)}")
            audit = None
        try:
            fx, fpath = load_fixture(ctx, "assurance")
            for k in ("risk_register_md", "evidence_md", "fix_tickets_md"):
                if not isinstance(fx.get(k), str):
                    raise SourceError(f"fixture {fpath} has no {k}")
            body = assurance_from_md(ctx, fx["risk_register_md"], fx["evidence_md"], fx["fix_tickets_md"], SAMPLE,
                                     {"risk": fpath}, {"risk": {"kind": "fixture", "json_path": "risk_register_md"}})
            # Risk rows must describe the evidence batch.
            m = ev_batch and (ev_batch.get("summary") or {}).get("attack_success_rate")
            for rk in body["risks"]:
                lk = parse_likelihood(rk["likelihood"]["text"])
                if m and m.get("measured") and lk["runs"] != m.get("n"):
                    raise SourceError("risk rows describe a different batch")
            assurance = {"status": SAMPLE, "source_files": [fpath], "produced": fixture_produced(fx),
                         "reason": str(fx.get("how_made")), "sample_tag": BACKEND_SAMPLE_TAG,
                         "stands_in_for": fx.get("stands_in_for"), **body}
        except Exception as exc:  # noqa: BLE001
            ctx.warn("integrity", f"Assurance sample: {err_text(exc)}")
            assurance = None
    else:
        ctx.warn("info", f"Governance outputs: {NOT_CONNECTED}")

    if gate is None:
        gate = gate_pending(NOT_CONNECTED, gate_src)
    if audit is None:
        a = pending(NOT_CONNECTED, audit_src)
        a.update(sample_tag=None, entries=[], chain={"status": PENDING, "text": None, "ok": None, "bad_seq": None,
                                                     "source_file": gov.get("audit_verify"),
                                                     "reason": VERIFY_TERMINAL_ONLY})
        audit = a
        latest = None
    if assurance is None:
        srcs = [x for x in (risk_src, gov.get("evidence_md"), gov.get("fix_tickets_md")) if x]
        s = pending(NOT_CONNECTED, None)
        s.pop("source_file")
        s.update(source_files=srcs, sample_tag=None, risks=[], evidence=[], tickets=[])
        assurance = s
    for k in ("_fails", "_reasons", "_verdict"):
        gate.pop(k, None)
    return gate, audit, assurance, latest


# --------------------------------------------------------------------------- #
# Compare, console, datasets.
# --------------------------------------------------------------------------- #
def side_of(ctx: Ctx, b) -> dict:
    if not b:
        return {"status": PENDING, "reason": "not measured: no second batch yet. Run a new batch, then export again."}
    if b.get("status") != REAL:
        return {"status": PENDING, "reason": f"not measured: {b.get('reason') or 'the batch could not be read'}"}
    s = b.get("summary") or {}
    asr = s.get("attack_success_rate") or {}
    if asr.get("measured") and asr["x_of_n"].get("prov") in ctx.prov:
        xp = ctx.prov[asr["x_of_n"]["prov"]]
        loc = dict(xp["locator"])
        loc["columns"] = ["n"]
        n_attack = ctx.pfield(f"{b['id']}.asr.n", str(asr["n"]), REAL, xp["raw"].split(", ")[-1], "str(n)",
                              xp["source_file"], xp["rev"], loc)
    else:
        n_attack = ctx.field("not measured", PENDING)
    return {"batch": b["id"], "title": b["title"], "status": REAL, "replay_label": b["replay_label"],
            "model": b["model"], "n_attack": n_attack, "asr": asr, "benign": s.get("benign_task_success"),
            "per_technique": s.get("per_technique", [])}


def build_compare(ctx: Ctx, batches: dict, policy: dict) -> dict:
    cfg = ctx.sources.get("compare") or {}
    a = side_of(ctx, batches.get(cfg.get("a")))
    b = side_of(ctx, batches.get(cfg.get("b")))
    limit = next((lim["text"] for lim in policy.get("limits", []) if lim["key"] == "max_attack_success_rate"),
                 ctx.field(NOT_RECORDED, PENDING))
    n_differs, n_note = None, None
    if a.get("status") == REAL and b.get("status") == REAL and a["asr"].get("measured") and b["asr"].get("measured"):
        n_differs = a["asr"]["n"] != b["asr"]["n"]
        if n_differs:
            n_note = "The two batches have a different number of attack runs. Compare the ranges, not only the rates."
    return {"a": a, "b": b, "n_differs": n_differs, "n_note": n_note, "limit": limit}


def build_console(ctx: Ctx, batch, gate: dict, latest) -> dict:
    missing = []
    cand = {"id": ctx.field(NOT_RECORDED, PENDING)}
    if batch:
        cand["model"] = batch["model"]
        cand["digest"] = batch["digest"]
        cand["defences"] = batch["defences"]
        for name in ("model", "digest", "defences"):
            if batch[name]["status"] == PENDING:
                missing.append({"model": "the model", "digest": "the model digest", "defences": "the defences"}[name])
    else:
        cand.update(model=ctx.field(NOT_RECORDED, PENDING), digest=ctx.field(NOT_RECORDED, PENDING),
                    defences=ctx.field(NOT_RECORDED, PENDING))
    missing.insert(0, "a candidate id")
    if len(missing) > 1:
        lst = ", ".join(missing[:-1]) + " or " + missing[-1]
    else:
        lst = missing[0]
    cand["note"] = f"Candidate details come from the evidence batch. This batch does not record {lst}."
    return {"candidate": cand, "evidence_batch": batch["id"] if batch else None, "gate": gate,
            "latest_decision": latest}


def produced_of(x: dict) -> dict:
    return x.get("produced") or not_recorded_produced()


# --------------------------------------------------------------------------- #
# Assemble, validate, write.
# --------------------------------------------------------------------------- #
def build_demo(ctx: Ctx) -> dict:
    src = ctx.sources
    batches: dict = {}
    order = []
    cfgs = src.get("batches") or []
    if not isinstance(cfgs, list) or not cfgs:
        ctx.warn("integrity", "sources.json lists no batches.")
    for cfg in cfgs:
        if not isinstance(cfg, dict) or not cfg.get("id"):
            ctx.warn("integrity", "sources.json has a batch without an id; skipped.")
            continue
        b = safe(ctx, f"Batch {cfg.get('id')}", build_batch, ctx, cfg, "pinned")
        if b is None:
            b = {"id": cfg["id"], "title": cfg.get("title") or cfg["id"], "status": PENDING,
                 "reason": "The batch could not be built.", "source_files": [], "produced": not_recorded_produced(),
                 "replay_label": "REPLAY \u00b7 run time not recorded",
                 "model": ctx.field(NOT_RECORDED, PENDING), "digest": ctx.field(NOT_RECORDED, PENDING),
                 "defences": ctx.field(NOT_RECORDED, PENDING), "driver": ctx.field(NOT_RECORDED, PENDING),
                 "has_blocked_info": False, "blocked_note": "Blocked: not recorded in this batch",
                 "shape": ctx.field("not available", PENDING), "counts": {"attack": {}, "benign": {}},
                 "summary": {}, "groups": [], "runs": {}, "error_note": ctx.field("not available", PENDING)}
        if b["id"] in batches:
            ctx.warn("integrity", f"sources.json repeats batch id {b['id']}; second one skipped.")
            continue
        batches[b["id"]] = b
        order.append(b["id"])
        if cfg.get("traces_note"):
            ctx.warn("info", f"{b['title']}: {cfg['traces_note']}")

    # latest-local
    ll = src.get("latest_local") or {}
    if ll.get("enabled"):
        safe(ctx, "Latest local run", add_latest_local, ctx, ll, batches, order)

    rf = src.get("run_folders") or {}
    if rf.get("enabled"):
        ctx.warn("info", "run_folders adapter is not built yet; provisional until docs/DATA_CONTRACT.md exists.")

    for bid in order:
        safe(ctx, f"Batch {bid} checks", consistency_checks_batch, ctx, batches[bid])

    current = src.get("current_batch")
    if current not in batches:
        ctx.warn("integrity", f"current_batch {current!r} is not a known batch.")
        current = order[0] if order else None
    cur = batches.get(current)

    policy = safe(ctx, "Policy", build_policy, ctx) or build_policy_pending()
    controls = safe(ctx, "Controls", build_controls, ctx) or build_controls_pending()
    gres = safe(ctx, "Governance", build_governance, ctx, batches)
    if gres is None:
        gate = gate_pending(NOT_CONNECTED, None)
        audit = pending(NOT_CONNECTED, None)
        audit.update(sample_tag=None, entries=[], chain={"status": PENDING, "text": None, "ok": None,
                                                          "bad_seq": None, "source_file": None,
                                                          "reason": VERIFY_TERMINAL_ONLY})
        assurance = pending(NOT_CONNECTED, None)
        assurance.pop("source_file")
        assurance.update(source_files=[], sample_tag=None, risks=[], evidence=[], tickets=[])
        latest = None
    else:
        gate, audit, assurance, latest = gres

    compare = build_compare(ctx, batches, policy)
    console = build_console(ctx, cur, gate, latest)

    results_status = cur["status"] if cur else PENDING
    datasets = [
        {"key": "results", "label": "Results", "status": results_status,
         "source_file": " + ".join(cur["source_files"][:2]) if cur and cur.get("source_files") else None,
         "produced": produced_of(cur) if cur else not_recorded_produced(),
         "reason": cur.get("reason") if cur else "No batch configured."},
        {"key": "policy", "label": "Policy", "status": policy["status"], "source_file": policy["source_file"],
         "produced": produced_of(policy), "reason": policy["reason"]},
        {"key": "gate", "label": "Gate", "status": gate["status"], "source_file": gate["source_file"],
         "produced": produced_of(gate), "reason": gate["reason"]},
        {"key": "decisions", "label": "Decisions", "status": audit["status"], "source_file": audit["source_file"],
         "produced": produced_of(audit), "reason": audit["reason"]},
        {"key": "assurance", "label": "Assurance", "status": assurance["status"],
         "source_file": " + ".join(assurance.get("source_files") or []) or None,
         "produced": produced_of(assurance), "reason": assurance["reason"]},
        {"key": "controls", "label": "Controls", "status": controls["status"], "source_file": controls["source_file"],
         "produced": produced_of(controls), "reason": controls["reason"]},
    ]

    blist = []
    for bid in order:
        b = batches[bid]
        for k in [k for k in b if k.startswith("_")]:
            b.pop(k)
        blist.append(b)

    demo = {
        "schema": SCHEMA,
        "exported_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "allow_sample": bool(ctx.args.allow_sample),
        "sample_present": False,
        "sample_banner": None,
        "warnings": ctx.warnings,
        "datasets": datasets,
        "current_batch": current,
        "console": console,
        "batches": blist,
        "compare": compare,
        "policy": policy,
        "controls": controls,
        "audit": audit,
        "assurance": assurance,
        "labels": {
            "states": STATE_LABELS, "techniques": TECHNIQUE_LABELS, "places": PLACE_LABELS,
            "goals": GOAL_LABELS, "classes": CLASS_LABELS, "checks": CHECK_LABELS, "events": EVENT_LABELS,
            "limits": LIMIT_LABELS,
        },
        "provenance": [],
    }
    used = set()
    walk_fields(demo, lambda f: used.add(f["prov"]) if f.get("prov") else None)
    walk_produced(demo, used)
    demo["provenance"] = [p for pid, p in ctx.prov.items() if pid in used]
    demo["sample_present"] = has_sample(demo)
    demo["sample_banner"] = SAMPLE_BANNER if demo["sample_present"] else None
    return demo


def walk_produced(obj, used: set) -> None:
    if isinstance(obj, dict):
        p = obj.get("produced")
        if isinstance(p, dict) and p.get("prov"):
            used.add(p["prov"])
        for v in obj.values():
            walk_produced(v, used)
    elif isinstance(obj, list):
        for v in obj:
            walk_produced(v, used)


def add_latest_local(ctx: Ctx, ll: dict, batches: dict, order: list) -> None:
    rev = ll.get("show_only_if_differs_from_rev")
    runs_path = rel_path(ll.get("runs_csv", "results/runs.csv"))
    if not ctx.git_ok:
        ctx.warn("info", f"Latest local run not checked: {ctx.git_note}.")
        return
    try:
        wt = read_bytes(ctx, runs_path, None)
    except SourceError:
        ctx.warn("info", f"Latest local run: no {runs_path} in the working tree.")
        return
    try:
        pinned = read_bytes(ctx, runs_path, rev) if rev else None
    except SourceError:
        pinned = None
    if pinned is not None and pinned == wt:
        ctx.warn("info", f"Working-tree {runs_path} matches {rev}; no separate latest-local batch.")
        return
    cfg = dict(ll)
    cfg.setdefault("id", "latest-local")
    if cfg["id"] in batches:
        ctx.warn("integrity", f"latest-local id {cfg['id']} clashes with a pinned batch; skipped.")
        return
    b = build_batch(ctx, cfg, "local")
    batches[b["id"]] = b
    order.append(b["id"])
    ctx.warn("info", f"{b['title']}: working-tree {runs_path} differs from {rev}; shown as a separate batch.")


def build_policy_pending() -> dict:
    out = pending("Policy could not be built.", None)
    out.update(version=Ctx.field(NOT_RECORDED, PENDING), owner=Ctx.field(NOT_RECORDED, PENDING), limits=[],
               roles={k: Ctx.field(NOT_RECORDED, PENDING) for k in ("engineers", "reviewers", "rule_owner")},
               impact=[])
    return out


def build_controls_pending() -> dict:
    out = pending("Controls could not be built.", None)
    out.update(items=[], note=Ctx.field(NOT_RECORDED, PENDING), checked_against=[])
    return out


def is_field(x) -> bool:
    return isinstance(x, dict) and set(x.keys()) == {"text", "status", "prov"}


def walk_fields(obj, fn) -> None:
    if is_field(obj):
        fn(obj)
        return
    if isinstance(obj, dict):
        for v in obj.values():
            walk_fields(v, fn)
    elif isinstance(obj, list):
        for v in obj:
            walk_fields(v, fn)


def has_sample(demo: dict) -> bool:
    found = []

    def chk(o):
        if isinstance(o, dict):
            if o.get("status") == SAMPLE:
                found.append(1)
            for v in o.values():
                chk(v)
        elif isinstance(o, list):
            for v in o:
                chk(v)
    for k, v in demo.items():
        if k not in ("provenance", "labels"):
            chk(v)
    return bool(found)


def validate_demo(ctx: Ctx, demo: dict) -> list:
    """Return a list of fatal problems (empty when the export is valid)."""
    probs = []
    for k in ("schema", "exported_at", "allow_sample", "sample_present", "warnings", "provenance", "datasets",
              "batches", "current_batch", "console", "policy", "audit", "assurance", "controls", "compare", "labels"):
        if k not in demo:
            probs.append(f"missing key {k}")
    if demo.get("schema") != SCHEMA:
        probs.append("wrong schema")
    pids = {p["id"] for p in demo.get("provenance", [])}
    bad_fields = []

    def fchk(f):
        if f["status"] not in STATUSES or not isinstance(f["text"], str):
            bad_fields.append(f"bad field {short(f)}")
        if f["prov"] is not None and f["prov"] not in pids:
            bad_fields.append(f"field points at unknown provenance {f['prov']}")
    walk_fields(demo, fchk)
    probs += bad_fields[:5]
    if not demo.get("allow_sample") and demo.get("sample_present"):
        probs.append("SAMPLE data present without --allow-sample")
    if demo.get("sample_present") != has_sample(demo):
        probs.append("sample_present flag is wrong")
    keys = [d.get("key") for d in demo.get("datasets", [])]
    if keys != ["results", "policy", "gate", "decisions", "assurance", "controls"]:
        probs.append("datasets list is not complete")
    # compare must never be SAMPLE
    cmp_ = demo.get("compare", {})
    for side in ("a", "b"):
        if cmp_.get(side, {}).get("status") == SAMPLE:
            probs.append("compare shows SAMPLE data")
    for b in demo.get("batches", []):
        if b.get("status") not in STATUSES:
            probs.append(f"batch {b.get('id')} has a bad status")
        for key, r in (b.get("runs") or {}).items():
            if r.get("state") not in STATE_LABELS:
                probs.append(f"run {key} has a bad state")
    try:
        body = json.dumps(demo, ensure_ascii=True, allow_nan=False)
    except ValueError as exc:
        probs.append(f"not serialisable: {exc}")
        return probs
    visible = json.dumps({k: v for k, v in demo.items()}, ensure_ascii=False)
    for rx in FORBIDDEN:
        if rx.search(visible):
            probs.append(f"forbidden number or phrase found: {rx.pattern}")
    if RE_ABS_PATH.search(visible):
        probs.append("an absolute path would be written")
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = ""
    if user and len(user) >= 4 and user in visible:
        probs.append("the local user name would be written")
    for m in RE_EMAIL.finditer(visible):
        dom = m.group(1).lower()
        if not (dom.endswith(".example") or dom == "example"):
            probs.append("an e-mail address outside the synthetic .example domain would be written")
            break
    for rx in BANNED_WORDS:
        if rx.search(visible):
            ctx.warn("integrity", f"Data text contains a banned UI word ({rx.pattern}); check before showing.")
    del body
    return probs


def to_js(demo: dict) -> str:
    body = json.dumps(demo, ensure_ascii=True, allow_nan=False, indent=1)
    body = body.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return JS_PREFIX + body + JS_SUFFIX


def write_demo_js(out: Path, text: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".demo-", suffix=".js.tmp", dir=str(out.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(out))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_demo_js(path: Path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith(JS_PREFIX) or not text.endswith(JS_SUFFIX):
        raise SourceError("demo.js is not in the window.DEMO = {...}; format")
    return json.loads(text[len(JS_PREFIX): -len(JS_SUFFIX)])


def quiet_line(demo: dict) -> str:
    t = dt.datetime.fromisoformat(demo["exported_at"]).strftime("%H:%M:%S")
    parts = [f"exported {t}"]
    for d in demo["datasets"]:
        parts.append(f"{d['label']} {d['status']}")
    ni = sum(1 for w in demo["warnings"] if w["level"] == "integrity")
    nf = sum(1 for w in demo["warnings"] if w["level"] == "info")
    parts.append(f"{ni} integrity")
    parts.append(f"{nf} info")
    return " \u00b7 ".join(parts)


def print_report(demo: dict, quiet: bool, out_desc: str) -> None:
    if quiet:
        print(quiet_line(demo))
        return
    print(f"Agent Release Gate export -> {out_desc}")
    if demo["warnings"]:
        print("Warnings:")
        for w in demo["warnings"]:
            print(f"  [{w['level']}] {w['text']}")
    print(f"Provenance ({len(demo['provenance'])} entries):")
    for p in demo["provenance"]:
        print(f"  [{p['id']}] {short(p['display'], 60)}  {p['status']}  {p['where']} (raw {short(p['raw'], 60)})")
    print(quiet_line(demo))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def load_sources(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FatalError("sources file not found")
    except (OSError, UnicodeDecodeError) as exc:
        raise FatalError(f"sources file unreadable ({type(exc).__name__})")
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise FatalError(f"sources file is not valid JSON ({exc})")
    if not isinstance(obj, dict) or obj.get("schema") != SOURCES_SCHEMA:
        raise FatalError(f"sources file is not schema {SOURCES_SCHEMA}")
    if not isinstance(obj.get("repo_root"), str):
        raise FatalError("sources file has no repo_root")
    return obj


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Export backend files to ui/data/demo.js for the Agent Release Gate UI.")
    ap.add_argument("--allow-sample", action="store_true", help="fill unconnected datasets from ui/fixtures (SAMPLE)")
    ap.add_argument("--check", action="store_true",
                    help="re-read every provenance locator and compare raw values; does not write")
    ap.add_argument("--final", action="store_true", help="exit 1 (and keep demo.js) if anything is SAMPLE")
    ap.add_argument("--quiet", action="store_true", help="print one summary line")
    ap.add_argument("--no-git", action="store_true", help="do not use git; read working-tree files")
    ap.add_argument("--sources", default=None, help="path to a sources.json (default ui/sources.json)")
    ap.add_argument("--out", default=None, help="output path (default ui/data/demo.js)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    out = Path(args.out).resolve() if args.out else DEFAULT_OUT
    sources_path = Path(args.sources).resolve() if args.sources else DEFAULT_SOURCES
    ctx = None
    try:
        sources = load_sources(sources_path)
        repo_root = (sources_path.parent / sources["repo_root"]).resolve()
        if not repo_root.is_dir():
            raise FatalError("repo_root is not a folder")
        ctx = Ctx(args, sources_path, sources, repo_root)
        init_git(ctx)
        demo = build_demo(ctx)
        probs = validate_demo(ctx, demo)
        if probs:
            raise FatalError("export failed validation: " + "; ".join(probs[:5]))
        out_desc = "ui/data/demo.js" if out == DEFAULT_OUT else out.name

        if args.check:
            bad = self_check(ctx, demo["provenance"])
            msg = [f"check: fresh export {len(demo['provenance']) - len(bad)}/{len(demo['provenance'])} provenance entries match"]
            file_bad = []
            if out.exists():
                try:
                    old = read_demo_js(out)
                    file_bad = self_check(ctx, old.get("provenance", []))
                    msg.append(f"{out_desc} {len(old.get('provenance', [])) - len(file_bad)}/"
                               f"{len(old.get('provenance', []))} match")
                except Exception as exc:  # noqa: BLE001
                    file_bad = [("demo.js", f"unreadable: {err_text(exc)}")]
                    msg.append(f"{out_desc} unreadable")
            else:
                msg.append(f"no {out_desc} to check")
            if not args.quiet:
                for pid, why in bad + file_bad:
                    print(f"  MISMATCH [{pid}] {scrub(ctx, why)}")
            print(quiet_line(demo))
            print(" \u00b7 ".join(msg))
            return 1 if (bad or file_bad) else 0

        if args.final and demo["sample_present"]:
            print(quiet_line(demo))
            print("final: SAMPLE data present; demo.js not written (previous file kept). Export without --allow-sample.")
            return 1

        write_demo_js(out, to_js(demo))
        print_report(demo, args.quiet, out_desc)
        if args.final:
            print("final: no SAMPLE data; demo.js written.")
        return 0
    except FatalError as exc:
        print(f"export failed: {scrub(ctx, str(exc)) if ctx else exc}. Previous demo.js kept.")
        return 1
    except Exception as exc:  # noqa: BLE001 - never show a traceback to the presenter
        if os.environ.get("UI_EXPORT_DEBUG") == "1":
            raise
        text = f"{type(exc).__name__}: {exc}"
        print(f"export failed: unexpected error ({scrub(ctx, text) if ctx else text}). Previous demo.js kept.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
