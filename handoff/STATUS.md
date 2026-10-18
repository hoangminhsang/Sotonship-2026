# Friend pack — status (v2, Sun 18 Oct)

## What to run now

Use the **v2 batch runner**: steps V1–V15 at the top of `RUNBOOK_WINDOWS.md`.
The v1 sequential commands stay in the same file as **Fallback: v1 (F0–F7)**. Use them if v2 fails and the owner cannot fix it in 5 minutes.

Do not run a model again if it is already running or finished under v1. Runbook step V5 shows how to check.

## Branch and code

- Branch: `demo/integrated` in `hoangminhsang/Sotonship-2026`.
- The owner gives you the commit id. After the pull, `git log --oneline -1` must start with that id (runbook V3).
- Test suite: the owner gives you the expected last line of `pytest` for this commit. It has not been run on Windows yet.
  (v1 had `291 passed` on macOS at `5e3062f`.)

## What is new in v2

- `scripts\run_governance.py` no longer rewrites `corpus\*.jsonl`. It only checks them (read-only). So:
  - two runs at the same time are safe for the files,
  - `git status` stays clean,
  - manifests no longer get `git_dirty: true` because of line endings.
- Each run writes `<out>\progress.jsonl` (one JSON line per event).
- `scripts\run_models.py` runs several models in one batch. Per model: preflight, smoke run, tool-support check, full run.
  - `--parallel 1` to `4` runs models at once. It forces 1 when the corpus fix is missing.
  - It writes `results\models\<STAMP>\plan.json`, `status.jsonl` and `<slug>.<step>.log`.
  - Exit codes: 0 ok, 1 a step failed, 2 usage error, 130 stopped.
  - A model with no tool support is skipped after its smoke run, with the reason. That is a result, not a failure.
  - `config\models.txt` holds one Ollama tag per line (now `qwen2.5:7b-instruct`). Add exact tags from `ollama list`. Never guess a tag.
- `ui\bridge.py` feeds the dashboard from `progress.jsonl`: `--serve --follow-latest`, `--replay`, `--run demo`, `--batch`.
  The bridge check is runbook step V15.

## Not verified on Windows (watch these and report exactly what you see)

- **Ctrl-C and `CTRL_BREAK_EVENT`** for the child processes of `run_models.py` and `bridge.py`. Stop with one Ctrl-C and wait 60 s (V12).
- **The `os.replace` retry** (the atomic file replace that the writers retry on Windows). If you see `PermissionError` or `Access is denied`, copy the line.
- **`OLLAMA_NUM_PARALLEL` with two models at once.** Watch `ollama ps` (V11). If a model runs on CPU or the models unload in turns, restart with `--parallel 1`.
- `pytest` and the bridge on Windows.

The v1 sequential commands (F0–F7) stay the fallback for all of these.

## Coming (the owner tells you)

- The dashboard is now one page: `http://127.0.0.1:8765/`. It has a cover ("Redline", "Press any key to start") and three views: Live run, Results and Proof.
  The old seven-screen page moved to `http://127.0.0.1:8765/details.html`. Its address works as before: `details.html#s=6` shows the pipeline during a bridge run, and `details.html#s=7` shows the models.
- Do not pull while a batch runs. Pull between batches (V3).
- Expected `pytest` last line on macOS for this push: `1090 passed`

## Run the pipeline from the page

Not verified on Windows. The server part needs lane W (bridge run API) in the pull. Without it, the page shows no run bar: use the commands in V15 instead.

1. Start the bridge: `& $PY ui\bridge.py --serve`. Do V1 first, and start no batch at the same time.
2. Open the address that the `bridge:` line prints (`http://127.0.0.1:8765/`). Press any key to leave the cover. The run bar sits above the flow on the Live run view.
3. Pick a run kind in the first list (label "Run"). The choices are:
   - "Scripted demo (no model)": needs no model and no Ollama.
   - "Quick live check": needs Ollama running with the model pulled.
   - "Full run": needs Ollama running with the model pulled. It runs `--live --sample 26 --reps 3`.
   - "All models": the code does not say if Ollama is needed. Treat it as yes.
4. For "Quick live check" and "Full run", a second list (label "Model") appears. Pick a tag from it. Check the tag against `ollama list`.
5. Press "Run". "Full run" and "All models" first ask "This run takes a long time on this machine. Start it anyway?"
6. Watch the flow on the Live run view. The text next to the buttons says "Running", and later "Finished. The results are updated."
7. Press "Stop" to end the run. The text says "Stopping". Then run the process check of V2 (no python process left).

If the run bar does not appear, the bridge has no run API. Copy the `bridge:` lines to the owner.

## Watch the backend in a second window

Not verified on Windows. `ui\watch.py` prints the backend work as clean lines, in step with the page.
It reads `ui\data\live.js`, which `ui\bridge.py --serve` writes. It only reads the file and never changes it.

1. Start the bridge as in the section above. In a second terminal, run `python ui/watch.py` (Windows: `& $PY ui\watch.py`, with the `$PY` variable of runbook V1).
2. Flags: `--file PATH` (read another `live.js`), `--once` (print the current state once and exit), `--no-color` (plain text).
   Ctrl-C exits. With no run yet, it prints `waiting for a run`.
3. Layout for the pitch: the browser on one half of the screen, the `watch.py` terminal on the other half. The bridge runs in a third window, which stays hidden.
4. Not verified on Windows: the Unicode glyphs and the colours on the Windows console. If the glyphs look wrong or the colours show as raw codes, add `--no-color` and tell the owner what you saw.
5. The page demo ("Scripted demo (no model)") takes about 21 s on the owner's Mac. The time on the Windows PC is not measured.
6. Check: runbook V15, step 2.

## Known effects

- A bridge run rewrites the tracked files `ui\data\live.js` (and `replay.js` with `--bake`). `git status` then shows them as modified.
  Run `git checkout -- ui/data` before the next pull.
- Your edit of `config\models.txt` shows as modified. Never commit it.
- Run folders store a sha256 hash of every file. Clone with `core.autocrlf=false` (F1) so a push keeps the bytes exact.
- v1 runs (code before v2) rewrote `corpus\*.jsonl` with Windows line endings. If `git status` shows ` M corpus/...`, run `git checkout -- corpus`.
  Never commit the corpus files.
- After you push results from a `results/<stamp>` branch, stay on that branch. Switching back to `demo/integrated` hides the result folders (V3, case B).
