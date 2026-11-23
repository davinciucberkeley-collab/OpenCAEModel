/* OpenCAE drone dashboard. Vanilla JS, no build step, no network beyond this server. */
(function () {
'use strict';

// ---------------------------------------------------------------------------------------------- helpers
const $ = (s, r) => (r || document).querySelector(s);
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const k in (attrs || {})) {
    const v = attrs[k];
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(c));
  return e;
}
async function api(path, body) {
  const opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  let d = null;
  try { d = await r.json(); } catch (e) { /* not json */ }
  if (!r.ok) { const err = new Error((d && d.error) || ('HTTP ' + r.status)); err.status = r.status; throw err; }
  return d;
}
function fmt(v, digits) {
  if (v == null || typeof v !== 'number' || !isFinite(v)) return v == null ? '-' : String(v);
  const a = Math.abs(v);
  if (a === 0) return '0';
  if (digits == null) digits = a >= 1000 ? 0 : a >= 100 ? 1 : a >= 10 ? 2 : a >= 1 ? 2 : 3;
  const t = v.toFixed(digits).replace(/^-(0\.?0*)$/, '$1');
  return (t.includes('.') ? t.replace(/\.?0+$/, '') : t) || '0';
}
const ROLE_COLORS = { frame: '#9aa5b5', arm: '#4c9be8', motor: '#e5534b', prop: '#4cbb7f', battery: '#e2b33c',
  wing: '#7aa2f7', fin: '#b48ead', control_surface: '#d08770', landing: '#8fbcbb', payload: '#f78fb3',
  electronics: '#56b6c2', other: '#6b7686' };
// the pill follows the result status: 'OK' only for measured/datasheet evidence, estimates are 'Conditional'
const STATUS_TEXT = { ok: 'OK', watch: 'Watch', fail: 'Fail', conditional: 'Conditional – needs a test', error: 'Error', not_evaluated: 'Not evaluated' };
const PILL = { ok: 'green', watch: 'amber', conditional: 'amber', fail: 'red', error: 'red' };
const ORDER = ['mass', 'propulsion', 'structure', 'cfd', 'flight'];
const TITLES = { mass: 'Weight', propulsion: 'Power and lift', structure: 'Strength', cfd: 'Airflow', flight: 'Flight stability' };
const HEADLINE = {
  mass: ['mass_kg', 'total weight', v => v < 1 ? [fmt(v * 1000, 0), 'g'] : [fmt(v, 2), 'kg']],
  propulsion: ['tw_ratio', 'thrust-to-weight', v => [fmt(v, 1), ': 1']],
  structure: ['safety_factor', 'safety factor', v => [fmt(v, 2), 'x']],
  cfd: ['ld', 'lift-to-drag', v => [fmt(v, 1), '']],
  flight: ['max_tilt_deg', 'largest tilt in a gust', v => [fmt(v, 1), 'deg']],
};
// pipeline.BETTER arrives with /api/project: metric -> 'higher' | 'lower'
const betterOf = k => { const v = S.project && S.project.better && S.project.better[k]; return v === 'higher' ? true : v === 'lower' ? false : undefined; };

const S = { project: null, results: null, geo: null, variant: 'baseline', tab: 'results', sel: null, running: false,
  field: null, fieldSel: '', deform: 20, slices: false, streams: true, edits: {}, variantsData: {}, t: 0, playing: false,
  cmpMetric: '', lastDone: null };

// ---------------------------------------------------------------------------------------------- 3D scene
let renderer, scene, camera, controls, raycaster, modelGroup, partMeshes = [], fieldGroup = null, hovered = null, sceneRadius = 200;
const mouse = new THREE.Vector2();
const toThree = (x, y, z) => [x, z, -y];  // aircraft frame (x fwd, y left, z up) -> three (X fwd, Y up, Z right)

function initScene() {
  const el = $('#view');
  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  el.appendChild(renderer.domElement);
  scene = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(38, 1, 1, 20000);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x2a3038, 0.85));
  const d1 = new THREE.DirectionalLight(0xffffff, 0.8); d1.position.set(300, 500, 200); scene.add(d1);
  const d2 = new THREE.DirectionalLight(0x88aaff, 0.3); d2.position.set(-300, 100, -300); scene.add(d2);
  controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.dampingFactor = 0.12;
  raycaster = new THREE.Raycaster();
  modelGroup = new THREE.Group(); scene.add(modelGroup);
  const grid = new THREE.GridHelper(2000, 40, 0x4a5262, 0x2c323d); grid.material.transparent = true; grid.material.opacity = 0.35;
  grid.name = 'grid'; scene.add(grid);
  new ResizeObserver(resize).observe(el); resize();
  renderer.domElement.addEventListener('pointermove', onMove);
  renderer.domElement.addEventListener('pointerleave', () => { setHover(null); $('#tip').hidden = true; });
  let down = null;
  renderer.domElement.addEventListener('pointerdown', e => { down = [e.clientX, e.clientY]; });
  renderer.domElement.addEventListener('pointerup', e => {
    if (down && Math.hypot(e.clientX - down[0], e.clientY - down[1]) < 4) onClick(e);
    down = null;
  });
  (function loop() { requestAnimationFrame(loop); controls.update(); renderer.render(scene, camera); })();
}
function resize() {
  const el = $('#view'); const w = el.clientWidth, hgt = el.clientHeight;
  if (!w || !hgt || !renderer) return;
  renderer.setSize(w, hgt, false); camera.aspect = w / hgt; camera.updateProjectionMatrix();
}
function pick(e) {
  const r = renderer.domElement.getBoundingClientRect();
  mouse.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  raycaster.setFromCamera(mouse, camera);
  const hit = raycaster.intersectObjects(partMeshes.filter(m => m.visible), false)[0];
  return hit ? hit.object : null;
}
function setHover(m) {
  if (hovered === m) return;
  if (hovered && hovered.material.emissive) hovered.material.emissive.setHex(0x000000);
  hovered = m;
  if (m && m.material.emissive) m.material.emissive.setHex(0x335577);
  renderer.domElement.style.cursor = m ? 'pointer' : 'default';
}
function onMove(e) {
  const m = pick(e); setHover(m);
  const tip = $('#tip');
  if (m) {
    const r = $('#stage').getBoundingClientRect();
    tip.textContent = m.userData.name + '  (' + m.userData.role + ')'; tip.hidden = false;
    tip.style.left = (e.clientX - r.left + 14) + 'px'; tip.style.top = (e.clientY - r.top + 14) + 'px';
  } else tip.hidden = true;
}
function onClick(e) { const m = pick(e); S.sel = m ? m.userData.name : null; showPartCard(); }

