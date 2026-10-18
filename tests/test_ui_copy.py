"""Checks for ui/copy.js, the one file that holds every word the dashboard shows.

The rules (PLAN.md, "Design for change"):
- No number is typed in copy.js, and no digit appears anywhere in the file, ids included.
  Numbers come from data, through {placeholders}.
- Every {placeholder} is in the allowed list below, so a typo cannot reach the screen.
- Screens are named, never numbered. app.js maps the names to the keys one to seven.
- The words are plain: no banned wording, and the claims the plan corrected stay corrected.

copy.js is written as strict JSON after "window.COPY = ", so this test can read it
without a JavaScript engine.
"""
import json
import re
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "ui"
COPY_JS = UI / "copy.js"

# Every {placeholder} copy.js may use, and what fills it:
#   t    elapsed time text, from the pipeline view (lane E1)
#   cmd  the command that rebuilds the evidence screens, from the bridge
#   cand candidate id from data, for example the first release candidate
#   team team name (also copy.js)
#   first, last  the first and last screen key, from the router
#   who  reviewer name from data
#   n    a count of rows that are listed on the screen
ALLOWED_PLACEHOLDERS = {"t", "cmd", "cand", "team", "first", "last", "who", "n"}

# The beat ids. Lane G writes its script against the same ids (fill_script.py, SCRIPT_CUT.md).
BEAT_IDS = ["problem", "why_now", "pivot", "baseline", "duties", "tamper",
            "safeguard", "limits", "business", "close"]
SCREEN_NAMES = ["console", "attacks", "compare", "decisions", "assurance", "pipeline", "models"]
NODE_IDS = [
    "rt.attack", "rt.record", "rt.check", "rt.rate", "rt.fixes",
    "sl.fingerprint", "sl.gate", "sl.review", "sl.log", "fix.ticket",
    "ll.risks", "ll.controls", "ll.evidence", "ll.trace", "ll.summary",
]
STATE_KEYS = ["idle", "running", "done", "fail", "pass", "scripted", "error", "skipped"]
EDGE_KEYS = ["every_change", "same_test_data", "open_risk", "test_again"]
PIPELINE_KEYS = ["title", "status_done", "status_export_missing", "replay_hint"]
BANNED_WORDS = ["unchangeable", "immutable", "tamper-proof", "permanent", "halts unclassified"]


def _load():
    text = COPY_JS.read_text(encoding="utf-8")
    marker = "window.COPY = "
    assert marker in text, "copy.js must set window.COPY"
    body = text.rsplit(marker, 1)[1].strip()
    assert body.endswith(";"), "copy.js must end the assignment with a semicolon"
    return text, json.loads(body[:-1])


def _strings(node, path=""):
    """Yield (path, string) for every string value. Object keys are not values."""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _strings(value, f"{path}[{i}]")


def test_copy_js_has_no_digit():
    text, _ = _load()
    digits = sorted(set(re.findall(r"\d", text)))
    assert not digits, f"copy.js holds digits {digits}: numbers come from data through placeholders"


def test_every_placeholder_is_allowed():
    _, copy = _load()
    for path, value in _strings(copy):
        found = re.findall(r"\{([^{}]*)\}", value)
        for name in found:
            assert name in ALLOWED_PLACEHOLDERS, f"{path}: placeholder {{{name}}} is not allowed"
        leftover = re.sub(r"\{[^{}]*\}", "", value)
        assert "{" not in leftover and "}" not in leftover, f"{path}: stray brace in {value!r}"


def test_product_block():
    _, copy = _load()
    product = copy["product"]
    assert product["name"] == "Redline"
    assert product["team"] == "Gatekeepers"
    assert product["tagline"].strip()
    # the cover and the header are built from these words, nothing else
    assert "{team}" in product["team_line"]
    assert product["what"].strip() and product["track"].strip()
    assert copy["cover"]["hint"].strip()


def test_beats_use_the_plan_ids_in_order_and_name_their_screens():
    _, copy = _load()
    beats = copy["beats"]
    assert [b["id"] for b in beats] == BEAT_IDS
    for beat in beats:
        assert set(beat) >= {"id", "title", "screen", "focus", "note"}
        assert beat["title"].strip() and beat["note"].strip()
        assert beat["screen"] in SCREEN_NAMES, f"{beat['id']}: unknown screen {beat['screen']!r}"
        assert beat["focus"] is None or (isinstance(beat["focus"], str) and beat["focus"]), beat["id"]


