"""Assurance -- the long loop.

Keeps the agent's story over time. Every problem the red team finds becomes a
risk with an owner; a risk closes only when a re-test proves the fix works.
Every record here is built *by code* from the test data, not typed by hand, and
the same violation-class name (e.g. ``V_exfiltration``) links everything.

Module map:
    risks        -- risk register + fix tickets (likelihood from measured rates)
    inventory    -- AI inventory card from the fingerprint
    evidence     -- evidence register (control -> proof -> file)
    thread       -- traceability: one finding from first attack to closed risk
    summary_page -- one-page manager view

Framework labels (OWASP / MITRE ATLAS / NIST) live in ``control_map.yaml``.
"""
from __future__ import annotations

from pathlib import Path

import yaml

CONTROL_MAP_PATH = Path(__file__).resolve().parent / "control_map.yaml"


def load_control_map(path: Path | str = CONTROL_MAP_PATH) -> dict:
    """Load the human-authored control -> framework-label table."""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


__all__ = [
    "risks",
    "inventory",
    "evidence",
    "thread",
    "summary_page",
    "load_control_map",
    "CONTROL_MAP_PATH",
]
