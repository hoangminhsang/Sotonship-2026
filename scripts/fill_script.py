#!/usr/bin/env python3
"""Time a demo script beat by beat and fill its {{key}} numbers.

Usage:
    python scripts/fill_script.py TEMPLATE [--numbers ui/data/numbers.json]
                                  [--ids LIST|FILE] [--wpm 150] [--out FILE]

The demo must fit in 3 minutes and the numbers change with every run. Write
the script once with placeholders. This tool then shows the time per beat and
fills the numbers in about a second, with no recording. It also checks that
every beat id exists in ui/copy.js, so the script and the screens stay aligned.
Stdlib only.

TEMPLATE FORMAT (Markdown)

    # Redline demo script            <- no slot: ignored, like all notes
    Keys, notes and tables go here.

    ## problem: Problem [20s]        <- a beat: id, title and slot
    > Screen 2: the trace drawer.    <- stage direction: not spoken, not counted
    AI agents do not only chat. They act. Attack success is {{rc001.asr}}.

    ## Notes                         <- no slot: ends the beat, text is ignored

  1. A beat is a heading (# to ######) that ENDS with its time slot in seconds,
     in square brackets: "## problem: Problem [20s]" or "## Close [7.5s]".
  2. The heading may start with an id: lowercase letters, digits and
     underscores, then a colon ("problem:", "why_now:", "rc001:"). The id is
     the key of the beat in ui/copy.js. The beats may come in any order. A
     title that only contains a colon ("Why now: the gap") has no id.
  3. A beat runs from its heading to the next heading of any kind, or to the
     end of the file. A heading without a slot ends the beat and starts none.
  4. A line that starts with ">" is a stage direction. It is not spoken and
     not counted. Start every line of a direction with ">".
  5. Text outside the beats is ignored: put the title, the keys contract,
     notes and tables there. Fenced code blocks (``` or ~~~) are skipped
     completely, so a template can quote examples or a pasted timing table.
  6. {{key}} (spaces inside the braces are allowed) is replaced with the value
     from the numbers file, in the spoken lines and stage directions of the
     beats. A key that is not in the numbers file is left as {{key}} and
     listed as missing. A filled value is never expanded again.

WORDS AND TIME

  Words are counted after filling. A word is a run of characters between
  spaces that holds at least one letter or digit; an em dash or en dash
  separates two words; a lone dash or bullet is not a word. Seconds are
  words * 60 / wpm. A beat is OVER when its seconds are more than its slot.
  A filled number counts as one word although it takes longer to say ("42.3%"
  is about five spoken words), so leave a margin in beats full of numbers.

NUMBERS FILE (JSON object; default ui/data/numbers.json)

    {"rc001.asr": "42.3%"}                                  flat key -> value
    {"rc001.asr": {"value": "42.3%", "source": "x.csv"}}    value plus source

  Key names are lowercase and dotted (rc001.asr). A null, blank or value-less
  entry counts as missing. A numbers file that does not exist yet is not an
  error: every key is reported missing and the timing still prints.

BEAT IDS

  The id list is the beat list of ui/copy.js. Built in, in order: problem,
  why_now, pivot, rc001, duties, tamper, safeguard, limits, business, close.
  --ids sets another list: ids separated by commas, a JSON file (a list of ids
  or of {"id": ...} objects, an object keyed by id, or {"beats": ...}), or a
  .js file with a beats list (copy.js). Without --ids the tool reads
  ui/copy.js when it exists and has a beats list, else it uses the built-in
  list. A beat whose id is not in the list is flagged NOT IN ID LIST; a
  repeated id is flagged DUPLICATE ID. Ids of the list that the script does not
  use are printed, and that is not an error. A beat with no id is allowed and
  is not checked.

OUTPUT (stdout)

  One row per beat (id, title, words, seconds, slot, flags), the totals, the
  beats over their slot, the id checks, the missing keys with the beats that
  use them, then the numbers filled (key = value, with its source if the
  numbers file gives one). With --out the whole template is written with the
  numbers filled (UTF-8, LF lines); the tool refuses to overwrite the
  template itself.

EXIT CODES
    0  every beat is within its slot, no key is missing, every id is known
    1  a beat is over its slot, a key is missing, or an id is unknown or repeated
    2  usage or file error (no template, no beats, bad numbers or ids file, ...)
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_NUMBERS = REPO / "ui" / "data" / "numbers.json"
DEFAULT_COPY = REPO / "ui" / "copy.js"
DEFAULT_WPM = 150.0

# The beats of ui/copy.js, in its order (agreed with lane F, 18 Oct 2026).
BUILTIN_IDS = ("problem", "why_now", "pivot", "rc001", "duties",
               "tamper", "safeguard", "limits", "business", "close")

# "## problem: Problem [20s]": a heading whose text ends with the slot in seconds.
_BEAT = re.compile(
    r"^ {0,3}#{1,6}[ \t]+(?P<title>.*?)[ \t]*"
    r"\[[ \t]*(?P<slot>\d+(?:\.\d+)?)[ \t]*s[ \t]*\][ \t]*#*[ \t]*$"
)
_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")
_DASHES = re.compile("[" + chr(0x2012) + "-" + chr(0x2015) + "]")  # figure, en, em dash, bar
_ALNUM = re.compile(r"[^\W_]")
_ID_WORD = re.compile(r"[a-z][a-z0-9_]*")
_HEAD_ID = re.compile(r"^(?P<id>[a-z][a-z0-9_]*)[ \t]*:[ \t]*(?P<title>.*)$")
_JS_TOKEN = re.compile(
    r"""
    (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<str>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)
  | (?P<word>[A-Za-z_$][\w$]*)
  | (?P<punct>[\[\]{}:,])
    """,
    re.VERBOSE | re.DOTALL,
)


class NumbersError(Exception):
    """The numbers file exists but cannot be used."""


class IdsError(Exception):
    """The id list cannot be read or is not valid."""


class Beat:
    """One timed section of the script."""

    def __init__(self, title: str, slot: float, bid: str = "") -> None:
        self.title = title
        self.slot = slot
        self.id = bid
        self.spoken = []  # filled lines that are spoken (stage directions excluded)

    @property
    def label(self) -> str:
        """How the report names the beat: its id, else its title."""
        return self.id or self.title


def count_words(text: str) -> int:
    """Count spoken words: tokens that hold a letter or digit; dashes split words."""
    tokens = _DASHES.sub(" ", text).split()
    return sum(1 for token in tokens if _ALNUM.search(token))


def _as_text(value):
    """One line of text for a JSON value, or None when it cannot fill a slot."""
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return " ".join(str(value).split()) or None


def load_numbers(path: Path):
    """Return (numbers, sources, found).

    numbers maps key -> text, sources maps key -> its source text (if given),
    and found is False when the file does not exist.
    """
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}, {}, False
    except (OSError, UnicodeDecodeError) as exc:
        raise NumbersError(f"{path}: cannot read the numbers file ({exc})")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise NumbersError(f"{path}: not valid JSON ({exc})")
    if not isinstance(data, dict):
        raise NumbersError(f"{path}: the top level must be a JSON object of key -> value")
    numbers, sources = {}, {}
    for key, entry in data.items():
        source = None
        if isinstance(entry, dict):
            source = _as_text(entry.get("source"))
            entry = entry.get("value")
        text = _as_text(entry)
        if text is not None:
            numbers[str(key)] = text
            if source:
                sources[str(key)] = source
    return numbers, sources, True


# ---------------------------------------------------------------- beat ids


def _ids_from_json(data):
    """Ids from the shapes a beat list can take in JSON."""
    if isinstance(data, dict):
        for key in ("beats", "ids"):
            if key in data:
                return _ids_from_json(data[key])
        return [key for key in data if isinstance(key, str)]
    ids = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                ids.append(item)
            elif isinstance(item, dict) and isinstance(item.get("id"), str):
                ids.append(item["id"])
    return ids


def _walk_beats(tokens, start):
    """Collect the ids of the beats container that opens at tokens[start]."""
    stack = [tokens[start][1]]
    ids = []
    at = start + 1
    while at < len(tokens) and stack:
        kind, value = tokens[at]
        if kind == "punct":
            if value in "[{":
                stack.append(value)
            elif value in "]}":
                stack.pop()
        else:
            text = value[1:-1] if kind == "str" else value
            is_key = at + 1 < len(tokens) and tokens[at + 1] == ("punct", ":")
            if stack == ["["] and kind == "str" and not is_key:
                ids.append(text)  # beats: ["problem", "why_now"]
            elif stack == ["[", "{"] and is_key and text == "id":
                if at + 2 < len(tokens) and tokens[at + 2][0] == "str":
                    ids.append(tokens[at + 2][1][1:-1])  # beats: [{id: "problem"}]
            elif stack == ["{"] and is_key:
                ids.append(text)  # beats: {problem: {...}}
        at += 1
    return [text for text in ids if _ID_WORD.fullmatch(text)]


def ids_from_js(text: str):
    """Best effort: the ids of the first `beats` list or object in a JS file."""
    tokens = [(m.lastgroup, m.group()) for m in _JS_TOKEN.finditer(text)
              if m.lastgroup != "comment"]
    for at, (kind, value) in enumerate(tokens):
        name = value[1:-1] if kind == "str" else value
        if kind not in ("word", "str") or name != "beats":
            continue
        nxt = at + 1
        if nxt < len(tokens) and tokens[nxt] == ("punct", ":"):
            nxt += 1
        if nxt < len(tokens) and tokens[nxt][0] == "punct" and tokens[nxt][1] in "[{":
            ids = _walk_beats(tokens, nxt)
            if ids:
                return list(dict.fromkeys(ids))
    return []


def ids_from_file(path: Path):
    """Ids from a JSON file or a JS file (may be empty for a JS file with no list)."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise IdsError(f"{path}: cannot read the ids file ({exc})")
    if path.suffix.lower() != ".json":
        return ids_from_js(text)
    try:
        ids = _ids_from_json(json.loads(text))
    except ValueError as exc:
        raise IdsError(f"{path}: not valid JSON ({exc})")
    bad = [item for item in ids if not _ID_WORD.fullmatch(item)]
    if bad:
        raise IdsError(f"{path}: not a valid beat id: {', '.join(bad)} "
                       "(use lowercase letters, digits and underscores)")
    return list(dict.fromkeys(ids))


