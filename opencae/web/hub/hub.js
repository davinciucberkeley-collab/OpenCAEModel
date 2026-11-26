/* OpenCAE Results Hub front end. Plain JS, no build step. Renders only what /api/hub/* returns. */
(function () {
  'use strict';
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var content = $('#content');
  var TABS = ['overview', '3d', 'plots', 'motion', 'audit', 'history'];
  var S = {
    projects: [], pid: null, run: 'latest', tab: 'overview', cmp: null, project: null, gen: 0,
    webgl: true, webglError: '', cache: {}, viewer: null, plotObjs: [],
    v3: { field: null, cmap: 'viridis', lock: false, min: null, max: null, deform: null, slices: {}, stream: false, parts: {}, view: 'iso', allSlices: false }
  };

  // ------------------------------------------------------------------------------------------ helpers
  function h(tag, cls, text, kids) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null && text !== '') e.textContent = text;
    (kids || []).forEach(function (k) { if (k) e.appendChild(k); });
    return e;
  }
  function qs(p) {
    return Object.keys(p).filter(function (k) { return p[k] != null && p[k] !== ''; })
      .map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(p[k]); }).join('&');
  }
  function getJSON(path, params) {
    return fetch(path + (params ? '?' + qs(params) : ''), { cache: 'no-store' }).then(function (r) {
      return r.text().then(function (t) {
        var j = null; try { j = JSON.parse(t); } catch (e) { /* not json */ }
        if (!r.ok) throw new Error((j && (j.error || j.message)) || ('HTTP ' + r.status + ' for ' + path));
        if (j === null) throw new Error('The server did not return JSON for ' + path);
        return j;
      });
    });
  }
  function cached(kind, extra) {
    var key = kind + '|' + S.pid + '|' + S.run;
    if (!S.cache[key]) {
      S.cache[key] = getJSON('/api/hub/' + kind, { pid: S.pid, run: S.run }).catch(function (e) { delete S.cache[key]; throw e; });
    }
    return S.cache[key];
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function fmtTime(iso) {
    if (!iso) return '';
    var d = new Date(iso); if (isNaN(d)) return String(iso);
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function num(v) {
    if (v == null || v === '') return '-';
    if (typeof v !== 'number') return String(v);
    if (!isFinite(v)) return String(v);
    var a = Math.abs(v);
    if (a !== 0 && (a >= 1e6 || a < 1e-3)) return v.toExponential(3).replace('e+', 'e');
    return String(+v.toPrecision(5));
  }
  function val(v) {
    if (v == null) return '-';
    if (typeof v === 'object') return JSON.stringify(v);
    return typeof v === 'number' ? num(v) : String(v);
  }
  var STATUS = { ok: 'OK', watch: 'Watch', conditional: 'Conditional', fail: 'Fail', error: 'Solver failed', not_evaluated: 'Not evaluated' };
  function statusChip(st) {
    var k = String(st == null ? 'not_evaluated' : st).replace(/ /g, '_');
    return h('span', 'chip ' + (k === 'not_evaluated' ? 'nev' : k), STATUS[k] || String(st));
  }
  var VERDICT = { pass: 'Pass', conditional: 'Conditional', fail: 'Fail', 'not ready': 'Not ready' };
  function verdictChip(v) {
    if (!v) return h('span', 'chip nev', 'No verdict');
    var cls = v === 'pass' ? 'ok' : v === 'conditional' ? 'conditional' : v === 'fail' ? 'fail' : 'nev';
    return h('span', 'chip ' + cls, VERDICT[v] || v);
  }
  function basisBadge(b) { return b ? h('span', 'basis ' + b, 'basis: ' + b) : null; }
  var KIND = { drone: 'Drone', studio: 'Studio', 'k09l-study': 'K09-L study' };
  function kindBadge(k) { return h('span', 'kind', KIND[k] || k || 'project'); }
  function banner(cls, kids) { return h('div', 'banner ' + cls, '', kids); }
  function errorBox(e) { return banner('err', [h('b', '', 'Could not load this view'), h('div', 'why', e.message || String(e))]); }
  function loading(msg) { return h('div', 'loading', msg || 'Loading...'); }

  // ------------------------------------------------------------------------------------------ url + client report
  function readURL() {
    var p = new URLSearchParams(location.search);
    return { pid: p.get('pid'), run: p.get('run'), tab: p.get('tab'), cmp: p.get('cmp'), webgl: p.get('webgl') };
  }
  function syncURL() {
    var p = new URLSearchParams(location.search);
    function set(k, v) { if (v) p.set(k, v); else p.delete(k); }
    set('pid', S.pid); set('run', S.run && S.run !== 'latest' ? S.run : ''); set('tab', S.tab); set('cmp', S.cmp);
    try { history.replaceState(null, '', location.pathname + '?' + p.toString()); } catch (e) { /* ignore */ }
    var ob = $('#openbrowser'); if (ob) ob.href = location.href;
  }
  function postClient() {
    try {
      fetch('/api/hub/client', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ webgl: S.webgl, error: S.webglError || '', ua: navigator.userAgent }) }).catch(function () {});
    } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------------------------------------ sidebar
  function renderSidebar() {
    var q = ($('#search').value || '').toLowerCase().trim(), ul = $('#plist'); ul.textContent = '';
    var list = S.projects.filter(function (p) {
      return !q || (p.name + ' ' + p.kind + ' ' + (p.verdict || '') + ' ' + (p.disciplines || []).join(' ')).toLowerCase().indexOf(q) >= 0;
    });
    if (!list.length) ul.appendChild(h('li', 'muted small', S.projects.length ? 'No project matches the search.' : 'No projects found yet. Run an analysis or register a project.'));
    list.forEach(function (p) {
      var b = h('button', 'prow', '', [
        h('b', '', p.name, p.location ? [h('span', 'loc', ' ' + p.location)] : null),
        h('div', 'l2', '', [kindBadge(p.kind), verdictChip(p.verdict)]),
        h('div', 'l3', (p.runs ? p.runs + (p.runs === 1 ? ' run' : ' runs') + ' - last ' + fmtTime(p.updated) : 'no runs yet'))
      ]);
      b.type = 'button'; b.dataset.pid = p.id;
      if (p.id === S.pid) b.setAttribute('aria-current', 'true');
      b.onclick = function () { openProject(p.id, null, S.tab); };
      var li = h('li'); li.appendChild(b); ul.appendChild(li);
    });
  }

  // ------------------------------------------------------------------------------------------ header
  function renderHeader() {
    var p = S.project, pr = S.projects.filter(function (x) { return x.id === S.pid; })[0] || {};
    $('#pname').textContent = p ? p.name : 'Results Hub';
    if (p && pr.location) $('#pname').appendChild(h('span', 'loc', ' ' + pr.location));
    var k = $('#pkind'); k.hidden = !p; if (p) k.textContent = KIND[p.kind] || p.kind;
    var v = $('#pverdict'); v.textContent = ''; if (p) v.appendChild(verdictChip(pr.verdict));
    var rs = $('#runsel'), cs = $('#cmpsel'); rs.textContent = ''; cs.textContent = '';
    var runs = (p && p.runs) || [];
    runs.forEach(function (r) {
      var o = h('option', '', (r.label || r.run) + (r.started ? '  (' + fmtTime(r.started) + ')' : '') + (r.verdict ? '  - ' + r.verdict : '')); o.value = r.run; rs.appendChild(o);
    });
    if (!runs.length) { var o0 = h('option', '', p ? (p.run || 'no runs') : '-'); o0.value = p ? p.run : ''; rs.appendChild(o0); }
    rs.value = S.run; rs.disabled = !p;
    var none = h('option', '', 'none'); none.value = ''; cs.appendChild(none);
    runs.forEach(function (r) { if (r.run !== S.run) { var o = h('option', '', r.label || r.run); o.value = r.run; cs.appendChild(o); } });
    cs.value = S.cmp || ''; cs.disabled = runs.length < 2;
    syncURL();
  }
  function renderTabs() {
    Array.prototype.forEach.call(document.querySelectorAll('#tabs button'), function (b) {
      b.setAttribute('aria-selected', b.dataset.tab === S.tab ? 'true' : 'false');
    });
  }

  // ------------------------------------------------------------------------------------------ navigation
  function leaveTab() {
    if (S.viewer) { try { S.viewer.dispose(); } catch (e) { /* ignore */ } S.viewer = null; }
    S.plotObjs.forEach(function (p) { try { p.destroy(); } catch (e) { /* ignore */ } }); S.plotObjs = [];
    content.classList.remove('flush');
  }
  function setTab(t) {
    if (TABS.indexOf(t) < 0) t = 'overview';
    S.tab = t; renderTabs(); syncURL(); renderTab();
  }
  function renderTab() {
    leaveTab(); content.textContent = '';
    if (!S.pid) {
      content.appendChild(h('div', 'empty', S.projects.length ? 'Pick a project on the left.' : 'No projects found yet. Run an analysis from the app or with the opencae command line, then refresh.'));
      return;
    }
    if (!S.project) { content.appendChild(loading('Loading project...')); return; }
    var gen = S.gen;
    var fn = { overview: renderOverview, '3d': render3D, plots: renderPlots, motion: renderMotion, audit: renderAudit, history: renderHistory }[S.tab];
    try { fn(gen); } catch (e) { content.textContent = ''; content.appendChild(errorBox(e)); }
  }
  function openProject(pid, run, tab) {
    var gen = ++S.gen; S.pid = pid; S.run = run || 'latest'; S.project = null; S.cmp = null; S.compareResult = null;
    S.v3.field = null; S.v3.slices = {}; S.v3.parts = {}; S.v3.stream = false; S.v3.allSlices = false; S.v3.lock = false;
    renderSidebar(); renderHeader(); S.tab = TABS.indexOf(tab) >= 0 ? tab : 'overview'; renderTabs(); renderTab();
    return getJSON('/api/hub/project', { pid: pid, run: run || 'latest' }).then(function (p) {
      if (gen !== S.gen) return;
      S.project = p; S.run = p.run || S.run;
      renderHeader(); renderTab();
    }).catch(function (e) {
      if (gen !== S.gen) return;
      leaveTab(); content.textContent = ''; content.appendChild(errorBox(e));
    });
  }
  function openRun(run) {
    var gen = ++S.gen; S.run = run; S.cmp = null; S.compareResult = null; S.project = null; renderHeader(); renderTab();
    return getJSON('/api/hub/project', { pid: S.pid, run: run }).then(function (p) {
      if (gen !== S.gen) return; S.project = p; S.run = p.run || run; renderHeader(); renderTab();
    }).catch(function (e) { if (gen === S.gen) { leaveTab(); content.textContent = ''; content.appendChild(errorBox(e)); } });
  }

  // ------------------------------------------------------------------------------------------ overview
  function renderOverview() {
    var p = S.project, root = h('div');
    if (p.notice) root.appendChild(banner('info', [h('div', '', p.notice)]));
    (p.analyses || []).filter(function (a) { return a.status === 'error'; }).forEach(function (a) {
      root.appendChild(banner('err', [h('b', '', a.title + ' failed: '), h('div', 'why', a.summary || 'The solver reported an error and gave no message.')]));
    });
    var cards = h('div', 'cards'); cards.id = 'cards';
    (p.cards || []).forEach(function (c) {
      var card = h('div', 'card ' + (c.status || 'nev'), '', []);
      card.dataset.card = c.id;
      card.appendChild(h('div', 'hd', '', [
        h('div', '', '', [h('div', 'disc', c.discipline), h('b', '', c.title)]),
        h('div', 'badges', '', [statusChip(c.status), basisBadge(c.basis)])
      ]));
      if (c.headline && c.headline.value != null) {
        var big = h('div', 'big', num(c.headline.value)); if (c.headline.unit) big.appendChild(h('small', '', c.headline.unit));
        card.appendChild(big); if (c.headline.label) card.appendChild(h('div', 'biglab', c.headline.label));
      }
      if (c.text) card.appendChild(h('div', 'txt', c.text));
      if ((c.metrics || []).length) {
        var m = h('div', 'minis');
        c.metrics.forEach(function (x) {
          var b = h('b', '', val(x.value) + (x.unit ? ' ' + x.unit : ''));
          if (x.status) b.appendChild(h('i', x.status));
          var row = h('div', 'mini', '', [h('span', '', x.label), b]);
          if (x.basis) row.title = 'basis: ' + x.basis; m.appendChild(row);
        });
        card.appendChild(m);
      }
      cards.appendChild(card);
    });
    if (!(p.cards || []).length) root.appendChild(h('div', 'empty', 'This run has no result cards. ' + (p.notice || 'Run an analysis to see numbers here.')));
    else root.appendChild(cards);
    if ((p.analyses || []).length) {
      root.appendChild(h('h2', 'sec', 'Analyses in this run'));
      var t = h('table', 't'), th = h('thead'), tb = h('tbody');
      th.appendChild(h('tr', '', '', ['Analysis', 'Discipline', 'Status', 'Summary'].map(function (x) { return h('th', '', x); })));
      p.analyses.forEach(function (a) {
        var tr = h('tr', a.status === 'error' ? 'bad' : '', '', [h('td', '', a.title), h('td', '', a.discipline), h('td', '', '', [statusChip(a.status)]), h('td', '', a.summary || '')]);
        tb.appendChild(tr);
      });
      t.appendChild(th); t.appendChild(tb); var w = h('div', 'tblwrap'); w.appendChild(t); root.appendChild(w);
    }
    content.appendChild(root);
  }

  // ------------------------------------------------------------------------------------------ 3D & contours
  function render3D(gen) {
    content.classList.add('flush'); content.appendChild(loading('Loading 3D data...'));
    cached('scene').then(function (sc) {
      if (gen !== S.gen || S.tab !== '3d') return;
      content.textContent = ''; build3D(sc);
    }).catch(function (e) { if (gen === S.gen && S.tab === '3d') { content.classList.remove('flush'); content.textContent = ''; content.appendChild(errorBox(e)); } });
  }
  function fieldRange(f) {
    var v = S.v3;
    if (v.lock && v.min != null && v.max != null && v.unit === f.unit) return [v.min, v.max];
    return [f.min, f.max];
  }
  function build3D(sc) {
    var v = S.v3, fields = sc.fields || [], parts = sc.parts || [], slices = sc.slices || [];
    var wrap = h('div', 'v3'), ctl = h('div', 'ctl'), stage = h('div', 'stage');
    wrap.appendChild(ctl); wrap.appendChild(stage); content.appendChild(wrap);
    var field = fields.filter(function (f) { return f.id === v.field; })[0] || fields[0] || null;
    v.field = field ? field.id : null;
    var viewer = null, canvasHost = h('div', 'viewer'), legendEl = h('div', 'legend'), readout = h('div', 'readout', 'Click the surface to read the value.'), vnote = h('div', 'vnote');
    vnote.hidden = true; var fieldBtns = {};

    // -- field picker, grouped by discipline
    ctl.appendChild(h('h4', '', 'Field'));
    var groups = {}, order = [];
    fields.forEach(function (f) { var d = f.discipline || 'other'; if (!groups[d]) { groups[d] = []; order.push(d); } groups[d].push(f); });
    var picker = h('div', 'picker'); picker.id = 'fieldpicker';
    if (!fields.length) picker.appendChild(h('div', 'muted small', 'This run stored no fields.'));
    order.forEach(function (d) {
      var g = h('div', 'fgroup'); g.appendChild(h('h4', '', d));
      groups[d].forEach(function (f) {
        var b = h('button', 'fbtn', '', [h('span', '', f.label || f.id), h('span', 'u', f.unit || '')]);
        b.type = 'button'; b.dataset.field = f.id; b.setAttribute('aria-pressed', f.id === v.field ? 'true' : 'false');
        b.onclick = function () { v.field = f.id; field = f; Object.keys(fieldBtns).forEach(function (k) { fieldBtns[k].setAttribute('aria-pressed', k === f.id ? 'true' : 'false'); }); applyField(); };
        fieldBtns[f.id] = b; g.appendChild(b);
      });
      picker.appendChild(g);
    });
    ctl.appendChild(picker);
    if (sc.notes && sc.notes.length) { var nl = h('ul', 'note-list'); sc.notes.forEach(function (n) { nl.appendChild(h('li', '', n)); }); ctl.appendChild(nl); }

    var webglOnly = h('div', 'webglctl');
    // colour map + range
    webglOnly.appendChild(h('h4', '', 'Colours'));
    var cmapSel = h('select'); cmapSel.id = 'cmap'; HubViewer.cmaps.forEach(function (c) { var o = h('option', '', c); o.value = c; cmapSel.appendChild(o); }); cmapSel.value = v.cmap;
    webglOnly.appendChild(h('div', 'fld', '', [h('label', '', 'Colour map'), cmapSel]));
    var minIn = h('input'), maxIn = h('input'); minIn.type = maxIn.type = 'number'; minIn.step = maxIn.step = 'any'; minIn.id = 'rmin'; maxIn.id = 'rmax';
    webglOnly.appendChild(h('div', 'rng', '', [minIn, maxIn]));
    var lockCb = h('input'); lockCb.type = 'checkbox'; lockCb.id = 'rlock'; lockCb.checked = v.lock;
    var lockLab = h('label', 'chk', '', [lockCb, h('span', '', 'Lock range'), h('small', '', '(keeps min and max when you change field or run)')]);
    webglOnly.appendChild(lockLab);
    // deformation
    var defWrap = h('div'); defWrap.id = 'deformbox';
    var defRange = h('input'); defRange.type = 'range'; defRange.id = 'deform'; defRange.min = 0; defRange.step = 'any';
    var defVal = h('span', 'mono', '');
    defWrap.appendChild(h('h4', '', 'Deformation scale'));
    defWrap.appendChild(h('div', 'fld', '', [h('label', '', 'Scale'), h('div', '', '', [defRange])]));
    defWrap.appendChild(h('div', 'small muted', '')); defWrap.lastChild.appendChild(defVal);
    defWrap.lastChild.appendChild(document.createTextNode(' times the stored displacement (shape only, not a result)'));
    webglOnly.appendChild(defWrap);
    // CFD layers
    var layers = h('div'); layers.id = 'layers';
    var sliceBoxes = [], strCb = null;
    if (slices.length || sc.streamlines) {
      layers.appendChild(h('h4', '', 'CFD layers'));
      if (slices.length) {
        var allCb = h('input'); allCb.type = 'checkbox'; allCb.id = 'slices-all'; allCb.checked = v.allSlices;
        layers.appendChild(h('label', 'chk', '', [allCb, h('span', '', 'Slices'), h('small', '', '(' + slices.length + ')')]));
        slices.forEach(function (s, i) {
          var cb = h('input'); cb.type = 'checkbox'; cb.dataset.slice = i; cb.checked = !!v.slices[s.name];
          var lab = h('label', 'chk', '', [cb, h('span', '', s.name || ('slice ' + (i + 1))), h('small', '', s.unit || '')]); lab.style.marginLeft = '18px';
          cb.onchange = function () { v.slices[s.name] = cb.checked; if (viewer) viewer.setSliceVisible(i, cb.checked); drawLegend(); };
          sliceBoxes.push(cb); layers.appendChild(lab);
        });
        allCb.onchange = function () { v.allSlices = allCb.checked; sliceBoxes.forEach(function (cb, i) { cb.checked = allCb.checked; v.slices[slices[i].name] = allCb.checked; if (viewer) viewer.setSliceVisible(i, allCb.checked); }); drawLegend(); };
      }
      if (sc.streamlines) {
        strCb = h('input'); strCb.type = 'checkbox'; strCb.id = 'streams'; strCb.checked = v.stream;
        layers.appendChild(h('label', 'chk', '', [strCb, h('span', '', 'Streamlines'), h('small', '', sc.streamlines.unit || '')]));
        strCb.onchange = function () { v.stream = strCb.checked; if (viewer) viewer.setStreamVisible(strCb.checked); drawLegend(); };
      }
    }
    webglOnly.appendChild(layers);
    // parts
    var partsBox = h('div'); partsBox.id = 'parts';
    if (parts.length) {
      partsBox.appendChild(h('h4', '', 'Parts'));
      parts.forEach(function (p) {
        var cb = h('input'); cb.type = 'checkbox'; cb.checked = v.parts[p.name] !== false; cb.dataset.part = p.name;
        cb.onchange = function () { v.parts[p.name] = cb.checked; if (viewer) viewer.setPartVisible(p.name, cb.checked); };
        partsBox.appendChild(h('label', 'chk', '', [cb, h('span', '', p.name), p.role ? h('small', '', p.role) : null]));
      });
    }
    webglOnly.appendChild(partsBox);
    ctl.appendChild(webglOnly);

    // -- stage chrome
    var tools = h('div', 'vtools'), seg = h('div', 'seg'), viewBtns = {};
    [['iso', 'Iso'], ['top', 'Top'], ['front', 'Front'], ['side', 'Side']].forEach(function (x) {
      var b = h('button', '', x[1]); b.type = 'button'; b.dataset.view = x[0]; b.setAttribute('aria-pressed', v.view === x[0] ? 'true' : 'false');
      b.onclick = function () { v.view = x[0]; Object.keys(viewBtns).forEach(function (k) { viewBtns[k].setAttribute('aria-pressed', k === x[0] ? 'true' : 'false'); }); if (viewer) viewer.view(x[0]); else applyImage(); };
      viewBtns[x[0]] = b; seg.appendChild(b);
    });
    tools.appendChild(seg);
    var reset = h('button', 'btn small', 'Reset view'); reset.type = 'button'; reset.id = 'resetview';
    reset.onclick = function () { v.view = 'iso'; Object.keys(viewBtns).forEach(function (k) { viewBtns[k].setAttribute('aria-pressed', k === 'iso' ? 'true' : 'false'); }); if (viewer) viewer.view('reset'); else applyImage(); };
    tools.appendChild(reset);
    var hud = h('div', 'hud'); hud.appendChild(readout); hud.appendChild(legendEl);
    stage.appendChild(canvasHost); stage.appendChild(tools); stage.appendChild(vnote); stage.appendChild(hud);

    function msg(html) { var m = h('div', 'vmsg'); m.appendChild(h('div', '', html)); stage.appendChild(m); return m; }
    function note(t) { vnote.hidden = !t; vnote.textContent = t || ''; }

    function drawLegend() {
      legendEl.textContent = '';
      if (!viewer || !field) { legendEl.hidden = true; return; }
      legendEl.hidden = false;
      var r = fieldRange(field);
      function block(title, unit, lo, hi) {
        var cv = document.createElement('canvas'); HubViewer.drawBar(cv, v.cmap);
        return h('div', '', '', [h('div', 'lt', title, [h('span', '', unit || '')]), cv, h('div', 'lm', '', [h('span', '', num(lo)), h('span', '', num(hi))])]);
      }
      legendEl.appendChild(block(field.label || field.id, field.unit, r[0], r[1]));
      viewer.sliceInfo().forEach(function (s, i) {
        if (!(sliceBoxes[i] && sliceBoxes[i].checked)) return;
        var same = s.unit === field.unit; legendEl.appendChild(block('Slice: ' + (s.name || s.label), s.unit, same ? r[0] : s.min, same ? r[1] : s.max));
      });
      var si = viewer.streamInfo();
      if (si && strCb && strCb.checked) legendEl.appendChild(block('Streamlines', si.unit, si.min, si.max));
    }
    function setRangeInputs() {
      var r = field ? fieldRange(field) : [0, 1];
      minIn.value = r[0] == null ? '' : +(+r[0]).toPrecision(6); maxIn.value = r[1] == null ? '' : +(+r[1]).toPrecision(6);
    }
    function applyField() {
      var r = field ? fieldRange(field) : [0, 1];
      if (field) {
        defWrap.hidden = !field.displacement; var mx = (field.scale_hint || 1) * 5;
        defRange.max = mx; if (v.deform == null || v.deformField !== field.id) { v.deform = field.scale_hint || 0; v.deformField = field.id; }
        defRange.value = v.deform; defVal.textContent = num(v.deform) + 'x';
      } else defWrap.hidden = true;
      setRangeInputs();
      if (viewer) {
        try {
          viewer.showField(field, { cmap: v.cmap, min: r[0], max: r[1], deform: v.deform || 0 });
          note('');
        } catch (e) { note('This field could not be drawn: ' + e.message); }
        drawLegend();
      } else applyImage();
    }
    function applyRange() {
      var a = parseFloat(minIn.value), b = parseFloat(maxIn.value);
      if (!(a < b)) return;
      v.lock = true; lockCb.checked = true; v.min = a; v.max = b; v.unit = field && field.unit;
      if (viewer) { viewer.recolor(v.cmap, a, b); drawLegend(); }
    }
    minIn.onchange = maxIn.onchange = applyRange;
    cmapSel.onchange = function () { v.cmap = cmapSel.value; if (viewer && field) { var r = fieldRange(field); viewer.recolor(v.cmap, r[0], r[1]); drawLegend(); } };
    lockCb.onchange = function () {
      v.lock = lockCb.checked;
      if (v.lock && field) { v.min = parseFloat(minIn.value); v.max = parseFloat(maxIn.value); v.unit = field.unit; }
      if (!v.lock) { setRangeInputs(); if (viewer && field) { viewer.recolor(v.cmap, field.min, field.max); drawLegend(); } }
    };
    defRange.oninput = function () { v.deform = parseFloat(defRange.value); defVal.textContent = num(v.deform) + 'x'; if (viewer) viewer.setDeform(v.deform); };

    // -- fallback: server-rendered PNG
    var img = null, imgToken = 0;
    function applyImage() {
      var tok = ++imgToken;
      Array.prototype.forEach.call(stage.querySelectorAll('.vmsg'), function (m) { m.remove(); });
      if (!img) { img = h('img', 'shot'); img.id = 'shot'; img.alt = 'Server-rendered view'; stage.insertBefore(img, tools); }
      if (!field && !parts.length) { img.removeAttribute('src'); img.hidden = true; msg('This run has no 3D data. ' + (sc.notes || []).join(' ')); return; }
      img.hidden = false;
      var params = { pid: S.pid, run: S.run, field: field ? field.id : '', view: v.view };
      readout.textContent = 'Rendering on the server...';
      fetch('/api/hub/image.png?' + qs(params)).then(function (r) {
        var ct = r.headers.get('content-type') || '';
        if (!r.ok || ct.indexOf('image/') !== 0) return r.text().then(function (t) { var m = t; try { var j = JSON.parse(t); m = j.error || j.message || t; } catch (e) { /* text */ } throw new Error(m || ('HTTP ' + r.status)); });
        return r.blob();
      }).then(function (blob) {
        if (tok !== imgToken) return; img.src = URL.createObjectURL(blob); readout.textContent = 'Server image: ' + (field ? (field.label || field.id) + (field.unit ? ' (' + field.unit + ')' : '') : 'geometry') + ', ' + v.view + ' view.';
      }).catch(function (e) { if (tok !== imgToken) return; img.removeAttribute('src'); readout.textContent = ''; msg('The server could not draw this view: ' + e.message); });
    }

    // -- start
    var nothing = !fields.length && !parts.length;
    if (nothing) {
      readout.hidden = true; legendEl.hidden = true; tools.hidden = true; webglOnly.hidden = true;
      msg('No 3D data for this run. ' + ((sc.notes || []).join(' ') || 'Run an analysis that stores fields.')); return;
    }
    if (S.webgl) {
      try {
        viewer = HubViewer.create(canvasHost, {
          onLost: function () { S.webgl = false; S.webglError = 'WebGL context lost'; postClient(); startFallback('The 3D view lost its graphics context.'); },
          onPick: function (r) {
            if (!r) { readout.textContent = 'Nothing under the pointer. Click the surface to read the value.'; return; }
            var xyz = [r.point.x, r.point.y, r.point.z].map(function (n) { return num(+n.toFixed(2)); }).join(', ');
            readout.textContent = r.value != null ? (r.name + ': ' + num(r.value) + (r.unit ? ' ' + r.unit : '') + '  at (' + xyz + ') mm') : ('Part ' + (r.name || '?') + ' at (' + xyz + ') mm. Select a field to read values.');
          }
        });
        S.viewer = viewer;
        viewer.setScene(sc); viewer.setLayers(sc);
        parts.forEach(function (p) { if (v.parts[p.name] === false) viewer.setPartVisible(p.name, false); });
        slices.forEach(function (s, i) { viewer.setSliceVisible(i, !!v.slices[s.name]); });
        viewer.setStreamVisible(!!v.stream);
        viewer.view(v.view === 'reset' ? 'iso' : v.view);
        applyField();
        if (!fields.length) note('This run has no fields to colour. ' + (sc.notes || []).join(' '));
        return;
      } catch (e) {
        S.webgl = false; S.webglError = String(e && e.message || e); postClient();
        if (S.viewer) { try { S.viewer.dispose(); } catch (x) { /* ignore */ } S.viewer = null; } viewer = null;
        canvasHost.textContent = '';
      }
    }
    startFallback();
    function startFallback(why) {
      if (viewer) { try { viewer.dispose(); } catch (x) { /* ignore */ } viewer = null; S.viewer = null; }
      webglOnly.textContent = ''; webglOnly.appendChild(h('div', 'small muted', 'Colour map, range, slices and part controls need WebGL. The server image carries its own colour bar.'));
      legendEl.hidden = true;
      note((why ? why + ' ' : '') + '3D view is not available here (WebGL is off). Showing server-rendered images.');
      readout.textContent = '';
      applyField();
    }
  }

  // ------------------------------------------------------------------------------------------ plots
  function renderPlots(gen) {
    content.appendChild(loading('Loading plots...'));
    cached('plots').then(function (plots) {
      if (gen !== S.gen || S.tab !== 'plots') return;
      content.textContent = '';
      if (!plots.length) { content.appendChild(h('div', 'empty', 'This run has no plots. Residual and force histories appear after a CFD run that keeps its log.')); return; }
      var grid = h('div', 'plots'); content.appendChild(grid);
      plots.forEach(function (pl) {
        var card = h('div', 'pcard'), box = h('div', 'plotbox'); card.dataset.plot = pl.id;
        var acts = h('div', 'acts'), logCb = h('input'); logCb.type = 'checkbox';
        var logLab = h('label', 'chk', '', [logCb, h('span', '', 'Log y')]);
        var rb = h('button', 'btn small', 'Reset zoom'); rb.type = 'button';
        var cb = h('button', 'btn small', 'Download CSV'); cb.type = 'button';
        acts.appendChild(logLab); acts.appendChild(rb); acts.appendChild(cb);
        card.appendChild(h('div', 'hd', '', [h('h3', '', pl.title || pl.id), acts]));
        if (pl.note) card.appendChild(h('div', 'note', pl.note));
        card.appendChild(box); grid.appendChild(card);
        var po = HubPlots.mount(box, pl); S.plotObjs.push(po);
        logCb.checked = po.isLog(); logLab.hidden = !po.canLog;
        logCb.onchange = function () { po.setLog(logCb.checked); };
        rb.onclick = function () { po.reset(); };
        cb.onclick = function () { HubPlots.download((pl.id || 'plot') + '.csv', po.csv()); };
      });
    }).catch(function (e) { if (gen === S.gen) { content.textContent = ''; content.appendChild(errorBox(e)); } });
  }

  // ------------------------------------------------------------------------------------------ motion
  var motionScript = null;
  function loadMotion() {
    if (window.HubMotion) return Promise.resolve(true);
    if (!motionScript) motionScript = new Promise(function (res) {
      var s = document.createElement('script'); s.src = '/hub/motion.js';
      s.onload = function () { res(!!window.HubMotion); }; s.onerror = function () { res(false); }; document.head.appendChild(s);
    });
    return motionScript;
  }
  function renderMotion(gen) {
    content.appendChild(loading('Loading motion data...'));
    Promise.all([loadMotion(), cached('motion').catch(function (e) { return { error: e }; })]).then(function (r) {
      if (gen !== S.gen || S.tab !== 'motion') return;
      content.textContent = ''; var data = r[1];
      if (data.error) { content.appendChild(errorBox(data.error)); return; }
      if (!r[0]) {
        content.appendChild(h('div', 'empty', 'The motion player is not installed in this build, so the recorded motion cannot be played here.'));
        if ((data.notes || []).length) { var ul = h('ul', 'note-list'); data.notes.forEach(function (n) { ul.appendChild(h('li', '', n)); }); content.appendChild(ul); }
        return;
      }
      var host = h('div', 'motion-host'); host.id = 'motion-host'; content.appendChild(host);   // HubMotion draws title, basis, simulator and notes
      HubMotion.mount(host, data, { webgl: S.webgl });
    });
  }

  // ------------------------------------------------------------------------------------------ audit
  function kvList(obj) {
    var dl = h('dl', 'kv'); var keys = Object.keys(obj || {});
    if (!keys.length) return h('div', 'small muted', 'nothing recorded');
    keys.forEach(function (k) { dl.appendChild(h('dt', '', k.replace(/_/g, ' '))); dl.appendChild(h('dd', '', val(obj[k]))); });
    return dl;
  }
  function table(heads, rows, cls) {
    var t = h('table', 't'), th = h('thead'), tb = h('tbody');
    th.appendChild(h('tr', '', '', heads.map(function (x) { return h('th', x.r ? 'r' : '', x.t); })));
    rows.forEach(function (r) { var tr = h('tr', r.bad ? 'bad' : ''); r.cells.forEach(function (c, i) { var td = h('td', heads[i].r ? 'r' : ''); if (c instanceof Node) td.appendChild(c); else td.textContent = c; tr.appendChild(td); }); tb.appendChild(tr); });
    t.appendChild(th); t.appendChild(tb); var w = h('div', 'tblwrap'); w.appendChild(t); return w;
  }
  function renderAudit(gen) {
    content.appendChild(loading('Loading audit...'));
    cached('audit').then(function (list) {
      if (gen !== S.gen || S.tab !== 'audit') return;
      content.textContent = '';
      if (!list.length) { content.appendChild(h('div', 'empty', 'No audit records for this run.')); return; }
      list.forEach(function (a) {
        var c = h('section', 'acard'); c.dataset.analysis = a.analysis;
        c.appendChild(h('div', 'hd', '', [h('h2', '', a.title || a.analysis), statusChip(a.status)]));
        c.lastChild.firstChild.style.margin = '0';
        if ((a.problems || []).length || a.status === 'error') {
          var pb = h('div', 'problems'); pb.appendChild(h('b', '', a.status === 'error' ? 'This analysis failed' : 'Problems'));
          var ul = h('ul'); (a.problems || []).forEach(function (p) { ul.appendChild(h('li', '', p)); });
          if (!(a.problems || []).length) ul.appendChild(h('li', '', 'The solver reported an error and no further message was stored.'));
          pb.appendChild(ul); c.appendChild(pb);
        }
        var cols = h('div', 'cols');
        var l = h('div'); l.appendChild(h('h4', '', 'Solver')); l.appendChild(kvList(a.solver));
        l.appendChild(h('h4', '', 'Mesh quality')); l.appendChild(kvList(a.mesh));
        var r = h('div'); r.appendChild(h('h4', '', 'Convergence')); r.appendChild(kvList(a.convergence));
        if ((a.assumptions || []).length) { r.appendChild(h('h4', '', 'Assumptions')); var al = h('ul', 'plain'); a.assumptions.forEach(function (x) { al.appendChild(h('li', '', x)); }); r.appendChild(al); }
        cols.appendChild(l); cols.appendChild(r); c.appendChild(cols);
        var inputs = a.inputs || [];
        function inputTable(list) {
          return table([{ t: 'Input' }, { t: 'Value', r: true }, { t: 'Evidence' }, { t: 'Source' }], list.map(function (i) {
            return { cells: [i.name, val(i.value) + (i.unit ? ' ' + i.unit : ''), i.status ? basisBadge(i.status) : '', i.source || ''] };
          }));
        }
        var mainIn = inputs.filter(function (i) { return i.group !== 'configuration'; }), cfgIn = inputs.filter(function (i) { return i.group === 'configuration'; });
        if (mainIn.length) { c.appendChild(h('h4', '', 'Inputs and evidence')); c.appendChild(inputTable(mainIn)); }
        if (cfgIn.length) { var dc = h('details', 'log cfg'); dc.appendChild(h('summary', '', 'Configuration (' + cfgIn.length + ')')); dc.appendChild(inputTable(cfgIn)); c.appendChild(dc); }
        if ((a.checks || []).length) {
          c.appendChild(h('h4', '', 'Checks against limits'));
          c.appendChild(table([{ t: 'Check' }, { t: 'Value', r: true }, { t: 'Limit', r: true }, { t: 'Result' }], a.checks.map(function (k) {
            return { bad: k.ok === false, cells: [k.name, val(k.value) + (k.unit ? ' ' + k.unit : ''), val(k.limit) + (k.limit != null && k.unit ? ' ' + k.unit : ''), k.ok === true ? 'within limit' : k.ok === false ? 'outside limit' : 'not judged'] };
          })));
        }
        if (a.log_tail) {
          var d = h('details', 'log'); d.appendChild(h('summary', '', 'Solver log (last lines)')); d.appendChild(h('pre', 'logtxt', a.log_tail)); c.appendChild(d);
        }
        if ((a.files || []).length) {
          var d2 = h('details', 'log'); d2.appendChild(h('summary', '', 'Files (' + a.files.length + ')'));
          d2.appendChild(table([{ t: 'File' }, { t: 'Size', r: true }], a.files.map(function (f) { return { cells: [f.name, f.bytes != null ? f.bytes.toLocaleString('en-US') + ' bytes' : ''] }; })));
          c.appendChild(d2);
        }
        content.appendChild(c);
      });
    }).catch(function (e) { if (gen === S.gen) { content.textContent = ''; content.appendChild(errorBox(e)); } });
  }

  // ------------------------------------------------------------------------------------------ history + compare
  function renderHistory() {
    var p = S.project, runs = p.runs || [], picked = [];
    content.appendChild(h('h2', '', 'Run history'));
    if (!runs.length) { content.appendChild(h('div', 'empty', 'No runs have been stored for this project yet.')); return; }
    var bar = h('div', 'cmpbar'), cmpBtn = h('button', 'btn primary', 'Compare the 2 selected runs'); cmpBtn.type = 'button'; cmpBtn.disabled = true; cmpBtn.id = 'docompare';
    bar.appendChild(cmpBtn); bar.appendChild(h('span', 'small muted', 'Tick two runs to see the change in every number they share.'));
    content.appendChild(bar);
    var tb = [];
    runs.forEach(function (r) {
      var cb = h('input'); cb.type = 'checkbox'; cb.dataset.run = r.run; cb.setAttribute('aria-label', 'Select run ' + r.run);
      cb.onchange = function () {
        if (cb.checked) picked.push(r.run); else picked = picked.filter(function (x) { return x !== r.run; });
        if (picked.length > 2) { var drop = picked.shift(); var o = content.querySelector('input[data-run="' + drop + '"]'); if (o) o.checked = false; }
        cmpBtn.disabled = picked.length !== 2;
      };
      var open = h('button', 'btn small', r.run === S.run ? 'Open (current)' : 'Open'); open.type = 'button'; open.dataset.open = r.run; open.onclick = function () { openRun(r.run); };
      var vs = h('span'); vs.appendChild(verdictChip(r.verdict));
      var an = r.analyses; if (an && typeof an === 'object') { var items = Array.isArray(an) ? an : Object.keys(an).map(function (k) { return { id: k, status: an[k] }; }); items.forEach(function (x) { var c = statusChip(x.status); c.textContent = (x.title || x.id || x.analysis) + ': ' + c.textContent; c.style.marginLeft = '4px'; vs.appendChild(c); }); }
      tb.push({ cur: r.run === S.run, cells: [cb, r.label || r.run, fmtTime(r.started), vs, r.only ? String(r.only) : 'all', r.duration_s != null ? num(r.duration_s) + ' s' : '', open] });
    });
    var t = table([{ t: '' }, { t: 'Run' }, { t: 'Started' }, { t: 'Verdict' }, { t: 'Analyses run' }, { t: 'Duration', r: true }, { t: '' }], tb);
    Array.prototype.forEach.call(t.querySelectorAll('tbody tr'), function (tr, i) { if (tb[i].cur) tr.className = 'cur'; });
    t.id = 'runs'; content.appendChild(t);
    var out = h('div'); out.id = 'compare-out'; content.appendChild(out);
    function compare(a, b) {
      out.textContent = ''; out.appendChild(loading('Comparing...'));
      getJSON('/api/hub/compare', { pid: S.pid, a: a, b: b }).then(function (r) {
        out.textContent = ''; out.appendChild(h('h2', 'sec', 'Comparison'));
        out.appendChild(h('div', 'small muted', 'A = ' + a + '   B = ' + b + '. Change = B minus A.'));
        var ms = r.metrics || [];
        if (!ms.length) { out.appendChild(h('div', 'empty', 'These runs share no numbers to compare.')); return; }
        out.appendChild(table([{ t: 'Analysis' }, { t: 'Quantity' }, { t: 'Unit' }, { t: 'A', r: true }, { t: 'B', r: true }, { t: 'Change', r: true }, { t: 'Change %', r: true }],
          ms.map(function (m) { return { cells: [m.analysis || '', m.label, m.unit || '', num(m.a), num(m.b), num(m.delta), m.delta_pct == null ? '-' : num(m.delta_pct) + ' %'] }; })));
      }).catch(function (e) { out.textContent = ''; out.appendChild(errorBox(e)); });
    }
    cmpBtn.onclick = function () { compare(picked[0], picked[1]); };
    if (S.cmp) { compare(S.run, S.cmp); }
  }

  // ------------------------------------------------------------------------------------------ boot
  function boot() {
    var u = readURL();
    try { var th = localStorage.getItem('hub-theme'); if (th) document.documentElement.setAttribute('data-theme', th); } catch (e) { /* ignore */ }
    $('#theme').onclick = function () {
      var cur = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      document.documentElement.setAttribute('data-theme', cur); try { localStorage.setItem('hub-theme', cur); } catch (e) { /* ignore */ }
      S.plotObjs.forEach(function (p) { p.redraw(); });
    };
    var wg = HubViewer.webglAvailable();
    S.webgl = wg.ok && u.webgl !== '0'; S.webglError = wg.ok ? (u.webgl === '0' ? 'disabled by ?webgl=0' : '') : wg.error;
    if (typeof THREE === 'undefined') { S.webgl = false; S.webglError = 'three.js did not load'; }
    postClient();
    $('#search').oninput = renderSidebar;
    Array.prototype.forEach.call(document.querySelectorAll('#tabs button'), function (b) { b.onclick = function () { setTab(b.dataset.tab); }; });
    $('#runsel').onchange = function () { openRun(this.value); };
    $('#cmpsel').onchange = function () { S.cmp = this.value || null; if (S.cmp) { S.tab = 'history'; renderTabs(); } syncURL(); renderTab(); };
    var rf = h('button', 'btn small', 'Refresh'); rf.type = 'button'; rf.id = 'refresh';
    rf.onclick = function () { S.cache = {}; loadProjects().then(function () { if (S.pid) openProject(S.pid, S.run === 'latest' ? null : S.run, S.tab); }); };
    $('.pctl').appendChild(rf);
    loadProjects().then(function () {
      var pid = u.pid && S.projects.some(function (p) { return p.id === u.pid; }) ? u.pid : (S.projects[0] && S.projects[0].id);
      S.tab = TABS.indexOf(u.tab) >= 0 ? u.tab : 'overview'; renderTabs();
      if (pid) { openProject(pid, u.run, S.tab).then(function () { if (u.cmp) { S.cmp = u.cmp; renderHeader(); renderTab(); } }); }
      else renderTab();
    }).catch(function (e) { content.textContent = ''; content.appendChild(errorBox(e)); });
  }
  function loadProjects() {
    return getJSON('/api/hub/projects').then(function (l) { S.projects = l; renderSidebar(); });
  }
  window.HubApp = { state: S };
  boot();
})();
