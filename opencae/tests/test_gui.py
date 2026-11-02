"""GUI smoke / integration tests (run under xvfb-run; marked 'gui')."""
import time

import numpy as np

import pytest

pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    from opencae.ui.theme import apply_theme
    a = QApplication.instance() or QApplication([])
    apply_theme(a)
    from opencae.utilities.qt_guard import check_and_guard
    check_and_guard()
    return a


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QMessageBox
    from opencae.ui.main_window import MainWindow
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Discard))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    w = MainWindow(settings=QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat))
    w.show()
    yield w
    w.dirty = False
    w.close()


def pump(app, sec=0.3):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents()
        time.sleep(0.01)


def wait(app, w, timeout=240):
    t = time.time()
    while w.proc is not None and time.time() - t < timeout:
        app.processEvents()
        time.sleep(0.02)
    assert w.proc is None, "background job did not finish"


def test_every_command_has_a_handler(win):
    from PySide6.QtCore import SIGNAL
    assert len(win.A) > 80
    for name, act in win.A.items():
        assert act.receivers(SIGNAL("triggered(bool)")) > 0, f"action {name} has no handler"


def test_full_workflow_in_gui(app, win, tmp_path):
    win.cmd_example()
    pump(app)
    assert win.session.analysis_body() is not None
    win.cmd_generate_mesh()
    wait(app, win)
    assert win.session.mesh is not None and win.view_mode == "mesh"
    win.cmd_solve()
    wait(app, win)
    assert win.session.results is not None and win.view_mode == "results"
    for key in ("U_mag", "S_vm", "S_p1", "E_xx"):
        win.cmd_result(key)
        pump(app, 0.1)
    w, h = win.viewport.plotter.window_size
    win.viewport.pick_at(w // 2, h // 2)
    win.results_panel.anim_btn.setChecked(True)
    pump(app, 0.4)
    win.results_panel.anim_btn.setChecked(False)
    for n in ("view_front", "view_top", "view_iso", "fit", "ortho", "render_wireframe", "render_shaded_edges",
              "show_geometry", "show_mesh", "mesh_color_gamma", "show_results"):
        win.A[n].trigger()
        pump(app, 0.05)
    win.viewport.screenshot(tmp_path / "shot.png")
    assert (tmp_path / "shot.png").stat().st_size > 10000
    win.session.save(tmp_path / "g.ocae")
    win.dirty = False
    win.cmd_open(str(tmp_path / "g.ocae"))
    pump(app)
    assert win.session.results is not None and win.session.project.results_are_current()


def test_undo_redo_geometry(app, win):
    from opencae.project.model import Feature
    n0 = len(win.session.project.geometry.features)
    win.mutate("add box", lambda: win.session.add_feature(Feature("primitive", {"shape": "box"})), regen=False)
    assert len(win.session.project.geometry.features) == n0 + 1
    win.cmd_undo()
    assert len(win.session.project.geometry.features) == n0
    win.cmd_redo()
    assert len(win.session.project.geometry.features) == n0 + 1


def test_face_picking_selects_face(app, win):
    from opencae.project.model import Feature
    win.mutate("add box", lambda: win.session.add_feature(Feature("primitive", {"shape": "box"})), regen=False)
    win.viewport.fit()
    pump(app)
    w, h = win.viewport.plotter.window_size
    pk = win.viewport.pick_at(w // 2, h // 2)
    assert pk is not None and pk.kind == "face" and pk.index >= 0
    assert win.selection and win.selection[0].kind == "face"


def test_every_dialog_form_builds_and_validates(app, win):
    """Build each creation/edit form with its defaults and read the values back."""
    from opencae.ui import dialogs as D
    from opencae.ui.forms import FormWidget
    from opencae.materials.library import builtin_materials
    from opencae.project.model import Feature
    win.cmd_example()
    s = win.session
    s.add_feature(Feature("primitive", {"shape": "box", "position": [300, 0, 0]}))
    b, o = s.bodies, s.regen.order
    body = s.analysis_body()
    us = s.units
    forms = [D.primitive_fields(shape, us) for shape in D.PRIMITIVE_LABELS]
    forms += [D.transform_fields(b, o, us), D.boolean_fields(b, o, "union"), D.extrude_fields(b, o, us),
              D.edge_op_fields("fillet", b, o, us), D.edge_op_fields("chamfer", b, o, us),
              D.material_fields(builtin_materials()[0], us), D.mesh_fields(s.project.mesh_settings, b, o, us, 10.0),
              D.analysis_fields(s.project.analysis, "ccx")]
    forms += [D.constraint_fields(k, body, us) for k in ("fixed", "displacement", "symmetry")]
    forms += [D.load_fields(k, body, us) for k in ("force", "pressure", "gravity")]
    forms += [D.interaction_fields([body], us, kind=k) for k in ("bonded", "contact")]
    for f in s.project.geometry.features:
        forms.append(D.feature_fields(f, b, o, us))
    for fields in forms:
        vals = FormWidget(fields).values()
        assert isinstance(vals, dict)


def test_selecting_every_tree_item(app, win):
    from PySide6.QtWidgets import QTreeWidgetItemIterator
    win.cmd_example()
    win.cmd_generate_mesh()
    wait(app, win)
    win.cmd_solve()
    wait(app, win)
    win.view_mode = "geometry"
    win.refresh_all()
    it = QTreeWidgetItemIterator(win.tree)
    items = []
    while it.value():
        items.append(it.value())
        it += 1
    assert len(items) > 40
    for item in items:
        win.tree.setCurrentItem(item)
        app.processEvents()
    # properties panel "Apply" on the mesh settings round-trips without error
    win.on_tree_selected("mesh_settings", "")
    win.props._apply()
    assert not win.props.error.text()


def test_dialog_driven_commands(app, win, monkeypatch, tmp_path):
    """Run each interactive command with its dialog auto-accepted (default values)."""
    from opencae.ui.forms import ParamDialog
    from opencae.ui.viewport import Pick

    def auto_exec(self):
        self._accept()
        return int(self.result_values is not None)
    monkeypatch.setattr(ParamDialog, "exec", auto_exec)
    s = win.session
    for shape in ("box", "cylinder", "sphere", "cone", "plate", "beam"):
        win.cmd_primitive(shape)
    assert len(s.regen.order) == 6 and not s.regen.errors
    win.cmd_transform()
    win.cmd_boolean("union")  # box + cylinder (defaults: first two bodies)
    assert not s.regen.errors, s.regen.errors
    plate = next(b for b in s.bodies.values() if b.name.startswith("Plate"))
    win._select([Pick("edge", plate.id, 0), Pick("edge", plate.id, 2, additive=True)])
    win.cmd_edge_op("chamfer")
    win._select([Pick("edge", plate.id, 5)])
    win.cmd_edge_op("fillet")
    win.cmd_extrude()
    assert not s.regen.errors, s.regen.errors
    n_feat = len(s.project.geometry.features)
    win.cmd_undo()
    assert len(s.project.geometry.features) == n_feat - 1
    win.cmd_redo()
    # analysis setup on the cantilever template
    win.cmd_primitive("cantilever_beam")
    body = s.analysis_body()
    tip = s.project.loads[-1].targets[0].index
    root = s.project.constraints[-1].targets[0].index
    win._select([Pick("face", body.id, tip)])
    win.cmd_load("force")
    win.cmd_load("pressure")
    win.cmd_load("gravity")
    win._select([Pick("face", body.id, root)])
    win.cmd_constraint("displacement")
    win.cmd_constraint("symmetry")
    win.cmd_refinement()
    win.cmd_assign_material()
    win.cmd_new_material()
    assert len(s.project.loads) == 4 and len(s.project.constraints) == 3
    assert len(s.project.mesh_settings.refinements) == 1
    assert any(m.name == "Custom material" for m in s.project.materials)
    issues = win.cmd_validate()
    assert all(i.severity != "error" or "mesh" in i.message.lower() for i in issues)
    # export geometry in every exact format
    for ext in ("step", "igs", "brep", "stl"):
        monkeypatch.setattr("PySide6.QtWidgets.QFileDialog.getSaveFileName",
                            staticmethod(lambda *a, e=ext, **k: (str(tmp_path / f"g.{e}"), "")))
        win.cmd_export_geometry()
        assert (tmp_path / f"g.{ext}").stat().st_size > 100
    # import the STEP back as a new body
    monkeypatch.setattr("PySide6.QtWidgets.QFileDialog.getOpenFileNames",
                        staticmethod(lambda *a, **k: ([str(tmp_path / "g.step"), str(tmp_path / "g.stl")], "")))
    nb = len(s.regen.order)
    win.cmd_import()  # several files at once; multi-solid files are split into one body per solid
    assert len(s.regen.order) >= nb + 2 and not s.regen.errors, s.regen.errors
    win.cmd_assembly_check()
    assert "Contacts and interferences" in win.summary.toHtml()
    win.cmd_isolate()
    win.cmd_show_all()
    assert not s.project.geometry.hidden_bodies


def test_results_panel_controls(app, win):
    win.cmd_example()
    win.cmd_generate_mesh()
    wait(app, win)
    win.cmd_solve()
    wait(app, win)
    rp = win.results_panel
    for cb in (rp.undeformed, rp.edges, rp.minmax):
        cb.setChecked(not cb.isChecked())
        app.processEvents()
        cb.setChecked(not cb.isChecked())
    for i in range(rp.scale_mode.count()):
        rp.scale_mode.setCurrentIndex(i)
        app.processEvents()
    rp.scale.setText("50")
    rp.scale.editingFinished.emit()
    rp.manual.setChecked(True)
    rp.cmin.setText("10")
    rp.cmax.setText("100")
    rp.cmin.editingFinished.emit()
    assert rp.clim() == (10.0, 100.0)
    rp.cmap.setCurrentIndex(2)
    rp.bands.setValue(20)
    for i in range(rp.quantity.count()):
        rp.quantity.setCurrentIndex(i)
        app.processEvents()
    rp.anim_btn.setChecked(True)
    pump(app, 0.3)
    assert win.viewport.animating()
    rp.anim_btn.setChecked(False)
    assert not win.viewport.animating()


def test_cancel_button_stops_solver(app, win):
    win.cmd_example()
    win.session.project.mesh_settings.size_max = 2.0  # large enough that solving takes a while
    win.cmd_generate_mesh()
    wait(app, win)
    win.cmd_solve()
    assert win.proc is not None
    pump(app, 0.3)
    win.solver_panel.cancel_btn.click()  # real button click path
    wait(app, win, 30)
    assert win.session.results is None
    assert "cancel" in win.solver_panel.state.text().lower()


def test_node_and_element_highlight(app, win):
    """Regression: node/element selection crashed with 'Invalid color ... #111' (PyVista rejects 3-digit hex)."""
    from opencae.ui.viewport import Pick
    win.cmd_example()
    win.session.project.mesh_settings.size_max = 10
    win.session.generate_mesh()
    win.set_view_mode("mesh")
    win._select([Pick("node", "", 3), Pick("element", "", 5, additive=True)])
    assert len(win.viewport._groups.get("highlight", [])) >= 2


def _wait_for(app, cond, timeout=60):
    t = time.time()
    while not cond() and time.time() - t < timeout:
        app.processEvents()
        time.sleep(0.02)
    return cond()


@pytest.mark.solver
def test_startup_selftest_and_settings_test_button(app, win):
    assert _wait_for(app, lambda: "verified" in win.solver_label.text() or "FAILED" in win.solver_label.text())
    assert win.session.backend.verified is True and "verified" in win.solver_label.text()
    from opencae.ui.solver_settings import SolverSettingsDialog
    dlg = SolverSettingsDialog(win)
    dlg.test_btn.click()
    assert _wait_for(app, lambda: dlg.report is not None)
    assert dlg.report.ok and "PASSED" in dlg.out.toPlainText()
    dlg.reject()


def test_broken_solver_blocks_solve_with_reason(app, win, tmp_path):
    import sys as _sys
    from opencae.solver.base import CalculixBackend
    bad = tmp_path / "ccx"
    bad.write_text("#!/bin/sh\nexit 3\n")
    bad.chmod(0o755)
    if _sys.platform.startswith("win"):
        pytest.skip("POSIX shell stub")
    be = CalculixBackend(str(bad), [])
    rep = be.selftest()
    assert not rep.ok and be.verified is False
    win.session.backend = be
    win.cmd_example()
    win.refresh_all()
    assert not win.A["solve"].isEnabled()
    assert "self-test" in win.A["solve"].toolTip()
    assert "FAILED" in win.solver_label.text()
    issues, _ = win.session.validate()
    assert any("self-test" in i.message for i in issues)


def test_studies_thermal_and_modal_in_gui(app, win, monkeypatch):
    """New studies from the GUI (thermal, modal), thermal conditions via the dialogs, solving,
    frames in the results panel, dashboard and activation by double-click."""
    from opencae.ui.forms import ParamDialog
    from opencae.ui.viewport import Pick
    from opencae.ui.model_tree import ROLE
    from PySide6.QtWidgets import QTreeWidgetItemIterator
    monkeypatch.setattr(ParamDialog, "exec", lambda self: (self._accept(), int(self.result_values is not None))[1])
    s = win.session
    win.cmd_example()
    structural = s.project.active_study
    body = s.analysis_body()
    fixed = s.project.constraints[0].targets[0].index
    tip = s.project.loads[0].targets[0].index
    # --- thermal study
    orig = ParamDialog.__init__

    def init(self, title, fields, *a, **k):
        for f in fields:
            if f.key == "analysis_type" and title == "New study":
                f.value = "thermal_steady"
        orig(self, title, fields, *a, **k)
    monkeypatch.setattr(ParamDialog, "__init__", init)
    win.cmd_new_study()
    monkeypatch.setattr(ParamDialog, "__init__", orig)
    assert len(s.project.studies) == 2 and s.project.analysis.analysis_type == "thermal_steady"
    assert not win.A["bc_fixed"].isEnabled() and win.A["bc_temp"].isEnabled()
    win._select([Pick("face", body.id, fixed)])
    win.cmd_constraint("temperature")
    win._select([Pick("face", body.id, tip)])
    win.cmd_load("convection")
    assert [c.kind for c in s.project.constraints] == ["temperature"]
    assert s.project.loads[0].kind == "convection"
    win.cmd_generate_mesh()
    wait(app, win)
    win.cmd_solve()
    wait(app, win)
    assert s.results is not None and s.results.kind == "thermal"
    assert win.results_panel.current_key() == "T"
    assert not win.results_panel.anim_btn.isEnabled()
    assert "Max temperature" in win.summary.toHtml()
    # --- modal study (copy of the structural setup)
    win.cmd_activate_study(structural)
    assert s.project.analysis.analysis_type == "linear_static"
    s.duplicate_study(structural, "Modes")
    s.project.analysis.analysis_type = "modal"
    s.project.analysis.n_modes = 4
    win.refresh_all()
    win.cmd_generate_mesh()
    wait(app, win)
    win.cmd_solve()
    wait(app, win)
    rs = s.results
    assert rs.kind == "modal" and len(rs.frequencies) == 4
    assert win.results_panel.frame.isVisible() and win.results_panel.frame.count() == 4
    win.results_panel.frame.setCurrentIndex(2)
    pump(app, 0.1)
    assert rs.frame == 2
    win.results_panel.anim_btn.setChecked(True)
    pump(app, 0.3)
    win.results_panel.anim_btn.setChecked(False)
    win.cmd_plot_explorer("spectrum")
    pe = win._plot_explorer
    assert np.allclose(pe.data[0][2], rs.frequencies) and len(pe.data) == 4  # + cumulative mass X/Y/Z
    pe.close()
    # --- dashboard and tree activation
    win.cmd_studies_dashboard()
    html = win.summary.toHtml()
    assert "Modes" in html and "Hz" in html and "°C" in html
    it = QTreeWidgetItemIterator(win.tree)
    target = None
    while it.value():
        if it.value().data(0, ROLE) == ("study", structural):
            target = it.value()
        it += 1
    win.tree._double(target, 0)
    assert s.project.active_study == structural


def test_section_plane_and_probe_on_section(app, win):
    win.cmd_example()
    win.cmd_generate_mesh()
    wait(app, win)
    win.cmd_solve()
    wait(app, win)
    rp = win.results_panel
    full = win.viewport._res_surface.n_points
    rp.section_axis.setCurrentText("X")
    rp.section_pos.setValue(50)
    rp.section.setChecked(True)
    pump(app, 0.2)
    surf = win.viewport._res_surface
    ids = np.asarray(surf.point_data["node_id"])
    nodes = win.session.mesh.nodes
    # only (whole) elements behind the plane x = 100 remain; their nodes keep exact nodal values
    assert nodes[ids, 0].min() < 1e-9 and nodes[ids, 0].max() < 100.0 + 15.0
    assert np.allclose(surf.point_data["value"], win.session.results.quantity(win._current_result)[ids])
    assert surf.n_points != full
    w, h = win.viewport.plotter.window_size
    win.viewport.set_view("iso")
    pk = win.viewport.pick_at(w // 2, h // 2)
    rp.section.setChecked(False)
    pump(app, 0.1)
    assert win.viewport._res_surface.n_points == full


def test_plot_explorer_and_convergence_worker(app, win, monkeypatch, tmp_path):
    from opencae.ui.forms import ParamDialog
    s = win.session
    win.cmd_example()
    win.cmd_generate_mesh()
    wait(app, win)
    win.cmd_solve()
    wait(app, win)
    win.cmd_plot_explorer()
    pe = win._plot_explorer
    pe.quantity.setCurrentIndex(pe.quantity.findData("S_xx"))
    pe.p0.setText("0, 0, 10")  # top fibre along the beam (section -10..10)
    pe.p1.setText("200, 0, 10")
    pe.replot()
    label, d, v = pe.data[0]
    # bending stress at the top fibre: sigma_xx = M(x) c / I = F (L - x) (h/2) / I (tension at the top)
    xs = np.array([50.0, 100.0, 150.0])
    exact = 1000.0 * (200.0 - xs) * 10.0 / (20 ** 4 / 12)
    assert np.interp(xs, d, v) == pytest.approx(exact, rel=0.08)
    assert pe.save_csv(str(tmp_path / "line.csv")).stat().st_size > 1000
    assert pe.save_png(str(tmp_path / "line.png")).stat().st_size > 5000
    pe.close()

    def auto(self):
        for f in self.form.fields:
            if f.key == "sizes":
                self.form.editors["sizes"].setText("20, 10, 7")
        self._accept()
        return 1
    monkeypatch.setattr(ParamDialog, "exec", auto)
    before = (s.project.mesh.mesh_id, s.project.results.directory)
    win.cmd_mesh_convergence()
    wait(app, win, 600)
    assert win.last_convergence is not None, win.solver_panel
    assert len(win.last_convergence["elements"]) == 3
    assert (s.project.mesh.mesh_id, s.project.results.directory) == before
    assert "Mesh convergence" in win.summary.toHtml()
    pe = win._plot_explorer
    assert pe.kind.currentData() == "convergence" and len(pe.data) >= 1
    pe.close()


@pytest.mark.cfd
def test_flow_study_in_gui(app, win, monkeypatch):
    from opencae.ui.forms import ParamDialog
    from opencae.project.model import Feature
    s = win.session

    def auto(self):
        for f in self.form.fields:
            if f.key == "speed":
                self.form.editors["speed"].setText("0.02")
            if f.key == "fluid":
                self.form.set_value("fluid", "Water (20 °C)")
        self._accept()
        return 1
    monkeypatch.setattr(ParamDialog, "exec", auto)
    s.add_feature(Feature("primitive", {"shape": "box", "dx": 10, "dy": 10, "dz": 10}))
    win.refresh_all()
    win.cmd_flow_domain()
    assert s.project.analysis.analysis_type == "cfd_incompressible"
    assert s.project.analysis.fluid_density == pytest.approx(998.2)
    assert win.A["flow_inlet"].isEnabled() and not win.A["bc_fixed"].isEnabled()
    assert s.project.mesh_settings.element_order == 1
    s.project.mesh_settings.size_max = 8.0
    win.cmd_generate_mesh()
    wait(app, win)
    win.cmd_solve()
    wait(app, win, 600)
    rs = s.results
    assert rs is not None and rs.kind == "cfd", win.solver_panel
    assert win.results_panel.current_key() == "V_mag"
    assert win.results_panel.vectors.isVisible()
    win.results_panel.vectors.setChecked(True)
    win.results_panel.streamlines.setChecked(True)
    pump(app, 0.3)
    names = set(win.viewport.plotter.actors)
    assert any("flow_vectors" in n for n in names) and any("flow_streamlines" in n for n in names)
    html = win.summary.toHtml()
    assert "Max velocity" in html and "flow-rate balance" in html


@pytest.mark.cfd
def test_2d_flow_in_gui(app, win, monkeypatch):
    from opencae.ui.forms import ParamDialog
    from opencae.ui.viewport import Pick
    from opencae.project.model import Feature
    from opencae.geometry.references import find_planar_face
    monkeypatch.setattr(ParamDialog, "exec", lambda self: (self._accept(), int(self.result_values is not None))[1])
    s = win.session
    s.add_feature(Feature("primitive", {"shape": "box", "dx": 100, "dy": 10, "dz": 1}))
    b = s.analysis_body()
    st = s.new_study("2D channel", "cfd_incompressible")
    a = st.analysis
    a.cfd_dimension, a.fluid_density, a.fluid_viscosity = "2d", 1000.0, 1e-3
    top = find_planar_face(b, [0, 0, 1], [0, 0, 1])
    a.cfd_face = f"{b.id}|face:{top}"
    st.mesh_settings.element_order, st.mesh_settings.size_max = 1, 1.0
    win.refresh_all()
    win.on_tree_selected("solver", "")  # the analysis form lists the planar faces
    ein = next(e.index for e in b.edges if abs(e.centroid[2] - 1) < 1e-9 and abs(e.centroid[0]) < 1e-9)
    eout = next(e.index for e in b.edges if abs(e.centroid[2] - 1) < 1e-9 and abs(e.centroid[0] - 100) < 1e-9)
    win._select([Pick("edge", b.id, ein)])
    win.cmd_load("velocity_inlet")
    win._select([Pick("edge", b.id, eout)])
    win.cmd_load("pressure_outlet")
    assert [l.targets[0].kind for l in s.project.loads] == ["edge", "edge"]
    win.cmd_generate_mesh()
    wait(app, win)
    assert s.mesh.element_type == "F2D3"
    win.cmd_solve()
    wait(app, win, 600)
    assert s.results is not None and s.results.kind == "cfd" and s.project.results.summary["two_dimensional"]
    assert win.viewport._res_surface.n_cells >= s.mesh.n_elements


def test_tangent_face_selection(app, win):
    from opencae.project.model import Feature
    from opencae.ui.viewport import Pick
    s = win.session
    s.add_feature(Feature("primitive", {"shape": "cylinder", "radius": 5, "height": 20}))
    s.add_feature(Feature("primitive", {"shape": "box", "dx": 10, "dy": 10, "dz": 10, "position": [20, 0, 0]}))
    win.refresh_all()
    cyl, box = (s.bodies[b] for b in s.regen.order)
    side = next(f.index for f in cyl.faces if f.surface_type == "cylinder")
    win._select([Pick("face", cyl.id, side)])
    win.cmd_select_tangent()
    assert [p.index for p in win.selection] == [side]  # end caps meet the side at 90 degrees
    win._select([Pick("face", box.id, 0)])
    win.cmd_select_body_faces()
    assert len(win.selection) == 6


def test_assembly_contact_in_gui(app, win, monkeypatch):
    """Two stacked blocks: detect touching faces, create a contact interaction, mesh separately,
    solve, contact results; the interaction appears in the tree and can be edited and deleted."""
    from opencae.ui.forms import ParamDialog
    from opencae.ui.model_tree import ROLE
    from opencae.project.model import Constraint, Feature, Load
    from opencae.geometry.references import find_planar_face, make_ref
    from PySide6.QtWidgets import QTreeWidgetItemIterator
    s = win.session
    s.add_feature(Feature("primitive", {"shape": "box", "dx": 10, "dy": 10, "dz": 10}))
    s.add_feature(Feature("primitive", {"shape": "box", "dx": 10, "dy": 10, "dz": 10, "position": [0, 0, 10]}))
    lo, hi = (s.bodies[b] for b in s.regen.order)
    s.set_study_bodies(s.regen.order)
    for b in (lo, hi):
        s.project.assign_material(b.id, "mat_steel")
    F = lambda b, n, p: make_ref(b, "face", find_planar_face(b, n, p))  # noqa: E731
    s.project.constraints = [
        Constraint("symmetry", [F(lo, [0, 0, -1], [0, 0, 0])], name="base"),
        Constraint("symmetry", [F(lo, [-1, 0, 0], [0, 0, 0]), F(hi, [-1, 0, 0], [0, 0, 10])], name="sx"),
        Constraint("symmetry", [F(lo, [0, -1, 0], [0, 0, 0]), F(hi, [0, -1, 0], [0, 0, 10])], name="sy")]
    s.project.loads = [Load("pressure", [F(hi, [0, 0, 1], [0, 0, 20])], {"magnitude": 100.0}, name="p")]
    s.project.mesh_settings.size_max = 2.5
    win.refresh_all()
    assert win.A["int_detect"].isEnabled() and win.A["int_contact"].isEnabled()
    orig = ParamDialog.__init__

    def init(self, title, fields, *a, **k):
        for f in fields:
            if f.key == "kind" and title == "Detect touching faces":
                f.value = "contact"
        orig(self, title, fields, *a, **k)
    monkeypatch.setattr(ParamDialog, "__init__", init)
    monkeypatch.setattr(ParamDialog, "exec", lambda self: (self._accept(), int(self.result_values is not None))[1])
    win.cmd_detect_interactions()
    monkeypatch.setattr(ParamDialog, "__init__", orig)
    assert len(s.project.interactions) == 1 and s.project.interactions[0].kind == "contact"
    assert s.project.mesh_settings.assembly_mesh == "separate"
    win.cmd_generate_mesh()
    wait(app, win)
    win.cmd_solve()
    wait(app, win)
    assert s.results is not None and s.project.results.completed
    assert s.project.results.summary["max_contact_pressure"] == pytest.approx(100.0, rel=0.05)
    assert "contact pressure" in win.summary.toHtml().lower()
    it = QTreeWidgetItemIterator(win.tree)
    found = None
    while it.value():
        if it.value().data(0, ROLE)[0] == "interaction":
            found = it.value()
        it += 1
    assert found is not None
    ident = found.data(0, ROLE)[1]
    win.on_tree_selected("interaction", ident)
    win.props._apply()
    assert not win.props.error.text()
    win._delete("interaction", ident)
    assert s.project.interactions == []
    win.cmd_undo()
    assert len(s.project.interactions) == 1


def test_qt_none_refcount_guard(app):
    """After the guard, None can no longer be freed by the PySide6 / Python 3.11 defect."""
    import sys
    from opencae.utilities.qt_guard import check_and_guard
    info = check_and_guard()
    if sys.version_info < (3, 12) and info.get("affected"):
        assert info["guarded"] and sys.getrefcount(None) > 1 << 39
    else:
        assert not info["affected"] or info["guarded"]
