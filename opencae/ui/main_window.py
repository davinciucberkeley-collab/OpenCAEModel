"""OpenCAE Studio main window: menus, ribbon, docks and command handlers.

Model/view separation: all engineering state lives in :class:`Session`
(no Qt). This window translates user actions into Session calls, records
undo snapshots, and refreshes the views. Meshing and solving run in separate
processes (QProcess) so the UI never blocks and both can be cancelled.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PySide6.QtCore import QProcess, QProcessEnvironment, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QIcon, QKeySequence
from PySide6.QtWidgets import (QApplication, QColorDialog, QDockWidget, QFileDialog, QInputDialog, QLabel,
                               QMainWindow, QMessageBox, QTabWidget, QToolBar, QWidget)

from opencae import __version__
from opencae.app.session import Session
from opencae.boundary_conditions.resolve import ERROR, Issue
from opencae.geometry.references import make_ref
from opencae.materials.library import EXAMPLE_WARNING, custom_material
from opencae.meshing.mesh_data import FEMesh
from opencae.postprocessing.results import QUANTITIES
from opencae.project import io as pio
from opencae.project.model import Constraint, Feature, Load, MeshRefinement
from opencae.project.undo import UndoStack
from opencae.solver.calculix.runner import INSTALL_HELP, analyse_output, command_line, solver_env
from opencae.ui import dialogs as D
from opencae.ui import theme
from opencae.ui.forms import Field, ParamDialog
from opencae.ui.icons import icon
from opencae.ui.model_tree import ModelTree
from opencae.ui.panels import (ConsolePanel, IssuesPanel, MeshDiagnostics, PropertiesPanel, QtLogHandler,
                               ResultsPanel, SolverPanel, SummaryPanel, WorkflowBar)
from opencae.ui.viewport import Pick, Viewport
from opencae.utilities.logging_setup import get_logger
from opencae.visualization.scene import auto_deformation_scale

log = get_logger("ui")

CAD_FILTER = "CAD files (*.step *.stp *.iges *.igs *.brep *.stl);;STEP (*.step *.stp);;IGES (*.iges *.igs);;BREP (*.brep);;STL (*.stl)"
MESH_STAGES = {"Importing": 5, "Mapping": 10, "curves": 20, "surfaces": 35, "volume": 60, "second order": 80,
               "Extracting": 88, "quality": 94, "Done": 100}
SOLVER_STAGES = {"Decascading": 10, "Determining the structure": 20, "number of equations": 30,
                 "Factoring": 50, "stress calculation": 85, "Job finished": 100}


class MainWindow(QMainWindow):
    def __init__(self, session: Optional[Session] = None, settings: Optional[QSettings] = None):
        super().__init__()
        self.settings = settings or QSettings("OpenCAE", "OpenCAE Studio")
        ccx = self.settings.value("ccx_path", "", str) or None
        self.session = session or Session(ccx_path=ccx)
        if session is None:
            self.session.new()
        self.undo = UndoStack()
        self.dirty = False
        self.selection: list[Pick] = []
        self.view_mode = "geometry"  # geometry | mesh | results
        self.mesh_color = "none"
        self.proc: Optional[QProcess] = None
        self.proc_kind = ""
        self._proc_buf = ""
        self._job: dict = {}
        self._current_result = "S_vm"
        self.setWindowTitle("OpenCAE Studio")
        self.setWindowIcon(icon("app"))
        self.resize(1560, 960)
        self.setDockOptions(QMainWindow.AnimatedDocks | QMainWindow.AllowTabbedDocks | QMainWindow.AllowNestedDocks)

        self.viewport = Viewport(self)
        self.setCentralWidget(self.viewport)
        self._build_docks()
        self._build_actions()
        self._build_menus()
        self._build_ribbon()
        self._connect()
        self.statusBar().showMessage("Ready")
        self.units_label = QLabel()
        self.statusBar().addPermanentWidget(self.units_label)
        self.solver_label = QLabel()
        self.statusBar().addPermanentWidget(self.solver_label)
        self.refresh_all(fit=True)
        if not self.session.backend.executable():
            QTimer.singleShot(400, lambda: self.console.append(INSTALL_HELP, logging.WARNING))

    # ================================================================== construction
    def _dock(self, title: str, widget: QWidget, area, name: str) -> QDockWidget:
        d = QDockWidget(title, self)
        d.setObjectName(name)
        d.setWidget(widget)
        d.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable | QDockWidget.DockWidgetClosable)
        self.addDockWidget(area, d)
        return d

    def _build_docks(self) -> None:
        self.tree = ModelTree()
        self.tree_dock = self._dock("Model tree", self.tree, Qt.LeftDockWidgetArea, "tree")
        self.props = PropertiesPanel()
        self.props_dock = self._dock("Properties", self.props, Qt.RightDockWidgetArea, "props")
        self.results_panel = ResultsPanel()
        self.results_dock = self._dock("Results display", self.results_panel, Qt.RightDockWidgetArea, "results")
        self.tabifyDockWidget(self.props_dock, self.results_dock)
        self.props_dock.raise_()
        self.bottom = QTabWidget()
        self.console = ConsolePanel()
        self.solver_panel = SolverPanel()
        self.mesh_diag = MeshDiagnostics()
        self.issues = IssuesPanel()
        self.summary = SummaryPanel()
        self.bottom.addTab(self.console, icon("report"), "Console")
        self.bottom.addTab(self.solver_panel, icon("solve"), "Solver progress")
        self.bottom.addTab(self.mesh_diag, icon("quality"), "Mesh diagnostics")
        self.bottom.addTab(self.issues, icon("validate"), "Errors && warnings")
        self.bottom.addTab(self.summary, icon("contour"), "Simulation summary")
        self.bottom_dock = self._dock("Output", self.bottom, Qt.BottomDockWidgetArea, "bottom")
        self.resizeDocks([self.tree_dock, self.props_dock], [330, 360], Qt.Horizontal)
        self.resizeDocks([self.bottom_dock], [230], Qt.Vertical)
        self.log_handler = QtLogHandler()
        self.log_handler.bridge.record.connect(self.console.append)
        logging.getLogger("opencae").addHandler(self.log_handler)

    def _act(self, name: str, text: str, slot: Callable, ico: str = "", shortcut: str = "", tip: str = "",
             checkable: bool = False) -> QAction:
        a = QAction(icon(ico) if ico else QIcon(), text, self)
        a.setObjectName(name)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.setToolTip(tip or text.replace("&", "").replace("…", ""))
        a.setStatusTip(tip or text.replace("&", ""))
        a.setCheckable(checkable)
        a.triggered.connect(lambda checked=False, s=slot: self._safe(s))
        self.A[name] = a
        return a

    def _safe(self, slot: Callable) -> None:
        """Run a command; report any exception instead of crashing the app."""
        try:
            slot()
        except Exception as exc:  # pragma: no cover - interactive safety net
            log.exception("Command failed")
            QMessageBox.critical(self, "Command failed", f"{type(exc).__name__}: {exc}")

    def _build_actions(self) -> None:
        self.A: dict[str, QAction] = {}
        a = self._act
        # file
        a("new", "&New project…", self.cmd_new, "new", "Ctrl+N")
        a("open", "&Open project…", self.cmd_open, "open", "Ctrl+O")
        a("save", "&Save project", self.cmd_save, "save", "Ctrl+S")
        a("save_as", "Save project &as…", self.cmd_save_as, "save", "Ctrl+Shift+S")
        a("import", "&Import geometry…", self.cmd_import, "import", "Ctrl+I")
        a("export_geom", "Export geometry…", self.cmd_export_geometry, "export")
        a("export_mesh", "Export mesh…", self.cmd_export_mesh, "export")
        a("export_results", "Export results…", self.cmd_export_results, "export")
        a("export_report", "Export &report…", self.cmd_report, "report", "Ctrl+R")
        a("example", "Open cantilever example", self.cmd_example, "cantilever")
        a("quit", "&Quit", self.close, "", "Ctrl+Q")
        # edit
        a("undo", "&Undo", self.cmd_undo, "undo", "Ctrl+Z")
        a("redo", "&Redo", self.cmd_redo, "redo", "Ctrl+Y")
        a("delete", "&Delete selected", self.cmd_delete_selected, "delete", "Del")
        a("rename", "Re&name selected", self.cmd_rename_selected, "", "F2")
        a("clear_sel", "Clear selection", self.cmd_clear_selection, "", "Esc")
        # view
        for v, key in (("front", "Ctrl+1"), ("back", "Ctrl+2"), ("top", "Ctrl+3"), ("bottom", "Ctrl+4"),
                       ("left", "Ctrl+5"), ("right", "Ctrl+6"), ("iso", "Ctrl+0")):
            a(f"view_{v}", f"{v.capitalize()} view", (lambda vv=v: self.viewport.set_view(vv)), f"view_{v}", key)
        a("fit", "&Fit to view", self.viewport.fit, "fit", "F")
        a("ortho", "Orthographic projection", self.cmd_projection, "ortho", "", checkable=True)
        self.A["ortho"].setIcon(icon("ortho"))
        grp = QActionGroup(self)
        for mode, label in (("shaded", "Shaded"), ("shaded_edges", "Shaded with edges"), ("wireframe", "Wireframe")):
            act = a(f"render_{mode}", label, (lambda m=mode: self.cmd_render_mode(m)), mode, checkable=True)
            grp.addAction(act)
        self.A["render_shaded_edges"].setChecked(True)
        a("show_geometry", "Show geometry", lambda: self.set_view_mode("geometry"), "box", "G")
        a("show_mesh", "Show mesh", lambda: self.set_view_mode("mesh"), "mesh_view", "M")
        a("show_results", "Show results", lambda: self.set_view_mode("results"), "contour", "R")
        a("background", "Background colour…", self.cmd_background, "background")
        a("screenshot", "Save screenshot (PNG)…", self.cmd_screenshot, "screenshot", "Ctrl+P")
        sgrp = QActionGroup(self)
        for k, label in (("body", "Select bodies"), ("face", "Select faces"), ("edge", "Select edges"),
                         ("vertex", "Select vertices"), ("node", "Select nodes"), ("element", "Select elements")):
            act = a(f"sel_{k}", label, (lambda kk=k: self.cmd_pick_kind(kk)), f"select_{k}", checkable=True)
            sgrp.addAction(act)
        self.A["sel_face"].setChecked(True)
        # geometry
        for shape in ("box", "cylinder", "sphere", "cone", "plate", "beam", "cantilever_beam"):
            a(f"prim_{shape}", D.PRIMITIVE_LABELS[shape] + "…", (lambda s=shape: self.cmd_primitive(s)),
              {"cantilever_beam": "cantilever"}.get(shape, shape))
        a("transform", "Transform (move / rotate / scale)…", self.cmd_transform, "transform")
        for op in ("union", "subtract", "intersect"):
            a(f"bool_{op}", f"Boolean {op}…", (lambda o=op: self.cmd_boolean(o)), op)
        a("fillet", "Fillet edges…", lambda: self.cmd_edge_op("fillet"), "fillet")
        a("chamfer", "Chamfer edges…", lambda: self.cmd_edge_op("chamfer"), "chamfer")
        a("extrude", "Extrude profile…", self.cmd_extrude, "extrude")
        a("check_geom", "Check geometry", self.cmd_check_geometry, "check")
        a("stl_to_solid", "Convert STL body to solid", self.cmd_stl_to_solid, "check")
        # mesh
        a("mesh_settings", "Mesh settings…", self.cmd_mesh_settings, "mesh_settings")
        a("mesh_refine", "Local refinement on selection…", self.cmd_refinement, "refine")
        a("mesh_generate", "&Generate mesh", self.cmd_generate_mesh, "mesh", "Ctrl+M")
        for q, label in (("none", "Plain"), ("gamma", "Gamma quality"), ("aspect_ratio", "Aspect ratio"),
                         ("sj", "Scaled Jacobian"), ("size", "Element size (density)")):
            a(f"mesh_color_{q}", f"Colour mesh by: {label}", (lambda qq=q: self.cmd_mesh_color(qq)), "quality")
        # materials
        a("mat_assign", "Assign material…", self.cmd_assign_material, "material")
        a("mat_new", "New custom material…", self.cmd_new_material, "material")
        a("mat_edit", "Edit material…", self.cmd_edit_material, "settings")
        # BCs
        a("bc_fixed", "Fixed support…", lambda: self.cmd_constraint("fixed"), "fixed")
        a("bc_disp", "Prescribed displacement…", lambda: self.cmd_constraint("displacement"), "displacement")
        a("bc_sym", "Symmetry…", lambda: self.cmd_constraint("symmetry"), "symmetry")
        # loads
        a("load_force", "Force…", lambda: self.cmd_load("force"), "force")
        a("load_pressure", "Pressure…", lambda: self.cmd_load("pressure"), "pressure")
        a("load_gravity", "Gravity…", lambda: self.cmd_load("gravity"), "gravity")
        a("load_disp", "Prescribed displacement load…", lambda: self.cmd_constraint("displacement"), "displacement")
        # analysis
        a("analysis_settings", "Analysis settings…", self.cmd_analysis_settings, "settings")
        a("validate", "&Validate model", self.cmd_validate, "validate", "F7")
        a("solve", "&Solve", self.cmd_solve, "solve", "F5")
        a("cancel", "Cancel running job", self.cmd_cancel, "stop", "Ctrl+Break")
        # results
        a("res_deformed", "Deformed shape", lambda: self.cmd_result("U_mag"), "deform")
        a("res_vm", "Von Mises stress", lambda: self.cmd_result("S_vm"), "contour")
        a("res_disp", "Displacement magnitude", lambda: self.cmd_result("U_mag"), "contour")
        a("res_p1", "Max principal stress", lambda: self.cmd_result("S_p1"), "contour")
        a("res_animate", "Animate deformation", self.cmd_animate, "animate", "", checkable=True)
        a("res_probe", "Probe values (click model)", self.cmd_probe_help, "probe")
        a("res_compare", "Compare with theory", self.cmd_compare, "inspect")
        # tools
        a("settings", "Settings (solver path)…", self.cmd_settings, "settings")
        a("benchmarks", "Run validation benchmarks", self.cmd_benchmarks, "validate")
        a("open_data", "Open project data folder", self.cmd_open_data, "open")
        # help
        a("guide", "User guide", self.cmd_guide, "help", "F1")
        a("ccx_help", "Installing CalculiX", lambda: D.TextDialog("Installing CalculiX", "<pre>" + INSTALL_HELP + "</pre>", self).exec(), "help")
        a("about", "About OpenCAE Studio", self.cmd_about, "app")

    def _build_menus(self) -> None:
        mb = self.menuBar()
        A = self.A

        def menu(title, names):
            m = mb.addMenu(title)
            for n in names:
                if n == "-":
                    m.addSeparator()
                elif isinstance(n, tuple):
                    sub = m.addMenu(n[0])
                    for x in n[1]:
                        sub.addAction(A[x])
                else:
                    m.addAction(A[n])
            return m

        fm = menu("&File", ["new", "open", "save", "save_as", "-", "import", "export_geom", "export_mesh",
                            "export_results", "export_report", "-", "example"])
        self.recent_menu = fm.addMenu("Recent projects")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        fm.addSeparator()
        fm.addAction(A["quit"])
        menu("&Edit", ["undo", "redo", "-", "rename", "delete", "clear_sel"])
        menu("&View", ["show_geometry", "show_mesh", "show_results", "-", "fit", "view_iso", "view_front", "view_back",
                       "view_top", "view_bottom", "view_left", "view_right", "-", "ortho", "render_shaded",
                       "render_shaded_edges", "render_wireframe", "-",
                       ("Selection mode", ["sel_body", "sel_face", "sel_edge", "sel_vertex", "sel_node", "sel_element"]),
                       "background", "screenshot"])
        menu("&Geometry", ["prim_box", "prim_cylinder", "prim_sphere", "prim_cone", "prim_plate", "prim_beam",
                           "prim_cantilever_beam", "-", "transform", "bool_union", "bool_subtract", "bool_intersect",
                           "fillet", "chamfer", "extrude", "-", "import", "export_geom", "-", "check_geom",
                           "stl_to_solid"])
        menu("&Mesh", ["mesh_settings", "mesh_refine", "mesh_generate", "-", "show_mesh", "mesh_color_none",
                       "mesh_color_gamma", "mesh_color_aspect_ratio", "mesh_color_sj", "mesh_color_size", "-",
                       "export_mesh"])
        menu("Ma&terials", ["mat_assign", "mat_new", "mat_edit"])
        menu("&Loads", ["load_force", "load_pressure", "load_gravity", "load_disp"])
        menu("&Boundary Conditions", ["bc_fixed", "bc_disp", "bc_sym"])
        menu("&Analysis", ["analysis_settings", "validate", "solve", "cancel"])
        menu("&Results", ["show_results", "res_deformed", "res_disp", "res_vm", "res_p1", "res_animate", "res_probe",
                          "res_compare", "-", "export_results", "export_report", "screenshot"])
        menu("&Tools", ["settings", "benchmarks", "open_data"])
        menu("&Help", ["guide", "ccx_help", "about"])

    def _build_ribbon(self) -> None:
        self.ribbon = QTabWidget()
        self.ribbon.setDocumentMode(True)
        pages = {
            "Home": ["new", "open", "save", "-", "undo", "redo", "-", "import", "example", "-", "validate", "solve",
                     "-", "export_report"],
            "Geometry": ["prim_box", "prim_cylinder", "prim_sphere", "prim_cone", "prim_plate", "prim_beam",
                         "prim_cantilever_beam", "-", "transform", "bool_union", "bool_subtract", "bool_intersect",
                         "fillet", "chamfer", "extrude", "-", "check_geom", "import", "export_geom"],
            "Mesh": ["mesh_settings", "mesh_refine", "mesh_generate", "-", "show_mesh", "mesh_color_gamma",
                     "mesh_color_aspect_ratio", "-", "export_mesh"],
            "Setup": ["mat_assign", "mat_new", "-", "bc_fixed", "bc_disp", "bc_sym", "-", "load_force",
                      "load_pressure", "load_gravity", "-", "analysis_settings", "validate"],
            "Solve": ["validate", "solve", "cancel", "-", "settings"],
            "Results": ["show_results", "res_disp", "res_vm", "res_p1", "res_deformed", "res_animate", "res_probe",
                        "res_compare", "-", "export_results", "export_report", "screenshot"],
            "View": ["fit", "view_iso", "view_front", "view_back", "view_top", "view_bottom", "view_left",
                     "view_right", "-", "ortho", "render_shaded", "render_shaded_edges", "render_wireframe", "-",
                     "sel_body", "sel_face", "sel_edge", "sel_vertex", "sel_node", "sel_element", "-", "background",
                     "screenshot"],
        }
        short = {"prim_cantilever_beam": "Cantilever", "bool_union": "Union", "bool_subtract": "Subtract",
                 "bool_intersect": "Intersect", "mesh_generate": "Generate", "mesh_settings": "Settings",
                 "mesh_refine": "Refine", "mesh_color_gamma": "Quality", "mesh_color_aspect_ratio": "Aspect",
                 "transform": "Transform", "render_shaded_edges": "Edges", "render_shaded": "Shaded",
                 "render_wireframe": "Wireframe", "analysis_settings": "Analysis", "res_deformed": "Deformed",
                 "res_vm": "Von Mises", "res_disp": "Displacement", "res_p1": "Principal", "res_animate": "Animate",
                 "res_probe": "Probe", "res_compare": "Theory", "export_report": "Report", "export_results": "Export",
                 "export_mesh": "Export", "export_geom": "Export", "check_geom": "Check", "mat_assign": "Assign",
                 "mat_new": "New material", "bc_fixed": "Fixed", "bc_disp": "Displacement", "bc_sym": "Symmetry",
                 "load_force": "Force", "load_pressure": "Pressure", "load_gravity": "Gravity", "show_mesh": "Mesh",
                 "show_results": "Results", "example": "Example", "screenshot": "Screenshot",
                 "background": "Background", "ortho": "Ortho", "sel_body": "Body", "sel_face": "Face",
                 "sel_edge": "Edge", "sel_vertex": "Vertex", "sel_node": "Node", "sel_element": "Element",
                 "view_iso": "Iso", "view_front": "Front", "view_back": "Back", "view_top": "Top",
                 "view_bottom": "Bottom", "view_left": "Left", "view_right": "Right"}
        self.ribbon_bars: dict[str, QToolBar] = {}
        for page, names in pages.items():
            tb = QToolBar(page)
            tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            from PySide6.QtCore import QSize
            tb.setIconSize(QSize(26, 26))
            for n in names:
                if n == "-":
                    tb.addSeparator()
                    continue
                act = self.A[n]
                # iconText is what QToolButton shows under the icon; it survives action updates
                act.setIconText(short.get(n, act.text().replace("&", "").replace("…", "")))
                tb.addAction(act)
            self.ribbon.addTab(tb, page)
            self.ribbon_bars[page] = tb
        top = QToolBar("Ribbon")
        top.setObjectName("ribbon")
        top.setMovable(False)
        top.addWidget(self.ribbon)
        self.addToolBar(Qt.TopToolBarArea, top)
        self.addToolBarBreak()
        self.workflow = WorkflowBar()
        wtb = QToolBar("Workflow")
        wtb.setObjectName("workflow")
        wtb.setMovable(False)
        lab = QLabel("  Workflow: ")
        lab.setObjectName("hint")
        wtb.addWidget(lab)
        wtb.addWidget(self.workflow)
        self.addToolBar(Qt.TopToolBarArea, wtb)

    def _connect(self) -> None:
        self.tree.selected.connect(self.on_tree_selected)
        self.tree.action.connect(self.on_tree_action)
        self.tree.visibility.connect(self.on_visibility)
        self.props.applied.connect(self.on_properties_applied)
        self.viewport.picked.connect(self.on_pick)
        self.results_panel.changed.connect(self.refresh_results_view)
        self.results_panel.animate.connect(self.on_animate_toggled)
        self.solver_panel.cancel.connect(self.cmd_cancel)
        self.workflow.clicked.connect(self.on_workflow_clicked)

    # ================================================================== state helpers
    @property
    def us(self):
        return self.session.units

    def mutate(self, description: str, fn: Callable[[], None], regen: bool = True) -> None:
        before = self.session.project.clone()
        fn()
        self.undo.push(description, before)
        self.dirty = True
        if regen:
            self.session.regenerate()
            errs = self.session.regen.errors
            if errs:
                self.console.append("Geometry regeneration errors: " + "; ".join(errs.values()), logging.ERROR)
        log.info(description)
        self.refresh_all()

    def refresh_all(self, fit: bool = False) -> None:
        self.tree.rebuild(self.session)
        if self.view_mode == "mesh" and self.session.mesh is None:
            self.view_mode = "geometry"
        if self.view_mode == "results" and self.session.results is None:
            self.view_mode = "geometry"
        self.refresh_view()
        if fit:
            self.viewport.fit()
        self.workflow.update_status(self.session.workflow_status())
        self.mesh_diag.show_mesh(self.session.mesh, self.us.length)
        self.update_actions()
        name = self.session.path.name if self.session.path else "unsaved"
        self.setWindowTitle(f"OpenCAE Studio — {self.session.project.name} [{name}]{' *' if self.dirty else ''}")
        self.units_label.setText(f"Units: {self.us.key} ({self.us.length}, {self.us.force}, {self.us.stress})  ")
        exe = self.session.backend.executable()
        self.solver_label.setText(f"Solver: CalculiX {self.session.backend.version()}" if exe else
                                  "Solver: CalculiX NOT FOUND")
        self.solver_label.setStyleSheet(f"color:{theme.OK if exe else theme.ERR};")
        self._update_summary()

    def update_actions(self) -> None:
        s = self.session
        has_solid = s.analysis_body() is not None
        nsolids = sum(1 for b in s.bodies.values() if b.kind == "solid")
        busy = self.proc is not None
        A = self.A

        def en(name, ok, why=""):
            A[name].setEnabled(ok)
            base = A[name].statusTip()
            A[name].setToolTip(base if ok else f"{base} — unavailable: {why}")

        en("undo", self.undo.can_undo(), "nothing to undo")
        A["undo"].setText(f"&Undo {self.undo.undo_text()}".strip())
        en("redo", self.undo.can_redo(), "nothing to redo")
        A["redo"].setText(f"&Redo {self.undo.redo_text()}".strip())
        for n in ("transform", "fillet", "chamfer", "check_geom", "mesh_settings", "export_geom", "mat_assign"):
            en(n, has_solid, "no solid body")
        for n in ("bool_union", "bool_subtract", "bool_intersect"):
            en(n, nsolids >= 2, "needs two solid bodies")
        en("stl_to_solid", any(b.kind == "tessellated" for b in s.bodies.values()), "no STL body")
        en("mesh_generate", has_solid and not busy, "no solid body" if not has_solid else "a job is running")
        en("mesh_refine", has_solid, "no solid body")
        en("show_mesh", s.mesh is not None, "no mesh")
        for q in ("none", "gamma", "aspect_ratio", "sj", "size"):
            en(f"mesh_color_{q}", s.mesh is not None, "no mesh")
        en("export_mesh", s.mesh is not None, "no mesh")
        for n in ("bc_fixed", "bc_disp", "bc_sym", "load_force", "load_pressure", "load_gravity", "load_disp"):
            en(n, has_solid, "no solid body")
        has_ccx = bool(s.backend.executable())
        en("solve", has_ccx and not busy, "CalculiX not installed (see Help)" if not has_ccx else "a job is running")
        en("cancel", busy, "no running job")
        res = s.results is not None
        for n in ("show_results", "res_deformed", "res_vm", "res_disp", "res_p1", "res_animate", "res_probe",
                  "export_results", "res_compare"):
            en(n, res, "no results - run the solver")
        en("sel_node", s.mesh is not None, "no mesh")
        en("sel_element", s.mesh is not None, "no mesh")
        en("open_data", True)

    def refresh_view(self) -> None:
        s = self.session
        vp = self.viewport
        if self.view_mode == "results" and s.results is not None:
            self.refresh_results_view()
            return
        if self.view_mode == "mesh" and s.mesh is not None:
            vp.show_mesh(s.mesh, self.mesh_color)
        else:
            self.view_mode = "geometry"
            vp.show_geometry(s.bodies, s.regen.order, set(s.project.geometry.hidden_bodies))
        self._show_bc_glyphs()
        vp.highlight(self.selection, s.bodies, s.mesh)

    def _show_bc_glyphs(self) -> None:
        s = self.session
        body = s.analysis_body()
        if body is None:
            self.viewport.show_bc_glyphs(None, [], [])
            return
        cons, loads = [], []
        for c in s.project.constraints:
            if c.suppressed:
                continue
            cons.append((c.kind, [t.index for t in c.targets if t.kind == "face" and t.body == body.id
                                  and 0 <= t.index < len(body.faces)],
                         [t.index for t in c.targets if t.kind == "vertex" and t.body == body.id
                          and 0 <= t.index < len(body.vertices)]))
        for l in s.project.loads:
            if l.suppressed:
                continue
            loads.append({"kind": l.kind, "faces": [t.index for t in l.targets if t.kind == "face"
                                                    and 0 <= t.index < len(body.faces)],
                          "vertices": [t.index for t in l.targets if t.kind == "vertex"
                                       and 0 <= t.index < len(body.vertices)],
                          "dir": l.params.get("direction", [0, 0, -1]), "value": l.params.get("magnitude", 1.0),
                          "cs": l.params.get("coordinate_system", "global")})
        self.viewport.show_bc_glyphs(body, cons, loads)

    def set_view_mode(self, mode: str) -> None:
        if mode == "mesh" and self.session.mesh is None:
            self.statusBar().showMessage("No mesh yet - generate the mesh first.", 5000)
            return
        if mode == "results" and self.session.results is None:
            self.statusBar().showMessage("No results yet - run the solver first.", 5000)
            return
        self.view_mode = mode
        if mode == "results":
            self._fill_result_quantities()
            self.results_dock.raise_()
            self.ribbon.setCurrentIndex(list(self.ribbon_bars).index("Results"))
        elif mode == "mesh":
            self.ribbon.setCurrentIndex(list(self.ribbon_bars).index("Mesh"))
        self.refresh_view()

    # ================================================================== tree / properties
    def on_tree_selected(self, kind: str, ident: str) -> None:
        s = self.session
        p = s.project
        us = self.us
        fields: list[Field] = []
        title, note = kind.replace("_", " ").capitalize(), ""
        if kind == "feature":
            f = next((x for x in p.geometry.features if x.id == ident), None)
            if f:
                title = f"Feature: {f.name} ({f.kind})"
                fields = D.feature_fields(f, s.bodies, s.regen.order, us)
                note = s.regen.errors.get(f.id, "Edit parameters and Apply to regenerate the geometry.")
        elif kind == "body":
            b = s.bodies.get(ident)
            if b:
                title = f"Body: {b.name}"
                chk = b.check
                fields = [Field("name", "Name", "str", b.name),
                          Field("kind", "Representation", "text", "exact B-rep solid" if b.kind == "solid" else
                                "tessellated STL (not an exact solid)"),
                          Field("volume", "Volume", "text", f"{b.volume:.6g} {us.length}^3"),
                          Field("area", "Surface area", "text", f"{b.area:.6g} {us.length}^2"),
                          Field("bbox", "Bounding box", "text",
                                f"{np.round(b.bbox[0], 5).tolist()} → {np.round(b.bbox[1], 5).tolist()}"),
                          Field("topo", "Topology", "text", f"{chk.n_solids} solid(s), {chk.n_faces} faces, "
                                f"{chk.n_edges} edges, {chk.n_vertices} vertices"),
                          Field("valid", "Validity", "text", "valid, closed" if chk.valid and chk.closed else
                                "; ".join(chk.issues))]
                mat = p.material_for_body(b.id)
                fields.append(Field("material", "Material", "choice", mat.id if mat else "",
                                    options=[("", "(none)")] + [(m.id, m.name) for m in p.materials]))
                self._select([Pick("body", b.id)])
        elif kind in ("face", "edge", "vertex"):
            bid, idx = ident.rsplit(":", 1)
            self._select([Pick(kind, bid, int(idx))])
            self._show_entity_props(Pick(kind, bid, int(idx)))
            return
        elif kind == "material":
            m = p.material_by_id(ident)
            if m:
                title = f"Material: {m.name}"
                fields = D.material_fields(m, us)
                note = EXAMPLE_WARNING if m.is_example else "User-defined material."
        elif kind == "constraint":
            c = next((x for x in p.constraints if x.id == ident), None)
            body = s.analysis_body()
            if c and body:
                title = f"Constraint: {c.name} ({c.kind})"
                fields = D.constraint_fields(c.kind, body, us, c)
                self._select([Pick(t.kind, t.body, t.index) for t in c.targets])
        elif kind == "load":
            l = next((x for x in p.loads if x.id == ident), None)
            body = s.analysis_body()
            if l and body:
                title = f"Load: {l.name} ({l.kind})"
                fields = D.load_fields(l.kind, body, us, l)
                self._select([Pick(t.kind, t.body, t.index) for t in l.targets])
        elif kind == "named_selection":
            item = next((x for x in p.constraints + p.loads if x.id == ident), None)
            if item:
                self._select([Pick(t.kind, t.body, t.index) for t in item.targets])
                title = f"Named selection: {item.name}"
                fields = [Field("t", "Entities", "text", ", ".join(t.label() for t in item.targets))]
                if s.mesh is not None:
                    from opencae.boundary_conditions.resolve import _ref_nodes
                    n = 0
                    for t in item.targets:
                        nodes = _ref_nodes(t, t.index, s.mesh)
                        n += 0 if nodes is None else len(nodes)
                    fields.append(Field("n", "Mesh nodes", "text", str(n)))
        elif kind in ("mesh_settings", "mesh"):
            title = "Mesh controls"
            b = s.analysis_body()
            fields = D.mesh_fields(p.mesh_settings, s.bodies, s.regen.order, us, (b.size / 20) if b else 0)
            kind = "mesh_settings"
        elif kind == "refinement":
            r = next((x for x in p.mesh_settings.refinements if x.id == ident), None)
            if r:
                title = f"Refinement: {r.name}"
                fields = [Field("name", "Name", "str", r.name), Field("size", "Element size", "float", r.size,
                                                                       us.length, minimum=1e-12),
                          Field("targets", "Targets", "text", ", ".join(t.label() for t in r.targets))]
                self._select([Pick(t.kind, t.body, t.index) for t in r.targets])
        elif kind in ("nodes", "elements", "named_selections"):
            title = "Mesh"
            st = s.mesh.stats() if s.mesh else {}
            fields = [Field(k, k.replace("_", " "), "text", f"{v:.6g}" if isinstance(v, float) else str(v))
                      for k, v in st.items()]
        elif kind in ("solver", "analysis", "step", "steps"):
            title = "Analysis / solver configuration"
            info = f"CalculiX {s.backend.version()} at {s.backend.executable()}" if s.backend.executable() else \
                "CalculiX not found - see Help > Installing CalculiX"
            fields = D.analysis_fields(p.analysis, info)
            kind = "solver"
        elif kind == "project":
            title = "Project"
            fields = [Field("name", "Project name", "str", p.name), Field("description", "Description", "str",
                                                                         p.description),
                      Field("units", "Unit system", "text", f"{us.key} (fixed at project creation)"),
                      Field("file", "File", "text", str(s.path or "unsaved"))]
        elif kind in ("result", "result_group", "results"):
            if s.results is not None:
                if kind == "result":
                    self._current_result = ident
                self.set_view_mode("results")
            return
        self.props.show_fields(title, kind, ident, fields, note)
        self.props_dock.raise_()

    def _show_entity_props(self, pk: Pick) -> None:
        s = self.session
        us = self.us
        b = s.bodies.get(pk.body)
        fields, title = [], ""
        if pk.kind == "face" and b and 0 <= pk.index < len(b.faces):
            f = b.faces[pk.index]
            title = f"Face {f.index + 1} of {b.name}"
            fields = [Field("t", "Surface type", "text", f.surface_type),
                      Field("a", "Area", "text", f"{f.area:.6g} {us.length}^2"),
                      Field("c", "Centroid", "text", str(np.round(f.centroid, 6).tolist())),
                      Field("n", "Outward normal", "text", "curved" if f.normal is None else str(np.round(f.normal, 6).tolist()))]
            if s.mesh is not None and pk.index in s.mesh.face_tris:
                fields.append(Field("m", "Mesh", "text", f"{len(s.mesh.face_tris[pk.index])} triangles, "
                                                          f"{len(s.mesh.face_nodes(pk.index))} nodes"))
        elif pk.kind == "edge" and b and 0 <= pk.index < len(b.edges):
            e = b.edges[pk.index]
            title = f"Edge {e.index + 1} of {b.name}"
            fields = [Field("t", "Curve type", "text", e.curve_type), Field("l", "Length", "text", f"{e.length:.6g} {us.length}"),
                      Field("c", "Centroid", "text", str(np.round(e.centroid, 6).tolist()))]
        elif pk.kind == "vertex" and b and 0 <= pk.index < len(b.vertices):
            v = b.vertices[pk.index]
            title = f"Vertex {v.index + 1} of {b.name}"
            fields = [Field("p", "Position", "text", str(np.round(v.point, 6).tolist()))]
        elif pk.kind == "node" and s.mesh is not None:
            title = f"Node {pk.index + 1}"
            fields = [Field("p", "Coordinates", "text", str(np.round(s.mesh.nodes[pk.index], 6).tolist()))]
            if s.results is not None:
                u = s.results.displacement[pk.index]
                fields.append(Field("u", "Displacement", "text", f"{np.round(u, 8).tolist()} {us.length}"))
        elif pk.kind == "element" and s.mesh is not None:
            q = s.mesh.quality
            title = f"Element {pk.index + 1} ({s.mesh.element_type})"
            fields = [Field("n", "Nodes", "text", ", ".join(str(n + 1) for n in s.mesh.elements[pk.index])),
                      Field("v", "Volume", "text", f"{q['volume'][pk.index]:.6g} {us.length}^3"),
                      Field("g", "Gamma", "text", f"{q['gamma'][pk.index]:.4f}"),
                      Field("sj", "Min scaled Jacobian", "text", f"{q['sj'][pk.index]:.4f}"),
                      Field("ar", "Aspect ratio", "text", f"{q['aspect_ratio'][pk.index]:.4f}")]
        self.props.show_fields(title or "Selection", pk.kind, f"{pk.body}:{pk.index}", fields)

    def on_properties_applied(self, kind: str, ident: str, v: dict) -> None:
        s = self.session
        p = s.project
        us = self.us
        if kind == "feature":
            f = next(x for x in p.geometry.features if x.id == ident)

            def fn():
                f.name = v.pop("name", f.name)
                if f.kind == "primitive":
                    v.pop("force", None)
                f.params.update(v)
                if f.kind in ("fillet", "chamfer") and not f.params.get("edges"):
                    f.params["edges"] = "all"
                if f.creates_body() and f.id in p.geometry.body_names and not p.geometry.body_names[f.id]:
                    p.geometry.body_names[f.id] = f.name
            self.mutate(f"Edit {f.name}", fn)
            if f.id in s.regen.errors:
                QMessageBox.warning(self, "Regeneration failed",
                                    f"{s.regen.errors[f.id]}\n\nUse Edit > Undo to restore the previous parameters.")
        elif kind == "body":
            def fn():
                p.geometry.body_names[ident] = v["name"]
                if v.get("material"):
                    p.assign_material(ident, v["material"])
                else:
                    p.assignments = [a for a in p.assignments if a.body != ident]
            self.mutate("Edit body", fn)
        elif kind == "material":
            m = p.material_by_id(ident)
            self.mutate(f"Edit material {m.name}", lambda: D.apply_material_values(m, v, us), regen=False)
        elif kind == "constraint":
            c = next(x for x in p.constraints if x.id == ident)
            self.mutate(f"Edit {c.name}", lambda: self._apply_bc_values(c, v), regen=False)
        elif kind == "load":
            l = next(x for x in p.loads if x.id == ident)
            self.mutate(f"Edit {l.name}", lambda: self._apply_load_values(l, v), regen=False)
        elif kind == "mesh_settings":
            def fn():
                ms = p.mesh_settings
                ms.body, ms.element_order = v["body"], int(v["element_order"])
                ms.size_max, ms.size_min = v["size_max"], v["size_min"]
                ms.curvature_points, ms.optimize = v["curvature_points"], v["optimize"]
            self.mutate("Edit mesh settings", fn, regen=False)
        elif kind == "refinement":
            r = next(x for x in p.mesh_settings.refinements if x.id == ident)
            self.mutate("Edit refinement", lambda: (setattr(r, "name", v["name"]), setattr(r, "size", v["size"])),
                        regen=False)
        elif kind == "solver":
            def fn():
                a = p.analysis
                a.title, a.analysis_type, a.threads = v["title"], v["analysis_type"], v["threads"]
                a.write_ip_stress = v["write_ip_stress"]
            self.mutate("Edit analysis settings", fn, regen=False)
        elif kind == "project":
            self.mutate("Edit project", lambda: (setattr(p, "name", v["name"]), setattr(p, "description",
                                                                                         v["description"])), regen=False)
        self.statusBar().showMessage("Applied.", 3000)

    def on_tree_action(self, act: str, kind: str, ident: str) -> None:
        s = self.session
        p = s.project
        if act.startswith("rename:"):
            name = act.split(":", 1)[1].split(" (STL")[0].strip()
            if not name:
                return
            if kind == "body" and p.geometry.body_names.get(ident) != name:
                self.mutate("Rename body", lambda: p.geometry.body_names.__setitem__(ident, name), regen=False)
                self.session.regenerate()
                self.refresh_all()
            elif kind == "feature":
                f = next(x for x in p.geometry.features if x.id == ident)
                if f.name != name:
                    self.mutate("Rename feature", lambda: setattr(f, "name", name), regen=False)
            elif kind in ("material", "constraint", "load", "refinement"):
                coll = {"material": p.materials, "constraint": p.constraints, "load": p.loads,
                        "refinement": p.mesh_settings.refinements}[kind]
                obj = next(x for x in coll if x.id == ident)
                if obj.name != name:
                    self.mutate(f"Rename {kind}", lambda: setattr(obj, "name", name), regen=False)
            return
        if act == "edit":
            self.on_tree_selected(kind, ident)
            self.props_dock.raise_()
        elif act == "delete":
            self._delete(kind, ident)
        elif act == "suppress":
            coll = {"feature": p.geometry.features, "constraint": p.constraints, "load": p.loads}[kind]
            obj = next(x for x in coll if x.id == ident)
            self.mutate(f"{'Unsuppress' if obj.suppressed else 'Suppress'} {obj.name}",
                        lambda: setattr(obj, "suppressed", not obj.suppressed), regen=(kind == "feature"))
        elif act == "set_analysis":
            self.mutate("Set analysis body", lambda: setattr(p.mesh_settings, "body", ident), regen=False)
        elif act == "inspect":
            self.cmd_check_geometry(ident)
        elif act == "export":
            self.cmd_export_geometry([ident])
        elif act == "toggle_visible":
            self.on_visibility(ident, ident in p.geometry.hidden_bodies)
        elif act == "assign":
            b = s.analysis_body()
            if b:
                self.mutate("Assign material", lambda: p.assign_material(b.id, ident), regen=False)
        elif act == "select":
            bid, idx = ident.rsplit(":", 1)
            self._select([Pick(kind, bid, int(idx))])
        elif act == "generate":
            self.cmd_generate_mesh()
        elif act == "report":
            self.cmd_report()
        elif act == "stl_to_solid":
            self.cmd_stl_to_solid(ident)

    def on_visibility(self, body: str, visible: bool) -> None:
        hb = self.session.project.geometry.hidden_bodies
        if visible and body in hb:
            hb.remove(body)
        elif not visible and body not in hb:
            hb.append(body)
        self.dirty = True
        self.refresh_view()

    def _delete(self, kind: str, ident: str) -> None:
        p = self.session.project
        if kind == "feature":
            f = next(x for x in p.geometry.features if x.id == ident)
            deps = [x.name for x in p.geometry.features if ident in (x.params.get("body"), x.params.get("target"),
                                                                     x.params.get("tool"), x.params.get("fuse_with"))]
            refs = [x.name for x in p.constraints + p.loads if any(t.body == ident for t in x.targets)]
            if deps or refs:
                if QMessageBox.question(self, "Delete feature",
                                        f"'{f.name}' is used by: {', '.join(deps + refs)}.\nThese will become invalid. "
                                        "Delete anyway?") != QMessageBox.Yes:
                    return

            def fn():
                p.geometry.features.remove(f)
                p.geometry.body_names.pop(ident, None)
            self.mutate(f"Delete {f.name}", fn)
        elif kind == "material":
            m = p.material_by_id(ident)
            self.mutate(f"Delete material {m.name}", lambda: (p.materials.remove(m), setattr(
                p, "assignments", [a for a in p.assignments if a.material != ident])), regen=False)
        elif kind == "constraint":
            c = next(x for x in p.constraints if x.id == ident)
            self.mutate(f"Delete {c.name}", lambda: p.constraints.remove(c), regen=False)
        elif kind == "load":
            l = next(x for x in p.loads if x.id == ident)
            self.mutate(f"Delete {l.name}", lambda: p.loads.remove(l), regen=False)
        elif kind == "refinement":
            r = next(x for x in p.mesh_settings.refinements if x.id == ident)
            self.mutate("Delete refinement", lambda: p.mesh_settings.refinements.remove(r), regen=False)

    def cmd_delete_selected(self) -> None:
        it = self.tree.currentItem()
        if it is None:
            return
        kind, ident = it.data(0, Qt.UserRole + 1)
        if kind in ("feature", "material", "constraint", "load", "refinement"):
            self._delete(kind, ident)
        else:
            self.statusBar().showMessage(f"'{kind}' items cannot be deleted.", 4000)

    def cmd_rename_selected(self) -> None:
        it = self.tree.currentItem()
        if it is not None and it.flags() & Qt.ItemIsEditable:
            self.tree.editItem(it, 0)

    # ================================================================== selection / picking
    def _select(self, picks: list[Pick]) -> None:
        self.selection = picks
        self.viewport.highlight(picks, self.session.bodies, self.session.mesh)
        if picks:
            self.statusBar().showMessage("Selected: " + ", ".join(
                f"{p.kind} {p.index + 1}" if p.index >= 0 else p.kind for p in picks[:12]) +
                (" …" if len(picks) > 12 else ""))

    def cmd_clear_selection(self) -> None:
        self._select([])
        self.statusBar().showMessage("Selection cleared", 2000)

    def on_pick(self, pk: Optional[Pick]) -> None:
        if pk is None:
            if self.view_mode != "results":
                self.cmd_clear_selection()
            return
        if self.view_mode == "results":
            self._probe(pk)
            return
        if pk.additive:
            keys = [x.key() for x in self.selection]
            if pk.key() in keys:
                sel = [x for x in self.selection if x.key() != pk.key()]
            else:
                sel = self.selection + [pk]
        else:
            sel = [pk]
        self._select(sel)
        self._show_entity_props(pk)
        self.props_dock.raise_()

    def _probe(self, pk: Pick) -> None:
        rs = self.session.results
        if rs is None or pk.kind != "node":
            return
        key = self.results_panel.current_key() or "U_mag"
        q = QUANTITIES[key]
        unit = self._unit_for(q.unit_kind)
        n = pk.index
        val = rs.quantity(key)[n]
        u = rs.displacement[n]
        txt = (f"<b>Node {n + 1}</b> at {np.round(rs.mesh.nodes[n], 5).tolist()}<br>"
               f"{q.label}: <b>{val:.6g} {unit}</b> ({q.location})<br>"
               f"U = ({u[0]:.5g}, {u[1]:.5g}, {u[2]:.5g}) {self.us.length}")
        if rs.stress is not None:
            txt += f"<br>von Mises: {rs.quantity('S_vm')[n]:.6g} {self.us.stress}"
        self.results_panel.probe.setText(txt)
        self.viewport.show_probe(pk.point, f"N{n + 1}: {val:.4g}")
        self.console.append(f"Probe node {n + 1}: {q.label} = {val:.6g} {unit}")

    def cmd_pick_kind(self, kind: str) -> None:
        if kind in ("node", "element") and self.view_mode == "geometry" and self.session.mesh is not None:
            self.set_view_mode("mesh")
        self.viewport.set_pick_kind(kind)
        self.statusBar().showMessage(f"Selection mode: {kind}s (click; Ctrl+click to add/remove)", 4000)

    def _selected_targets(self, kinds: tuple[str, ...]) -> list[str]:
        body = self.session.analysis_body()
        return [f"{p.kind}:{p.index}" for p in self.selection if p.kind in kinds and body and p.body == body.id]

    # ================================================================== file commands
    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        r = QMessageBox.question(self, "Unsaved changes", "Save changes to the current project?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.cmd_save()
        return r == QMessageBox.Discard

    def cmd_new(self) -> None:
        if not self._confirm_discard():
            return
        dlg = ParamDialog("New project", [Field("name", "Project name", "str", "Untitled"),
                                          Field("units", "Unit system", "choice", "mm-N-s",
                                                options=[("mm-N-s", "mm, N, MPa, t/mm³ (mm-N-s)"),
                                                         ("m-N-s", "m, N, Pa, kg/m³ (SI)")])], self,
                          note="The unit system is consistent and fixed for the project; all inputs and results use it.")
        if not dlg.exec():
            return
        self.session.new(dlg.result_values["name"], dlg.result_values["units"])
        self._reset_after_load()

    def _reset_after_load(self) -> None:
        self.undo.clear()
        self.dirty = False
        self.selection = []
        self.view_mode = "geometry"
        self.session.last_report = None
        self.refresh_all(fit=True)
        if self.session.results is not None:
            self.set_view_mode("results")

    def cmd_open(self, path: Optional[str] = None) -> None:
        if not self._confirm_discard():
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Open project", self._last_dir(),
                                                  "OpenCAE projects (*.ocae)")
        if not path:
            return
        try:
            self.session.open(Path(path))
        except Exception as exc:
            QMessageBox.critical(self, "Open failed", str(exc))
            return
        self._add_recent(path)
        self._reset_after_load()
        self.console.append(f"Opened {path}")

    def cmd_save(self) -> bool:
        if self.session.path is None:
            return self.cmd_save_as()
        self.session.save()
        self.dirty = False
        self._add_recent(str(self.session.path))
        self.refresh_all()
        self.statusBar().showMessage(f"Saved {self.session.path}", 4000)
        return True

    def cmd_save_as(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(self, "Save project as",
                                              str(Path(self._last_dir()) / f"{self.session.project.name}.ocae"),
                                              "OpenCAE projects (*.ocae)")
        if not path:
            return False
        self.session.save(Path(path))
        self.dirty = False
        self._add_recent(str(self.session.path))
        self.refresh_all()
        self.statusBar().showMessage(f"Saved {self.session.path}", 4000)
        return True

    def _last_dir(self) -> str:
        return self.settings.value("last_dir", str(Path.home()), str)

    def _add_recent(self, path: str) -> None:
        rec = [r for r in (self.settings.value("recent", [], list) or []) if r != path]
        rec.insert(0, path)
        self.settings.setValue("recent", rec[:10])
        self.settings.setValue("last_dir", str(Path(path).parent))

    def _fill_recent(self) -> None:
        self.recent_menu.clear()
        rec = [r for r in (self.settings.value("recent", [], list) or []) if Path(r).exists()]
        if not rec:
            a = self.recent_menu.addAction("(no recent projects)")
            a.setEnabled(False)
        for r in rec:
            self.recent_menu.addAction(r, lambda rr=r: self.cmd_open(rr))

    def cmd_example(self) -> None:
        if not self._confirm_discard():
            return
        self.session.new("Cantilever example", "mm-N-s")
        self.session.create_cantilever()
        self.session.project.mesh_settings.size_max = 5.0
        self._reset_after_load()
        self.console.append("Cantilever example created: 200 x 20 x 20 mm steel beam, fixed at x = 0, "
                            "1000 N tip load in -Z. Next: Mesh > Generate, then Analysis > Solve.")

    def cmd_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import geometry", self._last_dir(), CAD_FILTER)
        if not path:
            return
        to_solid = False
        if path.lower().endswith(".stl"):
            r = QMessageBox.question(self, "STL import",
                                     "STL files contain only triangles, not exact geometry.\n\n"
                                     "Yes: try to reconstruct a faceted solid (requires a watertight STL).\n"
                                     "No: import as tessellated body for display only.",
                                     QMessageBox.Yes | QMessageBox.No)
            to_solid = r == QMessageBox.Yes
        before = self.session.project.clone()
        f = self.session.import_geometry(Path(path), to_solid)
        self.undo.push(f"Import {Path(path).name}", before)
        self.dirty = True
        self.settings.setValue("last_dir", str(Path(path).parent))
        if f.id in self.session.regen.errors:
            QMessageBox.warning(self, "Import problem", self.session.regen.errors[f.id])
        self.refresh_all(fit=True)

    def cmd_export_geometry(self, body_ids: Optional[list[str]] = None) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export geometry", str(Path(self._last_dir()) / "geometry.step"),
                                              "STEP (*.step);;IGES (*.igs);;BREP (*.brep);;STL (*.stl)")
        if not path:
            return
        if not Path(path).suffix:
            path += ".step"
        self.session.export_geometry(Path(path), body_ids)
        self.console.append(f"Exported geometry to {path}")

    def cmd_export_mesh(self) -> None:
        if self.session.mesh is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export mesh", str(Path(self._last_dir()) / "mesh.inp"),
                                              "CalculiX/Abaqus (*.inp);;VTK unstructured (*.vtu);;Gmsh (*.msh)")
        if not path:
            return
        self.session.mesh.export(Path(path))
        self.console.append(f"Exported mesh to {path}")

    def cmd_export_results(self) -> None:
        from opencae.postprocessing.report import export_results
        path, _ = QFileDialog.getSaveFileName(self, "Export results", str(Path(self._last_dir()) / "results.vtu"),
                                              "VTK for ParaView (*.vtu);;CSV nodal table (*.csv);;JSON summary (*.json)")
        if not path:
            return
        export_results(self.session, Path(path))
        self.console.append(f"Exported results to {path}")

    def cmd_report(self) -> None:
        from opencae.postprocessing.report import write_report
        s = self.session
        default = (s.data_dir / "report" / f"{s.project.name.replace(' ', '_')}_report.html")
        path, filt = QFileDialog.getSaveFileName(self, "Export report", str(default),
                                                 "HTML report (*.html);;PDF report (*.pdf)")
        if not path:
            return
        images = self._report_images(Path(path).parent)
        if path.lower().endswith(".pdf"):
            html_path = Path(path).with_suffix(".html")
            write_report(s, html_path, images)
            self._html_to_pdf(html_path, Path(path))
            s.last_report = Path(path)
        else:
            write_report(s, Path(path), images)
        self.console.append(f"Report written to {path}")
        self.refresh_all()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _report_images(self, folder: Path) -> list[Path]:
        """Capture standard figures for the report, then restore the view."""
        folder.mkdir(parents=True, exist_ok=True)
        imgs = []
        mode = self.view_mode
        cam = self.viewport.plotter.camera_position
        try:
            self.view_mode = "geometry"
            self.refresh_view()
            self.viewport.plotter.camera_position = cam
            imgs.append(self.viewport.screenshot(folder / "model_and_boundary_conditions.png"))
            if self.session.mesh is not None:
                self.view_mode = "mesh"
                self.refresh_view()
                self.viewport.plotter.camera_position = cam
                imgs.append(self.viewport.screenshot(folder / "finite_element_mesh.png"))
            if self.session.results is not None:
                self._fill_result_quantities()
                for key in ("U_mag", "S_vm"):
                    if key in self.session.results.available():
                        self._current_result = key
                        self.view_mode = "results"
                        self._fill_result_quantities()
                        self.refresh_results_view()
                        self.viewport.plotter.camera_position = cam
                        imgs.append(self.viewport.screenshot(folder / f"{QUANTITIES[key].label.lower().replace(' ', '_')}.png"))
        finally:
            self.view_mode = mode
            self.refresh_view()
            self.viewport.plotter.camera_position = cam
            self.viewport.plotter.render()
        return imgs

    def _html_to_pdf(self, html_path: Path, pdf_path: Path) -> None:
        from PySide6.QtGui import QPageSize, QPdfWriter, QTextDocument
        doc = QTextDocument()
        doc.setBaseUrl(QUrl.fromLocalFile(str(html_path.parent) + "/"))
        doc.setHtml(html_path.read_text(encoding="utf-8"))
        w = QPdfWriter(str(pdf_path))
        w.setPageSize(QPageSize(QPageSize.A4))
        w.setResolution(110)
        doc.setPageSize(w.pageLayout().paintRectPixels(w.resolution()).size().toSizeF())
        doc.print_(w)

    # ================================================================== edit commands
    def cmd_undo(self) -> None:
        if not self.undo.can_undo():
            return
        self.viewport.stop_animation()
        self.session.project = self.undo.undo(self.session.project)
        self.session.reload_derived()
        self.dirty = True
        self.refresh_all()
        self.statusBar().showMessage("Undo", 2000)

    def cmd_redo(self) -> None:
        if not self.undo.can_redo():
            return
        self.session.project = self.undo.redo(self.session.project)
        self.session.reload_derived()
        self.dirty = True
        self.refresh_all()
        self.statusBar().showMessage("Redo", 2000)

    # ================================================================== view commands
    def cmd_projection(self) -> None:
        on = self.A["ortho"].isChecked()
        self.viewport.set_parallel(on)
        self.statusBar().showMessage("Orthographic projection" if on else "Perspective projection", 2000)

    def cmd_render_mode(self, mode: str) -> None:
        self.viewport.render_mode = mode
        self.refresh_view()

    def cmd_background(self) -> None:
        c = QColorDialog.getColor(parent=self, title="Viewport background")
        if c.isValid():
            top = c.lighter(170).name() if c.lightness() < 128 else None
            self.viewport.set_background(c.name(), top)

    def cmd_screenshot(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save screenshot", str(Path(self._last_dir()) / "screenshot.png"),
                                              "PNG image (*.png)")
        if path:
            if not path.lower().endswith(".png"):
                path += ".png"
            self.viewport.screenshot(Path(path), scale=2)
            self.console.append(f"Screenshot saved to {path}")

    # ================================================================== geometry commands
    def _bodies(self):
        return self.session.bodies, self.session.regen.order

    def cmd_primitive(self, shape: str) -> None:
        dlg = ParamDialog(f"Create {D.PRIMITIVE_LABELS[shape]}", D.primitive_fields(shape, self.us), self,
                          note=D.PRIMITIVE_NOTES.get(shape, ""))
        if not dlg.exec():
            return
        v = dict(dlg.result_values)
        if shape == "cantilever_beam":
            force = v.pop("force")
            before = self.session.project.clone()
            self.session.create_cantilever(v["length"], v["width"], v["height"], force, position=v["position"])
            self.undo.push("Create cantilever template", before)
            self.dirty = True
            self.session.regenerate()
            self.refresh_all(fit=True)
            return
        feat = Feature("primitive", {"shape": shape, **v})
        self.mutate(f"Create {shape}", lambda: self.session.add_feature(feat), regen=False)
        self.viewport.fit()

    def cmd_transform(self) -> None:
        b, o = self._bodies()
        dlg = ParamDialog("Transform body", D.transform_fields(b, o, self.us), self,
                          note="Applied in order: scale, rotate, translate.")
        if dlg.exec():
            self.mutate("Transform", lambda: self.session.add_feature(Feature("transform", dlg.result_values)),
                        regen=False)

    def cmd_boolean(self, op: str) -> None:
        b, o = self._bodies()
        dlg = ParamDialog(f"Boolean {op}", D.boolean_fields(b, o, op), self,
                          validator=lambda v: "Target and tool must be different bodies." if v["target"] == v["tool"] else "")
        if dlg.exec():
            v = dlg.result_values
            self.mutate(f"Boolean {v['op']}", lambda: self.session.add_feature(Feature("boolean", v,
                                                                                        name=f"{v['op'].title()}")),
                        regen=False)
            if any(fid for fid in self.session.regen.errors):
                QMessageBox.warning(self, "Boolean failed", "\n".join(self.session.regen.errors.values()) +
                                    "\n\nUse Undo to revert.")

    def cmd_edge_op(self, kind: str) -> None:
        b, o = self._bodies()
        sel = [p.index for p in self.selection if p.kind == "edge"]
        body_id = next((p.body for p in self.selection if p.kind == "edge"), None) or \
            (self.session.analysis_body().id if self.session.analysis_body() else None)
        dlg = ParamDialog(f"{kind.title()} edges", D.edge_op_fields(kind, b, o, self.us, preselect=sel, body_id=body_id),
                          self, note="Tip: switch to edge selection mode and Ctrl+click edges before opening this "
                                     "dialog. With no edges checked, all edges are processed.")
        if dlg.exec():
            v = dlg.result_values
            if not v["edges"]:
                v["edges"] = "all"
            self.mutate(kind.title(), lambda: self.session.add_feature(Feature(kind, v)), regen=False)
            errs = self.session.regen.errors
            if errs:
                QMessageBox.warning(self, f"{kind.title()} failed", "\n".join(errs.values()) +
                                    "\n\nTry a smaller value or fewer edges; use Undo to revert.")

    def cmd_extrude(self) -> None:
        b, o = self._bodies()
        dlg = ParamDialog("Extrude profile", D.extrude_fields(b, o, self.us), self,
                          note="Creates a prism from a planar profile; optionally fuses it with an existing body.")
        if dlg.exec():
            v = dlg.result_values
            if not v.get("fuse_with"):
                v.pop("fuse_with", None)
            self.mutate("Extrude", lambda: self.session.add_feature(Feature("extrude", v)), regen=False)

    def cmd_check_geometry(self, body_id: Optional[str] = None) -> None:
        s = self.session
        issues = []
        for fid, msg in s.regen.errors.items():
            issues.append(Issue("error", msg, "Feature"))
        for bid in ([body_id] if body_id else s.regen.order):
            b = s.bodies.get(bid)
            if b is None:
                continue
            c = b.check
            if not c.issues:
                issues.append(Issue("info", f"valid closed solid, volume {b.volume:.6g} {self.us.length}^3, "
                                            f"{c.n_faces} faces, {c.n_edges} edges", b.name))
            for i in c.issues:
                issues.append(Issue("error" if not c.valid or not c.is_solid else "warning", i, b.name))
        self.issues.show_issues(issues)
        self.bottom.setCurrentWidget(self.issues)
        self.console.append("Geometry check: " + "; ".join(f"{i.source}: {i.message}" for i in issues))

    def cmd_stl_to_solid(self, body_id: Optional[str] = None) -> None:
        p = self.session.project
        target = body_id or next((b for b, x in self.session.bodies.items() if x.kind == "tessellated"), None)
        f = next((x for x in p.geometry.features if x.id == target and x.kind == "import"), None)
        if f is None:
            return
        self.mutate("Convert STL to solid", lambda: f.params.__setitem__("to_solid", True))
        if f.id in self.session.regen.errors:
            QMessageBox.warning(self, "Conversion failed", self.session.regen.errors[f.id] + "\n\nUse Undo to revert.")

    # ================================================================== mesh commands
    def cmd_mesh_settings(self) -> None:
        self.on_tree_selected("mesh_settings", "")
        self.props_dock.raise_()

    def cmd_refinement(self) -> None:
        body = self.session.analysis_body()
        if body is None:
            return
        sel = self._selected_targets(("face", "edge"))
        opts = D.target_options(body, ("face", "edge"))
        size = (self.session.project.mesh_settings.size_max or body.size / 20) / 4
        dlg = ParamDialog("Local mesh refinement",
                          [Field("name", "Name", "str", "Local refinement"),
                           Field("size", "Target element size", "float", size, self.us.length, minimum=1e-12),
                           Field("targets", "Faces / edges", "multicheck", sel, options=opts)], self,
                          validator=lambda v: "Select at least one face or edge." if not v["targets"] else "")
        if dlg.exec():
            v = dlg.result_values
            refs = [make_ref(body, k, int(i)) for k, i in (t.split(":") for t in v["targets"])]
            r = MeshRefinement(targets=refs, size=v["size"], name=v["name"])
            self.mutate("Add mesh refinement", lambda: self.session.project.mesh_settings.refinements.append(r),
                        regen=False)

    def cmd_mesh_color(self, q: str) -> None:
        self.mesh_color = q
        self.set_view_mode("mesh")

    def cmd_generate_mesh(self) -> None:
        if self.proc is not None:
            return
        try:
            job = self.session.mesh_job()
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot mesh", str(exc))
            return
        jf = Path(job["out"]).with_suffix(".job.json")
        Path(job["out"]).unlink(missing_ok=True)
        jf.write_text(json.dumps(job))
        self._job = job
        from opencae.app.main import worker_command
        prog, args = worker_command("opencae.meshing.worker", [str(jf)])
        self._start_process("mesh", prog, args, str(jf.parent))
        self.solver_panel.start("Meshing")
        self.bottom.setCurrentWidget(self.solver_panel)

    # ================================================================== materials
    def cmd_assign_material(self) -> None:
        s = self.session
        p = s.project
        b, o = self._bodies()
        body = s.analysis_body()
        cur = p.material_for_body(body.id) if body else None
        dlg = ParamDialog("Assign material", [
            Field("body", "Body", "choice", body.id if body else "", options=D.body_options(b, o)),
            Field("material", "Material", "choice", cur.id if cur else (p.materials[0].id if p.materials else ""),
                  options=[(m.id, m.name) for m in p.materials])], self, note=EXAMPLE_WARNING)
        if dlg.exec():
            v = dlg.result_values
            self.mutate("Assign material", lambda: p.assign_material(v["body"], v["material"]), regen=False)

    def cmd_new_material(self) -> None:
        m = custom_material()
        dlg = ParamDialog("New material", D.material_fields(m, self.us), self,
                          note="Isotropic linear-elastic material. Values are in the project unit system.")
        if dlg.exec():
            D.apply_material_values(m, dlg.result_values, self.us)
            self.mutate(f"New material {m.name}", lambda: self.session.project.materials.append(m), regen=False)

    def cmd_edit_material(self) -> None:
        p = self.session.project
        if not p.materials:
            return
        names = [m.name for m in p.materials]
        name, ok = QInputDialog.getItem(self, "Edit material", "Material:", names, 0, False)
        if ok:
            m = p.materials[names.index(name)]
            dlg = ParamDialog(f"Edit {m.name}", D.material_fields(m, self.us), self)
            if dlg.exec():
                self.mutate(f"Edit material {m.name}", lambda: D.apply_material_values(m, dlg.result_values, self.us),
                            regen=False)

    # ================================================================== BCs and loads
    def _refs_from(self, body, targets: list[str]):
        return [make_ref(body, k, int(i)) for k, i in (t.split(":") for t in targets)]

    def _apply_bc_values(self, c: Constraint, v: dict) -> None:
        body = self.session.analysis_body()
        c.name = v["name"]
        c.targets = self._refs_from(body, v["targets"])
        if c.kind == "displacement":
            c.params = {k: v[k] for k in ("ux", "uy", "uz")}

    def _apply_load_values(self, l: Load, v: dict) -> None:
        body = self.session.analysis_body()
        l.name = v["name"]
        if "targets" in v:
            l.targets = self._refs_from(body, v["targets"])
        for k in ("magnitude", "direction", "coordinate_system"):
            if k in v:
                l.params[k] = v[k]

    def cmd_constraint(self, kind: str) -> None:
        body = self.session.analysis_body()
        if body is None:
            return
        pre = self._selected_targets(("face", "edge", "vertex") if kind != "symmetry" else ("face",))
        fields = D.constraint_fields(kind, body, self.us, preselect=pre)

        def validate(v):
            if not v["targets"]:
                return "Select at least one target entity."
            if kind == "displacement" and all(v[k] is None for k in ("ux", "uy", "uz")):
                return "Specify at least one displacement component."
            return ""
        dlg = ParamDialog({"fixed": "Fixed support", "displacement": "Prescribed displacement",
                           "symmetry": "Symmetry constraint"}[kind], fields, self, validator=validate,
                          note="Targets are pre-checked from the viewport selection.")
        if dlg.exec():
            c = Constraint(kind, [])
            self._apply_bc_values(c, dlg.result_values)
            self.mutate(f"Add {c.name}", lambda: self.session.project.constraints.append(c), regen=False)

    def cmd_load(self, kind: str) -> None:
        body = self.session.analysis_body()
        if body is None:
            return
        pre = self._selected_targets(D.LOAD_TARGETS[kind])

        def validate(v):
            if D.LOAD_TARGETS[kind] and not v.get("targets"):
                return "Select at least one target entity."
            if "direction" in v and np.linalg.norm(v["direction"]) < 1e-12 and v.get("coordinate_system") != "face_normal":
                return "Direction vector must be non-zero."
            return ""
        notes = {"force": "Total force distributed as a uniform traction over the selected faces (consistent nodal "
                          "loads), or split equally between selected vertices.",
                 "pressure": "Positive pressure pushes into the surface (opposite to the outward normal).",
                 "gravity": "Body force rho*g on the whole analysis body; requires material density."}
        dlg = ParamDialog(f"Add {kind}", D.load_fields(kind, body, self.us, preselect=pre), self, validator=validate,
                          note=notes[kind])
        if dlg.exec():
            l = Load(kind, [], {})
            if kind != "force":
                l.params["coordinate_system"] = "global"
            self._apply_load_values(l, dlg.result_values)
            self.mutate(f"Add {l.name}", lambda: self.session.project.loads.append(l), regen=False)

    # ================================================================== analysis
    def cmd_analysis_settings(self) -> None:
        self.on_tree_selected("solver", "")
        self.props_dock.raise_()

    def cmd_validate(self) -> list[Issue]:
        issues, rm = self.session.validate()
        self.issues.show_issues(issues)
        self.bottom.setCurrentWidget(self.issues)
        nerr = sum(1 for i in issues if i.severity == ERROR)
        msg = f"Validation: {nerr} error(s), {len(issues) - nerr} warning(s)"
        self.console.append(msg, logging.ERROR if nerr else logging.INFO)
        self.statusBar().showMessage(msg, 6000)
        return issues

    def cmd_solve(self) -> None:
        if self.proc is not None:
            return
        if not self.session.backend.executable():
            D.TextDialog("CalculiX not found", "<pre>" + INSTALL_HELP + "</pre>", self).exec()
            return
        job_dir, jobname, issues = self.session.prepare_solve()
        self.issues.show_issues(issues)
        if not jobname:
            self.bottom.setCurrentWidget(self.issues)
            QMessageBox.warning(self, "Model not ready", "The model has errors; see Errors & warnings:\n\n" +
                                "\n".join(str(i) for i in issues if i.severity == ERROR)[:2000])
            return
        self._job = {"dir": str(job_dir), "jobname": jobname}
        exe = self.session.backend.executable()
        cmd = command_line(exe, jobname)
        self._start_process("solve", cmd[0], cmd[1:], str(job_dir), solver_env(self.session.project.analysis.threads))
        self.solver_panel.start(f"Solving with CalculiX ({self.session.mesh.n_nodes} nodes)")
        self.bottom.setCurrentWidget(self.solver_panel)

    def cmd_cancel(self) -> None:
        if self.proc is not None:
            self.console.append(f"Cancelling {self.proc_kind}…", logging.WARNING)
            self._cancelled = True
            self.proc.kill()

    # ------------------------------------------------------------------ process handling
    def _start_process(self, kind: str, program: str, args: list[str], cwd: str,
                       env: Optional[dict] = None) -> None:
        self.proc = QProcess(self)
        self.proc_kind = kind
        self._proc_buf = ""
        self._proc_log: list[str] = []
        self._cancelled = False
        self.proc.setWorkingDirectory(cwd)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        qenv = QProcessEnvironment.systemEnvironment()
        for k, v in (env or {}).items():
            qenv.insert(k, v)
        if kind == "mesh":
            root = str(Path(__file__).resolve().parents[2])
            qenv.insert("PYTHONPATH", root + os.pathsep + qenv.value("PYTHONPATH", ""))
        self.proc.setProcessEnvironment(qenv)
        self.proc.readyRead.connect(self._proc_output)
        self.proc.finished.connect(self._proc_finished)
        self.proc.errorOccurred.connect(self._proc_error)
        self.proc.start(program, args)
        self.update_actions()

    def _proc_output(self) -> None:
        data = bytes(self.proc.readAll()).decode(errors="replace")
        self._proc_buf += data
        *lines, self._proc_buf = self._proc_buf.split("\n")
        for ln in lines:
            ln = ln.rstrip()
            if not ln.strip():
                continue
            self._proc_log.append(ln)
            self.solver_panel.line(ln)
            table = MESH_STAGES if self.proc_kind == "mesh" else SOLVER_STAGES
            for key, pct in table.items():
                if key in ln:
                    self.solver_panel.stage(ln.replace("PROGRESS: ", "")[:80], pct)
                    break

    def _proc_error(self, err) -> None:
        if self.proc is not None and err == QProcess.FailedToStart:
            self.solver_panel.finish(False, f"Failed to start {self.proc_kind} process")
            self.console.append(f"Failed to start process: {self.proc.errorString()}", logging.ERROR)
            self.proc = None
            self.update_actions()

    def _proc_finished(self, code: int, status) -> None:
        if self.proc is None:
            return
        if self._proc_buf:
            self._proc_log.append(self._proc_buf)
            self.solver_panel.line(self._proc_buf)
        kind = self.proc_kind
        self.proc = None
        cancelled = self._cancelled
        text = "\n".join(self._proc_log)
        try:
            if kind == "mesh":
                self._mesh_finished(code, cancelled, text)
            elif kind == "solve":
                self._solve_finished(code, cancelled, text)
            elif kind == "benchmarks":
                ok = code == 0 and not cancelled
                self.solver_panel.finish(ok, "Benchmarks passed" if ok else "Benchmarks failed or cancelled")
                rep = Path(self._job["dir"]) / "validation_report.html"
                if rep.exists():
                    self.console.append(f"Validation report: {rep}")
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(rep)))
        finally:
            self.update_actions()

    def _mesh_finished(self, code: int, cancelled: bool, text: str) -> None:
        out = Path(self._job.get("out", ""))
        if cancelled:
            self.solver_panel.finish(False, "Meshing cancelled")
            return
        if code != 0 or not out.exists():
            err = next((l for l in text.splitlines() if l.startswith("ERROR")), f"exit code {code}")
            self.solver_panel.finish(False, "Meshing failed")
            self.console.append(f"Meshing failed: {err}", logging.ERROR)
            QMessageBox.warning(self, "Meshing failed", err)
            return
        mesh = FEMesh.load(out)
        before = self.session.project.clone()
        self.session.set_mesh(mesh)
        self.undo.push("Generate mesh", before)
        self.dirty = True
        st = mesh.stats()
        self.solver_panel.finish(True, f"Mesh: {st['nodes']} nodes, {st['elements']} {st['element_type']} elements")
        self.console.append(f"Mesh generated: {st['nodes']} nodes, {st['elements']} {st['element_type']} elements, "
                            f"min gamma {st.get('gamma_min', 0):.3f}, min scaled Jacobian {st.get('sj_min', 0):.3f}")
        for w in mesh.warnings():
            self.console.append("Mesh warning: " + w, logging.WARNING)
        self.view_mode = "mesh"
        self.refresh_all()
        self.bottom.setCurrentWidget(self.mesh_diag)

    def _solve_finished(self, code: int, cancelled: bool, text: str) -> None:
        job_dir = Path(self._job["dir"])
        jobname = self._job["jobname"]
        ok, errors, warnings = analyse_output(text, code, job_dir / f"{jobname}.frd")
        if cancelled:
            ok, errors = False, ["Cancelled by user."]
        for w in warnings[:20]:
            self.console.append("Solver: " + w, logging.WARNING)
        if not ok:
            self.solver_panel.finish(False, "Solver failed" if not cancelled else "Solver cancelled")
            self.issues.show_issues([Issue("error", e, "CalculiX") for e in errors[:50]])
            for e in errors[:10]:
                self.console.append("Solver: " + e, logging.ERROR)
            if not cancelled:
                self.bottom.setCurrentWidget(self.issues)
                QMessageBox.warning(self, "Solver failed", "\n".join(errors[:10]) +
                                    f"\n\nSee {job_dir / (jobname + '.inp')} and the solver log.")
            return
        try:
            rs = self.session.load_results(job_dir, jobname)
        except Exception as exc:
            self.solver_panel.finish(False, "Reading results failed")
            QMessageBox.critical(self, "Reading results failed", str(exc))
            return
        self.dirty = True
        sm = rs.summary()
        self.solver_panel.finish(True, "Solution complete")
        self.console.append(f"Solved. Max displacement {sm['max_displacement']:.6g} {self.us.length}" +
                            (f", max von Mises (nodal) {sm['max_von_mises_nodal']:.6g} {self.us.stress}"
                             if 'max_von_mises_nodal' in sm else ""))
        self._current_result = "S_vm" if rs.stress is not None else "U_mag"
        self.refresh_all()
        self.set_view_mode("results")
        self.bottom.setCurrentWidget(self.summary)

    # ================================================================== results
    def _unit_for(self, kind: str) -> str:
        return {"displacement": self.us.length, "stress": self.us.stress, "strain": "-"}.get(kind, "")

    def _fill_result_quantities(self) -> None:
        rs = self.session.results
        if rs is None:
            return
        items = [(k, QUANTITIES[k].label) for k in rs.available()]
        self.results_panel.set_quantities(items, self._current_result)

    def refresh_results_view(self) -> None:
        rs = self.session.results
        if rs is None or self.view_mode != "results":
            return
        key = self.results_panel.current_key() or self._current_result
        if key not in rs.available():
            key = "U_mag"
        self._current_result = key
        q = QUANTITIES[key]
        vals = rs.quantity(key)
        unit = self._unit_for(q.unit_kind)
        auto = auto_deformation_scale(rs.mesh.nodes, rs.displacement)
        scale = self.results_panel.scale_value(auto)
        loc = "nodal" if q.location == "nodal" else "nodal, extrapolated & averaged"
        title = f"{q.label} [{unit}]" + ("" if q.location == "nodal" else "\n(nodal avg.)")
        rp = self.results_panel
        self.viewport.show_results(rs.mesh, vals, rs.displacement, title, scale, rp.clim(), rp.undeformed.isChecked(),
                                   rp.edges.isChecked(), rp.cmap.currentText(), rp.bands.value(),
                                   rp.minmax.isChecked())
        stale = "" if self.session.project.results_are_current() else \
            f"<br><span style='color:{theme.WARN}'>⚠ Results are out of date with the model.</span>"
        rp.location.setText(f"Location: {loc}. Units: {unit}." + (" Shear strains are tensor components."
                                                                  if key in ("E_xy", "E_yz", "E_zx") else ""))
        rp.info.setText(f"<b>{q.label}</b> [{unit}]<br>min {vals.min():.6g} &nbsp; max {vals.max():.6g}<br>"
                        f"Deformation scale: {scale:g}× {'(auto)' if rp.scale_mode.currentIndex() == 0 else ''}"
                        + stale)
        if not rp.manual.isChecked():
            rp.cmin.setText(f"{vals.min():.6g}")
            rp.cmax.setText(f"{vals.max():.6g}")
        self.A["res_animate"].setChecked(False)
        rp.anim_btn.blockSignals(True)
        rp.anim_btn.setChecked(False)
        rp.anim_btn.blockSignals(False)

    def cmd_result(self, key: str) -> None:
        self._current_result = key
        self.set_view_mode("results")
        i = self.results_panel.quantity.findData(key)
        if i >= 0:
            self.results_panel.quantity.setCurrentIndex(i)

    def cmd_animate(self) -> None:
        on = self.A["res_animate"].isChecked()
        self.results_panel.anim_btn.setChecked(on)

    def on_animate_toggled(self, on: bool) -> None:
        rs = self.session.results
        if rs is None:
            return
        if self.view_mode != "results":
            self.set_view_mode("results")
        if on:
            scale = self.results_panel.scale_value(auto_deformation_scale(rs.mesh.nodes, rs.displacement))
            if scale == 0:
                scale = auto_deformation_scale(rs.mesh.nodes, rs.displacement)
            self.viewport.start_animation(scale)
        else:
            self.viewport.stop_animation()
        self.A["res_animate"].setChecked(on)

    def cmd_probe_help(self) -> None:
        self.set_view_mode("results")
        self.results_dock.raise_()
        self.statusBar().showMessage("Probe: click on the model to read the nodal value of the current quantity.", 6000)

    def cmd_compare(self) -> None:
        from opencae.postprocessing.theory import cantilever_comparison
        c = cantilever_comparison(self.session)
        if c is None:
            QMessageBox.information(self, "Compare with theory",
                                    "A closed-form comparison is available for the cantilever template (rectangular "
                                    "beam, one fixed end face, one transverse tip force). This model does not match "
                                    "that configuration.")
            return
        rows = "".join(f"<tr><td>{q['name']}</td><td>{q['theory']:.6g}</td><td>{q['fe']:.6g}</td><td>{q['unit']}</td>"
                       f"<td>{q['diff_pct']:+.2f} %</td></tr>" for q in c.quantities)
        html = (f"<h3>{c.case}</h3><table border=1 cellpadding=4><tr><th>Quantity</th><th>Theory</th><th>FE</th>"
                f"<th>Unit</th><th>Diff</th></tr>{rows}</table><h4>Assumptions</h4><ul>" +
                "".join(f"<li>{a}</li>" for a in c.assumptions) + "</ul>")
        self.summary.setHtml(html)
        self.bottom.setCurrentWidget(self.summary)

    def _update_summary(self) -> None:
        s = self.session
        rs = s.results
        if rs is None:
            mesh_txt = f"{s.mesh.n_nodes} nodes / {s.mesh.n_elements} {s.mesh.element_type}" if s.mesh else "none"
            self.summary.setHtml(f"<h3>{s.project.name}</h3><p>Mesh: {mesh_txt}</p><p>No results yet.</p>")
            return
        sm = rs.summary()
        us = self.us
        rows = [("Max displacement", f"{sm['max_displacement']:.6g} {us.length}", f"node {sm['max_displacement_node']}")]
        if "max_von_mises_nodal" in sm:
            rows.append(("Max von Mises (nodal avg.)", f"{sm['max_von_mises_nodal']:.6g} {us.stress}",
                         f"node {sm['max_von_mises_node']}"))
        if "max_von_mises_integration_point" in sm:
            rows.append(("Max von Mises (integration pts)", f"{sm['max_von_mises_integration_point']:.6g} {us.stress}",
                         "raw solver output"))
        if "max_principal" in sm:
            rows.append(("Principal stress max / min", f"{sm['max_principal']:.6g} / {sm['min_principal']:.6g} {us.stress}", ""))
        from opencae.postprocessing.results import equilibrium_check
        e = equilibrium_check({**sm, **s.project.results.summary})
        eq = ""
        if e:
            eq = (f"<p>Reactions: {np.round(e['reactions'], 6).tolist()} {us.force}; applied: "
                  f"{np.round(e['applied'], 6).tolist()} {us.force} — <span class='{'ok' if e['ok'] else 'err'}'>"
                  f"equilibrium {'OK' if e['ok'] else 'NOT satisfied'} (rel. residual {e['relative']:.1e})</span></p>")
        mat = s.project.material_for_body(s.analysis_body().id) if s.analysis_body() else None
        fos = ""
        if mat and mat.yield_strength and "max_von_mises_nodal" in sm:
            fos = f"<p>Yield safety factor (nodal max): <b>{mat.yield_strength * us.stress_from_si / sm['max_von_mises_nodal']:.3g}</b></p>"
        stale = "" if s.project.results_are_current() else "<p class='warn'>⚠ Results are out of date with the model.</p>"
        self.summary.setHtml(f"<h3>{s.project.name} — {s.project.results.solver_version}</h3>{stale}<table>" +
                             "".join(f"<tr><th align=left>{a}</th><td>{b}</td><td>{c}</td></tr>" for a, b, c in rows) +
                             f"</table>{eq}{fos}<p style='color:{theme.FG_DIM}'>Solve time "
                             f"{s.project.results.wall_time_s:.2f} s. Nodal stresses are extrapolated from integration "
                             "points and averaged by CalculiX.</p>")

    # ================================================================== workflow
    def on_workflow_clicked(self, stage: str) -> None:
        acts = {"Project": self.cmd_save, "Geometry": lambda: self.ribbon.setCurrentIndex(1),
                "Material": self.cmd_assign_material, "Mesh": self.cmd_generate_mesh,
                "Constraints": lambda: self.ribbon.setCurrentIndex(3), "Loads": lambda: self.ribbon.setCurrentIndex(3),
                "Analysis": self.cmd_analysis_settings, "Solve": self.cmd_solve,
                "Results": lambda: self.set_view_mode("results"), "Report": self.cmd_report}
        fn = acts.get(stage)
        if fn:
            self._safe(fn)

    # ================================================================== tools / help
    def cmd_settings(self) -> None:
        cur = self.settings.value("ccx_path", "", str)
        dlg = ParamDialog("Settings", [Field("ccx", "CalculiX executable (empty = auto-detect)", "str", cur)], self,
                          note=f"Detected: {self.session.backend.executable() or 'not found'}")
        if dlg.exec():
            path = dlg.result_values["ccx"].strip()
            self.settings.setValue("ccx_path", path)
            from opencae.solver.base import CalculixBackend
            self.session.backend = CalculixBackend(path or None)
            self.refresh_all()

    def cmd_benchmarks(self) -> None:
        if self.proc is not None:
            return
        out = self.session.data_dir / "validation"
        out.mkdir(parents=True, exist_ok=True)
        self._job = {"dir": str(out)}
        from opencae.app.main import worker_command
        prog, args = worker_command("opencae.examples.run_validation", ["--out", str(out), "--quick"])
        self._start_process("benchmarks", prog, args, str(out))
        self.solver_panel.start("Validation benchmarks")
        self.bottom.setCurrentWidget(self.solver_panel)

    def cmd_open_data(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.session.data_dir)))

    def cmd_guide(self) -> None:
        doc = Path(__file__).resolve().parents[2] / "docs" / "USER_GUIDE.md"
        text = doc.read_text(encoding="utf-8") if doc.exists() else "User guide not found."
        dlg = D.TextDialog("User guide", "", self, 900, 700)
        tb = dlg.findChild(__import__("PySide6.QtWidgets", fromlist=["QTextBrowser"]).QTextBrowser)
        tb.setMarkdown(text)
        dlg.exec()

    def cmd_about(self) -> None:
        import gmsh
        import pyvista
        import vtk
        QMessageBox.about(self, "About OpenCAE Studio",
                          f"<h3>OpenCAE Studio {__version__}</h3><p>Open-source CAD, meshing and linear static FEA.</p>"
                          f"<p>Geometry kernel: OpenCascade (OCP)<br>Mesher: Gmsh {gmsh.__version__}<br>"
                          f"Solver: CalculiX {self.session.backend.version()}<br>Visualisation: VTK "
                          f"{vtk.vtkVersion.GetVTKVersion()} / PyVista {pyvista.__version__}</p>"
                          "<p>Results are only as good as the model. Verify material data, boundary conditions and "
                          "mesh convergence before making engineering decisions.</p>")

    # ================================================================== shutdown
    def closeEvent(self, ev) -> None:
        if not self._confirm_discard():
            ev.ignore()
            return
        if self.proc is not None:
            self.proc.kill()
            self.proc.waitForFinished(2000)
        logging.getLogger("opencae").removeHandler(self.log_handler)
        self.viewport.close_plotter()
        ev.accept()
