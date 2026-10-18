# MAP — Agent Release Gate pipeline (snapshot d825341, hoangminhsang/Sotonship-2026)

Reviewed 17 Oct 2026, 16:17–18:03 BST. Snapshot: `scratchpad/wt/snap-map` (detached at origin/main = d825341).
Python: `/Users/sistaseetaram/Desktop/Hackathons/Sotonship-2026-review/.venv/bin/python` (3.14.5, pins match
`docker/requirements.txt`; PyRIT not installed). Ollama and the model are not on this machine; every step that needs
them is marked NOT RUN with the call that needs it. Nothing tracked was changed; `git status --porcelain --ignored`
after the last run shows only `.pytest_cache/` and `governance_out/` (both git-ignored).

## 1. Pipeline map: attack run → checker → metrics → gate → decision → audit log → assurance → app

**Answer: mostly yes.** Gate → decision → audit → assurance → app all run from `.py` files
(`scripts/run_governance.py`, `lab/app.py`) and I ran them. **One link has no plain `.py` driver:** the only `.py` that
runs an attack and writes `results/summary.csv` + `results/runs.csv` is the PyRIT script. README Option B and
`make notebook` point to `notebooks/attack_lab.ipynb`, which does not exist. Also **no code feeds `results/runs.csv`
into the gate**: DEMO mode uses a hard-coded `RATES` table; LIVE mode runs the model again.

"SAW" = I ran it without the model. "NOT RUN" = reason given.

