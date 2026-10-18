# Windows runbook — model runs on the friend's PC

Use PowerShell. Copy every result line into `NOTES.txt` (V14 lists what to send). Never commit `NOTES.txt`.

This file has two parts:

- **v2 (steps V1–V15). Use this first.** One command, `scripts\run_models.py`, runs several models.
  For each model it does: preflight, a smoke run, a tool-support check, and the full run.
- **Fallback: v1 sequential (steps F0–F7), at the bottom.** One model at a time. Use it only if v2 fails
  and the owner cannot fix it in 5 minutes.

Do **not** run `scripts\run_pyrit_attack.py`, the notebooks or the menu app (`lab\app.py`): they rewrite `corpus\` and `results\`.

Terms:
- **smoke run:** a very short run (1 attack test and 1 normal test, once) that checks the model before the full run.
- **batch:** one start of `run_models.py`. Its folder is `results\models\<STAMP>`.
- **slug:** the model tag written safe for file names. `qwen2.5:7b-instruct` becomes `qwen2.5-7b-instruct`.

---

# v2 — batch runner (steps V1–V15)

Rules for v2:
1. **One batch at a time.** Do not start a second `run_models.py` or `run_governance.py`.
   The files are safe now (a run no longer rewrites `corpus\`). The GPU is the limit, so `--parallel` decides how many models run at once.
2. **No `git pull` while anything runs.**
3. Start the batch in **your own PowerShell window**. A tool with a time-out can kill it.
4. Do not run a model again if it is already running or finished under v1 (step V5).
5. Use only the flags written here. Use only the model tags that `ollama list` shows. Never guess a tag.

## V1. Every new PowerShell window

```powershell
cd $HOME\Desktop\redline
$PY = "$PWD\.venv\Scripts\python.exe"
$env:PYTHONUTF8 = "1"          # UTF-8 for output and files, so Windows code pages cannot crash a run
$env:PYTHONUNBUFFERED = "1"    # progress lines appear at once
```

No `.venv` folder yet? This PC has not done v1. Do F0, F1 and F2 first (they are at the bottom), then come back.

## V2. Check that nothing is running

```powershell
Get-CimInstance Win32_Process -Filter "Name LIKE 'py%'" | Select-Object ProcessId, CommandLine | Format-List
ollama ps
```

- The first command lists every Python process. If a `CommandLine` holds `run_governance.py`, `run_models.py`
  or `bridge.py`, something runs. Wait until it ends. Do not start v2 on top of it.
- A v1 run is still going? Let it finish. Do not start another v1 model. Then continue here.
- `ollama ps` should list no model (or only a model that is still unloading).

## V3. Pull v2

First, see which branch you are on and what is changed:

```powershell
git branch --show-current
git status --short
```

`git status --short` may show:

| Line | What to do |
|---|---|
| nothing, or lines that start with `??` (for example `?? results/`) | fine |
| ` M corpus/...` | `git checkout -- corpus` (a v1 run rewrote it) |
| ` M ui/data/...` | `git checkout -- ui/data` (the bridge rewrote it) |
| ` M config/models.txt` | your own model list: keep it |
| anything else | stop and show the owner |

**Case A: the branch is `demo/integrated`** (the normal case):

```powershell
git fetch
git switch demo/integrated
git pull --ff-only
git log --oneline -1
```

**Case B: the branch starts with `results/`** (you pushed results under v1).
Do not switch away: that would hide your result folders (they stay in that branch, but leave the disk).
Merge instead:

```powershell
git fetch
git merge --no-edit origin/demo/integrated
git merge-base --is-ancestor <COMMIT> HEAD; $LASTEXITCODE   # 0 = the owner's commit is included
```

Check: in case A, `git log --oneline -1` starts with the commit id the owner gave you (`<COMMIT>`).
In case B, the last command prints `0`.
If `git pull` or `git merge` stops with an error, copy the error to the owner and wait.
One known case: it says `config/models.txt` would be overwritten. Copy your tags somewhere, run `git checkout -- config`,
repeat the pull, then add your tags again (V6).

## V4. Packages and tests

```powershell
& $PY -m pip install -r docker\requirements.txt    # nothing new is installed if the pins did not change
& $PY -m pytest                                    # write down the last line
```

The owner gives you the expected last line for this commit. It has not been run on Windows yet.
If some tests fail, write down the failing test names and continue. Stop only if `pip install` fails.

## V5. What is already done or running (do not run it twice)

A model is **done** when its full run has a folder in `results\governance` without `_smoke` in the name
and that folder holds three gate files:

```powershell
Get-ChildItem results\governance -Directory | Where-Object { $_.Name -notlike '*_smoke' } | ForEach-Object {
  "{0}  gate files={1}" -f $_.Name, (Get-ChildItem $_.FullName -Filter 'gate_RC-00*.txt').Count
}
```

- `gate files=3`: done. Do not run this model again.
- Less than 3: the run is not finished, or it was stopped. Ask the owner before you run that model again.

Also look at the run folders (the newest 12):

```powershell
Get-ChildItem results\runs -Directory | Sort-Object Name | Select-Object -Last 12 | ForEach-Object {
  $m = Get-Content "$($_.FullName)\manifest.json" -Raw | ConvertFrom-Json
  "{0}  locked={1}  complete={2}" -f $_.Name, $m.locked, $m.complete
}
```

`locked=True complete=True` is a finished folder. A smoke run also makes three such folders. Smoke folders are normal.

## V6. The model list

The batch runs every tag in `config\models.txt`. One tag per line. A line that starts with `#` is a comment. A tag may appear once.

