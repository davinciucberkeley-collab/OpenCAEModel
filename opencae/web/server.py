"""Local dashboard server for drone projects (stdlib only).

``DashboardServer(project_dir)`` serves the page in ``opencae/web/dashboard`` and a small JSON API on
127.0.0.1. Runs happen on one background thread at a time (``JobManager``); the page polls
``/api/progress``. ``opencae.drone.pipeline`` and ``opencae.drone.spec`` are imported lazily inside the
handlers, so this module imports even when they are missing.

API: GET /api/project, /api/results?variant=, /api/geometry?variant=, /api/field?variant=&file=,
/api/progress, /api/variants, /api/history; POST /api/run, /api/params, /api/iterate, /api/stop.

Results Hub (every project, see ``opencae.hub``): GET /api/hub/projects, /api/hub/project, /api/hub/scene,
/api/hub/plots, /api/hub/audit, /api/hub/motion, /api/hub/compare, /api/hub/image.png; POST /api/hub/client;
the page itself at /hub/ (``opencae/web/hub``). ``DashboardServer(None)`` serves the hub only (``opencae hub``).
"""
from __future__ import annotations

import argparse
import inspect
import json
import mimetypes
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

HERE = Path(__file__).resolve().parent
DASH_DIR = HERE / "dashboard"
VENDOR_DIR = HERE / "vendor"
HUB_DIR = HERE / "hub"
K09L_STUDY = HERE.parent / "examples" / "k09l" / "k09l_study.json"
MAX_TRIANGLES = 200_000
_VARIANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_FIELD_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,80}\.npz$")
_FOOTER = "Simulation results plan the tests; they are not flight approval."

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------------------------------------ jobs
_ANALYSIS_IDS = ("mass", "propulsion", "structure", "cfd", "flight")
_ANALYSIS_TITLES = {"mass": "Mass and balance", "propulsion": "Propulsion", "structure": "Structure",
                    "cfd": "Aerodynamics (CFD)", "flight": "Hover flight"}


def _accepts(fn, name: str) -> bool:
    """True when ``fn`` takes a keyword argument ``name`` (or **kwargs)."""
    try:
        ps = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind == p.VAR_KEYWORD for p in ps)


