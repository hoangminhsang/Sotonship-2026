"""One-page manager summary, built by code from the register and audit log.

Bosses read one page, not 300 traces. This is not a hand-typed dashboard: open
risks, the live version, the current attack rate, the last decision and any
override about to expire all come straight from the records.

Two rules keep the page honest:

* **Live version** is the newest *still-valid* approval after folding the log
  per candidate: the latest decision for a candidate wins, so a later BLOCK
  revokes an earlier APPROVE, and an OVERRIDE past its expiry is not live.
* **Last decision** is the last event whose ``event == "decision"``; a later
  submit, refusal or role change does not erase it.

The live line shows the live version's *own* attack rate with its 95% CI and
the mode it was measured in (``SCRIPTED`` or ``LIVE``), never the rate of
whichever candidate happens to be selected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from lab.assurance.risks import Risk
from lab.governance.decision import APPROVE, OVERRIDE, override_expired


@dataclass
class Summary:
    open_risks: list[str] = field(default_factory=list)
    register_candidate: str | None = None  # whose risk register open_risks come from
    current_asr: float | None = None
    asr_candidate: str | None = None  # whose attack rate current_asr is
    live_version: str | None = None
    last_decision: str = ""
    expiring_overrides: list[str] = field(default_factory=list)
    no_fix_risks: list[str] = field(default_factory=list)
    # The live version's own numbers (None = not recorded in the summaries given).
    live_asr: float | None = None
    live_asr_ci: tuple[float, float] | None = None
    live_mode: str | None = None
    # Which candidate the "current" rate belongs to, and its CI and mode.
    current_candidate: str | None = None
    current_asr_ci: tuple[float, float] | None = None
    current_mode: str | None = None
    # Audit-log head anchor (see lab.governance.audit.head).
    log_entries: int | None = None
    log_head: str | None = None


def _is_live(event: dict, today: date | None) -> bool:
    """Whether a candidate whose latest decision is ``event`` is still approved."""
    action = event.get("decision")
    if action == APPROVE:
        return True
    if action == OVERRIDE:
        return not override_expired(event, today)
    return False


def live_version(audit_events: list[dict], today: date | None = None) -> str | None:
    """The newest still-valid approval after folding decisions per candidate."""
    latest: dict[str, dict] = {}
    for e in audit_events:
        cid = e.get("candidate_id")
        if e.get("event") == "decision" and cid:
            latest.pop(cid, None)          # re-insert so dict order = order of latest decision
            latest[cid] = e
    for cid in reversed(list(latest)):
        if _is_live(latest[cid], today):
            return cid
    return None


def _ci(summary: dict | None) -> tuple[float, float] | None:
    if not summary or summary.get("asr_lo") is None or summary.get("asr_hi") is None:
        return None
    return (summary["asr_lo"], summary["asr_hi"])


def build_summary(
    register: list[Risk],
    audit_events: list[dict],
    current_asr: float | None = None,
    today: date | None = None,
    asr_by_candidate: dict[str, float] | None = None,
    asr_candidate: str | None = None,
    register_candidate: str | None = None,
    *,
    summaries: dict[str, dict] | None = None,
    current_candidate: str | None = None,
    log_head: tuple[int, str | None] | None = None,
) -> Summary:
    """Assemble the manager view.

    ``summaries`` maps candidate_id -> the gate's metrics summary
    (``asr``, ``asr_lo``, ``asr_hi`` and, if recorded, ``mode``). It fills the
    live version's own rate, CI and mode, and the current candidate's CI and
    mode. ``current_candidate`` names whose rate ``current_asr`` is; when
    ``current_asr`` is None it is taken from ``summaries``. ``log_head`` is
    ``audit.head(path)``: ``(entry_count, last_hash)``.

    Pass ``asr_by_candidate`` (candidate id -> ASR) so "current attack rate" is
    the *live* version's own number, labelled with its id; it never mixes in
    another candidate's. ``current_asr`` + ``asr_candidate`` remain for a caller
    that already knows which candidate's rate it is reporting.
    ``register_candidate`` names the candidate ``register`` was built for, so
    its open risks are labelled too.
    """
    summaries = summaries or {}
    open_risks = [f"{r.id} {r.title}" for r in register if r.status in ("Open", "Re-opened")]
    no_fix = [f"{r.id} {r.title}" for r in register if r.control is None]

    live = live_version(audit_events, today)
    live_summary = summaries.get(live) if live else None
    if asr_by_candidate is not None:
        asr_candidate = live
        current_asr = asr_by_candidate.get(live) if live else None

    # The newest real decision, even if submissions or refusals were logged after it.
    decisions = [e for e in audit_events if e.get("event") == "decision"]
    last = decisions[-1] if decisions else None
    last_decision = (
        f"{last.get('decision')} on {last.get('candidate_id')} by {last.get('reviewer')} "
        f"({last.get('time', '')[:10]})"
        if last
        else "none yet"
    )

    expiring = [
        f"{e['candidate_id']} override expired {e['expiry']}"
        for e in decisions
        if e.get("decision") == OVERRIDE and override_expired(e, today)
    ]

    current_summary = summaries.get(current_candidate) if current_candidate else None
    if current_asr is None and current_summary:
        current_asr = current_summary.get("asr")

    return Summary(
        open_risks=open_risks,
        register_candidate=register_candidate,
        current_asr=current_asr,
        asr_candidate=asr_candidate,
        live_version=live,
        last_decision=last_decision,
        expiring_overrides=expiring,
        no_fix_risks=no_fix,
        live_asr=live_summary.get("asr") if live_summary else None,
        live_asr_ci=_ci(live_summary),
        live_mode=live_summary.get("mode") if live_summary else None,
        current_candidate=current_candidate,
        current_asr_ci=_ci(current_summary),
        current_mode=current_summary.get("mode") if current_summary else None,
        log_entries=log_head[0] if log_head else None,
        log_head=log_head[1] if log_head else None,
    )


def _rate_text(rate: float | None, ci: tuple[float, float] | None) -> str:
    if rate is None:
        return "n/a"
    text = f"{rate:.1%}"
    if ci:
        text += f" (95% CI [{ci[0]:.1%}, {ci[1]:.1%}])"
    return text


def _mode_text(mode: str | None) -> str:
    return f"mode {mode}" if mode else "(mode not recorded)"


def to_markdown(summary: Summary) -> str:
    if summary.live_version is None:
        live = "none approved"
    elif summary.live_asr is None:
        live = f"{summary.live_version} (rate not recorded)"
    else:
        live = (f"{summary.live_version} -- attack rate "
                f"{_rate_text(summary.live_asr, summary.live_asr_ci)}, {_mode_text(summary.live_mode)}")

    current = _rate_text(summary.current_asr, summary.current_asr_ci)
    if summary.current_candidate and summary.current_asr is not None:
        current += f" for {summary.current_candidate}, {_mode_text(summary.current_mode)}"

    lines = [
        "**Manager summary**",
        "",
        f"- **Live version:** {live}",
        f"- **Current attack rate{f' ({summary.asr_candidate})' if summary.asr_candidate else ''}:** {current}",
        f"- **Open risks{f' in {summary.register_candidate}' if summary.register_candidate else ''} "
        f"({len(summary.open_risks)}):** "
        + (", ".join(summary.open_risks) or "none"),
        f"- **Last decision:** {summary.last_decision}",
    ]
    if summary.log_entries is not None:
        head = summary.log_head or "none"
        lines.append(f"- **Audit log:** {summary.log_entries} entries, head {head}")
    if summary.expiring_overrides:
        lines.append(f"- **Overrides expired:** {', '.join(summary.expiring_overrides)}")
    if summary.no_fix_risks:
        lines.append(f"- **No fix in table (rule owner must act):** {', '.join(summary.no_fix_risks)}")
    return "\n".join(lines)