def test_beat_ids_are_unique_plain_words():
    # A new beat (for example a live attack beat on the pipeline screen) is one more entry in the
    # list. Its id is lower-case letters and underscores, so lane G can cite it without a digit.
    _, copy = _load()
    ids = [b["id"] for b in copy["beats"]]
    assert len(ids) == len(set(ids)), "beat ids must be unique"
    for beat_id in ids:
        assert re.fullmatch(r"[a-z]+(_[a-z]+)*", beat_id), f"beat id {beat_id!r} is not plain words"


def test_all_seven_screens_have_a_title_and_a_nav_label():
    _, copy = _load()
    assert list(copy["screens"]) == SCREEN_NAMES
    for name in SCREEN_NAMES:
        screen = copy["screens"][name]
        assert screen["title"].strip() and screen["nav"].strip(), name
        assert not re.match(r"\s*\d", screen["nav"]), f"{name}: the key digit comes from the router"


def test_node_captions_are_one_line_each_with_both_tenses():
    _, copy = _load()
    assert list(copy["nodes"]) == NODE_IDS
    for node_id, node in copy["nodes"].items():
        for tense in ("running", "done"):
            assert node[tense].strip(), f"{node_id}.{tense}"
            assert "\n" not in node[tense], f"{node_id}.{tense} must be one line"
        assert node["running"] != node["done"], f"{node_id}: present and past tense must differ"


def test_state_edge_and_pipeline_keys_that_lane_e1_reads():
    _, copy = _load()
    assert set(STATE_KEYS) <= set(copy["states"])
    assert copy["states"]["idle"] == "Waiting"
    assert copy["states"]["error"] == "Stopped"
    assert copy["states"]["skipped"] == "Skipped"
    assert set(EDGE_KEYS) <= set(copy["edges"])
    assert set(PIPELINE_KEYS) <= set(copy["pipeline"])
    assert copy["pipeline"]["status_done"] == "Done in {t}. Evidence screens updated: press R."
    assert "{cmd}" in copy["pipeline"]["status_export_missing"]
    assert "{t}" in copy["pipeline"]["status_export_missing"]


def test_the_pipeline_title_is_the_same_in_both_places():
    # app.js sets the h1 from screens.pipeline.title; the pipeline view may read pipeline.title.
    _, copy = _load()
    assert copy["pipeline"]["title"] == copy["screens"]["pipeline"]["title"]
    assert copy["models"]["title"] == copy["screens"]["models"]["title"]


def test_decision_card_and_models_blocks_exist():
    _, copy = _load()
    assert copy["decision"]["title"].strip()
    assert copy["models"]["title"].strip()
    assert copy["models"]["empty"].strip()
    assert copy["models"]["columns"], "the Models table needs column labels"
    assert copy["models"]["tools"]["no"] == "no — every run errored"
    assert copy["models"]["tools"]["unchecked"] == "not checked"


def test_claims_the_plan_corrected_stay_corrected():
    text, copy = _load()
    lowered = text.lower()
    for word in BANNED_WORDS:
        assert word not in lowered, f"copy.js uses banned wording: {word}"
    assert copy["decision"]["cost"] == "No API fees: runs on a local model"


# ---------------------------------------------------------------------------------------------
# The page shell (ui/index.html): script order, the seven screens, the skeletons other lanes fill.
# ---------------------------------------------------------------------------------------------
from html.parser import HTMLParser

INDEX_HTML = UI / "index.html"
APP_JS = UI / "app.js"
APP_CSS = UI / "app.css"
REPLAY_JS = UI / "data" / "replay.js"

SCRIPT_ORDER = ["data/demo.js", "data/replay.js", "copy.js", "pipeline-glyphs.js", "pipeline.js", "app.js"]
VOID_TAGS = {"meta", "link", "br", "img", "input", "hr"}


