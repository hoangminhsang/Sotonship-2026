"""Honesty guards for ui/index.html (the one-page demo dashboard).

The page runs in a browser, so these checks read its source. They keep three rules from slipping back:
- A scripted LIVE feed never decides what a real DEMO export says (chips, log check, badge).
- The log check comes from the exported data only.
- The page can call its own local runner and nothing else (CSP connect-src 'self').
"""
import json
import re
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "ui"
HTML = (UI / "index.html").read_text(encoding="utf-8")
COPY = json.loads((UI / "copy.js").read_text(encoding="utf-8").rsplit("window.COPY = ", 1)[1].strip()[:-1])


def _function(name):
    """The source of `function name(...)` up to the next function or section comment at the same indent."""
    start = HTML.index("function %s(" % name)
    rest = HTML[start + 1:]
    m = re.search(r"\n  (?:function |/\* ---)", rest)
    return HTML[start:start + 1 + (m.start() if m else len(rest))]


def test_the_log_check_never_reads_the_live_feed():
    block = _function("renderProof")
    assert "LIVE" not in block and "liveFeed" not in block and "sl.log" not in block
    assert "'intact'" in block and "audit" in block   # intact comes from the exported chain status word
    assert "tag" not in block.replace("tag ", "")  # no LIVE node tag


def test_chips_use_the_live_rail_only_while_a_live_or_replay_run_is_in_progress():
    chips = _function("paintChips")
    assert "liveInProgress()" in chips
    assert chips.index("liveInProgress()") < chips.index(".rail")
    guard = _function("liveInProgress")
    assert "anyRunning()" in guard and "LIVE" in guard and "REPLAY" in guard
    assert "'running'" in _function("anyRunning")
    assert "SCRIPTED" not in guard


def test_a_scripted_live_feed_gets_its_own_small_badge_on_the_live_view():
    assert "scripted_badge" in HTML and "scripted_badge" in COPY["page"]


def test_the_example_says_where_it_comes_from():
    assert "from_rehearsal" in HTML and "from_rehearsal" in COPY["page"]["happened"]
    assert "from_earlier" in COPY["page"]["happened"]


def test_the_page_may_call_only_its_own_origin():
    assert "connect-src 'self'" in HTML
    assert "connect-src 'none'" not in HTML
    urls = re.findall(r"""fetch\(\s*['"]([^'"]+)""", HTML)
    assert urls and all(u.startswith("/api/") for u in urls), urls


def test_the_run_bar_words_are_in_copy_and_have_no_digit():
    run = COPY["page"]["run"]
    for kind in ("demo", "live-short", "live-full", "models"):
        assert run["kinds"][kind].strip()
    assert "python ui/bridge.py --serve" in run["file_line"] or "bridge" in run["file_line"]
    assert not re.search(r"\d", json.dumps(run))


def test_the_chart_starts_neutral_and_only_follows_a_run_this_page_has_seen():
    state = _function("stateOf")
    assert "cands.length ? 'done'" not in state and "if (!nodes) return 'idle'" in state
    fresh = _function("fresh")
    assert "'running'" in fresh and "seen" in fresh
    assert "startedHere()" in HTML   # the page's own POST /api/run also counts
    assert "paintNodes" in _function("paint")


def test_node_states_have_their_own_looks_and_motion_respects_the_user_setting():
    for cls in (".node.running::before", ".node.done", ".node.error", ".node.skipped", "#wires path.done"):
        assert cls in HTML, cls
    assert "conic-gradient" in HTML and "stroke-dashoffset" in HTML
    assert "prefers-reduced-motion" in HTML


def test_the_stream_panel_reads_the_feed_and_the_pinned_example():
    assert 'id="stream"' in HTML and 'id="stream-body"' in HTML
    assert "feed_pin" in HTML and ".feed" in HTML and ".seq" in HTML
    assert "source.uid" in HTML        # a new run clears the stream
    assert "labels" in HTML and "classes" in HTML
    paint = _function("paintStream")
    assert "fresh()" in paint and "$('lower').hidden" in paint


