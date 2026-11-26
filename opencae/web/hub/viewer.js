/* three.js viewer for the Results Hub (contours on surfaces, slices, streamlines, part visibility, click to read).
   window.HubViewer: decoding helpers, colour maps, webglAvailable(), create(el, handlers).
   Frame: aircraft/model (x fwd, y left, z up, mm) -> three (X, Y, Z) = (x, z, -y). */
(function () {
  'use strict';
  function b64bytes(s) {
    var bin = atob(s), n = bin.length, u = new Uint8Array(n);
    for (var i = 0; i < n; i++) u[i] = bin.charCodeAt(i);
    return u;
  }
  function b64f32(s) { if (!s) return null; var u = b64bytes(s); return new Float32Array(u.buffer, 0, u.length >> 2); }
  function b64i32(s) { if (!s) return null; var u = b64bytes(s); return new Int32Array(u.buffer, 0, u.length >> 2); }

  var CMAPS = {
    viridis: ['#440154', '#472d7b', '#3b528b', '#2c728e', '#21918c', '#28ae80', '#5ec962', '#addc30', '#fde725'],
    turbo: ['#30123b', '#4662d7', '#36aaf9', '#1ae4b6', '#72fe5e', '#c7ef34', '#fabb21', '#f6600b', '#c32804', '#7a0403'],
    coolwarm: ['#3b4cc0', '#6f92f3', '#aac7fd', '#dddcdc', '#f4b69b', '#e7745b', '#b40426'],
    inferno: ['#000004', '#320a5e', '#781c6d', '#bb3754', '#ed6925', '#fbb61a', '#fcffa4'],
    gray: ['#101010', '#808080', '#f4f4f4']
  };
  var parsed = {};
  function stops(name) {
    if (!parsed[name]) parsed[name] = (CMAPS[name] || CMAPS.viridis).map(function (h) {
      return [parseInt(h.slice(1, 3), 16) / 255, parseInt(h.slice(3, 5), 16) / 255, parseInt(h.slice(5, 7), 16) / 255];
    });
    return parsed[name];
  }
  function cmapColor(name, t, out) {
    var s = stops(name); t = t < 0 ? 0 : t > 1 ? 1 : (t === t ? t : 0);
    var f = t * (s.length - 1), i = Math.min(s.length - 2, Math.floor(f)), u = f - i, a = s[i], b = s[i + 1];
    out = out || [0, 0, 0]; out[0] = a[0] + (b[0] - a[0]) * u; out[1] = a[1] + (b[1] - a[1]) * u; out[2] = a[2] + (b[2] - a[2]) * u; return out;
  }
  function drawBar(canvas, name) {
    var w = canvas.width = 256, h = canvas.height = 1, c = canvas.getContext('2d'), col = [0, 0, 0];
    for (var x = 0; x < w; x++) {
      cmapColor(name, x / (w - 1), col);
      c.fillStyle = 'rgb(' + Math.round(col[0] * 255) + ',' + Math.round(col[1] * 255) + ',' + Math.round(col[2] * 255) + ')';
      c.fillRect(x, 0, 1, h);
    }
  }
  function webglAvailable() {
    try {
      var c = document.createElement('canvas');
      var gl = c.getContext('webgl2') || c.getContext('webgl') || c.getContext('experimental-webgl');
      if (!gl) return { ok: false, error: 'getContext returned null' };
      var lose = gl.getExtension && gl.getExtension('WEBGL_lose_context'); if (lose) lose.loseContext();
      return { ok: true, error: '' };
    } catch (e) { return { ok: false, error: String(e && e.message || e) }; }
  }
  function aToThree(x, y, z) { return [x, z, -y]; }

  function create(el, handlers) {
    handlers = handlers || {};
    if (typeof THREE === 'undefined') throw new Error('three.js is not loaded');
    var renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });   // throws when WebGL is unavailable
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    el.appendChild(renderer.domElement);
    var scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(38, 1, 0.5, 100000);
    scene.add(new THREE.HemisphereLight(0xffffff, 0x2a3038, 0.9));
    var d1 = new THREE.DirectionalLight(0xffffff, 0.75); d1.position.set(300, 500, 200); scene.add(d1);
    var d2 = new THREE.DirectionalLight(0x88aaff, 0.3); d2.position.set(-300, 100, -300); scene.add(d2);
    var controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = false;
    var raycaster = new THREE.Raycaster(), mouse = new THREE.Vector2();
    var root = new THREE.Group(); scene.add(root);
    var S = { data: null, partMeshes: {}, field: null, fieldMesh: null, fieldCtx: null, slices: [], stream: null,
      center: new THREE.Vector3(), radius: 200, opts: { cmap: 'viridis', min: 0, max: 1, deform: 0 }, marker: null, dead: false };
    var pending = false;
    function render() { pending = false; if (!S.dead) renderer.render(scene, camera); }
    function want() { if (!pending && !S.dead) { pending = true; requestAnimationFrame(render); } }
    controls.addEventListener('change', want);
    renderer.domElement.addEventListener('webglcontextlost', function (e) { e.preventDefault(); if (handlers.onLost) handlers.onLost(); });
    function resize() {
      var w = el.clientWidth, h = el.clientHeight; if (!w || !h) return;
      renderer.setSize(w, h, false); renderer.domElement.style.width = w + 'px'; renderer.domElement.style.height = h + 'px';
      camera.aspect = w / h; camera.updateProjectionMatrix(); want();
    }
    var ro = new ResizeObserver(resize); ro.observe(el); resize();

    function clear(group) {
      group.traverse && group.traverse(function (o) { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); });
      root.remove(group);
    }
    function meshGeometry(points, tris, disp) {
      var n = points.length / 3, pos = new Float32Array(points.length), base = new Float32Array(points.length);
      var dsp = disp && disp.length === points.length ? new Float32Array(points.length) : null;
      for (var i = 0; i < n; i++) {
        var p = aToThree(points[3 * i], points[3 * i + 1], points[3 * i + 2]);
        base[3 * i] = pos[3 * i] = p[0]; base[3 * i + 1] = pos[3 * i + 1] = p[1]; base[3 * i + 2] = pos[3 * i + 2] = p[2];
        if (dsp) { var d = aToThree(disp[3 * i], disp[3 * i + 1], disp[3 * i + 2]); dsp[3 * i] = d[0]; dsp[3 * i + 1] = d[1]; dsp[3 * i + 2] = d[2]; }
      }
      var g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
      g.setIndex(new THREE.BufferAttribute(Uint32Array.from(tris), 1));
      g.computeVertexNormals();
      return { geo: g, base: base, disp: dsp };
    }
    function bbox(points, bb) {
      for (var i = 0; i < points.length; i += 3) {
        var p = aToThree(points[i], points[i + 1], points[i + 2]);
        for (var k = 0; k < 3; k++) { if (p[k] < bb[0][k]) bb[0][k] = p[k]; if (p[k] > bb[1][k]) bb[1][k] = p[k]; }
      }
    }
    function frame(bb) {
      if (!isFinite(bb[0][0])) { S.center.set(0, 0, 0); S.radius = 200; return; }
      S.center.set((bb[0][0] + bb[1][0]) / 2, (bb[0][1] + bb[1][1]) / 2, (bb[0][2] + bb[1][2]) / 2);
      S.radius = Math.max(1, 0.5 * Math.hypot(bb[1][0] - bb[0][0], bb[1][1] - bb[0][1], bb[1][2] - bb[0][2]));
      camera.near = S.radius / 200; camera.far = S.radius * 200; camera.updateProjectionMatrix();
    }
    function colorize(vals, min, max, cmap, nvert, tris, faceVals) {
      var col = new Float32Array(nvert * 3), c = [0, 0, 0], span = max - min || 1;
      for (var i = 0; i < nvert; i++) {
        var v = vals[i]; cmapColor(cmap, (v - min) / span, c);
        if (!(v === v)) { c[0] = c[1] = c[2] = 0.35; }
        col[3 * i] = c[0]; col[3 * i + 1] = c[1]; col[3 * i + 2] = c[2];
      }
      return col;
    }
    function rangeOf(vals) { var lo = Infinity, hi = -Infinity; for (var i = 0; i < vals.length; i++) { var v = vals[i]; if (v < lo) lo = v; if (v > hi) hi = v; } return [lo, hi]; }

    var api = {
      setScene: function (sc) {
        api.clearAll(); S.data = sc;
        var bb = [[Infinity, Infinity, Infinity], [-Infinity, -Infinity, -Infinity]];
        (sc.parts || []).forEach(function (p) {
          var pts = b64f32(p.points), tris = b64i32(p.triangles); if (!pts || !tris) return;
          var g = meshGeometry(pts, tris, null);
          var isProp = /prop/i.test(p.role || '');
          var m = new THREE.Mesh(g.geo, new THREE.MeshLambertMaterial({ color: 0x7a8693, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
            transparent: isProp, opacity: isProp ? 0.25 : 1, depthWrite: !isProp }));
          if (isProp) m.renderOrder = 2;
          m.userData = { part: p.name, role: p.role }; root.add(m); S.partMeshes[p.name] = m; bbox(pts, bb);
        });
        (sc.fields || []).forEach(function (f) { if (f.mesh === 'own' && f.points) bbox(b64f32(f.points), bb); });
        frame(bb); api.view('iso'); want();
      },
      clearAll: function () {
        Object.keys(S.partMeshes).forEach(function (k) { clear(S.partMeshes[k]); }); S.partMeshes = {};
        api.clearField(); api.clearLayers(); S.data = null;
      },
      clearField: function () {
        if (S.fieldMesh) { clear(S.fieldMesh); S.fieldMesh = null; }
        S.field = null; S.fieldCtx = null;
        Object.keys(S.partMeshes).forEach(function (k) { S.partMeshes[k].userData.hiddenByField = false; });
        api.clearMarker(); want();
      },
      showField: function (f, opts) {
        api.clearField(); if (!f) { applyVis(); return null; }
        S.field = f; S.opts = Object.assign({}, S.opts, opts || {});
        var pts, tris, host = null;
        if (f.mesh && f.mesh !== 'own' && S.partMeshes[f.mesh]) {
          host = S.data.parts.filter(function (p) { return p.name === f.mesh; })[0]; pts = b64f32(host.points); tris = b64i32(host.triangles);
        } else { pts = b64f32(f.points); tris = b64i32(f.triangles); }
        if (!pts || !tris) throw new Error('field "' + f.id + '" has no mesh');
        var vals = b64f32(f.values), disp = b64f32(f.displacement), nv = pts.length / 3, face = false;
        if (vals.length !== nv) {
          if (vals.length === tris.length / 3) face = true; else throw new Error('field "' + f.id + '": ' + vals.length + ' values do not fit ' + nv + ' points');
        }
        var g = meshGeometry(pts, tris, disp), geo = g.geo, ctx = { base: g.base, disp: g.disp, vals: vals, face: face, tris: tris };
        if (face) {   // one value per triangle: unshare vertices so every face carries its own colour
          var ng = geo.toNonIndexed(), pos = ng.getAttribute('position').array, nb = new Float32Array(pos.length), nd = g.disp ? new Float32Array(pos.length) : null;
          var ti = 0;
          for (var f3 = 0; f3 < tris.length / 3; f3++) for (var v3 = 0; v3 < 3; v3++, ti++) {
            var src = tris[3 * f3 + v3] * 3;
            for (var a2 = 0; a2 < 3; a2++) { nb[3 * ti + a2] = g.base[src + a2]; if (nd) nd[3 * ti + a2] = g.disp[src + a2]; }
          }
          geo = ng; ctx.base = nb; ctx.disp = nd; ctx.nvert = tris.length;
        } else ctx.nvert = nv;
        var rng = rangeOf(vals), min = S.opts.min != null ? S.opts.min : rng[0], max = S.opts.max != null ? S.opts.max : rng[1];
        S.opts.min = min; S.opts.max = max;
        ctx.colorVals = face ? (function () { var a = new Float32Array(tris.length); for (var q = 0; q < tris.length / 3; q++) { a[3 * q] = a[3 * q + 1] = a[3 * q + 2] = vals[q]; } return a; })() : vals;
        geo.setAttribute('color', new THREE.BufferAttribute(colorize(ctx.colorVals, min, max, S.opts.cmap, ctx.nvert), 3));
        var mesh = new THREE.Mesh(geo, new THREE.MeshLambertMaterial({ vertexColors: true, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1 }));
        mesh.userData = { field: f.id }; root.add(mesh); S.fieldMesh = mesh; S.fieldCtx = ctx;
        if (host && /prop/i.test(host.role || '')) { mesh.material.transparent = true; mesh.material.opacity = 0.25; mesh.material.depthWrite = false; }
        if (host && S.partMeshes[f.mesh]) S.partMeshes[f.mesh].userData.hiddenByField = true;
        api.setDeform(S.opts.deform || 0); applyVis(); want();
        return { min: min, max: max, dataMin: rng[0], dataMax: rng[1] };
      },
      recolor: function (cmap, min, max) {
        S.opts.cmap = cmap; S.opts.min = min; S.opts.max = max;
        if (S.fieldMesh) {
          S.fieldMesh.geometry.setAttribute('color', new THREE.BufferAttribute(colorize(S.fieldCtx.colorVals, min, max, cmap, S.fieldCtx.nvert), 3));
        }
        recolorLayers(); want();
      },
      setDeform: function (scale) {
        S.opts.deform = scale; var c = S.fieldCtx; if (!c || !S.fieldMesh) return;
        var pos = S.fieldMesh.geometry.getAttribute('position'), a = pos.array;
        for (var i = 0; i < a.length; i++) a[i] = c.base[i] + (c.disp ? c.disp[i] * scale : 0);
        pos.needsUpdate = true; S.fieldMesh.geometry.computeVertexNormals(); S.fieldMesh.geometry.computeBoundingSphere();
        if (S.field && S.field.mesh && S.partMeshes[S.field.mesh]) { /* host part is hidden while its field is shown */ }
        want();
      },
      hasDisplacement: function () { return !!(S.fieldCtx && S.fieldCtx.disp); },
      // layers: slices + streamlines
      clearLayers: function () {
        S.slices.forEach(function (s) { clear(s.mesh); }); S.slices = [];
        if (S.stream) { clear(S.stream.obj); S.stream = null; }
      },
      setLayers: function (sc) {
        api.clearLayers();
        (sc.slices || []).forEach(function (sl) {
          var pts = b64f32(sl.points), tris = b64i32(sl.triangles), vals = b64f32(sl.values); if (!pts || !tris || !vals) return;
          var g = meshGeometry(pts, tris, null), rng = rangeOf(vals);
          var mesh = new THREE.Mesh(g.geo, new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide, transparent: true, opacity: 0.92 }));
          g.geo.setAttribute('color', new THREE.BufferAttribute(new Float32Array(pts.length), 3));
          mesh.visible = false; root.add(mesh);
          S.slices.push({ mesh: mesh, vals: vals, n: pts.length / 3, info: { name: sl.name, label: sl.label, unit: sl.unit, min: rng[0], max: rng[1] } });
        });
        var st = sc.streamlines;
        if (st && st.points) {
          var sp = b64f32(st.points), off = b64i32(st.offsets), sv = b64f32(st.values), idx = [], n = sp.length / 3;
          var offs = Array.prototype.slice.call(off); if (offs[offs.length - 1] !== n) offs.push(n);
          for (var l = 0; l + 1 < offs.length; l++) for (var k = offs[l]; k + 1 < offs[l + 1]; k++) idx.push(k, k + 1);
          var geo = new THREE.BufferGeometry(), pos = new Float32Array(sp.length);
          for (var i = 0; i < n; i++) { var p = aToThree(sp[3 * i], sp[3 * i + 1], sp[3 * i + 2]); pos[3 * i] = p[0]; pos[3 * i + 1] = p[1]; pos[3 * i + 2] = p[2]; }
          geo.setAttribute('position', new THREE.BufferAttribute(pos, 3)); geo.setAttribute('color', new THREE.BufferAttribute(new Float32Array(pos.length), 3));
          geo.setIndex(new THREE.BufferAttribute(Uint32Array.from(idx), 1));
          var obj = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ vertexColors: true })); obj.visible = false; root.add(obj);
          var rg = sv ? rangeOf(sv) : [0, 1];
          S.stream = { obj: obj, vals: sv, n: n, info: { unit: st.unit, min: rg[0], max: rg[1] } };
        }
        recolorLayers(); want();
      },
      sliceInfo: function () { return S.slices.map(function (s) { return s.info; }); },
      streamInfo: function () { return S.stream ? S.stream.info : null; },
      setSliceVisible: function (i, on) { if (S.slices[i]) S.slices[i].mesh.visible = !!on; want(); },
      setStreamVisible: function (on) { if (S.stream) S.stream.obj.visible = !!on; want(); },
      setPartVisible: function (name, on) { var m = S.partMeshes[name]; if (m) { m.userData.visible = !!on; applyVis(); } },
      view: function (name) {
        var c = S.center, d = S.radius * 2.7, dir = { iso: [1, 0.75, 1.1], top: [-0.0005, 1, 0], front: [1, 0.02, 0], side: [0.0005, 0.02, 1], reset: [1, 0.75, 1.1] }[name] || [1, 0.75, 1.1];
        var L = Math.hypot(dir[0], dir[1], dir[2]);
        camera.position.set(c.x + dir[0] / L * d, c.y + dir[1] / L * d, c.z + dir[2] / L * d);
        controls.target.copy(c); controls.update(); camera.lookAt(c); want();
      },
      clearMarker: function () { if (S.marker) { clear(S.marker); S.marker = null; } },
      resize: resize,
      dispose: function () {
        S.dead = true; ro.disconnect(); try { api.clearAll(); renderer.dispose(); var x = renderer.getContext().getExtension('WEBGL_lose_context'); if (x) x.loseContext(); } catch (e) { /* ignore */ }
        if (renderer.domElement.parentNode) renderer.domElement.parentNode.removeChild(renderer.domElement);
      },
      canvas: renderer.domElement
    };
    function applyVis() {
      Object.keys(S.partMeshes).forEach(function (k) {
        var m = S.partMeshes[k]; m.visible = m.userData.visible !== false && !m.userData.hiddenByField;
      });
      if (S.fieldMesh) {
        var host = S.field && S.field.mesh; S.fieldMesh.visible = !(host && S.partMeshes[host] && S.partMeshes[host].userData.visible === false);
      }
      want();
    }
    function recolorLayers() {
      var f = S.field, c = [0, 0, 0];
      function fill(vals, n, info, attrOwner) {
        var lo = info.min, hi = info.max;
        if (f && f.unit && info.unit === f.unit && S.opts.min != null && S.opts.max != null) { lo = S.opts.min; hi = S.opts.max; }
        var col = attrOwner.geometry.getAttribute('color'), span = hi - lo || 1;
        for (var i = 0; i < n; i++) { cmapColor(S.opts.cmap, (vals[i] - lo) / span, c); col.array[3 * i] = c[0]; col.array[3 * i + 1] = c[1]; col.array[3 * i + 2] = c[2]; }
        col.needsUpdate = true;
      }
      S.slices.forEach(function (s) { fill(s.vals, s.n, s.info, s.mesh); });
      if (S.stream && S.stream.vals) fill(S.stream.vals, S.stream.n, S.stream.info, S.stream.obj);
    }
    function ndc(e) {
      var r = renderer.domElement.getBoundingClientRect();
      mouse.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1); raycaster.setFromCamera(mouse, camera);
    }
    function toAircraft(p) { return { x: p.x, y: -p.z, z: p.y }; }
    function bary(p, a, b, c) {
      var v0 = new THREE.Vector3().subVectors(b, a), v1 = new THREE.Vector3().subVectors(c, a), v2 = new THREE.Vector3().subVectors(p, a);
      var d00 = v0.dot(v0), d01 = v0.dot(v1), d11 = v1.dot(v1), d20 = v2.dot(v0), d21 = v2.dot(v1), den = d00 * d11 - d01 * d01 || 1;
      var v = (d11 * d20 - d01 * d21) / den, w = (d00 * d21 - d01 * d20) / den; return [1 - v - w, v, w];
    }
    function pick(e) {
      ndc(e);
      var targets = [];
      if (S.fieldMesh && S.fieldMesh.visible) targets.push(S.fieldMesh);
      S.slices.forEach(function (s) { if (s.mesh.visible) targets.push(s.mesh); });
      Object.keys(S.partMeshes).forEach(function (k) { if (S.partMeshes[k].visible) targets.push(S.partMeshes[k]); });
      var hit = raycaster.intersectObjects(targets, false)[0]; if (!hit) return null;
      var m = hit.object, res = { point: toAircraft(hit.point), source: 'part', name: m.userData.part || '' };
      var pos = m.geometry.getAttribute('position'), f = hit.face, idx = [f.a, f.b, f.c];
      function val(vals, faceVals) {
        if (faceVals) return vals[hit.faceIndex];
        var w = bary(hit.point, new THREE.Vector3().fromBufferAttribute(pos, f.a), new THREE.Vector3().fromBufferAttribute(pos, f.b), new THREE.Vector3().fromBufferAttribute(pos, f.c));
        return w[0] * vals[idx[0]] + w[1] * vals[idx[1]] + w[2] * vals[idx[2]];
      }
      if (m === S.fieldMesh) {
        res.source = 'field'; res.name = S.field.label || S.field.id; res.unit = S.field.unit;
        res.value = S.fieldCtx.face ? S.fieldCtx.vals[hit.faceIndex] : val(S.fieldCtx.vals, false);
      } else {
        var sl = S.slices.filter(function (s) { return s.mesh === m; })[0];
        if (sl) { res.source = 'slice'; res.name = sl.info.label || sl.info.name; res.unit = sl.info.unit; res.value = val(sl.vals, false); }
      }
      return res;
    }
    var down = null;
    renderer.domElement.addEventListener('pointerdown', function (e) { down = [e.clientX, e.clientY]; });
    renderer.domElement.addEventListener('pointerup', function (e) {
      if (down && Math.hypot(e.clientX - down[0], e.clientY - down[1]) < 4 && handlers.onPick) {
        var r = pick(e); api.clearMarker();
        if (r) {
          var sp = new THREE.Mesh(new THREE.SphereGeometry(S.radius / 120, 12, 8), new THREE.MeshBasicMaterial({ color: 0xffffff }));
          sp.position.set(r.point.x, r.point.z, -r.point.y); root.add(sp); S.marker = sp;
        }
        handlers.onPick(r); want();
      }
      down = null;
    });
    // test hook: lets a script confirm that something was drawn
    api.snapshot = function () { renderer.render(scene, camera); return renderer.domElement.toDataURL('image/png'); };
    api.stats = function () { return { parts: Object.keys(S.partMeshes).length, field: !!S.fieldMesh, slices: S.slices.length, stream: !!S.stream }; };
    return api;
  }
  window.HubViewer = { create: create, webglAvailable: webglAvailable, cmapColor: cmapColor, drawBar: drawBar, cmaps: Object.keys(CMAPS), b64f32: b64f32, b64i32: b64i32 };
})();
