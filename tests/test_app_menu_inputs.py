"""Input handling in the menu app (pure helpers; never touches governance_out/).

From PR #2 (was tests/test_app.py there). Merge adaptations, each marked
"merge:", keep review/hardening's behaviour: Ctrl-C and EOF cancel the action
(they never return a default or exit mid-action), candidate ids are RC-NNN,
refusal texts are the prompts' texts, and no test rewrites the committed corpus.
"""
from __future__ import annotations

import builtins
from types import SimpleNamespace

import pytest

from lab import app, corpus
from lab.governance import load_policy


@pytest.mark.parametrize("raw, n, noted", [("17", 17, False), ("abc", 8, True), ("0", 8, True),
                                           ("-3", 8, True), ("2.5", 8, True)])
def test_parse_sample_falls_back_to_8(raw, n, noted):
    got, note = app.parse_sample(raw)
    assert got == n and bool(note) == noted


def test_live_subset_never_repeats_a_test():
    tests = corpus.read_jsonl(corpus.TEST_PATH)  # merge: the committed split, never rebuilt
    for sample in (8, 26, 30, 100):
        subset = app.live_subset(tests, sample)
        ids = [t["id"] for t in subset]
        assert len(ids) == len(set(ids))
        assert sum(t["kind"] == "benign" for t in subset) == min(sample, 20)


def test_ask_exits_cleanly_on_eof(monkeypatch, capsys):
    def eof(_prompt):
        raise EOFError
    monkeypatch.setattr(builtins, "input", eof)
    # merge: EOF cancels the action (app.Cancelled); with stdin closed every
    # prompt cancels, so the menus unwind and main() ends with "bye." (see
    # tests/test_app.py::test_eof_at_main_menu_exits_cleanly).
    with pytest.raises(app.Cancelled):
        app.ask("Choose")
    assert "(cancelled)" in capsys.readouterr().out


def test_ask_keeps_default_on_ctrl_c(monkeypatch):
    def interrupt(_prompt):
        raise KeyboardInterrupt
    monkeypatch.setattr(builtins, "input", interrupt)
    with pytest.raises(app.Cancelled):  # merge: Ctrl-C cancels; it never answers with the default
        app.ask("Name", "sarah")


def test_corrupt_state_file_stops_with_a_message(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "WORK", tmp_path)
    bad = tmp_path / "candidates.json"
    bad.write_text("{not json")
    with pytest.raises(SystemExit, match="candidates.json is not valid JSON"):
        app._load_state_json(bad)
    assert bad.read_text() == "{not json"  # left as found


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "WORK", tmp_path)
    (tmp_path / "RC-009").mkdir()  # evidence on disk but missing from candidates.json
    # merge: the app's state names its own workspace (GovState.work)
    return SimpleNamespace(candidates={"RC-001": {}}, policy=load_policy(), work=tmp_path,
                           next_candidate_id=lambda: "RC-002")


@pytest.mark.parametrize("cid, by, codes, why", [
    ("RC-001", "tom", [], "already exists"),
    ("RC-009", "tom", [], "already exists"),
    ("../RC-001", "tom", [], "is not a candidate id"),         # merge: our RC-NNN id rule and text
    ("RC-002", "bob", [], "bob is not an engineer"),
    ("RC-002", "tom", ["D2", "D9"], "unknown defence code(s): D9"),  # merge: our text
])
def test_bad_new_candidates_are_refused(state, cid, by, codes, why):
    refusal = app._refuse_new_candidate(state, cid, by, codes)
    assert refusal.startswith("REFUSED:") and why in refusal


def test_good_new_candidate_is_allowed(state):
    assert app._refuse_new_candidate(state, "RC-002", "tom", ["D1", "D2", "D3"]) is None


@pytest.mark.parametrize("raw, clean, dropped", [
    ("\udcc3es-003", "es-003", 1),          # half a UTF-8 char left by an IME
    ("RC-\u200b001 ", "RC-001", 1),         # zero-width space
    ("\ufffdRC-005", "RC-005", 1),          # replacement char
    ("ve\u0301", "v\u00e9", 0),             # real accented letter kept (NFC)
    ("RC-001", "RC-001", 0),
])
def test_clean_input_drops_only_unreadable_characters(raw, clean, dropped):
    assert app.clean_input(raw) == (clean, dropped)


def test_ask_matches_an_id_despite_a_stray_byte(monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", lambda _p: "\udcc3es-003")
    assert app.ask("Compare against prior candidate") == "es-003"
    assert "ignored 1 unreadable character" in capsys.readouterr().out


# --- merge: PR #2's "a damaged audit log blocks submit, decide and role changes",
#     checked against the merged log (chain + head anchor), driving the real menu.

def _drive(monkeypatch, work, answers):
    it = iter(answers)

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise RuntimeError(f"test input exhausted at prompt {prompt!r}") from None

    monkeypatch.setattr(builtins, "input", fake_input)
    app.main(work)


NEW_DEMO = ["1", "1", "", "1", "", "1", ""]   # red team -> new RC-001, engineer #1, no defences, DEMO


@pytest.mark.parametrize("action", [
    ["1", "1", "", "1", "", "", "3"],          # submit RC-002: blocked before the mode prompt
    ["2", "2", "", "", "7"],                   # decide
    ["2", "3", "", "7"],                       # manage roles
])
def test_a_truncated_log_blocks_actions_before_anything_is_written(tmp_path, monkeypatch, capsys, action):
    from lab.governance import audit
    work = tmp_path / "governance_out"
    _drive(monkeypatch, work, [*NEW_DEMO, "3", "4"])
    log = work / "audit_log.jsonl"
    audit.append(log, {"event": "note", "by": "tom"})
    log.write_text(log.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in work.iterdir() if p.is_file()}
    capsys.readouterr()

    _drive(monkeypatch, work, [*action, "4"])
    out = capsys.readouterr().out
    assert "audit log problem" in out and "truncated" in out and "nothing was recorded" in out
    assert {p.name: p.read_bytes() for p in work.iterdir() if p.is_file()} == before
    assert not (work / "RC-002").exists()