class JobManager:
    """One background job at a time. ``progress`` is the dict the page polls: ``running, kind, step, frac, log,
    error, started, finished, result`` plus the run tracker fields ``run_id, events (last 500), analyses
    ({id: {title, state, status, started, duration_s}}), eta_s``."""

    def __init__(self, project_dir=None):
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self.project_dir = Path(project_dir) if project_dir else None
        self._typical: dict = {}
        self._plan: list = []
        self._events_seen = False
        self.progress = {"running": False, "kind": "", "step": "", "frac": 0.0, "log": [], "error": "",
                         "started": None, "finished": None, "result": None, "run_id": None, "events": [],
                         "analyses": {}, "eta_s": None}

    # snapshot for JSON
    def snapshot(self) -> dict:
        with self._lock:
            d = dict(self.progress)
            d["log"] = list(self.progress["log"])
            d["events"] = list(self.progress["events"])
            d["analyses"] = {k: dict(v) for k, v in self.progress["analyses"].items()}
            d["eta_s"] = self._eta_locked()
            return d

    @property
    def running(self) -> bool:
        return bool(self.progress["running"])

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _log(self, msg: str):
        with self._lock:
            self.progress["log"].append(msg)
            del self.progress["log"][:-200]

    def _on_progress(self, msg: str, frac: float = None):
        with self._lock:
            if self._events_seen and msg != "Stopping after the current step...":
                return  # the run events carry the step text and the overall bar
            if msg:
                self.progress["step"] = str(msg)
                self.progress["log"].append(str(msg))
                del self.progress["log"][:-200]
            if frac is not None:
                try:
                    self.progress["frac"] = max(0.0, min(1.0, float(frac)))
                except (TypeError, ValueError):
                    pass

    def _eta_locked(self) -> Optional[float]:
        """Seconds left from the median durations of earlier runs; None without history for a pending analysis."""
        p = self.progress
        if not p["running"] or not p["analyses"] or not self._plan:
            return None
        left = 0.0
        now = time.time()
        for aid in self._plan:
            a = p["analyses"].get(aid) or {}
            st = a.get("state")
            if st in ("done", "cached", "error", "skipped"):
                continue
            typ = self._typical.get(aid)
            if typ is None:
                return None
            if st == "running" and a.get("started"):
                left += max(typ - (now - a["started"]), 0.0)
            else:
                left += typ
        return round(left, 1)

    def on_event(self, ev: dict) -> None:
        """Receives the pipeline's run events (any thread)."""
        kind, aid = ev.get("kind"), ev.get("id")
        with self._lock:
            self._events_seen = True
            p = self.progress
            p["events"].append(dict(ev))
            del p["events"][:-500]
            p["run_id"] = ev.get("run_id") or p["run_id"]
            if ev.get("frac") is not None:
                p["frac"] = max(0.0, min(1.0, float(ev["frac"])))
            msg = str(ev.get("message") or "")
            if kind == "run_start":
                plan = [(x.get("id"), x.get("title", "")) for x in (ev.get("plan") or [])]
                self._plan = [a for a, _ in plan]
                titles = dict(_ANALYSIS_TITLES, **dict(plan))
                p["analyses"] = {a: {"title": titles.get(a, a), "state": "waiting" if a in self._plan else "skipped",
                                     "status": None, "started": None, "duration_s": None} for a in _ANALYSIS_IDS}
                for a in self._plan:
                    p["analyses"].setdefault(a, {"title": titles.get(a, a), "state": "waiting", "status": None,
                                                 "started": None, "duration_s": None})
                self._typical = self._load_typical()
            elif kind == "analysis_start" and aid in p["analyses"]:
                p["analyses"][aid].update(state="running", started=ev.get("t") or time.time(), duration_s=None)
            elif kind == "analysis_done" and aid in p["analyses"]:
                st = ev.get("status")
                state = ("skipped" if ev.get("skipped") else "error" if st == "error" else
                         "cached" if ev.get("cached") else "done")
                p["analyses"][aid].update(state=state, status=st, duration_s=ev.get("duration_s"))
            if kind in ("step", "analysis_start", "analysis_done") and msg:
                p["step"] = msg
            if kind in ("step", "log", "analysis_start", "analysis_done", "run_done") and msg:
                p["log"].append(msg)
                del p["log"][:-200]

    def _load_typical(self) -> dict:
        if self.project_dir is None:
            return {}
        try:
            from opencae.drone import pipeline
            return pipeline.typical_durations(self.project_dir)
        except Exception:
            return {}

    def start(self, kind: str, fn: Callable) -> None:
        """Run ``fn(progress_cb, cancelled)`` in a thread. Raises BadRequest(409) if one is running."""
        with self._lock:
            if self.progress["running"]:
                raise BadRequest("A run is already in progress.", 409)
            self._cancel.clear()
            self._events_seen = False
            self._plan = []
            self.progress.update({"running": True, "kind": kind, "step": "Starting", "frac": 0.0, "log": [],
                                  "error": "", "started": time.time(), "finished": None, "result": None,
                                  "run_id": None, "events": [], "analyses": {}, "eta_s": None})

        def body():
            err, res = "", None
            try:
                res = fn(self._on_progress, self.cancelled)
            except BaseException as e:  # a solver failure is reported, never hidden
                err = str(e) or e.__class__.__name__
                if self._cancel.is_set():
                    err = "Stopped."
            if not err and (self._cancel.is_set() or (isinstance(res, dict) and res.get("cancelled"))):
                err = "Stopped."  # a stopped run returns normally; it is not "Done"
            with self._lock:
                self.progress.update({"running": False, "error": err, "finished": time.time(),
                                      "state": "cancelled" if err == "Stopped." else "error" if err else "done",
                                      "frac": 1.0 if not err else self.progress["frac"],
                                      "step": "Done" if not err else "Stopped" if err == "Stopped." else "Failed"})
                if err:
                    self.progress["log"].append(err)
                self.progress["result"] = _small(res)
                for a in self.progress["analyses"].values():  # nothing stays "running" after the run ended
                    if a["state"] == "running":
                        a["state"] = "error" if err and err != "Stopped." else "skipped"
                    elif a["state"] == "waiting":
                        a["state"] = "skipped"

        self._thread = threading.Thread(target=body, name="opencae-job", daemon=True)
        self._thread.start()

    def stop(self) -> bool:
        if self.running:
            self._cancel.set()
            self._on_progress("Stopping after the current step...")
            return True
        return False

    def join(self, timeout: float = None):
        t = self._thread
        if t:
            t.join(timeout)


