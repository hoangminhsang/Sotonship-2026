"""Governance -- the short loop (release gate).

Every time the agent changes, this turns the red team's numbers into a human
decision and a tamper-evident record. The tool never releases on its own.

Module map:
    candidate -- fingerprint of exactly what was tested
    gate      -- deterministic PASS/FAIL verdict against the policy
    decision  -- roles, Approve/Block/Override, re-test rule
    audit     -- hash-chained append-only decision log

The policy the gate reads lives in ``policy.yaml`` next to these modules.
:func:`load_policy` validates it on load: a limit written as a percentage
(``5`` instead of ``0.05``), a YAML ``yes``, a quoted number, a missing key or
a reviewer list written as one string all raise one :class:`PolicyError` that
names the key and the value, instead of silently passing a bad candidate.
"""
from __future__ import annotations

from pathlib import Path

import yaml

POLICY_PATH = Path(__file__).resolve().parent / "policy.yaml"

#: Fractions in [0, 1] that every policy must set.
RATE_KEYS = ("max_attack_success_rate", "min_benign_task_success", "max_regression_vs_last_approved")
#: Fractions in [0, 1] that a policy may set.
OPTIONAL_RATE_KEYS = ("max_error_rate",)
#: Role groups that must be lists of names.
ROLE_GROUPS = ("engineers", "reviewers")


class PolicyError(ValueError):
    """policy.yaml cannot be used. The message names each bad key and value."""


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _not_a_number(value) -> str:
    if isinstance(value, bool):
        return f"{value!r} is a boolean (YAML yes/no/true/false), not a number"
    if isinstance(value, str):
        return f"{value!r} is a quoted string, not a number (remove the quotes)"
    return f"{value!r} is a {type(value).__name__}, not a number"


def _check_rate(limits: dict, key: str, required: bool) -> list[str]:
    if key not in limits:
        return [f"release_limits.{key}: missing (a fraction such as 0.05)"] if required else []
    value = limits[key]
    if not _is_number(value):
        return [f"release_limits.{key} = {_not_a_number(value)}"]
    if 0 <= value <= 1:
        return []
    if 1 < value <= 100:
        hint = f"this looks like a percentage; use a fraction such as {value / 100:g}"
    else:
        hint = "use a fraction such as 0.05"
    return [f"release_limits.{key} = {value}: out of range [0, 1]; {hint}"]


def validate_policy(policy, source: str = "policy") -> dict:
    """Check the rule book's required keys and types; return it unchanged.

    Raises one :class:`PolicyError` listing every problem found. Extra keys
    are allowed so the policy can grow without touching this code.
    """
    if not isinstance(policy, dict):
        what = "empty" if policy is None else f"a {type(policy).__name__}, not a mapping"
        raise PolicyError(f"{source} is {what}; expected release_limits and roles sections")

    problems: list[str] = []

    limits = policy.get("release_limits")
    if not isinstance(limits, dict):
        problems.append(f"release_limits = {limits!r}: missing or not a mapping")
    else:
        for key in RATE_KEYS:
            problems += _check_rate(limits, key, required=True)
        for key in OPTIONAL_RATE_KEYS:
            problems += _check_rate(limits, key, required=False)
        runs = limits.get("min_attack_runs")
        if not _is_int(runs) or runs < 1:
            problems.append(
                f"release_limits.min_attack_runs = {runs!r}: must be a whole number of at least 1"
            )

    roles = policy.get("roles")
    if not isinstance(roles, dict):
        problems.append(f"roles = {roles!r}: missing or not a mapping")
    else:
        for group in ROLE_GROUPS:
            members = roles.get(group)
            if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
                problems.append(
                    f"roles.{group} = {members!r}: must be a list of names, e.g. [\"sarah\"]"
                )
        owner = roles.get("rule_owner")
        if not isinstance(owner, str) or not owner.strip():
            problems.append(f"roles.rule_owner = {owner!r}: must be one name (a string)")

    if problems:
        raise PolicyError(f"{source} is not valid:\n  - " + "\n  - ".join(problems))
    return policy


def load_policy(path: Path | str = POLICY_PATH) -> dict:
    """Load and validate the release rule book."""
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return validate_policy(data, source=p.name)


__all__ = [
    "candidate", "gate", "decision", "roles", "audit",
    "load_policy", "validate_policy", "PolicyError", "POLICY_PATH",
]
