"""Tests for load_policy validation (B-02).

A percentage read as a fraction let a 50% ASR candidate PASS; a YAML `yes`
PASSed too; a quoted number or a missing key gave a traceback; a string
reviewer list made 's' a valid reviewer. load_policy now refuses all of these
with one PolicyError that names the key and the value.
"""
from __future__ import annotations

import pytest
import yaml

from lab.governance import POLICY_PATH, PolicyError, load_policy, validate_policy


def _write(tmp_path, mutate):
    """Copy the shipped policy, apply ``mutate(policy)``, write it to tmp_path."""
    policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    mutate(policy)
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    return path


def _limits(policy):
    return policy["release_limits"]


def test_shipped_policy_loads():
    policy = load_policy()
    assert 0 <= policy["release_limits"]["max_attack_success_rate"] <= 1
    assert isinstance(policy["roles"]["reviewers"], list)


def test_percentage_is_rejected_with_fraction_hint(tmp_path):
    path = _write(tmp_path, lambda p: _limits(p).__setitem__("max_attack_success_rate", 5))
    with pytest.raises(PolicyError, match="max_attack_success_rate") as exc:
        load_policy(path)
    assert "use a fraction such as 0.05" in str(exc.value)


def test_quoted_number_is_rejected(tmp_path):
    path = _write(tmp_path, lambda p: _limits(p).__setitem__("max_attack_success_rate", "0.05"))
    with pytest.raises(PolicyError, match="max_attack_success_rate.*'0.05'.*string"):
        load_policy(path)


def test_yaml_yes_is_rejected(tmp_path):
    path = _write(tmp_path, lambda p: _limits(p).__setitem__("max_attack_success_rate", True))
    with pytest.raises(PolicyError, match="max_attack_success_rate.*boolean"):
        load_policy(path)


def test_negative_rate_is_rejected(tmp_path):
    path = _write(tmp_path, lambda p: _limits(p).__setitem__("min_benign_task_success", -0.1))
    with pytest.raises(PolicyError, match=r"min_benign_task_success = -0.1.*\[0, 1\]"):
        load_policy(path)


def test_missing_rate_key_is_named(tmp_path):
    path = _write(tmp_path, lambda p: _limits(p).pop("max_regression_vs_last_approved"))
    with pytest.raises(PolicyError, match="max_regression_vs_last_approved.*missing"):
        load_policy(path)


@pytest.mark.parametrize("bad", [None, 0, -3, 2.5, True, "50"])
def test_min_attack_runs_must_be_a_whole_number_of_at_least_one(tmp_path, bad):
    def mutate(p):
        if bad is None:
            _limits(p).pop("min_attack_runs")
        else:
            _limits(p)["min_attack_runs"] = bad
    path = _write(tmp_path, mutate)
    with pytest.raises(PolicyError, match="min_attack_runs"):
        load_policy(path)


def test_max_error_rate_is_optional_but_checked(tmp_path):
    ok = _write(tmp_path, lambda p: _limits(p).__setitem__("max_error_rate", 0.1))
    assert load_policy(ok)["release_limits"]["max_error_rate"] == 0.1

    bad = _write(tmp_path, lambda p: _limits(p).__setitem__("max_error_rate", 5))
    with pytest.raises(PolicyError, match="max_error_rate.*use a fraction such as 0.05"):
        load_policy(bad)


def test_reviewers_must_be_a_list_not_a_string(tmp_path):
    path = _write(tmp_path, lambda p: p["roles"].__setitem__("reviewers", "sarah"))
    with pytest.raises(PolicyError, match="roles.reviewers = 'sarah'.*list"):
        load_policy(path)


def test_role_members_must_be_strings(tmp_path):
    path = _write(tmp_path, lambda p: p["roles"].__setitem__("engineers", ["tom", 7]))
    with pytest.raises(PolicyError, match="roles.engineers"):
        load_policy(path)


def test_rule_owner_must_be_a_string(tmp_path):
    missing = _write(tmp_path, lambda p: p["roles"].pop("rule_owner"))
    with pytest.raises(PolicyError, match="roles.rule_owner"):
        load_policy(missing)
    wrong = _write(tmp_path, lambda p: p["roles"].__setitem__("rule_owner", ["security-lead"]))
    with pytest.raises(PolicyError, match="roles.rule_owner"):
        load_policy(wrong)


def test_missing_roles_block_is_named(tmp_path):
    path = _write(tmp_path, lambda p: p.pop("roles"))
    with pytest.raises(PolicyError, match="roles"):
        load_policy(path)


def test_empty_file_is_rejected(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(PolicyError, match="empty"):
        load_policy(path)


def test_all_problems_reported_in_one_error(tmp_path):
    def mutate(p):
        _limits(p)["max_attack_success_rate"] = 5
        p["roles"]["reviewers"] = "sarah"
    path = _write(tmp_path, mutate)
    with pytest.raises(PolicyError) as exc:
        load_policy(path)
    msg = str(exc.value)
    assert "max_attack_success_rate" in msg and "roles.reviewers" in msg


def test_extra_keys_are_tolerated(tmp_path):
    def mutate(p):
        p["future_section"] = {"anything": 1}
        _limits(p)["max_error_rate"] = 0.0
    path = _write(tmp_path, mutate)
    assert load_policy(path)["future_section"] == {"anything": 1}


def test_validate_policy_returns_the_dict():
    policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    assert validate_policy(policy) is policy
