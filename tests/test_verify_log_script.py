"""Tests for scripts/verify_log.py -- the read-only log verifier (D-05).

Exit codes: 0 intact, 1 tampered (naming the entry), 2 usage error.
A missing file must not print a traceback.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta
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


# --------------------------------------------------------------------------- #
# --json OUT: the same core fields as <out>/audit_verify.json.
# --------------------------------------------------------------------------- #
JSON_KEYS = {"schema", "ok", "bad_seq", "kind", "detail", "entries", "last_hash", "verified_at", "mode",
             "tamper_demo"}


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_json_for_an_intact_log(tmp_path, capsys, verify_log_main):
    log, out = tmp_path / "audit.jsonl", tmp_path / "verify.json"
    _seed(log)
    before = log.read_bytes()
    assert verify_log_main([str(log), "--json", str(out)]) == 0
    doc = _read(out)
    check = audit.verify_log(log)
    assert set(doc) == JSON_KEYS and doc["schema"] == "audit-verify/1"
    assert doc["ok"] is True and doc["bad_seq"] is None and doc["kind"] is None
    assert doc["entries"] == 4 == check.entries and doc["last_hash"] == check.last_hash and doc["detail"] == check.detail
    assert doc["mode"] is None and doc["tamper_demo"] is None            # only the governance CLI knows these
    assert datetime.fromisoformat(doc["verified_at"]).utcoffset() == timedelta(0)
    assert log.read_bytes() == before                                     # still read-only
    assert "Chain intact" in capsys.readouterr().out                      # the text report is unchanged


def test_json_for_a_tampered_copy_names_the_entry_and_exits_one(tmp_path, verify_log_main):
    log, out = tmp_path / "audit.jsonl", tmp_path / "verify.json"
    _seed(log)
    _forge(log)
    assert verify_log_main(["--json", str(out), str(log)]) == 1           # the option may come first
    doc = _read(out)
    assert set(doc) == JSON_KEYS and doc["ok"] is False and doc["bad_seq"] == 3 and doc["kind"] == "edited"
    assert doc["detail"] == audit.verify_log(log).detail and doc["last_hash"] is None and doc["entries"] == 4


def test_json_for_an_empty_log(tmp_path, verify_log_main):
    log, out = tmp_path / "empty.jsonl", tmp_path / "verify.json"
    log.write_text("", encoding="utf-8")
    assert verify_log_main([str(log), "--json", str(out)]) == 0
    doc = _read(out)
    assert doc["ok"] is True and doc["entries"] == 0 and doc["last_hash"] is None and "no entries" in doc["detail"]


def test_json_equals_form_and_same_keys_as_the_governance_cli(tmp_path, verify_log_main):
    log, out = tmp_path / "audit.jsonl", tmp_path / "verify.json"
    _seed(log)
    assert verify_log_main([str(log), f"--json={out}"]) == 0
    spec = importlib.util.spec_from_file_location("rg_for_verify_keys", SCRIPT.parent / "run_governance.py")
    gov = importlib.util.module_from_spec(spec)
    sys.modules["rg_for_verify_keys"] = gov
    spec.loader.exec_module(gov)
    cli_doc = gov.audit_verify_document(audit.verify_log(log), "SCRIPTED")
    assert set(_read(out)) == set(cli_doc) == JSON_KEYS                   # one shape, two writers


def test_json_is_not_written_for_a_missing_log(tmp_path, capsys, verify_log_main):
    out = tmp_path / "verify.json"
    assert verify_log_main([str(tmp_path / "nope.jsonl"), "--json", str(out)]) == 2
    assert not out.exists() and "Traceback" not in capsys.readouterr().err


def test_json_refuses_to_overwrite_the_log_it_verifies(tmp_path, capsys, verify_log_main):
    log = tmp_path / "audit.jsonl"
    _seed(log)
    before = log.read_bytes()
    assert verify_log_main([str(log), "--json", str(log)]) == 2
    assert log.read_bytes() == before and "same file" in capsys.readouterr().err


def test_json_without_a_value_or_with_extra_arguments_is_a_usage_error(tmp_path, capsys, verify_log_main):
    log = tmp_path / "audit.jsonl"
    _seed(log)
    assert verify_log_main([str(log), "--json"]) == 2
    assert verify_log_main([str(log), "--json", str(tmp_path / "a.json"), str(log)]) == 2
    assert verify_log_main([str(log), "--bogus"]) == 2
    assert capsys.readouterr().err.count("usage: verify_log.py PATH [--json OUT]") == 3
    assert not (tmp_path / "a.json").exists()


def test_json_that_cannot_be_written_keeps_the_verdict_and_never_shows_a_traceback(tmp_path, capsys, verify_log_main):
    log = tmp_path / "audit.jsonl"
    _seed(log)
    missing_dir = tmp_path / "no-such-folder" / "verify.json"
    assert verify_log_main([str(log), "--json", str(missing_dir)]) == 2     # intact log, but the file asked for is missing
    err = capsys.readouterr().err
    assert "verify.json" in err and "Traceback" not in err
    _forge(log)
    assert verify_log_main([str(log), "--json", str(missing_dir)]) == 1     # tampering still wins over the write failure


def test_json_through_the_command_line(tmp_path):
    log, out = tmp_path / "audit.jsonl", tmp_path / "verify.json"
    _seed(log)
    proc = subprocess.run([sys.executable, str(SCRIPT), str(log), "--json", str(out)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert _read(out)["entries"] == 4 and not list(tmp_path.glob("*.tmp"))


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
