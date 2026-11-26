"""Contour plots of the solver-backed field analyses, component by component.

``ContourPanel`` lists every contour analysis of the current design (``study.FIELD_NODES``: motor-tube FEA,
spar FEA, outrigger-foot FEA, motor-mount thermal, cruise CFD) with its status for the loaded inputs. Only a
record that is CURRENT for these inputs is drawn; a stale or missing one is reported as "out of date" or
"not run" and nothing is plotted for it. Beside the 3D view: the failure parameter, load case, solver and the
mesh profile (every key of the record's ``mesh`` dict; histogram-like entries are drawn as small bar charts).
"""
from __future__ import annotations

import html
import json
from typing import Optional

import numpy as np
import pyvista as pv
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QPushButton, QScrollArea, QSplitter, QTextBrowser, QVBoxLayout, QWidget)

from opencae.engineering import field_analyses as FA
from opencae.engineering.graph import CURRENT, FAILED, NOT_RUN, STALE

FIELD_NODES = ("fea_motor_tube", "fea_spar", "fea_foot", "thermal_mount", "cfd_cruise")
try:  # the study module is the source of truth; keep a copy so the panel imports without it
    from opencae.engineering.study import FIELD_NODES  # noqa: F811
except Exception:  # pragma: no cover
    pass

# fallbacks for analyses that have no record yet (title, discipline, BOM component)
DEFAULTS = {
    "fea_motor_tube": ("Motor tube FEA", "structural", "Motor tube CF 16x13.08x380 — DragonPlate FDPT.625*.515*24"),
    "fea_spar": ("Wing spar FEA", "structural", "Spar tube CF 12x10x560 — Superstition CFT3K12101"),
    "fea_foot": ("Outrigger foot FEA", "structural", None),  # the landing option's BOM component
    "thermal_mount": ("Motor mount thermal", "thermal", "Motor mount PETG elbow — K09L-PROP-MNT-01"),
    "cfd_cruise": ("Cruise CFD", "fluid", "Wing core XPS — K09L-AF-WING-CORE"),
}
CMAPS = {"structural": "turbo", "thermal": "YlOrRd", "fluid": "coolwarm"}
STATUS_TEXT = {CURRENT: "current", STALE: "out of date", NOT_RUN: "not run", FAILED: "failed"}
STATUS_COL = {CURRENT: "#1A6E3A", STALE: "#8A5600", NOT_RUN: "#6B7280", FAILED: "#AD2A1F"}
NO_ANALYSIS = "No contour analysis for this component"


def screening_ids(engine) -> list[str]:
    """'cs:<component key>' for every component of the current component screening that has a contour."""
    if "component_screening" not in engine.heavy:
        return []
    h = engine.heavy_status("component_screening")
    if h["status"] != CURRENT:
        return []
    comps = (h.get("record") or {}).get("components") or {}
    return ["cs:" + k for k, c in sorted(comps.items()) if (c or {}).get("field")]


def field_summary(engine, nid: str) -> tuple[dict, Optional[dict], dict]:
    """(status dict, field summary or None, record) for one field analysis, as the web build reads it.
    ``nid`` is a heavy node id, or 'cs:<component key>' for a component screening contour."""
    if nid.startswith("cs:"):
        h = engine.heavy_status("component_screening") if "component_screening" in engine.heavy else {"status": NOT_RUN}
        rec = h.get("record") or {}
        c = ((rec.get("components") or {}).get(nid[3:]) or {})
        s = c.get("field")
        if s and not s.get("part"):
            s = dict(s, part=nid[3:])
        return h, (s if s and s.get("field_name") else None), rec
    if nid not in engine.heavy:
        return {"status": NOT_RUN, "reason": "not in this engine"}, None, {}
    h = engine.heavy_status(nid)
    rec = h.get("record") or {}
    s = rec.get("field") if nid in ("fea_motor_tube", "fea_spar") else rec
    if not s or not s.get("field_name"):
        s = None
    return h, s, rec


