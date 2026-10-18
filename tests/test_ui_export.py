"""ui/export.py reads what scripts/run_governance.py writes (lane D, export fixes).

SCRIPTED input comes from running the CLI in-process (the pattern of
tests/test_governance_cli.py). Input from a real model run (tag LIVE) is a small
hand-written snippet in the same layout: no real run output is committed, because
it holds absolute paths. The synthetic numbers in the snippets only exercise the
parsers; nothing here pins a measured number of the backend.
"""
from __future__ import annotations

import importlib.util
import json
import re
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


# --------------------------------------------------------------------------- #
# Breakage 2: the mode comes from the tags, never from a prefix of the model digest.
# --------------------------------------------------------------------------- #
# The DEMO placeholder "sha256:845dbda0..." is the start of the real qwen digest. A real digest has
# 64 hex digits; this one only has the same first eight, which is all the old prefix test looked at.
REAL_SHAPED_DIGEST = "sha256:845dbda0" + "0" * 56


def tag_gates(repo: Path, tag: str | None, only: str | None = None) -> None:
    """Put `tag` on the first line of the gate files (tag=None removes it: an old, untagged file)."""
    for p in (repo / "governance_out").glob("gate_*.txt"):
        if only and p.name != only:
            continue
        text = re.sub(r"^\[[A-Z]+\] ", "", p.read_text(encoding="utf-8"))
        p.write_text((f"[{tag}] " if tag else "") + text, encoding="utf-8")


def tag_markdown(repo: Path, tag: str | None) -> None:
    """Put the mode line on every .md file (tag=None removes it, with the blank line under it)."""
    for p in (repo / "governance_out").glob("*.md"):
        lines = p.read_text(encoding="utf-8").split("\n")
        if lines[0].startswith("**Mode: ["):
            lines[0:2] = []
        if tag:
            lines[0:0] = [f"**Mode: [{tag}]**", ""]
        p.write_text("\n".join(lines), encoding="utf-8")


def set_digest(repo: Path, digest: str) -> None:
    for p in (repo / "governance_out").glob("RC-00?.json"):
        obj = json.loads(p.read_text(encoding="utf-8"))
        obj["model_digest"] = digest
        p.write_text(json.dumps(obj), encoding="utf-8")


def tag_json_files(repo: Path, tag: str | None) -> None:
    """Set (or, for tag=None, remove) "mode" in run_index.json and audit_verify.json, when the CLI wrote them."""
    for name in ("run_index.json", "audit_verify.json"):
        p = repo / "governance_out" / name
        if not p.is_file():
            continue
        obj = json.loads(p.read_text(encoding="utf-8"))
        if tag:
            obj["mode"] = tag
        else:
            obj.pop("mode", None)
        p.write_text(json.dumps(obj), encoding="utf-8")


def relabel(repo: Path, tag: str | None, digest: str | None = None) -> None:
    """Re-tag the whole CLI output copy. Only the mode logic is under test: the scripted numbers stay."""
    tag_gates(repo, tag)
    tag_markdown(repo, tag)
    tag_json_files(repo, tag)
    if digest:
        set_digest(repo, digest)


def _mode(ex, repo: Path, batch=None):
    ctx = make_ctx(ex, repo, dict(GOV_FILES))
    return ex.detect_mode(ctx, ctx.sources["governance"], batch)


def test_the_scripted_cli_output_is_detected_from_its_tags(ex, repo):
    assert _mode(ex, repo) == "SCRIPTED"


def test_a_real_model_run_that_shares_the_digest_prefix_is_real_data(ex, repo):
    """The reported bug: the smoke run of qwen was taken for scripted output and hidden."""
    relabel(repo, "LIVE", digest=REAL_SHAPED_DIGEST)
    ctx = make_ctx(ex, repo, dict(GOV_FILES))  # no --allow-sample: scripted output would be held back
    gate, audit, assurance, _latest = ex.build_governance(ctx, {"b": _stub_batch_for(ex, repo)})
    assert (gate["status"], audit["status"], assurance["status"]) == (ex.REAL, ex.REAL, ex.REAL)
    assert gate["sample_tag"] is None and gate["reason"] is None and [c["name"] for c in gate["checks"]] == GATE_CHECKS
    assert not [w for w in ctx.warnings if w["level"] == "integrity"], ctx.warnings
    assert _mode(ex, repo) == "LIVE"


