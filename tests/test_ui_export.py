"""ui/export.py reads what scripts/run_governance.py writes (lane D, export fixes).

SCRIPTED input comes from running the CLI in-process (the pattern of
tests/test_governance_cli.py). Input from a real model run (tag LIVE) is a small
hand-written snippet in the same layout: no real run output is committed, because
it holds absolute paths. The synthetic numbers in the snippets only exercise the
parsers; nothing here pins a measured number of the backend.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ex():
    return _load("ui_export_under_test", "ui/export.py")


@pytest.fixture(scope="module")
def cli_out(tmp_path_factory):
    """One SCRIPTED run of the governance CLI, shared by the tests of this module."""
    gov = _load("run_governance_for_ui_export", "scripts/run_governance.py")
    out = tmp_path_factory.mktemp("cli") / "gov"
    assert gov.main(["--out", str(out)]) == 0
    return out


@pytest.fixture
def repo(tmp_path, cli_out):
    """A throw-away repo root that holds a copy of the CLI output in governance_out/."""
    shutil.copytree(cli_out, tmp_path / "governance_out")
    return tmp_path


def make_ctx(ex, root: Path, governance: dict | None = None, allow_sample: bool = False):
    args = ex.parse_args(["--no-git"] + (["--allow-sample"] if allow_sample else []))
    sources = {"schema": ex.SOURCES_SCHEMA, "repo_root": ".", "governance": governance or {}}
    ctx = ex.Ctx(args, root / "sources.json", sources, root)
    ex.init_git(ctx)
    return ctx


# Gate report of a real-model run: the layout of gate.report_text with the mode tag on top.
# The numbers are synthetic.
LIVE_GATE = """\
[LIVE] Candidate RC-009
  OK    attack_success_rate                0.0%  limit 5.0%    (0/4 95% CI [0.0%, 49.0%]; attempted 0/4 (0.0%) incl. calls blocked by defences)
  FAIL  min_attack_runs                       4  limit 50      (not enough proof)
  OK    min_benign_task_success          100.0%  limit 85.0%   (4/4)
  OK    max_error_rate                     0.0%  limit 10.0%   (0/8)
  info  attempted_incl_blocked             0.0%  not a limit  (0/4 95% CI [0.0%, 49.0%])
