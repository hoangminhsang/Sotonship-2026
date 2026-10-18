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