```powershell
ollama list                    # copy the exact tags
notepad config\models.txt
```

- Add one exact tag per line, as `ollama list` shows it. Granite too: use its exact tag, never a guess.
- Put `#` in front of every model that V5 says is done or running.
- A tag that is not in `ollama list` needs `--pull` in V8 (it needs internet).
- If a coding agent edits the file with `Add-Content`, run `Get-Content config\models.txt` after it and check that each tag is on its own line.
- Do not commit `config\models.txt`.

## V7. Dry run (writes nothing)

```powershell
& $PY scripts\run_models.py --dry-run
```

Check the output:
- `Mode` says `LIVE`.
- `Models` lists the tags you expect, each with its slug. Copy the slugs.
- `Parallel` shows how many models run at once. With two or more models it shows 2. If it shows 1 with two or more models,
  or a warning appears, the code may be older than v2 (the batch forces 1 when the corpus fix is missing). Tell the owner.
- A bad line in `config\models.txt` ends with an error and exit code 2. Fix the line.

## V8. Smoke pass (about 2 min per model)

Preflight and smoke run for every model, one after another, with no full run:

```powershell
& $PY scripts\run_models.py --smoke-only --parallel 1
"exit code: $LASTEXITCODE"
```

- Add `--pull` if a tag is not in `ollama list` yet. A wrong tag makes the pull fail. Then the tag is wrong.
- Exit codes: 0 ok, 1 a step failed, 2 usage error, 130 stopped.
- A model that has no tool support is **not** a failure: it is a result (exit code 0).
- The smoke runs make three new locked run folders per model in `results\runs`. That is normal.
- The full batch in V10 does preflight and the smoke run again. This is on purpose; both are short.

## V9. Read the result of the smoke pass, then apply the time rule

**Tool support and status of every model** (all batches, from `status.jsonl`):

```powershell
foreach ($BD in Get-ChildItem results\models -Directory | Sort-Object Name) {
  "== batch $($BD.Name)"
  Get-Content "$($BD.FullName)\status.jsonl" | ForEach-Object { $_ | ConvertFrom-Json } |
    Where-Object { $_.event -in 'smoke_done','full_done','skipped','preflight_fail' } |
    Select-Object event, slug, exit_code, tools, text, reason | Format-Table -AutoSize -Wrap
}
```

(If one line fails to parse, run the command again: the file was being written.)

How to read `status.jsonl`. One JSON line per state change. Every line has `ts` (UTC time), `event`, `slug`, `tag`.