VERDICT: FAIL (1 reason)
"""

GATE_CHECKS = ["attack_success_rate", "min_attack_runs", "min_benign_task_success", "max_error_rate"]


# --------------------------------------------------------------------------- #
# Breakage 3: the gate file starts with a mode tag and ends a block with an info line.
# --------------------------------------------------------------------------- #
def test_gate_parses_every_candidate_of_the_scripted_cli_output(ex, cli_out):
    gates = sorted(cli_out.glob("gate_RC-*.txt"))
    assert len(gates) >= 3
    for path in gates:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("[SCRIPTED] Candidate "), "the CLI tags every .txt file with its mode"
        g = ex.parse_gate_report(text)
        assert g["candidate"] == path.stem.split("_", 1)[1]
        assert g["mode"] == "SCRIPTED"
        assert [c["name"] for c in g["checks"]] == GATE_CHECKS
        fails = sum(1 for c in g["checks"] if c["mark"] == "FAIL")
        assert g["verdict"] == ("FAIL" if fails else "PASS")
        assert (g["reasons"] or 0) == fails


def test_info_line_is_kept_apart_and_never_counted_as_a_check(ex):
    g = ex.parse_gate_report(LIVE_GATE)
    assert g["mode"] == "LIVE" and g["candidate"] == "RC-009"
    assert [c["name"] for c in g["checks"]] == GATE_CHECKS
    assert [(i["name"], i["value"]) for i in g["info"]] == [("attempted_incl_blocked", "0.0%")]
    assert g["info"][0]["note"] == "0/4 95% CI [0.0%, 49.0%]"
    assert g["info"][0]["line"] == 6
    assert (g["verdict"], g["reasons"]) == ("FAIL", 1)


def test_untagged_legacy_gate_file_still_parses(ex):
    legacy = LIVE_GATE.replace("[LIVE] ", "", 1)
    g = ex.parse_gate_report(legacy)
    assert g["mode"] is None and g["candidate"] == "RC-009"


def test_saved_tag_is_accepted_and_unknown_tags_are_not(ex):
    assert ex.parse_gate_report(LIVE_GATE.replace("[LIVE]", "[SAVED]", 1))["mode"] == "SAVED"
    with pytest.raises(ex.SourceError):
        ex.parse_gate_report(LIVE_GATE.replace("[LIVE]", "[DEMO]", 1))


def test_info_line_must_have_the_backend_shape(ex):
    assert ex.parse_gate_report(LIVE_GATE)["info"]  # the intact report parses
    broken = LIVE_GATE.replace("not a limit", "limit 5.0%")
    with pytest.raises(ex.SourceError):
        ex.parse_gate_report(broken)
    before_candidate = LIVE_GATE.splitlines()
    before_candidate.insert(0, before_candidate.pop(5))
    with pytest.raises(ex.SourceError):
        ex.parse_gate_report("\n".join(before_candidate))


def test_gate_with_only_an_info_line_has_no_check_lines(ex):
    text = "[LIVE] Candidate RC-009\n" + LIVE_GATE.splitlines()[5] + "\nVERDICT: PASS\n"
    with pytest.raises(ex.SourceError, match="no check lines"):
        ex.parse_gate_report(text)


def test_labels_cover_the_error_rate_check_and_the_info_row(ex):
    for table in (ex.CHECK_LABELS, ex.LIMIT_LABELS):
        for key in ("max_error_rate", "attempted_incl_blocked"):
            assert table.get(key) and table[key] != key, f"{key} needs a plain-language label"


def test_gate_from_text_keeps_info_separate_and_provenance_re_reads(ex, repo):
    ctx = make_ctx(ex, repo)
    text = (repo / "governance_out" / "gate_RC-001.txt").read_text(encoding="utf-8")
    gate = ex.gate_from_text(ctx, text, ex.SAMPLE, "governance_out/gate_RC-001.txt", {"kind": "text"})
    assert [c["name"] for c in gate["checks"]] == GATE_CHECKS
    assert gate["checks"][3]["label"] == ex.CHECK_LABELS["max_error_rate"]
    info = gate["info"]
    assert [i["name"] for i in info] == ["attempted_incl_blocked"]
    assert info[0]["label"] == ex.CHECK_LABELS["attempted_incl_blocked"]
    assert info[0]["value"]["status"] == ex.SAMPLE and info[0]["value"]["prov"] in ctx.prov
    assert gate["_fails"] == sum(1 for c in gate["checks"] if c["mark"] == "FAIL")
    assert gate["candidate_id"]["text"] == "RC-001"
    assert gate["raw_lines"][0].startswith("[SCRIPTED] Candidate ")
    assert ex.self_check(ctx, list(ctx.prov.values())) == []  # --check can re-read every locator


def test_scripted_gate_reaches_the_dashboard_data(ex, repo):
    """Before the fix the whole gate fell back to "No usable gate output"."""
    gov = {"connected": True, "candidate_id": "RC-001", "evidence_batch": "b",
           "gate_report": "governance_out/gate_RC-001.txt",
           "fingerprint": ["governance_out/RC-001.json"],
           "audit_log": "governance_out/audit_log.jsonl",
           "risk_register_md": "governance_out/risk_register_before.md"}
    ctx = make_ctx(ex, repo, gov, allow_sample=True)
    text = (repo / "governance_out" / "gate_RC-001.txt").read_text(encoding="utf-8")
    note = ex.parse_gate_report(text)["checks"][0]["note"]
    x, n = (int(v) for v in note.split(" ")[0].split("/"))
    batch = {"id": "b", "title": "stub", "summary": {"attack_success_rate": {"measured": True, "successes": x, "n": n}}}
    gate, _audit, _assurance, _latest = ex.build_governance(ctx, {"b": batch})
    assert not [w for w in ctx.warnings if w["text"].startswith("Gate:")], ctx.warnings
    assert gate["status"] == ex.SAMPLE and gate["candidate_id"]["text"] == "RC-001"
    assert [c["name"] for c in gate["checks"]] == GATE_CHECKS
    assert [i["name"] for i in gate["info"]] == ["attempted_incl_blocked"]


# --------------------------------------------------------------------------- #
# Breakage 4: the first line of every .md file is "**Mode: [TAG]** <note>".
# --------------------------------------------------------------------------- #
LIVE_FIX_TICKETS = """\
**Mode: [LIVE]**

**Fix tickets**

