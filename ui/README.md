# Redline dashboard (`ui/`)

A read-only, offline view of the evidence that the release gate produced. It has seven screens. The page never starts a command and never computes a number: every number is written by a script from a backend file.

Commands below are for macOS (`python3`) and Windows PowerShell (`py`). Run them from the repo folder. The scripts use only the Python standard library and need Python 3.9 or newer. On Windows you can also use the repo's own interpreter, `.venv\Scripts\python.exe`.

Files that matter:

| File | What it is |
|---|---|
| `index.html`, `app.css`, `app.js` | the page and the router |
| `copy.js` | every word on the page, and the beat list (no digit allowed) |
| `pipeline.js`, `pipeline-glyphs.js` | screen 6, the pipeline view, and its 15 line-drawn glyphs |
| `fonts.css` | the embedded fonts. Do not open it in an editor: it is about 146 KB of base64 |
| `export.py`, `sources.json` | read the backend files and write `data/demo.js` and `data/numbers.json` |
| `bridge.py` | follows a pipeline run and writes `data/live.js`; can also serve the page |
| `data/` | `demo.js` (the evidence), `live.js` (the live run), `replay.js` (a baked replay) |
| `docs/BUILD_CONTRACT.md` | the rules and the schemas. `../docs/DATA_CONTRACT.md` lists the file formats |

## 1. Open the dashboard

**From a file (no server, Wi-Fi can be off).**

```
macOS:     open ui/index.html
Windows:   Start-Process ui\index.html
```

Or double-click `ui/index.html`. Everything the page needs is in the folder. If the page says "No data yet", run the export (section 3).

**With the bridge (screen 6 follows a run as it happens).** The bridge serves `ui/` on this computer only (`127.0.0.1`, port 8765).

```
macOS:     python3 ui/bridge.py --serve
Windows:   py ui\bridge.py --serve
```

Then open `http://127.0.0.1:8765/`. To open a screen directly, add `#s=6`, for example `http://127.0.0.1:8765/#s=6`. `Ctrl-C` stops the bridge. Use `--port N` if 8765 is taken.

Add one more mode to the same command to feed screen 6:

| Goal | macOS | Windows |
|---|---|---|
| Scripted run, shown paced for display | `python3 ui/bridge.py --run demo --serve` | `py ui\bridge.py --run demo --serve` |
| Short real-model run (12 runs, about 2 to 3 minutes) | `python3 ui/bridge.py --run live-short --model qwen2.5:7b-instruct --serve` | `py ui\bridge.py --run live-short --model qwen2.5:7b-instruct --serve` |
| Several models in one batch | `python3 ui/bridge.py --run models --parallel 2 --serve` | `py ui\bridge.py --run models --parallel 2 --serve` |
| Follow the newest run that is still being written | `python3 ui/bridge.py --follow-latest --serve` | `py ui\bridge.py --follow-latest --serve` |
| Play back a finished log | `python3 ui/bridge.py --replay results/governance/<run>/progress.jsonl --speed 4 --loop --serve` | `py ui\bridge.py --replay results\governance\<run>\progress.jsonl --speed 4 --loop --serve` |
| Bake a log into `data/replay.js` for `file://` | `python3 ui/bridge.py --bake results/governance/<run>/progress.jsonl` | `py ui\bridge.py --bake results\governance\<run>\progress.jsonl` |

Notes:

- `--run` accepts `demo`, `live-short`, `live-smoke` and `models`. The bridge starts only these fixed commands, without a shell. A real-model run first runs `scripts/preflight.py` and refuses to start if it fails. `--model` must be a tag in `config/models.txt`.
- When a `--run` ends, the bridge runs `ui/export.py --quiet --gov <run folder>` so the evidence screens update. Add `--no-export` to skip it. Then press **R** in the page.
- The bridge finds the repo's `.venv` by itself. Use `--python PATH` to name another interpreter.
- Stopping a run with `Ctrl-C` on Windows uses `CTRL_BREAK_EVENT`. That is not verified on Windows.
- `--bake` makes the replay that screen 6 plays when no bridge runs (opened from a file). It keeps at most 120 frames.

## 2. Keys

| Key | What it does |
|---|---|
| `1` to `7` | go to a screen: 1 Console, 2 Attacks, 3 Compare, 4 Decisions, 5 Assurance, 6 Pipeline, 7 Models |
| any key on the cover | starts the show. The cover shows when the address names no screen |
| `R` | reload the page. Use it after an export, because the page reads `data/demo.js` once |
| `P` | presenter mode: hides the key hints, the informational warnings and the idle cursor. The status chips, the REPLAY label and data-integrity warnings stay |
| `B` / `Shift`+`B` | next / previous beat of the demo script (the beats in `copy.js`) |
| arrow keys, `Enter`, `Esc` (screen 2) | move between tiles, open the run in the drawer, close the drawer |
| `Space` (screen 6) | play or pause the recorded run |
| `Home` (screen 6) | start the recorded run again |
| `[` and `]` (screen 6) | previous or next recorded run (a model); with one recorded run, one frame back or forward |
| `D` (screen 6) | show or hide the release-decision card |

