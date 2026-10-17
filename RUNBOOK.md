# Runbook — attack, governance & assurance

A step-by-step guide to reproduce the study end to end:

1. **Attack** — fire the injection corpus at the insecure agent and measure how
   often it breaks (the *before* numbers).
2. **Governance & assurance** — turn those numbers into a release decision, a
   risk register, an audit trail, and a manager summary.

All commands are run **from the repo root** (the folder that holds this file,
`README.md` and `lab/`):

```bash
cd Sotonship-2026
```

The pipeline runs from the `.py` files (`lab/`, `scripts/`, `python -m lab.app`).
The notebooks under `notebooks/` are prototypes and are not part of it.

---

## 0. One-time prerequisites

### Environments

Two Python environments are used, both created inside the repo (see README
"Quick start" and "Attack with PyRIT"):

| Env | Interpreter | Used for |
| --- | --- | --- |
| `.venv` | `.venv/bin/python` | the tests, the menu app and the governance program (`docker/requirements.txt`) |
| `.venv-pyrit` | `.venv-pyrit/bin/python` | the PyRIT-driven attack experiment; it also has every lab dep, so it can run the governance program too (`docker/requirements.txt` + `docker/requirements-pyrit.txt`) |

> This runbook uses **`.venv-pyrit/bin/python`** throughout so one interpreter
> covers both halves. `.venv/bin/python` works for every command except
> `scripts/run_pyrit_attack.py`.

### Ollama + model (needed for the live attack and `--live` governance)

```bash
ollama pull qwen2.5:7b-instruct        # once
curl -s http://localhost:11434/api/tags | grep qwen2.5   # should print the model
```

If the server is not running, start it in another terminal with `ollama serve`.

---

## 1. Run the attack experiment

This drives every corpus attack through the insecure agent, scores each run with
the deterministic oracle, and writes the results.

### Step 1.1 — Quick rehearsal (fast, proves the pipeline)

First 12 tests, 2 reps each:

```bash
.venv-pyrit/bin/python scripts/run_pyrit_attack.py --sample 12 --reps 2
```

### Step 1.2 — Full run (the headline numbers)

Whole test split, 3 reps, labelled as candidate RC-001:

```bash
.venv-pyrit/bin/python scripts/run_pyrit_attack.py --candidate RC-001
```

Useful flags: `--split dev|test`, `--reps N`, `--sample N`, `--temperature T`,
`--candidate LABEL` (default `adhoc`).

### Step 1.3 — Read the attack results

The script prints the overall ASR with its 95% confidence interval and a
per-technique table (worst first).

Each run is saved to its own locked folder `results/runs/<run_id>/` (run_id =
UTC time + model tag + candidate, e.g.
`20261017T171253Z_qwen2.5-7b-instruct_RC-001`): `manifest.json` (model +
digest, driver, corpus/rules/policy hashes, git commit, split, reps, timings,
sha256 of every file, `locked: true`), `records.jsonl` (the exact records
run), `traces/<test_id>__rep<k>.json`, `runs.csv` (one row per run),
`summary.csv`. Files are read-only; nothing writes into or deletes a saved
run. The script checks `corpus/*.jsonl` against the generator and stops if
they differ (it never rewrites them). Use `--candidate RC-001` to label the
folder. Read it with:

```bash
cat results/runs/<run_id>/summary.csv
```

The committed `results/summary.csv` and `results/runs.csv` are the 24-run
rehearsal and are never overwritten.

Reading the CSVs in code: open `runs.csv` with
`pd.read_csv(path, keep_default_na=False)`, or empty `error` cells become NaN
and every run counts as an error. Rates in the CSVs are fractions 0–1
(`docs/DATA_CONTRACT.md`).

### Headline runs

The committed `results/summary.csv` is a 24-run rehearsal (12 attack tests ×
2 reps, benign not measured). The headline run is the full test split at
3 reps: 26 attack tests × 3 = 78 attack runs and 20 benign × 3 = 60 benign
runs. Its results are **pending**. The commands:

```bash
# 1. RC-001, the insecure baseline: full test split, 3 reps, saved and locked
.venv-pyrit/bin/python scripts/run_pyrit_attack.py --candidate RC-001
cat results/runs/<run_id>/summary.csv

# 2. gate + assurance from that saved run. RC-002 (D2) and RC-003 (D2+D3) are
#    deterministic replays of the same traces unless you pass saved run folders.
#    Output: governance_out/cli_demo/
.venv-pyrit/bin/python scripts/run_governance.py --run results/runs/<run_id> \
    [--run-after results/runs/<d2_run_id>] [--run-after-d3 results/runs/<d2d3_run_id>]
```

