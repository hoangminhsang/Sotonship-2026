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
   Screen coordinates follow PLAN.md A4: the content area is x 120-1800, y 150-1016 on the
   1920 x 1080 stage. Board space below has its origin at (120, 150). */
(function () {
  'use strict';

  var doc = document;
  /* the SVG namespace, read from a parsed literal tag so no URL string sits in this file */
  var SVGNS = (function () {
    try { return doc.createRange().createContextualFragment('<svg></svg>').firstChild.namespaceURI; } catch (e) { return null; }
  })();

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
  var TILE_STATES = ['broke', 'blocked', 'clean', 'error', 'handled', 'not_handled'];
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
  var rcRefs = [];
  var tileRefs = { sig: '', els: [] };
  var glide = null;
  var reduceMq = null;
  var rp = { frames: null, i: -1, timer: 0, playing: false };

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
  function liveStatus(v) {
    return v && v.label && v.label.status === 'REAL' ? 'REAL' : 'SAMPLE';
  }
  /* a value is REAL only when the data says so */
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
      refs[n.id] = { li: li, g: g, word: word, script: scr, tag: tag, cap: cap, fact: fact, capSig: null, factSig: null, tagSig: null };
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

  function buildRail(ol) { clear(ol); rcRefs = []; }

  function ensureBuilt() {
    if (built) return true;
    var sec = $('screen-6');
    if (!sec) return false;
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
    buildRail(rail);
    try { reduceMq = window.matchMedia('(prefers-reduced-motion: reduce)'); } catch (e) { reduceMq = null; }
    var g = '';
    try { g = window.getComputedStyle(doc.documentElement).getPropertyValue('--glide').trim(); } catch (e2) { g = ''; }
    glide = g || GLIDE_FALLBACK;
    built = true;
    return true;
  }

  /* ---------- render: nodes ---------- */
  function nodeLook(nd, v) {
    var s = normState(nd && nd.state);
    var status = statusOf(nd, v);
    var real = status === 'REAL';
    var scripted = s !== 'idle' && s !== 'skipped' && (s === 'scripted' || !real);
    var fill = '';
    if (real && s === 'fail') fill = 'fail';
    if (real && s === 'pass') fill = 'pass';
    return { state: s, status: status, scripted: scripted, fill: fill };
  }

  function renderNode(id, v) {
    var r = refs[id];
    if (!r) return;
    var nd = v && v.nodes && isObj(v.nodes[id]) ? v.nodes[id] : null;
    var look = nodeLook(nd, v);
    var li = r.li;
    setAttr(li, 'data-state', look.state);
    setAttr(li, 'data-status', look.status);
    setAttr(li, 'data-fill', look.fill || null);
    setAttr(li, 'data-scripted', look.scripted ? '1' : null);
    setAttr(li, 'aria-current', v && v.active === id ? 'step' : null);
    li.classList.toggle('is-sample', look.scripted);
    setText(r.word, cState(look.state));
    if (look.scripted && look.state !== 'scripted') {
      r.script.hidden = false;
      setText(r.script, cState('scripted'));
    } else {
      r.script.hidden = true;
    }
    var completed = DONE_STATES.indexOf(look.state) >= 0;
    var which = completed ? 'done' : 'running';
    var text = cNode(id, which);
    var base = 'live:nodes.' + id;
    var capSig = which + '\u0001' + text + '\u0001' + (nd ? [nd.fact, nd.fact2, nd.tag, nd.verdict, nd.decision, look.status].join('\u0002') : '');
    if (r.capSig !== capSig) {
      r.capSig = capSig;
      clear(r.cap);
      r.cap.appendChild(tpl(text, nd, base, look.status));
    }
    var factSig = nd ? [nd.fact, nd.fact2, look.status].join('\u0002') : '';
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
    var tagSig = nd && scalar(nd.tag) ? String(nd.tag) + look.status : '';
    if (r.tagSig !== tagSig) {
      r.tagSig = tagSig;
      clear(r.tag);
      if (tagSig) r.tag.appendChild(valSpan(String(nd.tag), base + '.tag', look.status));
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
    var chip = valSpan(String(text), 'live:label', lb.status === 'REAL' ? 'REAL' : 'SAMPLE');
    chip.className = 'p-chip';
    host.appendChild(chip);
  }

  function renderRail(v) {
    var ol = $('p-rail');
    var list = v && Array.isArray(v.rail) ? v.rail : [];
    clear(ol);
    rcRefs = [];
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
      if (scalar(rc.text)) r2.appendChild(valSpan(String(rc.text), base + '.text', status));
      li.appendChild(r2);
      ol.appendChild(li);
      rcRefs.push(li);
    });
  }

  function tileSize(n, w, h) {
    var best = 3;
    for (var rows = 1; rows <= 2; rows++) {
      var cols = Math.ceil(n / rows);
      var s = Math.min(26, Math.floor((h - (rows - 1) * 2) / rows), Math.floor((w - (cols - 1) * 2) / cols));
      if (s > best) best = s;
    }
    return best;
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
    var labels = window.DEMO && window.DEMO.labels && window.DEMO.labels.states;
    tileRefs.els.forEach(function (tile, i) {
      var item = list[i];
      var key = Array.isArray(item) ? item[0] : null;
      var st = Array.isArray(item) && TILE_STATES.indexOf(item[1]) >= 0 ? item[1] : 'idle';
      setAttr(tile, 'data-state', st);
      var title = '';
      if (key !== null && key !== undefined) title = String(key) + (labels && labels[st] ? ' ' + labels[st] : '');
      setAttr(tile, 'title', title || null);
    });
    if (tileRefs.grid) setAttr(tileRefs.grid, 'aria-label', cand || null);
  }

  function activeNode(v) {
    return v && v.active && v.nodes && isObj(v.nodes[v.active]) ? v.nodes[v.active] : null;
  }

  function renderStatus(v) {
    var p = $('p-status');
    var frag = doc.createDocumentFragment();
    var st = v ? String(v.state || '') : '';
    var status = v ? liveStatus(v) : 'SAMPLE';
    if (!v || st === 'waiting' || st === '') {
      frag.appendChild(doc.createTextNode(replayFrames().length ? cPipe('replay_hint') : cState('idle')));
    } else if (st === 'done') {
      var d = isObj(v.done) ? v.done : {};
      var ex = isObj(d.export) ? d.export : null;
      var okExport = !ex || !scalar(ex.status) || EXPORT_OK.indexOf(String(ex.status).toLowerCase()) >= 0;
      var ctx = {
        t: first(d.t, d.dur, d.duration, d.dur_s, d.took, d.time, v.t, v.dur, v.duration),
        cmd: first(ex && ex.cmd, ex && ex.command, d.cmd, ex && ex.text)
      };
      frag.appendChild(tpl(cPipe(okExport ? 'status_done' : 'status_export_missing'), ctx, 'live:done', status));
    } else {
      var rc = runningCand(v);
      if (rc) {
        frag.appendChild(valSpan(rc, 'live:rail.cand', status));
        frag.appendChild(doc.createTextNode(' · '));
      }
      frag.appendChild(doc.createTextNode(cState(st === 'running' ? 'running' : 'error')));
      var an = activeNode(v);
      if (an && v.active) {
        var capKey = DONE_STATES.indexOf(normState(an.state)) >= 0 ? 'done' : 'running';
        frag.appendChild(doc.createTextNode(' · '));
        frag.appendChild(tpl(cNode(v.active, capKey), an, 'live:nodes.' + v.active, statusOf(an, v)));
      }
    }
    var sig = frag.textContent;
    if (p.getAttribute('data-sig') === sig + '|' + status) return;
    p.setAttribute('data-sig', sig + '|' + status);
    clear(p);
    p.appendChild(frag);
  }

  function runningCand(v) {
    var list = v && Array.isArray(v.rail) ? v.rail : [];
    for (var i = 0; i < list.length; i++) {
      if (isObj(list[i]) && normState(list[i].state) === 'running' && scalar(list[i].id)) return String(list[i].id);
    }
    return '';
  }

  function renderSrc(v) {
    var host = $('p-src');
    clear(host);
    if (!v) return;
    var status = liveStatus(v);
    var items = [
      ['mode', v.mode], ['model', v.model], ['digest', v.digest],
      ['source.log', v.source && v.source.log]
    ];
    var w = Array.isArray(v.warnings) ? v.warnings : [];
    w.forEach(function (x, i) { items.push(['warnings.' + i, x]); });
    var firstItem = true;
    items.forEach(function (it) {
      if (!scalar(it[1])) return;
      if (!firstItem) host.appendChild(doc.createTextNode(' · '));
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
  function inkShapes(g) {
    var list = g.querySelectorAll('.f-ink, .f-thin');
    if (!list.length) list = g.querySelectorAll('path, line, polyline, polygon, rect, circle, ellipse');
    return list;
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
    var n = shapes.length;
    var spread = n > 1 ? DRAW_SPREAD_MS : 0;
    for (var i = 0; i < n; i++) {
      var len = 0;
      try { len = shapes[i].getTotalLength(); } catch (e) { len = 0; }
      if (!(len > 0) || typeof shapes[i].animate !== 'function') continue;
      var dd = len + ' ' + len;
      list.push(shapes[i].animate(
        [{ strokeDasharray: dd, strokeDashoffset: len }, { strokeDasharray: dd, strokeDashoffset: 0 }],
        { duration: DRAW_MS - spread, delay: delay + (n > 1 ? i * spread / (n - 1) : 0), easing: DRAW_EASE, fill: 'both' }));
    }
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
    s.onload = function () { pollBusy = false; onPolled(); };
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
  function replayFrames() {
    if (rp.frames) return rp.frames;
    var R = window.LIVE_REPLAY;
    var arr = Array.isArray(R) ? R : (R && (R.frames || R.key_frames)) || [];
    var out = [];
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
        if (R && !Array.isArray(R) && isObj(R.label) && !isObj(live.label)) live.label = R.label;
        out.push({ wait: wait, live: live });
      });
    }
    rp.frames = out;
    return out;
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
    if (cur && String(cur.state) === 'running' && source === 'poll') return false;
    if (rp.i >= fr.length - 1) rp.i = -1;
    rp.playing = true;
    source = 'replay';
    if (rp.timer) window.clearTimeout(rp.timer);
    rp.timer = window.setTimeout(replayTick, rp.i < 0 ? 0 : fr[rp.i + 1].wait);
    return true;
  }

  function restartReplay() {
    var fr = replayFrames();
    if (!fr.length) return false;
    pauseReplay();
    source = 'replay';
    rp.i = -1;
    resetRun();
    lastRev = null;
    lastUid = null;
    cur = null;
    renderAll(null);
    return true;
  }

  function stepReplay(dir) {
    var fr = replayFrames();
    if (!fr.length) return false;
    pauseReplay();
    var next = Math.max(0, Math.min(fr.length - 1, rp.i + dir));
    showFrame(next);
    return true;
  }

  /* ---------- public API ---------- */
  function visible(flag) {
    flag = !!flag;
    if (flag === shown) return;
    shown = flag;
    if (flag) {
      if (!ensureBuilt()) { shown = false; return; }
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
