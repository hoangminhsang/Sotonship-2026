"""scripts/run_governance.py never deletes an audit log, logs refusals, and documents its defaults."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from lab.governance import audit

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def gov():
    spec = importlib.util.spec_from_file_location("run_governance_cli", ROOT / "scripts" / "run_governance.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_governance_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


def _demo(gov, out: Path, *extra: str) -> int:
    return gov.main(["--out", str(out), "--no-tamper-demo", *extra])


def test_default_out_is_the_cli_demo_folder(gov):
    default = Path(gov.build_parser().get_default("out"))
    assert default == ROOT / "governance_out" / "cli_demo"
    assert default != ROOT / "governance_out"  # the menu app's workspace is never the CLI's --out


def test_existing_audit_log_is_rotated_never_deleted(gov, tmp_path, capsys):
    out = tmp_path / "gov"
    assert _demo(gov, out) == 0
    log = out / "audit_log.jsonl"
    first = log.read_bytes()
    assert audit.verify_log(log).ok

    assert _demo(gov, out) == 0
    rotated = sorted(p for p in out.glob("audit_log.*.jsonl") if p != log)
    assert len(rotated) == 1, list(out.iterdir())
    assert rotated[0].read_bytes() == first          # old log intact, byte for byte
    assert audit.verify_log(rotated[0]).ok           # and still verifies
    assert audit.verify_log(log).ok                  # the new log is a fresh chain
    assert audit.read_log(log)[0]["seq"] == 1
    assert rotated[0].name in capsys.readouterr().out  # the script says what it did


def test_keep_log_appends(gov, tmp_path):
    out = tmp_path / "gov"
    assert _demo(gov, out) == 0
    log = out / "audit_log.jsonl"
    first = log.read_bytes()
    n = len(audit.read_log(log))

    assert _demo(gov, out, "--keep-log") == 0
    assert log.read_bytes().startswith(first)        # nothing rewritten
    assert len(audit.read_log(log)) == 2 * n
    assert audit.verify_log(log).ok
    assert not [p for p in out.glob("audit_log.*.jsonl") if p != log]  # no rotation


def test_self_review_refusal_is_logged(gov, tmp_path):
    out = tmp_path / "gov"
    assert _demo(gov, out) == 0
    events = audit.read_log(out / "audit_log.jsonl")
    refused = [e for e in events if e.get("event") == "decision_refused"]
    assert len(refused) == 1
    assert refused[0]["candidate_id"] == "RC-001"
    assert refused[0]["reviewer"] == "tom" and refused[0]["action"] == "APPROVE"
    assert refused[0]["detail"]


def test_help_text_states_real_defaults(gov):
    import re

    text = gov.build_parser().format_help()
    assert "(default: True)" not in text
    assert "--no-tamper-demo" in text
    joined = re.sub(r"\n\s+", "", text)  # argparse wraps the long absolute default path
    assert "governance_out/cli_demo" in joined
