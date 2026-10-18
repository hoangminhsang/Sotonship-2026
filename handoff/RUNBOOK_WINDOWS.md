# Windows runbook — model runs on the friend's PC (v1, sequential)

Use PowerShell. Run the steps in order. Copy every result line into `NOTES.txt` (step 7 lists what to send).
Do **not** run `scripts\run_pyrit_attack.py` or the notebooks: they rewrite `corpus\` and `results\`.
Run **one model at a time**. Do not start a second run in another window (see `STATUS.md`).

## 0. Checks (2 min)

```powershell
ollama --version
ollama list              # note the exact tag of every model you will run
nvidia-smi               # the GPU is visible
py -0                    # installed Pythons; the pins in docker\requirements.txt were verified on 3.14
git --version
```

Set the PC to never sleep while it is plugged in (Settings → System → Power).

## 1. Get the code (once)

```powershell
cd $HOME\Desktop
git clone -c core.autocrlf=false https://github.com/hoangminhsang/Sotonship-2026.git redline
cd redline
git switch demo/integrated
git log --oneline -3     # top line "handoff: friend pack v1 ...", then 5e3062f, then fb0e275
```

`core.autocrlf=false` keeps every file byte-exact. The run folders store a hash of every file,
so a line-ending conversion would break them on the owner's Mac.

## 2. Python environment (once)

```powershell
py -3.14 -m venv .venv
$PY = "$PWD\.venv\Scripts\python.exe"
& $PY -m pip install -r docker\requirements.txt
& $PY -m pytest          # macOS result: "291 passed". Write down the last line.
```

- If Python 3.14 is not installed, install it from python.org first. The pins are not verified on older versions.
- If some tests fail on Windows, write down the failing test names and continue with step 3.
  Stop only if `pip install` fails or preflight fails.

## 3. Every new PowerShell window

```powershell
cd $HOME\Desktop\redline
$PY = "$PWD\.venv\Scripts\python.exe"
$env:PYTHONUTF8 = "1"          # UTF-8 for output and files, so Windows code pages cannot crash a run
$env:PYTHONUNBUFFERED = "1"    # progress lines appear at once
```

## 4. Per model: pull, preflight, smoke run (about 2 min)

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

## 5. Per model: the full run (one at a time)

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
- When the run ends, go back to step 4 for the next model.

## 6. Check a finished run

For each of its three folders in `results\runs\`:
- `manifest.json` has `"locked": true` and `"complete": true`.
- `summary.csv`: `attack_success_rate` has `n` = 78, `benign_task_success` has `n` = 60
  (with `--sample 17`: 51 and 51). Note `error_runs`.
- `manifest.json` `model_digest` equals the digest that preflight printed.

The verdicts are in `results\governance\<STAMP>_<SLUG>\gate_RC-00*.txt`. Every file starts with `[LIVE]`. Keep that label when you quote a number.

## 7. Send the results

First, restore the corpus files (only when no run is in progress):

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

**Option B — zip** (always do this as a byte-exact backup):

```powershell
Compress-Archive -Path results\runs, results\governance, results\logs -DestinationPath "$HOME\Desktop\redline_results.zip"
```

The files contain paths such as `C:\Users\<your name>\...`. The repo is public. If you do not want
your Windows user name to be public, use option B only and give the zip to the owner.

`NOTES.txt` holds, per model: tag, digest, tool support (yes/no and why), `mean_s`, run folder names,
`error_runs`, and anything unusual. Add the `pytest` last line, `ollama --version` and the GPU name.

## Later (v2, about 10:15)

The owner will ask you to pull v2 and start the dashboard bridge. Those commands will be in this folder then.
