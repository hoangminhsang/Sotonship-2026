"""Human decision: roles, Approve/Block/Override, and the re-test rule.

The gate only suggests. A person decides here, and the rules below make the
decision honest:

* **No marking your own homework.** The engineer who submitted a candidate can
  never review it.
* **Approve only a PASS.** Block any time (with a reason). Override only a FAIL
  (with a reason, the reviewer's name, and an expiry date).
* **Silence is not yes.** With no decision, a candidate stays blocked and the
  last approved version keeps running (the safe default lives in the caller).
* **A changed fingerprint kills an old approval** (:func:`approval_still_valid`).

Pure functions over dicts, so every rule is unit-testable with no model.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

APPROVE = "APPROVE"
BLOCK = "BLOCK"
OVERRIDE = "OVERRIDE"
ACTIONS = (APPROVE, BLOCK, OVERRIDE)


class DecisionError(ValueError):
    """A decision that the rules refuse. The message is safe to show a user."""


def review_allowed(policy: dict, submitted_by: str, reviewer: str) -> tuple[bool, str]:
    """Whether ``reviewer`` may decide on a candidate submitted by ``submitted_by``."""
    reviewers = set(policy["roles"].get("reviewers", []))
    if reviewer == submitted_by:
        return False, f"{reviewer} submitted this candidate and cannot review it"
    if reviewer not in reviewers:
        return False, f"{reviewer} is not a reviewer"
    return True, ""


def decide(
    *,
    candidate_id: str,
    submitted_by: str,
    verdict_passed: bool,
    policy: dict,
    reviewer: str,
    action: str,
    reason: str | None = None,
    expiry: str | None = None,
    now: datetime | None = None,
    today: date | None = None,
) -> dict:
    """Validate a decision and return the event to append to the audit log.

    ``today`` (default: the UTC date of ``now``) is what an OVERRIDE expiry is
    checked against; pass it to make the check deterministic in tests.

    Raises :class:`DecisionError` when a rule is broken; the caller logs the
    refusal separately so attempts are never silently dropped.
    """
    action = action.upper()
    if action not in ACTIONS:
        raise DecisionError(f"unknown action {action!r}; use APPROVE, BLOCK or OVERRIDE")

    ok, why = review_allowed(policy, submitted_by, reviewer)
    if not ok:
        raise DecisionError(f"REFUSED: {why}")

    if action == APPROVE and not verdict_passed:
        raise DecisionError("REFUSED: cannot APPROVE a candidate whose verdict is FAIL (use OVERRIDE)")

    if action == BLOCK and not (reason and reason.strip()):
        raise DecisionError("REFUSED: BLOCK needs a short reason")

    if action == OVERRIDE:
        if verdict_passed:
            raise DecisionError("REFUSED: OVERRIDE is only for a FAIL verdict (use APPROVE)")
        if not (reason and reason.strip()):
            raise DecisionError("REFUSED: OVERRIDE needs a reason")
        if not expiry:
            raise DecisionError("REFUSED: OVERRIDE needs an expiry date (YYYY-MM-DD)")
        _check_date(expiry, today or (now or datetime.now(timezone.utc)).date())

    now = now or datetime.now(timezone.utc)
    return {
        "event": "decision",
        "candidate_id": candidate_id,
        "submitted_by": submitted_by,
        "verdict": "PASS" if verdict_passed else "FAIL",
        "decision": action,
        "reviewer": reviewer,
        "reason": reason or "",
        "expiry": expiry or "",
        "time": now.isoformat(),
    }


def _check_date(value: str, today: date) -> None:
    try:
        expiry = date.fromisoformat(value)
    except ValueError as exc:
        raise DecisionError(f"REFUSED: expiry {value!r} is not a YYYY-MM-DD date") from exc
    if expiry < today:
        raise DecisionError(f"REFUSED: expiry {value!r} is in the past (today is {today.isoformat()})")


def approval_still_valid(approved_identity_hash: str, current_identity_hash: str) -> bool:
    """The re-test rule: an approval holds only while the fingerprint is unchanged."""
    return approved_identity_hash == current_identity_hash


def override_expired(event: dict, today: date | None = None) -> bool:
    """Whether an OVERRIDE decision has passed its expiry date."""
    if event.get("decision") != OVERRIDE or not event.get("expiry"):
        return False
    today = today or datetime.now(timezone.utc).date()
    return today > date.fromisoformat(event["expiry"])
