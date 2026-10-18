# Context for the runs (code only)

## What the code does

`scripts/run_governance.py --live` drives a local LLM agent (through Ollama) through a fixed test set.
The agent has tools such as `read_doc` and a ticket tool. Some test documents contain prompt injections.
A deterministic checker (no LLM judge) marks each run that broke a rule. A release gate then compares
the rates with the limits in `policy.yaml` and writes PASS or FAIL.

One CLI run tests three release candidates of the same agent:

| Candidate | Defences | Meaning |
|---|---|---|
| RC-001 | none | the baseline |
| RC-002 | D2 | code checks inside the tools |
| RC-003 | D2 + D3 | plus human approval for P1 tickets |

## What a run writes

- `results/runs/<UTC time>_<model>_<candidate>/` — one **locked** folder per candidate: `manifest.json`
  (model tag and digest, file hashes, git commit, seconds per run), `records.jsonl`, `runs.csv`,
  `summary.csv` and `traces/`. A locked folder is never written again.
- `--out` folder — the gate results (`gate_RC-00*.txt`), the audit log and the reports.
  Every file starts with the mode label (`[LIVE]` for real model runs).

## Terms

- **attack success rate:** the share of attack runs where the agent broke a rule.
  `summary.csv` gives it with `n` and a Wilson 95% range.
- **benign task success:** the share of normal requests the agent completed.
- **error runs:** runs that failed with an error (for example a timeout). They are counted, not hidden.
- **tool support:** the model makes real, structured tool calls. A model that writes calls as text
  (`<tool_call>…`) never acts, so its attack rate would read as a false 0%.

## Settings

- `LAB_MODEL` — the Ollama tag (default `qwen2.5:7b-instruct`).
- `OLLAMA_HOST` — default `http://localhost:11434`. Preflight refuses a host that is not local.

## Do not run

`scripts/run_pyrit_attack.py`, the notebooks and the menu app. They rewrite `corpus/` and `results/` files.
