"""Results Hub front end: a stub HTTP server returns contract-shaped JSON and Playwright drives the real page.

pytest runs this file in the project venv, which has no Playwright. The browser part therefore runs as a child
process (`python test_hub_frontend.py --drive <shots dir>`) on whichever interpreter can import Playwright; it prints
one JSON line per check and the pytest functions below read those results.
"""
import base64
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve()
WEB = HERE.parents[1] / "web"
SHOTS = Path("/tmp/claude-0/-home-claude/cca99fdc-b1aa-5a4c-a668-10c4bdf36bbb/scratchpad/hub_shots")

CHECKS = [
    "sidebar_lists_projects", "sidebar_search_filters", "overview_cards", "overview_badges", "footer_line",
    "tabs_all_render", "url_params_select_state", "field_picker_lists_cfd_fields", "field_picker_grouped_by_discipline",
    "webgl_canvas_drawn", "webgl_reported_true", "click_reads_value", "deformation_control_for_structural",
    "slices_and_streams_toggle", "plots_draw", "plots_hover_readout", "plots_log_axis", "plots_csv_download",
    "audit_sections", "audit_log_expands", "failed_overview_shows_message", "failed_audit_shows_problem",
    "failed_3d_shows_note_not_blank", "history_compare", "history_open_run", "motion_mounts", "motion_missing_note",
    "studio_project_overview", "fallback_png_shown", "fallback_no_blank_canvas", "fallback_reported_false",
    "fallback_view_buttons", "fallback_note", "no_console_errors",
]


# ------------------------------------------------------------------------------------------ fixtures (stdlib only)
def b64f(vals):
    vals = list(vals)
    return base64.b64encode(struct.pack("<%df" % len(vals), *vals)).decode()


def b64i(vals):
    vals = list(vals)
    return base64.b64encode(struct.pack("<%di" % len(vals), *vals)).decode()


def grid(nx, ny, fn):
    """(u, v in 0..1) -> xyz grid; returns points list, triangle list."""
    pts, tris = [], []
    for j in range(ny + 1):
        for i in range(nx + 1):
            pts.append(fn(i / nx, j / ny))
    for j in range(ny):
        for i in range(nx):
            a = j * (nx + 1) + i
            tris += [a, a + 1, a + nx + 2, a, a + nx + 2, a + nx + 1]
    return pts, tris


def merge(meshes):
    pts, tris = [], []
    for p, t in meshes:
        tris += [i + len(pts) for i in t]
        pts += p
    return pts, tris


def box_mesh(sx, sy, sz, c, div=10):
    cx, cy, cz = c
    faces = [
        lambda u, v: (cx - sx / 2 + sx * u, cy - sy / 2 + sy * v, cz - sz / 2),
        lambda u, v: (cx - sx / 2 + sx * u, cy - sy / 2 + sy * v, cz + sz / 2),
        lambda u, v: (cx - sx / 2 + sx * u, cy - sy / 2, cz - sz / 2 + sz * v),
        lambda u, v: (cx - sx / 2 + sx * u, cy + sy / 2, cz - sz / 2 + sz * v),
        lambda u, v: (cx - sx / 2, cy - sy / 2 + sy * u, cz - sz / 2 + sz * v),
        lambda u, v: (cx + sx / 2, cy - sy / 2 + sy * u, cz - sz / 2 + sz * v),
    ]
    return merge([grid(div, div, f) for f in faces])


def wing_mesh():
    return grid(24, 40, lambda u, v: (-35 + 70 * u, -250 + 500 * v, 14 + 6 * math.sin(u * math.pi) * (1 - abs(2 * v - 1))))


def part(name, role, mesh):
    return {"name": name, "role": role, "points": b64f([c for p in mesh[0] for c in p]), "triangles": b64i(mesh[1])}


def field(fid, label, disc, unit, qty, mesh_name, mesh, valfn, disp=None, hint=None):
    pts, tris = mesh
    vals = [valfn(p) for p in pts]
    f = {"id": fid, "label": label, "discipline": disc, "unit": unit, "quantity": qty, "mesh": mesh_name,
         "values": b64f(vals), "min": min(vals), "max": max(vals)}
    if mesh_name == "own":
        f["points"] = b64f([c for p in pts for c in p]); f["triangles"] = b64i(tris)
    if disp:
        f["displacement"] = b64f([c for p in pts for c in disp(p)]); f["scale_hint"] = hint
    return f


