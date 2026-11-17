/* K09-L design review - front end. Data: window.K09L_SUMMARY, K09L_FIELD_INFO, K09L_AIRCRAFT, K09L_FIELDS
   (written by opencae.web.build). Aircraft frame: mm, x aft, y right, z up; the 3D views use z up. */
(function () {
  "use strict";
  var S = window.K09L_SUMMARY, FI = window.K09L_FIELD_INFO || {}, AC = window.K09L_AIRCRAFT, FD = window.K09L_FIELDS || {};
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var esc = function (t) { return String(t == null ? "" : t).replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); };
  var fmt = function (v, nd) { return v == null || isNaN(v) ? "—" : Number(v).toLocaleString(undefined, {minimumFractionDigits: nd || 0, maximumFractionDigits: nd || 0}); };
  var ST = {pass: ["pass", "✓", "Pass"], cond: ["cond", "◐", "Conditional"], fail: ["fail", "✕", "Fail"], nev: ["nev", "○", "Not evaluated"],
            ok: ["ok", "✓", "OK"], watch: ["watch", "◐", "Watch"], current: ["current", "✓", "current"], stale: ["stale", "◐", "stale"],
            "not run": ["not-run", "○", "not run"], failed: ["failed", "✕", "failed"]};
  var chip = function (s, label) { var d = ST[s] || ST.nev; return '<span class="chip ' + d[0] + '"><i>' + d[1] + "</i>" + esc(label || d[2]) + "</span>"; };
  var STATUS_RGB = {ok: [0.18, 0.55, 0.30], watch: [0.85, 0.62, 0.05], fail: [0.80, 0.20, 0.17]};

  function b64(s, Type) {
    var bin = atob(s), n = bin.length, u8 = new Uint8Array(n);
    for (var i = 0; i < n; i++) u8[i] = bin.charCodeAt(i);
    return new Type(u8.buffer);
  }

  /* ------------------------------------------------------------------ colour maps */
  var MAPS = {
    turbo: [[0.19, 0.07, 0.23], [0.27, 0.42, 0.89], [0.16, 0.73, 0.91], [0.22, 0.93, 0.56], [0.65, 0.99, 0.23], [0.96, 0.82, 0.18], [0.98, 0.49, 0.13], [0.80, 0.16, 0.03], [0.48, 0.02, 0.01]],
    ylorrd: [[1.00, 1.00, 0.80], [1.00, 0.93, 0.63], [1.00, 0.85, 0.46], [1.00, 0.70, 0.30], [0.99, 0.55, 0.24], [0.99, 0.31, 0.16], [0.89, 0.10, 0.11], [0.74, 0.00, 0.15], [0.50, 0.00, 0.15]],
    coolwarm: [[0.23, 0.30, 0.75], [0.48, 0.62, 0.97], [0.75, 0.83, 0.96], [0.87, 0.87, 0.87], [0.96, 0.77, 0.68], [0.92, 0.49, 0.38], [0.71, 0.02, 0.15]]
  };
  function cmap(name, t) {
    var m = MAPS[name], x = Math.min(Math.max(t, 0), 1) * (m.length - 1), i = Math.min(Math.floor(x), m.length - 2), f = x - i;
    return [m[i][0] + (m[i + 1][0] - m[i][0]) * f, m[i][1] + (m[i + 1][1] - m[i][1]) * f, m[i][2] + (m[i + 1][2] - m[i][2]) * f];
  }
  function mapFor(disc) { return disc === "fluid" ? "coolwarm" : disc === "thermal" ? "ylorrd" : "turbo"; }
  function gradientCss(name) { return "linear-gradient(90deg," + MAPS[name].map(function (c) { return "rgb(" + c.map(function (v) { return Math.round(v * 255); }).join(",") + ")"; }).join(",") + ")"; }

  /* ------------------------------------------------------------------ 3D viewer */
  function Viewer(el, opts) {
    opts = opts || {};
    this.el = el;
    var w = el.clientWidth || 600, h = el.clientHeight || 400;
    this.renderer = new THREE.WebGLRenderer({antialias: true, alpha: true});
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setSize(w, h);
    el.appendChild(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(35, w / h, 1, 20000);
    this.camera.up.set(0, 0, 1);
    this.camera.position.set(-900, -1250, 650);
    this.controls = new THREE.OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.autoRotate = !!opts.autoRotate;
    this.controls.autoRotateSpeed = 0.6;
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x8899aa, 0.85));
    var d = new THREE.DirectionalLight(0xffffff, 0.75); d.position.set(-1, -1.4, 2.2); this.scene.add(d);
    var d2 = new THREE.DirectionalLight(0xffffff, 0.3); d2.position.set(1.2, 1, -0.5); this.scene.add(d2);
    this.root = new THREE.Group(); this.scene.add(this.root);
    this.tick = [];
    var self = this;
    new ResizeObserver(function () { self.resize(); }).observe(el);
    var last = performance.now();
    (function loop(now) {
      var dt = Math.min((now - last) / 1000, 0.1); last = now;
      self.tick.forEach(function (f) { f(dt); });
      self.controls.update();
      self.renderer.render(self.scene, self.camera);
      requestAnimationFrame(loop);
    })(last);
  }
  Viewer.prototype.resize = function () {
    var w = this.el.clientWidth, h = this.el.clientHeight; if (!w || !h) return;
    this.renderer.setSize(w, h); this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
  };
  Viewer.prototype.frame = function (obj, dir) {
    var box = new THREE.Box3().setFromObject(obj), c = box.getCenter(new THREE.Vector3()), r = box.getSize(new THREE.Vector3()).length() / 2;
    dir = (dir || new THREE.Vector3(-0.55, -0.75, 0.4)).normalize();
    var dist = r / Math.tan(this.camera.fov * Math.PI / 360) * 1.05;
    this.controls.target.copy(c);
    this.camera.position.copy(c.clone().add(dir.multiplyScalar(dist)));
    this.camera.near = Math.max(dist / 200, 0.1); this.camera.far = dist * 20; this.camera.updateProjectionMatrix();
  };
  Viewer.prototype.pick = function (ev, objs) {
    var r = this.renderer.domElement.getBoundingClientRect();
    var m = new THREE.Vector2((ev.clientX - r.left) / r.width * 2 - 1, -(ev.clientY - r.top) / r.height * 2 + 1);
    var rc = new THREE.Raycaster(); rc.setFromCamera(m, this.camera);
    var hit = rc.intersectObjects(objs, false);
    return hit.length ? hit[0] : null;
  };

  /* ------------------------------------------------------------------ aircraft */
  function partStatus(p) {
    var worst = null, order = {fail: 3, watch: 2, ok: 1};
    (p.analyses || []).forEach(function (id) {
      var f = FI[id]; if (!f || !f.failure) return;
      var s = f.failure.status; if (!worst || order[s] > order[worst]) worst = s;
    });
    return worst;
  }
  function buildAircraft(tint) {
    var g = new THREE.Group(), meshes = [], rotors = AC.rotors.map(function (r) {
      var pivot = new THREE.Group(); pivot.position.set(r.origin[0], r.origin[1], r.origin[2]);
      var inner = new THREE.Group(); inner.position.set(-r.origin[0], -r.origin[1], -r.origin[2]);
      pivot.add(inner); g.add(pivot);
      return {pivot: pivot, inner: inner, axis: new THREE.Vector3(r.axis[0], r.axis[1], r.axis[2]).normalize(), sense: r.sense, angle: 0};
    });
    var mats = {};
    AC.bodies.forEach(function (b) {
      var geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(b64(b.pos, Float32Array), 3));
      geo.setIndex(new THREE.BufferAttribute(b64(b.idx, b.i32 ? Uint32Array : Uint16Array), 1));
      geo.computeVertexNormals();
      var p = AC.parts[b.part], st = tint ? partStatus(p) : null;
      var col = st ? STATUS_RGB[st] : (tint ? [0.78, 0.80, 0.83] : b.rgb.map(function (v) { return v / 255; }));
      var key = col.join(",");
      var mat = mats[key] || (mats[key] = new THREE.MeshStandardMaterial({color: new THREE.Color(col[0], col[1], col[2]), roughness: 0.65, metalness: 0.05, side: THREE.DoubleSide}));
      var mesh = new THREE.Mesh(geo, mat);
      mesh.userData = {part: b.part, body: b.name};
      meshes.push(mesh);
      (b.rotor != null ? rotors[b.rotor].inner : g).add(mesh);
    });
    return {group: g, meshes: meshes, rotors: rotors};
  }
  function spin(ac, v) {
    var rpm = AC.hover_rpm || [6500, 6500, 6500, 6500], k = AC.spin_display || 0.025;
    v.tick.push(function (dt) {
      ac.rotors.forEach(function (r, i) {
        r.angle += (rpm[i] || rpm[0]) / 60 * 2 * Math.PI * k * dt * r.sense;
        r.pivot.quaternion.setFromAxisAngle(r.axis, r.angle);
      });
    });
  }

  /* ------------------------------------------------------------------ overview */
  function overview() {
    var v = S.verdict;
    var flagged = S.analyses.filter(function (a) { return a.status === "current" && (a.verdict === "fail" || a.verdict === "watch"); });
    var bad = flagged.filter(function (a) { return a.verdict === "fail"; });
    var cls = v.fail || bad.length ? "fail" : (v.cond || v.nev || flagged.length ? "watch" : "ok");
    var headline = v.fail ? v.fail + " requirement" + (v.fail > 1 ? "s" : "") + " fail" :
      bad.length ? "No requirement fails, but " + bad.length + " analysis flags a problem" : "No requirement fails";
    var next = v.fail ? "Fix the failing items before building." :
      bad.length ? "Resolve or disprove " + bad.map(function (a) { return a.label; }).join(", ") + " before building (click its card below)." :
        "Ready to build and test.";
    $("#verdict").className = "verdict " + cls;
    $("#verdict").innerHTML = "<h1>" + esc(headline) + "</h1><p>" + esc(S.variant) + ". " + esc(next) +
      " Not approved to fly: " + v.cond + " requirements are predictions on estimated data that the tests must confirm." +
      '</p><div class="counts">' + chip("pass", v.pass + " pass") + chip("cond", v.cond + " conditional") + chip("fail", v.fail + " fail") +
      (v.nev ? chip("nev", v.nev + " not evaluated") : "") + "</div>";
    $("#kpis").innerHTML = S.kpis.map(function (k) {
      return '<div class="kpi"><span>' + esc(k.label) + "</span><b>" + (k.unit === "USD" ? "$" : "") + fmt(k.value, k.unit === "" ? 3 : (k.unit === "°" || k.unit === "min" ? 1 : 0)) +
        (k.unit && k.unit !== "USD" ? ' <span class="small">' + esc(k.unit) + "</span>" : "") + "</b><small>" + esc(k.limit) + " " + chip(k.status, k.req) + "</small></div>";
    }).join("");
    var fieldCards = S.analyses.filter(function (a) { return a.has_field; }).map(function (a) {
      var f = FI[a.id], fs = f.failure || {};
      return '<button class="card" data-field="' + a.id + '">' + (f.thumb ? '<img src="' + f.thumb + '" alt="' + esc(f.title) + ' contour">' : "") +
        '<div class="body"><div class="head"><span class="disc">' + esc(f.discipline) + "</span>" + chip(fs.status) + "</div><b>" + esc(f.title) +
        "</b><p>" + esc(shortResult(f)) + "</p></div></button>";
    }).join("");
    $("#analysis-cards").innerHTML = fieldCards || '<p class="muted">No contour analysis is current for this variant.</p>';
    document.querySelectorAll(".card[data-field]").forEach(function (c) {
      c.addEventListener("click", function () { location.hash = "results"; setTimeout(function () { openField(c.dataset.field); }, 60); });
    });
    $("#system-checks").innerHTML = S.analyses.filter(function (a) { return !a.has_field; }).map(function (a) {
      return '<div class="check">' + chip(a.status) + "<b>" + esc(a.label) + "</b><span class=\"muted\">" + esc(sysResult(a)) + "</span></div>";
    }).join("");
    $("#base-name").textContent = S.baseline;
    $("#changes").innerHTML = (S.changes || []).map(function (c) {
      return "<li><b>" + esc(c.title) + "</b><span>" + esc(c.why.split(". ")[0]) + ". (" + esc(c.requirement) + ")</span></li>";
    }).join("") || "<li>No changes from the baseline.</li>";
    $("#limits").innerHTML = (S.limits || []).map(function (l) { return "<li>" + esc(l.split(":")[0]) + "</li>"; }).join("") || "<li>None recorded.</li>";
    $("#tests-top").innerHTML = S.tests.slice(0, 5).map(function (t) { return "<li><b>" + esc(t.id) + "</b> " + esc(t.title) + "</li>"; }).join("");
    var vt = '<thead><tr><th>Variant</th><th>Pass</th><th>Cond.</th><th>Fail</th><th>Mass</th><th>Tip-over</th><th>Box fit</th><th>Current margin</th><th>Static margin</th><th>Analyses</th></tr></thead><tbody>' +
      S.variants.map(function (r) {
        return '<tr class="' + (r.selected ? "sel" : "") + '"><td><b>' + esc(r.name) + "</b>" + (r.baseline ? ' <span class="muted small">baseline</span>' : "") + (r.selected ? ' <span class="muted small">shown</span>' : "") +
          '</td><td class="num">' + r.counts.pass + '</td><td class="num">' + r.counts.cond + '</td><td class="num">' + (r.counts.fail ? '<b style="color:var(--fail)">' + r.counts.fail + "</b>" : 0) +
          '</td><td class="num">' + fmt(r.mass_g) + ' g</td><td class="num">' + fmt(r.tipover, 1) + '°</td><td class="num">' + fmt(r.box_fit, 3) +
          '</td><td class="num">' + fmt(r.current_margin, 2) + '</td><td class="num">' + fmt(r.static_margin, 2) + ' %</td><td class="num">' + r.analyses_current + "/" + S.analyses.length + "</td></tr>";
      }).join("") + "</tbody>";
    $("#variants").innerHTML = vt;
    var hv = new Viewer($("#hero-viewer"), {autoRotate: true});
    var ac = buildAircraft(false);
    hv.root.add(ac.group);
    hv.frame(ac.group);
    spin(ac, hv);
    var rpm = AC.hover_rpm ? Math.round(AC.hover_rpm.reduce(function (a, b) { return a + b; }, 0) / AC.hover_rpm.length) : null;
    $("#spin-note").textContent = "Drag to rotate. Props turn at 1/" + Math.round(1 / AC.spin_display) + " of their simulated hover speed" + (rpm ? " (" + rpm.toLocaleString() + " rpm)" : "") + ".";
  }
  function shortResult(f) {
    var fs = f.failure || {};
    if (f.id === "thermal_mount") return "Hottest point " + fmt(fs.value, 1) + " °C vs " + fmt(fs.allowable, 0) + " °C limit, margin " + fmt(fs.margin, 1) + " K";
    if (f.id === "cfd_cruise") return "Lift at trim " + fmt((fs.safety_factor || 0) * 100, 0) + " % of what level cruise needs; L/D " + fmt((f.extra.cl || 0) / (f.extra.cd || 1), 1);
    return "Safety factor " + fmt(fs.safety_factor, fs.safety_factor < 10 ? 2 : 1) + " (" + fmt(fs.value, 1) + " vs " + fmt(fs.allowable, 0) + " " + (fs.unit || "") + ")";
  }
  function sysResult(a) {
    var s = a.summary || {};
    if (a.id === "assembly_check" && s) return (s.interferences != null ? s.interferences + " interference, " : "") + (s.prop_clearance_mm != null ? "props clear by " + s.prop_clearance_mm + " mm" : "");
    if (a.id === "flight_hover" && s.max_position_error_mm != null) return "holds position within " + fmt(s.max_position_error_mm, 1) + " mm";
    if (a.id === "flight_transition" && s.height_loss_m != null) return "loses " + fmt(s.height_loss_m, 2) + " m in the transition";
    return a.reason || "";
  }

  /* ------------------------------------------------------------------ results */
  var RV = null, RAC = null, current = null, fieldObj = null, filter = "all";
  function results() {
    if (RV) return;
    RV = new Viewer($("#res-viewer"));
    RAC = buildAircraft(true);
    RV.root.add(RAC.group); RV.frame(RAC.group); spin(RAC, RV);
    var down = null;
    RV.renderer.domElement.addEventListener("pointerdown", function (e) { down = [e.clientX, e.clientY]; });
    RV.renderer.domElement.addEventListener("pointerup", function (e) {
      if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 5) return;
      if (fieldObj) { pickField(e); return; }
      var h = RV.pick(e, RAC.meshes); if (!h) return;
      var p = AC.parts[h.object.userData.part];
      if (p.analyses && p.analyses.length) openField(p.analyses[0], p.key); else showPart(p);
    });
    var tip = document.createElement("div"); tip.className = "tip3d"; tip.hidden = true; $("#res-viewer").appendChild(tip);
    RV.renderer.domElement.addEventListener("pointermove", function (e) {
      if (fieldObj) { tip.hidden = true; return; }
      var h = RV.pick(e, RAC.meshes);
      if (!h) { tip.hidden = true; return; }
      var p = AC.parts[h.object.userData.part], r = $("#res-viewer").getBoundingClientRect();
      tip.textContent = p.name + (p.analyses.length ? "  ·  click for results" : "");
      tip.style.left = (e.clientX - r.left + 12) + "px"; tip.style.top = (e.clientY - r.top + 12) + "px"; tip.hidden = false;
    });
    document.querySelectorAll(".filters button").forEach(function (b) {
      b.addEventListener("click", function () {
        filter = b.dataset.f;
        document.querySelectorAll(".filters button").forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); });
        list();
      });
    });
    $("#btn-back").addEventListener("click", aircraftMode);
    $("#crit-only").addEventListener("change", function () { if (current) colour(current); });
    list();
    showIntro();
  }
  function list() {
    $("#field-list").innerHTML = Object.keys(FI).filter(function (id) { return filter === "all" || FI[id].discipline === filter; }).map(function (id) {
      var f = FI[id];
      return '<li><button data-id="' + id + '" aria-current="' + (current === id) + '"><span class="dot ' + (f.failure.status || "nev") + '"></span>' + esc(f.title) + '<span class="disc">' + esc({structural: "FEA", thermal: "Heat", fluid: "CFD"}[f.discipline] || f.discipline) + "</span></button></li>";
    }).join("") || '<li class="muted small">No analysis in this discipline.</li>';
    document.querySelectorAll("#field-list button").forEach(function (b) { b.addEventListener("click", function () { openField(b.dataset.id); }); });
  }
  function showIntro() {
    var n = Object.keys(FI).length;
    $("#detail").innerHTML = "<h3>Pick a part</h3><p class=\"small\">" + n + " contour analyses: " +
      Object.keys(FI).map(function (id) { return esc(FI[id].title); }).join(", ") +
      ". Coloured parts on the model have one; grey parts are covered by the level-1 checks (mass, CG, loads) on the Audit page.</p>";
  }
  function showPart(p) {
    $("#detail").innerHTML = "<h3>" + esc(p.name) + "</h3><dl class=\"kv\"><dt>Part number</dt><dd>" + esc(p.pn) + "</dd><dt>Quantity</dt><dd>" + esc(p.qty) +
      "</dd><dt>Mass each</dt><dd>" + (p.mass_g == null ? "—" : fmt(p.mass_g, 1) + " g <span class=\"muted\">(" + esc(p.mass_source) + ")</span>") + "</dd><dt>Group</dt><dd>" + esc(p.group) + "</dd></dl>" +
      '<p class="small muted">No contour analysis for this part. Its mass and position feed the level-1 checks (mass, CG, tip-over, loads).</p>' + (p.notes ? '<p class="small">' + esc(p.notes) + "</p>" : "");
  }
  function aircraftMode() {
    if (fieldObj) { RV.root.remove(fieldObj); fieldObj.geometry.dispose(); fieldObj = null; }
    RAC.group.visible = true; current = null;
    $("#btn-back").hidden = true; $("#crit-toggle").hidden = true; $("#legend").hidden = true;
    RV.frame(RAC.group); list(); showIntro();
  }
  function openField(id) {
    results();
    var f = FI[id], d = FD[id]; if (!f || !d) return;
    current = id;
    if (fieldObj) { RV.root.remove(fieldObj); fieldObj.geometry.dispose(); }
    var geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(b64(d.pos, Float32Array), 3));
    geo.setIndex(new THREE.BufferAttribute(b64(d.idx, Uint32Array), 1));
    geo.computeVertexNormals();
    geo.setAttribute("color", new THREE.BufferAttribute(new Float32Array(d.n * 3), 3));
    fieldObj = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({vertexColors: true, roughness: 0.75, metalness: 0, side: THREE.DoubleSide}));
    fieldObj.userData = {id: id, val: b64(d.val, Float32Array), crit: b64(d.crit, Uint8Array)};
    // worst-point marker
    var w = f.worst_location_mm, mk = new THREE.Mesh(new THREE.SphereGeometry(1, 16, 12), new THREE.MeshBasicMaterial({color: 0xff2d55}));
    var size = new THREE.Box3().setFromObject(fieldObj).getSize(new THREE.Vector3()).length();
    mk.scale.setScalar(size * 0.012); mk.position.set(w[0], w[1], w[2]); fieldObj.add(mk);
    RV.root.add(fieldObj);
    RAC.group.visible = false;
    colour(id);
    RV.frame(fieldObj, id === "cfd_cruise" ? new THREE.Vector3(-0.5, -0.9, 0.7) : null);
    $("#btn-back").hidden = false; $("#crit-toggle").hidden = false; $("#legend").hidden = false;
    list(); detail(id);
  }
  function colour(id) {
    var f = FI[id], u = fieldObj.userData, c = fieldObj.geometry.getAttribute("color"), lo = f.range[0], hi = f.range[1];
    var m = mapFor(f.discipline), only = $("#crit-only").checked, sym = f.discipline === "fluid";
    if (sym) { var a = Math.max(Math.abs(lo), Math.abs(hi)); lo = -a; hi = Math.min(a, 1.0); lo = -a; }
    for (var i = 0; i < u.val.length; i++) {
      var col = cmap(m, (u.val[i] - lo) / ((hi - lo) || 1));
      if (only && !u.crit[i]) { col = [col[0] * 0.2 + 0.5, col[1] * 0.2 + 0.52, col[2] * 0.2 + 0.55]; } // outside the critical region: neutral grey, a hint of the contour
      c.setXYZ(i, col[0], col[1], col[2]);
    }
    c.needsUpdate = true;
    $("#legend").innerHTML = "<span>" + esc(f.quantity) + "</span><span class=\"num\">" + fmt(lo, 2) + '</span><span class="bar" style="background:' + gradientCss(m) + '"></span><span class="num">' + fmt(hi, 2) + " " + esc(f.unit === "-" ? "" : f.unit) + "</span>";
  }
  function pickField(e) {
    var h = RV.pick(e, [fieldObj]); if (!h) return;
    var u = fieldObj.userData, f = FI[current], idx = [h.face.a, h.face.b, h.face.c];
    var val = (u.val[idx[0]] + u.val[idx[1]] + u.val[idx[2]]) / 3;
    $("#probe").innerHTML = "Probe: <b>" + fmt(val, 2) + " " + esc(f.unit === "-" ? "" : f.unit) + "</b> at (" + h.point.toArray().map(function (x) { return fmt(x, 0); }).join(", ") + ") mm" +
      (u.crit[idx[0]] ? ' · <span style="color:var(--fail)">in the critical region</span>' : "");
  }
  function detail(id) {
    var f = FI[id], fs = f.failure || {}, st = fs.status || "nev";
    var main = id === "thermal_mount" ? [fmt(fs.margin, 1) + " K", "temperature margin"] : id === "cfd_cruise" ? [fmt((fs.safety_factor || 0) * 100, 0) + " %", "of the lift level cruise needs"] :
      [fmt(fs.safety_factor, 2), "safety factor (line " + fmt(fs.line, 1) + ")"];
    var ex = f.extra || {}, sol = f.solver || {}, mesh = f.mesh || {};
    $("#detail").innerHTML = "<h3>" + esc(f.title) + " " + chip(st) + "</h3>" +
      '<div class="fail-card ' + st + '"><span class="small">' + esc(fs.criterion) + '</span><div class="big">' + main[0] + '</div><span class="small">' + esc(main[1]) + "</span></div>" +
      '<p class="small">' + esc(fs.text) + "</p>" +
      '<p class="small" id="probe">Click the contour to read a value.</p>' +
      '<dl class="kv"><dt>Worst value</dt><dd class="num">' + fmt(fs.value, id === "cfd_cruise" ? 3 : 1) + " " + esc(fs.unit) + "</dd><dt>Allowable</dt><dd class=\"num\">" + fmt(fs.allowable, id === "cfd_cruise" ? 3 : 1) + " " + esc(fs.unit) +
      "</dd><dt>Where</dt><dd class=\"num\">(" + f.worst_location_mm.map(function (x) { return fmt(x, 1); }).join(", ") + ") mm <span class=\"muted\">red dot</span></dd><dt>Critical region</dt><dd>" + esc(f.critical.text) +
      "</dd><dt>Load case</dt><dd>" + esc(f.load_case) + "</dd><dt>Evidence</dt><dd>" + esc(f.evidence) + "</dd></dl>" +
      "<details><summary>Solver and mesh</summary><dl class=\"kv\"><dt>Solver</dt><dd>" + esc(sol.name) + "</dd><dt>Run time</dt><dd>" + fmt(sol.seconds, 0) + " s</dd><dt>Mesh</dt><dd>" +
      esc(Object.keys(mesh).map(function (k) { return k + " " + mesh[k]; }).join(", ")) + "</dd>" +
      (sol.iterations ? "<dt>Iterations</dt><dd>" + sol.iterations + "</dd>" : "") + (sol.checkMesh ? "<dt>Mesh check</dt><dd>" + esc(sol.checkMesh) + "</dd>" : "") +
      (ex.cl != null ? "<dt>C_L / C_D</dt><dd>" + fmt(ex.cl, 3) + " / " + fmt(ex.cd, 4) + " at α " + fmt(ex.alpha_deg, 1) + "°</dd>" : "") +
      "<dt>Run</dt><dd>" + esc(f.record_time) + "</dd></dl></details>" +
      "<details><summary>Assumptions</summary><ul>" + (f.assumptions || []).map(function (a) { return "<li>" + esc(a) + "</li>"; }).join("") + "</ul></details>";
  }

  /* ------------------------------------------------------------------ audit */
  function audit() {
    var rs = {PASS: "pass", FAIL: "fail", "CONDITIONAL / REVIEW REQUIRED": "cond", "NOT EVALUATED": "nev"};
    $("#req-table").innerHTML = "<thead><tr><th>ID</th><th>Requirement</th><th>Limit</th><th>Predicted</th><th>Status</th><th>Basis</th><th>Verified by</th></tr></thead><tbody>" +
      S.requirements.map(function (r) {
        return "<tr><td class=\"mono\">" + r.id + "</td><td>" + esc(r.text) + "</td><td class=\"num\">" + esc(r.op) + " " + esc(r.threshold) + " " + esc(r.unit || "") + "</td><td class=\"num\">" +
          (r.predicted == null ? "—" : (typeof r.predicted === "number" ? fmt(r.predicted, Math.abs(r.predicted) < 10 ? 3 : 1) : esc(r.predicted))) + "</td><td>" + chip(rs[r.status] || "nev") + "</td><td class=\"small\">" + esc(r.why) + "</td><td>" + esc(r.verification) + "</td></tr>";
      }).join("") + "</tbody>";
    $("#gates").innerHTML = S.gates.map(function (g) {
      return '<details class="gate"><summary>' + chip(g.status) + " <span class=\"mono\">" + g.id + "</span> " + esc(g.name) + "</summary><ul>" +
        g.criteria.map(function (c) { return "<li>" + chip(c.status) + " " + esc(c.text) + (c.why ? ' <span class="muted">— ' + esc(c.why) + "</span>" : "") + "</li>"; }).join("") + "</ul></details>";
    }).join("");
    $("#runs").innerHTML = "<thead><tr><th>Analysis</th><th>Status</th><th>Result</th><th>Run</th></tr></thead><tbody>" + S.analyses.map(function (a) {
      return "<tr><td>" + esc(a.label) + "</td><td>" + chip(a.status) + "</td><td class=\"small\">" + esc(a.result || sysResult(a)) + "</td><td class=\"num small\">" + esc(a.time || "") + "</td></tr>";
    }).join("") + "</tbody>";
    $("#estimates").innerHTML = "<thead><tr><th>Input</th><th>Value</th><th>Basis</th></tr></thead><tbody>" + S.estimates.map(function (x) {
      return "<tr><td class=\"mono\">" + esc(x.name) + "</td><td class=\"num\">" + (x.value == null ? "missing" : esc(x.value) + " " + esc(x.unit)) + "</td><td class=\"small\">" + esc(x.ref) + "</td></tr>";
    }).join("") + "</tbody>";
    $("#unresolved").innerHTML = S.unresolved.map(function (u) { return "<li><b>" + esc(u.item) + "</b> — " + esc(u.why) + "</li>"; }).join("");
    $("#robust").innerHTML = S.robustness.length ? "<thead><tr><th>Case</th><th>Height lost</th><th>Within 5 m</th><th>Time above motor rating</th></tr></thead><tbody>" + S.robustness.map(function (r) {
      var ok = !r.ground_contact && r.altitude_loss <= 5;
      return "<tr><td>" + esc(r.case) + "</td><td class=\"num\">" + (r.ground_contact ? "ground contact" : fmt(r.altitude_loss, 2) + " m") + "</td><td>" + chip(ok ? "pass" : "fail", ok ? "yes" : "no") + "</td><td class=\"num\">" + fmt(r.time_above_motor_rating_s, 2) + " s</td></tr>";
    }).join("") + "</tbody>" : "<tbody><tr><td class=\"muted\">No current robustness run for this variant.</td></tr></tbody>";
    var groups = {};
    S.metrics.forEach(function (m) { (groups[m.group] = groups[m.group] || []).push(m); });
    $("#metrics").innerHTML = "<thead><tr><th>Quantity</th><th>Value</th><th>Evidence</th><th>Basis</th></tr></thead><tbody>" + Object.keys(groups).map(function (g) {
      return "<tr><th colspan=\"4\">" + esc(g) + "</th></tr>" + groups[g].map(function (m) {
        return "<tr><td>" + esc(m.label) + "</td><td class=\"num\">" + fmt(m.value, Math.abs(m.value) < 10 ? 2 : 1) + " " + esc(m.unit) + "</td><td class=\"small\">" + esc((m.source || "").replace("provisional_estimate", "estimate").replace("_", " ")) + "</td><td class=\"small\">" + esc((m.ref || "").slice(0, 160)) + "</td></tr>";
      }).join("");
    }).join("") + "</tbody>";
    $("#tests").innerHTML = "<thead><tr><th>Test</th><th>What</th><th>Requirements</th><th>Pass line</th></tr></thead><tbody>" + S.tests.map(function (t) {
      return "<tr><td class=\"mono\">" + esc(t.id) + "</td><td>" + esc(t.title) + "</td><td class=\"mono small\">" + esc((t.reqs || []).join(", ")) + "</td><td class=\"small\">" + esc(t.acceptance) + "</td></tr>";
    }).join("") + "</tbody>";
  }

  /* ------------------------------------------------------------------ routing */
  var built = {};
  function route() {
    var v = (location.hash || "#overview").slice(1);
    if (["overview", "results", "audit"].indexOf(v) < 0) v = "overview";
    ["overview", "results", "audit"].forEach(function (n) { $("#view-" + n).hidden = n !== v; });
    document.querySelectorAll(".tabs a").forEach(function (a) { if (a.dataset.view === v) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
    if (!built[v]) { built[v] = true; if (v === "overview") overview(); else if (v === "results") results(); else audit(); }
    if (v === "results" && RV) RV.resize();
  }
  window.addEventListener("hashchange", route);
  route();
})();
