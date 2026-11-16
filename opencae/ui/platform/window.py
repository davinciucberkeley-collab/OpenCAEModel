"""K09-L Engineering Platform: one window, six connected working environments on one Engine.

1 Configuration & Design Studio   2 Multidisciplinary Analysis   3 Virtual Flight & Environment
4 Fabrication & Validation        5 Assembly & Integration       6 Cost, Optimization & Approval

Every page reads the same Engine (opencae.engineering.graph): a change of motor, battery or landing on
page 1 recomputes the level-1 results at once, marks the solver-backed analyses that depend on it STALE
(with the reason), and every page refreshes from the engine's change event. Heavy work (CalculiX FEA,
assembly check, flight simulation) runs in worker threads; the GUI stays responsive.
"""
from __future__ import annotations

import html
import json
import math
import tempfile
import traceback
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pyvista as pv
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QKeySequence, QPen
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGraphicsScene, QGraphicsView,
                               QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
                               QSlider, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QTextBrowser,
                               QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)
from pyvistaqt import QtInteractor

from opencae.engineering import approval as AP
from opencae.engineering import calcs
from opencae.engineering import catalog as C
from opencae.engineering.graph import CURRENT, FAILED, HEAVY, NOT_RUN, STALE, Engine
from opencae.engineering.model import GROUP_COLOURS, GROUPS
from opencae.engineering.quantities import CLASSES, SHORT, Q
from opencae.ui.platform import scene as SC

STATUS_COL = {CURRENT: "#1A6E3A", STALE: "#8A5600", NOT_RUN: "#6B7280", FAILED: "#AD2A1F",
              AP.PASS: "#1A6E3A", AP.FAIL: "#AD2A1F", AP.COND: "#8A5600", AP.NOT_EV: "#6B7280"}
CLASS_COL = {"test_measurement": "#1A6E3A", "supplier_spec": "#2f5d8a", "user_input": "#2f5d8a",
             "analytical": "#2e7f78", "simulation": "#2e7f78", "reduced_order": "#8A5600",
             "provisional_estimate": "#8A5600", "missing": "#AD2A1F"}
MODES = ("Beginner", "Engineer", "Expert")

LABELS = {  # result key -> (label, display scale, display unit, digits, analysis category)
    "mass_total": ("Take-off mass", 1000, "g", 5, "Mass & performance"),
    "weight": ("Weight", 1, "N", 4, "Mass & performance"),
    "tw_full": ("Thrust/weight, full battery", 1, "", 3, "Mass & performance"),
    "tw_low": ("Thrust/weight, low battery", 1, "", 3, "Mass & performance"),
    "disk_loading": ("Disk loading", 1, "N/m²", 3, "Mass & performance"),
    "p_hover_battery": ("Hover battery power", 1, "W", 3, "Mass & performance"),
    "p_hover_momentum": ("Hover power, momentum theory (check)", 1, "W", 3, "Mass & performance"),
    "endurance_hover": ("Hover endurance", 1 / 60, "min", 3, "Mass & performance"),
    "v_cruise": ("Cruise speed (min power)", 1, "m/s", 3, "Aerodynamic & fluid"),
    "cl_cruise": ("Cruise C_L", 1, "", 3, "Aerodynamic & fluid"),
    "ld_cruise": ("Cruise L/D", 1, "", 3, "Aerodynamic & fluid"),
    "p_cruise_elec": ("Cruise electrical power", 1, "W", 3, "Aerodynamic & fluid"),
    "endurance_cruise": ("Cruise endurance", 1 / 60, "min", 3, "Mass & performance"),
    "mission_margin": ("Mission energy margin (2 min hover + 5 min cruise)", 1, "×", 3, "Mass & performance"),
    "aspect_ratio": ("Aspect ratio", 1, "", 3, "Aerodynamic & fluid"),
    "static_margin": ("Static margin", 100, "% c", 3, "Aerodynamic & fluid"),
    "cg_frac": ("CG position", 100, "% c from LE", 3, "Mass & performance"),
    "tipover_deg": ("Tip-over angle standing", 1, "°", 3, "Structural"),
    "tipover_margin": ("Tip-over margin vs 8 m/s gust lean", 1, "°", 3, "Structural"),
    "landing_n": ("Landing load factor (1 m/s)", 1, "g", 3, "Structural"),
    "landing_force_per_contact": ("Landing force per contact", 1, "N", 3, "Structural"),
    "thrust_max_full": ("Max static thrust, full battery", 1, "N", 3, "Mass & performance"),
    "rpm_hover": ("Hover rpm", 1, "rpm", 4, "Mass & performance"),
    "hover_throttle": ("Hover throttle", 100, "%", 3, "Mass & performance"),
    "i_hover_battery": ("Hover battery current", 1, "A", 3, "Thermal & electrical"),
    "i_hover_motor": ("Hover current per motor", 1, "A", 3, "Thermal & electrical"),
    "i_motor_max_full": ("Full-throttle current per motor", 1, "A", 3, "Thermal & electrical"),
    "motor_current_margin": ("Motor rating / full-throttle current", 1, "×", 3, "Thermal & electrical"),
    "esc_margin": ("ESC rating margin", 1, "×", 3, "Thermal & electrical"),
    "battery_c_margin": ("Battery C-rating margin", 1, "×", 3, "Thermal & electrical"),
    "cells_ok": ("Cell count compatible", 1, "", 1, "Thermal & electrical"),
    "motor_loss_hover": ("Motor heat, hover (each)", 1, "W", 3, "Thermal & electrical"),
    "motor_loss_max": ("Motor heat, full throttle (each)", 1, "W", 3, "Thermal & electrical"),
    "battery_heat_hover": ("Battery heat, hover", 1, "W", 3, "Thermal & electrical"),
    "box_fit": ("Transport box fit (assembled)", 1, "×", 3, "Structural"),
    "box_fit_fins_off": ("Transport box fit (fins/motor sets off)", 1, "×", 3, "Structural"),
    "cost_total": ("Total cost (parts+tax+contingency)", 1, "USD", 5, "Cost"),
    "reserve": ("Budget reserve", 1, "USD", 4, "Cost"),
}


def _fmt(q: Optional[Q], key: str = "") -> str:
    if q is None or q.value is None:
        return "—"
    if isinstance(q.value, list):
        v = np.asarray(q.value, float)
        return "[" + ", ".join(f"{x * (1000 if q.unit == 'm' else 1):.4g}" for x in v.ravel()[:9]) + "]" + \
            (" mm" if q.unit == "m" else f" {q.unit}")
    lab = LABELS.get(key)
    if lab:
        return f"{q.value * lab[1]:.{lab[3]}g} {lab[2]}".strip()
    return q.fmt(4)


def _item(text, colour: Optional[str] = None, bold=False, tip: str = "") -> QTableWidgetItem:
    it = QTableWidgetItem(str(text))
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    if colour:
        it.setForeground(QBrush(QColor(colour)))
    if bold:
        f = it.font()
        f.setBold(True)
        it.setFont(f)
    if tip:
        it.setToolTip(tip)
    return it


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setAlternatingRowColors(True)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def _fill(t: QTableWidget, rows: list[list]) -> None:
    t.setSortingEnabled(False)
    t.setRowCount(len(rows))
    for i, r in enumerate(rows):
        for j, v in enumerate(r):
            t.setItem(i, j, v if isinstance(v, QTableWidgetItem) else _item(v))
    t.resizeColumnsToContents()


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class Job(QRunnable):
    """Run ``fn`` in the thread pool; ``done(result)`` or ``failed(traceback)`` arrive in the GUI thread."""

    def __init__(self, fn: Callable, on_done: Callable, on_fail: Callable):
        super().__init__()
        self.fn, self.sig = fn, _Signals()
        self.sig.done.connect(on_done)
        self.sig.failed.connect(on_fail)

    def run(self):
        try:
            res = self.fn()
        except Exception:
            self.sig.failed.emit(traceback.format_exc())
            return
        self.sig.done.emit(res)


class Viewer(QWidget):
    """A VTK viewport showing the aircraft; emits the picked body index."""
    picked = Signal(int)

    def __init__(self, parent=None, background=("#1d232c", "#3a4250")):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plotter = QtInteractor(self, auto_update=False)
        lay.addWidget(self.plotter.interactor)
        self.plotter.set_background(background[0], top=background[1])
        try:
            self.plotter.add_axes(line_width=2, color="white", xlabel="x aft", ylabel="y right", zlabel="z up")
        except Exception:
            pass
        self._press = None
        self.plotter.iren.add_observer("LeftButtonPressEvent", self._on_press)
        self.plotter.iren.add_observer("LeftButtonReleaseEvent", self._on_release)
        self.pd: Optional[pv.PolyData] = None
        self.base_rgb = None
        self.actor = None

    def _on_press(self, obj, ev):
        self._press = self.plotter.iren.interactor.GetEventPosition()

    def _on_release(self, obj, ev):
        pos = self.plotter.iren.interactor.GetEventPosition()
        if self._press is None or abs(pos[0] - self._press[0]) + abs(pos[1] - self._press[1]) > 4 or self.pd is None:
            return
        import vtk
        pk = vtk.vtkCellPicker()
        pk.SetTolerance(0.0005)
        if pk.Pick(pos[0], pos[1], 0, self.plotter.renderer) and pk.GetActor() is self.actor and pk.GetCellId() >= 0:
            self.picked.emit(int(self.pd.cell_data["body"][pk.GetCellId()]))
        else:
            self.picked.emit(-1)

    def show_aircraft(self, pd: pv.PolyData, reset=False, opacity=1.0, edges=False):
        self.pd = pd
        self.base_rgb = np.asarray(pd.cell_data["rgb"]).copy()
        self.actor = self.plotter.add_mesh(pd, scalars="rgb", rgb=True, name="aircraft", opacity=opacity,
                                           show_edges=edges, edge_color="#20242a", smooth_shading=False,
                                           reset_camera=False, pickable=True)
        if reset:
            self.plotter.reset_camera()
        self.plotter.render()

    def recolour(self, selected: set, dim: Optional[set] = None):
        if self.pd is None:
            return
        self.pd.cell_data["rgb"] = SC.highlight_colours(self.pd, self.base_rgb, selected, dim=dim)
        self.plotter.render()

    def view(self, name: str):
        p = self.plotter
        {"iso": lambda: p.view_isometric(), "top": lambda: p.view_xy(), "front": lambda: p.view_yz(negative=True),
         "side": lambda: p.view_xz(negative=True)}.get(name, p.view_isometric)()
        p.reset_camera()
        p.render()


