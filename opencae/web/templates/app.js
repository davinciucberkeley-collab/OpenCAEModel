/* K09-L design review - front end.
   Data (written by opencae.web.build): K09L_SUMMARY, K09L_FIELD_INFO, K09L_EXTRA, K09L_AIRCRAFT, K09L_FIELDS, K09L_FLOW.
   Aircraft frame: mm, x aft, y right, z up. One WebGL viewer is shared by every section; each section sets its mode. */
(function () {
  "use strict";
  var S = window.K09L_SUMMARY || {}, FI = window.K09L_FIELD_INFO || {}, X = window.K09L_EXTRA || {},
      AC = window.K09L_AIRCRAFT, FD = window.K09L_FIELDS || {}, FL = window.K09L_FLOW || {};
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var esc = function (t) { return String(t == null ? "" : t).replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); };
  var fmt = function (v, nd) { return v == null || v === "" || isNaN(v) ? "—" : Number(v).toLocaleString("en-US", {minimumFractionDigits: nd || 0, maximumFractionDigits: nd || 0}); };
  var auto = function (v) { if (v == null || isNaN(v)) return "—"; var a = Math.abs(v); return fmt(v, a >= 100 ? 0 : a >= 10 ? 1 : a >= 1 ? 2 : 3); };
  var ST = {pass: ["pass", "✓", "Pass"], cond: ["cond", "◐", "Conditional"], fail: ["fail", "✕", "Fail"], nev: ["nev", "○", "Not evaluated"],
            ok: ["ok", "✓", "OK"], watch: ["watch", "◐", "Watch"], current: ["current", "✓", "Current"], stale: ["stale", "◐", "Out of date"],
            "not run": ["not-run", "○", "Not run"], failed: ["failed", "✕", "Run failed"],
            GO: ["go", "✓", "GO"], "GO WITH CONDITIONS": ["gwc", "◐", "GO with conditions"], "NO-GO": ["nogo", "✕", "NO-GO"], "NOT READY": ["nr", "○", "Not ready"]};
  var chip = function (s, label) { var d = ST[s] || ST[norm(s)] || ST.nev; return '<span class="chip ' + d[0] + '"><i>' + d[1] + "</i>" + esc(label || d[2]) + "</span>"; };
  function norm(s) {
    s = String(s || "").toLowerCase();
    if (s === "pass" || s === "ok" || s === "current") return s;
    if (s.indexOf("fail") === 0 || s === "no-go") return "fail";
    if (s.indexOf("cond") === 0 || s === "watch" || s === "stale") return s.indexOf("cond") === 0 ? "cond" : s;
    return "nev";
  }
  var RANK = {fail: 4, failed: 4, watch: 3, cond: 3, stale: 2, nev: 1, "not run": 1, ok: 0, pass: 0, current: 0};
  var STATUS_RGB = {ok: [0.20, 0.58, 0.33], pass: [0.20, 0.58, 0.33], watch: [0.90, 0.64, 0.08], cond: [0.90, 0.64, 0.08], fail: [0.82, 0.21, 0.17], nev: [0.66, 0.70, 0.74]};
  function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }

  function b64(s, Type) {
    var bin = atob(s), n = bin.length, u8 = new Uint8Array(n);
    for (var i = 0; i < n; i++) u8[i] = bin.charCodeAt(i);
    return new Type(u8.buffer);
  }

  /* ------------------------------------------------------------------ colour maps */
  var MAPS = {
    turbo: [[0.19, 0.07, 0.23], [0.27, 0.42, 0.89], [0.16, 0.73, 0.91], [0.22, 0.93, 0.56], [0.65, 0.99, 0.23], [0.96, 0.82, 0.18], [0.98, 0.49, 0.13], [0.80, 0.16, 0.03], [0.48, 0.02, 0.01]],
    ylorrd: [[1.00, 1.00, 0.80], [1.00, 0.93, 0.63], [1.00, 0.85, 0.46], [1.00, 0.70, 0.30], [0.99, 0.55, 0.24], [0.99, 0.31, 0.16], [0.89, 0.10, 0.11], [0.74, 0.00, 0.15], [0.50, 0.00, 0.15]],
    coolwarm: [[0.23, 0.30, 0.75], [0.48, 0.62, 0.97], [0.75, 0.83, 0.96], [0.87, 0.87, 0.87], [0.96, 0.77, 0.68], [0.92, 0.49, 0.38], [0.71, 0.02, 0.15]],
    viridis: [[0.27, 0.00, 0.33], [0.28, 0.17, 0.48], [0.23, 0.32, 0.55], [0.17, 0.45, 0.56], [0.13, 0.57, 0.55], [0.12, 0.69, 0.51], [0.32, 0.79, 0.39], [0.63, 0.86, 0.22], [0.99, 0.91, 0.14]],
    oranges: [[0.99, 0.93, 0.85], [0.99, 0.82, 0.64], [0.99, 0.66, 0.40], [0.95, 0.47, 0.18], [0.80, 0.30, 0.04], [0.50, 0.15, 0.02]]
  };
  function cmap(name, t) {
    var m = MAPS[name], x = Math.min(Math.max(t, 0), 1) * (m.length - 1), i = Math.min(Math.floor(x), m.length - 2), f = x - i;
    return [m[i][0] + (m[i + 1][0] - m[i][0]) * f, m[i][1] + (m[i + 1][1] - m[i][1]) * f, m[i][2] + (m[i + 1][2] - m[i][2]) * f];
  }
  function mapFor(disc) { return disc === "fluid" ? "coolwarm" : disc === "thermal" ? "ylorrd" : "turbo"; }
  function gradientCss(name) { return "linear-gradient(90deg," + MAPS[name].map(function (c) { return "rgb(" + c.map(function (v) { return Math.round(v * 255); }).join(",") + ")"; }).join(",") + ")"; }
  function rgbCss(c) { return "rgb(" + c.map(function (v) { return Math.round(v * 255); }).join(",") + ")"; }

  /* ------------------------------------------------------------------ the common viewer */
  var V = (function () {
    var el = $("#viewer"), w = el.clientWidth || 800, h = el.clientHeight || 600;
    var renderer = new THREE.WebGLRenderer({antialias: true, alpha: true, logarithmicDepthBuffer: true, preserveDrawingBuffer: true});
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(w, h);
    el.appendChild(renderer.domElement);
    var scene = new THREE.Scene();
    var camera = new THREE.PerspectiveCamera(35, w / h, 1, 5e6);
    camera.up.set(0, 0, 1);
    camera.position.set(-900, -1250, 650);
    var controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true; controls.autoRotateSpeed = 0.5;
    scene.add(new THREE.HemisphereLight(0xffffff, 0x8a99a8, 0.85));
    var d = new THREE.DirectionalLight(0xffffff, 0.75); d.position.set(-1, -1.4, 2.2); scene.add(d);
    var d2 = new THREE.DirectionalLight(0xffffff, 0.3); d2.position.set(1.2, 1, -0.5); scene.add(d2);
    var ticks = [], last = performance.now();
    function resize() { var W = el.clientWidth, H = el.clientHeight; if (!W || !H) return; renderer.setSize(W, H); camera.aspect = W / H; camera.updateProjectionMatrix(); }
    new ResizeObserver(resize).observe(el);
    (function loop(now) {
      var dt = Math.min((now - last) / 1000, 0.1); last = now;
      ticks.forEach(function (f) { f(dt); });
      controls.update();
      renderer.render(scene, camera);
      requestAnimationFrame(loop);
    })(last);
    function frame(obj, dir, k) {
      var box = new THREE.Box3().setFromObject(obj); if (box.isEmpty()) return;
      var c = box.getCenter(new THREE.Vector3()), r = box.getSize(new THREE.Vector3()).length() / 2;
      dir = (dir || new THREE.Vector3(-0.55, -0.75, 0.42)).clone().normalize();
      var dist = r / Math.tan(camera.fov * Math.PI / 360) * (k || 1.05);
      controls.target.copy(c);
      camera.position.copy(c.clone().add(dir.multiplyScalar(dist)));
      camera.updateProjectionMatrix();
    }
    function pick(ev, objs) {
      var r = renderer.domElement.getBoundingClientRect();
      var m = new THREE.Vector2((ev.clientX - r.left) / r.width * 2 - 1, -(ev.clientY - r.top) / r.height * 2 + 1);
      var rc = new THREE.Raycaster(); rc.setFromCamera(m, camera);
      var hit = rc.intersectObjects(objs.filter(function (o) { return o.visible; }), true);
      for (var i = 0; i < hit.length; i++) { var o = hit[i].object, vis = true; while (o) { if (!o.visible) vis = false; o = o.parent; } if (vis) return hit[i]; }
      return null;
    }
    // click (not drag) and hover hooks for the active mode
    var down = null, onClick = null, onHover = null, tip = $("#tip");
    renderer.domElement.addEventListener("pointerdown", function (e) { down = [e.clientX, e.clientY]; });
    renderer.domElement.addEventListener("pointerup", function (e) {
      if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 5) return;
      if (onClick) onClick(e);
    });
    renderer.domElement.addEventListener("pointermove", function (e) {
      var t = onHover ? onHover(e) : null;
      if (!t) { tip.hidden = true; return; }
      var r = el.getBoundingClientRect();
      tip.textContent = t; tip.style.left = Math.min(e.clientX - r.left + 12, r.width - 200) + "px"; tip.style.top = (e.clientY - r.top + 14) + "px"; tip.hidden = false;
    });
    renderer.domElement.addEventListener("pointerleave", function () { tip.hidden = true; });
    return {el: el, renderer: renderer, scene: scene, camera: camera, controls: controls, ticks: ticks, frame: frame, pick: pick, resize: resize,
            setClick: function (f) { onClick = f; }, setHover: function (f) { onHover = f; }};
  })();

  /* ------------------------------------------------------------------ aircraft (built once) */
  var COMP = {};
  (X.components || []).forEach(function (c) { COMP[c.key] = c; });
  function partStatus(key) {
    var c = COMP[key]; if (c && c.status) return norm(c.status) === "pass" ? "ok" : norm(c.status);
    var p = AC.parts[key], worst = null;
    ((p && p.analyses) || []).forEach(function (id) { var f = FI[id]; if (f && f.failure && (!worst || RANK[f.failure.status] > RANK[worst])) worst = f.failure.status; });
    return worst || "nev";
  }
  var AIR = (function () {
    var g = new THREE.Group(), meshes = [];
    var rotors = AC.rotors.map(function (r) {
      var pivot = new THREE.Group(); pivot.position.set(r.origin[0], r.origin[1], r.origin[2]);
      var inner = new THREE.Group(); inner.position.set(-r.origin[0], -r.origin[1], -r.origin[2]);
      pivot.add(inner); g.add(pivot);
      return {pivot: pivot, inner: inner, axis: new THREE.Vector3(r.axis[0], r.axis[1], r.axis[2]).normalize(), sense: r.sense, angle: 0};
    });
    var ev = (X.explode && X.explode.body_vectors) || [];
    var edgeMat = new THREE.LineBasicMaterial({color: 0x1b2630, transparent: true, opacity: 0.28});
    AC.bodies.forEach(function (b, i) {
      var geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(b64(b.pos, Float32Array), 3));
      geo.setIndex(new THREE.BufferAttribute(b64(b.idx, b.i32 ? Uint32Array : Uint16Array), 1));
      geo.computeVertexNormals();
      var base = b.rgb.map(function (v) { return v / 255; });
      var mat = new THREE.MeshStandardMaterial({color: new THREE.Color(base[0], base[1], base[2]), roughness: 0.62, metalness: 0.06, side: THREE.DoubleSide, transparent: true, opacity: 1});
      var mesh = new THREE.Mesh(geo, mat);
      mesh.userData = {part: b.part, body: b.name, base: base, ex: ev[i] || [0, 0, 0], i: i};
      var eg = new THREE.LineSegments(new THREE.EdgesGeometry(geo, 32), edgeMat); eg.name = "edges"; mesh.add(eg);  // CAD-style feature edges
      meshes.push(mesh);
      (b.rotor != null ? rotors[b.rotor].inner : g).add(mesh);
    });
    var spinRate = 1;
    V.ticks.push(function (dt) {
      if (!g.visible || !spinRate) return;
      var rpm = AC.hover_rpm || [6500, 6500, 6500, 6500], k = AC.spin_display || 0.025;
      rotors.forEach(function (r, i) {
        r.angle += (rpm[i] || rpm[0]) / 60 * 2 * Math.PI * k * dt * r.sense * spinRate;
        r.pivot.quaternion.setFromAxisAngle(r.axis, r.angle);
      });
    });
    V.scene.add(g);
    return {
      group: g, meshes: meshes, rotors: rotors,
      setSpin: function (s) { spinRate = s; },
      setRotorAngles: function (a) { rotors.forEach(function (r, i) { r.pivot.quaternion.setFromAxisAngle(r.axis, (a[i] || 0) * r.sense); }); },
      paint: function (fn) {   // fn(mesh.userData) -> [r,g,b] or null (base colour); opacity via fn2
        meshes.forEach(function (m) { var c = fn ? fn(m.userData) : null; c = c || m.userData.base; m.material.color.setRGB(c[0], c[1], c[2]); });
      },
      fade: function (keep) {  // keep: function(userData) -> true keeps full opacity
        meshes.forEach(function (m) { var k = !keep || keep(m.userData); m.material.opacity = k ? 1 : 0.1; m.material.depthWrite = k; var e = m.getObjectByName("edges"); if (e) e.visible = k; });
      },
      edges: function (on) { meshes.forEach(function (m) { var e = m.getObjectByName("edges"); if (e) e.visible = on; }); },
      explode: function (f) { meshes.forEach(function (m) { var e = m.userData.ex; m.position.set(e[0] * f, e[1] * f, e[2] * f); }); },
      reset: function () {
        this.paint(null); this.fade(null); this.explode(0); this.edges(true); g.visible = true; g.matrixAutoUpdate = true;
        g.position.set(0, 0, 0); g.quaternion.identity(); g.scale.setScalar(1); g.updateMatrix(); spinRate = 1;
      }
    };
  })();

  /* ------------------------------------------------------------------ viewer mode plumbing */
  var layers = {};      // named groups that only one mode shows
  function layer(name) { if (!layers[name]) { layers[name] = new THREE.Group(); layers[name].visible = false; V.scene.add(layers[name]); } return layers[name]; }
  function showLayers(names) { Object.keys(layers).forEach(function (k) { layers[k].visible = names.indexOf(k) >= 0; }); }
  function clear(grp) { while (grp.children.length) { var c = grp.children.pop(); c.traverse(function (o) { if (o.geometry) o.geometry.dispose(); if (o.material) [].concat(o.material).forEach(function (m) { if (m.map) m.map.dispose(); m.dispose(); }); }); } }
  function tools(html) { $("#vtools").innerHTML = html || ""; }
  function caption(t) { $("#vcaption").textContent = t || ""; }
  function legend(html) { var l = $("#legend"); l.innerHTML = html || ""; l.hidden = !html; }
  var flightTick = null;
  function enterMode(name) {
    AIR.reset(); showLayers([]); tools(""); caption(""); legend(""); $("#timeline").hidden = true;
    var hud = $("#hud"); if (hud) hud.remove(); var ft = $("#ftag"); if (ft) ft.remove();
    V.controls.autoRotate = false; V.setClick(null); V.setHover(null);
    V.scene.background = null; V.scene.fog = null;
    if (flightTick) { flightTick.stop(); flightTick = null; }
    V.camera.near = 1; V.camera.updateProjectionMatrix(); $(".stage").classList.remove("has-tl");
    return name;
  }
  function seg(id, items, cur) {
    return '<div class="seg" role="group" id="' + id + '">' + items.map(function (it) {
      return '<button data-v="' + it[0] + '" aria-pressed="' + (it[0] === cur) + '">' + esc(it[1]) + "</button>";
    }).join("") + "</div>";
  }
  function onSeg(id, fn) {
    $$("#" + id + " button").forEach(function (b) {
      b.addEventListener("click", function () { $$("#" + id + " button").forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); }); fn(b.dataset.v); });
    });
  }
  function hoverPart(e) {
    var h = V.pick(e, AIR.meshes); if (!h) return null;
    var p = AC.parts[h.object.userData.part], c = COMP[h.object.userData.part];
    return (p ? p.name : h.object.userData.body) + (c && c.status ? "  ·  " + (ST[norm(c.status)] || ST.nev)[2] : "");
  }
  function label(text, pos, color, size) {
    var cv = document.createElement("canvas"), ctx = cv.getContext("2d"), fs = 44;
    ctx.font = "600 " + fs + "px IBM Plex Sans, Segoe UI, sans-serif";
    var w = Math.ceil(ctx.measureText(text).width) + 28; cv.width = w; cv.height = fs + 22;
    ctx.font = "600 " + fs + "px IBM Plex Sans, Segoe UI, sans-serif";
    ctx.fillStyle = "rgba(255,255,255,0.92)"; ctx.strokeStyle = color || "#333"; ctx.lineWidth = 4;
    ctx.beginPath(); if (ctx.roundRect) ctx.roundRect(2, 2, w - 4, fs + 18, 12); else ctx.rect(2, 2, w - 4, fs + 18); ctx.fill(); ctx.stroke();
    ctx.fillStyle = "#14202a"; ctx.textBaseline = "middle"; ctx.fillText(text, 14, (fs + 22) / 2 + 1);
    var tex = new THREE.CanvasTexture(cv); tex.minFilter = THREE.LinearFilter;
    var sp = new THREE.Sprite(new THREE.SpriteMaterial({map: tex, depthTest: false, transparent: true}));
    var s = size || 30; sp.scale.set(s * w / (fs + 22), s, 1); sp.position.set(pos[0], pos[1], pos[2]); sp.renderOrder = 10;
    return sp;
  }

  /* ------------------------------------------------------------------ shared text helpers */
  function failureMain(id, f) {
    var fs = f.failure || {};
    if (id === "cfd_cruise") return [fmt((fs.safety_factor || 0) * 100, 0) + " %", "of the lift level cruise needs"];
    if (id === "thermal_mount" || (fs.margin != null && fs.safety_factor == null)) return [fmt(fs.margin, 1) + " " + (f.discipline === "thermal" ? "K" : (fs.unit || "")), f.discipline === "thermal" ? "temperature margin" : "margin"];
    return [fmt(fs.safety_factor, 2), "safety factor (watch below " + fmt(fs.line || 1.5, 1) + ")"];
  }
  function shortResult(f) {
    var fs = f.failure || {};
    if (f.discipline === "thermal") return fmt(fs.value, 1) + " °C vs " + fmt(fs.allowable, 0) + " °C limit · margin " + fmt(fs.margin, 1) + " K";
    if (fs.safety_factor == null) return fs.text ? fs.text.slice(0, 90) : "";
    if (f.id === "cfd_cruise") return "Lift at trim " + fmt((fs.safety_factor || 0) * 100, 0) + " % of required · L/D " + fmt((f.extra.cl || 0) / (f.extra.cd || 1), 1);
    return "SF " + fmt(fs.safety_factor, fs.safety_factor < 10 ? 2 : 1) + " · " + fmt(fs.value, 1) + " vs " + fmt(fs.allowable, 0) + " " + (fs.unit || "");
  }
  function sysResult(a) {
    var s = a.summary || {};
    if (a.id === "assembly_check") return (s.interferences != null ? s.interferences + " interference · " : "") + (s.prop_clearance_mm != null ? "props clear by " + fmt(s.prop_clearance_mm, 1) + " mm" : "");
    if (a.id === "flight_hover" && s.max_position_error_mm != null) return "holds position within " + fmt(s.max_position_error_mm, 1) + " mm";
    if (a.id === "flight_transition" && s.height_loss_m != null) return "loses " + fmt(s.height_loss_m, 2) + " m in the transition";
    return a.reason || "";
  }
  function remediesFor(pred) { return (X.remedies || []).filter(pred); }
  function issueHtml(r) {
    var st = norm(r.status);
    return '<div class="issue ' + st + '"><div class="hd">' + chip(st) + "<b>" + esc(r.title) + '</b><span class="muted small">' + esc(r.kind) + "</span></div>" +
      '<p class="small">' + esc(r.problem) + "</p>" + (r.actions && r.actions.length ? "<ol>" + r.actions.map(function (a) {
        return "<li>" + esc(a.action) + (a.effort ? '<span class="effort">' + esc(a.effort) + " effort</span>" : "") + (a.effect ? "<br><span>" + esc(a.effect) + "</span>" : "") +
          (a.verify_by ? '<br><span class="muted">Verify: ' + esc(a.verify_by) + "</span>" : "") + "</li>";
      }).join("") + "</ol>" : "") +
      (r.parts && r.parts.length ? '<div><button class="btn" data-show-parts="' + esc(r.parts.join("|")) + '">Show on the model</button></div>' : "") + "</div>";
  }
  function wireShowParts(root) {
    $$("[data-show-parts]", root).forEach(function (b) {
      b.addEventListener("click", function () {
        var keys = b.dataset.showParts.split("|");
        if (currentView !== "overview" && currentView !== "decisions" && currentView !== "components") location.hash = "decisions";
        highlightParts(keys);
      });
    });
  }
  function highlightParts(keys) {
    var set = {}; keys.forEach(function (k) { set[k] = 1; });
    AIR.explode(0); AIR.group.visible = true; showLayers([]);
    AIR.paint(function (u) { return set[u.part] ? [0.89, 0.38, 0.10] : [0.78, 0.81, 0.84]; });
    AIR.fade(function (u) { return !!set[u.part]; });
    var objs = AIR.meshes.filter(function (m) { return set[m.userData.part]; });
    if (objs.length) { var g = new THREE.Group(); var box = new THREE.Box3(); objs.forEach(function (o) { box.expandByObject(o); });
      var helper = new THREE.Box3Helper(box); g.add(helper); V.frame(helper, null, 2.2); }
    caption("Highlighted: " + keys.map(function (k) { return AC.parts[k] ? AC.parts[k].name.split(" — ")[0] : k; }).join(", "));
  }

  /* ================================================================== OVERVIEW */
  function verdictInfo() {
    var v = S.verdict || {fail: 0, cond: 0, pass: 0, nev: 0};
    var bad = (S.analyses || []).filter(function (a) { return a.status === "current" && a.verdict === "fail"; });
    var cls = v.fail || bad.length ? "fail" : (v.cond || v.nev ? "watch" : "ok");
    var headline = v.fail ? v.fail + " requirement" + (v.fail > 1 ? "s" : "") + " fail" :
      bad.length ? "No requirement fails, but " + bad.length + " analysis flags a problem" : "No requirement fails";
    return {v: v, bad: bad, cls: cls, headline: headline};
  }
  function screeningLine() {
    var ids = Object.keys(FI).filter(function (id) { return FI[id].group === "component"; });
    var comps = X.components || [], c = {fail: 0, watch: 0, ok: 0, nev: 0};
    comps.forEach(function (k) { c[partStatus(k.key)] = (c[partStatus(k.key)] || 0) + 1; });
    return '<p class="small" style="margin-top:10px"><b>' + comps.length + " components evaluated</b>: " + ids.length + " with their own FEA contour, the rest by rating, mass or wiring checks. " +
      chip("fail", c.fail + " fail") + " " + chip("watch", c.watch + " watch") + " " + chip("ok", c.ok + " ok") + (c.nev ? " " + chip("nev", c.nev + " not evaluated") : "") + ' <a href="#components">Components →</a></p>';
  }
  function viewOverview() {
    enterMode("overview");
    V.controls.autoRotate = true;
    V.frame(AIR.group);
    V.setHover(hoverPart);
    V.setClick(function (e) { var h = V.pick(e, AIR.meshes); if (h) { location.hash = "components"; setTimeout(function () { openComponent(h.object.userData.part); }, 30); } });
    var rpm = AC.hover_rpm ? Math.round(AC.hover_rpm.reduce(function (a, b) { return a + b; }, 0) / AC.hover_rpm.length) : null;
    caption("Drag to rotate, scroll to zoom. Props turn at 1/" + Math.round(1 / (AC.spin_display || 0.025)) + " of their simulated hover speed" + (rpm ? " (" + rpm.toLocaleString() + " rpm)" : "") + ". Click a part for its checks.");
    if (built.overview) return;
    var vi = verdictInfo(), v = vi.v;
    var next = v.fail ? "Fix the failing items before building." : vi.bad.length ? "Resolve or disprove " + vi.bad.map(function (a) { return a.label; }).join(", ") + " before building." : "Ready to build and test.";
    var gates = X.gates || S.gates || [];
    var issues = (X.remedies || []).filter(function (r) { return norm(r.status) === "fail"; });
    var watch = (X.remedies || []).filter(function (r) { return norm(r.status) !== "fail"; });
    var cost = X.cost || {};
    var h = '<div class="stack">' +
      '<div class="card verdict ' + vi.cls + '"><h1>' + esc(vi.headline) + "</h1><p>" + esc(S.variant) + ". " + esc(next) +
      " Not approved to fly: " + v.cond + " requirements are predictions on estimated data that the tests must confirm.</p>" +
      '<div class="counts">' + chip("pass", v.pass + " pass") + chip("cond", v.cond + " conditional") + chip("fail", v.fail + " fail") + (v.nev ? chip("nev", v.nev + " not evaluated") : "") + "</div>" +
      (gates.length ? '<h4>Readiness gates</h4><div class="gates-row">' + gates.map(function (g) { return '<div class="' + norm(g.status) + '" title="' + esc(g.id + " " + g.name + ": " + (g.decision || g.status)) + '"></div>'; }).join("") + "</div>" +
        '<p class="small muted">' + gates.map(function (g) { return g.id + " " + (g.decision ? (ST[g.decision] || [0, 0, g.decision])[2] : (ST[norm(g.status)] || ST.nev)[2]); }).join(" · ") + ' · <a href="#decisions">gate decisions →</a></p>' : "") + "</div>" +
      '<div class="kpis">' + (S.kpis || []).map(function (k) {
        return '<div class="kpi"><span>' + esc(k.label) + "</span><b>" + (k.unit === "USD" ? "$" : "") + fmt(k.value, k.unit === "" ? 3 : (k.unit === "°" || k.unit === "min" ? 1 : 0)) +
          (k.unit && k.unit !== "USD" ? ' <span class="small">' + esc(k.unit) + "</span>" : "") + "</b><small>" + esc(k.limit) + " " + chip(norm(k.status) === "nev" ? k.status : k.status, k.req) + "</small></div>";
      }).join("") + "</div>" +
      '<div class="card"><h3>What has to change</h3>' + (issues.length ? '<div class="stack">' + issues.slice(0, 3).map(issueHtml).join("") + "</div>" : '<p class="small">Nothing fails. ' + watch.length + " items are conditional; see Decisions.</p>") +
      (watch.length ? '<p class="small" style="margin-top:8px"><a href="#decisions">' + watch.length + " more items to watch, each with suggested actions →</a></p>" : "") + "</div>" +
      '<div class="card"><h3>Analyses</h3><div class="thumbs">' + Object.keys(FI).filter(function (id) { return FI[id].group !== "component"; }).map(function (id) {
        var f = FI[id], fs = f.failure || {};
        return '<button class="thumb" data-field="' + id + '">' + (f.thumb ? '<img src="' + f.thumb + '" alt="">' : "") + "<div>" + chip(fs.status) + "<b>" + esc(f.title) + '</b><span class="muted">' + esc(shortResult(f)) + "</span></div></button>";
      }).join("") + "</div>" + screeningLine() +
      '<ul class="list" style="margin-top:8px">' + (S.analyses || []).filter(function (a) { return !a.has_field; }).map(function (a) {
        return '<li class="row" style="cursor:default">' + chip(a.status) + '<span class="grow"><b>' + esc(a.label) + "</b><span>" + esc(sysResult(a)) + "</span></span></li>";
      }).join("") + "</ul></div>" +
      (cost.total_usd != null ? '<div class="card"><h3>Cost</h3><p class="small"><b class="num">$' + fmt(cost.total_usd) + "</b> of a $" + fmt(cost.budget_usd) + " budget · reserve $" + fmt(cost.reserve_usd) +
        ' · <a href="#components">breakdown by subsystem →</a></p></div>' : "") +
      '<div class="card"><h3>What changed from ' + esc(S.baseline) + '</h3><ol style="margin:0;padding-left:20px;display:grid;gap:6px;font-size:13px">' + (S.changes || []).map(function (c) {
        return "<li><b>" + esc(c.title) + '</b><br><span class="muted">' + esc(c.why.split(". ")[0]) + ". (" + esc(c.requirement) + ")</span></li>";
      }).join("") + "</ol></div>" +
      '<div class="card"><h3>Before the first flight</h3><ul style="margin:0;padding-left:18px;font-size:13px;display:grid;gap:4px">' + (S.limits || []).map(function (l) { return "<li>" + esc(l.split(":")[0]) + "</li>"; }).join("") + "</ul></div>" +
      '<div class="card"><h3>Variants</h3><div class="tw"><table><thead><tr><th>Variant</th><th>Pass</th><th>Cond.</th><th>Fail</th><th>Mass</th><th>Tip-over</th><th>Analyses</th></tr></thead><tbody>' +
      (S.variants || []).map(function (r) {
        return '<tr class="' + (r.selected ? "sel" : "") + '"><td><b>' + esc(r.name) + "</b>" + (r.baseline ? ' <span class="muted small">baseline</span>' : "") + (r.selected ? ' <span class="muted small">shown</span>' : "") +
          '</td><td class="num">' + r.counts.pass + '</td><td class="num">' + r.counts.cond + '</td><td class="num">' + (r.counts.fail ? '<b style="color:var(--fail)">' + r.counts.fail + "</b>" : 0) +
          '</td><td class="num">' + fmt(r.mass_g) + ' g</td><td class="num">' + fmt(r.tipover, 1) + '°</td><td class="num">' + r.analyses_current + "/" + (S.analyses || []).length + "</td></tr>";
      }).join("") + "</tbody></table></div></div></div>";
    $("#pane-overview").innerHTML = h;
    $$("#pane-overview [data-field]").forEach(function (b) { b.addEventListener("click", function () { location.hash = "analyses"; setTimeout(function () { openField(b.dataset.field); }, 30); }); });
    wireShowParts($("#pane-overview"));
  }

  /* ================================================================== DESIGN (exploded, wiring, fabrication, cost) */
  var designMode = "assembly", fabIndex = -1, explodeF = 0;
  function viewDesign() {
    enterMode("design");
    V.setHover(hoverPart);
    tools(seg("dmode", [["assembly", "Assembled"], ["exploded", "Exploded"], ["wiring", "Electrical routing"], ["fabrication", "Fabrication"], ["cost", "Cost map"], ["status", "Check status"], ["drawings", "Drawings"]], designMode) +
      '<label class="ctl" id="exctl"' + (designMode === "exploded" ? "" : " hidden") + '>Explode <input type="range" id="exf" min="0" max="1.6" step="0.01" value="' + (explodeF || 0.8) + '"></label>');
    onSeg("dmode", function (m) { designMode = m; drawDesign(); });
    $("#exf").addEventListener("input", function (e) { explodeF = +e.target.value; AIR.explode(explodeF); });
    drawDesign(true);
  }
  function drawDesign(first) {
    AIR.reset(); showLayers([]); legend(""); $("#exctl").hidden = designMode !== "exploded";
    V.setClick(function (e) { var h = V.pick(e, AIR.meshes); if (h) showPartCard(h.object.userData.part); });
    var pane = $("#pane-design");
    if (designMode === "assembly") {
      caption("Assembled aircraft as imported from the CAD, with revision changes applied. Click a part.");
      pane.innerHTML = designIntro() + subsystemList();
    } else if (designMode === "exploded") {
      explodeF = explodeF || 0.8; AIR.setSpin(0); AIR.explode(explodeF);
      caption("Exploded view: parts move apart along their subassembly directions. Drag the slider.");
      pane.innerHTML = '<div class="card"><h3>Exploded view</h3><p class="small">Each part moves away from the aircraft along the direction its subassembly is assembled from. Use it to see what sits inside the pod, how the motor sets stack and where the fasteners go.</p></div>' + subsystemList();
    } else if (designMode === "wiring") {
      drawWiring(); pane.innerHTML = wiringPanel();
    } else if (designMode === "fabrication") {
      pane.innerHTML = fabricationPanel(); wireFab(); if (fabIndex >= 0) showFab(fabIndex); else caption("Pick a fabrication or assembly step to see its parts.");
    } else if (designMode === "cost") {
      var max = 0; (X.components || []).forEach(function (c) { if (c.cost_usd > max) max = c.cost_usd; });
      AIR.paint(function (u) { var c = COMP[u.part]; return c && c.cost_usd != null ? cmap("oranges", Math.sqrt(c.cost_usd / (max || 1))) : [0.85, 0.87, 0.89]; });
      legend('<span>Part cost (each line)</span><span class="num">$0</span><span class="bar" style="background:' + gradientCss("oranges") + '"></span><span class="num">$' + fmt(max) + "</span>");
      caption("Parts coloured by their BOM cost (square-root scale). Grey: no cost on record.");
      pane.innerHTML = costPanel();
    } else if (designMode === "drawings") {
      caption("Drawing sheets of this design. Click a sheet to open it full screen; scroll to zoom there.");
      pane.innerHTML = '<div class="card"><h3>Drawings</h3><p class="small">Sheets generated from the same engineering model as the 3D view (A3, mm).</p><div class="thumbs" style="margin-top:8px">' +
        (S.drawings || []).map(function (d) { return '<button class="thumb" data-drawing="' + esc(d.src) + '"><img src="' + esc(d.src) + '" alt="' + esc(d.title) + '" style="object-fit:contain"><div><b>' + esc(d.title) + "</b></div></button>"; }).join("") + "</div></div>";
      $$("[data-drawing]", pane).forEach(function (b) { b.addEventListener("click", function () { openDrawing(b.dataset.drawing, b.textContent); }); });
    } else if (designMode === "status") {
      AIR.paint(function (u) { return STATUS_RGB[partStatus(u.part)] || STATUS_RGB.nev; });
      legend(["ok", "watch", "fail", "nev"].map(function (s) { return '<span class="sw"><i style="background:' + rgbCss(STATUS_RGB[s]) + '"></i>' + (ST[s] || ST.nev)[2] + "</span>"; }).join(""));
      caption("Every part coloured by its worst check. Grey: no analysis covers the part (mass and position still feed the level-1 checks).");
      pane.innerHTML = subsystemList(true);
    }
    wireParts(pane); wireShowParts(pane);
    if (first) V.frame(AIR.group, null, designMode === "exploded" ? 1.5 : 1.05);
  }
  function designIntro() {
    var n = Object.keys(AC.parts).length, b = AC.bodies.length;
    return '<div class="card"><h3>The design</h3><p class="small">' + esc(S.variant) + ": " + n + " BOM lines, " + b + " bodies. Configuration: " +
      esc(Object.keys(S.config || {}).map(function (k) { return k + " " + S.config[k]; }).join(" · ")) + ".</p></div>";
  }
  function subsystems() {
    var g = {};
    (X.components || Object.keys(AC.parts).map(function (k) { var p = AC.parts[k]; return {key: k, name: p.name, subsystem: p.group, qty: p.qty, mass_g: p.mass_g}; })).forEach(function (c) {
      (g[c.subsystem || "Other"] = g[c.subsystem || "Other"] || []).push(c);
    });
    return g;
  }
  function subsystemList(withStatus) {
    var g = subsystems();
    return '<div class="card"><h3>Subsystems</h3>' + Object.keys(g).sort().map(function (s) {
      var items = g[s], mass = items.reduce(function (a, c) { return a + (c.mass_g || 0) * (c.qty || 1); }, 0);
      return "<details" + (withStatus ? " open" : "") + "><summary>" + esc(s) + ' <span class="muted small">· ' + items.length + " parts · " + fmt(mass, 0) + " g</span></summary><ul class=\"list\">" +
        items.map(function (c) { return '<li><button class="row" data-part="' + esc(c.key) + '"><span class="dot ' + partStatus(c.key) + '"></span><span class="grow"><b>' + esc(c.name.split(" — ")[0]) + "</b><span>" + esc((c.qty || 1) + " × " + fmt(c.mass_g, 1) + " g" + (c.cost_usd != null ? " · $" + fmt(c.cost_usd, 2) : "")) + "</span></span></button></li>"; }).join("") + "</ul></details>";
    }).join("") + "</div>";
  }
  function wireParts(root) {
    $$("[data-part]", root).forEach(function (b) { b.addEventListener("click", function () { highlightParts([b.dataset.part]); showPartCard(b.dataset.part, true); }); });
  }
  function showPartCard(key, keepView) {
    var c = COMP[key] || {}, p = AC.parts[key] || {};
    if (!keepView) highlightParts([key]);
    var box = $("#partcard"); if (!box) { box = document.createElement("div"); box.id = "partcard"; box.className = "card"; $("#pane-" + currentView).prepend(box); }
    box.innerHTML = componentDetail(key) + '<p style="margin-top:8px"><a href="#components" data-open-comp="' + esc(key) + '">All checks and suggestions →</a></p>';
    var a = $("[data-open-comp]", box); if (a) a.addEventListener("click", function () { setTimeout(function () { openComponent(key); }, 30); });
    box.scrollIntoView({block: "nearest"});
  }

  function openDrawing(src, title) {
    var ov = document.createElement("div"); ov.className = "lightbox"; ov.setAttribute("role", "dialog"); ov.setAttribute("aria-label", title);
    ov.innerHTML = '<div class="lb-bar"><b>' + esc(title) + '</b><span class="muted small">scroll to zoom, drag to pan</span><button class="btn" id="lb-close">Close</button></div><div class="lb-body"><img src="' + esc(src) + '" alt="' + esc(title) + '"></div>';
    document.body.appendChild(ov);
    var img = $("img", ov), body = $(".lb-body", ov), z = 1, drag = null;
    function close() { ov.remove(); document.removeEventListener("keydown", key); }
    function key(e) { if (e.key === "Escape") close(); }
    document.addEventListener("keydown", key);
    $("#lb-close").addEventListener("click", close);
    body.addEventListener("wheel", function (e) { e.preventDefault(); z = Math.min(8, Math.max(0.5, z * (e.deltaY < 0 ? 1.15 : 1 / 1.15))); img.style.width = (z * 100) + "%"; }, {passive: false});
    body.addEventListener("pointerdown", function (e) { drag = [e.clientX, e.clientY, body.scrollLeft, body.scrollTop]; });
    body.addEventListener("pointermove", function (e) { if (drag) { body.scrollLeft = drag[2] - (e.clientX - drag[0]); body.scrollTop = drag[3] - (e.clientY - drag[1]); } });
    window.addEventListener("pointerup", function () { drag = null; });
  }

  /* ---- wiring */
  var WIRE_COL = {power: 0xd2362b, phase: 0xe2861a, signal: 0x2a6aa6, other: 0x6b7a86};
  function drawWiring() {
    var W = X.wiring || {}, grp = layer("wiring"); clear(grp);
    var harness = {}; (W.harness_parts || []).forEach(function (k) { harness[k] = 1; });
    var nodeParts = {}; (W.nodes || []).forEach(function (n) { if (n.part) nodeParts[n.part] = 1; });
    AIR.paint(function (u) { return harness[u.part] ? [0.85, 0.30, 0.20] : nodeParts[u.part] ? [0.25, 0.45, 0.70] : [0.80, 0.83, 0.86]; });
    AIR.fade(function (u) { return !!(harness[u.part] || nodeParts[u.part]); });
    var pos = {}; (W.nodes || []).forEach(function (n) { if (n.pos_mm) pos[n.id] = new THREE.Vector3(n.pos_mm[0], n.pos_mm[1], n.pos_mm[2]); });
    (W.links || []).forEach(function (l, i) {
      var a = pos[l.source], b = pos[l.target]; if (!a || !b) return;
      var mid = a.clone().add(b).multiplyScalar(0.5), off = (i % 5 - 2) * 4; mid.z += 18 + off; mid.x += off;
      var curve = new THREE.QuadraticBezierCurve3(a, mid, b);
      var tube = new THREE.Mesh(new THREE.TubeGeometry(curve, 24, l.kind === "power" ? 3.2 : 1.8, 6, false),
        new THREE.MeshStandardMaterial({color: WIRE_COL[l.kind] || WIRE_COL.other, roughness: 0.5, emissive: WIRE_COL[l.kind] || WIRE_COL.other, emissiveIntensity: 0.25}));
      tube.userData = {link: l}; grp.add(tube);
    });
    (W.nodes || []).forEach(function (n) {
      if (!pos[n.id]) return;
      var s = new THREE.Mesh(new THREE.SphereGeometry(6, 14, 10), new THREE.MeshStandardMaterial({color: 0x14202a}));
      s.position.copy(pos[n.id]); s.userData = {node: n}; grp.add(s);
      if (/battery|esc|fc|receiver|motor|servo|power/i.test(n.kind || "")) grp.add(label(n.label, [pos[n.id].x, pos[n.id].y, pos[n.id].z + 26], "#2a6aa6", 18));
    });
    showLayers(["wiring"]);
    legend(Object.keys(WIRE_COL).filter(function (k) { return k !== "other"; }).map(function (k) { return '<span class="sw"><i style="background:#' + WIRE_COL[k].toString(16).padStart(6, "0") + '"></i>' + k + "</span>"; }).join(""));
    caption("Electrical routing: battery → power distribution → ESCs → motors (phase), flight controller and receiver signals to ESCs and servos. Hover a wire.");
    V.setHover(function (e) {
      var h = V.pick(e, grp.children); if (h) { var u = h.object.userData; if (u.link) return u.link.kind + ": " + nodeName(u.link.source) + " → " + nodeName(u.link.target) + (u.link.current_A ? " · " + fmt(u.link.current_A, 1) + " A" : ""); if (u.node) return u.node.label; }
      return hoverPart(e);
    });
  }
  function nodeName(id) { var n = ((X.wiring || {}).nodes || []).filter(function (x) { return x.id === id; })[0]; return n ? n.label : id; }
  function wiringPanel() {
    var W = X.wiring || {};
    if (!W.nodes) return '<div class="card"><p class="small muted">No wiring data in this build.</p></div>';
    var rows = (W.links || []).map(function (l) {
      return "<tr><td>" + esc(l.kind) + "</td><td>" + esc(nodeName(l.source)) + " → " + esc(nodeName(l.target)) + '</td><td class="num">' + (l.current_A != null ? fmt(l.current_A, 1) + " A" : "—") + "</td><td>" + esc(l.wire || "") + "</td><td>" + chip(norm(l.status || "nev")) + '</td><td class="small muted">' + esc(l.note || "") + "</td></tr>";
    }).join("");
    var ck = W.checks || [], checks = "";
    if (Array.isArray(ck)) checks = ck.map(function (c) { return "<li>" + chip(norm(c.status || c.severity || "nev")) + " " + esc(c.text || c.name || JSON.stringify(c)) + "</li>"; }).join("");
    else {
      checks = (ck.electrical_gaps || []).map(function (g) { return "<li>" + chip("watch") + " <b>" + esc(g.interface) + "</b>: " + esc(g.issue) + "</li>"; }).join("");
      if (ck.rule) checks += '<li class="muted">' + esc(ck.rule) + "</li>";
    }
    return '<div class="card"><h3>Electrical routing</h3><p class="small">' + (W.nodes.length) + " nodes, " + (W.links || []).length + ' connections. Links marked “inferred” come from the BOM, not from a modelled harness.</p>' +
      '<div class="tw" style="margin-top:8px"><table><thead><tr><th>Kind</th><th>From → to</th><th>Current</th><th>Wire</th><th>Status</th><th>Note</th></tr></thead><tbody>' + rows + "</tbody></table></div>" +
      (checks ? "<h4>Harness checks</h4><ul style=\"margin:0;padding-left:18px;font-size:12.8px;display:grid;gap:4px\">" + checks + "</ul>" : "") + "</div>" +
      (X.wiring_svg || W.wiring_svg ? '<div class="card"><h3>Wiring sheet</h3><div class="svgbox">' + (X.wiring_svg || W.wiring_svg) + "</div></div>" : "");
  }

  /* ---- fabrication */
  function fabSteps() {
    var F = X.fabrication || {}, out = [];
    (F.steps || []).forEach(function (s) { out.push({kind: "Make", s: s}); });
    (F.assembly || []).forEach(function (s) { out.push({kind: "Assemble", s: s}); });
    return out;
  }
  function fabricationPanel() {
    var st = fabSteps();
    if (!st.length) return '<div class="card"><p class="small muted">No fabrication data in this build.</p></div>';
    var mins = (X.fabrication.steps || []).reduce(function (a, s) { return a + (s.time_min || 0); }, 0);
    return '<div class="card"><h3>Fabrication and assembly</h3><p class="small">' + (X.fabrication.steps || []).length + " fabrication steps (about " + fmt(mins / 60, 1) + " h of shop time, estimate) and " + (X.fabrication.assembly || []).length +
      ' assembly steps. Pick a step: its parts light up on the model.</p><div style="display:flex;gap:6px;margin-top:8px"><button class="btn" id="fab-prev">← Previous</button><button class="btn primary" id="fab-next">Next step →</button></div></div><div id="fabdetail"></div>' +
      '<div class="card"><ol class="steps">' + st.map(function (x, i) {
        var s = x.s;
        return '<li><button class="row" data-fab="' + i + '" aria-current="' + (i === fabIndex) + '"><span class="n"></span><span class="grow"><b>' + esc(s.title) + "</b><span>" + esc(x.kind + (s.process ? " · " + s.process : "") + (s.time_min ? " · " + fmt(s.time_min) + " min" : "") + ((s.parts || []).length ? " · " + s.parts.length + " parts" : "")) + "</span></span></button></li>";
      }).join("") + "</ol></div>";
  }
  function wireFab() {
    $$("[data-fab]").forEach(function (b) { b.addEventListener("click", function () { showFab(+b.dataset.fab); }); });
    var n = fabSteps().length;
    var pv = $("#fab-prev"), nx = $("#fab-next");
    if (pv) pv.addEventListener("click", function () { showFab(Math.max(0, fabIndex - 1)); });
    if (nx) nx.addEventListener("click", function () { showFab(Math.min(n - 1, fabIndex + 1)); });
  }
  function showFab(i) {
    var st = fabSteps(); if (!st[i]) return; fabIndex = i;
    var s = st[i].s, set = {}; (s.parts || []).forEach(function (k) { set[k] = 1; });
    // assembly steps: everything installed up to this step stays solid
    var done = {}; if (st[i].kind === "Assemble") st.slice(0, i + 1).forEach(function (x) { if (x.kind === "Assemble") (x.s.parts || []).forEach(function (k) { done[k] = 1; }); });
    AIR.paint(function (u) { return set[u.part] ? [0.89, 0.38, 0.10] : done[u.part] ? [0.70, 0.74, 0.78] : [0.82, 0.85, 0.88]; });
    AIR.fade(function (u) { return !!(set[u.part] || done[u.part]); });
    $$("[data-fab]").forEach(function (b) { b.setAttribute("aria-current", String(+b.dataset.fab === i)); });
    caption("Step " + (i + 1) + " of " + st.length + " · " + st[i].kind + ": " + s.title);
    var list = function (a) { return a && a.length ? "<ul>" + [].concat(a).map(function (x) { return "<li>" + esc(typeof x === "string" ? x : JSON.stringify(x)) + "</li>"; }).join("") + "</ul>" : '<span class="muted">—</span>'; };
    $("#fabdetail").innerHTML = '<div class="card"><h3>' + esc(s.title) + '</h3><dl class="kv"><dt>Kind</dt><dd>' + esc(st[i].kind) + "</dd>" + (s.process ? "<dt>Process</dt><dd>" + esc(s.process) + "</dd>" : "") +
      (s.time_min ? "<dt>Time</dt><dd>" + fmt(s.time_min) + " min (estimate)</dd>" : "") + "<dt>Parts</dt><dd>" + ((s.parts || []).map(function (k) { return esc(AC.parts[k] ? AC.parts[k].name.split(" — ")[0] : k); }).join(", ") || "—") + "</dd></dl>" +
      (s.tools ? "<details open><summary>Tools</summary>" + list(s.tools) + "</details>" : "") + (s.fasteners ? "<details><summary>Fasteners</summary>" + list(s.fasteners) + "</details>" : "") +
      (s.inspection ? "<details><summary>Inspection</summary>" + list(s.inspection) + "</details>" : "") + (s.checks ? "<details open><summary>Checks</summary>" + list(s.checks) + "</details>" : "") +
      (s.post_processing ? "<details><summary>Post-processing</summary>" + list(s.post_processing) + "</details>" : "") + (s.risks ? "<details><summary>Risks</summary>" + list(s.risks) + "</details>" : "") +
      (s.notes ? '<p class="small">' + esc([].concat(s.notes).join(" ")) + "</p>" : "") + (s.class || s.time_source ? '<p class="small muted">' + esc([s.class, s.time_source ? "time: " + s.time_source.replace("provisional_estimate", "estimate") + (s.time_basis ? " (" + s.time_basis + ")" : "") : ""].filter(Boolean).join(" · ")) + "</p>" : "") + "</div>";
  }

  /* ---- cost */
  function costPanel() {
    var C = X.cost || {};
    if (C.total_usd == null) return '<div class="card"><p class="small muted">No cost data in this build.</p></div>';
    var bs = (C.by_subsystem || []).slice().sort(function (a, b) { return b.usd - a.usd; }), mx = bs.length ? bs[0].usd : 1;
    var cat = C.by_category || {};
    return '<div class="card"><h3>Cost</h3><p class="small"><b class="num">$' + fmt(C.total_usd, 0) + "</b> total of a $" + fmt(C.budget_usd) + " budget; reserve $" + fmt(C.reserve_usd) + ".</p>" +
      "<h4>By subsystem</h4><div class=\"bars\">" + bs.map(function (b) { return '<div class="b"><span>' + esc(b.subsystem) + '</span><span class="t" style="width:' + (100 * b.usd / mx).toFixed(1) + '%"></span><span class="v">$' + fmt(b.usd, 0) + "</span></div>"; }).join("") + "</div>" +
      (Array.isArray(cat) && cat.length ? "<h4>Build-up</h4><dl class=\"kv\">" + cat.map(function (v) { return "<dt>" + esc(v.category) + "</dt><dd><span class=\"num\">$" + fmt(v.usd, 0) + "</span> " + '<span class="muted small">' + esc((v.source || "").replace("provisional_estimate", "estimate") + (v.note ? " · " + v.note : "")) + "</span></dd>"; }).join("") + "</dl>" : "") +
      "<h4>Most expensive lines</h4><ul class=\"list\">" + (C.top_items || []).map(function (t) { return '<li><button class="row" data-part="' + esc(t.key) + '"><span class="grow"><b>' + esc(t.name.split(" — ")[0]) + '</b></span><span class="num">$' + fmt(t.usd, 2) + "</span></button></li>"; }).join("") + "</ul>" +
      (C.sources || C.notes ? '<p class="small muted" style="margin-top:8px">' + esc([].concat(C.sources || C.notes).map(function (x) { return typeof x === "string" ? x : Object.keys(x).map(function (k) { return k + ": " + x[k]; }).join(", "); }).join(" · ")) + "</p>" : "") + "</div>";
  }

  /* ================================================================== ANALYSES (contours, deformation, mesh, flow) */
  var current = null, fieldObj = null, aFilter = "all", showCrit = true, showMesh = false, showStream = true, qty = "primary",
      deformOn = false, defMul = 1, animOn = false, animT = 0, sliceOn = {};
  function loadField(id, cb) {
    if (FD[id]) { cb(); return; }
    var s = document.createElement("script"); s.src = "data/fields/" + id + ".js";
    s.onload = function () { FD = window.K09L_FIELDS || {}; cb(); };
    s.onerror = function () { var p = $("#fdetail"); if (p) p.innerHTML = '<div class="card"><p class="small">The field data of this analysis could not be loaded.</p></div>'; };
    document.head.appendChild(s);
  }
  function fieldOrder(ids) {
    return ids.sort(function (a, b) { var fa = FI[a], fb = FI[b]; return (RANK[(fb.failure || {}).status] || 0) - (RANK[(fa.failure || {}).status] || 0) || fa.title.localeCompare(fb.title); });
  }
  function viewAnalyses() {
    enterMode("analyses");
    showLayers(["field"]);
    if (!current) {
      AIR.paint(function (u) { var p = AC.parts[u.part]; return p && p.analyses && p.analyses.length ? STATUS_RGB[partStatus(u.part)] : [0.80, 0.83, 0.86]; });
      V.frame(AIR.group);
      caption("Coloured parts have a contour analysis (green ok, amber watch, red fails). Click one, or pick an analysis on the right.");
      V.setHover(function (e) { var h = V.pick(e, AIR.meshes); if (!h) return null; var p = AC.parts[h.object.userData.part]; return p.name.split(" — ")[0] + (p.analyses.length ? " · click for its contour" : " · no contour analysis"); });
      V.setClick(function (e) { var h = V.pick(e, AIR.meshes); if (!h) return; var p = AC.parts[h.object.userData.part]; if (p.analyses.length) openField(p.analyses[0]); else { location.hash = "components"; setTimeout(function () { openComponent(p.key); }, 30); } });
    } else openField(current);
    if (!built.analyses) analysesList();
  }
  function analysesList() {
    var ids = Object.keys(FI);
    var air = fieldOrder(ids.filter(function (id) { return FI[id].group !== "component"; })), comp = fieldOrder(ids.filter(function (id) { return FI[id].group === "component"; }));
    $("#pane-analyses").innerHTML = '<div class="card"><h3>Contour analyses</h3><p class="small muted">' + air.length + " aircraft-level runs and " + comp.length + " component screening runs. Every part without a field has a rating or mass check on the Components page.</p>" +
      '<div class="filters" role="group">' + ["all", "structural", "thermal", "fluid"].map(function (f) { return '<button data-af="' + f + '" aria-pressed="' + (aFilter === f) + '">' + {all: "All", structural: "Structural", thermal: "Thermal", fluid: "Fluid"}[f] + "</button>"; }).join("") +
      '</div><h4>Aircraft level</h4><ul class="list" id="flist"></ul>' + (comp.length ? '<h4>Component screening</h4><ul class="list" id="clist2"></ul>' : "") + '</div><div id="fdetail"></div>';
    function row(id) {
      var f = FI[id];
      return '<li><button class="row" data-fid="' + id + '" aria-current="' + (current === id) + '"><span class="dot ' + ((f.failure || {}).status || "nev") + '"></span><span class="grow"><b>' + esc(f.title) + "</b><span>" + esc(shortResult(f)) + '</span></span><span class="meta">' + esc({structural: "FEA", thermal: "Heat", fluid: "CFD"}[f.discipline] || "") + "</span></button></li>";
    }
    function fill() {
      var ok = function (id) { return aFilter === "all" || FI[id].discipline === aFilter; };
      $("#flist").innerHTML = air.filter(ok).map(row).join("") || '<li class="muted small">None in this discipline.</li>';
      if ($("#clist2")) $("#clist2").innerHTML = comp.filter(ok).map(row).join("") || '<li class="muted small">None in this discipline.</li>';
      $$("[data-fid]").forEach(function (b) { b.addEventListener("click", function () { openField(b.dataset.fid); }); });
    }
    $$("[data-af]").forEach(function (b) { b.addEventListener("click", function () { aFilter = b.dataset.af; $$("[data-af]").forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); }); fill(); }); });
    fill();
    if (!current) $("#fdetail").innerHTML = '<div class="card"><p class="small">Each analysis shows its field on the part, with a choice of quantity (stress, strain, displacement, temperature, pressure, velocity …), the critical region in colour (the rest greyed), the worst point, the deformed shape and a loading animation for structural runs, the failure parameter against its limit, the mesh profile and what to change when a check is not clean.</p></div>';
  }
  function quantities(f) {
    var q = [{key: "primary", quantity: f.quantity, unit: f.unit, range: f.range}];
    (f.fields || []).forEach(function (x) { q.push(x); });
    return q;
  }
  function openField(id) {
    if (!FI[id]) return;
    loadField(id, function () { buildField(id); });
  }
  function buildField(id) {
    var f = FI[id], d = FD[id]; if (!f || !d) return;
    if (current !== id) { qty = "primary"; deformOn = false; animOn = false; defMul = 1; }
    current = id;
    var grp = layer("field"); clear(grp); showLayers(["field"]);
    AIR.group.visible = false;
    var P0 = b64(d.pos, Float32Array), geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(P0), 3));
    geo.setIndex(new THREE.BufferAttribute(b64(d.idx, Uint32Array), 1));
    geo.computeVertexNormals();
    geo.setAttribute("color", new THREE.BufferAttribute(new Float32Array(d.n * 3), 3));
    fieldObj = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({vertexColors: true, roughness: 0.72, metalness: 0, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1}));
    var sec = {}; Object.keys(d.sec || {}).forEach(function (k) { sec[k] = d.sec[k]; });
    fieldObj.userData = {id: id, P0: P0, val: b64(d.val, Float32Array), crit: b64(d.crit, Uint8Array), sec: sec, dec: {}, D: d.disp ? b64(d.disp, Float32Array) : null};
    grp.add(fieldObj);
    var wire = new THREE.LineSegments(new THREE.WireframeGeometry(geo), new THREE.LineBasicMaterial({color: 0x1d2a35, transparent: true, opacity: 0.32}));
    wire.name = "mesh"; wire.visible = showMesh; grp.add(wire);
    var size = new THREE.Box3().setFromObject(fieldObj).getSize(new THREE.Vector3()).length();
    fieldObj.userData.size = size;
    if (fieldObj.userData.D) {   // undeformed outline, shown with the deformed shape
      var ghost = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BufferGeometry().copy(geo), 30), new THREE.LineBasicMaterial({color: 0x5b6873, transparent: true, opacity: 0.45}));
      ghost.name = "ghost"; ghost.visible = false; grp.add(ghost);
      var D = fieldObj.userData.D, mx = 0; for (var i = 0; i < D.length; i += 3) { var m = Math.hypot(D[i], D[i + 1], D[i + 2]); if (m > mx) mx = m; }
      fieldObj.userData.dmax = mx; fieldObj.userData.auto = mx > 0 ? 0.06 * size / mx : 0;
    }
    var w = f.worst_location_mm, mk = new THREE.Mesh(new THREE.SphereGeometry(1, 18, 12), new THREE.MeshBasicMaterial({color: 0xff2d55, depthTest: false}));
    mk.scale.setScalar(size * 0.011); mk.position.set(w[0], w[1], w[2]); mk.renderOrder = 5; mk.name = "worst"; grp.add(mk);
    if (id === "cfd_cruise") drawFlow(grp);
    colourField();
    V.frame(fieldObj, id === "cfd_cruise" ? new THREE.Vector3(-0.5, -0.9, 0.6) : null, id === "cfd_cruise" ? 1.15 : 1.05);
    fieldTools(id, grp, wire);
    caption(f.title + " · " + (f.part || "").split(" — ")[0] + ". Click the contour to probe a value; the red marker is the worst point.");
    V.setHover(null);
    V.setClick(function (e) {
      var h = V.pick(e, [fieldObj]); if (!h) return;
      var u = fieldObj.userData, idx = [h.face.a, h.face.b, h.face.c], arr = qtyValues(), qd = qtyDef(), val = (arr[idx[0]] + arr[idx[1]] + arr[idx[2]]) / 3;
      var p = $("#probe"); if (p) p.innerHTML = "Probe: <b>" + auto(val) + " " + esc(qd.unit === "-" ? "" : qd.unit) + "</b> " + esc(qd.quantity) + " at (" + h.point.toArray().map(function (x) { return fmt(x, 0); }).join(", ") + ") mm" + (u.crit[idx[0]] ? ' · <span style="color:var(--fail)">critical region</span>' : "");
    });
    if (!built.analyses) { built.analyses = true; analysesList(); }
    $$("[data-fid]").forEach(function (b) { b.setAttribute("aria-current", String(b.dataset.fid === id)); });
    fieldDetail(id);
  }
  function fieldTools(id, grp, wire) {
    var f = FI[id], u = fieldObj.userData, q = quantities(f), fl = id === "cfd_cruise" ? FL.cfd_cruise : null;
    var slices = fl ? (fl.slices || []) : [];
    slices.forEach(function (s, i) { if (sliceOn[i] == null) sliceOn[i] = i === 0; });
    tools('<button class="btn" id="b-back">← Whole aircraft</button>' +
      (q.length > 1 ? '<label class="ctl">Show <select id="t-qty" style="font:inherit;font-size:12px;border:0;background:none;color:inherit;max-width:200px">' + q.map(function (x) { return '<option value="' + esc(x.key) + '"' + (x.key === qty ? " selected" : "") + ">" + esc(x.quantity + (x.unit && x.unit !== "-" ? " (" + x.unit + ")" : "")) + "</option>"; }).join("") + "</select></label>" : "") +
      '<label class="ctl"><input type="checkbox" id="t-crit"' + (showCrit ? " checked" : "") + "> Critical region</label>" +
      '<label class="ctl"><input type="checkbox" id="t-mesh"' + (showMesh ? " checked" : "") + "> Mesh</label>" +
      (u.D ? '<label class="ctl"><input type="checkbox" id="t-def"' + (deformOn ? " checked" : "") + '> Deformed <input type="range" id="t-defs" min="0" max="3" step="0.05" value="' + defMul + '" aria-label="Deformation scale"></label><button class="btn" id="t-anim">' + (animOn ? "❚❚ Stop" : "▶ Animate loading") + "</button>" : "") +
      slices.map(function (s, i) { return '<label class="ctl"><input type="checkbox" data-slice="' + i + '"' + (sliceOn[i] ? " checked" : "") + "> " + esc(s.name) + "</label>"; }).join("") +
      (fl && fl.streamlines ? '<label class="ctl"><input type="checkbox" id="t-stream"' + (showStream ? " checked" : "") + "> Streamlines</label>" : ""));
    $("#b-back").addEventListener("click", function () { current = null; animOn = false; clear(layer("field")); AIR.group.visible = true; built.analyses = false; viewAnalyses(); });
    if ($("#t-qty")) $("#t-qty").addEventListener("change", function (e) { qty = e.target.value; colourField(); fieldDetail(current); });
    $("#t-crit").addEventListener("change", function (e) { showCrit = e.target.checked; colourField(); });
    $("#t-mesh").addEventListener("change", function (e) { showMesh = e.target.checked; wire.visible = showMesh; });
    if (u.D) {
      $("#t-def").addEventListener("change", function (e) { deformOn = e.target.checked; if (!deformOn) animOn = false; applyDeform(1); });
      $("#t-defs").addEventListener("input", function (e) { defMul = +e.target.value; deformOn = true; $("#t-def").checked = true; applyDeform(1); });
      $("#t-anim").addEventListener("click", function () { animOn = !animOn; if (animOn) { deformOn = true; $("#t-def").checked = true; animT = 0; } this.textContent = animOn ? "❚❚ Stop" : "▶ Animate loading"; if (!animOn) applyDeform(1); });
      applyDeform(1);
    }
    $$("[data-slice]").forEach(function (c) { c.addEventListener("change", function () { var i = +c.dataset.slice; sliceOn[i] = c.checked; var o = grp.getObjectByName("slice" + i); if (o) o.visible = c.checked; colourLegend(); }); });
    if ($("#t-stream")) $("#t-stream").addEventListener("change", function (e) { showStream = e.target.checked; var o = grp.getObjectByName("stream"); if (o) o.visible = showStream; colourLegend(); });
  }
  function applyDeform(phase) {
    if (!fieldObj || !fieldObj.userData.D) return;
    var u = fieldObj.userData, P = fieldObj.geometry.getAttribute("position"), s = deformOn ? u.auto * defMul * phase : 0;
    for (var i = 0; i < u.P0.length; i++) P.array[i] = u.P0[i] + u.D[i] * s;
    P.needsUpdate = true;
    if (!animOn) fieldObj.geometry.computeVertexNormals();
    var wire = layer("field").getObjectByName("mesh"); if (wire && wire.visible && !animOn) { wire.geometry.dispose(); wire.geometry = new THREE.WireframeGeometry(fieldObj.geometry); }
    var ghost = layer("field").getObjectByName("ghost"); if (ghost) ghost.visible = deformOn && s > 0;
    var cap = deformOn ? " Deformed shape at " + fmt(u.auto * defMul * phase, 0) + "× (largest displacement " + auto(u.dmax) + " mm)." : "";
    caption(FI[current].title + "." + cap + " Click the contour to probe a value.");
  }
  V.ticks.push(function (dt) {
    if (!animOn || !fieldObj || currentView !== "analyses") return;
    animT += dt; applyDeform(0.5 - 0.5 * Math.cos(animT * 2.2));
  });
  function qtyDef() { var f = FI[current]; if (qty === "primary") return {key: "primary", quantity: f.quantity, unit: f.unit, range: f.range, cmap: mapFor(f.discipline), symmetric: f.discipline === "fluid"}; return (f.fields || []).filter(function (x) { return x.key === qty; })[0] || {key: "primary", quantity: f.quantity, unit: f.unit, range: f.range}; }
  function qtyValues() {
    var u = fieldObj.userData; if (qty === "primary" || !u.sec[qty]) return u.val;
    if (!u.dec[qty]) u.dec[qty] = b64(u.sec[qty], Float32Array);
    return u.dec[qty];
  }
  function colourField() {
    var f = FI[current], u = fieldObj.userData, c = fieldObj.geometry.getAttribute("color"), qd = qtyDef(), arr = qtyValues();
    var lo = qd.range ? qd.range[0] : 0, hi = qd.range ? qd.range[1] : 1;
    if (!qd.range) { lo = Infinity; hi = -Infinity; for (var j = 0; j < arr.length; j++) { if (arr[j] < lo) lo = arr[j]; if (arr[j] > hi) hi = arr[j]; } }
    var m = qd.cmap || (qty === "primary" ? mapFor(f.discipline) : "turbo");
    if (qty === "primary" && f.discipline === "fluid") { lo = Math.max(lo, -2.0); hi = Math.min(hi, 1.0); }
    if (qd.symmetric && qty !== "primary") { var a = Math.max(Math.abs(lo), Math.abs(hi)); lo = -a; hi = a; }
    var useCrit = showCrit && qty === "primary";
    for (var i = 0; i < arr.length; i++) {
      var col = cmap(m, (arr[i] - lo) / ((hi - lo) || 1));
      if (useCrit && !u.crit[i]) col = [col[0] * 0.2 + 0.5, col[1] * 0.2 + 0.52, col[2] * 0.2 + 0.55];
      c.setXYZ(i, col[0], col[1], col[2]);
    }
    c.needsUpdate = true;
    u.lo = lo; u.hi = hi; u.map = m;
    colourLegend();
  }
  function colourLegend() {
    var u = fieldObj.userData, qd = qtyDef();
    var h = "<span>" + esc(qd.quantity) + '</span><span class="num">' + auto(u.lo) + '</span><span class="bar" style="background:' + gradientCss(u.map) + '"></span><span class="num">' + auto(u.hi) + " " + esc(qd.unit === "-" ? "" : qd.unit) + "</span>";
    var fl = FL[current], anySlice = false;
    if (fl) (fl.slices || []).forEach(function (s, i) { if (sliceOn[i]) anySlice = true; });
    if (fl && (anySlice || (showStream && fl.streamlines))) h += '<span style="flex-basis:100%;height:0"></span><span>flow speed</span><span class="num">0</span><span class="bar" style="background:' + gradientCss("viridis") + '"></span><span class="num">' + fmt(fl.vmax, 1) + " m/s</span>";
    legend(h);
  }
  function drawFlow(grp) {
    var fl = FL.cfd_cruise; if (!fl) return;
    var vmax = fl.vmax || 20;
    (fl.slices || []).forEach(function (sl, k) {
      var g = new THREE.BufferGeometry(), sp = b64(sl.speed, Float32Array), n = sp.length, col = new Float32Array(n * 3);
      g.setAttribute("position", new THREE.BufferAttribute(b64(sl.pos, Float32Array), 3));
      g.setIndex(new THREE.BufferAttribute(b64(sl.idx, Uint32Array), 1));
      for (var i = 0; i < n; i++) { var c = cmap("viridis", sp[i] / vmax); col[3 * i] = c[0]; col[3 * i + 1] = c[1]; col[3 * i + 2] = c[2]; }
      g.setAttribute("color", new THREE.BufferAttribute(col, 3));
      var sm = new THREE.Mesh(g, new THREE.MeshBasicMaterial({vertexColors: true, side: THREE.DoubleSide, transparent: true, opacity: 0.6, depthWrite: false}));
      sm.name = "slice" + k; sm.visible = !!sliceOn[k]; grp.add(sm);
    });
    if (fl.streamlines) {
      var P = b64(fl.streamlines.pos, Float32Array), Sp = b64(fl.streamlines.speed, Float32Array), off = fl.streamlines.offsets;
      var pos = [], cols = [];
      for (var k = 0; k + 1 < off.length; k++) for (var j = off[k]; j < off[k + 1] - 1; j++) {
        pos.push(P[3 * j], P[3 * j + 1], P[3 * j + 2], P[3 * j + 3], P[3 * j + 4], P[3 * j + 5]);
        var c1 = cmap("viridis", Sp[j] / vmax), c2 = cmap("viridis", Sp[j + 1] / vmax); cols.push(c1[0], c1[1], c1[2], c2[0], c2[1], c2[2]);
      }
      var lg = new THREE.BufferGeometry(); lg.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3)); lg.setAttribute("color", new THREE.Float32BufferAttribute(cols, 3));
      var ls = new THREE.LineSegments(lg, new THREE.LineBasicMaterial({vertexColors: true, transparent: true, opacity: 0.9}));
      ls.name = "stream"; ls.visible = showStream; grp.add(ls);
    }
  }
  function meshProfile(id) {
    var mp = ((X.mesh_profiles || {})[id]) || null, m = (mp && mp.mesh) || FI[id].mesh || {};
    var rows = [], hist = null;
    Object.keys(m).forEach(function (k) {
      var v = m[k];
      if (Array.isArray(v) && v.length > 3 && typeof v[0] === "number" && /hist/i.test(k)) { hist = hist || {k: k, v: v}; return; }
      if (v && typeof v === "object" && !Array.isArray(v)) {
        if (v.hist || v.histogram) hist = hist || {k: k, v: v.hist || v.histogram, lo: v.min, hi: v.max};
        rows.push("<dt>" + esc(k.replace(/_/g, " ")) + "</dt><dd class=\"num\">" + esc(Object.keys(v).filter(function (x) { return !/hist/.test(x); }).map(function (x) { return x + " " + (typeof v[x] === "number" ? auto(v[x]) : v[x]); }).join(", ")) + "</dd>");
        return;
      }
      rows.push("<dt>" + esc(k.replace(/_/g, " ")) + "</dt><dd" + (typeof v === "number" ? ' class="num"' : "") + ">" + esc(typeof v === "number" ? auto(v) : Array.isArray(v) ? v.join(", ") : v) + "</dd>");
    });
    var hh = "";
    if (hist) {
      var arr = hist.v.counts || hist.v, mx = Math.max.apply(null, arr) || 1;
      hh = "<h4>" + esc(hist.k.replace(/_/g, " ")) + '</h4><div class="hist">' + arr.map(function (c) { return '<i style="height:' + (100 * c / mx).toFixed(1) + '%" title="' + c + '"></i>'; }).join("") + "</div>" +
        '<div class="hist-ax"><span>' + (hist.lo != null ? auto(hist.lo) : "low quality") + "</span><span>" + (hist.hi != null ? auto(hist.hi) : "high") + "</span></div>";
    }
    return "<details open><summary>Mesh profile</summary>" + (mp && mp.summary ? '<p class="small">' + esc(mp.summary) + "</p>" : "") + '<dl class="kv">' + rows.join("") + "</dl>" + hh + '<p class="small muted">Tick “Mesh” above the viewer to see the surface mesh.</p></details>';
  }
  function quantityTable(f) {
    var q = quantities(f); if (q.length < 2 && !f.displacement) return "";
    return "<h4>Quantities in this run</h4><div class=\"tw\"><table><thead><tr><th>Quantity</th><th>Min</th><th>Max</th><th>Unit</th></tr></thead><tbody>" +
      q.map(function (x) { return '<tr class="click" data-q="' + esc(x.key) + '"' + (x.key === qty ? ' style="font-weight:600"' : "") + "><td>" + esc(x.quantity) + '</td><td class="num">' + auto(x.range && x.range[0]) + '</td><td class="num">' + auto(x.range && x.range[1]) + "</td><td>" + esc(x.unit === "-" ? "" : x.unit) + "</td></tr>"; }).join("") +
      "</tbody></table></div>" + (f.displacement ? '<p class="small" style="margin-top:6px">Largest displacement <b class="num">' + auto(f.displacement.max_mm) + ' mm</b>. Tick “Deformed” or press “Animate loading” above the viewer.</p>' : "");
  }
  function fieldDetail(id) {
    var f = FI[id], fs = f.failure || {}, st = fs.status || "nev", main = failureMain(id, f);
    var ex = f.extra || {}, sol = f.solver || {};
    var rem = remediesFor(function (r) { return r.kind === "analysis" && (r.id === id || (r.title || "").indexOf(f.title) >= 0); });
    var hist = ex.history && ex.history.length > 1 ? ex.history : null;
    $("#fdetail").innerHTML = '<div class="card"><h3>' + esc(f.title) + " " + chip(st) + "</h3>" +
      '<div class="fail-card ' + st + '"><span class="small">' + esc(fs.criterion) + '</span><div class="big">' + main[0] + '</div><span class="small">' + esc(main[1]) + "</span></div>" +
      '<p class="small">' + esc(fs.text) + "</p>" +
      '<p class="small" id="probe" style="margin-top:6px">Click the contour to read a value.</p>' +
      '<dl class="kv"><dt>Worst value</dt><dd class="num">' + auto(fs.value) + " " + esc(fs.unit) + "</dd><dt>Allowable</dt><dd class=\"num\">" + auto(fs.allowable) + " " + esc(fs.unit) +
      "</dd><dt>Where</dt><dd class=\"num\">(" + f.worst_location_mm.map(function (x) { return fmt(x, 1); }).join(", ") + ") mm</dd><dt>Critical region</dt><dd>" + esc(f.critical.text) +
      "</dd><dt>Load case</dt><dd>" + esc(f.load_case) + "</dd><dt>Evidence</dt><dd>" + esc(f.evidence) + "</dd><dt>Part</dt><dd>" + esc((f.part || "").split(" — ")[0]) + "</dd></dl>" +
      quantityTable(f) +
      (rem.length ? "<h4>What to do</h4>" + rem.map(issueHtml).join("") : (st === "ok" ? '<p class="small muted">Passes its limit; no change needed. It still rests on estimated material data until the structural tests confirm it.</p>' : "")) +
      meshProfile(id) +
      "<details><summary>Solver</summary><dl class=\"kv\"><dt>Solver</dt><dd>" + esc(sol.name) + "</dd><dt>Run time</dt><dd>" + fmt(sol.seconds, 0) + " s</dd>" +
      (sol.iterations ? "<dt>Iterations</dt><dd>" + sol.iterations + "</dd>" : "") + (sol.checkMesh ? "<dt>Mesh check</dt><dd>" + esc(sol.checkMesh) + "</dd>" : "") +
      (sol.cl_oscillation_last_10pct != null ? "<dt>C_L oscillation</dt><dd class=\"num\">" + auto(sol.cl_oscillation_last_10pct) + "</dd>" : "") +
      (ex.cl != null ? "<dt>C_L / C_D</dt><dd class=\"num\">" + fmt(ex.cl, 3) + " / " + fmt(ex.cd, 4) + " at α " + fmt(ex.alpha_deg, 1) + "°</dd>" : "") +
      "<dt>Run</dt><dd>" + esc(f.record_time) + "</dd></dl>" + (hist ? "<h4>C_L at the written iterations</h4>" + spark(hist.map(function (r) { return r[0]; }), [hist.map(function (r) { return r[1]; })], ["var(--accent2)"]) : "") + "</details>" +
      "<details><summary>Assumptions</summary><ul>" + (f.assumptions || []).map(function (a) { return "<li>" + esc(a) + "</li>"; }).join("") + "</ul></details></div>";
    wireShowParts($("#fdetail"));
    $$("[data-q]", $("#fdetail")).forEach(function (r) { r.addEventListener("click", function () { qty = r.dataset.q; var sel = $("#t-qty"); if (sel) sel.value = qty; colourField(); fieldDetail(current); }); });
  }
  function spark(xs, series, colors, cursor) {
    var w = 300, h = 64, x0 = xs[0], x1 = xs[xs.length - 1], lo = Infinity, hi = -Infinity;
    series.forEach(function (s) { s.forEach(function (v) { if (v < lo) lo = v; if (v > hi) hi = v; }); });
    if (hi === lo) { hi += 1; lo -= 1; }
    var X_ = function (x) { return 4 + (x - x0) / ((x1 - x0) || 1) * (w - 8); }, Y = function (v) { return h - 4 - (v - lo) / (hi - lo) * (h - 8); };
    return '<svg class="spark" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none">' + series.map(function (s, k) {
      return '<path d="' + s.map(function (v, i) { return (i ? "L" : "M") + X_(xs[i]).toFixed(1) + " " + Y(v).toFixed(1); }).join("") + '" style="stroke:' + colors[k] + '"/>';
    }).join("") + (cursor != null ? '<line class="cur" x1="' + X_(cursor) + '" x2="' + X_(cursor) + '" y1="0" y2="' + h + '" style="stroke:var(--accent);stroke-width:1.2"/>' : "") +
      '</svg><div class="hist-ax"><span>' + auto(lo) + " … " + auto(hi) + "</span><span>" + auto(x0) + " → " + auto(x1) + "</span></div>";
  }

  /* ================================================================== FLIGHT (virtual environment, real scale) */
  var flightCase = null, camMode = "follow";
  var PHASE_COL = ["#2a6aa6", "#2c7a47", "#e2861a", "#b42f29", "#7a4fb3", "#1f8f8f", "#8a5c00", "#5b6873"];
  function buildWorld(tr) {
    var grp = layer("world"); clear(grp);
    var ext = tr.world && tr.world.extent_mm, xs = [0], ys = [0];
    if (ext) { xs = [ext[0], ext[1]]; ys = [ext[2], ext[3]]; }
    var span = Math.max(80000, Math.max(xs[1] - xs[0] || 0, ys[1] - ys[0] || 0) * 1.8);
    var size = Math.ceil(span / 20000) * 20000, cx = (Math.min.apply(null, xs.concat(0)) + Math.max.apply(null, xs.concat(0))) / 2, cy = (Math.min.apply(null, ys.concat(0)) + Math.max.apply(null, ys.concat(0))) / 2;
    // ground: one 10 m tile with 1 m lines, mip-mapped (no aliasing at grazing angles)
    var gc = document.createElement("canvas"); gc.width = gc.height = 1024; var gx = gc.getContext("2d");
    gx.fillStyle = "#8db07a"; gx.fillRect(0, 0, 1024, 1024);
    gx.strokeStyle = "rgba(60,90,45,0.35)"; gx.lineWidth = 3;
    for (var q = 1; q < 10; q++) { gx.beginPath(); gx.moveTo(q * 102.4, 0); gx.lineTo(q * 102.4, 1024); gx.moveTo(0, q * 102.4); gx.lineTo(1024, q * 102.4); gx.stroke(); }
    gx.strokeStyle = "rgba(45,70,35,0.8)"; gx.lineWidth = 8; gx.strokeRect(0, 0, 1024, 1024);
    var gt = new THREE.CanvasTexture(gc); gt.wrapS = gt.wrapT = THREE.RepeatWrapping; var G = size * 4;
    gt.repeat.set(G / 10000, G / 10000); gt.anisotropy = V.renderer.capabilities.getMaxAnisotropy();
    var ground = new THREE.Mesh(new THREE.PlaneGeometry(G, G), new THREE.MeshStandardMaterial({map: gt, roughness: 1}));
    ground.position.set(0, 0, -3); grp.add(ground);
    // runway along the direction the aircraft flies away in (from the pad to the farthest sample)
    var far = [0, 0], fd = 0; (tr.m || []).forEach(function (m) { var d = Math.hypot(m[3], m[7]); if (d > fd) { fd = d; far = [m[3], m[7]]; } });
    var ang = fd > 1000 ? Math.atan2(far[1], far[0]) : 0, len = Math.max(fd * 1.2, 30000);
    var rw = new THREE.Mesh(new THREE.PlaneGeometry(len, 6000), new THREE.MeshStandardMaterial({color: 0x8d9296, roughness: 0.95}));
    rw.position.set(Math.cos(ang) * len / 2, Math.sin(ang) * len / 2, 15); rw.rotation.z = ang; grp.add(rw);
    for (var k = 4000; k < len - 2000; k += 9000) {
      var dash = new THREE.Mesh(new THREE.PlaneGeometry(4000, 220), new THREE.MeshBasicMaterial({color: 0xf4f4f4}));
      dash.position.set(Math.cos(ang) * k, Math.sin(ang) * k, 30); dash.rotation.z = ang; grp.add(dash);
    }
    // take-off pad with an H and a 30 m height pole with 5 m marks
    var pad = new THREE.Mesh(new THREE.CircleGeometry(1800, 48), new THREE.MeshStandardMaterial({color: 0x30363c, roughness: 0.9})); pad.position.z = 40; grp.add(pad);
    var ring = new THREE.Mesh(new THREE.RingGeometry(1600, 1800, 48), new THREE.MeshBasicMaterial({color: 0xe2601a})); ring.position.z = 50; grp.add(ring);
    [[-450, 0, 160, 1200], [450, 0, 160, 1200], [0, 0, 900, 160]].forEach(function (b) { var h = new THREE.Mesh(new THREE.PlaneGeometry(b[2], b[3]), new THREE.MeshBasicMaterial({color: 0xffffff})); h.position.set(b[0], b[1], 55); grp.add(h); });
    var px = -5000, py = 5000;
    var pole = new THREE.Mesh(new THREE.CylinderGeometry(45, 45, 30000, 8), new THREE.MeshStandardMaterial({color: 0xf2f2f2})); pole.rotation.x = Math.PI / 2; pole.position.set(px, py, 15000); grp.add(pole);
    for (var z = 5; z <= 30; z += 5) {
      var band = new THREE.Mesh(new THREE.CylinderGeometry(80, 80, 450, 8), new THREE.MeshStandardMaterial({color: 0xe2601a})); band.rotation.x = Math.PI / 2; band.position.set(px, py, z * 1000); grp.add(band);
      grp.add(label(z + " m", [px, py + 1000, z * 1000], "#e2601a", 700));
    }
    for (var d = 20; d * 1000 <= len; d += 20) grp.add(label(d + " m", [Math.cos(ang) * d * 1000 - Math.sin(ang) * 4500, Math.sin(ang) * d * 1000 + Math.cos(ang) * 4500, 60], "#3f5a30", 900));
    // trees about 8 m tall (scale cue), kept off the runway
    var rnd = 7; function rr() { rnd = (rnd * 16807) % 2147483647; return rnd / 2147483647; }
    for (var t = 0; t < 70; t++) {
      var a = rr() * Math.PI * 2, r = size * (0.3 + rr() * 0.6), x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r, s = 0.7 + rr() * 0.6;
      var along = x * Math.cos(ang) + y * Math.sin(ang), across = -x * Math.sin(ang) + y * Math.cos(ang);
      if (Math.abs(across) < 9000 && along > -6000 && along < len + 6000) continue;
      var trunk = new THREE.Mesh(new THREE.CylinderGeometry(180 * s, 250 * s, 2500 * s, 6), new THREE.MeshStandardMaterial({color: 0x6b4f35})); trunk.rotation.x = Math.PI / 2; trunk.position.set(x, y, 1250 * s); grp.add(trunk);
      var crown = new THREE.Mesh(new THREE.ConeGeometry(2000 * s, 6000 * s, 8), new THREE.MeshStandardMaterial({color: 0x3f6b3a, roughness: 1})); crown.rotation.x = Math.PI / 2; crown.position.set(x, y, 5200 * s); grp.add(crown);
    }
    // distant hills on the horizon
    for (var hgt = 0; hgt < 14; hgt++) {
      var ha = hgt / 14 * Math.PI * 2 + rr() * 0.3, hr = G * 0.42, hs = 0.6 + rr() * 0.8;
      var hill = new THREE.Mesh(new THREE.ConeGeometry(40000 * hs, 22000 * hs, 6), new THREE.MeshStandardMaterial({color: 0x9db3a6, roughness: 1, flatShading: true}));
      hill.rotation.x = Math.PI / 2; hill.position.set(Math.cos(ha) * hr, Math.sin(ha) * hr, 11000 * hs - 3000); grp.add(hill);
    }
    var cv = document.createElement("canvas"); cv.width = 2; cv.height = 256; var ctx = cv.getContext("2d"), gr = ctx.createLinearGradient(0, 0, 0, 256);
    gr.addColorStop(0, "#4f86c6"); gr.addColorStop(0.55, "#9cc2e6"); gr.addColorStop(1, "#e4edf3"); ctx.fillStyle = gr; ctx.fillRect(0, 0, 2, 256);
    return {sky: new THREE.CanvasTexture(cv), size: size, ang: ang, len: len, cx: cx, cy: cy};
  }
  function trackPhases(tr) {
    if (tr.phases && tr.phases.length) return tr.phases;
    return [{name: flightCase === "hover" ? "hover" : "flight", t0: tr.t[0], t1: tr.t[tr.t.length - 1]}];
  }
  function viewFlight() {
    enterMode("flight");
    var F = X.flight || {}, cases = ["mission", "transition", "hover"].filter(function (k) { return F[k] && F[k].m; });
    if (!flightCase || cases.indexOf(flightCase) < 0) flightCase = cases[0];
    var tr = F[flightCase];
    var NAMES = {mission: "Full mission (take-off to landing)", transition: "Forward transition", hover: "Hover hold"};
    tools(seg("fcase", cases.map(function (k) { return [k, {mission: "Full mission", transition: "Transition only", hover: "Hover only"}[k]]; }), flightCase) +
      seg("fcam", [["follow", "Follow"], ["ground", "Ground observer"], ["side", "Side, whole path"], ["top", "Top"]], camMode));
    onSeg("fcase", function (c) { flightCase = c; viewFlight(); });
    onSeg("fcam", function (c) { camMode = c; placeCamera(true); });
    if (!tr) { $("#pane-flight").innerHTML = '<div class="card"><h3>Flight simulation</h3><p class="small muted">No flight track in this build.</p></div>'; return; }
    if (!tr.current_A && tr.battery_current_A) tr.current_A = tr.battery_current_A;
    if (!tr.pitch_deg && tr.euler_deg) tr.pitch_deg = tr.euler_deg.map(function (e) { return e[1]; });
    var w = buildWorld(tr);
    V.scene.background = w.sky;
    showLayers(["world", "trail"]);
    AIR.setSpin(0); AIR.edges(false);
    AIR.group.matrixAutoUpdate = false;
    var phases = trackPhases(tr), n = tr.t.length, T0 = tr.t[0], T1 = tr.t[n - 1];
    function phaseAt(t) { for (var i = phases.length - 1; i >= 0; i--) if (t >= phases[i].t0) return i; return 0; }
    // trail coloured by phase
    var trail = layer("trail"); clear(trail); showLayers(["world", "trail"]);
    var pts = [], cols = [];
    for (var i = 0; i < n; i++) { var m = tr.m[i]; pts.push(m[3], m[7], m[11]); var c = new THREE.Color(PHASE_COL[phaseAt(tr.t[i]) % PHASE_COL.length]); cols.push(c.r, c.g, c.b); }
    var tg = new THREE.BufferGeometry(); tg.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3)); tg.setAttribute("color", new THREE.Float32BufferAttribute(cols, 3));
    trail.add(new THREE.Line(tg, new THREE.LineBasicMaterial({vertexColors: true})));
    var tubeR = Math.max(120, (w.len || 30000) / 300);
    phases.forEach(function (ph, pi) {  // the path as a tube per phase: visible from far away
      var vs = []; for (var i = 0; i < n; i += Math.max(1, Math.round(n / 400))) { if (tr.t[i] >= ph.t0 - 1e-6 && tr.t[i] <= ph.t1 + 1e-6) vs.push(new THREE.Vector3(pts[3 * i], pts[3 * i + 1], pts[3 * i + 2])); }
      if (vs.length < 2) return;
      var tube = new THREE.Mesh(new THREE.TubeGeometry(new THREE.CatmullRomCurve3(vs), Math.min(400, vs.length * 2), tubeR, 6, false),
        new THREE.MeshBasicMaterial({color: PHASE_COL[pi % PHASE_COL.length], transparent: true, opacity: 0.75}));
      trail.add(tube);
    });
    var tag = document.createElement("div"); tag.className = "ftag"; tag.id = "ftag"; tag.textContent = "K09-L"; $(".stage").appendChild(tag);
    var dg = new THREE.BufferGeometry(), dp = []; for (var j = 0; j < n; j += Math.max(1, Math.round(n / 120))) { dp.push(pts[3 * j], pts[3 * j + 1], pts[3 * j + 2], pts[3 * j], pts[3 * j + 1], 0); }
    dg.setAttribute("position", new THREE.Float32BufferAttribute(dp, 3));
    trail.add(new THREE.LineSegments(dg, new THREE.LineBasicMaterial({color: 0x56606a, transparent: true, opacity: 0.18})));  // drop lines: height above ground
    var shadow = new THREE.Mesh(new THREE.CircleGeometry(500, 32), new THREE.MeshBasicMaterial({color: 0x000000, transparent: true, opacity: 0.28, depthWrite: false})); shadow.position.z = 6; trail.add(shadow);
    // timeline with the phases as coloured segments
    $("#timeline").hidden = false; $(".stage").classList.add("has-tl");
    $("#timeline").innerHTML = '<button id="fplay">❚❚ Pause</button><select id="fspeed" style="font:inherit;font-size:12px"><option value="0.5">½×</option><option value="1"' + (flightCase === "mission" ? "" : " selected") + '>1× real time</option><option value="2"' + (flightCase === "mission" ? " selected" : "") + '>2×</option><option value="4">4×</option></select>' +
      '<div class="tl-track"><div class="tl-phases">' + phases.map(function (p, i) { return '<span data-ph="' + i + '" title="' + esc(p.name) + '" style="flex:' + Math.max(p.t1 - p.t0, 0.01) + ";background:" + PHASE_COL[i % PHASE_COL.length] + '"></span>'; }).join("") + '</div><input type="range" id="fscrub" min="0" max="' + (n - 1) + '" value="0" step="1" aria-label="Time"></div><span class="num" id="ftime">0.0 s</span>';
    var hud = document.createElement("div"); hud.className = "hud"; hud.id = "hud"; $(".stage").appendChild(hud);
    var state = {t: T0, play: true, speed: flightCase === "mission" ? 2 : 1, ang: [0, 0, 0, 0]};
    var M4 = new THREE.Matrix4(), prev = null;
    $("#fplay").addEventListener("click", function () { state.play = !state.play; if (state.play && state.t >= T1) state.t = T0; this.textContent = state.play ? "❚❚ Pause" : "▶ Play"; });
    $("#fspeed").addEventListener("change", function (e) { state.speed = +e.target.value; });
    $("#fscrub").addEventListener("input", function (e) { state.t = tr.t[+e.target.value]; state.play = false; $("#fplay").textContent = "▶ Play"; });
    $$("[data-ph]").forEach(function (sp) { sp.addEventListener("click", function () { state.t = phases[+sp.dataset.ph].t0; }); });
    function idx(t) { var lo = 0, hi = n - 1; while (hi - lo > 1) { var mid = (lo + hi) >> 1; if (tr.t[mid] <= t) lo = mid; else hi = mid; } return lo; }
    function pos(k) { var m = tr.m[k]; return new THREE.Vector3(m[3], m[7], m[11]); }
    window.placeCamera = placeCamera;
    function placeCamera(jump) {
      var k = idx(state.t), p = pos(k), c = V.camera, ctl = V.controls;
      if (camMode === "follow") { if (jump) { ctl.target.copy(p); c.position.copy(p.clone().add(new THREE.Vector3(-3800, -5600, 2200))); } }
      else if (camMode === "ground") { c.position.set(-9000, -16000, 1700); ctl.target.copy(p); }
      else if (camMode === "side") {
        var box = new THREE.Box3(); for (var i = 0; i < n; i += 5) box.expandByPoint(pos(i)); box.expandByPoint(new THREE.Vector3(0, 0, 0));
        var ctr = box.getCenter(new THREE.Vector3()), sz = box.getSize(new THREE.Vector3()), dir = new THREE.Vector3(-Math.sin(w.ang), Math.cos(w.ang), 0).multiplyScalar(-1);
        var dist = Math.max(sz.x, sz.y, sz.z * 2, 20000) / 2 / Math.tan(c.fov * Math.PI / 360) * 1.15;
        ctl.target.copy(ctr); c.position.copy(ctr.clone().add(dir.multiplyScalar(dist)).add(new THREE.Vector3(0, 0, dist * 0.2)));
      } else if (camMode === "top") {
        var bx = new THREE.Box3(); for (var q = 0; q < n; q += 5) bx.expandByPoint(pos(q)); bx.expandByPoint(new THREE.Vector3(0, 0, 0));
        var cc = bx.getCenter(new THREE.Vector3()), ss = bx.getSize(new THREE.Vector3()), dd = Math.max(ss.x, ss.y, 20000) / 2 / Math.tan(c.fov * Math.PI / 360) * 1.2;
        ctl.target.set(cc.x, cc.y, 0); c.position.set(cc.x - 1, cc.y - dd * 0.15, dd);
      }
      prev = p;
    }
    function apply(dt) {
      if (state.play) { state.t += dt * state.speed; if (state.t > T1) { state.t = T1; state.play = false; $("#fplay").textContent = "↺ Replay"; } }
      var k = idx(state.t), m = tr.m[k];
      M4.set(m[0], m[1], m[2], m[3], m[4], m[5], m[6], m[7], m[8], m[9], m[10], m[11], m[12], m[13], m[14], m[15]);
      AIR.group.matrix.copy(M4); AIR.group.matrixWorldNeedsUpdate = true;
      if (tr.rpm && tr.rpm[k]) { for (var r = 0; r < 4; r++) state.ang[r] += (tr.rpm[k][r] || 0) / 60 * 2 * Math.PI * dt * (state.play ? state.speed : 0) * (AC.spin_display || 0.025); AIR.setRotorAngles(state.ang); }
      var p = pos(k);
      var sp = p.clone().project(V.camera), r = V.el.getBoundingClientRect();
      tag.hidden = sp.z > 1; tag.style.left = ((sp.x + 1) / 2 * r.width) + "px"; tag.style.top = ((1 - sp.y) / 2 * r.height) + "px";
      shadow.position.set(p.x, p.y, 70); shadow.material.opacity = Math.max(0.06, 0.32 - p.z / 120000);
      if (camMode === "follow" && prev) { var dlt = p.clone().sub(prev); V.camera.position.add(dlt); V.controls.target.add(dlt); }  // rigid follow: no lag, no smear; drag to orbit
      else if (camMode === "ground") V.controls.target.copy(p);
      prev = p;
      $("#fscrub").value = k; $("#ftime").textContent = (state.t - T0).toFixed(1) + " / " + (T1 - T0).toFixed(1) + " s";
      var ph = phases[phaseAt(state.t)], row = function (lab, v) { return "<div>" + lab + " <b>" + v + "</b></div>"; };
      hud.innerHTML = '<div style="font-weight:600;color:' + PHASE_COL[phaseAt(state.t) % PHASE_COL.length] + '">' + esc(ph.name) + "</div>" + row("Height", fmt(tr.altitude_m ? tr.altitude_m[k] : p.z / 1000, 1) + " m") +
        (tr.airspeed ? row("Airspeed", fmt(tr.airspeed[k], 1) + " m/s") : "") + (tr.pitch_deg ? row("Pitch", fmt(tr.pitch_deg[k], 0) + "°") : "") +
        (tr.rpm ? row("Motors", tr.rpm[k].map(function (x) { return fmt(x / 1000, 1); }).join(" / ") + " k rpm") : "") +
        (tr.current_A ? row("Battery", fmt(tr.current_A[k], 1) + " A") : "") + (tr.soc ? row("Charge", fmt(tr.soc[k] * 100, 0) + " %") : "");
      var ev = (tr.events || []).filter(function (e) { return e.t <= state.t; }).pop();
      caption(NAMES[flightCase] + " at real scale: 1 m grid, 10 m bold lines, runway stripes every 9 m, trees about 8 m, pole marks every 5 m." + (ev ? " · " + ev.text : ""));
      var cur = $$(".fchart .cur"); if (cur.length) { var xx = 4 + (state.t - T0) / ((T1 - T0) || 1) * 292; cur.forEach(function (l) { l.setAttribute("x1", xx); l.setAttribute("x2", xx); }); }
    }
    flightTick = {stop: function () { var i = V.ticks.indexOf(apply); if (i >= 0) V.ticks.splice(i, 1); AIR.group.matrixAutoUpdate = true; window.placeCamera = null; }};
    V.ticks.push(apply);
    placeCamera(true);
    flightPanel(tr, F, phases, function (t) { state.t = t; });
  }
  function flightPanel(tr, F, phases, seek) {
    var o = tr.outcome || {}, ts = tr.t;
    var chart = function (title, series, colors) { return '<h4>' + title + '</h4><div class="fchart">' + spark(ts, series, colors, ts[0]) + "</div>"; };
    var charts = "";
    if (tr.altitude_m) charts += chart("Height (m)", [tr.altitude_m], ["var(--accent2)"]);
    if (tr.airspeed) charts += chart("Airspeed (m/s)", [tr.airspeed], ["var(--accent)"]);
    if (tr.pitch_deg) charts += chart("Pitch (°)", [tr.pitch_deg], ["var(--ink2)"]);
    if (tr.rpm) charts += chart("Motor speed (rpm)", [0, 1, 2, 3].map(function (r) { return tr.rpm.map(function (x) { return x[r]; }); }), ["#d2362b", "#e2861a", "#2a6aa6", "#2c7a47"]);
    if (tr.current_A) charts += chart("Battery current (A)", [tr.current_A], ["var(--fail)"]);
    var outc = Object.keys(o).filter(function (k) { return typeof o[k] !== "object"; }).map(function (k) { return "<dt>" + esc(k.replace(/_/g, " ")) + "</dt><dd class=\"num\">" + esc(typeof o[k] === "number" ? auto(o[k]) : o[k]) + "</dd>"; }).join("");
    var ps = tr.phase_summary || tr.summary || null;
    var rob = F.robustness || S.robustness || [];
    $("#pane-flight").innerHTML = '<div class="card"><h3>' + esc({mission: "Full mission: take-off to landing", transition: "Forward transition", hover: "Hover hold"}[flightCase]) + "</h3>" +
      '<p class="small">Replay of the 6-DOF flight simulation of this variant at real scale. Reduced-order model on provisional aerodynamic, propeller and battery data: it plans the flight tests and is not flight evidence.</p>' +
      '<h4>Phases</h4><ul class="list">' + phases.map(function (p, i) { return '<li><button class="row" data-seek="' + p.t0 + '"><span class="dot" style="background:' + PHASE_COL[i % PHASE_COL.length] + '"></span><span class="grow"><b>' + esc(p.name) + '</b><span class="num">' + fmt(p.t0, 1) + " – " + fmt(p.t1, 1) + " s" + (p.note ? " · " + esc(p.note) : "") + "</span></span></button></li>"; }).join("") + "</ul>" +
      (ps && typeof ps === "object" ? '<h4>Summary</h4><dl class="kv">' + Object.keys(ps).filter(function (k) { return typeof ps[k] !== "object"; }).map(function (k) { return "<dt>" + esc(k.replace(/_/g, " ")) + '</dt><dd class="num">' + esc(typeof ps[k] === "number" ? auto(ps[k]) : ps[k]) + "</dd>"; }).join("") + "</dl>" : "") +
      (outc ? '<details><summary>Outcome</summary><dl class="kv">' + outc + "</dl></details>" : "") + charts + "</div>" +
      (tr.events && tr.events.length ? '<div class="card"><h3>Events</h3><ul class="list">' + tr.events.map(function (e) { return '<li><button class="row" data-seek="' + e.t + '"><span class="num">' + fmt(e.t, 1) + ' s</span><span class="grow"><span>' + esc(e.text) + "</span></span>" + (e.kind === "warning" ? chip("watch", "warning") : "") + "</button></li>"; }).join("") + "</ul></div>" : "") +
      (rob.length ? '<div class="card"><h3>Transition robustness</h3><div class="tw"><table><thead><tr><th>Case</th><th>Height lost</th><th>Within 5 m</th></tr></thead><tbody>' + rob.map(function (r) {
        var ok = !r.ground_contact && r.altitude_loss <= 5;
        return "<tr><td>" + esc(r.case) + '</td><td class="num">' + (r.ground_contact ? "ground contact" : fmt(r.altitude_loss, 2) + " m") + "</td><td>" + chip(ok ? "pass" : "fail", ok ? "yes" : "no") + "</td></tr>";
      }).join("") + "</tbody></table></div></div>" : "");
    $$("[data-seek]", $("#pane-flight")).forEach(function (b) { b.addEventListener("click", function () { seek(+b.dataset.seek); }); });
  }

  /* ================================================================== COMPONENTS */
  var cFilter = "all", cSearch = "", curComp = null;
  function viewComponents() {
    enterMode("components");
    AIR.paint(function (u) { return STATUS_RGB[partStatus(u.part)] || STATUS_RGB.nev; });
    V.frame(AIR.group);
    V.setHover(hoverPart);
    V.setClick(function (e) { var h = V.pick(e, AIR.meshes); if (h) openComponent(h.object.userData.part); });
    legend(["ok", "watch", "fail", "nev"].map(function (s) { return '<span class="sw"><i style="background:' + rgbCss(STATUS_RGB[s]) + '"></i>' + (ST[s] || ST.nev)[2] + "</span>"; }).join(""));
    caption("Every component coloured by its worst check. Click a part or pick it from the list.");
    if (!built.components) {
      var comps = X.components || [];
      var counts = {fail: 0, watch: 0, ok: 0, nev: 0}; comps.forEach(function (c) { counts[partStatus(c.key)] = (counts[partStatus(c.key)] || 0) + 1; });
      $("#pane-components").innerHTML = '<div id="compdetail"></div><div class="card"><h3>Components</h3><p class="small">' + comps.length + " BOM components: " + counts.fail + " fail, " + counts.watch + " to watch, " + counts.ok + " ok, " + counts.nev + " without an analysis.</p>" +
        '<div class="filters" role="group">' + [["all", "All"], ["fail", "Fails"], ["watch", "Watch"], ["ok", "OK"], ["nev", "No analysis"]].map(function (f) { return '<button data-cf="' + f[0] + '" aria-pressed="' + (cFilter === f[0]) + '">' + f[1] + "</button>"; }).join("") + "</div>" +
        '<input type="search" id="csearch" placeholder="Search parts, part numbers, subsystems" aria-label="Search components"><ul class="list" id="clist" style="margin-top:8px"></ul></div>' + costPanel();
      $$("[data-cf]").forEach(function (b) { b.addEventListener("click", function () { cFilter = b.dataset.cf; $$("[data-cf]").forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); }); fillComps(); }); });
      $("#csearch").addEventListener("input", function (e) { cSearch = e.target.value.toLowerCase(); fillComps(); });
      fillComps(); wireParts($("#pane-components"));
      built.components = true;
    }
    if (curComp) openComponent(curComp);
  }
  function fillComps() {
    var comps = (X.components || []).filter(function (c) {
      var s = partStatus(c.key); if (cFilter !== "all" && s !== cFilter) return false;
      return !cSearch || (c.name + " " + (c.pn || "") + " " + (c.subsystem || "")).toLowerCase().indexOf(cSearch) >= 0;
    }).sort(function (a, b) { return (RANK[partStatus(b.key)] || 0) - (RANK[partStatus(a.key)] || 0) || a.name.localeCompare(b.name); });
    $("#clist").innerHTML = comps.map(function (c) {
      return '<li><button class="row" data-comp="' + esc(c.key) + '" aria-current="' + (curComp === c.key) + '"><span class="dot ' + partStatus(c.key) + '"></span><span class="grow"><b>' + esc(c.name.split(" — ")[0]) + "</b><span>" +
        esc((c.subsystem || "") + " · " + (c.qty || 1) + " × " + fmt(c.mass_g, 1) + " g" + (c.cost_usd != null ? " · $" + fmt(c.cost_usd, 2) : "")) + "</span></span></button></li>";
    }).join("") || '<li class="muted small">No component matches.</li>';
    $$("[data-comp]").forEach(function (b) { b.addEventListener("click", function () { openComponent(b.dataset.comp); }); });
  }
  function componentDetail(key) {
    var c = COMP[key] || {}, p = AC.parts[key] || {}, st = partStatus(key);
    var checks = (c.checks || []).map(function (k) {
      return "<tr><td>" + esc(k.name) + '</td><td class="num">' + (k.value == null ? "—" : (typeof k.value === "number" ? auto(k.value) : esc(k.value)) + " " + esc(k.unit || "")) + '</td><td class="num">' + esc(k.limit == null ? "—" : k.limit) + "</td><td>" + chip(norm(k.status)) + '</td><td class="small muted">' + esc(k.evidence || "") + (k.analysis_id ? ' · <a href="#analyses" data-goto-field="' + esc(k.analysis_id) + '">contour</a>' : "") + "</td></tr>";
    }).join("");
    var rem = remediesFor(function (r) { return (r.parts || []).indexOf(key) >= 0; });
    return "<h3>" + esc((c.name || p.name || key).split(" — ")[0]) + " " + chip(st) + "</h3>" +
      '<dl class="kv"><dt>Part number</dt><dd>' + esc(c.pn || p.pn) + "</dd><dt>Subsystem</dt><dd>" + esc(c.subsystem || p.group) + "</dd><dt>Quantity</dt><dd>" + esc(c.qty || p.qty) +
      "</dd><dt>Mass each</dt><dd>" + fmt(c.mass_g != null ? c.mass_g : p.mass_g, 1) + ' g <span class="muted">(' + esc(c.mass_source || p.mass_source || "") + ")</span></dd>" +
      (c.material ? "<dt>Material</dt><dd>" + esc(c.material) + "</dd>" : "") + (c.process ? "<dt>Made by</dt><dd>" + esc([].concat(c.process).join(" · ")) + (c.fab_time_min ? " · about " + fmt(c.fab_time_min) + " min" : "") + "</dd>" : "") +
      (c.cost_usd != null ? "<dt>Cost</dt><dd>$" + fmt(c.cost_usd, 2) + (c.unit_cost_usd != null ? ' <span class="muted">($' + fmt(c.unit_cost_usd, 2) + " each, " + esc(c.cost_source || "") + ")</span>" : "") + "</dd>" : "") + "</dl>" +
      (checks ? '<div class="tw"><table><thead><tr><th>Check</th><th>Value</th><th>Limit</th><th>Status</th><th>Evidence</th></tr></thead><tbody>' + checks + "</tbody></table></div>" : '<p class="small muted">No check covers this part.</p>') +
      (rem.length ? "<h4>What to do</h4><div class=\"stack\">" + rem.map(issueHtml).join("") + "</div>" : "") +
      ((c.suggestions || []).length ? "<h4>Suggestions</h4><ul style=\"margin:0;padding-left:18px;font-size:12.8px;display:grid;gap:4px\">" + c.suggestions.map(function (s) { return "<li>" + esc(s) + "</li>"; }).join("") + "</ul>" : "");
  }
  function openComponent(key) {
    if (currentView !== "components") return;
    curComp = key;
    var c = COMP[key] || {}, p = AC.parts[key] || {};
    $("#compdetail").innerHTML = '<div class="card">' + componentDetail(key) + "</div>";
    $$("[data-goto-field]", $("#compdetail")).forEach(function (a) { a.addEventListener("click", function () { var id = a.dataset.gotoField; setTimeout(function () { openField(id); }, 30); }); });
    wireShowParts($("#compdetail"));
    var set = {}; set[key] = 1;
    AIR.paint(function (u) { return u.part === key ? [0.89, 0.38, 0.10] : STATUS_RGB[partStatus(u.part)].map(function (x) { return x * 0.35 + 0.55; }); });
    AIR.fade(function (u) { return u.part === key; });
    var objs = AIR.meshes.filter(function (m) { return m.userData.part === key; });
    if (objs.length) { var box = new THREE.Box3(); objs.forEach(function (o) { box.expandByObject(o); }); var hlp = new THREE.Box3Helper(box); V.frame(hlp, null, 3); }
    caption((c.name || p.name || key).split(" — ")[0] + " highlighted. Click another part, or Esc for the whole aircraft.");
    $$("[data-comp]").forEach(function (b) { b.setAttribute("aria-current", String(b.dataset.comp === key)); });
    $("#compdetail").scrollIntoView({block: "start", behavior: "smooth"});
  }
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && currentView === "components") { curComp = null; $("#compdetail").innerHTML = ""; viewComponents(); } });

  /* ================================================================== DECISIONS (gates + remedies) */
  function viewDecisions() {
    enterMode("decisions");
    AIR.paint(function (u) { return STATUS_RGB[partStatus(u.part)] || STATUS_RGB.nev; });
    V.frame(AIR.group); V.controls.autoRotate = true; V.setHover(hoverPart);
    caption("Parts coloured by their worst check. Use “Show on the model” on any item.");
    if (built.decisions) return;
    var gates = X.gates || S.gates || [];
    var rem = (X.remedies || []).slice().sort(function (a, b) { return (RANK[norm(b.status)] || 0) - (RANK[norm(a.status)] || 0); });
    $("#pane-decisions").innerHTML = '<div class="card"><h3>Gate decisions</h3><p class="small">Each gate collects criteria from the requirements, analyses and tests. A gate is never passed on simulation alone: flight gates need the physical tests and a recorded human decision.</p></div>' +
      '<div class="stack" style="margin-top:12px">' + gates.map(function (g) {
        var dec = g.decision || ({pass: "GO", cond: "GO WITH CONDITIONS", fail: "NO-GO"}[norm(g.status)] || "NOT READY");
        return '<div class="gate-card"><div class="hd"><span class="mono">' + esc(g.id) + "</span><b>" + esc(g.name) + "</b>" + chip(dec) + "</div>" +
          (g.decision_text ? '<p class="small">' + esc(g.decision_text) + "</p>" : "") +
          ((g.blocking || []).length ? '<p class="small"><b>Blocking:</b> ' + esc(g.blocking.join("; ")) + "</p>" : "") +
          "<details><summary>" + (g.criteria || []).length + " criteria</summary><ul>" + (g.criteria || []).map(function (c) { return "<li>" + chip(norm(c.status)) + " " + esc(c.text) + (c.why ? ' <span class="muted">— ' + esc(c.why) + "</span>" : "") + (c.evidence ? ' <span class="muted">[' + esc(c.evidence) + "]</span>" : "") + "</li>"; }).join("") + "</ul></details></div>";
      }).join("") + "</div>" +
      evidencePanel() +
      '<div class="card" style="margin-top:12px"><h3>What to do to pass</h3><p class="small">' + rem.length + " items are not a clean pass. Each has actions with their expected effect (where the model can compute it) and the test or analysis that verifies it.</p>" +
      '<div class="filters" role="group">' + [["all", "All"], ["fail", "Fails"], ["cond", "Conditional / watch"], ["requirement", "Requirements"], ["analysis", "Analyses"], ["component", "Components"], ["gate", "Gates"]].map(function (f, i) { return '<button data-rf="' + f[0] + '" aria-pressed="' + (i === 0) + '">' + f[1] + "</button>"; }).join("") + '</div><div class="stack" id="remlist"></div></div>';
    function fill(f) {
      $("#remlist").innerHTML = rem.filter(function (r) { var s = norm(r.status); return f === "all" || (f === "fail" && s === "fail") || (f === "cond" && s !== "fail") || r.kind === f; }).map(issueHtml).join("") || '<p class="small muted">Nothing here.</p>';
      wireShowParts($("#remlist"));
    }
    $$("[data-rf]").forEach(function (b) { b.addEventListener("click", function () { $$("[data-rf]").forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); }); fill(b.dataset.rf); }); });
    fill("all");
    wireEvidence($("#pane-decisions"));
    built.decisions = true;
  }

  /* ================================================================== RECOMMENDATIONS (revision C vs B, evidence to close) */
  var R = window.K09L_RECS || {};
  function fmtv(v, u) { return v == null || v === "" ? "—" : (typeof v === "number" ? auto(v) : esc(v)) + (u && u !== "-" ? " " + esc(u) : ""); }
  function refLinks(refs) { return (refs || []).map(function (u, i) { return ' <a href="' + esc(u) + '" target="_blank" rel="noopener">source ' + (i + 1) + "</a>"; }).join(""); }
  function viewRecommendations() {
    enterMode("recommendations");
    var mod = {}; AC.bodies.forEach(function (b, i) { if (b.modified) mod[i] = 1; });
    AIR.paint(function (u) { return mod[u.i] ? [0.89, 0.38, 0.10] : [0.78, 0.81, 0.84]; });
    V.frame(AIR.group); V.setHover(hoverPart); V.controls.autoRotate = true;
    caption("Orange: parts changed from the imported CAD in " + S.variant + ". Revision C changes how the aircraft is flown, not its hardware.");
    if (built.recommendations) return;
    var h = "";
    if (!R || R.error || !R.changes) {
      h = '<div class="card"><h3>Recommendations</h3><p class="small muted">' + (R && R.error ? "The comparison could not be built: " + esc(R.error) : "This build has no recommended revision to compare.") + "</p></div>";
    } else {
      var vn = R.variants || {};
      h += '<div class="card"><h3>Revision C: the recommended design</h3><p class="small">Revision C starts from revision B and takes in what the analyses found. Every number below is computed by the engineering model for both revisions; nothing is typed in.</p>' +
        ((S.variant_links || []).length > 1 ? '<p style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">' + S.variant_links.map(function (v) { return '<a class="btn' + (v.current ? " primary" : "") + '" href="' + esc(v.href) + '#recommendations" style="text-decoration:none">' + esc(v.name) + (v.current ? " · shown" : " · open →") + "</a>"; }).join("") + "</p>" : "") + "</div>";
      h += (R.changes || []).map(function (c) {
        var tbl = function (rows, a, b) {
          return rows && rows.length ? '<div class="tw" style="margin-top:6px"><table class="cmp"><thead><tr><th>Quantity</th><th>B</th><th>C</th></tr></thead><tbody>' + rows.map(function (r) {
            return "<tr><td>" + esc(r.label || r.param || r.metric) + '</td><td class="num">' + fmtv(r[a], r.unit) + '</td><td class="num">' + fmtv(r[b], r.unit) + "</td></tr>"; }).join("") + "</tbody></table></div>" : "";
        };
        return '<div class="issue" style="border-left-color:var(--accent2);margin-top:12px"><div class="hd"><span class="mono">' + esc(c.id) + "</span><b>" + esc(c.title) + '</b><span class="muted small">' + esc([c.requirement, c.analysis].filter(Boolean).join(" · ")) + "</span></div>" +
          '<p class="small">' + esc(c.what) + "</p>" + (c.why ? '<p class="small muted">' + esc(c.why) + "</p>" : "") +
          (c.evidence ? '<p class="small"><b>Evidence:</b> ' + esc(c.evidence) + "</p>" : "") +
          tbl(c.params, "B", "C") + tbl(c.effects, "B", "C") +
          ((c.analyses || []).length ? '<p class="small muted" style="margin-top:6px">' + c.analyses.map(function (a) { return esc(a.analysis + ": " + (a.note || ((a.status_B || "") + " / " + (a.status_C || "")))); }).join(" · ") + "</p>" : "") + "</div>";
      }).join("");
      var cmp = R.comparison || [];
      if (cmp.length) {
        var kinds = {requirement: "Requirements", gate: "Gates", metric: "Key quantities"};
        h += '<div class="card" style="margin-top:12px"><h3>' + esc((vn.B || {}).name || "B") + " vs " + esc((vn.C || {}).name || "C") + "</h3>" + Object.keys(kinds).map(function (k) {
          var rows = cmp.filter(function (r) { return r.kind === k; }); if (!rows.length) return "";
          return "<h4>" + kinds[k] + '</h4><div class="tw"><table class="cmp"><thead><tr><th>Item</th><th>B</th><th>C</th>' + (k !== "metric" ? "<th>Status B</th><th>Status C</th>" : "") + "</tr></thead><tbody>" + rows.map(function (r) {
            var sb = norm(r.status_B), sc = norm(r.status_C), better = (RANK[sb] || 0) > (RANK[sc] || 0), worse = (RANK[sc] || 0) > (RANK[sb] || 0), same = String(r.B) === String(r.C);
            return "<tr><td>" + (r.id && k !== "metric" ? '<span class="mono">' + esc(r.id) + "</span> " : "") + esc(r.label) + (r.criterion ? ' <span class="muted small">' + esc(r.criterion) + "</span>" : "") + '</td><td class="num">' + fmtv(r.B, r.unit) + '</td><td class="num' + (better ? " better" : worse ? " worse" : "") + '"' + (same ? ' style="color:var(--ink3)"' : "") + ">" + fmtv(r.C, r.unit) + "</td>" +
              (k !== "metric" ? "<td>" + (r.status_B ? chip(sb) : "") + "</td><td>" + (r.status_C ? chip(sc) : "") + "</td>" : "") + "</tr>";
          }).join("") + "</tbody></table></div>";
        }).join("") + "</div>";
      }
      if ((R.operating_limits || []).length) h += '<div class="card" style="margin-top:12px"><h3>Flight card for revision C</h3><ul style="margin:0;padding-left:18px;font-size:12.8px;display:grid;gap:5px">' + R.operating_limits.map(function (l) { return "<li>" + esc(l) + "</li>"; }).join("") + "</ul></div>";
      var up = R.datasheet_upgrades || [], nu = R.datasheets_checked_not_upgraded || [];
      if (up.length || nu.length) h += '<div class="card" style="margin-top:12px"><h3>Estimates checked against datasheets</h3>' +
        (up.length ? "<h4>Confirmed from a manufacturer source</h4><ul style=\"margin:0;padding-left:18px;font-size:12.8px;display:grid;gap:6px\">" + up.map(function (u) { return "<li><b>" + esc(u.input) + "</b><br><span class=\"muted\">" + esc(u.now) + "</span>" + refLinks(u.refs) + (u.status_change ? '<br><span class="small">' + esc(u.status_change) + "</span>" : "") + "</li>"; }).join("") + "</ul>" : "") +
        (nu.length ? "<h4>Checked, still an estimate</h4><ul style=\"margin:0;padding-left:18px;font-size:12.8px;display:grid;gap:6px\">" + nu.map(function (u) { return "<li><b>" + esc(u.input) + "</b><br><span class=\"muted\">" + esc(u.checked) + "</span>" + refLinks(u.refs) + (u.stays ? '<br><span class="small">' + esc(u.stays) + "</span>" : "") + "</li>"; }).join("") + "</ul>" : "") + "</div>";
      var na = R.not_applied || [];
      if (na.length) h += '<div class="card" style="margin-top:12px"><h3>Evaluated but not applied</h3><ul style="margin:0;padding-left:18px;font-size:12.8px;display:grid;gap:6px">' + na.map(function (x) { return "<li><b>" + esc(x.title) + "</b> " + (x.requirement ? '<span class="mono small">' + esc(x.requirement) + "</span>" : "") + '<br><span class="muted">' + esc(x.why_not || x.why || "") + "</span></li>"; }).join("") + "</ul></div>";
    }
    $("#pane-recommendations").innerHTML = h;
    built.recommendations = true;
  }
  /* ---- measurements that close the conditionals */
  function measItems() {
    var seen = {}, out = [];
    ((R && R.evidence_needed) || []).forEach(function (e) {
      (e.closes_by_test || []).forEach(function (t) {
        var k = t.test_id + "|" + t.quantity;
        if (!seen[k]) { seen[k] = {test_id: t.test_id, quantity: t.quantity, unit: t.unit, how: t.how, reqs: [], lines: []}; out.push(seen[k]); }
        seen[k].reqs.push(e.id);
        var m = String(e.pass_line || "").match(new RegExp(":\\s*" + t.quantity + "\\s*(<=|>=|<|>)\\s*([-\\d.]+)"));
        if (m) seen[k].lines.push({req: e.id, op: m[1], value: +m[2]});
      });
    });
    return out;
  }
  function evidencePanel() {
    var ev = (R && R.evidence_needed) || [];
    if (!ev.length) return "";
    var items = measItems();
    return '<div class="card" style="margin-top:12px"><h3>How each conditional becomes a pass</h3><p class="small">' + ev.length + " requirements are conditional. None can be passed by changing the model: a test-verified requirement passes only with a recorded measurement, and an estimate-based one when a datasheet or measurement replaces the estimate.</p>" +
      '<div class="tw" style="margin-top:8px"><table><thead><tr><th>Req.</th><th>Why conditional</th><th>Closes with</th><th>Pass line</th></tr></thead><tbody>' + ev.map(function (e) {
        return '<tr><td class="mono">' + esc(e.id) + "</td><td class=\"small\">" + esc(e.why_conditional) + ((e.estimated_inputs || []).length ? '<details><summary class="small">' + e.estimated_inputs.length + " estimated inputs</summary><ul>" + e.estimated_inputs.map(function (x) { return "<li>" + esc(x.input) + (x.note ? ' <span class="muted">— ' + esc(x.note) + "</span>" : "") + "</li>"; }).join("") + "</ul></details>" : "") +
          '</td><td class="small">' + (e.closes_by_test || []).map(function (t) { return '<b class="mono">' + esc(t.test_id) + "</b> " + esc(t.quantity.replace(/_/g, " ")) + " (" + esc(t.unit) + ")"; }).join("<br>") + ((e.closes_by_data || []).length ? "<br>" + e.closes_by_data.map(function (d) { return esc(typeof d === "string" ? d : (d.input || JSON.stringify(d))); }).join("<br>") : "") + '</td><td class="small mono">' + esc(e.pass_line) + "</td></tr>";
      }).join("") + "</tbody></table></div>" +
      "<h4>Record measurements</h4><p class=\"small\">Type what you measured. Simple pass lines are checked here; “Copy measurements” gives the CSV the app imports to update the requirements (the app checks combined pass lines such as thrust-to-weight).</p>" +
      '<div class="meas" style="margin-top:8px">' + items.map(function (t, i) {
        return '<div class="ml"><b><span class="mono">' + esc(t.test_id) + "</span> " + esc(t.quantity.replace(/_/g, " ")) + '</b><span>for ' + esc(t.reqs.join(", ")) + " · " + esc(t.how || "") + '</span></div><input type="number" step="any" data-meas="' + i + '" placeholder="' + esc(t.unit) + '" aria-label="Measured ' + esc(t.quantity) + '"><span data-mres="' + i + '" class="small muted">' + esc(t.lines.map(function (l) { return l.op + " " + l.value; }).join(", ") || "combined check") + "</span>";
      }).join("") + '</div><p style="margin-top:10px"><button class="btn primary" id="meas-copy">Copy measurements (CSV)</button> <span class="small muted" id="meas-msg"></span></p></div>';
  }
  function wireEvidence(root) {
    var items = measItems(), ops = {"<=": function (a, b) { return a <= b; }, ">=": function (a, b) { return a >= b; }, "<": function (a, b) { return a < b; }, ">": function (a, b) { return a > b; }};
    $$("[data-meas]", root).forEach(function (inp) {
      inp.addEventListener("input", function () {
        var t = items[+inp.dataset.meas], v = parseFloat(inp.value), out = $('[data-mres="' + inp.dataset.meas + '"]', root);
        if (isNaN(v) || !t.lines.length) { out.innerHTML = esc(t.lines.map(function (l) { return l.op + " " + l.value; }).join(", ") || "combined check"); return; }
        out.innerHTML = t.lines.map(function (l) { return chip(ops[l.op](v, l.value) ? "pass" : "fail", l.req + (ops[l.op](v, l.value) ? " would pass" : " would fail")); }).join(" ");
      });
    });
    var b = $("#meas-copy", root);
    if (b) b.addEventListener("click", function () {
      var lines = ["test_id,quantity,value,unit,date,operator,notes,variant"], today = new Date().toISOString().slice(0, 10);
      $$("[data-meas]", root).forEach(function (inp) { if (inp.value === "") return; var t = items[+inp.dataset.meas]; lines.push([t.test_id, t.quantity, inp.value, t.unit, today, "", "entered on the design review page", S.variant].map(function (x) { return '"' + String(x).replace(/"/g, '""') + '"'; }).join(",")); });
      var txt = lines.join("\n"), msg = $("#meas-msg", root);
      if (lines.length < 2) { msg.textContent = "Enter at least one measured value first."; return; }
      var fallback = function () { var ta = document.createElement("textarea"); ta.value = txt; ta.style.width = "100%"; ta.rows = Math.min(10, lines.length + 1); msg.textContent = "Copy the text below:"; msg.after(ta); ta.select(); };
      try { navigator.clipboard.writeText(txt).then(function () { msg.textContent = (lines.length - 1) + " measurement(s) copied."; }, fallback); } catch (e) { fallback(); }
    });
  }

  /* ================================================================== PROCESS (knowledge graph + flows) */
  var cyKG = null, cyFlow = null, flowId = null;
  var KIND_COL = {input: "#6b8fb3", calc: "#7a8794", heavy: "#e2601a", requirement: "#2a6aa6", gate: "#14202a", component: "#9b6bb3", metric: "#5c9c8a"};
  function stCol(s) { var n = norm(s); return {pass: "#2c7a47", ok: "#2c7a47", current: "#2c7a47", cond: "#c48a0a", watch: "#c48a0a", stale: "#c48a0a", fail: "#b42f29"}[n] || "#9aa6b0"; }
  function viewProcess() {
    enterMode("process");
    if (built.process) { if (cyKG) cyKG.resize(); if (cyFlow) cyFlow.resize(); return; }
    built.process = true;
    var flows = X.process_flows || [];
    $("#pane-process").innerHTML = '<div class="grid2"><div><h2>Process flows</h2><div class="filters flowtabs" role="group">' + flows.map(function (f, i) { return '<button data-flow="' + esc(f.id) + '" aria-pressed="' + (i === 0) + '">' + esc(f.title) + "</button>"; }).join("") +
      '</div><div class="graph" id="flowgraph"><div class="graph-side" id="flowside"></div></div></div>' +
      '<div><h2>Knowledge graph</h2><p class="small muted" style="margin:8px 0">How inputs flow through the calculations and solver runs into requirements and gates. Colour = status; click a node.</p><div class="filters" id="kgk" role="group"></div>' +
      '<div class="graph" id="kg"><div class="graph-side" id="kgside"></div></div></div></div>';
    if (typeof cytoscape === "undefined") { $("#pane-process").insertAdjacentHTML("afterbegin", '<p class="small muted">Graph library not loaded.</p>'); return; }
    try { if (window.cytoscapeDagre) cytoscape.use(window.cytoscapeDagre); } catch (e) { /* already registered */ }
    var ink = css("--ink") || "#131c24";
    var style = [
      {selector: "node", style: {"label": "data(label)", "font-family": "IBM Plex Sans, Segoe UI, sans-serif", "font-size": 11, "color": ink, "text-wrap": "wrap", "text-max-width": 130,
        "text-valign": "center", "text-halign": "center", "shape": "round-rectangle", "width": "label", "height": "label", "padding": 8, "background-color": "#ffffff", "border-width": 2, "border-color": "data(col)"}},
      {selector: "node[kind='gate']", style: {"background-color": "data(col)", "color": "#fff", "font-weight": 600}},
      {selector: "node[kind='heavy']", style: {"border-width": 3, "font-weight": 600}},
      {selector: "edge", style: {"width": 1.3, "line-color": "#a7b2bc", "target-arrow-color": "#a7b2bc", "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": 0.8,
        "label": "data(label)", "font-size": 9, "color": "#77838d", "text-rotation": "autorotate"}},
      {selector: ".dim", style: {"opacity": 0.15}},
      {selector: "node:selected", style: {"border-color": "#e2601a", "border-width": 4}}
    ];
    function drawFlow(id) {
      var f = flows.filter(function (x) { return x.id === id; })[0] || flows[0]; if (!f) return; flowId = f.id;
      var els = f.steps.map(function (s) { return {data: {id: "s_" + s.id, label: s.label, col: s.status ? stCol(s.status) : "#2a6aa6", kind: "step", raw: s}}; })
        .concat((f.edges || []).map(function (e, i) { return {data: {id: "e" + i, source: "s_" + e[0], target: "s_" + e[1], label: e[2] || ""}}; }));
      if (cyFlow) cyFlow.destroy();
      cyFlow = cytoscape({container: $("#flowgraph"), elements: els, style: style, layout: {name: window.cytoscapeDagre ? "dagre" : "breadthfirst", rankDir: "TB", nodeSep: 22, rankSep: 34, directed: true, padding: 16},
        wheelSensitivity: 0.25, minZoom: 0.2, maxZoom: 3});
      $("#flowside").innerHTML = "";
      cyFlow.on("tap", "node", function (ev) {
        var s = ev.target.data("raw");
        $("#flowside").innerHTML = "<b>" + esc(s.label) + "</b>" + (s.status ? " " + chip(norm(s.status)) : "") + (s.detail ? '<p class="small" style="margin-top:4px">' + esc(s.detail) + "</p>" : "") +
          ((s.parts || []).length ? '<p class="small" style="margin-top:4px">Parts: ' + esc(s.parts.map(function (k) { return AC.parts[k] ? AC.parts[k].name.split(" — ")[0] : k; }).join(", ")) + "</p>" : "");
      });
      cyFlow.on("tap", function (ev) { if (ev.target === cyFlow) $("#flowside").innerHTML = ""; });
    }
    $$("[data-flow]").forEach(function (b) { b.addEventListener("click", function () { $$("[data-flow]").forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); }); drawFlow(b.dataset.flow); }); });
    drawFlow(flows[0] && flows[0].id);
    var KG = X.knowledge_graph || {nodes: [], edges: []};
    var kinds = {}; KG.nodes.forEach(function (n) { kinds[n.kind] = 1; });
    $("#kgk").innerHTML = '<button data-kk="all" aria-pressed="true">All</button>' + Object.keys(kinds).map(function (k) { return '<button data-kk="' + esc(k) + '" aria-pressed="false"><span class="dot" style="background:' + (KIND_COL[k] || "#888") + '"></span> ' + esc(k) + "</button>"; }).join("");
    var kgEls = KG.nodes.map(function (n) { return {data: {id: n.id, label: n.label + (n.value_text ? "\n" + n.value_text : ""), kind: n.kind, col: n.status && norm(n.status) !== "nev" ? stCol(n.status) : (KIND_COL[n.kind] || "#888"), raw: n}}; })
      .concat(KG.edges.map(function (e, i) { return {data: {id: "k" + i, source: e.source, target: e.target, label: e.label || ""}}; }));
    cyKG = cytoscape({container: $("#kg"), elements: kgEls, style: style, layout: {name: window.cytoscapeDagre ? "dagre" : "cose", rankDir: "LR", nodeSep: 8, rankSep: 70, padding: 12},
      wheelSensitivity: 0.25, minZoom: 0.1, maxZoom: 3});
    cyKG.ready(function () {  // fit-to-view makes ~150 nodes unreadable: start readable, centred on the gates
      if (cyKG.zoom() < 0.6) { var gates = cyKG.nodes("[kind='gate']"); cyKG.zoom(0.75); cyKG.center(gates.length ? gates : cyKG.nodes()); }
    });
    $("#kg").insertAdjacentHTML("beforeend", '<button class="btn" id="kgfit" style="position:absolute;left:10px;top:10px;z-index:2">Fit all</button>');
    $("#kgfit").addEventListener("click", function () { cyKG.fit(undefined, 12); });
    cyKG.on("tap", "node", function (ev) {
      var n = ev.target, raw = n.data("raw"), hood = n.closedNeighborhood().union(n.predecessors()).union(n.successors());
      cyKG.elements().addClass("dim"); hood.removeClass("dim");
      $("#kgside").innerHTML = "<b>" + esc(raw.label) + "</b> " + (raw.status ? chip(norm(raw.status)) : "") + '<p class="small muted">' + esc(raw.kind) + (raw.group ? " · " + esc(raw.group) : "") + "</p>" +
        (raw.value_text ? '<p class="small num">' + esc(raw.value_text) + "</p>" : "") + (raw.detail ? '<p class="small">' + esc(raw.detail) + "</p>" : "") +
        '<p class="small" style="margin-top:4px">Upstream ' + n.predecessors("node").length + " · downstream " + n.successors("node").length + "</p>";
    });
    cyKG.on("tap", function (ev) { if (ev.target === cyKG) { cyKG.elements().removeClass("dim"); $("#kgside").innerHTML = ""; } });
    $$("[data-kk]").forEach(function (b) {
      b.addEventListener("click", function () {
        $$("[data-kk]").forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); });
        var k = b.dataset.kk; cyKG.elements().removeClass("dim");
        if (k !== "all") { cyKG.nodes().filter(function (n) { return n.data("kind") !== k; }).addClass("dim"); cyKG.edges().addClass("dim"); }
      });
    });
  }

  /* ================================================================== AUDIT */
  function viewAudit() {
    enterMode("audit");
    if (built.audit) return; built.audit = true;
    var rs = {PASS: "pass", FAIL: "fail", "CONDITIONAL / REVIEW REQUIRED": "cond", "NOT EVALUATED": "nev"};
    var groups = {}; (S.metrics || []).forEach(function (m) { (groups[m.group] = groups[m.group] || []).push(m); });
    $("#pane-audit").innerHTML = "<h2>Requirements</h2><div class=\"tw\"><table><thead><tr><th>ID</th><th>Requirement</th><th>Limit</th><th>Predicted</th><th>Status</th><th>Basis</th><th>Verified by</th></tr></thead><tbody>" +
      (S.requirements || []).map(function (r) {
        return "<tr><td class=\"mono\">" + r.id + "</td><td>" + esc(r.text) + "</td><td class=\"num\">" + esc(r.op) + " " + esc(r.threshold) + " " + esc(r.unit || "") + "</td><td class=\"num\">" +
          (r.predicted == null ? "—" : (typeof r.predicted === "number" ? fmt(r.predicted, Math.abs(r.predicted) < 10 ? 3 : 1) : esc(r.predicted))) + "</td><td>" + chip(rs[r.status] || "nev") + "</td><td class=\"small\">" + esc(r.why) + "</td><td>" + esc(r.verification) + "</td></tr>";
      }).join("") + "</tbody></table></div>" +
      "<h2>Solver runs</h2><div class=\"tw\"><table><thead><tr><th>Analysis</th><th>Status</th><th>Result</th><th>Run</th></tr></thead><tbody>" + (S.analyses || []).map(function (a) {
        return "<tr><td>" + esc(a.label) + "</td><td>" + chip(a.status) + "</td><td class=\"small\">" + esc(a.result || sysResult(a)) + "</td><td class=\"num small\">" + esc(a.time || "") + "</td></tr>";
      }).join("") + "</tbody></table></div>" +
      '<div class="grid2"><div><h2>Inputs that are estimates</h2><div class="tw"><table><thead><tr><th>Input</th><th>Value</th><th>Basis</th></tr></thead><tbody>' + (S.estimates || []).map(function (x) {
        return "<tr><td class=\"mono\">" + esc(x.name) + "</td><td class=\"num\">" + (x.value == null ? "missing" : esc(x.value) + " " + esc(x.unit)) + "</td><td class=\"small\">" + esc(x.ref) + "</td></tr>";
      }).join("") + '</tbody></table></div></div><div><h2>Open inputs</h2><ul style="padding-left:18px;font-size:13px;display:grid;gap:5px">' + (S.unresolved || []).map(function (u) { return "<li><b>" + esc(u.item) + "</b> — " + esc(u.why) + "</li>"; }).join("") + "</ul></div></div>" +
      "<h2>All monitored quantities</h2><div class=\"tw\"><table><thead><tr><th>Quantity</th><th>Value</th><th>Evidence</th><th>Basis</th></tr></thead><tbody>" + Object.keys(groups).map(function (g) {
        return "<tr><th colspan=\"4\">" + esc(g) + "</th></tr>" + groups[g].map(function (m) {
          return "<tr><td>" + esc(m.label) + "</td><td class=\"num\">" + fmt(m.value, Math.abs(m.value) < 10 ? 2 : 1) + " " + esc(m.unit) + "</td><td class=\"small\">" + esc((m.source || "").replace("provisional_estimate", "estimate").replace("_", " ")) + "</td><td class=\"small\">" + esc((m.ref || "").slice(0, 160)) + "</td></tr>";
        }).join("");
      }).join("") + "</tbody></table></div>" +
      "<h2>Test matrix</h2><div class=\"tw\"><table><thead><tr><th>Test</th><th>What</th><th>Requirements</th><th>Pass line</th></tr></thead><tbody>" + (S.tests || []).map(function (t) {
        return "<tr><td class=\"mono\">" + esc(t.id) + "</td><td>" + esc(t.title) + "</td><td class=\"mono small\">" + esc((t.reqs || []).join(", ")) + "</td><td class=\"small\">" + esc(t.acceptance) + "</td></tr>";
      }).join("") + "</tbody></table></div>";
  }

  /* ================================================================== routing */
  var built = {}, currentView = null;
  var VIEWS = {overview: viewOverview, design: viewDesign, analyses: viewAnalyses, flight: viewFlight, components: viewComponents, decisions: viewDecisions, recommendations: viewRecommendations, process: viewProcess, audit: viewAudit};
  function route() {
    var v = (location.hash || "#overview").slice(1);
    if (!VIEWS[v]) v = "overview";
    currentView = v;
    Object.keys(VIEWS).forEach(function (n) { var p = $("#pane-" + n); if (p) p.hidden = n !== v; });
    document.body.classList.toggle("wide-mode", v === "process" || v === "audit");
    $$(".tabs a").forEach(function (a) { if (a.dataset.view === v) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
    var pc = $("#partcard"); if (pc) pc.remove();
    VIEWS[v]();
    built[v] = true;
    V.resize();
  }
  (function variants() {
    var L = S.variant_links || [], el = $("#variant-switch"); if (L.length < 2) return;
    el.hidden = false;
    el.innerHTML = L.map(function (v) { return '<a href="' + esc(v.href) + '" aria-current="' + !!v.current + '" title="' + esc(v.name) + '">' + esc(v.name.replace(" (recommended)", " ★")) + "</a>"; }).join("");
    $$("a", el).forEach(function (a) { a.addEventListener("click", function () { a.href = a.getAttribute("href").split("#")[0] + (location.hash || ""); }); });
  })();
  (function pill() {
    var vi = verdictInfo();
    $("#verdict-pill").innerHTML = chip(vi.cls === "fail" ? "fail" : vi.cls === "watch" ? "cond" : "pass", vi.headline);
  })();
  window.addEventListener("hashchange", route);
  route();
})();
