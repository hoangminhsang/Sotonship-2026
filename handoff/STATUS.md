# Friend pack — status (v1, Sun 18 Oct, 06:55 BST)

## What to run today

Run the **sequential commands** in `RUNBOOK_WINDOWS.md`: one model at a time, one PowerShell window.
They need nothing that is still being built.

## Branch and code

- Branch: `demo/integrated` in `hoangminhsang/Sotonship-2026`.
- The run code (the `scripts/` and `lab/` folders) is the same as `review/hardening` at `fb0e275` (PR #1).
- The dashboard (`ui/`) is work in progress. You do not need it for the runs.
- `git log --oneline -3` must show, from the top:
  1. `handoff: friend pack v1 …` (this pack)
  2. `5e3062f Add demo UI (work in progress) …`
  3. `fb0e275 review: REPORT, MAP and lane files …`
- Test suite on macOS at `5e3062f`: `291 passed`. It has not been run on Windows yet.

## Not in v1 (it comes in v2, about 10:15)

- `scripts/run_models.py` (several models in parallel, with progress logs). Until v2, run one model at a time.
- The read-only corpus check. In v1, every run rewrites `corpus\*.jsonl` at start. That is why
  two runs at the same time are not safe.
- `ui/bridge.py` and the live pipeline screen.

When v2 is pushed, the owner tells you. Models that have not started by then can use the parallel runner.

## Known effects on Windows (v1)

- Each run rewrites `corpus\*.jsonl` with Windows line endings. `git status` then shows the
  corpus files as modified, and the run manifests record `git_dirty: true` and a different
  `corpus_sha256` than the Mac. The records are the same, so the results are the same.
  Never commit the corpus files.
- Run folders store a sha256 hash of every file. Clone with `core.autocrlf=false` (runbook step 1) so a push keeps the bytes exact.