def test_the_stream_words_are_in_copy_and_have_no_digit():
    stream = COPY["page"]["stream"]
    for key in ("head_scripted", "head_model", "scripted_agent", "hidden_tag", "caught", "held", "more_runs",
                "fingerprint", "gate", "audit", "reviewer_tag", "chain_ok", "chain_bad", "ticket",
                "eb_task", "eb_hidden", "eb_agent", "eb_verdict", "eb_batch", "eb_finger", "eb_gate", "eb_log",
                "read", "hidden_cap", "caught_word", "held_pre", "held_word", "held_cap", "gate_pre", "log_pre",
                "log_word", "entry_n", "hash", "chain_intact"):
        assert stream[key].strip(), key
    assert "scripted" in stream["head_scripted"].lower() and "no model" in stream["head_scripted"].lower()
    assert not re.search(r"\d", json.dumps(stream))
    block = HTML[HTML.index("the stream: what passes"):HTML.index("run bar: only when")]
    used = set(re.findall(r"\bS\.([a-z_]+)", block))
    assert used <= set(stream), sorted(used - set(stream))


def test_the_stream_is_a_scene_player_in_the_editorial_style():
    block = HTML[HTML.index("the stream: what passes"):HTML.index("run bar: only when")]
    assert "getTotalLength" in block and "strokeDashoffset" in block      # the drawings draw themselves
    assert "cubic-bezier(.65,0,.35,1)" in block and "blur(6px)" in block
    assert "feed_pin" not in block or "sD.pin" in block
    for colour in ("#F1F0EE", "#1A0F0A", "#B8ADA4", "#6E625A", "#8A8580", "#1C1A18"):
        assert colour in HTML, colour
    assert "letter-spacing:.18em" in HTML
    assert HTML.count("{ get: function ()") == 8                          # eight scenes, in order


# ---------------------------------------------------------------------------------------------
# The fourth view, "Test a model": import a model, add your own attacks, run, read the result.
# ---------------------------------------------------------------------------------------------
def _test_block():
    """The source of the Test a model section of the script."""
    return HTML[HTML.index("Test a model: import"):HTML.index("keys, cover, polling")]


TEST_KEYS = ("offline_title", "offline_file", "offline_failed", "intro", "model_title", "model_help", "source_label",
             "source_hint", "import", "need_source", "step_pull", "step_create", "step_check", "step_error",
             "name_label", "ready", "ready_text", "no_tools", "no_tools_text", "unknown", "unknown_text",
             "import_failed", "attacks_title", "attacks_help", "file_pre", "file_post", "attacks_none",
             "name_field", "goal_field", "place_field", "prompt_field", "add", "added", "goals", "places",
             "run_title", "run_help", "model_field", "kind_field", "extra", "extra_help", "no_models",
             "import_busy", "go", "result_title", "result_none", "standard", "yours", "not_available",
             "no_extra", "no_extra_runs", "broke", "range", "attack_one", "attack_many", "result_note",
             "no_answer")


def test_the_page_has_a_fourth_view_test_a_model_with_key_four_and_its_own_hash():
    assert re.search(r"var VIEWS = \['live', 'results', 'proof', 'test'\]", HTML)
    assert 'id="view-test"' in HTML
    assert COPY["page"]["tabs"]["test"] == "Test a model"
    assert COPY["page"]["tabs"]["live"] and COPY["page"]["tabs"]["results"] and COPY["page"]["tabs"]["proof"]
    assert "e.key <= '4'" in HTML                                  # keys one to three keep their views
    assert "v=(live|results|proof|test)" in HTML                   # #v=test opens the view
    assert "e.key >= '1'" in HTML