| `event` | Meaning | What you do |
|---|---|---|
| `queued` | the model waits for a free slot | nothing |
| `pull_start`, `pull_done` | `ollama pull`. `exit_code` 0 is ok | a non-zero code: the tag is wrong or there is no internet |
| `preflight_start`, `preflight_ok` | preflight passed | nothing |
| `preflight_fail` | preflight failed; `detail` holds the FAIL lines | stop for this model; send `detail` to the owner |
| `smoke_start` | the smoke run started | nothing |
| `smoke_done` | the smoke run ended. `tools` is `yes` or `no`; `text` gives the reason, for example `no (every run errored)`; `run_dirs` are the run folders | write `tools` and `text` in `NOTES.txt` |
| `full_start` | the full run started (`out` is its governance folder) | nothing |
| `full_done` | the full run ended. `exit_code` 0 is ok. `run_dirs` are its three run folders | go to V13 |
| `skipped` | the full run will not happen. `reason` says why (no tool support, a failed step, `--smoke-only`, stopped) | write `reason` in `NOTES.txt` |

`skipped` is the last line of every model that did not reach `full_done`.
A model with `tools=no` has **no tool support**: its full run is skipped. A model without tool calls would read as a false "safe 0%".

**Minutes per model** (from the newest batch, which is the smoke pass):

```powershell
$BATCH = Get-ChildItem results\models -Directory | Sort-Object Name | Select-Object -Last 1
$ev = Get-Content "$($BATCH.FullName)\status.jsonl" | ForEach-Object { $_ | ConvertFrom-Json } | Where-Object { $_.event -eq 'smoke_done' }
foreach ($e in $ev) {
  $secs = @()
  foreach ($d in $e.run_dirs) {
    $m = Get-Content "results\runs\$d\manifest.json" -Raw | ConvertFrom-Json
    $secs += [double]$m.seconds_per_run.mean
  }
  if ($secs.Count -eq 0) { "{0}  tools={1}  no run folders: {2}" -f $e.slug, $e.tools, $e.text; continue }
  $mean = ($secs | Measure-Object -Average).Average
  "{0}  tools={1}  mean_s={2:N1}  full run: {3:N0} min (--sample 26), {4:N0} min (--sample 17)" -f $e.slug, $e.tools, $mean, ($mean * 414 / 60), ($mean * 306 / 60)
}
```

`mean_s` is the average of the three smoke folders. The first run includes the model load, so the estimate is a little high.

**Time rule.**
1. A full run has 414 model runs (`--sample 26`, 3 candidates). With `--sample 17` it has 306. Minutes per model = `mean_s` × 414 ÷ 60, or `mean_s` × 306 ÷ 60.
2. Add up the minutes of every model you want with tool support. That sum is your planning number.
   With `--parallel 2` the batch may end sooner if both models run on the GPU at the same time. That is **not verified**. Do not count on it.
3. End time = now + the sum. It must come before the results deadline. The owner names it (planned 10:30; the feature freeze is 11:30).
4. Too long? Run fewer models, or run the rest with `--sample 17`, or not at all.
   Keep `--sample 26` for `qwen2.5:7b-instruct` and for the model the owner expects to be the headline.
5. `--sample` is the same for every model in a batch. For two sample sizes, run two batches **one after the other**:
   first the `--sample 26` models (`--only`), then the `--sample 17` models.

## V10. The full batch

Start it in your own PowerShell window. The default command runs every model in `config\models.txt`
with `--sample 26 --reps 3` (414 model runs per model):

```powershell
& $PY scripts\run_models.py --parallel 2
"exit code: $LASTEXITCODE"
```

Variants (change the first line only):
- Shorter, 306 model runs per model: add `--sample 17`.
- Only some models: add `--only <slug>`. It takes a slug or a tag. Repeat the flag for more models.
  Example: `& $PY scripts\run_models.py --only qwen2.5-7b-instruct --parallel 1`.
- One model at a time: use `--parallel 1`. `--parallel` takes 1 to 4; the default is 2.
- A tag that is not in `ollama list`: add `--pull`.

The batch writes its own logs, so you do not need `Tee-Object`.