def is_histogram(key: str, v) -> bool:
    """A mesh-profile entry that reads as a histogram: a dict with counts, or a numeric list that is named
    like one or long enough to be one."""
    if isinstance(v, dict):
        return isinstance(v.get("counts"), (list, tuple)) and len(v["counts"]) > 0
    if isinstance(v, (list, tuple)) and v:
        if all(isinstance(x, (list, tuple)) and len(x) == 2 and isinstance(x[1], (int, float)) for x in v):
            return True  # [[label, count], ...]
        if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
            return "hist" in key.lower() or len(v) >= 5
    return False


def histogram_bars(v) -> tuple[list[str], list[float]]:
    """Labels and heights of a histogram entry in any of the accepted shapes."""
    if isinstance(v, dict):
        counts = [float(c) for c in v["counts"]]
        edges = v.get("edges") or v.get("bins") or v.get("bin_edges")
        labels = v.get("labels")
        if labels and len(labels) == len(counts):
            return [str(x) for x in labels], counts
        if edges and len(edges) == len(counts) + 1:
            return [f"{float(a):.3g}-{float(b):.3g}" for a, b in zip(edges[:-1], edges[1:])], counts
        if edges and len(edges) == len(counts):
            return [f"{float(a):.3g}" for a in edges], counts
        return [str(i) for i in range(len(counts))], counts
    if all(isinstance(x, (list, tuple)) for x in v):
        return [str(x[0]) for x in v], [float(x[1]) for x in v]
    return [str(i) for i in range(len(v))], [float(x) for x in v]


def _flat(d: dict, prefix="") -> list[tuple[str, object]]:
    out = []
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and not is_histogram(key, v):
            out += _flat(v, key + ".")
        else:
            out.append((key, v))
    return out


def _num(v, digits=4) -> str:
    if isinstance(v, bool) or v is None:
        return str(v)
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:.{digits}g}"
    return str(v)


