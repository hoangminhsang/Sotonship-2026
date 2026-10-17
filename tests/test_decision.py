"""Tests for roles, Approve/Block/Override and the re-test rule."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from lab.governance import candidate, decision, load_policy

# A fixed "now" so the expiry rules are deterministic whenever the suite runs.
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def policy() -> dict:
    return load_policy()


def _decide(policy, **kw):
    base = dict(
        candidate_id="RC-003", submitted_by="tom", verdict_passed=True,
        policy=policy, reviewer="sarah", action="APPROVE", now=NOW,
    )
    base.update(kw)
    return decision.decide(**base)


def test_engineer_cannot_approve_own_candidate(policy):
    with pytest.raises(decision.DecisionError, match="cannot review"):
        _decide(policy, reviewer="tom")


def test_non_reviewer_refused(policy):
    with pytest.raises(decision.DecisionError, match="not a reviewer"):
        _decide(policy, reviewer="mallory")


def test_cannot_approve_a_failing_verdict(policy):
    with pytest.raises(decision.DecisionError, match="cannot APPROVE"):
        _decide(policy, verdict_passed=False)


def test_block_needs_a_reason(policy):
    with pytest.raises(decision.DecisionError, match="needs a short reason"):
        _decide(policy, action="BLOCK", reason="")


def test_block_with_reason_ok(policy):
    event = _decide(policy, action="BLOCK", verdict_passed=False, reason="leaks data")
    assert event["decision"] == "BLOCK" and event["reason"] == "leaks data"


def test_override_requires_fail_reason_and_expiry(policy):
    with pytest.raises(decision.DecisionError, match="only for a FAIL"):
        _decide(policy, action="OVERRIDE", verdict_passed=True, reason="x", expiry="2026-10-24")
    with pytest.raises(decision.DecisionError, match="needs a reason"):
        _decide(policy, action="OVERRIDE", verdict_passed=False, expiry="2026-10-24")
    with pytest.raises(decision.DecisionError, match="needs an expiry"):
        _decide(policy, action="OVERRIDE", verdict_passed=False, reason="low harm")


def test_override_ok_with_everything(policy):
    event = _decide(
        policy, action="OVERRIDE", verdict_passed=False,
        reason="low harm, ship now", expiry="2026-10-24",
    )
    assert event["decision"] == "OVERRIDE" and event["expiry"] == "2026-10-24"


def test_override_expiry_detected():
    event = {"decision": "OVERRIDE", "expiry": "2026-10-24"}
    assert decision.override_expired(event, today=date(2026, 10, 25))
    assert not decision.override_expired(event, today=date(2026, 10, 23))


# --- Fix 4C: the submitter can never approve or override their own candidate --

def test_submitter_cannot_override_own_candidate(policy):
    with pytest.raises(decision.DecisionError, match="cannot review"):
        _decide(policy, reviewer="tom", action="OVERRIDE", verdict_passed=False,
                reason="low harm", expiry="2026-10-24")


@pytest.mark.parametrize("alias", ["Tom", "TOM", " tom "])
def test_submitter_alias_cannot_approve_own_candidate(policy, alias):
    # Even if a spelling variant of the submitter was made a reviewer, it is
    # still the same person: identity comparisons ignore case and spaces.
    policy["roles"]["reviewers"].append(alias)
    with pytest.raises(decision.DecisionError, match="cannot review"):
        _decide(policy, reviewer=alias)


def test_reviewer_membership_ignores_case_and_spaces(policy):
    event = _decide(policy, reviewer=" Sarah ")
    assert event["decision"] == "APPROVE"


# --- Decisions extra: B-04 expiry in the future, B-25 exact format, B-26 action=None

def test_override_expiry_must_be_in_the_future(policy):
    today = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
    with pytest.raises(decision.DecisionError, match="future"):  # today is not enough
        _decide(policy, action="OVERRIDE", verdict_passed=False, reason="r",
                expiry="2026-10-24", now=today)
    with pytest.raises(decision.DecisionError, match="future"):  # past
        _decide(policy, action="OVERRIDE", verdict_passed=False, reason="r",
                expiry="2026-10-01", now=today)
    ok = _decide(policy, action="OVERRIDE", verdict_passed=False, reason="r",
                 expiry="2026-10-25", now=today)
    assert ok["expiry"] == "2026-10-25"


@pytest.mark.parametrize("bad", ["20261024", "2026-1-5", "24/10/2026", "2026-10-24T00:00", "2026-13-01"])
def test_override_expiry_needs_exact_iso_format(policy, bad):
    with pytest.raises(decision.DecisionError, match="YYYY-MM-DD"):
        _decide(policy, action="OVERRIDE", verdict_passed=False, reason="r", expiry=bad)


def test_missing_action_is_a_decision_error(policy):
    with pytest.raises(decision.DecisionError, match="unknown action"):
        _decide(policy, action=None)


def _fp(**kw):
    base = dict(
        candidate_id="RC-001", submitted_by="tom", model="qwen2.5:7b-instruct",
        model_digest="sha256:aaa", system_prompt="hello", tools=["read_doc"],
        defences=[], rules_text="r", test_set_text="t",
    )
    base.update(kw)
    return candidate.fingerprint(**base)


def test_changed_fingerprint_invalidates_approval():
    approved = _fp()
    same = _fp(candidate_id="RC-001b", submitted_by="sarah")  # bookkeeping differs only
    changed = _fp(model="llama3.1:8b")  # identity field differs
    assert decision.approval_still_valid(approved.identity_hash(), same.identity_hash())
    assert not decision.approval_still_valid(approved.identity_hash(), changed.identity_hash())
