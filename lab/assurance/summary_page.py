"""One-page manager summary, built by code from the register and audit log.

Bosses read one page, not 300 traces. This is not a hand-typed dashboard: open
risks, the live version, the current attack rate, the last decision and any
override about to expire all come straight from the records.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from lab.assurance.risks import Risk
from lab.governance.decision import APPROVE, OVERRIDE, override_expired


@dataclass
class Summary:
    open_risks: list[str] = field(default_factory=list)
    current_asr: float | None = None
    live_version: str | None = None
    last_decision: str = ""
    expiring_overrides: list[str] = field(default_factory=list)
    no_fix_risks: list[str] = field(default_factory=list)


def build_summary(
    register: list[Risk],
    audit_events: list[dict],
    current_asr: float | None = None,
    today: date | None = None,
) -> Summary:
    """Assemble the manager view."""
    open_risks = [f"{r.id} {r.title}" for r in register if r.status in ("Open", "Re-opened")]
    no_fix = [f"{r.id} {r.title}" for r in register if r.control is None]

    approvals = [e for e in audit_events if e.get("decision") in (APPROVE, OVERRIDE)]
    live = approvals[-1]["candidate_id"] if approvals else None

    last = audit_events[-1] if audit_events else None
    last_decision = (
        f"{last.get('decision')} on {last.get('candidate_id')} by {last.get('reviewer')} "
        f"({last.get('time', '')[:10]})"
        if last and last.get("event") == "decision"
        else "none yet"
    )

    expiring = [
        f"{e['candidate_id']} override expired {e['expiry']}"
        for e in audit_events
        if e.get("decision") == OVERRIDE and override_expired(e, today)
    ]

    return Summary(
        open_risks=open_risks,
        current_asr=current_asr,
        live_version=live,
        last_decision=last_decision,
        expiring_overrides=expiring,
        no_fix_risks=no_fix,
    )


def to_markdown(summary: Summary) -> str:
    asr = f"{summary.current_asr:.1%}" if summary.current_asr is not None else "n/a"
    lines = [
        "**Manager summary**",
        "",
        f"- **Live version:** {summary.live_version or 'none approved'}",
        f"- **Current attack rate:** {asr}",
        f"- **Open risks ({len(summary.open_risks)}):** "
        + (", ".join(summary.open_risks) or "none"),
        f"- **Last decision:** {summary.last_decision}",
    ]
    if summary.expiring_overrides:
        lines.append(f"- **Overrides expired:** {', '.join(summary.expiring_overrides)}")
    if summary.no_fix_risks:
        lines.append(f"- **No fix in table (rule owner must act):** {', '.join(summary.no_fix_risks)}")
    return "\n".join(lines)