// a part's normal look: props are see-through so the airframe shows behind them
function baseLook(m) {
  const prop = m.userData.role === 'prop';
  m.visible = true; m.material.color.setHex(m.userData.base);
  m.material.transparent = prop; m.material.opacity = prop ? 0.25 : 1; m.material.depthWrite = !prop;
  m.material.needsUpdate = true;
}
function ghost(m, opacity) {
  m.material.color.setHex(0x8b95a5); m.material.transparent = true; m.material.depthWrite = false;
  m.material.opacity = m.userData.role === 'prop' ? Math.min(opacity, 0.2) : opacity; m.material.needsUpdate = true;
}
function buildModel() {
  if (!modelGroup) return;
  while (modelGroup.children.length) { const c = modelGroup.children.pop(); c.geometry && c.geometry.dispose(); }
  partMeshes = [];
  const box = new THREE.Box3();
  if (!S.geo) { $('#legend').innerHTML = ''; return; }
  const roles = new Set();
  for (const p of S.geo.parts) {
    const n = p.points.length / 3, pos = new Float32Array(p.points.length);
    for (let i = 0; i < n; i++) { const t = toThree(p.points[3 * i], p.points[3 * i + 1], p.points[3 * i + 2]); pos.set(t, 3 * i); }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    g.setIndex(p.triangles); g.computeVertexNormals(); g.computeBoundingBox();
    box.union(g.boundingBox);
    const mat = new THREE.MeshStandardMaterial({ color: ROLE_COLORS[p.role] || ROLE_COLORS.other, roughness: 0.55, metalness: 0.15,
      side: THREE.DoubleSide, flatShading: false });
    const m = new THREE.Mesh(g, mat); m.userData = { name: p.name, role: p.role, base: mat.color.getHex() };
    baseLook(m); modelGroup.add(m); partMeshes.push(m); roles.add(p.role);
  }
  const size = box.getSize(new THREE.Vector3()), ctr = box.getCenter(new THREE.Vector3());
  sceneRadius = Math.max(size.length() / 2, 20);
  modelGroup.userData.center = ctr.clone();
  const grid = scene.getObjectByName('grid'); grid.scale.setScalar(sceneRadius / 200 * 0.5 + 0.0001);
  grid.position.set(ctr.x, box.min.y - sceneRadius * 0.02, ctr.z);
  controls.target.copy(ctr);
  camera.near = sceneRadius / 100; camera.far = sceneRadius * 60; camera.updateProjectionMatrix();
  camera.position.set(ctr.x - sceneRadius * 1.5, ctr.y + sceneRadius * 1.1, ctr.z + sceneRadius * 1.9);
  controls.update();
  const lg = $('#legend'); lg.innerHTML = '';
  [...roles].sort().forEach(r => lg.append(h('div', {}, h('i', { style: 'background:' + (ROLE_COLORS[r] || ROLE_COLORS.other) }), r.replace('_', ' '))));
  if (S.geo.decimated) lg.append(h('div', { class: 'dim' }, 'Shape simplified for display'));
}

function showPartCard() {
  const c = $('#partcard');
  partMeshes.forEach(m => { m.material.color.setHex(S.field ? m.material.color.getHex() : m.userData.base); });
  if (!S.sel) { c.hidden = true; return; }
  const p = (S.geo && S.geo.parts.find(q => q.name === S.sel));
  if (!p) { c.hidden = true; return; }
  const spec = (S.project && S.project.spec) || {};
  const A = (S.results && S.results.analyses) || {};
  const rows = [['Role', p.role.replace('_', ' ')]];
  const pm = A.mass && A.mass.extra && A.mass.extra.part_masses;
  let g = pm && (pm[p.name] != null ? pm[p.name] : null);
  if (g == null && pm && pm[p.role] != null) g = pm[p.role];
  if (g != null) { if (typeof g === 'object') g = g.value != null ? g.value : g.mass_g; rows.push(['Mass', typeof g === 'number' ? fmt(g, 1) + ' g' : String(g)]); }
  const mat = p.material || (spec.role_materials && spec.role_materials[p.role]);
  if (mat) { rows.push(['Material', mat]); const m = spec.materials && spec.materials[mat]; if (m && m.density_kg_m3 != null) rows.push(['Density', fmt(plain(m.density_kg_m3)) + ' kg/m3']); }
  const sp = A.structure && A.structure.extra && A.structure.extra.parts;
  let info = sp && (Array.isArray(sp) ? sp.find(x => x.name === p.name || x.part === p.name) : sp[p.name]);
  if (info && typeof info === 'object') {
    if (info.safety_factor != null) rows.push(['Safety factor', fmt(info.safety_factor, 2)]);
    if (info.max_stress_mpa != null) rows.push(['Peak stress', fmt(info.max_stress_mpa, 1) + ' MPa']);
    if (info.max_disp_mm != null) rows.push(['Bends by', fmt(info.max_disp_mm, 2) + ' mm']);
  }
  c.innerHTML = '';
  c.append(h('button', { class: 'x', onclick: () => { S.sel = null; showPartCard(); } }, '×'), h('h4', { text: p.name }),
    h('dl', {}, rows.map(r => [h('dt', { text: r[0] }), h('dd', { text: r[1] })])));
  c.hidden = false;
  const m = partMeshes.find(q => q.userData.name === S.sel);
  if (m && !S.field) m.material.color.setHex(0xffffff);
}
// numbers, or lists of numbers (centre of gravity) and matrices (inertia: the three diagonal terms)
function fmtVal(v) {
  if (!Array.isArray(v)) return fmt(v);
  if (v.length && Array.isArray(v[0])) return 'diag ' + v.map((r, i) => fmt(r[i], 3)).join(', ');
  return v.map(x => fmt(x, 1)).join(', ');
}
function nice(k) { return k.replace(/_(kg|mm|mpa|n|w|min|deg|s|a|m|ms|in|g)$/, '').replace(/_/g, ' '); }
function plain(v) { return v && typeof v === 'object' && 'value' in v ? v.value : v; }