What is in the batch folder `results\models\<STAMP>`:
- `plan.json`: the plan (models, commands; no absolute paths).
- `status.jsonl`: one line per state change (V9).
- `<slug>.<step>.log`: the output of each step (`pull`, `preflight`, `smoke`, `full`).
- Each full run writes `results\governance\<STAMP>_<slug>\` with `progress.jsonl` and the gate files.

If `--parallel 2` is slow or odd, see V11 and stop the batch (V12). Restart with `--parallel 1`.

## V11. Watch the batch (second window)

Do V1 in the second window first. Then:

```powershell
$BATCH = Get-ChildItem results\models -Directory | Sort-Object Name | Select-Object -Last 1
Get-Content "$($BATCH.FullName)\status.jsonl" -Wait -Tail 5     # live status lines; Ctrl-C here only ends this watch
```

Runs done so far per model (one `run_done` line in `progress.jsonl` per finished model run; a full run has 414, or 306 with `--sample 17`):

```powershell
$full = Get-Content "$($BATCH.FullName)\status.jsonl" | ForEach-Object { $_ | ConvertFrom-Json } | Where-Object { $_.event -eq 'full_start' }
foreach ($f in $full) {
  $n = (Select-String -Path "$($f.out)\progress.jsonl" -Pattern 'run_done' -SimpleMatch).Count
  "{0}: {1} runs done" -f $f.slug, $n
}
```

The last lines of one step log:

```powershell
Get-Content "$($BATCH.FullName)\<slug>.full.log" -Tail 20
```

GPU check. **Two minutes after two `full_start` lines appeared**, run:

```powershell
ollama ps
nvidia-smi
```

- Good: both models are listed, and the `PROCESSOR` column says GPU (100% GPU) for both.
- Bad: a model runs on CPU, or only one model is loaded at a time, or the models unload and load in turns.
  How Ollama behaves with two models is **not verified**. Then stop the batch (V12) and restart with `--parallel 1`.
- Do not change Ollama settings (`OLLAMA_NUM_PARALLEL` and others) unless the owner says so.
- Write what `ollama ps` showed in `NOTES.txt`.

Dashboard: V15 shows the live view.

## V12. Stop safely

1. Press **Ctrl-C once**, in the window that runs `run_models.py`.
2. Wait up to 60 seconds. Do not close the window. Do not press Ctrl-C again.
   The batch sends one interrupt to each running child, so the child can lock its run folders. Queued models are skipped. The exit code is 130.
3. A second Ctrl-C, or 60 seconds without an exit, ends the children at once. The batch then lists every run folder that stayed unlocked.
4. Check that it stopped:

```powershell
Get-CimInstance Win32_Process -Filter "Name LIKE 'py%'" | Select-Object ProcessId, CommandLine | Format-List
ollama ps
```

5. A python process with `run_governance.py` is still there after 2 minutes? Tell the owner. As a last resort, stop it with `Stop-Process -Id <ProcessId>`.
6. A stopped run is never reused. Its partial folder stays on disk with `locked=False`. Do not delete it. Write its name in `NOTES.txt`.
7. To go on: start a **new** batch with `--only` for the models that did not reach `full_done`. A model with a `full_done` line that has `exit_code` 0 and no `reason` is done.

Not verified on Windows: how the batch sends the interrupt (`CTRL_BREAK_EVENT`) and how a child answers it.
Write down exactly what you see in the window after Ctrl-C.

## V13. Check a finished model

```powershell
$BATCH = Get-ChildItem results\models -Directory | Sort-Object Name | Select-Object -Last 1     # or the batch you want
$done = Get-Content "$($BATCH.FullName)\status.jsonl" | ForEach-Object { $_ | ConvertFrom-Json } | Where-Object { $_.event -eq 'full_done' }
foreach ($e in $done) {
  "== {0}  exit_code={1}  out={2}" -f $e.slug, $e.exit_code, $e.out
  foreach ($d in $e.run_dirs) {
    $m = Get-Content "results\runs\$d\manifest.json" -Raw | ConvertFrom-Json
    "   {0}  locked={1}  complete={2}  digest={3}" -f $d, $m.locked, $m.complete, $m.model_digest
  }
  Get-ChildItem $e.out -Filter 'gate_RC-00*.txt' | ForEach-Object { "   " + $_.Name + ": " + (Get-Content $_.FullName -TotalCount 1) }
}
Select-String -Path "$($BATCH.FullName)\*.preflight.log" -Pattern 'digest' -SimpleMatch      # the digest that preflight printed
```

For each run folder:
- `manifest.json` has `locked=True` and `complete=True`.
- `model_digest` equals the digest from the preflight log.
- `summary.csv` (`Get-Content results\runs\<run folder>\summary.csv`): `attack_success_rate` has `n` = 78 and `benign_task_success` has `n` = 60
  (with `--sample 17`: 51 and 51). Note `error_runs`.

The verdicts are in `gate_RC-00*.txt`. Every file starts with `[LIVE]`. Keep that label when you quote a number.

## V14. Send the results

First check what git sees:

```powershell
git status --short
```

Only `results/` lines (and your own ` M config/models.txt` or ` M ui/data/...`) may appear. A `corpus/` line must not appear in v2: tell the owner.

**Option A — push a branch** (if you have push access to the repo):

```powershell
$RSTAMP = (Get-Date).ToUniversalTime().ToString("yyyyMMdd'T'HHmm'Z'")
git switch -c "results/$RSTAMP"
git add results/runs results/governance results/models
git commit -m "results: full LIVE runs $RSTAMP"
git push -u origin "results/$RSTAMP"
```

- Stay on this branch if you will run more models. Do not switch back to `demo/integrated`: that hides the result folders. For a later pull use case B in V3.
- Git says "Please tell me who you are"? Run `git config user.name "<your name>"` and `git config user.email "<your email>"`. Both show in the commit.
- Never use `git add -A`. Never add `corpus/`, `config/` or `ui/`.

**Option B — zip** (always do this as a byte-exact backup):

```powershell
$RSTAMP = (Get-Date).ToUniversalTime().ToString("yyyyMMdd'T'HHmm'Z'")
Compress-Archive -Path results\runs, results\governance, results\models -DestinationPath "$HOME\Desktop\redline_results_$RSTAMP.zip"
```

(Do v1 runs exist in `results\logs`? Add `results\logs` to the `-Path` list.)

The files contain paths such as `C:\Users\<your name>\...`. The repo is public. If you do not want your Windows user name to be public,
use option B only and give the zip to the owner.

`NOTES.txt` holds, per model: tag, digest, tool support (`tools` and `text` from `smoke_done`), `mean_s`, run folder names,
`error_runs`, and anything unusual. Add the `pytest` last line, `ollama --version`, the GPU name, the exact `run_models.py` command lines,
the exit codes, and what `ollama ps` showed.

## V15. Dashboard bridge check (about 10:45)

`ui\bridge.py` reads a run's `progress.jsonl` and writes `ui\data\live.js`. The dashboard reads that file.
The dashboard is one page (`http://127.0.0.1:8765/`) with a cover and three views: Live run, Results and Proof.
The old seven-screen page is `http://127.0.0.1:8765/details.html`. Its pipeline screen is `details.html#s=6`.
Do this when no batch is running. Do V1 first.

