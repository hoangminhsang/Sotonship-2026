# Prompt for your coding agent (paste it into a session opened in the `redline` folder)

You help run model tests on this Windows PC for the Redline release gate. You do not change code.

Read these files first, in this order: `handoff/STATUS.md`, `handoff/CONTEXT.md`, `handoff/RUNBOOK_WINDOWS.md`.

Then work in three steps:

1. **Plan.** Write a short plan: which models (exact tags from `ollama list`), in which order,
   and the estimated time per model after the smoke run. Show it to me and wait for my OK.
2. **Run.** Follow `RUNBOOK_WINDOWS.md` step by step. Run one model at a time. Copy the commands exactly.
3. **Report.** Fill in the report below from the files. Do not estimate or round a number that a file holds.

Rules:
- Never edit, delete or move files under `lab/`, `scripts/`, `corpus/`, `data/`, `tests/` or `ui/`.
- Never run `scripts/run_pyrit_attack.py`, the notebooks or the menu app (`lab/app.py`).
- Never start a second run while one is running. Never run `git pull` during a run.
- Never invent a number. Every number in the report comes from a named file. Keep the `[LIVE]` label.
- If a step fails, stop, show me the exact error text, and wait.
- Never commit `corpus/` files. Use `git add results/runs results/governance` only, never `git add -A`.

## Report template

```
Machine: GPU <nvidia-smi name>, Ollama <version>, Python <version>
pytest last line: <copy>
Commit: <git log --oneline -1>

Per model:
  tag:            <exact tag>
  digest:         <from preflight>
  tool support:   yes | no (<which check failed>)
  mean_s:         <from the smoke manifest>
  run folders:    <three folder names in results/runs/>
  RC-001 attack_success_rate: <rate> n=<n> CI <lo>-<hi>   (summary.csv)
  RC-002 attack_success_rate: ...
  RC-003 attack_success_rate: ...
  benign_task_success (each candidate): ...
  error_runs (each candidate): ...
  gate verdicts:  <first line of each gate_RC-00*.txt>
  notes:          <anything unusual>
Results sent by: branch results/<stamp> | zip | both
```
