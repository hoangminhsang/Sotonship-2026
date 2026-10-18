/* SAMPLE: hand-made for layout and state checks. This is NOT produced by the backend or the bridge.
   It is not loaded by index.html. Do not copy it to ui/data/live.js in the repo.
   To look at it: copy it to data/live.js in a scratch copy of ui/, open index.html, press 6.
   - Shape: "ui-live/1" (PLAN.md A3), the way ui/bridge.py writes it: node facts are strings (empty
     when there is nothing to say), tags carry the gate outcome, tiles use the bridge's words
     (violated, held, ok, missed, error, running, unscored, idle), rail text starts with the id.
   - Every node state appears at least once: idle, running, done, fail, pass, scripted, error, skipped.
     The gate (done + tag FAIL) and the reviewer (done + tag "scripted by the CLI") show the way the
     bridge writes them; the explicit fail, pass and scripted states check the other path.
   - Node statuses are mixed on purpose, so one picture shows the REAL fail fill, the REAL pass fill,
     the scripted hatch with an outcome word, the REAL scripted reviewer, a stop and a skip.
     A real run never mixes them like this.
   - There are no measurements here. "n", "i" and "x" stand for numbers and hashes the bridge would
     write. tiles.n only sizes the tile strip and is not shown as text. */
window.LIVE = {
  schema: "ui-live/1",
  rev: 1,
  written_at: "SAMPLE",
  source: { kind: "sample", log: "ui/fixtures/live.sample.js", uid: "sample", speed: null, paced: false },
  label: { word: "SCRIPTED", text: "SCRIPTED - hand-made sample, not a measured run", status: "SAMPLE" },
  mode: "SCRIPTED",
  model: "no model (sample)",
  digest: "no digest (sample)",
  state: "running",
  active: "ll.evidence",
  prev_active: "ll.controls",
  nodes: {
    "rt.attack":      { state: "done",     status: "REAL",   fact: "n runs", fact2: "", tag: "" },
    "rt.record":      { state: "done",     status: "REAL",   fact: "n tool calls - n blocked", fact2: "", tag: "" },
    "rt.check":       { state: "done",     status: "REAL",   fact: "class x n - class y n", fact2: "", tag: "" },
    "rt.rate":        { state: "pass",     status: "REAL",   fact: "n of n - n% (n% to n%)", fact2: "normal requests handled n% of n", tag: "" },
    "rt.fixes":       { state: "skipped",  status: "REAL",   fact: "", fact2: "", tag: "" },
    "sl.fingerprint": { state: "done",     status: "SAMPLE", fact: "identity xxxxxxxxxxxx - changed", fact2: "", tag: "" },
    "sl.gate":        { state: "done",     status: "REAL",   fact: "FAIL - n of n checks failed", fact2: "attack_success_rate n% (limit n%)", tag: "FAIL" },
    "sl.review":      { state: "done",     status: "REAL",   fact: "BLOCK by reviewer", fact2: "reason from the gate", tag: "scripted by the CLI" },
    "sl.log":         { state: "done",     status: "SAMPLE", fact: "n entries - chain intact - head xxxxxxxxxxxx", fact2: "", tag: "intact" },
    "fix.ticket":     { state: "error",    status: "REAL",   fact: "stopped before the next defence", fact2: "", tag: "" },
    "ll.risks":       { state: "fail",     status: "SAMPLE", fact: "n risks - n open", fact2: "", tag: "" },
    "ll.controls":    { state: "pass",     status: "SAMPLE", fact: "controls used: x, y", fact2: "", tag: "" },
    "ll.evidence":    { state: "running",  status: "REAL",   fact: "n evidence rows", fact2: "", tag: "" },
    "ll.trace":       { state: "idle",     status: "REAL",   fact: "", fact2: "", tag: "" },
    "ll.summary":     { state: "scripted", status: "REAL",   fact: "n summary lines", fact2: "", tag: "" }
  },
  edges: {
    "rt.attack>rt.record": "done",
    "rt.record>rt.check": "done",
    "rt.check>rt.rate": "done",
    "rt.rate>rt.fixes": "idle",
    "sl.fingerprint>sl.gate": "done",
    "sl.gate>sl.review": "done",
    "sl.review>sl.log": "done",
    "sl.log>fix.ticket": "done",
    "ll.risks>ll.controls": "done",
    "ll.controls>ll.evidence": "done",
    "ll.evidence>ll.trace": "idle",
    "ll.trace>ll.summary": "idle",
    "rt.rate>sl.fingerprint": "done",
    "fix.ticket>rt.attack": "idle",
    "rt.attack>ll.risks": "done",
    "ll.summary>fix.ticket": "idle"
  },
  rail: [
    { id: "RC-001", text: "RC-001 - no defences", verdict: "FAIL", state: "done", status: "REAL" },
    { id: "RC-002", text: "RC-002 - defences x", verdict: "", state: "running", status: "SAMPLE" },
    { id: "RC-003", text: "RC-003 - defences x, y", verdict: "", state: "idle", status: "SAMPLE" }
  ],
  tiles: {
    cand: "RC-001",
    n: 30,
    list: [
      ["t-a:a", "violated"], ["t-a:b", "violated"], ["t-a:c", "held"], ["t-b:a", "held"], ["t-b:b", "violated"],
      ["t-b:c", "held"], ["t-c:a", "held"], ["t-c:b", "held"], ["t-c:c", "held"], ["t-d:a", "error"],
      ["t-d:b", "held"], ["t-d:c", "violated"], ["t-e:a", "held"], ["t-e:b", "held"], ["t-e:c", "unscored"],
      ["n-a:a", "ok"], ["n-a:b", "ok"], ["n-a:c", "missed"], ["n-b:a", "ok"], ["n-b:b", "ok"],
      ["n-b:c", "ok"], ["n-c:a", "error"], ["n-c:b", "ok"], ["n-c:c", "running"],
      ["#25", "idle"], ["#26", "idle"], ["#27", "idle"], ["#28", "idle"], ["#29", "idle"], ["#30", "idle"]
    ]
  },
  events_tail: [],
  warnings: [],
  done: null,
  batch: null
};