---

## 2. Run the governance & assurance program

This is `scripts/run_governance.py` — the pipeline program
(`notebooks/release_gate_demo.ipynb` is its prototype). It runs the whole
release-gate and assurance story and writes every artifact to an output folder.

It has two modes:

- **Demo (default)** — offline and deterministic. Candidate runs are rebuilt
  from calibrated per-technique attack rates (the `RATES` table at the top of
  the script), so the governance story is reproducible with no model.
- **Live (`--live`)** — drives the real Ollama agent for the candidate runs.

### Step 2.1 — Run it (demo mode, offline)

```bash
.venv-pyrit/bin/python scripts/run_governance.py
```

It prints the full report and writes the artifacts into
`governance_out/cli_demo/` (the default `--out`). The menu app's own state in
`governance_out/` (its `audit_log.jsonl`, candidates and roles) is never
touched. If `--out` already holds an `audit_log.jsonl`, the CLI rotates it to
`audit_log.<UTC stamp>.jsonl` (never deleted) and starts a new chain, unless
you pass `--keep-log` to append to it.

### Step 2.2 — (optional) Run it against the live model

Make sure Ollama is up (see §0), then:

```bash
.venv-pyrit/bin/python scripts/run_governance.py --live --sample 12
```

> **Live-gate note (D-08 / X-05).** Policy `min_attack_runs: 50`; at 3 reps a
> live gate needs at least 17 attack tests (`--live --sample 17` or more; the
> default sample of 12 gives 36 attack runs and FAILs on `min_attack_runs`).
> With fewer, the RC-003 PASS in the story below cannot happen live. The app's
> LIVE default sample of 8 gives 24 runs. Duration of a 17-test run is not
> measured.

### Step 2.3 — Read the governance artifacts

```bash
ls governance_out/cli_demo/
```

| File | What it is |
| --- | --- |
| `RC-001.json`, `RC-002.json`, `RC-003.json` | candidate fingerprints (exact ID card of what was tested) |
| `gate_RC-001.txt`, `gate_RC-002.txt`, `gate_RC-003.txt` | the PASS/FAIL gate verdicts |
| `RC-001/runs.csv`, `RC-002/runs.csv`, `RC-003/runs.csv` | one row per scored run for each candidate |
| `audit_log.jsonl` | the hash-chained, tamper-evident decision log (a previous log is rotated to `audit_log.<UTC stamp>.jsonl`, never deleted) |
| `inventory_card.md` | the AI inventory card |
| `risk_register_before.md` | risks for the insecure agent (RC-001) |
| `risk_register_RC-002.md` | risks after D2 alone (RC-002): leaks closed, unrequested tickets still open |
| `risk_register_RC-003.md`, `risk_register_after.md` | risks after D2+D3 (RC-003), closed as Mitigated (`_after` is the same register) |
| `fix_tickets.md` | drafted fix tickets |
| `evidence_register_RC-002.md`, `evidence_register_RC-003.md`, `evidence_register.md` | control → proof → file, per re-test (`evidence_register.md` = RC-003) |
| `traceability.md` | one finding followed start to finish |
| `manager_summary.md` | the one-page manager view |
| `audit_log_tampered_demo.jsonl` | a forged copy, used only to prove detection |

Read any of them, e.g.:

```bash
cat governance_out/cli_demo/manager_summary.md
cat governance_out/cli_demo/risk_register_after.md
```

### What the program does, step by step (printed as it runs)

The DEMO numbers below are scripted from the `RATES` table (see §3), not
measured; benign success is 60/60 by construction in all three candidates.

1. **RC-001 — the insecure agent.** Build its fingerprint, measure it, gate it
   against `lab/governance/policy.yaml` → **FAIL** (DEMO: 33/78 = 42.3%, far
   above the 5% limit).
2. **Audit + decision.** Record the submission and verdict. The engineer who
   submitted it is **refused** self-review; the reviewer **BLOCKs** it with a
   reason. (A FAIL can never be APPROVED.)