def load_ids(spec: str):
    """Read --ids. Returns (ids, is_file)."""
    path = Path(spec) if spec else None
    if path is not None and path.is_file():
        ids = ids_from_file(path)
        if not ids:
            raise IdsError(f"{spec}: no beat ids found (looked for a 'beats' list)")
        return ids, True
    ids = [part.strip() for part in spec.split(",") if part.strip()]
    bad = [item for item in ids if not _ID_WORD.fullmatch(item)]
    if not ids or bad:
        why = f"not a valid beat id: {', '.join(bad)}" if bad else "no ids given"
        raise IdsError(f"--ids {spec!r}: {why}; give comma-separated ids (lowercase "
                       "letters, digits, underscores) or a JSON or .js file")
    return list(dict.fromkeys(ids)), False


def _shown(path: Path) -> str:
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return str(path)


def resolve_ids(spec):
    """Return (ids, label, note) for the beat-id check."""
    if spec is not None:
        ids, is_file = load_ids(spec)
        return ids, (spec if is_file else "--ids"), ""
    shown = _shown(DEFAULT_COPY)
    if DEFAULT_COPY.is_file():
        try:
            ids = ids_from_file(DEFAULT_COPY)
        except IdsError:
            ids = []
        if ids:
            return ids, shown, ""
        return list(BUILTIN_IDS), "built-in list", f"{shown} has no readable beats list"
    return list(BUILTIN_IDS), "built-in list", f"{shown} not found"


