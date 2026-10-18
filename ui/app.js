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

  /* Source paths that share a folder: the folder once, then the file names ("dir/{a.md, b.md}").
     This only shortens a label; each file is still named in full in the export report. */
  function joinPaths(list) {
    if (!Array.isArray(list) || list.length < 2) return (list || []).join(', ');
    var dirs = list.map(function (p) { var i = String(p).lastIndexOf('/'); return i >= 0 ? String(p).slice(0, i + 1) : ''; });
    var same = dirs.every(function (d) { return d === dirs[0]; });
    if (!same || !dirs[0]) return list.join(', ');
    return dirs[0] + '{' + list.map(function (p) { return String(p).slice(dirs[0].length); }).join(', ') + '}';
  }

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
      src(['Risks, evidence and tickets: ', V(joinPaths(R.source_files || []) || 'not connected'), ' ', tag(R.status)]),
      CT.source_file ? src(['Controls: ', V(CT.source_file), ' ', tag(CT.status)]) : null
    ]);
  }

  /* ---------- data that other lanes add: governance, models, numbers ----------
     Expected shapes (lane D exports them into window.DEMO; a name that is absent reads as PENDING):
       governance {dir, mode, selected, scripted_reviewer?, candidates:[{id, verdict, defences?, runs?, duration?,
                   decision?: Field | {decision, reviewer, reason, scripted}}]}
       models {status, source_files?, headline?, rows:[{tag, digest12, tools, asr, benign, errors, verdicts, mode, folder}]}
       numbers {key: Field}    keys: runs.total, headline.model, audit.entries, chain.status, models.n, ...
     Every value is a Field {text, status, prov}. A bare string is wrapped as PENDING, so it never reads as REAL.
     Nothing here types, computes or rounds a measured number. */

  function pickKey(obj, names) {
    if (!obj || typeof obj !== 'object') return null;
    for (var i = 0; i < names.length; i++) {
      var v = obj[names[i]];
      if (v !== undefined && v !== null) return v;
    }
    return null;
  }

  function asField(v, status) {
    if (v === null || v === undefined) return null;
    if (isField(v)) return v;
    if (typeof v === 'string') return { text: v, status: status || 'PENDING', prov: null };
    return null;
  }

  function fieldOf(obj, names, status) { return asField(pickKey(obj, names), status); }

  /* numbers['runs.total']: a Field, or null when the key is missing */
  function numberField(key) {
    var n = D && D.numbers;
    var f = n && typeof n === 'object' ? n[key] : null;
    return isField(f) ? f : null;
  }

  function firstNumber(keys) {
    for (var i = 0; i < keys.length; i++) {
      var f = numberField(keys[i]);
      if (f && f.status !== 'PENDING') return f;
    }
    return null;
  }

  /* a value with its status tag; a missing value is a PENDING tag, never a typed number */
  function withTag(f) {
    if (!f) return [tag('PENDING')];
    return [F(f), ' ', tag(f.status)];
  }

  function fact(label, value) {
    return el('div', { class: 'dc-fact' }, [el('dt', null, label), el('dd', null, value)]);
  }

  /* ---------- screen 6: the release-decision card (for the buyer: the person who signs off) ---------- */

  function auditDecisionFor(candText) {
    var A = D.audit || {};
    var list = Array.isArray(A.entries) ? A.entries : [];
    var found = null;
    list.forEach(function (e) {
      if (e && e.readable !== false && e.event === 'decision' && e.candidate && String(e.candidate.text) === String(candText)) found = e;
    });
    return found;
  }

  /* The candidates on the card: governance.candidates when lane D exports them, else the one gate verdict
     that the console already shows. Nothing is invented: no data means one PENDING column. */
  function decisionCandidates() {
    var g = D.governance;
    var raw = g && Array.isArray(g.candidates) ? g.candidates : [];
    var out = raw.map(function (c) {
      var verdict = fieldOf(c, ['verdict', 'gate_verdict']) || (c && c.gate ? fieldOf(c.gate, ['verdict']) : null);
      return { id: fieldOf(c, ['id', 'candidate', 'candidate_id']), verdict: verdict, raw: c };
    });
    if (!out.length) {
      var gate = (D.console && D.console.gate) || null;
      var shown = !!(gate && gate.status !== 'PENDING' && gate.verdict);
      out.push({
        id: shown ? gate.candidate_id : (D.console && D.console.candidate ? D.console.candidate.id : null),
        verdict: shown ? gate.verdict : null, raw: null
      });
    }
    return out;
  }

  /* who decided, what, why, and whether the reviewer was scripted (the CLI scripts its reviewer, LIVE runs too) */
  function decisionOf(c) {
    var raw = c.raw || {};
    var d = raw.decision;
    var who = null, what = null, reason = null, scripted = null;
    if (d && isField(d)) {
      what = d;
    } else if (d && typeof d === 'object') {
      who = fieldOf(d, ['reviewer', 'by', 'who']);
      what = fieldOf(d, ['decision', 'verdict', 'what']);
      reason = fieldOf(d, ['reason']);
      if (typeof d.scripted === 'boolean') scripted = d.scripted;
    }
    if (!who && !what && c.id) {
      var e = auditDecisionFor(c.id.text);
      if (e) {
        who = e.who || null; what = e.what || null; reason = e.reason || null;
        if (typeof e.scripted === 'boolean') scripted = e.scripted;
      }
    }
    if (scripted === null && typeof raw.scripted === 'boolean') scripted = raw.scripted;
    if (scripted === null && D.governance && typeof D.governance.scripted_reviewer === 'boolean') scripted = D.governance.scripted_reviewer;
    if (scripted === null && D.audit && /scripted/i.test(String(D.audit.sample_tag || ''))) scripted = true;
    if (scripted === null && D.governance && (who || what)) scripted = true;
    return { who: who, what: what, reason: reason, scripted: scripted };
  }

  function candidateColumn(c) {
    var dec = decisionOf(c);
    var vf = c.verdict;
    var vStatus = vf ? word(vf.status) : 'PENDING';
    var kids = [
      el('p', { class: 'dc-id', title: copy('decision.candidate_label') }, c.id ? F(c.id) : tag('PENDING')),
      el('p', { class: 'dc-label' }, copy('decision.verdict_label')),
      el('div', { class: 'verdict compact', 'data-verdict': vf ? vf.text : '', 'data-status': vStatus }, [
        el('p', { class: 'verdict-word' }, vf ? F(vf, 'num-mid') : tag('PENDING')),
        vf ? tag(vf.status) : null
      ]),
      el('p', { class: 'dc-label' }, copy('decision.decided_label'))
    ];
    if (dec.who || dec.what) {
      var main = dec.what || dec.who;
      kids.push(el('p', { class: 'dc-who' }, [
        dec.who ? F(dec.who) : null, dec.who && dec.what ? ': ' : null, dec.what ? F(dec.what) : null, ' ', tag(main.status)
      ]));
      if (dec.scripted) kids.push(el('p', { class: 'dc-who' }, el('span', { class: 'scripted-tag', title: copy('decision.scripted_note') }, copy('decision.scripted'))));
      if (dec.reason) kids.push(el('p', { class: 'dc-reason' }, [copy('decision.reason_label') + ': ', F(dec.reason)]));
    } else {
      kids.push(el('p', { class: 'dc-who' }, [copy('decision.no_decision') + ' ', tag('PENDING')]));
    }
    return el('div', { class: 'dc-cand' }, kids);
  }

  function runsValue() {
    var g = D.governance && Array.isArray(D.governance.candidates) ? D.governance.candidates : [];
    var f = null;
    g.forEach(function (c) { f = f || fieldOf(c, ['runs', 'run_count', 'n_runs']); });
    f = f || firstNumber(['runs.total']);
    if (f) return withTag(f);
    var batch = byBatch(D.console && D.console.evidence_batch) || currentBatch();
    var counts = batch && batch.counts ? batch.counts : null;
    if (counts && counts.attack && counts.attack.runs && counts.benign && counts.benign.runs) {
      return [F(counts.attack.runs), ' attack runs, ', F(counts.benign.runs), ' normal requests ', tag(counts.attack.runs.status)];
    }
    return withTag(null);
  }

  function timeValue() {
    var g = D.governance && Array.isArray(D.governance.candidates) ? D.governance.candidates : [];
    var f = null;
    g.forEach(function (c) { f = f || fieldOf(c, ['duration', 'seconds', 'time_to_decision', 'elapsed']); });
    f = f || firstNumber(['time.to_decision', 'time.total', 'run.seconds', 'duration.total']);
    return withTag(f);
  }

  function modelValue() {
    var cand = D.console && D.console.candidate ? D.console.candidate : null;
    var f = (cand && cand.model && cand.model.status !== 'PENDING') ? cand.model : null;
    f = f || firstNumber(['headline.model']) || (cand ? cand.model : null);
    return withTag(f && isField(f) ? f : null);
  }

  function auditValue() {
    var A = D.audit || {};
    var chain = A.chain || {};
    var out = [];
    var chainField = isField(chain.text) && chain.status !== 'PENDING' ? chain.text : firstNumber(['chain.status']);
    out.push(chainField ? withTag(chainField) : [tag('PENDING')]);
    /* the backend's chain text already names the entry count; show the count apart only when there is no chain text */
    if (chainField) return out;
    var entries = firstNumber(['audit.entries']);
    var list = Array.isArray(A.entries) ? A.entries : [];
    if (entries) out.push(el('span', { class: 'dc-line' }, V(copy('decision.entries', { n: entries.text }), null, entries.prov || '')));
    else if (list.length && A.status !== 'PENDING') out.push(el('span', { class: 'dc-line' }, V(copy('decision.entries', { n: String(list.length) }))));
    return out;
  }

  /* a short list of listed rows (counting rows for display is allowed; the count sits in a data span) */
  function listValue(items, countKey, line, statusTag) {
    if (!items) return withTag(null);
    if (!items.length) return [copy('decision.none_listed'), ' ', statusTag];
    var out = [V(copy(countKey, { n: String(items.length) })), ' ', statusTag];
    items.slice(0, 2).forEach(function (it) { out.push(el('span', { class: 'dc-line' }, line(it))); });
    return out;
  }

  function renderDecision() {
    var box = $('p-decision');
    if (!box) return;
    box.setAttribute('tabindex', '-1');
    box.setAttribute('role', 'region');
    box.setAttribute('aria-labelledby', 'dc-title');
    var R = D.assurance || { status: 'PENDING' };
    var open = Array.isArray(R.risks) && R.status !== 'PENDING'
      ? R.risks.filter(function (r) { return /^open/i.test(String(r.status_text || '')); }) : null;
    var tickets = Array.isArray(R.tickets) && R.status !== 'PENDING' ? R.tickets : null;
    var riskTag = tag(R.status && R.status !== 'PENDING' ? R.status : 'PENDING');
    var cands = decisionCandidates();
    fill(box, [
      el('div', { class: 'dc-head' }, [
        el('h2', { id: 'dc-title' }, copy('decision.title') || 'Release decision'),
        el('p', { class: 'dc-lede' }, copy('decision.lede'))
      ]),
      el('div', { class: 'dc-cands' }, cands.map(candidateColumn)),
      el('div', { class: 'dc-facts' }, [
        el('dl', { class: 'dc-col' }, [
          fact(copy('decision.runs_label'), runsValue()),
          fact(copy('decision.time_label'), timeValue()),
          fact(copy('decision.model_label'), modelValue()),
          fact(copy('decision.cost_label'), el('span', null, copy('decision.cost')))
        ]),
        el('dl', { class: 'dc-col' }, [
          fact(copy('decision.audit_label'), auditValue()),
          fact(copy('decision.risks_label'), listValue(open, 'decision.count_open', function (r) { return [V(r.id), ' ', V(r.title)]; }, riskTag.cloneNode(true))),
          fact(copy('decision.tickets_label'), listValue(tickets, 'decision.count_listed', function (t) { return [V(t.id), ' ', V(t.title)]; }, riskTag.cloneNode(true)))
        ])
      ])
    ]);
  }

  /* ---------- screen 7: Models. One row per model, sorted by tag, no best highlight ---------- */

  function textOf(v) { return isField(v) ? String(v.text) : (typeof v === 'string' ? v : ''); }

  function rowTag(row) { return fieldOf(row, ['tag', 'model', 'name']); }

  /* tool support as three states: yes, no (every run errored), unchecked. Anything else reads as unchecked. */
  function toolsState(row) {
    var raw = pickKey(row, ['tools', 'tool_calls', 'tool_support']);
    var t = raw && typeof raw === 'object' && !isField(raw) ? pickKey(raw, ['state', 'status', 'text']) : raw;
    var text = typeof t === 'boolean' ? (t ? 'yes' : 'no') : textOf(t);
    text = String(text || '').trim().toLowerCase();
    if (/^(yes|true|ok|supported)/.test(text)) return 'yes';
    if (/^(no\b|false|none|unsupported)/.test(text) || /every run errored/.test(text)) return 'no';
    return 'unchecked';
  }

  function toolsStatus(row) {
    var raw = pickKey(row, ['tools', 'tool_calls', 'tool_support']);
    return isField(raw) ? raw.status : 'PENDING';
  }

  /* a metric {measured, x_of_n, rate, ci}: the counts first, then the rate, then the range */
  function metricNodes(m) {
    if (!m) return [tag('PENDING')];
    if (isField(m)) return withTag(m);
    if (typeof m !== 'object') return [tag('PENDING')];
    if (m.measured === false) return [F(asField(m.x_of_n) || null)];
    var out = [F(asField(m.x_of_n) || null)];
    if (m.rate) out.push(', ', F(asField(m.rate)));
    if (m.ci) out.push(el('span', { class: 'dc-line' }, [copy('models.range') + ' ', F(asField(m.ci))]));
    return out;
  }

  function verdictNodes(row) {
    var raw = pickKey(row, ['verdicts', 'gates']);
    var items = [];
    if (Array.isArray(raw)) {
      raw.forEach(function (v) {
        var c = fieldOf(v, ['cand', 'id', 'candidate']);
        var vv = fieldOf(v, ['verdict']);
        if (c || vv) items.push([c ? V(c.text, 'm-cand', c.prov || '') : null, ' ', vv ? F(vv) : tag('PENDING')]);
      });
    } else if (raw && typeof raw === 'object') {
      Object.keys(raw).forEach(function (k) { items.push([V(k, 'm-cand'), ' ', raw[k] ? F(asField(raw[k])) : tag('PENDING')]); });
    }
    if (!items.length) return [tag('PENDING')];
    return items.map(function (it) { return el('span', { class: 'dc-line m-v' }, it); });
  }

  /* the first candidate's id as data names it, for the column title; a plain word when data has none */
  function baselineName(rows) {
    for (var i = 0; i < rows.length; i++) {
      var raw = pickKey(rows[i], ['verdicts', 'gates']);
      if (Array.isArray(raw) && raw.length) {
        var c = fieldOf(raw[0], ['cand', 'id', 'candidate']);
        if (c) return c.text;
      }
    }
    var g = D.governance && Array.isArray(D.governance.candidates) ? D.governance.candidates : [];
    if (g.length) {
      var f = fieldOf(g[0], ['id', 'candidate', 'candidate_id']);
      if (f) return f.text;
    }
    return copy('models.cand_fallback');
  }

  function headlineTag() {
    var M = D.models || {};
    var h = M.headline_tag !== undefined ? M.headline_tag : M.headline;
    var t = textOf(asField(h));
    if (!t) {
      var f = firstNumber(['headline.model']);
      t = f ? f.text : '';
    }
    return t;
  }

  /* the last folder name of a path, for a narrow column; the whole path stays in the title */
  function baseName(p) {
    var t = String(p || '').replace(/[\\/]+$/, '');
    var i = Math.max(t.lastIndexOf('/'), t.lastIndexOf('\\'));
    return i >= 0 ? t.slice(i + 1) : t;
  }

  /* a column title; the candidate id from data stays in one piece (RC-001 never breaks at its hyphen) */
  function columnHead(col, baseline) {
    var raw = copy('models.columns.' + col);
    var at = raw.indexOf('{cand}');
    if (at < 0) return raw;
    return [raw.slice(0, at), V(baseline, 'nowrap'), raw.slice(at + 6)];
  }

  function modelRow(row, headline, baseline) {
    var tf = rowTag(row);
    var st = toolsState(row);
    var noTools = st === 'no';
    var digest = fieldOf(row, ['digest12', 'digest']);
    var asr = noTools ? [copy('models.not_measured')] : metricNodes(pickKey(row, ['asr', 'attack', 'attack_success']));
    /* a model whose tool calls were never checked must not read as safe because its rate is low */
    if (st === 'unchecked') asr.push(el('span', { class: 'dc-line m-caution' }, copy('models.unchecked_note')));
    var benign = noTools ? [copy('models.not_measured')] : metricNodes(pickKey(row, ['benign', 'normal']));
    var mode = fieldOf(row, ['mode', 'mode_tag']);
    var folder = fieldOf(row, ['folder', 'source', 'dir', 'path']);
    var isHead = !!headline && !!tf && tf.text === headline;
    return el('tr', { 'data-tools': st, 'data-headline': isHead ? 'true' : null }, [
      el('td', { class: 'm-model' }, [
        tf ? F(tf, 'm-tag') : tag('PENDING'),
        digest ? el('span', { class: 'm-digest' }, [copy('models.digest_label') + ' ', F(digest)]) : null,
        isHead ? el('span', { class: 'headline-mark' }, copy('models.headline')) : null
      ]),
      el('td', null, [el('span', { class: 'm-tools', 'data-state': st }, copy('models.tools.' + st)), ' ', tag(toolsStatus(row))]),
      el('td', null, asr),
      el('td', null, benign),
      el('td', null, errorsNodes(row)),
      el('td', null, verdictNodes(row)),
      el('td', null, [mode ? tag(mode.status, mode.text) : tag('PENDING'), folder ? el('span', { class: 'dc-line m-folder', title: folder.text }, F({ text: baseName(folder.text), status: folder.status, prov: folder.prov })) : null])
    ]);
  }

  function errorsNodes(row) {
    var f = fieldOf(row, ['errors', 'error_runs']);
    return f ? withTag(f) : [tag('PENDING')];
  }

  function renderModels() {
    var t = $('m-table'), note = $('m-note'), sr = $('m-src');
    if (!t) return;
    var M = D.models && typeof D.models === 'object' ? D.models : null;
    var rows = M && Array.isArray(M.rows) ? M.rows.slice() : [];
    if (!rows.length) {
      setPanelStatus(t, 'PENDING');
      fill(t, [pendingBlock(copy('models.empty'), M && M.reason ? M.reason : null)]);
      fill(note, []);
      fill(sr, []);
      return;
    }
    rows.sort(function (a, b) {
      var x = textOf(rowTag(a)), y = textOf(rowTag(b));
      return x < y ? -1 : x > y ? 1 : 0;
    });
    var headline = headlineTag();
    var baseline = baselineName(rows);
    var cols = ['tag', 'tools', 'asr', 'benign', 'errors', 'verdicts', 'source'];
    setPanelStatus(t, M.status && M.status !== 'REAL' ? M.status : null);
    t.classList.remove('is-pending');
    fill(t, [
      el('table', { class: 'grid models' }, [
        el('colgroup', null, cols.map(function (c) { return el('col', { class: 'm-col-' + c }); })),
        el('thead', null, el('tr', null, cols.map(function (c) { return el('th', null, columnHead(c, baseline)); }))),
        el('tbody', null, rows.map(function (r) { return modelRow(r, headline, baseline); }))
      ])
    ]);
    fill(note, [
      el('p', { class: 'n-note' }, headline
        ? [copy('models.headline') + ': ', V(headline), '. ', copy('models.note')]
        : [copy('models.no_headline') + ' ', copy('models.note')])
    ]);
    var files = Array.isArray(M.source_files) ? M.source_files : (M.source_file ? [M.source_file] : []);
    fill(sr, files.length || M.status ? [src(['Models: ', V(files.join(', ') || 'no file'), ' ', tag(M.status || 'PENDING')])] : []);
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

  /* ---------- the cover: Redline, the team and the tagline, all from copy.js ---------- */

  var coverShown = false;

  function setText(id, text) {
    var n = $(id);
    if (!n) return;
    clear(n);
    if (text) { n.textContent = text; n.setAttribute('data-static', ''); }
  }

  /* Without copy.js there is no cover: the page opens on a screen, as before. */
  function renderCover() {
    var name = copy('product.name');
    if (!$('cover') || !name) return false;
    setText('cover-name', name);
    setText('cover-what', copy('product.what'));
    setText('cover-tagline', copy('product.tagline'));
    setText('cover-team', copy('product.team_line', { team: copy('product.team') }));
    setText('cover-track', copy('product.track'));
    setText('cover-hint', copy('cover.hint'));
    return true;
  }

  function showCover() {
    var cv = $('cover');
    if (!cv || !renderCover()) return false;
    if (drawerOpen) closeDrawer(false);
    cv.hidden = false;
    coverShown = true;
    doc.body.classList.add('cover-open');
    try { cv.focus({ preventScroll: true }); } catch (e) { cv.focus(); }
    return true;
  }

  function hideCover() {
    var cv = $('cover');
    if (!cv || !coverShown) return;
    cv.hidden = true;
    coverShown = false;
    doc.body.classList.remove('cover-open');
    var h = doc.querySelector('#screen-' + state.s + ' .screen-title');
    if (h) { try { h.focus({ preventScroll: true }); } catch (e) { h.focus(); } }
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
    /* on the cover any key starts the show; a digit also opens its screen, and R and P keep their jobs */
    if (coverShown) {
      if (k === 'Shift' || k === 'Control' || k === 'Alt' || k === 'Meta' || k === 'CapsLock') return;
      hideCover();
      if (k === ' ' || k === 'Enter' || k === 'Escape' || k === 'Tab' || k === 'ArrowRight') { e.preventDefault(); return; }
    }
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
    guard('decision', renderDecision);
    guard('models', renderModels);
  }

  function boot() {
    /* the cover shows only when the page opens with no screen in the address; a reload keeps the screen */
    var fresh = !location.hash || location.hash === '#';
    readHash();
    applyCopy();
    bindNav();
    var cover = $('cover');
    if (cover) cover.addEventListener('click', hideCover);
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
    if (fresh) showCover();
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
