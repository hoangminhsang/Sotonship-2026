/* Redline: demo UI behaviour.
   Reads window.DEMO (schema "ui-demo/1", written by ui/export.py) and draws seven screens.
   Words come from window.COPY (ui/copy.js). The pipeline view (screen 6) is drawn by pipeline.js;
   every call into it is guarded, because the page must work without it.
   Rules kept here: every data value goes through F() or V() into an element with [data-prov];
   text is set with textContent only; no data number is formatted in this file (the only
   arithmetic on data is SVG geometry inside meter()); state is kept in location.hash only. */
(function () {
  'use strict';

  var D = window.DEMO;
  var doc = document;
  var C = window.COPY || {};
  /* The screens by name. A screen's key digit is its place in this list, so the digits live here and
     nowhere else: copy.js names screens, never numbers them. */
  var ORDER = ['console', 'attacks', 'compare', 'decisions', 'assurance', 'pipeline', 'models'];
  var SCREEN_COUNT = ORDER.length;
  var PIPELINE_SCREEN = ORDER.indexOf('pipeline') + 1;
  /* the SVG namespace, read from a parsed literal tag so no URL string sits in this file */
  var SVGNS = (function () {
    try { return doc.createRange().createContextualFragment('<svg></svg>').firstChild.namespaceURI; } catch (e) { return null; }
  })();
  var STAGE_W = 1920;
  var STAGE_H = 1080;
  var DENSE_AT = 30;
  var IDLE_MS = 2000;
  var STATUS_WORDS = { REAL: 'REAL', SAMPLE: 'SAMPLE', PENDING: 'PENDING' };
  var ATTACK_STATES = ['broke', 'blocked', 'clean', 'error'];
  var BENIGN_STATES = ['handled', 'not_handled', 'broke', 'error'];
  var EVENT_LABELS = {
    submit: 'Submitted', verdict: 'Gate verdict', decision: 'Decision',
    decision_refused: 'Refused', role_change: 'Role change', unreadable: 'Unreadable'
  };
  var TRACE_TEXT = {
    'not-committed': 'Trace file not committed',
    ok: 'Trace file read and matched',
    mismatch: 'Trace file does not match this row',
    missing: 'Trace file not found'
  };

  var state = { s: 1, p: false, run: null, tech: null, batch: null, d: false, card: false };
  var beatIndex = -1;       /* the beat shown now (index into COPY.beats), -1 before the first B */
  var lastHash = '';
  var deckReady = false;
  var drawerOpen = false;
  var drawerReturnKey = null;
  var inertSet = [];
  var rovingKey = null;
  var idleTimer = 0;
  var resizeTimer = 0;

  /* ---------- small DOM helpers ---------- */

  function $(id) { return doc.getElementById(id); }

  /* copy('screens.console.nav'): one word from window.COPY, or '' when it is missing.
     {name} placeholders are filled from vars; an unknown placeholder stays visible, so a typo shows. */
  function copy(path, vars) {
    var v = C;
    var parts = String(path).split('.');
    for (var i = 0; i < parts.length && v; i++) v = v[parts[i]];
    if (typeof v !== 'string') return '';
    if (!vars) return v;
    return v.replace(/\{([a-z_]+)\}/g, function (m, k) { return vars[k] !== undefined ? vars[k] : m; });
  }

  /* the demo beats, in the order copy.js lists them; app.js never names a beat */
  function beatList() { return Array.isArray(C.beats) ? C.beats : []; }

  function isNode(x) { return x && typeof x === 'object' && typeof x.nodeType === 'number'; }

  /* el(tag, attrs, kids): plain-string kids are UI copy, so their parent gets [data-static]. */
  function el(tag, attrs, kids) {
    var n = doc.createElement(tag);
    setAttrs(n, attrs);
    add(n, kids);
    return n;
  }

  function setAttrs(n, attrs) {
    if (!attrs) return;
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === 'class') n.setAttribute('class', v);
      else if (k === 'text') n.textContent = String(v);
      else n.setAttribute(k, v === true ? '' : String(v));
    });
  }

  function add(n, kids) {
    if (kids === null || kids === undefined || kids === false) return n;
    if (!Array.isArray(kids)) kids = [kids];
    kids.forEach(function (k) {
      if (k === null || k === undefined || k === false) return;
      if (Array.isArray(k)) { add(n, k); return; }
      if (isNode(k)) { n.appendChild(k); return; }
      if (!n.hasAttribute('data-prov')) n.setAttribute('data-static', '');
      n.appendChild(doc.createTextNode(String(k)));
    });
    return n;
  }

  function svg(tag, attrs) {
    var n = doc.createElementNS(SVGNS, tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (attrs[k] !== null && attrs[k] !== undefined) n.setAttribute(k, String(attrs[k]));
    });
    return n;
  }

  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

  /* fill(): replace only what app.js drew before; static markup from index.html stays. */
  function fill(container, nodes) {
    if (!container) return null;
    Array.prototype.slice.call(container.querySelectorAll(':scope > [data-gen]')).forEach(function (c) {
      container.removeChild(c);
    });
    (function put(list) {
      (Array.isArray(list) ? list : [list]).forEach(function (c) {
        if (!c) return;
        if (Array.isArray(c)) { put(c); return; }
        c.setAttribute('data-gen', '');
        container.appendChild(c);
      });
    })(nodes);
    return container;
  }

  function hasStaticHeading(container) {
    if (!container) return false;
    return Array.prototype.some.call(container.children, function (c) {
      return !c.hasAttribute('data-gen') && /^H[1-6]$/.test(c.tagName);
    });
  }

  /* A panel heading, only when index.html did not already give the container one. */
  function head(container, text) {
    return hasStaticHeading(container) ? null : el('h2', { class: 'panel-title' }, text);
  }

  /* ---------- data value renderers ---------- */

  function isField(f) { return f && typeof f === 'object' && 'text' in f; }

  /* F(field): one data value, with its provenance id and status. textContent only. */
  function F(f, cls) {
    if (!isField(f)) {
      var p = el('span', { 'data-prov': '', 'data-status': 'PENDING', class: cls || null });
      p.textContent = 'PENDING';
      return p;
    }
    var s = el('span', { 'data-prov': f.prov || '', 'data-status': f.status || 'PENDING', class: cls || null });
    s.textContent = f.text === null || f.text === undefined ? '' : String(f.text);
    return s;
  }

  /* V(text): a raw string taken from window.DEMO that is not a Field (labels, ids, file names). */
  function V(text, cls, prov) {
    var s = el('span', { 'data-prov': prov || '', class: cls || null });
    s.textContent = text === null || text === undefined ? '' : String(text);
    return s;
  }

  function word(status) { return STATUS_WORDS[status] || 'PENDING'; }

  /* tag(status, text): status token. The word is always present, never colour alone. */
  function tag(status, text) {
    var t = el('span', { class: 'tag', 'data-status': word(status), 'data-prov': '' });
    t.textContent = text ? String(text) : word(status);
    return t;
  }

  function src(kids) { return el('p', { class: 'src' }, kids); }

  function pendingBlock(lines, reason) {
    return el('div', { class: 'pending-block' }, [
      el('p', null, [tag('PENDING'), ' ', lines]),
      reason ? el('p', { class: 'reason' }, V(reason)) : null
    ]);
  }

  function quote(text) { return el('p', { class: 'quote' }, text); }

  /* dt and dd sit directly in the dl, so the two-column grid in app.css lines them up */
  function row(label, value) {
    return [el('dt', null, label), el('dd', null, value)];
  }

  function dl(rows) { return el('dl', { class: 'kvs' }, rows); }

  function setPanelStatus(container, status) {
    if (!container) return;
    container.classList.add('panel');
    container.classList.toggle('is-sample', status === 'SAMPLE');
    container.classList.toggle('is-pending', status === 'PENDING');
  }

  function byBatch(id) {
    var list = (D && D.batches) || [];
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
    return null;
  }

  function currentBatch() {
    return byBatch(state.batch) || byBatch(D.current_batch) || (D.batches && D.batches[0]) || null;
  }

  function label(map, key) {
    var labels = (D && D.labels && D.labels[map]) || {};
    return labels[key] || key;
  }

  function guard(name, fn) {
    try { fn(); } catch (err) {
      if (window.console) console.error((copy('product.name') || 'Dashboard') + ': could not draw ' + name, err);
    }
  }

  /* ---------- hash state ---------- */

  function readHash() {
    var q;
    try { q = new URLSearchParams(location.hash.replace(/^#/, '')); } catch (e) { return; }
    var s = parseInt(q.get('s') || '', 10);
    state.s = s >= 1 && s <= SCREEN_COUNT ? s : 1;
    state.p = q.get('p') === '1';
    state.card = q.get('card') === '1';
    state.run = q.get('run') || null;
    state.tech = q.get('tech') || null;
    state.batch = q.get('batch') || null;
    state.d = q.get('d') === '1';
  }

  function writeHash() {
    var q = new URLSearchParams();
    q.set('s', String(state.s));
    if (state.p) q.set('p', '1');
    if (state.card && state.s === PIPELINE_SCREEN) q.set('card', '1');
    if (state.batch) q.set('batch', state.batch);
    if (state.tech) q.set('tech', state.tech);
    if (state.run) q.set('run', state.run);
    if (state.d) q.set('d', '1');
    var h = '#' + q.toString();
    lastHash = h;
    if (location.hash === h) return;
    try { history.replaceState(null, '', h); } catch (e) {
      try { location.replace(h); } catch (e2) { /* hash is a convenience only */ }
    }
  }

  /* ---------- stage fit and the deck line ---------- */

  function fit() {
    var stage = $('stage');
    if (!stage) return;
    var w = stage.offsetWidth || STAGE_W;
    var h = stage.offsetHeight || STAGE_H;
    var s = Math.min(innerWidth / w, innerHeight / h);
    stage.style.transform = 'translate(' + (innerWidth - w * s) / 2 + 'px,' + (innerHeight - h * s) / 2 + 'px) scale(' + s + ')';
  }

  /* position inside #stage from offsets, which ignore the running scale transform */
  function rel(node) {
    var stage = $('stage');
    var x = 0, y = 0, e = node;
    while (e && e !== stage) { x += e.offsetLeft; y += e.offsetTop; e = e.offsetParent; }
    return { x: x, y: y, w: node.offsetWidth, h: node.offsetHeight };
  }

  function reducedMotion() {
    try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; }
  }

  function navItem(n) { return doc.querySelector('#nav .nav-item[data-screen="' + n + '"]'); }

  function placeDeck(mode) {
    var deck = $('deck');
    var target = navItem(state.s);
    if (!deck || !target || !deckReady) return;
    var t = rel(target);
    var base = deck.offsetParent && deck.offsetParent !== $('stage') ? rel(deck.offsetParent) : { x: 0, y: 0 };
    var nav = $('nav');
    var gap = nav && nav.getAttribute('data-gap') !== null ? Number(nav.getAttribute('data-gap')) : 6;
    var x = t.x - base.x;
    var y = t.y + t.h + gap - 1 - base.y;
    if (reducedMotion() && mode !== 'none') mode = 'instant';
    deck.classList.remove('draw');
    if (mode === 'draw') {
      deck.style.transition = 'none';
      deck.style.left = x + 'px'; deck.style.top = y + 'px'; deck.style.width = '0px';
      void deck.offsetWidth;
      deck.style.transition = '';
      deck.classList.add('draw');
      deck.style.width = t.w + 'px';
    } else {
      if (mode === 'instant') deck.style.transition = 'none';
      deck.style.left = x + 'px'; deck.style.top = y + 'px'; deck.style.width = t.w + 'px';
      if (mode === 'instant') { void deck.offsetWidth; deck.style.transition = ''; }
    }
  }

  function fontsReady(ms) {
    var faces = ['300 40px "Cormorant Garamond"', '400 40px "Cormorant Garamond"',
      'italic 300 40px "Cormorant Garamond"', '400 40px "Inter"', '500 40px "Inter"'];
    var load;
    try {
      load = doc.fonts && doc.fonts.load
        ? Promise.all(faces.map(function (f) { return doc.fonts.load(f).catch(function () { return null; }); }))
        : Promise.resolve();
    } catch (e) { load = Promise.resolve(); }
    var timeout = new Promise(function (resolve) { setTimeout(resolve, ms); });
    return Promise.race([load, timeout]).then(function () {}, function () {});
  }

  /* ---------- header: chips, banner, REPLAY, stamp, warnings ---------- */

  function samplePresent() {
    if (D.sample_present) return true;
    return (D.datasets || []).some(function (d) { return d.status === 'SAMPLE'; });
  }

  function renderChips() {
    var box = $('chips');
    if (!box) return;
    clear(box);
    (D.datasets || []).forEach(function (ds, i) {
      if (i) box.appendChild(doc.createTextNode(' '));
      var chip = el('span', {
        class: 'chip', 'data-status': word(ds.status), 'data-prov': '', 'data-key': ds.key,
        title: ds.reason ? ds.reason : (ds.source_file || null)
      }, [V(ds.label, 'chip-label'), ' ', V(word(ds.status), 'chip-word')]);
      box.appendChild(chip);
    });
  }

  function renderBanner() {
    var b = $('banner');
    if (!b) return;
    clear(b);
    if (samplePresent()) {
      add(b, el('p', null, 'SAMPLE data on screen — not produced by the backend'));
      b.hidden = false;
    } else {
      b.hidden = true;
    }
  }

  function renderStamp() {
    var s = $('stamp');
    if (!s) return;
    clear(s);
    add(s, ['Exported ', V(D.exported_at)]);
  }

  function renderWarnings() {
    var w = $('warn');
    if (!w) return;
    clear(w);
    /* integrity warnings first and always (presenter too); info notes only outside presenter mode.
       The footer holds two lines, so a longer list ends with a counted pointer to the export report. */
    var all = D.warnings || [];
    var integrity = all.filter(function (x) { return x.level === 'integrity'; });
    var info = all.filter(function (x) { return x.level !== 'integrity'; });
    var shown = integrity.concat(state.p ? [] : info);
    var MAX = 2;
    var visible = shown.length > MAX ? shown.slice(0, MAX - 1) : shown;
    visible.forEach(function (item) {
      w.appendChild(el('p', { class: 'warn-line', 'data-level': item.level === 'integrity' ? 'integrity' : 'info', title: item.text }, [
        el('span', { class: 'warn-level' }, item.level === 'integrity' ? 'Integrity:' : 'Note:'), ' ', V(item.text)
      ]));
    });
    if (shown.length > visible.length) {
      var rest = shown.slice(visible.length);
      var restIntegrity = rest.some(function (x) { return x.level === 'integrity'; });
      w.appendChild(el('p', {
        class: 'warn-line', 'data-level': restIntegrity ? 'integrity' : 'info',
        title: rest.map(function (x) { return x.text; }).join('\n')
      }, [V(String(rest.length), 'warn-count'), restIntegrity ? ' more warnings, with integrity ones. The export report lists them.' : ' more notes. The export report lists them.']));
    }
  }

  function replayLabelsFor(n) {
    if (n === 1) {
      var eb = byBatch(D.console && D.console.evidence_batch) || currentBatch();
      return eb ? [eb.replay_label] : [];
    }
    if (n === 2) {
      var b = currentBatch();
      return b ? [b.replay_label] : [];
    }
    if (n === 3) {
      var c = D.compare || {};
      var out = [];
      if (c.a && c.a.status !== 'PENDING' && c.a.replay_label) out.push('A: ' + c.a.replay_label);
      if (c.b && c.b.status !== 'PENDING' && c.b.replay_label) out.push('B: ' + c.b.replay_label);
      return out;
    }
    /* the pipeline screen shows its own mode chip (LIVE, REPLAY, SCRIPTED); the Models table tags each row */
    if (n === PIPELINE_SCREEN || n === ORDER.indexOf('models') + 1) return [];
    return [];
  }

  function renderReplay(n) {
    var r = $('replay');
    if (!r) return;
    clear(r);
    var labels = D ? replayLabelsFor(n).filter(Boolean) : [];
    labels.forEach(function (t, i) {
      if (i) r.appendChild(doc.createTextNode(' '));
      r.appendChild(V(t, 'replay-label'));
    });
    r.hidden = labels.length === 0;
  }

  /* The key hints; the digits come from the router, the words from copy.js.
     While a beat is active the line names the beat instead (hidden in presenter mode with the other hints). */
  function renderHints() {
    var h = $('hints');
    if (!h) return;
    clear(h);
    var b = beatList()[beatIndex];
    if (b && b.title) {
      add(h, (copy('hints.beat_label') || 'Beat') + ': ' + b.title);
      return;
    }
    var keys = state.s === PIPELINE_SCREEN && copy('hints.pipeline') ? copy('hints.pipeline')
      : copy('hints.keys', { first: '1', last: String(SCREEN_COUNT) });
    add(h, keys || ('Keys 1 to ' + SCREEN_COUNT));
  }

  /* ---------- meter: the only place that turns data numbers into geometry ---------- */

  function meter(metric, limitField) {
    if (!metric || !metric.measured || !metric.geom || !SVGNS) return null;
    var g = metric.geom;
    var W = 1000, H = 72, pad = 12, axisY = 50;
    function num(v) { return typeof v === 'number' && isFinite(v) ? Math.max(0, Math.min(1, v)) : null; }
    function x(v) { return pad + v * (W - 2 * pad); }
    var rate = num(g.rate), lo = num(g.lo), hi = num(g.hi);

    /* limit position: an explicit geom on the limit, else the raw value in its provenance entry */
    var lim = null;
    if (limitField) {
      if (typeof limitField.geom === 'number') lim = num(limitField.geom);
      else if (limitField.geom && typeof limitField.geom.value === 'number') lim = num(limitField.geom.value);
      else if (g && typeof g.limit === 'number') lim = num(g.limit);
      else if (limitField.prov) {
        var p = (D.provenance || []).filter(function (e) { return e.id === limitField.prov; })[0];
        if (p && p.raw !== null && p.raw !== undefined && /^\s*-?\d*\.?\d+\s*$/.test(String(p.raw))) {
          var raw = parseFloat(p.raw);
          if (raw >= 0 && raw <= 1) lim = raw;
        }
      }
    }

    var aria = 'Rate ' + (metric.rate ? metric.rate.text : '') +
      (metric.ci ? ', range ' + metric.ci.text : '') +
      (limitField && lim !== null ? ', release limit ' + limitField.text : '');
    var s = svg('svg', { class: 'meter', viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': aria,
      preserveAspectRatio: 'xMinYMid meet', 'data-prov': metric.rate ? (metric.rate.prov || '') : '' });

    if (lo !== null && hi !== null) {
      s.appendChild(svg('rect', { class: 'm-ci', x: x(lo), y: axisY - 14, width: Math.max(2, x(hi) - x(lo)), height: 28,
        fill: 'currentColor', 'fill-opacity': '0.16', stroke: 'currentColor', 'stroke-width': 3 }));
    }
    var axis = svg('g', { class: 'm-axis' });
    axis.appendChild(svg('line', { x1: x(0), y1: axisY, x2: x(1), y2: axisY, stroke: 'currentColor', 'stroke-width': 2 }));
    axis.appendChild(svg('line', { x1: x(0) + 1, y1: axisY - 10, x2: x(0) + 1, y2: axisY + 10, stroke: 'currentColor', 'stroke-width': 2 }));
    axis.appendChild(svg('line', { x1: x(1) - 1, y1: axisY - 10, x2: x(1) - 1, y2: axisY + 10, stroke: 'currentColor', 'stroke-width': 2 }));
    s.appendChild(axis);
    if (lim !== null) {
      s.appendChild(svg('line', { class: 'm-limit', x1: x(lim), y1: 26, x2: x(lim), y2: H - 1,
        stroke: 'currentColor', 'stroke-width': 3, 'stroke-dasharray': '8 6' }));
    }
    if (rate !== null) {
      s.appendChild(svg('rect', { class: 'm-rate', x: x(rate) - 4, y: axisY - 20, width: 8, height: 40, fill: 'currentColor' }));
    }
    var t0 = svg('text', { class: 'm-tick', x: x(0), y: 18, 'text-anchor': 'start', 'data-static': '', fill: 'currentColor' });
    t0.textContent = '0%';
    var t1 = svg('text', { class: 'm-tick', x: x(1), y: 18, 'text-anchor': 'end', 'data-static': '', fill: 'currentColor' });
    t1.textContent = '100%';
    s.appendChild(t0);
    s.appendChild(t1);
    return s;
  }

  /* hero block for a Metric: counts first, then the rate, then the range, then the meter */
  function heroMetric(metric, what, limitField, compact) {
    var out = [];
    if (!metric) { out.push(pendingBlock('No measurement in this batch.')); return out; }
    if (metric.measured) {
      /* the count and its words share one line; the words wrap below when the column is narrow */
      out.push(el('p', { class: 'hero' }, [F(metric.x_of_n, (compact ? 'num-mid' : 'num-hero') + ' x-of-n'), ' ', el('span', { class: 'hero-what' }, what)]));
      out.push(el('p', { class: 'rate-line' }, [
        metric.rate ? F(metric.rate, 'num-mid') : null,
        metric.ci ? el('span', null, ['range ', F(metric.ci)]) : null,
        limitField ? el('span', null, ['release limit ', F(limitField)]) : null
      ]));
      var m = meter(metric, limitField);
      if (m) out.push(m);
    } else {
      out.push(el('p', { class: 'lede hero-what' }, [what.replace(/ broke a rule.*$/, ''), ': ', F(metric.x_of_n)]));
    }
    if (metric.note) out.push(src(V(metric.note)));
    return out;
  }

  function unitWhat(metric) {
    return metric && metric.unit === 'tests' ? 'tests broke a rule at least once' : 'attack runs broke a rule';
  }

  function policyLimit(key) {
    var limits = (D.policy && D.policy.limits) || [];
    for (var i = 0; i < limits.length; i++) if (limits[i].key === key) return limits[i].text;
    return null;
  }

  /* ---------- screen 1: release console ---------- */

  /* sampleNote(): short SAMPLE tag, then the rest of the dataset's sample_tag as plain text, so it can wrap */
  function sampleNote(text, fallback) {
    var t = String(text || fallback || '');
    var rest = t.replace(/^SAMPLE\s*·\s*/, '');
    return el('p', { class: 'sample-note' }, [tag('SAMPLE'), ' ', rest && rest !== t ? V(rest) : V(t)]);
  }

  function renderConsole() {
    var C = D.console || {};
    var cand = C.candidate || {};
    var batch = byBatch(C.evidence_batch) || currentBatch();
    var gate = C.gate || { status: 'PENDING' };
    var gateShown = gate.status !== 'PENDING' && !!gate.verdict;
    var audit = D.audit || {};
    var s1 = $('screen-1');
    if (s1) s1.setAttribute('data-gate', gateShown ? word(gate.status) : 'PENDING');

    /* candidate strip; the latest human decision sits here too, because it answers the screen title */
    var decision;
    if (C.latest_decision) decision = F(C.latest_decision);
    else if (audit.status === 'PENDING' || !audit.status) decision = tag('PENDING', 'PENDING');
    else decision = el('span', null, 'none recorded yet');
    var cc = $('c-candidate');
    fill(cc, [
      dl([
        row('Candidate', F(cand.id)),
        row('Model', F(cand.model)),
        row('Digest', F(cand.digest)),
        row('Defences', F(cand.defences)),
        row('Latest decision', decision)
      ])
    ]);
    if (cc && cand.note) cc.setAttribute('title', cand.note);

    var ce = $('c-evidence');
    if (!batch) {
      fill(ce, [head(ce, 'Evidence'), pendingBlock('No test batch yet. Run the attack lab, then export.')]);
    } else {
      var sm = batch.summary || {};
      var counts = (batch.counts && batch.counts.attack) || {};
      var benign = sm.benign_task_success;
      fill(ce, [
        el('h2', { class: 'panel-title' }, ['Evidence: ', V(batch.title)]),
        heroMetric(sm.attack_success_rate, unitWhat(sm.attack_success_rate), policyLimit('max_attack_success_rate')),
        el('ul', { class: 'facts' }, [
          el('li', null, [
            sm.ever_hijacked_rate ? [F(sm.ever_hijacked_rate.x_of_n), ' ', unitWhat(sm.ever_hijacked_rate), '. '] : null,
            'Normal requests handled: ', benign ? F(benign.x_of_n) : F(null),
            benign && benign.measured && benign.rate ? [', ', F(benign.rate)] : null, '.'
          ]),
          el('li', null, [F(counts.runs), ' runs in the file, ', F(counts.error), ' errored. Summary file: ', F(sm.error_runs), '.'])
        ])
      ]);
    }

    var cl = $('c-limits');
    var P = D.policy || {};
    setPanelStatus(cl, P.status);
    if (P.status === 'PENDING' || !P.limits) {
      fill(cl, [head(cl, 'Release limits'), pendingBlock('No policy file read.', P.reason)]);
    } else if (gateShown) {
      /* the gate's check lines already show each limit it applied; keep the policy limits to one short block */
      fill(cl, [
        P.status === 'SAMPLE' ? tag('SAMPLE') : null,
        el('p', { class: 'src limits-compact' }, ['Release limits, policy ', F(P.version), ': ',
          P.limits.map(function (l, i) { return [i ? '; ' : null, V(l.label), ' ', F(l.text)]; }), '.'])
      ]);
    } else {
      fill(cl, [
        el('h2', { class: 'panel-title' }, ['Release limits, policy ', F(P.version)]),
        P.status === 'SAMPLE' ? tag('SAMPLE') : null,
        dl(P.limits.map(function (l) { return row(V(l.label), F(l.text)); }))
      ]);
    }

    renderVerdict(gate, gateShown);

    var cs = $('c-src');
    fill(cs, [
      batch ? src(['Evidence: ', V((batch.source_files || []).join(', ')), batch.produced ? [' (', V(batch.produced.text), ')'] : null, '. ',
        cand.note ? V(cand.note) : null]) : null,
      src([P.source_file ? ['Policy: ', V(P.source_file), ', owner ', F(P.owner), '. '] : null,
        'Gate: ', V(gate.source_file || 'no file'), ' ', tag(gate.status)])
    ]);
  }

  function renderVerdict(gate, gateShown) {
    var cv = $('c-verdict');
    var kids = [];
    if (!gateShown) {
      setPanelStatus(cv, 'PENDING');
      kids.push(head(cv, 'Gate verdict'));
      kids.push(pendingBlock('No gate output yet.', gate.reason));
    } else {
      /* the verdict block itself carries the REAL or SAMPLE styling, so the container gets no second hatch */
      if (cv) { cv.classList.add('panel'); cv.classList.remove('is-sample', 'is-pending'); }
      kids.push(el('h2', { class: 'panel-title' }, ['Gate verdict for ', F(gate.candidate_id)]));
      if (gate.status === 'SAMPLE') kids.push(sampleNote(gate.sample_tag, 'SAMPLE · hand-written, not produced by the gate'));
      var lines = gate.raw_lines || [];
      var lastLine = lines.length ? lines[lines.length - 1] : '';
      var tail = lastLine.replace(/^VERDICT:\s*\S+\s*/, '');
      /* OK / FAIL marks come only from the gate's own lines (check.mark), never from comparing numbers here */
      kids.push(el('div', { class: 'verdict', 'data-verdict': gate.verdict.text, 'data-status': word(gate.verdict.status || gate.status) }, [
        el('p', { class: 'verdict-word', title: lastLine || null }, [F(gate.verdict, 'num-mid'), tail ? [' ', V(tail, 'verdict-tail', gate.verdict.prov)] : null]),
        (gate.checks || []).length ? el('ul', { class: 'checks' }, gate.checks.map(function (c) {
          return el('li', { 'data-mark': c.mark }, [
            V(c.mark, 'mark'),
            el('span', { class: 'check-main' }, [V(c.label || c.name), ' ', F(c.value), ', limit ', F(c.limit)]),
            c.note ? el('span', { class: 'note' }, F(c.note)) : null
          ]);
        })) : null
      ]));
      if (gate.verdict.text === 'PASS') {
        kids.push(quote('Pass does not mean safe — it means the agent resisted these particular tricks.'));
      }
    }
    fill(cv, kids);
  }

  /* ---------- screen 2: attack board ---------- */

  function techOrder(code) {
    var m = /^T(\d+)/.exec(code || '');
    return m ? parseInt(m[1], 10) : 99;
  }

  function sortedGroups(batch, band) {
    return (batch.groups || []).filter(function (g) { return g.band === band; }).slice().sort(function (a, b) {
      return techOrder(a.technique) - techOrder(b.technique);
    });
  }

  function bandTileCount(batch, band) {
    var n = 0;
    sortedGroups(batch, band).forEach(function (g) {
      (g.tests || []).forEach(function (t) { n += (t.runs || []).length; });
    });
    return n;
  }

  function renderBoard() {
    var batch = currentBatch();
    var boxes = ['b-batches', 'b-filter', 'b-legend', 'b-attack', 'b-benign', 'b-src'].map($);
    if (!batch) {
      fill(boxes[3], pendingBlock('No test batch yet. Run the attack lab, then export.'));
      return;
    }
    if (state.tech && !(batch.groups || []).some(function (g) { return g.technique === state.tech; })) state.tech = null;

    /* batches */
    var bb = $('b-batches');
    var list = D.batches || [];
    fill(bb, [
      list.length > 1 ? el('div', { class: 'batch-buttons', role: 'group', 'aria-label': 'Batch' }, list.map(function (b) {
        var btn = el('button', { type: 'button', class: 'batch-btn', 'aria-pressed': b.id === batch.id ? 'true' : 'false', 'data-batch': b.id }, V(b.title));
        btn.addEventListener('click', function () { setBatch(b.id); });
        return btn;
      })) : null,
      el('p', { class: 'batch-line' }, [V(batch.title), ': ', F(batch.shape)]),
      el('p', { class: 'batch-model' }, ['Model: ', F(batch.model)])
    ]);

    /* technique filter */
    var bf = $('b-filter');
    var techs = sortedGroups(batch, 'attack').concat(sortedGroups(batch, 'benign'));
    var seen = {};
    var buttons = [{ code: null, label: 'All techniques' }];
    techs.forEach(function (g) {
      if (seen[g.technique]) return;
      seen[g.technique] = true;
      buttons.push({ code: g.technique, label: g.label || label('techniques', g.technique) });
    });
    if (bf && !bf.hasAttribute('role')) { bf.setAttribute('role', 'group'); bf.setAttribute('aria-label', 'Filter by technique'); }
    fill(bf, buttons.map(function (b) {
      var pressed = (state.tech || null) === b.code;
      var btn = el('button', { type: 'button', class: 'filter-btn', 'aria-pressed': pressed ? 'true' : 'false', 'data-tech': b.code || 'all' },
        b.code ? V(b.label) : b.label);
      btn.addEventListener('click', function () { setTech(b.code); });
      return btn;
    }));

    /* legend with counted states */
    var bl = $('b-legend');
    var ca = (batch.counts && batch.counts.attack) || {};
    var cb = (batch.counts && batch.counts.benign) || {};
    var hasBenign = bandTileCount(batch, 'benign') > 0;
    var legend = [];
    if (hasBenign) legend.push(el('p', { class: 'legend-band' }, 'Attack runs'));
    ATTACK_STATES.forEach(function (st) {
      if (st === 'blocked' && !batch.has_blocked_info) {
        legend.push(el('div', { class: 'legend-item', 'data-state': 'blocked', 'data-missing': 'true' }, [
          el('span', { class: 'legend-text' }, 'Blocked: not recorded in this batch')
        ]));
        return;
      }
      legend.push(el('div', { class: 'legend-item', 'data-state': st }, [
        el('span', { class: 'legend-text' }, [V(label('states', st)), ' ', F(ca[st], 'legend-count')])
      ]));
    });
    if (hasBenign) {
      legend.push(el('p', { class: 'legend-band' }, 'Normal requests'));
      BENIGN_STATES.forEach(function (st) {
        legend.push(el('div', { class: 'legend-item', 'data-state': st, 'data-band': 'benign' }, [
          el('span', { class: 'legend-text' }, [V(label('states', st)), ' ', F(cb[st], 'legend-count')])
        ]));
      });
    }
    if (bl && !bl.hasAttribute('aria-label')) bl.setAttribute('aria-label', 'Legend');
    fill(bl, legend);

    /* bands */
    renderBand($('b-attack'), batch, 'attack');
    renderBand($('b-benign'), batch, 'benign');
    applyFilter();
    initRoving();

    var bs = $('b-src');
    fill(bs, [
      src(['Source: ', V((batch.source_files || []).join(', ')), batch.produced ? [' (', V(batch.produced.text), ')'] : null]),
      src(['Each tile is one run. Shape, pattern and label carry the state.'])
    ]);
  }

  function renderBand(box, batch, band) {
    if (!box) return;
    box.classList.add('band');
    box.setAttribute('data-band', band);
    var groups = sortedGroups(batch, band);
    var counts = (batch.counts && batch.counts[band]) || {};
    var title = band === 'attack' ? 'Attack runs' : 'Normal requests';
    if (!groups.length || bandTileCount(batch, band) === 0) {
      var metric = band === 'attack' ? (batch.summary || {}).attack_success_rate : (batch.summary || {}).benign_task_success;
      fill(box, [
        el('h2', { class: 'band-title' }, title),
        el('p', { class: 'band-empty' }, [title, ': ', metric && !metric.measured ? F(metric.x_of_n) : el('span', { 'data-static': '' }, 'not measured (n = 0)')])
      ]);
      return;
    }
    var dense = bandTileCount(batch, band) > DENSE_AT;
    var col = 0;
    var groupNodes = groups.map(function (g) {
      var tests = (g.tests || []).map(function (t) {
        var c = col++;
        var tiles = (t.runs || []).map(function (key, r) {
          var run = (batch.runs || {})[key];
          return run ? tile(run, band, c, r) : null;
        });
        return el('div', { class: 'test-col', 'data-test': t.test_id }, [V(t.test_id, 'test-id'), tiles]);
      });
      var gm = g.metric;
      var what = band === 'attack' ? 'broke a rule' : 'handled';
      return el('div', { class: 'group', 'data-technique': g.technique }, [
        el('div', { class: 'group-head' }, [
          V(g.label || label('techniques', g.technique), 'group-label'),
          gm ? [doc.createTextNode(' '), el('span', { class: 'group-metric' }, [F(gm.x_of_n), gm.measured ? [' ', what] : null])] : null
        ]),
        el('div', { class: 'tiles ' + (dense ? 'dense' : 'roomy') }, tests)
      ]);
    });
    fill(box, [
      el('h2', { class: 'band-title' }, [title, ' ', el('span', { class: 'band-count' }, ['(', F(counts.runs), ')'])]),
      groupNodes
    ]);
  }

  function tile(run, band, c, r) {
    var aria = run.test_id + ', rep ' + run.rep + ': ' + (run.state_label || run.state) +
      ((run.badges || []).length ? ' (' + run.badges.join(', ') + ')' : '');
    var b = el('button', {
      type: 'button', class: 'tile', 'data-state': run.state, 'data-c': c, 'data-r': r, 'data-band': band,
      'data-key': run.key, 'data-prov': run.source_row ? (run.source_row.prov || '') : '',
      tabindex: '-1', 'aria-label': aria
    }, [
      V(run.state_label || label('states', run.state), 'tile-label'),
      (run.badges || []).map(function (bd) { return V(bd, 'badge'); })
    ]);
    b.addEventListener('click', function () { openDrawer(run.key); });
    b.addEventListener('keydown', onTileKey);
    b.addEventListener('focus', function () { setRoving(b, false); });
    return b;
  }

  function allTiles() {
    return Array.prototype.slice.call(doc.querySelectorAll('#screen-2 button.tile'));
  }

  function activeTiles() {
    return allTiles().filter(function (t) { return !t.closest('[inert]'); });
  }

  function setRoving(target, focus) {
    if (!target) return;
    allTiles().forEach(function (t) { t.setAttribute('tabindex', t === target ? '0' : '-1'); });
    rovingKey = target.getAttribute('data-key');
    if (focus) target.focus();
  }

  function initRoving() {
    var tiles = activeTiles();
    if (!tiles.length) return;
    var keep = rovingKey && tiles.filter(function (t) { return t.getAttribute('data-key') === rovingKey; })[0];
    setRoving(keep || tiles[0], false);
  }

  function onTileKey(e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var k = e.key;
    var cur = e.currentTarget;
    var tiles = activeTiles();
    if (!tiles.length) return;
    var band = cur.getAttribute('data-band');
    var c = Number(cur.getAttribute('data-c'));
    var r = Number(cur.getAttribute('data-r'));
    var next = null;
    function at(bd, cc, rr) {
      return tiles.filter(function (t) {
        return t.getAttribute('data-band') === bd && Number(t.getAttribute('data-c')) === cc && Number(t.getAttribute('data-r')) === rr;
      })[0] || null;
    }
    function scan(bd, start, rr, dir) {
      var cols = tiles.filter(function (t) { return t.getAttribute('data-band') === bd; })
        .map(function (t) { return Number(t.getAttribute('data-c')); });
      if (!cols.length) return null;
      var max = Math.max.apply(null, cols), min = Math.min.apply(null, cols);
      for (var cc = start; cc >= min && cc <= max; cc += dir) {
        var hit = at(bd, cc, rr);
        if (hit) return hit;
        if (rr > 0) { hit = at(bd, cc, 0); if (hit) return hit; }
      }
      return null;
    }
    function nearestInBand(bd) {
      var inBand = tiles.filter(function (t) { return t.getAttribute('data-band') === bd; });
      if (!inBand.length) return null;
      return inBand.reduce(function (best, t) {
        var d = Math.abs(Number(t.getAttribute('data-c')) - c);
        return !best || d < best.d ? { t: t, d: d } : best;
      }, null).t;
    }
    if (k === 'ArrowRight') next = scan(band, c + 1, r, 1);
    else if (k === 'ArrowLeft') next = scan(band, c - 1, r, -1);
    else if (k === 'ArrowDown') next = at(band, c, r + 1) || (band === 'attack' ? nearestInBand('benign') : null);
    else if (k === 'ArrowUp') next = at(band, c, r - 1) || (band === 'benign' && r === 0 ? nearestInBand('attack') : null);
    else if (k === 'Home') next = tiles[0];
    else if (k === 'End') next = tiles[tiles.length - 1];
    else return;
    e.preventDefault();
    if (next) setRoving(next, true);
  }

  function applyFilter() {
    doc.querySelectorAll('#screen-2 .group').forEach(function (g) {
      var off = !!state.tech && g.getAttribute('data-technique') !== state.tech;
      if (off) { g.inert = true; g.setAttribute('inert', ''); g.classList.add('is-dim'); }
      else { g.inert = false; g.removeAttribute('inert'); g.classList.remove('is-dim'); }
    });
  }

  function setTech(code) {
    state.tech = code || null;
    doc.querySelectorAll('#b-filter button[data-tech]').forEach(function (b) {
      b.setAttribute('aria-pressed', (b.getAttribute('data-tech') === (code || 'all')) ? 'true' : 'false');
    });
    applyFilter();
    var live = activeTiles();
    if (live.length && !live.some(function (t) { return t.getAttribute('data-key') === rovingKey; })) setRoving(live[0], false);
    writeHash();
  }

  function setBatch(id) {
    if (!byBatch(id)) return;
    state.batch = id === D.current_batch ? null : id;
    state.tech = null;
    rovingKey = null;
    guard('board', renderBoard);
    renderReplay(state.s);
    writeHash();
  }

  /* ---------- drawer ---------- */

  function section(title, kids) {
    return el('section', { class: 'd-section' }, [el('h3', { class: 'd-label' }, title), kids]);
  }

  function plantedSection(run) {
    var p = run.planted || { status: 'missing' };
    if (p.status === 'match') {
      return section('Planted text, and where it was hidden', [
        el('div', { class: 'd-code' }, F(p.text)),
        el('p', { class: 'd-text' }, ['Hidden in: ', F(p.where)]),
        p.caveat ? el('p', { class: 'caveat' }, V(p.caveat)) : null
      ]);
    }
    if (p.status === 'none') {
      return section('Planted text', el('p', { class: 'd-text' }, 'None. This is a normal request.'));
    }
    if (p.status === 'differs') {
      return section('Planted text', el('p', { class: 'caveat' }, p.caveat ? V(p.caveat) : 'corpus version differs from this run'));
    }
    return section('Planted text', el('p', { class: 'caveat' }, p.caveat ? V(p.caveat) : 'Not found in the test set.'));
  }

  function callsSection(run) {
    var calls = run.tool_calls || [];
    if (!calls.length) return section('Tool calls, in order', el('p', { class: 'd-text' }, 'No tool calls.'));
    return section('Tool calls, in order', el('ol', { class: 'calls' }, calls.map(function (c) {
      return el('li', { 'data-blocked': c.blocked ? 'true' : 'false', 'data-flagged': c.flagged ? 'true' : 'false' }, [
        V(c.n, 'call-n'),
        el('span', { class: 'call-text' }, [V(c.text),
          c.flagged ? [' ', el('span', { class: 'call-flag' }, '(the checker flagged this call)')] : null,
          c.blocked ? [' ', el('span', { class: 'call-flag' }, '(blocked)')] : null])
      ]);
    })));
  }

  function checkerSection(run) {
    var kids = [];
    if (run.violated) {
      kids.push(el('p', { class: 'd-text verdict-run', 'data-state': 'broke' }, [
        'Rule broken: ', (run.classes || []).map(function (c, i) { return [i ? ', ' : null, V(c.label || c.code)]; })
      ]));
    } else {
      kids.push(el('p', { class: 'd-text verdict-run', 'data-state': run.state }, 'No rule broken.'));
    }
    if ((run.detail || []).length) {
      kids.push(el('div', { class: 'd-code detail' }, V(run.detail.join('\n'))));
    }
    if (run.error) kids.push(el('p', { class: 'd-text' }, ['Error: ', V(run.error)]));
    kids.push(quote('Code cannot be talked into breaking a rule.'));
    return section('Checker verdict (plain code, no AI)', kids);
  }

  function runInfoSection(run) {
    var ri = run.run_info || {};
    return section('Run record', [
      dl([
        row('Model', F(ri.model)),
        row('Digest', F(ri.digest)),
        row('Started', F(ri.started)),
        row('Duration', F(ri.duration)),
        row('Driver', F(ri.driver)),
        row('Source', F(run.source_row)),
        row('Trace', V(TRACE_TEXT[run.trace_status] || run.trace_status || 'not recorded'))
      ])
    ]);
  }

  function renderDrawer(run) {
    var t = $('d-title');
    var prov = run.source_row ? (run.source_row.prov || '') : '';
    if (t) {
      clear(t);
      add(t, [V(run.test_id, null, prov), ', rep ', V(run.rep, null, prov)]);
    }
    var body = $('d-body');
    if (!body) return;
    clear(body);
    add(body, [
      section('Result', [
        el('p', { class: 'd-text d-state', 'data-state': run.state }, [
          V(run.state_label || label('states', run.state)),
          (run.badges || []).map(function (bd) { return [' ', V(bd, 'badge')]; })
        ]),
        el('p', { class: 'd-text' }, ['Technique: ', V(run.technique_label || run.technique), '. Place: ',
          V(run.place_label || run.place), '. Goal: ', V(run.goal_label || run.goal), '.'])
      ]),
      section("User's task", el('p', { class: 'd-text' }, F(run.user_task))),
      plantedSection(run),
      callsSection(run),
      checkerSection(run),
      runInfoSection(run)
    ]);
    body.scrollTop = 0;
  }

  function setBackgroundInert(on) {
    inertSet.forEach(function (n) { n.inert = false; n.removeAttribute('inert'); });
    inertSet = [];
    if (!on) return;
    var drawer = $('drawer');
    var node = drawer;
    while (node && node.parentElement && node !== doc.body) {
      Array.prototype.forEach.call(node.parentElement.children, function (sib) {
        if (sib === node || sib.tagName === 'SCRIPT' || sib.hasAttribute('inert')) return;
        sib.inert = true;
        sib.setAttribute('inert', '');
        inertSet.push(sib);
      });
      node = node.parentElement;
    }
  }

  function openDrawer(key) {
    var batch = currentBatch();
    var run = batch && batch.runs ? batch.runs[key] : null;
    var drawer = $('drawer');
    if (!run || !drawer) return false;
    if (state.s !== 2) show(2);
    renderDrawer(run);
    drawerReturnKey = key;
    var t = allTiles().filter(function (x) { return x.getAttribute('data-key') === key; })[0];
    if (t) setRoving(t, false);
    drawer.hidden = false;
    drawerOpen = true;
    doc.body.classList.add('drawer-open');
    setBackgroundInert(true);
    state.run = key;
    writeHash();
    var close = $('d-close');
    (close || drawer).focus();
    return true;
  }

  function closeDrawer(restore) {
    var drawer = $('drawer');
    if (!drawerOpen || !drawer) return;
    drawer.hidden = true;
    drawerOpen = false;
    doc.body.classList.remove('drawer-open');
    setBackgroundInert(false);
    state.run = null;
    writeHash();
    if (restore !== false && drawerReturnKey) {
      var t = allTiles().filter(function (x) { return x.getAttribute('data-key') === drawerReturnKey; })[0];
      if (t) setRoving(t, true);
    }
  }

  function drawerFocusables() {
    var drawer = $('drawer');
    if (!drawer) return [];
    return Array.prototype.slice.call(drawer.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    )).filter(function (n) { return !n.disabled && !n.hidden && n.offsetParent !== null; });
  }

  function onDrawerKey(e) {
    if (!drawerOpen) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeDrawer(true); return; }
    if (e.key !== 'Tab') return;
    var f = drawerFocusables();
    if (!f.length) { e.preventDefault(); return; }
    var first = f[0], last = f[f.length - 1];
    var active = doc.activeElement;
    if (e.shiftKey && (active === first || !$('drawer').contains(active))) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (active === last || !$('drawer').contains(active))) { e.preventDefault(); first.focus(); }
  }

  /* ---------- screen 3: compare ---------- */

  function renderSide(box, side, name, limitField) {
    if (!box) return;
    if (!side || side.status === 'PENDING' || side.status === 'SAMPLE') {
      setPanelStatus(box, 'PENDING');
      var reason = side && side.status === 'SAMPLE' ? 'SAMPLE data is never compared.' : (side && side.reason) || null;
      fill(box, [
        el('h2', { class: 'side-title' }, ['Side ', name]),
        pendingBlock('Not measured.', reason)
      ]);
      return;
    }
    setPanelStatus(box, side.status);
    var asr = side.asr;
    fill(box, [
      el('h2', { class: 'side-title' }, ['Side ', name, ': ', V(side.title)]),
      side.replay_label ? el('p', null, el('span', { class: 'replay' }, V(side.replay_label))) : null,
      el('p', { class: 'side-model' }, ['Model: ', F(side.model)]),
      heroMetric(asr, unitWhat(asr), limitField, true),
      el('p', { class: 'benign-line' }, ['Normal requests handled: ', side.benign ? F(side.benign.x_of_n) : F(null),
        side.benign && side.benign.measured && side.benign.rate ? [', ', F(side.benign.rate)] : null]),
      el('p', { class: 'runs-line' }, ['Attack runs: ', F(side.n_attack)])
    ]);
  }

  function renderCompare() {
    var c = D.compare || {};
    var limit = c.limit || policyLimit('max_attack_success_rate');
    renderSide($('k-a'), c.a, 'A', limit);
    renderSide($('k-b'), c.b, 'B', limit);
    var bPending = !c.b || c.b.status !== 'REAL';
    var aPending = !c.a || c.a.status !== 'REAL';

    var note = $('k-note');
    if (c.n_note) fill(note, el('p', { class: 'n-note' }, V(c.n_note)));
    else if (bPending || aPending) fill(note, el('p', { class: 'n-note' }, 'Only one side is measured, so there is nothing to compare yet.'));
    else fill(note, el('p', { class: 'n-note' }, 'Both sides were measured; their n are shown with each rate.'));

    var kt = $('k-tech');
    var aT = (!aPending && c.a.per_technique) || [];
    var bT = (!bPending && c.b.per_technique) || [];
    var codes = [];
    aT.concat(bT).forEach(function (t) { if (codes.indexOf(t.technique) < 0) codes.push(t.technique); });
    codes.sort(function (x, y) { return techOrder(x) - techOrder(y); });
    function find(list, code) { return list.filter(function (t) { return t.technique === code; })[0] || null; }
    function cell(entry, pending) {
      if (pending) return el('td', null, tag('PENDING'));
      if (!entry || !entry.metric) return el('td', null, 'not run');
      var m = entry.metric;
      return el('td', null, [F(m.x_of_n), m.measured && m.rate ? [', ', F(m.rate)] : null]);
    }
    if (!codes.length) {
      fill(kt, [head(kt, 'Per technique'), pendingBlock('No per-technique numbers yet.')]);
    } else {
      fill(kt, [
        head(kt, 'Per technique'),
        el('table', { class: 'grid' }, [
          el('thead', null, el('tr', null, [el('th', null, 'Technique'), el('th', null, 'Side A'), el('th', null, 'Side B')])),
          el('tbody', null, codes.map(function (code) {
            var a = find(aT, code), b = find(bT, code);
            return el('tr', null, [el('td', null, V((a || b).label || label('techniques', code))), cell(a, aPending), cell(b, bPending)]);
          }))
        ])
      ]);
    }

    var ks = $('k-src');
    fill(ks, [
      src('Compare uses summary files only. SAMPLE data is never compared.')
    ]);
  }

  /* ---------- screen 4: decisions and audit log ---------- */

  function renderAudit() {
    var A = D.audit || { status: 'PENDING' };
    var chain = A.chain || { status: 'PENDING' };

    var ac = $('a-chain');
    var okAttr = chain.ok === true ? 'true' : chain.ok === false ? 'false' : 'pending';
    var chainKids;
    if (chain.ok === true) {
      chainKids = [el('p', null, ['Chain check: ', tag(chain.status), ' ', F(chain.text)])];
    } else if (chain.ok === false) {
      chainKids = [
        el('p', null, ['Chain check: ', tag(chain.status), ' ', F(chain.text)]),
        chain.bad_seq ? el('p', { class: 'tamper' }, ['TAMPERING DETECTED at entry ', F(chain.bad_seq)]) : null
      ];
    } else {
      chainKids = [
        el('p', null, ['Chain check: ', tag('PENDING')]),
        chain.reason ? el('p', { class: 'reason' }, V(chain.reason)) : null
      ];
    }
    fill(ac, [
      head(ac, 'Tamper-evident log'),
      el('div', { class: 'chain', 'data-ok': okAttr }, chainKids),
      src('Each entry carries the hash of the entry before it. The backend checks the chain; this screen only shows its result.'),
      quote('The machine gives evidence. A person decides.')
    ]);

    var ae = $('a-entries');
    setPanelStatus(ae, A.status);
    if (A.status === 'PENDING' || !(A.entries || []).length) {
      fill(ae, [
        pendingBlock('No decision log yet. Entries appear when a candidate is submitted, checked and decided.', A.reason),
        quote('Silence must never mean yes.')
      ]);
    } else {
      var refused = A.entries.some(function (e) {
        return e.event === 'decision_refused' && e.what && /cannot review it/.test(String(e.what.text));
      });
      var kids = [];
      if (A.status === 'SAMPLE') kids.push(sampleNote(A.sample_tag, 'SAMPLE · hand-written, not produced by the backend'));
      A.entries.forEach(function (e) {
        if (!e.readable) {
          kids.push(el('div', { class: 'entry', 'data-event': 'unreadable', 'data-readable': 'false' }, [
            el('span', { class: 'seq' }), el('span', { class: 'time' }), el('span', { class: 'event' }, 'Unreadable'), el('span', { class: 'who' }),
            el('span', { class: 'what' }, V('line ' + e.line + ': unreadable'))
          ]));
          return;
        }
        kids.push(el('div', { class: 'entry', 'data-event': e.event, 'data-readable': 'true' }, [
          F(e.seq, 'seq'),
          F(e.time, 'time'),
          V(EVENT_LABELS[e.event] || e.event, 'event'),
          F(e.who, 'who'),
          el('span', { class: 'what' }, [
            el('span', null, [e.candidate ? [F(e.candidate), ': '] : null, F(e.what)]),
            e.reason ? el('span', null, ['Reason: ', F(e.reason)]) : null,
            e.hash_short ? el('span', null, ['Hash ', V(e.hash_short, 'hash')]) : null
          ])
        ]));
      });
      if (refused) kids.push(quote('No marking your own homework.'));
      fill(ae, kids);
    }

    var as = $('a-src');
    var roles = (D.policy && D.policy.roles) || {};
    fill(as, [
      src(['Engineers: ', F(roles.engineers), '. Reviewers: ', F(roles.reviewers), '. Rule owner: ', F(roles.rule_owner), '.']),
      src(['Log: ', V(A.source_file || 'governance_out/audit_log.jsonl'), ' ', tag(A.status),
        A.produced && A.produced.text ? [' (', V(A.produced.text), ')'] : null])
    ]);
  }

  function scrollEntriesToEnd() {
    var ae = $('a-entries');
    if (ae) ae.scrollTop = ae.scrollHeight;
  }

  /* ---------- screen 5: assurance ---------- */

  function renderAssurance() {
    var R = D.assurance || { status: 'PENDING' };
    var pending = R.status === 'PENDING';
    var sampleTag = R.status === 'SAMPLE' ? sampleNote(R.sample_tag, 'SAMPLE · hand-written, not produced by the backend') : null;

    var rr = $('r-risks');
    setPanelStatus(rr, R.status);
    if (pending) {
      fill(rr, [head(rr, 'Risks'), pendingBlock('No risk register yet. The assurance step writes it from a tested batch.', R.reason)]);
    } else {
      fill(rr, [
        head(rr, 'Risks'), sampleTag,
        el('table', { class: 'grid risks' }, [
          el('thead', null, el('tr', null, ['ID', 'Risk', 'Violation class', 'Likelihood', 'Impact', 'Control', 'Owner', 'Status'].map(function (h) { return el('th', null, h); }))),
          el('tbody', null, (R.risks || []).map(function (r) {
            return el('tr', null, [
              el('td', null, V(r.id)), el('td', null, V(r.title)),
              el('td', null, [V(label('classes', r['class'])), ' ', V(r['class'], 'd-code')]),
              el('td', null, F(r.likelihood)), el('td', null, V(r.impact)),
              el('td', null, r.control ? V(r.control) : el('strong', null, 'no fix')),
              el('td', null, V(r.owner)), el('td', null, V(r.status_text))
            ]);
          }))
        ])
      ]);
    }

    var rc = $('r-controls');
    var CT = D.controls || { status: 'PENDING' };
    setPanelStatus(rc, CT.status);
    if (CT.status === 'PENDING' || !(CT.items || []).length) {
      fill(rc, [head(rc, 'Controls'), pendingBlock('No control map read.', CT.reason)]);
    } else {
      fill(rc, [
        head(rc, 'Controls'),
        quote('Mapped to, never compliant with.'),
        el('table', { class: 'grid controls' }, [
          el('thead', null, el('tr', null, ['Control', 'OWASP LLM Top 10', 'MITRE ATLAS', 'NIST AI RMF'].map(function (h) { return el('th', null, h); }))),
          el('tbody', null, CT.items.map(function (c) {
            return el('tr', null, [
              el('td', null, [V(c.code), ' ', V(c.name)]),
              el('td', null, V((c.owasp || []).join(', '))),
              el('td', null, V((c.mitre_atlas || []).join(', '))),
              el('td', null, V((c.nist_ai_rmf || []).join(', ')))
            ]);
          }))
        ]),
        CT.note ? el('p', { class: 'caveat' }, F(CT.note)) : null
      ]);
    }

    var re = $('r-evidence');
    setPanelStatus(re, R.status);
    if (pending) {
      fill(re, [head(re, 'Evidence'), pendingBlock('No evidence register yet.')]);
    } else {
      fill(re, [
        head(re, 'Evidence'), sampleTag ? sampleTag.cloneNode(true) : null,
        el('table', { class: 'grid evidence' }, [
          el('thead', null, el('tr', null, ['Control', 'Proof needed', 'Proof file', 'Result'].map(function (h) { return el('th', null, h); }))),
          el('tbody', null, (R.evidence || []).map(function (ev) {
            return el('tr', null, [
              el('td', null, V(ev.control)), el('td', null, V(ev.proof_needed)),
              el('td', null, [V(ev.proof_file, 'd-code'), ev.proof_file_exists ? null : [' ', el('span', { class: 'badge missing' }, 'file not found')]]),
              el('td', null, V(ev.result))
            ]);
          }))
        ])
      ]);
    }

    var rt = $('r-tickets');
    setPanelStatus(rt, R.status);
    if (pending) {
      fill(rt, [head(rt, 'Fix tickets'), pendingBlock('No fix tickets yet.')]);
    } else {
      fill(rt, [
        head(rt, 'Fix tickets'), sampleTag ? sampleTag.cloneNode(true) : null,
        el('ul', { class: 'tickets' }, (R.tickets || []).map(function (t) {
          return el('li', null, [
            el('span', null, [V(t.id, 'ticket-id'), ' ', V(t.title)]),
            el('span', null, ['Owner ', V(t.owner), ', due ', V(t.due), ', risk ', V(t.risk), '.'])
          ]);
        }))
      ]);
    }

    var rs = $('r-src');
    fill(rs, [
      src(['Risks, evidence and tickets: ', V((R.source_files || []).join(', ') || 'not connected'), ' ', tag(R.status)]),
      CT.source_file ? src(['Controls: ', V(CT.source_file), ' ', tag(CT.status)]) : null
    ]);
  }

  /* ---------- screens, presenter, keys ---------- */

  function screens() { return Array.prototype.slice.call(doc.querySelectorAll('section.screen')); }

  /* The pipeline view (pipeline.js) may be missing or may throw; the page works without it. */
  function pipelineVisible(flag) {
    try {
      if (window.Pipeline && typeof window.Pipeline.visible === 'function') window.Pipeline.visible(flag);
    } catch (err) {
      if (window.console) console.error('Pipeline view: ' + err);
    }
  }

  function pipelineKey(name) {
    try {
      if (window.Pipeline && typeof window.Pipeline.key === 'function') return !!window.Pipeline.key(name);
    } catch (err) {
      if (window.console) console.error('Pipeline view: ' + err);
    }
    return false;
  }

  /* The release-decision card sits over the board on screen 6, and only there. */
  function syncCard() {
    var card = $('p-decision');
    var on = state.card && state.s === PIPELINE_SCREEN;
    if (card) card.hidden = !on;
    doc.body.classList.toggle('card-open', !!on);
  }

  function toggleCard(on) {
    if (state.s !== PIPELINE_SCREEN) return;
    state.card = on === undefined ? !state.card : !!on;
    syncCard();
    writeHash();
    var card = $('p-decision');
    if (state.card && card) { try { card.focus({ preventScroll: true }); } catch (e) { card.focus(); } }
  }

  function show(n, opts) {
    opts = opts || {};
    n = Math.max(1, Math.min(SCREEN_COUNT, n | 0));
    if (drawerOpen && n !== 2) closeDrawer(false);
    state.s = n;
    if (n !== PIPELINE_SCREEN) state.card = false;
    screens().forEach(function (sec) {
      var k = Number(sec.getAttribute('data-screen')) || Number((sec.id || '').replace('screen-', ''));
      sec.hidden = k !== n;
    });
    doc.querySelectorAll('#nav .nav-item').forEach(function (b) {
      if (Number(b.getAttribute('data-screen')) === n) b.setAttribute('aria-current', 'page');
      else b.removeAttribute('aria-current');
    });
    renderReplay(n);
    renderHints();
    syncCard();
    writeHash();
    placeDeck(opts.initial ? 'none' : 'glide');
    pipelineVisible(n === PIPELINE_SCREEN);
    if (n === 4) scrollEntriesToEnd();
    if (!opts.noFocus) {
      var h = doc.querySelector('#screen-' + n + ' .screen-title');
      if (h) { try { h.focus({ preventScroll: true }); } catch (e) { h.focus(); } }
    }
  }

  function togglePresenter(on) {
    state.p = on === undefined ? !state.p : !!on;
    doc.body.classList.toggle('presenter', state.p);
    if (!state.p) { clearTimeout(idleTimer); doc.body.classList.remove('idle'); }
    else armIdle();
    if (D && D.schema === 'ui-demo/1') renderWarnings();
    writeHash();
  }

  function armIdle() {
    clearTimeout(idleTimer);
    doc.body.classList.remove('idle');
    if (!state.p) return;
    idleTimer = setTimeout(function () { doc.body.classList.add('idle'); }, IDLE_MS);
  }

  function onKey(e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var k = e.key;
    if (drawerOpen && (k === 'Escape' || k === 'Tab')) { onDrawerKey(e); return; }
    if (k.length === 1 && k >= '1' && k <= String(SCREEN_COUNT)) { e.preventDefault(); show(Number(k)); return; }
    if (k === 'r' || k === 'R') { if (!e.repeat) { e.preventDefault(); location.reload(); } return; }
    if (k === 'p' || k === 'P') { if (!e.repeat) { e.preventDefault(); togglePresenter(); } return; }
    /* on the pipeline screen: D shows or hides the release-decision card; Space, Home, [ and ] drive the
       recorded run in pipeline.js (only while the card is hidden, so nothing moves out of sight) */
    if (state.s === PIPELINE_SCREEN) {
      if (k === 'd' || k === 'D') { if (!e.repeat) { e.preventDefault(); toggleCard(); } return; }
      if (k === 'Escape' && state.card) { e.preventDefault(); toggleCard(false); return; }
      var name = k === ' ' ? 'space' : k === 'Home' ? 'home' : k === '[' ? 'prev' : k === ']' ? 'next' : '';
      if (name) {
        e.preventDefault();
        if (!state.card && !e.repeat) pipelineKey(name);
        return;
      }
    }
  }

  function onHashChange() {
    if (location.hash === lastHash) return;
    var before = { batch: state.batch, tech: state.tech };
    readHash();
    if (!D || D.schema !== 'ui-demo/1') { show(state.s); return; }
    doc.body.classList.toggle('presenter', state.p);
    if (before.batch !== state.batch) guard('board', renderBoard);
    else if (before.tech !== state.tech) setTech(state.tech);
    var want = state.run;
    if (drawerOpen && want !== drawerReturnKey) closeDrawer(false);
    show(state.s, { noFocus: !!want });
    if (want && state.s === 2 && !drawerOpen && !openDrawer(want)) { state.run = null; writeHash(); }
  }

  /* Put the words from copy.js into the static markup. index.html keeps a plain fallback for each one,
     so the page still reads if copy.js is missing. The key digits come from the router, not from copy.js. */
  function applyCopy() {
    var name = copy('product.name');
    if (name) doc.title = name;
    var brand = doc.querySelector('.brand');
    if (brand && name) {
      clear(brand);
      brand.appendChild(el('span', { class: 'brand-name' }, name));
      var team = copy('product.team_line', { team: copy('product.team') });
      if (team) {
        brand.appendChild(doc.createTextNode(' '));
        brand.appendChild(el('span', { class: 'brand-team' }, team));
      }
    }
    ORDER.forEach(function (screenName, i) {
      var n = i + 1;
      var item = navItem(n);
      var label = copy('screens.' + screenName + '.nav');
      if (item && label) item.textContent = n + ' ' + label;
      var title = copy('screens.' + screenName + '.title');
      var h = $('title-' + n);
      if (h && title) h.textContent = title;
    });
    var rail = $('p-rail');
    if (rail && !rail.hasAttribute('aria-label') && copy('pipeline.rail_label')) rail.setAttribute('aria-label', copy('pipeline.rail_label'));
    var board = $('p-board');
    if (board && !board.hasAttribute('aria-label') && copy('pipeline.board_label')) board.setAttribute('aria-label', copy('pipeline.board_label'));
    var missing = $('p-status');
    if (missing && !window.Pipeline && copy('pipeline.not_loaded')) {
      missing.textContent = copy('pipeline.not_loaded');
      missing.setAttribute('data-static', '');
    }
  }

  function bindNav() {
    doc.querySelectorAll('#nav .nav-item').forEach(function (b) {
      b.addEventListener('click', function () { show(Number(b.getAttribute('data-screen'))); });
    });
    var close = $('d-close');
    if (close) close.addEventListener('click', function () { closeDrawer(true); });
    var drawer = $('drawer');
    if (drawer) drawer.addEventListener('keydown', onDrawerKey);
    var body = $('d-body');
    if (body && !body.hasAttribute('tabindex')) body.setAttribute('tabindex', '0');
    var ae = $('a-entries');
    if (ae && !ae.hasAttribute('tabindex')) ae.setAttribute('tabindex', '0');
  }

  function noData() {
    doc.body.classList.add('no-data');
    screens().forEach(function (sec) {
      var msg = el('div', { class: 'pending-block no-data-msg', 'data-gen': '' }, [
        el('p', null, [tag('PENDING'), ' No data yet. Run: python3 ui/export.py'])
      ]);
      var h = sec.querySelector('.screen-title');
      if (h && h.nextSibling) sec.insertBefore(msg, h.nextSibling); else sec.appendChild(msg);
    });
    var stamp = $('stamp');
    if (stamp) { clear(stamp); add(stamp, 'No export yet'); }
    var r = $('replay');
    if (r) { clear(r); r.hidden = true; }
  }

  function renderAll() {
    guard('header', function () { renderChips(); renderBanner(); renderStamp(); renderWarnings(); renderHints(); });
    guard('console', renderConsole);
    guard('board', renderBoard);
    guard('compare', renderCompare);
    guard('audit', renderAudit);
    guard('assurance', renderAssurance);
  }

  function boot() {
    readHash();
    applyCopy();
    bindNav();
    doc.addEventListener('keydown', onKey);
    addEventListener('hashchange', onHashChange);
    addEventListener('mousemove', armIdle, { passive: true });
    addEventListener('resize', function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () { fit(); placeDeck('instant'); }, 60);
    });
    fit();
    var ok = D && D.schema === 'ui-demo/1';
    if (!ok) {
      noData();
    } else {
      renderAll();
    }
    if (state.p) togglePresenter(true);
    var wantRun = state.run;
    show(state.s, { initial: true, noFocus: !!wantRun });
    if (ok && wantRun && state.s === 2) {
      if (!openDrawer(wantRun)) { state.run = null; writeHash(); }
    } else if (wantRun) {
      state.run = null;
      writeHash();
    }
    fontsReady(1000).then(function () {
      fit();
      deckReady = true;
      placeDeck(state.d ? 'instant' : 'draw');
      if (!state.d) { state.d = true; writeHash(); }
    });
  }

  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
