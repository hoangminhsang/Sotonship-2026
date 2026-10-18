# Venue checklist — owner and friend, 07:30–08:00

The commands are in `RUNBOOK_WINDOWS.md`. This list says what to do and what to check.

1. On the PC: `ollama --version`, `ollama list` (note the exact Granite tag), `nvidia-smi`, `py -0` (runbook step 0).
2. Clone and switch to `demo/integrated`. Check that `git log --oneline -3` shows the handoff commit on top (step 1).
3. Make the venv, install, run `pytest` and note the last line (step 2).
   Stop only if `pip install` fails.
4. For `qwen2.5:7b-instruct` first: preflight, smoke run, tool-support check (step 4).
   Note `mean_s` and the time estimate.
5. Start the full run for qwen (step 5). Leave it running. Watch `ollama ps` in a second window.
6. Smoke-test every other model **only between full runs** (a smoke run is a run too).
   Note which models support tool calls. Run their full runs one after another.
7. Ask the teammates to push their model list and model-testing code now.
8. 07:45: open the laptop and tell the session `continue Stage 2`. When the teammates push, say `teammates pushed`.
9. About 10:15: pull v2 on the PC when the session says it is ready (bridge and live view).
10. About 10:30: send the results (step 7, branch and zip). Tell the laptop session `results pushed`,
    then `headline is <model>`.

**Time rule:** add up the estimates from step 4 (`mean_s` × 414 ÷ 60 minutes per model).
If the total ends after 10:30, run fewer models. Keep `--sample 26` for qwen and the likely headline model.
Run the rest with `--sample 17` (306 model runs instead of 414) or not at all.
