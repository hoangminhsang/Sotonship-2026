"""Traceability: follow one finding from first attack to closed risk, by IDs.

Anyone, months later, can read this page and see the whole story without asking
the team: which attack, which rules it broke, which risks it opened, which fix,
which re-test, which audit entry, and the final status. Stitched by code from
the trace, the risk register, the fix tickets and the audit log.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from lab.assurance.risks import RISK_CATALOG, FixTicket, Risk, build_register, fix_tickets, prior_from
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


def _violated_count(runs: pd.DataFrame, finding_id: str) -> tuple[int, int]:
    rows = runs[runs["test_id"] == finding_id]
    return int(rows["violated"].astype(bool).sum()), len(rows)


def build_thread(
    finding_id: str,
    before_runs: pd.DataFrame,
    register: list[Risk],
    tickets: list[FixTicket],
    audit_events: list[dict],
    retest_candidate: str | None = None,
    retest_runs: pd.DataFrame | None = None,
    *,
    landed_in: str | None = None,
    retest_note: str = "",
    warnings: list[str] | None = None,
) -> Thread:
    """Stitch the end-to-end story for a single finding (e.g. ``ATK-0007``).

    ``before_runs`` must be the runs where the finding *landed* (the failing
    candidate), not the re-test: a fixed re-test has no violated row to trace.
    ``retest_runs``, if given, adds what the re-test measured for this finding,
    with ``retest_note`` appended to that line. ``landed_in`` names the candidate
    when there is no re-test (the finding is traced where it landed). ``warnings``
    are extra lines placed after the re-test / landed-in step; they never change
    the status, which always comes from ``register``.

    Prefer :func:`trace_finding`, which picks the runs and adds the warnings.
    """
    warnings = list(warnings or [])
    row = _finding_row(before_runs, finding_id)
    if row is None:
        return Thread(finding_id, steps=[f"{finding_id}: not found in runs"], chain=finding_id)

    if not bool(row.get("violated")):
        # Nothing landed, so there is no risk, fix or closure to follow. Say so.
        hits, n = _violated_count(before_runs, finding_id)
        return Thread(
            finding_id,
            steps=[f"Finding **{finding_id}** (technique {row.get('technique')}, place {row.get('place')}): "
                   f"not violated in these runs ({hits}/{n} runs violated), so there is nothing to trace. "
                   "Trace it from the candidate where it landed."],
            chain=f"{finding_id} -> not violated",
        )

    classes = [c for c in str(row.get("violation_classes", "")).split(";") if c]
    by_class = {r.violation_class: r for r in register}
    tickets_by_risk = {t.risk_id: t for t in tickets}

    steps = [
        f"Finding **{finding_id}** (technique {row.get('technique')}, place {row.get('place')}): "
        f"{row.get('oracle_detail') or 'violated: ' + ', '.join(classes)}",
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
        if retest_runs is not None:
            hits, n = _violated_count(retest_runs, finding_id)
            outcome = "not found in the re-test" if n == 0 else f"{hits}/{n} runs violated{retest_note}"
            steps.append(f"Re-tested as **{retest_candidate}**: {finding_id} {outcome}")
        else:
            steps.append(f"Re-tested as **{retest_candidate}**")
        chain_ids.append(retest_candidate)
    elif landed_in:
        hits, n = _violated_count(before_runs, finding_id)
        steps.append(f"Landed in **{landed_in}**: {finding_id} {hits}/{n} runs violated (no re-test of a fix yet)")
        chain_ids.append(landed_in)
    steps.extend(warnings)

    decided_on = retest_candidate or landed_in
    approve = next(
        (e for e in audit_events
         if e.get("event") == "decision"
         and e.get("candidate_id") == decided_on and e.get("decision") == APPROVE),
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


# Fewer re-test runs than this for one finding is flagged as thin evidence.
MIN_RUNS_PER_FINDING = 3


def _landed(runs: pd.DataFrame | None, finding_id: str) -> bool:
    return runs is not None and _violated_count(runs, finding_id)[0] > 0


def _when(rec: dict | None) -> datetime | None:
    try:
        return datetime.fromisoformat(rec["created_at"]) if rec and rec.get("created_at") else None
    except (TypeError, ValueError):
        return None


def newer_prior_warning(prior_id: str, prior: dict | None, candidate_id: str, candidate: dict | None) -> str | None:
    """A warning if the "prior" was created after the candidate it is compared with."""
    p, c = _when(prior), _when(candidate)
    try:
        newer = bool(p and c and p > c)
    except TypeError:  # one timestamp has a timezone, the other not: cannot order them
        newer = False
    if newer:
        return (f"Warning: {prior_id} is newer than {candidate_id}. The prior should be the older "
                "candidate; did you mean to swap them?")
    return None


def mode_warning(prior_id: str, prior: dict | None, candidate_id: str, candidate: dict | None) -> str | None:
    """A warning if the two candidates ran in different modes; a note if a mode is unknown."""
    modes = {prior_id: (prior or {}).get("mode"), candidate_id: (candidate or {}).get("mode")}
    unknown = [cid for cid, mode in modes.items() if not mode]
    if unknown:
        return (f"Note: mode not recorded for {', '.join(unknown)} (created before modes were recorded), "
                "so whether these results are comparable is unknown.")
    if modes[prior_id] != modes[candidate_id]:
        return (f"Warning: {prior_id} ran in {modes[prior_id]} and {candidate_id} in "
                f"{modes[candidate_id]}; the results are not directly comparable.")
    return None


def thin_evidence_note(n_runs: int, mode: str | None) -> str:
    """Suffix for a re-test line resting on little evidence ('' when the evidence is fine)."""
    reasons = []
    if 0 < n_runs < MIN_RUNS_PER_FINDING:
        reasons.append(f"only {n_runs} run(s), fewer than {MIN_RUNS_PER_FINDING}")
    if mode == "LIVE":
        reasons.append("LIVE results vary between runs")
    return f" (small sample; {'; '.join(reasons)})" if reasons else ""


def trace_finding(
    finding_id: str,
    *,
    candidate_id: str,
    candidate_runs: pd.DataFrame,
    candidate: dict,
    policy: dict,
    control_map: dict,
    audit_events: list[dict],
    prior_id: str | None = None,
    prior_runs: pd.DataFrame | None = None,
    prior: dict | None = None,
    tickets: list[FixTicket] | None = None,
) -> Thread:
    """Trace one finding for a chosen candidate, optionally against an older prior.

    ``candidate`` / ``prior`` are records from ``candidates.json`` (``defences``,
    ``created_at``, ``mode``). Where the story starts:

    * landed in the prior   -> trace from the prior, re-tested as the candidate;
    * landed only in the candidate (or no prior given) -> trace from the candidate;
    * landed in neither     -> "nothing to trace".

    ``tickets`` are the fix tickets to show (e.g. with owners and due dates); by
    default they are drafted from the register where the finding landed.

    Status always comes from the candidate's register (built against the prior,
    as the risk register is); warnings are extra lines and never change it.
    """
    prior_reg = build_register(prior_runs, policy, control_map) if prior_runs is not None else None
    register = build_register(candidate_runs, policy, control_map,
                              prior=prior_from(prior_reg) if prior_reg is not None else None)

    compare = []
    if prior_runs is not None:
        compare = [w for w in (newer_prior_warning(prior_id, prior, candidate_id, candidate),
                               mode_warning(prior_id, prior, candidate_id, candidate)) if w]

    if _landed(prior_runs, finding_id):
        row = _finding_row(prior_runs, finding_id)
        classes = [c for c in str(row.get("violation_classes", "")).split(";") if c]
        before_hits, _ = _violated_count(prior_runs, finding_id)
        after_hits, after_n = _violated_count(candidate_runs, finding_id)
        enabled = set(candidate.get("defences") or [])
        control_warnings = []
        if after_n and after_hits < before_hits:  # a drop: say whether its control was even on
            for code in sorted({r.control for r in register if r.violation_class in classes and r.control}):
                if code not in enabled:
                    control_warnings.append(f"Warning: {code} was not enabled in {candidate_id}, so this "
                                            f"drop is not evidence that {code} works.")
        return build_thread(finding_id, prior_runs, register,
                            fix_tickets(prior_reg) if tickets is None else tickets, audit_events,
                            retest_candidate=candidate_id, retest_runs=candidate_runs,
                            retest_note=thin_evidence_note(after_n, candidate.get("mode")),
                            warnings=control_warnings + compare)

    if _landed(candidate_runs, finding_id):
        return build_thread(finding_id, candidate_runs, register,
                            fix_tickets(register) if tickets is None else tickets, audit_events,
                            landed_in=candidate_id, warnings=compare)

    searched = [(cid, runs) for cid, runs in ((prior_id, prior_runs), (candidate_id, candidate_runs))
                if runs is not None]
    if not any(_violated_count(runs, finding_id)[1] for _, runs in searched):
        return Thread(finding_id, steps=[f"{finding_id}: not found in runs", *compare], chain=finding_id)
    counts = ", ".join(f"{cid} {'/'.join(map(str, _violated_count(runs, finding_id)))}" for cid, runs in searched)
    return Thread(
        finding_id,
        steps=[f"Finding **{finding_id}**: not violated in {' or '.join(c for c, _ in searched)} "
               f"(runs violated: {counts}), so there is nothing to trace.", *compare],
        chain=f"{finding_id} -> not violated",
    )


def to_markdown(thread: Thread) -> str:
    lines = [f"**Traceability for {thread.finding_id}**", "", f"`{thread.chain}`", ""]
    lines += [f"{i}. {s}" for i, s in enumerate(thread.steps, start=1)]
    return "\n".join(lines)
