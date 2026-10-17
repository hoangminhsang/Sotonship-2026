"""Input handling in the menu app (pure helpers; never touches governance_out/)."""
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
    corpus.build_and_write()
    tests = corpus.read_jsonl(corpus.TEST_PATH)
    for sample in (8, 26, 30, 100):
        subset = app.live_subset(tests, sample)
        ids = [t["id"] for t in subset]
        assert len(ids) == len(set(ids))
        assert sum(t["kind"] == "benign" for t in subset) == min(sample, 20)


def test_ask_exits_cleanly_on_eof(monkeypatch, capsys):
    def eof(_prompt):
        raise EOFError
    monkeypatch.setattr(builtins, "input", eof)
    with pytest.raises(SystemExit) as exc:
        app.ask("Choose")
    assert exc.value.code == 0 and "bye." in capsys.readouterr().out


def test_ask_keeps_default_on_ctrl_c(monkeypatch):
    def interrupt(_prompt):
        raise KeyboardInterrupt
    monkeypatch.setattr(builtins, "input", interrupt)
    assert app.ask("Name", "sarah") == "sarah"


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
    return SimpleNamespace(candidates={"RC-001": {}}, policy=load_policy(),
                           next_candidate_id=lambda: "RC-002")


@pytest.mark.parametrize("cid, by, codes, why", [
    ("RC-001", "tom", [], "already exists"),
    ("RC-009", "tom", [], "already exists"),
    ("../RC-001", "tom", [], "may use only"),
    ("RC-002", "bob", [], "bob is not an engineer"),
    ("RC-002", "tom", ["D2", "D9"], "unknown defence code(s) D9"),
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
