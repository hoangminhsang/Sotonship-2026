#!/usr/bin/env python3
"""Time a demo script beat by beat and fill its {{key}} numbers.

Usage:
    python scripts/fill_script.py TEMPLATE [--numbers ui/data/numbers.json]
                                  [--wpm 150] [--out FILE]

The demo must fit in 3 minutes and the numbers change with every run. Write
the script once with placeholders. This tool then shows the time per beat and
fills the numbers in about a second, with no recording. Stdlib only.

TEMPLATE FORMAT (Markdown)

    # Redline demo script            <- no slot: ignored, like all notes
    Keys, notes and tables go here.

    ## Problem [20s]                 <- a beat: its heading ends with the slot
    > Screen 2: the trace drawer.    <- stage direction: not spoken, not counted
    AI agents do not only chat. They act. Attack success is {{rc001.asr}}.

    ## Notes                         <- no slot: ends the beat, text is ignored

  1. A beat is a heading (# to ######) that ENDS with its time slot in seconds,
     in square brackets: "## Problem [20s]" or "## Close [7.5s]".
  2. A beat runs from its heading to the next heading of any kind, or to the
     end of the file. A heading without a slot ends the beat and starts none.
  3. A line that starts with ">" is a stage direction. It is not spoken and
     not counted. Start every line of a direction with ">".
  4. Text outside the beats is ignored: put the title, the keys contract,
     notes and tables there. Fenced code blocks (``` or ~~~) are skipped
     completely, so a template can quote examples or a pasted timing table.
  5. {{key}} (spaces inside the braces are allowed) is replaced with the value
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

  A null, blank or value-less entry counts as missing. A numbers file that
  does not exist yet is not an error: every key is reported missing and the
  timing still prints.

OUTPUT (stdout)

  One row per beat (words, seconds, slot, OVER), the totals, the beats over
  their slot, then the missing keys with the beats that use them. With --out
  the whole template is written with the numbers filled (UTF-8, LF lines);
  the tool refuses to overwrite the template itself.

EXIT CODES
    0  every beat is within its slot and no key is missing
    1  a beat is over its slot, or a key is missing
    2  usage or file error (no template, no beats, bad numbers file, ...)
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
DEFAULT_WPM = 150.0

# "## Problem [20s]": a heading whose text ends with the slot in seconds.
_BEAT = re.compile(
    r"^ {0,3}#{1,6}[ \t]+(?P<title>.*?)[ \t]*"
    r"\[[ \t]*(?P<slot>\d+(?:\.\d+)?)[ \t]*s[ \t]*\][ \t]*#*[ \t]*$"
)
_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")
_DASHES = re.compile("[\u2012-\u2015]")  # figure, en, em dash and horizontal bar
_ALNUM = re.compile(r"[^\W_]")


class NumbersError(Exception):
    """The numbers file exists but cannot be used."""


class Beat:
    """One timed section of the script."""

    def __init__(self, title: str, slot: float) -> None:
        self.title = title
        self.slot = slot
        self.spoken = []  # filled lines that are spoken (stage directions excluded)


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
    """Return (numbers, found). numbers maps key -> text; found is False if no file."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}, False
    except (OSError, UnicodeDecodeError) as exc:
        raise NumbersError(f"{path}: cannot read the numbers file ({exc})")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise NumbersError(f"{path}: not valid JSON ({exc})")
    if not isinstance(data, dict):
        raise NumbersError(f"{path}: the top level must be a JSON object of key -> value")
    numbers = {}
    for key, entry in data.items():
        if isinstance(entry, dict):
            entry = entry.get("value")
        text = _as_text(entry)
        if text is not None:
            numbers[str(key)] = text
    return numbers, True


