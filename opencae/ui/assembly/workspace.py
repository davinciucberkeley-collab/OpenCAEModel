"""Assembly mode: the workspace widget (tree, 3-D view, playback, plots, inspectors) and the extension object that
adds the ribbon page and the stack page to the main window.

State is shared with the Studio: ``workspace.project`` IS ``main_window.session.project`` (never a copy). The CAD is
shown by the existing :class:`Viewport`; the animation only sets the user transform of the actors.
"""
from __future__ import annotations

import copy
import math
import os
import shutil
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pyvista as pv
from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox, QProgressBar,
                               QPushButton, QSplitter, QStackedWidget, QTabWidget, QToolBar, QVBoxLayout, QWidget)

from opencae.assembly import (GROUND, Component, Joint, RigidBody, get_mbd, set_mbd)
from opencae.assembly.massprops import _combine, update_body_masses
from opencae.mbd import backend
from opencae.mbd.results import MBDResult, pose_transforms, quat_to_matrix
from opencae.project.model import EntityRef
from opencae.ui import theme
from opencae.ui.assembly import dialogs as D
from opencae.ui.assembly.panels import AssemblyTree, PropertiesPanel, fnum, fvec
from opencae.ui.assembly.playback import PlaybackBar, ReadoutPanel
from opencae.ui.assembly.plots import PlotsPanel
from opencae.ui.viewport import Pick, Viewport
from opencae.utilities.units import get_unit_system

PAGE = "Motion"
JOBS_KEY = "opencae.ui.jobs"
INSTALL_HINT = "python -m pip install mujoco"
EXAMPLES = {"gripper": "Two-finger gripper", "drone_rotors": "Drone rotor assembly"}


def examples_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "examples" / "projects"


def projects_home() -> Path:
    return Path(os.environ.get("OPENCAE_HOME") or Path.home() / ".opencae") / "projects"


def copy_example(key: str) -> Path:
    """Copy a bundled example to ``~/.opencae/projects/<key>/`` once (the package folder is never written)."""
    src = examples_dir() / f"{key}.ocae"
    if not src.exists():
        raise FileNotFoundError(f"example not found: {src}")
    dst_dir = projects_home() / key
    dst = dst_dir / f"{key}.ocae"
    if not dst.exists():
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        data = examples_dir() / f"{key}_data"
        if data.exists():
            shutil.copytree(data, dst_dir / f"{key}_data", dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("results", "__pycache__"))
    return dst


class _Bridge(QObject):
    """Carries run progress from the worker thread to the GUI thread (queued signal)."""
    progress = Signal(float, str)


# ============================================================================================ overlay geometry
def _unit(v) -> np.ndarray:
    v = np.asarray(v, float)
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else np.array([0.0, 0.0, 1.0])


def arrow_mesh(origin, direction, length: float) -> pv.PolyData:
    return pv.Arrow(start=np.asarray(origin, float), direction=_unit(direction), scale=float(length),
                    tip_length=0.28, tip_radius=0.09, shaft_radius=0.03)


def arc_mesh(origin, R: np.ndarray, radius: float, sign: float = 1.0, sweep_deg: float = 270.0) -> pv.PolyData:
    """Circular arrow around the joint z axis (columns of ``R`` = joint x, y, z); ``sign`` > 0 turns right-handed."""
    o = np.asarray(origin, float)
    x, y = R[:, 0], R[:, 1]
    s = 1.0 if sign >= 0 else -1.0
    th = np.radians(np.linspace(20.0, 20.0 + sweep_deg, 48)) * s
    pts = o + radius * (np.outer(np.cos(th), x) + np.outer(np.sin(th), y))
    tube = pv.Spline(pts, 48).tube(radius=max(radius * 0.045, 1e-6))
    tangent = _unit(pts[-1] - pts[-3])
    head = pv.Cone(center=pts[-1] + tangent * radius * 0.12, direction=tangent, height=radius * 0.28,
                   radius=radius * 0.11, resolution=16)
    return tube.merge(head)


def circle_marker(origin, radius: float) -> pv.PolyData:
    return pv.Sphere(radius=radius, center=np.asarray(origin, float), theta_resolution=16, phi_resolution=16)


# ============================================================================================ study panel
def snapshot_for_run(project, study):
    """(project, study) for a motion worker: deep copies of the assembly and of the study, so the run never
    sees edits made while it runs and never writes the live objects. The other project fields are shared
    read-only (the run reads only the name, the unit system and the assembly)."""
    snap = copy.copy(project)
    snap.assembly = copy.deepcopy(project.assembly)
    return snap, copy.deepcopy(study)


# Largest coupling error the motion tests accept (test_mbd_gripper.py: constraint_violation_max < 1e-8); the
# stiffest MuJoCo coupling keeps positions on the ideal transmission to about 1e-9 (mbd.build.COUPLING_ACCURACY_NOTE).
COUPLING_TOL = 1e-8


def _esc(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def run_notes_html(meta: dict) -> str:
    """What a finished motion run reports beyond its curves: its warnings, the largest coupling-constraint error
    (with what it means and its tolerance) and, for rotors, where the thrust model came from."""
    parts = []
    warnings = list(meta.get("warnings") or [])
    if warnings:
        parts.append(f"<b style='color:{theme.WARN}'>Run warnings ({len(warnings)}):</b><br>• "
                     + "<br>• ".join(_esc(w) for w in warnings))
    else:
        parts.append("<b>Run warnings:</b> none.")
    joints = meta.get("joints") or []
    if any(j.get("coupling_follower_of") for j in joints):
        v = float(meta.get("constraint_violation_max") or 0.0)
        prism = any(j.get("coupling_follower_of") and j.get("kind") == "prismatic" for j in joints)
        unit = "rad (m for a prismatic joint)" if prism else "rad"
        ok = v <= COUPLING_TOL
        verdict = (f"<span style='color:{theme.OK}'>within the tolerance of {COUPLING_TOL:.0e}</span>: the "
                   "transmission behaved as an ideal one." if ok else
                   f"<span style='color:{theme.ERR}'>above the tolerance of {COUPLING_TOL:.0e}</span>: the "
                   "follower did not track the transmission exactly; reduce the time step and run again.")
        parts.append(f"<b>Largest coupling error:</b> {v:.2e} {unit}. This is how far a coupled (follower) joint "
                     f"strayed from ratio × leader at any time of the run; 0 would be a perfect transmission. "
                     f"It is {verdict}")
    thrust = meta.get("thrust_models") or []
    if thrust:
        names = {j.get("id"): j.get("name") or j.get("id") for j in joints}
        rows = []
        for t in thrust:
            src = (t.get("source") or "prescribed").lower()
            how = ("prescribed by the user (coefficients typed in, not computed by CFD)" if src == "prescribed"
                   else "from a CFD analysis" if src == "cfd" else _esc(src))
            rows.append(f"{_esc(names.get(t.get('rotor_joint'), t.get('rotor_joint') or 'rotor'))}: thrust {how}")
        parts.append("<b>Rotor thrust:</b><br>• " + "<br>• ".join(rows) + "<br>The thrust models are stored only. "
                     "This run spins the rotors as commanded; it does not compute thrust and does not predict flight.")
    return "<br><br>".join(parts)


class StudyPanel(QWidget):
    """Choose the study, see whether it can run (backend, model check), run it, hand a load to the structure."""

    def __init__(self, ws: "AssemblyWorkspace") -> None:
        super().__init__()
        self.ws = ws
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        title = QLabel("<b style='font-size:14px'>Motion study</b>")
        lay.addWidget(title)
        row = QHBoxLayout()
        self.combo = QComboBox()
        self.combo.currentIndexChanged.connect(self._combo)
        row.addWidget(self.combo, 1)
        self.settings_btn = QPushButton("Settings…")
        self.settings_btn.setToolTip("Gravity, duration, time step and the starting state of the joints")
        self.settings_btn.clicked.connect(lambda: ws.edit_study_settings())
        row.addWidget(self.settings_btn)
        lay.addLayout(row)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        self.backend_label = QLabel("")
        self.backend_label.setWordWrap(True)
        self.backend_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.backend_label)
        self.problems = QLabel("")
        self.problems.setObjectName("error")
        self.problems.setWordWrap(True)
        lay.addWidget(self.problems)
        self.run_btn = QPushButton("Run study")
        self.run_btn.setObjectName("primary")
        self.run_btn.setToolTip("Simulate the motion. Progress appears in the Progress Monitor on the left.")
        self.run_btn.clicked.connect(lambda: ws.run_study())
        lay.addWidget(self.run_btn)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setVisible(False)
        lay.addWidget(self.progress)
        self.status = QLabel("")
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.load_btn = QPushButton("Create structural load case…")
        self.load_btn.clicked.connect(lambda: ws.open_load_case())
        lay.addWidget(self.load_btn)
        self.load_hint = QLabel("")
        self.load_hint.setObjectName("hint")
        self.load_hint.setWordWrap(True)
        lay.addWidget(self.load_hint)
        self.run_notes = QLabel("")  # warnings, coupling error and rotor thrust of the shown run
        self.run_notes.setObjectName("hint")
        self.run_notes.setWordWrap(True)
        self.run_notes.setTextFormat(Qt.RichText)
        lay.addWidget(self.run_notes)
        self.assumptions = QLabel("")
        self.assumptions.setObjectName("hint")
        self.assumptions.setWordWrap(True)
        lay.addWidget(self.assumptions)
        lay.addStretch(1)

    def _combo(self, _i: int) -> None:
        sid = self.combo.currentData()
        if sid and sid != self.ws.study_id:
            self.ws.set_study(sid)

    def refresh(self) -> None:
        ws = self.ws
        self.combo.blockSignals(True)
        self.combo.clear()
        for st in ws.mbd_studies():
            self.combo.addItem(st.name + ("  ✓" if st.results.completed else ""), st.id)
        if ws.study is not None:
            self.combo.setCurrentIndex(max(0, self.combo.findData(ws.study.id)))
        self.combo.blockSignals(False)
        ok, why = backend.available()
        if ok:
            self.backend_label.setText(f"<span style='color:{theme.OK}'>Motion solver ready: MuJoCo "
                                       f"{backend.version()}</span>")
        else:
            reason = why.split("; install")[0].strip()
            self.backend_label.setText(f"<span style='color:{theme.ERR}'>{reason}</span><br>MuJoCo is the motion "
                                       f"solver. Install it yourself with:<br><b>{INSTALL_HINT}</b><br>then restart "
                                       "OpenCAE Studio. Nothing is installed automatically.")
        st = ws.study
        if st is None:
            self.summary.setText("This project has no motion study. Open one of the examples to try the workflow.")
            self.problems.setText("")
        else:
            S = get_mbd(st)
            g = np.asarray(S.gravity, float)
            n = int(round(S.duration_s / S.dt_s)) if S.dt_s > 0 else 0
            self.summary.setText(f"Gravity: {'off' if np.allclose(g, 0) else fvec(g) + ' m/s²'} — "
                                 f"duration {S.duration_s:g} s, step {S.dt_s:g} s ({n:,} steps), "
                                 f"{len(S.drivers)} motor(s)")
            probs = ws.study_problems()
            self.problems.setText("Fix before running:\n• " + "\n• ".join(probs) if probs else "")
        self.run_btn.setEnabled(ws.can_run() and not ws.study_problems())
        self.settings_btn.setEnabled(st is not None and not ws.running)
        can, why_not = ws.load_case_status()
        self.load_btn.setEnabled(can)
        self.load_hint.setText("" if can else why_not)
        res = ws.result
        if res is not None:
            self.run_notes.setText(run_notes_html(res.meta))
            self.assumptions.setText("Assumptions of this run:\n• " + "\n• ".join(res.meta.get("assumptions", [])))
        else:
            self.run_notes.setText("")
            self.assumptions.setText("")