# ------------------------------------------------------------ the template


def analyse(text: str, numbers: dict):
    """Walk the template once.

    Returns (filled_text, beats, missing, used, open_fence_line). missing maps
    each unfilled key to the labels of the beats that use it, in script order.
    used maps each filled key to its text, in script order. open_fence_line is
    the line number of a code fence that never closes, or 0.
    """
    out, beats, missing, used = [], [], {}, {}
    beat = None
    fence, fence_line = "", 0

    def swap(match):
        key = match.group(1)
        value = numbers.get(key)
        if value is None:
            users = missing.setdefault(key, [])
            if beat.label not in users:
                users.append(beat.label)
            return match.group(0)
        used.setdefault(key, value)
        return value

    for number, line in enumerate(text.split("\n"), start=1):
        if fence:  # inside a fenced block: copy as is
            out.append(line)
            closer = line.strip()
            if closer and set(closer) == {fence[0]} and len(closer) >= len(fence):
                fence = ""
            continue
        opener = _FENCE.match(line)
        if opener:
            fence, fence_line = opener.group(1), number
            out.append(line)
            continue
        found = _BEAT.match(line)
        if found:
            heading = found.group("title")
            tagged = _HEAD_ID.match(heading)
            bid, title = (tagged.group("id"), tagged.group("title")) if tagged else ("", heading)
            beat = Beat(title or bid or f"Beat {len(beats) + 1}", float(found.group("slot")), bid)
            beats.append(beat)
            out.append(line)
        elif _HEADING.match(line):
            beat = None
            out.append(line)
        elif beat is None:
            out.append(line)
        else:
            filled = _PLACEHOLDER.sub(swap, line)
            out.append(filled)
            if not line.lstrip().startswith(">"):
                beat.spoken.append(filled)
    return "\n".join(out), beats, missing, used, (fence_line if fence else 0)