def test_typing_in_a_field_never_switches_the_view():
    keys = HTML[HTML.index("document.addEventListener('keydown'"):HTML.index("var lastRunning")]
    assert "'INPUT'" in keys and "'TEXTAREA'" in keys


def test_the_view_has_the_four_cards_in_order():
    ids = [HTML.index('id="%s"' % i) for i in ("tm-model", "tm-attacks", "tm-run", "tm-result")]
    assert ids == sorted(ids), "the cards come in the order Model, Your attacks, Run, Result"
    rule = HTML.index(".tm-grid {")
    assert "grid-template-columns:repeat(2, minmax(0, 1fr))" in HTML[rule:rule + 200]    # two columns at wide widths
    # the one-column rule must come AFTER the two-column rule, or the later rule wins and the page stays at two columns
    narrow = HTML.index("@media (max-width: 1100px) { .tm-grid")
    assert narrow > rule and "grid-template-columns:1fr" in HTML[narrow:narrow + 200]
    for area in ("model", "attacks", "run", "result"):
        assert "grid-area:%s" % area in HTML


def test_the_view_calls_only_the_agreed_api_paths_through_the_shared_helpers():
    block = _test_block()
    assert "postRetry('import'" in block and "postRetry('attacks'" in block
    assert "call('GET', 'attacks')" in block
    assert "postRetry('run'" in HTML                                # the run bar and this view share one start path
    assert "fetch(" not in block                                    # no second way to reach the network
    helper = _function("postRetry")
    assert "403" in helper and "'session'" in helper                # the retry on a restarted bridge stays
    assert "extra:" in block                                        # POST /api/run {kind, model, extra}
    assert "'live-short'" in block and "'live-full'" in block
    assert "show('live')" in block                                  # after Run the owner sees the live run


def test_api_and_user_text_only_ever_goes_in_as_text():
    block = _test_block()
    assert "innerHTML" not in block and "insertAdjacentHTML" not in block and "document.write" not in block
    assert "json.error" in block                                    # errors from the API are shown as text


def test_the_view_words_come_from_copy_and_exist():
    test = COPY["page"]["test"]
    for key in TEST_KEYS:
        assert test[key], key
    block = _test_block()
    used = set(re.findall(r"\bT\.([a-z_]+)", block))
    assert used <= set(test), sorted(used - set(test))
    assert "python ui/bridge.py --serve" in test["offline_file"]
    assert "python ui/bridge.py --serve" in test["offline_failed"]
    assert not re.search(r"\d", json.dumps(test))


def test_goals_and_places_use_the_exported_plain_labels_first():
    block = _test_block()
    assert "labels" in block and "DEMO" in block
    assert len(COPY["page"]["test"]["goals"]) == 3 and len(COPY["page"]["test"]["places"]) == 3
    for code in ("G1_read_restricted", "G2_exfiltrate", "G3_tool_misuse",
                 "P1_doc_body", "P2_doc_comment", "P3_asset_notes"):
        assert code in block


def test_the_result_card_reads_the_feed_and_the_last_run_and_keeps_the_gate_apart():
    block = _test_block()
    for word in ("liveFeed()", ".feed", "'gate'", "'extra'", ".rate", "asr_lo", "asr_hi", ".violations", "last.extra"):
        assert word in block, word
    assert "toFixed(1)" in block                                    # one decimal
    note = COPY["page"]["test"]["result_note"].lower()
    assert "not part of the gate" in note
    assert "no_extra_runs" in block                                 # n of zero or null is said in words


def test_the_local_runner_hint_shows_when_the_page_is_a_file_or_the_session_fails():
    block = _test_block()
    assert "offline_file" in block and "offline_failed" in block
    assert "testOff('file')" in HTML and "testOff('failed')" in HTML


def test_every_word_in_the_test_block_of_copy_is_used_by_the_page():
    block = _test_block()
    for key in COPY["page"]["test"]:
        assert ("T." + key) in block or ("'" + key + "'") in block, "copy.js page.test.%s is never used" % key
