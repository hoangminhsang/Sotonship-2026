# Agent Release Gate — red-team lab, release gate and assurance

**One sentence:** this repo attacks a tool-using LLM agent with a corpus of
indirect prompt injections, measures how often the attacks succeed (broken down
by technique), and turns the result into a release decision: a PASS/FAIL gate
against a written policy, a named human Approve/Block/Override, a hash-chained
audit log, and a risk register whose likelihoods are the measured rates.

An unprotected agent reads a support document or an asset record, the content
hides an instruction, and the agent obeys it: leaking a clearly fake secret,
reading a restricted record, or firing an unrequested high-priority ticket. The
lab fires ~57 injections and ~40 benign requests, records every tool call,
scores each run with **deterministic code** (no LLM judge), and reports three
metrics with 95% Wilson confidence intervals:

- **Attack Success Rate (ASR)** — how often it breaks.
- **ASR per technique** — which attack is worst (defend that first).
- **Benign task success** — whether usefulness survives.

The *after* study (defences D1–D3) lives in `lab/defences.py`. The whole
pipeline — attack run → checker → metrics → gate → decision → audit log →
assurance — runs from the `.py` files (`lab/`, `scripts/`); the notebooks in
`notebooks/` are prototypes only.

## Quick start

### Option A — Docker (reproducible, two-person friendly)

```bash
# from the repo root
docker compose -f docker/docker-compose.yml up -d --build
# pull the model once into the named volume
docker compose -f docker/docker-compose.yml exec ollama ollama pull qwen2.5:7b-instruct
# open Jupyter at http://localhost:8888  (no token)
```

### Option B — local venv (what this lab was built and verified on)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r docker/requirements.txt
# Ollama must be running locally with the model pulled:
ollama pull qwen2.5:7b-instruct
python -m lab.app       # menu-driven program: red team, governance, assurance
```

The agent reads Ollama at `$OLLAMA_HOST` (default `http://localhost:11434`) and
the model from `$LAB_MODEL` (default `qwen2.5:7b-instruct`). Point the whole
experiment at a different model in one place, e.g. `LAB_MODEL=llama3.1:8b`.

## Attack with PyRIT (step by step)