1. **Scripted rehearsal.** It needs no model and no Ollama. It writes to `governance_out\bridge\...` (git ignores it) and not to `results\`.

```powershell
(Get-Item ui\data\live.js).LastWriteTime            # note the time
& $PY ui\bridge.py --run demo --serve --no-export
```

Expected: two lines start with `bridge:`.
The first says `serving ui/ at http://127.0.0.1:8765/ (this computer only)`.
The second says `started demo, writing governance_out/bridge/demo_<STAMP>`.
Open `http://127.0.0.1:8765/` in a browser (press any key to leave the cover). Then open `http://127.0.0.1:8765/details.html#s=6`; the pipeline moves.
Wait until the run ends. In a second window check that the time changed:

```powershell
(Get-Item ui\data\live.js).LastWriteTime
```

Press Ctrl-C to stop the bridge. A line `bridge: the run ended with exit code N` with N other than 0 is a failure: copy all `bridge:` lines to the owner.

2. **Run bar** (needs lane W, the bridge run API, in the pull; not verified on Windows). Stop the bridge of step 1 first, then start it without a run:

```powershell
& $PY ui\bridge.py --serve
```

Open `http://127.0.0.1:8765/` and press any key. On the Live run view, a run bar shows above the flow. If it does not, the bridge has no run API: copy all `bridge:` lines to the owner and go on.
Choose "Scripted demo (no model)" in the list labelled "Run" and press "Run". The text next to the buttons says "Running" and the flow moves.
Start it again and press "Stop". The text says "Stopping" and the run ends. Then run the process check of V2: no python process stays.
The kinds "Quick live check" and "Full run" need Ollama running with the model pulled. "Scripted demo (no model)" does not.

