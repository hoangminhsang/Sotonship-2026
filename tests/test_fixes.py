"""Tests for the fix suggester."""
from __future__ import annotations

from lab import fixes, oracle


def test_every_oracle_class_has_a_fix():
    for cls in oracle.ALL_CLASSES:
        assert cls in fixes.FIXES, cls


def test_suggest_maps_classes_to_controls():
    out = fixes.suggest(["V_read_restricted"], injected=False)
    assert len(out) == 1
    assert out[0]["control"] == fixes.FIXES["V_read_restricted"]["control"]


def test_suggest_adds_trust_label_when_injected():
    out = fixes.suggest(["V_exfiltration"], injected=True)
    assert fixes.TRUST_LABEL_FIX in out
    assert len(out) == 2


def test_suggest_no_trust_label_when_not_injected():
    out = fixes.suggest(["V_exfiltration"], injected=False)
    assert fixes.TRUST_LABEL_FIX not in out


def test_every_fix_has_required_keys():
    required = {"control", "detail", "after_defence"}
    for entry in fixes.FIXES.values():
        assert required <= set(entry)
    assert required <= set(fixes.TRUST_LABEL_FIX)