def _canvas(nrows=1, ncols=1, h=3.0):
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
    from matplotlib.figure import Figure
    fig = Figure(figsize=(6, h), tight_layout=True)
    axes = [fig.add_subplot(nrows, ncols, i + 1) for i in range(nrows * ncols)]
    return FigureCanvasQTAgg(fig), fig, axes


# =========================================================================== page 1
class ConfigPage(QWidget):
    def __init__(self, win: "PlatformWindow"):
        super().__init__()
        self.win, self.e = win, win.engine
        self.selected: Optional[str] = None
        self.hidden: set = set()
        self.isolated: Optional[set] = None
        outer = QVBoxLayout(self)
        tb = QHBoxLayout()
        for v in ("iso", "top", "front", "side"):
            b = QPushButton(v.capitalize())
            b.clicked.connect(lambda _=False, v=v: self.viewer.view(v))
            tb.addWidget(b)
        self.colour_by = QComboBox()
        self.colour_by.addItems(["Colour: parts", "Colour: groups"])
        self.colour_by.currentIndexChanged.connect(lambda _: self.redraw())
        tb.addWidget(self.colour_by)
        for txt, fn in (("Isolate", self.isolate), ("Hide", self.hide_sel), ("Show all", self.show_all),
                        ("Frames", self.toggle_frames), ("Compare configurations…", self.compare)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            tb.addWidget(b)
        tb.addStretch()
        outer.addLayout(tb)
        vs = QSplitter(Qt.Vertical)
        hs = QSplitter(Qt.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Assembly / component", "Qty", "Mass g"])
        self.tree.itemClicked.connect(self._tree_clicked)
        hs.addWidget(self.tree)
        self.viewer = Viewer()
        self.viewer.picked.connect(self._picked)
        hs.addWidget(self.viewer)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        box = QGroupBox("Configuration (catalog selections)")
        fl = QFormLayout(box)
        self.combos = {}
        for kind in C.KINDS:
            cb = QComboBox()
            for o in self.e.model.catalog[kind]:
                cb.addItem(o.name + ("" if o.compatible else "  (not offered)"), o.id)
                if not o.compatible:
                    idx = cb.count() - 1
                    cb.model().item(idx).setEnabled(False)
                    cb.setItemData(idx, o.incompatible_reason, Qt.ToolTipRole)
            cb.setCurrentIndex(max(0, cb.findData(self.e.model.config[kind])))
            cb.currentIndexChanged.connect(lambda _i, k=kind, c=cb: self.win.set_option(k, c.currentData()))
            fl.addRow(kind.capitalize(), cb)
            self.combos[kind] = cb
        self.payload = QDoubleSpinBox()
        self.payload.setRange(0, 2000)
        self.payload.setSuffix(" g")
        self.payload.editingFinished.connect(lambda: self.win.set_param("payload", self.payload.value() / 1000.0))
        fl.addRow("Payload", self.payload)
        rl.addWidget(box)
        self.props = QTextBrowser()
        self.props.setOpenExternalLinks(True)
        rl.addWidget(self.props, 1)
        hs.addWidget(right)
        hs.setSizes([280, 900, 360])
        vs.addWidget(hs)
        bottom = QTabWidget()
        self.graph_view = QGraphicsView()
        self.graph_view.setRenderHint(self.graph_view.renderHints())
        bottom.addTab(self.graph_view, "Dependency graph")
        self.changes = QPlainTextEdit()
        self.changes.setReadOnly(True)
        bottom.addTab(self.changes, "Changes && warnings")
        self.actions_box = QTextBrowser()
        bottom.addTab(self.actions_box, "Suggested actions")
        vs.addWidget(bottom)
        vs.setSizes([620, 230])
        outer.addWidget(vs, 1)
        self.frames_on = False
        self._built = False

    # ------------------------------------------------------------------ building
    def build(self):
        self.fill_tree()
        self.redraw(reset=True)
        self.draw_graph()
        self.refresh()
        self._built = True

    def fill_tree(self):
        from opencae.engineering import assembly as AS
        m = self.e.model
        self.tree.clear()
        for sub in AS.subassemblies(m):
            it = QTreeWidgetItem([sub["name"], "", ""])
            it.setData(0, Qt.UserRole, ("sub", sub["id"]))
            for k in sub["components"]:
                c = m.components[k]
                q = m.effective_qty(c)
                ch = QTreeWidgetItem([c.name, str(q), f"{(m.component_mass(c).value or 0) * 1000 * q:.1f}"])
                ch.setData(0, Qt.UserRole, ("comp", k))
                ch.setForeground(0, QBrush(QColor(GROUP_COLOURS.get(c.group, "#999"))))
                it.addChild(ch)
            self.tree.addTopLevelItem(it)
        self.tree.resizeColumnToContents(0)

    def visible_bodies(self):
        bodies = [b for b in self.e.model.active_bodies() if b.index not in self.hidden]
        if self.isolated:
            bodies = [b for b in bodies if b.index in self.isolated]
        return bodies

    def redraw(self, reset=False):
        pd = SC.aircraft_polydata(self.e.model, bodies=self.visible_bodies(),
                                  colour_by="group" if self.colour_by.currentIndex() else "body")
        self.viewer.show_aircraft(pd, reset=reset)
        self.highlight()

    def highlight(self):
        sel = set(self.e.model.components[self.selected].bodies) if self.selected in self.e.model.components else set()
        self.viewer.recolour(sel)

    def toggle_frames(self):
        p = self.viewer.plotter
        self.frames_on = not self.frames_on
        for i in range(3):
            p.remove_actor(f"frame{i}", render=False)
        if self.frames_on:
            cg = np.asarray(self.e.results["cg"].value) * 1000
            for i, (mesh, c) in enumerate(SC.frame_glyph(cg, 150)):
                p.add_mesh(mesh, color=c, name=f"frame{i}", reset_camera=False, pickable=False)
        p.render()

    def isolate(self):
        if self.selected:
            self.isolated = set(self.e.model.components[self.selected].bodies)
            self.redraw(reset=True)

    def hide_sel(self):
        if self.selected:
            self.hidden |= set(self.e.model.components[self.selected].bodies)
            self.redraw()

    def show_all(self):
        self.hidden, self.isolated = set(), None
        self.redraw(reset=True)

    # ------------------------------------------------------------------ selection
    def _picked(self, body: int):
        if body < 0:
            self.win.select_component(None)
            return
        self.win.select_component(self.e.model.bodies[body].component)

    def _tree_clicked(self, item, _col):
        kind, ident = item.data(0, Qt.UserRole) or (None, None)
        if kind == "comp":
            self.win.select_component(ident)

    def on_select(self, key: Optional[str]):
        self.selected = key
        self.highlight()
        self.props.setHtml(self.win.component_html(key))

    # ------------------------------------------------------------------ dependency graph
    def draw_graph(self, highlight: Optional[list] = None):
        g = self.e.graph
        sc = QGraphicsScene()
        depth = {}

        def d(nid):
            if nid not in depth:
                depth[nid] = 0 if not g[nid].reads else 1 + max(d(r) for r in g[nid].reads)
            return depth[nid]

        for nid in g:
            d(nid)
        cols: dict[int, list] = {}
        for nid, k in depth.items():
            cols.setdefault(k, []).append(nid)
        pos = {}
        W, H = 190, 34
        for k, ids in sorted(cols.items()):
            for i, nid in enumerate(sorted(ids)):
                pos[nid] = (k * (W + 50), i * (H + 12))
        for a, b in self.e.edges():
            (x1, y1), (x2, y2) = pos[a], pos[b]
            hl = highlight and a in highlight + [highlight[0]] and b in highlight
            sc.addLine(x1 + W, y1 + H / 2, x2, y2 + H / 2,
                       QPen(QColor("#E0613A" if hl else "#9AA4B1"), 2 if hl else 1))
        for nid, (x, y) in pos.items():
            n = g[nid]
            if n.cost == HEAVY:
                st = self.e.heavy_status(nid)["status"]
                fill = {CURRENT: "#DDF5E5", STALE: "#FBEFD6", NOT_RUN: "#EEF0F3", FAILED: "#F8E9E7"}[st]
            else:
                fill = "#E6EEFB" if n.kind != "input" else "#FFFFFF"
            if highlight and nid in highlight:
                fill = "#FDE3D6"
            r = sc.addRect(x, y, W, H, QPen(QColor("#4B5563")), QBrush(QColor(fill)))
            t = sc.addText(n.label + (f"  [{self.e.heavy_status(nid)['status']}]" if n.cost == HEAVY else ""))
            t.setDefaultTextColor(QColor("#121619"))
            f = t.font()
            f.setPointSize(7)
            t.setFont(f)
            t.setTextWidth(W - 4)
            t.setPos(x + 2, y)
            r.setToolTip(self.e.explain(nid))
            t.setToolTip(self.e.explain(nid))
        self.graph_view.setScene(sc)

    def refresh(self, ev: Optional[dict] = None):
        m = self.e.model
        for k, cb in self.combos.items():
            cb.blockSignals(True)
            cb.setCurrentIndex(max(0, cb.findData(m.config[k])))
            cb.blockSignals(False)
        self.payload.blockSignals(True)
        self.payload.setValue((m.params["payload"].value or 0) * 1000)
        self.payload.blockSignals(False)
        if ev and ev.get("ok"):
            lines = [f"► {ev['change']}  (config {ev['config_hash']})",
                     f"  affected: {', '.join(self.e.graph[a].label for a in ev['affected'])}"]
            for k, (a, b) in list(ev["delta"].items())[:14]:
                if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    lines.append(f"  {LABELS.get(k, (k,))[0]}: {_fmt(Q(a, self.e.results[k].unit), k)} → "
                                 f"{_fmt(self.e.results[k], k)}")
            if ev["stale"]:
                lines.append("  STALE (re-run when needed): " + ", ".join(self.e.graph[s].label for s in ev["stale"]))
            self.changes.appendPlainText("\n".join(lines) + "\n")
            self.draw_graph(highlight=[ev["input"]] + ev["affected"])
        else:
            self.draw_graph()
        req = AP.requirements_table(self.e)
        acts = [f"<li><b>{r['id']}</b> {html.escape(r['text'])}: <span style='color:{STATUS_COL[r['status']]}'>"
                f"{r['status']}</span> - {html.escape(r['action'] or r['why'])}</li>" for r in req if r["status"] != AP.PASS]
        self.actions_box.setHtml("<ul>" + "".join(acts) + "</ul>")
        if ev and ev.get("input") in ("landing", "mount", "motor", "battery"):
            self.fill_tree()
            if ev.get("input") == "landing":
                self.hidden, self.isolated = set(), None
                self.redraw()

    def compare(self):
        self.win.tabs.setCurrentIndex(5)
        self.win.pages[5].run_trade()


# =========================================================================== page 2
class AnalysisPage(QWidget):
    CATS = ["Structural", "Aerodynamic & fluid", "Thermal & electrical", "Mass & performance", "Cost"]

    def __init__(self, win: "PlatformWindow"):
        super().__init__()
        self.win, self.e = win, win.engine
        lay = QHBoxLayout(self)
        left = QVBoxLayout()
        self.cat = QListWidget()
        self.cat.addItems(["All"] + self.CATS)
        self.cat.setCurrentRow(0)
        self.cat.currentRowChanged.connect(lambda _: self.refresh())
        left.addWidget(QLabel("<b>Category</b>"))
        left.addWidget(self.cat)
        self.scope = QLabel("Scope: whole aircraft")
        self.scope.setWordWrap(True)
        left.addWidget(self.scope)
        b = QPushButton("Inspect full aircraft")
        b.clicked.connect(lambda: self.win.select_component(None))
        left.addWidget(b)
        box = QGroupBox("Level 3 - solver-backed analyses")
        bl = QVBoxLayout(box)
        self.heavy_labels = {}
        for nid, txt, fn in (("fea_motor_tube", "Run motor-tube FEA (CalculiX)", lambda: self.win.run_fea("motor")),
                             ("fea_spar", "Run spar FEA (CalculiX)", lambda: self.win.run_fea("spar")),
                             ("assembly_check", "Run assembly check", self.win.run_assembly_check),
                             ("flight_hover", "Run hover simulation", lambda: self.win.run_flight("hover"))):
            btn = QPushButton(txt)
            btn.clicked.connect(fn)
            lab = QLabel()
            lab.setWordWrap(True)
            bl.addWidget(btn)
            bl.addWidget(lab)
            self.heavy_labels[nid] = lab
        b2 = QPushButton("Analyse selected component…")
        b2.clicked.connect(self.win.analyse_selected)
        bl.addWidget(b2)
        left.addWidget(box)
        left.addStretch()
        w = QWidget()
        w.setLayout(left)
        w.setMaximumWidth(320)
        lay.addWidget(w)
        mid = QSplitter(Qt.Vertical)
        self.table = _table(["Quantity", "Value", "Fidelity", "Evidence", "Source / method"])
        self.table.itemSelectionChanged.connect(self._row)
        mid.addWidget(self.table)
        self.detail = QTextBrowser()
        mid.addWidget(self.detail)
        self.canvas, self.fig, self.axes = _canvas(1, 2, 2.8)
        mid.addWidget(self.canvas)
        mid.setSizes([380, 200, 260])
        lay.addWidget(mid, 1)
        self.keys: list[str] = []

    def refresh(self, ev=None):
        cat = self.cat.currentItem().text() if self.cat.currentItem() else "All"
        sel = self.win.selected
        keys = [k for k, v in LABELS.items() if cat == "All" or v[4] == cat]
        if sel:
            rel = self.win.relevant_results(sel)
            if rel is not None:
                keys = [k for k in keys if k in rel]
            self.scope.setText(f"Scope: {self.e.model.components[sel].name} (results it affects)")
        else:
            self.scope.setText("Scope: whole aircraft")
        rows = []
        self.keys = []
        mode = self.win.mode
        for k in keys:
            q = self.e.results.get(k)
            if q is None:
                continue
            fid = "L2 reduced-order" if q.source == "reduced_order" or k in ("v_cruise", "cl_cruise", "ld_cruise",
                                                                              "p_cruise_elec", "endurance_cruise",
                                                                              "mission_margin", "landing_n",
                                                                              "landing_force_per_contact") else "L1 analytical"
            rows.append([LABELS[k][0], _item(_fmt(q, k), bold=True), fid,
                         _item(SHORT.get(q.source, q.source), CLASS_COL.get(q.source), tip=CLASSES.get(q.source, "")),
                         q.ref if mode != "Beginner" else ""])
            self.keys.append(k)
        if "motor_tube_sf" in self.e.results:
            q = self.e.results["motor_tube_sf"]
            rows.append(["Motor tube safety factor (FEA)", _item(f"{q.value:.3g}", bold=True), "L3 FEA (CalculiX)",
                         _item(SHORT.get(q.source), CLASS_COL.get(q.source)), q.ref])
            self.keys.append("motor_tube_sf")
        _fill(self.table, rows)
        for nid, lab in self.heavy_labels.items():
            h = self.e.heavy_status(nid)
            lab.setText(f"<span style='color:{STATUS_COL[h['status']]}'>● {h['status']}</span> "
                        f"<small>{html.escape(h['reason'] or '')}</small>")
        self.plot()

    def _row(self):
        r = self.table.currentRow()
        if r < 0 or r >= len(self.keys):
            return
        k = self.keys[r]
        q = self.e.results.get(k)
        node = self.e.node_of_result(k)
        txt = f"<h3>{html.escape(LABELS.get(k, (k,))[0])}</h3><p><b>{_fmt(q, k)}</b> - {CLASSES.get(q.source)}"
        if q.uncertainty:
            txt += f", ±{q.uncertainty * 100:.0f} % (1σ, relative)"
        txt += f"</p><p><b>Method:</b> {html.escape(q.ref)}</p>"
        if q.inputs:
            txt += "<p><b>Inputs:</b> " + ", ".join(html.escape(i) for i in q.inputs) + "</p>"
        if q.note:
            txt += f"<p><b>Note:</b> {html.escape(q.note)}</p>"
        if node:
            txt += f"<p><b>Dependency:</b> {html.escape(self.e.explain(node))}</p>"
        reqs = [r for r in self.e.model.requirements if r.metric == k]
        for rq in reqs:
            ev = AP.evaluate_requirement(rq, q)
            txt += (f"<p><b>{rq.id}</b> {html.escape(rq.text)} ({rq.op} {rq.threshold.value:g}): "
                    f"<span style='color:{STATUS_COL[ev['status']]}'>{ev['status']}</span> - {html.escape(ev['why'])}</p>")
        if k == "motor_tube_sf":
            txt += self.win.fea_html("fea_motor_tube")
        self.detail.setHtml(txt)

    def plot(self):
        m = self.e.model
        ax1, ax2 = self.axes
        ax1.clear()
        ax2.clear()
        bat = m.option("battery")
        th = np.linspace(0.1, 1.0, 19)
        for v, lab in ((bat.q("v_full").value, "full"), (bat.q("v_low").value, "low")):
            ops = [calcs.motor_point(m, v, t) for t in th]
            ax1.plot(th * 100, [4 * o["thrust"] / 9.80665 for o in ops], label=f"thrust, {lab} battery")
            ax2.plot(th * 100, [o["current"] for o in ops], label=f"{lab} battery")
        W = self.e.results["weight"].value / 9.80665
        ax1.axhline(W, color="k", lw=0.8, ls="--", label="weight")
        ax1.set_xlabel("throttle %")
        ax1.set_ylabel("total static thrust (kgf)")
        ax1.set_title("Thrust envelope (provisional prop data)", fontsize=9)
        ax1.legend(fontsize=7)
        imax = m.option("motor").q("imax").value
        ax2.axhline(imax, color="r", lw=0.8, ls="--", label="motor 30-s rating")
        ax2.set_xlabel("throttle %")
        ax2.set_ylabel("current per motor (A)")
        ax2.legend(fontsize=7)
        self.canvas.draw_idle()


# =========================================================================== page 3
class FlightPage(QWidget):
    def __init__(self, win: "PlatformWindow"):
        super().__init__()
        self.win, self.e = win, win.engine
        from opencae.engineering import flightsim as FS
        self.FS = FS
        lay = QHBoxLayout(self)
        ctl = QVBoxLayout()
        f = QFormLayout()
        self.scen = QComboBox()
        self.scen.addItems(list(FS.SCENARIOS))
        f.addRow("Scenario", self.scen)
        self.wind = QDoubleSpinBox()
        self.wind.setRange(0, 15)
        self.wind.setSuffix(" m/s")
        f.addRow("Wind", self.wind)
        self.wdir = QDoubleSpinBox()
        self.wdir.setRange(0, 359)
        self.wdir.setSuffix(" ° from")
        f.addRow("Wind direction", self.wdir)
        self.gust = QDoubleSpinBox()
        self.gust.setRange(0, 10)
        self.gust.setSuffix(" m/s")
        f.addRow("Gust amplitude", self.gust)
        self.pay = QDoubleSpinBox()
        self.pay.setRange(0, 1500)
        self.pay.setSuffix(" g")
        f.addRow("Extra payload", self.pay)
        self.degr = QDoubleSpinBox()
        self.degr.setRange(0, 1)
        self.degr.setSingleStep(0.1)
        self.degr.setValue(1.0)
        f.addRow("Motor M1 thrust factor", self.degr)
        self.soc = QDoubleSpinBox()
        self.soc.setRange(0.05, 1)
        self.soc.setValue(1.0)
        self.soc.setSingleStep(0.05)
        f.addRow("Start state of charge", self.soc)
        self.temp = QDoubleSpinBox()
        self.temp.setRange(-20, 45)
        self.temp.setValue(15)
        self.temp.setSuffix(" °C")
        f.addRow("Air temperature", self.temp)
        self.dur = QDoubleSpinBox()
        self.dur.setRange(0, 600)
        self.dur.setSuffix(" s (0 = default)")
        f.addRow("Duration", self.dur)
        g = QGroupBox("Scenario")
        g.setLayout(f)
        ctl.addWidget(g)
        self.run_btn = QPushButton("Run simulation")
        self.run_btn.clicked.connect(self.run)
        ctl.addWidget(self.run_btn)
        pb = QHBoxLayout()
        for txt, fn in (("▶", self.play), ("❚❚", self.pause), ("⟲", self.reset)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            pb.addWidget(b)
        self.cmp = QCheckBox("Overlay previous")
        pb.addWidget(self.cmp)
        self.follow = QCheckBox("Follow")
        self.follow.setChecked(True)
        pb.addWidget(self.follow)
        ctl.addLayout(pb)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.valueChanged.connect(self.seek)
        ctl.addWidget(self.slider)
        self.tele = QTextBrowser()
        ctl.addWidget(self.tele, 1)
        self.validity = QLabel()
        self.validity.setWordWrap(True)
        self.validity.setStyleSheet("color:#8A5600")
        ctl.addWidget(self.validity)
        w = QWidget()
        w.setLayout(ctl)
        w.setMaximumWidth(360)
        lay.addWidget(w)
        vs = QSplitter(Qt.Vertical)
        self.viewer = Viewer(background=("#a9c8e8", "#e9f1f8"))
        vs.addWidget(self.viewer)
        self.canvas, self.fig, self.axes = _canvas(2, 3, 3.2)
        vs.addWidget(self.canvas)
        vs.setSizes([520, 300])
        lay.addWidget(vs, 1)
        self.sim = None
        self.prev = None
        self.k = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._scene_built = False

    def build_scene(self):
        p = self.viewer.plotter
        for i, (mesh, col, kw) in enumerate(SC.garden()):
            p.add_mesh(mesh, color=col, name=f"g{i}", reset_camera=False, pickable=False, **kw)
        pd = SC.aircraft_polydata(self.e.model, colour_by="body")
        self.ac_actor = p.add_mesh(pd, scalars="rgb", rgb=True, name="flyer", reset_camera=False)
        self.trail = None
        self._scene_built = True
        self.place(np.array([0, 0, 0.26]), np.array([math.sqrt(0.5), 0, math.sqrt(0.5), 0]))
        p.camera_position = [(2.6, -3.2, 1.6), (0, 0, 0.45), (0, 0, 1)]
        p.render()

    def place(self, pos, quat):
        cg = np.asarray(self.e.results["cg"].value) * 1000
        import vtk
        M = SC.pose_matrix(pos, quat, cg)
        vm = vtk.vtkMatrix4x4()
        for i in range(4):
            for j in range(4):
                vm.SetElement(i, j, M[i, j])
        self.ac_actor.SetUserMatrix(vm)

    def params(self):
        ov = {}
        if self.wind.value() > 0:
            a = math.radians(self.wdir.value())
            ov["wind"] = [-self.wind.value() * math.sin(a), -self.wind.value() * math.cos(a), 0.0]
        if self.gust.value() > 0:
            ov["gust"] = {"amplitude": self.gust.value(), "duration": 2.0, "start": 3.0, "direction": [1, 0, 0]}
        if self.pay.value() > 0:
            ov["payload"] = self.pay.value() / 1000
        if self.degr.value() < 1:
            ov["motor_failure"] = {"motor": 0, "factor": self.degr.value(), "time": 2.0}
        if self.soc.value() < 1:
            ov["soc0"] = self.soc.value()
        T = self.temp.value() + 273.15
        ov["rho"] = 101325 / (287.05 * T)
        return ov

    def run(self):
        sc = self.scen.currentText()
        ov = self.params()
        dur = self.dur.value() or None
        self.run_btn.setEnabled(False)
        self.win.status(f"Simulating '{sc}'…")
        from opencae.engineering import services

        def fn():
            try:
                return services.run_flight(self.e, sc, duration=dur, **ov)
            except TypeError:  # an override this scenario does not take
                return services.run_flight(self.e, sc, duration=dur)

        self.win.submit(fn, self._done, lambda tb: (self.run_btn.setEnabled(True), self.win.error("Simulation", tb)))

    def _done(self, sim):
        self.run_btn.setEnabled(True)
        self.prev, self.sim = self.sim, sim
        self.slider.setRange(0, len(sim.t) - 1)
        self.validity.setText("⚠ " + sim.meta.get("validity", ""))
        self.plot()
        self.reset()
        self.play()
        self.win.engine_changed({"ok": True, "input": None})
        self.win.status(f"Simulation done: {sim.meta.get('outcome', {})}")

    def plot(self):
        s = self.sim
        if s is None:
            return
        A = self.axes
        for a in A:
            a.clear()
        # nose (-x body) direction from the quaternion: tilt from vertical is well defined in hover and cruise
        w, x, y, z = s.quat.T
        nose_z = -(2 * (x * z - w * y))  # world z of -x_B
        tilt = np.degrees(np.arccos(np.clip(nose_z, -1, 1)))
        series = [(A[0], "Altitude (m)", [s.pos[:, 2]]), (A[1], "Nose tilt from vertical (deg)", [tilt]),
                  (A[2], "Thrust (N)", [s.thrust[:, i] for i in range(4)]),
                  (A[3], "Battery current (A)", [s.current]), (A[4], "State of charge", [s.soc]),
                  (A[5], "Motor temp. rise (K)", [s.motor_temp_rise[:, i] for i in range(4)])]
        for ax, title, ys in series:
            for y in ys:
                ax.plot(s.t, y, lw=1)
            ax.set_title(title, fontsize=8)
            ax.tick_params(labelsize=7)
        if self.cmp.isChecked() and self.prev is not None:
            p = self.prev
            A[0].plot(p.t, p.pos[:, 2], "k--", lw=0.8)
            A[3].plot(p.t, p.current, "k--", lw=0.8)
            A[4].plot(p.t, p.soc, "k--", lw=0.8)
        self.cursor = [ax.axvline(0, color="#E0613A", lw=0.8) for ax in A]
        self.canvas.draw_idle()

    def play(self):
        if self.sim is not None:
            self.timer.start(40)

    def pause(self):
        self.timer.stop()

    def reset(self):
        self.timer.stop()
        self.k = 0
        self.seek(0)

    def _tick(self):
        if self.sim is None:
            return
        step = max(1, int(0.04 / max(self.sim.t[1] - self.sim.t[0], 1e-6)))
        self.k += step
        if self.k >= len(self.sim.t):
            self.timer.stop()
            self.k = len(self.sim.t) - 1
        self.slider.blockSignals(True)
        self.slider.setValue(self.k)
        self.slider.blockSignals(False)
        self.seek(self.k)

    def seek(self, k):
        s = self.sim
        if s is None or not self._scene_built:
            return
        self.k = k = int(min(max(k, 0), len(s.t) - 1))
        self.place(s.pos[k], s.quat[k])
        p = self.viewer.plotter
        if self.follow.isChecked():  # chase camera: keep the user's viewing direction, follow the aircraft
            cam = p.camera
            off = np.asarray(cam.position) - np.asarray(cam.focal_point)
            cam.focal_point = tuple(s.pos[k])
            cam.position = tuple(s.pos[k] + off)
        if k > 1:
            pts = s.pos[:k + 1:max(1, k // 400)]
            if len(pts) > 1:
                p.add_mesh(pv.lines_from_points(pts), color="#E0613A", line_width=2, name="trail",
                           reset_camera=False, pickable=False)
        p.render()
        for c in getattr(self, "cursor", []):
            c.set_xdata([s.t[k], s.t[k]])
        self.canvas.draw_idle()
        warns = [w for w in s.warnings if w[0] <= s.t[k]]
        v = s.vel[k]
        self.tele.setHtml(
            f"<b>t = {s.t[k]:.2f} s</b><br>Position {s.pos[k, 0]:.2f}, {s.pos[k, 1]:.2f} m · altitude "
            f"<b>{s.pos[k, 2]:.2f} m</b><br>Velocity {np.linalg.norm(v):.2f} m/s (vz {v[2]:+.2f}) · accel "
            f"{np.linalg.norm(s.acc[k]):.2f} m/s²<br>Attitude roll/pitch/yaw {s.euler[k, 0]:.1f} / {s.euler[k, 1]:.1f} / "
            f"{s.euler[k, 2]:.1f}°<br>Rates {np.degrees(s.omega[k]).round(1).tolist()} °/s<br>Thrust "
            f"{s.thrust[k].round(2).tolist()} N<br>RPM {s.rpm[k].round(0).astype(int).tolist()}<br>Battery "
            f"{s.current[k]:.1f} A · {s.power[k]:.0f} W · {s.v_batt[k]:.2f} V · SOC {s.soc[k] * 100:.1f} % · used "
            f"{s.energy_used[k] / 3600:.2f} Wh<br>Airspeed {s.airspeed[k]:.1f} m/s · α {s.alpha[k]:.1f}°<br>Aero force "
            f"{s.aero_force[k].round(2).tolist()} N<br>Elevons {s.elevon[k].round(1).tolist()}° · throttles "
            f"{s.cmd[k].round(2).tolist()}<br>Motor temp. rise {s.motor_temp_rise[k].round(1).tolist()} K<br>Distance "
            f"{s.distance[k]:.1f} m · endurance at this power ≈ "
            f"{(s.soc[k] * self.e.model.option('battery').q('capacity').value * self.e.model.option('battery').q('usable').value / max(s.current[k], 0.1)) / 60:.1f} min"
            + "".join(f"<br><span style='color:#AD2A1F'>⚠ {t:.1f} s: {html.escape(w)}</span>" for t, w in warns[-6:]))

    def refresh(self, ev=None):
        if self._scene_built and ev and ev.get("input") in ("landing", "mount", "motor", "battery", "payload"):
            self.viewer.plotter.remove_actor("flyer", render=False)
            pd = SC.aircraft_polydata(self.e.model, colour_by="body")
            self.ac_actor = self.viewer.plotter.add_mesh(pd, scalars="rgb", rgb=True, name="flyer", reset_camera=False)
            self.place(np.array([0, 0, 0.26]), np.array([math.sqrt(0.5), 0, math.sqrt(0.5), 0]))
            self.viewer.plotter.render()


# =========================================================================== page 4
class FabricationPage(QWidget):
    def __init__(self, win: "PlatformWindow"):
        super().__init__()
        self.win, self.e = win, win.engine
        self.measurements: list[dict] = []
        lay = QVBoxLayout(self)
        tb = QHBoxLayout()
        for txt, fn in (("Import measurements (CSV)…", self.import_csv), ("Write measurement template…", self.template)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            tb.addWidget(b)
        tb.addStretch()
        lay.addLayout(tb)
        self.tabs = QTabWidget()
        self.flow = QTreeWidget()
        self.flow.setHeaderLabels(["Stage", "Depends on", "Owner", "Gate"])
        self.tabs.addTab(self.flow, "Process graph")
        self.plan = _table(["Part", "Qty", "Process", "Material", "Time/unit", "Tools", "Risks"])
        self.plan.itemSelectionChanged.connect(self._plan_row)
        self.tabs.addTab(self.plan, "Manufacturing plan")
        self.matrix = _table(["Test", "Title", "Requirements", "Acceptance", "Predicted", "Measured", "Status"])
        self.matrix.itemSelectionChanged.connect(self._test_row)
        self.tabs.addTab(self.matrix, "Verification matrix")
        self.detail = QTextBrowser()
        sp = QSplitter(Qt.Vertical)
        sp.addWidget(self.tabs)
        sp.addWidget(self.detail)
        sp.setSizes([520, 240])
        lay.addWidget(sp, 1)
        self.plan_rows, self.tests, self.cmp = [], [], []

    def refresh(self, ev=None):
        from opencae.engineering import manufacturing as MF
        if not self.flow.topLevelItemCount():
            for st in MF.fabrication_workflow():
                it = QTreeWidgetItem([st.get("stage", st.get("id", "")), ", ".join(st.get("depends_on", [])),
                                      st.get("owner", ""), str(st.get("gate", ""))])
                for c in st.get("checklist", []):
                    it.addChild(QTreeWidgetItem(["☐ " + str(c)]))
                self.flow.addTopLevelItem(it)
            self.flow.resizeColumnToContents(0)
        self.plan_rows = MF.process_plan(self.e.model)
        rows = []
        for r in self.plan_rows:
            t = r["time_per_unit"]
            rows.append([r["component"].split(" — ")[0], r["qty"], r["process"], r["material"],
                         _item(f"{t.value:.2f} h EST" if t.value is not None else "—", CLASS_COL.get(t.source)),
                         ", ".join(r["tools"])[:60], "; ".join(r["risks"])[:80]])
        _fill(self.plan, rows)
        self.tests = MF.test_matrix(self.e.model, self.e.results)
        self.cmp = MF.compare(self.tests, self.measurements, self.e.results) if self.measurements else []
        cmpd = {}
        for c in self.cmp:
            if c.get("measured") is not None or c.get("test_id") not in cmpd:
                cmpd[c.get("test_id")] = c
        rows = []
        for t in self.tests:
            c = cmpd.get(t["id"], {})
            ptxt = "—"
            for mt, pred in (t.get("expected") or {}).items():
                if pred and pred.get("value") is not None and not isinstance(pred["value"], list):
                    ptxt = f"{mt}: {pred['value']:.4g} {pred.get('unit', '')} ({SHORT.get(pred.get('class'), '')})"
                    break
            st = c.get("status", t.get("status", AP.NOT_EV))
            acc = t.get("acceptance", "")
            rows.append([t["id"], t["title"], ", ".join(t.get("requirement_ids", [])),
                         (acc.get("text") if isinstance(acc, dict) else str(acc))[:90], ptxt,
                         "—" if c.get("measured") is None else f"{c['measured']:.4g}",
                         _item(st, STATUS_COL.get(st, "#6B7280"), bold=True)])
        _fill(self.matrix, rows)

    def _plan_row(self):
        r = self.plan.currentRow()
        if 0 <= r < len(self.plan_rows):
            row = self.plan_rows[r]
            self.win.select_component(row["component"], from_page=3)
            self.detail.setHtml("<pre>" + html.escape(json.dumps(row, indent=1, default=lambda o: o.to_dict() if
                                                                 isinstance(o, Q) else str(o))) + "</pre>")

    def _test_row(self):
        r = self.matrix.currentRow()
        if 0 <= r < len(self.tests):
            t = self.tests[r]
            self.detail.setHtml(
                f"<h3>{t['id']} {html.escape(t['title'])}</h3><p><b>Purpose:</b> {html.escape(str(t.get('purpose')))}</p>"
                f"<p><b>Equipment:</b> {html.escape(', '.join(t.get('equipment', [])))}</p><p><b>Procedure:</b><ol>"
                + "".join(f"<li>{html.escape(str(s))}</li>" for s in t.get("procedure", [])) +
                f"</ol></p><p><b>Record:</b> {html.escape(', '.join(map(str, t.get('data_to_record', []))))}</p>"
                f"<p><b>Acceptance:</b> {html.escape(str((t.get('acceptance') or {}).get('text', '')))}</p><p><b>Safety:</b> "
                f"{html.escape(str(t.get('safety')))}</p><p><i>A prediction verifies the design against the model; only "
                "a measurement validates it. Status stays NOT EVALUATED until a measurement is imported.</i></p>")

    def import_csv(self, path=None):
        from opencae.engineering import manufacturing as MF
        path = path or QFileDialog.getOpenFileName(self, "Measurements", "", "CSV (*.csv)")[0]
        if not path:
            return
        self.measurements = MF.import_measurements(path)
        self.refresh()
        self.win.measurements_changed(self.cmp)

    def template(self):
        from opencae.engineering import manufacturing as MF
        path = QFileDialog.getSaveFileName(self, "Template", "k09l_measurements.csv", "CSV (*.csv)")[0]
        if path:
            MF.write_measurements(path, MF.measurement_template(self.tests))


# =========================================================================== page 5
class AssemblyPage(QWidget):
    def __init__(self, win: "PlatformWindow"):
        super().__init__()
        self.win, self.e = win, win.engine
        lay = QHBoxLayout(self)
        left = QVBoxLayout()
        self.steps = QListWidget()
        self.steps.currentRowChanged.connect(self.step)
        left.addWidget(QLabel("<b>Assembly sequence</b>"))
        left.addWidget(self.steps, 1)
        self.info = QTextBrowser()
        left.addWidget(self.info, 1)
        b = QPushButton("Check assembly")
        b.clicked.connect(self.win.run_assembly_check)
        left.addWidget(b)
        b2 = QPushButton("Export BOM (CSV)…")
        b2.clicked.connect(self.export_bom)
        left.addWidget(b2)
        w = QWidget()
        w.setLayout(left)
        w.setMaximumWidth(380)
        lay.addWidget(w)
        right = QVBoxLayout()
        tb = QHBoxLayout()
        tb.addWidget(QLabel("Explode"))
        self.explode = QSlider(Qt.Horizontal)
        self.explode.setRange(0, 100)
        self.explode.valueChanged.connect(lambda _: self.redraw())
        tb.addWidget(self.explode)
        self.trans = QCheckBox("Transparent")
        self.trans.toggled.connect(lambda _: self.redraw())
        tb.addWidget(self.trans)
        self.section = QCheckBox("Section (y = 0)")
        self.section.toggled.connect(lambda _: self.redraw())
        tb.addWidget(self.section)
        self.labels = QCheckBox("Labels")
        self.labels.toggled.connect(lambda _: self.redraw())
        tb.addWidget(self.labels)
        self.sub = QComboBox()
        self.sub.currentIndexChanged.connect(lambda _: self.redraw())
        tb.addWidget(self.sub)
        right.addLayout(tb)
        self.viewer = Viewer(background=("#f3f5f8", "#d6dce4"))
        self.viewer.picked.connect(lambda b: self.win.select_component(
            None if b < 0 else self.e.model.bodies[b].component, from_page=4))
        right.addWidget(self.viewer, 1)
        self.check = QTreeWidget()
        self.check.setHeaderLabels(["Check assembly", "Result"])
        right.addWidget(self.check)
        lay.addLayout(right, 1)
        self.step_rows, self.highlight = [], set()
        self._built = False

    def build(self):
        from opencae.engineering import assembly as AS
        self.sub.blockSignals(True)
        self.sub.clear()
        self.sub.addItem("All subassemblies", None)
        for s in AS.subassemblies(self.e.model):
            self.sub.addItem(s["name"], s["components"])
        self.sub.blockSignals(False)
        self.step_rows = AS.assembly_steps(self.e.model)
        self.steps.clear()
        for s in self.step_rows:
            self.steps.addItem(f"{s['step']}. {s['title']}")
        self._built = True
        self.redraw(reset=True)

    def redraw(self, reset=False):
        if not self._built:
            return
        from opencae.engineering import assembly as AS
        m = self.e.model
        f = self.explode.value() / 100.0
        P = AS.exploded_points(m, f) if f > 0 else m.points
        bodies = m.active_bodies()
        comps = self.sub.currentData()
        if comps:
            bodies = [b for b in bodies if b.component in comps]
        pd = SC.aircraft_polydata(m, P, bodies, colour_by="group")
        if self.section.isChecked():
            pd = pd.clip(normal="y", origin=(0, 0, 0), invert=False)
        self.viewer.show_aircraft(pd, reset=reset, opacity=0.35 if self.trans.isChecked() else 1.0)
        dim = {b.index for b in bodies} - self.highlight if self.highlight else None
        self.viewer.recolour(self.highlight, dim=dim)
        p = self.viewer.plotter
        p.remove_actor("labels", render=False)
        if self.labels.isChecked():
            shown, pts, txt = set(), [], []
            for b in bodies:
                if b.component in shown or (self.highlight and b.index not in self.highlight):
                    continue
                shown.add(b.component)
                c = m.components[b.component]
                pts.append(P[b.vo:b.vo + b.vc].mean(0))
                txt.append(f"{c.pn}")
            if pts:
                p.add_point_labels(np.asarray(pts), txt[:60], name="labels", font_size=9, point_size=4,
                                   shape_opacity=0.6, always_visible=False, reset_camera=False)
        p.render()

    def step(self, i):
        if not (0 <= i < len(self.step_rows)):
            return
        s = self.step_rows[i]
        m = self.e.model
        keys = list(s.get("components", [])) + [f if isinstance(f, str) else f.get("component", "")
                                                for f in s.get("fasteners", [])]
        self.highlight = {b for k in keys if k in m.components for b in m.components[k].bodies}
        self.info.setHtml(f"<h3>Step {s['step']}: {html.escape(s['title'])}</h3><p><b>Parts:</b><br>"
                          + "<br>".join(html.escape(k.split(' — ')[0]) for k in s.get("components", []))
                          + "</p><p><b>Fasteners:</b><br>" + "<br>".join(html.escape(str(f)) for f in s.get("fasteners", []))
                          + f"</p><p><b>Tools:</b> {html.escape(str(s.get('tools')))}</p><p><b>Checks:</b> "
                          f"{html.escape(str(s.get('checks')))}</p><p><b>Tolerances:</b> "
                          f"{html.escape(str(s.get('tolerances_note')))}</p>")
        self.redraw()

    def show_check(self, res: dict):
        self.check.clear()
        for key in ("missing", "not_modelled_by_design", "interferences", "tight_clearances", "unresolved_joints",
                    "incompatible_mounting", "electrical_gaps", "orphan_fasteners", "expected_contacts"):
            items = res.get(key, [])
            top = QTreeWidgetItem([key.replace("_", " "), str(len(items))])
            if key in ("interferences", "electrical_gaps", "incompatible_mounting") and items:
                top.setForeground(1, QBrush(QColor("#AD2A1F")))
            for it in items[:200]:
                top.addChild(QTreeWidgetItem([json.dumps(it, default=str)[:220]]))
            self.check.addTopLevelItem(top)
        self.check.resizeColumnToContents(0)

    def export_bom(self, path=None):
        from opencae.engineering import assembly as AS
        import csv
        path = path or QFileDialog.getSaveFileName(self, "BOM", "k09l_bom.csv", "CSV (*.csv)")[0]
        if not path:
            return
        rows = AS.bill_of_materials(self.e.model)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    def refresh(self, ev=None):
        if self._built and ev and ev.get("input") in ("landing", "mount", "motor", "battery"):
            self.build()


# =========================================================================== page 6
class ApprovalPage(QWidget):
    def __init__(self, win: "PlatformWindow"):
        super().__init__()
        self.win, self.e = win, win.engine
        self.decisions: dict = {}
        lay = QVBoxLayout(self)
        tb = QHBoxLayout()
        self.obj = {}
        for k in AP.OBJECTIVES:
            cb = QCheckBox(k)
            cb.setChecked(k in ("mass", "cost", "endurance"))
            self.obj[k] = cb
            tb.addWidget(cb)
        b = QPushButton("Run trade study")
        b.clicked.connect(self.run_trade)
        tb.addWidget(b)
        b2 = QPushButton("Record gate decision…")
        b2.clicked.connect(self.decide)
        tb.addWidget(b2)
        b3 = QPushButton("Export readiness report…")
        b3.clicked.connect(lambda: self.win.export_report())
        tb.addWidget(b3)
        tb.addStretch()
        lay.addLayout(tb)
        top = QSplitter(Qt.Horizontal)
        self.summary = QTextBrowser()
        top.addWidget(self.summary)
        self.canvas, self.fig, self.axes = _canvas(1, 2, 3.0)
        top.addWidget(self.canvas)
        top.setSizes([620, 620])
        bot = QTabWidget()
        self.gates = QTreeWidget()
        self.gates.setHeaderLabels(["Gate / criterion", "Status", "Why / evidence"])
        self.gates.itemClicked.connect(self._gate_clicked)
        bot.addTab(self.gates, "Readiness gates")
        self.reqs = _table(["Req", "Requirement", "Criterion", "Predicted", "Evidence", "Measured", "Status", "Action"])
        self.reqs.itemSelectionChanged.connect(self._req_row)
        bot.addTab(self.reqs, "Requirements")
        self.costs = _table(["Group", "Cost USD", "Evidence"])
        bot.addTab(self.costs, "Cost breakdown")
        self.trade = _table(["Motor", "Battery", "Landing", "Objectives", "Feasible", "Failed", "Pareto"])
        bot.addTab(self.trade, "Trade study")
        self.revs = _table(["Revision", "Time", "Note", "Mass g", "T/W low", "Cost USD", "Config"])
        bot.addTab(self.revs, "Revisions")
        sp = QSplitter(Qt.Vertical)
        sp.addWidget(top)
        sp.addWidget(bot)
        sp.setSizes([330, 420])
        lay.addWidget(sp, 1)
        self.req_rows = []

    def refresh(self, ev=None):
        meas = self.win.measured_by_req()
        rs = AP.readiness_summary(self.e, meas, self.decisions)
        r = self.e.results
        cg = np.asarray(r["cg"].value) * 1000
        st_col = lambda s: STATUS_COL.get(s, "#6B7280")
        self.summary.setHtml(
            f"<h2>Design readiness - K09-L ({self.e.model.config_hash()})</h2>"
            f"<p><b>Current stage:</b> {html.escape(rs['stage'])}</p><table cellspacing=6>"
            + "".join(f"<tr><td>{g}</td><td>{html.escape(n)}</td><td style='color:{st_col(s)}'><b>{s}</b></td></tr>"
                      for g, n, s in rs["gates"]) + "</table>"
            f"<p><b>Configuration:</b> {html.escape(', '.join(f'{k}={v}' for k, v in self.e.model.config.items()))}<br>"
            f"<b>Mass</b> {_fmt(r['mass_total'], 'mass_total')} · <b>CG</b> x {cg[0]:.1f} mm ({_fmt(r['cg_frac'], 'cg_frac')})"
            f" · <b>T/W low</b> {_fmt(r['tw_low'], 'tw_low')} · <b>Hover endurance</b> "
            f"{_fmt(r['endurance_hover'], 'endurance_hover')} · <b>Cost</b> {_fmt(r['cost_total'], 'cost_total')} "
            f"(reserve {_fmt(r['reserve'], 'reserve')}; {r['cost_total'].note})</p>"
            f"<p><b>Failed constraints:</b> " + (", ".join(f"{x['id']}" for x in rs["failed"]) or "none") +
            f"<br><b>Missing validation evidence:</b> {len(rs['missing_evidence'])} requirements</p>"
            "<p><b>Major risks</b><ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in rs["risks"]) + "</ul></p>"
            "<p><b>Next steps</b><ol>" + "".join(f"<li>{html.escape(x)}</li>" for x in rs["next_steps"]) + "</ol></p>"
            "<p><i>Decision support only: gates never approve themselves, and simulated data cannot pass gates 5-6.</i></p>")
        self.gates.clear()
        for g in AP.gates(self.e, meas, self.decisions):
            it = QTreeWidgetItem([f"{g['id']} {g['name']}", g["status"],
                                  ("Decision: " + json.dumps(g["decision"])) if g["decision"] else ""])
            it.setForeground(1, QBrush(QColor(st_col(g["status"]))))
            for c in g["criteria"]:
                ch = QTreeWidgetItem([c["text"], c["status"], c.get("why", "")])
                ch.setForeground(1, QBrush(QColor(st_col(c["status"]))))
                it.addChild(ch)
            self.gates.addTopLevelItem(it)
        self.gates.resizeColumnToContents(0)
        self.req_rows = AP.requirements_table(self.e, meas)
        _fill(self.reqs, [[x["id"], x["text"], f"{x['op']} {x['threshold']:g} {x['unit']}",
                           "—" if x["predicted"] is None or isinstance(x["predicted"], list) else f"{x['predicted']:.4g}",
                           _item(SHORT.get(x["predicted_class"], ""), CLASS_COL.get(x["predicted_class"])),
                           "—" if x["measured"] is None else f"{x['measured']:.4g}",
                           _item(x["status"], st_col(x["status"]), bold=True), x["action"]] for x in self.req_rows])
        groups = [(g, r.get(f"cost_{g}")) for g in GROUPS if r.get(f"cost_{g}")]
        rows = [[g, f"{q.value:.2f}", _item(SHORT.get(q.source), CLASS_COL.get(q.source))] for g, q in groups]
        for k in ("cost_parts", "cost_tax", "cost_contingency", "cost_total", "reserve"):
            rows.append([LABELS.get(k, (k,))[0], f"{r[k].value:.2f}", _item(SHORT.get(r[k].source),
                                                                            CLASS_COL.get(r[k].source))])
        rows.append(["BOM lines with estimated price", f"{r['cost_estimated_lines'].value:.0f}", ""])
        _fill(self.costs, rows)
        ax = self.axes[0]
        ax.clear()
        ax.barh([g for g, _ in groups], [q.value for _, q in groups],
                color=[GROUP_COLOURS.get(g, "#999") for g, _ in groups])
        ax.set_xlabel("USD (parts, pre-tax)")
        ax.set_title("Cost by group", fontsize=9)
        self.canvas.draw_idle()
        _fill(self.revs, [[v["id"], v["time"], v["note"], f"{v['results'].get('mass_total', 0) * 1000:.0f}",
                           f"{v['results'].get('tw_low', 0):.2f}", f"{v['results'].get('cost_total', 0):.0f}",
                           json.dumps(v["config"])] for v in self.e.model.revisions])

    def run_trade(self):
        objs = tuple(k for k, cb in self.obj.items() if cb.isChecked()) or ("mass", "cost")
        ts = AP.trade_study(self.e, objs)
        rows = []
        for c in ts["candidates"]:
            rows.append([c["config"]["motor"], c["config"]["battery"], c["config"]["landing"],
                         ", ".join(f"{k}={v:.4g}" for k, v in c["objectives"].items() if v is not None),
                         _item("yes" if c["feasible"] else "no", "#1A6E3A" if c["feasible"] else "#AD2A1F"),
                         ", ".join(c["failed"]), "★" if c["pareto"] else ""])
        _fill(self.trade, rows)
        ax = self.axes[1]
        ax.clear()
        if len(objs) >= 2:
            a, b = objs[0], objs[1]
            for c in ts["candidates"]:
                x, y = c["objectives"].get(a), c["objectives"].get(b)
                if x is None or y is None:
                    continue
                ax.scatter(x, y, c="#1A6E3A" if c["feasible"] else "#AD2A1F", marker="*" if c["pareto"] else "o",
                           s=90 if c["pareto"] else 30)
                ax.annotate(f"{c['config']['motor'][:6]}/{c['config']['landing']}", (x, y), fontsize=6)
            ax.set_xlabel(a)
            ax.set_ylabel(b)
            ax.set_title("Trade study (★ Pareto, red = infeasible)", fontsize=9)
        self.canvas.draw_idle()
        self.trade_result = ts
        return ts

    def decide(self):
        gid, ok = QInputDialog.getItem(self, "Gate decision", "Gate", [f"G{i}" for i in range(1, 7)], 0, False)
        if not ok:
            return
        rev, ok = QInputDialog.getText(self, "Gate decision", "Reviewer name")
        if not ok or not rev.strip():
            return
        dec, ok = QInputDialog.getItem(self, "Gate decision", "Decision", ["APPROVE", "REJECT", "DEFER"], 2, False)
        if not ok:
            return
        notes, _ = QInputDialog.getText(self, "Gate decision", "Notes")
        AP.record_decision(self.decisions, gid, rev.strip(), dec, notes)
        self.refresh()

    def _gate_clicked(self, item, _c):
        txt = item.text(0)
        for rid in [r.id for r in self.e.model.requirements]:
            if txt.startswith(rid + ":") or txt.startswith(rid + " "):
                self.win.goto_requirement(rid)

    def _req_row(self):
        r = self.reqs.currentRow()
        if 0 <= r < len(self.req_rows):
            pass


# =========================================================================== main window
class PlatformWindow(QMainWindow):
    TITLES = ["1 · Configuration && Design", "2 · Engineering Analysis", "3 · Virtual Flight",
              "4 · Fabrication && V&&V", "5 · Assembly && Integration", "6 · Cost && Approval"]

    def __init__(self, engine: Optional[Engine] = None, state_path: Optional[Path] = None):
        super().__init__()
        self.setWindowTitle("OpenCAE Studio - K09-L Tailsitter Engineering Platform")
        self.resize(1600, 980)
        self.engine = engine or Engine()
        self.state_path = state_path
        self.selected: Optional[str] = None
        self.mode = "Engineer"
        self.pool = QThreadPool.globalInstance()
        self._jobs: list = []
        tb = QToolBar("Project")
        self.addToolBar(tb)
        for txt, fn, key in (("Save state", self.save_state, QKeySequence.Save), ("Load state", self.load_state, None),
                             ("Save revision", self.save_revision, None),
                             ("Open revision B (design study)", self.open_revision_b, None)):
            a = QAction(txt, self)
            a.triggered.connect(fn)
            if key:
                a.setShortcut(key)
            tb.addAction(a)
        self.rev_combo = QComboBox()
        self.rev_combo.setMinimumWidth(160)
        self.rev_combo.activated.connect(self._restore_rev)
        tb.addWidget(QLabel(" Revisions: "))
        tb.addWidget(self.rev_combo)
        tb.addSeparator()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(MODES)
        self.mode_combo.setCurrentIndex(1)
        self.mode_combo.currentTextChanged.connect(self._mode)
        tb.addWidget(QLabel(" Mode: "))
        tb.addWidget(self.mode_combo)
        tb.addWidget(QLabel("  Units: SI internally (display mm, g, °) "))
        tb.addSeparator()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find component / part number…")
        self.search.setMaximumWidth(240)
        self.search.returnPressed.connect(self._search)
        tb.addWidget(self.search)
        tb.addSeparator()
        self.badge = QLabel()
        tb.addWidget(self.badge)
        self.tabs = QTabWidget()
        self.pages = [ConfigPage(self), AnalysisPage(self), FlightPage(self), FabricationPage(self), AssemblyPage(self),
                      ApprovalPage(self)]
        for p, t in zip(self.pages, self.TITLES):
            self.tabs.addTab(p, t)
        self.tabs.currentChanged.connect(self._tab)
        self.setCentralWidget(self.tabs)
        for i in range(6):
            a = QAction(self)
            a.setShortcut(QKeySequence(f"Ctrl+{i + 1}"))
            a.triggered.connect(lambda _=False, i=i: self.tabs.setCurrentIndex(i))
            self.addAction(a)
        self.engine.listeners.append(self.engine_changed)
        self.measure_cmp: list = []
        self.pages[0].build()
        self.engine_changed({"ok": True, "input": None})
        if state_path and Path(state_path).exists():
            self.load_state(state_path)

    # ------------------------------------------------------------------ shared behaviour
    def status(self, txt: str):
        self.statusBar().showMessage(txt, 8000)

    def error(self, title: str, tb: str):
        self.status(f"{title} failed")
        box = QMessageBox(QMessageBox.Warning, title, f"{title} failed - details below; nothing was recorded as a "
                                                      "result.", parent=self)
        box.setDetailedText(tb)
        box.show()
        self._jobs.append(box)

    def submit(self, fn, on_done, on_fail):
        job = Job(fn, on_done, on_fail)
        self._jobs.append(job)
        self.pool.start(job)

    def _mode(self, m):
        self.mode = m
        self.engine_changed({"ok": True, "input": None})

    def _tab(self, i):
        if i == 2 and not self.pages[2]._scene_built:
            self.pages[2].build_scene()
        if i == 4 and not self.pages[4]._built:
            self.pages[4].build()
        self.pages[i].refresh()

    def set_option(self, kind, oid):
        ev = self.engine.set_option(kind, oid)
        if not ev.get("ok"):
            QMessageBox.information(self, "Not compatible", ev.get("reason", ""))
            self.pages[0].refresh()
        return ev

    def set_param(self, name, value):
        return self.engine.set_param(name, value)

    def engine_changed(self, ev: dict):
        for i, p in enumerate(self.pages):
            if i in (2, 4) and not (getattr(p, "_scene_built", False) or getattr(p, "_built", False)):
                continue
            if i == self.tabs.currentIndex() or i in (0, 5) or ev.get("input"):
                try:
                    p.refresh(ev if ev.get("input") else None)
                except Exception:
                    traceback.print_exc()
        rs = AP.readiness_summary(self.engine, self.measured_by_req())
        n_fail = len(rs["failed"])
        stale = sum(1 for nid in self.engine.heavy if self.engine.heavy_status(nid)["status"] == STALE)
        self.badge.setText(f"  Readiness: {rs['stage']} · {n_fail} failed · {stale} stale analyses "
                           f"· config {self.engine.model.config_hash()} ")
        self.badge.setStyleSheet(f"color: {'#AD2A1F' if n_fail else '#1A6E3A'}; font-weight: 600")

    def select_component(self, key: Optional[str], from_page: Optional[int] = None):
        self.selected = key if key in self.engine.model.components else None
        self.pages[0].on_select(self.selected)
        self.pages[1].refresh()
        if self.pages[4]._built and from_page != 4:
            m = self.engine.model
            self.pages[4].highlight = set(m.components[key].bodies) if self.selected else set()
            self.pages[4].redraw()
        if self.selected:
            self.status(f"Selected: {self.engine.model.components[key].name}")

    def relevant_results(self, key: str) -> Optional[set]:
        """Results a component influences: through its mass (mass node and downstream), its cost, and the
        catalog slot it occupies."""
        c = self.engine.model.components[key]
        nodes = {"mass", "cost"}
        for kind in C.KINDS:
            o = self.engine.model.option(kind)
            if c.key == o.bom_component:
                nodes.add(kind)
        out = set()
        for n in nodes:
            for d in [n] + self.engine.downstream(n):
                out |= set(self.engine.graph[d].produces)
        return out

    def component_html(self, key: Optional[str]) -> str:
        m = self.engine.model
        if not key:
            r = self.engine.results
            return (f"<h3>K09-L (whole aircraft)</h3><p>{len(m.active_bodies())} bodies, "
                    f"{len(m.active_components())} BOM lines</p><p>Mass {_fmt(r['mass_total'], 'mass_total')}<br>"
                    f"CG {_fmt(r['cg'])}<br>Inertia {_fmt(r['inertia'])}</p><p><b>Unresolved inputs</b><ul>"
                    + "".join(f"<li><b>{html.escape(a)}</b>: {html.escape(b)}</li>" for a, b in m.unresolved) + "</ul>")
        c = m.components[key]
        q = m.effective_qty(c)
        mass, price = m.component_mass(c), m.component_price(c)
        bodies = [m.bodies[i] for i in c.bodies]
        P = np.vstack([m.body_mesh(b)[0] for b in bodies]) if bodies else np.zeros((1, 3))
        ext = np.ptp(P, axis=0)
        cen = P.mean(0)
        slot = next((k for k in C.KINDS
                     if m.option(k).bom_component == c.key), None)
        opt = ""
        if slot:
            o = m.option(slot)
            opt = "<p><b>Catalog option (" + slot + "):</b> " + html.escape(o.name) + "<br>" + "<br>".join(
                f"{html.escape(k)}: {html.escape(v.fmt(4))} <small style='color:{CLASS_COL.get(v.source)}'>"
                f"[{SHORT.get(v.source)}] {html.escape(v.note)}</small>" for k, v in o.data.items()) + "</p>"
        link = f"<a href='{html.escape(c.url)}'>product page</a>" if c.url else ""
        return (f"<h3>{html.escape(c.name)}</h3><p><code>{html.escape(c.pn)}</code> · {c.group} · revision {c.revision}"
                f" · validation: {c.validation}</p><table>"
                f"<tr><td>Quantity</td><td>{q}{'' if q == c.qty else f' (BOM {c.qty})'}</td></tr>"
                f"<tr><td>Unit mass</td><td>{mass.fmt(4, 1000, 'g')} <small>[{SHORT.get(mass.source)}]</small></td></tr>"
                f"<tr><td>Unit price</td><td>{price.fmt(4)} <small>[{SHORT.get(price.source)}] "
                f"{html.escape(c.price_note)}</small></td></tr>"
                f"<tr><td>Vendor</td><td>{html.escape(c.vendor)} {link}</td></tr>"
                f"<tr><td>Process</td><td>{html.escape(c.process)}</td></tr><tr><td>Material</td>"
                f"<td>{html.escape(c.material or 'see vendor')}</td></tr>"
                f"<tr><td>Bodies</td><td>{len(bodies)}</td></tr><tr><td>Extent (mm)</td>"
                f"<td>{ext[0]:.1f} × {ext[1]:.1f} × {ext[2]:.1f}</td></tr><tr><td>Centre (mm)</td>"
                f"<td>{cen[0]:.1f}, {cen[1]:.1f}, {cen[2]:.1f} (aircraft frame)</td></tr></table>{opt}"
                f"<p><small>Frames: aircraft x aft, y right, z up (cruise); hover = nose up.</small></p>")

    def _search(self):
        t = self.search.text().strip().lower()
        if not t:
            return
        hit = next((k for k, c in self.engine.model.components.items() if t in (c.name + " " + c.pn).lower()), None)
        if hit:
            self.tabs.setCurrentIndex(0)
            self.select_component(hit)
        else:
            self.status(f"No component matches '{t}'")

    def goto_requirement(self, rid: str):
        r = next(x for x in self.engine.model.requirements if x.id == rid)
        node = self.engine.node_of_result(r.metric)
        self.tabs.setCurrentIndex(1)
        pg = self.pages[1]
        if r.metric in pg.keys:
            pg.table.selectRow(pg.keys.index(r.metric))
        elif node:
            self.status(self.engine.explain(node))

    # ------------------------------------------------------------------ heavy jobs
    def run_fea(self, which="motor"):
        from opencae.engineering import services
        comp = services.MOTOR_TUBE if which == "motor" else services.SPAR_TUBE
        self.status(f"FEA running on {comp.split(' — ')[0]} (CalculiX)…")
        self.submit(lambda: services.run_fea(self.engine, comp), lambda rec: (
            self.status(f"FEA done in {rec['seconds']} s"), self.engine_changed({"ok": True, "input": None})),
            lambda tb: (self.error("FEA", tb), self.engine_changed({"ok": True, "input": None})))

    def analyse_selected(self):
        key = self.selected
        if not key:
            QMessageBox.information(self, "Component analysis", "Select a structural component first (page 1).")
            return
        from opencae.engineering import component_analysis as CA
        try:
            ctx = CA.analysis_context(self.engine.model, key)
        except Exception as exc:
            QMessageBox.information(self, "Component analysis", f"No analysis rule for this component yet:\n{exc}")
            return
        pub = CA.public_context(ctx)
        txt = "\n".join(f"- {c.get('name', c.get('component', ''))}: {c.get('role', '')} - {c.get('reason', '')}"
                        for c in pub.get("components", []))
        if QMessageBox.question(self, "Component analysis", f"Analysis model for {key.split(' — ')[0]}:\n{txt}\n\n"
                                "Run it with CalculiX now?") == QMessageBox.Yes:
            from opencae.engineering import services
            self.submit(lambda: services.run_fea(self.engine, key), lambda rec: (
                self.status(f"FEA done in {rec['seconds']} s"), self.engine_changed({"ok": True, "input": None})),
                lambda tb: (self.error("FEA", tb), self.engine_changed({"ok": True, "input": None})))

    def fea_html(self, nid: str) -> str:
        rec = (self.engine.heavy.get(nid) or {}).get("record") or {}
        res = rec.get("result")
        if not res:
            return "<p>No FEA result yet.</p>"
        sf = res.get("safety_factor", {})
        return (f"<h4>FEA record</h4><p>Solver: CalculiX (C3D10), {res['mesh'].get('n_elements', '?')} elements, "
                f"{res['mesh'].get('dof')} DOF; converged: {res['converged']}; equilibrium "
                f"{html.escape(json.dumps(res.get('equilibrium'), default=str)[:200])}</p><p>Max von Mises "
                f"{res['max_von_mises_nodal_MPa']:.2f} MPa, max displacement {res['max_displacement_mm']:.3f} mm; "
                f"safety factor {sf.get('value', 0):.3g} [{SHORT.get(sf.get('source'))}]</p><p>Material: "
                f"{html.escape(res['material']['name'])} ({res['material']['class']})</p><p>Assumptions: "
                + "; ".join(html.escape(a) for a in res.get("assumptions", [])[:6]) +
                f"</p><p>Config {rec.get('config_hash')} · geometry {rec.get('geometry_hash')} · {rec.get('time')}"
                f" · log {html.escape(res.get('solver_log', ''))}</p><p><i>{html.escape(res.get('note', ''))}</i></p>")

    def run_assembly_check(self):
        from opencae.engineering import services
        self.status("Assembly check running…")

        def done(rec):
            self.status(f"Assembly check done in {rec['seconds']} s")
            if self.pages[4]._built:
                self.pages[4].show_check(rec)
            self.engine_changed({"ok": True, "input": None})

        self.submit(lambda: services.run_assembly_check(self.engine), done, lambda tb: self.error("Assembly check", tb))

    def run_flight(self, scenario="hover"):
        self.tabs.setCurrentIndex(2)
        pg = self.pages[2]
        pg.scen.setCurrentText(scenario)
        pg.run()

    # ------------------------------------------------------------------ measurements / revisions / persistence
    def measurements_changed(self, cmp_rows):
        self.measure_cmp = cmp_rows
        self.engine_changed({"ok": True, "input": None})

    def measured_by_req(self) -> dict:
        """Map requirement id -> measurement record from the verification comparison (physical validation)."""
        out = {}
        for c in self.measure_cmp or []:
            rid = c.get("requirement_id")
            if c.get("measured") is None or not rid:
                continue
            req = next((r for r in self.engine.model.requirements if r.id == rid), None)
            if req is not None and c.get("quantity") == req.metric:
                out[rid] = {"value": c["measured"], "test_id": c.get("test_id")}
        return out

    def _snapshot(self) -> dict:
        r = self.engine.results
        return {k: r[k].value for k in ("mass_total", "tw_low", "cost_total", "endurance_hover") if r.get(k)}

    def save_revision(self):
        note, ok = QInputDialog.getText(self, "Save revision", "Note")
        if not ok:
            return
        rev = self.engine.model.save_revision(note or "", self._snapshot())
        self.rev_combo.addItem(f"{rev['id']} {rev['note']}", rev["id"])
        self.pages[5].refresh()

    def _restore_rev(self, i):
        rid = self.rev_combo.itemData(i)
        if not rid:
            return
        self.engine.restore_revision(rid)
        self.status(f"Revision {self.rev_combo.itemText(i)} restored")

    def open_revision_b(self):
        """Load the bundled revision-B state (revisions A and B with their solver runs); if it is missing,
        apply the revision-B changes to the current engine (heavy analyses then show as not run)."""
        from opencae.engineering import revision_b as RB
        if RB.STATE_FILE.exists():
            self.load_state(RB.STATE_FILE)
            rid = next((r["id"] for r in reversed(self.engine.model.revisions) if r["note"].startswith("Revision B")),
                       None)
            if rid:
                self.rev_combo.setCurrentIndex(max(self.rev_combo.findData(rid), 0))
            self.status("Revision B loaded (bundled state with its solver runs); switch revisions in the toolbar")
        else:
            RB.apply(self.engine)
            self.status("Revision B applied; heavy analyses have not been run for it")

    def save_state(self, path=None):
        path = path or self.state_path or QFileDialog.getSaveFileName(self, "Save state", "k09l_state.json",
                                                                      "JSON (*.json)")[0]
        if path:
            self.engine.save_state(Path(path))
            self.state_path = Path(path)
            self.status(f"Saved {path}")

    def load_state(self, path=None):
        path = path or QFileDialog.getOpenFileName(self, "Load state", "", "JSON (*.json)")[0]
        if not path:
            return
        m = self.engine.model
        self.engine.load_state(Path(path))  # records stay current only where their input hashes match
        for f in self.engine.listeners:  # refresh every page for the loaded parameters and records
            f({"ok": True, "change": f"state loaded from {Path(path).name}", "input": "landing", "affected": [],
               "delta": {}, "stale": [], "config_hash": m.config_hash()})
        self.rev_combo.clear()
        for r in m.revisions:
            self.rev_combo.addItem(f"{r['id']} {r['note']}", r["id"])

    def export_report(self, path=None):
        from opencae.engineering import report
        path = path or QFileDialog.getSaveFileName(self, "Readiness report", "k09l_readiness.html", "HTML (*.html)")[0]
        if path:
            report.write_report(self.engine, Path(path), self.measured_by_req(), self.pages[5].decisions)
            self.status(f"Report written: {path}")
        return path


def main(argv=None) -> int:
    import sys
    from opencae.ui.theme import apply_theme
    app = QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    try:
        apply_theme(app)
    except Exception:
        pass
    w = PlatformWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
