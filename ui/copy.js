/* Redline: every word the dashboard shows.
   Edit a word here and reload the page. No code change is needed.
   Rules:
   - No digit anywhere in this file, ids included. A number comes from data and fills a {placeholder}.
     tests/test_ui_copy.py checks this.
   - A beat names its screen by name. app.js maps the names to the screen keys.
   - A beat focus is the id of an element on that screen, the word cover, or null.
   - To reorder or cut beats, edit the beats list. app.js reads it in order.
   - The pipeline view (pipeline.js) reads nodes, states, edges and pipeline with exactly these keys.
   - The object below is strict JSON, so a Python test can read it. */
window.COPY = {
  "product": {
    "name": "Redline",
    "team": "Gatekeepers",
    "team_line": "Team {team}",
    "what": "A release gate for AI agents",
    "tagline": "The machine gives evidence. A person decides.",
    "track": "Responsible AI track"
  },
  "warnings": {
    "summary": "{n} warnings. The export report lists them.",
    "summary_integrity": "{n} warnings, {i} about data integrity. The export report lists them.",
    "integrity_only": "{i} data integrity warnings. The export report lists them.",
    "sample": "SAMPLE data on screen — not produced by the backend"
  },
  "cover": {
    "label": "Redline cover",
    "hint": "Press any key to start"
  },
  "hints": {
    "keys": "Keys {first} to {last}, B beat, P presenter, R reload",
    "pipeline": "Space play, Home restart, [ and ] switch model, D decision card",
    "beat_label": "Beat"
  },
  "beats": [
    {
      "id": "problem",
      "title": "The problem",
      "screen": "attacks",
      "focus": "drawer",
      "note": "Open the first trace that broke a rule: a hidden command in a document leads to a restricted record."
    },
    {
      "id": "why_now",
      "title": "Why now",
      "screen": "console",
      "focus": null,
      "note": "Hold on the question: can this update ship today?"
    },
    {
      "id": "pivot",
      "title": "A live attack, judged by plain code",
      "screen": "pipeline",
      "focus": "p-board",
      "note": "Start or replay the attack run. The checker is plain code, not another model."
    },
    {
      "id": "baseline",
      "title": "The gate and the first candidate",
      "screen": "console",
      "focus": "c-verdict",
      "note": "Read the verdict, the rate and its range from the screen. Do not quote a number from memory."
    },
    {
      "id": "duties",
      "title": "A person decides",
      "screen": "decisions",
      "focus": "a-entries",
      "note": "Show the refused self-approval and the reviewer's block. Say the reviewer is scripted."
    },
    {
      "id": "tamper",
      "title": "Tamper evidence",
      "screen": "decisions",
      "focus": "a-chain",
      "note": "Show the chain check. Edit a copy of the log in the terminal, then verify it."
    },
    {
      "id": "safeguard",
      "title": "The safeguard",
      "screen": "compare",
      "focus": "k-b",
      "note": "Compare the first and the last candidate. Say attempted against executed."
    },
    {
      "id": "limits",
      "title": "Honest limits",
      "screen": "assurance",
      "focus": "r-risks",
      "note": "A pass means within policy on these tests. It does not mean safe in general."
    },
    {
      "id": "business",
      "title": "The business view",
      "screen": "pipeline",
      "focus": "p-decision",
      "note": "Show the release decision card. Say it runs on a local model with no API fees."
    },
    {
      "id": "close",
      "title": "Close",
      "screen": "console",
      "focus": "cover",
      "note": "Return to the cover. Name the team and say where the code lives."
    }
  ],
  "screens": {
    "console": { "title": "Can this update ship today?", "nav": "Console" },
    "attacks": { "title": "Attack board", "nav": "Attacks" },
    "compare": { "title": "Compare", "nav": "Compare" },
    "decisions": { "title": "Decisions and audit log", "nav": "Decisions" },
    "assurance": { "title": "Are the risks under control?", "nav": "Assurance" },
    "pipeline": { "title": "From attack to decision", "nav": "Pipeline" },
    "models": { "title": "Models we tested", "nav": "Models" }
  },
  "nodes": {
    "rt.attack": { "running": "Attacking the agent", "done": "Attacked the agent" },
    "rt.record": { "running": "Recording every tool call", "done": "Recorded every tool call" },
    "rt.check": { "running": "Checker naming each broken rule", "done": "Checker named each broken rule" },
    "rt.rate": { "running": "Measuring the attack rate with its range", "done": "Measured the attack rate with its range" },
    "rt.fixes": { "running": "Suggesting fixes", "done": "Suggested fixes" },
    "sl.fingerprint": { "running": "Fingerprinting what was tested", "done": "Fingerprinted what was tested" },
    "sl.gate": {
      "running": "Gate checking the rule book",
      "done": "Gate gave its verdict",
      "done_fail": "Gate said FAIL",
      "done_pass": "Gate said PASS"
    },
    "sl.review": {
      "running": "Reviewer deciding",
      "done": "Reviewer decided",
      "done_block": "Reviewer blocked it",
      "done_approve": "Reviewer approved it"
    },
    "sl.log": { "running": "Writing it in the stone diary", "done": "Wrote it in the stone diary" },
    "fix.ticket": { "running": "Engineer changing the agent", "done": "Engineer changed the agent" },
    "ll.risks": { "running": "Updating the risk register", "done": "Updated the risk register" },
    "ll.controls": { "running": "Mapping controls to OWASP, ATLAS and NIST", "done": "Mapped controls to OWASP, ATLAS and NIST" },
    "ll.evidence": { "running": "Filing the proof files", "done": "Filed the proof files" },
    "ll.trace": { "running": "Tracing one finding start to finish", "done": "Traced one finding start to finish" },
    "ll.summary": { "running": "Writing the one-page summary", "done": "Wrote the one-page summary" }
  },
  "states": {
    "idle": "Waiting",
    "running": "Running",
    "done": "Done",
    "fail": "FAIL",
    "pass": "PASS",
    "scripted": "Scripted",
    "error": "Stopped",
    "skipped": "Skipped"
  },
  "edges": {
    "every_change": "Every change",
    "same_test_data": "Same test data",
    "open_risk": "Open risk",
    "test_again": "Test again"
  },
  "pipeline": {
    "title": "From attack to decision",
    "status_done": "Done in {t}. Evidence screens updated: press R.",
    "status_export_missing": "Done in {t}. Evidence screens not updated. Run this, then press R: {cmd}",
    "replay_hint": "Press Space to play the recorded run.",
    "not_loaded": "The pipeline view did not load.",
    "rail_label": "Gate result per candidate",
    "board_label": "Release pipeline"
  },
  "decision": {
    "title": "Release decision",
    "lede": "One page for the person who signs off the release.",
    "candidate_label": "Candidate",
    "verdict_label": "Gate verdict",
    "decided_label": "Decided by",
    "scripted": "scripted by the CLI",
    "scripted_note": "The command line scripts this reviewer. In use, a person decides.",
    "no_decision": "No decision recorded",
    "reason_label": "Reason",
    "runs_label": "Runs",
    "time_label": "Time to decision",
    "model_label": "Model",
    "cost_label": "Cost",
    "cost": "No API fees: runs on a local model",
    "audit_label": "Audit log",
    "entries": "Entries: {n}",
    "risks_label": "Open risks",
    "tickets_label": "Fix tickets",
    "count_open": "{n} open",
    "count_listed": "{n} listed",
    "none_listed": "None listed",
    "not_recorded": "Not recorded"
  },
  "models": {
    "title": "Models we tested",
    "columns": {
      "tag": "Model",
      "tools": "Tool calls",
      "asr": "Attack success, {cand}",
      "benign": "Normal requests handled",
      "errors": "Errors",
      "verdicts": "Gate verdicts",
      "source": "Mode and source"
    },
    "cand_fallback": "first candidate",
    "digest_label": "Digest",
    "tools": {
      "yes": "yes",
      "no": "no — every run errored",
      "unchecked": "not checked"
    },
    "not_measured": "Not measured: no tool calls ran",
    "unchecked_note": "Tool calls not checked: not proof of safety.",
    "range": "range",
    "headline": "Headline model",
    "no_headline": "No headline model chosen yet.",
    "empty": "No model results yet. Run the models, export, then press R.",
    "note": "Sorted by model tag. Nothing here ranks a model as best. A model without tool calls is not measured: a zero there is not a pass."
  },
  "page": {
    "title": "Can this AI agent ship?",
    "badge_real": "Real model run",
    "badge_scripted": "Scripted demo",
    "badge_none": "No run exported yet",
    "verdict": { "PASS": "Ready to ship", "FAIL": "Blocked" },
    "defences": { "none": "No defences", "code": "Code checks", "human": "Human approval" },
    "attacks": "attacks broke a rule",
    "of": "of",
    "why": "Why",
    "limit_word": "limit",
    "normal": "Normal tasks completed",
    "steps_title": "How the decision is made",
    "steps": [
      { "id": "rt.attack", "label": "Attack the agent" },
      { "id": "rt.check", "label": "Check every action" },
      { "id": "sl.gate", "label": "Gate against the rules" },
      { "id": "sl.review", "label": "A person decides" },
      { "id": "sl.log", "label": "Record it in a tamper-evident log" }
    ],
    "state": { "idle": "Waiting", "running": "Running", "done": "Done" },
    "decision": "Decision",
    "scripted_reviewer": "scripted reviewer",
    "decided_in": "Decided in",
    "log_entries": "audit-log entries",
    "pending": "not available yet"
  }
};
