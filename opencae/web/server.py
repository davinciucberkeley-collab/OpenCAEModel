"""Local dashboard server for drone projects (stdlib only).

``DashboardServer(project_dir)`` serves the page in ``opencae/web/dashboard`` and a small JSON API on
127.0.0.1. Runs happen on one background thread at a time (``JobManager``); the page polls
``/api/progress``. ``opencae.drone.pipeline`` and ``opencae.drone.spec`` are imported lazily inside the
handlers, so this module imports even when they are missing.

API: GET /api/project, /api/results?variant=, /api/geometry?variant=, /api/field?variant=&file=,
/api/progress, /api/variants; POST /api/run, /api/params, /api/iterate, /api/stop.
"""
from __future__ import annotations

import argparse
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
K09L_STUDY = HERE.parent / "examples" / "k09l" / "k09l_study.json"
MAX_TRIANGLES = 200_000
_VARIANT_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")
_FOOTER = "Simulation results plan the tests; they are not flight approval."

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------------------------------------ jobs
class JobManager:
    """One background job at a time. ``progress`` is the dict the page polls."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self.progress = {"running": False, "kind": "", "step": "", "frac": 0.0, "log": [], "error": "",
                         "started": None, "finished": None, "result": None}

    # snapshot for JSON
    def snapshot(self) -> dict:
        with self._lock:
            d = dict(self.progress)
            d["log"] = list(self.progress["log"])
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
            if msg:
                self.progress["step"] = str(msg)
                self.progress["log"].append(str(msg))
                del self.progress["log"][:-200]
            if frac is not None:
                try:
                    self.progress["frac"] = max(0.0, min(1.0, float(frac)))
                except (TypeError, ValueError):
                    pass

    def start(self, kind: str, fn: Callable) -> None:
        """Run ``fn(progress_cb, cancelled)`` in a thread. Raises BadRequest(409) if one is running."""
        with self._lock:
            if self.progress["running"]:
                raise BadRequest("A run is already in progress.", 409)
            self._cancel.clear()
            self.progress.update({"running": True, "kind": kind, "step": "Starting", "frac": 0.0, "log": [],
                                  "error": "", "started": time.time(), "finished": None, "result": None})

        def body():
            err, res = "", None
            try:
                res = fn(self._on_progress, self.cancelled)
            except BaseException as e:  # a solver failure is reported, never hidden
                err = str(e) or e.__class__.__name__
                if self._cancel.is_set():
                    err = "Stopped."
            with self._lock:
                self.progress.update({"running": False, "error": err, "finished": time.time(),
                                      "frac": 1.0 if not err else self.progress["frac"],
                                      "step": "Done" if not err else "Stopped" if err == "Stopped." else "Failed"})
                if err:
                    self.progress["log"].append(err)
                self.progress["result"] = _small(res)

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
        self.project_dir = Path(project_dir).resolve()
        self.host = "127.0.0.1" if host in ("", "0.0.0.0") else host  # local only
        self.port = port
        self.jobs = JobManager()
        self.k09l_dir = Path.home() / ".opencae" / "k09l_website"
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.url = ""

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
        return self.url

    def stop(self) -> None:
        self.jobs.stop()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None

    # -- project data
    def _drone_json(self) -> dict:
        p = self.project_dir / "drone.json"
        try:
            from opencae.drone import spec as _spec
            return _spec.load(self.project_dir)
        except ImportError:
            pass
        if not p.exists():
            raise BadRequest("No drone.json in the project folder.", 404)
        return json.loads(p.read_text(encoding="utf-8"))

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
        return {"name": spec.get("name") or self.project_dir.name, "type": spec.get("type", ""),
                "engine": spec.get("engine", "generic"), "spec": spec, "dir": str(self.project_dir),
                "variants": self.variants(), "footer": _FOOTER, "k09l": spec.get("engine") == "k09l"}

    def results(self, variant: str) -> dict:
        f = self.variant_dir(variant) / "results.json"
        if not f.exists():
            raise BadRequest("Not run yet", 404)
        return _clean(json.loads(f.read_text(encoding="utf-8")))

    def geometry(self, variant: str) -> dict:
        d = self.variant_dir(variant)
        f = d / "geometry.npz"
        if not f.exists():
            f = self.project_dir / "results" / "baseline" / "geometry.npz"
        if not f.exists():
            raise BadRequest("No geometry yet; run the project first.", 404)
        with np.load(f, allow_pickle=False) as z:
            names = [str(s) for s in z["names"]]
            roles = [str(s) for s in z["roles"]] if "roles" in z.files else ["other"] * len(names)
            mats = [str(s) for s in z["materials"]] if "materials" in z.files else [""] * len(names)
            P = [z[f"p{i}"] for i in range(len(names))]
            T = [z[f"t{i}"] for i in range(len(names))]
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
        return {"parts": parts, "decimated": stride > 1, "stride": stride, "triangles_total": int(total),
                "units": "mm"}

    def field(self, variant: str, file: str) -> dict:
        d = self.variant_dir(variant).resolve()
        f = (d / file).resolve()
        if not file or d not in f.parents or f.suffix != ".npz" or not f.exists():
            raise BadRequest("No such field file.", 404)
        out = {}
        with np.load(f, allow_pickle=False) as z:
            for k in z.files:
                a = z[k]
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
        force = bool(body.get("force"))
        self.variant_dir(variant)
        if isinstance(only, str):
            only = [s for s in only.split(",") if s]
        if self.jobs.running:
            raise BadRequest("A run is already in progress.", 409)
        from opencae.drone import pipeline  # lazy: the page still loads without it

        def job(progress, cancelled):
            return pipeline.run_project(self.project_dir, only=only, overrides=overrides, variant=variant,
                                        progress=progress, cancelled=cancelled, force=force)

        self.jobs.start("run", job)
        return {"started": True, "variant": variant}

    def save_params(self, body: dict) -> dict:
        overrides = body.get("overrides")
        if not isinstance(overrides, dict) or not overrides:
            raise BadRequest("Send {\"overrides\": {\"dotted.key\": value}}.")
        from opencae.drone import spec
        try:
            new = spec.apply_overrides(spec.load(self.project_dir), overrides)
            problems = spec.validate(new) if hasattr(spec, "validate") else []
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
            raise BadRequest("Give the variant a short name (letters, digits, - _ .).")
        if name == "baseline":
            raise BadRequest("\"baseline\" is reserved.")
        from opencae.drone import pipeline, spec
        try:  # validate before starting so the form can show the message
            spec.apply_overrides(spec.load(self.project_dir), overrides)
        except (KeyError, ValueError, TypeError) as e:
            raise BadRequest(str(e.args[0]) if e.args else str(e))
        only = body.get("only")

        def job(progress, cancelled):
            try:
                return pipeline.iterate(self.project_dir, overrides, name, only=only, progress=progress,
                                        cancelled=cancelled)
            except TypeError:  # older signature without progress/cancel
                return pipeline.iterate(self.project_dir, overrides, name, only=only)

        self.jobs.start("iterate", job)
        return {"started": True, "variant": name}

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

    def _query(self):
        u = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        return unquote(u.path), q

    # -- verbs
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, q = self._query()
        try:
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
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except ValueError:
                raise BadRequest("The request body is not valid JSON.")
            if not isinstance(body, dict):
                raise BadRequest("The request body must be a JSON object.")
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
