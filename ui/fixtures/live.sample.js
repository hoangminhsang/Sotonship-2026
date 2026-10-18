/* SAMPLE: hand-made for layout and state checks. This is NOT produced by the backend or the bridge.
   It is not loaded by index.html. Do not copy it to ui/data/live.js in the repo.
   To look at it: copy it to data/live.js in a scratch copy of ui/, open index.html, press 6.
   - Shape: "ui-live/1" (PLAN.md A3).
   - Every node state appears at least once: idle, running, done, fail, pass, scripted, error, skipped.
   - Node statuses are mixed on purpose, so one picture shows the REAL fail fill, the REAL pass fill,
     the scripted hatch with an outcome word, the REAL scripted reviewer, a stop and a skip.
     A real run never mixes them like this.
   - There are no measurements here. "n", "i" and "x" stand for numbers and hashes the bridge would
     write. Counts that size the tile strip (tiles.n) are layout values only and are not shown as text. */
window.LIVE = {
  schema: "ui-live/1",
  rev: 1,
  written_at: "SAMPLE",
  source: { kind: "sample", log: "ui/fixtures/live.sample.js", uid: "sample", speed: null, paced: false },
  label: { word: "SCRIPTED", text: "SCRIPTED · hand-made sample, not a measured run", status: "SAMPLE" },
  mode: "SAMPLE",
  model: "no model (sample)",
  digest: "no digest (sample)",
  state: "running",
  active: "ll.evidence",
  prev_active: "ll.controls",
  nodes: {
    "rt.attack":      { state: "done",    status: "REAL",   fact: "run i of n · test x, rep i", fact2: null, tag: null },
    "rt.record":      { state: "done",    status: "REAL",   fact: "calls n · blocked n", fact2: null, tag: null },
    "rt.check":       { state: "done",    status: "REAL",   fact: "broken rules: class x n, class y n", fact2: null, tag: null },
    "rt.rate":        { state: "pass",    status: "REAL",   fact: "rate n% · range n% to n%", fact2: null, tag: null },
    "rt.fixes":       { state: "skipped", status: "REAL",   fact: null, fact2: null, tag: null },
    "sl.fingerprint": { state: "done",    status: "SAMPLE", fact: "xxxxxxxxxxxx · changed", fact2: null, tag: null },
    "sl.gate":        { state: "fail",    status: "REAL",   fact: "n of n checks failed", fact2: "attack rate, proof", tag: null },
    "sl.review":      { state: "scripted", status: "REAL",  fact: "BLOCK · reasons from the gate", fact2: null, tag: "scripted by the CLI" },
    "sl.log":         { state: "done",    status: "SAMPLE", fact: "entry n · hash xxxxxxxxxxxx", fact2: "chain intact", tag: null },
    "fix.ticket":     { state: "error",   status: "REAL",   fact: "stopped before the next defence", fact2: null, tag: null },
    "ll.risks":       { state: "fail",    status: "SAMPLE", fact: "n risk rows, n open", fact2: null, tag: null },
    "ll.controls":    { state: "pass",    status: "SAMPLE", fact: "n controls mapped", fact2: null, tag: null },
    "ll.evidence":    { state: "running", status: "REAL",   fact: "n proof files so far", fact2: null, tag: null },
    "ll.trace":       { state: "idle",    status: "REAL",   fact: null, fact2: null, tag: null },
    "ll.summary":     { state: "idle",    status: "REAL",   fact: null, fact2: null, tag: null }
  },
  edges: {
    "rt.attack>rt.record": "done",
    "rt.record>rt.check": "done",
    "rt.check>rt.rate": "done",
    "rt.rate>rt.fixes": "idle",
    "rt.rate>sl.fingerprint": "done",
    "sl.fingerprint>sl.gate": "done",
    "sl.gate>sl.review": "done",
    "sl.review>sl.log": "done",
    "sl.log>fix.ticket": "done",
    "rt.attack>ll.risks": "done",
    "ll.risks>ll.controls": "done",
    "ll.controls>ll.evidence": "done",
    "ll.evidence>ll.trace": "idle",
    "ll.trace>ll.summary": "idle",
    "ll.summary>fix.ticket": "idle",
    "fix.ticket>rt.attack": "idle"
  },
  rail: [
    { id: "RC-001", text: "no defences", verdict: "FAIL", state: "done", status: "REAL" },
    { id: "RC-002", text: "defence a", verdict: null, state: "running", status: "SAMPLE" },
    { id: "RC-003", text: "defences a and b", verdict: null, state: "idle", status: "SAMPLE" }
  ],
  tiles: {
    cand: "RC-001",
    n: 30,
    list: [
      ["t-a:a", "broke"], ["t-a:b", "broke"], ["t-a:c", "clean"], ["t-b:a", "clean"], ["t-b:b", "broke"],
      ["t-b:c", "clean"], ["t-c:a", "blocked"], ["t-c:b", "clean"], ["t-c:c", "clean"], ["t-d:a", "error"],
      ["t-d:b", "clean"], ["t-d:c", "broke"], ["t-e:a", "clean"], ["t-e:b", "clean"], ["t-e:c", "blocked"],
      ["n-a:a", "handled"], ["n-a:b", "handled"], ["n-a:c", "not_handled"], ["n-b:a", "handled"], ["n-b:b", "handled"],
      ["n-b:c", "handled"], ["n-c:a", "error"], ["n-c:b", "handled"], ["n-c:c", "handled"]
    ]
  },
  events_tail: [],
  warnings: [],
  done: null,
  batch: null
};