def test_a_saved_run_is_real_data_too(ex, repo):
    relabel(repo, "SAVED", digest=REAL_SHAPED_DIGEST)
    ctx = make_ctx(ex, repo, dict(GOV_FILES))
    gate, _audit, _assurance, _latest = ex.build_governance(ctx, {"b": _stub_batch_for(ex, repo)})
    assert gate["status"] == ex.REAL
    assert _mode(ex, repo) == "SAVED"


def test_scripted_output_is_still_held_back_without_allow_sample(ex, repo):
    ctx = make_ctx(ex, repo, dict(GOV_FILES))
    gate, audit, assurance, _latest = ex.build_governance(ctx, {"b": _stub_batch_for(ex, repo)})
    assert gate["status"] == ex.PENDING and audit["status"] == ex.PENDING and assurance["status"] == ex.PENDING
    assert any("--allow-sample" in w["text"] for w in ctx.warnings)


def test_a_tag_beats_the_digest_in_both_directions(ex, repo):
    relabel(repo, "SCRIPTED", digest=REAL_SHAPED_DIGEST)   # scripted tags, real-looking digest
    assert _mode(ex, repo) == "SCRIPTED"
    relabel(repo, "LIVE", digest="sha256:845dbda0...")     # real tags, placeholder digest
    assert _mode(ex, repo) == "LIVE"


def test_untagged_legacy_files_are_scripted_only_for_the_exact_placeholder(ex, repo):
    relabel(repo, None)
    assert _mode(ex, repo) == "SCRIPTED"                   # the exact literal still marks old scripted output
    set_digest(repo, REAL_SHAPED_DIGEST)
    assert _mode(ex, repo) is None                         # the same first eight digits are not enough
    set_digest(repo, "sha256:845dbda0..." + "x")
    assert _mode(ex, repo) is None                         # nor is "starts with the literal"


def test_legacy_batch_without_start_times_still_marks_scripted_traces(ex, repo):
    relabel(repo, None, digest=REAL_SHAPED_DIGEST)
    assert _mode(ex, repo, {"_trace_count": 3, "_traces_started": 0}) == "SCRIPTED"
    assert _mode(ex, repo, {"_trace_count": 3, "_traces_started": 3}) is None


def test_run_index_comes_first_then_the_gate_then_the_md_files(ex, repo):
    run_index = repo / "governance_out" / "run_index.json"
    run_index.unlink(missing_ok=True)                      # newer CLI versions write one; this test makes its own
    relabel(repo, "SCRIPTED")
    tag_gates(repo, "SAVED", only="gate_RC-001.txt")
    assert _mode(ex, repo) == "SAVED"                      # the gate before the md files
    run_index.write_text('{"schema": "run-index/1", "mode": "LIVE"}', encoding="utf-8")
    assert _mode(ex, repo) == "LIVE"                       # run_index.json before the gate
    run_index.unlink()
    tag_gates(repo, None)
    assert _mode(ex, repo) == "SCRIPTED"                   # no gate tag: the md files speak


def test_the_md_tag_is_used_when_the_gate_has_none(ex, repo):
    relabel(repo, "LIVE", digest=REAL_SHAPED_DIGEST)
    tag_gates(repo, None)
    assert _mode(ex, repo) == "LIVE"


def test_a_run_index_with_another_mode_is_noticed_as_a_mix(ex, repo):
    (repo / "governance_out" / "run_index.json").write_text('{"mode": "LIVE"}', encoding="utf-8")
    ctx, _built = _build(ex, repo)
    mixed = [w for w in ctx.warnings if "mixes" in w["text"]]
    assert len(mixed) == 1 and "run_index.json" in mixed[0]["text"]


