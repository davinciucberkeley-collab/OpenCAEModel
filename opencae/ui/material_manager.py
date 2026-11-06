"""Material Manager: project materials, the sourced database and the user's library.

Everything shown comes from ``opencae.materials.database`` (values with sources, scatter and
the list of estimated properties) or from the user's own entries. Assigning or editing a
material goes through ``MainWindow.mutate`` (undo, and the setup hash makes affected results
out of date)."""
from __future__ import annotations

import copy
import html
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QSplitter, QTableWidget, QTableWidgetItem, QTextBrowser, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from opencae.materials import database as db
from opencae.materials.library import load_user_library, save_user_library
from opencae.project.model import new_id

ROLE = Qt.UserRole


def material_html(m, us) -> str:
    e = html.escape

    def val(x, f=1.0, unit="", fmt="{:.4g}"):
        return "—" if x is None else fmt.format(x * f) + (f" {unit}" if unit else "")
    rows = [("Category", e(m.category or "—")),
            ("Young's modulus", val(m.youngs_modulus, us.stress_from_si, us.stress)),
            ("Poisson's ratio", val(m.poissons_ratio)),
            ("Density", val(m.density, 1.0, "kg/m³")),
            ("Strength for safety factors", val(m.yield_strength, us.stress_from_si, us.stress)),
            ("Thermal expansion", val(m.thermal_expansion, 1e6, "×10⁻⁶/K")),
            ("Thermal conductivity", val(m.thermal_conductivity, 1.0, "W/(m·K)")),
            ("Specific heat", val(m.specific_heat, 1.0, "J/(kg·K)"))]
    out = ["<h3>" + e(m.name) + "</h3>"]
    if m.is_example:
        out.append("<p style='color:#c05621'><b>Example values</b> — verify before engineering use.</p>")
    out.append("<table cellpadding=3>" + "".join(f"<tr><td>{a}</td><td><b>{b}</b></td></tr>" for a, b in rows)
               + "</table>")
    if m.strengths:
        out.append("<p><b>Strengths by print orientation</b></p><table cellpadding=3>" + "".join(
            f"<tr><td>{e(k.replace('_', ' '))}</td><td>{v * us.stress_from_si:.4g} {us.stress}</td></tr>"
            for k, v in m.strengths.items()) + "</table>")
    if m.orthotropic:
        o = m.orthotropic
        out.append("<p><b>Orthotropic (print frame: 1, 2 in the layer plane, 3 = build direction)</b></p>"
                   "<table cellpadding=3>" + "".join(
                       f"<tr><td>{k}</td><td>{(o[k] * us.stress_from_si if k[0] in 'EG' else o[k]):.4g}"
                       f"{' ' + us.stress if k[0] in 'EG' else ''}</td></tr>" for k in
                       ("E1", "E2", "E3", "G12", "G13", "G23", "nu12", "nu13", "nu23")) + "</table>")
    if m.uncertainty:
        out.append("<p><b>Reported scatter (1σ, relative)</b>: " + ", ".join(
            f"{e(k)} {100 * v:.1f} %" for k, v in m.uncertainty.items()) + "</p>")
    if m.estimated:
        out.append("<p style='color:#c05621'><b>Not from the source (estimated)</b>: " +
                   e(", ".join(m.estimated)) + "</p>")
    if m.temperature_table:
        out.append("<p><b>Temperature table</b>: " + ", ".join(
            f"{r['T']:g} °C: E {r['E'] * us.stress_from_si:.4g}" for r in m.temperature_table) + "</p>")
    out.append("<p><b>Source</b>: " + (e(m.source) if m.source else "—") + "</p>")
    if m.notes:
        out.append("<p><b>Notes</b>: " + e(m.notes) + "</p>")
    return "".join(out)


