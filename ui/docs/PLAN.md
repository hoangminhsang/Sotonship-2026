# Agent Release Gate — demo UI in `ui/` (plan)

> **Status, 18 Oct 2026:** this is the approved plan of 17 Oct for the five-screen dashboard. It is kept as the record. On 18 Oct the team added a bridge, a models runner and, with lane F, screens 6 and 7 (being merged into `demo/integrated`). The current rules are in `BUILD_CONTRACT.md` (see its "Amendments of 18 Oct 2026"), the file formats are in `../../docs/DATA_CONTRACT.md`, and how to run it is in `../README.md`. Where this plan disagrees with them, they win.


## Context
- **Need:** a read-only, offline, projector-ready view of the evidence for the demo after the pitch. "Working product" is the largest judging weight (25%), and the attack board carries the demo.
- **Change since the prompt:** governance, assurance, a CLI (`scripts/run_governance.py`), a menu app (`lab/app.py`) and a demo module (`lab/demo.py`) are on `origin/main` of the lab repo (`f729076`, `d825341`).
- **User's rules added this session (binding):**
  - Build on today's real `results/`, plus labelled SAMPLE data for gate, decisions, audit log and assurance.
  - Do not run backend commands. Never run the gate CLI or `lab/app.py` from this session. Read the audit log only; the tamper demo uses a copy.
  - Run "After the backend is pushed" only when the user says the review PR is merged. The review session owns the results layout and `docs/DATA_CONTRACT.md`.
  - The headline model is not chosen yet. Use the available rehearsal data, and keep the model easy to swap.
  - The user wants a visual demo soon and will refine it after seeing it.

## Where we build (user chose: new lab worktree)
- This session's worktree is in the planning repo, so nothing is written there.
- Create the worktree:
  `git -C /Users/sistaseetaram/Desktop/Hackathons/Sotonship-2026 worktree add --no-track -b ui/demo /Users/sistaseetaram/Desktop/Hackathons/Sotonship-2026-ui-lab origin/main`
  It starts at `d825341`. Only a local branch and a worktree are added; no lab file changes.
- All edits use absolute paths under `/Users/sistaseetaram/Desktop/Hackathons/Sotonship-2026-ui-lab/ui/`. Push access to `hoangminhsang/Sotonship-2026` is confirmed.

## No duplication of `lab/app.py` / `lab/demo.py`
- `lab/app.py` is the operator's terminal menu. It runs tests, records decisions, edits roles, and prints and verifies the log.
- `lab/demo.py` writes scripted traces, so DEMO-mode numbers are never measurements.
- The UI adds only what neither file has: a tile board, a drawer for one run, a compare view, a projector view, and data-status labels. The UI never acts.

## Data the UI uses now
| Dataset | Source | Status |
|---|---|---|
| Runs (24, all attacks) | `results/runs.csv` | REAL |
| Rates + Wilson ranges | `results/summary.csv` | REAL |
| Planted text | `corpus/test.jsonl` (all 12 rehearsal `user_task`s match at `d825341`) | REAL, labelled "from the corpus file; the run did not record it" |
| Policy limits + roles | `lab/governance/policy.yaml` | REAL |
| Control map ("mapped to") | `lab/assurance/control_map.yaml`, including its "IDs are illustrative until verified" note | REAL |
| Lab default model | `lab/agent.py` (`LAB_MODEL` default) | shown as "not recorded (lab default: qwen2.5:7b-instruct)" |
| Traces | `results/traces/` (absent everywhere) | PENDING |
| Gate verdict, audit log, assurance records | `ui/fixtures/` | SAMPLE with `--allow-sample`, else PENDING |
| Verify result | none (the backend only prints it) | PENDING |
- REPLAY label: "REPLAY · results committed 2026-10-17T06:30:08+01:00".
- Never shown: the scripted numbers in `f729076:governance_out/` (33/78, 60/60, 0/78) and the idea-doc numbers.
- `sources.json` names a `headline` batch and a `compare` pair. When the team picks the headline model, editing those keys and re-running the export swaps it.

## SAMPLE fixtures (`ui/fixtures/*.sample.json`, each `"sample": true`)
- Each fixture embeds the backend's own on-disk format, so `export.py` runs the same parser it will use on real files:
  - Gate: `report_text`.
  - Audit: JSONL lines with the real field names.
  - Assurance: the markdown tables.
- Values come from the real rehearsal and the backend's exact templates:
  - **Gate:** FAIL on attack success 12/24 [31.4%, 68.6%], min attack runs 24 < 50 ("not enough proof"), and normal requests "not measured (0 benign runs)".
  - **Audit:** follows the menu app's flow — submit RC-001 by tom, then `decision_refused` "REFUSED: tom submitted this candidate and cannot review it", then BLOCK by sarah with a reason. Hashes are visibly fake ("sample…").
  - **Risks:** counts from `runs.csv`, with the backend's likelihood labels.
  - **Evidence:** "(re-test not run)" / "Not yet run".
- Every SAMPLE panel carries a visible "SAMPLE — not produced by the backend" label.
- The committed `ui/data/demo.js` is always exported **without** `--allow-sample`. Rehearsals export with the flag on the local machine.

## Files (all in `ui/`)
- **`export.py`** (stdlib only):
  - One loader per dataset, each wrapped so a failure gives PENDING plus a warning.
  - Parsers: CSV, the flattened `tool_calls` string (split at top level only; `[blocked]` suffix), JSONL, a YAML subset for the two YAML files, `report_text`, and markdown tables.
  - REPLAY time: the earliest trace `started_at`, else `git log -1 --format=%cI` (via subprocess with a timeout).
  - A trace is used only if its flattened tool calls equal the `runs.csv` row.
  - Writes atomically (tmp file + `os.replace`).
  - Prints warnings and a provenance list (each number on screen → file + row).
  - Flags: `--allow-sample`, `--sources`, `--out`.
