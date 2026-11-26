"""The Results Hub inside the application: one dashboard for every project.

``install(main_window)`` (called by ``MainWindow._install_extensions``) adds a **Results hub** button to the Home,
Drone and Motion ribbon pages, View > Results hub and Ctrl+Shift+R. It opens a page of the centre stack (like Drone
mode) that shows ``<server>/hub/?pid=<project>&tab=<tab>`` in a QtWebEngine view.

WebGL inside the application can fail on a PC ("THREE.WebGLRenderer: Error creating WebGL context"), so this page
never depends on it:

* **Open in browser** hands the same URL to the normal browser (which has working WebGL),
* **Show in 3D viewer** draws a result field of the project (``/api/hub/scene``) in a native pyvista viewport
  (``opencae.ui.viewport.Interactor``) with a field picker and a colour bar,
* a link ``opencae://show3d?pid=&run=&field=`` in the page opens that viewer (so the page can offer it),
* when the web view is not allowed (safe mode, ``OPENCAE_NO_WEBVIEW=1``) the same buttons stay and a short text says why.

The backend (``opencae.hub``) is imported only when the page is opened; when it is missing the page says so in plain
words. Everything that talks to a server is in :func:`start_hub_server` / :class:`HubView.ensure_server`.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel, QPushButton, QStackedWidget, QVBoxLayout,
                               QWidget)

from opencae.ui import theme

log = logging.getLogger("opencae.ui")

SHORTCUT = "Ctrl+Shift+R"
TABS = ("overview", "3d", "plots", "motion", "audit", "history")
WEBGL_PROBE = ("(function(){try{var c=document.createElement('canvas');"
               "var g=c.getContext('webgl2')||c.getContext('webgl');"
               "if(!g)return false;var e=g.getExtension('WEBGL_lose_context');if(e)e.loseContext();return true;}"
               "catch(err){return false;}})()")
MISSING_BACKEND = ("The Results Hub backend is not part of this installation (the opencae.hub module is missing), "
                   "so there is nothing to show yet. Update OpenCAE Studio, then press Refresh.")


def webgl_failure(message: str) -> bool:
    """True for a browser console line that means WebGL cannot be used in this view."""
    m = (message or "").lower()
    return "webgl" in m and any(k in m for k in ("error creating", "context_lost", "context lost", "not supported",
                                                "could not create", "failed to create"))


def hub_url(base: str, pid: str = "", tab: str = "", run: str = "") -> str:
    """``<base>hub/?pid=&run=&tab=`` (only the given parts)."""
    q = {k: v for k, v in (("pid", pid), ("run", run), ("tab", tab)) if v}
    return base.rstrip("/") + "/hub/" + ("?" + urllib.parse.urlencode(q) if q else "")


def parse_show3d(url) -> Optional[dict]:
    """``opencae://show3d?pid=&run=&field=`` -> {"pid", "run", "field"}, else None."""
    s = url.toString() if hasattr(url, "toString") else str(url)
    u = urllib.parse.urlparse(s)
    if u.scheme != "opencae" or (u.netloc or u.path.strip("/")) != "show3d":
        return None
    q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
    return {"pid": q.get("pid", ""), "run": q.get("run", "") or "latest", "field": q.get("field", "")}


# ------------------------------------------------------------------------------------------------ the server
def start_hub_server():
    """A DashboardServer that serves the hub for every project (needs no project folder of its own). The one place
    that decides how the application starts it; returns (server, url) or raises. Kept apart so that a dedicated hub
    server (``opencae hub``) can replace it without touching the rest."""
    from opencae.web.server import DashboardServer
    from opencae.utilities.crash import home_dir
    last: Optional[Exception] = None
    for project_dir in (None, home_dir()):
        try:
            srv = DashboardServer(project_dir)
            return srv, srv.start()
        except Exception as exc:  # noqa: BLE001 - try the next way
            last = exc
    raise RuntimeError(f"could not start the dashboard server: {last}")