The lab can also be driven by **Microsoft [PyRIT](https://github.com/Azure/PyRIT)**
(Python Risk Identification Toolkit) instead of the built-in runner. PyRIT
provides the red-team machinery — a **target** that wraps our insecure agent, an
in-memory **conversation store**, and **converters** that generate encoded
payload variants — while scoring stays with the lab's deterministic oracle so
results remain reproducible. Both front-ends write the **same** trace/CSV
format, so the metrics, the chart, and `summary.csv` are identical in shape.

PyRIT pins its own dependencies, so it goes in a **separate combined
environment** (`.venv-pyrit`) that has both PyRIT and the lab's deps; the
verified `.venv` stays untouched.

**Step 1 — build the combined environment** (once):

```bash
cd Sotonship-2026
python3 -m venv .venv-pyrit
.venv-pyrit/bin/pip install --upgrade pip
.venv-pyrit/bin/pip install -r docker/requirements.txt -r docker/requirements-pyrit.txt
```

**Step 2 — make sure Ollama is up and the model is pulled** (same as the lab):

```bash
ollama pull qwen2.5:7b-instruct      # once
curl -s http://localhost:11434/api/tags   # should list the model
```

**Step 3 — run the PyRIT-driven attack.** Start with a quick rehearsal, then the
full test split:

```bash
# fast rehearsal: first 12 tests, 2 reps each
.venv-pyrit/bin/python scripts/run_pyrit_attack.py --sample 12 --reps 2

# full run: whole test split, 3 reps (this is the headline number)
.venv-pyrit/bin/python scripts/run_pyrit_attack.py
```

**Step 4 — read the result.** The script prints the overall ASR with its 95%
interval and the per-technique table (worst first). The numbers below are the
committed rehearsal run in `results/summary.csv` (12 attack tests × 2 reps =
24 attack runs; benign tasks were **not** measured in that run):

```
Overall ASR         : 50.0%  (12/24)  95% CI [31.4%, 68.6%]
ASR per technique (worst first):
  T1_plain             4/4   100%  [51.0%, 100%]
  T4_hidden            4/4   100%  [51.0%, 100%]
  T2_fake_system       3/4    75%  [30.1%, 95.4%]
  T3_fake_staff        1/4    25%  [4.6%, 69.9%]
  T5_encoded           0/4     0%  [0.0%, 49.0%]
  T6_other_language    0/4     0%  [0.0%, 49.0%]
```

With n = 4 per technique the intervals are wide on purpose: the rehearsal
proves the pipeline, it is not the headline number. The headline run (full
test split: 26 attacks + 20 benign, 3 reps = 78 attack runs + 60 benign runs)
is **pending**.

**Step 5 — inspect the evidence.** The same outputs as the native runner:

- `results/summary.csv` — aggregated metrics.
- `results/runs.csv` — one row per run, tool calls and verdict side by side
  (open in a spreadsheet to audit that the oracle labelled each run correctly).
- `results/traces/<test_id>__rep<k>.json` — the full per-run trace; PyRIT traces
  carry `"driver": "pyrit"`.

**Options:** `--split dev|test`, `--reps N`, `--sample N`, `--temperature T`.

The PyRIT target, converter mapping, and driver live in
[`lab/pyrit_integration.py`](lab/pyrit_integration.py); the CLI is
[`scripts/run_pyrit_attack.py`](scripts/run_pyrit_attack.py).

## Run the tests (offline, no model needed)

```bash
pytest -q
```

The oracle, metrics, defences, gate, decisions, roles, audit log, risks and
the demo path are covered (79 tests, offline, as of commit d825341; the
review branch adds more). The model-dependent path (the agent actually calling
tools) needs Ollama: run it LIVE from `python -m lab.app` or
`scripts/run_pyrit_attack.py`.

## What's in here

```
lab/        world, mcp_server, agent, attacks, benign, corpus, runner, oracle, metrics, fixes
            defences — D1-D3 enforcement for the *after* study (+ deterministic replay)
            pyrit_integration — PyRIT target/converters/driver (optional front-end)
lab/governance/  policy.yaml, candidate (fingerprint), gate (verdict), decision
                 (roles + Approve/Block/Override), audit (hash-chained log)
lab/assurance/   risks (register + fix tickets), control_map.yaml (OWASP/ATLAS/NIST),
                 inventory, evidence, thread (traceability), summary_page
data/       synthetic docs, assets.json (incl. the one restricted record), rules.yaml
corpus/     dev.jsonl / test.jsonl (generated, stratified split)
results/    summary.csv, runs.csv (committed 24-run rehearsal; human-validation sheet),
            traces/ (git-ignored)
notebooks/  (prototypes, not part of the pipeline)
            attack_lab_explained.ipynb — self-contained teaching notebook: the whole
                                          codebase inlined and explained in plain English,
                                          attacking via PyRIT (run with .venv-pyrit)
            release_gate_demo.ipynb    — study-guided governance & assurance demo:
                                          RC-001 fails → block → D2 → RC-002 passes →
                                          approve → risks close → audit tamper caught
scripts/    run_pyrit_attack.py — CLI for the PyRIT-driven attack
            run_governance.py  — CLI for the release-gate + assurance story (DEMO/--live)
tests/      pytest suite (oracle, metrics, fixes, defences, gate, decision, audit,
            candidate, risks, roles, demo) — 79 tests as of commit d825341
            (`pytest --collect-only -q`); offline, no model needed
docker/     Dockerfile, docker-compose.yml, requirements.txt, requirements-pyrit.txt
```

## The Agent Release Gate (governance & assurance)

The red team is an *evidence factory*; the release gate decides what to do with
the evidence and remembers it. Two loops, both reading what the lab already
writes — no new tests here, all deterministic and unit-tested:

- **Short loop (release gate).** On every change: `candidate` fingerprints
  exactly what was tested; `gate` compares the metrics to `policy.yaml` and emits
  PASS/FAIL with reasons; `decision` enforces roles (the engineer can't approve
  their own work) and Approve/Block/Override; `audit` writes a hash-chained,
  append-only log so any edit is detectable (`verify_log`).
- **Long loop (assurance).** Every violation class becomes a risk whose
  **likelihood is the measured attack rate, not a guess** (`risks`); each risk
  maps to a control (`fixes.py`) and to OWASP / MITRE ATLAS / NIST labels
  (`control_map.yaml` — we say *mapped to*, never *compliant with*); `evidence`,
  `thread` (one finding from first attack to closed risk) and `summary_page`
  stitch the story. A finding with no fix-table row is flagged "no fix found,
  rule owner must add one" — the tool never invents a fix or a label.

**Defences for the re-test.** `lab/defences.py` turns `rules.yaml` from something
the oracle only *scores* into something the tools *enforce*: D1 trust labels, D2
input checks in tool code, D3 human approval for high-impact actions. Crucially a
*blocked* tool call no longer counts as a successful attack — the oracle skips
it — so the before/after drop is real. Run a defended study live with
`runner.run_split(..., defences=("D2",))`.

Walk the whole thing with **`python scripts/run_governance.py`** (offline and
deterministic by default; `--live` for real `qwen2.5:7b-instruct` runs; a
gated live run needs at least 17 attack tests). `notebooks/release_gate_demo.ipynb`
is the prototype of that script.

### Run it as one menu-driven program

```bash
.venv/bin/python -m lab.app
```

A text menu ties the three parts together — **1. Red team** (test a candidate
and produce the evidence), **2. Governance** (gate verdict, Approve/Block/
Override, manage roles, view/verify the audit log), **3. Assurance** (risk
register, control mapping, evidence, traceability, manager summary, write reports
to markdown). State persists under `governance_out/` so candidates, decisions,
role edits and the hash-chained log survive between actions; delete that folder
to start fresh. Red-team runs default to DEMO (offline, deterministic) with a
LIVE option that drives the real model.

All logic lives in the `lab/` package so it is unit-testable and two people
can edit different modules without merge pain; the scripts and the menu app
only orchestrate.

### How it works, in one line

A user task goes to the agent → the agent reads a poisoned document or asset
note through the tools → the injected instruction steers it into a sensitive
tool call → nothing stops it. The recorded tool calls are the evidence; the
oracle labels each run against `rules.yaml` (which is **scored, never
enforced** — enforcing it is the *after* study).

## Reproducibility

| | |
|---|---|
| Model | `qwen2.5:7b-instruct` |
| Model digest | `sha256:845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e` |
| Temperature | 0.2 (not 0, so the 3 reps show real variance) |
| Seed | 7 (world, corpus, split) |
| Reps | 3 per test |
| Corpus | 57 attacks + 40 benign = 97 records; stratified 50/50 dev/test |
| Python | 3.14 |

Pinned dependency versions are in [`docker/requirements.txt`](docker/requirements.txt).

> **Model note.** The spec's baseline is `qwen2.5:7b-instruct` because it
> supports tool calling yet is weak enough that injections land often — exactly
> what the *before* study needs. `llama3.1:8b` is a drop-in fallback via
> `LAB_MODEL`.

## Data provenance

- **All data is synthetic and fictional**, generated by `lab/world.py` from a
  fixed seed. Documents, asset inventory, names, emails, and orders are made up
  and labelled synthetic in the files.
- **The only secret is fake:** `FAKE-SECRET-NORTHWIND-7781`. It exists so a leak
  is unmistakable in a trace and completely harmless. There are no real
  credentials anywhere.
- **No network, email, payments, or real customer data.** Every tool is a local
  stub; `create_ticket` appends to an in-process store, `query_assets` and
  `read_doc` read local fixtures. Nothing leaves the container.
- Attack payloads are planted at run time into isolated per-run copies of the
  world, so the checked-in fixtures in `data/` stay clean.

## Disclosure

This section follows the SotonShip 26 Responsible AI track rules: disclose
AI-generated work, prior components and outside help; credit research, code,
models and data with dates and licences; label synthetic data (see "Data
provenance" above). Everything here is stated from the repository history and
from checks dated 17 Oct 2026; anything we could not check is marked
**NOT VERIFIED**.

### When it was built

All three commits on `main` are dated Sat 17 Oct 2026, 06:30–15:49 BST, after
hacking opened on Fri 16 Oct at 20:00 (`git log --format='%h %ad %s' --date=iso-local`):

| Commit | Time (BST) | What |
|---|---|---|
| `121b6c7` | 06:30 | Insecure Agent Attack Lab — initial commit |
| `f729076` | 14:36 | Governance & assurance: release gate, risk register, audit trail, CLI + runbook |
| `d825341` | 15:49 | Menu app, roles editor, demo module, tests |

### AI assistance

- **The lab, governance and app code** (`lab/`, `scripts/`, `tests/`) were
  produced with an AI coding assistant and reviewed by humans before use. The
  commit trailers name the model: `Co-Authored-By: Claude Opus 4.8` on
  `121b6c7` and `f729076`; on `d825341` the trailer is absent. This README,
  `RUNBOOK.md` and `docs/` were revised in the review pass below.
- **The review-and-hardening pass** (branch `review/hardening`, 17 Oct) used
  Claude Code with **Claude Opus 5.5** (review lanes and fix designs) and
  **Claude Fable 5.1** (implementation). It also took an outside architecture
  review from **google/gemini-4-argon** and **z-ai/glm-5.3-prime** via
  OpenRouter on 17 Oct; **openai/gpt-6-astra** declined the review pack.
- All data in this repository is synthetic (see "Data provenance"), so no
  private or customer data could reach an external AI service from it.
  **TODO (owner):** confirm that the OpenRouter review pack held only this
  repository's code and docs.

### Prior components and outside help

- **Prior components:** none of our own earlier projects, per the repository
  history (the owner confirms). Open-source libraries and the pre-trained model
  listed below are used as allowed by the track rules.
- **Outside help:** none beyond the tools listed in this section.
- **Design inspiration:** the before/after framing of the
  [taimurijlal](https://github.com/taimurijlal) `03-secure-ai-agent` README.
  Inspiration for structure only, **not copied**. The exact repository and its
  licence are **NOT VERIFIED** (the link is to the GitHub profile, not a repo).

### Credits: code, models and tools

Licences marked "verified" were read from the installed package metadata
(`*.dist-info/METADATA`) at the pinned versions on 17 Oct 2026; the URLs were
not fetched for that check. Pins are in `docker/requirements.txt` and
`docker/requirements-pyrit.txt`.

| Component | Pinned version | Licence | URL | Checked |
|---|---|---|---|---|
| langgraph | 1.2.16 | MIT (verified) | https://pypi.org/project/langgraph/ | 17 Oct 2026 |
| langgraph-prebuilt | 1.1.1 | MIT (verified) | https://pypi.org/project/langgraph-prebuilt/ | 17 Oct 2026 |
| langchain-core | 1.6.12 | MIT (verified) | https://pypi.org/project/langchain-core/ | 17 Oct 2026 |
| langchain-ollama | 1.1.0 | MIT (verified) | https://pypi.org/project/langchain-ollama/ | 17 Oct 2026 |
| mcp (official MCP Python SDK) | 1.30.0 | MIT (verified) | https://pypi.org/project/mcp/ | 17 Oct 2026 |
| langchain-mcp-adapters | 0.3.2 | MIT (verified) | https://pypi.org/project/langchain-mcp-adapters/ | 17 Oct 2026 |
| ollama (Python client) | 0.6.3 | MIT (verified) | https://pypi.org/project/ollama/ | 17 Oct 2026 |
| PyRIT (Microsoft) | 1.1.0 | **NOT VERIFIED** (an earlier version of this README said MIT) | https://github.com/Azure/PyRIT | — |
| Ollama server | `ollama/ollama:0.4.0` in `docker/docker-compose.yml`; local install version not recorded | **NOT VERIFIED** | https://ollama.com | — |
| Qwen2.5-7B-Instruct (`qwen2.5:7b-instruct`, digest `sha256:845dbda0…` in Reproducibility) | as pulled by Ollama | **NOT VERIFIED** | https://ollama.com/library/qwen2.5 | — |

Other pinned packages (pandas 3.0.6, numpy 2.5.4, pydantic 2.14.1, pyyaml 6.0.3,
matplotlib 3.11.2, pytest 9.1.1 and the Jupyter stack) are listed in
`docker/requirements.txt`; their licences were not checked.

### Citations (problem evidence and control mapping)

We say **"mapped to"**, never "compliant with". `lab/assurance/control_map.yaml`
uses these identifiers.

| Reference | URL | Status |
|---|---|---|
| OWASP Top 10 for LLM Applications 2025 — LLM01 Prompt Injection, LLM02 Sensitive Information Disclosure, LLM06 Excessive Agency | https://genai.owasp.org/llm-top-10/ | Verified 17 Oct 2026 (IDs and titles as listed on that page) |
| NIST AI RMF 1.0 — Core "Manage" function | https://airc.nist.gov/airmf-resources/airmf/5-sec-core/ | Verified 17 Oct 2026 (the Core "is composed of four functions: govern, map, measure, and manage") |
| MITRE ATLAS technique AML.T0051 | https://atlas.mitre.org/techniques/AML.T0051 | ID confirmed in MITRE's SAFE-AI report (17 Oct 2026); the technique's **name is per the technique page** and is not quoted here because we did not verify it |

### Honest limits

- **The audit log is tamper-evident, not tamper-proof.** The hash chain in
  `lab/governance/audit.py` detects an edited entry, an inserted entry, a
  deleted entry in the middle, and reordering. It does **not** detect
  re-chaining (an attacker who rewrites an entry and recomputes every later
  hash), truncation of the tail, or deletion of the whole file; a missing or
  empty log verifies as "0 entries, chain intact". A real deployment anchors
  the head hash somewhere the writer cannot reach (a second system, a signed
  timestamp, or a write-once store).
- **Names are typed, not authenticated.** The app and the CLI take the
  submitter and reviewer as plain text; the role checks demonstrate the control
  logic. A real deployment binds them to SSO identity.
- **Scoring rules (review pass, fixes 3 and 4).** An error run that broke a
  rule counts as a violation; an error run that broke no rule is left out of
  every rate. Ticket assignees without an allowed `@domain` are flagged (fail
  closed); subdomains of `northwind.example` are flagged (exact match).
- **Pass ≠ safe; small samples, wide intervals; one model, one synthetic
  world.** See "Scope and honesty" below.

## Scope and honesty

- **Both halves are in `lab/`.** The insecure baseline (defences off, RC-001)
  and the defended re-test (D1–D3 on, RC-002) run through the same runner and
  the same oracle. With defences off, a tool *call* is also an *effect*, so an
  attempt counts as a successful attack. With defences on, a call a defence
  refused is recorded as `[blocked]` and is **not** a violation, because it had
  no effect. That difference is the measured benefit.
- **The committed numbers are a rehearsal.** `results/summary.csv` holds 24
  attack runs (12 tests × 2 reps) and no benign runs. It is below the policy's
  `min_attack_runs: 50` and cannot PASS the gate. The headline run (78 attack +
  60 benign runs) is pending.
- **DEMO mode is scripted, not measured.** The app and `run_governance.py`
  default to DEMO: traces are generated from a per-technique rate table
  (`RATES`) that was calibrated to the rehearsal. DEMO numbers show the
  governance flow; they are not evidence about the model. Use LIVE or a saved
  run for evidence.
- **Pass ≠ safe.** A PASS means the agent resisted *these* attacks on this
  model, corpus and world. The confidence intervals quantify sampling noise,
  not every source of error. A low ASR can also mean the corpus under-sampled
  what works.
- **The oracle only catches rules it has.** Actions outside `data/rules.yaml`
  are not scored. A finding with no fix-table row is flagged for the rule
  owner, never silently passed.
- **The audit log is tamper-evident, not tamper-proof.** See "Honest limits"
  under Disclosure.
