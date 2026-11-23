"""Drone project window: parameter sheet, runs through the dashboard server's job manager, in-app dashboard.

The window starts a ``DashboardServer`` for the project and shows its page in a QWebEngineView (or offers the
system browser when QtWebEngine is missing). Runs are started with POST /api/run, so the app and the web page
show the same progress.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QFont, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHeaderView,
                               QLabel, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QTabWidget, QToolBar, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
                               QDockWidget)

from opencae.ui import theme

try:  # optional: without it the dashboard opens in the system browser
    from PySide6.QtWebEngineWidgets import QWebEngineView
    HAVE_WEB = True
except Exception:  # pragma: no cover - depends on the install
    QWebEngineView = None
    HAVE_WEB = False

EXAMPLES = {"Quad 250 (generic example)": "quad250", "K09-L tailsitter": "k09l"}
STATUSES = ["estimate", "datasheet", "measured", "user"]
SKIP_TOP = {"format", "name", "type", "engine", "notes", "description"}
FOOTER = "Simulation results plan the tests; they are not flight approval."


def examples_root() -> Path:
    return Path(__file__).resolve().parents[2] / "examples" / "drones"


def projects_home() -> Path:
    return Path(os.environ.get("OPENCAE_HOME") or Path.home() / ".opencae") / "projects"


def copy_example(key: str) -> Path:
    """Copy a bundled example to ~/.opencae/projects/<key> once (the package folder is never written to)."""
    src, dst = examples_root() / key, projects_home() / key
    if not src.exists():
        raise FileNotFoundError(f"example not found: {src}")
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("results", "__pycache__"))
    return dst


# ------------------------------------------------------------------------------------------------ parameter sheet
def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def iter_params(node, prefix=""):
    """Yield (dotted key, value, unit, status) for every numeric leaf of a drone.json section."""
    if isinstance(node, dict):
        if "value" in node and not any(isinstance(v, dict) for v in node.values()):
            if _is_number(node["value"]):
                yield prefix, node["value"], node.get("unit", ""), node.get("status", "estimate")
            return
        for k, v in node.items():
            yield from iter_params(v, f"{prefix}.{k}" if prefix else k)
    elif _is_number(node):
        yield prefix, node, "", "estimate"


class ParameterSheet(QTreeWidget):
    """Numeric inputs of drone.json grouped by top-level section: value, unit, evidence status."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(4)
        self.setHeaderLabels(["Parameter", "Value", "Unit", "Status"])
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed |
                             QAbstractItemView.SelectedClicked)
        self.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.header().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._orig: dict = {}
        self._items: dict = {}
        self._combos: dict = {}

    def load(self, spec: dict) -> None:
        self.clear()
        self._orig, self._items, self._combos = {}, {}, {}
        groups: dict = {}
        for key, value, unit, status in iter_params({k: v for k, v in spec.items() if k not in SKIP_TOP}):
            top = key.split(".")[0]
            if top not in groups:
                g = QTreeWidgetItem(self, [top.replace("_", " ").capitalize()])
                g.setFirstColumnSpanned(True)
                f = g.font(0)
                f.setBold(True)
                g.setFont(0, f)
                groups[top] = g
            rest = key[len(top) + 1:] or top
            it = QTreeWidgetItem(groups[top], [rest, _fmt(value), unit, ""])
            it.setFlags(it.flags() | Qt.ItemIsEditable)
            it.setData(0, Qt.UserRole, key)
            self._items[key] = it
            self._orig[key] = (value, status)
            cb = QComboBox()
            cb.addItems(STATUSES)
            cb.setCurrentText(status if status in STATUSES else "estimate")
            self.setItemWidget(it, 3, cb)
            self._combos[key] = cb
        for g in groups.values():
            g.setExpanded(True)
        self.resizeColumnToContents(0)

    def edits(self) -> tuple[dict, dict]:
        """(value overrides, status changes) against the loaded file. Raises ValueError on a non-number."""
        vals, stats = {}, {}
        for key, it in self._items.items():
            ov, os_ = self._orig[key]
            text = it.text(1).strip()
            try:
                num = float(text)
            except ValueError:
                raise ValueError(f"'{text}' is not a number ({key})")
            if num != float(ov):
                vals[key] = int(num) if isinstance(ov, int) and num == int(num) else num
            st = self._combos[key].currentText()
            if st != os_:
                stats[key] = st
        return vals, stats


def _fmt(v) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def _set_status(spec: dict, key: str, status: str) -> None:
    parts = key.split(".")
    node = spec
    for p in parts[:-1]:
        node = node[p]
    leaf = node[parts[-1]]
    if isinstance(leaf, dict):
        leaf["status"] = status
    else:
        node[parts[-1]] = {"value": leaf, "unit": "", "status": status, "source": ""}