# ------------------------------------------------------------------------------------------------ scene -> pyvista
def _f32(b64: str, cols: int = 0) -> np.ndarray:
    a = np.frombuffer(base64.b64decode(b64), dtype="<f4").astype(np.float32)
    return a.reshape(-1, cols) if cols else a


def _tri(b64: str):
    import pyvista as pv
    t = np.frombuffer(base64.b64decode(b64), dtype="<i4").astype(np.int64).reshape(-1, 3)
    return np.hstack([np.full((len(t), 1), 3, dtype=np.int64), t]).ravel()


def scene_to_meshes(scene: dict) -> dict:
    """Decode a ``/api/hub/scene`` answer: {"parts": {name: PolyData}, "fields": [{id, label, unit, min, max, mesh,
    notes}], "notes": [str]}. A field whose values fit neither the points nor the triangles is dropped with a note
    (nothing is invented)."""
    import pyvista as pv
    parts: dict = {}
    notes = list(scene.get("notes") or [])
    for p in scene.get("parts") or []:
        try:
            parts[p["name"]] = pv.PolyData(_f32(p["points"], 3), _tri(p["triangles"]))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Part {p.get('name', '?')} could not be read: {exc}")
    fields = []
    for f in scene.get("fields") or []:
        try:
            vals = _f32(f["values"])
            if f.get("mesh") == "own" or f.get("mesh") not in parts:
                if not f.get("points") or not f.get("triangles"):
                    raise ValueError("no mesh for this field")
                mesh = pv.PolyData(_f32(f["points"], 3), _tri(f["triangles"]))
            else:
                mesh = parts[f["mesh"]].copy()
            if len(vals) == mesh.n_points:
                mesh.point_data["values"] = vals
            elif len(vals) == mesh.n_cells:
                mesh.cell_data["values"] = vals
            else:
                raise ValueError(f"{len(vals)} values for {mesh.n_points} points / {mesh.n_cells} triangles")
            lo = float(f["min"]) if f.get("min") is not None else float(np.nanmin(vals))
            hi = float(f["max"]) if f.get("max") is not None else float(np.nanmax(vals))
            fields.append({"id": f.get("id", ""), "label": f.get("label") or f.get("id", ""), "unit": f.get("unit", ""),
                           "min": lo, "max": hi, "mesh": mesh, "part": f.get("mesh", "")})
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Field {f.get('label') or f.get('id', '?')} not shown: {exc}")
    return {"parts": parts, "fields": fields, "notes": notes}


