"""GUI smoke / integration tests (run under xvfb-run; marked 'gui')."""
import time

import pytest

pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    from opencae.ui.theme import apply_theme
    a = QApplication.instance() or QApplication([])
    apply_theme(a)
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
    monkeypatch.setattr("PySide6.QtWidgets.QFileDialog.getOpenFileName",
                        staticmethod(lambda *a, **k: (str(tmp_path / "g.step"), "")))
    nb = len(s.regen.order)
    win.cmd_import()
    assert len(s.regen.order) == nb + 1 and not s.regen.errors


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