def drone_scene():
    fus, wing = box_mesh(200, 40, 30, (0, 0, 0)), wing_mesh()
    own = box_mesh(200, 40, 30, (0, 0, 0), div=14)
    slice_a = grid(20, 14, lambda u, v: (-150 + 300 * u, 0.0, -60 + 140 * v))
    slice_b = grid(16, 16, lambda u, v: (0.0, -250 + 500 * u, -60 + 140 * v))
    lines, offs, vals = [], [0], []
    for k in range(6):
        for s in range(30):
            x = -180 + 12 * s
            lines.append((x, -120 + 48 * k, 40 + 12 * math.sin(s / 5.0 + k)))
            vals.append(12 + 0.05 * s * 10 + k)
        offs.append(len(lines))
    return {
        "units": "mm",
        "parts": [part("fuselage", "body", fus), part("wing", "lifting surface", wing)],
        "fields": [
            field("cfd.cp", "Pressure coefficient Cp", "cfd", "-", "cp", "wing", wing, lambda p: -1.5 * math.cos(p[0] / 35 * 1.4) * (1 - abs(p[1]) / 300)),
            field("cfd.p", "Static pressure p", "cfd", "Pa", "p", "own", own, lambda p: 101325 + 40 * p[0] / 100 + 12 * p[2]),
            field("cfd.wss", "Wall shear stress", "cfd", "Pa", "wss", "wing", wing, lambda p: 0.8 + 0.5 * abs(p[1]) / 250),
            field("fea.vm", "Von Mises stress", "structural", "MPa", "von_mises", "wing", wing, lambda p: 4 + 0.09 * abs(p[1]),
                  disp=lambda p: (0.0, 0.0, 0.02 * (p[1] / 250) ** 2 * 100), hint=3.0),
        ],
        "slices": [
            {"name": "Mid-span (y = 0)", "points": b64f([c for p in slice_a[0] for c in p]), "triangles": b64i(slice_a[1]),
             "values": b64f([10 + 6 * math.sin(p[0] / 60) + p[2] / 20 for p in slice_a[0]]), "label": "Speed", "unit": "m/s"},
            {"name": "Wing station (x = 0)", "points": b64f([c for p in slice_b[0] for c in p]), "triangles": b64i(slice_b[1]),
             "values": b64f([9 + p[2] / 15 + abs(p[1]) / 100 for p in slice_b[0]]), "label": "Speed", "unit": "m/s"},
        ],
        "streamlines": {"points": b64f([c for p in lines for c in p]), "offsets": b64i(offs), "values": b64f(vals), "unit": "m/s"},
        "notes": ["Fields kept for the latest run only."],
    }


def card(cid, title, disc, status, basis, hv, hu, hl, text, metrics=()):
    return {"id": cid, "title": title, "discipline": disc, "status": status, "basis": basis,
            "headline": {"value": hv, "unit": hu, "label": hl} if hv is not None else None, "text": text, "metrics": list(metrics)}


def residual_plot():
    it = list(range(1, 301, 3))
    return {"id": "cfd.residuals", "title": "CFD residuals", "discipline": "cfd", "x": {"label": "Iteration", "unit": ""},
            "y": {"label": "Initial residual", "unit": "", "log": True},
            "series": [{"name": n, "x": it, "y": [math.exp(-k * i / 40.0) * s + 1e-6 for i in it]} for n, k, s in [("Ux", 1.0, 1), ("p", 0.7, 0.6), ("k", 0.5, 0.3)]],
            "note": "Parsed from the solver log."}


def hist_plot(pid):
    it = list(range(0, 300, 5))
    return {"id": "cfd.forces", "title": "Lift and drag coefficients", "discipline": "cfd", "x": {"label": "Iteration", "unit": ""},
            "y": {"label": "Coefficient", "unit": "-", "log": False},
            "series": [{"name": "C_L", "x": it, "y": [0.62 - 0.4 * math.exp(-i / 50.0) for i in it]},
                       {"name": "C_D", "x": it, "y": [0.05 + 0.03 * math.exp(-i / 60.0) for i in it]}], "note": ""}