class NativeSceneView(QWidget):
    """A pyvista view (``opencae.ui.viewport.Interactor``) of one project's result fields, with a field picker."""

    scene_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene: dict = {}
        self.plotter = None
        self._token = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.setContentsMargins(6, 4, 6, 4)
        row.addWidget(QLabel("Show:"))
        self.picker = QComboBox()
        self.picker.setMinimumWidth(260)
        self.picker.setToolTip("Which result to colour the model with")
        self.picker.currentIndexChanged.connect(lambda *_: self.draw())
        row.addWidget(self.picker)
        self.status = QLabel("")
        self.status.setStyleSheet(f"color:{theme.FG_DIM}")
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        lay.addLayout(row)
        self.holder = QVBoxLayout()
        lay.addLayout(self.holder, 1)
        self.scene_ready.connect(self.set_scene)
        self.failed.connect(self._failed)

    # ---- loading
    def load(self, base: str, pid: str, run: str = "latest", field: str = "") -> None:
        """Fetch the scene in a thread (never blocks the window), then show it."""
        self._token += 1
        token = self._token
        self._want_field = field
        self.status.setText("Loading the 3D results ...")
        q = urllib.parse.urlencode({k: v for k, v in (("pid", pid), ("run", run)) if v})
        url = f"{base.rstrip('/')}/api/hub/scene?{q}"

        def work():
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    data = json.loads(r.read().decode("utf-8"))
                if token == self._token:
                    self.scene_ready.emit(data)
            except Exception as exc:  # noqa: BLE001 - shown in the panel
                if token == self._token:
                    self.failed.emit(f"Could not load the 3D results: {exc}")
        threading.Thread(target=work, name="opencae-hub-scene", daemon=True).start()

    def _failed(self, text: str) -> None:
        self.status.setText(text)

    def set_scene(self, scene: dict) -> None:
        try:
            self.scene = scene_to_meshes(scene)
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"The 3D results could not be read: {exc}")
            return
        self.picker.blockSignals(True)
        self.picker.clear()
        for f in self.scene["fields"]:
            self.picker.addItem(f"{f['label']} [{f['unit']}]" if f["unit"] else f["label"], f["id"])
        want = getattr(self, "_want_field", "")
        if want:
            i = self.picker.findData(want)
            if i >= 0:
                self.picker.setCurrentIndex(i)
        self.picker.blockSignals(False)
        self.draw()

    # ---- drawing
    def _ensure_plotter(self) -> bool:
        if self.plotter is not None:
            return True
        try:
            from opencae.ui.viewport import Interactor
            self.plotter = Interactor(self, auto_update=False)
            self.plotter.set_background(theme.VIEW_BG_BOTTOM, top=theme.VIEW_BG_TOP)
            self.holder.addWidget(self.plotter.interactor)
            return True
        except Exception as exc:  # noqa: BLE001 - no OpenGL: say so, never crash
            self.plotter = None
            self.status.setText(f"The built-in 3D viewer could not start on this PC ({exc}). "
                                "Use Open in browser instead.")
            log.warning("hub native viewer unavailable: %s", exc)
            return False

    def current_field(self) -> Optional[dict]:
        i = self.picker.currentIndex()
        fields = (self.scene or {}).get("fields") or []
        return fields[i] if 0 <= i < len(fields) else None

    def draw(self) -> None:
        sc = self.scene or {}
        notes = " ".join(sc.get("notes") or [])
        f = self.current_field()
        if not sc.get("parts") and f is None:
            self.status.setText(notes or "This run has no 3D results to show.")
            return
        if not self._ensure_plotter():
            return
        pl = self.plotter
        try:
            pl.clear()
            for name, mesh in sc.get("parts", {}).items():
                if f is None or name != f.get("part"):
                    pl.add_mesh(mesh, color="#9aa4b2", opacity=1.0 if f is None else 0.25, name=f"part:{name}")
            if f is not None:
                key = "values"
                title = f"{f['label']} [{f['unit']}]" if f["unit"] else f["label"]
                pl.add_mesh(f["mesh"], scalars=key, cmap="turbo", clim=(f["min"], f["max"]), name="field",
                            scalar_bar_args={"title": title, "color": "white", "vertical": True, "position_x": 0.9,
                                                 "position_y": 0.1, "height": 0.78, "width": 0.06,
                                                 "title_font_size": 14, "label_font_size": 12}, show_scalar_bar=True)
            pl.reset_camera()
            pl.render()
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"Could not draw the 3D results: {exc}")
            return
        self.status.setText(notes or (f"{f['label']}: {f['min']:.4g} to {f['max']:.4g} {f['unit']}".strip()
                                      if f else ""))

    def close_plotter(self) -> None:
        if self.plotter is not None:
            try:
                self.plotter.close()
            except Exception:  # noqa: BLE001
                pass
            self.plotter = None


# ------------------------------------------------------------------------------------------------ the web page
_PAGE_CLASS = None


def _page_class():
    """QWebEnginePage subclass (created on first use: QtWebEngine is never imported at start)."""
    global _PAGE_CLASS
    if _PAGE_CLASS is None:
        from PySide6.QtWebEngineCore import QWebEnginePage

        class HubPage(QWebEnginePage):
            show3d = Signal(dict)
            webgl_failed = Signal(str)

            def acceptNavigationRequest(self, url, nav_type, is_main_frame):  # noqa: N802
                req = parse_show3d(url)
                if req is not None:
                    self.show3d.emit(req)
                    return False
                if url.scheme() == "opencae":
                    return False
                return super().acceptNavigationRequest(url, nav_type, is_main_frame)

            def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
                if webgl_failure(message):
                    log.warning("Results Hub: %s", message)
                    self.webgl_failed.emit(message)
                super().javaScriptConsoleMessage(level, message, line, source)

        _PAGE_CLASS = HubPage
    return _PAGE_CLASS