- **`sources.json`:** `batches[]` (id, title, runs, summary, traces_dir, corpus), `headline`, `compare`, `policy`, `control_map`, `agent_source`, and per governance dataset `{file|null, fixture}`.
- **`data/demo.js`:** `window.DEMO = {schema, exported_at, allow_sample, warnings[], provenance[], status{}, batches[{id, title, status, source_file, replay_label, model/digest/defences{display, source}, summary{asr, benign, ever_hijacked, error_runs, per_technique[]}, runs[{key, test_id, kind, technique, place, goal, rep, user_task, tool_calls[{text, blocked}], violated, violation_classes[], oracle_detail[], benign_success, error, state, planted{status, poison, inject_target, source}, trace|null}], techniques_not_run[]}], policy{}, gate{status, candidate_id, verdict, reasons_count, checks[], raw_text}, audit{status, entries[], chain{status, text}}, assurance{status, risks[], evidence[], controls{status, items[], note}}, compare{a, b, b_status}}`.
- **Tile state** (set in `export.py` from backend fields):
  - `violated` → broke (filled). If the run also errored, it gets an "errored" badge, so harm is never hidden.
  - Else `error` → error (hatched).
  - Else any `[blocked]` call → blocked (hollow).
  - Else attack runs → clean; normal requests → handled or not handled.
- **`index.html` + `app.css` + `app.js`:**
  - Layout and loading:
    - A fixed 1920×1080 stage scaled to fit, like the carousel, so 1366×768 shows the same layout. Text is at least 22px on the stage.
    - Classic scripts only: `data/demo.js`, then `app.js`. If the data is missing, the page says "run python3 ui/export.py".
  - Brand:
    - The five `@font-face` lines (287–291 of `carousel.html`, without the trailing `</style>`) open `app.css`, followed by the brand tokens.
    - The deck line sits under the screen nav and glides to the active screen. The meters use it as a static axis. Nothing else moves, and reduced motion is respected.
  - Header on every screen: data-status chips (REAL solid, SAMPLE dashed, PENDING dotted, each with its word), the REPLAY label on screens 1–3, and the export time.
  - Screens:
    1. **"Can this update ship today?"** — candidate fields, policy limits, measured rates with a range bar and the 5% limit line, and the verdict exactly as the gate text says (or PENDING).
    2. **Attack board** — attack and normal rows ("not measured (n = 0)"), technique filter, legend, and the drawer with the user's task, the planted text and where it was hidden, tool calls in order, the checker's verdict and detail, model, digest and duration.
    3. **Compare** — A against B with rate, range bar, n and per-technique rows; B is PENDING today, and differing n are flagged.
    4. **Decision & audit log** — entries in order, short hashes, and the chain status as the backend wrote it (PENDING today).
    5. **"Are the risks under control?"** — risks, controls ("mapped to, never compliant with"), evidence.
  - Keys: 1–5 switch screens, R calls `location.reload()`, P toggles presenter mode (hides hints, warnings and the idle cursor; chips and the REPLAY label stay), Esc closes the drawer, arrows move between tiles, Enter opens one.
  - `location.hash` keeps the screen, presenter mode, filter and open run across reloads.
  - The user's suggested phrases are used only where they are true.
- **`README.md`:**
  - How to run: `python3 ui/export.py [--allow-sample]`, then open `ui/index.html`; after each terminal command, export and press R.
  - Status rules.
  - Font credits: Cormorant by Christian Thalmann (Catharsis Fonts) and Inter by Rasmus Andersson, both SIL OFL 1.1, verified on their GitHub repos. Inter lists a Reserved Font Name, and these are embedded subsets, so this is noted as a point to check.
  - Demo safety: never run the CLI without `--keep-log`; the tamper beat uses a copy.
  - Requests for the builders: a verify result file, a DEMO/LIVE marker on disk, executed/blocked_by per call, the gate verdict as JSON, assurance as JSON, model + digest + `started_at` per batch.

## Build steps (after approval)
1. Inline:
   - Create the worktree.
   - Copy the font lines with a short script (base64 never printed).
2. Workflow "build-ui", about 7 agents:
   - **Build, in parallel with no shared files:**
     - Data agent: `export.py`, `sources.json`, fixtures, README.
     - Front-end agent: `index.html`, `app.css`, `app.js`, built against the schema above.
   - **Integrate:** one agent exports with `--allow-sample`, takes headless-Chrome screenshots of every screen and the drawer at both sizes, and fixes layout.
   - **Verify, in parallel, adversarial:**
     - Honesty and provenance audit: every number on screen traced to its row, and no SAMPLE in the no-sample export.
     - Robustness tests of `export.py` on scratch copies only (missing, empty, corrupt).
     - Accessibility and keyboard review: keyboard-only demo run, reduced motion, glyph coverage, offline.
   - **Fix:** one agent applies the confirmed findings.
3. Show the screenshots and open the page for the user.
4. Re-export without `--allow-sample`, commit `ui/` only, run `git push -u origin ui/demo`, and open a PR to `main`.

## Verification
- `python3 ui/export.py` and `python3 ui/export.py --allow-sample`: no crash, warnings listed, the provenance list matches the source rows.
- Missing, empty and corrupt copies of each source give PENDING plus a warning. These tests run on scratch copies, never on repo files.
- Screenshots via `file://` at 1920×1080 and 1366×768: all 5 screens plus the ATK-0002 rep 1 drawer, with no overflow.
- Offline: there are no `http` URLs in `ui/`, and only `file://` requests are made.
- Keyboard-only run of the Part 6 flow, a reduced-motion check, and a glyph-coverage check.
- `git status` shows changes only inside `ui/`.