def _small(res):
    """A job's return value, reduced to what the page needs (verdict and variant name)."""
    if isinstance(res, dict):
        out = {k: res[k] for k in ("variant", "verdict") if k in res}
        if "results" in res and isinstance(res["results"], dict):
            out.setdefault("verdict", res["results"].get("verdict"))
        return out
    return None


# ------------------------------------------------------------------------------------------------ data
def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        v = float(o)
        return None if v != v or v in (float("inf"), float("-inf")) else v
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"not JSON serialisable: {type(o)}")


def _dumps(obj) -> bytes:
    return json.dumps(obj, default=_json_default, allow_nan=False, separators=(",", ":")).encode()


def _clean(o):
    """Replace NaN/inf by None so the JSON stays valid for the browser."""
    if isinstance(o, float):
        return None if o != o or o in (float("inf"), float("-inf")) else o
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def _sig(a: np.ndarray, digits: int = 4) -> list:
    """Round to ``digits`` significant digits and return a plain list (keeps integers integers)."""
    a = np.asarray(a)
    if a.dtype.kind in "iub":
        return a.astype(np.int64).tolist()
    a = a.astype(np.float64)
    out = np.zeros_like(a)
    nz = np.isfinite(a) & (a != 0)
    mag = np.floor(np.log10(np.abs(a[nz]))).astype(int)
    scale = 10.0 ** (digits - 1 - mag)
    out[nz] = np.round(a[nz] * scale) / scale
    out[~np.isfinite(a)] = np.nan
    lst = out.tolist()
    return _clean(lst)