# ------------------------------------------------------------------------------------------------ window
class DroneWindow(QMainWindow):
    def __init__(self, project_dir: Optional[Path] = None, parent=None, start_server: bool = True):
        super().__init__(parent)
        self.setWindowTitle("OpenCAE Drone")
        self.resize(1360, 860)
        self.project_dir: Optional[Path] = None
        self.server = None
        self.url = ""
        self._was_running = False
        self._start_server = start_server
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._poll)
        if project_dir is not None:
            self.open_project(Path(project_dir))
        else:
            self._enable(False)

    # ------------------------------------------------------------------ layout
    def _build(self):
        tb = QToolBar("Drone")
        tb.setMovable(False)
        self.addToolBar(tb)
        self.acts: dict = {}

        def act(key, text, fn, tip="", shortcut=None):
            a = QAction(text, self)
            a.triggered.connect(lambda checked=False, f=fn: f())
            a.setToolTip(tip or text)
            if shortcut:
                a.setShortcut(shortcut)
            tb.addAction(a)
            self.acts[key] = a
            return a

        act("new", "New…", self.new_project, "Create a drone project from a CAD file", QKeySequence.New)
        act("open", "Open…", self.open_dialog, "Open a drone project folder", QKeySequence.Open)
        ex = QToolButton()
        ex.setText("Open example")
        ex.setPopupMode(QToolButton.InstantPopup)
        exm = QMenu(ex)
        for label, key in EXAMPLES.items():
            a = exm.addAction(label)
            a.triggered.connect(lambda checked=False, k=key: self.open_example(k))
        ex.setMenu(exm)
        tb.addWidget(ex)
        tb.addSeparator()
        act("run_all", "Run all", lambda: self.run(None), "Run every analysis (finished ones are skipped)", "Ctrl+R")
        self.sel_btn = QToolButton()
        self.sel_btn.setText("Run selected")
        self.sel_btn.setPopupMode(QToolButton.InstantPopup)
        self.sel_menu = QMenu(self.sel_btn)
        from opencae.drone.types import ANALYSES
        for aid in ANALYSES:
            a = self.sel_menu.addAction(aid)
            a.triggered.connect(lambda checked=False, i=aid: self.run([i]))
        self.sel_btn.setMenu(self.sel_menu)
        tb.addWidget(self.sel_btn)
        act("stop", "Stop", self.stop, "Stop after the step that is running")
        tb.addSeparator()
        act("browser", "Open in browser", self.open_in_browser, "Open the dashboard in the system browser")

        # left dock: parameter sheet
        dock = QDockWidget("Drone setup", self)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(4, 4, 4, 4)
        self.sheet = ParameterSheet()
        v.addWidget(self.sheet, 1)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        v.addWidget(self.msg)
        self.save_btn = QPushButton("Save parameters")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(lambda checked=False: self.save_parameters())
        v.addWidget(self.save_btn)
        dock.setWidget(box)
        dock.setMinimumWidth(380)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        self.dock = dock

        # centre tabs
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.web = None
        if HAVE_WEB:
            self.web = QWebEngineView()
            self.tabs.addTab(self.web, "Dashboard")
        else:
            fb = QWidget()
            fl = QVBoxLayout(fb)
            fl.addStretch(1)
            lab = QLabel("The built-in web view is not available in this installation.\n"
                         "The dashboard opens in your normal browser instead.")
            lab.setAlignment(Qt.AlignCenter)
            fl.addWidget(lab)
            b = QPushButton("Open dashboard in browser")
            b.setObjectName("primary")
            b.clicked.connect(lambda checked=False: self.open_in_browser())
            fl.addWidget(b, 0, Qt.AlignCenter)
            fl.addStretch(1)
            self.tabs.addTab(fb, "Dashboard")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 9))
        self.tabs.addTab(self.log, "Log")

        # status bar
        self.prog = QProgressBar()
        self.prog.setMaximumWidth(300)
        self.prog.setRange(0, 1000)
        self.prog.setVisible(False)
        self.statusBar().addPermanentWidget(self.prog)
        self.statusBar().showMessage(FOOTER)

    def _enable(self, on: bool):
        for k in ("run_all", "stop", "browser"):
            self.acts[k].setEnabled(on)
        self.sel_btn.setEnabled(on)
        self.save_btn.setEnabled(on)

    def _log(self, text: str):
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")
        self.statusBar().showMessage(text, 8000)

    # ------------------------------------------------------------------ project
    def open_project(self, project_dir: Path, show_roles: bool = False) -> bool:
        project_dir = Path(project_dir)
        if not (project_dir / "drone.json").exists():
            QMessageBox.warning(self, "Open drone project", f"No drone.json in {project_dir}")
            return False
        self._stop_server()
        self.project_dir = project_dir
        self.setWindowTitle(f"OpenCAE Drone - {project_dir.name}")
        self._reload_sheet()
        self._enable(True)
        self._log(f"Project opened: {project_dir}")
        if self._start_server:
            try:
                from opencae.web.server import DashboardServer
                self.server = DashboardServer(project_dir, port=0)
                self.url = self.server.start()
                self._log(f"Dashboard server: {self.url}")
                if self.web is not None:
                    self.web.setUrl(QUrl(self.url))
                self.timer.start()
            except Exception as e:
                self._log(f"Could not start the dashboard server: {e}")
                self.msg.setStyleSheet(f"color:{theme.ERR}")
                self.msg.setText(f"Dashboard server not started: {e}")
        if show_roles:
            self.show_roles()
        return True

    def _reload_sheet(self):
        from opencae.drone import spec as S
        try:
            self.sheet.load(S.load(self.project_dir))
        except Exception as e:
            self.msg.setStyleSheet(f"color:{theme.ERR}")
            self.msg.setText(f"Could not read drone.json: {e}")

    def new_project(self):
        from opencae.ui.drone.new_dialog import NewDroneDialog
        dlg = NewDroneDialog(self)
        if dlg.exec() == QDialog.Accepted and dlg.project_dir:
            self.open_project(dlg.project_dir, show_roles=True)

    def open_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "Open drone project", str(projects_home()))
        if d:
            self.open_project(Path(d))

    def open_example(self, key: str):
        try:
            self.open_project(copy_example(key))
        except Exception as e:
            QMessageBox.warning(self, "Open example", str(e))

    def show_roles(self):
        """List the parts per role that were guessed from the part names, so the person can check them."""
        from opencae.cli import guess_roles_of
        roles = guess_roles_of(self.project_dir)
        self.roles_text = "\n".join(f"{r}: {len(n)} part(s) - {', '.join(n[:8])}{' ...' if len(n) > 8 else ''}"
                                    for r, n in roles.items()) or "No parts were found in the CAD file."
        self._log("Roles guessed from the part names:\n" + self.roles_text)
        QMessageBox.information(self, "Parts and roles guessed from the CAD file",
                                self.roles_text + "\n\nCheck them; roles can be changed in drone.json.")

    # ------------------------------------------------------------------ parameters
    def save_parameters(self) -> bool:
        """Write the edited values (and evidence statuses) to drone.json; on bad input show a red message and
        leave the file unchanged."""
        from opencae.drone import spec as S
        try:
            vals, stats = self.sheet.edits()
            cur = S.load(self.project_dir)
            new = S.apply_overrides(cur, vals) if vals else cur
            for k, st in stats.items():
                _set_status(new, k, st)
            problems = S.validate(new)
            if problems:
                raise ValueError("; ".join(problems))
            S.save(new, self.project_dir)
        except (KeyError, ValueError, TypeError) as e:
            self.msg.setStyleSheet(f"color:{theme.ERR}")
            self.msg.setText(f"Not saved: {str(e).strip(chr(39) + chr(34))}")
            return False
        self.msg.setStyleSheet(f"color:{theme.OK}")
        self.msg.setText("Saved." if (vals or stats) else "Nothing changed.")
        self._reload_sheet()
        self._log(f"Parameters saved ({len(vals)} value(s), {len(stats)} status(es) changed)")
        return True

    # ------------------------------------------------------------------ runs
    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.url.rstrip("/") + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")

    def run(self, only: Optional[list]):
        if not self.server:
            self._log("No dashboard server: cannot start a run.")
            return
        if not self.save_parameters():
            return
        try:
            self._post("/api/run", {"only": only} if only else {})
            self._log("Run started" + (f": {', '.join(only)}" if only else " (all analyses)"))
            self.tabs.setCurrentIndex(0)
        except Exception as e:
            self._log(f"Could not start the run: {e}")

    def stop(self):
        if self.server:
            try:
                self._post("/api/stop", {})
                self._log("Stop requested; the running step finishes first.")
            except Exception as e:
                self._log(f"Could not stop: {e}")

    def _poll(self):
        if not self.server:
            return
        try:
            p = dict(self.server.jobs.progress)
        except Exception:
            return
        running = bool(p.get("running"))
        self.prog.setVisible(running)
        if running:
            self.prog.setValue(int(1000 * float(p.get("frac") or 0)))
            self.prog.setFormat(f"{p.get('step', '')}  %p%".replace("%p%", f"{float(p.get('frac') or 0) * 100:.0f}%"))
        log = p.get("log") or []
        if len(log) > getattr(self, "_nlog", 0):
            for line in log[self._nlog:]:
                self.log.appendPlainText(str(line))
            self._nlog = len(log)
        elif len(log) < getattr(self, "_nlog", 0):
            self._nlog = len(log)
        if self._was_running and not running:
            err = p.get("error")
            self._log(f"Run failed: {err}" if err else "Run finished.")
            self._reload_sheet()
            if self.web is not None:
                self.web.reload()
        self._was_running = running

    def open_in_browser(self):
        if self.url:
            QDesktopServices.openUrl(QUrl(self.url))

    # ------------------------------------------------------------------ closing
    def _stop_server(self):
        self.timer.stop()
        self._nlog = 0
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:
                pass
            self.server = None

    def closeEvent(self, ev):
        self._stop_server()
        if self.web is not None:
            self.web.setUrl(QUrl("about:blank"))
        super().closeEvent(ev)