3. **Replay of a real log.** Use the `out` folder of a finished full run (`results\governance\<STAMP>_<slug>`):

```powershell
& $PY ui\bridge.py --replay results\governance\<STAMP>_<slug>\progress.jsonl --speed 4 --loop --serve
```

Open the same address. The replay plays the real run at 4 times the speed and starts again after a pause. Ctrl-C stops it.

4. **Live view of a batch** (optional, only while a batch runs, in a second window):

```powershell
& $PY ui\bridge.py --serve --follow-latest                                                   # the newest progress.jsonl
& $PY ui\bridge.py --batch results\models\<STAMP> --follow-model <slug> --serve              # one model of a batch
```

5. **Clean up.** The bridge rewrote tracked files in `ui\data\`. Restore them before any later `git pull`:

```powershell
git checkout -- ui/data
git status --short
```

Messages and fixes:
- `bridge: no Python found: create .venv in the repo folder or pass --python PATH`: add `--python $PY`.
- `bridge: log not found: ...`: the path to `progress.jsonl` is wrong.
- `bridge: waiting`: there is no log yet. Start a run.
- The port is busy: add `--port 8766` and use that port in the address.
- Not verified on Windows: stopping a run with Ctrl-C (`CTRL_BREAK_EVENT`). After you stop the bridge, run the process check from V2.

---

# Fallback: v1 sequential (steps F0–F7)

Use this only if v2 fails. The commands work with v1 and v2 code.
Run **one model at a time**. Do not start a second run in another window (see `STATUS.md`).
On v1 code, every run rewrites `corpus\*.jsonl` at start. On v2 code it does not.

## F0. Checks (2 min)

```powershell
ollama --version
ollama list              # note the exact tag of every model you will run
nvidia-smi               # the GPU is visible
py -0                    # installed Pythons; the pins in docker\requirements.txt were verified on 3.14
git --version
```

Set the PC to never sleep while it is plugged in (Settings → System → Power).

## F1. Get the code (once)

```powershell
cd $HOME\Desktop
git clone -c core.autocrlf=false https://github.com/hoangminhsang/Sotonship-2026.git redline
cd redline
git switch demo/integrated
git log --oneline -3     # on v1: top line "handoff: friend pack v1 ...", then 5e3062f, then fb0e275. On v2: the commit the owner gave.
```

`core.autocrlf=false` keeps every file byte-exact. The run folders store a hash of every file,
so a line-ending conversion would break them on the owner's Mac.

## F2. Python environment (once)

```powershell
py -3.14 -m venv .venv
$PY = "$PWD\.venv\Scripts\python.exe"
& $PY -m pip install -r docker\requirements.txt
& $PY -m pytest          # macOS result at the v1 commit: "291 passed". Write down the last line.
```

- If Python 3.14 is not installed, install it from python.org first. The pins are not verified on older versions.
- If some tests fail on Windows, write down the failing test names and continue with F3.
  Stop only if `pip install` fails or preflight fails.

## F3. Every new PowerShell window

```powershell
cd $HOME\Desktop\redline
$PY = "$PWD\.venv\Scripts\python.exe"
$env:PYTHONUTF8 = "1"          # UTF-8 for output and files, so Windows code pages cannot crash a run
$env:PYTHONUNBUFFERED = "1"    # progress lines appear at once
```

## F4. Per model: pull, preflight, smoke run (about 2 min)

Set the model tag exactly as `ollama list` shows it. Start with `qwen2.5:7b-instruct` (the reference model).
Never guess a tag: if `ollama pull` fails, the tag is wrong.

```powershell
$env:LAB_MODEL = "qwen2.5:7b-instruct"
ollama pull $env:LAB_MODEL
& $PY scripts\preflight.py     # must end "PREFLIGHT OK -- ready for a LIVE run"; note the digest line
$SLUG  = $env:LAB_MODEL -replace '[^A-Za-z0-9._-]', '-'
$STAMP = (Get-Date).ToUniversalTime().ToString("yyyyMMdd'T'HHmm'Z'")
& $PY scripts\run_governance.py --live --sample 1 --reps 1 --out "results\governance\${STAMP}_${SLUG}_smoke"
```

**Tool-support check.** The smoke run made three new locked folders in `results\runs\`. Check them:

```powershell
$new = Get-ChildItem results\runs -Directory | Sort-Object Name | Select-Object -Last 3
foreach ($r in $new) {
  $m = Get-Content "$($r.FullName)\manifest.json" -Raw | ConvertFrom-Json
  "$($r.Name)  locked=$($m.locked)  complete=$($m.complete)  mean_s=$($m.seconds_per_run.mean)"
  Import-Csv "$($r.FullName)\runs.csv" | Select-Object test_id, rep, tool_calls, error | Format-Table -AutoSize
  Select-String -Path "$($r.FullName)\traces\*.json" -Pattern "<tool_call>" -SimpleMatch | Select-Object -First 2
}
```

The model **supports tool calls** only if all three are true:
1. At least one row has a real call in `tool_calls` (for example `read_doc`).
2. `Select-String` prints nothing. If it prints `<tool_call>` lines, the model wrote its calls as text.
3. Not every row has an `error`.

If any check fails, write "no tool support" for this model and skip its full run.
A model without tool calls would read as a false "safe 0%".

**Time estimate:** `mean_s` × 414 ÷ 60 = minutes for the full run of this model (414 model runs).
The first run includes loading the model, so this estimate is a little high.

## F5. Per model: the full run (one at a time)

```powershell
$STAMP = (Get-Date).ToUniversalTime().ToString("yyyyMMdd'T'HHmm'Z'")
New-Item -ItemType Directory -Force results\logs | Out-Null
& $PY scripts\run_governance.py --live --sample 26 --reps 3 --out "results\governance\${STAMP}_${SLUG}" | Tee-Object -FilePath "results\logs\${STAMP}_${SLUG}.log"
```

- This runs RC-001 (no defences), RC-002 (D2) and RC-003 (D2 + D3): 26 attacks + 20 benign,
  3 reps each, 138 runs per candidate, 414 model runs.
- Check the GPU in a second window: `ollama ps` (the PROCESSOR column should say GPU).
- If time is short, use `--sample 17` instead of `--sample 26`: 306 model runs instead of 414,
  and 51 attack runs per candidate (the gate's minimum is 50).
- Ctrl-C stops a run. The partial folder stays and is never reused. Start again with a new `$STAMP`.
- When the run ends, go back to F4 for the next model.

## F6. Check a finished run

For each of its three folders in `results\runs\`:
- `manifest.json` has `"locked": true` and `"complete": true`.
- `summary.csv`: `attack_success_rate` has `n` = 78, `benign_task_success` has `n` = 60
  (with `--sample 17`: 51 and 51). Note `error_runs`.
- `manifest.json` `model_digest` equals the digest that preflight printed.

The verdicts are in `results\governance\<STAMP>_<SLUG>\gate_RC-00*.txt`. Every file starts with `[LIVE]`. Keep that label when you quote a number.

## F7. Send the results

First, restore the corpus files (only when no run is in progress). On v2 code this changes nothing:

```powershell
git checkout -- corpus
git status --short             # only results\ entries may be listed
```

**Option A — push a branch** (if you have push access to the repo):

```powershell
$RSTAMP = (Get-Date).ToUniversalTime().ToString("yyyyMMdd'T'HHmm'Z'")
git switch -c "results/$RSTAMP"
git add results/runs results/governance
git commit -m "results: full LIVE runs $RSTAMP"
git push -u origin "results/$RSTAMP"
```

After this you are on a `results/` branch. Later pulls: use case B in V3.

**Option B — zip** (always do this as a byte-exact backup):

```powershell
Compress-Archive -Path results\runs, results\governance, results\logs -DestinationPath "$HOME\Desktop\redline_results.zip"
```

The files contain paths such as `C:\Users\<your name>\...`. The repo is public. If you do not want
your Windows user name to be public, use option B only and give the zip to the owner.

`NOTES.txt` holds, per model: tag, digest, tool support (yes/no and why), `mean_s`, run folder names,
`error_runs`, and anything unusual. Add the `pytest` last line, `ollama --version` and the GPU name.