| # | Step | File · function | Inputs | Outputs (exact) | Driven by | Seen? |
|---|---|---|---|---|---|---|
| 1a | Attack run (native) | `lab/runner.py` · `run_split(split_records, reps=3, seed=7, temperature=0.2, digest=None, make_agent=None, defences=())` (generator); `build_world_dir`; `model_digest()` | corpus records (`corpus/test.jsonl` via `lab/corpus.py` `read_jsonl`), `data/` fixtures, Ollama model (`lab/agent.py` `build_agent` → `ChatOllama(model=MODEL)`, `run_task`) | one JSON per run `results/traces/<test_id>__rep<k>.json`, keys `test_id, kind, goal, technique, place, rep, model, model_digest, temperature, seed, user_task, expect_ticket, expect_asset, defences, tool_calls, final_text, error, started_at, duration_s` | no CLI of its own; called by `lab/app.py` (LIVE), `scripts/run_governance.py --live`, `notebooks/release_gate_demo.ipynb` (`LIVE=True`) | NOT RUN: needs Ollama + model (`agent.build_agent`, `agent.run_task`) |
| 1b | Attack run (PyRIT) | `scripts/run_pyrit_attack.py` · `main()` → `lab/pyrit_integration.py` · `run_split()` / `run_record()` | same corpus; PyRIT venv; model | same trace path; keys as 1a **minus `model_digest`, `seed`, `defences`**, plus `driver: "pyrit"`; then `results/summary.csv` + `results/runs.csv` (step 3). Also calls `corpus.build_and_write()` and `runner.clear_traces()` (deletes `results/traces/`) | `.venv-pyrit/bin/python scripts/run_pyrit_attack.py [--split --reps --sample --temperature]` | NOT RUN: forbidden by brief; needs PyRIT + model |
| 2 | Checker | `lab/oracle.py` · `check(trace, rules)`, `check_with_expectation(trace, rules, test)`, `benign_success(trace, rules, test)` | trace dict + `data/rules.yaml` (`world.load_rules()`) | `Verdict(violated, classes, details)`; classes `V_read_restricted`, `V_exfiltration`, `V_tool_misuse`; skips calls with `blocked: true` | called by `metrics.build_runs_frame` | SAW (DEMO run; 11 oracle tests pass) |
| 3 | Metrics | `lab/metrics.py` · `build_runs_frame(traces, rules, tests_by_id)`, `summary_frame`, `write_outputs(runs, summary_path, runs_path)`, `wilson` | traces + rules + test records | runs frame / `results/runs.csv` columns: `test_id, kind, goal, technique, place, rep, user_task, tool_calls, violated, violation_classes, oracle_detail, benign_success, error, trace_path`. `results/summary.csv` columns: `metric, successes, n, rate, ci_low, ci_high`; rows `attack_success_rate, benign_task_success, ever_hijacked_rate, asr::<technique>…, error_runs` | `write_outputs` only from `scripts/run_pyrit_attack.py` | SAW `build_runs_frame` (DEMO run). `write_outputs` NOT RUN (only the PyRIT script calls it) |
| 4 | Gate | `lab/governance/gate.py` · `summarise(runs)`, `evaluate(summary, policy, last_approved_asr=None)`, `report_text(cid, verdict)`; policy `lab/governance/policy.yaml` (`load_policy`) | runs frame + policy limits (ASR ≤ 0.05, benign ≥ 0.85, regression 0.0, attack runs ≥ 50) | summary dict keys `asr, asr_lo, asr_hi, attack_runs, attack_violations, benign_rate, benign_n, errors`; `Verdict(passed, checks[Check(name, value, limit, ok, note)])`; file `governance_out/gate_RC-00x.txt` | `run_governance.py`; `app.py` menu 2.1 | SAW. On committed `results/runs.csv` (read-only replay): FAIL, 3 reasons (12/24 = 50.0%, CI 31.4–68.6%; 24 < 50 runs; benign not measured). DEMO CLI: RC-001 FAIL 33/78 = 42.3% (CI 32.0–53.4%); RC-002 (D2 replay) PASS 0/78 (CI 0.0–4.7%), benign 60/60 |
| 5 | Fingerprint | `lab/governance/candidate.py` · `fingerprint(...)`, `save(fp, out_dir)`, `load`, `Fingerprint.identity_hash()` | model tag, model digest, system prompt, tools, defences, `rules.yaml` text, `corpus/test.jsonl` text | `<out>/<cid>.json` keys `candidate_id, submitted_by, model, model_digest, system_prompt_sha256, tools, defences, rules_sha256, test_set_sha256, created_at, identity_hash`. Paths: `governance_out/RC-00x.json` (CLI), `governance_out/candidates/<cid>.json` (app) | both drivers | SAW (`governance_out/RC-001.json`, `RC-002.json` written by the CLI run; identity `sha256:736cc1d4…` matches the author's at f729076). DEMO `model_digest` is the placeholder `"sha256:845dbda0..."` |
| 6 | Decision + roles | `lab/governance/decision.py` · `decide(...)`, `review_allowed`, `approval_still_valid`, `override_expired`; `lab/governance/roles.py` · `change_role`, `has_role` | candidate id, submitter, verdict, policy roles, reviewer, action, reason, expiry | event dict keys `event, candidate_id, submitted_by, verdict, decision, reviewer, reason, expiry, time`; `DecisionError` on refusal. Role event keys `event=role_change, actor, action, person, role, members_after, time` | both drivers | SAW in the CLI run: "Self-review refused: REFUSED: tom submitted this candidate and cannot review it"; "Logged decision: BLOCK by sarah"; "APPROVE RC-002 by sarah". `change_role` SAW only via 5 tests |
| 7 | Audit log | `lab/governance/audit.py` · `append(path, event)`, `read_log`, `verify_log(path)` | events from steps 4–6 | JSONL lines: keys `seq, time, prev_hash, hash` + event keys; `LogCheck(ok, bad_seq, detail)`. Paths `governance_out/audit_log.jsonl`, `governance_out/audit_log_tampered_demo.jsonl` | both drivers | SAW: CLI wrote 6 entries; `Real log: LogCheck(ok=True, … '6 entries, chain intact')`; `Forged copy: LogCheck(ok=False, bad_seq=3, 'contents of entry 3 were changed after it was written')` |
| 8 | Assurance | `lab/assurance/risks.py` · `build_register(runs, policy, control_map, prior=None)`, `fix_tickets`, `prior_from`, `to_markdown`; `inventory.py` · `build_card(fp, policy)`; `evidence.py` · `build_register(register, retest_candidate, results)`; `thread.py` · `build_thread(...)`; `summary_page.py` · `build_summary(register, audit_events, current_asr)`; `control_map.yaml`; `lab/fixes.py` | runs frame, policy `impact`, control map, fingerprint, audit events | Markdown: `governance_out/{inventory_card, risk_register_before, risk_register_after, fix_tickets, evidence_register, traceability, manager_summary}.md` (CLI); `governance_out/<cid>/{inventory, risk_register, evidence_register, summary}.md` (app) | both drivers | SAW: all 7 markdown files written by the CLI run. Register before: R-01 33/78, R-02 27/78, R-03 27/78 (High, Open); after D2: all 0/78 (Low, **Mitigated**). `manager_summary.md`: live RC-002, ASR 0.0%, 0 open risks, APPROVE by sarah |
| 9 | Orchestration | `scripts/run_governance.py` · `run(args)`, `run_candidate`, `demo_traces`; `lab/app.py` · `main()`; `lab/demo.py` · `demo_traces` | as above | as above; CLI writes 13 files to `governance_out/` | `<py> scripts/run_governance.py [--live --sample N --reps --out --engineer --reviewer --due --keep-log --no-tamper-demo]`; `<py> -m lab.app` | SAW: CLI demo exit 0, 2.85 s wall, "Headline: attack success 42.3% -> 0.0%, benign 100% -> 100%". App SAW on the Quit path only (`printf '4\n' \| … -m lab.app`): banner + main menu + "bye.", no new files beyond the CLI's `governance_out/`; `corpus/*.jsonl` rewritten with identical content (git diff empty). `--live` NOT RUN (model) |

Notebooks: `release_gate_demo.ipynb` imports `lab/` but carries its own copy of `demo_traces` / `run_candidate`.
`attack_lab_explained.ipynb` inlines the code and does not import `lab/`. The one step that exists only in a notebook
(native runner → `results/*.csv`) names a notebook that is missing.

### 1b. Pipeline gaps (evidence in brackets)

1. **Attack results never reach the gate.** DEMO uses the hard-coded `RATES` table (RUNBOOK §3 says to update it by hand);
   LIVE re-runs the model. No code reads `results/runs.csv` into the gate. [`run_governance.py:82`, `demo.py:19`]
2. **Thin calibration.** `RATES` comes from the 24-run rehearsal (n = 4 per technique); `T7_split` was never measured and
   defaults to 0.0. DEMO benign is scripted always-correct (60/60 by construction). DEMO scripts G3 tool-misuse attacks as
   exfiltration, so R-03 (control **D3**) shows "Mitigated" after **D2** alone. [`demo.py` `_attack_calls`; CLI output]
3. **The documented live commands can never PASS.** `--live --sample 12` → 12 × 3 = 36 attack runs < `min_attack_runs: 50`.
   The app's LIVE default sample of 8 → 24 runs. A sample ≥ 17 (51 runs) is needed. [arithmetic from `run_candidate`]
4. **No driver enforces the re-test rule.** `approval_still_valid` is only called in tests. Audit events carry no
   `identity_hash` or digest, so the fingerprint JSON sits outside the hash chain.
5. **DEMO writes a placeholder digest** `"sha256:845dbda0..."` into fingerprints and the inventory card.
6. **Tracked files are rewritten.** `app.py` (on start), `run_governance.py`, `run_pyrit_attack.py` and the
   `tests/test_demo.py` fixture call `corpus.build_and_write()`, which rewrites `corpus/*.jsonl`. Today the content is
   identical (verified: in-memory regeneration == committed; git diff empty after the suite, the CLI and the app).
7. **`runs.csv` reads wrongly with plain `pd.read_csv`.** Empty `error` cells load as NaN; `astype(bool)` then counts all
   24 runs as errors and the gate shows ASR **0.0% "OK"** (0/0). Read with `keep_default_na=False`. **Matters for ui/.**
8. **No model provenance in `results/*.csv`** (no model or digest column). PyRIT traces lack `model_digest` and `seed`.
   Output paths are fixed, so each run overwrites the last.
9. **The app does not log gate verdicts as audit events** (the CLI does). `summary_page` reports "none yet" when the last
   log event is not a decision.
10. **The same demo logic exists 3 times** (`lab/demo.py`, `scripts/run_governance.py`, the notebook). Today identical
    (`RATES` equal; `demo_traces` output equal on the test split).
11. **Stale docs:** README says 72 tests (79 collected); RUNBOOK `cd` path is the author's Kali machine; README PyRIT step
    says `cd agent-attack-lab` (folder is `Sotonship-2026`); `make notebook` targets the missing notebook; `--help` shows
    `--no-tamper-demo … (default: True)`.

## 2. `lab/app.py` and `lab/demo.py` in plain words

**`lab/app.py` — a terminal text menu (stdin/stdout), not a web page.**
- Launch from the repo root: `<python> -m lab.app` (README: `.venv/bin/python -m lab.app`). Smoke-tested: it prints the
  banner "Agent Release Gate -- sandbox lab, all data synthetic / workspace: governance_out/ test split: 46 records" and
  the MAIN MENU; `4` quits with "bye.".
- Menus: **1 Red team** (test a NEW candidate; list candidates), **2 Governance** (show gate verdict; make a decision
  Approve/Block/Override; manage roles; view audit log; verify audit log), **3 Assurance** (risk register vs optional prior
  candidate; fix tickets; control mapping OWASP/ATLAS/NIST; evidence register; traceability; manager summary; AI inventory
  card; write ALL reports to markdown), **4 Quit**.
- Typed inputs: candidate id (default `RC-00n`), submitter (default `tom`), defences (`D1,D2,D3` or blank), DEMO/LIVE,
  LIVE sample size (default 8), reviewer (default `sarah`), reason, override expiry, role-edit actor (must be the rule owner
  `security-lead`), finding id, prior candidate id.
- Reads `policy.yaml`, `control_map.yaml`, `data/rules.yaml`, `corpus/test.jsonl`.
- **Writes files**, all under `governance_out/` (git-ignored): `candidates.json`, `roles_overlay.json`, `audit_log.jsonl`,
  `candidates/<cid>.json`, `<cid>/traces.json`, `<cid>/runs.csv`, `<cid>/{inventory,risk_register,evidence_register,summary}.md`.
  Rewrites `corpus/dev.jsonl` and `corpus/test.jsonl` on start. LIVE runs also write `results/traces/`.
- Overlap for ui/: the app already shows the gate verdict, decisions, role edits, the audit log + verify, and every assurance
  view. The dashboard should **read `governance_out/` files** or call the same `lab` functions, never recompute.

**`lab/demo.py` — a library, not an app.** No UI, no `__main__`, writes no files. `demo_traces(tests, reps=3)` returns
scripted tool-call traces; a seeded roll (`sha256("<id>:<rep>")`) decides per technique whether each attack lands, using
`RATES`. The real oracle, metrics and D2 replay then score them. Used by `app.py` DEMO mode and `tests/test_demo.py`.

## 4. Model-testing workflow (install → test → log → delete): MISSING

No file pulls a model, loops over models, logs per model or deletes a model. No model list. What exists:
- `lab/agent.py:40` `MODEL = os.environ.get("LAB_MODEL", "qwen2.5:7b-instruct")`, read **once at import**;
  `build_agent(tools, temperature)` has **no `model` argument**, so one process cannot loop over models.
- `lab/runner.py:29` `model_digest(model, host)` reads `/api/tags`; best-effort, returns `''` on failure.
- Model tag + digest are recorded in: native traces (`model`, `model_digest`), fingerprint JSON (`model`, `model_digest`,
  `identity_hash`), `inventory_card.md`. **Not** in `results/runs.csv`, `results/summary.csv`, `audit_log.jsonl`, nor
  (digest) in PyRIT traces.
- Governance is ready for it: `model` and `model_digest` are identity fields, so a new model = new fingerprint = needs new
  test (`tests/test_decision.py:86`). `docker-compose.yml` runs `ollama/ollama:0.4.0` with `LAB_MODEL` set.

Hooks needed: (1) a model list file (tag, licence, tool-calling y/n); (2) `model=` argument on `agent.build_agent` and
`runner.run_split`, plus an output-folder argument (`runner.TRACES` is a module constant); (3) read the digest before and
after the run and assert unchanged; (4) per-model folder `results/<tag>/<digest12>/{runs.csv,summary.csv,traces/}`;
(5) `model` and `model_digest` columns in `build_runs_frame`; (6) an audit event with model, digest, identity hash;
(7) `ollama pull` / `ollama rm` (or `/api/pull`, `/api/delete`) around the run.