# ------------------------------------------------------------------------------------------------ the page
class HubView(QWidget):
    """The Results Hub page of the main window's centre stack."""

    back_requested = Signal()

    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self.pid = ""
        self.run = ""
        self.tab = ""
        self.project_path: Optional[Path] = None
        self.project_name = ""
        self.base = ""
        self.url = ""
        self.hub_missing = False
        self.webgl_ok: Optional[bool] = None
        self.web = None
        self._server = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 6, 8, 6)
        self.title = QLabel("Results Hub")
        f = self.title.font()
        f.setBold(True)
        self.title.setFont(f)
        h.addWidget(self.title)
        self.status = QLabel("")
        self.status.setStyleSheet(f"color:{theme.FG_DIM}")
        h.addWidget(self.status, 1)
        self.refresh_btn = self._btn("Refresh", "Load the page again", lambda: self.reload())
        self.browser_btn = self._btn("Open in browser", "Open the Results Hub in your normal web browser "
                                     "(3D usually works there)", lambda: self.open_in_browser())
        self.browser_btn.setObjectName("primary")
        self.viewer_btn = self._btn("Show in 3D viewer", "Draw the result fields in the built-in 3D viewer "
                                    "(needs no WebGL)", lambda: self.show_native())
        self.back_btn = self._btn("Back", "Return to what you were doing", lambda: self.back_requested.emit())
        for b in (self.refresh_btn, self.viewer_btn, self.browser_btn, self.back_btn):
            h.addWidget(b)
        root.addWidget(bar)
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setStyleSheet(f"color:{theme.FG_DIM}; font-size:14px; padding:40px")
        self.pages.addWidget(self.message)                       # 0: message (no web view / no backend)
        self.native = NativeSceneView()
        self.pages.addWidget(self.native)                        # 1: native 3D viewer
        self._make_web()
        self.pages.setCurrentIndex(0)

    def _btn(self, text, tip, slot) -> QPushButton:
        b = QPushButton(text)
        b.setToolTip(tip)
        b.clicked.connect(lambda checked=False, s=slot: s())
        return b

    # ---- the web view
    def web_reason(self) -> str:
        """Why the web view is not used (plain words), or "" when it can be."""
        try:
            from opencae.ui.drone.workspace import web_fallback_reason
            return web_fallback_reason().replace("the dashboard opens in your browser",
                                                 "the Results Hub opens in your browser")
        except Exception as exc:  # noqa: BLE001
            return f"The built-in web view is not available ({exc}), so the Results Hub opens in your browser."

    def _make_web(self) -> None:
        reason = self.web_reason()
        if not reason:
            try:
                from PySide6.QtWebEngineWidgets import QWebEngineView
                self.web = QWebEngineView()
                page = _page_class()(self.web)
                self.web.setPage(page)
                page.show3d.connect(self._on_show3d)
                page.webgl_failed.connect(lambda m: self._set_webgl(False, m))
                self.web.loadFinished.connect(self._probe_webgl)
                try:
                    self.web.renderProcessTerminated.connect(self._render_gone)
                except Exception:  # noqa: BLE001
                    pass
                self.pages.addWidget(self.web)                   # 2: the hub page
            except Exception as exc:  # noqa: BLE001 - a broken install falls back to the browser
                self.web = None
                reason = f"The built-in web view could not be loaded ({exc}), so the Results Hub opens in your browser."
        self.web_reason_text = reason

    def _probe_webgl(self, ok=True) -> None:
        if self.web is None or not ok:
            return
        try:
            self.web.page().runJavaScript(WEBGL_PROBE, lambda v: self._set_webgl(bool(v), "" if v else
                                                                                  "WebGL is not available in this view"))
        except Exception:  # noqa: BLE001
            pass

    def _set_webgl(self, ok: bool, why: str = "") -> None:
        self.webgl_ok = ok
        if not ok:
            self.status.setText("3D is not available in this window on this PC: use Show in 3D viewer or Open in browser.")
            self.viewer_btn.setProperty("hint", True)
            log.warning("Results Hub: WebGL unavailable in the embedded view (%s)", why)

    def _render_gone(self, *_a) -> None:
        log.error("Results Hub web view render process terminated")
        self._show_message("The built-in web view stopped. Your results are safe. Use Open in browser, "
                           "or press Refresh.")

    # ---- project, server, url
    def register(self, path) -> Optional[dict]:
        """Register the project folder / .ocae with the hub registry; None (and hub_missing) when V1 is absent."""
        try:
            from opencae.hub import registry
        except ImportError:
            self.hub_missing = True
            return None
        self.hub_missing = False
        try:
            return registry.register(Path(path))
        except Exception as exc:  # noqa: BLE001 - say what failed
            log.exception("could not register %s with the Results Hub", path)
            self.status.setText(f"Could not add this project to the Results Hub: {exc}")
            return None

    def ensure_server(self) -> str:
        """Base URL (ending with /) of a running dashboard server: the Drone mode one when there is one, else one
        of our own (started once)."""
        ws = getattr(self.mw, "_drone", None)
        srv = getattr(ws, "server", None)
        if srv is not None and getattr(srv, "url", ""):
            self.base = srv.url
            return self.base
        if self._server is not None and getattr(self._server, "url", ""):
            self.base = self._server.url
            return self.base
        self._server, self.base = start_hub_server()
        return self.base

    def build_url(self) -> str:
        return hub_url(self.base or "http://127.0.0.1/", self.pid, self.tab, self.run)

    def open_project(self, path=None, tab: str = "", run: str = "") -> bool:
        """Point the page at a project (``path`` = drone folder or .ocae; None = no project) and load it."""
        self.tab, self.run = tab or "", run or ""
        self.project_path = Path(path) if path else None
        self.pid, self.project_name = "", self.project_path.stem if self.project_path else ""
        self.hub_missing = False
        if self.project_path is not None:
            rec = self.register(self.project_path)
            if rec:
                self.pid = str(rec.get("id", ""))
                self.project_name = str(rec.get("name") or self.project_name)
        else:
            try:
                import opencae.hub  # noqa: F401
            except ImportError:
                self.hub_missing = True
        self.title.setText("Results Hub" + (f"  -  {self.project_name}" if self.project_name else ""))
        for b in (self.refresh_btn, self.browser_btn, self.viewer_btn):
            b.setEnabled(not self.hub_missing)
        if self.hub_missing:
            self._show_message(MISSING_BACKEND)
            return False
        try:
            self.ensure_server()
        except Exception as exc:  # noqa: BLE001
            log.exception("Results Hub server did not start")
            self._show_message(f"The Results Hub server could not start: {exc}")
            return False
        self.url = self.build_url()
        if self.web is None:
            self._show_message(self.web_reason_text + " Use Open in browser, or Show in 3D viewer for the model.")
            self.status.setText("")
            return True
        self.status.setText("")
        self.pages.setCurrentWidget(self.web)
        self.web.setUrl(QUrl(self.url))
        return True

    def reload(self) -> None:
        self.open_project(self.project_path, self.tab, self.run)

    def _show_message(self, text: str) -> None:
        self.message.setText(text)
        self.pages.setCurrentIndex(0)

    # ---- the buttons
    def open_in_browser(self) -> bool:
        if not self.url:
            return False
        return bool(QDesktopServices.openUrl(QUrl(self.url)))

    def show_native(self, field: str = "", run: str = "") -> None:
        """The native pyvista viewer for the current project (no WebGL)."""
        if self.hub_missing or not self.base:
            self.status.setText("Open a project in the Results Hub first.")
            return
        if not self.pid:
            self.status.setText("Save or open a project first: the 3D viewer shows the results of a project.")
            return
        self.native.load(self.base, self.pid, run or self.run or "latest", field)
        self.pages.setCurrentWidget(self.native)
        self.status.setText("Built-in 3D viewer. Press Refresh to go back to the web page.")

    def _on_show3d(self, req: dict) -> None:
        if req.get("pid") and req["pid"] != self.pid:
            self.pid = req["pid"]
        self.show_native(req.get("field", ""), req.get("run", ""))

    # ---- closing
    def shutdown(self) -> None:
        self.native.close_plotter()
        if self._server is not None:
            try:
                self._server.stop()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        if self.web is not None:
            try:
                self.web.setUrl(QUrl("about:blank"))
                self.web.stop()
            except Exception:  # noqa: BLE001
                pass