// ---------------------------------------------------------------------------------------------- data loading
async function loadProject() {
  S.project = await api('/api/project');
  $('#pname').textContent = S.project.name;
  $('#ptype').textContent = (S.project.type || '').replace('_', ' ');
  document.title = S.project.name + ' - OpenCAE Drone';
  if (S.project.k09l) {
    document.body.classList.add('k09l'); const f = $('#k09l'); f.hidden = false; f.src = '/k09l/';
  }
}
async function loadResults() {
  S.results = null;
  if ((S.project.variants || []).includes(S.variant)) {
    try { S.results = await api('/api/results?variant=' + encodeURIComponent(S.variant)); } catch (e) { if (e.status !== 404) console.warn(e); }
  }
  const vp = $('#verdict');
  const v = S.results && S.results.verdict;
  vp.textContent = v || 'not run yet';
  vp.className = 'pill ' + (v === 'ready for physical tests' ? 'green' : v === 'conditional' ? 'amber' : v === 'not ready' ? 'red' : 'grey');
}
async function loadGeometry() {
  S.geo = null; $('#stagemsg').hidden = true;
  try { S.geo = await api('/api/geometry?variant=' + encodeURIComponent(S.variant)); }
  catch (e) { if (e.status !== 404) console.warn(e); }
  buildModel();
  const m = $('#stagemsg');
  if (!S.geo) { m.textContent = 'No 3D model yet. Check geometry.file in the parameters, then press Run.'; m.hidden = false; }
}
async function refreshAll(keepTab) {
  await loadProject();
  if (S.project.k09l) return;
  await Promise.all([loadResults(), loadGeometry()]);
  clearField(); S.sel = null; showPartCard();
  renderTab();
}

// ---------------------------------------------------------------------------------------------- progress
let pollTimer = null;
async function poll() {
  let p;
  try { p = await api('/api/progress'); } catch (e) { pollTimer = setTimeout(poll, 2000); return; }
  const was = S.running; S.running = !!p.running;
  $('#prog').hidden = !p.running && !p.error;
  $('#progbar i').style.width = Math.round((p.frac || 0) * 100) + '%';
  $('#progtxt').textContent = p.running ? (p.step || 'Working') + ' ' + Math.round((p.frac || 0) * 100) + '%' : (p.error ? p.error : '');
  $('#progtxt').style.color = p.error && !p.running ? 'var(--err)' : '';
  $('#btn-stop').hidden = !p.running; $('#btn-run').disabled = !!p.running;
  document.querySelectorAll('[data-needs-idle]').forEach(b => { b.disabled = !!p.running; });
  if (was && !p.running) {
    const done = p.finished; S.lastDone = done;
    if (p.result && p.result.variant && p.kind === 'iterate' && !p.error) S.variant = p.result.variant;
    if (!p.error) setTimeout(() => $('#prog').hidden = true, 3000);
    await refreshAll();
  }
  pollTimer = setTimeout(poll, p.running ? 1000 : 1500);
}
async function startRun(body) {
  try { await api('/api/run', body || {}); } catch (e) { toast(e.message); }
  clearTimeout(pollTimer); poll();
}
function toast(msg) { const t = $('#progtxt'); $('#prog').hidden = false; t.textContent = msg; t.style.color = 'var(--err)'; }

// ---------------------------------------------------------------------------------------------- tabs
function renderTab() {
  const b = $('#tabbody'); b.innerHTML = '';
  document.querySelectorAll('#tabs button').forEach(x => x.classList.toggle('on', x.dataset.tab === S.tab));
  stopPlay();
  if (S.tab !== 'contours' && S.field) clearField();
  const flightOn = S.tab === 'flight';
  if (!flightOn) resetPose();
  ({ results: tabResults, contours: tabContours, flight: tabFlight, params: tabParams, iter: tabIter })[S.tab](b);
}
function emptyRun(msg) {
  return h('div', { class: 'empty' }, h('div', {}, msg || 'Not run yet — press Run'),
    h('button', { class: 'btn primary', 'data-needs-idle': 1, onclick: () => startRun({}) }, 'Run all'));
}