class DashboardServer:
    def __init__(self, project_dir, host: str = "127.0.0.1", port: int = 0):
        self.project_dir = Path(project_dir).resolve() if project_dir else None  # None: the hub only
        self.host = "127.0.0.1" if host in ("", "0.0.0.0") else host  # local only
        self.port = port
        self.jobs = JobManager(self.project_dir)  # JobManager accepts None
        self.k09l_dir = Path.home() / ".opencae" / "k09l_website"
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.url = ""
        self._geo_cache: dict = {}

    # -- lifecycle
    def start(self) -> str:
        server = self

        class Handler(_Handler):
            app = server

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self.url = f"http://{self.host}:{self.port}/"
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="opencae-dashboard", daemon=True)
        self._thread.start()
        if self.project_dir is not None:  # the project open in the app shows up in the Results Hub
            try:
                from opencae.hub import registry
                if (self.project_dir / "drone.json").is_file():
                    registry.register(self.project_dir, "drone")
            except Exception:  # noqa: BLE001 - the hub list is a convenience
                pass
        return self.url

    def stop(self) -> None:
        if self.jobs.stop():
            self.jobs.join(20)  # let the cancelled run kill its solver processes before the app goes away
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None

    # -- project data
    def _drone_json(self) -> dict:
        from opencae.drone import spec as _spec
        return _spec.load(self.project_dir)

    def variant_dir(self, variant: str) -> Path:
        variant = variant or "baseline"
        if not _VARIANT_RE.match(variant):
            raise BadRequest("Bad variant name.")
        return self.project_dir / "results" / variant

    def variants(self) -> list:
        root = self.project_dir / "results"
        names = []
        if root.is_dir():
            names = sorted((d.name for d in root.iterdir() if (d / "results.json").exists()),
                           key=lambda n: (n != "baseline", n))
        return names

    def project(self) -> dict:
        spec = self._drone_json()
        from opencae.drone import pipeline
        return {"better": dict(getattr(pipeline, "BETTER", {})), "name": spec.get("name") or self.project_dir.name, "type": spec.get("type", ""),
                "engine": spec.get("engine", "generic"), "spec": spec, "dir": str(self.project_dir),
                "variants": self.variants(), "footer": _FOOTER, "k09l": spec.get("engine") == "k09l"}

    def results(self, variant: str) -> dict:
        f = self.variant_dir(variant) / "results.json"
        if not f.exists():
            raise BadRequest("Not run yet", 404)
        return _clean(json.loads(f.read_text(encoding="utf-8")))

    def _geometry_arrays(self, variant: str):
        """(names, roles, materials, points, triangles) from the variant's geometry.npz, or from the CAD file
        when no run has written one yet."""
        for d in (self.variant_dir(variant), self.project_dir / "results" / "baseline"):
            f = d / "geometry.npz"
            if f.exists():
                with np.load(f, allow_pickle=False) as z:
                    names = [str(s) for s in z["names"]]
                    roles = [str(s) for s in z["roles"]] if "roles" in z.files else ["other"] * len(names)
                    mats = [str(s) for s in z["materials"]] if "materials" in z.files else [""] * len(names)
                    return names, roles, mats, [z[f"p{i}"] for i in range(len(names))], \
                        [z[f"t{i}"] for i in range(len(names))]
        from opencae.drone import geometry as G
        try:
            ac = G.load(self.project_dir, self._drone_json())
        except Exception as e:
            raise BadRequest(f"No model yet: {e}", 404)
        return ([p.name for p in ac.parts], [p.role for p in ac.parts], [p.material or "" for p in ac.parts],
                [p.points for p in ac.parts], [p.triangles for p in ac.parts])

    def geometry(self, variant: str) -> dict:
        self.variant_dir(variant)
        files = [self.variant_dir(variant) / "geometry.npz", self.project_dir / "results" / "baseline" / "geometry.npz",
                 self.project_dir / "drone.json"]
        stamp = tuple(f.stat().st_mtime_ns if f.exists() else 0 for f in files)
        hit = self._geo_cache.get(variant)
        if hit and hit[0] == stamp:
            return hit[1]
        names, roles, mats, P, T = self._geometry_arrays(variant)
        total = sum(len(t) for t in T)
        stride = max(1, int(np.ceil(total / MAX_TRIANGLES)))
        parts = []
        for i, name in enumerate(names):
            t = np.asarray(T[i])
            if stride > 1:
                t = t[::stride]
            used, inv = np.unique(t, return_inverse=True)  # drop vertices no kept triangle uses
            pts = np.round(np.asarray(P[i], dtype=np.float64)[used], 1)
            parts.append({"name": name, "role": roles[i], "material": mats[i],
                          "points": pts.ravel().tolist(), "triangles": inv.reshape(-1, 3).ravel().tolist()})
        out = {"parts": parts, "decimated": stride > 1, "stride": stride, "triangles_total": int(total),
               "units": "mm"}
        self._geo_cache[variant] = (stamp, out)
        return out

    def _npz(self, d: Path, file: str) -> Path:
        if not _FIELD_RE.match(file or ""):
            raise BadRequest("Bad field file name.")
        f = d / file
        if not f.is_file():
            raise BadRequest("No such field file.", 404)
        return f

    def field(self, variant: str, file: str) -> dict:
        d = self.variant_dir(variant)
        f = self._npz(d, file)
        out = {}
        with np.load(f, allow_pickle=False) as z:
            arrays = {k: z[k] for k in z.files}
        mesh = arrays.get("mesh")
        if mesh is not None and "points" not in arrays:  # values only: the mesh lives in another field file
            try:
                with np.load(self._npz(d, str(np.asarray(mesh).reshape(-1)[0])), allow_pickle=False) as m:
                    for k in ("points", "triangles"):
                        if k in m.files:
                            arrays[k] = m[k]
            except BadRequest:
                pass
        for k, a in arrays.items():
            if k == "mesh":
                continue
            if k in ("points", "slice_points", "stream_points"):
                data = np.round(a.astype(np.float64), 1).ravel().tolist()
            elif a.dtype.kind in "OUS":
                data = a.tolist()
            else:
                data = _sig(a.ravel(), 4)
            out[k] = {"shape": list(a.shape), "data": data}
        return out

    # -- actions
    def run(self, body: dict) -> dict:
        only, overrides = body.get("only"), body.get("overrides") or None
        variant = body.get("variant") or "baseline"
        force, keep = bool(body.get("force")), bool(body.get("keep_cases"))
        self.variant_dir(variant)
        if isinstance(only, str):
            only = [s for s in only.split(",") if s]
        if self.jobs.running:
            raise BadRequest("A run is already in progress.", 409)
        from opencae.drone import pipeline  # lazy: the page still loads without it

        def job(progress, cancelled):
            kw = {"events": self.jobs.on_event} if _accepts(pipeline.run_project, "events") else {}
            return pipeline.run_project(self.project_dir, only=only, overrides=overrides, variant=variant,
                                        progress=progress, cancelled=cancelled, force=force,
                                        keep_cases=keep, **kw)

        self.jobs.start("run", job)
        return {"started": True, "variant": variant}

    def save_params(self, body: dict) -> dict:
        overrides = body.get("overrides")
        if not isinstance(overrides, dict) or not overrides:
            raise BadRequest("Send {\"overrides\": {\"dotted.key\": value}}.")
        from opencae.drone import spec
        try:
            new = spec.apply_overrides(spec.load(self.project_dir), overrides)
            problems = spec.validate(new)
        except (KeyError, ValueError, TypeError) as e:
            raise BadRequest(str(e.args[0]) if e.args else str(e))
        if problems:
            raise BadRequest("; ".join(problems))
        spec.save(new, self.project_dir / "drone.json")
        return {"ok": True, "spec": spec.load(self.project_dir)}

    def iterate(self, body: dict) -> dict:
        overrides = body.get("overrides")
        name = str(body.get("name") or "").strip()
        if not isinstance(overrides, dict) or not overrides:
            raise BadRequest("Send {\"overrides\": {...}, \"name\": \"...\"}.")
        if not _VARIANT_RE.match(name):
            raise BadRequest("Give the variant a short name (letters, digits, - and _, at most 40).")
        if name == "baseline":
            raise BadRequest("\"baseline\" is reserved.")
        from opencae.drone import pipeline, spec
        try:  # validate before starting so the form can show the message
            spec.apply_overrides(spec.load(self.project_dir), overrides)
        except (KeyError, ValueError, TypeError) as e:
            raise BadRequest(str(e.args[0]) if e.args else str(e))
        only = body.get("only")

        def job(progress, cancelled):
            kw = {"events": self.jobs.on_event} if _accepts(pipeline.iterate, "events") else {}
            return pipeline.iterate(self.project_dir, overrides, name, only=only, progress=progress,
                                    cancelled=cancelled, **kw)

        self.jobs.start("iterate", job)
        return {"started": True, "variant": name}

    def history(self) -> list:
        """The earlier runs of this project (``pipeline.run_history``), newest last."""
        from opencae.drone import pipeline
        return pipeline.run_history(self.project_dir)

    def stop_job(self) -> dict:
        return {"stopping": self.jobs.stop()}

    # -- K09-L design review site
    def k09l_ready(self) -> bool:
        return (self.k09l_dir / "index.html").exists()

    def ensure_k09l(self) -> bool:
        if self.k09l_ready():
            return True
        if not self.jobs.running:
            def job(progress, cancelled):
                from opencae.engineering.study import Study
                from opencae.web.build import build_sites
                progress("Building the K09-L design review site", 0.05)
                build_sites(Study.open(K09L_STUDY), out=self.k09l_dir,
                            progress=lambda *a: progress(str(a[0]) if a else "", None))
                return {}
            try:
                self.jobs.start("k09l-site", job)
            except BadRequest:
                pass
        return False