def test_an_unknown_mode_in_run_index_is_ignored_with_a_warning(ex, repo):
    (repo / "governance_out" / "run_index.json").write_text('{"mode": "DEMO"}', encoding="utf-8")
    ctx = make_ctx(ex, repo, dict(GOV_FILES))
    assert ex.detect_mode(ctx, ctx.sources["governance"], None) == "SCRIPTED"   # the gate speaks instead
    assert [w["level"] for w in ctx.warnings] == ["integrity"]
    assert "run_index.json" in ctx.warnings[0]["text"] and "DEMO" in ctx.warnings[0]["text"]


# --------------------------------------------------------------------------- #
# Breakage 9: the audit chain check says "10 entries, chain intact (head ...)" and names the tampering.
# The wordings come from the real lab.governance.audit, so a change there fails this test loudly.
# --------------------------------------------------------------------------- #
def _seed_log(path: Path, n: int = 6) -> None:
    from lab.governance import audit

    events = [{"event": "submit", "candidate_id": "RC-001", "by": "tom"},
              {"event": "verdict", "candidate_id": "RC-001", "verdict": "FAIL"},
              {"event": "decision", "candidate_id": "RC-001", "decision": "BLOCK", "reviewer": "sarah", "reason": "x"},
              {"event": "submit", "candidate_id": "RC-002", "by": "tom"},
              {"event": "verdict", "candidate_id": "RC-002", "verdict": "PASS"},
              {"event": "decision", "candidate_id": "RC-002", "decision": "APPROVE", "reviewer": "sarah", "reason": ""}]
    for ev in events[:n]:
        audit.append(path, ev)


def _edit_line(lines, i, **changes):
    from lab.governance import audit

    rec = json.loads(lines[i])
    rehash = changes.pop("_rehash", False)
    rec.update(changes)
    if rehash:
        rec["hash"] = audit._hash_record(rec)
    lines[i] = json.dumps(rec, sort_keys=True)


def _m_intact(lines):
    pass


def _m_edited(lines):
    _edit_line(lines, 2, decision="APPROVE")


def _m_seq_changed_and_rehashed(lines):
    _edit_line(lines, 2, seq=99, _rehash=True)


def _m_edited_and_rehashed(lines):
    _edit_line(lines, 2, decision="APPROVE", _rehash=True)   # the chain breaks one entry later


def _m_one_missing(lines):
    del lines[2]


def _m_two_missing(lines):
    del lines[2:4]


def _m_forged_insert(lines):
    forged = json.loads(lines[2])
    forged.update(seq=99, decision="APPROVE")
    lines.insert(2, json.dumps(forged, sort_keys=True))


def _m_duplicate_at_the_end(lines):
    lines.append(lines[0])


def _m_swapped(lines):
    lines[2], lines[3] = lines[3], lines[2]


def _m_malformed(lines):
    lines[2] = lines[2][:-1]


MUTATIONS = [_m_intact, _m_edited, _m_seq_changed_and_rehashed, _m_edited_and_rehashed, _m_one_missing,
             _m_two_missing, _m_forged_insert, _m_duplicate_at_the_end, _m_swapped, _m_malformed]


def _verify_json(check, mode: str = "SCRIPTED") -> str:
    """audit_verify.json as the CLI writes it (audit-verify/1): the LogCheck fields plus bookkeeping."""
    return json.dumps({"schema": "audit-verify/1", "ok": check.ok, "bad_seq": check.bad_seq, "kind": check.kind,
                       "detail": check.detail, "entries": check.entries, "last_hash": check.last_hash,
                       "verified_at": "2026-10-18T06:00:00+00:00", "mode": mode, "tamper_demo": None})