function headline(a) {
  const hd = HEADLINE[a.id]; if (!hd) return null;
  const m = a.metrics && a.metrics[hd[0]]; if (!m || m.value == null) return null;
  const [num, unit] = hd[2](m.value);
  return { num, unit, label: hd[1], basis: m.basis };
}
function tabResults(b) {
  if (!S.results) return b.append(emptyRun());
  const A = S.results.analyses || {};
  b.append(h('h3', { class: 'sec', text: 'What the simulation found' + (S.variant !== 'baseline' ? ' — ' + S.variant : '') }));
  const ids = ORDER.filter(i => A[i]).concat(Object.keys(A).filter(i => !ORDER.includes(i)));
  for (const id of ids) {
    const a = A[id], hl = headline(a), st = a.status || 'not_evaluated';
    const card = h('div', { class: 'card ' + st },
      h('div', { class: 'hd' }, h('b', { text: TITLES[id] || a.title || id }),
        h('span', { class: 'pill ' + (PILL[st] || 'grey'), text: STATUS_TEXT[st] || st })));
    if (hl) card.append(h('div', { class: 'big ns-' + (((a.extra || {}).numeric_status) || (['ok', 'watch', 'fail'].includes(st) ? st : '')) }, hl.num, h('small', { text: hl.unit }), hl.basis ? h('span', { class: 'chip ' + hl.basis, text: 'basis: ' + hl.basis }) : null),
      h('div', { class: 'biglab', text: hl.label }));
    card.append(h('p', { text: a.summary || '' }));
    const minis = Object.entries(a.metrics || {}).filter(([k, m]) => m.value != null && (!HEADLINE[id] || k !== HEADLINE[id][0])).slice(0, 5);
    if (minis.length) card.append(h('div', { class: 'minis' }, minis.map(([k, m]) =>
      h('div', {}, h('span', { text: (m.label || nice(k)) + ' ' }), h('b', { text: fmtVal(m.value) + (m.unit ? ' ' + m.unit : '') })))));
    if ((a.missing || []).length) card.append(h('div', { class: 'miss', text: 'Missing: ' + a.missing.join(', ') }));
    if (st === 'error' && a.log) card.append(h('details', { class: 'miss' }, h('summary', { text: 'Details' }), h('pre', { text: a.log.slice(-600), style: 'white-space:pre-wrap;font-size:11px;margin:4px 0 0' })));
    b.append(card);
  }
  const checks = S.results.checks || [];
  if (checks.length) {
    b.append(h('h3', { class: 'sec', text: 'Requirement checks' }));
    const groups = [['pass', 'Pass'], ['conditional', 'Conditional – needs a physical test'], ['fail', 'Fail'], ['not_evaluated', null]];
    for (const [k, title] of groups) {
      const rows = checks.filter(c => c.status === k); if (!rows.length) continue;
      let t = title;
      if (!t) { const miss = [...new Set(rows.map(c => (c.metric || '').split('.')[0]).filter(Boolean))]; t = 'Not evaluated – missing: ' + (miss.join(', ') || 'inputs'); }
      b.append(h('div', { class: 'grp' }, h('div', { class: 'gh' }, h('span', { class: 'dot ' + k }), t + ' (' + rows.length + ')'),
        rows.map(c => h('div', { class: 'chk' }, h('span', { text: c.label || c.id }),
          h('span', { class: 'v', text: (c.value != null ? fmt(c.value) : '-') + ' ' + (c.op || '') + ' ' + fmt(c.target) }),
          c.why ? h('span', { class: 'why', text: c.why }) : null))));
    }
  }
}

