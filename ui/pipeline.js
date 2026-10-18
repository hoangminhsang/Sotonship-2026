/* Redline pipeline screen (screen 6).
   Classic script. It exposes window.Pipeline = { visible(bool), key(name) } and nothing else.
   It draws 15 glyph nodes, their wires, the pass rail, the tile strip and the status line
   from window.LIVE (shape "ui-live/1", rewritten by ui/bridge.py into data/live.js).
   Rules kept here:
   - Every word comes from window.COPY. A missing string shows its key name.
   - Every number comes from window.LIVE and is drawn inside an element with
     [data-prov="live:..."]. Nothing is measured, rounded or formatted in this file.
   - Text is set with textContent only. No storage. No network calls: data/live.js is read
     by re-inserting a classic script tag every 500 ms, only while screen 6 is visible.
   - One moving element at a time: a 120 px dash travels along the wire into the active node,
     then that node's glyph draws itself once. A new step finishes the running one first.
     With reduced motion there is no animation: states switch instantly.
   - Without live data, Space plays window.LIVE_REPLAY (recorded key frames).
   - A value is REAL only when the data says REAL. Anything else is drawn as scripted data:
     a hatch strip and the word, never the alarm or ok fill.
   Screen coordinates follow PLAN.md A4: the content area is x 120-1800, y 150-1016 on the
   1920 x 1080 stage. Board space below has its origin at (120, 150). */
