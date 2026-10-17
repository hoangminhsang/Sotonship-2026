"""Tests for scripts/verify_log.py -- the read-only log verifier (D-05).

Exit codes: 0 intact, 1 tampered (naming the entry), 2 usage error.
A missing file must not print a traceback.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lab.governance import audit

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_log.py"


@pytest.fixture(scope="module")
def verify_log_main():
    spec = importlib.util.spec_from_file_location("verify_log_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def _seed(path: Path) -> None:
    audit.append(path, {"event": "submit", "candidate_id": "RC-001", "by": "tom"})
    audit.append(path, {"event": "verdict", "candidate_id": "RC-001", "verdict": "FAIL"})
    audit.append(path, {"event": "decision", "candidate_id": "RC-001", "decision": "BLOCK",
                        "reviewer": "sarah", "reason": "leaks data"})
    audit.append(path, {"event": "submit", "candidate_id": "RC-002", "by": "tom"})


def _forge(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[2])
    rec["decision"] = "APPROVE"
    lines[2] = json.dumps(rec, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_intact_log_exits_zero(tmp_path, capsys, verify_log_main):
    log = tmp_path / "audit.jsonl"
    _seed(log)
    assert verify_log_main([str(log)]) == 0
    out = capsys.readouterr().out
    assert "intact" in out and "4 entries" in out


def test_tampered_copy_exits_one_and_names_the_entry(tmp_path, capsys, verify_log_main):
    log = tmp_path / "audit.jsonl"
    _seed(log)
    copy = tmp_path / "copy.jsonl"
    copy.write_text(log.read_text(encoding="utf-8"), encoding="utf-8")
    _forge(copy)

    assert verify_log_main([str(copy)]) == 1
    out = capsys.readouterr().out
    assert "TAMPERING" in out and "#3" in out and "edited" in out
    assert audit.verify_log(log).ok  # the original is untouched


def test_malformed_line_exits_one_without_traceback(tmp_path, capsys, verify_log_main):
    log = tmp_path / "audit.jsonl"
    _seed(log)
    lines = log.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1][:-1]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert verify_log_main([str(log)]) == 1
    out = capsys.readouterr().out
    assert "entry 2 is not valid JSON" in out


def test_empty_log_exits_zero_with_warning(tmp_path, capsys, verify_log_main):
    log = tmp_path / "empty.jsonl"
    log.write_text("", encoding="utf-8")
    assert verify_log_main([str(log)]) == 0
    assert "no entries" in capsys.readouterr().out


def test_missing_file_is_a_usage_error_not_a_traceback(tmp_path, capsys, verify_log_main):
    assert verify_log_main([str(tmp_path / "nope.jsonl")]) == 2
    captured = capsys.readouterr()
    assert "no such file" in captured.err
    assert "Traceback" not in captured.err + captured.out


def test_no_arguments_is_a_usage_error(capsys, verify_log_main):
    assert verify_log_main([]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_cli_entry_point_exit_code(tmp_path):
    log = tmp_path / "audit.jsonl"
    _seed(log)
    _forge(log)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(log)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 1, proc.stderr
    assert "#3" in proc.stdout