// -- Contours
const CMAP = [[68, 1, 84], [59, 82, 139], [33, 145, 140], [94, 201, 98], [253, 231, 37]];
const SMAP = [[38, 70, 150], [110, 165, 220], [232, 232, 232], [245, 180, 90], [200, 70, 40]];   // air speed on slices
function cmap(t, M) {
  M = M || CMAP;
  t = Math.max(0, Math.min(1, t)) * (M.length - 1); const i = Math.min(M.length - 2, Math.floor(t)), f = t - i;
  return M[i].map((v, k) => (v + (M[i + 1][k] - v) * f) / 255);
}
function fieldList() {
  const out = [], A = (S.results && S.results.analyses) || {};
  for (const id of ORDER) for (const f of ((A[id] && A[id].fields) || [])) out.push(Object.assign({ analysis: id }, f));
  return out;
}
function clearField() {
  S.field = null; S.fieldSel = '';
  if (fieldGroup && scene) { scene.remove(fieldGroup); fieldGroup.traverse(o => { o.geometry && o.geometry.dispose(); }); fieldGroup = null; }
  partMeshes.forEach(baseLook);
  $('#colorbar').hidden = true;
}
function tabContours(b) {
  const fl = fieldList();
  if (!S.results) return b.append(emptyRun());
  if (!fl.length) return b.append(h('div', { class: 'empty' }, 'No contour fields yet. Strength and airflow analyses create them when they run.'));
  const sel = h('select', { onchange: e => { S.fieldSel = e.target.value; applyField(); } },
    h('option', { value: '', text: 'Plain colours by role' }),
    fl.map(f => h('option', { value: f.file + '|' + (f.key || ''), text: (TITLES[f.analysis] || f.analysis) + ': ' + (f.quantity || f.file) + (f.part ? ' (' + f.part + ')' : '') })));
  sel.value = S.fieldSel;
  b.append(h('h3', { class: 'sec', text: 'Colour the model by' }), h('div', { class: 'row' }, sel));
  const ex = h('div', { id: 'fieldctl' }); b.append(ex);
  if (S.fieldSel) buildFieldControls(ex);
}
function buildFieldControls(ex) {
  ex.innerHTML = ''; const f = S.field; if (!f) return;
  if (f.disp) ex.append(h('div', { class: 'row' }, h('label', { text: 'Bending' }),
    h('input', { type: 'range', min: 0, max: 100, value: S.deform, oninput: e => { S.deform = +e.target.value; updateDeform(); } }),
    h('span', { class: 'dim', id: 'dfl', text: S.deform + 'x' })));
  if (f.slice || f.stream) ex.append(h('div', { class: 'toggles' },
    f.slice ? h('label', {}, h('input', { type: 'checkbox', checked: S.slices, onchange: e => { S.slices = e.target.checked; updateOverlays(); renderTabKeep(); } }), 'Air slices') : null,
    f.stream ? h('label', {}, h('input', { type: 'checkbox', checked: S.streams, onchange: e => { S.streams = e.target.checked; updateOverlays(); } }), 'Airflow lines') : null));
  if (f.slice && S.slices) ex.append(h('div', { class: 'speedbar' }, h('span', { class: 'dim', text: 'Slices: air speed ' + fmt(f.sliceRange[0], 3) + ' to ' + fmt(f.sliceRange[1], 3) + ' m/s' }),
    h('i', { style: 'background:linear-gradient(90deg,' + [0, .25, .5, .75, 1].map(t => 'rgb(' + cmap(t, SMAP).map(v => Math.round(v * 255)) + ')').join(',') + ')' })));
  ex.append(h('p', { class: 'dim', text: f.note || '' }));
}
function arr(o) { return o ? o.data : null; }
async function applyField() {
  const [file, key] = S.fieldSel.split('|');
  const sel = S.fieldSel; clearField(); S.fieldSel = sel;
  if (!file) { showPartCard(); renderTabKeep(); return; }
  const meta = fieldList().find(f => f.file === file);
  let d;
  try { d = await api('/api/field?variant=' + encodeURIComponent(S.variant) + '&file=' + encodeURIComponent(file)); }
  catch (e) { toast(e.message); return; }
  const vals = arr(d[key] || d.values); if (!vals) return;
  let vmin = Infinity, vmax = -Infinity; for (const v of vals) if (v != null && isFinite(v)) { if (v < vmin) vmin = v; if (v > vmax) vmax = v; }
  if (!isFinite(vmin)) { vmin = 0; vmax = 1; } if (vmax === vmin) vmax = vmin + 1e-9;
  const f = { meta, vmin, vmax, vals, d, note: '' };
  fieldGroup = new THREE.Group(); scene.add(fieldGroup);
  if (d.points && d.triangles) {
    const P = d.points.data, T = d.triangles.data, n = P.length / 3;
    const pos = new Float32Array(3 * n), col = new Float32Array(3 * n);
    for (let i = 0; i < n; i++) { pos.set(toThree(P[3 * i], P[3 * i + 1], P[3 * i + 2]), 3 * i); const c = cmap((vals[i] - vmin) / (vmax - vmin)); col.set(c, 3 * i); }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3)); g.setAttribute('color', new THREE.BufferAttribute(col, 3)); g.setIndex(T); g.computeVertexNormals();
    const m = new THREE.Mesh(g, new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide }));
    fieldGroup.add(m); f.mesh = m; f.base = pos.slice();
    if (d.displacement) {
      f.disp = d.displacement.data; f.dispShape = d.displacement.shape;
      f.note = 'Shape is exaggerated so you can see how it bends.';
    }
    partMeshes.forEach(pm => {
      const own = meta && meta.part && pm.userData.name === meta.part;
      if (own) pm.visible = false; else if (meta && meta.part) ghost(pm, 0.4);
      else pm.visible = false;
    });
  } else {
    f.note = 'This field has no surface of its own, so only the airflow overlays are shown.';
    partMeshes.forEach(pm => ghost(pm, 0.35));
  }
  if (d.slice_points && d.slice_values && d.slice_triangles) {   // coloured surfaces, one vertex colour per air speed
    const P = d.slice_points.data, V = d.slice_values.data, n = P.length / 3;
    const pos = new Float32Array(3 * n), col = new Float32Array(3 * n);
    let a = Infinity, z = -Infinity; for (const v of V) { if (v < a) a = v; if (v > z) z = v; } if (z === a) z = a + 1e-9;
    for (let i = 0; i < n; i++) { pos.set(toThree(P[3 * i], P[3 * i + 1], P[3 * i + 2]), 3 * i); col.set(cmap((V[i] - a) / (z - a), SMAP), 3 * i); }
    const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.BufferAttribute(pos, 3)); g.setAttribute('color', new THREE.BufferAttribute(col, 3));
    g.setIndex(d.slice_triangles.data);
    f.slice = new THREE.Mesh(g, new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide, transparent: true, opacity: 0.5, depthWrite: false }));
    f.slice.renderOrder = 2; f.sliceRange = [a, z];
    fieldGroup.add(f.slice);
    if (!f.mesh) { f.vmin = a; f.vmax = z; }
  }
  if (d.stream_points && d.stream_offsets) {
    const P = d.stream_points.data, O = d.stream_offsets.data, Sp = d.stream_speed ? d.stream_speed.data : null;
    const seg = [], cols = []; let a = Infinity, z = -Infinity;
    if (Sp) for (const v of Sp) { if (v < a) a = v; if (v > z) z = v; } if (z <= a) z = a + 1e-9;
    for (let s = 0; s < O.length; s++) {
      const i0 = O[s], i1 = s + 1 < O.length ? O[s + 1] : P.length / 3;
      for (let i = i0; i < i1 - 1; i++) {
        seg.push(...toThree(P[3 * i], P[3 * i + 1], P[3 * i + 2]), ...toThree(P[3 * i + 3], P[3 * i + 4], P[3 * i + 5]));
        const c = Sp ? cmap((Sp[i] - a) / (z - a)) : [0.7, 0.8, 1]; cols.push(...c, ...c);
      }
    }
    const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.Float32BufferAttribute(seg, 3)); g.setAttribute('color', new THREE.Float32BufferAttribute(cols, 3));
    f.streamRange = [a, z];
    f.stream = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.8 }));
    fieldGroup.add(f.stream);
    if (!f.mesh && !f.slice) { f.vmin = a; f.vmax = z; }
  }
  S.field = f; updateOverlays(); updateDeform();
  drawColorbar(f, meta); renderTabKeep();
}
function renderTabKeep() { if (S.tab === 'contours') { const c = $('#fieldctl'); if (c) buildFieldControls(c); } }
function updateOverlays() { const f = S.field; if (!f) return; if (f.slice) f.slice.visible = S.slices; if (f.stream) f.stream.visible = S.streams; }
function updateDeform() {
  const f = S.field; if (!f || !f.mesh || !f.disp) return;
  const pos = f.mesh.geometry.attributes.position, n = pos.count, sh = f.dispShape; const k = sh.length > 1 ? sh[1] : 3;
  let mx = 0; for (let i = 0; i < f.disp.length; i++) mx = Math.max(mx, Math.abs(f.disp[i]));
  const scale = mx > 0 ? (S.deform / 100) * (sceneRadius * 0.15 / mx) : 0;
  for (let i = 0; i < n; i++) {
    const d = k === 3 ? toThree(f.disp[3 * i], f.disp[3 * i + 1], f.disp[3 * i + 2]) : [0, f.disp[i], 0];
    pos.setXYZ(i, f.base[3 * i] + d[0] * scale, f.base[3 * i + 1] + d[1] * scale, f.base[3 * i + 2] + d[2] * scale);
  }
  pos.needsUpdate = true; f.mesh.geometry.computeBoundingSphere();
  const l = $('#dfl'); if (l) l.textContent = S.deform + '%';
}
function drawColorbar(f, meta) {
  const c = $('#colorbar'); const cv = $('canvas', c), ctx = cv.getContext('2d');
  for (let y = 0; y < cv.height; y++) { const col = cmap(1 - y / (cv.height - 1)); ctx.fillStyle = `rgb(${col.map(v => Math.round(v * 255))})`; ctx.fillRect(0, y, cv.width, 1); }
  const u = (meta && meta.unit) || '';
  $('#cb-max').textContent = fmt(f.vmax, 3); $('#cb-min').textContent = fmt(f.vmin, 3);
  $('#cb-name').textContent = ((meta && meta.quantity) || 'value') + (u ? ' (' + u + ')' : '');
  c.hidden = false;
}

