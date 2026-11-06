"""Intelligent Mesh Manager dialog: geometry analysis, presets, plan, budget, defeaturing and
automatic mesh convergence for the active study."""
from __future__ import annotations

import html
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QTextBrowser, QVBoxLayout)

from opencae.geometry.analysis import analyse_body
from opencae.meshing import intelligent as im
from opencae.meshing import simplify


def physics_of(analysis_type: str) -> str:
    if analysis_type.startswith("cfd"):
        return "cfd"
    if analysis_type.startswith("thermal_") and analysis_type != "thermal_stress":
        return "thermal"
    if analysis_type in ("modal", "buckling"):
        return "modal"
    return "structural"


class MeshManager(QDialog):
    def __init__(self, win, parent=None):
        super().__init__(parent or win)
        self.win = win
        self.setWindowTitle("Intelligent Mesh Manager")
        self.resize(900, 720)
        s = win.session
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.preset = QComboBox()
        for k in ("quick", "balanced", "validation"):
            self.preset.addItem(im.PRESET_TEXT[k], k)
        self.preset.setCurrentIndex(1)
        form.addRow("Preset", self.preset)
        self.order = QComboBox()
        self.order.addItem("Quadratic (recommended for stresses)", 2)
        self.order.addItem("Linear", 1)
        form.addRow("Element order", self.order)
        b = im.default_budget()
        self.budget = b
        form.addRow("Resource budget", QLabel(f"solver memory ≤ {b['memory_mb']:,.0f} MB (half of this computer's "
                                              f"RAM), ≤ {b['seconds'] / 60:.0f} min per solve"))
        lay.addLayout(form)
        self.text = QTextBrowser()
        lay.addWidget(self.text, 1)
        row = QHBoxLayout()
        for text, fn in (("Apply plan", self.apply), ("Apply and generate mesh", self.apply_and_mesh),
                         ("Simplify geometry…", self.simplify_dialog),
                         ("Automatic convergence…", self.auto_convergence)):
            btn = QPushButton(text)
            btn.clicked.connect(fn)
            row.addWidget(btn)
        lay.addLayout(row)
        self.physics = physics_of(s.project.analysis.analysis_type)
        if self.physics == "cfd":
            self.order.setCurrentIndex(1)
            self.order.setEnabled(False)
        self.reports = {}
        for body in s.analysis_bodies():
            if body.kind == "solid":
                self.reports[body.id] = analyse_body(body)
        self.preset.currentIndexChanged.connect(self.update_plan)
        self.order.currentIndexChanged.connect(self.update_plan)
        self.update_plan()

    # ------------------------------------------------------------------ plan
    def _flow(self, body_id) -> Optional[dict]:
        s = self.win.session
        a = s.project.analysis
        speeds = [float(l.params.get("magnitude", 0.0)) for l in s.project.loads if l.kind == "velocity_inlet"
                  and not l.suppressed]
        walls = s.flow_wall_faces(body_id)
        b = s.bodies[body_id]
        pts = [b.faces[i].bbox for i in walls]
        length = float(np.linalg.norm(np.max([p[1] for p in pts], axis=0) - np.min([p[0] for p in pts], axis=0))) \
            if pts else b.size
        return {"speed": max(speeds) if speeds else None, "nu": a.fluid_viscosity / a.fluid_density,
                "length": length, "turbulent": a.flow_model != "laminar"}

    def plans(self) -> dict:
        s = self.win.session
        out = {}
        for bid, rep in self.reports.items():
            kw = {"body": s.bodies[bid], "order": self.order.currentData(),
                  "length_to_m": 1.0 / s.units.length_from_si if hasattr(s.units, "length_from_si") else 1e-3}
            if self.physics == "cfd":
                kw["wall_faces"] = s.flow_wall_faces(bid)
                kw["flow"] = self._flow(bid)
            else:
                kw["bc_faces"] = im.bc_face_indices(s, bid)
            out[bid] = im.plan_mesh(rep, self.preset.currentData(), self.physics, **kw)
        return out

    def update_plan(self) -> None:
        s = self.win.session
        e = html.escape
        parts = []
        self._plans = self.plans()
        total = {"elements": 0, "mem": 0.0, "sec": 0.0}
        for bid, rep in self.reports.items():
            pl = self._plans[bid]
            parts.append(f"<h3>{e(s.bodies[bid].name)}</h3><p><b>Geometry</b></p><ul>" +
                         "".join(f"<li>{e(x)}</li>" for x in rep.summary()) + "</ul><p><b>Mesh plan</b></p><ul>" +
                         "".join(f"<li>{e(x)}</li>" for x in pl.describe()) + "</ul>")
            total["elements"] += pl.estimated_elements
            total["mem"] += pl.estimated_memory_mb
            total["sec"] += pl.estimated_seconds
        warn = []
        for pl in self._plans.values():
            warn += im.check_budget(pl, self.budget)
        if warn:
            parts.append("<p style='color:#c53030'><b>Over budget:</b><br>" + "<br>".join(e(w) for w in warn) + "</p>")
        if not self.reports:
            parts.append("<p>No solid bodies in this study: use the normal mesh settings.</p>")
        self.text.setHtml("".join(parts))
        self.warnings = warn

    # ------------------------------------------------------------------ actions
    def apply(self) -> None:
        s = self.win.session
        plans = self._plans

        def fn():
            first = True
            for bid, pl in plans.items():
                if first:
                    im.apply_plan(s, pl, bid)
                    first = False
                else:  # several bodies: keep the smallest global size, add every body's refinements
                    ms = s.project.mesh_settings
                    keep = list(ms.refinements)
                    gs, mn = ms.size_max, ms.size_min
                    im.apply_plan(s, pl, bid)
                    ms.refinements = keep + [r for r in ms.refinements if r not in keep]
                    ms.size_max, ms.size_min = min(gs, pl.global_size), min(mn, pl.min_size)
        self.win.mutate(f"Apply {self.preset.currentData()} mesh plan", fn, regen=False)

    def apply_and_mesh(self) -> None:
        if self.warnings and QMessageBox.question(
                self, "Mesh Manager", "The plan exceeds the resource budget:\n\n" + "\n".join(self.warnings) +
                "\n\nGenerate anyway?") != QMessageBox.Yes:
            return
        self.apply()
        self.win.cmd_generate_mesh()

    def simplify_dialog(self, accept: bool = False):
        s = self.win.session
        bid = next(iter(self.reports), None)
        if bid is None:
            return None
        protected = set(im.bc_face_indices(s, bid))
        cands = simplify.candidates(self.reports[bid], protected)
        if not cands:
            QMessageBox.information(self, "Simplify geometry", "No small cosmetic features found (holes, concave "
                                                               "fillets, thin walls and faces with conditions are "
                                                               "never proposed).")
            return None
        dlg = QDialog(self)
        dlg.setWindowTitle("Simplify geometry for analysis")
        lay = QVBoxLayout(dlg)
        lab = QLabel("Tick the faces to remove. The original geometry stays in the feature history; suppress the "
                     "'Simplify' feature to restore it. Holes, concave fillets, thin walls and faces with loads, "
                     "supports or interfaces are never proposed.")
        lab.setWordWrap(True)
        lay.addWidget(lab)
        lst = QListWidget()
        for c in cands:
            it = QListWidgetItem(f"face {c.face + 1}: {c.reason}")
            it.setData(Qt.UserRole, c.face)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            lst.addItem(it)
        lay.addWidget(lst)
        ok = QPushButton("Remove ticked faces")
        ok.clicked.connect(dlg.accept)
        lay.addWidget(ok)
        if not accept and not dlg.exec():
            return None
        faces = [lst.item(i).data(Qt.UserRole) for i in range(lst.count()) if lst.item(i).checkState() == Qt.Checked]
        reasons = [c.reason for c in cands if c.face in faces]
        result = {}

        def fn():
            result.update(simplify.defeature(s, bid, faces, reasons))
        try:
            self.win.mutate("Simplify geometry", fn, regen=False)
        except ValueError as exc:
            QMessageBox.warning(self, "Simplify geometry", str(exc))
            return None
        if result:
            QMessageBox.information(self, "Simplify geometry",
                                    f"Removed faces {result['faces_removed']}: volume change "
                                    f"{100 * result['volume_change']:+.4f} %, faces {result['faces_before']} → "
                                    f"{result['faces_after']}. Suppress the feature to restore the original.")
            self.reports[bid] = analyse_body(s.bodies[bid])
            self.update_plan()
        return result

    def default_monitors(self) -> list[dict]:
        rs_kind = self.physics
        if rs_kind == "cfd":
            return [{"kind": "force", "key": "wall_force_N", "component": 0, "name": "Wall force x [N]"}]
        if rs_kind == "thermal":
            return [{"kind": "summary", "key": "max_temperature", "name": "Max temperature"}]
        if rs_kind == "modal":
            return [{"kind": "summary", "key": "frequencies_hz", "name": "f1 [Hz]"}]
        return [{"kind": "summary", "key": "max_displacement", "name": "Max displacement"}]

    def auto_convergence(self, monitors: Optional[list[dict]] = None, tol: float = 0.01, max_levels: int = 4,
                         confirm: bool = True) -> bool:
        from opencae.postprocessing.convergence import check_monitors
        mons = monitors or self.default_monitors()
        if self.physics == "modal":  # vector summary: first frequency
            mons = [dict(m, kind="force", key="frequencies_hz", component=0) if m["key"] == "frequencies_hz" else m
                    for m in mons]
        warn = check_monitors(mons)
        if confirm and QMessageBox.question(
                self, "Automatic convergence",
                "A copy of the study is meshed and solved repeatedly (all sizes / 1.5 per level) until the monitored "
                f"values change by less than {100 * tol:g} % (at least 3 levels), up to {max_levels} levels or the "
                "resource budget. Monitored: " + ", ".join(m["name"] for m in mons) + "." +
                ("\n\n" + "\n".join(warn) if warn else "") + "\n\nStart?") != QMessageBox.Yes:
            return False
        self.apply()
        self.win.start_convergence_job({"auto": True, "monitors": mons, "tol": tol, "max_levels": max_levels,
                                        "budget": self.budget})
        return True
