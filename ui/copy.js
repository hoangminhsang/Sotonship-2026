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
    "badge_none": "No run yet",
    "badge_replay": "Replay of a real run",
    "scripted_badge": "Scripted demo",
    "tabs": { "live": "Live run", "results": "Results", "proof": "Proof", "test": "Test a model" },
    "verdict": { "PASS": "Ready to ship", "FAIL": "Blocked" },
    "chip": { "PASS": "Ready", "FAIL": "Blocked" },
    "defences": { "none": "No defences", "labels": "Trust labels", "code": "Code checks", "human": "Human approval" },
    "attacks": "attacks broke a rule",
    "of": "of",
    "why": "Why",
    "limit_max": "allowed up to",
    "limit_min": "needs at least",
    "normal": "Normal tasks completed",
    "decided_in": "Decided in",
    "pending": "not available yet",
    "groups": {
      "red": "Red team",
      "gate": "Can this update ship today?",
      "risk": "Are the risks under control?"
    },
    "steps": [
      { "id": "rt.attack", "label": "Attack the agent" },
      { "id": "rt.record", "label": "Watch every tool call" },
      { "id": "rt.check", "label": "Check the rules" },
      { "id": "sl.gate", "label": "Release gate" },
      { "id": "sl.review", "label": "A person decides" },
      { "id": "sl.log", "label": "Tamper-evident log" },
      { "id": "ll.risks", "label": "Risks under control?" },
      { "id": "fix.ticket", "label": "Fix and test again" }
    ],
    "loop_label": "test again",
    "state": { "idle": "Waiting", "running": "Running", "done": "Done" },
    "happened": {
      "title": "What just happened",
      "asked": "A person asked",
      "hidden": "Hidden in the document",
      "did": "The agent did",
      "broke": "Rule broken",
      "blocked": "A defence stopped the agent",
      "held": "No rule broken",
      "caught": "Redline caught it.",
      "from_rehearsal": "From the rehearsal run",
      "from_earlier": "From an earlier run"
    },
    "run": {
      "kinds": {
        "demo": "Scripted demo (no model)",
        "live-short": "Quick live check",
        "live-full": "Full run",
        "models": "All models"
      },
      "model_label": "Model",
      "run": "Run",
      "stop": "Stop",
      "file_line": "To run from here, start the local runner with python ui/bridge.py --serve and open the address it prints.",
      "confirm_long": "This run takes a long time on this machine. Start it anyway?",
      "idle": "Ready. Choose a run and press Run.",
      "running": "Running",
      "runs_done": "runs done",
      "finished": "Finished. The results are updated.",
      "finished_no_export": "Finished. The results are not updated yet.",
      "failed": "The run failed.",
        "stopped": "Stopped. The results were not updated.",
      "starting": "Starting",
      "stopping": "Stopping",
      "could_not": "The run could not start.",
      "no_model": "Choose a model first."
    },
    "stream": {
      "head_scripted": "The agent's replies are scripted. No model is called.",
      "head_model": "Agent model",
      "scripted_agent": "Scripted agent",
      "waiting": "Waiting for the first run",
      "asks": "A person asks",
      "document": "Document",
      "hidden_tag": "hidden instruction",
      "caught": "Caught",
      "held": "Held: it did only the task",
      "caught_pill": "Caught",
      "held_pill": "Held",
      "normal": "Normal task",
      "more_runs": "more runs",
      "fingerprint": "Agent fingerprint",
      "changed": "Changed since the last version",
      "same": "Same as the last version",
      "gate": "Gate",
      "audit": "Noted in the log",
      "entry": "Entry",
      "reviewer_tag": "scripted reviewer",
      "chain_ok": "The log is intact",
      "chain_bad": "The log is broken",
      "entries": "entries",
      "ticket": "Fix raised",
      "eb_task": "The task",
      "eb_hidden": "Hidden inside",
      "eb_agent": "What the agent did",
      "eb_verdict": "The verdict",
      "eb_batch": "Across the test batch",
      "eb_finger": "Fingerprint",
      "eb_gate": "The gate",
      "eb_log": "The log",
      "read": "Read",
      "hidden_cap": "a hidden instruction in the document",
      "caught_word": "Caught.",
      "held_pre": "It",
      "held_word": "held.",
      "held_cap": "it did only the task",
      "gate_pre": "The gate says",
      "log_pre": "Noted in the",
      "log_word": "log.",
      "entry_n": "entry",
      "hash": "hash",
      "chain_intact": "the chain is intact"
    },
    "chips_title": "Release candidates",
    "proof": {
      "who_title": "Who decided, and when",
      "none_yet": "No decision recorded yet",
      "log_title": "Is the log intact?",
      "log_ok": "Checked: no edits found",
      "log_bad": "Checked: an edit was found",
      "log_none": "Not checked yet",
      "rules_title": "Three rules",
      "rules": [
        "The machine gives evidence. A person decides.",
        "Write it in stone.",
        "Anything changes, test again."
      ],
      "engineer": "(engineer) cannot approve their own work.",
      "reviewer": "(reviewer) decides.",
      "risks_title": "Open risks",
      "no_risks": "No open risks listed",
      "controls_title": "Controls used",
      "controls_none": "None listed",
      "controlled_by": "Controlled by"
    },
    "test": {
      "intro": "Test your own local model in the simulated Northwind office. The documents, tickets and assets are all made up, and nothing leaves this laptop.",
      "offline_title": "Start the local runner first",
      "offline_file": "Testing a model needs the local runner. Start it with python ui/bridge.py --serve and open the address it prints. Everything stays on this laptop.",
      "offline_failed": "The local runner did not answer. Check that python ui/bridge.py --serve is still running, then reload this page.",
      "no_answer": "The local runner did not answer.",
      "model_title": "Model",
      "model_help": "Give a model file or an Ollama tag. Redline adds it to Ollama and checks that it can call tools.",
      "source_label": "Model file or Ollama tag",
      "source_hint": "{path} or {tag}",
      "import": "Import and check",
      "need_source": "Type a model file path or an Ollama tag first.",
      "step_pull": "Get the model from Ollama",
      "step_create": "Import the file into Ollama",
      "step_check": "Check that it can call tools",
      "step_error": "Stopped",
      "name_label": "Name in Ollama",
      "ready": "Ready",
      "ready_text": "This model calls tools, so Redline can test it.",
      "no_tools": "No tool calls",
      "no_tools_text": "This model does not call tools. Redline cannot test it.",
      "unknown": "Unknown",
      "unknown_text": "Redline could not tell whether this model calls tools. It cannot test the model until that is known.",
      "import_failed": "The import failed.",
      "attacks_title": "Your attacks",
      "attacks_help": "Your own hidden prompts. Redline plants each one in the simulated office and runs it after the standard attacks.",
      "file_pre": "They are kept in",
      "file_post": "You can also edit that file in a text editor.",
      "attacks_none": "No attacks yet. Add one below.",
      "task_label": "Task",
      "name_field": "Name",
      "goal_field": "What it tries to do",
      "place_field": "Where it hides",
      "prompt_field": "Prompt, one line",
      "add": "Add attack",
      "added": "Added.",
      "goals": ["Read a restricted record", "Send data out", "Misuse a tool"],
      "places": ["Inside a document", "In a document comment", "In an asset's notes"],
      "run_title": "Run",
      "run_help": "Pick a model and a kind of run. The quick check is short. The full run takes much longer.",
      "model_field": "Model",
      "kind_field": "Kind of run",
      "extra": "Include my attacks",
      "extra_help": "Your attacks run as an extra set. They need a live run.",
      "no_models": "No model is listed yet. Import one first.",
      "import_busy": "An import is running. Wait until it is finished.",
      "go": "Run",
      "result_title": "Result",
      "result_none": "No result yet. Run a live check with Include my attacks on.",
      "result_from": "From the last run",
      "standard": "Standard attacks",
      "yours": "Your attacks",
      "not_available": "not available",
      "no_extra": "No result for your attacks in this run.",
      "extra_wait": "Not finished yet.",
      "no_extra_runs": "No runs of your attacks, so there is no rate.",
      "broke": "runs broke a rule",
      "range": "range",
      "to": "to",
      "attack_one": "attack",
      "attack_many": "attacks",
      "result_note": "Your attacks are an extra set. They are not part of the gate verdict, and they are never added to the standard test set."
    },
    "months": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
  }
};