// -- Flight
function flightData() {   // att = roll, pitch, yaw in degrees, pos in metres, both in the aircraft frame (x fwd, y left, z up)
  const fl = S.results && S.results.analyses && S.results.analyses.flight, ex = fl && fl.extra;
  if (!ex || !ex.t || !ex.t.length || !Array.isArray(ex.att)) return null;
  const rad = Math.PI / 180, euler = ex.att.map(a => [a[0] || 0, a[1] || 0, a[2] || 0]);
  const tilt = euler.map(a => Math.acos(Math.max(-1, Math.min(1, Math.cos(a[0] * rad) * Math.cos(a[1] * rad)))) / rad);
  return { t: ex.t, pos: ex.pos || [], euler, tilt };
}
function tabFlight(b) {
  const fd = flightData();
  if (!S.results) return b.append(emptyRun());
  if (!fd) return b.append(h('div', { class: 'empty' }, 'No flight replay yet. The flight analysis records how the drone holds still in a gust when it runs.'));
  const a = S.results.analyses.flight;
  b.append(h('h3', { class: 'sec', text: 'How the drone holds still' }), h('p', { class: 'dim', text: a.summary || '' }));
  const cv = h('canvas', { id: 'chart' }); b.append(cv);
  const play = h('button', { class: 'btn', onclick: () => S.playing ? stopPlay() : startPlay() }, S.playing ? 'Pause' : 'Play');
  const lab = h('span', { class: 'dim', id: 'tlab' });
  const sl = h('input', { type: 'range', min: 0, max: fd.t.length - 1, value: 0, id: 'tsl', oninput: e => { stopPlay(); setT(+e.target.value); } });
  b.append(h('div', { class: 'row', style: 'margin-top:10px' }, play, sl, lab));
  b.append(h('p', { class: 'dim', text: 'Hover over the chart to scrub, or press Play to watch the model tilt and move.' }));
  cv.addEventListener('pointermove', e => { const r = cv.getBoundingClientRect(); stopPlay(); setT(Math.round((e.clientX - r.left) / r.width * (fd.t.length - 1))); });
  S.t = Math.min(S.t, fd.t.length - 1); setT(S.t);
}
let raf = 0, lastTs = 0;
function startPlay() {
  const fd = flightData(); if (!fd) return; S.playing = true;
  const btn = $('#tabbody .btn'); if (btn) btn.textContent = 'Pause';
  if (S.t >= fd.t.length - 1) S.t = 0; lastTs = 0;
  (function step(ts) {
    if (!S.playing) return;
    if (!lastTs) lastTs = ts;
    const dur = fd.t[fd.t.length - 1] - fd.t[0] || 1, speed = Math.max(dur / 10, 0.5); // whole run in about 10 s at least
    const target = (fd.t[S.t] - fd.t[0]) + ((ts - lastTs) / 1000) * speed; lastTs = ts;
    let i = S.t; while (i < fd.t.length - 1 && fd.t[i] - fd.t[0] < target) i++;
    setT(i); if (i >= fd.t.length - 1) { stopPlay(); return; }
    raf = requestAnimationFrame(step);
  })(performance.now());
}
function stopPlay() { S.playing = false; cancelAnimationFrame(raf); const btn = $('#tabbody .btn'); if (btn && S.tab === 'flight') btn.textContent = 'Play'; }
function setT(i) {
  const fd = flightData(); if (!fd) return; S.t = Math.max(0, Math.min(fd.t.length - 1, i));
  const sl = $('#tsl'); if (sl) sl.value = S.t;
  const lab = $('#tlab'); if (lab) lab.textContent = fmt(fd.t[S.t], 2) + ' s, tilt ' + fmt(fd.tilt[S.t], 1) + ' deg';
  drawChart(fd); applyPose(fd);
}
function drawChart(fd) {
  const cv = $('#chart'); if (!cv) return;
  const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight; cv.width = W * dpr; cv.height = H * dpr;
  const g = cv.getContext('2d'); g.scale(dpr, dpr); g.clearRect(0, 0, W, H);
  const L = 38, R = 8, T = 12, B = 22, w = W - L - R, hh = H - T - B;
  const tmax = fd.t[fd.t.length - 1], tmin = fd.t[0], ymax = Math.max(4, Math.ceil(Math.max(...fd.tilt) / 4) * 4);
  const X = t => L + (t - tmin) / ((tmax - tmin) || 1) * w, Y = v => T + hh - v / ymax * hh;
  g.strokeStyle = '#2d333d'; g.fillStyle = '#8b95a5'; g.font = '10.5px system-ui'; g.lineWidth = 1;
  for (let k = 0; k <= 4; k++) { const v = ymax * k / 4, y = Y(v); g.beginPath(); g.moveTo(L, y); g.lineTo(L + w, y); g.stroke(); g.fillText(fmt(v, ymax < 8 ? 1 : 0) + '°', 4, y + 3); }
  for (let k = 0; k <= 4; k++) { const t = tmin + (tmax - tmin) * k / 4; g.fillText(fmt(t, 1) + ' s', X(t) - 10, H - 6); }
  g.strokeStyle = '#4c9be8'; g.lineWidth = 2; g.beginPath();
  fd.t.forEach((t, i) => i ? g.lineTo(X(t), Y(fd.tilt[i])) : g.moveTo(X(t), Y(fd.tilt[i]))); g.stroke();
  const x = X(fd.t[S.t]); g.strokeStyle = '#e8a33d'; g.lineWidth = 1.5; g.beginPath(); g.moveTo(x, T); g.lineTo(x, T + hh); g.stroke();
  g.fillStyle = '#e8a33d'; g.beginPath(); g.arc(x, Y(fd.tilt[S.t]), 4, 0, 7); g.fill();
}
function applyPose(fd) {
  if (!modelGroup) return;
  const p = fd.pos[S.t], a = fd.euler[S.t], p0 = fd.pos[0] || [0, 0, 0], k = Math.PI / 180;
  if (p) { const q = toThree((p[0] - p0[0]) * 1000, (p[1] - p0[1]) * 1000, (p[2] - p0[2]) * 1000); modelGroup.position.set(q[0], q[1], q[2]); }
  // roll about forward (three X), yaw about up (Y); pitch is about aircraft y (left) = three -Z
  if (a) modelGroup.rotation.set(a[0] * k, a[2] * k, -a[1] * k, 'YZX');
}
function resetPose() { if (!modelGroup) return; modelGroup.position.set(0, 0, 0); modelGroup.rotation.set(0, 0, 0); modelGroup.quaternion.identity(); }

