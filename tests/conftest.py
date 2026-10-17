"""Shared fixtures for the governance/assurance tests.

A tiny factory that builds a runs DataFrame with the exact columns
:func:`lab.metrics.build_runs_frame` produces, so the gate and the risk
register can be tested with hand-chosen numbers and no model.
"""
from __future__ import annotations

import pandas as pd
import pytest

_COLUMNS = {
    "test_id": "ATK-0001",
    "kind": "attack",
    "goal": "G",
    "technique": "T1_plain",
    "place": "P1_doc_body",
    "rep": 1,
    "user_task": "",
    "tool_calls": "",
    "violated": False,
    "violation_classes": "",
    "oracle_detail": "",
    "benign_success": False,
    "error": "",
    "trace_path": "",
}


@pytest.fixture
def make_runs():
    """Return a factory: a list of partial row dicts -> a full runs DataFrame."""

    def _make(rows: list[dict]) -> pd.DataFrame:
        full = []
        for i, row in enumerate(rows):
            record = dict(_COLUMNS)
            record["test_id"] = f"ATK-{i + 1:04d}"
            record.update(row)
            if isinstance(record["violation_classes"], (list, tuple)):
                record["violation_classes"] = ";".join(record["violation_classes"])
            full.append(record)
        return pd.DataFrame(full, columns=list(_COLUMNS))

    return _make
