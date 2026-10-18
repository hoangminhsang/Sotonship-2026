"""Tests for the one-page manager summary (B-06, B-24, D-10).

Live version = the newest still-valid approval after folding events per
candidate (a later BLOCK revokes; an expired OVERRIDE is not live).
Last decision = the last event whose event == "decision", whatever came after.
The live line carries the live version's own rate, CI and mode.
"""
from __future__ import annotations

from datetime import date

from lab.assurance import summary_page


def _dec(cid, action, time, reviewer="sarah", expiry=""):
    return {
        "event": "decision", "candidate_id": cid, "decision": action,
        "reviewer": reviewer, "reason": "r", "expiry": expiry, "time": time,
    }


T1, T2, T3, T4 = ("2026-10-17T10:00:00+00:00", "2026-10-17T11:00:00+00:00",
                  "2026-10-17T12:00:00+00:00", "2026-10-17T13:00:00+00:00")


# -- B-06: live version -------------------------------------------------------

def test_approve_then_block_leaves_no_live_version():
    events = [_dec("RC-002", "APPROVE", T1), _dec("RC-002", "BLOCK", T2)]
    s = summary_page.build_summary([], events)
    assert s.live_version is None
    assert "none approved" in summary_page.to_markdown(s)


def test_block_of_newer_candidate_falls_back_to_older_approval():
    events = [_dec("RC-001", "APPROVE", T1), _dec("RC-002", "APPROVE", T2), _dec("RC-002", "BLOCK", T3)]
    assert summary_page.build_summary([], events).live_version == "RC-001"


def test_newest_approval_wins_regardless_of_candidate_id():
    assert summary_page.build_summary(
        [], [_dec("RC-001", "APPROVE", T1), _dec("RC-002", "APPROVE", T2)]).live_version == "RC-002"
    assert summary_page.build_summary(
        [], [_dec("RC-002", "APPROVE", T1), _dec("RC-001", "APPROVE", T2)]).live_version == "RC-001"


def test_expired_override_is_not_live():
    events = [_dec("RC-002", "APPROVE", T1), _dec("RC-005", "OVERRIDE", T2, expiry="2026-10-18")]
    s = summary_page.build_summary([], events, today=date(2026, 10, 30))
    assert s.live_version == "RC-002"
    assert s.expiring_overrides == ["RC-005 override expired 2026-10-18"]

    still_valid = summary_page.build_summary([], events, today=date(2026, 10, 17))
    assert still_valid.live_version == "RC-005"
    assert still_valid.expiring_overrides == []


def test_refused_attempt_carrying_a_decision_key_is_not_an_approval():
    events = [
        _dec("RC-001", "BLOCK", T1),
        {"event": "decision_refused", "candidate_id": "RC-003", "reviewer": "tom",
         "decision": "APPROVE", "action": "APPROVE", "detail": "REFUSED", "time": T2},
    ]
    assert summary_page.build_summary([], events).live_version is None


# -- B-24: last decision ------------------------------------------------------

def test_last_decision_survives_a_later_role_change():
    events = [
        _dec("RC-002", "APPROVE", T1),
        {"event": "role_change", "actor": "security-lead", "action": "add", "person": "ana",
         "role": "reviewers", "members_after": ["ana", "sarah"], "time": T2},
    ]
    s = summary_page.build_summary([], events)
    assert s.last_decision == "APPROVE on RC-002 by sarah (2026-10-17)"


def test_last_decision_survives_a_later_refusal_and_submit():
    events = [
        _dec("RC-002", "APPROVE", T1),
        {"event": "decision_refused", "candidate_id": "RC-003", "reviewer": "tom",
         "action": "APPROVE", "detail": "REFUSED", "time": T2},
        {"event": "submit", "candidate_id": "RC-004", "by": "tom", "time": T3},
    ]
    assert summary_page.build_summary([], events).last_decision.startswith("APPROVE on RC-002")


def test_no_decisions_yet():
    events = [{"event": "submit", "candidate_id": "RC-001", "by": "tom", "time": T1}]
    assert summary_page.build_summary([], events).last_decision == "none yet"


# -- D-10: the live version's own rate, CI and mode --------------------------

def test_live_line_shows_live_versions_own_rate_ci_and_mode():
    events = [_dec("RC-002", "APPROVE", T1)]
    summaries = {
        "RC-001": {"asr": 0.423, "asr_lo": 0.30, "asr_hi": 0.55, "mode": "LIVE"},
        "RC-002": {"asr": 0.0, "asr_lo": 0.0, "asr_hi": 0.06, "mode": "SCRIPTED"},
    }
    s = summary_page.build_summary([], events, current_asr=0.423, summaries=summaries,
                                   current_candidate="RC-001")
    assert s.live_version == "RC-002"
    assert s.live_asr == 0.0 and s.live_asr_ci == (0.0, 0.06) and s.live_mode == "SCRIPTED"
    assert s.current_candidate == "RC-001" and s.current_asr_ci == (0.30, 0.55)
    assert s.current_mode == "LIVE"

    md = summary_page.to_markdown(s)
    live_line = next(line for line in md.splitlines() if "Live version" in line)
    assert "RC-002" in live_line and "0.0%" in live_line
    assert "[0.0%, 6.0%]" in live_line and "SCRIPTED" in live_line
    current_line = next(line for line in md.splitlines() if "Current attack rate" in line)
    assert "42.3%" in current_line and "RC-001" in current_line and "LIVE" in current_line


def test_mode_not_recorded_is_said_out_loud():
    events = [_dec("RC-002", "APPROVE", T1)]
    s = summary_page.build_summary(
        [], events, summaries={"RC-002": {"asr": 0.0, "asr_lo": 0.0, "asr_hi": 0.06}})
    assert s.live_mode is None
    assert "(mode not recorded)" in summary_page.to_markdown(s)


def test_live_version_without_a_summary_says_rate_not_recorded():
    s = summary_page.build_summary([], [_dec("RC-002", "APPROVE", T1)])
    assert s.live_asr is None
    assert "rate not recorded" in summary_page.to_markdown(s)


def test_current_asr_taken_from_summaries_when_not_given():
    s = summary_page.build_summary(
        [], [], summaries={"RC-001": {"asr": 0.25, "asr_lo": 0.1, "asr_hi": 0.4}},
        current_candidate="RC-001")
    assert s.current_asr == 0.25 and s.current_asr_ci == (0.1, 0.4)


# -- B-08 / D-20: audit head anchor on the summary ----------------------------

def test_log_head_is_printed_when_given():
    s = summary_page.build_summary([], [], log_head=(7, "ab" * 32))
    assert s.log_entries == 7 and s.log_head == "ab" * 32
    md = summary_page.to_markdown(s)
    assert "7 entries" in md and "ab" * 32 in md


def test_old_call_signature_still_works():
    s = summary_page.build_summary([], [], current_asr=0.1)
    md = summary_page.to_markdown(s)
    assert "10.0%" in md and "none approved" in md and "none yet" in md
    assert "Audit log" not in md