class ContourPanel(QWidget):
    """Analysis list + component selector, a pyvista contour view and the failure / mesh panel."""

    def __init__(self, parent=None, engine=None):
        super().__init__(parent)
        self.e = None
        self.plotter = None  # created on first draw (keeps hidden pages free of GL contexts)
        self.current: Optional[str] = None
        self._drawn_key = None
        self._mesh: Optional[pv.PolyData] = None
        self._summary: Optional[dict] = None
        self._hist_canvases: list = []
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        split = QSplitter(Qt.Horizontal)
        lay.addWidget(split)
        # left: component selector + analysis list + toggles
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("<b>Component</b>"))
        self.comp = QComboBox()
        self.comp.setMinimumContentsLength(18)
        self.comp.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.comp.activated.connect(lambda _i: self.select_component(self.comp.currentData()))
        ll.addWidget(self.comp)
        self.comp_note = QLabel()
        self.comp_note.setWordWrap(True)
        ll.addWidget(self.comp_note)
        ll.addWidget(QLabel("<b>Contour analyses</b>"))
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._row)
        ll.addWidget(self.list, 1)
        self.crit = QCheckBox("Critical region only")
        self.crit.setToolTip("Grey out the surface outside the critical region")
        self.crit.toggled.connect(lambda _b: self._draw(force=True, keep_camera=True))
        self.edges = QCheckBox("Show mesh")
        self.edges.setToolTip("Show the edges of the surface mesh")
        self.edges.toggled.connect(lambda _b: self._draw(force=True, keep_camera=True))
        self.qty = QComboBox()
        self.qty.setToolTip("Quantity to colour the surface with")
        self.qty.currentIndexChanged.connect(lambda _i: self._draw(force=True, keep_camera=True))
        self.deform = QCheckBox("Deformed shape (exaggerated)")
        self.deform.setToolTip("Draw the structure displaced by its nodal displacements, scaled so the largest is "
                               "6 % of the part size")
        self.deform.toggled.connect(lambda _b: self._draw(force=True, keep_camera=True))
        ll.addWidget(QLabel("Show"))
        ll.addWidget(self.qty)
        ll.addWidget(self.crit)
        ll.addWidget(self.edges)
        ll.addWidget(self.deform)
        b = QPushButton("Reset view")
        b.clicked.connect(self.reset_view)
        ll.addWidget(b)
        left.setMaximumWidth(300)
        split.addWidget(left)
        # centre: 3D view
        self.view_box = QWidget()
        self.view_lay = QVBoxLayout(self.view_box)
        self.view_lay.setContentsMargins(0, 0, 0, 0)
        self.header = QLabel()
        self.header.setWordWrap(True)
        self.view_lay.addWidget(self.header)
        self.placeholder = QLabel("Select a contour analysis")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet("color:#6B7280; font-size:14px")
        self.view_lay.addWidget(self.placeholder, 1)
        split.addWidget(self.view_box)
        # right: failure parameter, load case, solver, mesh profile
        right = QScrollArea()
        right.setWidgetResizable(True)
        rw = QWidget()
        self.right_lay = QVBoxLayout(rw)
        self.info = QTextBrowser()
        self.info.setOpenExternalLinks(False)
        self.info.setMinimumHeight(260)
        self.right_lay.addWidget(self.info, 2)
        self.mesh_box = QGroupBox("Mesh profile")
        self.mesh_lay = QVBoxLayout(self.mesh_box)
        self.mesh_text = QLabel()
        self.mesh_text.setWordWrap(True)
        self.mesh_text.setTextFormat(Qt.RichText)
        self.mesh_lay.addWidget(self.mesh_text)
        self.right_lay.addWidget(self.mesh_box)
        self.right_lay.addStretch()
        right.setWidget(rw)
        right.setMinimumWidth(320)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([260, 760, 380])
        if engine is not None:
            self.set_engine(engine)

    # ------------------------------------------------------------------ data
    def set_engine(self, engine) -> None:
        self.e = engine
        self._drawn_key = None
        self.refresh()

    def all_ids(self) -> list[str]:
        return list(FIELD_NODES) + (screening_ids(self.e) if self.e is not None else [])

    def part_of(self, nid: str) -> Optional[str]:
        _h, s, _rec = field_summary(self.e, nid)
        if s and s.get("part"):
            return s["part"]
        part = DEFAULTS.get(nid, (None, None, None))[2]
        if nid == "fea_foot":
            try:
                part = self.e.model.option("landing").bom_component
            except Exception:
                part = None
        return part

    def analysis_for(self, comp_key: Optional[str]) -> Optional[str]:
        if not comp_key:
            return None
        return next((nid for nid in self.all_ids() if self.part_of(nid) == comp_key), None)

    def entries(self) -> list[dict]:
        """One row per field analysis: id, title, status, reason, summary (None unless current + loadable)."""
        out = []
        for nid in self.all_ids():
            h, s, rec = field_summary(self.e, nid)
            st = h["status"]
            title = (s or {}).get("title") or DEFAULTS.get(nid, (nid,))[0]
            reason = h.get("reason") or ""
            if st == CURRENT and s is None:
                st, reason = NOT_RUN, "the current run recorded no contour field"
            out.append({"id": nid, "title": title, "status": st, "reason": reason,
                        "summary": s if st == CURRENT else None, "record": rec})
        return out

    # ------------------------------------------------------------------ UI
    def refresh(self, ev=None) -> None:
        if self.e is None:
            return
        rows = self.entries()
        keep = self.current
        self.list.blockSignals(True)
        self.list.clear()
        for r in rows:
            fs = r["summary"].get("failure") if r["summary"] else None
            extra = ""
            if fs:
                if r.get("id") == "cfd_cruise" and fs.get("safety_factor") is not None:
                    extra = f"  lift {fs['safety_factor'] * 100:.0f} % of required"
                elif fs.get("safety_factor") is not None:
                    extra = f"  SF {fs['safety_factor']:.3g}"
                elif fs.get("margin") is not None:
                    extra = f"  margin {fs['margin']:.3g}"
                extra += f" ({fs.get('status', '?')})"
            it = QListWidgetItem(f"{r['title']}\n● {STATUS_TEXT.get(r['status'], r['status'])}{extra}")
            it.setData(Qt.UserRole, r["id"])
            it.setForeground(QBrush(QColor(STATUS_COL.get(r["status"], "#000"))))
            tip = r["reason"] or ("current for the loaded design" if r["status"] == CURRENT else "")
            it.setToolTip(f"{r['id']}: {STATUS_TEXT.get(r['status'], r['status'])}" + (f" - {tip}" if tip else ""))
            self.list.addItem(it)
        self.list.blockSignals(False)
        self._fill_components()
        ids = [r["id"] for r in rows]
        if keep in ids:
            self.list.setCurrentRow(ids.index(keep))  # signal blocked above, so redraw explicitly
            self._show(keep)
        elif rows:
            first = next((r["id"] for r in rows if r["status"] == CURRENT), ids[0])
            self.list.setCurrentRow(ids.index(first))

    def _fill_components(self):
        cur = self.comp.currentData()
        self.comp.blockSignals(True)
        self.comp.clear()
        self.comp.addItem("(choose a BOM component)", None)
        with_field = {self.part_of(n) for n in self.all_ids()}
        for k, c in sorted(self.e.model.components.items(), key=lambda kv: kv[1].name.lower()):
            self.comp.addItem(("◆ " if k in with_field else "    ") + c.name, k)
        i = self.comp.findData(cur)
        self.comp.setCurrentIndex(max(i, 0))
        self.comp.blockSignals(False)

    def select_component(self, key: Optional[str]) -> Optional[str]:
        """Jump to the contour analysis of a BOM component; returns its id or None."""
        i = self.comp.findData(key)
        if i >= 0 and self.comp.currentIndex() != i:
            self.comp.blockSignals(True)
            self.comp.setCurrentIndex(i)
            self.comp.blockSignals(False)
        if not key:
            self.comp_note.setText("")
            return None
        nid = self.analysis_for(key)
        if nid is None:
            self.comp_note.setText(f"<span style='color:#8A5600'>{NO_ANALYSIS}</span>")
            return None
        self.comp_note.setText(f"Contour analysis: <b>{html.escape(nid)}</b>")
        self.select(nid)
        return nid

    def select(self, nid: str) -> None:
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == nid:
                if self.list.currentRow() == i:
                    self._show(nid)
                else:
                    self.list.setCurrentRow(i)
                return

    def _row(self, r: int):
        if r < 0:
            return
        self._show(self.list.item(r).data(Qt.UserRole))

    def _ensure_plotter(self):
        if self.plotter is not None:
            return
        from opencae.ui.viewport import Interactor  # never keeps a stale GL surface wrapper (see there)
        self.plotter = Interactor(self.view_box, auto_update=False)
        self.plotter.set_background("#ffffff", top="#dfe5ec")
        try:
            self.plotter.add_axes(line_width=2, color="black", xlabel="x aft", ylabel="y right", zlabel="z up")
        except Exception:
            pass
        self.view_lay.replaceWidget(self.placeholder, self.plotter.interactor)
        self.placeholder.hide()

    def _clear_view(self, msg: str):
        self._mesh, self._summary, self._drawn_key = None, None, None
        if self.plotter is not None:
            self.plotter.clear_actors()
            if self.plotter.scalar_bars:
                self.plotter.remove_scalar_bar()
            self.plotter.add_text(msg, position="upper_left", font_size=10, color="#444444", name="msg")
            self.plotter.render()
        else:
            self.placeholder.setText(msg)

    def _show(self, nid: str):
        self.current = nid
        row = next(r for r in self.entries() if r["id"] == nid)
        st = row["status"]
        col = STATUS_COL.get(st, "#000")
        self.header.setText(f"<b>{html.escape(row['title'])}</b> &nbsp;<span style='color:{col}'>● "
                            f"{STATUS_TEXT.get(st, st)}</span> <small>{html.escape(row['reason'])}</small>")
        s = row["summary"]
        if s is None:
            why = {STALE: "Out of date: the inputs changed since this analysis ran. Re-run it for this design.",
                   FAILED: "The last run failed; no result is recorded.",
                   NOT_RUN: "Not run for this design: no result to show."}.get(st, "No result to show.")
            if row["reason"]:
                why += f"\n({row['reason']})"
            self.info.setHtml(f"<h3>{html.escape(row['title'])}</h3><p style='color:{col}'><b>"
                              f"{STATUS_TEXT.get(st, st)}</b></p><p>{html.escape(why)}</p>")
            self._set_mesh_profile(None)
            self._clear_view(why.split("\n")[0])
            return
        arr = FA.load_field(s)
        if arr is None:
            msg = f"The field file {s.get('field_name')} of this run is missing; nothing to plot."
            self.info.setHtml(f"<h3>{html.escape(row['title'])}</h3><p>{html.escape(msg)}</p>")
            self._set_mesh_profile(s.get("mesh"))
            self._clear_view(msg)
            return
        self._summary = s
        self._arr = arr
        self.qty.blockSignals(True)
        self.qty.clear()
        self.qty.addItem(f"{s.get('quantity', 'value')} [{s.get('unit', '')}]", "primary")
        for fd in s.get("fields") or []:
            if fd.get("key") in (arr.get("fields") or {}):
                self.qty.addItem(f"{fd.get('quantity', fd['key'])} [{fd.get('unit', '')}]", fd["key"])
        self.qty.blockSignals(False)
        self.deform.setEnabled(arr.get("disp") is not None)
        self.info.setHtml(self.info_html(s, row["record"]))
        self._set_mesh_profile(s.get("mesh"))
        self._draw(key=(nid, row["record"].get("node_hash"), row["record"].get("time")))

    # ------------------------------------------------------------------ rendering
    def _draw(self, key=None, force=False, keep_camera=False):
        s = self._summary
        if s is None:
            return
        key = key or self._drawn_key
        qk = self.qty.currentData() or "primary"
        state = (key, self.crit.isChecked(), self.edges.isChecked(), qk, self.deform.isChecked())
        if not force and state == getattr(self, "_drawn_state", None):
            return
        new_field = key != self._drawn_key
        self._ensure_plotter()
        p = self.plotter
        arr = self._arr
        pts = np.asarray(arr["points_mm"], float)
        tri = np.asarray(arr["tri"], np.int64)
        crit = np.asarray(arr["critical"], bool)
        disc = s.get("discipline", "structural")
        fdef = next((f for f in s.get("fields") or [] if f.get("key") == qk), None) if qk != "primary" else None
        if fdef is not None:
            vals = np.asarray(arr["fields"][qk], float)
            lo, hi = fdef.get("range") or [float(np.nanmin(vals)), float(np.nanmax(vals))]
        else:
            vals = np.asarray(arr["values"], float)
            lo, hi = (s.get("range") or [float(vals.min()), float(vals.max())])
        if self.deform.isChecked() and arr.get("disp") is not None:
            d = np.asarray(arr["disp"], float)
            dm = float(np.linalg.norm(d, axis=1).max()) or 1.0
            pts = pts + d * (0.06 * float(np.linalg.norm(pts.max(0) - pts.min(0))) / dm)
        mesh = pv.PolyData(pts, np.hstack([np.full((len(tri), 1), 3), tri]).ravel())
        if fdef is not None:
            m = max(abs(lo), abs(hi)) or 1.0
            clim = [-m, m] if fdef.get("symmetric") else ([lo, hi] if hi > lo else [lo, lo + 1e-9])
        elif disc == "fluid":
            m = max(abs(lo), abs(hi)) or 1.0
            clim = [-m, m]
        else:
            clim = [lo, hi] if hi > lo else [lo, lo + 1e-9]
        v = vals.copy()
        if self.crit.isChecked() and fdef is None:
            v[~crit] = np.nan
        title = (f"{fdef.get('quantity', qk)} [{fdef.get('unit', '')}]" if fdef is not None
                 else f"{s.get('quantity', 'value')} [{s.get('unit', '')}]")
        mesh.point_data[title] = v
        cam = p.camera_position if (keep_camera and not new_field and self._drawn_key) else None
        p.clear_actors()
        if p.scalar_bars:
            p.remove_scalar_bar()
        cmap = (fdef.get("cmap") or "turbo") if fdef is not None else CMAPS.get(disc, "turbo")
        cmap = {"ylorrd": "YlOrRd"}.get(cmap, cmap)
        p.add_mesh(mesh, scalars=title, cmap=cmap, clim=clim, nan_color="#c8c8c8",
                   show_edges=self.edges.isChecked(), edge_color="#303030", line_width=0.5,
                   smooth_shading=not self.edges.isChecked(), name="contour", reset_camera=False,
                   scalar_bar_args={"title": title, "vertical": True, "position_x": 0.9, "position_y": 0.12,
                                    "height": 0.72, "width": 0.045, "color": "black", "fmt": "%.3g",
                                    "title_font_size": 12, "label_font_size": 11, "n_labels": 5})
        w = s.get("worst_location_mm")
        if w is not None:
            diag = float(np.linalg.norm(pts.max(0) - pts.min(0))) or 1.0
            p.add_mesh(pv.Sphere(radius=diag * 0.012, center=w), color="#ff00ff", name="worst", reset_camera=False)
            plo, phi = s.get("range") or [lo, hi]  # the worst point belongs to the primary field
            wv = plo if (s.get("critical") or {}).get("higher_is_worse", True) is False else phi
            p.add_point_labels(np.asarray([w], float), [f"worst {wv:.3g} {'' if s.get('unit') in ('-', None) else s['unit']}".strip()], name="worst_label",
                               font_size=12, point_size=1, text_color="black", shape_color="#ffffffcc",
                               always_visible=True, reset_camera=False)
        self._mesh = mesh
        if cam is not None:
            p.camera_position = cam
        else:
            p.reset_camera()
        p.render()
        self._drawn_key = key
        self._drawn_state = state

    def reset_view(self):
        if self.plotter is not None:
            self.plotter.view_isometric()
            self.plotter.reset_camera()
            self.plotter.render()

    # ------------------------------------------------------------------ text panels
    def info_html(self, s: dict, rec: dict) -> str:
        f = s.get("failure") or {}
        st = str(f.get("status", "?"))
        col = {"ok": "#1A6E3A", "fail": "#AD2A1F"}.get(st, "#8A5600")
        u = f.get("unit", "")
        rows = [("Criterion", html.escape(str(f.get("criterion", "-")))),
                ("Value", f"{_num(f.get('value'))} {html.escape(u)}"),
                ("Allowable", f"{_num(f.get('allowable'))} {html.escape(u)}")]
        if f.get("safety_factor") is not None:
            rows.append(("Safety factor", f"<b>{_num(f['safety_factor'], 3)}</b>"
                         + (f" (line {_num(f.get('line'))})" if f.get("line") is not None else "")))
        if f.get("margin") is not None:
            rows.append(("Margin", f"<b>{_num(f['margin'], 3)}</b>"
                         + (f" (line {_num(f.get('line'))})" if f.get("line") is not None else "")))
        rows.append(("Status", f"<b style='color:{col}'>{html.escape(st)}</b>"))
        for k, v in f.items():
            if k not in ("criterion", "value", "allowable", "unit", "safety_factor", "margin", "status", "line",
                         "text") and not isinstance(v, (dict, list)):
                rows.append((html.escape(k), _num(v)))
        tbl = "".join(f"<tr><td style='color:#555;padding-right:8px'>{a}</td><td>{b}</td></tr>" for a, b in rows)
        sol = s.get("solver") or {}
        srows = []
        for k, v in sol.items():
            if isinstance(v, dict):
                if k == "equilibrium":
                    fr, mr = v.get("force_residual_pct"), v.get("moment_residual_pct")
                    srows.append((k, f"force residual {_num(fr, 2)} %, moment residual {_num(mr, 2)} %"))
                else:
                    srows.append((k, html.escape(json.dumps(v, default=str)[:160])))
            elif isinstance(v, list):
                srows.append((k, f"{len(v)} entries"))
            else:
                srows.append((k, html.escape(_num(v))))
        stbl = "".join(f"<tr><td style='color:#555;padding-right:8px'>{html.escape(a)}</td><td>{b}</td></tr>"
                       for a, b in srows)
        crit = (s.get("critical") or {}).get("text", "")
        rng = s.get("range") or [None, None]
        return (f"<h3 style='margin-bottom:2px'>{html.escape(s.get('title', ''))}</h3>"
                f"<p style='margin-top:0'><small>{html.escape(s.get('part', ''))}</small></p>"
                f"<h4>Failure parameter</h4><table>{tbl}</table>"
                f"<p>{html.escape(str(f.get('text', '')))}</p>"
                f"<h4>Load case</h4><p>{html.escape(str(s.get('load_case', '-')))}</p>"
                f"<h4>Field</h4><p>{html.escape(s.get('quantity', ''))}: {_num(rng[0])} … {_num(rng[1])} "
                f"{html.escape(s.get('unit', ''))}<br>Critical region: {html.escape(crit)}</p>"
                f"<h4>Solver</h4><table>{stbl}</table>"
                f"<p><small>Run {html.escape(str(rec.get('time') or s.get('time') or '?'))} · config "
                f"{html.escape(str(rec.get('config_hash', '?')))} · current for the loaded design</small></p>")

    def _set_mesh_profile(self, mesh: Optional[dict]):
        for c in self._hist_canvases:
            self.mesh_lay.removeWidget(c)
            c.setParent(None)
            c.deleteLater()
        self._hist_canvases = []
        if not mesh:
            self.mesh_text.setText("<i>No mesh profile recorded.</i>")
            return
        rows, hists = [], []
        for k, v in _flat(mesh):
            if is_histogram(k, v):
                hists.append((k, v))
                rows.append((k, "histogram below"))
            elif isinstance(v, (list, tuple)):
                rows.append((k, ", ".join(_num(x) for x in v)))
            else:
                rows.append((k, _num(v)))
        self.mesh_text.setText("<table>" + "".join(
            f"<tr><td style='color:#555;padding-right:8px'>{html.escape(a)}</td><td>{html.escape(b)}</td></tr>"
            for a, b in rows) + "</table>")
        for k, v in hists:
            self._hist_canvases.append(self._hist_canvas(k, v))
            self.mesh_lay.addWidget(self._hist_canvases[-1])

    def _hist_canvas(self, key: str, v):
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        labels, counts = histogram_bars(v)
        fig = Figure(figsize=(3.4, 1.7), tight_layout=True)
        ax = fig.add_subplot(1, 1, 1)
        ax.bar(range(len(counts)), counts, color="#2f5d8a", width=0.85)
        step = max(1, len(labels) // 6)
        ax.set_xticks(range(0, len(labels), step))
        ax.set_xticklabels(labels[::step], fontsize=6, rotation=30, ha="right")
        ax.tick_params(axis="y", labelsize=6)
        ax.set_title(key, fontsize=8)
        c = FigureCanvasQTAgg(fig)
        c.setMinimumHeight(150)
        c.setObjectName(f"hist:{key}")
        return c

    # ------------------------------------------------------------------ lifetime
    def close_plotter(self):
        if self.plotter is not None:
            try:
                self.plotter.close()
            except Exception:
                pass
            self.plotter = None

    def closeEvent(self, ev):
        self.close_plotter()
        super().closeEvent(ev)
