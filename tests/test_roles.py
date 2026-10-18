"""Tests for governed, logged role editing."""
from __future__ import annotations

import pytest

from lab.governance import decision, load_policy, roles


@pytest.fixture
def policy() -> dict:
    return load_policy()


def test_rule_owner_can_add_a_reviewer(policy):
    event = roles.change_role(policy, actor="security-lead", person="bob", role="reviewers", action="add")
    assert roles.has_role(policy, "bob", "reviewers")
    assert event["event"] == "role_change" and event["person"] == "bob"


def test_non_owner_cannot_change_roles(policy):
    with pytest.raises(roles.RoleError, match="only the rule owner"):
        roles.change_role(policy, actor="tom", person="tom", role="reviewers", action="add")


def test_cannot_remove_the_last_reviewer(policy):
    with pytest.raises(roles.RoleError, match="last reviewer"):
        roles.change_role(policy, actor="security-lead", person="sarah", role="reviewers", action="remove")


def test_unknown_role_group_refused(policy):
    with pytest.raises(roles.RoleError, match="role group"):
        roles.change_role(policy, actor="security-lead", person="bob", role="auditors", action="add")


def test_a_newly_added_reviewer_can_review_but_still_not_their_own(policy):
    # Rule owner promotes Bob to reviewer.
    roles.change_role(policy, actor="security-lead", person="bob", role="reviewers", action="add")

    # Bob may now review Tom's candidate...
    event = decision.decide(candidate_id="RC-010", submitted_by="tom", verdict_passed=True,
                            policy=policy, reviewer="bob", action="APPROVE")
    assert event["decision"] == "APPROVE"

    # ...but the self-review invariant still holds: Bob cannot approve Bob's own work.
    with pytest.raises(decision.DecisionError, match="cannot review"):
        decision.decide(candidate_id="RC-011", submitted_by="bob", verdict_passed=True,
                        policy=policy, reviewer="bob", action="APPROVE")


def test_removing_someone_not_in_the_role_is_refused(policy):
    with pytest.raises(roles.RoleError, match="REFUSED: bob is not in reviewers"):
        roles.change_role(policy, actor="security-lead", person="bob", role="reviewers", action="remove")
    assert policy["roles"]["reviewers"] == ["sarah"]  # unchanged


def test_adding_someone_already_in_the_role_is_refused(policy):
    with pytest.raises(roles.RoleError, match="REFUSED: sarah is already in reviewers"):
        roles.change_role(policy, actor="security-lead", person="sarah", role="reviewers", action="add")
    assert policy["roles"]["reviewers"] == ["sarah"]  # unchanged