def bar_plot():
    return {"id": "cfd.final", "title": "Final residuals", "discipline": "cfd", "x": {"label": "", "unit": ""},
            "y": {"label": "Residual", "unit": "", "log": True},
            "series": [{"name": "Ux", "x": [300], "y": [2.1e-5]}, {"name": "p", "x": [300], "y": [8.4e-4]}],
            "note": "The solver log was not kept; only the final residuals are stored."}


def drone_audit():
    return [
        {"analysis": "cfd", "title": "Cruise CFD", "status": "ok",
         "solver": {"name": "OpenFOAM", "version": "v2412", "cells": 412000, "iterations": 300},
         "mesh": {"max_non_orthogonality": 61.2, "max_skewness": 2.3, "limit_non_orthogonality": 70},
         "inputs": [{"name": "Cruise speed", "value": 12.0, "unit": "m/s", "status": "user", "source": "drone.json"},
                    {"name": "Air density", "value": 1.225, "unit": "kg/m3", "status": "datasheet", "source": "ISA sea level"},
                    {"name": "Turbulence intensity", "value": 0.05, "unit": "", "status": "estimate", "source": "default"}],
         "checks": [{"name": "Residuals below limit", "value": 2.1e-5, "limit": 1e-4, "unit": "", "ok": True},
                    {"name": "Force convergence (last 50 it.)", "value": 3.2, "limit": 1.0, "unit": "%", "ok": False}],
         "convergence": {"iterations": 300, "converged": False, "force_drift_pct": 3.2},
         "assumptions": ["Steady RANS, k-omega SST.", "No propeller wash included."], "problems": [],
         "log_tail": "Time = 300\nSolving for Ux, Initial residual = 2.1e-05\nEnd\n", "files": [{"name": "forces.dat", "bytes": 48211}]},
        {"analysis": "structural", "title": "Wing bending", "status": "watch", "solver": {"name": "CalculiX"}, "mesh": {}, "inputs": [], "checks": [],
         "convergence": {}, "assumptions": [], "problems": [], "log_tail": "", "files": []},
    ]


FAIL_MSG = "foamToVTK segfaulted after the solve finished (exit code -11)"


def projects():
    return [
        {"id": "drone1", "kind": "drone", "name": "quad250", "path": "/p/quad250", "updated": "2026-11-25T14:02:00", "verdict": "conditional",
         "runs": 3, "disciplines": ["structural", "cfd", "flight", "mass"]},
        {"id": "fail1", "kind": "drone", "name": "k09l-tiltwing", "path": "/p/k09l", "updated": "2026-11-24T09:30:00", "verdict": "fail",
         "runs": 1, "disciplines": ["cfd"]},
        {"id": "mbd1", "kind": "studio", "name": "gripper", "path": "/p/gripper.ocae", "updated": "2026-11-20T18:45:00", "verdict": None,
         "runs": 1, "disciplines": ["mbd"]},
    ]