# ------------------------------------------------------------------------------------------------ http
_BUILDING = ("<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=2><title>Building</title>"
             "<body style=\"font:15px system-ui;padding:40px;color:#46545f\">Building the K09-L design review "
             "site (first time only, about a minute)...</body>").encode()


class _Handler(BaseHTTPRequestHandler):
    app: DashboardServer
    protocol_version = "HTTP/1.1"
    server_version = "OpenCAEDashboard"

    def log_message(self, *a):  # quiet
        pass

    # -- plumbing
    def _send(self, status: int, body: bytes, ctype: str, nostore: bool = False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if nostore else "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200):
        self._send(status, _dumps(obj), "application/json; charset=utf-8", nostore=True)

    def _error(self, status: int, message: str):
        self._json({"error": message}, status)

    def _static(self, root: Path, rel: str):
        root = root.resolve()
        try:
            f = (root / rel).resolve()
        except (OSError, ValueError):
            return self._error(404, "Not found")
        if root not in f.parents or not f.is_file():
            return self._error(404, "Not found")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype)

    def _host_ok(self) -> bool:
        """Only our own host:port, so a web page on another site cannot reach the API (DNS rebinding)."""
        port = self.app.port
        return (self.headers.get("Host") or "").lower() in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _origin_ok(self) -> bool:
        o = self.headers.get("Origin")
        if o is None:
            return True
        port = self.app.port
        return o.lower() in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def _query(self):
        u = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        return unquote(u.path), q

    # -- Results Hub
    def _hub_get(self, path: str, q: dict):
        """GET /hub/... (the page) and /api/hub/... (JSON, or the PNG of image.png)."""
        from opencae.hub import HubError, adapters, render
        if path in ("/hub", "/hub/"):
            if not (HUB_DIR / "index.html").is_file():
                return self._error(404, "The Results Hub page is not installed (opencae/web/hub/index.html).")
            return self._static(HUB_DIR, "index.html")
        if path.startswith("/hub/"):
            return self._static(HUB_DIR, path[len("/hub/"):])
        if not self._host_ok():
            return self._error(403, "Bad Host header.")
        route = path[len("/api/hub/"):]
        pid, run = q.get("pid", ""), q.get("run") or "latest"
        try:
            if route == "projects":
                return self._json(_clean(adapters.list_projects()))
            if route not in ("project", "scene", "plots", "audit", "motion", "compare", "image.png"):
                return self._error(404, "Not found")
            if not pid:
                return self._error(400, "pid is required")
            if route == "project":
                return self._json(_clean(adapters.project_view(pid, run)))
            if route == "scene":
                return self._json(_clean(adapters.scene(pid, run)))
            if route == "plots":
                return self._json(_clean(adapters.plots(pid, run)))
            if route == "audit":
                return self._json(_clean(adapters.audit(pid, run)))
            if route == "motion":
                return self._json(_clean(adapters.motion(pid, run)))
            if route == "compare":
                if not q.get("a") or not q.get("b"):
                    return self._error(400, "a and b (run ids) are required")
                return self._json(_clean(adapters.compare(pid, q["a"], q["b"])))
            data = render.render_png(pid, run, q.get("field", ""), q.get("view") or "iso")
            return self._send(200, data, "image/png")
        except HubError as e:
            return self._error(e.status, str(e))

    def _hub_post(self, path: str, body: dict):
        """POST /api/hub/client {webgl, error, ua}: a browser reports whether WebGL works (logged)."""
        if path != "/api/hub/client":
            return self._error(404, "Not found")
        if not isinstance(body.get("webgl"), bool):
            raise BadRequest("webgl must be true or false.")
        from opencae.utilities.logging_setup import get_logger
        get_logger("hub").info("client webgl=%s error=%s ua=%s", body["webgl"], str(body.get("error") or "")[:400],
                               str(body.get("ua") or "")[:200])
        return self._json({"ok": True})

    # -- verbs
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, q = self._query()
        try:
            if path == "/hub" or path.startswith("/hub/") or path.startswith("/api/hub/"):
                return self._hub_get(path, q)
            if self.app.project_dir is None:  # hub-only server: no project dashboard
                if path in ("/", "/index.html"):
                    self.send_response(302)
                    self.send_header("Location", "/hub/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path.startswith("/vendor/"):
                    return self._static(VENDOR_DIR, path[len("/vendor/"):])
                return self._error(404, "No project is open: this server shows the Results Hub at /hub/")
            if path in ("/", "/index.html"):
                return self._static(DASH_DIR, "index.html")
            if path.startswith("/assets/"):
                return self._static(DASH_DIR, path[len("/assets/"):])
            if path.startswith("/vendor/"):
                return self._static(VENDOR_DIR, path[len("/vendor/"):])
            if path == "/k09l":
                self.send_response(301)
                self.send_header("Location", "/k09l/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path.startswith("/k09l/"):
                if not self.app.project().get("k09l"):
                    return self._error(404, "Not a K09-L project")
                if not self.app.ensure_k09l():
                    return self._send(200, _BUILDING, "text/html; charset=utf-8", nostore=True)
                return self._static(self.app.k09l_dir, path[len("/k09l/"):] or "index.html")
            if path.startswith("/api/") and not self._host_ok():
                return self._error(403, "Bad Host header.")
            if path == "/api/project":
                return self._json(self.app.project())
            if path == "/api/results":
                return self._json(self.app.results(q.get("variant", "baseline")))
            if path == "/api/geometry":
                return self._json(self.app.geometry(q.get("variant", "baseline")))
            if path == "/api/field":
                return self._json(self.app.field(q.get("variant", "baseline"), q.get("file", "")))
            if path == "/api/progress":
                return self._json(self.app.jobs.snapshot())
            if path == "/api/history":
                return self._json(self.app.history())
            if path == "/api/variants":
                return self._json({"variants": self.app.variants()})
            return self._error(404, "Not found")
        except BadRequest as e:
            return self._error(e.status, str(e))
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:  # never crash the thread; tell the page
            return self._error(500, f"{e.__class__.__name__}: {e}")

    def do_POST(self):
        path, _ = self._query()
        try:
            if not self._host_ok() or not self._origin_ok():
                self.close_connection = True  # the body is left unread
                return self._error(403, "Request refused: it did not come from this page.")
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                self.close_connection = True
                return self._error(415, "Send Content-Type: application/json.")
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except ValueError:
                raise BadRequest("The request body is not valid JSON.")
            if not isinstance(body, dict):
                raise BadRequest("The request body must be a JSON object.")
            if path.startswith("/api/hub/"):
                return self._hub_post(path, body)
            if self.app.project_dir is None:
                return self._error(404, "No project is open: this server shows the Results Hub at /hub/")
            if path == "/api/run":
                return self._json(self.app.run(body))
            if path == "/api/params":
                return self._json(self.app.save_params(body))
            if path == "/api/iterate":
                return self._json(self.app.iterate(body))
            if path == "/api/stop":
                return self._json(self.app.stop_job())
            return self._error(404, "Not found")
        except BadRequest as e:
            return self._error(e.status, str(e))
        except ImportError as e:
            return self._error(501, f"The drone pipeline is not available: {e}")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            return self._error(500, f"{e.__class__.__name__}: {e}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m opencae.web.server", description="OpenCAE drone dashboard")
    ap.add_argument("project", help="drone project folder (contains drone.json)")
    ap.add_argument("--port", type=int, default=0, help="port (default: a free one)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    a = ap.parse_args(argv)
    proj = Path(a.project)
    if not (proj / "drone.json").exists():
        print(f"No drone.json in {proj}")
        return 2
    srv = DashboardServer(proj, port=a.port)
    url = srv.start()
    print(f"OpenCAE dashboard: {url}   (Ctrl+C to stop)")
    if not a.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