class _Page(HTMLParser):
    """Collects scripts in order and every element that has an id, with its parent id and prior sibling."""

    def __init__(self):
        super().__init__()
        self.scripts = []
        self.by_id = {}
        self.nav_items = []
        self._stack = []          # (tag, id or None)
        self._last_child = {}     # depth -> last child (tag, id) seen at that depth
        self.order = {}           # id -> previous sibling (tag, id) or None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and a.get("src"):
            self.scripts.append(a["src"])
        parent = self._stack[-1] if self._stack else (None, None)
        depth = len(self._stack)
        if a.get("id"):
            self.order[a["id"]] = self._last_child.get(depth)
            self.by_id[a["id"]] = {"tag": tag, "attrs": a, "parent": parent[1]}
        self._last_child[depth] = (tag, a.get("id"))
        if tag == "button" and "nav-item" in (a.get("class") or "").split():
            self.nav_items.append(a.get("data-screen"))
        if tag not in VOID_TAGS:
            self._stack.append((tag, a.get("id")))
            self._last_child[len(self._stack)] = None

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        while self._stack:
            top = self._stack.pop()
            if top[0] == tag:
                break


def _page():
    page = _Page()
    page.feed(INDEX_HTML.read_text(encoding="utf-8"))
    return page


def test_scripts_load_in_the_planned_order():
    assert _page().scripts == SCRIPT_ORDER


def test_nav_has_seven_items_keyed_one_to_seven():
    assert _page().nav_items == [str(n) for n in range(1, 8)]


def test_screen_six_has_the_skeleton_the_pipeline_view_expects():
    page = _page()
    ids = page.by_id
    assert ids["screen-6"]["tag"] == "section"
    assert ids["p-mode"]["parent"] == "screen-6"
    assert ids["p-rail"]["tag"] == "ol" and ids["p-rail"]["parent"] == "screen-6"
    assert ids["p-status"]["tag"] == "p" and ids["p-status"]["attrs"].get("role") == "status"
    assert ids["p-board"]["parent"] == "screen-6"
    assert ids["p-wires"]["tag"] == "svg" and ids["p-wires"]["parent"] == "p-board"
    assert ids["p-nodes"]["tag"] == "ol" and ids["p-nodes"]["parent"] == "p-board"
    assert page.order["p-nodes"] == ("svg", "p-wires"), "ol#p-nodes must follow svg#p-wires"
    for name in ("p-tiles", "p-src", "p-decision"):
        assert ids[name]["parent"] == "screen-6", name
    assert "hidden" in ids["p-decision"]["attrs"], "the decision card starts hidden"


def test_screen_seven_has_the_models_containers():
    ids = _page().by_id
    assert ids["screen-7"]["tag"] == "section"
    for name in ("m-table", "m-note", "m-src"):
        assert ids[name]["parent"] == "screen-7", name


def test_replay_placeholder_is_the_agreed_one_line():
    assert REPLAY_JS.read_text(encoding="utf-8") == "window.LIVE_REPLAY = null;\n"


def test_files_the_browser_loads_hold_no_network_or_storage_strings():
    banned = ["http", "fetch", "XMLHttpRequest", "localStorage", "sessionStorage", "WebSocket", "import("]
    for path in (INDEX_HTML, APP_JS, COPY_JS, APP_CSS, REPLAY_JS):
        text = path.read_text(encoding="utf-8").replace("http-equiv", "")
        for word in banned:
            assert word not in text, f"{path.name} holds {word!r}"
        assert 'type="module"' not in text and "type='module'" not in text, path.name
        assert not re.search(r"^\s*import\s", text, re.M), f"{path.name} uses an import"


def test_the_content_security_policy_is_kept():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "connect-src 'none'" in html and "font-src data:" in html
    assert "img-src data:" in html and "object-src 'none'" in html


def test_app_js_never_names_a_beat():
    # The beat list is data. A new beat is one more entry in copy.js and needs no change in app.js.
    # (Words such as close or tamper also appear in app.js for other reasons, so the check looks for a
    # beat id used as a key: compared with .id, in a case label, or as an index.)
    _, copy = _load()
    source = APP_JS.read_text(encoding="utf-8")
    for beat in copy["beats"]:
        keyed = re.compile(r"""(?:\bid\s*[!=]==?\s*|\bcase\s+|\[\s*)['"]%s['"]""" % re.escape(beat["id"]))
        assert not keyed.search(source), f"app.js keys its logic on the beat {beat['id']!r}"
