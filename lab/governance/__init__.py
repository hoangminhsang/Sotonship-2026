"""Governance -- the short loop (release gate).

Every time the agent changes, this turns the red team's numbers into a human
decision and a tamper-evident record. The tool never releases on its own.

Module map:
    candidate -- fingerprint of exactly what was tested
    gate      -- deterministic PASS/FAIL verdict against the policy
    decision  -- roles, Approve/Block/Override, re-test rule
    audit     -- hash-chained append-only decision log

The policy the gate reads lives in ``policy.yaml`` next to these modules.
"""
from __future__ import annotations

from pathlib import Path

import yaml

POLICY_PATH = Path(__file__).resolve().parent / "policy.yaml"


def load_policy(path: Path | str = POLICY_PATH) -> dict:
    """Load the release rule book."""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


__all__ = ["candidate", "gate", "decision", "roles", "audit", "load_policy", "POLICY_PATH"]