On screen 6 the keys `Space`, `Home`, `[` and `]` do nothing while a run from the bridge is still going, and while the release-decision card is open. The address (`#s=6&p=1` and so on) keeps the screen and the presenter mode, so a reload returns to the same place.

## 3. Export the evidence

The export reads the backend files, formats every number, and writes `ui/data/demo.js` and `ui/data/numbers.json`. It never runs backend code.

```
macOS:     python3 ui/export.py
Windows:   py ui\export.py
```

| Option | Meaning |
|---|---|
| (none) | export what is on disk. Data that is missing or not connected shows as PENDING with its reason |
| `--gov DIR` | show this governance output folder, for example `results/governance/<run>`. The folder must be inside the repo. It wins over `governance.pick` in `sources.json` |
| `--allow-sample` | fill unconnected datasets from `ui/fixtures` and label them SAMPLE. Use it for rehearsals on your own machine. Never commit the result |
| `--final` | exit with code 1 and keep the old `demo.js` if any SAMPLE data is present. Use it for the last export |
| `--check` | read every source again and compare it with `demo.js`; write nothing |
| `--quiet` | print one summary line |
| `--no-git`, `--sources PATH`, `--out PATH` | read working-tree files only; use another `sources.json`; write somewhere else |

`--gov` and `ui/data/numbers.json` come from lane D, which is being merged. Until then `export.py` has no `--gov` and writes only `demo.js`.

Every value on the page has one of three status words, and the word is always visible:

| Word | Meaning |
|---|---|
| REAL | read from a backend file; the source is in the provenance list |
| SAMPLE | hand-written stand-in from `ui/fixtures`, or scripted output. It is never produced by a model |
| PENDING | not available. The reason is shown |

The mode words are below. The header shows the REPLAY label on screens 1 to 3, and screen 6 has its own mode chip. **SCRIPTED** is a DEMO run: the traces come from `lab/demo.py`, not from a model. **REPLAY** is a recorded run. **SAVED** is a saved run folder that the CLI re-scored. **LIVE** appears only while a real-model run is still growing. Reviewer decisions in CLI runs are scripted, including LIVE runs, and the page says so.

A committed `ui/data/demo.js` is always exported without `--allow-sample`. Before a commit, restore the two bridge files: `git checkout -- ui/data/live.js ui/data/replay.js`.

## 4. Fill and time the demo script

`scripts/fill_script.py` times the script beat by beat and fills its numbers. Write the script once as a Markdown template with placeholders, for example `{{rc001.asr}}`. Each beat is a heading that ends with its time slot:

```
## baseline: First candidate [20s]
> Screen 6: the tile board fills, then the gate says FAIL.
Redline fingerprints the agent, then runs {{runs.attack}} attack runs.
```

The id before the colon is a beat id of `ui/copy.js`. A line that starts with `>` is a stage direction: it is not spoken and not counted.

```
macOS:     python3 scripts/fill_script.py <template>
           python3 scripts/fill_script.py <template> --numbers ui/data/numbers.json --out filled.md
           python3 scripts/fill_script.py <template> --wpm 140
Windows:   py scripts\fill_script.py <template>
           py scripts\fill_script.py <template> --numbers ui\data\numbers.json --out filled.md
```

It prints the words and seconds for each beat against its slot, the beats that are over, ids that `copy.js` does not know, and the numbers that are missing. The default pace is 150 words per minute. A filled number counts as one word, so leave a margin in beats full of numbers. Exit code 0 means every beat fits, no key is missing and every id is known. Exit code 1 means one of those failed. Exit code 2 means a usage or file error. Run it again after every export, because the numbers change with each run.

## 5. Fonts and credits

`fonts.css` embeds Cormorant Garamond (300, 400, 300 italic) and Inter (400, 500) under the SIL Open Font License 1.1. The 17 Oct plan credits Cormorant to Christian Thalmann (Catharsis Fonts) and Inter to Rasmus Andersson. Inter lists a Reserved Font Name and these are embedded subsets, so check that point before you publish.

The pipeline screen's line-drawing style and stroke draw-on technique are adapted from the project lead's own website component (setu-site PipelineFigure). The 15 glyphs were drawn new for this project on 18 Oct 2026.

## 6. When something looks wrong

| You see | Do this |
|---|---|
| "No data yet" | run the export (section 3), then press `R` |
| Screen 6 says "Waiting" | no bridge is feeding it. Start one (section 1), or bake a replay and press `Space` |
| An old run on screens 1 to 5 | export again with `--gov <run folder>`, then press `R` |
| `export failed ... Previous demo.js kept` | the export found a fatal problem. Read the line it printed. The old file is still in place |
| The port is busy | add `--port 8766` to the bridge command |
| Numbers differ from the script | export again, then run `fill_script.py` with `--numbers ui/data/numbers.json` |
