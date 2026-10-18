# Redline dashboard (`ui/`)

Two pages show the evidence that the release gate produced.

- **`index.html` is the demo page.** It has a cover ("Redline", "Press any key to start") and three views: **Live run**, **Results** and **Proof**.
- **`details.html` is the old seven-screen page** (Console, Attacks, Compare, Decisions, Assurance, Pipeline, Models). It stays for detail work.

Neither page computes a number. Every number is written by a script from a backend file and read from `window.DEMO` (`ui/data/demo.js`, made by `export.py`) or from `window.LIVE` (`ui/data/live.js`, made by `bridge.py`, schema `ui-live/1`).

Commands below are for macOS (`python3`) and Windows PowerShell (`py`). Run them from the repo folder. The scripts use only the Python standard library and need Python 3.9 or newer. On Windows you can also use the repo's own interpreter, `.venv\Scripts\python.exe`.

Files that matter:

| File | What it is |
|---|---|
| `index.html` | the demo page. Its styles and script are inline |
| `details.html`, `app.css`, `app.js`, `pipeline.js`, `pipeline.css` | the old seven-screen page and its router and pipeline view |
| `copy.js` | every word on screen, in `window.COPY`. No digit is allowed in it. `index.html` reads the `page` block. `details.html` reads the rest |
| `pipeline-glyphs.js` | the line-drawn glyphs. Both pages use them |
| `fonts.css` | the embedded fonts. Do not open it in an editor: it is about 146 KB of base64 |
| `export.py`, `sources.json` | read the backend files and write `data/demo.js` and `data/numbers.json` |
| `bridge.py` | follows a pipeline run and writes `data/live.js`; also serves the page |
| `data/` | `demo.js` (the evidence), `live.js` (the live run), `replay.js` (a baked replay, read by `details.html` only), `numbers.json` |
| `docs/BUILD_CONTRACT.md` | the rules and the schemas. `../docs/DATA_CONTRACT.md` lists the file formats |

## 1. Open the dashboard

**From a file (no server, Wi-Fi can be off).**

```
macOS:     open ui/index.html
Windows:   Start-Process ui\index.html
```

Or double-click `ui/index.html`. Everything the page needs is in the folder. The run bar (section 2) is hidden when you open the page from a file. The page then shows how to start the local runner.

To open a view directly, add `#v=live`, `#v=results` or `#v=proof` to the address. That also skips the cover. The old page is `ui/details.html`; add `#s=1` to `#s=7` to open a screen.

**With the bridge.** The bridge serves `ui/` on this computer only (`127.0.0.1`, port 8765).

```
macOS:     python3 ui/bridge.py --serve
Windows:   py ui\bridge.py --serve
```

Then open `http://127.0.0.1:8765/` (the demo page) or `http://127.0.0.1:8765/details.html`. `Ctrl-C` stops the bridge. Use `--port N` if 8765 is taken.

The Live run view reads `data/live.js` once a second, from a file or from the bridge. Add one more mode to the same command to feed it:

| Goal | macOS | Windows |
|---|---|---|
| Scripted run, shown paced for display | `python3 ui/bridge.py --run demo --serve` | `py ui\bridge.py --run demo --serve` |
| Short real-model run (12 runs, about 2 to 3 minutes) | `python3 ui/bridge.py --run live-short --model qwen2.5:7b-instruct --serve` | `py ui\bridge.py --run live-short --model qwen2.5:7b-instruct --serve` |
| Several models in one batch | `python3 ui/bridge.py --run models --parallel 2 --serve` | `py ui\bridge.py --run models --parallel 2 --serve` |
| Follow the newest run that is still being written | `python3 ui/bridge.py --follow-latest --serve` | `py ui\bridge.py --follow-latest --serve` |
| Play back a finished log | `python3 ui/bridge.py --replay results/governance/<run>/progress.jsonl --speed 4 --loop --serve` | `py ui\bridge.py --replay results\governance\<run>\progress.jsonl --speed 4 --loop --serve` |
| Bake a log into `data/replay.js` for `file://` (`details.html`) | `python3 ui/bridge.py --bake results/governance/<run>/progress.jsonl` | `py ui\bridge.py --bake results\governance\<run>\progress.jsonl` |

Notes:

- `--run` accepts `demo`, `live-short`, `live-smoke` and `models`. The bridge starts only these fixed commands, without a shell. A real-model run first runs `scripts/preflight.py` and refuses to start if it fails. `--model` must be a tag in `config/models.txt`.
- When a `--run` ends, the bridge runs `ui/export.py --quiet --gov <run folder>` so the evidence updates. Add `--no-export` to skip it.
- The bridge finds the repo's `.venv` by itself. Use `--python PATH` to name another interpreter.
- Stopping a run with `Ctrl-C` on Windows uses `CTRL_BREAK_EVENT`. That is not verified on Windows.
- `--bake` keeps at most 120 frames.

## 2. The demo page

**Cover.** Any key or a click starts the show.