# ============================================================================================ workspace
class AssemblyWorkspace(QWidget):
    selection_changed = Signal(str, str)
    changed = Signal()

    def __init__(self, mw, ext=None) -> None:
        super().__init__()
        self.mw = mw
        self.ext = ext
        self._project = None
        self.study_id: Optional[str] = None
        self.result: Optional[MBDResult] = None
        self.result_stale = False
        self.sample = 0
        self.selected: Optional[tuple[str, str]] = ("assembly", "")
        self.running = False
        self._run_ctx: Optional[dict] = None  # the live project of the active run, and whether it was edited
        self.preview_state: Optional[dict] = None
        self.last_pick = None
        self.show_joint_overlay = True
        self._pick_kind = "body"
        self._pick_consumer: Optional[tuple[str, Callable]] = None
        self._dialogs: list = []
        self._overlay: dict[str, list[str]] = {}
        self._gap_cache: Optional[tuple] = None
        self._tips_cache: Optional[tuple] = None
        self._bridge = _Bridge()
        self._bridge.progress.connect(self._on_progress)
        self._build()
        mw.installEventFilter(self)
        self._shortcuts = []
        for w in (self.viewport, self.tree, self.playback):  # Space plays / pauses (not inside dialogs or text fields)
            sc = QShortcut(QKeySequence(Qt.Key_Space), w)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(lambda: self.playback.toggle())
            self._shortcuts.append(sc)

    # ---- lifetime --------------------------------------------------------------
    def eventFilter(self, obj, ev) -> bool:
        if ev.type() == QEvent.Close and obj is self.mw:
            self.playback.pause()
            try:
                self.viewport.close_plotter()
            except Exception:  # noqa: BLE001
                pass
        return False

    # ---- shared state ----------------------------------------------------------
    @property
    def session(self):
        return self.mw.session

    @property
    def project(self):
        return self.mw.session.project

    @property
    def assembly(self):
        return self.mw.session.project.assembly

    @property
    def length_unit(self) -> str:
        return get_unit_system(self.project.unit_system).length

    @property
    def study(self):
        return self.project.study_by_id(self.study_id) if self.study_id else None

    def mbd_studies(self) -> list:
        return [s for s in self.project.studies if s.analysis.analysis_type == "mbd"]

    @property
    def current_time(self) -> float:
        if self.result is None:
            return 0.0
        return float(self.result.t[min(max(self.sample, 0), len(self.result.t) - 1)])

    # ---- UI ----------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # top bar
        bar = QFrame()
        bar.setObjectName("asm_topbar")
        bar.setStyleSheet(f"QFrame#asm_topbar {{ background:{theme.BG_2}; border-bottom:1px solid {theme.BORDER}; }}")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(8, 5, 8, 5)
        self.btn_gripper = QPushButton("Open gripper example")
        self.btn_drone = QPushButton("Open drone rotors example")
        self.btn_open = QPushButton("Open project…")
        self.btn_gripper.setToolTip("A palm with two hinged fingers, one motor and an ideal transmission")
        self.btn_drone.setToolTip("A frame with four motor housings and four spinning rotors")
        self.btn_gripper.clicked.connect(lambda: self.open_example("gripper"))
        self.btn_drone.clicked.connect(lambda: self.open_example("drone_rotors"))
        self.btn_open.clicked.connect(lambda: self.open_project())
        for b in (self.btn_gripper, self.btn_drone, self.btn_open):
            bl.addWidget(b)
        bl.addStretch(1)
        self.top_study = QLabel("")
        self.top_study.setObjectName("hint")
        bl.addWidget(self.top_study)
        self.top_run = QPushButton("Run")
        self.top_run.setObjectName("primary")
        self.top_run.clicked.connect(lambda: self.run_study())
        bl.addWidget(self.top_run)
        outer.addWidget(bar)
        self.pages = QStackedWidget()
        outer.addWidget(self.pages, 1)

        # welcome page
        self.welcome = QWidget()
        wl = QVBoxLayout(self.welcome)
        wl.addStretch(1)
        h = QLabel("<div style='font-size:22px;font-weight:600'>Assembly motion</div>")
        h.setAlignment(Qt.AlignCenter)
        wl.addWidget(h)
        self.welcome_text = QLabel("Define how the parts of a mechanism connect, drive a joint with a motor, "
                                   "watch the motion and hand the loads to a structural study.<br><br>"
                                   "Start with an example, open a project, or build an assembly from the parts "
                                   "of the project that is open now.")
        self.welcome_text.setWordWrap(True)
        self.welcome_text.setAlignment(Qt.AlignCenter)
        self.welcome_text.setObjectName("hint")
        self.welcome_text.setMaximumWidth(640)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.welcome_text)
        row.addStretch(1)
        wl.addLayout(row)
        crow = QHBoxLayout()
        crow.addStretch(1)
        self.create_btn = QPushButton("Create an assembly from this project's parts")
        self.create_btn.clicked.connect(lambda: self.create_assembly_from_parts())
        crow.addWidget(self.create_btn)
        crow.addStretch(1)
        wl.addLayout(crow)
        wl.addStretch(2)
        self.pages.addWidget(self.welcome)

        # main page
        self.main = QSplitter(Qt.Horizontal)
        self.pages.addWidget(self.main)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(4, 4, 4, 4)
        self.tree = AssemblyTree()
        self.tree.selected.connect(lambda k, i: self.select(k, i, from_tree=True))
        self.tree.context_requested.connect(self._context_menu)
        ll.addWidget(self.tree, 1)
        self.hint_label = QLabel("")
        self.hint_label.setObjectName("hint")
        self.hint_label.setWordWrap(True)
        ll.addWidget(self.hint_label)
        brow = QHBoxLayout()
        for text, slot, tip in (("Add joint…", lambda: self.add_joint(), "Connect two bodies"),
                                ("Add motor…", lambda: self.add_driver(), "Command a joint")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, s=slot: s())
            brow.addWidget(b)
        ll.addLayout(brow)
        self.main.addWidget(left)

        centre = QSplitter(Qt.Vertical)
        vp_box = QWidget()
        vl = QVBoxLayout(vp_box)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)
        tools = QHBoxLayout()
        tools.setContentsMargins(6, 3, 6, 3)
        tools.addWidget(QLabel("Click selects:"))
        self.pick_buttons = {}
        for key, text, tip in (("body", "Parts", "Click selects a whole part"), ("face", "Faces", "Click selects a face"),
                               ("edge", "Edges", "Click selects an edge (circular edges can set a joint axis)")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(key == "body")
            b.clicked.connect(lambda _=False, k=key: self.set_pick_kind(k))
            tools.addWidget(b)
            self.pick_buttons[key] = b
        tools.addSpacing(14)
        for name, text in (("iso", "Iso"), ("front", "Front"), ("top", "Top")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, n=name: self.viewport.set_view(n))
            tools.addWidget(b)
        fit = QPushButton("Fit")
        fit.clicked.connect(lambda: self.viewport.fit())
        tools.addWidget(fit)
        from PySide6.QtWidgets import QCheckBox
        self.joint_check = QCheckBox("Show joints")
        self.joint_check.setChecked(True)
        self.joint_check.toggled.connect(self._toggle_overlay)
        tools.addWidget(self.joint_check)
        tools.addStretch(1)
        vl.addLayout(tools)
        self.viewport = Viewport(self)
        self.viewport.picked.connect(self.on_viewport_pick)
        vl.addWidget(self.viewport, 1)
        centre.addWidget(vp_box)
        lower = QWidget()
        lw = QVBoxLayout(lower)
        lw.setContentsMargins(0, 0, 0, 0)
        lw.setSpacing(0)
        self.playback = PlaybackBar(self)
        lw.addWidget(self.playback)
        self.plots = PlotsPanel(self)
        lw.addWidget(self.plots, 1)
        centre.addWidget(lower)
        centre.setStretchFactor(0, 3)
        centre.setStretchFactor(1, 2)
        centre.setSizes([480, 360])
        self.main.addWidget(centre)

        self.tabs = QTabWidget()
        self.props = PropertiesPanel(self)
        self.readout = ReadoutPanel(self)
        self.study_panel = StudyPanel(self)
        self.tabs.addTab(self.props, "Properties")
        self.tabs.addTab(self.readout, "Readout")
        self.tabs.addTab(self.study_panel, "Study")
        self.main.addWidget(self.tabs)
        self.main.setStretchFactor(0, 0)
        self.main.setStretchFactor(1, 1)
        self.main.setStretchFactor(2, 0)
        self.main.setSizes([300, 860, 400])
        self.pages.setCurrentWidget(self.welcome)

    def welcome_visible(self) -> bool:
        return self.pages.currentWidget() is self.welcome

    # ---- open / sync ------------------------------------------------------------
    def enter(self) -> None:
        if self.ext is not None:
            self.ext.show_mode()

    def open_example(self, key: str) -> bool:
        try:
            path = copy_example(key)
        except Exception as exc:  # noqa: BLE001
            self.warn("Example not found", str(exc))
            return False
        return self.open_path(path)

    def open_project(self) -> bool:
        before = self.session.path
        self.mw.cmd_open()
        self.enter()
        self.sync(fit=True)
        return self.session.path != before

    def open_path(self, path) -> bool:
        before = self.project
        self.mw.cmd_open(str(path))
        if self.project is before and (self.session.path is None or Path(self.session.path) != Path(path)):
            return False  # the open failed or was cancelled
        self.enter()
        self.sync(fit=True)
        return True

    def sync(self, fit: bool = False) -> None:
        """Bring the workspace in line with the project (call when the mode is entered or the project changed)."""
        p = self.project
        changed_project = p is not self._project
        if changed_project:
            self._project = p
            self.result = None
            self.result_stale = False
            self.sample = 0
            self._gap_cache = self._tips_cache = None
            self.selected = ("assembly", "")
            self.study_id = None
        ids = [s.id for s in self.mbd_studies()]
        if self.study_id not in ids:
            self.study_id = p.active_study if p.active_study in ids else (ids[0] if ids else None)
            changed_project = True
        if changed_project:
            self._load_result()
        empty = self.assembly.is_empty()
        self.pages.setCurrentWidget(self.welcome if empty else self.main)
        self.create_btn.setVisible(empty and any(b.kind == "solid" for b in self.session.bodies.values()))
        self.tree.rebuild(self)
        self._refresh_panels()
        if not empty:
            self.refresh_scene(fit=fit)
            self.tree.select_item(*(self.selected or ("assembly", "")), emit=False)

    def _refresh_panels(self) -> None:
        self.props.refresh()
        self.study_panel.refresh()
        st = self.study
        self.top_study.setText(f"Study: {st.name}" if st else "no motion study")
        self.top_run.setEnabled(self.can_run() and not self.study_problems())
        if self.result is not None and self.playback.slider.maximum() != len(self.result.t) - 1:
            self.playback.configure(self.result, self.sample)
        self.plots.refresh()
        self.readout.update_for(self.sample)
        self.plots.set_cursor(self.current_time)

    def _load_result(self) -> None:
        self.result = None
        self.result_stale = False
        self.sample = 0
        self._gap_cache = None
        st = self.study
        if st is not None and st.results.completed and st.results.directory:
            try:
                self.result = MBDResult.load(self.session.data_dir / st.results.directory)
            except Exception as exc:  # noqa: BLE001 - say so, never fake a result
                self.hint_label.setText(f"Could not load the saved results: {exc}")
        if self.result is not None:
            self.playback.configure(self.result)
        else:
            self.playback.set_enabled(False)
            self.readout.clear()

    def set_study(self, study_id: str) -> None:
        if study_id == self.study_id or self.project.study_by_id(study_id) is None:
            return
        self.study_id = study_id
        self._load_result()
        self.tree.rebuild(self)
        self._refresh_panels()
        self.refresh_scene()

    def create_assembly_from_parts(self) -> bool:
        """One component and one rigid body per solid of the open project (no joints yet)."""
        asm = self.assembly
        n = 0
        for gid, body in self.session.bodies.items():
            if body.kind != "solid":
                continue
            c = Component(name=body.name, body_id=gid, source="from the project geometry")
            asm.components.append(c)
            asm.bodies.append(RigidBody(name=body.name, component_ids=[c.id]))
            n += 1
        if not n:
            self.warn("Nothing to add", "The project has no solid bodies.")
            return False
        try:
            update_body_masses(self.project, bodies=self.session.bodies)
        except Exception as exc:  # noqa: BLE001
            self.hint_label.setText(f"Masses could not be computed: {exc}")
        self.mw.dirty = True
        self.sync(fit=True)
        return True

    def model_problems(self) -> list[str]:
        asm = self.assembly
        probs = asm.validate()
        if asm.bodies and not any(b.grounded for b in asm.bodies) and not any(j.parent == GROUND for j in asm.joints):
            probs.append("Nothing is grounded: ground one body (tick “Ground” on its card) so that the "
                         "mechanism does not float freely.")
        return probs

    def study_problems(self) -> list[str]:
        st = self.study
        if st is None:
            return []
        try:
            from opencae.mbd import validate_mbd
            return validate_mbd(self.project, st)
        except Exception as exc:  # noqa: BLE001
            return [str(exc)]

    def warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text, QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes

    # ---- scene ---------------------------------------------------------------------
    def refresh_scene(self, fit: bool = False) -> None:
        s = self.session
        order = list(s.regen.order) if getattr(s, "regen", None) is not None else list(s.bodies)
        self.viewport.show_geometry(s.bodies, order, set())
        self.viewport.set_pick_kind(self._pick_kind)
        self._draw_overlays()
        self._highlight()
        self._apply_pose()
        if fit:
            self.viewport.set_view("iso")
            self.viewport.fit()

    def set_pick_kind(self, kind: str) -> None:
        self._pick_kind = kind
        for k, b in self.pick_buttons.items():
            b.setChecked(k == kind)
        self.viewport.set_pick_kind(kind)

    def _toggle_overlay(self, on: bool) -> None:
        self.show_joint_overlay = on
        self._draw_overlays()
        self._apply_pose()

    # ---- overlay: joint axes and spin arrows ------------------------------------------------
    def _driver_sign(self, joint) -> Optional[float]:
        d = self.driver_of(joint.id)
        ratio = 1.0
        if d is None:  # a coupled follower turns with its leader's motor, times the ratio
            c = next((c for c in self.assembly.couplings if c.follower == joint.id), None)
            d = self.driver_of(c.leader) if c is not None else None
            ratio = c.ratio if c is not None else 1.0
        if d is None:
            return None
        ts, vs = d.profile.breakpoints()
        v = float(vs[int(np.argmax(np.abs(vs)))]) if len(vs) else 0.0
        return (1.0 if v >= 0 else -1.0) * (1.0 if ratio >= 0 else -1.0)

    def _child_extent(self, joint) -> float:
        rb = self.assembly.body(joint.child)
        size = self.viewport.model_size or 100.0
        r = 0.0
        if rb is not None:
            o, R = joint.frame()
            z = R[:, 2]
            for cid in rb.component_ids:
                c = self.assembly.component(cid)
                gb = self.session.bodies.get(c.body_id) if c else None
                if gb is None:
                    continue
                lo, hi = gb.bbox
                corners = np.array([[x, y, zc] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for zc in (lo[2], hi[2])])
                P = c.placement_matrix()
                corners = (P[:3, :3] @ corners.T).T + P[:3, 3]
                d = corners - o
                d = d - np.outer(d @ z, z)
                r = max(r, float(np.linalg.norm(d, axis=1).max()))
        return r if r > 0 else 0.15 * size

    def _draw_overlays(self) -> None:
        vp = self.viewport
        vp.clear_group("asm_joints")
        self._overlay = {}
        if not self.show_joint_overlay or self.assembly.is_empty():
            vp.plotter.render()
            return
        size = vp.model_size or 100.0
        L = max(0.16 * size, 1e-3)
        for j in self.assembly.joints:
            names = []
            o, R = j.frame()
            if j.kind == "fixed":
                n = f"asm_joint_{j.id}_marker"
                vp._add("asm_joints", n, circle_marker(o, 0.025 * size), color="#9aa3b2", pickable=False)
                names.append(n)
            else:
                z = R[:, 2]
                col = theme.ACCENT if j.kind == "revolute" else theme.OK
                n = f"asm_joint_{j.id}_axis"
                vp._add("asm_joints", n, arrow_mesh(o, z, L), color=col, pickable=False)
                names.append(n)
                if j.kind == "prismatic":
                    n2 = f"asm_joint_{j.id}_axis2"
                    vp._add("asm_joints", n2, arrow_mesh(o, -z, 0.6 * L), color=col, pickable=False)
                    names.append(n2)
                else:
                    sgn = self._driver_sign(j)
                    if sgn is not None:  # spin / motion direction of the motor
                        n3 = f"asm_joint_{j.id}_arc"
                        ext = self._child_extent(j)
                        r = 1.1 * ext if ext <= 0.25 * size else 0.25 * size  # round parts: just outside the rim
                        r = max(r, 0.04 * size)
                        vp._add("asm_joints", n3, arc_mesh(o + 0.05 * r * R[:, 2], R, r, sgn), color=theme.ACCENT_2,
                                pickable=False)
                        names.append(n3)
            self._overlay[j.id] = names
        vp.plotter.render()

    def preview_joint(self, j: Optional[Joint]) -> None:
        """Axis arrow (and the turning arc / second arrow) of a joint that is still being edited."""
        vp = self.viewport
        vp.clear_group("asm_preview")
        if j is None:
            self.preview_state = None
            if self._pick_consumer is None:
                self.hint_label.setText("")
            vp.plotter.render()
            return
        self.hint_label.setText("Preview: the arrow is the joint axis; the arc shows the positive turning direction."
                                if j.kind == "revolute" else "Preview: the arrow is the joint axis."
                                if j.kind == "prismatic" else "Preview: a fixed joint.")
        if self.result is not None and self.sample != 0:
            self.playback.set_time(0.0)  # the preview is drawn in the reference pose
        size = vp.model_size or 100.0
        L = max(0.3 * size, 1e-3)
        o = np.asarray(j.origin, float)
        z = j.z_axis() if j.kind != "fixed" else np.array([0.0, 0.0, 1.0])
        self.preview_state = {"origin": o.tolist(), "axis": z.tolist(), "kind": j.kind}
        if j.kind == "fixed":
            vp._add("asm_preview", "asm_preview_marker", circle_marker(o, 0.03 * size), color=theme.ACCENT_2,
                    pickable=False)
        elif np.linalg.norm(z) > 1e-12:
            vp._add("asm_preview", "asm_preview_axis", arrow_mesh(o, z, L), color=theme.ACCENT_2, pickable=False)
            if j.kind == "prismatic":
                vp._add("asm_preview", "asm_preview_axis2", arrow_mesh(o, -z, L), color=theme.ACCENT_2,
                        pickable=False)
            else:
                _, R = j.frame()
                vp._add("asm_preview", "asm_preview_arc", arc_mesh(o, R, 0.45 * L, 1.0), color="#ffd27a",
                        pickable=False)
        vp.plotter.render()

    # ---- poses -------------------------------------------------------------------------------
    def _result_fits(self) -> bool:
        res = self.result
        if res is None:
            return False
        comps = res.meta.get("components", {})
        return all(c.id in comps for c in self.assembly.components if self.assembly.body_of_component(c.id))

    def component_matrices(self, i: Optional[int]) -> dict:
        asm = self.assembly
        mats = {c.id: c.placement_matrix() for c in asm.components}
        if i is not None and self._result_fits():
            mats.update(pose_transforms(self.result, i))
        return mats

    def body_motion(self, bid: str, i: Optional[int]) -> np.ndarray:
        res = self.result
        T = np.eye(4)
        if res is None or i is None or bid not in res.body_quat:
            return T
        com0 = next((np.asarray(b["com0"], float) for b in res.meta["bodies"] if b["id"] == bid), np.zeros(3))
        R = quat_to_matrix(res.body_quat[bid][i])
        T[:3, :3] = R
        T[:3, 3] = res.body_pos[bid][i] - R @ com0
        return T

    def _apply_pose(self) -> None:
        vp = self.viewport
        i = self.sample if (self.result is not None and self._result_fits()) else None
        mats = self.component_matrices(i)
        names = list(vp._groups.get("geometry", [])) + list(vp._groups.get("highlight", []))
        for c in self.assembly.components:
            M = mats.get(c.id)
            if M is None:
                continue
            for n in names:
                if c.body_id in n:
                    a = vp.plotter.actors.get(n)
                    if a is not None:
                        a.user_matrix = M
        for j in self.assembly.joints:
            M = np.eye(4) if j.parent == GROUND else self.body_motion(j.parent, i)
            for n in self._overlay.get(j.id, []):
                a = vp.plotter.actors.get(n)
                if a is not None:
                    a.user_matrix = M
        vp.plotter.render()

    def set_sample(self, i: int) -> None:
        if self.result is None:
            return
        self.sample = int(min(max(i, 0), len(self.result.t) - 1))
        self._apply_pose()
        self.readout.update_for(self.sample)
        self.plots.set_cursor(self.current_time)
        self.playback.set_label(self.current_time, float(self.result.t[-1]))

    # ---- selection ----------------------------------------------------------------------------
    def geometry_id_of_body(self, bid: str) -> Optional[str]:
        rb = self.assembly.body(bid)
        if rb is None or not rb.component_ids:
            return None
        c = self.assembly.component(rb.component_ids[0])
        return c.body_id if c else None

    def _geometry_ids_for(self, sel) -> list[str]:
        if not sel:
            return []
        kind, ident = sel
        asm = self.assembly

        def of_body(bid):
            rb = asm.body(bid)
            return [asm.component(c).body_id for c in (rb.component_ids if rb else []) if asm.component(c)]
        if kind == "body":
            return of_body(ident)
        if kind == "component":
            c = asm.component(ident)
            return [c.body_id] if c else []
        if kind == "joint":
            j = asm.joint(ident)
            return of_body(j.child) if j else []
        if kind == "driver":
            d = self.driver_by_id(ident)
            j = asm.joint(d.joint) if d else None
            return of_body(j.child) if j else []
        if kind == "load":
            S = get_mbd(self.study) if self.study else None
            ld = next((x for x in (S.loads if S else []) if x.id == ident), None)
            return of_body(ld.body) if ld else []
        if kind == "coupling":
            c = next((x for x in asm.couplings if x.id == ident), None)
            out = []
            for jid in ((c.leader, c.follower) if c else ()):
                j = asm.joint(jid)
                out += of_body(j.child) if j else []
            return out
        return []

    def _highlight(self) -> None:
        gids = list(dict.fromkeys(self._geometry_ids_for(self.selected)))
        if gids:
            self.viewport.highlight([Pick("body", g) for g in gids], self.session.bodies)
        else:
            self.viewport.clear_group("highlight")
            self.viewport.plotter.render()

    def highlighted_geometry_ids(self) -> list[str]:
        return [n[len("hl_body_"):] for n in self.viewport._groups.get("highlight", []) if n.startswith("hl_body_")]

    def select(self, kind: str, ident: str, from_tree: bool = False) -> None:
        self.selected = (kind, ident)
        if not from_tree:
            if not self.tree.select_item(kind, ident, emit=False):
                self.tree.clear_selection_quiet()
        self.props.show_item(kind, ident)
        self._highlight()
        self._apply_pose()
        self.readout.update_for(self.sample)
        if self.tabs.currentWidget() is self.study_panel:
            self.tabs.setCurrentWidget(self.props)
        self.selection_changed.emit(kind, ident)

    def on_viewport_pick(self, pick) -> None:
        if self._pick_consumer is not None:
            kind, cb = self._pick_consumer
            if pick is not None and pick.kind == kind:
                cb(pick)
            return
        if pick is None:
            self.select("assembly", "")
            return
        c = next((c for c in self.assembly.components if c.body_id == pick.body), None)
        if c is None:
            self.hint_label.setText("That part is not in the assembly.")
            return
        self.last_pick = pick
        rb = self.assembly.body_of_component(c.id)
        if rb is None:
            self.select("component", c.id)
        elif len(rb.component_ids) > 1:
            self.select("component", c.id)
        else:
            self.select("body", rb.id)

    def begin_pick(self, kind: str, callback: Callable) -> None:
        """The next pick of ``kind`` goes to ``callback`` instead of changing the selection."""
        self._pick_consumer = (kind, callback)
        self.viewport.set_pick_kind(kind)
        self.hint_label.setText(f"Click a {'circular edge' if kind == 'edge' else kind} in the 3-D view…")

    def end_pick(self) -> None:
        if self._pick_consumer is not None:
            self._pick_consumer = None
            self.viewport.set_pick_kind(self._pick_kind)
            self.hint_label.setText("")

    def circle_of_edge(self, pick):
        """(centre, axis, radius) of a circular edge in the ASSEMBLY frame, or None."""
        if pick is None or pick.kind != "edge":
            return None
        body = self.session.bodies.get(pick.body)
        if body is None or body.tessellation is None or not 0 <= pick.index < len(body.tessellation.edge_polylines):
            return None
        circ = D.circle_from_polyline(body.tessellation.edge_polylines[pick.index])
        if circ is None:
            return None
        centre, normal, r = circ
        c = next((c for c in self.assembly.components if c.body_id == pick.body), None)
        if c is not None:
            P = c.placement_matrix()
            centre = P[:3, :3] @ centre + P[:3, 3]
            normal = P[:3, :3] @ normal
        return np.asarray(centre), _unit(normal), r

    def _context_menu(self, kind: str, ident: str, pos) -> None:
        m = QMenu(self)
        self.select(kind, ident)
        if kind == "body":
            m.addAction("Set manual values…", lambda: self.set_manual_mass(ident))
            m.addAction("Group with…", lambda: self.group_with(ident))
            rb = self.assembly.body(ident)
            m.addAction("Unground" if rb and rb.grounded else "Ground", lambda: self._toggle_ground(ident))
            m.addAction("Add joint…", lambda: self.add_joint(child_id=ident))
        elif kind == "joint":
            m.addAction("Edit…", lambda: self.edit_joint(ident))
            m.addAction("Add motor…", lambda: self.add_driver(ident))
            m.addAction("Delete", lambda: self.delete_joint(ident))
        elif kind == "driver":
            m.addAction("Edit…", lambda: self.edit_driver(ident))
            m.addAction("Delete", lambda: self.delete_driver(ident))
        elif kind == "study":
            m.addAction("Make current", lambda: self.set_study(ident))
            m.addAction("Settings…", lambda: self.edit_study_settings(ident))
        elif kind == "joints":
            m.addAction("Add joint…", lambda: self.add_joint())
        elif kind == "drivers":
            m.addAction("Add motor…", lambda: self.add_driver())
        if m.actions():
            m.exec(pos)

    # ---- editing: common ------------------------------------------------------------------------
    def mark_changed(self, scene: bool = False) -> None:
        self.mw.dirty = True
        if self.running and self._run_ctx is not None:
            self._run_ctx["edited"] = True
        if self.result is not None:
            self.result_stale = True
        self.tree.rebuild(self)
        self._refresh_panels()
        if scene:
            self.refresh_scene()
        self.changed.emit()

    def refuse_while_running(self, what: str) -> bool:
        """True (and a note in the hint line) when ``what`` would edit the model of a motion run in progress.
        The run works on a snapshot, so nothing breaks; refusing keeps the result and the model in step."""
        if not self.running:
            return False
        msg = (f"A motion run is in progress: {what} is available again when it ends (the run uses the joints, "
               "motors and settings as they were when Run was pressed).")
        self.hint_label.setText(msg)
        self.mw.statusBar().showMessage(msg, 6000)
        return True

    def _keep(self, dlg):
        self._dialogs.append(dlg)
        dlg.finished.connect(lambda _r, d=dlg: self._dialogs.remove(d) if d in self._dialogs else None)
        return dlg

    def body_com(self, bid: Optional[str]):
        rb = self.assembly.body(bid) if bid else None
        mp = rb.effective_mass() if rb else None
        return list(mp.com) if mp is not None else None

    # ---- bodies ------------------------------------------------------------------------------------
    def mass_source_text(self, b) -> str:
        mp = b.effective_mass()
        if mp is None:
            return "none"
        if b.manual is not None:
            return "manual values" + (f" ({b.manual.note})" if b.manual.note else "") + " — they replace the CAD values"
        comps = [self.assembly.component(c) for c in b.component_ids]
        names = []
        for c in comps:
            m = self.project.material_for_body(c.body_id, self.study) if c else None
            if m is not None and m.name not in names:
                names.append(m.name)
        if mp.source == "manual":
            return "manual"
        rho = f"density {mp.density_kg_m3:g} kg/m³" if mp.density_kg_m3 else "per-component densities"
        return f"from CAD: material {', '.join(names) if names else 'unknown'}, {rho}"

    def set_grounded(self, bid: str, on: bool) -> tuple[bool, str]:
        rb = self.assembly.body(bid)
        if rb is None:
            return False, "no such body"
        old = rb.grounded
        rb.grounded = bool(on)
        bad = [p for p in self.assembly.validate() if f"'{rb.name}'" in p and "is grounded" in p]
        if bad:
            rb.grounded = old
            return False, bad[0] + ". Remove the joint first, or leave the body free."
        if old != rb.grounded:
            self.mark_changed()
        return True, ""

    def _toggle_ground(self, bid: str) -> None:
        rb = self.assembly.body(bid)
        ok, why = self.set_grounded(bid, not rb.grounded)
        if not ok:
            self.warn("Cannot change grounding", why)

    def make_manual_mass_dialog(self, body) -> D.ManualMassDialog:
        return self._keep(D.ManualMassDialog(self, body))

    def set_manual_mass(self, bid: str) -> None:
        rb = self.assembly.body(bid)
        if rb is not None:
            self.make_manual_mass_dialog(rb).show()

    def commit_manual_mass(self, body, mp) -> None:
        body.manual = mp
        self.mark_changed()

    def clear_manual_mass(self, body) -> None:
        if body is not None and body.manual is not None:
            body.manual = None
            self.mark_changed()

    def group_with(self, bid: str) -> None:
        rb = self.assembly.body(bid)
        if rb is not None:
            self._keep(D.GroupDialog(self, rb)).show()

    @staticmethod
    def _merge(asm, keep_id: str, other_ids: list[str]) -> Optional[str]:
        """Merge ``other_ids`` into ``keep_id`` in ``asm``; returns a reason when it cannot be done."""
        keep = asm.body(keep_id)
        group = {keep_id, *other_ids}
        for j in asm.joints:
            if j.parent in group and j.child in group and j.kind != "fixed":
                return (f"'{asm.body(j.parent).name}' and '{asm.body(j.child).name}' are connected by the "
                        f"{j.kind} joint '{j.name}': remove the joint first, or leave them separate.")
        asm.joints = [j for j in asm.joints if not (j.parent in group and j.child in group)]
        for oid in other_ids:
            o = asm.body(oid)
            keep.component_ids += [c for c in o.component_ids if c not in keep.component_ids]
            keep.grounded = keep.grounded or o.grounded
            for j in asm.joints:
                if j.parent == oid:
                    j.parent = keep_id
                if j.child == oid:
                    j.child = keep_id
            asm.bodies = [b for b in asm.bodies if b.id != oid]
        return None

    def group_bodies(self, keep_id: str, other_ids: list[str]) -> tuple[bool, str]:
        asm = self.assembly
        keep = asm.body(keep_id)
        others = [asm.body(i) for i in other_ids]
        if keep is None or any(o is None for o in others) or not others:
            return False, "choose existing bodies to merge"
        before = set(asm.validate())
        trial = copy.deepcopy(asm)
        why = self._merge(trial, keep_id, other_ids)
        if why:
            return False, why
        new = [p for p in trial.validate() if p not in before]
        if new:
            return False, "; ".join(new)
        parts = [keep.effective_mass()] + [o.effective_mass() for o in others]
        any_manual = keep.manual is not None or any(o.manual is not None for o in others)
        self._merge(asm, keep_id, other_ids)
        s = 1.0 / get_unit_system(self.project.unit_system).length_from_si
        if all(p is not None for p in parts):
            comb = _combine(parts, s)
            if any_manual:
                comb.source = "cad+manual"
                comb.note = "combined from the merged bodies (some manual values)"
                keep.manual, keep.mass = comb, None
                keep.mass = None
            else:
                keep.mass, keep.manual = comb, None
        else:
            keep.manual = None
            try:
                miss = update_body_masses(self.project, bodies=self.session.bodies)
                if miss:
                    self.hint_label.setText("Mass missing: " + "; ".join(miss))
            except Exception as exc:  # noqa: BLE001
                self.hint_label.setText(f"Masses could not be recomputed: {exc}")
        self.mark_changed(scene=True)
        self.select("body", keep_id)
        return True, ""

    # ---- joints --------------------------------------------------------------------------------------
    def make_joint_dialog(self, joint=None, parent_id=None, child_id=None) -> D.JointDialog:
        if joint is None and parent_id is None and child_id is None:
            kind, ident = self.selected or ("", "")
            if kind == "body":
                child_id = ident
            rb_ground = next((b for b in self.assembly.bodies if b.grounded and b.id != child_id), None)
            parent_id = rb_ground.id if rb_ground else GROUND
        return self._keep(D.JointDialog(self, joint, parent_id, child_id))

    def add_joint(self, child_id: Optional[str] = None) -> None:
        if self.refuse_while_running("adding a joint"):
            return
        if self.assembly.is_empty():
            self.warn("No assembly", "Open an example or create an assembly from the project's parts first.")
            return
        self.make_joint_dialog(child_id=child_id).show()

    def edit_joint(self, jid: str) -> None:
        if self.refuse_while_running("editing a joint"):
            return
        j = self.assembly.joint(jid)
        if j is not None:
            self.make_joint_dialog(joint=j).show()

    def commit_joint(self, j: Joint, editing: Optional[Joint]) -> None:
        asm = self.assembly
        if editing is not None:
            k = next(i for i, x in enumerate(asm.joints) if x.id == editing.id)
            asm.joints[k] = j
            if j.kind == "fixed":  # a fixed joint cannot carry a motor
                self._drop_drivers_of(j.id)
        else:
            asm.joints.append(j)
        self.mark_changed(scene=True)
        self.select("joint", j.id)

    def _drop_drivers_of(self, jid: str) -> None:
        for st in self.mbd_studies():
            S = get_mbd(st)
            n = len(S.drivers)
            S.drivers = [d for d in S.drivers if d.joint != jid]
            if len(S.drivers) != n:
                set_mbd(st, S)

    def delete_joint(self, jid: str) -> None:
        if self.refuse_while_running("deleting a joint"):
            return
        asm = self.assembly
        j = asm.joint(jid)
        if j is None:
            return
        if not self.confirm("Delete joint", f"Delete the joint '{j.name}' together with its motors and "
                            "transmissions?"):
            return
        asm.joints = [x for x in asm.joints if x.id != jid]
        asm.couplings = [c for c in asm.couplings if jid not in (c.leader, c.follower)]
        self._drop_drivers_of(jid)
        self.mark_changed(scene=True)
        self.select("assembly", "")

    # ---- drivers -----------------------------------------------------------------------------------
    def driver_of(self, jid: str):
        S = get_mbd(self.study) if self.study else None
        return next((d for d in (S.drivers if S else []) if d.joint == jid), None)

    def driver_by_id(self, did: str):
        S = get_mbd(self.study) if self.study else None
        return next((d for d in (S.drivers if S else []) if d.id == did), None)

    def make_driver_dialog(self, study=None, driver=None, joint_id=None) -> D.DriverDialog:
        return self._keep(D.DriverDialog(self, study or self.study, driver, joint_id))

    def add_driver(self, joint_id: Optional[str] = None) -> None:
        if self.refuse_while_running("adding a motor"):
            return
        if self.study is None:
            self.warn("No motion study", "This project has no motion study. Open one of the examples.")
            return
        if not any(j.kind != "fixed" for j in self.assembly.joints):
            self.warn("No joint to drive", "Add a revolute or prismatic joint first.")
            return
        if joint_id is None and self.selected and self.selected[0] == "joint":
            joint_id = self.selected[1]
        self.make_driver_dialog(joint_id=joint_id).show()

    def edit_driver(self, did: str) -> None:
        if self.refuse_while_running("editing a motor"):
            return
        d = self.driver_by_id(did)
        if d is not None:
            self.make_driver_dialog(driver=d).show()

    def commit_driver(self, study, d, editing) -> None:
        S = get_mbd(study)
        if editing is not None:
            S.drivers = [d if x.id == editing.id else x for x in S.drivers]
        else:
            S.drivers.append(d)
        set_mbd(study, S)
        self.mark_changed(scene=True)
        self.select("driver", d.id)

    def delete_driver(self, did: str) -> None:
        if self.refuse_while_running("deleting a motor"):
            return
        d = self.driver_by_id(did)
        if d is None or not self.confirm("Delete motor", f"Delete '{d.name}'?"):
            return
        S = get_mbd(self.study)
        S.drivers = [x for x in S.drivers if x.id != did]
        set_mbd(self.study, S)
        self.mark_changed(scene=True)
        self.select("assembly", "")

    def mode_word(self, d, long: bool = False) -> str:
        j = self.assembly.joint(d.joint)
        prism = j is not None and j.kind == "prismatic"
        if d.mode == "effort":
            return "force (N)" if prism else "torque (N·m)"
        if d.mode == "position":
            return f"position ({self.length_unit})" if prism else "angle (deg)"
        return f"speed ({self.length_unit}/s)" if prism else "angular speed"

    def profile_text(self, d) -> str:
        j = self.assembly.joint(d.joint)
        prism = j is not None and j.kind == "prismatic"
        f, u = 1.0, ""
        if d.mode == "effort":
            u = "N" if prism else "N·m"
        elif prism:
            u = self.length_unit if d.mode == "position" else f"{self.length_unit}/s"
        else:
            f = 180 / math.pi
            u = "deg" if d.mode == "position" else "deg/s"
        p = d.profile
        if p.kind == "constant":
            return f"constant {fnum((p.value or 0.0) * f)} {u}"
        if p.kind == "ramp":
            return (f"ramp from {fnum(p.v0 * f)} to {fnum(p.v1 * f)} {u} between t = {p.t0:g} s and {p.t1:g} s, "
                    "then held")
        return f"table of {len(p.table or [])} points (t, value in {u})"

    def coupling_formula(self, c, capital: bool = False) -> str:
        asm = self.assembly
        lj, fj = asm.joint(c.leader), asm.joint(c.follower)
        ln = asm.body(lj.child).name if lj and asm.body(lj.child) else (lj.name if lj else "?")
        fn = asm.body(fj.child).name if fj and asm.body(fj.child) else (fj.name if fj else "?")
        txt = f"{fn} = {self.ratio_text(c.ratio)} × {ln[:1].lower() + ln[1:]}"
        return txt if capital else txt[:1].lower() + txt[1:] if False else txt

    @staticmethod
    def ratio_text(r: float) -> str:
        s = f"{abs(r):g}"
        return f"−{s}" if r < 0 else s

    # ---- study settings ------------------------------------------------------------------------------
    def make_study_settings_dialog(self, study=None) -> D.StudySettingsDialog:
        return self._keep(D.StudySettingsDialog(self, study or self.study))

    def edit_study_settings(self, study_id: Optional[str] = None) -> None:
        if self.refuse_while_running("editing the study settings"):
            return
        st = self.project.study_by_id(study_id) if study_id else self.study
        if st is None:
            self.warn("No motion study", "This project has no motion study.")
            return
        self.make_study_settings_dialog(st).show()

    def commit_study_settings(self, study, S, initial: dict) -> None:
        set_mbd(study, S)
        for jid, (q, qd) in initial.items():
            j = self.assembly.joint(jid)
            if j is not None:
                j.initial_q, j.initial_qd = q, qd
        self.mark_changed()

    # ---- running --------------------------------------------------------------------------------------
    def can_run(self) -> bool:
        ok, _ = backend.available()
        return ok and self.study is not None and not self.running

    def _on_progress(self, frac: float, msg: str) -> None:
        pb = self.study_panel.progress
        pb.setVisible(True)
        pb.setValue(int(1000 * max(0.0, min(1.0, frac))))
        pb.setFormat(f"{msg}  {frac * 100:.0f}%")

    def run_study(self):
        ok, why = backend.available()
        if not ok:
            self.study_panel.refresh()
            self.tabs.setCurrentWidget(self.study_panel)
            self.hint_label.setText("The motion solver (MuJoCo) is not installed: see the Study tab.")
            return None
        st = self.study
        if st is None:
            self.warn("No motion study", "This project has no motion study.")
            return None
        if self.running:
            return None
        probs = self.study_problems()
        if probs:
            self.tabs.setCurrentWidget(self.study_panel)
            self.study_panel.refresh()
            return None
        # The worker gets a snapshot taken here, on the GUI thread: a deep copy of the assembly and of the study.
        # Edits made while it runs never reach the worker, and the worker never writes the live project; the
        # results are written back on the GUI thread in _run_finished (review N8).
        project, data_dir = self.project, self.session.data_dir
        snap_project, snap_study = snapshot_for_run(project, st)
        self.running = True
        self._run_ctx = {"project": project, "edited": False}
        self.playback.pause()
        self.study_panel.status.setText("Running… (the run uses the model as it is now: joints, motors and "
                                        "study settings cannot be edited until it ends)")
        self.study_panel.run_btn.setEnabled(False)
        self.top_run.setEnabled(False)
        bridge, holder = self._bridge, {}
        sid, sname = st.id, st.name

        def work(progress, cancelled):
            from opencae.mbd.run import run_study as _run
            holder["res"] = _run(snap_project, snap_study,
                                 progress=lambda f, msg="": (bridge.progress.emit(float(f), msg), progress(f, msg)),
                                 cancelled=cancelled, data_dir=data_dir)
            return {"study": sname, "study_id": sid, "opener": "mbd.playback"}

        def done(_res=None):
            self._run_finished(holder.get("res"), sid, snap_study)

        def failed(msg: str):
            self._run_failed(msg)
        try:
            from opencae.ui.jobs import track_callable
            if self.mw.extensions.get(JOBS_KEY) is None:
                raise ImportError("no Progress Monitor")
            return track_callable(self.mw, "motion", f"Motion run: {sname}", sname, work,
                                  stages=("Input validation", "Solver", "Postprocessing", "Results ready"),
                                  source="gui", analysis_type="mbd", inputs={"study": sname},
                                  on_done=done, on_failed=failed)
        except ImportError:
            return self._run_in_thread(work, done, failed)

    def _run_in_thread(self, work, done, failed):
        from PySide6.QtCore import QThread
        from opencae.utilities import threads

        class T(QThread):
            ok = Signal()
            bad = Signal(str)

            def run(self_t):  # noqa: N805
                import traceback
                try:
                    work(lambda f, m="": None, lambda: False)
                except BaseException:  # noqa: BLE001
                    self_t.bad.emit(traceback.format_exc().strip().splitlines()[-1])
                    return
                self_t.ok.emit()
        t = T(self)
        t.ok.connect(lambda: done())
        t.bad.connect(failed)
        threads.track(t)
        self._thread = t
        t.start()
        return t

    def _run_finished(self, res, sid: str, snap_study=None) -> None:
        """GUI thread: the only place a finished run touches the live project."""
        ctx = getattr(self, "_run_ctx", None) or {}
        self._run_ctx = None
        self.running = False
        self.study_panel.progress.setVisible(False)
        run_project = ctx.get("project", self.project)
        live = run_project.study_by_id(sid) if run_project is not None else None
        if live is not None and snap_study is not None:
            live.results = snap_study.results
        same = run_project is self.project and live is not None
        edited = bool(ctx.get("edited"))
        self.study_panel.status.setText(
            "Results ready: press Play or drag the time slider." + (
                " The model was changed during the run: these results are for the model as it was when Run was "
                "pressed (marked out of date)." if edited else ""))
        self.mw.dirty = True
        if res is not None and same and sid == self.study_id:
            self.result, self.result_stale = res, edited
            self._gap_cache = None
            self.playback.configure(res)
            self.sample = 0
            self.plots.refresh()
            self._apply_pose()
            self.readout.update_for(0)
            self.plots.set_cursor(0.0)
        self.tree.rebuild(self)
        self._refresh_panels()
        self.mw.statusBar().showMessage("Motion study finished", 6000)

    def _run_failed(self, msg: str) -> None:
        self._run_ctx = None
        self.running = False
        self.study_panel.progress.setVisible(False)
        self.study_panel.status.setText(f"The run did not finish: {msg}")
        self.study_panel.refresh()
        self.top_run.setEnabled(self.can_run())
        self.mw.statusBar().showMessage("Motion study failed: " + msg, 8000)

    # ---- fingertip gap (gripper) -------------------------------------------------------------------------
    def _tip_pairs(self):
        """Two (component id, CAD-frame point) pairs: the inner tip corners of the two coupled revolute fingers."""
        asm = self.assembly
        key = (id(self.session.bodies), tuple(c.id for c in asm.components), tuple(x.id for x in asm.couplings),
               tuple(tuple(j.origin) for j in asm.joints))
        if self._tips_cache is not None and self._tips_cache[0] == key:
            return self._tips_cache[1]
        out = None
        if len(asm.couplings) == 1:
            c = asm.couplings[0]
            jl, jf = asm.joint(c.leader), asm.joint(c.follower)
            if jl and jf and jl.kind == jf.kind == "revolute" and jl.parent == jf.parent:
                pts = []
                for j, other in ((jl, jf), (jf, jl)):
                    rb = asm.body(j.child)
                    comp = asm.component(rb.component_ids[0]) if rb and len(rb.component_ids) == 1 else None
                    gb = self.session.bodies.get(comp.body_id) if comp else None
                    if gb is None:
                        pts = []
                        break
                    lo, hi = gb.bbox
                    cad = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
                    P = comp.placement_matrix()
                    ref = (P[:3, :3] @ cad.T).T + P[:3, 3]
                    o = np.asarray(j.origin, float)
                    u = _unit(ref.mean(axis=0) - o)
                    tol = 1e-6 * max(gb.size, 1.0)
                    far = (ref - o) @ u
                    cand = np.nonzero(far >= far.max() - tol)[0]
                    e = _unit(np.asarray(other.origin, float) - o)
                    score = ref[cand] @ e
                    inner = cand[score >= score.max() - tol]
                    pts.append((comp.id, cad[inner].mean(axis=0)))
                if len(pts) == 2:
                    out = pts
        self._tips_cache = (key, out)
        return out

    def has_gap(self) -> bool:
        return self.result is not None and self._tip_pairs() is not None

    def fingertip_gap_mm(self, i: int) -> Optional[float]:
        pairs = self._tip_pairs()
        if pairs is None or self.result is None or not self._result_fits():
            return None
        T = pose_transforms(self.result, int(i))
        (c1, p1), (c2, p2) = pairs
        a = T[c1] @ np.append(p1, 1.0)
        b = T[c2] @ np.append(p2, 1.0)
        return float(np.linalg.norm(a[:3] - b[:3]))

    def gap_series(self) -> Optional[np.ndarray]:
        if not self.has_gap():
            return None
        key = id(self.result)
        if self._gap_cache is None or self._gap_cache[0] != key:
            self._gap_cache = (key, np.array([self.fingertip_gap_mm(i) for i in range(len(self.result.t))]))
        return self._gap_cache[1]

    # ---- export --------------------------------------------------------------------------------------------
    def export_csv(self, directory) -> list[Path]:
        if self.result is None:
            raise ValueError("there is no result to export: run the study first")
        return self.result.export_csv(Path(directory))

    def export_csv_dialog(self) -> None:
        if self.result is None:
            self.warn("Nothing to export", "Run the study first.")
            return
        d = QFileDialog.getExistingDirectory(self, "Export CSV files to folder", str(Path.home()))
        if d:
            files = self.export_csv(d)
            self.mw.statusBar().showMessage(f"Wrote {len(files)} CSV files to {d}", 8000)

    # ---- structural load case -----------------------------------------------------------------------------
    @staticmethod
    def transfer_module():
        try:
            from opencae.assembly import transfer
            return transfer
        except ImportError:
            return None

    def loadcase_bodies(self) -> list:
        asm = self.assembly
        out = []
        for b in asm.bodies:
            if b.grounded or len(b.component_ids) != 1:
                continue
            if not any(j.child == b.id for j in asm.joints):
                continue
            if any(j.parent == b.id for j in asm.joints):
                continue
            out.append(b)
        return out

    def load_case_status(self) -> tuple[bool, str]:
        if self.transfer_module() is None:
            return False, "Load transfer is not available in this build."
        if self.result is None:
            return False, "Run the study first: the load case is taken from its results."
        if self.result_stale:
            return False, "The model changed since this run: run the study again."
        sel = self.selected
        if not sel or sel[0] != "body":
            return False, "Select the body (a link or finger) whose load you want."
        if sel[1] not in [b.id for b in self.loadcase_bodies()]:
            return False, "Select a moving body with one component that carries no other body."
        return True, ""

    def can_create_load_case(self) -> bool:
        return self.load_case_status()[0]

    def open_load_case(self) -> None:
        ok, why = self.load_case_status()
        if not ok:
            self.warn("Create structural load case", why)
            return
        self.make_loadcase_dialog().show()

    def make_loadcase_dialog(self) -> D.LoadCaseDialog:
        return self._keep(D.LoadCaseDialog(self))

    def end_faces(self, bid: str) -> list[dict]:
        gid = self.geometry_id_of_body(bid)
        gb = self.session.bodies.get(gid) if gid else None
        if gb is None or not gb.faces:
            return []
        lo, hi = np.asarray(gb.bbox[0], float), np.asarray(gb.bbox[1], float)
        ax = int(np.argmax(hi - lo))
        j = next((x for x in self.assembly.joints if x.child == bid), None)
        comp = self.assembly.component(self.assembly.body(bid).component_ids[0])
        P = comp.placement_matrix() if comp else np.eye(4)
        o_cad = np.linalg.inv(P) @ np.append(np.asarray(j.origin, float), 1.0) if j else np.append(lo, 1.0)
        ends, sides = [], []
        axname = "xyz"[ax]
        for f in gb.faces:
            if f.normal is None:
                continue
            d = dict(index=int(f.index), area=float(f.area), centroid=np.asarray(f.centroid, float))
            if abs(float(f.normal[ax])) > 0.99:
                d["label"] = f"end face at {axname} = {fnum(float(f.centroid[ax]))} {self.length_unit}"
                ends.append(d)
            else:
                d["label"] = f"side face (centre {fvec(f.centroid)})"
                sides.append(d)
        if ends:
            ends.sort(key=lambda d: np.linalg.norm(d["centroid"] - o_cad[:3]))
            ends[0]["role"] = "root"
            ends[0]["label"] += " — next to the joint (root)"
            if len(ends) > 1:
                ends[-1]["role"] = "tip"
                ends[-1]["label"] += " — far end (tip)"
        sides.sort(key=lambda d: -d["area"])
        return ends + sides

    def _face_ref(self, bid: str, idx: int) -> EntityRef:
        gid = self.geometry_id_of_body(bid)
        gb = self.session.bodies[gid]
        return EntityRef("face", gid, int(idx), gb.faces[int(idx)].signature())

    def preview_load_case(self, bid: str, t: float, root_idx: int, load_idx: int):
        T = self.transfer_module()
        if T is None:
            raise RuntimeError("load transfer is not available")
        proj = copy.deepcopy(self.project)
        st = proj.study_by_id(self.study_id)
        _study, rec = T.make_static_hold_study(proj, st, t, bid, self._face_ref(bid, root_idx),
                                               self._face_ref(bid, load_idx), result=self.result,
                                               data_dir=self.session.data_dir)
        return T.transfer_summary(rec), rec

    def create_load_case(self, bid: str, t: float, root_idx: int, load_idx: int):
        T = self.transfer_module()
        study, rec = T.make_static_hold_study(self.project, self.study, t, bid, self._face_ref(bid, root_idx),
                                              self._face_ref(bid, load_idx), result=self.result,
                                              data_dir=self.session.data_dir)
        mw = self.mw
        self.session.set_active_study(study.id)
        mw.dirty = True
        mw.selection = []
        mw.view_mode = "geometry"
        mw.ribbon.setCurrentIndex(0)
        mw.stack.setCurrentWidget(mw.viewport)
        mw.refresh_all(fit=True)
        msg = (f"Structural study '{study.name}' created from the motion at t = {t:g} s. Next: Mesh > Generate, "
               "then Analysis > Solve.")
        try:
            mw.console.append(msg)
        except Exception:  # noqa: BLE001
            pass
        mw.statusBar().showMessage(msg, 10000)
        return study, rec


# ============================================================================================ extension
class AssemblyExtension:
    """Returned by ``install``: ribbon page, stack page and the opener for finished motion jobs."""

    def __init__(self, main_window) -> None:
        self.mw = main_window
        self._ws: Optional[AssemblyWorkspace] = None
        self._showing = False
        self._build_ribbon()
        jobs = getattr(main_window, "extensions", {}).get(JOBS_KEY)
        reg = getattr(jobs, "reg", None)
        if reg is not None and hasattr(reg, "register_opener"):
            try:
                reg.register_opener("mbd.playback", self._open_job)
            except Exception:  # noqa: BLE001
                pass

    def workspace(self) -> AssemblyWorkspace:
        if self._ws is None:
            self._ws = AssemblyWorkspace(self.mw, self)
            self.mw.stack.addWidget(self._ws)
        return self._ws

    # ---- ribbon ------------------------------------------------------------------
    def _build_ribbon(self) -> None:
        mw = self.mw
        tb = QToolBar(PAGE)
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        from PySide6.QtCore import QSize
        tb.setIconSize(QSize(26, 26))
        specs = [("mo_gripper", "Open gripper example", "Gripper", "beam", "Open the two-finger gripper example",
                  lambda: self.workspace().open_example("gripper")),
                 ("mo_drone", "Open drone rotors example", "Drone rotors", "cylinder",
                  "Open the four-rotor drone assembly example", lambda: self.workspace().open_example("drone_rotors")),
                 ("mo_open", "Open project…", "Open", "open", "Open a project with an assembly",
                  lambda: self.workspace().open_project()),
                 "-",
                 ("mo_joint", "Add joint…", "Add joint", "transform", "Connect two bodies with a joint",
                  lambda: self._in_mode().add_joint()),
                 ("mo_motor", "Add motor…", "Add motor", "animate", "Command a joint with a motor",
                  lambda: self._in_mode().add_driver()),
                 ("mo_settings", "Study settings…", "Settings", "settings",
                  "Gravity, duration, time step, starting state", lambda: self._in_mode().edit_study_settings()),
                 "-",
                 ("mo_run", "Run motion study", "Run", "solve", "Simulate the motion of the current study",
                  lambda: self._in_mode().run_study()),
                 ("mo_loadcase", "Create structural load case…", "Load case", "force",
                  "Turn the loads of one body at one time into a structural study",
                  lambda: self._in_mode().open_load_case()),
                 "-",
                 ("mo_back", "Back to Studio", "Studio", "fit", "Return to the Studio viewport", self._back)]
        for s in specs:
            if s == "-":
                tb.addSeparator()
                continue
            name, text, short, ico, tip, slot = s
            a = mw._act(name, text, slot, ico, "", tip)
            a.setIconText(short)
            tb.addAction(a)
        mw.ribbon.addTab(tb, PAGE)
        mw.ribbon_bars[PAGE] = tb
        # connected after the main window's own handler, so the last word on the stack page is ours
        mw.ribbon.currentChanged.connect(self._on_ribbon)
        mw.ribbon.tabBar().tabBarClicked.connect(self._on_ribbon)
        # a project opened or created from the File menu while this page is showing: follow it
        orig = getattr(mw, "_reset_after_load", None)
        if orig is not None:
            def after_load(*a, **k):
                r = orig(*a, **k)
                if self._ws is not None and mw.stack.currentWidget() is self._ws:
                    self._ws.sync(fit=True)
                return r
            mw._reset_after_load = after_load

    def _on_ribbon(self, idx: int) -> None:
        if idx >= 0 and self.mw.ribbon.tabText(idx) == PAGE:
            self.show_mode()

    def show_mode(self) -> None:
        if self._showing:
            return
        self._showing = True
        try:
            ws = self.workspace()
            idx = list(self.mw.ribbon_bars).index(PAGE)
            if self.mw.ribbon.currentIndex() != idx:
                self.mw.ribbon.setCurrentIndex(idx)
            if self.mw.stack.currentWidget() is not ws:
                self.mw.stack.setCurrentWidget(ws)
                ws.sync(fit=True)
        finally:
            self._showing = False

    def _in_mode(self) -> AssemblyWorkspace:
        self.show_mode()
        return self.workspace()

    def _back(self) -> None:
        self.mw.ribbon.setCurrentIndex(0)
        self.mw.stack.setCurrentWidget(self.mw.viewport)

    def _open_job(self, job) -> None:
        """Opener of a finished motion job in the Progress Monitor: show its results here."""
        ws = self._in_mode()
        sid = (getattr(job, "outputs", {}) or {}).get("study_id")
        if sid and ws.project.study_by_id(sid) is not None:
            ws.set_study(sid)
        ws.tabs.setCurrentWidget(ws.readout)