class CompareDialog(QDialog):
    def __init__(self, mats, us, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Compare materials")
        self.resize(900, 520)
        lay = QVBoxLayout(self)
        rows = db.compare(mats)
        tab = QTableWidget(len(rows) - 1, len(mats) + 1)
        tab.setHorizontalHeaderLabels(["Property"] + [m.name for m in mats])
        for r, row in enumerate(rows[1:]):
            for c, v in enumerate(row):
                txt = "—" if v is None else (f"{v:.4g}" if isinstance(v, float) else str(v))
                it = QTableWidgetItem(txt)
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                tab.setItem(r, c, it)
        tab.resizeColumnsToContents()
        lay.addWidget(tab)
        self.table = tab
        note = QLabel("Values in SI-based units as labelled. Printed-polymer data are typical values of the "
                      "manufacturers' test prints; real parts depend on printer, settings and orientation.")
        note.setWordWrap(True)
        note.setObjectName("hint")
        lay.addWidget(note)


class MaterialManager(QDialog):
    def __init__(self, win, parent=None):
        super().__init__(parent or win)
        self.win = win
        self.setWindowTitle("Material Manager")
        self.resize(1100, 680)
        lay = QVBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Material", "Category", "E", "ρ [kg/m³]"])
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self._show)
        split.addWidget(self.tree)
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        split.addWidget(self.detail)
        split.setSizes([520, 580])
        lay.addWidget(split, 1)
        row = QHBoxLayout()
        self.buttons = {}
        for key, text, fn in (("add", "Add to project", self.add_to_project),
                              ("assign", "Assign to body…", self.assign),
                              ("new", "New…", self.new), ("edit", "Edit…", self.edit),
                              ("compare", "Compare selected", self.compare),
                              ("import", "Import…", self.import_file), ("export", "Export selected…", self.export),
                              ("library", "Save to my library", self.save_library)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
            self.buttons[key] = b
        lay.addLayout(row)
        self.refresh()

    # ------------------------------------------------------------------ data
    @property
    def project(self):
        return self.win.session.project

    def refresh(self) -> None:
        us = self.win.us
        self.tree.clear()
        self._items = {}
        groups = (("project", "Project materials", self.project.materials),
                  ("db", "Database (sourced datasheets and examples)", db.database_materials()),
                  ("library", "My library", load_user_library()))
        for key, label, mats in groups:
            g = QTreeWidgetItem([label])
            g.setFlags(g.flags() & ~Qt.ItemIsSelectable)
            self.tree.addTopLevelItem(g)
            for m in mats:
                it = QTreeWidgetItem([m.name, m.category or "", f"{m.youngs_modulus * us.stress_from_si:.4g} {us.stress}",
                                      f"{m.density:.0f}" if m.density else ""])
                it.setData(0, ROLE, (key, m.id))
                g.addChild(it)
                self._items[(key, m.id)] = m
            g.setExpanded(True)
        for c in range(4):
            self.tree.resizeColumnToContents(c)

    def selected(self) -> list[tuple[str, object]]:
        out = []
        for it in self.tree.selectedItems():
            d = it.data(0, ROLE)
            if d:
                out.append((d[0], self._items[d]))
        return out

    def _show(self) -> None:
        sel = self.selected()
        self.detail.setHtml(material_html(sel[0][1], self.win.us) if sel else "")

    def select(self, group: str, material_id: str) -> None:
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            for j in range(g.childCount()):
                if g.child(j).data(0, ROLE) == (group, material_id):
                    self.tree.setCurrentItem(g.child(j))
                    return

    # ------------------------------------------------------------------ actions
    def _copy_into_project(self, m):
        p = self.project
        if any(x.id == m.id for x in p.materials):
            return next(x for x in p.materials if x.id == m.id)
        c = copy.deepcopy(m)
        self.win.mutate(f"Add material {c.name}", lambda: p.materials.append(c), regen=False)
        return c

    def add_to_project(self) -> Optional[list]:
        added = [self._copy_into_project(m) for g, m in self.selected() if g != "project"]
        self.refresh()
        return added

    def assign(self, body_id: str = "") -> None:
        sel = self.selected()
        if len(sel) != 1:
            QMessageBox.information(self, "Assign", "Select one material.")
            return
        m = sel[0][1] if sel[0][0] == "project" else self._copy_into_project(sel[0][1])
        self.refresh()
        self.win.cmd_assign_material(body_id, preset_material=m.id)

    def new(self) -> None:
        self.win.cmd_new_material()
        self.refresh()

    def edit(self) -> None:
        sel = self.selected()
        if len(sel) != 1 or sel[0][0] != "project":
            QMessageBox.information(self, "Edit", "Select one project material (database entries are read-only: "
                                                  "add them to the project first).")
            return
        self.win.cmd_edit_material(sel[0][1].id)
        self.refresh()

    def compare(self) -> Optional[CompareDialog]:
        mats = [m for _, m in self.selected()]
        if len(mats) < 2:
            QMessageBox.information(self, "Compare", "Select two or more materials (Ctrl+click).")
            return None
        dlg = CompareDialog(mats, self.win.us, self)
        dlg.show()
        return dlg

    def import_file(self, path: str = "") -> list:
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Import materials", "", "Materials (*.json *.csv)")
        if not path:
            return []
        try:
            mats = db.import_csv(path) if path.lower().endswith(".csv") else db.import_json(path)
        except (ValueError, OSError, KeyError) as exc:
            QMessageBox.warning(self, "Import materials", str(exc))
            return []
        p = self.project
        for m in mats:
            if any(x.id == m.id for x in p.materials):
                m.id = new_id("mat")
        self.win.mutate(f"Import {len(mats)} material(s)", lambda: p.materials.extend(mats), regen=False)
        self.refresh()
        return mats

    def export(self, path: str = ""):
        mats = [m for _, m in self.selected()]
        if not mats:
            return None
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "Export materials", "materials.json",
                                                  "JSON (*.json);;CSV (*.csv)")
        if not path:
            return None
        return db.export_csv(mats, path) if path.lower().endswith(".csv") else db.export_json(mats, path)

    def save_library(self) -> None:
        lib = load_user_library()
        ids = {m.id for m in lib}
        for _, m in self.selected():
            if m.id not in ids:
                lib.append(copy.deepcopy(m))
        save_user_library(lib)
        self.refresh()