**Live run.** An n8n-style flow of nodes and wires: attack the agent, watch every tool call, check the rules, release gate, a person decides, tamper-evident log, risks under control, fix and test again. The state of each node comes from `window.LIVE`. With no feed, every node reads Done when `window.DEMO` holds candidates, and Waiting when it does not.

- **What just happened** shows one run from `window.DEMO`: the task, the hidden text, the tool call and the rule it broke. A run is chosen only from the data; nothing is invented. When that run is not the one the badge names, a line names its source (the rehearsal run or an earlier run, with its date).
- **Release candidates** lists one chip per candidate with the gate verdict from `window.DEMO`. While a real run is going (word LIVE, REPLAY or SAVED), the chips follow the gate results in `window.LIVE`. A scripted feed never replaces them.
- **One honesty badge** in the header says "Real model run", "Replay of a real run", "Scripted demo" or "No run yet". It adds the model name when it knows it. A scripted feed on the canvas also carries a "Scripted demo" label.

**Results.** One card per candidate: its defences, the verdict, how many attacks broke a rule, and the failed check with its limit. Below the cards are the share of normal tasks completed and the time to decision, when the data has them.

**Proof.** The last five decisions with who decided and when, whether the log check found an edit, the three rules, open risks and the controls used. This view reads `window.DEMO` only, never `window.LIVE`.

**Keys.** `1`, `2` and `3` open Live run, Results and Proof. The tabs do the same. The page has no other key. After a run ends with a good export, the page loads the new `demo.js` by itself. Reload the page after an export that you ran by hand.

**Run bar.** The bar sits above the flow. It shows only when the page is served by the bridge and `GET /api/session` returns a token. It lets you pick a run, pick a model for the live runs, and press Run or Stop. The page asks the bridge to start a run; the bridge, not the page, starts the command. A full run or an all-models run asks for confirmation first. The page checks `GET /api/status` every two seconds.

The bridge side of this contract is being added by lane W (server side: lane W). Until it is merged, the bridge answers 404 to `/api/...`, so the run bar stays hidden. The contract is on `127.0.0.1` only:

| Call | Meaning |
|---|---|
| `GET /api/session` | returns `{token}` |
| `GET /api/status` | returns the run state. The page reads `running`, `kind`, `model`, `kinds`, `models` and `last{exit_code, export_status, export_text}` |
| `POST /api/run` | body `{kind, model, parallel}` with the header `X-Redline-Token`. `kind` is `demo`, `live-short`, `live-full` or `models`. Answers 202 (started), 400, 403 or 409. The page treats any answer but 202 as "could not start" |
| `POST /api/stop` | stops the run. Same header |

## 3. Keys on the old page (`details.html`)

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

On screen 6 the keys `Space`, `Home`, `[` and `]` do nothing while a run from the bridge is still going, and while the release-decision card is open. The address (`#s=6&p=1` and so on) keeps the screen and the presenter mode, so a reload returns to the same place. The old page has no run bar.

## 4. Export the evidence

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

The status words below are shown on `details.html`. A value is REAL when a backend file produced it, SAMPLE when it is a hand-written stand-in from `ui/fixtures` or scripted output, and PENDING when it is not available. `index.html` does not show these three words. It shows the badge from section 2 and leaves out a value that is not available.

The mode words are below. **SCRIPTED** is a DEMO run: the traces come from `lab/demo.py`, not from a model. **REPLAY** is a recorded run. **SAVED** is a saved run folder that the CLI re-scored. **LIVE** appears only while a real-model run is still growing. Reviewer decisions in CLI runs are scripted, including LIVE runs, and `details.html` says so.

A committed `ui/data/demo.js` is always exported without `--allow-sample`. Before a commit, restore the two bridge files: `git checkout -- ui/data/live.js ui/data/replay.js`.

## 5. Fill and time the demo script

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

## 6. Fonts and credits

`fonts.css` embeds Cormorant Garamond (300, 400, 300 italic) and Inter (400, 500) under the SIL Open Font License 1.1. The 17 Oct plan credits Cormorant to Christian Thalmann (Catharsis Fonts) and Inter to Rasmus Andersson. Inter lists a Reserved Font Name and these are embedded subsets, so check that point before you publish.

The line-drawing style and stroke draw-on technique of the pipeline glyphs are adapted from the project lead's own website component (setu-site PipelineFigure). The 15 glyphs were drawn new for this project on 18 Oct 2026.

## 7. When something looks wrong

| You see | Do this |
|---|---|
| "No data yet" on `details.html` | run the export (section 4), then press `R` |
| Results or Proof show an old run | export again with `--gov <run folder>`, then reload the page |
| No run bar on the demo page | open the page through the bridge (`--serve`), not from a file. The server side of the run bar is lane W's work |
| Live run stays idle | no bridge is writing `data/live.js`. Start one (section 1) |
| Screen 6 of `details.html` says "Waiting" | no bridge is feeding it. Start one, or bake a replay and press `Space` |
| `export failed ... Previous demo.js kept` | the export found a fatal problem. Read the line it printed. The old file is still in place |
| The port is busy | add `--port 8766` to the bridge command |
| Numbers differ from the script | export again, then run `fill_script.py` with `--numbers ui/data/numbers.json` |