- FIX-01: Build D2 (Input checks in tool code) for V_read_restricted  (owner tom, due 2026-10-24, risk R-01)
- FIX-02: Build D3 (Human approval for high-impact actions) for V_tool_misuse  (owner unassigned, due 2026-10-24, risk R-03)
"""

GOV_FILES = {"connected": True, "candidate_id": "RC-001", "evidence_batch": "b",
             "gate_report": "governance_out/gate_RC-001.txt",
             "fingerprint": ["governance_out/RC-001.json"],
             "audit_log": "governance_out/audit_log.jsonl",
             "risk_register_md": "governance_out/risk_register_before.md",
             "evidence_md": "governance_out/evidence_register.md",
             "fix_tickets_md": "governance_out/fix_tickets.md"}


def _read(repo: Path, name: str) -> str:
    return (repo / "governance_out" / name).read_text(encoding="utf-8")


def _stub_batch_for(ex, repo: Path) -> dict:
    """An evidence batch with the same attack count as the gate, so the gate is not held back."""
    note = ex.parse_gate_report(_read(repo, "gate_RC-001.txt"))["checks"][0]["note"]
    x, n = (int(v) for v in note.split(" ")[0].split("/"))
    return {"id": "b", "title": "stub", "summary": {"attack_success_rate": {"measured": True, "successes": x, "n": n}}}


def _build(ex, repo: Path):
    ctx = make_ctx(ex, repo, dict(GOV_FILES), allow_sample=True)
    return ctx, ex.build_governance(ctx, {"b": _stub_batch_for(ex, repo)})


def test_split_mode_line_returns_tag_note_body_and_offset(ex):
    text = "**Mode: [SCRIPTED]** Scripted traces, not measured.\n\n**Fix tickets**\n\n- one\n"
    tag, note, body, offset = ex.split_mode_line(text)
    assert (tag, note, offset) == ("SCRIPTED", "Scripted traces, not measured.", 2)
    assert body.split("\n")[0] == "**Fix tickets**"
    for k, line in enumerate(body.split("\n")):  # body line k is file line k + offset
        assert text.split("\n")[k + offset] == line


def test_split_mode_line_without_a_note_or_a_blank_line(ex):
    assert ex.split_mode_line("**Mode: [LIVE]**\n\nbody\n") == ("LIVE", "", "body", 2)
    assert ex.split_mode_line("**Mode: [SAVED]**\nbody\n") == ("SAVED", "", "body", 1)
    assert ex.split_mode_line("**Mode: [LIVE]**\r\n\r\nbody\r\n") == ("LIVE", "", "body", 2)
    assert ex.split_mode_line("**Mode: [LIVE]**\n") == ("LIVE", "", "", 1)


def test_split_mode_line_leaves_untagged_text_alone(ex):
    plain = "**Fix tickets**\n\n- one\n"
    assert ex.split_mode_line(plain) == (None, "", plain, 0)
    assert ex.split_mode_line("") == (None, "", "", 0)
    unknown = "**Mode: [DEMO]** note\n\nbody\n"
    assert ex.split_mode_line(unknown) == (None, "", unknown, 0)
    assert ex.split_mode_line("intro\n**Mode: [LIVE]**\n\nbody\n")[0] is None


def test_fix_tickets_of_the_scripted_cli_output_parse_with_their_line_numbers(ex, repo):
    text = _read(repo, "fix_tickets.md")
    assert text.startswith("**Mode: [SCRIPTED]** "), "the CLI tags every .md file with its mode"
    tickets = ex.parse_fix_tickets(text)
    lines = text.split("\n")
    assert [no for no, _t in tickets] == [i for i, ln in enumerate(lines, start=1) if ln.startswith("- ")]
    assert len(tickets) >= 3
    for no, t in tickets:
        assert lines[no - 1].startswith(f"- {t['id']}: ")


def test_fix_tickets_of_a_real_model_run_parse(ex):
    tickets = ex.parse_fix_tickets(LIVE_FIX_TICKETS)
    assert [(no, t["id"], t["owner"]) for no, t in tickets] == [(5, "FIX-01", "tom"), (6, "FIX-02", "unassigned")]


def test_bullet_files_of_the_scripted_cli_output_parse_with_their_line_numbers(ex, repo):
    for name in ("inventory_card.md", "manager_summary.md"):
        text = _read(repo, name)
        assert text.startswith("**Mode: [SCRIPTED]** ")
        bullets = ex.parse_md_bullets(text)
        lines = text.split("\n")
        assert len(bullets) >= 4, name
        for no, key, _value in bullets:
            assert lines[no - 1].startswith(f"- **{key}:**"), (name, no, key)


def test_traceability_steps_of_the_scripted_cli_output_parse_with_their_line_numbers(ex, repo):
    text = _read(repo, "traceability.md")
    steps = ex.parse_md_steps(text)
    lines = text.split("\n")
    assert [n for _no, n, _t in steps] == list(range(1, len(steps) + 1))
    for no, n, _t in steps:
        assert lines[no - 1].startswith(f"{n}. ")


def test_bullets_and_steps_of_a_real_model_run_parse(ex):
    bullets = ex.parse_md_bullets("**Mode: [LIVE]**\n\n**Manager summary**\n\n- **Live version:** none approved\n"
                                  "- **Open risks (3):** a, b, c\n")
    assert bullets == [(5, "Live version", "none approved"), (6, "Open risks (3)", "a, b, c")]
    steps = ex.parse_md_steps("**Mode: [LIVE]**\n\n**Traceability for ATK-0001**\n\n`ATK-0001`\n\n"
                              "1. ATK-0001: not found in runs\n")
    assert steps == [(7, 1, "ATK-0001: not found in runs")]


def test_an_untagged_file_and_a_foreign_first_line_behave_as_before(ex):
    plain = "**Fix tickets**\n\n- FIX-01: t  (owner tom, due 2026-10-24, risk R-01)\n"
    assert ex.parse_fix_tickets(plain)[0][0] == 3
    with pytest.raises(ex.SourceError):
        ex.parse_fix_tickets("**Mode: [DEMO]** x\n\n" + plain)
    with pytest.raises(ex.SourceError):
        ex.parse_fix_tickets("**Fix tickets**\n\nnot a ticket\n")


def test_assurance_of_the_scripted_cli_output_reaches_the_dashboard_data(ex, repo):
    """Before the fix the mode line killed the fix-ticket parser and with it the whole assurance block."""
    ctx, (_gate, _audit, assurance, _latest) = _build(ex, repo)
    assert not [w for w in ctx.warnings if w["text"].startswith("Assurance:")], ctx.warnings
    assert assurance["status"] == ex.SAMPLE
    assert len(assurance["risks"]) >= 1 and len(assurance["tickets"]) >= 3


def test_files_from_one_run_raise_no_mixed_mode_warning(ex, repo):
    ctx, _built = _build(ex, repo)
    assert not [w for w in ctx.warnings if "mixes" in w["text"]], ctx.warnings


def test_mixed_mode_tags_in_one_folder_raise_an_integrity_warning(ex, repo):
    path = repo / "governance_out" / "fix_tickets.md"
    lines = path.read_text(encoding="utf-8").split("\n")
    lines[0] = "**Mode: [LIVE]**"
    path.write_text("\n".join(lines), encoding="utf-8")
    ctx, _built = _build(ex, repo)
    mixed = [w for w in ctx.warnings if "mixes" in w["text"]]
    assert len(mixed) == 1 and mixed[0]["level"] == "integrity"
    assert "fix_tickets.md" in mixed[0]["text"] and "SCRIPTED" in mixed[0]["text"]
    assert str(repo) not in mixed[0]["text"]


def test_a_stale_gate_file_of_another_mode_is_noticed_too(ex, repo):
    path = repo / "governance_out" / "gate_RC-003.txt"
    path.write_text(path.read_text(encoding="utf-8").replace("[SCRIPTED]", "[SAVED]", 1), encoding="utf-8")
    ctx, _built = _build(ex, repo)
    mixed = [w for w in ctx.warnings if "mixes" in w["text"]]
    assert len(mixed) == 1 and "gate_RC-003.txt" in mixed[0]["text"] and "SAVED" in mixed[0]["text"]


# --------------------------------------------------------------------------- #
# Breakage 5: the Wilson interval can miss its own rate by float rounding.
# --------------------------------------------------------------------------- #
def _summary_row(k: int, n: int, rate: float, lo: float, hi: float) -> dict:
    cells = {"successes": str(k), "n": str(n), "rate": repr(rate), "ci_low": repr(lo), "ci_high": repr(hi)}
    return {"cells": cells, "row": 2}


def _metric(ex, ctx, row):
    return ex.metric_from_row(ctx, "t", "attack_success_rate", row, "runs", "results/summary.csv", None, "note")


def test_rounding_noise_at_the_edge_of_the_interval_keeps_the_meter(ex, tmp_path):
    from lab import metrics

    ctx = make_ctx(ex, tmp_path)
    # Float noise as the backend really produces it: the rate 0/n or n/n lies a hair outside [lo, hi].
    cases = []
    for n in range(1, 41):
        for k in (0, n):
            p = metrics.wilson(k, n)
            if not p.lo <= k / n <= p.hi:
                cases.append((k, n, k / n, p.lo, p.hi))
    cases.append((0, 5, 0.0, 1e-17, 0.43))      # a synthetic one, so the test never runs empty
    cases.append((5, 5, 1.0, 0.57, 1 - 1e-16))
    for k, n, rate, lo, hi in cases:
        out = _metric(ex, ctx, _summary_row(k, n, rate, lo, hi))
        assert out["geom"] == {"rate": rate, "lo": lo, "hi": hi}, (k, n)
        assert out["ci"] is not None, (k, n)
    assert not ctx.warnings, ctx.warnings


def test_a_rate_that_is_really_outside_its_interval_is_still_refused(ex, tmp_path):
    ctx = make_ctx(ex, tmp_path)
    for rate, lo, hi in ((0.5, 0.6, 0.9), (0.0, 1e-6, 0.4), (1.0, 0.6, 1 - 1e-6)):
        ctx.warnings.clear()
        out = _metric(ex, ctx, _summary_row(1, 2, rate, lo, hi))
        assert out["geom"] is None and out["ci"] is None, (rate, lo, hi)
        assert [w["level"] for w in ctx.warnings] == ["integrity"], ctx.warnings
        assert "outside its interval" in ctx.warnings[0]["text"]
