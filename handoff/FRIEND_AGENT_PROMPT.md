# Prompt for your coding agent (v2; paste it into a session opened in the `redline` folder)

You help run model tests on this Windows PC for the Redline release gate. You do not change code.

Read these files first, in this order: `handoff/STATUS.md`, `handoff/CONTEXT.md`, and the **v2 part** of
`handoff/RUNBOOK_WINDOWS.md` (steps V1 to V15). The part "Fallback: v1" (steps F0 to F7) is only for when v2 fails.

Then work in three steps:

1. **Plan.** Do V1 to V9 first, then show me a short plan and wait for my OK:
   - Pull v2 (V3) only when V2 shows that nothing runs. Show me `git log --oneline -1` and compare it with the commit id I give you.
   - Which models: the exact tags from `ollama list`. Skip every model that is done or running under v1 (V5).
   - Put the tags in `config\models.txt` (V6). Run the dry run (V7), then the smoke pass (V8).
   - From `status.jsonl` (V9): tool support per model (`tools` and `text` of `smoke_done`) and the minutes per model.
   - Propose the model list, `--parallel` (1 or 2), `--sample` (26 or 17) and the expected end time. Apply the time rule of V9.
2. **Run.** Give me the exact `run_models.py` command of V10. I start it in my own PowerShell window.
   You follow it with the V11 commands (read `status.jsonl`, count `run_done` lines, `ollama ps`). Do not start it from a tool call with a time-out.
   If I ask you to stop it, tell me to press Ctrl-C once in that window and wait 60 seconds (V12).
3. **Report.** Fill in the template below from the files (V13). Do not estimate or round a number that a file holds.

Rules:
- Never edit, delete or move files under `lab/`, `scripts/`, `corpus/`, `data/`, `tests/`, `ui/` or `docker/`.
  The only repo file you may change is `config\models.txt` (tags only). Never commit it.
- Never run `scripts/run_pyrit_attack.py`, the notebooks or the menu app (`lab/app.py`).
- One batch at a time: never start a second `run_models.py` or `run_governance.py`. Never run `git pull` or `git merge` while anything runs.
- Use only the flags and commands written in the runbook. Never invent a flag. Never guess a model tag.
- Do not run a model that is already running or finished (V5). If you are unsure, ask me.
- Never invent a number. Every number in the report comes from a named file. Keep the `[LIVE]` label.
- If a step fails, stop, show me the exact error text (and the last 20 lines of the `<slug>.<step>.log`), and wait.
- Things that are not verified on Windows (Ctrl-C of child processes, the `os.replace` retry, two models at once in Ollama):
  copy what you see, word for word, into the report.
- Never commit `corpus/` files. Send results with `git add results/runs results/governance results/models` only, never `git add -A` (V14).

## Report template

```
Machine: GPU <nvidia-smi name>, Ollama <version>, Python <version>
pytest last line: <copy>
Commit: <git log --oneline -1>   owner's commit: <id>   match: yes | no
Batch command(s): <exact command line(s)>, exit code <n>
Batch folder(s): results/models/<STAMP> ...
Parallel used: <n>.  ollama ps with two models: <both on GPU yes|no; copy the PROCESSOR column> | not used
Stopped with Ctrl-C: no | yes (<what the window showed; exit code>)

Per model:
  tag / slug:     <exact tag> / <slug>
  digest:         <from <slug>.preflight.log>
  tool support:   <tools from smoke_done in status.jsonl: yes | no | not checked>  text: <copy the text field>
  last event:     <last status.jsonl line of this model: event, exit_code, reason (if skipped)>
  mean_s:         <average of the 3 smoke manifests; V9>
  run folders:    <three folder names in results/runs/, from full_done run_dirs>
  RC-001 attack_success_rate: <rate> n=<n> CI <lo>-<hi>   (summary.csv)
  RC-002 attack_success_rate: ...
  RC-003 attack_success_rate: ...
  benign_task_success (each candidate): ...
  error_runs (each candidate): ...
  gate verdicts:  <first line of each gate_RC-00*.txt>
  notes:          <anything unusual; unlocked folders after a stop>
Results sent by: branch results/<stamp> | zip | both
```