// -- Parameters
function leaves(obj, prefix, out, skip) {
  for (const k in obj) {
    const v = obj[k], key = prefix ? prefix + '.' + k : k;
    if (typeof v === 'number') out.push({ key, value: v, unit: '', status: 'estimate', source: '' });
    else if (v && typeof v === 'object' && !Array.isArray(v) && 'value' in v && typeof v.value === 'number') out.push({ key, value: v.value, unit: v.unit || '', status: v.status || 'estimate', source: v.source || '' });
    else if (typeof v === 'boolean' && k === 'enabled') out.push({ key, value: v, bool: true, unit: '', status: '', source: '' });
    else if (v && typeof v === 'object' && !Array.isArray(v) && !(skip && skip.includes(k))) leaves(v, key, out, skip);
  }
  return out;
}
const SECTION_NAMES = { mass: 'Weight', propulsion: 'Motors, props and battery', aero: 'Aerodynamics', mission: 'Mission', environment: 'Air and gravity', analyses: 'What to analyse', materials: 'Materials', requirements: 'Requirements' };
function allParams() {
  const spec = S.project.spec, out = [];
  for (const sec of Object.keys(spec)) {
    if (['name', 'type', 'engine', 'format', 'geometry', 'roles', 'role_materials', 'requirements'].includes(sec)) continue;
    if (spec[sec] && typeof spec[sec] === 'object') leaves(spec[sec], sec, [], null).forEach(l => { l.section = sec; out.push(l); });
  }
  return out;
}
function niceKey(key) { const p = key.split('.'); return p.slice(1).join(' / ').replace(/_/g, ' '); }
function tabParams(b) {
  const ps = allParams(); S.edits = S.edits || {};
  b.append(h('div', { class: 'dim', text: 'Change a value and press Save. Orange boxes are unsaved edits.' }));
  const secs = [...new Set(ps.map(p => p.section))];
  for (const sec of secs) {
    b.append(h('h3', { class: 'sec', text: SECTION_NAMES[sec] || sec }));
    const grp = h('div', { class: 'grp' });
    for (const p of ps.filter(q => q.section === sec)) {
      const inp = p.bool
        ? h('input', { type: 'checkbox', checked: p.value, 'data-key': p.key, onchange: e => { S.edits[p.key] = e.target.checked; } })
        : h('input', { type: 'number', step: 'any', value: S.edits[p.key] != null ? S.edits[p.key] : p.value, 'data-key': p.key,
          class: S.edits[p.key] != null ? 'changed' : '',
          oninput: e => { const v = e.target.value; if (v === '' || +v === p.value) { delete S.edits[p.key]; e.target.classList.remove('changed'); } else { S.edits[p.key] = +v; e.target.classList.add('changed'); } e.target.classList.remove('bad'); } });
      grp.append(h('div', { class: 'prow', title: p.key + (p.source ? ' - ' + p.source : '') }, h('span', { class: 'lab' }, niceKey(p.key), p.status ? h('span', { class: 'chip ' + p.status, text: p.status }) : null), inp, h('span', { class: 'u', text: p.unit })));
    }
    b.append(grp);
  }
  const err = h('div', { class: 'err', id: 'perr' });
  b.append(err, h('div', { class: 'sticky' },
    h('button', { class: 'btn', 'data-needs-idle': 1, onclick: () => saveParams(false) }, 'Save'),
    h('button', { class: 'btn primary', 'data-needs-idle': 1, onclick: () => saveParams(true) }, 'Save & run')));
}
async function saveParams(run) {
  const err = $('#perr'); err.textContent = '';
  document.querySelectorAll('.prow input.bad').forEach(i => i.classList.remove('bad'));
  const ov = Object.assign({}, S.edits);
  if (!Object.keys(ov).length) { if (run) startRun({}); else err.textContent = 'Nothing changed.'; return; }
  try {
    const r = await api('/api/params', { overrides: ov });
    S.edits = {}; if (r.spec) S.project.spec = r.spec; else await loadProject();
    renderTab();
    if (run) startRun({});
  } catch (e) {
    err.textContent = e.message;
    for (const k in ov) if (e.message.includes(k.split('.').pop())) { const i = document.querySelector(`input[data-key="${k}"]`); if (i) i.classList.add('bad'); }
  }
}

