"""Edit who does what -- but governed, so it can't be abused.

"Who does what" is plain data in ``policy.yaml`` (``roles.engineers``,
``roles.reviewers``, ``roles.rule_owner``). You could edit that file by hand, but
role changes are themselves a governance action, so this module makes them safe:

* **Only the rule owner may change roles.** Otherwise anyone could add themselves
  as a reviewer and approve their own work -- the exact thing the gate prevents.
* **Every change returns an audit event** to append to the hash-chained log, so
  "who gave whom which role, and when" is on the record.
* **You can't remove the last reviewer**, or there would be nobody to decide.

Note the invariant this does *not* touch: :func:`lab.governance.decision.decide`
always refuses ``reviewer == submitted_by``. Editing roles changes who can review
in general; it can never let someone approve their *own* candidate.

Every name comparison here uses :func:`lab.governance.decision._norm` (case
and surrounding spaces ignored), the same normaliser the decision rules use.
"""
from __future__ import annotations

from datetime import datetime, timezone

from lab.governance.decision import _norm

# Role groups a rule owner may edit (the rule_owner itself is not list-editable
# here; changing ownership is a bigger, deliberate act left to the file).
EDITABLE = ("engineers", "reviewers")
ACTIONS = ("add", "remove")


class RoleError(ValueError):
    """A role change the rules refuse. The message is safe to show a user."""


def change_role(
    policy: dict,
    *,
    actor: str,
    person: str,
    role: str,
    action: str,
    now: datetime | None = None,
) -> dict:
    """Add or remove ``person`` from ``role``; mutate ``policy`` and return the event.

    Raises :class:`RoleError` if ``actor`` is not the rule owner, the role or
    action is unknown, the change would do nothing (adding a current member,
    removing a non-member), or it would leave no reviewer.
    """
    roles = policy.setdefault("roles", {})
    if _norm(actor) != _norm(roles.get("rule_owner")):
        raise RoleError(f"REFUSED: only the rule owner may change roles ({actor!r} is not)")
    if role not in EDITABLE:
        raise RoleError(f"REFUSED: cannot edit role group {role!r} (editable: {', '.join(EDITABLE)})")
    if action not in ACTIONS:
        raise RoleError(f"REFUSED: unknown action {action!r} (use add or remove)")

    members = list(roles.get(role, []))
    if action == "add":
        if _norm(person) in {_norm(m) for m in members}:
            raise RoleError(f"REFUSED: {person} is already in {role}")
        members.append(person)
    else:  # remove
        remaining = [m for m in members if _norm(m) != _norm(person)]
        if len(remaining) == len(members):
            raise RoleError(f"REFUSED: {person} is not in {role}")
        if role == "reviewers" and remaining == []:
            raise RoleError("REFUSED: cannot remove the last reviewer (someone must be able to decide)")
        members = remaining

    roles[role] = sorted(members)
    now = now or datetime.now(timezone.utc)
    return {
        "event": "role_change",
        "actor": actor,
        "action": action,
        "person": person,
        "role": role,
        "members_after": list(roles[role]),
        "time": now.isoformat(),
    }


def has_role(policy: dict, person: str, role: str) -> bool:
    """Whether ``person`` currently holds ``role`` (case and spaces ignored)."""
    return _norm(person) in {_norm(m) for m in policy.get("roles", {}).get(role, [])}