# ------------------------------------------------------------------------------------------------ the extension
class HubController(QObject):
    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        self.view: Optional[HubView] = None
        self.previous: Optional[QWidget] = None
        self.action = QAction("Results hub", mw)
        self.action.setObjectName("open_results_hub")
        self.action.setIconText("Results hub")
        self.action.setToolTip("See the results of every project in one place: contours, plots, audit, history "
                               f"({SHORTCUT})")
        self.action.setStatusTip("Open the Results Hub")
        self.action.setShortcut(QKeySequence(SHORTCUT))
        self.action.setShortcutContext(Qt.WindowShortcut)
        try:
            from opencae.ui.icons import icon
            self.action.setIcon(icon("contour"))
        except Exception:  # noqa: BLE001
            pass
        self.action.triggered.connect(lambda checked=False: self.show())
        mw.addAction(self.action)
        mw.installEventFilter(self)       # the window closing: stop our server and finish the 3D viewer first

    def eventFilter(self, obj, ev):  # noqa: N802
        if obj is self.mw and ev.type() == QEvent.Close and self.view is not None:
            try:
                self.view.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("Results Hub shutdown failed")
        return False

    # ---- which project is open
    def current_project(self) -> Optional[Path]:
        mw = self.mw
        ws = getattr(mw, "_drone", None)
        cur = mw.stack.currentWidget()
        if ws is not None and getattr(ws, "project_dir", None) and (cur is ws or cur is self.view and
                                                                    self.previous is ws):
            return Path(ws.project_dir)
        path = getattr(getattr(mw, "session", None), "path", None)
        if path and Path(path).exists() and (cur is not ws):
            return Path(path)
        if ws is not None and getattr(ws, "project_dir", None):
            return Path(ws.project_dir)
        return None

    def ensure_view(self) -> HubView:
        if self.view is None:
            self.view = HubView(self.mw)
            self.mw.stack.addWidget(self.view)
            self.view.back_requested.connect(self.back)
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self.view.shutdown)
        return self.view

    def show(self, tab: str = "", project=None, native: bool = False, field: str = "", run: str = "") -> HubView:
        """Open the Results Hub page on ``project`` (default: the open one) and bring it to the front."""
        view = self.ensure_view()
        cur = self.mw.stack.currentWidget()
        if cur is not view:
            self.previous = cur
        path = Path(project) if project else self.current_project()
        ok = view.open_project(path, tab, run)
        self.mw.stack.setCurrentWidget(view)
        if ok and native:
            view.show_native(field, run)
        return view

    def back(self) -> None:
        prev = self.previous if self.previous is not None else self.mw.viewport
        if self.mw.stack.indexOf(prev) < 0:
            prev = self.mw.viewport
        self.mw.stack.setCurrentWidget(prev)


def install(mw) -> HubController:
    existing = getattr(mw, "_hub_controller", None)
    if existing is not None:
        return existing
    ctl = HubController(mw)
    mw._hub_controller = ctl
    for page in ("Home", "Drone", "Motion"):
        bar = getattr(mw, "ribbon_bars", {}).get(page)
        if bar is not None:
            bar.addSeparator()
            bar.addAction(ctl.action)
    for a in mw.menuBar().actions():
        if a.text().replace("&", "") == "View" and a.menu() is not None:
            a.menu().addSeparator()
            a.menu().addAction(ctl.action)
            break
    return ctl