def _clock(seconds: float) -> str:
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes}:{rest:02d}"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def build_report(beats, missing, used, sources, wpm, template, numbers_path, found, count, ids):
    """Return (lines, failed) for the stdout report. ids is (list, label, note)."""
    id_list, id_label, id_note = ids
    id_set = set(id_list)
    in_script = {}
    for beat in beats:
        if beat.id:
            in_script[beat.id] = in_script.get(beat.id, 0) + 1

    stats = []
    for beat in beats:
        words = count_words("\n".join(beat.spoken))
        secs = words * 60.0 / wpm
        flags = []
        if round(secs, 1) > round(beat.slot, 1):
            flags.append("OVER")
        if beat.id and beat.id not in id_set:
            flags.append("NOT IN ID LIST")
        if beat.id and in_script[beat.id] > 1:
            flags.append("DUPLICATE ID")
        stats.append((beat, words, secs, flags))

    id_width = max([len("Id")] + [len(beat.id or "-") for beat in beats])
    width = max([len("Beat"), len("Total")] + [len(beat.title) for beat in beats])
    total_words = sum(words for _, words, _, _ in stats)
    total_secs = sum(secs for _, _, secs, _ in stats)
    total_slot = sum(beat.slot for beat in beats)

    if found:
        numbers_note = _plural(count, "key")
    else:
        numbers_note = "not found: every key is reported missing"
    ids_note = _plural(len(id_list), "id") + (f"; {id_note}" if id_note else "")
    lines = [
        f"fill_script: {template} at {wpm:g} wpm",
        f"numbers: {numbers_path} ({numbers_note})",
        f"beat ids: {id_label} ({ids_note})",
        "",
        f"{'Id':<{id_width}}  {'Beat':<{width}}  {'Words':>5}  {'Secs':>6}  {'Slot':>6}",
    ]
    for beat, words, secs, flags in stats:
        row = (f"{beat.id or '-':<{id_width}}  {beat.title:<{width}}  {words:>5}  "
               f"{secs:>6.1f}  {beat.slot:>5g}s")
        lines.append(row + ("  " + "  ".join(flags) if flags else ""))
    lines.append(f"{'':<{id_width}}  {'Total':<{width}}  {total_words:>5}  "
                 f"{total_secs:>6.1f}  {total_slot:>5g}s")
    lines.append("")
    lines.append(f"Spoken time {_clock(total_secs)}; the slots add up to {_clock(total_slot)}.")

    over = [beat.label for beat, _, _, flags in stats if "OVER" in flags]
    lines.append(f"Beats over slot: {len(over)} ({', '.join(over)})" if over
                 else "Beats over slot: none")

    unknown = list(dict.fromkeys(beat.id for beat in beats if beat.id and beat.id not in id_set))
    repeated = [bid for bid, times in in_script.items() if times > 1]
    if in_script:
        lines.append(f"Beats not in the id list: {len(unknown)} ({', '.join(unknown)})" if unknown
                     else "Beats not in the id list: none")
        if repeated:
            lines.append(f"Duplicate beat ids: {len(repeated)} ({', '.join(repeated)})")
        unused = [bid for bid in id_list if bid not in in_script]
        lines.append("Ids in the list but not in the script: " + (", ".join(unused) or "none"))
        no_id = [beat.title for beat in beats if not beat.id]
        if no_id:
            lines.append(f"Beats without an id: {len(no_id)} ({', '.join(no_id)})")
    else:
        lines.append("Beat ids: no beat declares an id, so none were checked against the id list.")

    if missing:
        lines.append(f"Missing keys: {len(missing)} (left unfilled in the script)")
        for key, users in missing.items():
            lines.append(f"  {{{{{key}}}}}  in {', '.join(users)}")
    else:
        lines.append("Missing keys: none")

    if used:
        lines.append(f"Numbers filled: {len(used)}")
        for key, value in used.items():
            source = sources.get(key)
            lines.append(f"  {key} = {value}" + (f"  ({source})" if source else ""))
    return lines, bool(over or missing or unknown or repeated)


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number")
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a number above 0")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fill_script.py",
        description="Time a demo script beat by beat and fill its {{key}} numbers.",
        epilog="Exit codes: 0 clean; 1 a beat is over its slot, a key is missing, or an id is "
        "unknown or repeated; 2 usage or file error.",
    )
    parser.add_argument("template", metavar="TEMPLATE",
                        help='Markdown script; a beat is a heading that ends with its slot, '
                             'for example "## problem: Problem [20s]"')
    parser.add_argument("--numbers", metavar="FILE", default=None,
                        help="numbers.json to fill {{key}} from (default: ui/data/numbers.json "
                             "in the repo; a missing file means every key is missing)")
    parser.add_argument("--ids", metavar="LIST|FILE", default=None,
                        help="beat ids to check against: comma-separated ids, a JSON file or a "
                             ".js file with a beats list (default: ui/copy.js if it has a beats "
                             "list, else the built-in list)")
    parser.add_argument("--wpm", type=_positive_float, default=DEFAULT_WPM, metavar="N",
                        help="speaking speed in words per minute (default: 150)")
    parser.add_argument("--out", metavar="FILE", default=None,
                        help="write the template with the numbers filled to FILE")
    return parser


