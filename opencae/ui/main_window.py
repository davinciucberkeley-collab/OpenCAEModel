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
from opencae.solver.calculix.runner import INSTALL_HELP, analyse_output, write_solver_log
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

CAD_SUFFIXES = (".step", ".stp", ".iges", ".igs", ".brep", ".stl")
CAD_FILTER = "CAD files (*.step *.stp *.iges *.igs *.brep *.stl);;STEP (*.step *.stp);;IGES (*.iges *.igs);;BREP (*.brep);;STL (*.stl)"
MESH_STAGES = {"Importing": 5, "Mapping": 10, "curves": 20, "surfaces": 35, "volume": 60, "second order": 80,
               "Extracting": 88, "quality": 94, "Done": 100}
SOLVER_STAGES = {"Reading the SU2": 5, "Geometry preprocessing": 10, "Begin solver": 20, "Convergence history": 25,
                 "All convergence criteria": 95, "Writing": 97, "Decascading": 10, "Determining the structure": 20, "number of equations": 30,
                 "Factoring": 50, "stress calculation": 85, "Job finished": 100}


class MainWindow(QMainWindow):
    def __init__(self, session: Optional[Session] = None, settings: Optional[QSettings] = None):
        super().__init__()
        self.settings = settings or QSettings("OpenCAE", "OpenCAE Studio")
        self.session = session or Session()
        if session is None:
            self.session.new()
        self.undo = UndoStack()
        self.dirty = False
        self.setAcceptDrops(True)
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
        elif os.environ.get("OPENCAE_SKIP_SOLVER_CHECK") != "1":
            QTimer.singleShot(300, self._verify_solver_background)

    def _verify_solver_background(self) -> None:
        """Run the real solver self-test in a thread so readiness is proven, not assumed."""
        from opencae.ui.solver_settings import run_selftest_async
        if getattr(self, "_solver_thread", None) is not None:
            return
        self._tested_backend = self.session.backend
        self.solver_label.setText("Solver: testing CalculiX…  ")
        # thread owned by the application; result delivered to a bound slot, which Qt disconnects
        # automatically if this window is destroyed first
        self._solver_thread = run_selftest_async(QApplication.instance(), self._tested_backend, self._solver_checked)

    def _solver_checked(self, rep) -> None:
        self._solver_thread = None
        if self.session.backend is not self._tested_backend:
            return
        if rep.ok:
            self.console.append(f"CalculiX {rep.version} self-test passed ({rep.exe}).")
        else:
            self.console.append("CalculiX self-test FAILED - solving will not work until this is fixed "
                                "(Tools > Settings > Test Solver):\n" + rep.summary(), logging.ERROR)
        self.refresh_all()

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
        a("sel_tangent", "Select tangent-connected faces", self.cmd_select_tangent, "select_face")
        a("sel_body_faces", "Select all faces of body", self.cmd_select_body_faces, "select_face")
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
        a("asm_check", "Check contacts / interferences", self.cmd_assembly_check, "inspect")
        a("int_bonded", "Bonded interaction…", lambda: self.cmd_interaction("bonded"), "fixed")
        a("int_contact", "Contact interaction…", lambda: self.cmd_interaction("contact"), "symmetry")
        a("int_detect", "Detect touching faces…", self.cmd_detect_interactions, "inspect")
        a("body_isolate", "Isolate selected bodies", self.cmd_isolate, "view_iso")
        a("body_show_all", "Show all bodies", self.cmd_show_all, "box")
        a("study_new", "New study…", self.cmd_new_study, "solve")
        a("study_bodies", "Bodies in study…", self.cmd_study_bodies, "box")
        a("study_dashboard", "Studies dashboard", self.cmd_studies_dashboard, "inspect")
        a("flow_domain", "External flow domain…", self.cmd_flow_domain, "box")
        a("stl_to_solid", "Convert STL body to solid", self.cmd_stl_to_solid, "check")
        # mesh
        a("mesh_settings", "Mesh settings…", self.cmd_mesh_settings, "mesh_settings")
        a("mesh_refine", "Local refinement on selection…", self.cmd_refinement, "refine")
        a("mesh_generate", "&Generate mesh", self.cmd_generate_mesh, "mesh", "Ctrl+M")
        a("mesh_convergence", "Mesh convergence study…", self.cmd_mesh_convergence, "refine")
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
        a("bc_temp", "Temperature…", lambda: self.cmd_constraint("temperature"), "fixed")
        a("flow_inlet", "Velocity inlet…", lambda: self.cmd_load("velocity_inlet"), "force")
        a("flow_outlet", "Pressure outlet…", lambda: self.cmd_load("pressure_outlet"), "pressure")
        a("flow_wall", "Wall (no slip)…", lambda: self.cmd_constraint("wall"), "fixed")
        a("flow_slip", "Slip wall / symmetry…", lambda: self.cmd_constraint("slip_wall"), "symmetry")
        # thermal loads
        for k in ("heat_flux", "heat_power", "convection", "radiation", "heat_generation", "uniform_temperature"):
            a(f"load_{k}", D.LOAD_NAMES[k] + "…", (lambda kk=k: self.cmd_load(kk)), "force")
        # loads
        a("load_force", "Force…", lambda: self.cmd_load("force"), "force")
        a("load_pressure", "Pressure…", lambda: self.cmd_load("pressure"), "pressure")
        a("load_moment", "Moment…", lambda: self.cmd_load("moment"), "force")
        a("load_remote", "Remote force…", lambda: self.cmd_load("remote_force"), "force")
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
        a("plot_explorer", "Plot Explorer…", self.cmd_plot_explorer, "contour")
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
                       "sel_tangent", "sel_body_faces",
                       "background", "screenshot"])
        menu("&Geometry", ["prim_box", "prim_cylinder", "prim_sphere", "prim_cone", "prim_plate", "prim_beam",
                           "prim_cantilever_beam", "-", "transform", "bool_union", "bool_subtract", "bool_intersect",
                           "fillet", "chamfer", "extrude", "-", "import", "export_geom", "-", "check_geom",
                           "stl_to_solid"])
        menu("&Mesh", ["mesh_settings", "mesh_refine", "mesh_generate", "mesh_convergence", "-", "show_mesh", "mesh_color_none",
                       "mesh_color_gamma", "mesh_color_aspect_ratio", "mesh_color_sj", "mesh_color_size", "-",
                       "export_mesh"])
        menu("Ma&terials", ["mat_assign", "mat_new", "mat_edit"])
        menu("&Loads", ["load_force", "load_remote", "load_moment", "load_pressure", "load_gravity", "load_disp", "-",
                        ("Thermal", ["load_heat_flux", "load_heat_power", "load_convection", "load_radiation",
                                     "load_heat_generation"]), "load_uniform_temperature"])
        menu("&Boundary Conditions", ["bc_fixed", "bc_disp", "bc_sym", "-", "bc_temp", "-",
                                      ("Flow (CFD)", ["flow_inlet", "flow_outlet", "flow_wall", "flow_slip"])])
        menu("&Analysis", ["study_new", "study_bodies", "study_dashboard", "flow_domain", "-", "analysis_settings", "validate",
                           "solve", "cancel"])
        menu("A&ssembly", ["import", "study_bodies", "asm_check", "-", "int_detect", "int_bonded", "int_contact", "-",
                            "body_isolate", "body_show_all"])
        menu("&Results", ["show_results", "plot_explorer", "res_deformed", "res_disp", "res_vm", "res_p1", "res_animate", "res_probe",
                          "res_compare", "-", "export_results", "export_report", "screenshot"])
        menu("&Tools", ["settings", "benchmarks", "open_data"])
        menu("&Help", ["guide", "ccx_help", "about"])

    def _build_ribbon(self) -> None:
        self.ribbon = QTabWidget()
        self.ribbon.setDocumentMode(True)
        # Ribbon tabs follow the simulation workflow. Only commands that work are listed;
        # commands that do not apply to the current study are disabled with the reason in the tooltip.
        pages = {
            "Home": ["new", "open", "save", "-", "undo", "redo", "-", "import", "example", "-", "study_new",
                     "study_dashboard", "-", "validate", "solve", "-", "export_report"],
            "Geometry": ["prim_box", "prim_cylinder", "prim_sphere", "prim_cone", "prim_plate", "prim_beam",
                         "prim_cantilever_beam", "-", "transform", "bool_union", "bool_subtract", "bool_intersect",
                         "fillet", "chamfer", "extrude", "-", "import", "check_geom", "stl_to_solid", "export_geom"],
            "Assembly": ["import", "-", "study_bodies", "asm_check", "-", "int_detect", "int_bonded", "int_contact", "-",
                         "body_isolate", "body_show_all", "-",
                         "sel_body", "sel_face", "sel_tangent", "sel_body_faces"],
            "Materials": ["mat_assign", "mat_new", "mat_edit"],
            "Mesh": ["mesh_settings", "mesh_refine", "mesh_generate", "mesh_convergence", "-", "show_mesh", "mesh_color_gamma",
                     "mesh_color_aspect_ratio", "mesh_color_sj", "mesh_color_size", "-", "export_mesh"],
            "Physics & Setup": ["study_new", "study_bodies", "analysis_settings", "-", "flow_domain", "-",
                                "study_dashboard", "validate"],
            "Boundary Conditions": ["bc_fixed", "bc_disp", "bc_sym", "load_force", "load_remote", "load_moment",
                                    "load_pressure",
                                    "load_gravity",
                                    "-", "bc_temp", "load_heat_flux", "load_heat_power", "load_convection",
                                    "load_radiation", "load_heat_generation", "-", "load_uniform_temperature", "-",
                                    "flow_inlet", "flow_outlet", "flow_wall", "flow_slip"],
            "Solve": ["validate", "solve", "cancel", "-", "settings"],
            "Results": ["show_results", "res_disp", "res_vm", "res_p1", "res_deformed", "res_animate", "res_probe",
                        "res_compare", "-", "plot_explorer", "mesh_convergence", "study_dashboard", "-",
                        "export_results"],
            "Reports": ["export_report", "export_results", "screenshot"],
            "View": ["fit", "view_iso", "view_front", "view_back", "view_top", "view_bottom", "view_left",
                     "view_right", "-", "ortho", "render_shaded", "render_shaded_edges", "render_wireframe", "-",
                     "sel_body", "sel_face", "sel_edge", "sel_vertex", "sel_node", "sel_element", "sel_tangent",
                     "sel_body_faces", "-", "body_isolate", "body_show_all", "background", "screenshot"],
            "Tools": ["settings", "benchmarks", "open_data"],
        }
        short = {"sel_tangent": "Tangent faces", "sel_body_faces": "Body faces", "load_remote": "Remote force", "flow_domain": "Flow domain", "flow_inlet": "Inlet", "flow_outlet": "Outlet", "flow_wall": "Wall", "flow_slip": "Slip wall",
                 "mesh_convergence": "Convergence", "plot_explorer": "Plot Explorer", "study_new": "New study", "study_bodies": "Study bodies", "study_dashboard": "Studies",
                 "asm_check": "Check", "int_detect": "Detect", "int_bonded": "Bonded", "int_contact": "Contact",
                 "body_isolate": "Isolate", "body_show_all": "Show all",
                 "bc_temp": "Temperature", "load_heat_flux": "Heat flux", "load_heat_power": "Heat power",
                 "load_convection": "Convection", "load_radiation": "Radiation", "load_heat_generation": "Heat gen.",
                 "load_uniform_temperature": "Uniform T", "mesh_color_sj": "Jacobian", "mesh_color_size": "Size",
                 "mat_edit": "Edit", "settings": "Solver settings", "benchmarks": "Benchmarks",
                 "open_data": "Data folder", "stl_to_solid": "STL→solid", "cancel": "Cancel",
                 "prim_cantilever_beam": "Cantilever", "bool_union": "Union", "bool_subtract": "Subtract",
                 "bool_intersect": "Intersect", "mesh_generate": "Generate", "mesh_settings": "Settings",
                 "mesh_refine": "Refine", "mesh_color_gamma": "Quality", "mesh_color_aspect_ratio": "Aspect",
                 "transform": "Transform", "render_shaded_edges": "Edges", "render_shaded": "Shaded",
                 "render_wireframe": "Wireframe", "analysis_settings": "Analysis", "res_deformed": "Deformed",
                 "res_vm": "Von Mises", "res_disp": "Displacement", "res_p1": "Principal", "res_animate": "Animate",
                 "res_probe": "Probe", "res_compare": "Theory", "export_report": "Report", "export_results": "Export",
                 "export_mesh": "Export", "export_geom": "Export", "check_geom": "Check", "mat_assign": "Assign",
                 "mat_new": "New material", "bc_fixed": "Fixed", "bc_disp": "Displacement", "bc_sym": "Symmetry",
                 "load_force": "Force", "load_moment": "Moment", "load_pressure": "Pressure", "load_gravity": "Gravity", "show_mesh": "Mesh",
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
        be = self.session.backend
        self.solver_label.setText(f"Solver: {be.status_text()}  ")
        col = theme.OK if be.verified else theme.ERR if (be.verified is False or not be.executable()) else theme.WARN
        self.solver_label.setStyleSheet(f"color:{col};")
        self.solver_label.setToolTip(be.last_report.summary() if be.last_report else (be.executable() or ""))
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
        t = s.project.analysis.analysis_type
        thermal = t in ("thermal_steady", "thermal_transient")
        flow = t.startswith("cfd")
        for n in ("bc_fixed", "bc_disp", "bc_sym", "load_disp"):
            en(n, has_solid and not thermal and not flow,
               "no solid body" if not has_solid else f"not used in a {t.replace('_', ' ')} analysis")
        for n in ("flow_inlet", "flow_outlet", "flow_wall", "flow_slip"):
            en(n, has_solid and flow, "no solid body" if not has_solid else
               "flow conditions need a flow (CFD) study (Physics & Setup > New study)")
        for n in ("load_force", "load_remote", "load_moment", "load_pressure", "load_gravity"):
            en(n, has_solid and t in ("linear_static", "thermal_stress", "buckling"),
               "no solid body" if not has_solid else f"loads are not used in a {t.replace('_', ' ')} analysis")
        for n in ("bc_temp", "load_heat_flux", "load_heat_power", "load_convection", "load_radiation",
                  "load_heat_generation"):
            en(n, has_solid and thermal, "no solid body" if not has_solid else
               "thermal conditions need a thermal study (Physics & Setup > New study)")
        en("load_uniform_temperature", has_solid and t == "thermal_stress",
           "only used by a thermal-stress analysis")
        en("asm_check", nsolids >= 2, "needs two solid bodies")
        nstudy = len([b for b in s.study.analysis_body_ids() if b in s.bodies])
        struct_or_heat = not t.startswith("cfd")
        for n in ("int_bonded", "int_detect"):
            en(n, nstudy >= 2 and struct_or_heat, "needs two bodies in a structural or thermal study")
        en("int_contact", nstudy >= 2 and t in ("linear_static", "thermal_stress"),
           "contact needs two bodies in a static structural study")
        en("flow_domain", nsolids >= 1, "no solid body")
        en("study_bodies", nsolids >= 1, "no solid body")
        sb = s.solver_backend()
        has_ccx = bool(s.backend.executable())
        has_solver = bool(sb is not None and sb.executable())
        why = (f"{sb.name if sb else 'solver'} not installed (see Tools > Settings)" if not has_solver else
               f"{sb.name} failed its self-test (Tools > Settings)" if sb.verified is False else
               "a job is running")
        en("solve", has_solver and sb.verified is not False and not busy, why)
        en("cancel", busy, "no running job")
        res = s.results is not None
        for n in ("show_results", "res_probe", "export_results"):
            en(n, res, "no results - run the solver")
        en("plot_explorer", res or bool(getattr(self, "last_convergence", None)), "no results - run the solver")
        en("mesh_convergence", has_solid and not busy and has_ccx and s.backend.verified is not False,
           "no solid body" if not has_solid else "a job is running" if busy else "solver not available")
        av = set(s.results.available()) if res else set()
        kind = getattr(s.results, "kind", "") if res else ""
        for n, key in (("res_deformed", "U_mag"), ("res_disp", "U_mag"), ("res_vm", "S_vm"), ("res_p1", "S_p1")):
            en(n, key in av, "no results - run the solver" if not res else "not available for this analysis type")
        en("res_animate", res and kind in ("structural", "modal", "buckling"),
           "no results - run the solver" if not res else "nothing moves in a thermal result")
        en("res_compare", res and kind == "structural", "no results - run the solver" if not res else
           "closed-form comparison exists for the static cantilever template")
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
        items = []
        for body in s.analysis_bodies():
            cons, loads = [], []
            for c in s.project.constraints:
                if c.suppressed:
                    continue
                cons.append((c.kind, [t.index for t in c.targets if t.kind == "face" and t.body == body.id
                                      and 0 <= t.index < len(body.faces)],
                             [t.index for t in c.targets if t.kind == "vertex" and t.body == body.id
                              and 0 <= t.index < len(body.vertices)],
                             [t.index for t in c.targets if t.kind == "edge" and t.body == body.id
                              and 0 <= t.index < len(body.edges)]))
            for l in s.project.loads:
                if l.suppressed:
                    continue
                whole = l.kind in ("gravity", "heat_generation", "uniform_temperature")
                if whole and body is not s.analysis_bodies()[0]:
                    continue
                loads.append({"kind": l.kind, "edges": [t.index for t in l.targets if t.kind == "edge"
                                                        and t.body == body.id and 0 <= t.index < len(body.edges)],
                              "faces": [t.index for t in l.targets if t.kind == "face"
                                                        and t.body == body.id and 0 <= t.index < len(body.faces)],
                              "vertices": [t.index for t in l.targets if t.kind == "vertex" and t.body == body.id
                                           and 0 <= t.index < len(body.vertices)],
                              "dir": l.params.get("direction", [0, 0, -1]), "value": l.params.get("magnitude", 1.0),
                              "cs": l.params.get("coordinate_system", "global"),
                              "point": l.params.get("point", [0, 0, 0])})
            items.append((body, cons, loads))
        if not items:
            self.viewport.show_bc_glyphs(None, [], [])
            return
        self.viewport.show_bc_glyphs(*items[0], extra=items[1:])

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
            body = s.analysis_bodies()
            if c and body:
                title = f"Constraint: {c.name} ({c.kind})"
                fields = D.constraint_fields(c.kind, body, us, c, flow2d=self._flow2d())
                self._select([Pick(t.kind, t.body, t.index) for t in c.targets])
        elif kind == "load":
            l = next((x for x in p.loads if x.id == ident), None)
            body = s.analysis_bodies()
            if l and body:
                title = f"Load: {l.name} ({l.kind})"
                fields = D.load_fields(l.kind, body, us, l, flow2d=self._flow2d())
                self._select([Pick(t.kind, t.body, t.index) for t in l.targets])
        elif kind == "interaction":
            it = next((x for x in p.interactions if x.id == ident), None)
            body = s.analysis_bodies()
            if it and body:
                title = f"Interaction: {it.name} ({it.kind})"
                fields = D.interaction_fields(body, us, it)
                note = D.INTERACTION_NOTE
                self._select([Pick(r.kind, r.body, r.index) for r in it.master + it.slave])
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
            info = f"{s.backend.status_text()} at {s.backend.executable()}" if s.backend.executable() else \
                "CalculiX not found - see Help > Installing CalculiX"
            planar = [(D.target_key(b.id, "face", f.index).replace("|face:", "|face:"),
                       f"{b.name}: face {f.index + 1} ({f.area:.4g} {us.length}², n={np.round(f.normal, 3).tolist()})")
                      for b in s.analysis_bodies() for f in b.faces if f.normal is not None]
            fields = D.analysis_fields(p.analysis, info, p.studies, p.active_study, planar)
            from opencae.physics import physics_for
            try:
                ph = physics_for(p.analysis.analysis_type)
                note = "<b>" + ph.name + "</b><br>" + "<br>".join(f"{n}: {e}" for n, e in ph.equations) + \
                       "<br><i>Assumptions:</i> " + " ".join(ph.assumptions)
            except KeyError:
                note = ""
            kind = "solver"
        elif kind == "project":
            title = "Project"
            fields = [Field("name", "Project name", "str", p.name), Field("description", "Description", "str",
                                                                         p.description),
                      Field("units", "Unit system", "text", f"{us.key} (fixed at project creation)"),
                      Field("file", "File", "text", str(s.path or "unsaved"))]
        elif kind in ("result", "result_group", "results", "result_frame"):
            if s.results is not None:
                if kind == "result":
                    self._current_result = ident
                if kind == "result_frame":
                    s.results.set_frame(int(ident))
                    self._fill_result_quantities()
                self.set_view_mode("results")
            return
        elif kind == "studies":
            self.cmd_studies_dashboard()
            return
        elif kind == "study":
            st = p.study_by_id(ident)
            if st is None:
                return
            from opencae.project.model import ANALYSIS_TYPES
            from opencae.ui.model_tree import study_status
            title = f"Study: {st.name}"
            fields = [Field("name", "Name", "str", st.name),
                      Field("type", "Analysis type", "text", ANALYSIS_TYPES.get(st.analysis.analysis_type, "?")),
                      Field("bodies", "Bodies", "text", ", ".join(s.bodies[b].name if b in s.bodies else b + " (missing)"
                                                                 for b in st.analysis_body_ids()) or "(none)"),
                      Field("status", "Status", "text", study_status(p, st)),
                      Field("notes", "Notes (printed in the report)", "multiline", st.notes)]
            note = "Active study." if st.id == p.active_study else "Double-click the study in the tree to activate it."
        elif kind == "study_bodies":
            b, o = self._bodies()
            title = f"Bodies of study '{s.study.name}'"
            fields = [Field("bodies", "Bodies", "multicheck", s.study.analysis_body_ids(), options=D.body_options(b, o))]
            note = ("Bodies that touch are meshed with shared nodes (bonded). Changing the bodies makes the mesh out "
                    "of date.")
            kind = "study_bodies"
        elif kind == "study_body":
            b = s.bodies.get(ident)
            if b:
                self._select([Pick("body", b.id)])
                mat = p.material_for_body(b.id)
                title = f"Body in study: {b.name}"
                fields = [Field("material", "Material (this study)", "choice", mat.id if mat else "",
                                options=[("", "(none)")] + [(m.id, m.name) for m in p.materials]),
                          Field("volume", "Volume", "text", f"{b.volume:.6g} {us.length}^3")]
                kind = "study_body"
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
                      Field("g", "Gamma" if "gamma" in q else "SICN", "text",
                            f"{q['gamma' if 'gamma' in q else 'sicn'][pk.index]:.4f}"),
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
        elif kind == "interaction":
            it = next(x for x in p.interactions if x.id == ident)
            err = self._interaction_check(v)
            if err:
                QMessageBox.warning(self, "Interaction", err)
                return
            self.mutate(f"Edit {it.name}", lambda: self._apply_interaction_values(it, v), regen=False)
        elif kind == "mesh_settings":
            def fn():
                ms = p.mesh_settings
                shape, order = v["element"].split(":")
                ms.body, ms.element_shape, ms.element_order = v["body"], shape, int(order)
                ms.sizing_mode = v["sizing_mode"]
                ms.target_elements = int(v["target_elements"])
                ms.size_max, ms.size_min = v["size_max"], v["size_min"]
                ms.curvature_points, ms.optimize = v["curvature_points"], v["optimize"]
                ms.algorithm_2d, ms.algorithm_3d = int(v["algorithm_2d"]), int(v["algorithm_3d"])
                ms.growth_rate, ms.smoothing_steps = float(v["growth_rate"]), int(v["smoothing_steps"])
                ms.quality_threshold, ms.hex_formulation = float(v["quality_threshold"]), v["hex_formulation"]
                ms.assembly_mesh = v.get("assembly_mesh", ms.assembly_mesh)
                ms.bl_layers, ms.bl_first, ms.bl_growth = int(v["bl_layers"]), float(v["bl_first"]), \
                    float(v["bl_growth"])
            self.mutate("Edit mesh settings", fn, regen=False)
        elif kind == "refinement":
            r = next(x for x in p.mesh_settings.refinements if x.id == ident)
            self.mutate("Edit refinement", lambda: (setattr(r, "name", v["name"]), setattr(r, "size", v["size"])),
                        regen=False)
        elif kind == "solver":
            changed_type = v.get("analysis_type") != p.analysis.analysis_type

            def fn():
                a = p.analysis
                from opencae.materials.library import FLUIDS
                from opencae.project.model import ANALYSIS_TYPES
                new_fluid = v.get("fluid_name") not in (None, "custom", a.fluid_name)
                for k in D.ANALYSIS_KEYS:
                    if k in v:
                        setattr(a, k, v[k])
                if new_fluid:  # picking a fluid fills its properties
                    a.fluid_density, a.fluid_viscosity = FLUIDS[a.fluid_name]
                if changed_type and a.title in ANALYSIS_TYPES.values():
                    a.title = ANALYSIS_TYPES[a.analysis_type]
                if changed_type and a.analysis_type.startswith("cfd") and p.mesh_settings.element_order != 1:
                    p.mesh_settings.element_order = 1  # finite-volume flow solver: linear cells
            self.mutate("Edit analysis settings", fn, regen=False)
            if changed_type:  # the form depends on the analysis type
                self.on_tree_selected("solver", "")
        elif kind == "study":
            st = p.study_by_id(ident)
            if st is not None:
                self.mutate("Edit study", lambda: (setattr(st, "name", v["name"] or st.name),
                                                   setattr(st, "notes", v["notes"])), regen=False)
        elif kind == "study_bodies":
            if not v["bodies"]:
                self.props.error.setText("Select at least one body.")
                return
            self.mutate("Change study bodies", lambda: s.set_study_bodies(v["bodies"]), regen=False)
        elif kind == "study_body":
            if v.get("material"):
                self.mutate("Assign material", lambda: p.assign_material(ident, v["material"]), regen=False)
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
            elif kind in ("material", "constraint", "load", "refinement", "interaction"):
                coll = {"material": p.materials, "constraint": p.constraints, "load": p.loads,
                        "refinement": p.mesh_settings.refinements, "interaction": p.interactions}[kind]
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
            coll = {"feature": p.geometry.features, "constraint": p.constraints, "load": p.loads,
                    "interaction": p.interactions}[kind]
            obj = next(x for x in coll if x.id == ident)
            self.mutate(f"{'Unsuppress' if obj.suppressed else 'Suppress'} {obj.name}",
                        lambda: setattr(obj, "suppressed", not obj.suppressed), regen=(kind == "feature"))
        elif act == "detect_interactions":
            self.cmd_detect_interactions()
        elif act == "set_analysis":
            self.mutate("Set study body", lambda: s.set_study_bodies([ident]), regen=False)
        elif act == "toggle_study_body":
            ids = p.study.analysis_body_ids()
            ids = [b for b in ids if b != ident] if ident in ids else ids + [ident]
            if not ids:
                QMessageBox.information(self, "Study bodies", "A study needs at least one body.")
                return
            self.mutate("Change study bodies", lambda: s.set_study_bodies(ids), regen=False)
        elif act == "activate":
            self.cmd_activate_study(ident)
        elif act == "new_study":
            self.cmd_new_study()
        elif act == "duplicate_study":
            self.mutate("Duplicate study", lambda: s.duplicate_study(ident), regen=False)
        elif act == "rename_study":
            st = p.study_by_id(ident)
            name, ok = QInputDialog.getText(self, "Rename study", "Name:", text=st.name)
            if ok and name.strip():
                self.mutate("Rename study", lambda: s.rename_study(ident, name.strip()), regen=False)
        elif act == "delete_study":
            st = p.study_by_id(ident)
            if len(p.studies) == 1:
                QMessageBox.information(self, "Delete study", "A project needs at least one study.")
                return
            if QMessageBox.question(self, "Delete study", f"Delete study '{st.name}' with its setup? Its mesh and "
                                    "result files stay in the project data folder until you save.") == QMessageBox.Yes:
                self.mutate("Delete study", lambda: s.delete_study(ident), regen=False)
        elif act == "study_bodies":
            if ident and ident != p.active_study:
                self.cmd_activate_study(ident)
            self.cmd_study_bodies()
        elif act == "assign_body_material":
            self.cmd_assign_material(ident)
        elif act == "dashboard":
            self.cmd_studies_dashboard()
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
        elif kind == "interaction":
            it = next(x for x in p.interactions if x.id == ident)
            self.mutate(f"Delete {it.name}", lambda: p.interactions.remove(it), regen=False)

    def cmd_delete_selected(self) -> None:
        it = self.tree.currentItem()
        if it is None:
            return
        kind, ident = it.data(0, Qt.UserRole + 1)
        if kind in ("feature", "material", "constraint", "load", "refinement", "interaction"):
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

    def cmd_select_tangent(self) -> None:
        """Grow the face selection across smooth (tangent) edges: e.g. a whole wing skin."""
        from opencae.geometry.occ_kernel import tangent_faces
        picks = [p for p in self.selection if p.kind == "face"]
        if not picks:
            self.statusBar().showMessage("Select a face first (Select faces mode), then use this command.", 6000)
            return
        out, seen = [], set()
        for pk in picks:
            b = self.session.bodies.get(pk.body)
            if b is None or b.shape is None:
                continue
            for i in tangent_faces(b.shape, pk.index, 10.0):
                if (pk.body, i) not in seen:
                    seen.add((pk.body, i))
                    out.append(Pick("face", pk.body, i, additive=True))
        self._select(out)
        self.statusBar().showMessage(f"{len(out)} tangent-connected face(s) selected (normals within 10° across "
                                     "shared edges).", 6000)

    def cmd_select_body_faces(self) -> None:
        ids = self._picked_bodies()
        if not ids:
            self.statusBar().showMessage("Select a face or body first.", 5000)
            return
        out = [Pick("face", bid, f.index, additive=True) for bid in ids for f in self.session.bodies[bid].faces]
        self._select(out)

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
        if key not in rs.available():
            key = self._default_result(rs)
        q = rs.info(key)
        unit = self._unit_for(q.unit_kind)
        n = pk.index
        self._last_probe_node = n
        val = rs.quantity(key)[n]
        txt = (f"<b>Node {n + 1}</b> at {np.round(rs.mesh.nodes[n], 5).tolist()}<br>"
               f"{q.label}: <b>{val:.6g} {unit}</b> ({q.location})")
        if getattr(rs, "kind", "structural") == "structural":
            u = rs.displacement[n]
            txt += f"<br>U = ({u[0]:.5g}, {u[1]:.5g}, {u[2]:.5g}) {self.us.length}"
        if getattr(rs, "stress", None) is not None:
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
        ids = {b.id for b in self.session.analysis_bodies()}
        return [D.target_key(p.body, p.kind, p.index) for p in self.selection if p.kind in kinds and p.body in ids]

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
        paths, _ = QFileDialog.getOpenFileNames(self, "Import geometry (several files allowed)", self._last_dir(),
                                                CAD_FILTER)
        if paths:
            self.import_files([Path(p) for p in paths])

    def import_files(self, paths: list[Path]) -> None:
        """Import one or more CAD/STL files as one undoable step, after showing what each
        file contains and asking how to treat it (nothing is joined or repaired silently)."""
        from opencae.geometry.occ_kernel import read_stl, stl_components, stl_topology_check
        paths = [p for p in paths if p.suffix.lower() in CAD_SUFFIXES]
        if not paths:
            return
        stl = [p for p in paths if p.suffix.lower() == ".stl"]
        cad = [p for p in paths if p.suffix.lower() != ".stl"]
        fields = []
        diag = []
        for p in stl:
            try:
                pts, tris = read_stl(p)
                c = stl_topology_check(pts, tris)
                nshell = len(stl_components(pts, tris))
                diag.append(f"<b>{p.name}</b>: {c['triangles']} triangles, {nshell} separate shell(s), "
                            f"{'watertight' if c['watertight'] else 'NOT watertight'} "
                            f"({c['boundary_edges']} open edges, {c['nonmanifold_edges']} non-manifold, "
                            f"{c['degenerate_triangles']} degenerate, {c['inconsistent_orientation_edges']} "
                            f"inconsistently oriented), size {c['bbox_size']:.4g} {self.us.length}")
            except Exception as exc:
                diag.append(f"<b>{p.name}</b>: cannot be read ({exc})")
        if cad:
            fields.append(Field("split", "STEP/IGES/BREP: one body per solid", "bool", True,
                                tooltip="Multi-body files (assemblies) become separate, individually selectable "
                                        "bodies. Off: the whole file is one (multi-solid) body."))
        if stl:
            fields += [Field("stl_mode", "STL files", "choice", "solid",
                             options=[("solid", "Reconstruct faceted solid (needs watertight shells)"),
                                      ("display", "Display only (tessellated, cannot be meshed)")]),
                       Field("stl_split", "STL: one body per separate shell", "bool", True,
                             tooltip="Separate triangle shells (e.g. arms, motors exported together) become separate "
                                     "bodies. Shells are never joined and gaps are never filled automatically."),
                       Field("sew_tol", "STL sewing tolerance (0 = automatic 1e-6 × size)", "float", 0.0,
                             self.us.length, minimum=0.0,
                             tooltip="Maximum distance between triangle edges that are sewn together. Increase "
                                     "only if the file has tiny gaps you accept closing.")]
        if fields:
            dlg = ParamDialog(f"Import {len(paths)} file(s)", fields, self,
                              note="<br>".join(diag) or "Choose how the files are imported.")
            if not dlg.exec():
                return
            v = dlg.result_values
        else:
            v = {}
        before = self.session.project.clone()
        errors = []
        for p in paths:
            try:
                if p.suffix.lower() == ".stl":
                    f = self.session.import_geometry(p, v.get("stl_mode") == "solid", bool(v.get("stl_split")),
                                                     v.get("sew_tol") or None)
                else:
                    f = self.session.import_geometry(p, False, bool(v.get("split", True)))
                if f.id in self.session.regen.errors:
                    errors.append(f"{p.name}: {self.session.regen.errors[f.id]}")
                else:
                    n = len(self.session.feature_bodies(f.id))
                    self.console.append(f"Imported {p.name}: {n} body/bodies")
            except Exception as exc:
                errors.append(f"{p.name}: {exc}")
        self.undo.push(f"Import {len(paths)} file(s)", before)
        self.dirty = True
        self.settings.setValue("last_dir", str(paths[0].parent))
        if errors:
            QMessageBox.warning(self, "Import problem", "\n\n".join(errors) + "\n\nUse Undo to remove the import.")
        self.refresh_all(fit=True)

    # drag & drop of CAD files onto the window
    def dragEnterEvent(self, ev) -> None:
        if ev.mimeData().hasUrls() and any(Path(u.toLocalFile()).suffix.lower() in CAD_SUFFIXES + (".ocae",)
                                           for u in ev.mimeData().urls()):
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:
        paths = [Path(u.toLocalFile()) for u in ev.mimeData().urls()]
        if any(p.suffix.lower() == ".ocae" for p in paths):
            self.cmd_open(str(next(p for p in paths if p.suffix.lower() == ".ocae")))
            return
        self.import_files(paths)

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
        dlg = ParamDialog("Report notes", [Field("notes", "Engineering notes / conclusions", "multiline",
                                                 s.study.notes)], self,
                          note="Saved with the study and printed in the report (blank line = new paragraph).")
        if dlg.exec() and dlg.result_values["notes"] != s.study.notes:
            notes = dlg.result_values["notes"]
            self.mutate("Edit study notes", lambda: setattr(s.study, "notes", notes), regen=False)
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
                for key in ("U_mag", "S_vm", "T", "HF_mag"):
                    if key in self.session.results.available():
                        self._current_result = key
                        self.view_mode = "results"
                        self._fill_result_quantities()
                        self.refresh_results_view()
                        self.viewport.plotter.camera_position = cam
                        imgs.append(self.viewport.screenshot(
                            folder / f"{self.session.results.info(key).label.lower().replace(' ', '_')}.png"))
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
        body = self.session.analysis_bodies()
        if not body:
            return
        sel = self._selected_targets(("face", "edge"))
        opts = D.target_options(body, ("face", "edge"))
        size = (self.session.project.mesh_settings.size_max or max(b.size for b in body) / 20) / 4
        dlg = ParamDialog("Local mesh refinement",
                          [Field("name", "Name", "str", "Local refinement"),
                           Field("size", "Target element size", "float", size, self.us.length, minimum=1e-12),
                           Field("targets", "Faces / edges", "multicheck", sel, options=opts)], self,
                          validator=lambda v: "Select at least one face or edge." if not v["targets"] else "")
        if dlg.exec():
            v = dlg.result_values
            refs = self._refs_from(body, v["targets"])
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
        self.session.running = "mesh"
        self.solver_panel.start("Meshing")
        self.workflow.update_status(self.session.workflow_status())
        self.bottom.setCurrentWidget(self.solver_panel)

    # ================================================================== materials
    def cmd_assign_material(self, body_id: str = "") -> None:
        s = self.session
        p = s.project
        bodies = s.analysis_bodies()
        if not bodies:
            QMessageBox.information(self, "Assign material", "The active study has no body yet.")
            return
        body = s.bodies.get(body_id) or bodies[0]
        cur = p.material_for_body(body.id)
        dlg = ParamDialog(f"Assign material (study '{s.study.name}')", [
            Field("body", "Body", "choice", body.id, options=[(b.id, b.name) for b in bodies]),
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
    def _refs_from(self, bodies, targets: list[str]):
        by_id = {b.id: b for b in (bodies if isinstance(bodies, (list, tuple)) else [bodies])}
        out = []
        for t in targets:
            bid, k, i = D.parse_target_key(t)
            out.append(make_ref(by_id[bid], k, i))
        return out

    def _apply_bc_values(self, c: Constraint, v: dict) -> None:
        body = self.session.analysis_bodies()
        c.name = v["name"]
        c.targets = self._refs_from(body, v["targets"])
        if c.kind == "displacement":
            c.params = {k: v[k] for k in ("ux", "uy", "uz")}
        if c.kind == "temperature":
            c.params = {"value": float(v["value"])}

    def _apply_load_values(self, l: Load, v: dict) -> None:
        body = self.session.analysis_bodies()
        l.name = v["name"]
        if "targets" in v:
            l.targets = self._refs_from(body, v["targets"])
        for k in ("magnitude", "direction", "coordinate_system", "film", "ambient", "emissivity", "value", "point"):
            if k in v:
                l.params[k] = v[k]
        if l.kind == "velocity_inlet":
            l.params.pop("coordinate_system", None)
            if v.get("dir_mode", "normal") == "normal":
                l.params["direction"] = "normal"

    def cmd_constraint(self, kind: str) -> None:
        body = self.session.analysis_bodies()
        if not body:
            return
        self._warn_physics_mismatch(kind)
        pre = self._selected_targets(("face", "edge", "vertex") if kind != "symmetry" else ("face",))
        f2 = self._flow2d() if kind in D.FLOW_ITEMS else None
        if kind in D.FLOW_ITEMS:
            pre = [x for x in pre if ("|edge:" in x) == (f2 is not None)]
        fields = D.constraint_fields(kind, body, self.us, preselect=pre, flow2d=f2)

        def validate(v):
            if not v["targets"]:
                return "Select at least one target entity."
            if kind == "displacement" and all(v[k] is None for k in ("ux", "uy", "uz")):
                return "Specify at least one displacement component."
            return ""
        note = "Targets are pre-checked from the viewport selection."
        if kind in ("wall", "slip_wall"):
            note = ("No-slip wall: the fluid is at rest on these faces (faces without any flow condition are "
                    "no-slip walls by default). " if kind == "wall" else
                    "Slip wall / symmetry plane: no flow through the faces and no shear. ") + note
        if kind == "temperature":
            note = "Prescribed temperature (°C) on faces, edges or vertices. " + note
        dlg = ParamDialog(D.CONSTRAINT_NAMES[kind], fields, self, validator=validate, note=note)
        if dlg.exec():
            c = Constraint(kind, [])
            self._apply_bc_values(c, dlg.result_values)
            self.mutate(f"Add {c.name}", lambda: self.session.project.constraints.append(c), regen=False)

    def cmd_load(self, kind: str) -> None:
        body = self.session.analysis_bodies()
        if not body:
            return
        self._warn_physics_mismatch(kind)
        pre = self._selected_targets(D.LOAD_TARGETS[kind])

        def validate(v):
            if (D.LOAD_TARGETS[kind] or f2 is not None) and not v.get("targets"):
                return "Select at least one target entity."
            if "direction" in v and v.get("dir_mode", "vector") == "vector" and \
                    np.linalg.norm(v["direction"]) < 1e-12 and v.get("coordinate_system") != "face_normal":
                return "Direction vector must be non-zero."
            return ""
        f2 = self._flow2d() if kind in D.FLOW_ITEMS else None
        if f2 is not None:
            pre = [x for x in self._selected_targets(("edge",))]
        dlg = ParamDialog(f"Add {D.LOAD_NAMES[kind].lower()}", D.load_fields(kind, body, self.us, preselect=pre,
                                                                              flow2d=f2),
                          self, validator=validate, note=D.LOAD_NOTES[kind])
        if dlg.exec():
            l = Load(kind, [], {})
            if kind in ("pressure", "gravity"):
                l.params["coordinate_system"] = "global"
            self._apply_load_values(l, dlg.result_values)
            self.mutate(f"Add {l.name}", lambda: self.session.project.loads.append(l), regen=False)

    def _flow2d(self):
        """(body, face index) when the active study is a 2-D flow study with a valid face, else None."""
        a = self.session.project.analysis
        if not (a.analysis_type.startswith("cfd") and a.cfd_dimension == "2d"):
            return None
        try:
            bid, face = self.session.flow_face()
        except ValueError:
            return None
        return self.session.bodies[bid], face

    def _warn_physics_mismatch(self, kind: str) -> None:
        """Tell the user when an item will not act in the current analysis type."""
        t = self.session.project.analysis.analysis_type
        thermal_item = kind in D.THERMAL_LOADS or kind == "temperature"
        flow_item = kind in D.FLOW_ITEMS
        if t.startswith("cfd") != flow_item:
            msg = (f"'{kind}' is a flow condition; it only acts in a flow (CFD) study." if flow_item else
                   f"'{kind}' does not act in a flow study (use inlet / outlet / wall conditions).")
            self.statusBar().showMessage(msg, 8000)
            self.console.append(msg, logging.WARNING)
            return
        if t.startswith("thermal_") and t != "thermal_stress" and not thermal_item:
            msg = f"'{kind}' is a structural item; it is ignored by the current {t.replace('_', ' ')} analysis."
        elif not t.startswith("thermal_") or t == "thermal_stress":
            msg = (f"'{kind}' is a thermal item; it is ignored by the current {t.replace('_', ' ')} analysis "
                   "(change the analysis type, or create a thermal study).") if thermal_item else ""
            if kind == "uniform_temperature" and t != "thermal_stress":
                msg = "A uniform temperature only acts in a thermal-stress analysis."
        else:
            msg = ""
        if msg:
            self.statusBar().showMessage(msg, 8000)
            self.console.append(msg, logging.WARNING)

    # ================================================================== assembly
    def _picked_bodies(self) -> list[str]:
        ids = []
        for pk in self.selection:
            if pk.body in self.session.bodies and pk.body not in ids:
                ids.append(pk.body)
        it = self.tree.currentItem()
        if not ids and it is not None:
            from opencae.ui.model_tree import ROLE
            k, ident = it.data(0, ROLE)
            if k in ("body", "study_body") and ident in self.session.bodies:
                ids = [ident]
        return ids

    def cmd_isolate(self) -> None:
        ids = self._picked_bodies() or self.session.study.analysis_body_ids()
        if not ids:
            return
        g = self.session.project.geometry
        g.hidden_bodies = [b for b in self.session.regen.order if b not in ids]
        self.dirty = True
        self.refresh_all()
        self.statusBar().showMessage(f"Showing {len(ids)} body/bodies; View > Show all bodies to restore.", 5000)

    def cmd_show_all(self) -> None:
        self.session.project.geometry.hidden_bodies = []
        self.dirty = True
        self.refresh_all()

    def cmd_assembly_check(self) -> None:
        from opencae.geometry.occ_kernel import pair_relations
        s = self.session
        bodies = [s.bodies[b] for b in s.regen.order if s.bodies[b].kind == "solid"]
        if len(bodies) < 2:
            QMessageBox.information(self, "Contacts / interferences", "Needs at least two solid bodies.")
            return
        size = max(float(np.linalg.norm(np.asarray(b.bbox[1]) - b.bbox[0])) for b in bodies)
        rel = pair_relations(bodies, 1e-6 * size)
        study = set(s.study.analysis_body_ids())
        css = {"overlapping": theme.ERR, "touching": theme.OK, "separate": theme.FG_DIM}
        rows = []
        for r in rel:
            if r["relation"] == "separate" and r["distance"] is None:
                continue
            both = r["a"] in study and r["b"] in study
            meaning = {"overlapping": "INTERFERENCE: the solids interpenetrate. Fix the geometry (move or "
                                      "subtract) — a bonded mesh would double the material.",
                       "touching": "Contact: bonded (shared nodes) when both are in the same study." +
                                   (" Both are in the active study." if both else ""),
                       "separate": f"Gap {r['distance']:.4g} {self.us.length}: not connected; each body needs its "
                                   "own supports. Gaps are not closed automatically."}[r["relation"]]
            ov = f"{r['overlap_volume']:.4g} {self.us.length}³" if r["relation"] == "overlapping" else ""
            rows.append(f"<tr><td>{r['a_name']}</td><td>{r['b_name']}</td><td style='color:{css[r['relation']]}'>"
                        f"{r['relation']}</td><td>{ov}</td><td>{meaning}</td></tr>")
        n_far = sum(1 for r in rel if r["distance"] is None)
        self.summary.setHtml("<h3>Contacts and interferences</h3><table border=1 cellpadding=4><tr><th>Body</th>"
                             "<th>Body</th><th>Relation</th><th>Overlap</th><th>Meaning</th></tr>" + "".join(rows) +
                             f"</table><p>{n_far} further pair(s) are far apart (bounding boxes do not meet). "
                             f"Touch tolerance {1e-6 * size:.3g} {self.us.length}.</p>")
        self.bottom.setCurrentWidget(self.summary)

    # ------------------------------------------------------------------ interactions
    def _apply_interaction_values(self, it, v: dict) -> None:
        body = self.session.analysis_bodies()
        it.name, it.kind = v["name"], v["kind"]
        it.master = self._refs_from(body, v["master"])
        it.slave = self._refs_from(body, v["slave"])
        it.params["friction"] = float(v.get("friction", 0.0))
        it.params["stiffness"] = float(v.get("stiffness", 0.0))

    def _interaction_check(self, v: dict) -> str:
        if not v["master"] or not v["slave"]:
            return "Select master and slave faces."
        if set(v["master"]) & set(v["slave"]):
            return "A face cannot be on both sides."
        mb = {D.parse_target_key(k)[0] for k in v["master"]}
        sb = {D.parse_target_key(k)[0] for k in v["slave"]}
        if mb & sb:
            return "Master and slave faces must belong to different bodies."
        return ""

    def _ensure_separate_mesh(self) -> None:
        ms = self.session.project.mesh_settings
        if ms.assembly_mesh != "separate":
            ms.assembly_mesh = "separate"
            self.statusBar().showMessage("Mesh controls > Assembly mesh set to 'separate' (needed for interactions); "
                                         "remesh before solving.", 8000)

    def cmd_interaction(self, kind: str) -> None:
        from opencae.project.model import Interaction
        s = self.session
        body = s.analysis_bodies()
        if len(body) < 2:
            QMessageBox.information(self, "Interaction", "Put at least two bodies in the study (Bodies in study…).")
            return
        sel = self._selected_targets(("face",))
        first = D.parse_target_key(sel[0])[0] if sel else None
        master = [k for k in sel if D.parse_target_key(k)[0] == first]
        slave = [k for k in sel if D.parse_target_key(k)[0] != first]
        dlg = ParamDialog(f"Add {D.INTERACTION_NAMES[kind].lower()} interaction",
                          D.interaction_fields(body, self.us, master=master, slave=slave, kind=kind), self,
                          validator=self._interaction_check, note=D.INTERACTION_NOTE)
        if dlg.exec():
            it = Interaction(kind, [], [], {})
            self._apply_interaction_values(it, dlg.result_values)

            def fn():
                s.project.interactions.append(it)
                self._ensure_separate_mesh()
            self.mutate(f"Add {it.name}", fn, regen=False)

    def cmd_detect_interactions(self) -> None:
        """List touching face pairs of the study bodies; the user picks which become interactions."""
        from opencae.geometry.occ_kernel import touching_faces
        from opencae.project.model import Interaction
        from opencae.geometry.references import make_ref
        s = self.session
        bodies = [b for b in s.analysis_bodies() if b.kind == "solid"]
        if len(bodies) < 2:
            QMessageBox.information(self, "Detect touching faces", "Put at least two solid bodies in the study.")
            return
        size = max(float(np.linalg.norm(np.asarray(b.bbox[1]) - b.bbox[0])) for b in bodies)
        tol = 1e-6 * size
        existing = {(r.body, r.index) for it in s.project.interactions for r in it.master + it.slave}
        found, opts = [], []
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for i in range(len(bodies)):
                for j in range(i + 1, len(bodies)):
                    a, b = bodies[i], bodies[j]
                    for fa, fb, area in touching_faces(a, b, tol):
                        key = f"{len(found)}"
                        found.append((a, fa, b, fb))
                        opts.append((key, f"{a.name} face {fa + 1}  ↔  {b.name} face {fb + 1}   (shared area "
                                          f"{area:.4g} {self.us.length}²)" +
                                     ("   [already used]" if (a.id, fa) in existing or (b.id, fb) in existing
                                      else "")))
        finally:
            QApplication.restoreOverrideCursor()
        if not found:
            QMessageBox.information(self, "Detect touching faces",
                                    f"No touching faces between the study bodies (tolerance {tol:.3g} "
                                    f"{self.us.length}). Gaps are not closed automatically.")
            return
        pre = [k for k, lab in opts if "already used" not in lab]
        dlg = ParamDialog("Detect touching faces", [
            Field("pairs", "Face pairs to connect", "multicheck", pre, options=opts),
            Field("kind", "Interaction type", "choice", "bonded", options=list(D.INTERACTION_NAMES.items())),
            Field("friction", "Friction coefficient (contact)", "float", 0.0, minimum=0.0, maximum=5.0)],
            self, note="Faces of different bodies that lie on each other. Only the pairs you tick are connected; "
                       "each pair becomes one interaction (first body = master).")
        if not dlg.exec():
            return
        v = dlg.result_values
        new = []
        for k in v["pairs"]:
            a, fa, b, fb = found[int(k)]
            new.append(Interaction(v["kind"], [make_ref(a, "face", fa)], [make_ref(b, "face", fb)],
                                   {"friction": float(v["friction"])},
                                   name=f"{D.INTERACTION_NAMES[v['kind']]} {a.name}/{b.name}"))
        if new:
            def fn():
                s.project.interactions.extend(new)
                self._ensure_separate_mesh()
            self.mutate(f"Add {len(new)} interaction(s)", fn, regen=False)

    def cmd_flow_domain(self) -> None:
        """External flow around the selected bodies: enclosure + configured flow study."""
        s = self.session
        b, o = self._bodies()
        solids = D.body_options(b, o)
        if not solids:
            QMessageBox.information(self, "Flow domain", "Create or import the solid parts first.")
            return
        pre = self._picked_bodies() or [x for x in s.study.analysis_body_ids() if x in s.bodies]
        from opencae.materials.library import FLUIDS
        dlg = ParamDialog("External flow domain", [
            Field("bodies", "Solid bodies inside the flow", "multicheck", pre, options=solids),
            Field("axis", "Flow direction", "choice", "+x",
                  options=[(a, a.upper()) for a in ("+x", "-x", "+y", "-y", "+z", "-z")]),
            Field("speed", "Free-stream speed", "float", 10.0, "m/s", minimum=1e-9),
            Field("fluid", "Fluid", "choice", "Air (20 °C)", options=[(k, k) for k in FLUIDS]),
            Field("upstream", "Upstream distance (× body size)", "float", 3.0, minimum=0.5),
            Field("downstream", "Downstream distance (× body size)", "float", 6.0, minimum=0.5),
            Field("lateral", "Side clearance (× body size)", "float", 2.0, minimum=0.5),
            Field("symmetry", "Symmetry plane", "choice", "",
                  options=[("", "None (bodies fully surrounded by air)")] +
                          [(a, f"On the bodies' {a.upper()} side (e.g. half-wing root)")
                           for a in ("+x", "-x", "+y", "-y", "+z", "-z")],
                  tooltip="Puts that side of the box exactly at the bodies' extent and makes it a symmetry (slip) "
                          "plane: model half of a symmetric aircraft, with the wing root on the plane."),
            Field("name", "Study name", "str", "External flow")], self,
            validator=lambda v: "Select at least one body." if not v["bodies"] else
            "The symmetry plane cannot be normal to the flow direction." if v.get("symmetry") and
            v["symmetry"][-1] == v["axis"][-1] else "",
            note="Creates a box-shaped fluid body around the parts (the parts themselves are kept for structural "
                 "studies) and a flow study with an inlet on the upstream face, a pressure outlet downstream, slip "
                 "walls on the sides and no-slip walls on the parts. Larger clearances reduce blockage effects "
                 "but cost more cells.")
        if not dlg.exec():
            return
        v = dlg.result_values

        def fn():
            st = s.create_flow_domain(v["bodies"], v["axis"], v["speed"], v["upstream"], v["downstream"],
                                      v["lateral"], v["name"], symmetry=v.get("symmetry", ""))
            st.analysis.fluid_name = v["fluid"]
            st.analysis.fluid_density, st.analysis.fluid_viscosity = FLUIDS[v["fluid"]]
        try:
            st_before = len(s.project.studies)
            self.mutate("Create flow domain", fn, regen=False)
        except ValueError as exc:
            if len(s.project.studies) > st_before:  # roll back a half-created setup
                self.cmd_undo()
            QMessageBox.warning(self, "Flow domain", str(exc))
            return
        self.console.append("Flow domain created. Check the mesh size near the parts (Mesh > Local refinement) "
                            "before solving.")

    # ================================================================== studies
    def cmd_activate_study(self, study_id: str) -> None:
        s = self.session
        if study_id == s.project.active_study:
            return
        self.viewport.stop_animation()
        s.set_active_study(study_id)
        self.dirty = True
        self.selection = []
        self.console.append(f"Active study: {s.study.name}")
        self.refresh_all()

    def cmd_new_study(self) -> None:
        from opencae.physics import available_analysis_types
        from opencae.project.model import ANALYSIS_TYPES
        s = self.session
        p = s.project
        b, o = self._bodies()
        solids = [(bid, lab) for bid, lab in D.body_options(b, o)]
        if not solids:
            QMessageBox.information(self, "New study", "Create or import a solid body first.")
            return
        cur = p.study.analysis_body_ids()
        fields = [Field("name", "Study name", "str", f"Study {len(p.studies) + 1}"),
                  Field("analysis_type", "Analysis type", "choice", "linear_static",
                        options=[(t, ANALYSIS_TYPES.get(t, t)) for t in available_analysis_types()]),
                  Field("bodies", "Bodies (bonded where they touch)", "multicheck", cur, options=solids),
                  Field("copy", "Copy supports, loads, mesh controls and materials from the active study", "bool",
                        False),
                  Field("material", "Material for the bodies (solids)", "choice", "",
                        options=[("", "(keep assignments / first material)")] + [(m.id, m.name) for m in p.materials]),
                  Field("mesh", "Mesh density", "choice", "medium",
                        options=[("coarse", "Coarse (quick check: size = body / 10)"),
                                 ("medium", "Medium (size = body / 20)"), ("fine", "Fine (size = body / 40)"),
                                 ("keep", "Keep / automatic")])]

        def validate(v):
            return "Select at least one body." if not v["bodies"] else ""
        dlg = ParamDialog("New study", fields, self, validator=validate,
                          note="A study is one analysis with its own bodies, materials, mesh, conditions and "
                               "results. Use several studies to compare designs, load cases or physics (e.g. a "
                               "thermal study feeding a thermal-stress study).")
        if not dlg.exec():
            return
        v = dlg.result_values

        def fn():
            _fn_inner()
            st = s.study
            if v["analysis_type"].startswith("cfd"):
                st.mesh_settings.element_order = 1  # finite-volume flow solver uses linear cells
            if v.get("material"):
                for bid in v["bodies"]:
                    p.assign_material(bid, v["material"])
            if v.get("mesh") in ("coarse", "medium", "fine"):
                size = max(s.bodies[b].size for b in v["bodies"] if b in s.bodies)
                st.mesh_settings.size_max = round(size / {"coarse": 10, "medium": 20, "fine": 40}[v["mesh"]], 4)

        def _fn_inner():
            if v["copy"]:
                st = s.duplicate_study(None, v["name"])
                st.analysis.analysis_type = v["analysis_type"]
                st.analysis.title = ANALYSIS_TYPES[v["analysis_type"]]
                s.set_study_bodies(v["bodies"])
            else:
                s.new_study(v["name"], v["analysis_type"], v["bodies"])
            for bid in v["bodies"]:  # default material so the study is immediately usable
                if p.material_for_body(bid) is None and p.materials:
                    p.assign_material(bid, p.materials[0].id)
        self.mutate("New study", fn, regen=False)
        nxt = {"linear_static": "add supports and loads (Boundary Conditions tab), then Mesh > Generate and Solve",
               "modal": "add supports (none = free-free), set the number of modes, then mesh and solve",
               "buckling": "add supports and the loads whose critical multiple you want, then mesh and solve",
               "thermal_steady": "add temperatures / heat loads (Boundary Conditions tab, thermal group)",
               "thermal_transient": "add thermal conditions and set the end time and time step in Analysis settings",
               "thermal_stress": "choose the thermal study that provides the temperatures (Analysis settings)",
               "cfd_incompressible": "set the fluid, add an inlet and an outlet (Flow group), mesh with linear "
                                     "cells and solve"}.get(v["analysis_type"], "")
        self.console.append(f"Study '{v['name']}' created. Next: {nxt}.")
        self.statusBar().showMessage("Next: " + nxt, 12000)
        if v["analysis_type"] in ("thermal_stress", "cfd_incompressible"):
            self.on_tree_selected("solver", "")

    def cmd_study_bodies(self) -> None:
        s = self.session
        b, o = self._bodies()
        dlg = ParamDialog(f"Bodies of study '{s.study.name}'", [
            Field("bodies", "Bodies", "multicheck", s.study.analysis_body_ids(), options=D.body_options(b, o))],
            self, validator=lambda v: "Select at least one body." if not v["bodies"] else "",
            note="Bodies that touch are meshed with shared nodes (bonded). Separate bodies must each be supported. "
                 "Changing the bodies makes the mesh out of date.")
        if dlg.exec():
            ids = dlg.result_values["bodies"]
            self.mutate("Change study bodies", lambda: s.set_study_bodies(ids), regen=False)

    def cmd_studies_dashboard(self) -> None:
        from opencae.project.model import ANALYSIS_TYPES
        from opencae.ui.model_tree import study_status
        s = self.session
        p = s.project
        rows = []
        for st in p.studies:
            key = ""
            sm = st.results.summary or {}
            if st.results.completed:
                if "frequencies_hz" in sm:
                    key = "f1 = " + ", ".join(f"{f:.4g}" for f in sm["frequencies_hz"][:3]) + " Hz"
                elif "max_temperature" in sm:
                    key = f"T max {sm['max_temperature']:.4g} °C, min {sm['min_temperature']:.4g} °C"
                elif "max_displacement" in sm:
                    key = f"|u| max {sm['max_displacement']:.4g} {self.us.length}"
                    if "max_von_mises_nodal" in sm:
                        key += f", σvM max {sm['max_von_mises_nodal']:.4g} {self.us.stress}"
            mesh = st.mesh.stats.get("elements", "") if st.mesh.file else ""
            bold = ' style="font-weight:bold"' if st.id == p.active_study else ""
            rows.append(f"<tr{bold}><td>{st.name}</td>"
                        f"<td>{ANALYSIS_TYPES.get(st.analysis.analysis_type, '?')}</td>"
                        f"<td>{', '.join(s.bodies[b].name if b in s.bodies else '?' for b in st.analysis_body_ids())}</td>"
                        f"<td>{mesh}</td><td>{study_status(p, st)}</td><td>{key}</td>"
                        f"<td>{st.results.finished_at}</td></tr>")
        self.summary.setHtml("<h3>Studies</h3><table border=1 cellpadding=4><tr><th>Study</th><th>Type</th>"
                             "<th>Bodies</th><th>Elements</th><th>Status</th><th>Key result</th><th>Solved</th></tr>"
                             + "".join(rows) + "</table><p>Bold: active study. Results marked out of date no longer "
                             "match the study's current geometry, mesh or setup.</p>")
        self.bottom.setCurrentWidget(self.summary)

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
        be = self.session.solver_backend()
        if be is None or not be.executable():
            if be is self.session.backend:
                D.TextDialog("CalculiX not found", "<pre>" + INSTALL_HELP + "</pre>", self).exec()
            else:
                from opencae.cfd.su2 import INSTALL_HELP as SU2_HELP
                D.TextDialog("SU2 not found", "<pre>" + SU2_HELP + "</pre>", self).exec()
            return
        job_dir, jobname, issues = self.session.prepare_solve()
        self.issues.show_issues(issues)
        if not jobname:
            self.bottom.setCurrentWidget(self.issues)
            QMessageBox.warning(self, "Model not ready", "The model has errors; see Errors & warnings:\n\n" +
                                "\n".join(str(i) for i in issues if i.severity == ERROR)[:2000])
            return
        self._job = {"dir": str(job_dir), "jobname": jobname, "backend": be}
        cmd = be.command(jobname)
        env = be.environment(self.session.project.analysis.threads)
        self._job.update(cmd=cmd, env=env)
        self.console.append(f"Starting {' '.join(cmd)} in {job_dir} (isolated solver environment)")
        self._start_process("solve", cmd[0], cmd[1:], str(job_dir), env, replace_env=True)
        self.session.running = "solve"
        self.session.last_solve_error = ""
        self.solver_panel.start(f"Solving with {be.name} ({self.session.mesh.n_nodes} nodes)")
        self.workflow.update_status(self.session.workflow_status())
        self.bottom.setCurrentWidget(self.solver_panel)

    def cmd_cancel(self) -> None:
        if self.proc is not None:
            self.console.append(f"Cancelling {self.proc_kind}…", logging.WARNING)
            self._cancelled = True
            self.proc.kill()

    # ------------------------------------------------------------------ process handling
    def _start_process(self, kind: str, program: str, args: list[str], cwd: str,
                       env: Optional[dict] = None, replace_env: bool = False) -> None:
        """Start a background process. With replace_env the child gets exactly ``env``
        (used for the solver, whose PATH must not be the GUI's)."""
        self.proc = QProcess(self)
        self.proc_kind = kind
        self._proc_buf = ""
        self._proc_log: list[str] = []
        self._cancelled = False
        self.proc.setWorkingDirectory(cwd)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        qenv = QProcessEnvironment() if replace_env else QProcessEnvironment.systemEnvironment()
        for k, v in (env or {}).items():
            qenv.insert(k, v)
        self._proc_t0 = __import__("time").time()
        if kind in ("mesh", "convergence"):
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
            self.session.running = ""
            if self.proc_kind == "solve":
                self.session.last_solve_error = f"could not start solver: {self.proc.errorString()}"
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
        self.session.running = ""
        cancelled = self._cancelled
        text = "\n".join(self._proc_log)
        try:
            if kind == "mesh":
                self._mesh_finished(code, cancelled, text)
            elif kind == "solve":
                self._solve_finished(code, cancelled, text)
            elif kind == "convergence":
                self._convergence_finished(code, cancelled, text)
            elif kind == "benchmarks":
                ok = code == 0 and not cancelled
                self.solver_panel.finish(ok, "Benchmarks passed" if ok else "Benchmarks failed or cancelled")
                rep = Path(self._job["dir"]) / "validation_report.html"
                if rep.exists():
                    self.console.append(f"Validation report: {rep}")
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(rep)))
        finally:
            self.update_actions()

    def cmd_mesh_convergence(self) -> None:
        s = self.session
        if self.proc is not None:
            return
        bodies = s.analysis_bodies()
        if not bodies:
            return
        issues, _ = s.validate()
        blocking = [i for i in issues if i.severity == ERROR and i.source != "Mesh"]
        if blocking:
            QMessageBox.warning(self, "Mesh convergence", "Fix the model first:\n\n" +
                                "\n".join(str(i) for i in blocking[:10]))
            return
        h0 = s.project.mesh_settings.size_max or max(b.size for b in bodies) / 20
        dlg = ParamDialog("Mesh convergence study", [
            Field("sizes", "Element sizes (coarse to fine, comma-separated)", "str",
                  ", ".join(f"{h0 * f:.4g}" for f in (2.0, 1.0, 0.6, 0.4)))], self,
            note="A copy of the active study is meshed and solved at each size in the background; your study, mesh "
                 "and results are not changed. Three or more levels allow an observed order, a Richardson "
                 "extrapolation and a grid convergence index (GCI). Fine levels can take long.")
        if not dlg.exec():
            return
        try:
            sizes = [float(x) for x in dlg.result_values["sizes"].replace(";", ",").split(",") if x.strip()]
        except ValueError:
            QMessageBox.warning(self, "Mesh convergence", "Enter numbers separated by commas.")
            return
        from opencae.app.main import worker_command
        from opencae.project import io as pio
        work = s.data_dir / "convergence"
        work.mkdir(parents=True, exist_ok=True)
        out = work / f"convergence_{s.study.id}.json"
        out.unlink(missing_ok=True)
        jf = work / "convergence.job.json"
        jf.write_text(json.dumps({"project": pio.project_to_dict(s.project), "study": s.project.active_study,
                                  "data_dir": str(s.data_dir), "sizes": sizes, "out": str(out)}))
        self._job = {"out": str(out)}
        prog, args = worker_command("opencae.postprocessing.convergence", [str(jf)])
        self._start_process("convergence", prog, args, str(work))
        self.solver_panel.start("Mesh convergence")
        self.bottom.setCurrentWidget(self.solver_panel)

    def _convergence_finished(self, code: int, cancelled: bool, text: str) -> None:
        from opencae.postprocessing.convergence import summary_html
        out = Path(self._job.get("out", ""))
        if cancelled or code != 0 or not out.exists():
            err = next((l for l in text.splitlines() if l.startswith("ERROR")), f"exit code {code}")
            self.solver_panel.finish(False, "Convergence study cancelled" if cancelled else "Convergence study failed")
            if not cancelled:
                QMessageBox.warning(self, "Mesh convergence failed", err)
            return
        res = json.loads(out.read_text())
        self.last_convergence = res
        self.solver_panel.finish(True, "Convergence study finished")
        self.summary.setHtml(summary_html(res))
        self.bottom.setCurrentWidget(self.summary)
        self.cmd_plot_explorer(kind="convergence")

    def cmd_plot_explorer(self, kind: str = "") -> None:
        from opencae.ui.plot_explorer import PlotExplorer
        probe = getattr(self, "_last_probe_node", None)
        dlg = PlotExplorer(self.session, self, probe, getattr(self, "last_convergence", None))
        if kind:
            i = dlg.kind.findData(kind)
            if i >= 0:
                dlg.kind.setCurrentIndex(i)
        self._plot_explorer = dlg
        dlg.show()

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
        for w in mesh.warnings(self.session.project.mesh_settings.quality_threshold):
            self.console.append("Mesh warning: " + w, logging.WARNING)
        self.view_mode = "mesh"
        self.refresh_all()
        self.bottom.setCurrentWidget(self.mesh_diag)

    def _solve_finished(self, code: int, cancelled: bool, text: str) -> None:
        job_dir = Path(self._job["dir"])
        jobname = self._job["jobname"]
        be = self._job.get("backend") or self.session.backend
        ok, errors, warnings = be.analyse(text, code, job_dir, jobname)
        if cancelled:
            ok, errors = False, ["Cancelled by user."]
        import time as _t
        log_path = write_solver_log(job_dir, jobname, self._job.get("cmd", []), self._job.get("env", {}), code, text,
                                    errors, _t.time() - getattr(self, "_proc_t0", _t.time()))
        self.console.append(f"Solver log: {log_path}")
        if ok:
            be.verified = True
        for w in warnings[:20]:
            self.console.append("Solver: " + w, logging.WARNING)
        if not ok:
            self.session.last_solve_error = "cancelled" if cancelled else (errors[0] if errors else "solver failed")
            self.refresh_all()
            self.solver_panel.finish(False, "Solver failed" if not cancelled else "Solver cancelled")
            self.issues.show_issues([Issue("error", e, be.name) for e in errors[:50]])
            for e in errors[:10]:
                self.console.append("Solver: " + e, logging.ERROR)
            if not cancelled:
                self.bottom.setCurrentWidget(self.issues)
                QMessageBox.warning(self, "Solver failed",
                                    f"What failed: {be.name} on study '{self.session.study.name}'.\n\n" +
                                    "\n".join(errors[:10]) +
                                    f"\n\nInput: {job_dir / jobname}.*\nFull log: {log_path}")
            return
        try:
            rs = self.session.load_results(job_dir, jobname)
        except Exception as exc:
            self.solver_panel.finish(False, "Reading results failed")
            QMessageBox.critical(self, "Reading results failed", str(exc))
            return
        self.dirty = True
        from opencae.postprocessing.summary import headline
        self.solver_panel.finish(True, "Solution complete")
        self.console.append("Solved. " + headline(self.session))
        self._current_result = self._default_result(rs)
        self.refresh_all()
        self.set_view_mode("results")
        self.bottom.setCurrentWidget(self.summary)

    # ================================================================== results
    def _unit_for(self, kind: str) -> str:
        from opencae.postprocessing.summary import unit_for
        return unit_for(kind, self.us)

    @staticmethod
    def _default_result(rs) -> str:
        av = rs.available()
        for k in ("S_vm", "T", "V_mag", "U_mag"):
            if k in av:
                return k
        return av[0]

    def _fill_result_quantities(self) -> None:
        rs = self.session.results
        if rs is None:
            return
        if self._current_result not in rs.available():
            self._current_result = self._default_result(rs)
        items = [(k, rs.info(k).label) for k in rs.available()]
        self.results_panel.set_quantities(items, self._current_result)
        self.results_panel.set_frames(rs.frames(), rs.frame)
        kind = getattr(rs, "kind", "structural")
        self.results_panel.set_flow_controls(kind == "cfd")
        self.results_panel.set_deformation_available(kind not in ("thermal", "cfd"),
                                                     "Animate mode shape" if kind in ("modal", "buckling")
                                                     else "Animate deformation")

    def refresh_results_view(self) -> None:
        rs = self.session.results
        if rs is None or self.view_mode != "results":
            return
        key = self.results_panel.current_key() or self._current_result
        if key not in rs.available():
            key = self._default_result(rs)
        self._current_result = key
        fi = self.results_panel.frame_index()
        if rs.frames() and fi is not None and fi != rs.frame:
            rs.set_frame(fi)
        q = rs.info(key)
        vals = rs.quantity(key)
        unit = self._unit_for(q.unit_kind)
        auto = auto_deformation_scale(rs.mesh.nodes, rs.displacement)
        scale = self.results_panel.scale_value(auto)
        loc = "nodal" if q.location == "nodal" else "nodal, extrapolated & averaged"
        title = f"{q.label} [{unit}]" + ("" if q.location == "nodal" else "\n(nodal avg.)")
        if rs.frames():
            title += "\n" + rs.frames()[rs.frame]
        rp = self.results_panel
        sec = rp.section_plane(rs.mesh.nodes.min(axis=0), rs.mesh.nodes.max(axis=0))
        self.viewport.show_results(rs.mesh, vals, rs.displacement, title, scale, rp.clim(), rp.undeformed.isChecked(),
                                   rp.edges.isChecked(), rp.cmap.currentText(), rp.bands.value(),
                                   rp.minmax.isChecked(), section=sec)
        if getattr(rs, "kind", "") == "cfd" and (rp.vectors.isChecked() or rp.streamlines.isChecked()):
            self.viewport.add_flow_overlay(rs.mesh, rs.velocity, rp.vectors.isChecked(), rp.streamlines.isChecked())
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
        from opencae.postprocessing.summary import summary_rows
        s = self.session
        rs = s.results
        if rs is None:
            mesh_txt = f"{s.mesh.n_nodes} nodes / {s.mesh.n_elements} {s.mesh.element_type}" if s.mesh else "none"
            self.summary.setHtml(f"<h3>{s.project.name} — {s.study.name}</h3><p>Mesh: {mesh_txt}</p>"
                                 "<p>No results yet.</p>")
            return
        rows, checks = summary_rows(s)
        chk = "".join(f"<p>{n}: <span class='{'ok' if ok else 'err'}'>{'OK' if ok else 'NOT satisfied'}</span> — "
                      f"{d}</p>" for n, ok, d in checks)
        stale = "" if s.project.results_are_current() else "<p class='warn'>⚠ Results are out of date with the model.</p>"
        note = {"structural": "Nodal stresses are extrapolated from integration points and averaged by CalculiX.",
                "thermal": "Temperatures are nodal solver values; heat fluxes are extrapolated and averaged.",
                "modal": "Mode-shape amplitudes are mass-normalised (relative); compare shapes, not magnitudes.",
                "cfd": "Flow quantities in SI units (m/s, Pa); nodal values from SU2's vertex-based finite volumes."
                }.get(getattr(rs, "kind", "structural"), "")
        self.summary.setHtml(f"<h3>{s.project.name} — {s.study.name} — {s.project.results.solver_version}</h3>"
                             f"{stale}<table>" +
                             "".join(f"<tr><th align=left>{a}</th><td>{b}</td><td>{c}</td></tr>" for a, b, c in rows) +
                             f"</table>{chk}<p style='color:{theme.FG_DIM}'>Solve time "
                             f"{s.project.results.wall_time_s:.2f} s. {note}</p>")

    # ================================================================== workflow
    def on_workflow_clicked(self, stage: str) -> None:
        page = {n: k for k, n in enumerate(self.ribbon_bars)}
        acts = {"Geometry": lambda: self.ribbon.setCurrentIndex(page.get("Geometry", 0)),
                "Materials": self.cmd_assign_material, "Mesh": self.cmd_generate_mesh,
                "Physics": self.cmd_analysis_settings,
                "Constraints & Loads": lambda: self.ribbon.setCurrentIndex(page.get("Boundary Conditions",
                                                                                    page.get("Setup", 0))),
                "Validation": self.cmd_validate, "Solve": self.cmd_solve,
                "Results": lambda: self.set_view_mode("results"), "Report": self.cmd_report}
        fn = acts.get(stage)
        if fn:
            self._safe(fn)

    # ================================================================== tools / help
    def cmd_settings(self) -> None:
        from opencae.ui.solver_settings import SolverSettingsDialog
        dlg = SolverSettingsDialog(self, self.session.project.analysis.threads)
        if dlg.exec():
            be = dlg.make_backend()
            if dlg.report is not None and dlg.report.exe == be.executable():
                be.verified, be.last_report, be._version = dlg.report.ok, dlg.report, dlg.report.version or None
            self.session.backend = be
            sbe = dlg.make_su2_backend()
            if dlg.su2_report is not None and dlg.su2_report.exe == sbe.executable():
                sbe.verified, sbe.last_report = dlg.su2_report.ok, dlg.su2_report
                sbe._version = dlg.su2_report.version or None
            self.session.cfd_backend = sbe
            self.refresh_all()
            if be.verified is None and be.executable():
                self._verify_solver_background()

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
                          f"Solver: {self.session.backend.status_text()}<br>Visualisation: VTK "
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
        th = getattr(self, "_solver_thread", None)
        if th is not None:
            try:
                th.quit()
                th.wait(130000)  # self-test has its own timeouts; never destroy a running thread
            except RuntimeError:
                pass
            self._solver_thread = None
        logging.getLogger("opencae").removeHandler(self.log_handler)
        self.viewport.close_plotter()
        ev.accept()
