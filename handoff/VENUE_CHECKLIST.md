# Venue checklist — owner and friend

The commands are in `RUNBOOK_WINDOWS.md`. This list says what to do and what to check.
Steps V1–V15 are v2 (the batch runner). Steps F0–F7 are the v1 sequential fallback.

## Setup, 07:30–08:00 (v1 sequential)

1. On the PC: `ollama --version`, `ollama list` (note the exact Granite tag), `nvidia-smi`, `py -0` (runbook F0).
2. Clone and switch to `demo/integrated`. Check that `git log --oneline -3` shows the handoff commit on top (F1).
3. Make the venv, install, run `pytest` and note the last line (F2).
   Stop only if `pip install` fails.
4. For `qwen2.5:7b-instruct` first: preflight, smoke run, tool-support check (F4).
   Note `mean_s` and the time estimate.
5. Start the full run for qwen (F5). Leave it running. Watch `ollama ps` in a second window.
6. Smoke-test every other model **only between full runs** (a smoke run is a run too).
   Note which models support tool calls. Run their full runs one after another.
7. Ask the teammates to push their model list and model-testing code now.
8. 07:45: open the laptop and tell the session `continue Stage 2`. When the teammates push, say `teammates pushed`.

## v2, about 10:15 (when the session says it is ready)

9. Nothing runs (V2). Pull v2 (V3). Check `git log --oneline -1` against the commit id the owner gave. Run `pytest` and note the last line (V4).
10. Skip every model that is done or running under v1: check `results\runs` and `results\governance` first (V5).
    Add the other exact tags from `ollama list` to `config\models.txt` (V6). Dry run (V7).
11. Smoke pass: `run_models.py --smoke-only --parallel 1` (V8). Read `status.jsonl`: tool support and minutes per model (V9).
12. Apply the time rule (below). Start the batch in your own window (V10). Check `ollama ps` two minutes after two models run (V11).
13. About 10:30, or the time the owner names: send the results (V14, branch and zip). Tell the laptop session `results pushed`,
    then `headline is <model>`.

## Bridge check, about 10:45

Do this when no batch runs and the results are sent. Details are in runbook V15.
Only the `#s=6` part of check 15 needs the owner's push with the pipeline screen. The rest works now.

14. `cd $HOME\Desktop\redline`, then V1. `git status --short` shows no `corpus/` line.
15. `& $PY ui\bridge.py --run demo --serve --no-export`. Check:
    - Two `bridge:` lines appear: `serving ui/ at http://127.0.0.1:8765/ ...` and `started demo, writing governance_out/bridge/demo_<STAMP>`.
    - `(Get-Item ui\data\live.js).LastWriteTime` changes while the run goes.
    - Browser: `http://127.0.0.1:8765/` loads. After the push with screen 6: `http://127.0.0.1:8765/#s=6` shows the pipeline moving.
    - No line `bridge: the run ended with exit code 1` (or another code that is not 0) appears. Ctrl-C stops the bridge.
16. Replay of a real run: `& $PY ui\bridge.py --replay results\governance\<STAMP>_<slug>\progress.jsonl --speed 4 --loop --serve`. Check that it plays and starts again. Ctrl-C stops it.
17. Clean up: `git checkout -- ui/data`. Run the process check of V2 (no python process left).
18. Tell the owner `bridge ok`, or copy every `bridge:` line and what the window showed after Ctrl-C (not verified on Windows).

## Time rule

Add up the estimates from V9 (`mean_s` × 414 ÷ 60 minutes per model; `mean_s` × 306 ÷ 60 with `--sample 17`).
The sum is the planning number. A parallel gain is not verified.
If now + the sum is after the results deadline (planned 10:30; the feature freeze is 11:30), run fewer models.
Keep `--sample 26` for qwen and the likely headline model.
Run the rest with `--sample 17` (306 model runs instead of 414) or not at all.
Mixed sample sizes need two batches, one after the other.