def _fail(message: str) -> int:
    print(f"fill_script: {message}", file=sys.stderr)
    return 2


def _tolerate_console_encoding() -> None:
    """Never crash on a character the console cannot print (Windows cp1252)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    _tolerate_console_encoding()

    template = Path(args.template)
    numbers_path = Path(args.numbers) if args.numbers is not None else DEFAULT_NUMBERS
    out_path = Path(args.out) if args.out else None
    if out_path is not None and out_path.resolve() == template.resolve():
        return _fail(f"refusing to overwrite the template ({template}); choose another --out")

    try:
        text = template.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return _fail(f"{template}: no such file")
    except (OSError, UnicodeDecodeError) as exc:
        return _fail(f"{template}: cannot read the template, it must be UTF-8 text ({exc})")
    try:
        numbers, sources, found = load_numbers(numbers_path)
        ids = resolve_ids(args.ids)
    except (NumbersError, IdsError) as exc:
        return _fail(str(exc))

    filled, beats, missing, used, open_fence = analyse(text, numbers)
    if open_fence:
        return _fail(f"{template}: the code fence opened on line {open_fence} never closes")
    if not beats:
        return _fail(f'{template}: no beats found; a beat is a heading that ends with its '
                     f'slot in seconds, for example "## problem: Problem [20s]"')

    lines, failed = build_report(beats, missing, used, sources, args.wpm, template,
                                 numbers_path, found, len(numbers), ids)
    if out_path is not None:
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(filled)
        except OSError as exc:
            return _fail(f"{out_path}: cannot write the output file ({exc})")
        lines.append(f"Wrote the filled script to {out_path}")
    print("\n".join(lines))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
