# Runbook — attack, governance & assurance

A step-by-step guide to reproduce the study end to end:

1. **Attack** — fire the injection corpus at the insecure agent and measure how
   often it breaks (the *before* numbers).
2. **Governance & assurance** — turn those numbers into a release decision, a
   risk register, an audit trail, and a manager summary.

All commands are run **from the repo root**:

```bash
cd /home/kali/Desktop/soton_ship_2026/agent-attack-lab
```

---

## 0. One-time prerequisites

### Environments

Two Python environments are used:

| Env | Interpreter | Used for |
| --- | --- | --- |
| `.venv-pyrit` | `.venv-pyrit/bin/python` | the attack experiment **and** the governance program (has the lab's pinned deps) |
| `msc_dissertation` (conda) | `/home/kali/miniconda3/envs/msc_dissertation/bin/python` | the notebooks; also fine for governance in demo mode |

> Throughout this runbook we use **`.venv-pyrit/bin/python`** because it carries
> the lab's verified dependency pins. If you prefer the conda env for the
> governance program in demo mode, it works too.

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

Whole test split, 3 reps:

```bash
.venv-pyrit/bin/python scripts/run_pyrit_attack.py
```

Useful flags: `--split dev|test`, `--reps N`, `--sample N`, `--temperature T`.

### Step 1.3 — Read the attack results

The script prints the overall ASR with its 95% confidence interval and a
per-technique table (worst first). It also writes:

- `results/summary.csv` — aggregated metrics.
- `results/runs.csv` — one row per run (tool calls + verdict side by side).
- `results/traces/<test_id>__rep<k>.json` — the full per-run trace.

```bash
cat results/summary.csv
```

---

## 2. Run the governance & assurance program

This is `scripts/run_governance.py` — the command-line twin of
`notebooks/release_gate_demo.ipynb`. It runs the whole release-gate and
assurance story and writes every artifact to an output folder.

It has two modes:

- **Demo (default)** — offline and deterministic. Candidate runs are rebuilt
  from calibrated per-technique attack rates (the `RATES` table at the top of
  the script), so the governance story is reproducible with no model.
- **Live (`--live`)** — drives the real Ollama agent for the candidate runs.

### Step 2.1 — Run it (demo mode, offline)

```bash
.venv-pyrit/bin/python scripts/run_governance.py
```

It prints the full report and writes the artifacts into `governance_out/`.

### Step 2.2 — (optional) Run it against the live model

Make sure Ollama is up (see §0), then:

```bash
.venv-pyrit/bin/python scripts/run_governance.py --live --sample 12
```

### Step 2.3 — Read the governance artifacts

```bash
ls governance_out/
```

| File | What it is |
| --- | --- |
| `RC-001.json`, `RC-002.json` | candidate fingerprints (exact ID card of what was tested) |
| `gate_RC-001.txt`, `gate_RC-002.txt` | the PASS/FAIL gate verdicts |
| `audit_log.jsonl` | the hash-chained, tamper-evident decision log |
| `inventory_card.md` | the AI inventory card |
| `risk_register_before.md` | risks for the insecure agent (RC-001) |
| `risk_register_after.md` | risks after the D2 fix (RC-002), closed as Mitigated |
| `fix_tickets.md` | drafted fix tickets |
| `evidence_register.md` | control → proof → file |
| `traceability.md` | one finding followed start to finish |
| `manager_summary.md` | the one-page manager view |
| `audit_log_tampered_demo.jsonl` | a forged copy, used only to prove detection |

Read any of them, e.g.:

```bash
cat governance_out/manager_summary.md
cat governance_out/risk_register_after.md
```

### What the program does, step by step (printed as it runs)

1. **RC-001 — the insecure agent.** Build its fingerprint, measure it, gate it
   against `lab/governance/policy.yaml` → **FAIL** (attack success far above the
   5% limit).
2. **Audit + decision.** Record the submission and verdict. The engineer who
   submitted it is **refused** self-review; the reviewer **BLOCKs** it with a
   reason. (A FAIL can never be APPROVED.)
3. **Assurance (before).** Build the AI inventory card, the risk register
   (likelihood = the *measured* attack rate), and draft fix tickets.
4. **RC-002 — defence D2 enforced.** New fingerprint (so RC-001's decision does
   **not** carry over — the re-test rule). Re-measure → attack success drops.
5. **Gate again → PASS**, and the reviewer **APPROVEs** it.
6. **Assurance (after).** The re-test closes the covered risks as **Mitigated**
   and fills the evidence register.
7. **Traceability.** Stitch one finding from first attack → risk → control →
   fix → re-test → approval → closed status.
8. **Manager summary.** The one-page view: live version, current attack rate,
   open risks, last decision.
9. **Tamper-evidence.** Verify the real audit log (intact), then prove a forged
   copy is caught at the exact entry that was changed. The real log is left
   untouched.

### All options

```bash
.venv-pyrit/bin/python scripts/run_governance.py --help
```

Key flags:

- `--out DIR` — write artifacts somewhere other than `governance_out/`.
- `--reps N` — repetitions per test.
- `--engineer NAME` / `--reviewer NAME` — roles (must match `policy.yaml`).
- `--keep-log` — append to an existing audit log instead of starting fresh.
- `--no-tamper-demo` — skip the tamper-evidence demonstration.

---

## 3. Keeping the demo honest

In **demo mode** the governance numbers come from the calibrated `RATES` table
in `scripts/run_governance.py`, not from the attack run in §1. Those rates were
originally measured from a real attack run. If you re-run §1 and the measured
per-technique rates change, update `RATES` to match — or run the governance
program with `--live` so it measures the agent directly.

---

## 4. Quick reference — the whole thing in four commands

```bash
cd /home/kali/Desktop/soton_ship_2026/agent-attack-lab

# 1. attack (measure how often the insecure agent breaks)
.venv-pyrit/bin/python scripts/run_pyrit_attack.py

# 2. governance + assurance (turn the result into a decision + records)
.venv-pyrit/bin/python scripts/run_governance.py

# 3. read the headline outputs
cat results/summary.csv
cat governance_out/manager_summary.md
```
