"""Traceability: follow one finding from first attack to closed risk, by IDs.

Anyone, months later, can read this page and see the whole story without asking
the team: which attack, which rules it broke, which risks it opened, which fix,
which re-test, which audit entry, and the final status. Stitched by code from
the trace, the risk register, the fix tickets and the audit log.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from lab.assurance.risks import RISK_CATALOG, Risk, FixTicket
from lab.governance.decision import APPROVE


@dataclass
class Thread:
    finding_id: str
    steps: list[str] = field(default_factory=list)
    chain: str = ""


def _finding_row(runs: pd.DataFrame, finding_id: str) -> pd.Series | None:
    rows = runs[(runs["test_id"] == finding_id) & (runs["violated"])]
    if rows.empty:
        rows = runs[runs["test_id"] == finding_id]
    return rows.iloc[0] if not rows.empty else None


def build_thread(
    finding_id: str,
    before_runs: pd.DataFrame,
    register: list[Risk],
    tickets: list[FixTicket],
    audit_events: list[dict],
    retest_candidate: str | None = None,
) -> Thread:
    """Stitch the end-to-end story for a single finding (e.g. ``ATK-0007``)."""
    row = _finding_row(before_runs, finding_id)
    if row is None:
        return Thread(finding_id, steps=[f"{finding_id}: not found in runs"], chain=finding_id)

    classes = [c for c in str(row.get("violation_classes", "")).split(";") if c]
    by_class = {r.violation_class: r for r in register}
    tickets_by_risk = {t.risk_id: t for t in tickets}

    steps = [
        f"Finding **{finding_id}** (technique {row.get('technique')}, place {row.get('place')}): "
        f"{row.get('oracle_detail') or 'violation recorded'}",
    ]
    chain_ids = [finding_id]

    risk_ids = []
    for cls in classes:
        risk = by_class.get(cls) or RISK_CATALOG.get(cls)
        rid = risk.id if isinstance(risk, Risk) else (risk or {}).get("id", "R-??")
        risk_ids.append(rid)
        if isinstance(risk, Risk):
            steps.append(f"Broke `{cls}` -> risk **{risk.id}** ({risk.title}), status {risk.status}")
    chain_ids.extend(sorted(set(risk_ids)))

    controls = sorted({r.control for r in register if r.violation_class in classes and r.control})
    for code in controls:
        steps.append(f"Control **{code}** assigned by the fix table")
    chain_ids.extend(controls)

    for rid in sorted(set(risk_ids)):
        ticket = tickets_by_risk.get(rid)
        if ticket:
            steps.append(f"Fix ticket **{ticket.id}**: {ticket.title} (owner {ticket.owner})")
            chain_ids.append(ticket.id)

    if retest_candidate:
        steps.append(f"Re-tested as **{retest_candidate}**")
        chain_ids.append(retest_candidate)

    approve = next(
        (e for e in audit_events
         if e.get("candidate_id") == retest_candidate and e.get("decision") == APPROVE),
        None,
    )
    if approve:
        steps.append(f"Approved by {approve['reviewer']} -> audit entry #{approve['seq']}")
        chain_ids.append(f"entry #{approve['seq']}")

    final = {by_class[c].status for c in classes if c in by_class}
    if final:
        status = "Mitigated" if final == {"Mitigated"} else ", ".join(sorted(final))
        steps.append(f"Final status: **{status}**")
        chain_ids.append(status)

    return Thread(finding_id, steps=steps, chain=" -> ".join(chain_ids))


def to_markdown(thread: Thread) -> str:
    lines = [f"**Traceability for {thread.finding_id}**", "", f"`{thread.chain}`", ""]
    lines += [f"{i}. {s}" for i, s in enumerate(thread.steps, start=1)]
    return "\n".join(lines)