@pytest.mark.parametrize("mutate", MUTATIONS, ids=lambda f: f.__name__.removeprefix("_m_"))
def test_every_wording_of_the_real_audit_check_is_accepted(ex, tmp_path, mutate):
    from lab.governance import audit

    path = tmp_path / "audit_log.jsonl"
    _seed_log(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    mutate(lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    check = audit.verify_log(path)
    want = {"ok": check.ok, "bad_seq": check.bad_seq, "detail": check.detail}
    assert ex.parse_verify(_verify_json(check)) == want            # audit_verify.json
    assert ex.parse_verify(repr(check)) == want                    # the LogCheck line the CLI prints


def test_the_mutations_cover_every_kind_of_tampering_and_the_intact_case(tmp_path):
    from lab.governance import audit

    kinds = set()
    for i, mutate in enumerate(MUTATIONS):
        path = tmp_path / f"audit_{i}.jsonl"
        _seed_log(path)
        lines = path.read_text(encoding="utf-8").splitlines()
        mutate(lines)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        kinds.add(audit.verify_log(path).kind)
    assert kinds == {None, *audit.KINDS}


def test_an_empty_or_missing_log_is_accepted_as_ok(ex, tmp_path):
    from lab.governance import audit

    check = audit.verify_log(tmp_path / "nope.jsonl")
    assert check.ok and check.detail.startswith("no entries")
    assert ex.parse_verify(_verify_json(check)) == {"ok": True, "bad_seq": None, "detail": check.detail}


def test_the_older_wordings_are_still_accepted(ex):
    for ok, bad, detail in ((True, None, "10 entries, chain intact"),
                            (False, 3, "sequence jumped at entry 3 (saw seq=5)"),
                            (False, 4, "broken link at entry 4: prev_hash does not match entry 3"),
                            (False, 4, "contents of entry 4 were changed after it was written")):
        got = ex.parse_verify(json.dumps({"ok": ok, "bad_seq": bad, "detail": detail}))
        assert got == {"ok": ok, "bad_seq": bad, "detail": detail}


def test_the_ok_flag_must_agree_with_the_wording(ex):
    intact = "10 entries, chain intact (head 8709e0d29aed)"
    tampered = "entry #4 edited (contents changed after it was written)"
    for ok, bad, detail in ((False, None, intact), (True, None, tampered), (False, None, "no entries (log missing or empty)"),
                            (True, 4, tampered)):
        with pytest.raises(ex.SourceError, match="contradicts"):
            ex.parse_verify(json.dumps({"ok": ok, "bad_seq": bad, "detail": detail}))


def test_unknown_wording_and_broken_files_are_refused_as_source_errors(ex):
    with pytest.raises(ex.SourceError, match="wording"):
        ex.parse_verify(json.dumps({"ok": True, "bad_seq": None, "detail": "everything looks fine"}))
    with pytest.raises(ex.SourceError, match="wording"):
        ex.parse_verify(json.dumps({"ok": True, "bad_seq": None, "detail": "10 entries, chain intact (head xyz)"}))
    for broken in ("{not json", "[]", '{"ok": "yes", "detail": "x"}', "plain text", ""):
        with pytest.raises(ex.SourceError):
            ex.parse_verify(broken)
    with pytest.raises(ex.SourceError, match="bad_seq"):
        ex.parse_verify(json.dumps({"ok": False, "bad_seq": "4", "detail": "entry #4 missing (between #3 and #5)"}))


def _chain(ex, repo: Path, **extra):
    gov = dict(GOV_FILES, **extra)
    ctx = make_ctx(ex, repo, gov, allow_sample=True)
    _gate, audit_block, _assurance, _latest = ex.build_governance(ctx, {"b": _stub_batch_for(ex, repo)})
    return ctx, audit_block["chain"]


def test_audit_verify_json_in_the_folder_reaches_the_chain_node(ex, repo):
    from lab.governance import audit

    out = repo / "governance_out"
    check = audit.verify_log(out / "audit_log.jsonl")
    assert check.ok and "chain intact (head " in check.detail
    (out / "audit_verify.json").write_text(_verify_json(check), encoding="utf-8")
    ctx, chain = _chain(ex, repo)                     # sources.json names no audit_verify file
    assert chain["ok"] is True and chain["bad_seq"] is None and chain["reason"] is None
    assert chain["text"]["text"] == check.detail and chain["text"]["status"] == ex.SAMPLE
    assert chain["source_file"] == "governance_out/audit_verify.json"
    assert not [w for w in ctx.warnings if w["level"] == "integrity"], ctx.warnings
    assert ex.self_check(ctx, [ctx.prov[chain["text"]["prov"]]]) == []   # --check can re-read the detail


def test_a_tampered_chain_is_shown_as_not_ok_with_its_position(ex, repo):
    from lab.governance import audit

    out = repo / "governance_out"
    check = audit.verify_log(out / "audit_log_tampered_demo.jsonl")
    assert not check.ok
    (out / "verify_of_the_copy.json").write_text(_verify_json(check), encoding="utf-8")
    ctx, chain = _chain(ex, repo, audit_verify="governance_out/verify_of_the_copy.json")   # a named file wins
    assert chain["ok"] is False and chain["bad_seq"]["text"] == str(check.bad_seq)
    assert chain["text"]["text"] == check.detail
    assert not [w for w in ctx.warnings if w["level"] == "integrity"], ctx.warnings


def test_without_a_verify_file_the_chain_stays_pending(ex, repo):
    (repo / "governance_out" / "audit_verify.json").unlink(missing_ok=True)   # newer CLI versions write it
    ctx, chain = _chain(ex, repo)
    assert chain["ok"] is None and chain["text"] is None and chain["reason"] == ex.VERIFY_TERMINAL_ONLY
    assert chain["source_file"] is None
    assert not [w for w in ctx.warnings if w["level"] == "integrity"], ctx.warnings


def test_a_contradicting_verify_file_is_reported_and_leaves_the_rest_of_the_audit_alone(ex, repo):
    out = repo / "governance_out"
    (out / "audit_verify.json").write_text(
        json.dumps({"ok": True, "bad_seq": None, "detail": "entry #4 edited (contents changed after it was written)"}),
        encoding="utf-8")
    ctx = make_ctx(ex, repo, dict(GOV_FILES), allow_sample=True)
    _gate, audit_block, _assurance, _latest = ex.build_governance(ctx, {"b": _stub_batch_for(ex, repo)})
    assert audit_block["chain"]["ok"] is None and len(audit_block["entries"]) >= 5
    assert any(w["level"] == "integrity" and "Audit verify" in w["text"] and "contradicts" in w["text"]
               for w in ctx.warnings), ctx.warnings


# --------------------------------------------------------------------------- #
# Guard: the fatal rules of validate_demo stay (lane D does not touch them).
# --------------------------------------------------------------------------- #
def _minimal_demo(ex, text: str) -> dict:
    return {"schema": ex.SCHEMA, "exported_at": "2026-10-18T06:00:00+01:00", "allow_sample": False,
            "sample_present": False, "warnings": [], "provenance": [], "datasets": [], "batches": [],
            "current_batch": None, "console": {"note": text}, "policy": {}, "audit": {}, "assurance": {},
            "controls": {}, "compare": {}, "labels": {}}


def test_validate_demo_still_refuses_absolute_paths_and_user_names(ex, tmp_path, monkeypatch):
    ctx = make_ctx(ex, tmp_path)
    monkeypatch.setattr(ex.getpass, "getuser", lambda: "tester-name")
    clean = ex.validate_demo(ctx, _minimal_demo(ex, "proof in results/runs/x/runs.csv"))
    assert not [p for p in clean if "path" in p or "user name" in p], clean
    for path in ("/Users/someone/x/runs.csv", "/home/someone/x/runs.csv", "/private/tmp/x/runs.csv", "C:\\\\work\\\\runs.csv"):
        assert "an absolute path would be written" in ex.validate_demo(ctx, _minimal_demo(ex, path)), path
    assert "the local user name would be written" in ex.validate_demo(ctx, _minimal_demo(ex, "owner tester-name"))
