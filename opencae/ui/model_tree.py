"""Hierarchical project tree (Geometry / Mesh / Materials / Analysis / Results)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import QAbstractItemView, QMenu, QTreeWidget, QTreeWidgetItem

from opencae.ui import theme
from opencae.ui.icons import icon

ROLE = Qt.UserRole + 1
MAX_TOPO_ITEMS = 400

RENAMABLE = {"feature", "body", "material", "constraint", "load", "refinement", "interaction"}


def study_status(project, st) -> str:
    """Short status of any study (not only the active one)."""
    if st.results.completed:
        return "solved" if project.results_are_current(st) else "solved, out of date"
    if st.mesh.file:
        return "meshed" if project.mesh_is_current(st) else "mesh out of date"
    if st.constraints or st.loads:
        return "set up"
    return "new"
DELETABLE = {"feature", "material", "constraint", "load", "refinement", "interaction"}
SUPPRESSIBLE = {"feature", "constraint", "load", "interaction"}

FEATURE_ICONS = {"box": "box", "cylinder": "cylinder", "sphere": "sphere", "cone": "cone", "plate": "plate",
                 "beam": "beam", "cantilever_beam": "cantilever"}
OP_ICONS = {"union": "union", "subtract": "subtract", "intersect": "intersect"}


class ModelTree(QTreeWidget):
    selected = Signal(str, str)  # kind, id
    action = Signal(str, str, str)  # action, kind, id
    visibility = Signal(str, bool)  # body id, visible

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setAlternatingRowColors(False)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.itemSelectionChanged.connect(self._sel)
        self.itemChanged.connect(self._changed)
        self.itemDoubleClicked.connect(self._double)
        self.setEditTriggers(QAbstractItemView.EditKeyPressed)
        self._building = False
        self._expanded: set[str] = set()

    # ------------------------------------------------------------------ build
    def _item(self, parent, text: str, kind: str, ident: str = "", ico: str = "", tip: str = "",
              color: Optional[str] = None) -> QTreeWidgetItem:
        it = QTreeWidgetItem(parent, [text])
        it.setData(0, ROLE, (kind, ident))
        if ico:
            it.setIcon(0, icon(ico))
        if tip:
            it.setToolTip(0, tip)
        if color:
            it.setForeground(0, QColor(color))
        if kind in RENAMABLE:
            it.setFlags(it.flags() | Qt.ItemIsEditable)
        return it

    def rebuild(self, session) -> None:
        self._building = True
        # remember expansion state
        def walk(item):
            for i in range(item.childCount()):
                c = item.child(i)
                k = c.data(0, ROLE)
                if c.isExpanded() and k:
                    self._expanded.add(repr(k))
                elif k:
                    self._expanded.discard(repr(k))
                walk(c)
        if self.topLevelItemCount():
            walk(self.invisibleRootItem())
        sel = self.currentItem().data(0, ROLE) if self.currentItem() else None
        self.clear()
        p = session.project
        us = session.units
        root = self._item(self, f"{p.name}  ({us.key})", "project", "", "app")
        # geometry
        g = self._item(root, "Geometry", "geometry", "", "box")
        parts = self._item(g, f"Parts / feature history ({len(p.geometry.features)})", "parts", "", "transform")
        for f in p.geometry.features:
            ico = FEATURE_ICONS.get(f.params.get("shape", ""), OP_ICONS.get(f.params.get("op", ""), f.kind))
            if f.kind == "import":
                ico = "import"
            err = session.regen.errors.get(f.id)
            label = f.name + ("  [suppressed]" if f.suppressed else "") + ("  ⚠" if err else "")
            self._item(parts, label, "feature", f.id, ico, err or f.kind,
                       theme.ERR if err else (theme.FG_DIM if f.suppressed else None))
        bodies = self._item(g, f"Bodies ({len(session.regen.order)})", "bodies", "", "box")
        hidden = set(p.geometry.hidden_bodies)
        for bid in session.regen.order:
            b = session.bodies[bid]
            tag = " (STL, tessellated)" if b.kind == "tessellated" else ""
            mark = "  ● in active study" if bid in p.study.analysis_body_ids() else ""
            bi = self._item(bodies, f"{b.name}{tag}{mark}", "body", bid, "box",
                            "; ".join(b.check.issues) if b.check and b.check.issues else "valid solid",
                            theme.WARN if b.check and b.check.issues else None)
            bi.setFlags(bi.flags() | Qt.ItemIsUserCheckable)
            bi.setCheckState(0, Qt.Unchecked if bid in hidden else Qt.Checked)
            if b.kind == "solid":
                fi = self._item(bi, f"Faces ({len(b.faces)})", "faces", bid, "select_face")
                for f in b.faces[:MAX_TOPO_ITEMS]:
                    n = "" if f.normal is None else f", n=({f.normal[0]:+.2f},{f.normal[1]:+.2f},{f.normal[2]:+.2f})"
                    self._item(fi, f"Face {f.index + 1}: {f.surface_type}, A={f.area:.4g}{n}", "face",
                               f"{bid}:{f.index}", "select_face")
                ei = self._item(bi, f"Edges ({len(b.edges)})", "edges", bid, "select_edge")
                for e in b.edges[:MAX_TOPO_ITEMS]:
                    self._item(ei, f"Edge {e.index + 1}: {e.curve_type}, L={e.length:.4g}", "edge",
                               f"{bid}:{e.index}", "select_edge")
                vi = self._item(bi, f"Vertices ({len(b.vertices)})", "vertices", bid, "select_vertex")
                for v in b.vertices[:MAX_TOPO_ITEMS]:
                    x, y, z = v.point
                    self._item(vi, f"Vertex {v.index + 1}: ({x:.4g}, {y:.4g}, {z:.4g})", "vertex",
                               f"{bid}:{v.index}", "select_vertex")
        # materials (library shared by all studies)
        mats = self._item(root, f"Materials ({len(p.materials)})", "materials", "", "material")
        assigned = {a.material for st in p.studies for a in st.assignments}
        for mt in p.materials:
            self._item(mats, mt.name + ("  ✓ used" if mt.id in assigned else ""), "material", mt.id, "material",
                       mt.notes)
        # studies
        from opencae.project.model import ANALYSIS_TYPES
        sts = self._item(root, f"Studies ({len(p.studies)})", "studies", "", "solve",
                         "Each study has its own bodies, materials, mesh, conditions, settings and results. "
                         "Right-click for new / duplicate / delete; double-click to activate.")
        for st in p.studies:
            active = st.id == p.active_study
            status = study_status(p, st)
            label = f"{'▶ ' if active else ''}{st.name}  [{ANALYSIS_TYPES.get(st.analysis.analysis_type, '?')}] " \
                    f"— {status}"
            col = None if active else theme.FG_DIM
            if "out of date" in status:
                col = theme.WARN
            si = self._item(sts, label, "study", st.id, "solve", "active study" if active else
                            "double-click to make this the active study", col)
            if active:
                self._study_children(si, session)
                self.expandItem(si)
        self.expandItem(root)
        for i in range(root.childCount()):
            self.expandItem(root.child(i))
        def restore(item):
            for i in range(item.childCount()):
                c = item.child(i)
                if repr(c.data(0, ROLE)) in self._expanded:
                    self.expandItem(c)
                if sel and c.data(0, ROLE) == sel:
                    self.setCurrentItem(c)
                restore(c)
        restore(self.invisibleRootItem())
        self._building = False

    def _study_children(self, si, session) -> None:
        p = session.project
        st = p.study
        # bodies of the study
        ids = st.analysis_body_ids()
        bl = self._item(si, f"Bodies in study ({len(ids)})", "study_bodies", st.id, "box",
                        "Right-click > Choose bodies… (several bodies are meshed together and bonded where they "
                        "touch)")
        for bid in ids:
            b = session.bodies.get(bid)
            mat = p.material_for_body(bid)
            self._item(bl, f"{b.name if b else bid + ' (missing)'} — {mat.name if mat else 'no material'}",
                       "study_body", bid, "material" if mat else "box", color=None if (b and mat) else theme.WARN)
        # mesh
        m = self._item(si, "Mesh", "mesh", "", "mesh")
        mc = self._item(m, "Mesh controls", "mesh_settings", "", "mesh_settings")
        for r in p.mesh_settings.refinements:
            self._item(mc, f"{r.name} (h={r.size:g})", "refinement", r.id, "refine")
        mesh = session.mesh
        stale = mesh is not None and not p.mesh_is_current()
        self._item(m, f"Nodes ({mesh.n_nodes if mesh else 0})", "nodes", "", "select_node")
        self._item(m, f"Elements ({mesh.n_elements if mesh else 0} {mesh.element_type if mesh else ''})" +
                   ("  [out of date]" if stale else ""), "elements", "", "select_element",
                   color=theme.WARN if stale else None)
        ns = self._item(m, "Named selections", "named_selections", "", "select_face")
        for c in p.constraints + p.loads:
            if c.targets:
                self._item(ns, f"{c.name}: " + ", ".join(t.label() for t in c.targets[:6]) +
                           (" …" if len(c.targets) > 6 else ""), "named_selection", c.id, "select_face")
        # physics
        thermal = p.analysis.analysis_type in ("thermal_steady", "thermal_transient")
        flow = p.analysis.analysis_type.startswith("cfd")
        cs = self._item(si, f"{'Thermal conditions' if thermal else 'Walls' if flow else 'Supports'} "
                            f"({len(p.constraints)})", "constraints", "", "fixed")
        for c in p.constraints:
            self._item(cs, c.name + ("  [suppressed]" if c.suppressed else ""), "constraint", c.id, c.kind,
                       color=theme.FG_DIM if c.suppressed else None)
        ls = self._item(si, f"{'Heat loads' if thermal else 'Inlets / outlets' if flow else 'Loads'} "
                            f"({len(p.loads)})", "loads", "", "force")
        for l in p.loads:
            self._item(ls, l.name + ("  [suppressed]" if l.suppressed else ""), "load", l.id, l.kind,
                       color=theme.FG_DIM if l.suppressed else None)
        inter = list(getattr(p, "interactions", []) or [])
        if inter or p.mesh_settings.assembly_mesh == "separate":
            ig = self._item(si, f"Interactions ({len(inter)})", "interactions", "", "symmetry")
            for it in inter:
                self._item(ig, f"{it.name} ({it.kind})" + ("  [suppressed]" if it.suppressed else ""),
                           "interaction", it.id, "fixed" if it.kind == "bonded" else "symmetry",
                           color=theme.FG_DIM if it.suppressed else None)
        self._item(si, f"Analysis settings: {p.analysis.title}", "solver", "", "settings")
        # results
        res = self._item(si, "Results" + ("" if session.results is not None else " (none)"), "results", "", "contour",
                         color=None if session.results is not None else theme.FG_DIM)
        if session.results is not None:
            rs = session.results
            groups: dict[str, list[str]] = {}
            names = {"displacement": "Displacement", "stress": "Stress", "strain": "Strain", "temperature": "Temperature",
                     "heat_flux": "Heat flux", "temperature_gradient": "Temperature gradient", "mode_shape": "Mode shapes",
                     "velocity": "Velocity", "pressure_si": "Pressure", "viscosity": "Turbulence"}
            for key in rs.available():
                groups.setdefault(rs.info(key).unit_kind, []).append(key)
            for uk, keys in groups.items():
                gi = self._item(res, names.get(uk, uk), "result_group", uk, "contour")
                for key in keys:
                    self._item(gi, rs.info(key).label, "result", key, "contour")
            for k, lab in enumerate(rs.frames()[:200]):
                self._item(res, lab, "result_frame", str(k), "contour")

    # ------------------------------------------------------------------ signals
    def _sel(self) -> None:
        if self._building:
            return
        it = self.currentItem()
        if it is None:
            return
        kind, ident = it.data(0, ROLE)
        self.selected.emit(kind, ident)

    def _changed(self, item: QTreeWidgetItem, col: int) -> None:
        if self._building:
            return
        kind, ident = item.data(0, ROLE)
        if kind == "body" and item.flags() & Qt.ItemIsUserCheckable:
            self.visibility.emit(ident, item.checkState(0) == Qt.Checked)
        if kind in RENAMABLE:
            text = item.text(0).split("  ")[0].strip()
            self.action.emit("rename:" + text, kind, ident)

    def _double(self, item, _col) -> None:
        kind, _ = item.data(0, ROLE)
        self._emit_action("activate" if kind == "study" else "edit", item)

    def _emit_action(self, act: str, item: QTreeWidgetItem) -> None:
        if item is None:
            return
        kind, ident = item.data(0, ROLE)
        self.action.emit(act, kind, ident)

    def _menu(self, pos) -> None:
        it = self.itemAt(pos)
        if it is None:
            return
        kind, ident = it.data(0, ROLE)
        menu = QMenu(self)

        def add(text, act, ico=""):
            a = QAction(icon(ico) if ico else icon("settings"), text, menu)
            a.triggered.connect(lambda: self.action.emit(act, kind, ident))
            menu.addAction(a)

        if kind in ("feature", "constraint", "load", "material", "refinement", "mesh_settings", "solver",
                    "interaction"):
            add("Edit…", "edit", "settings")
        if kind == "interactions":
            add("Detect touching faces…", "detect_interactions", "inspect")
        if kind in RENAMABLE:
            a = QAction("Rename", menu)
            a.triggered.connect(lambda: self.editItem(it, 0))
            menu.addAction(a)
        if kind == "body":
            add("Use as the only body of the active study", "set_analysis", "solve")
            add("Add to / remove from active study", "toggle_study_body", "box")
            add("Inspect / check geometry", "inspect", "inspect")
            add("Export body…", "export", "export")
            st = it.checkState(0) == Qt.Checked
            add("Hide" if st else "Show", "toggle_visible", "view_iso")
            if session_is_stl(it):
                add("Convert STL to solid", "stl_to_solid", "check")
        if kind in SUPPRESSIBLE:
            add("Suppress / unsuppress", "suppress", "stop")
        if kind == "material":
            add("Assign to analysis body", "assign", "material")
        if kind in ("face", "edge", "vertex"):
            add("Select in viewport", "select", "select_" + kind)
        if kind == "mesh":
            add("Generate mesh", "generate", "mesh")
        if kind == "studies":
            add("New study…", "new_study", "solve")
            add("Studies dashboard", "dashboard", "inspect")
        if kind == "study":
            add("Activate", "activate", "solve")
            add("Duplicate (copy setup)", "duplicate_study", "transform")
            add("New study…", "new_study", "solve")
            add("Rename…", "rename_study", "settings")
            add("Choose bodies…", "study_bodies", "box")
            menu.addSeparator()
            add("Delete study", "delete_study", "delete")
        if kind == "study_bodies":
            add("Choose bodies…", "study_bodies", "box")
        if kind == "study_body":
            add("Assign material…", "assign_body_material", "material")
        if kind == "results":
            add("Export report…", "report", "report")
        if kind in DELETABLE:
            menu.addSeparator()
            add("Delete", "delete", "delete")
        if not menu.isEmpty():
            menu.exec(self.viewport().mapToGlobal(pos))


def session_is_stl(item: QTreeWidgetItem) -> bool:
    return "(STL" in item.text(0)
