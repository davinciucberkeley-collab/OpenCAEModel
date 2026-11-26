/* Results Hub: motion simulator.   window.HubMotion.mount(el, data, {webgl}) -> {destroy, play, pause, seek, getState}
 *
 * data = the /api/hub/motion payload (opencae/hub/motion.py): bodies with float32/int32 base64 meshes (mm), a pose
 * track per body (x y z mm + quaternion w x y z per frame), joints, rotors, channels. Nothing is computed here: the
 * page only plays back stored results. WebGL (three.js, from /vendor) when opts.webgl is true and a context can be
 * made; otherwise 2-D canvas views (front, side, top: orthographic, flat shaded, painter sorted) that need no GPU.
 */
(function (global) {
  'use strict';

  var VENDOR = '/vendor/';
  var DISCLAIMER = 'Animation of computed results; not a flight prediction';
  var SERIES = ['#6db3f2', '#f3864a', '#6bc58b', '#d58cf0', '#e7c444', '#4fd1c5', '#f0807a', '#a7b4bf'];
  var MODE_COLOR = { prescribed: '#4aa3ff', 'torque-driven': '#ff9d3c', other: '#9aa5b4' };
  var SPIN_REV_PER_S = 1.25;          // drawn spin at the largest stored speed: revolutions per second of data time

  /* ------------------------------------------------------------------------------------------ style */
  var CSS = [
    '.hm{--hm-bg:var(--panel,#151d24);--hm-bg2:var(--panel2,#1a242c);--hm-ink:var(--ink,#e2e9ef);--hm-ink2:var(--ink2,#a7b4bf);',
    '--hm-ink3:var(--ink3,#7b8893);--hm-rule:var(--rule,#283540);--hm-acc:var(--accent,#f3864a);--hm-acc2:var(--accent2,#79afe0);',
    '--hm-stage1:var(--stage,#18222a);--hm-stage2:var(--stage2,#0f171d);',
    'color:var(--hm-ink);font:13.5px/1.45 var(--f,"IBM Plex Sans","Segoe UI",system-ui,sans-serif);display:flex;flex-direction:column;gap:10px;min-width:0;outline:none}',
    '.hm *{box-sizing:border-box}',
    '.hm-top{display:flex;flex-wrap:wrap;gap:4px 14px;align-items:baseline}',
    '.hm-title{font:600 15px/1.3 var(--fd,"Space Grotesk","Segoe UI",system-ui,sans-serif)}',
    '.hm-basis{font-size:12px;color:var(--hm-ink2)}',
    '.hm-disc{width:100%;font-size:12.5px;color:#e7b544;font-weight:500}',
    '.hm-stage{position:relative;border:1px solid var(--hm-rule);border-radius:8px;overflow:hidden;min-height:300px;',
    'background:radial-gradient(120% 90% at 30% 20%,var(--hm-stage1),var(--hm-stage2))}',
    '.hm-gl{position:relative;height:clamp(300px,50vh,540px)}',
    '.hm-gl canvas{display:block;width:100%;height:100%}',
    '.hm-views{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1px;background:var(--hm-rule)}',
    '.hm-view{position:relative;height:clamp(240px,36vh,380px);background:radial-gradient(120% 90% at 30% 20%,var(--hm-stage1),var(--hm-stage2));touch-action:none;cursor:grab}',
    '.hm-view canvas{display:block;width:100%;height:100%}',
    '.hm-vlabel{position:absolute;left:8px;top:6px;font-size:11.5px;color:var(--hm-ink2);pointer-events:none;letter-spacing:.02em}',
    '.hm-overlay{position:absolute;inset:0;pointer-events:none;overflow:hidden}',
    '.hm-tag{position:absolute;transform:translate(8px,-50%);font-size:11px;white-space:nowrap;padding:1px 6px;border-radius:9px;background:rgba(10,16,20,.72);color:#e2e9ef;border:1px solid rgba(255,255,255,.18)}',
    '.hm-rnote{font-size:11.5px;color:var(--hm-ink3);margin-top:-6px;text-align:right}',
    '.hm-bar{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;background:var(--hm-bg2);border:1px solid var(--hm-rule);border-radius:8px;padding:8px 10px}',
    '.hm-btn{background:var(--hm-bg);color:var(--hm-ink);border:1px solid var(--hm-rule);border-radius:6px;padding:4px 11px;cursor:pointer;font:inherit;line-height:1.3}',
    '.hm-btn:hover{border-color:var(--hm-acc2)}.hm-btn:focus-visible,.hm select:focus-visible,.hm input:focus-visible{outline:2px solid var(--hm-acc);outline-offset:2px}',
    '.hm-play{min-width:78px;font-weight:600;background:var(--hm-acc);border-color:var(--hm-acc);color:#13181d}',
    '.hm select{background:var(--hm-bg);color:var(--hm-ink);border:1px solid var(--hm-rule);border-radius:6px;padding:3px 6px;font:inherit;max-width:100%}',
    '.hm-lbl{display:inline-flex;align-items:center;gap:5px;color:var(--hm-ink2);font-size:12.5px}',
    '.hm-scrub{flex:1 1 220px;min-width:140px;accent-color:var(--hm-acc)}',
    '.hm-time{font:12.5px var(--fm,"IBM Plex Mono",Consolas,monospace);font-variant-numeric:tabular-nums;color:var(--hm-ink);white-space:nowrap}',
    '.hm-live{display:flex;flex-wrap:wrap;gap:6px}',
    '.hm-chip{display:inline-flex;gap:6px;align-items:center;border:1px solid var(--hm-rule);background:var(--hm-bg2);border-radius:14px;padding:2px 10px;font-size:12px}',
    '.hm-chip b{font-weight:600}.hm-chip .v{font-family:var(--fm,"IBM Plex Mono",Consolas,monospace);color:var(--hm-ink2);font-variant-numeric:tabular-nums}',
    '.hm-mode{border-radius:9px;padding:0 7px;font-size:11px;font-weight:600;color:#10161b}',
    '.hm-plotbox{border:1px solid var(--hm-rule);border-radius:8px;background:var(--hm-bg2);padding:8px 10px 6px}',
    '.hm-plothead{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center;margin-bottom:4px}',
    '.hm-plot{position:relative;height:210px}.hm-plot canvas{display:block;width:100%;height:100%;cursor:crosshair;touch-action:none}',
    '.hm-legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;margin-top:4px}',
    '.hm-legend span{display:inline-flex;gap:5px;align-items:center}.hm-legend i{width:14px;height:3px;border-radius:2px;display:inline-block}',
    '.hm-legend .v{font-family:var(--fm,"IBM Plex Mono",Consolas,monospace);color:var(--hm-ink2)}',
    '.hm-notes{border:1px solid var(--hm-rule);border-radius:8px;background:var(--hm-bg2);padding:6px 12px;font-size:12.5px;color:var(--hm-ink2)}',
    '.hm-notes summary{cursor:pointer;font-weight:600;color:var(--hm-ink)}.hm-notes ul{margin:6px 0 4px;padding-left:18px}.hm-notes li{margin:2px 0}',
    '.hm-empty{border:1px dashed var(--hm-rule);border-radius:8px;padding:22px 18px;background:var(--hm-bg2)}',
    '.hm-empty b{display:block;font:600 15px var(--fd,system-ui,sans-serif);margin-bottom:6px}',
    '@media (max-width:640px){.hm-bar{gap:6px 8px}.hm-view{height:clamp(220px,34vh,320px)}}'
  ].join('\n');

  function injectCss() {
    if (document.getElementById('hm-style')) return;
    var s = document.createElement('style');
    s.id = 'hm-style';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  /* ------------------------------------------------------------------------------------------ helpers */
  function h(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function b64buf(s) {
    var bin = atob(s), u = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
    return u.buffer;
  }
  function fmt(v, nd) {
    if (v == null || !isFinite(v)) return '-';
    var a = Math.abs(v);
    if (nd == null) nd = a >= 1000 ? 0 : a >= 100 ? 1 : a >= 10 ? 2 : a >= 1 ? 3 : 4;
    return v.toFixed(nd);
  }
  function hex2rgb(c) {
    var m = /^#?([0-9a-f]{6})$/i.exec(c || '');
    var n = m ? parseInt(m[1], 16) : 0x9aa5b4;
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function cssVar(el, name, fallback) {
    try {
      var v = getComputedStyle(el).getPropertyValue(name).trim();
      return v || fallback;
    } catch (e) { return fallback; }
  }
  function niceStep(span, target) {
    var raw = span / Math.max(target, 1), p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
    return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * p;
  }
  function lengthLabel(mm) {
    if (mm >= 1000) return fmt(mm / 1000, mm % 1000 === 0 ? 0 : 1).replace(/\.0$/, '') + ' m';
    return fmt(mm, mm < 1 ? 2 : 0).replace(/\.0+$/, '') + ' mm';
  }
  function bsearch(arr, x) {      // largest i with arr[i] <= x (clamped to 0..n-2)
    var lo = 0, hi = arr.length - 1;
    while (lo < hi - 1) { var mid = (lo + hi) >> 1; if (arr[mid] <= x) lo = mid; else hi = mid; }
    return lo;
  }

  /* ------------------------------------------------------------------------------------------ model */
  function prepare(data) {
    var M = { t: (data.t || []).map(Number), bodies: [], poses: {}, joints: data.joints || [], rotors: [], channels: data.channels || [] };
    M.n = M.t.length;
    M.T0 = M.t[0] || 0;
    M.T1 = M.t[M.n - 1] || 0;
    var key;
    for (key in (data.poses || {})) M.poses[key] = new Float32Array(b64buf(data.poses[key]));
    var po = data.pose_of || {};
    (data.bodies || []).forEach(function (b, i) {
      var pts = new Float32Array(b64buf(b.points)), tri = new Uint32Array(b64buf(b.triangles));
      var mn = [1e30, 1e30, 1e30], mx = [-1e30, -1e30, -1e30], k, j;
      for (k = 0; k < pts.length; k += 3) for (j = 0; j < 3; j++) { var v = pts[k + j]; if (v < mn[j]) mn[j] = v; if (v > mx[j]) mx[j] = v; }
      var c = [(mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, (mn[2] + mx[2]) / 2];
      var r = Math.hypot(mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2]) / 2;
      var pk = M.poses[b.id] ? b.id : (po[b.id] && M.poses[po[b.id]] ? po[b.id] : null);
      M.bodies.push({ id: b.id, name: b.name, color: b.color || '#9aa5b4', rgb: hex2rgb(b.color), pts: pts, tri: tri, nv: pts.length / 3,
        nt: tri.length / 3, centre: c, radius: r, poseKey: pk, idx: i, rotor: null, grounded: !!b.grounded });
    });
    var byId = {};
    M.bodies.forEach(function (b) { byId[b.id] = b; });
    M.byId = byId;
    // rotors: drawn spinning about their axis through `origin` (mesh coordinates) at a slowed, speed-proportional rate
    (data.rotors || []).forEach(function (ro) {
      var b = byId[ro.body_id];
      if (!b || !ro.speed_rpm || ro.speed_rpm.length !== M.n) return;
      var ax = ro.axis || [0, 0, 1], l = Math.hypot(ax[0], ax[1], ax[2]) || 1;
      var rpm = ro.speed_rpm.map(function (x) { return x == null ? 0 : x; });
      var mxr = Math.max.apply(null, rpm.map(Math.abs));
      var cum = new Float64Array(M.n);
      for (var i = 1; i < M.n; i++) {
        var w0 = (rpm[i - 1] / (mxr || 1)) * 2 * Math.PI * SPIN_REV_PER_S, w1 = (rpm[i] / (mxr || 1)) * 2 * Math.PI * SPIN_REV_PER_S;
        cum[i] = cum[i - 1] + 0.5 * (w0 + w1) * (M.t[i] - M.t[i - 1]) * (ro.sense || 1);
      }
      var obj = { body: b, axis: [ax[0] / l, ax[1] / l, ax[2] / l], origin: ro.origin || b.centre, cum: cum, rpm: rpm, name: ro.name || b.name, max: mxr };
      b.rotor = obj;
      M.rotors.push(obj);
    });
    // model size and the extent of the motion: bounding spheres of the bodies carried over sampled frames
    var step = Math.max(1, Math.floor(M.n / 80)), lo = [1e30, 1e30, 1e30], hi = [-1e30, -1e30, -1e30], tmp = new Float64Array(12);
    var mlo = [1e30, 1e30, 1e30], mhi = [-1e30, -1e30, -1e30];
    for (var f = 0; f < M.n; f += step) {
      M.bodies.forEach(function (b) {
        bodyMatrix(M, b, M.t[f], tmp, true);
        var x = tmp[0] * b.centre[0] + tmp[1] * b.centre[1] + tmp[2] * b.centre[2] + tmp[9];
        var y = tmp[3] * b.centre[0] + tmp[4] * b.centre[1] + tmp[5] * b.centre[2] + tmp[10];
        var z = tmp[6] * b.centre[0] + tmp[7] * b.centre[1] + tmp[8] * b.centre[2] + tmp[11];
        var p = [x, y, z];
        for (var j = 0; j < 3; j++) {
          if (p[j] - b.radius < lo[j]) lo[j] = p[j] - b.radius;
          if (p[j] + b.radius > hi[j]) hi[j] = p[j] + b.radius;
        }
        if (f === 0) for (j = 0; j < 3; j++) { if (p[j] - b.radius < mlo[j]) mlo[j] = p[j] - b.radius; if (p[j] + b.radius > mhi[j]) mhi[j] = p[j] + b.radius; }
      });
    }
    if (!M.bodies.length) { lo = [-1, -1, -1]; hi = [1, 1, 1]; mlo = lo; mhi = hi; }
    M.lo = lo; M.hi = hi;                                  // extent of everything that moves
    M.mlo = mlo; M.mhi = mhi;                              // the model at the first frame
    M.size = Math.max(Math.hypot(mhi[0] - mlo[0], mhi[1] - mlo[1], mhi[2] - mlo[2]), 1e-3);
    M.span = Math.max(Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]), M.size);
    M.groundZ = data.scene && data.scene.ground_z_mm != null ? data.scene.ground_z_mm : null;
    // trajectory of the first pose track: where the aircraft / the moving body has been
    var tk = null;
    for (var i = 0; i < M.bodies.length; i++) if (M.bodies[i].poseKey && !M.bodies[i].grounded) { tk = M.bodies[i]; break; }
    M.trackBody = tk;
    M.followKeys = [];
    var seen = {};
    M.bodies.forEach(function (b) {
      if (b.poseKey && !seen[b.poseKey]) {
        seen[b.poseKey] = 1;
        M.followKeys.push({ key: b.poseKey, body: b, label: b.poseKey === 'aircraft' ? 'Aircraft' : b.name });
      }
    });
    return M;
  }

  var _q = new Float64Array(7);
  function poseAt(M, key, tau, out) {      // out[0..6] = x y z qw qx qy qz at time tau (linear in position, nlerp in rotation)
    var P = key ? M.poses[key] : null;
    if (!P) { out[0] = out[1] = out[2] = 0; out[3] = 1; out[4] = out[5] = out[6] = 0; return out; }
    var i = M.n < 2 ? 0 : bsearch(M.t, tau), j = Math.min(i + 1, M.n - 1);
    var f = j === i ? 0 : Math.min(1, Math.max(0, (tau - M.t[i]) / (M.t[j] - M.t[i])));
    var a = i * 7, b = j * 7, k;
    for (k = 0; k < 3; k++) out[k] = P[a + k] * (1 - f) + P[b + k] * f;
    var dot = P[a + 3] * P[b + 3] + P[a + 4] * P[b + 4] + P[a + 5] * P[b + 5] + P[a + 6] * P[b + 6], s = dot < 0 ? -1 : 1, n = 0;
    for (k = 3; k < 7; k++) { out[k] = P[a + k] * (1 - f) + s * P[b + k] * f; n += out[k] * out[k]; }
    n = Math.sqrt(n) || 1;
    for (k = 3; k < 7; k++) out[k] /= n;
    return out;
  }
  function quatMat(q, m) {
    var w = q[3], x = q[4], y = q[5], z = q[6];
    m[0] = 1 - 2 * (y * y + z * z); m[1] = 2 * (x * y - z * w); m[2] = 2 * (x * z + y * w);
    m[3] = 2 * (x * y + z * w); m[4] = 1 - 2 * (x * x + z * z); m[5] = 2 * (y * z - x * w);
    m[6] = 2 * (x * z - y * w); m[7] = 2 * (y * z + x * w); m[8] = 1 - 2 * (x * x + y * y);
  }
  function spinAt(M, rot, tau) {
    var i = bsearch(M.t, tau), j = Math.min(i + 1, M.n - 1);
    var f = j === i ? 0 : Math.min(1, Math.max(0, (tau - M.t[i]) / (M.t[j] - M.t[i])));
    return rot.cum[i] * (1 - f) + rot.cum[j] * f;
  }
  /* body -> world: out[0..8] = R (row major), out[9..11] = t; v_world = R v + t. Includes the drawn rotor spin. */
  function bodyMatrix(M, b, tau, out, noSpin) {
    poseAt(M, b.poseKey, tau, _q);
    var R = out;
    quatMat(_q, R);
    out[9] = _q[0]; out[10] = _q[1]; out[11] = _q[2];
    if (b.rotor && !noSpin) {
      var r = b.rotor, th = spinAt(M, r, tau), c = Math.cos(th), s = Math.sin(th), ax = r.axis, o = r.origin, C = 1 - c;
      var S = [c + ax[0] * ax[0] * C, ax[0] * ax[1] * C - ax[2] * s, ax[0] * ax[2] * C + ax[1] * s,
        ax[1] * ax[0] * C + ax[2] * s, c + ax[1] * ax[1] * C, ax[1] * ax[2] * C - ax[0] * s,
        ax[2] * ax[0] * C - ax[1] * s, ax[2] * ax[1] * C + ax[0] * s, c + ax[2] * ax[2] * C];
      var so = [S[0] * o[0] + S[1] * o[1] + S[2] * o[2], S[3] * o[0] + S[4] * o[1] + S[5] * o[2], S[6] * o[0] + S[7] * o[1] + S[8] * o[2]];
      var d = [o[0] - so[0], o[1] - so[1], o[2] - so[2]];     // v' = S (v - o) + o = S v + (o - S o)
      var Rp = [R[0], R[1], R[2], R[3], R[4], R[5], R[6], R[7], R[8]], tp = [out[9], out[10], out[11]], i, j, k;
      for (i = 0; i < 3; i++) for (j = 0; j < 3; j++) { var sum = 0; for (k = 0; k < 3; k++) sum += Rp[i * 3 + k] * S[k * 3 + j]; out[i * 3 + j] = sum; }
      for (i = 0; i < 3; i++) out[9 + i] = tp[i] + Rp[i * 3] * d[0] + Rp[i * 3 + 1] * d[1] + Rp[i * 3 + 2] * d[2];
    }
    return out;
  }
  function apply(m, p, out) {
    out[0] = m[0] * p[0] + m[1] * p[1] + m[2] * p[2] + m[9];
    out[1] = m[3] * p[0] + m[4] * p[1] + m[5] * p[2] + m[10];
    out[2] = m[6] * p[0] + m[7] * p[1] + m[8] * p[2] + m[11];
    return out;
  }
  function rotv(m, p, out) {
    out[0] = m[0] * p[0] + m[1] * p[1] + m[2] * p[2];
    out[1] = m[3] * p[0] + m[4] * p[1] + m[5] * p[2];
    out[2] = m[6] * p[0] + m[7] * p[1] + m[8] * p[2];
    return out;
  }
  function chanAt(M, ch, tau) {
    var v = ch.values;
    if (!v || !v.length) return null;
    var i = M.n < 2 ? 0 : bsearch(M.t, tau), j = Math.min(i + 1, M.n - 1);
    var f = j === i ? 0 : Math.min(1, Math.max(0, (tau - M.t[i]) / (M.t[j] - M.t[i])));
    var a = v[i], b = v[j];
    if (a == null || b == null) return a != null ? a : b;
    return a * (1 - f) + b * f;
  }

  /* ------------------------------------------------------------------------------------------ 2-D renderer */
  var VIEWS = [
    { id: 'side', label: 'Side view (x right, z up)', right: [1, 0, 0], up: [0, 0, 1], near: [0, -1, 0], ground: true },
    { id: 'front', label: 'Front view (y right, z up)', right: [0, 1, 0], up: [0, 0, 1], near: [1, 0, 0], ground: true },
    { id: 'top', label: 'Top view (x right, y up)', right: [1, 0, 0], up: [0, 1, 0], near: [0, 0, 1], ground: false }
  ];
  var LIGHT = (function () { var l = [0.34, 0.5, 0.8], n = Math.hypot(l[0], l[1], l[2]); return [l[0] / n, l[1] / n, l[2] / n]; })();
  var NB = 2048;

  function make2D(host, M, S) {
    var grid = h('div', 'hm-views');
    host.appendChild(grid);
    var theme = { bg1: cssVar(host, '--stage', '#18222a'), bg2: cssVar(host, '--stage2', '#0f171d'), ink: cssVar(host, '--ink2', '#a7b4bf'), rule: cssVar(host, '--rule', '#283540') };
    var totalTri = 0, off = [], i;
    M.bodies.forEach(function (b) { off.push(totalTri); totalTri += b.nt; });
    var W = M.bodies.map(function (b) { return new Float32Array(b.nv * 3); });
    var Nn = M.bodies.map(function (b) { return new Float32Array(b.nt * 3); });
    var P = M.bodies.map(function (b) { return new Float32Array(b.nv * 2); });
    var tmpM = new Float64Array(12);
    var table = M.bodies.map(function (b) {
      var out = [];
      for (var l = 0; l < 16; l++) {
        var s = 0.4 + 0.72 * (l + 0.5) / 16, lift = 34;
        out.push('rgb(' + Math.min(255, Math.round((b.rgb[0] * 0.82 + lift) * s)) + ',' + Math.min(255, Math.round((b.rgb[1] * 0.82 + lift) * s)) + ',' + Math.min(255, Math.round((b.rgb[2] * 0.82 + lift) * s)) + ')');
      }
      return out;
    });
    var order = new Int32Array(Math.max(totalTri, 1)), bucket = new Int32Array(Math.max(totalTri, 1)), level = new Uint8Array(Math.max(totalTri, 1));
    var counts = new Int32Array(NB + 1), depth = new Float32Array(Math.max(totalTri, 1));
    var triBody = new Uint16Array(Math.max(totalTri, 1)), triLoc = new Int32Array(Math.max(totalTri, 1));
    M.bodies.forEach(function (b, bi) { for (var t = 0; t < b.nt; t++) { triBody[off[bi] + t] = bi; triLoc[off[bi] + t] = t; } });
    var views = VIEWS.map(function (vd) {
      var wrap = h('div', 'hm-view'), cv = h('canvas');
      wrap.appendChild(cv);
      wrap.appendChild(h('div', 'hm-vlabel', vd.label));
      grid.appendChild(wrap);
      var v = { d: vd, wrap: wrap, cv: cv, ctx: cv.getContext('2d'), w: 10, h: 10, dpr: 1, zoom: 1, panx: 0, pany: 0, fit: null };
      wrap.addEventListener('pointerdown', function (e) {
        v.drag = { x: e.clientX, y: e.clientY, px: v.panx, py: v.pany };
        try { wrap.setPointerCapture(e.pointerId); } catch (x) { /* ignore */ }
        wrap.style.cursor = 'grabbing';
      });
      wrap.addEventListener('pointermove', function (e) {
        if (!v.drag) return;
        v.panx = v.drag.px + (e.clientX - v.drag.x); v.pany = v.drag.py + (e.clientY - v.drag.y);
        S.dirty();
      });
      var up = function () { v.drag = null; wrap.style.cursor = 'grab'; };
      wrap.addEventListener('pointerup', up); wrap.addEventListener('pointercancel', up);
      wrap.addEventListener('wheel', function (e) {
        e.preventDefault();
        var r = cv.getBoundingClientRect(), mx = e.clientX - r.left - r.width / 2, my = e.clientY - r.top - r.height / 2;
        var f = Math.exp(-e.deltaY * 0.0015), nz = Math.min(40, Math.max(0.2, v.zoom * f)), k = nz / v.zoom;
        v.panx = mx - (mx - v.panx) * k; v.pany = my - (my - v.pany) * k; v.zoom = nz;
        S.dirty();
      }, { passive: false });
      wrap.addEventListener('dblclick', function () { v.zoom = 1; v.panx = v.pany = 0; S.dirty(); });
      return v;
    });
    var ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(resize) : null;
    function resize() {
      views.forEach(function (v) {
        var r = v.wrap.getBoundingClientRect(), dpr = Math.min(global.devicePixelRatio || 1, 2);
        var w = Math.max(40, Math.round(r.width)), hh = Math.max(40, Math.round(r.height));
        if (v.cv.width !== Math.round(w * dpr) || v.cv.height !== Math.round(hh * dpr)) { v.cv.width = Math.round(w * dpr); v.cv.height = Math.round(hh * dpr); }
        v.w = w; v.h = hh; v.dpr = dpr;
      });
      S.dirty();
    }
    if (ro) views.forEach(function (v) { ro.observe(v.wrap); });
    resize();

    /* world bounds used for each view when the camera is fixed: the whole motion */
    function fitFor(vd) {
      var lo = M.lo, hi = M.hi, f = S.follow();
      if (f) {
        var c = S.followPos, r = M.size * 0.7;
        return { cx: c[0] * vd.right[0] + c[1] * vd.right[1] + c[2] * vd.right[2], cy: c[0] * vd.up[0] + c[1] * vd.up[1] + c[2] * vd.up[2], hw: r, hh: r };
      }
      var cs = [[lo[0], lo[1], lo[2]], [hi[0], hi[1], hi[2]]], rx = [1e30, -1e30], ry = [1e30, -1e30];
      for (var a = 0; a < 8; a++) {
        var p = [cs[a & 1][0], cs[(a >> 1) & 1][1], cs[(a >> 2) & 1][2]];
        var x = p[0] * vd.right[0] + p[1] * vd.right[1] + p[2] * vd.right[2], y = p[0] * vd.up[0] + p[1] * vd.up[1] + p[2] * vd.up[2];
        rx[0] = Math.min(rx[0], x); rx[1] = Math.max(rx[1], x); ry[0] = Math.min(ry[0], y); ry[1] = Math.max(ry[1], y);
      }
      return { cx: (rx[0] + rx[1]) / 2, cy: (ry[0] + ry[1]) / 2, hw: Math.max((rx[1] - rx[0]) / 2, M.size * 0.15), hh: Math.max((ry[1] - ry[0]) / 2, M.size * 0.15) };
    }

    var lod = 1, ema = 0;
    function draw(tau, fr) {
      var t0 = performance.now(), bi, k;
      // 1. body -> world for every vertex, and triangle normals (shared by the three views)
      for (bi = 0; bi < M.bodies.length; bi++) {
        var b = M.bodies[bi], m = bodyMatrix(M, b, tau, tmpM), w = W[bi], p = b.pts, nn = Nn[bi], tr = b.tri;
        for (k = 0; k < b.nv; k++) {
          var x = p[k * 3], y = p[k * 3 + 1], z = p[k * 3 + 2];
          w[k * 3] = m[0] * x + m[1] * y + m[2] * z + m[9];
          w[k * 3 + 1] = m[3] * x + m[4] * y + m[5] * z + m[10];
          w[k * 3 + 2] = m[6] * x + m[7] * y + m[8] * z + m[11];
        }
        for (k = 0; k < b.nt; k++) {
          var a = tr[k * 3] * 3, bb = tr[k * 3 + 1] * 3, c = tr[k * 3 + 2] * 3;
          var ux = w[bb] - w[a], uy = w[bb + 1] - w[a + 1], uz = w[bb + 2] - w[a + 2], vx = w[c] - w[a], vy = w[c + 1] - w[a + 1], vz = w[c + 2] - w[a + 2];
          var nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx, l = Math.hypot(nx, ny, nz) || 1;
          nn[k * 3] = nx / l; nn[k * 3 + 1] = ny / l; nn[k * 3 + 2] = nz / l;
        }
      }
      var track = fr.track;
      views.forEach(function (v) { drawView(v, tau, fr, track); });
      var dt = performance.now() - t0;
      ema = ema ? ema * 0.8 + dt * 0.2 : dt;
      lod = ema > 60 ? 6 : ema > 38 ? 3 : 1;
    }

    function drawView(v, tau, fr, track) {
      var ctx = v.ctx, vd = v.d, dpr = v.dpr, W_ = v.w, H_ = v.h, bi, k;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W_, H_);
      var fit = fitFor(vd);
      var base = Math.min((W_ - 28) / (2 * fit.hw * 1.12), (H_ - 34) / (2 * fit.hh * 1.12));
      var sc = base * v.zoom;                                  // px per mm
      var ox = W_ / 2 + v.panx - fit.cx * sc, oy = H_ / 2 + v.pany + fit.cy * sc;
      var rr = vd.right, uu = vd.up, nr = vd.near;
      function sx(x, y, z) { return ox + (x * rr[0] + y * rr[1] + z * rr[2]) * sc; }
      function sy(x, y, z) { return oy - (x * uu[0] + y * uu[1] + z * uu[2]) * sc; }
      // grid in world coordinates (it moves past the body when the camera follows it)
      var step = niceStep(60 / sc, 1), wx0 = (0 - ox) / sc, wx1 = (W_ - ox) / sc, wy1 = (oy - 0) / sc, wy0 = (oy - H_) / sc;
      ctx.lineWidth = 1; ctx.strokeStyle = 'rgba(160,180,200,0.10)'; ctx.beginPath();
      for (var gx = Math.ceil(wx0 / step) * step; gx <= wx1; gx += step) { var px = Math.round(ox + gx * sc) + 0.5; ctx.moveTo(px, 0); ctx.lineTo(px, H_); }
      for (var gy = Math.ceil(wy0 / step) * step; gy <= wy1; gy += step) { var py = Math.round(oy - gy * sc) + 0.5; ctx.moveTo(0, py); ctx.lineTo(W_, py); }
      ctx.stroke();
      if (vd.ground && M.groundZ != null) {
        var gyy = oy - M.groundZ * sc;
        ctx.fillStyle = 'rgba(120,140,100,0.16)'; ctx.fillRect(0, gyy, W_, Math.max(0, H_ - gyy));
        ctx.strokeStyle = 'rgba(160,190,140,0.6)'; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.moveTo(0, gyy); ctx.lineTo(W_, gyy); ctx.stroke();
      }
      // origin cross
      ctx.strokeStyle = 'rgba(200,210,220,0.28)'; ctx.lineWidth = 1; ctx.beginPath();
      ctx.moveTo(ox - 6, oy); ctx.lineTo(ox + 6, oy); ctx.moveTo(ox, oy - 6); ctx.lineTo(ox, oy + 6); ctx.stroke();
      // trajectory of the followed / main body
      if (track && track.length > 1) {
        ctx.strokeStyle = 'rgba(243,134,74,0.75)'; ctx.lineWidth = 1.4; ctx.setLineDash([5, 4]); ctx.beginPath();
        for (k = 0; k < track.length; k++) { var X = sx(track[k][0], track[k][1], track[k][2]), Y = sy(track[k][0], track[k][1], track[k][2]); if (k) ctx.lineTo(X, Y); else ctx.moveTo(X, Y); }
        ctx.stroke(); ctx.setLineDash([]);
      }
      // 2. project vertices, depth keys, counting sort (far to near)
      var dmin = 1e30, dmax = -1e30, nvis = 0;
      var minArea = 0.12 * lod / (dpr > 1 ? 1 : 1);
      for (bi = 0; bi < M.bodies.length; bi++) {
        var w = W[bi], pr = P[bi], nv = M.bodies[bi].nv;
        for (k = 0; k < nv; k++) {
          var X1 = w[k * 3], Y1 = w[k * 3 + 1], Z1 = w[k * 3 + 2];
          pr[k * 2] = ox + (X1 * rr[0] + Y1 * rr[1] + Z1 * rr[2]) * sc;
          pr[k * 2 + 1] = oy - (X1 * uu[0] + Y1 * uu[1] + Z1 * uu[2]) * sc;
        }
      }
      for (bi = 0; bi < M.bodies.length; bi++) {
        var bd = M.bodies[bi], tri = bd.tri, ww = W[bi], p2 = P[bi], nn = Nn[bi], o = off[bi];
        for (k = 0; k < bd.nt; k++) {
          var a = tri[k * 3], bb = tri[k * 3 + 1], c = tri[k * 3 + 2];
          var x0 = p2[a * 2], y0 = p2[a * 2 + 1], x1 = p2[bb * 2], y1 = p2[bb * 2 + 1], x2 = p2[c * 2], y2 = p2[c * 2 + 1];
          var gi = o + k;
          if ((x0 < -8 && x1 < -8 && x2 < -8) || (x0 > W_ + 8 && x1 > W_ + 8 && x2 > W_ + 8) ||
              (y0 < -8 && y1 < -8 && y2 < -8) || (y0 > H_ + 8 && y1 > H_ + 8 && y2 > H_ + 8)) { bucket[gi] = -1; continue; }
          var area = Math.abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)) * 0.5;
          if (area < minArea) { bucket[gi] = -1; continue; }
          var dd = (ww[a * 3] * nr[0] + ww[a * 3 + 1] * nr[1] + ww[a * 3 + 2] * nr[2] + ww[bb * 3] * nr[0] + ww[bb * 3 + 1] * nr[1] + ww[bb * 3 + 2] * nr[2] +
            ww[c * 3] * nr[0] + ww[c * 3 + 1] * nr[1] + ww[c * 3 + 2] * nr[2]) / 3;
          depth[gi] = dd; if (dd < dmin) dmin = dd; if (dd > dmax) dmax = dd;
          var n0 = nn[k * 3], n1 = nn[k * 3 + 1], n2 = nn[k * 3 + 2];
          var nT = n0 * nr[0] + n1 * nr[1] + n2 * nr[2], sg = nT < 0 ? -1 : 1;
          var dl = sg * ((n0 * rr[0] + n1 * rr[1] + n2 * rr[2]) * LIGHT[0] + (n0 * uu[0] + n1 * uu[1] + n2 * uu[2]) * LIGHT[1] + nT * LIGHT[2]);
          var lv = Math.floor((0.32 + 0.68 * Math.max(dl, 0)) * 16);
          level[gi] = lv > 15 ? 15 : lv;
          bucket[gi] = 0; nvis++;
        }
      }
      counts.fill(0);
      var span = dmax - dmin || 1, ii;
      for (ii = 0; ii < totalTri; ii++) {
        if (bucket[ii] < 0) continue;
        var bk = Math.min(NB - 1, Math.floor((depth[ii] - dmin) / span * NB));
        bucket[ii] = bk; counts[bk + 1]++;
      }
      for (k = 0; k < NB; k++) counts[k + 1] += counts[k];
      for (ii = 0; ii < totalTri; ii++) if (bucket[ii] >= 0) order[counts[bucket[ii]]++] = ii;
      // 3. draw runs of equal colour
      ctx.lineJoin = 'round'; ctx.lineWidth = 0.7;
      var run = -1, started = false, col = '';
      for (k = 0; k < nvis; k++) {
        var g = order[k], bj = triBody[g], tl = triLoc[g], b3 = M.bodies[bj], ci = bj * 16 + level[g];
        if (ci !== run) {
          if (started) { ctx.fillStyle = col; ctx.strokeStyle = col; ctx.fill(); ctx.stroke(); }
          ctx.beginPath(); started = true; run = ci; col = table[bj][level[g]];
        }
        var q = P[bj], tt = b3.tri, ia = tt[tl * 3], ib = tt[tl * 3 + 1], ic = tt[tl * 3 + 2];
        ctx.moveTo(q[ia * 2], q[ia * 2 + 1]); ctx.lineTo(q[ib * 2], q[ib * 2 + 1]); ctx.lineTo(q[ic * 2], q[ic * 2 + 1]); ctx.closePath();
      }
      if (started) { ctx.fillStyle = col; ctx.strokeStyle = col; ctx.fill(); ctx.stroke(); }
      // 4. joints: axis marker and label
      var jm = fr.joints;
      ctx.font = '11px system-ui,Segoe UI,sans-serif';
      var placed = [];
      for (k = 0; k < jm.length; k++) {
        var J = jm[k], X0 = sx(J.o[0], J.o[1], J.o[2]), Y0 = sy(J.o[0], J.o[1], J.o[2]);
        var dxs = (J.a[0] * rr[0] + J.a[1] * rr[1] + J.a[2] * rr[2]), dys = -(J.a[0] * uu[0] + J.a[1] * uu[1] + J.a[2] * uu[2]);
        var L = Math.max(M.size * 0.06 * sc, 10);
        ctx.strokeStyle = J.color; ctx.fillStyle = J.color; ctx.lineWidth = 1.6;
        ctx.beginPath(); ctx.moveTo(X0 - dxs * L, Y0 - dys * L); ctx.lineTo(X0 + dxs * L, Y0 + dys * L); ctx.stroke();
        ctx.beginPath(); ctx.arc(X0, Y0, 4.2, 0, 6.2832); ctx.fill();
        ctx.lineWidth = 1; ctx.strokeStyle = 'rgba(10,16,20,0.9)'; ctx.stroke();
        var txt = J.name + (J.mode ? ' \u00B7 ' + J.mode : '');
        var tw = ctx.measureText(txt).width, lx = Math.min(X0 + 8, W_ - tw - 14), ly = Y0 - 17, tries = 0;
        while (tries++ < 8 && placed.some(function (r) { return lx < r[0] + r[2] && lx + tw + 10 > r[0] && ly < r[1] + r[3] && ly + 16 > r[1]; })) ly += 18;
        placed.push([lx, ly, tw + 10, 16]);
        ctx.fillStyle = 'rgba(10,16,20,0.78)'; ctx.fillRect(lx, ly, tw + 10, 16);
        ctx.fillStyle = '#e2e9ef'; ctx.fillText(txt, lx + 5, ly + 12);
      }
      // 5. scale bar and axes
      var bar = niceStep(Math.min(W_ * 0.22, 160) / sc, 1);
      var bx = 14, by = H_ - 16;
      ctx.strokeStyle = theme.ink; ctx.fillStyle = theme.ink; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(bx, by); ctx.lineTo(bx + bar * sc, by); ctx.moveTo(bx, by - 4); ctx.lineTo(bx, by + 4); ctx.moveTo(bx + bar * sc, by - 4); ctx.lineTo(bx + bar * sc, by + 4); ctx.stroke();
      ctx.font = '11px system-ui,Segoe UI,sans-serif'; ctx.fillText(lengthLabel(bar), bx, by - 8);
      if (v.zoom !== 1 || v.panx || v.pany) { ctx.fillText('double-click to reset', W_ - 120, H_ - 10); }
    }

    return {
      kind: '2d',
      draw: draw,
      resize: resize,
      resetCamera: function () { views.forEach(function (v) { v.zoom = 1; v.panx = v.pany = 0; }); S.dirty(); },
      destroy: function () { if (ro) ro.disconnect(); if (grid.parentNode) grid.parentNode.removeChild(grid); },
      info: function () { return { views: views.length }; },
      canvases: function () { return views.map(function (v) { return v.cv; }); }
    };
  }

  /* ------------------------------------------------------------------------------------------ WebGL renderer */
  function loadScript(src) {
    return new Promise(function (res, rej) {
      var s = document.createElement('script');
      s.src = src; s.onload = res; s.onerror = function () { rej(new Error('could not load ' + src)); };
      document.head.appendChild(s);
    });
  }
  function ensureThree() {
    if (global.THREE && global.THREE.OrbitControls) return Promise.resolve();
    var p = global.THREE ? Promise.resolve() : loadScript(VENDOR + 'three.min.js');
    return p.then(function () { return global.THREE.OrbitControls ? null : loadScript(VENDOR + 'OrbitControls.js'); });
  }

  function makeGL(host, M, S) {
    var T = global.THREE;
    var renderer = new T.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });   // throws without a GPU context
    var wrap = h('div', 'hm-gl'), overlay = h('div', 'hm-overlay');
    host.appendChild(wrap);
    renderer.setClearColor(0x000000, 0);
    wrap.appendChild(renderer.domElement);
    wrap.appendChild(overlay);
    var scene = new T.Scene(), camera = new T.PerspectiveCamera(38, 1, 1, 1e6);
    camera.up.set(0, 0, 1);
    scene.add(camera);
    scene.add(new T.AmbientLight(0xffffff, 0.5));
    scene.add(new T.HemisphereLight(0xdfe9f5, 0x2a323a, 0.45));
    var key = new T.DirectionalLight(0xffffff, 0.75);
    key.position.set(0.5, 0.35, 1);
    camera.add(key);
    var controls = new T.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = false;
    controls.addEventListener('change', function () { S.dirty(); });
    var meshes = M.bodies.map(function (b) {
      var g = new T.BufferGeometry();
      g.setAttribute('position', new T.BufferAttribute(b.pts, 3));
      g.setIndex(new T.BufferAttribute(b.tri, 1));
      var mat = new T.MeshPhongMaterial({ color: new T.Color(b.color), flatShading: true, side: T.DoubleSide, shininess: 28, specular: 0x222831 });
      var mesh = new T.Mesh(g, mat);
      mesh.matrixAutoUpdate = false;
      mesh.frustumCulled = false;
      scene.add(mesh);
      var edge = null;
      if (b.nt <= 6000) {
        edge = new T.LineSegments(new T.EdgesGeometry(g, 28), new T.LineBasicMaterial({ color: 0x0b1015, transparent: true, opacity: 0.45 }));
        edge.matrixAutoUpdate = false; edge.frustumCulled = false;
        scene.add(edge);
      }
      return { mesh: mesh, edge: edge };
    });
    // ground / floor grid
    var gz = M.groundZ != null ? M.groundZ : M.mlo[2] - M.size * 0.02;
    var gsize = niceStep(Math.max(M.size * 3, (M.hi[0] - M.lo[0]) * 1.2, (M.hi[1] - M.lo[1]) * 1.2), 1) * 2;
    var gdiv = Math.min(60, Math.max(8, Math.round(gsize / niceStep(M.size / 4, 1))));
    var gridH = new T.GridHelper(gsize, gdiv, 0x6b7b8a, 0x2f3b46);
    gridH.rotation.x = Math.PI / 2;
    gridH.position.set((M.lo[0] + M.hi[0]) / 2, (M.lo[1] + M.hi[1]) / 2, gz);
    gridH.material.transparent = true; gridH.material.opacity = 0.55;
    scene.add(gridH);
    // trajectory line
    var trackLine = null;
    // joint markers
    var jointObjs = (S.joints || []).map(function (J) {
      var geo = new T.BufferGeometry();
      geo.setAttribute('position', new T.BufferAttribute(new Float32Array(6), 3));
      var ln = new T.Line(geo, new T.LineBasicMaterial({ color: new T.Color(J.color), linewidth: 2, depthTest: false }));
      ln.frustumCulled = false; ln.renderOrder = 10;
      scene.add(ln);
      var tag = h('div', 'hm-tag', J.name + (J.mode ? ' · ' + J.mode : ''));
      tag.style.borderColor = J.color;
      overlay.appendChild(tag);
      return { ln: ln, tag: tag };
    });
    var size = { w: 10, h: 10 };
    function resize() {
      var r = wrap.getBoundingClientRect(), w = Math.max(40, Math.round(r.width)), hh = Math.max(40, Math.round(r.height));
      size = { w: w, h: hh };
      renderer.setPixelRatio(Math.min(global.devicePixelRatio || 1, 2));
      renderer.setSize(w, hh, false);
      camera.aspect = w / hh; camera.updateProjectionMatrix();
      S.dirty();
    }
    var ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(resize) : null;
    if (ro) ro.observe(wrap);
    resize();
    function setTrack(track) {
      if (trackLine) { scene.remove(trackLine); trackLine.geometry.dispose(); trackLine = null; }
      if (!track || track.length < 2) return;
      var a = new Float32Array(track.length * 3);
      track.forEach(function (p, i) { a[i * 3] = p[0]; a[i * 3 + 1] = p[1]; a[i * 3 + 2] = p[2]; });
      var g = new T.BufferGeometry(); g.setAttribute('position', new T.BufferAttribute(a, 3));
      trackLine = new T.Line(g, new T.LineBasicMaterial({ color: 0xf3864a, transparent: true, opacity: 0.55 }));
      trackLine.frustumCulled = false;
      scene.add(trackLine);
    }
    var tmpM = new Float64Array(12), mm4 = new T.Matrix4(), lastFollow = null, vec = new T.Vector3();
    function placeCamera(centre, radius) {
      var d = radius / Math.sin((camera.fov * Math.PI / 180) / 2) * 0.95;
      var dir = new T.Vector3(0.9, -1.15, 0.7).normalize();
      camera.position.copy(new T.Vector3(centre[0], centre[1], centre[2]).addScaledVector(dir, d));
      controls.target.set(centre[0], centre[1], centre[2]);
      camera.near = Math.max(radius * 0.01, 0.01); camera.far = Math.max(radius * 400, 1000);
      camera.updateProjectionMatrix();
      controls.update();
      S.dirty();
    }
    function resetCamera() {
      var f = S.follow();
      if (f) placeCamera(S.followPos, M.size * 0.9);
      else {
        var c = [(M.lo[0] + M.hi[0]) / 2, (M.lo[1] + M.hi[1]) / 2, (M.lo[2] + M.hi[2]) / 2];
        placeCamera(c, Math.max(M.span / 2, M.size * 0.5) * 0.9);
      }
      lastFollow = f ? S.followPos.slice() : null;
    }
    function draw(tau, fr) {
      var i, k;
      var f = S.follow();
      if (f && lastFollow) {
        var dx = S.followPos[0] - lastFollow[0], dy = S.followPos[1] - lastFollow[1], dz = S.followPos[2] - lastFollow[2];
        camera.position.x += dx; camera.position.y += dy; camera.position.z += dz;
        controls.target.x += dx; controls.target.y += dy; controls.target.z += dz;
      }
      lastFollow = f ? S.followPos.slice() : null;
      for (i = 0; i < M.bodies.length; i++) {
        var m = bodyMatrix(M, M.bodies[i], tau, tmpM);
        mm4.set(m[0], m[1], m[2], m[9], m[3], m[4], m[5], m[10], m[6], m[7], m[8], m[11], 0, 0, 0, 1);
        meshes[i].mesh.matrix.copy(mm4);
        if (meshes[i].edge) meshes[i].edge.matrix.copy(mm4);
      }
      var jm = fr.joints, L = M.size * 0.07;
      for (k = 0; k < jointObjs.length; k++) {
        var J = jm[k], po = jointObjs[k].ln.geometry.attributes.position;
        po.setXYZ(0, J.o[0] - J.a[0] * L, J.o[1] - J.a[1] * L, J.o[2] - J.a[2] * L);
        po.setXYZ(1, J.o[0] + J.a[0] * L, J.o[1] + J.a[1] * L, J.o[2] + J.a[2] * L);
        po.needsUpdate = true;
        vec.set(J.o[0], J.o[1], J.o[2]).project(camera);
        var tg = jointObjs[k].tag;
        if (vec.z > 1 || vec.z < -1) tg.style.display = 'none';
        else { tg.style.display = ''; tg.style.left = ((vec.x * 0.5 + 0.5) * size.w) + 'px'; tg.style.top = ((-vec.y * 0.5 + 0.5) * size.h) + 'px'; }
      }
      controls.update();
      renderer.render(scene, camera);
    }
    var lost = false;
    renderer.domElement.addEventListener('webglcontextlost', function (e) { e.preventDefault(); lost = true; S.glLost(); });
    var api = {
      kind: 'webgl', draw: draw, resize: resize, resetCamera: resetCamera, setTrack: setTrack,
      destroy: function () {
        if (ro) ro.disconnect();
        controls.dispose();
        meshes.forEach(function (m) { m.mesh.geometry.dispose(); m.mesh.material.dispose(); if (m.edge) { m.edge.geometry.dispose(); m.edge.material.dispose(); } });
        renderer.dispose();
        if (renderer.forceContextLoss) { try { renderer.forceContextLoss(); } catch (e) { /* ignore */ } }
        if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
      },
      info: function () { return { lost: lost }; },
      canvases: function () { return [renderer.domElement]; }
    };
    resetCamera();
    return api;
  }

  /* ------------------------------------------------------------------------------------------ channel plot */
  function makePlot(box, M, S) {
    var head = h('div', 'hm-plothead'), sel = h('select'), cvWrap = h('div', 'hm-plot'), cv = h('canvas'), legend = h('div', 'hm-legend');
    sel.setAttribute('aria-label', 'Channel to plot');
    head.appendChild(h('span', 'hm-lbl', 'Channel'));
    head.appendChild(sel);
    cvWrap.appendChild(cv);
    box.appendChild(head); box.appendChild(cvWrap); box.appendChild(legend);
    // options: one entry per quantity ("all") plus each channel
    var groups = [], byQ = {};
    M.channels.forEach(function (c, i) {
      var q = (c.quantity || c.name) + '|' + c.unit;
      if (!byQ[q]) { byQ[q] = { q: c.quantity || c.name, unit: c.unit, ids: [] }; groups.push(byQ[q]); }
      byQ[q].ids.push(i);
    });
    var opts = [];
    groups.forEach(function (g) {
      var og = document.createElement('optgroup');
      og.label = g.q + ' [' + g.unit + ']';
      if (g.ids.length > 1) {
        var o = document.createElement('option'); o.value = 'g:' + opts.length; o.textContent = 'all ' + g.q + ' (' + g.ids.length + ')'; og.appendChild(o);
        opts.push(g.ids);
      }
      g.ids.forEach(function (id) {
        var o2 = document.createElement('option'); o2.value = 'g:' + opts.length; o2.textContent = M.channels[id].name; og.appendChild(o2);
        opts.push([id]);
      });
      sel.appendChild(og);
    });
    var active = opts.length ? opts[0] : [];
    for (var oi = 0; oi < opts.length; oi++) {
      var c0 = M.channels[opts[oi][0]];
      if (/angle|position|attitude|altitude|rotor speed/.test(c0.quantity || c0.name)) { active = opts[oi]; break; }
    }
    var defIdx = opts.indexOf(active);
    if (defIdx >= 0) sel.value = 'g:' + defIdx;
    var statics = null, rect = { w: 10, h: 10, dpr: 1 };
    var PAD = { l: 58, r: 14, t: 12, b: 38 };
    var ctx = cv.getContext('2d');
    var colors = { ink: cssVar(box, '--ink2', '#a7b4bf'), rule: cssVar(box, '--rule', '#283540'), ink1: cssVar(box, '--ink', '#e2e9ef') };
    var y0 = 0, y1 = 1, unit = '';
    function layout() {
      var r = cvWrap.getBoundingClientRect(), dpr = Math.min(global.devicePixelRatio || 1, 2);
      rect = { w: Math.max(100, Math.round(r.width)), h: Math.max(80, Math.round(r.height)), dpr: dpr };
      if (cv.width !== Math.round(rect.w * dpr) || cv.height !== Math.round(rect.h * dpr)) { cv.width = Math.round(rect.w * dpr); cv.height = Math.round(rect.h * dpr); }
      statics = null;
    }
    function xOf(t) { return PAD.l + (t - M.T0) / ((M.T1 - M.T0) || 1) * (rect.w - PAD.l - PAD.r); }
    function yOf(v) { return rect.h - PAD.b - (v - y0) / ((y1 - y0) || 1) * (rect.h - PAD.t - PAD.b); }
    function paintStatic() {
      var c = document.createElement('canvas');
      c.width = cv.width; c.height = cv.height;
      var g = c.getContext('2d');
      g.setTransform(rect.dpr, 0, 0, rect.dpr, 0, 0);
      var chans = active.map(function (i) { return M.channels[i]; });
      unit = chans.length ? chans[0].unit : '';
      var lo = 1e30, hi = -1e30;
      chans.forEach(function (ch) { ch.values.forEach(function (v) { if (v != null) { if (v < lo) lo = v; if (v > hi) hi = v; } }); });
      if (!(lo <= hi)) { lo = 0; hi = 1; }
      if (hi - lo < 1e-12) { var e = Math.max(Math.abs(hi) * 0.1, 0.5); lo -= e; hi += e; }
      var pad = (hi - lo) * 0.08; y0 = lo - pad; y1 = hi + pad;
      var ys = niceStep(y1 - y0, 5), xs = niceStep(M.T1 - M.T0, 8);
      g.font = '11px system-ui,Segoe UI,sans-serif'; g.lineWidth = 1;
      g.strokeStyle = 'rgba(160,180,200,0.13)'; g.fillStyle = colors.ink;
      g.textAlign = 'right'; g.textBaseline = 'middle';
      g.beginPath();
      for (var v = Math.ceil(y0 / ys) * ys; v <= y1; v += ys) {
        var yy = Math.round(yOf(v)) + 0.5; g.moveTo(PAD.l, yy); g.lineTo(rect.w - PAD.r, yy);
      }
      g.stroke();
      for (v = Math.ceil(y0 / ys) * ys; v <= y1; v += ys) g.fillText(fmt(v, ys < 1 ? Math.min(4, Math.ceil(-Math.log10(ys))) : 0), PAD.l - 6, yOf(v));
      g.textAlign = 'center'; g.textBaseline = 'top';
      g.beginPath();
      for (var t = Math.ceil(M.T0 / xs) * xs; t <= M.T1 + 1e-9; t += xs) { var xx = Math.round(xOf(t)) + 0.5; g.moveTo(xx, PAD.t); g.lineTo(xx, rect.h - PAD.b); }
      g.stroke();
      for (t = Math.ceil(M.T0 / xs) * xs; t <= M.T1 + 1e-9; t += xs) g.fillText(fmt(t, xs < 1 ? 1 : 0), xOf(t), rect.h - PAD.b + 5);
      g.textAlign = 'right'; g.fillText('time [s]', rect.w - PAD.r, rect.h - 15);
      g.save(); g.translate(12, rect.h / 2); g.rotate(-Math.PI / 2); g.textBaseline = 'middle'; g.fillText(unit, 0, 0); g.restore();
      g.strokeStyle = colors.rule; g.strokeRect(PAD.l + 0.5, PAD.t + 0.5, rect.w - PAD.l - PAD.r, rect.h - PAD.t - PAD.b);
      // phases (bands) and events (dashed markers)
      (S.phases || []).forEach(function (p, i) {
        if (!(p.t1 > p.t0)) return;
        g.fillStyle = i % 2 ? 'rgba(120,150,190,0.06)' : 'rgba(120,150,190,0.12)';
        g.fillRect(xOf(p.t0), PAD.t, xOf(p.t1) - xOf(p.t0), rect.h - PAD.t - PAD.b);
      });
      g.save(); g.setLineDash([3, 3]); g.strokeStyle = 'rgba(231,181,68,0.7)'; g.fillStyle = 'rgba(231,181,68,0.95)'; g.textAlign = 'left'; g.textBaseline = 'top';
      (S.events || []).forEach(function (e, i) {
        if (e.t < M.T0 || e.t > M.T1) return;
        var x = xOf(e.t); g.beginPath(); g.moveTo(x, PAD.t); g.lineTo(x, rect.h - PAD.b); g.stroke();
        if (S.events.length <= 6) { g.setLineDash([]); g.fillText(e.text, x + 3, PAD.t + 2 + (i % 3) * 12); g.setLineDash([3, 3]); }
      });
      g.restore();
      // series
      chans.forEach(function (ch, si) {
        g.strokeStyle = SERIES[si % SERIES.length]; g.lineWidth = 1.7; g.lineJoin = 'round'; g.beginPath();
        var started = false;
        for (var i = 0; i < M.n; i++) {
          var v2 = ch.values[i];
          if (v2 == null) { started = false; continue; }
          var X = xOf(M.t[i]), Y = yOf(v2);
          if (started) g.lineTo(X, Y); else { g.moveTo(X, Y); started = true; }
        }
        g.stroke();
      });
      statics = c;
    }
    function paint(tau) {
      if (!statics) paintStatic();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, cv.width, cv.height);
      ctx.drawImage(statics, 0, 0);
      ctx.setTransform(rect.dpr, 0, 0, rect.dpr, 0, 0);
      var x = xOf(Math.min(Math.max(tau, M.T0), M.T1));
      ctx.strokeStyle = '#f3864a'; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(x, PAD.t); ctx.lineTo(x, rect.h - PAD.b); ctx.stroke();
      active.forEach(function (id, si) {
        var v = chanAt(M, M.channels[id], tau);
        if (v == null) return;
        ctx.fillStyle = SERIES[si % SERIES.length];
        ctx.beginPath(); ctx.arc(x, yOf(v), 3.6, 0, 6.2832); ctx.fill();
        ctx.strokeStyle = 'rgba(10,16,20,0.9)'; ctx.lineWidth = 1; ctx.stroke();
      });
    }
    function legendUpdate(tau) {
      active.forEach(function (id, si) {
        var e = legend.children[si];
        if (e) e.lastChild.textContent = fmt(chanAt(M, M.channels[id], tau)) + ' ' + M.channels[id].unit;
      });
    }
    function buildLegend() {
      legend.textContent = '';
      active.forEach(function (id, si) {
        var sp = h('span'), sw = h('i');
        sw.style.background = SERIES[si % SERIES.length];
        var ch = M.channels[id];
        sp.appendChild(sw); sp.appendChild(h('span', '', active.length > 1 ? (ch.short || ch.name) : ch.name)); sp.appendChild(h('span', 'v', ''));
        legend.appendChild(sp);
      });
      var note = M.channels[active[0]] && M.channels[active[0]].note;
      if (note) { var n = h('span', '', note); n.style.color = 'var(--ink3,#7b8893)'; n.style.flexBasis = '100%'; legend.appendChild(n); }
    }
    function seekFromEvent(e) {
      var r = cv.getBoundingClientRect(), x = e.clientX - r.left;
      var f = (x - PAD.l) / Math.max(r.width - PAD.l - PAD.r, 1);
      S.seek(M.T0 + Math.min(1, Math.max(0, f)) * (M.T1 - M.T0));
    }
    var dragging = false;
    cv.addEventListener('pointerdown', function (e) { dragging = true; try { cv.setPointerCapture(e.pointerId); } catch (x) { /* ignore */ } S.pause(); seekFromEvent(e); });
    cv.addEventListener('pointermove', function (e) { if (dragging) seekFromEvent(e); });
    cv.addEventListener('pointerup', function () { dragging = false; });
    cv.addEventListener('pointercancel', function () { dragging = false; });
    sel.addEventListener('change', function () {
      var k = parseInt(sel.value.slice(2), 10);
      active = opts[k] || [];
      buildLegend(); statics = null; S.dirty();
    });
    var ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(function () { layout(); S.dirty(); }) : null;
    if (ro) ro.observe(cvWrap);
    layout(); buildLegend();
    return {
      paint: function (tau) { if (!active.length) return; paint(tau); legendUpdate(tau); },
      destroy: function () { if (ro) ro.disconnect(); },
      select: function (name) {
        for (var k = 0; k < opts.length; k++) {
          if (opts[k].length === 1 && M.channels[opts[k][0]].name === name) {
            sel.value = 'g:' + k; active = opts[k]; buildLegend(); statics = null; S.dirty(); return true;
          }
        }
        return false;
      }
    };
  }

  /* ------------------------------------------------------------------------------------------ mount */
  function jointMode(j) {
    if (j.mode === 'prescribed' || j.mode === 'torque-driven') return j.mode;
    return null;
  }

  function mount(el, data, opts) {
    opts = opts || {};
    injectCss();
    el.textContent = '';
    var root = h('div', 'hm');
    root.tabIndex = 0;
    el.appendChild(root);
    var destroyed = false, raf = 0;
    var handle = { destroy: destroy, play: function () { }, pause: function () { }, seek: function () { }, getState: function () { return { kind: null }; } };

    if (!data || !data.kind) {
      var em = h('div', 'hm-empty');
      em.appendChild(h('b', '', 'No motion to show'));
      var notes = (data && data.notes && data.notes.length) ? data.notes : ['This run has no stored motion data.'];
      notes.forEach(function (n) { em.appendChild(h('div', '', n)); });
      root.appendChild(em);
      return handle;
    }

    var M = prepare(data);
    var isDrone = data.kind === 'hover' || data.kind === 'flight';
    var top = h('div', 'hm-top');
    top.appendChild(h('div', 'hm-title', data.title || 'Motion'));
    if (data.basis) top.appendChild(h('div', 'hm-basis', 'Basis: ' + data.basis));
    if (isDrone) top.appendChild(h('div', 'hm-disc', data.disclaimer || DISCLAIMER));
    root.appendChild(top);
    var stage = h('div', 'hm-stage');
    root.appendChild(stage);
    var bar = h('div', 'hm-bar');
    root.appendChild(bar);
    var live = h('div', 'hm-live');
    root.appendChild(live);
    var plotBox = h('div', 'hm-plotbox');
    root.appendChild(plotBox);
    var notesEl = h('details', 'hm-notes');

    // playback state
    var st = { tau: M.T0, playing: false, speed: 1, loop: true, followKey: null, followPos: [0, 0, 0], dirty: true, last: 0 };
    var follow = function () { return st.followKey; };
    var kindR = '2d';

    // controls
    var bPlay = h('button', 'hm-btn hm-play', 'Play'); bPlay.type = 'button'; bPlay.setAttribute('aria-label', 'Play or pause');
    var bBack = h('button', 'hm-btn', '⏮'); bBack.type = 'button'; bBack.title = 'Back one frame'; bBack.setAttribute('aria-label', 'Back one frame');
    var bFwd = h('button', 'hm-btn', '⏭'); bFwd.type = 'button'; bFwd.title = 'Forward one frame'; bFwd.setAttribute('aria-label', 'Forward one frame');
    var selSpeed = h('select'); selSpeed.setAttribute('aria-label', 'Playback speed');
    [0.25, 0.5, 1, 2, 4].forEach(function (s) { var o = h('option', '', s + '×'); o.value = s; if (s === 1) o.selected = true; selSpeed.appendChild(o); });
    var lblSpeed = h('label', 'hm-lbl'); lblSpeed.appendChild(document.createTextNode('Speed ')); lblSpeed.appendChild(selSpeed);
    var cbLoop = h('input'); cbLoop.type = 'checkbox'; cbLoop.checked = true;
    var lblLoop = h('label', 'hm-lbl'); lblLoop.appendChild(cbLoop); lblLoop.appendChild(document.createTextNode('Loop'));
    var scrub = h('input', 'hm-scrub'); scrub.type = 'range'; scrub.min = 0; scrub.max = 1000; scrub.value = 0; scrub.setAttribute('aria-label', 'Time');
    var tm = h('span', 'hm-time'); tm.setAttribute('data-hm', 'time'); tm.setAttribute('aria-live', 'off');
    var bReset = h('button', 'hm-btn', 'Reset camera'); bReset.type = 'button';
    var selFollow = h('select'); selFollow.setAttribute('aria-label', 'Follow body');
    var oFix = h('option', '', 'Fixed view'); oFix.value = ''; selFollow.appendChild(oFix);
    M.followKeys.forEach(function (fk) { var o = h('option', '', 'Follow: ' + fk.label); o.value = fk.key; selFollow.appendChild(o); });
    var lblFollow = h('label', 'hm-lbl'); lblFollow.appendChild(document.createTextNode('Camera ')); lblFollow.appendChild(selFollow);
    [bPlay, bBack, bFwd, lblSpeed, lblLoop, scrub, tm, bReset].forEach(function (x) { bar.appendChild(x); });
    if (M.followKeys.length) bar.appendChild(lblFollow);

    // default camera: follow the aircraft when it travels far compared with its size
    var travel = M.span / M.size;
    if (isDrone && M.followKeys.length && travel > 6) { st.followKey = M.followKeys[0].key; selFollow.value = st.followKey; }

    // live readouts: joints (with the prescribed / torque-driven label) and rotors
    var chipJoint = [], chipRotor = [];
    M.joints.filter(function (j) { return j.kind !== 'fixed'; }).forEach(function (j) {
      var mode = jointMode(j), chip = h('span', 'hm-chip');
      var badge = h('span', 'hm-mode', mode || (j.kind === 'fixed' ? 'fixed' : (j.label && /coupled/.test(j.label) ? 'coupled' : j.kind)));
      badge.style.background = MODE_COLOR[mode || 'other'];
      badge.title = j.label || '';
      chip.appendChild(h('b', '', j.name)); chip.appendChild(badge);
      var vEl = h('span', 'v', '');
      chip.appendChild(vEl);
      live.appendChild(chip);
      var ang = null, spd = null;
      M.channels.forEach(function (c) {
        var w = c.name.split(': ')[1] || '';
        if (c.group === j.name && /^(angle|position)$/.test(w)) ang = c;
        if (c.group === j.name && w === 'speed') spd = c;
      });
      chipJoint.push({ j: j, vEl: vEl, ang: ang, spd: spd });
    });
    M.rotors.forEach(function (r) {
      var chip = h('span', 'hm-chip'); chip.appendChild(h('b', '', r.name));
      var vEl = h('span', 'v', ''); chip.appendChild(vEl); live.appendChild(chip);
      chipRotor.push({ r: r, vEl: vEl });
    });
    if (!live.children.length) live.style.display = 'none';

    // joint marker data per frame
    var Jlist = M.joints.filter(function (j) { return j.axis && j.origin && j.kind !== 'fixed'; }).map(function (j) {
      var mode = jointMode(j);
      return { j: j, name: j.name, mode: mode, color: MODE_COLOR[mode || 'other'], parent: j.parent_body ? M.byId[j.parent_body] : null, o: [0, 0, 0], a: [0, 0, 1] };
    });
    var S = {
      follow: follow, joints: Jlist, followPos: st.followPos, phases: data.phases || [], events: data.events || [],
      dirty: function () { st.dirty = true; },
      seek: function (t) { seek(t); },
      pause: function () { pause(); },
      glLost: function () { switchTo2D('The graphics context was lost; showing the 2-D views.'); }
    };

    var renderer = null, plot = null, rnote = h('div', 'hm-rnote');
    var track = null;
    (function buildTrack() {
      var b = M.trackBody;
      if (!b || M.span / M.size < 1.5) return;
      track = [];
      var m = new Float64Array(12), c = [0, 0, 0], step = Math.max(1, Math.floor(M.n / 300));
      for (var i = 0; i < M.n; i += step) {
        bodyMatrix(M, b, M.t[i], m, true);
        track.push(apply(m, b.centre, [0, 0, 0]));
      }
    })();

    function start2D(note) {
      kindR = '2d';
      renderer = make2D(stage, M, S);
      rnote.textContent = note || (opts.webgl === false ? '2-D views (no WebGL)' : '2-D views');
      root.insertBefore(rnote, stage.nextSibling);
    }
    function switchTo2D(note) {
      if (destroyed) return;
      if (renderer) { try { renderer.destroy(); } catch (e) { /* ignore */ } }
      start2D(note);
      st.dirty = true;
    }
    plot = makePlot(plotBox, M, S);
    if (!M.channels.length) plotBox.style.display = 'none';

    var wantGL = opts.webgl !== false && !!opts.webgl;
    if (wantGL) {
      ensureThree().then(function () {
        if (destroyed) return;
        try {
          renderer = makeGL(stage, M, S);
          if (renderer.setTrack) renderer.setTrack(track);
          kindR = 'webgl';
          rnote.textContent = 'WebGL';
          root.insertBefore(rnote, stage.nextSibling);
        } catch (e) {
          renderer = null;
          start2D('WebGL could not start (' + (e && e.message ? e.message : e) + '); showing the 2-D views.');
        }
        st.dirty = true;
      }).catch(function (e) {
        if (destroyed) return;
        start2D('The 3-D viewer could not be loaded (' + (e && e.message ? e.message : e) + '); showing the 2-D views.');
        st.dirty = true;
      });
    } else {
      start2D();
    }

    // notes and basis
    notesEl.open = !isDrone && (data.notes || []).length <= 4;
    notesEl.appendChild(h('summary', '', 'Notes and basis (' + ((data.notes || []).length + (data.basis ? 1 : 0)) + ')'));
    var ul = h('ul');
    if (data.basis) { var li0 = h('li', '', 'Basis: ' + data.basis); ul.appendChild(li0); }
    (data.notes || []).forEach(function (n) { ul.appendChild(h('li', '', n)); });
    if (data.frames && data.frames.decimated) ul.appendChild(h('li', '', 'Frames: ' + data.frames.shown + ' shown of ' + data.frames.stored + ' stored.'));
    notesEl.appendChild(ul);
    root.appendChild(notesEl);

    /* ---- playback */
    function setTau(t) {
      st.tau = Math.min(M.T1, Math.max(M.T0, t));
      st.dirty = true;
    }
    function play() {
      if (st.tau >= M.T1 - 1e-9) setTau(M.T0);
      st.playing = true; st.last = 0; bPlay.textContent = 'Pause'; st.dirty = true;
    }
    function pause() { st.playing = false; bPlay.textContent = 'Play'; st.dirty = true; }
    function seek(t) { setTau(t); }
    function stepFrame(d) {
      pause();
      var i = bsearch(M.t, st.tau + (d > 0 ? 1e-9 : -1e-9));
      if (d > 0) i = Math.min(M.n - 1, (st.tau >= M.t[Math.min(i + 1, M.n - 1)] - 1e-12 ? i + 1 : i) + 1);
      else i = Math.max(0, (st.tau - M.t[i] > 1e-9 ? i : i - 1));
      setTau(M.t[Math.max(0, Math.min(M.n - 1, i))]);
    }
    bPlay.addEventListener('click', function () { if (st.playing) pause(); else play(); });
    bBack.addEventListener('click', function () { stepFrame(-1); });
    bFwd.addEventListener('click', function () { stepFrame(1); });
    selSpeed.addEventListener('change', function () { st.speed = parseFloat(selSpeed.value) || 1; });
    cbLoop.addEventListener('change', function () { st.loop = cbLoop.checked; });
    scrub.addEventListener('input', function () { pause(); setTau(M.T0 + (scrub.value / 1000) * (M.T1 - M.T0)); });
    bReset.addEventListener('click', function () { if (renderer) renderer.resetCamera(); });
    selFollow.addEventListener('change', function () {
      st.followKey = selFollow.value || null;
      if (renderer) renderer.resetCamera();
      st.dirty = true;
    });
    root.addEventListener('keydown', function (e) {
      if (/^(SELECT|INPUT|BUTTON|TEXTAREA)$/.test((e.target.tagName || ''))) { if (e.target !== scrub || (e.key !== ' ')) return; }
      if (e.key === ' ') { e.preventDefault(); if (st.playing) pause(); else play(); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); stepFrame(-1); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); stepFrame(1); }
    });

    function frameState() {
      var tau = st.tau, tmp = new Float64Array(12), i, k;
      // follow position
      if (st.followKey) {
        var fk = M.followKeys.filter(function (x) { return x.key === st.followKey; })[0];
        if (fk) { bodyMatrix(M, fk.body, tau, tmp, true); apply(tmp, fk.body.centre, st.followPos); }
      }
      var jm = Jlist;
      for (k = 0; k < jm.length; k++) {
        var J = jm[k], pm = J.parent;
        if (pm) { bodyMatrix(M, pm, tau, tmp, true); apply(tmp, J.j.origin, J.o); rotv(tmp, J.j.axis, J.a); }
        else { J.o[0] = J.j.origin[0]; J.o[1] = J.j.origin[1]; J.o[2] = J.j.origin[2]; J.a[0] = J.j.axis[0]; J.a[1] = J.j.axis[1]; J.a[2] = J.j.axis[2]; }
      }
      return { joints: jm, track: track };
    }
    function updateReadouts() {
      var tau = st.tau, i = M.n < 2 ? 0 : bsearch(M.t, tau);
      if (tau >= M.T1 - 1e-12) i = M.n - 1;
      tm.textContent = 't = ' + tau.toFixed(3) + ' s / ' + M.T1.toFixed(3) + ' s · frame ' + (i + 1) + '/' + M.n;
      scrub.value = M.T1 > M.T0 ? Math.round((tau - M.T0) / (M.T1 - M.T0) * 1000) : 0;
      chipJoint.forEach(function (c) {
        if (c.ang) c.vEl.textContent = fmt(chanAt(M, c.ang, tau)) + ' ' + c.ang.unit + (c.spd ? ' \u00B7 ' + fmt(chanAt(M, c.spd, tau), 1) + ' ' + c.spd.unit : '');
        else c.vEl.textContent = c.j.kind;
      });
      chipRotor.forEach(function (c) {
        var f = M.n < 2 ? 0 : bsearch(M.t, tau), g = Math.min(f + 1, M.n - 1), w = g === f ? 0 : (tau - M.t[f]) / (M.t[g] - M.t[f]);
        c.vEl.textContent = fmt(c.r.rpm[f] * (1 - w) + c.r.rpm[g] * w, 0) + ' rpm (drawn slowed)';
      });
    }
    var frames = 0;
    function tick(now) {
      if (destroyed) return;
      raf = requestAnimationFrame(tick);
      if (st.playing) {
        if (!st.last) st.last = now;
        var dt = Math.min(0.1, (now - st.last) / 1000);
        st.last = now;
        var nt = st.tau + dt * st.speed;
        if (nt >= M.T1) {
          if (st.loop) nt = M.T0 + (nt - M.T1) % Math.max(M.T1 - M.T0, 1e-6);
          else { nt = M.T1; pause(); }
        }
        st.tau = nt; st.dirty = true;
      }
      if (st.dirty && renderer) {
        st.dirty = false;
        var fr = frameState();
        renderer.draw(st.tau, fr);
        plot.paint(st.tau);
        updateReadouts();
        frames++;
      } else if (st.dirty && !renderer) { /* waiting for the viewer to load */ updateReadouts(); }
    }
    raf = requestAnimationFrame(tick);
    updateReadouts();

    function destroy() {
      if (destroyed) return;
      destroyed = true;
      cancelAnimationFrame(raf);
      try { if (renderer) renderer.destroy(); } catch (e) { /* ignore */ }
      try { plot.destroy(); } catch (e) { /* ignore */ }
      el.textContent = '';
    }
    handle.destroy = destroy;
    handle.play = play; handle.pause = pause; handle.seek = seek;
    handle.getState = function () {
      var cv = renderer ? renderer.canvases() : [];
      return { kind: data.kind, renderer: renderer ? renderer.kind : null, t: st.tau, T: M.T1, playing: st.playing, speed: st.speed, frames: frames,
        nFrames: M.n, bodies: M.bodies.length, canvases: cv.length, follow: st.followKey };
    };
    handle.renderNow = function () { st.dirty = true; };
    handle.selectChannel = function (name) { return plot.select(name); };
    return handle;
  }

  global.HubMotion = { mount: mount, version: 1 };
})(typeof window !== 'undefined' ? window : this);
