"""Tests for scripts/fill_script.py -- the demo-script timer and number filler.

Offline and fast: every test works on small temp files. The script is loaded
the same way as the other script tests (importlib), plus one subprocess test
that proves `python scripts/fill_script.py ...` runs from the command line.

Contract under test (see the module docstring of the script):
  * a beat is a heading that ends with its slot, e.g. "## Problem [20s]"
  * lines that start with ">" are stage directions: not counted, not spoken
  * {{key}} is filled from numbers.json (flat value, or {"value", "source"})
  * report: one row per beat (words, seconds, slot, OVER), totals, missing keys
  * exit 0 clean, 1 beat over slot or key missing, 2 usage or file error
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "fill_script.py"


@pytest.fixture(scope="module")
def fs():
    spec = importlib.util.spec_from_file_location("fill_script_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- helpers


def words(n: int) -> str:
    """n plain spoken words."""
    return " ".join(["word"] * n)


def beat(title: str, slot: str, *lines: str) -> str:
    return "\n".join([f"## {title} [{slot}]", *lines, ""]) + "\n"


ROW = re.compile(
    r"^(?P<title>\S.*?)\s{2,}(?P<words>\d+)\s+(?P<secs>\d+\.\d)\s+(?P<slot>\d+(?:\.\d+)?s)"
    r"(?:\s+(?P<flag>OVER))?\s*$",
    re.M,
)


def rows(out: str) -> dict:
    return {m["title"]: m for m in ROW.finditer(out)}


def run(fs, capsys, tmp_path, template: str, numbers=None, *extra: str):
    """Write the template (and numbers, if given), call main, return (code, out, err)."""
    tpl = tmp_path / "script.md"
    tpl.write_text(template, encoding="utf-8")
    num = tmp_path / "numbers.json"  # left absent when numbers is None
    if numbers is not None:
        num.write_text(json.dumps(numbers), encoding="utf-8")
    code = fs.main([str(tpl), "--numbers", str(num), *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------- word counting


def test_count_words_plain(fs):
    assert fs.count_words("one two  three\nfour") == 4


def test_count_words_splits_dashes_and_skips_stray_punctuation(fs):
    # An em dash glued to words is a word break; a lone dash or bullet is not a word.
    assert fs.count_words("chat\u2014they take") == 3
    assert fs.count_words("a \u2014 b - c * d") == 4
    # A hyphenated word is one word.
    assert fs.count_words("hash-chained log") == 2


# ------------------------------------------------------- timing and slots


def test_row_per_beat_with_words_seconds_and_slot(fs, capsys, tmp_path):
    tpl = beat("Problem", "20s", words(50)) + beat("Pivot", "10s", words(20))
    code, out, _ = run(fs, capsys, tmp_path, tpl, {})
    r = rows(out)
    assert (r["Problem"]["words"], r["Problem"]["secs"], r["Problem"]["slot"]) == ("50", "20.0", "20s")
    assert (r["Pivot"]["words"], r["Pivot"]["secs"], r["Pivot"]["slot"]) == ("20", "8.0", "10s")
    assert r["Problem"]["flag"] is None and r["Pivot"]["flag"] is None
    assert (r["Total"]["words"], r["Total"]["secs"], r["Total"]["slot"]) == ("70", "28.0", "30s")
    assert code == 0


def test_beat_over_its_slot_is_flagged_and_exits_1(fs, capsys, tmp_path):
    tpl = beat("Problem", "20s", words(50)) + beat("Pivot", "10s", words(26))
    code, out, _ = run(fs, capsys, tmp_path, tpl, {})
    r = rows(out)
    assert r["Pivot"]["secs"] == "10.4"
    assert r["Pivot"]["flag"] == "OVER"
    assert r["Problem"]["flag"] is None
    assert "Beats over slot: 1 (Pivot)" in out
    assert code == 1


def test_beat_exactly_at_its_slot_is_not_over(fs, capsys, tmp_path):
    code, out, _ = run(fs, capsys, tmp_path, beat("Pivot", "10s", words(25)), {})
    assert rows(out)["Pivot"]["flag"] is None
    assert "Beats over slot: none" in out
    assert code == 0


def test_wpm_option_changes_the_estimate(fs, capsys, tmp_path):
    tpl = beat("Pivot", "10s", words(25))
    code, out, _ = run(fs, capsys, tmp_path, tpl, {}, "--wpm", "120")
    r = rows(out)
    assert r["Pivot"]["secs"] == "12.5"
    assert r["Pivot"]["flag"] == "OVER"
    assert "120 wpm" in out
    assert code == 1


def test_slot_may_have_a_decimal_and_spaces(fs, capsys, tmp_path):
    tpl = "## Close [ 7.5 s ]\n" + words(18) + "\n"  # 18 words = 7.2 s at 150 wpm
    code, out, _ = run(fs, capsys, tmp_path, tpl, {})
    r = rows(out)
    assert r["Close"]["slot"] == "7.5s"
    assert r["Close"]["flag"] is None
    assert code == 0


def test_several_paragraphs_and_lines_all_count(fs, capsys, tmp_path):
    tpl = beat("Problem", "20s", words(10), "", words(10) + "\n" + words(10))
    _, out, _ = run(fs, capsys, tmp_path, tpl, {})
    assert rows(out)["Problem"]["words"] == "30"


# ------------------------------------------- what is and is not spoken text


def test_stage_directions_are_not_counted(fs, capsys, tmp_path):
    tpl = beat("Problem", "20s", "> Screen 2: " + words(30), words(5), ">another direction " + words(9))
    _, out, _ = run(fs, capsys, tmp_path, tpl, {})
    assert rows(out)["Problem"]["words"] == "5"


def test_text_outside_beats_is_ignored(fs, capsys, tmp_path):
    tpl = (
        "# Title\n\nKeys: {{ignored.key}} and " + words(40) + "\n\n"
        + beat("Problem", "20s", words(5))
        + "## Notes\n\n" + words(60) + " {{also.ignored}}\n"
    )
    code, out, _ = run(fs, capsys, tmp_path, tpl, {})
    r = rows(out)
    assert r["Problem"]["words"] == "5"
    assert "Notes" not in r
    assert "ignored.key" not in out and "also.ignored" not in out
    assert code == 0


def test_a_heading_without_a_slot_ends_the_beat(fs, capsys, tmp_path):
    tpl = beat("Problem", "20s", words(5)) + "### Sub heading\n" + words(40) + "\n"
    _, out, _ = run(fs, capsys, tmp_path, tpl, {})
    assert rows(out)["Problem"]["words"] == "5"


def test_beat_heading_levels_one_to_six_are_accepted(fs, capsys, tmp_path):
    tpl = "# A [10s]\n" + words(5) + "\n### B [10s]\n" + words(6) + "\n"
    _, out, _ = run(fs, capsys, tmp_path, tpl, {})
    r = rows(out)
    assert (r["A"]["words"], r["B"]["words"]) == ("5", "6")


def test_a_bracket_that_is_not_the_slot_does_not_make_a_beat(fs, capsys, tmp_path):
    tpl = "## Intro [draft]\n" + words(40) + "\n" + beat("Real", "10s", words(5))
    _, out, _ = run(fs, capsys, tmp_path, tpl, {})
    r = rows(out)
    assert "Intro" not in r
    assert r["Real"]["words"] == "5"


# ------------------------------------------------------------------ fill


def test_flat_numbers_fill_and_words_are_counted_after_filling(fs, capsys, tmp_path):
    tpl = beat("RC-001", "20s", "Range {{rc001.asr_range}} and rate {{rc001.asr}}.")
    nums = {"rc001.asr": "42.3%", "rc001.asr_range": "[32.0%, 53.4%]"}  # 2 tokens + 1 token
    out_file = tmp_path / "filled.md"
    code, out, _ = run(fs, capsys, tmp_path, tpl, nums, "--out", str(out_file))
    # Range(1) [32.0%,(1) 53.4%](1) and(1) rate(1) 42.3%.(1) = 6
    assert rows(out)["RC-001"]["words"] == "6"
    assert out_file.read_text(encoding="utf-8").count("Range [32.0%, 53.4%] and rate 42.3%.") == 1
    assert code == 0


def test_value_and_source_form_uses_the_value(fs, capsys, tmp_path):
    tpl = beat("RC-001", "20s", "Rate {{rc001.asr}}.")
    nums = {"rc001.asr": {"value": "42.3%", "source": "results/governance/x/summary.csv"}}
    out_file = tmp_path / "filled.md"
    code, out, _ = run(fs, capsys, tmp_path, tpl, nums, "--out", str(out_file))
    assert "Rate 42.3%." in out_file.read_text(encoding="utf-8")
    assert code == 0


def test_json_numbers_are_written_as_plain_text(fs, capsys, tmp_path):
    tpl = beat("Runs", "20s", "{{runs.total}} runs, {{ratio}} ratio.")
    out_file = tmp_path / "filled.md"
    run(fs, capsys, tmp_path, tpl, {"runs.total": 138, "ratio": 0.423}, "--out", str(out_file))
    assert "138 runs, 0.423 ratio." in out_file.read_text(encoding="utf-8")


def test_spaces_inside_the_braces_are_allowed(fs, capsys, tmp_path):
    tpl = beat("RC-001", "20s", "Rate {{ rc001.asr }}.")
    out_file = tmp_path / "filled.md"
    code, _, _ = run(fs, capsys, tmp_path, tpl, {"rc001.asr": "42.3%"}, "--out", str(out_file))
    assert "Rate 42.3%." in out_file.read_text(encoding="utf-8")
    assert code == 0


def test_a_filled_value_is_not_expanded_again(fs, capsys, tmp_path):
    tpl = beat("RC-001", "20s", "Value {{a}}.")
    out_file = tmp_path / "filled.md"
    code, out, _ = run(fs, capsys, tmp_path, tpl, {"a": "see {{b}}", "b": "NOPE"}, "--out", str(out_file))
    text = out_file.read_text(encoding="utf-8")
    assert "Value see {{b}}." in text
    assert "NOPE" not in text
    assert code == 0  # {{b}} came from a value, it is not a key of the script


def test_missing_key_is_reported_left_in_place_and_exits_1(fs, capsys, tmp_path):
    tpl = beat("RC-001", "20s", "Rate {{rc001.asr}} and {{runs.total}}.") + beat("Close", "7s", "{{runs.total}}")
    out_file = tmp_path / "filled.md"
    code, out, _ = run(fs, capsys, tmp_path, tpl, {"rc001.asr": "42.3%"}, "--out", str(out_file))
    text = out_file.read_text(encoding="utf-8")
    assert "Rate 42.3% and {{runs.total}}." in text
    assert "Missing keys: 1" in out
    assert "{{runs.total}}" in out
    assert "RC-001, Close" in out  # the beats that use it, in script order
    assert "rc001.asr" not in out.rsplit("Missing keys", 1)[1]
    assert code == 1


def test_no_numbers_file_reports_every_key_but_still_prints_the_timing(fs, capsys, tmp_path):
    tpl = beat("A", "20s", "{{one}} {{two.x}} " + words(48)) + beat("B", "10s", "{{one}}")
    code, out, _ = run(fs, capsys, tmp_path, tpl, None)  # numbers.json absent
    r = rows(out)
    assert r["A"]["words"] == "50" and r["A"]["secs"] == "20.0"
    assert r["B"]["words"] == "1"
    assert "Missing keys: 2" in out
    assert "{{one}}" in out and "{{two.x}}" in out
    assert "not found" in out
    assert code == 1


def test_null_and_blank_values_count_as_missing(fs, capsys, tmp_path):
    tpl = beat("A", "20s", "{{n}} {{blank}} {{nodict}} {{ok}}")
    nums = {"n": None, "blank": "  ", "nodict": {"source": "x"}, "ok": "fine"}
    code, out, _ = run(fs, capsys, tmp_path, tpl, nums)
    assert "Missing keys: 3" in out
    assert "{{ok}}" not in out
    assert code == 1


def test_all_keys_present_and_in_slot_exits_0(fs, capsys, tmp_path):
    tpl = beat("A", "20s", "{{one}} " + words(10))
    code, out, _ = run(fs, capsys, tmp_path, tpl, {"one": "1"})
    assert "Missing keys: none" in out
    assert code == 0


def test_stage_direction_placeholders_are_filled_and_missing_ones_reported(fs, capsys, tmp_path):
    tpl = beat("A", "20s", "> Show {{runs.total}} tiles and {{models.n}} models", words(5))
    out_file = tmp_path / "filled.md"
    code, out, _ = run(fs, capsys, tmp_path, tpl, {"runs.total": "138"}, "--out", str(out_file))
    assert "> Show 138 tiles and {{models.n}} models" in out_file.read_text(encoding="utf-8")
    assert rows(out)["A"]["words"] == "5"  # still not counted
    assert "{{models.n}}" in out
    assert code == 1


def test_placeholders_outside_beats_are_left_alone(fs, capsys, tmp_path):
    tpl = "Keys: {{rc001.asr}}\n\n" + beat("A", "20s", "Rate {{rc001.asr}}.")
    out_file = tmp_path / "filled.md"
    run(fs, capsys, tmp_path, tpl, {"rc001.asr": "42.3%"}, "--out", str(out_file))
    text = out_file.read_text(encoding="utf-8")
    assert "Keys: {{rc001.asr}}" in text
    assert "Rate 42.3%." in text


def test_bom_and_windows_line_endings_are_read(fs, capsys, tmp_path):
    tpl = tmp_path / "script.md"
    tpl.write_bytes(b"\xef\xbb\xbf## A [10s]\r\n> direction\r\nword word {{k}}\r\n")
    num = tmp_path / "numbers.json"
    num.write_text(json.dumps({"k": "v"}), encoding="utf-8")
    code = fs.main([str(tpl), "--numbers", str(num)])
    out = capsys.readouterr().out
    assert rows(out)["A"]["words"] == "3"
    assert code == 0


def test_brace_look_alikes_are_not_keys(fs, capsys, tmp_path):
    tpl = beat("A", "20s", "Use {{}} and {{ two words }} and {single} as written.")
    code, out, _ = run(fs, capsys, tmp_path, tpl, {})
    assert "Missing keys: none" in out
    assert code == 0


def test_fenced_code_blocks_are_skipped_entirely(fs, capsys, tmp_path):
    fence = "```"
    tpl = (
        "Format example:\n" + fence + "\n## Example [99s]\nsome {{fenced.key}} words\n" + fence + "\n\n"
        + beat("Real", "20s", words(5), "~~~~", "## Inside [1s]", "{{in.fence}}", "~~~~", words(3))
    )
    out_file = tmp_path / "filled.md"
    code, out, _ = run(fs, capsys, tmp_path, tpl, {"fenced.key": "X", "in.fence": "Y"}, "--out", str(out_file))
    r = rows(out)
    assert set(r) == {"Real", "Total"}
    assert r["Real"]["words"] == "8"  # the fenced lines inside the beat are not spoken
    text = out_file.read_text(encoding="utf-8")
    assert "{{fenced.key}}" in text and "{{in.fence}}" in text  # fenced text is not filled
    assert code == 0


def test_a_code_fence_that_never_closes_exits_2(fs, capsys, tmp_path):
    tpl = beat("A", "20s", words(5)) + "```\n" + beat("B", "10s", words(5))
    code, out, err = run(fs, capsys, tmp_path, tpl, {})
    assert code == 2
    assert "line 4" in err and "never closes" in err
    assert out == ""


# ------------------------------------------------------------------- --out


def test_out_writes_the_full_filled_file_and_leaves_the_template(fs, capsys, tmp_path):
    tpl = "# Title\n\nnotes\n\n" + beat("A", "20s", "> screen", "Rate {{k}}.")
    out_file = tmp_path / "sub" / "filled.md"  # parent folder does not exist yet
    code, _, _ = run(fs, capsys, tmp_path, tpl, {"k": "9"}, "--out", str(out_file))
    assert code == 0
    assert out_file.read_text(encoding="utf-8") == tpl.replace("{{k}}", "9")
    assert (tmp_path / "script.md").read_text(encoding="utf-8") == tpl  # template untouched


def test_out_refuses_to_overwrite_the_template(fs, capsys, tmp_path):
    tpl = beat("A", "20s", "Rate {{k}}.")
    template = tmp_path / "script.md"
    template.write_text(tpl, encoding="utf-8")
    code = fs.main([str(template), "--numbers", str(tmp_path / "n.json"), "--out", str(template)])
    err = capsys.readouterr().err
    assert code == 2
    assert "overwrite" in err
    assert template.read_text(encoding="utf-8") == tpl


# ----------------------------------------------------------- errors, exit 2


def test_missing_template_exits_2(fs, capsys, tmp_path):
    code = fs.main([str(tmp_path / "nope.md"), "--numbers", str(tmp_path / "n.json")])
    assert code == 2
    assert "nope.md" in capsys.readouterr().err


def test_template_without_beats_exits_2(fs, capsys, tmp_path):
    code, out, err = run(fs, capsys, tmp_path, "# Title\n\nJust notes.\n", {})
    assert code == 2
    assert "no beats" in err


def test_invalid_numbers_json_exits_2_without_a_traceback(fs, capsys, tmp_path):
    tpl = tmp_path / "script.md"
    tpl.write_text(beat("A", "20s", "{{k}}"), encoding="utf-8")
    bad = tmp_path / "numbers.json"
    bad.write_text("{not json", encoding="utf-8")
    code = fs.main([str(tpl), "--numbers", str(bad)])
    err = capsys.readouterr().err
    assert code == 2
    assert "numbers.json" in err and "Traceback" not in err


def test_numbers_json_that_is_not_an_object_exits_2(fs, capsys, tmp_path):
    tpl = tmp_path / "script.md"
    tpl.write_text(beat("A", "20s", "{{k}}"), encoding="utf-8")
    bad = tmp_path / "numbers.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    assert fs.main([str(tpl), "--numbers", str(bad)]) == 2
    assert "object" in capsys.readouterr().err


@pytest.mark.parametrize("wpm", ["0", "-5", "abc"])
def test_bad_wpm_is_a_usage_error(fs, tmp_path, wpm):
    tpl = tmp_path / "script.md"
    tpl.write_text(beat("A", "20s", "x"), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        fs.main([str(tpl), "--wpm", wpm])
    assert exc.value.code == 2


# ------------------------------------------------------------- defaults/CLI


def test_default_numbers_path_is_ui_data_numbers_json(fs):
    assert fs.DEFAULT_NUMBERS == REPO / "ui" / "data" / "numbers.json"


def test_numbers_default_is_used_when_the_flag_is_absent(fs, capsys, tmp_path, monkeypatch):
    default = tmp_path / "numbers.json"
    default.write_text(json.dumps({"k": "7"}), encoding="utf-8")
    monkeypatch.setattr(fs, "DEFAULT_NUMBERS", default)
    tpl = tmp_path / "script.md"
    tpl.write_text(beat("A", "20s", "{{k}} " + words(5)), encoding="utf-8")
    code = fs.main([str(tpl)])
    assert code == 0
    assert "Missing keys: none" in capsys.readouterr().out


def test_command_line_run_and_non_ascii_titles_do_not_crash(tmp_path):
    # Windows consoles (cp1252) cannot print every character; the report must not crash.
    tpl = tmp_path / "script.md"
    tpl.write_text(beat("Why now \u2014 the gap", "18s", words(40), "{{k}}"), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUTF8"}
    env["PYTHONIOENCODING"] = "ascii"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(tpl), "--numbers", str(tmp_path / "none.json")],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 1, proc.stderr  # key {{k}} is missing
    assert "Traceback" not in proc.stderr
    assert "Why now" in proc.stdout
    assert "{{k}}" in proc.stdout
