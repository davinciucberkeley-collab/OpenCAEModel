"""Drone mode of the OpenCAE main window: parameter sheet on the left, the dashboard of the project on the right.

Opening a project starts a ``DashboardServer`` and shows its page in a QWebEngineView (or offers the system
browser when QtWebEngine is missing). Runs go through the server's job manager, so the status-bar progress and
the embedded dashboard show the same run.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                               QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QStackedWidget,
                               QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from opencae.ui import theme

try:  # optional: without it the dashboard opens in the system browser
    from PySide6.QtWebEngineWidgets import QWebEngineView
    HAVE_WEB = True
except Exception:  # pragma: no cover - depends on the install
    QWebEngineView = None
    HAVE_WEB = False

def web_available() -> bool:
    """QtWebEngine needs Qt.AA_ShareOpenGLContexts set before the QApplication exists (app/main.py does it);
    without it creating a view crashes the process, so the browser fallback is used instead."""
    if not HAVE_WEB:
        return False
    from PySide6.QtCore import QCoreApplication
    if hasattr(os, "geteuid") and os.geteuid() == 0 and "--no-sandbox" not in os.environ.get(
            "QTWEBENGINE_CHROMIUM_FLAGS", ""):  # Chromium refuses to start as root (containers, CI) without it
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "") +
                                                    " --no-sandbox").strip()
    return QCoreApplication.testAttribute(Qt.AA_ShareOpenGLContexts)


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


# ------------------------------------------------------------------------------------------------ workspace
class DroneWorkspace(QWidget):
    """The "Drone" mode of the main window. Emits ``progress(running, fraction, text)`` for the status bar and
    ``project_changed(path)`` ("" when closed)."""

    progress = Signal(bool, float, str)
    project_changed = Signal(str)
    message = Signal(str)

    def __init__(self, parent=None, start_server: bool = True):
        super().__init__(parent)
        self.project_dir: Optional[Path] = None
        self.server = None
        self.url = ""
        self.roles_text = ""
        self._start_server = start_server
        self._was_running = False
        self._nlog = 0
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._poll)
        self._enable(False)

    # ------------------------------------------------------------------ layout
    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Horizontal)
        root.addWidget(split)

        left = QWidget()
        v = QVBoxLayout(left)
        v.setContentsMargins(6, 6, 6, 6)
        self.title = QLabel("No drone project open")
        f = self.title.font()
        f.setBold(True)
        self.title.setFont(f)
        v.addWidget(self.title)
        self.sheet = ParameterSheet()
        v.addWidget(self.sheet, 1)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        v.addWidget(self.msg)
        row = QHBoxLayout()
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(lambda checked=False: self.save_parameters())
        self.save_run_btn = QPushButton("Save && run")
        self.save_run_btn.setObjectName("primary")
        self.save_run_btn.clicked.connect(lambda checked=False: self.run(None))
        row.addWidget(self.save_btn)
        row.addWidget(self.save_run_btn)
        v.addLayout(row)
        left.setMinimumWidth(340)
        split.addWidget(left)

        right = QSplitter(Qt.Vertical)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._placeholder())          # 0: no project
        self.web = None
        if web_available():
            self.web = QWebEngineView()
            self.pages.addWidget(self.web)                 # 1: dashboard
        else:
            self.pages.addWidget(self._fallback())
        right.addWidget(self.pages)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 9))
        self.log.setMaximumBlockCount(1000)
        right.addWidget(self.log)
        right.setStretchFactor(0, 5)
        right.setStretchFactor(1, 1)
        right.setSizes([700, 110])
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([420, 1000])

    def _placeholder(self) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        l.addStretch(1)
        lab = QLabel("Drone mode\n\nCreate a project from a CAD file, or open the Quad 250 example,\n"
                     "set the parameters on the left and run the analyses.")
        lab.setAlignment(Qt.AlignCenter)
        lab.setStyleSheet(f"color:{theme.FG_DIM}")
        l.addWidget(lab)
        row = QHBoxLayout()
        row.addStretch(1)
        b = QPushButton("New drone project…")
        b.setObjectName("primary")
        b.clicked.connect(lambda checked=False: self.new_project())
        row.addWidget(b)
        o = QPushButton("Open project…")
        o.clicked.connect(lambda checked=False: self.open_dialog())
        row.addWidget(o)
        ex = QToolButton()
        ex.setText("Open example")
        ex.setPopupMode(QToolButton.InstantPopup)
        m = QMenu(ex)
        for label, key in EXAMPLES.items():
            m.addAction(label).triggered.connect(lambda checked=False, k=key: self.open_example(k))
        ex.setMenu(m)
        row.addWidget(ex)
        row.addStretch(1)
        l.addLayout(row)
        l.addStretch(2)
        return w

    def _fallback(self) -> QWidget:
        fb = QWidget()
        fl = QVBoxLayout(fb)
        fl.addStretch(1)
        lab = QLabel("The built-in web view is not available in this installation.\n"
                     "The dashboard opens in your normal browser instead.")
        lab.setAlignment(Qt.AlignCenter)
        fl.addWidget(lab)
        self.browser_btn = QPushButton("Open in browser")
        self.browser_btn.setObjectName("primary")
        self.browser_btn.clicked.connect(lambda checked=False: self.open_in_browser())
        fl.addWidget(self.browser_btn, 0, Qt.AlignCenter)
        fl.addStretch(1)
        return fb

    def _enable(self, on: bool):
        for w in (self.save_btn, self.save_run_btn):
            w.setEnabled(on)

    def _log(self, text: str):
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")
        self.message.emit(text)

    def _error(self, text: str):
        self.msg.setStyleSheet(f"color:{theme.ERR}")
        self.msg.setText(text)

    def _ok(self, text: str):
        self.msg.setStyleSheet(f"color:{theme.OK}")
        self.msg.setText(text)

    # ------------------------------------------------------------------ project
    def open_project(self, project_dir, show_roles: bool = False) -> bool:
        project_dir = Path(project_dir)
        if not (project_dir / "drone.json").exists():
            self._error(f"No drone.json in {project_dir}")
            self.message.emit(f"Not a drone project: {project_dir}")
            return False
        self.close_project()
        self.project_dir = project_dir
        self.title.setText(f"Drone project: {project_dir.name}")
        self._reload_sheet()
        self._enable(True)
        self._log(f"Project opened: {project_dir}")
        if self._start_server:
            try:
                from opencae.web.server import DashboardServer
                self.server = DashboardServer(project_dir, port=0)
                self.url = self.server.start()
                self._log(f"Dashboard: {self.url}")
                if self.web is not None:
                    self.web.setUrl(QUrl(self.url))
                self.pages.setCurrentIndex(1)
                self.timer.start()
            except Exception as e:
                self.server = None
                self._error(f"Dashboard server not started: {e}")
                self._log(f"Could not start the dashboard server: {e}")
        self.project_changed.emit(str(project_dir))
        if show_roles:
            self.show_roles()
        return True

    def close_project(self):
        self._stop_server()
        self.project_dir = None
        self.url = ""
        self.sheet.clear()
        self.title.setText("No drone project open")
        self.msg.setText("")
        self._enable(False)
        self.pages.setCurrentIndex(0)
        if self.web is not None:
            self.web.setUrl(QUrl("about:blank"))
        self.progress.emit(False, 0.0, "")
        self.project_changed.emit("")

    def _reload_sheet(self):
        try:
            from opencae.drone import spec as S
            self.sheet.load(S.load(self.project_dir))
        except Exception as e:
            self._error(f"Could not read drone.json: {e}")

    def new_project(self):
        from opencae.ui.drone.new_dialog import NewDroneDialog
        dlg = NewDroneDialog(self)
        if dlg.exec() == QDialog.Accepted and dlg.project_dir:
            self.open_project(dlg.project_dir, show_roles=True)

    def open_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "Open drone project", str(projects_home()))
        if d:
            self.open_project(Path(d))

    def open_example(self, key: str) -> bool:
        try:
            return self.open_project(copy_example(key))
        except Exception as e:
            self._error(f"Could not open the example: {e}")
            return False

    def show_roles(self):
        """List the parts per role that were guessed from the part names, so the person can check them."""
        from opencae.ui.drone.new_dialog import roles_summary
        self.roles_text = roles_summary(self.project_dir)
        self._log("Roles guessed from the part names:\n" + self.roles_text)
        self._ok("Check the roles guessed from the part names (change them in drone.json):\n" + self.roles_text)

    # ------------------------------------------------------------------ parameters
    def save_parameters(self) -> bool:
        """Write edited values and evidence statuses to drone.json. On bad input: red message, file unchanged."""
        from opencae.drone import spec as S
        if self.project_dir is None:
            return False
        try:
            vals, stats = self.sheet.edits()
            cur = S.load(self.project_dir)
            new = S.apply_overrides(cur, vals) if vals else cur
            for k, st in stats.items():
                _set_status(new, k, st)
            problems = S.validate(new)
            if problems:
                raise ValueError("; ".join(problems))
            if vals or stats:
                S.save(new, self.project_dir / "drone.json")
        except (KeyError, ValueError, TypeError) as e:
            self._error("Not saved: " + (str(e.args[0]) if e.args else str(e)))
            return False
        self._ok("Saved." if (vals or stats) else "Nothing changed.")
        self._reload_sheet()
        if vals or stats:
            self._log(f"Parameters saved ({len(vals)} value(s), {len(stats)} status(es) changed)")
        return True

    # ------------------------------------------------------------------ runs
    def run(self, only: Optional[list] = None) -> bool:
        if not self.server:
            self._log("No dashboard server: cannot start a run.")
            return False
        if not self.save_parameters():
            return False
        try:
            self.server.run({"only": only} if only else {})
            self._log("Run started" + (f": {', '.join(only)}" if only else " (all analyses)"))
        except Exception as e:
            self._error(f"Could not start the run: {e}")
            self._log(f"Could not start the run: {e}")
            return False
        self._poll()
        return True

    def stop(self):
        if self.server:
            self.server.stop_job()
            self._log("Stop requested; the running step finishes first.")

    def _poll(self):
        if not self.server:
            return
        p = self.server.jobs.snapshot()
        running = bool(p.get("running"))
        frac = float(p.get("frac") or 0)
        self.progress.emit(running, frac, str(p.get("step", "")))
        log = p.get("log") or []
        if len(log) < self._nlog:
            self._nlog = 0
        for line in log[self._nlog:]:
            self.log.appendPlainText(str(line))
        self._nlog = len(log)
        if self._was_running and not running:
            err = p.get("error")
            self._log(f"Run stopped: {err}" if err else "Run finished.")
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
        self._was_running = False
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:
                pass
            self.server = None

    def shutdown(self):
        """Stop the server and drop the web view (called when the app closes)."""
        self._stop_server()
        if self.web is not None:
            self.web.setUrl(QUrl("about:blank"))

    def closeEvent(self, ev):
        self.shutdown()
        super().closeEvent(ev)