// -- Iterations
async function loadVariantData() {
  const names = S.project.variants || [];
  for (const n of names) if (!S.variantsData[n] || S.variantsData[n]._stamp !== S.lastDone) {
    try { const r = await api('/api/results?variant=' + encodeURIComponent(n)); r._stamp = S.lastDone; S.variantsData[n] = r; } catch (e) { /* skip */ }
  }
}
function metricMap(r) {
  const m = {};
  for (const id in (r.analyses || {})) for (const k in (r.analyses[id].metrics || {})) {
    const x = r.analyses[id].metrics[k]; if (x && typeof x.value === 'number' && isFinite(x.value)) m[k] = { v: x.value, unit: x.unit || '', label: x.label || nice(k) };
  }
  return m;
}
async function tabIter(b) {
  const names = S.project.variants || [];
  b.append(h('h3', { class: 'sec', text: 'Variants' }));
  if (!names.length) b.append(emptyRun());
  const vl = h('div', { class: 'vlist' });
  for (const n of names) vl.append(h('div', { class: 'vrow' + (n === S.variant ? ' on' : ''), onclick: async () => { S.variant = n; await Promise.all([loadResults(), loadGeometry()]); clearField(); renderTab(); } },
    h('b', { text: n === 'baseline' ? 'baseline (your drone as it is)' : n }), h('span', { class: 'dim', text: n === S.variant ? 'showing' : 'show' })));
  b.append(vl);
  if (!names.length) return;
  // try a change
  const ps = allParams().filter(p => !p.bool);
  b.append(h('h3', { class: 'sec', text: 'Try a change' }));
  const psel = h('select', { id: 'it-param' }, ps.map(p => h('option', { value: p.key, text: (SECTION_NAMES[p.section] || p.section) + ': ' + niceKey(p.key) + (p.unit ? ' (' + p.unit + ')' : '') })));
  const cur = h('span', { class: 'dim', id: 'it-cur' });
  const nv = h('input', { type: 'number', step: 'any', id: 'it-val', style: 'width:110px' });
  const nm = h('input', { type: 'text', id: 'it-name', placeholder: 'e.g. bigger-battery', style: 'flex:1' });
  const upd = () => { const p = ps.find(q => q.key === psel.value); cur.textContent = p ? 'now ' + fmt(p.value) : ''; };
  psel.addEventListener('change', upd); upd();
  const ierr = h('div', { class: 'err' });
  b.append(h('div', { class: 'row' }, h('label', { text: 'Change' }), psel), h('div', { class: 'row' }, h('label', { text: 'To' }), nv, cur),
    h('div', { class: 'row' }, h('label', { text: 'Name it' }), nm),
    h('button', { class: 'btn primary', 'data-needs-idle': 1, onclick: async () => {
      ierr.textContent = '';
      if (nv.value === '') { ierr.textContent = 'Type the new value.'; return; }
      try { await api('/api/iterate', { overrides: { [psel.value]: +nv.value }, name: nm.value.trim() }); clearTimeout(pollTimer); poll(); }
      catch (e) { ierr.textContent = e.message; }
    } }, 'Run this variant'), ierr);
  // compare
  await loadVariantData();
  if (S.tab !== 'iter') return;
  const others = names.filter(n => n !== 'baseline' && S.variantsData[n]);
  if (!others.length || !S.variantsData.baseline) return;
  const base = metricMap(S.variantsData.baseline);
  b.append(h('h3', { class: 'sec', text: 'Compared with baseline' }));
  const keys = Object.keys(base).filter(k => others.some(n => metricMap(S.variantsData[n])[k]));
  const tbl = h('table', { class: 'cmp' }, h('tr', {}, h('th', { text: 'Measure' }), h('th', { text: 'baseline' }), others.map(n => h('th', { text: n }))));
  for (const k of keys) {
    tbl.append(h('tr', { style: 'cursor:pointer', onclick: () => { S.cmpMetric = k; renderTab(); } }, h('td', { text: base[k].label + (base[k].unit ? ' (' + base[k].unit + ')' : '') }), h('td', { text: fmt(base[k].v) }),
      others.map(n => {
        const x = metricMap(S.variantsData[n])[k]; if (!x) return h('td', { text: '-' });
        const d = x.v - base[k].v, rel = Math.abs(d) < 1e-9 * (Math.abs(base[k].v) + 1);
        const better = betterOf(k), good = rel || better == null ? null : (d > 0) === better;
        return h('td', { class: rel ? '' : better == null ? '' : (d > 0 ? 'up-' : 'dn-') + (good ? 'good' : 'bad') }, fmt(x.v), rel ? '' : (d > 0 ? ' ▲' : ' ▼'));
      })));
  }
  b.append(tbl);
  const mk = S.cmpMetric && keys.includes(S.cmpMetric) ? S.cmpMetric : keys[0];
  if (mk) {
    const all = ['baseline', ...others].map(n => [n, (metricMap(S.variantsData[n])[mk] || {}).v]).filter(x => x[1] != null);
    const mx = Math.max(...all.map(x => Math.abs(x[1])), 1e-9);
    b.append(h('h3', { class: 'sec', text: base[mk].label }),
      h('div', { class: 'bars' }, all.map(([n, v]) => h('div', { class: 'bar' }, h('span', { text: n, style: 'overflow:hidden;text-overflow:ellipsis' }), h('div', {}, h('i', { style: 'width:' + Math.max(2, Math.abs(v) / mx * 100) + '%;' + (n === 'baseline' ? 'background:var(--grey)' : '') })), h('span', { class: 't', text: fmt(v) })))));
  }
}

// ---------------------------------------------------------------------------------------------- init
async function init() {
  document.querySelectorAll('#tabs button').forEach(bn => bn.addEventListener('click', () => { S.tab = bn.dataset.tab; renderTab(); }));
  $('#btn-run').addEventListener('click', () => startRun({}));
  $('#btn-stop').addEventListener('click', async () => { try { await api('/api/stop', {}); } catch (e) { /* ignore */ } });
  try { initScene(); } catch (e) { $('#stagemsg').textContent = '3D view not available: ' + e.message; $('#stagemsg').hidden = false; }
  try { await refreshAll(); } catch (e) { $('#tabbody').textContent = e.message; }
  poll();
}
init();
})();