def project_payload(pid, run):
    base = {"id": pid, "run": run if run != "latest" else {"drone1": "r3", "fail1": "r1", "mbd1": "study1@1"}[pid]}
    if pid == "drone1":
        base.update({"name": "quad250", "kind": "drone", "notice": "",
                     "runs": [{"run": "r3", "started": "2026-11-25T14:02:00", "verdict": "conditional", "only": None, "duration_s": 1260.0},
                              {"run": "r2", "started": "2026-11-24T10:00:00", "verdict": "fail", "only": "cfd", "duration_s": 900.5},
                              {"run": "r1", "started": "2026-11-20T08:15:00", "verdict": "not ready", "only": None, "duration_s": 700}],
                     "cards": [
                         card("mass", "Mass and balance", "mass", "ok", "datasheet", 412.5, "g", "Take-off mass", "Within the 450 g class limit.",
                              [{"label": "CG position", "value": 51.2, "unit": "mm", "status": "ok", "basis": "estimate"}]),
                         card("cfd", "Cruise aerodynamics", "cfd", "watch", "estimate", 0.58, "-", "Lift coefficient C_L", "Lift is enough for cruise but the force history is still drifting.",
                              [{"label": "Drag coefficient", "value": 0.052, "unit": "-", "status": "watch", "basis": "estimate"},
                               {"label": "L/D", "value": 11.2, "unit": "-", "status": "ok", "basis": "estimate"}]),
                         card("structural", "Wing bending", "structural", "conditional", "estimate", 38.4, "MPa", "Peak von Mises stress", "Margin is 1.7 on an estimated material strength.", []),
                         card("flight", "Hover and flight", "flight", "not_evaluated", None, None, None, None, "Not evaluated: motor thrust table is missing.", []),
                     ],
                     "analyses": [{"id": "cfd", "title": "Cruise CFD", "discipline": "cfd", "status": "watch", "summary": "Converged to 3.2 % force drift."},
                                  {"id": "structural", "title": "Wing bending", "discipline": "structural", "status": "conditional", "summary": "Peak 38.4 MPa."}]})
    elif pid == "fail1":
        base.update({"name": "k09l-tiltwing", "kind": "drone", "notice": "",
                     "runs": [{"run": "r1", "started": "2026-11-24T09:30:00", "verdict": "fail", "only": "cfd", "duration_s": 300}],
                     "cards": [card("cfd", "Cruise aerodynamics", "cfd", "error", "estimate", None, None, None, "CFD failed: " + FAIL_MSG, [])],
                     "analyses": [{"id": "cfd", "title": "Cruise CFD", "discipline": "cfd", "status": "error", "summary": FAIL_MSG}]})
    else:
        base.update({"name": "gripper", "kind": "studio", "notice": "",
                     "runs": [{"run": "study1@1", "started": "2026-11-20T18:45:00", "verdict": None, "only": "mbd", "duration_s": 12}],
                     "cards": [card("mbd", "Gripper closing", "mbd", "ok", "user", 14.2, "N", "Peak grip force", "Both fingers close in 0.4 s.",
                                    [{"label": "Closing time", "value": 0.4, "unit": "s", "status": "ok", "basis": "user"}])],
                     "analyses": [{"id": "mbd", "title": "Multibody motion", "discipline": "mbd", "status": "ok", "summary": "Prescribed closing motion."}]})
    return base


PNG_CACHE = {}