def analyse(text: str, numbers: dict):
    """Walk the template once.

    Returns (filled_text, beats, missing, open_fence_line). missing maps each
    unfilled key to the titles of the beats that use it, in script order.
    open_fence_line is the line number of a code fence that never closes, or 0.
    """
    out, beats, missing = [], [], {}
    beat = None
    fence, fence_line = "", 0

    def swap(match):
        key = match.group(1)
        value = numbers.get(key)
        if value is None:
            users = missing.setdefault(key, [])
            if beat.title not in users:
                users.append(beat.title)
            return match.group(0)
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
            title = found.group("title") or f"Beat {len(beats) + 1}"
            beat = Beat(title, float(found.group("slot")))
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
    return "\n".join(out), beats, missing, (fence_line if fence else 0)


def _clock(seconds: float) -> str:
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes}:{rest:02d}"


def build_report(beats, missing, wpm, template, numbers_path, found, count):
    """Return (lines, over_titles) for the stdout report."""
    stats = []
    for beat in beats:
        words = count_words("\n".join(beat.spoken))
        secs = words * 60.0 / wpm
        stats.append((beat, words, secs, round(secs, 1) > round(beat.slot, 1)))

    width = max([len("Beat"), len("Total")] + [len(beat.title) for beat in beats])
    total_words = sum(words for _, words, _, _ in stats)
    total_secs = sum(secs for _, _, secs, _ in stats)
    total_slot = sum(beat.slot for beat in beats)

    if found:
        numbers_note = f"{count} key{'' if count == 1 else 's'}"
    else:
        numbers_note = "not found: every key is reported missing"
    lines = [
        f"fill_script: {template} at {wpm:g} wpm",
        f"numbers: {numbers_path} ({numbers_note})",
        "",
        f"{'Beat':<{width}}  {'Words':>5}  {'Secs':>6}  {'Slot':>6}",
    ]
    for beat, words, secs, over in stats:
        row = f"{beat.title:<{width}}  {words:>5}  {secs:>6.1f}  {beat.slot:>5g}s"
        lines.append(row + ("  OVER" if over else ""))
    lines.append(f"{'Total':<{width}}  {total_words:>5}  {total_secs:>6.1f}  {total_slot:>5g}s")
    lines.append("")
    lines.append(f"Spoken time {_clock(total_secs)}; the slots add up to {_clock(total_slot)}.")

    over_titles = [beat.title for beat, _, _, over in stats if over]
    if over_titles:
        lines.append(f"Beats over slot: {len(over_titles)} ({', '.join(over_titles)})")
    else:
        lines.append("Beats over slot: none")
    if missing:
        lines.append(f"Missing keys: {len(missing)} (left unfilled in the script)")
        for key, users in missing.items():
            lines.append(f"  {{{{{key}}}}}  in {', '.join(users)}")
    else:
        lines.append("Missing keys: none")
    return lines, over_titles


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
        epilog="Exit codes: 0 clean; 1 a beat is over its slot or a key is missing; "
        "2 usage or file error.",
    )
    parser.add_argument("template", metavar="TEMPLATE",
                        help='Markdown script; a beat is a heading that ends with its slot, '
                             'for example "## Problem [20s]"')
    parser.add_argument("--numbers", metavar="FILE", default=None,
                        help="numbers.json to fill {{key}} from (default: ui/data/numbers.json "
                             "in the repo; a missing file means every key is missing)")
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
        numbers, found = load_numbers(numbers_path)
    except NumbersError as exc:
        return _fail(str(exc))

    filled, beats, missing, open_fence = analyse(text, numbers)
    if open_fence:
        return _fail(f"{template}: the code fence opened on line {open_fence} never closes")
    if not beats:
        return _fail(f'{template}: no beats found; a beat is a heading that ends with its '
                     f'slot in seconds, for example "## Problem [20s]"')

    lines, over_titles = build_report(beats, missing, args.wpm, template, numbers_path,
                                      found, len(numbers))
    if out_path is not None:
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(filled)
        except OSError as exc:
            return _fail(f"{out_path}: cannot write the output file ({exc})")
        lines.append(f"Wrote the filled script to {out_path}")
    print("\n".join(lines))
    return 1 if (over_titles or missing) else 0


if __name__ == "__main__":
    sys.exit(main())