3. **Assurance (before).** Build the AI inventory card, the risk register
   (likelihood = the *measured* attack rate), and draft fix tickets.
4. **RC-002 — defence D2 enforced.** New fingerprint (so RC-001's decision does
   **not** carry over — the re-test rule). Re-measure → attack success drops
   (DEMO: 15/78 = 19.2%) but still **FAIL** → the reviewer **BLOCKs** it:
   "D2 stops the leaks, but unrequested P1 tickets still land. Add D3."
   `risk_register_RC-002.md` and `evidence_register_RC-002.md` show which
   risks D2 closed and which stayed open.
5. **RC-003 — D2 + D3 enforced.** New fingerprint again. Re-measure (DEMO:
   0/78 = 0.0%) → **PASS**, and the reviewer **APPROVEs** it.
6. **Assurance (after).** The re-test closes the covered risks as **Mitigated**
   (`risk_register_RC-003.md` = `risk_register_after.md`) and fills the
   evidence register.
7. **Traceability.** Stitch one finding from first attack → risk → control →
   fix → re-test → approval → closed status.
8. **Manager summary.** The one-page view: live version, current attack rate,
   open risks, last decision. Headline: `attack success 42.3% -> 19.2% (D2)
   -> 0.0% (D2+D3)` (DEMO).
9. **Tamper-evidence.** Verify the real audit log (intact), then prove a forged
   copy is caught at the exact entry that was changed. The real log is left
   untouched. To run the check by hand on any log file (always on a **copy**,
   never the real log):

   ```bash
   .venv-pyrit/bin/python scripts/verify_log.py governance_out/cli_demo/audit_log_tampered_demo.jsonl
   ```

   Exit codes: `0` intact, `1` tampered (the output names the entry), `2`
   usage. (Script added in the review pass; see README "Honest limits" for
   what the chain does and does not catch.)

### All options

```bash
.venv-pyrit/bin/python scripts/run_governance.py --help
```

Key flags:

- `--out DIR` — write artifacts somewhere other than `governance_out/cli_demo/`.
- `--reps N` — repetitions per test.
- `--engineer NAME` / `--reviewer NAME` — roles (must match `policy.yaml`).
- `--keep-log` — append to an existing audit log instead of rotating it.
- `--no-tamper-demo` — skip the tamper-evidence demonstration.
- `--run RUN_DIR` — gate a saved, locked run folder (`results/runs/<id>`) as
  RC-001 instead of DEMO/LIVE (`--run` with `--live` is refused).
- `--run-after RUN_DIR` — with `--run`: a saved D2 run folder as RC-002;
  without it RC-002 is a deterministic D2 replay of `--run`.
- `--run-after-d3 RUN_DIR` — with `--run`: a saved D2+D3 run folder as RC-003
  (a D2-only folder is refused, exit 2); without it RC-003 is a D2+D3 replay.

---

## 3. Keeping the demo honest

In **demo mode** the governance numbers come from the calibrated `RATES` table
in `scripts/run_governance.py` (and `lab/demo.py` for the app), not from the
attack run in §1. Those rates were calibrated to the committed 24-run
rehearsal (`results/summary.csv`: n = 4 per technique); `T7_split` has no
`RATES` entry and defaults to 0, and DEMO benign success is 100% by
construction, not measured. If you re-run §1 and the measured per-technique
rates change, update `RATES` to match — or run the governance program with
`--live` (or `--run` on a saved folder) so it measures the agent directly. The
DEMO story's numbers are RC-001 33/78 = 42.3%, RC-002 (D2) 15/78 = 19.2%,
RC-003 (D2+D3) 0/78 = 0.0%, benign 60/60 in all three — scripted. Say "DEMO,
scripted" next to any DEMO number you show.

---

## 4. Quick reference — the whole thing in four commands

```bash
cd Sotonship-2026

# 1. attack (measure how often the insecure agent breaks); saved + locked
.venv-pyrit/bin/python scripts/run_pyrit_attack.py --candidate RC-001
cat results/runs/<run_id>/summary.csv

# 2. governance + assurance from the saved run (decision + records)
.venv-pyrit/bin/python scripts/run_governance.py --run results/runs/<run_id> \
    [--run-after results/runs/<d2_run_id>] [--run-after-d3 results/runs/<d2d3_run_id>]

# 3. read the headline outputs
cat governance_out/cli_demo/manager_summary.md
```