def png_bytes():
    if "p" not in PNG_CACHE:
        w, h = 480, 300
        raw = b"".join(b"\x00" + bytes(v for x in range(w) for v in (30 + x * 200 // w, 80 + y * 120 // h, 200 - x * 150 // w)) for y in range(h))
        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
        PNG_CACHE["p"] = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    return PNG_CACHE["p"]


FAKE_MOTION = ("window.HubMotion={mount:function(el,data,o){var d=document.createElement('div');d.id='fake-motion';"
               "d.textContent='motion '+data.kind+' webgl='+o.webgl+' bodies='+data.bodies.length;el.appendChild(d);}};")


class Stub(BaseHTTPRequestHandler):
    log = []
    clients = []
    motion_js = True
    scene_cache = None

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        Stub.clients.append(json.loads(self.rfile.read(n) or b"{}")); self._json({"ok": True})

    def do_GET(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}; p = u.path
        Stub.log.append(self.path)
        if p in ("/hub", "/hub/"):
            return self._send(200, (WEB / "hub" / "index.html").read_bytes(), "text/html")
        if p == "/hub/motion.js":
            if not Stub.motion_js:
                return self._send(404, b"not found", "text/plain")
            return self._send(200, FAKE_MOTION.encode(), "application/javascript")
        if p.startswith("/hub/") or p.startswith("/vendor/"):
            f = (WEB / p.lstrip("/").replace("hub/", "hub/", 1)).resolve()
            if WEB in f.parents and f.is_file():
                ct = {".js": "application/javascript", ".css": "text/css", ".html": "text/html"}.get(f.suffix, "application/octet-stream")
                return self._send(200, f.read_bytes(), ct)
            return self._send(404, b"missing", "text/plain")
        pid, run = q.get("pid"), q.get("run", "latest")
        if p == "/api/hub/projects":
            return self._json(projects())
        if p == "/api/hub/project":
            return self._json(project_payload(pid, run))
        if p == "/api/hub/scene":
            if pid == "drone1":
                if Stub.scene_cache is None:
                    Stub.scene_cache = drone_scene()
                return self._json(Stub.scene_cache)
            if pid == "fail1":
                return self._json({"units": "mm", "parts": [], "fields": [], "slices": [], "streamlines": None, "notes": ["CFD failed: " + FAIL_MSG, "No fields were stored for this run."]})
            return self._json({"units": "mm", "parts": [part("finger", "link", box_mesh(60, 10, 10, (0, 0, 0)))], "fields": [], "slices": [], "streamlines": None, "notes": ["This study stores no fields."]})
        if p == "/api/hub/plots":
            return self._json([residual_plot(), hist_plot(pid), bar_plot()] if pid == "drone1" else [])
        if p == "/api/hub/audit":
            if pid == "drone1":
                return self._json(drone_audit())
            if pid == "fail1":
                return self._json([{"analysis": "cfd", "title": "Cruise CFD", "status": "error", "solver": {"name": "OpenFOAM", "version": "v2412"}, "mesh": {}, "inputs": [], "checks": [],
                                    "convergence": {}, "assumptions": [], "problems": [FAIL_MSG], "log_tail": "Segmentation fault", "files": []}])
            return self._json([])
        if p == "/api/hub/motion":
            if pid == "mbd1":
                return self._json({"kind": "mbd", "title": "Gripper closing", "basis": "user", "notes": [], "t": [0, 1], "bodies": [{"id": "f1", "name": "finger", "points": "", "triangles": "", "color": "#888"}],
                                   "poses": {}, "joints": [], "channels": [], "rotors": None})
            return self._json({"kind": None, "title": "Motion", "basis": "", "notes": ["No time history was stored for this run."], "t": [], "bodies": [], "poses": {}, "joints": [], "channels": [], "rotors": None})
        if p == "/api/hub/compare":
            return self._json({"metrics": [{"analysis": "cfd", "label": "Lift coefficient", "unit": "-", "a": q.get("a") and 0.51, "b": 0.58, "delta": 0.07, "delta_pct": 13.7},
                                           {"analysis": "mass", "label": "Take-off mass", "unit": "g", "a": 420.0, "b": 412.5, "delta": -7.5, "delta_pct": -1.8}]})
        if p == "/api/hub/image.png":
            return self._send(200, png_bytes(), "image/png")
        self._send(404, b"{}", "application/json")


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ------------------------------------------------------------------------------------------ browser driver
NO_WEBGL = """
(() => { const o = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (t, ...a) { if (/webgl/i.test(t)) return null; return o.call(this, t, ...a); }; })();
"""


def drive(shots):
    from playwright.sync_api import sync_playwright
    shots.mkdir(parents=True, exist_ok=True)
    for old in shots.glob("*.png"):
        old.unlink()
    srv = serve(); base = "http://127.0.0.1:%d" % srv.server_address[1]
    out = {}

    def chk(name, cond, detail=""):
        assert name in CHECKS, name
        out[name] = {"ok": bool(cond), "detail": str(detail)[:400]}

    errors = []
    with sync_playwright() as pw:
        br = pw.chromium.launch(args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])

        def new(width=1440, height=900, nowebgl=False):
            ctx = br.new_context(viewport={"width": width, "height": height}, device_scale_factor=1, accept_downloads=True)
            if nowebgl:
                ctx.add_init_script(NO_WEBGL)
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append("pageerror: %s" % e))
            pg.on("console", lambda m: errors.append("console: " + m.text) if m.type == "error" and "404" not in m.text else None)
            return ctx, pg

        def shot(pg, name):
            pg.screenshot(path=str(shots / (name + ".png")))

        # ---- scenario A: WebGL on, drone project
        Stub.clients.clear()
        ctx, pg = new()
        pg.goto(base + "/hub/?pid=drone1")
        pg.wait_for_selector("#cards .card")
        n = pg.locator("#plist .prow").count()
        chk("sidebar_lists_projects", n == 3 and "quad250" in pg.inner_text("#plist") and "k09l-tiltwing" in pg.inner_text("#plist") and "gripper" in pg.inner_text("#plist"), n)
        pg.fill("#search", "grip")
        chk("sidebar_search_filters", pg.locator("#plist .prow").count() == 1)
        pg.fill("#search", "")
        chk("overview_cards", pg.locator("#cards .card").count() == 4 and "Take-off mass" in pg.inner_text("#cards") and "412.5" in pg.inner_text("#cards"))
        chk("overview_badges", pg.locator("#cards .basis.estimate").count() >= 1 and pg.locator("#cards .basis.datasheet").count() >= 1 and pg.locator("#cards .chip").count() >= 4)
        chk("footer_line", "Simulation results plan the tests; they are not flight approval." in pg.inner_text(".foot"))
        shot(pg, "01_overview_1440")
        pg.set_viewport_size({"width": 1280, "height": 800}); shot(pg, "01_overview_1280")
        pg.set_viewport_size({"width": 1000, "height": 700}); shot(pg, "01_overview_1000")
        pg.set_viewport_size({"width": 1440, "height": 900})

        pg.click('#tabs [data-tab="3d"]')
        pg.wait_for_selector("#fieldpicker .fbtn")
        pg.wait_for_function("window.HubApp.state.viewer && window.HubApp.state.viewer.stats().field")
        ids = pg.eval_on_selector_all("#fieldpicker .fbtn", "els => els.map(e => e.dataset.field)")
        chk("field_picker_lists_cfd_fields", {"cfd.cp", "cfd.p", "cfd.wss"} <= set(ids), ids)
        heads = pg.eval_on_selector_all("#fieldpicker h4", "els => els.map(e => e.textContent)")
        chk("field_picker_grouped_by_discipline", heads == ["cfd", "structural"], heads)
        pg.wait_for_timeout(400)
        px = pg.evaluate("""() => { const c = document.querySelector('.viewer canvas'); if (!c) return null;
          const w = document.createElement('canvas'); w.width = c.width; w.height = c.height; return {w: c.width, h: c.height}; }""")
        shot(pg, "02_3d_cp_1440")
        # a coloured contour means the stage is not a single flat colour: sample the screenshot via a page-side readback
        colours = pg.evaluate("""() => new Promise(res => { const url = window.HubApp.state.viewer.snapshot(), im = new Image();
          im.onload = () => { const t = document.createElement('canvas'); t.width = 64; t.height = 40; const x = t.getContext('2d');
          x.drawImage(im, 0, 0, 64, 40); const d = x.getImageData(0, 0, 64, 40).data, s = new Set();
          for (let i = 0; i < d.length; i += 4) s.add((d[i] >> 4) + ',' + (d[i + 1] >> 4) + ',' + (d[i + 2] >> 4)); res(s.size); }; im.src = url; })""")
        chk("webgl_canvas_drawn", px and px["w"] > 200 and colours > 3, "%s colours=%s" % (px, colours))
        chk("webgl_reported_true", any(c.get("webgl") is True for c in Stub.clients), Stub.clients)
        legend = pg.inner_text(".legend")
        chk("tabs_all_render", "Pressure coefficient" in legend)
        # click to read: scan a few points over the model
        got = ""
        box = pg.locator(".viewer canvas").bounding_box()
        for fx in (0.5, 0.45, 0.55, 0.4, 0.6, 0.35, 0.65):
            for fy in (0.5, 0.45, 0.55, 0.4, 0.6, 0.35, 0.65):
                pg.mouse.click(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy)
                t = pg.inner_text(".readout")
                if " at (" in t and "Cp" in t:
                    got = t; break
            if got:
                break
        chk("click_reads_value", got, got or pg.inner_text(".readout"))
        pg.click('#fieldpicker [data-field="fea.vm"]')
        pg.wait_for_timeout(200)
        chk("deformation_control_for_structural", pg.is_visible("#deform") and "MPa" in pg.inner_text(".legend"))
        pg.fill("#deform", "9"); pg.dispatch_event("#deform", "input")
        pg.check("#slices-all"); pg.check("#streams"); pg.wait_for_timeout(300)
        st = pg.evaluate("window.HubApp.state.viewer.stats()")
        chk("slices_and_streams_toggle", st["slices"] == 2 and st["stream"] and "Slice" in pg.inner_text(".legend") and "Streamlines" in pg.inner_text(".legend"), st)
        pg.click('#fieldpicker [data-field="cfd.p"]'); pg.click('.seg [data-view="top"]'); pg.wait_for_timeout(300)
        shot(pg, "03_3d_pressure_slices_1440")
        pg.set_viewport_size({"width": 1920, "height": 1080}); pg.wait_for_timeout(400); shot(pg, "03_3d_1920")
        pg.set_viewport_size({"width": 1000, "height": 700}); pg.wait_for_timeout(400); shot(pg, "03_3d_1000")
        pg.set_viewport_size({"width": 1440, "height": 900})

        pg.click('#tabs [data-tab="plots"]')
        pg.wait_for_selector(".pcard canvas")
        pg.wait_for_timeout(300)
        chk("plots_draw", pg.locator(".pcard canvas").count() == 3)
        cv = pg.locator('.pcard[data-plot="cfd.residuals"] canvas'); b = cv.bounding_box()
        pg.mouse.move(b["x"] + b["width"] * 0.5, b["y"] + b["height"] * 0.5)
        pg.wait_for_selector(".ptip:not([hidden])")
        tip = pg.inner_text('.pcard[data-plot="cfd.residuals"] .ptip')
        chk("plots_hover_readout", "Ux" in tip and "Iteration" in tip, tip)
        lc = pg.locator('.pcard[data-plot="cfd.residuals"] input[type=checkbox]')
        chk("plots_log_axis", lc.is_checked() and not pg.locator('.pcard[data-plot="cfd.forces"] input[type=checkbox]').is_checked(), "log on for residuals, off for forces")
        shot(pg, "04_plots_1440")
        with pg.expect_download() as dl:
            pg.click('.pcard[data-plot="cfd.residuals"] button:has-text("Download CSV")')
        path = dl.value.path(); txt = Path(path).read_text()
        chk("plots_csv_download", txt.startswith("series,") and "Ux" in txt and len(txt.splitlines()) > 100, txt[:80])

        pg.click('#tabs [data-tab="audit"]')
        pg.wait_for_selector(".acard")
        t = pg.inner_text("#content")
        chk("audit_sections", pg.locator(".acard").count() == 2 and "OpenFOAM" in t and "Air density" in t and "datasheet" in t and "outside limit" in t, t[:200])
        pg.click(".acard details.log summary"); chk("audit_log_expands", pg.is_visible("pre.logtxt") and "Initial residual" in pg.inner_text("pre.logtxt"))
        shot(pg, "05_audit_1440")

        pg.click('#tabs [data-tab="history"]')
        pg.wait_for_selector("#runs")
        pg.check('input[data-run="r3"]'); pg.check('input[data-run="r2"]'); pg.click("#docompare")
        pg.wait_for_selector("#compare-out table")
        ct = pg.inner_text("#compare-out")
        chk("history_compare", "Lift coefficient" in ct and "13.7" in ct and "-7.5" in ct, ct[:200])
        shot(pg, "06_history_1440")
        pg.click('button[data-open="r2"]'); pg.wait_for_function("document.querySelector('#runsel').value === 'r2'")
        chk("history_open_run", "run=r2" in pg.url and any("run=r2" in l for l in Stub.log), pg.url)
        pg.goto(base + "/hub/?pid=drone1&tab=plots&run=r3"); pg.wait_for_selector(".pcard canvas")
        chk("url_params_select_state", pg.get_attribute('#tabs [data-tab="plots"]', "aria-selected") == "true" and pg.input_value("#runsel") == "r3")
        ctx.close()

        # ---- scenario B: failed CFD project
        ctx, pg = new()
        pg.goto(base + "/hub/?pid=fail1"); pg.wait_for_selector(".banner.err")
        chk("failed_overview_shows_message", FAIL_MSG in pg.inner_text(".banner.err") and pg.locator(".card.error").count() == 1)
        shot(pg, "07_failed_overview_1440")
        pg.click('#tabs [data-tab="3d"]'); pg.wait_for_selector(".vmsg")
        chk("failed_3d_shows_note_not_blank", "CFD failed" in pg.inner_text(".vmsg") and pg.locator(".viewer canvas").count() == 0)
        pg.click('#tabs [data-tab="audit"]'); pg.wait_for_selector(".problems")
        chk("failed_audit_shows_problem", FAIL_MSG in pg.inner_text(".problems"))
        shot(pg, "08_failed_audit_1440")
        ctx.close()

        # ---- scenario C: studio MBD project, motion tab with and without motion.js
        Stub.motion_js = True
        ctx, pg = new()
        pg.goto(base + "/hub/?pid=mbd1&tab=overview"); pg.wait_for_selector("#cards .card")
        chk("studio_project_overview", "Peak grip force" in pg.inner_text("#cards") and "studio" in pg.inner_text("#pkind").lower())
        pg.click('#tabs [data-tab="motion"]'); pg.wait_for_selector("#fake-motion")
        chk("motion_mounts", "motion mbd" in pg.inner_text("#fake-motion"), pg.inner_text("#fake-motion"))
        shot(pg, "09_studio_motion_1440")
        ctx.close()
        Stub.motion_js = False
        ctx, pg = new()
        pg.goto(base + "/hub/?pid=mbd1&tab=motion"); pg.wait_for_selector("#content .empty")
        chk("motion_missing_note", "not installed" in pg.inner_text("#content"), pg.inner_text("#content")[:120])
        ctx.close()
        Stub.motion_js = True

        # ---- scenario D: WebGL unavailable -> server PNG fallback
        Stub.clients.clear(); Stub.log.clear()
        ctx, pg = new(nowebgl=True)
        pg.goto(base + "/hub/?pid=drone1&tab=3d")
        pg.wait_for_selector("#shot")
        pg.wait_for_function("document.querySelector('#shot').naturalWidth > 0")
        chk("fallback_png_shown", pg.evaluate("document.querySelector('#shot').naturalWidth") == 480 and any("/api/hub/image.png" in l and "field=cfd.cp" in l and "view=iso" in l for l in Stub.log), Stub.log[-3:])
        chk("fallback_no_blank_canvas", pg.locator(".viewer canvas").count() == 0 and pg.is_visible("#shot"))
        chk("fallback_reported_false", any(c.get("webgl") is False for c in Stub.clients), Stub.clients)
        chk("fallback_note", "WebGL" in pg.inner_text(".vnote") and "server" in pg.inner_text(".vnote"), pg.inner_text(".vnote"))
        shot(pg, "10_fallback_1440")
        pg.click('.seg [data-view="side"]'); pg.click('#fieldpicker [data-field="cfd.wss"]'); pg.wait_for_timeout(500)
        chk("fallback_view_buttons", any("view=side" in l and "field=cfd.wss" in l for l in Stub.log), Stub.log[-3:])
        pg.set_viewport_size({"width": 1000, "height": 700}); pg.wait_for_timeout(300); shot(pg, "10_fallback_1000")
        ctx.close()
        br.close()
    srv.shutdown()
    chk("no_console_errors", not errors, errors[:3])
    for n in CHECKS:
        out.setdefault(n, {"ok": False, "detail": "check did not run"})
    print("HUBRESULT " + json.dumps(out))


# ------------------------------------------------------------------------------------------ pytest side
def _python_with_playwright():
    for exe in [sys.executable, "/usr/local/bin/python3", "/usr/bin/python3"]:
        if exe and shutil.which(exe) or exe and os.path.exists(exe):
            r = subprocess.run([exe, "-c", "import playwright.sync_api"], capture_output=True)
            if r.returncode == 0:
                return exe
    return None


try:
    import pytest
except ImportError:  # the driver may run under an interpreter without pytest
    pytest = None

if pytest is not None:
    @pytest.fixture(scope="module")
    def results():
        exe = _python_with_playwright()
        if not exe:
            pytest.skip("no Python with Playwright available")
        r = subprocess.run([exe, str(HERE), "--drive", str(SHOTS)], capture_output=True, text=True, timeout=420)
        line = [l for l in r.stdout.splitlines() if l.startswith("HUBRESULT ")]
        if not line:
            pytest.fail("browser driver failed:\n%s\n%s" % (r.stdout[-1500:], r.stderr[-3000:]))
        return json.loads(line[-1][len("HUBRESULT "):])

    @pytest.mark.parametrize("name", CHECKS)
    def test_hub_frontend(results, name):
        r = results[name]
        assert r["ok"], r["detail"]

    def test_page_files_exist():
        for f in ("index.html", "hub.js", "hub.css", "plots.js", "viewer.js"):
            assert (WEB / "hub" / f).is_file()
        html = (WEB / "hub" / "index.html").read_text()
        assert "Simulation results plan the tests; they are not flight approval." in html
        assert "http://" not in html and "https://" not in html   # self-contained, no CDN


if __name__ == "__main__" and "--drive" in sys.argv:
    drive(Path(sys.argv[sys.argv.index("--drive") + 1]))