(function () {
  'use strict';

  var doc = document;
  /* the SVG namespace, read from a parsed literal tag so no URL string sits in this file */
  var SVGNS = (function () {
    try { return doc.createRange().createContextualFragment('<svg></svg>').firstChild.namespaceURI; } catch (e) { return null; }
  })();
  var DOT = ' ' + String.fromCharCode(183) + ' ';
  /* separators that may sit between a rail id and its text: space . , : ; | - and the middle dot */
  var CUT_LEAD = new RegExp('^[\\s.,:;|' + String.fromCharCode(183) + '-]+');

  /* ---------- timing (display only) ---------- */
  var POLL_MS = 500;
  var DASH_PX = 120;            /* length of the travelling dash */
  var TRAVEL_MS = 1100;         /* dash along one wire, easing var(--glide) */
  var DRAW_MS = 1200;           /* glyph draw-on, whole glyph */
  var DRAW_SPREAD_MS = 360;     /* stagger spread inside DRAW_MS when a glyph has several strokes */
  var DRAW_EASE = 'cubic-bezier(0.23, 1, 0.32, 1)';
  var GLIDE_FALLBACK = 'cubic-bezier(0.65, 0, 0.35, 1)';
  var REPLAY_STEP_MS = 1400;    /* pause between key frames when a frame carries no timing */
  var REPLAY_MAX_MS = 3000;

  /* ---------- board geometry (board space) ---------- */
  var BOX_W = 300;
  var BOX_H = 190;
  var COL_PITCH = 320;
  var ROW_TOP = 180;
  var ROW_PITCH = 230;
  var HEAD_LEN = 10;            /* arrowhead length */
  var HEAD_HALF = 6;            /* arrowhead half width */
  var TRUNK_LEFT_X = -22;
  var TRUNK_RIGHT_X = 1630;
  var AGAIN_Y = 155;            /* the "test again" run, stage y 305 */
  var CHANNEL_Y = 390;          /* the "every change" run, between row 1 and row 2 */

  var STATES = ['idle', 'running', 'done', 'fail', 'pass', 'scripted', 'error', 'skipped'];
  var DONE_STATES = ['done', 'fail', 'pass', 'scripted'];
  var STARTED_STATES = ['running', 'done', 'fail', 'pass', 'scripted', 'error'];
  var STATE_ALIAS = {
    waiting: 'idle', queued: 'idle', ok: 'done', complete: 'done', completed: 'done',
    failed: 'fail', passed: 'pass', stopped: 'error', errored: 'error', skip: 'skipped'
  };
  /* tile states: the bridge writes violated, held, ok, missed, unscored, error, running, idle;
     the attack board words (broke, clean, handled, not_handled, blocked) are accepted too */
  var TILE_ALIAS = { violated: 'broke', held: 'clean', ok: 'handled', missed: 'not_handled' };
  var TILE_KNOWN = ['idle', 'running', 'broke', 'blocked', 'clean', 'error', 'handled', 'not_handled', 'unscored'];
  var EXPORT_OK = ['ok', 'done', 'exported', 'success'];

  var NODES = [
    { id: 'rt.attack', r: 0, c: 0 }, { id: 'rt.record', r: 0, c: 1 }, { id: 'rt.check', r: 0, c: 2 },
    { id: 'rt.rate', r: 0, c: 3 }, { id: 'rt.fixes', r: 0, c: 4 },
    { id: 'sl.fingerprint', r: 1, c: 0 }, { id: 'sl.gate', r: 1, c: 1 }, { id: 'sl.review', r: 1, c: 2 },
    { id: 'sl.log', r: 1, c: 3 }, { id: 'fix.ticket', r: 1, c: 4 },
    { id: 'll.risks', r: 2, c: 0 }, { id: 'll.controls', r: 2, c: 1 }, { id: 'll.evidence', r: 2, c: 2 },
    { id: 'll.trace', r: 2, c: 3 }, { id: 'll.summary', r: 2, c: 4 }
  ];
  var NODE_BY_ID = {};
  NODES.forEach(function (n) { NODE_BY_ID[n.id] = n; });

  function mid(id, side) {
    var n = NODE_BY_ID[id];
    var x = n.c * COL_PITCH;
    var y = ROW_TOP + n.r * ROW_PITCH;
    if (side === 'l') return [x, y + BOX_H / 2];
    if (side === 'r') return [x + BOX_W, y + BOX_H / 2];
    if (side === 't') return [x + BOX_W / 2, y];
    return [x + BOX_W / 2, y + BOX_H];
  }

  /* Wires. key = "from>to" as written in LIVE.edges. Chain wires join neighbours in a row. */
  var EDGES = [];
  (function () {
    var rows = [
      ['rt.attack', 'rt.record', 'rt.check', 'rt.rate', 'rt.fixes'],
      ['sl.fingerprint', 'sl.gate', 'sl.review', 'sl.log', 'fix.ticket'],
      ['ll.risks', 'll.controls', 'll.evidence', 'll.trace', 'll.summary']
    ];
    rows.forEach(function (row) {
      for (var i = 0; i < row.length - 1; i++) {
        EDGES.push({ key: row[i] + '>' + row[i + 1], from: row[i], to: row[i + 1], chain: true,
          pts: [mid(row[i], 'r'), mid(row[i + 1], 'l')] });
      }
    });
    var every = [mid('rt.rate', 'b'), null, null, mid('sl.fingerprint', 't')];
    every[1] = [every[0][0], CHANNEL_Y];
    every[2] = [every[3][0], CHANNEL_Y];
    EDGES.push({ key: 'rt.rate>sl.fingerprint', from: 'rt.rate', to: 'sl.fingerprint', pts: every,
      label: 'every_change', lx: (every[0][0] + every[3][0]) / 2, ly: CHANNEL_Y, mask: true });
    var a = mid('rt.attack', 'l');
    var b = mid('ll.risks', 'l');
    EDGES.push({ key: 'rt.attack>ll.risks', from: 'rt.attack', to: 'll.risks',
      alts: ['sl.fingerprint>ll.risks', 'rt.record>ll.risks', 'rt.check>ll.risks', 'rt.rate>ll.risks'],
      pts: [a, [TRUNK_LEFT_X, a[1]], [TRUNK_LEFT_X, b[1]], b],
      label: 'same_test_data', lx: TRUNK_LEFT_X, ly: (a[1] + b[1]) / 2, mask: true, turn: -90 });
    var s0 = mid('ll.summary', 't');
    var s1 = mid('fix.ticket', 'b');
    EDGES.push({ key: 'll.summary>fix.ticket', from: 'll.summary', to: 'fix.ticket', pts: [s0, s1],
      label: 'open_risk', lx: s0[0] + 14, ly: (s0[1] + s1[1]) / 2, side: true });
    var f0 = mid('fix.ticket', 'r');
    var f1 = mid('rt.attack', 't');
    EDGES.push({ key: 'fix.ticket>rt.attack', from: 'fix.ticket', to: 'rt.attack',
      pts: [f0, [TRUNK_RIGHT_X, f0[1]], [TRUNK_RIGHT_X, AGAIN_Y], [f1[0], AGAIN_Y], f1],
      label: 'test_again', lx: (TRUNK_RIGHT_X + f1[0]) / 2, ly: AGAIN_Y, mask: true });
  })();
  var EDGE_BY_KEY = {};
  EDGES.forEach(function (e) {
    e.len = 0;
    for (var i = 1; i < e.pts.length; i++) {
      e.len += Math.abs(e.pts[i][0] - e.pts[i - 1][0]) + Math.abs(e.pts[i][1] - e.pts[i - 1][1]);
    }
    EDGE_BY_KEY[e.key] = e;
  });

  /* ---------- state ---------- */
  var built = false;
  var shown = false;
  var cur = null;               /* the LIVE object on screen (or null) */
  var source = 'poll';          /* 'poll' or 'replay': who feeds the screen */
  var lastRev = null;
  var lastUid = null;
  var lastActive = null;
  var firstRender = true;
  var pollTimer = 0;
  var pollTag = null;
  var pollBusy = false;
  var anim = null;              /* { list: [animation], node: li, edge: edge def } */
  var refs = {};                /* node id -> element refs */
  var edgeRefs = {};            /* edge key -> element refs */
  var tileRefs = { sig: '', els: [] };
  var glide = null;
  var reduceMq = null;
  var rp = { runs: null, run: 0, i: -1, timer: 0, playing: false };

  /* ---------- small helpers ---------- */
  function $(id) { return doc.getElementById(id); }
  function has(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
  function isObj(x) { return x !== null && typeof x === 'object' && !Array.isArray(x); }
  function clear(n) { while (n && n.firstChild) n.removeChild(n.firstChild); }
  function el(tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function sv(tag, attrs) {
    var n = doc.createElementNS(SVGNS, tag);
    if (attrs) Object.keys(attrs).forEach(function (k) { n.setAttribute(k, String(attrs[k])); });
    return n;
  }
  function setAttr(n, k, v) {
    if (v === null || v === undefined || v === false) { if (n.hasAttribute(k)) n.removeAttribute(k); return; }
    var s = v === true ? '' : String(v);
    if (n.getAttribute(k) !== s) n.setAttribute(k, s);
  }
  function setText(n, t) { if (n.textContent !== t) n.textContent = t; }
  function safe(fn) {
    try { return fn(); } catch (e) { if (window.__pipelineDebug) { throw e; } return undefined; }
  }
  function scalar(v) { return typeof v === 'string' ? v !== '' : typeof v === 'number'; }
  function first() {
    for (var i = 0; i < arguments.length; i++) if (scalar(arguments[i])) return arguments[i];
    return undefined;
  }
  function reduced() { return !!(reduceMq && reduceMq.matches); }

  /* ---------- words: window.COPY, never typed here ---------- */
  function copyAt(group, key, sub) {
    var C = window.COPY;
    var v;
    try {
      v = C && C[group] && C[group][key];
      if (sub !== undefined) v = v && v[sub];
    } catch (e) { v = null; }
    return (typeof v === 'string' && v !== '') ? v : null;
  }
  function cNode(id, which) { return copyAt('nodes', id, which) || ('nodes.' + id + '.' + which); }
  function cState(s) { return copyAt('states', s) || ('states.' + s); }
  function cEdge(k) { return copyAt('edges', k) || ('edges.' + k); }
  function cPipe(k) { return copyAt('pipeline', k) || ('pipeline.' + k); }

  /* ---------- data values: always inside [data-prov] ---------- */
  function valSpan(text, prov, status) {
    var s = el('span', null, text);
    s.setAttribute('data-prov', prov);
    s.setAttribute('data-status', status || 'SAMPLE');
    return s;
  }
  function lookup(ctx, path) {
    var parts = path.split('.');
    var v = ctx;
    for (var i = 0; i < parts.length; i++) {
      if (v === null || v === undefined || typeof v !== 'object' || !has(v, parts[i])) return undefined;
      v = v[parts[i]];
    }
    return scalar(v) ? v : undefined;
  }
  /* A COPY string with {name} placeholders becomes text nodes plus data spans. */
  function tpl(str, ctx, provBase, status) {
    var frag = doc.createDocumentFragment();
    var re = /\{([A-Za-z_][A-Za-z0-9_.]*)\}/g;
    var last = 0;
    var m;
    while ((m = re.exec(str))) {
      if (m.index > last) frag.appendChild(doc.createTextNode(str.slice(last, m.index)));
      var v = ctx ? lookup(ctx, m[1]) : undefined;
      if (v === undefined) frag.appendChild(doc.createTextNode(m[0]));
      else frag.appendChild(valSpan(String(v), provBase + '.' + m[1], status));
      last = re.lastIndex;
    }
    if (last < str.length) frag.appendChild(doc.createTextNode(str.slice(last)));
    return frag;
  }

  function normState(raw) {
    var s = typeof raw === 'string' ? raw.toLowerCase() : '';
    if (STATES.indexOf(s) >= 0) return s;
    return STATE_ALIAS[s] || 'idle';
  }
  /* the status of the whole run: REAL, SAMPLE, or PENDING when the data says nothing */
  function liveStatus(v) {
    var s = v && v.label && v.label.status;
    return s === 'REAL' || s === 'SAMPLE' ? s : 'PENDING';
  }
  /* a value is REAL only when the data says so; otherwise it takes the run's status */
  function statusOf(x, v) {
    var s = x && x.status;
    if (s === 'REAL' || s === 'SAMPLE' || s === 'PENDING') return s;
    return liveStatus(v);
  }
  function okLive(v) {
    if (!isObj(v)) return false;
    if (typeof v.schema === 'string' && v.schema.indexOf('ui-live/') !== 0) return false;
    return true;
  }

  /* ---------- build the board once ---------- */
  function need(id, tag, parent, cls) {
    var n = $(id);
    if (!n && parent) {
      n = doc.createElement(tag);
      n.id = id;
      if (cls) n.className = cls;
      parent.appendChild(n);
    }
    return n;
  }

  function fillGlyph(g, id) {
    var markup = window.GLYPHS && typeof window.GLYPHS[id] === 'string' ? window.GLYPHS[id] : null;
    if (markup) {
      try {
        var root = doc.createRange().createContextualFragment('<svg>' + markup + '</svg>').firstChild;
        while (root && root.firstChild) g.appendChild(root.firstChild);
      } catch (e) { clear(g); }
    }
    /* a plain box when a glyph is missing */
    if (!g.firstChild) g.appendChild(sv('rect', { 'class': 'f-ink', x: 14, y: 10, width: 92, height: 70 }));
  }

  function buildNodes(ol) {
    clear(ol);
    refs = {};
    NODES.forEach(function (n) {
      var li = el('li', 'p-node');
      li.setAttribute('data-node', n.id);
      li.setAttribute('data-state', 'idle');
      li.style.left = (n.c * COL_PITCH) + 'px';
      li.style.top = (ROW_TOP + n.r * ROW_PITCH) + 'px';
      var top = el('div', 'p-top');
      var svg = sv('svg', { 'class': 'p-glyph', viewBox: '0 0 120 90', 'aria-hidden': 'true', focusable: 'false' });
      var g = sv('g');
      fillGlyph(g, n.id);
      svg.appendChild(g);
      var badge = el('div', 'p-badge');
      var st = el('div', 'p-state');
      var mark = el('span', 'p-mark');
      mark.setAttribute('aria-hidden', 'true');
      var word = el('span', 'p-word');
      word.setAttribute('data-static', '');
      st.appendChild(mark);
      st.appendChild(word);
      var scr = el('div', 'p-script');
      scr.setAttribute('data-static', '');
      scr.hidden = true;
      var tag = el('div', 'p-tag');
      badge.appendChild(st);
      badge.appendChild(scr);
      badge.appendChild(tag);
      top.appendChild(svg);
      top.appendChild(badge);
      var cap = el('p', 'p-cap');
      cap.setAttribute('data-static', '');
      var fact = el('p', 'p-fact');
      li.appendChild(top);
      li.appendChild(cap);
      li.appendChild(fact);
      ol.appendChild(li);
      refs[n.id] = { li: li, g: g, row: st, word: word, script: scr, tag: tag, cap: cap, fact: fact, capSig: null, factSig: null, tagSig: null };
    });
  }

  function pathD(pts) {
    return pts.map(function (p, i) { return (i ? 'L' : 'M') + p[0] + ' ' + p[1]; }).join(' ');
  }
  function headPts(pts) {
    var a = pts[pts.length - 2];
    var b = pts[pts.length - 1];
    var dx = b[0] === a[0] ? 0 : (b[0] > a[0] ? 1 : -1);
    var dy = b[1] === a[1] ? 0 : (b[1] > a[1] ? 1 : -1);
    var bx = b[0] - dx * HEAD_LEN;
    var by = b[1] - dy * HEAD_LEN;
    var px = dy * HEAD_HALF;
    var py = dx * HEAD_HALF;
    return [b[0], b[1], bx + px, by + py, bx - px, by - py].join(' ');
  }

  function buildWires(svg, labels) {
    clear(svg);
    clear(labels);
    edgeRefs = {};
    svg.setAttribute('viewBox', '0 0 1680 866');
    svg.setAttribute('width', '1680');
    svg.setAttribute('height', '866');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    EDGES.forEach(function (e) {
      var g = sv('g', { 'class': 'p-edge', 'data-edge': e.key, 'data-state': 'idle' });
      var base = sv('path', { 'class': 'p-e-base', d: pathD(e.pts) });
      var head = sv('polygon', { 'class': 'p-e-head', points: headPts(e.pts) });
      var dash = sv('path', { 'class': 'p-e-dash', d: pathD(e.pts) });
      g.appendChild(base);
      g.appendChild(head);
      g.appendChild(dash);
      svg.appendChild(g);
      var lab = null;
      if (e.label) {
        lab = el('span', 'p-elabel');
        lab.setAttribute('data-static', '');
        lab.setAttribute('data-edge', e.key);
        lab.style.left = e.lx + 'px';
        lab.style.top = e.ly + 'px';
        var tf = e.side ? 'translate(0, -50%)' : 'translate(-50%, -50%)';
        if (e.turn) tf += ' rotate(' + e.turn + 'deg)';
        lab.style.transform = tf;
        if (e.mask) lab.className += ' is-mask';
        labels.appendChild(lab);
      }
      edgeRefs[e.key] = { def: e, g: g, base: base, head: head, dash: dash, label: lab };
    });
  }

  function ensureBuilt() {
    if (built) return true;
    var sec = $('screen-6');
    if (!sec) return false;
    /* pipeline.css is linked by index.html; add the link when it is missing */
    if (!doc.querySelector('link[href$="pipeline.css"]')) {
      var lk = doc.createElement('link');
      lk.rel = 'stylesheet';
      lk.href = 'pipeline.css';
      doc.head.appendChild(lk);
    }
    var h1 = sec.querySelector('h1');
    if (h1 && h1.textContent.replace(/\s+/g, '') === '') {
      h1.textContent = cPipe('title');
      h1.setAttribute('data-static', '');
    }
    var mode = need('p-mode', 'div', sec);
    var rail = need('p-rail', 'ol', sec);
    var status = need('p-status', 'p', sec);
    var board = need('p-board', 'div', sec);
    var tiles = need('p-tiles', 'div', sec);
    var src = need('p-src', 'div', sec);
    if (!(mode && rail && status && board && tiles && src)) return false;
    status.setAttribute('role', 'status');
    rail.setAttribute('aria-label', cPipe('rail_label'));
    board.setAttribute('role', 'group');
    board.setAttribute('aria-label', cPipe('board_label'));
    var wires = $('p-wires');
    if (!wires || wires.namespaceURI !== SVGNS) {
      if (wires && wires.parentNode) wires.parentNode.removeChild(wires);
      wires = sv('svg', { id: 'p-wires' });
      board.insertBefore(wires, board.firstChild);
    }
    var nodes = need('p-nodes', 'ol', board);
    var labels = board.querySelector('.p-elabels');
    if (!labels) {
      labels = el('div', 'p-elabels');
      board.appendChild(labels);
    }
    buildWires(wires, labels);
    buildNodes(nodes);
    clear(rail);
    try { reduceMq = window.matchMedia('(prefers-reduced-motion: reduce)'); } catch (e) { reduceMq = null; }
    var g = '';
    try { g = window.getComputedStyle(doc.documentElement).getPropertyValue('--glide').trim(); } catch (e2) { g = ''; }
    glide = g || GLIDE_FALLBACK;
    built = true;
    return true;
  }

  /* ---------- render: nodes ---------- */
  /* The bridge writes a finished node as "done" and puts the outcome in the tag: FAIL or PASS
     on the gate, "scripted by the CLI" on the reviewer, intact or broken on the chain.
     The page shows those as fail, pass and scripted. An explicit state from the data wins. */
  function effectiveState(id, nd) {
    var s = normState(nd && nd.state);
    var tag = nd && scalar(nd.tag) ? String(nd.tag).toUpperCase() : '';
    if (s !== 'done' || !tag) return s;
    if (id === 'sl.gate') return tag === 'FAIL' ? 'fail' : (tag === 'PASS' ? 'pass' : s);
    if (id === 'sl.review') return tag.indexOf('SCRIPT') >= 0 ? 'scripted' : s;
    if (id === 'sl.log') return tag === 'BROKEN' ? 'fail' : s;
    return s;
  }

  function nodeLook(id, nd, v) {
    var s = effectiveState(id, nd);
    var status = statusOf(nd, v);
    var real = status === 'REAL';
    var scripted = s !== 'idle' && s !== 'skipped' && (s === 'scripted' || !real);
    var fill = '';
    if (real && s === 'fail') fill = 'fail';
    if (real && s === 'pass') fill = 'pass';
    return { state: s, status: status, scripted: scripted, fill: fill };
  }

  /* The finished caption has outcome variants in COPY: done_fail, done_pass, done_block,
     done_approve. The first word of the reviewer's fact says what was decided. */
  function doneKey(id, nd, state) {
    if (id === 'sl.gate') {
      if (state === 'fail' && copyAt('nodes', id, 'done_fail')) return 'done_fail';
      if (state === 'pass' && copyAt('nodes', id, 'done_pass')) return 'done_pass';
    }
    if (id === 'sl.review' && nd) {
      var w = String(first(nd.decision, nd.verdict, nd.fact) || '').split(/[\s:]/)[0].toUpperCase();
      if (w === 'BLOCK' && copyAt('nodes', id, 'done_block')) return 'done_block';
      if (w === 'APPROVE' && copyAt('nodes', id, 'done_approve')) return 'done_approve';
    }
    return 'done';
  }

  function captionKey(id, nd, state) {
    return DONE_STATES.indexOf(state) >= 0 ? doneKey(id, nd, state) : 'running';
  }

  function renderNode(id, v) {
    var r = refs[id];
    if (!r) return;
    var nd = v && v.nodes && isObj(v.nodes[id]) ? v.nodes[id] : null;
    var look = nodeLook(id, nd, v);
    var li = r.li;
    setAttr(li, 'data-state', look.state);
    setAttr(li, 'data-status', look.status);
    setAttr(li, 'data-fill', look.fill || null);
    setAttr(li, 'data-scripted', look.scripted ? '1' : null);
    setAttr(li, 'aria-current', v && v.active === id ? 'step' : null);
    li.classList.toggle('is-sample', look.scripted);
    var word = cState(look.state);
    setText(r.word, word);
    /* scripted data: the hatch strip with its word; the state word stays beside it, except for
       a node whose state is itself "scripted" (the strip and its word are then the state) */
    r.script.hidden = !look.scripted;
    r.row.hidden = look.state === 'scripted';
    if (look.scripted) setText(r.script, cState('scripted'));
    var which = captionKey(id, nd, look.state);
    var text = cNode(id, which);
    var base = 'live:nodes.' + id;
    var capSig = JSON.stringify([which, text, nd ? [nd.fact, nd.fact2, nd.tag, nd.verdict, nd.decision] : null, look.status]);
    if (r.capSig !== capSig) {
      r.capSig = capSig;
      clear(r.cap);
      r.cap.appendChild(tpl(text, nd, base, look.status));
    }
    var factSig = JSON.stringify([nd ? [nd.fact, nd.fact2] : null, look.status]);
    if (r.factSig !== factSig) {
      r.factSig = factSig;
      clear(r.fact);
      if (nd && scalar(nd.fact)) {
        var f1 = valSpan(String(nd.fact), base + '.fact', look.status);
        f1.className = 'p-f1';
        r.fact.appendChild(f1);
      }
      if (nd && scalar(nd.fact2)) {
        var f2 = valSpan(String(nd.fact2), base + '.fact2', look.status);
        f2.className = 'p-f2';
        r.fact.appendChild(f2);
      }
    }
    /* the tag sits under the state word, unless it only repeats that word (the gate's FAIL or PASS) */
    var tagText = nd && scalar(nd.tag) ? String(nd.tag) : '';
    if (tagText && tagText.toUpperCase() === word.toUpperCase()) tagText = '';
    var tagSig = tagText + '|' + look.status;
    if (r.tagSig !== tagSig) {
      r.tagSig = tagSig;
      clear(r.tag);
      if (tagText) r.tag.appendChild(valSpan(tagText, base + '.tag', look.status));
    }
  }

  /* ---------- render: wires ---------- */
  function edgeIsDone(e, v) {
    var m = v && isObj(v.edges) ? v.edges : null;
    if (m) {
      if (has(m, e.key)) return m[e.key] === 'done';
      if (e.alts) {
        for (var i = 0; i < e.alts.length; i++) if (has(m, e.alts[i])) return m[e.alts[i]] === 'done';
      }
    }
    if (e.chain && v && isObj(v.nodes)) {
      var tn = v.nodes[e.to];
      return !!tn && STARTED_STATES.indexOf(normState(tn.state)) >= 0;
    }
    return false;
  }

  function renderEdges(v) {
    EDGES.forEach(function (e) {
      var r = edgeRefs[e.key];
      if (!r) return;
      var running = anim && anim.edge === e;
      var st = (edgeIsDone(e, v) && !running) ? 'done' : 'idle';
      setAttr(r.g, 'data-state', st);
      if (r.label) {
        setText(r.label, cEdge(e.label));
        setAttr(r.label, 'data-state', st);
      }
    });
  }

  /* ---------- render: mode chip, rail, tiles, status, source ---------- */
  function renderMode(v) {
    var host = $('p-mode');
    clear(host);
    if (!v || !isObj(v.label)) return;
    var lb = v.label;
    var text = first(lb.text, lb.word);
    if (text === undefined) return;
    var chip = valSpan(String(text), 'live:label', liveStatus(v));
    chip.className = 'p-chip';
    host.appendChild(chip);
  }

  /* The bridge writes the rail text with the id in front ("RC-002 . defences D2"). The id has
     its own place in the cell, so it is cut from the second line. */
  function railText(rc) {
    var id = scalar(rc.id) ? String(rc.id) : '';
    var text = scalar(rc.text) ? String(rc.text) : '';
    if (id && text.indexOf(id) === 0) text = text.slice(id.length).replace(CUT_LEAD, '');
    return text;
  }

  function renderRail(v) {
    var ol = $('p-rail');
    var list = v && Array.isArray(v.rail) ? v.rail : [];
    clear(ol);
    list.forEach(function (rc, i) {
      if (!isObj(rc)) return;
      var s = normState(rc.state);
      var status = statusOf(rc, v);
      var verdict = scalar(rc.verdict) ? String(rc.verdict) : '';
      var vkey = verdict.toLowerCase();
      var li = el('li', 'p-rc');
      li.setAttribute('data-state', s);
      li.setAttribute('data-status', status);
      var prev = i > 0 && isObj(list[i - 1]) ? normState(list[i - 1].state) : '';
      if (i > 0) li.setAttribute('data-from', DONE_STATES.indexOf(prev) >= 0 ? 'done' : 'idle');
      if (vkey === 'fail' || vkey === 'pass') {
        li.setAttribute('data-verdict', vkey);
        if (status === 'REAL') li.setAttribute('data-fill', vkey);
      }
      if (status !== 'REAL' && s !== 'idle') li.className += ' is-sample';
      var base = 'live:rail.' + i;
      var r1 = el('div', 'p-rc1');
      if (scalar(rc.id)) r1.appendChild(valSpan(String(rc.id), base + '.id', status));
      var w = el('span', 'p-rcw');
      if (verdict) w.appendChild(valSpan(verdict, base + '.verdict', status));
      else { w.textContent = cState(s); w.setAttribute('data-static', ''); }
      r1.appendChild(w);
      li.appendChild(r1);
      var r2 = el('div', 'p-rc2');
      var rt = railText(rc);
      if (rt) r2.appendChild(valSpan(rt, base + '.text', status));
      li.appendChild(r2);
      ol.appendChild(li);
    });
  }

  function tileSize(n, w, h) {
    var best = 3;
    for (var rows = 1; rows <= 4; rows++) {
      var cols = Math.ceil(n / rows);
      var s = Math.min(26, Math.floor((h - (rows - 1) * 2) / rows), Math.floor((w - (cols - 1) * 2) / cols));
      if (s > best) best = s;
    }
    return best;
  }

  function tileState(raw) {
    var s = typeof raw === 'string' ? raw : '';
    if (has(TILE_ALIAS, s)) s = TILE_ALIAS[s];
    return TILE_KNOWN.indexOf(s) >= 0 ? s : 'idle';
  }

  function renderTiles(v) {
    var host = $('p-tiles');
    var t = v && isObj(v.tiles) ? v.tiles : null;
    if (!t) { clear(host); tileRefs = { sig: '', els: [] }; return; }
    var list = Array.isArray(t.list) ? t.list : [];
    var n = typeof t.n === 'number' && t.n > list.length ? t.n : list.length;
    var status = statusOf(t, v);
    var cand = scalar(t.cand) ? String(t.cand) : '';
    var shape = cand + '|' + n + '|' + status;
    if (tileRefs.sig !== shape) {
      clear(host);
      host.setAttribute('data-status', status);
      var lab = el('span', 'p-tl-cand');
      if (cand) lab.appendChild(valSpan(cand, 'live:tiles.cand', status));
      var grid = el('div', 'p-tl-grid');
      grid.setAttribute('role', 'img');
      host.appendChild(lab);
      host.appendChild(grid);
      var gw = grid.clientWidth || 700;
      var gh = grid.clientHeight || 32;
      var size = tileSize(n, gw, gh);
      var els = [];
      for (var i = 0; i < n; i++) {
        var tile = el('span', 'p-tile');
        tile.style.width = size + 'px';
        tile.style.height = size + 'px';
        tile.setAttribute('data-state', 'idle');
        grid.appendChild(tile);
        els.push(tile);
      }
      tileRefs = { sig: shape, els: els, grid: grid };
    }
    var words = window.DEMO && window.DEMO.labels && window.DEMO.labels.states;
    tileRefs.els.forEach(function (tile, i) {
      var item = list[i];
      var st = tileState(Array.isArray(item) ? item[1] : null);
      setAttr(tile, 'data-state', st);
      var title = '';
      if (Array.isArray(item) && scalar(item[0])) title = String(item[0]) + (words && words[st] ? ' ' + words[st] : '');
      setAttr(tile, 'title', title || null);
    });
    if (tileRefs.grid) setAttr(tileRefs.grid, 'aria-label', cand || null);
  }

  function activeNode(v) {
    return v && v.active && v.nodes && isObj(v.nodes[v.active]) ? v.nodes[v.active] : null;
  }

  function runningCand(v) {
    var list = v && Array.isArray(v.rail) ? v.rail : [];
    for (var i = 0; i < list.length; i++) {
      if (isObj(list[i]) && normState(list[i].state) === 'running' && scalar(list[i].id)) return String(list[i].id);
    }
    return '';
  }

  function renderStatus(v) {
    var p = $('p-status');
    var frag = doc.createDocumentFragment();
    var st = v ? String(v.state || '') : '';
    var status = liveStatus(v);
    var d = v && isObj(v.done) ? v.done : {};
    var wide = false;
    if (!v || st === 'waiting' || st === '') {
      frag.appendChild(doc.createTextNode(replayFrames().length ? cPipe('replay_hint') : cState('idle')));
    } else if (st === 'done') {
      /* export.status is ok, failed, not_run, running or skipped; when it did not run, its text
         is the command to run. Any other case shows the bridge's own line. */
      var ex = isObj(d.export) ? d.export : null;
      var exStatus = ex && scalar(ex.status) ? String(ex.status).toLowerCase() : '';
      var cmd = first(ex && ex.cmd, ex && ex.command, d.cmd);
      var ctx = { t: first(d.dur, d.t, d.duration, d.dur_s, d.took, d.time, v.dur, v.t, v.duration), cmd: cmd };
      if (!exStatus || EXPORT_OK.indexOf(exStatus) >= 0) {
        if (scalar(ctx.t) || !scalar(d.text)) {
          frag.appendChild(tpl(cPipe('status_done'), ctx, 'live:done', status));
        } else {
          /* no duration to fill {t}: show the bridge's own line instead of a raw placeholder */
          frag.appendChild(valSpan(String(d.text), 'live:done.text', status));
          wide = String(d.text).length > 54;
        }
      } else if ((exStatus === 'not_run' || exStatus === 'skipped') && scalar(first(cmd, ex.text))) {
        ctx.cmd = first(cmd, ex.text);
        frag.appendChild(tpl(cPipe('status_export_missing'), ctx, 'live:done', status));
        wide = true;
      } else if (scalar(d.text)) {
        frag.appendChild(valSpan(String(d.text), 'live:done.text', status));
        wide = String(d.text).length > 54;
      } else {
        frag.appendChild(tpl(cPipe('status_done'), ctx, 'live:done', status));
      }
    } else if ((st === 'error' || st === 'stopped') && scalar(d.text)) {
      /* a stopped run: the bridge's own line says why */
      frag.appendChild(valSpan(String(d.text), 'live:done.text', status));
      wide = true;
    } else {
      var rc = runningCand(v);
      if (rc) {
        frag.appendChild(valSpan(rc, 'live:rail.cand', status));
        frag.appendChild(doc.createTextNode(DOT));
      }
      frag.appendChild(doc.createTextNode(cState(st === 'running' ? 'running' : 'error')));
      var an = activeNode(v);
      if (an && v.active) {
        var an_state = effectiveState(v.active, an);
        frag.appendChild(doc.createTextNode(DOT));
        frag.appendChild(tpl(cNode(v.active, captionKey(v.active, an, an_state)), an, 'live:nodes.' + v.active, statusOf(an, v)));
      }
    }
    var sig = frag.textContent + '|' + status;
    setAttr(p, 'data-wide', wide);
    if (p.getAttribute('data-sig') === sig) return;
    p.setAttribute('data-sig', sig);
    clear(p);
    p.appendChild(frag);
  }

  /* model, digest and where the data came from. The word LIVE is never written here: the
     mode chip carries it, and only for a log that is still growing. */
  function renderSrc(v) {
    var host = $('p-src');
    clear(host);
    if (!v) return;
    var status = liveStatus(v);
    var items = [['model', v.model], ['digest', v.digest], ['source.log', v.source && v.source.log]];
    var w = Array.isArray(v.warnings) ? v.warnings : [];
    w.forEach(function (x, i) { items.push(['warnings.' + i, x]); });
    var firstItem = true;
    items.forEach(function (it) {
      if (!scalar(it[1])) return;
      if (!firstItem) host.appendChild(doc.createTextNode(DOT));
      firstItem = false;
      host.appendChild(valSpan(String(it[1]), 'live:' + it[0], status));
    });
  }

  function renderAll(v) {
    safe(function () { renderMode(v); });
    safe(function () { renderRail(v); });
    safe(function () { Object.keys(refs).forEach(function (id) { renderNode(id, v); }); });
    safe(function () { renderEdges(v); });
    safe(function () { renderTiles(v); });
    safe(function () { renderStatus(v); });
    safe(function () { renderSrc(v); });
  }

  /* ---------- motion: one moving element at a time ---------- */
  /* The strokes that draw on: .f-ink (main) and .f-ink.f-thin (detail). Their style="--i:N"
     sets the order (0 first); .f-dash parts are not drawn on. */
  function inkShapes(g) {
    var list = g.querySelectorAll('.f-ink, .f-thin');
    if (!list.length) list = g.querySelectorAll('path, line, polyline, polygon, rect, circle, ellipse');
    return Array.prototype.slice.call(list);
  }

  function drawOrder(shape, index) {
    var n = NaN;
    try { n = parseFloat(shape.style.getPropertyValue('--i')); } catch (e) { n = NaN; }
    return isNaN(n) ? index : n;
  }

  function finishCurrent() {
    var a = anim;
    if (!a) return;
    anim = null;
    a.list.forEach(function (x) {
      try { x.finish(); } catch (e) { /* already settled */ }
      try { x.cancel(); } catch (e2) { /* already settled */ }
    });
    if (a.node) a.node.classList.remove('is-drawing');
    if (cur) safe(function () { renderEdges(cur); });
  }

  function startTransition(from, to) {
    finishCurrent();
    if (reduced() || !built || !shown) return;
    var r = refs[to];
    if (!r) return;
    var list = [];
    var edge = from ? EDGE_BY_KEY[from + '>' + to] : null;
    var er = edge ? edgeRefs[edge.key] : null;
    var delay = 0;
    if (er && typeof er.dash.animate === 'function') {
      var L = edge.len;
      var dashing = DASH_PX + ' ' + (L + DASH_PX);
      list.push(er.dash.animate(
        [{ strokeDasharray: dashing, strokeDashoffset: DASH_PX }, { strokeDasharray: dashing, strokeDashoffset: -L }],
        { duration: TRAVEL_MS, easing: glide, fill: 'both' }));
      delay = TRAVEL_MS;
    } else {
      edge = null;
    }
    var shapes = inkShapes(r.g);
    var orders = shapes.map(drawOrder);
    var maxOrder = Math.max.apply(null, orders.concat([0]));
    var spread = maxOrder > 0 ? DRAW_SPREAD_MS : 0;
    shapes.forEach(function (shape, i) {
      var len = 0;
      try { len = shape.getTotalLength(); } catch (e) { len = 0; }
      if (!(len > 0) || typeof shape.animate !== 'function') return;
      var dd = len + ' ' + len;
      list.push(shape.animate(
        [{ strokeDasharray: dd, strokeDashoffset: len }, { strokeDasharray: dd, strokeDashoffset: 0 }],
        { duration: DRAW_MS - spread, delay: delay + (maxOrder > 0 ? orders[i] / maxOrder * spread : 0), easing: DRAW_EASE, fill: 'both' }));
    });
    if (!list.length) return;
    r.li.classList.add('is-drawing');
    var mine = { list: list, node: r.li, edge: edge };
    anim = mine;
    if (cur) safe(function () { renderEdges(cur); });
    Promise.all(list.map(function (a) { return a.finished; })).then(function () {
      if (anim !== mine) return;
      finishCurrent();
    }, function () { /* cancelled by a newer step */ });
  }

  /* ---------- feed: take one LIVE object ---------- */
  function resetRun() {
    finishCurrent();
    lastActive = null;
    firstRender = true;
  }

  function ingest(raw, force) {
    var v = okLive(raw) ? raw : null;
    if (v === null && source === 'replay') return;
    var uid = v && isObj(v.source) && scalar(v.source.uid) ? v.source.uid : null;
    if (v) {
      if (!force && typeof v.rev === 'number' && v.rev === lastRev && uid === lastUid) return;
      if (uid !== lastUid || (typeof v.rev === 'number' && typeof lastRev === 'number' && v.rev < lastRev)) resetRun();
      lastRev = typeof v.rev === 'number' ? v.rev : null;
      lastUid = uid;
    } else {
      if (cur === null && !firstRender) return;
      resetRun();
      lastRev = null;
      lastUid = null;
    }
    var active = v && typeof v.active === 'string' ? v.active : null;
    var moved = active !== null && active !== lastActive && !firstRender;
    var from = v && typeof v.prev_active === 'string' ? v.prev_active : lastActive;
    if (moved) finishCurrent();
    cur = v;
    renderAll(v);
    if (moved) {
      var nd = v.nodes && v.nodes[active];
      if (nd && STARTED_STATES.indexOf(normState(nd.state)) >= 0) safe(function () { startTransition(from, active); });
    }
    lastActive = active;
    firstRender = false;
  }

  /* ---------- feed: read data/live.js by re-inserting the script tag ---------- */
  function poll() {
    if (pollBusy || !shown) return;
    pollBusy = true;
    if (pollTag && pollTag.parentNode) pollTag.parentNode.removeChild(pollTag);
    var s = doc.createElement('script');
    s.src = 'data/live.js';
    s.onload = function () { pollBusy = false; onPolled(false); };
    s.onerror = function () { pollBusy = false; onPolled(true); };
    pollTag = s;
    doc.head.appendChild(s);
  }

  function onPolled(failed) {
    if (!shown) return;
    var v = failed ? null : window.LIVE;
    if (okLive(v) && String(v.state) === 'running' && source === 'replay') {
      pauseReplay();
      source = 'poll';
    }
    if (source === 'replay') return;
    safe(function () { ingest(v); });
  }

  function startPolling() {
    if (pollTimer) return;
    poll();
    pollTimer = window.setInterval(poll, POLL_MS);
  }
  function stopPolling() {
    if (pollTimer) window.clearInterval(pollTimer);
    pollTimer = 0;
    pollBusy = false;
    if (pollTag && pollTag.parentNode) pollTag.parentNode.removeChild(pollTag);
    pollTag = null;
  }

  /* ---------- feed: recorded key frames (window.LIVE_REPLAY) ---------- */
  /* window.LIVE_REPLAY is { frames: [...] } or { runs: [{ frames: [...] }, ...] } or a plain
     array of frames. A frame is a LIVE object, or { live: LIVE, dt: ms } or { live: LIVE, t: ms }.
     Without timing a frame waits REPLAY_STEP_MS. */
  function replayRuns() {
    if (rp.runs) return rp.runs;
    var R = window.LIVE_REPLAY;
    var runs = [];
    function add(src) {
      var arr = Array.isArray(src) ? src : (isObj(src) && (src.frames || src.key_frames)) || [];
      var frames = [];
      var prevT = 0;
      if (Array.isArray(arr)) {
        arr.forEach(function (f) {
          if (!isObj(f)) return;
          var live = isObj(f.live) ? f.live : f;
          if (!isObj(live.nodes) && !isObj(live.label) && !Array.isArray(live.rail)) return;
          var wait = REPLAY_STEP_MS;
          if (typeof f.dt === 'number') wait = f.dt;
          else if (typeof f.t === 'number') { wait = f.t - prevT; prevT = f.t; }
          if (!(wait >= 0)) wait = REPLAY_STEP_MS;
          if (wait > REPLAY_MAX_MS) wait = REPLAY_MAX_MS;
          frames.push({ wait: wait, live: live });
        });
      }
      if (frames.length) runs.push(frames);
    }
    if (isObj(R) && Array.isArray(R.runs)) R.runs.forEach(add); else add(R);
    rp.runs = runs;
    return runs;
  }

  function replayFrames() {
    var runs = replayRuns();
    return runs.length ? (runs[rp.run] || runs[0]) : [];
  }

  function showFrame(i) {
    var fr = replayFrames();
    if (i < 0 || i >= fr.length) return;
    rp.i = i;
    source = 'replay';
    safe(function () { ingest(fr[i].live, true); });
  }

  function replayTick() {
    rp.timer = 0;
    if (!rp.playing) return;
    var fr = replayFrames();
    var next = rp.i + 1;
    if (next >= fr.length) { rp.playing = false; return; }
    showFrame(next);
    if (next + 1 >= fr.length) { rp.playing = false; return; }
    rp.timer = window.setTimeout(replayTick, fr[next + 1].wait);
  }

  function pauseReplay() {
    rp.playing = false;
    if (rp.timer) window.clearTimeout(rp.timer);
    rp.timer = 0;
  }

  function playReplay() {
    var fr = replayFrames();
    if (!fr.length) return false;
    if (liveRunning()) return false;
    if (rp.i >= fr.length - 1) rp.i = -1;
    rp.playing = true;
    source = 'replay';
    if (rp.timer) window.clearTimeout(rp.timer);
    rp.timer = window.setTimeout(replayTick, rp.i < 0 ? 0 : fr[rp.i + 1].wait);
    return true;
  }

  function blankScreen() {
    resetRun();
    lastRev = null;
    lastUid = null;
    cur = null;
    renderAll(null);
  }

  /* a bridge run that is in progress owns the screen: Home, [ and ] leave it alone */
  function liveRunning() {
    return !!(cur && String(cur.state) === 'running' && source === 'poll');
  }

  function restartReplay() {
    if (!replayFrames().length || liveRunning()) return false;
    pauseReplay();
    source = 'replay';
    rp.i = -1;
    blankScreen();
    return true;
  }

  /* [ and ]: with several recorded runs, switch to the previous or next one (paused at its
     first frame); with one run, step one key frame back or forward. */
  function stepReplay(dir) {
    var runs = replayRuns();
    if (!runs.length || liveRunning()) return false;
    pauseReplay();
    if (runs.length > 1) {
      rp.run = (rp.run + dir + runs.length) % runs.length;
      rp.i = -1;
      source = 'replay';
      blankScreen();
      showFrame(0);
      return true;
    }
    var fr = runs[0];
    showFrame(Math.max(0, Math.min(fr.length - 1, rp.i + dir)));
    return true;
  }

  /* ---------- public API ---------- */
  function visible(flag) {
    flag = !!flag;
    if (flag === shown) return;
    shown = flag;
    if (flag) {
      if (!ensureBuilt()) { shown = false; return; }
      tileRefs = { sig: '', els: [] };
      renderAll(cur);
      startPolling();
    } else {
      stopPolling();
      pauseReplay();
      finishCurrent();
    }
  }

  function key(name) {
    if (!shown || !built) return false;
    if (name === 'space') {
      if (rp.playing) { pauseReplay(); return true; }
      return playReplay();
    }
    if (name === 'home') return restartReplay();
    if (name === 'prev') return stepReplay(-1);
    if (name === 'next') return stepReplay(1);
    return false;
  }

  window.Pipeline = { visible: visible, key: key };
})();
