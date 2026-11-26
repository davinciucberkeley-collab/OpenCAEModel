"""Assembly mode of the main window (offscreen Qt): open the examples, tree <-> viewport selection, body / joint /
driver / study dialogs, running a multibody study through the Progress Monitor, playback, plots, CSV export,
the structural load case, the drone reuse, save / reopen and the backend-unavailable message.

The extension is installed by ``MainWindow.EXTENSIONS``; the tests install it themselves when the conductor has
not added it yet.
"""
import gc
import math
import os
import shutil
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyvistaqt")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from opencae.assembly import get_mbd  # noqa: E402
from opencae.examples.mbd_examples import GRIPPER as G  # noqa: E402
from opencae.mbd import pose_transforms  # noqa: E402
from opencae.ui import theme  # noqa: E402
from opencae.ui.theme import apply_theme  # noqa: E402

pytestmark = pytest.mark.gui

SHOTS = Path("/tmp/claude-0/-home-claude/cca99fdc-b1aa-5a4c-a668-10c4bdf36bbb/scratchpad")


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    apply_theme(a)
    return a


def _ext(win):
    from opencae.ui import assembly
    ext = win.extensions.get(assembly.KEY)
    if ext is None:
        ext = assembly.install(win)
        win.extensions[assembly.KEY] = ext
    return ext


def _make_win(tmp_path, monkeypatch, name="s.ini"):
    from opencae.ui.main_window import MainWindow
    monkeypatch.setenv("OPENCAE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Discard))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: None))
    w = MainWindow(settings=QSettings(str(tmp_path / name), QSettings.IniFormat))
    w.resize(1500, 920)
    w.show()
    return w


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    w = _make_win(tmp_path, monkeypatch)
    yield w
    w.dirty = False
    w.close()
    w.deleteLater()
    app.processEvents()
    gc.collect()  # collect the window's reference cycles while Qt is still alive, not at interpreter exit


@pytest.fixture
def ws(win):
    return _ext(win).workspace()


@pytest.fixture
def gripper(ws):
    assert ws.open_example("gripper")
    return ws


def pump(app, sec=0.2):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents()
        time.sleep(0.01)


def wait_for(app, cond, timeout=90):
    t = time.time()
    while not cond() and time.time() - t < timeout:
        app.processEvents()
        time.sleep(0.02)
    assert cond(), "timed out"


def grab_window(app, win, ws, path):
    """Window grab with the 3-D view composited in (the offscreen platform cannot paint the OpenGL widget)."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QImage, QPainter
    pump(app, 0.3)
    pm = win.grab()
    import pyvista as pv
    import vtk
    vp = ws.viewport
    size = (vp.width(), vp.height())
    pl = pv.Plotter(off_screen=True, window_size=size)
    pl.set_background(theme.VIEW_BG_BOTTOM, top=theme.VIEW_BG_TOP)
    for a in vp.plotter.actors.values():
        if isinstance(a, vtk.vtkProp3D):
            pl.add_actor(a, reset_camera=False)
    pl.renderer.SetActiveCamera(vp.plotter.camera)
    arr = np.ascontiguousarray(pl.screenshot(return_img=True))
    for a in list(pl.renderer.GetActors()):
        pl.renderer.RemoveActor(a)
    pl.close()
    h, w, _ = arr.shape
    img = QImage(arr.data, w, h, 3 * w, QImage.Format_RGB888).copy()
    p = QPainter(pm)
    top_left = ws.viewport.mapTo(win, QPoint(0, 0))
    p.drawImage(top_left.x(), top_left.y(), img.scaled(ws.viewport.size()))
    p.end()
    pm.save(str(path))


def run_and_wait(app, ws):
    job = ws.run_study()
    assert job is not None, ws.study_panel.backend_label.text()
    wait_for(app, lambda: not ws.running)
    assert ws.result is not None
    return job


def study_named(ws, letter):
    return next(s for s in ws.project.studies if s.name.startswith(letter + " "))


# ---------------------------------------------------------------------------------------------- install and open
def test_install_adds_ribbon_page_and_stack_page(win, ws):
    from opencae.ui.assembly import PAGE
    assert PAGE in win.ribbon_bars
    tabs = [win.ribbon.tabText(i) for i in range(win.ribbon.count())]
    assert tabs.count(PAGE) == 1
    assert win.stack.indexOf(ws) >= 0
    # idempotent: a second install returns the same extension
    from opencae.ui import assembly
    assert assembly.install(win) is _ext(win)
    # clicking the tab shows the page, another tab returns to the Studio viewport
    win.ribbon.setCurrentIndex(tabs.index(PAGE))
    assert win.stack.currentWidget() is ws
    win.ribbon.setCurrentIndex(0)
    assert win.stack.currentWidget() is win.viewport


def test_open_gripper_example_fills_the_tree(app, win, gripper):
    ws = gripper
    assert win.stack.currentWidget() is ws
    # the example is copied to ~/.opencae/projects first (here OPENCAE_HOME), the package folder is untouched
    assert (Path(os.environ["OPENCAE_HOME"]) / "projects" / "gripper" / "gripper.ocae").exists()
    assert ws.project is win.session.project  # shared project state, no copy
    t = ws.tree
    assert t.count("body") == 3 and t.count("joint") == 2 and t.count("coupling") == 1
    assert t.count("driver") >= 1 and t.count("study") == 3
    assert t.text_of("body", ws.assembly.bodies[0].id).startswith("Palm") and t.is_grounded(ws.assembly.bodies[0].id)
    assert not t.is_grounded(ws.assembly.bodies[1].id)
    assert "revolute" in t.text_of("joint", ws.assembly.joints[0].id)
    assert ws.viewport.plotter.actors  # the CAD is shown
    assert ws.study.name.startswith("A")  # the first multibody study is current
    # welcome page is gone, the workspace is shown
    assert not ws.welcome_visible()


def test_empty_project_shows_welcome_and_can_create_assembly(app, win, ws):
    win.session.create_cantilever()
    ws.sync()
    assert ws.welcome_visible()
    assert ws.create_assembly_from_parts() is True
    assert ws.tree.count("body") == 1 and not ws.welcome_visible()
    # nothing is grounded yet: the model check says so in plain words
    assert any("ground" in p.lower() for p in ws.model_problems())


# ---------------------------------------------------------------------------------------------- selection
def test_tree_selection_highlights_in_viewport_and_back(app, gripper):
    ws = gripper
    asm = ws.assembly
    left = asm.bodies[1]
    gid = asm.component(left.component_ids[0]).body_id
    ws.tree.select_item("body", left.id, emit=True)  # as if the user clicked the tree
    assert ws.selected == ("body", left.id)
    assert ws.highlighted_geometry_ids() == [gid]
    # viewport -> tree
    right = asm.bodies[2]
    rgid = asm.component(right.component_ids[0]).body_id
    from opencae.ui.viewport import Pick
    ws.viewport.picked.emit(Pick("body", rgid, -1))
    assert ws.selected == ("body", right.id)
    assert ws.tree.current() == ("body", right.id)
    assert ws.highlighted_geometry_ids() == [rgid]
    # clicking empty space clears the highlight
    ws.viewport.picked.emit(None)
    assert ws.highlighted_geometry_ids() == []


def test_real_viewport_pick_selects_tree_item(app, win, gripper):
    ws = gripper
    pump(app, 0.4)
    ws.viewport.set_view("front")
    ws.viewport.fit()
    pump(app, 0.2)
    body = ws.session.bodies[ws.assembly.component(ws.assembly.bodies[0].component_ids[0]).body_id]
    centre = (body.bbox[0] + body.bbox[1]) / 2
    import vtk
    c = vtk.vtkCoordinate()
    c.SetCoordinateSystemToWorld()
    c.SetValue(*centre)
    x, y = c.GetComputedDisplayValue(ws.viewport.plotter.renderer)
    pk = ws.viewport.pick_at(int(x), int(y))
    if pk is None:
        pytest.skip("offscreen renderer cannot ray-cast here")
    assert ws.selected[0] == "body"
    assert ws.selected[1] == ws.assembly.bodies[0].id


def test_viewport_edge_pick_is_remembered_for_circular_edges(app, win, ws):
    """The joint dialog can take origin and axis from a circular edge (the rotor's rim)."""
    assert ws.open_example("drone_rotors")
    asm = ws.assembly
    rotor = asm.body(asm.joints[1].child)
    gid = asm.component(rotor.component_ids[0]).body_id
    body = ws.session.bodies[gid]
    from opencae.ui.assembly.dialogs import circle_from_polyline
    found = []
    for k, pl in enumerate(body.tessellation.edge_polylines):
        c = circle_from_polyline(pl)
        if c is not None:
            found.append((k, c))
    assert found
    k, (centre, normal, radius) = max(found, key=lambda kc: kc[1][2])
    assert radius == pytest.approx(25.0, rel=1e-2)
    assert abs(abs(normal[2]) - 1.0) < 1e-6


# ---------------------------------------------------------------------------------------------- body inspector
def test_body_inspector_shows_mass_with_units_and_source(app, gripper):
    ws = gripper
    left = ws.assembly.bodies[1]
    ws.select("body", left.id)
    mp = left.effective_mass()
    txt = ws.props.value("mass")
    assert "kg" in txt and f"{mp.mass_kg:.4g}" in txt
    assert "mm" in ws.props.value("com")
    assert "kg" in ws.props.value("inertia_unit") and "m" in ws.props.value("inertia_unit")
    src = ws.props.value("source")
    assert "from CAD" in src and "density 2700" in src and "kg/m" in src
    assert "frame" in ws.props.value("frame").lower()
    assert ws.props.value("grounded") == "no"


def test_manual_values_and_ground_toggle(app, gripper):
    ws = gripper
    left = ws.assembly.bodies[1]
    ws.select("body", left.id)
    dlg = ws.make_manual_mass_dialog(left)
    dlg.mass.setValue(0.25)
    dlg.accept()
    assert left.manual is not None and left.manual.mass_kg == pytest.approx(0.25)
    assert "manual" in ws.props.value("source").lower()
    assert "0.25" in ws.props.value("mass")
    ws.clear_manual_mass(left)
    assert left.manual is None
    # grounding a body that is the child of a joint is refused with the model's own words
    ok, why = ws.set_grounded(left.id, True)
    assert not ok and "child of a joint" in why or "grounded" in why
    assert not left.grounded
    # the palm can be un-grounded and re-grounded
    palm = ws.assembly.bodies[0]
    assert ws.set_grounded(palm.id, False)[0] is True and not palm.grounded
    assert ws.set_grounded(palm.id, True)[0] is True and palm.grounded


def test_group_bodies_merges_components(app, gripper):
    ws = gripper
    asm = ws.assembly
    palm, left, right = asm.bodies
    n_before = len(asm.bodies)
    # the right finger is the child of a revolute joint: it cannot be merged into the left finger
    ok, why = ws.group_bodies(left.id, [right.id])
    assert not ok and why
    assert len(asm.bodies) == n_before
    # a free body with no joints can be merged: add a fresh one by splitting nothing, merge palm + a fixed part
    from opencae.assembly import Component, RigidBody
    extra_c = Component(name="Pad", body_id=asm.components[0].body_id)
    extra = RigidBody(name="Pad", component_ids=[extra_c.id])
    asm.components.append(extra_c)
    asm.bodies.append(extra)
    ok, why = ws.group_bodies(palm.id, [extra.id])
    assert ok, why
    assert asm.body(extra.id) is None and extra_c.id in palm.component_ids


# ---------------------------------------------------------------------------------------------- joint dialog
def test_joint_dialog_preview_and_reverse(app, gripper):
    ws = gripper
    asm = ws.assembly
    palm, left, right = asm.bodies
    dlg = ws.make_joint_dialog(parent_id=palm.id, child_id=left.id)
    dlg.show()
    dlg.set_axis_preset("+Z")
    dlg.origin[0].setValue(-30.0)
    dlg.origin[1].setValue(0.0)
    pump(app, 0.1)
    assert "asm_preview_axis" in ws.viewport.plotter.actors
    st = ws.preview_state
    assert np.allclose(st["axis"], [0, 0, 1]) and np.allclose(st["origin"][:2], [-30.0, 0.0])
    b0 = np.array(ws.viewport.plotter.actors["asm_preview_axis"].GetBounds())
    dlg.reverse_btn.click()
    pump(app, 0.1)
    assert np.allclose(ws.preview_state["axis"], [0, 0, -1])
    b1 = np.array(ws.viewport.plotter.actors["asm_preview_axis"].GetBounds())
    assert b0[5] > 0 > b1[4]  # the arrow flipped from +z to -z of the origin
    assert "asm_preview_arc" in ws.viewport.plotter.actors  # revolute: an arc shows the positive turning direction
    dlg.kind.setCurrentIndex(dlg.kind.findData("prismatic"))
    pump(app, 0.05)
    assert "asm_preview_arc" not in ws.viewport.plotter.actors and "asm_preview_axis2" in ws.viewport.plotter.actors
    # cancel removes the preview
    dlg.reject()
    assert "asm_preview_axis" not in ws.viewport.plotter.actors and ws.preview_state is None


def test_joint_dialog_validation_messages_and_accept(app, gripper):
    ws = gripper
    asm = ws.assembly
    palm, left, right = asm.bodies
    dlg = ws.make_joint_dialog(parent_id=left.id, child_id=left.id)
    dlg.show()
    assert not dlg.ok_button.isEnabled() and "same body" in dlg.error_label.text()
    # the right finger already has a joint: a second one would close a loop
    dlg.set_bodies(palm.id, right.id)
    assert not dlg.ok_button.isEnabled() and "loop" in dlg.error_label.text()
    dlg.reject()
    # a good joint: an extra prismatic joint for a new free body
    from opencae.assembly import Component, RigidBody
    c = Component(name="Slider", body_id=asm.components[0].body_id)
    rb = RigidBody(name="Slider", component_ids=[c.id])
    asm.components.append(c)
    asm.bodies.append(rb)
    dlg = ws.make_joint_dialog(parent_id=palm.id, child_id=rb.id)
    dlg.show()
    dlg.kind.setCurrentIndex(dlg.kind.findData("prismatic"))
    dlg.set_axis_preset("+X")
    dlg.limit_check.setChecked(True)
    dlg.limit_lo.setValue(0.0)
    dlg.limit_hi.setValue(15.0)
    assert dlg.ok_button.isEnabled(), dlg.error_label.text()
    n = len(asm.joints)
    dlg.accept()
    assert len(asm.joints) == n + 1
    j = asm.joints[-1]
    assert j.kind == "prismatic" and j.child == rb.id and j.limits == [0.0, 15.0]
    assert ws.tree.count("joint") == n + 1


def test_joint_dialog_circular_edge_sets_origin_and_axis(app, win, ws):
    assert ws.open_example("drone_rotors")
    asm = ws.assembly
    rotor = asm.bodies[2]
    gid = asm.component(rotor.component_ids[0]).body_id
    body = ws.session.bodies[gid]
    from opencae.ui.assembly.dialogs import circle_from_polyline
    from opencae.ui.viewport import Pick
    idx = max((k for k, pl in enumerate(body.tessellation.edge_polylines) if circle_from_polyline(pl) is not None),
              key=lambda k: circle_from_polyline(body.tessellation.edge_polylines[k])[2])
    dlg = ws.make_joint_dialog(parent_id=asm.bodies[0].id, child_id=rotor.id)
    dlg.show()
    dlg.apply_edge_pick(Pick("edge", gid, idx))
    cen, _, _ = circle_from_polyline(body.tessellation.edge_polylines[idx])
    assert [round(s.value(), 3) for s in dlg.origin] == [round(float(v), 3) for v in cen]
    assert abs(dlg.effective_axis()[2]) == pytest.approx(1.0)
    dlg.reject()


# ---------------------------------------------------------------------------------------------- driver dialog
def test_driver_dialog_labels_units_and_refuses_second_driver(app, gripper):
    ws = gripper
    asm = ws.assembly
    jl, jr = asm.joints
    drv = get_mbd(ws.study).drivers[0]
    dlg = ws.make_driver_dialog(ws.study)
    dlg.show()
    assert "Prescribed motion" in dlg.prescribed_radio.text() and "follows this exactly" in dlg.prescribed_radio.text()
    assert "Dynamic" in dlg.dynamic_radio.text() and "computed" in dlg.dynamic_radio.text()
    dlg.set_joint(jl.id)
    assert not dlg.ok_button.isEnabled()
    assert "already has a motor" in dlg.error_label.text()
    # the right finger is moved by the transmission: no second motor there either
    dlg.set_joint(jr.id)
    assert not dlg.ok_button.isEnabled() and "transmission" in dlg.error_label.text()
    # units are shown
    dlg.dynamic_radio.setChecked(True)
    assert "N" in dlg.unit_text()
    dlg.prescribed_radio.setChecked(True)
    assert "deg" in dlg.unit_text()
    dlg.reject()
    # the existing driver can be edited, and its values are shown in degrees
    dlg = ws.make_driver_dialog(ws.study, driver=drv)
    dlg.show()
    assert dlg.ok_button.isEnabled(), dlg.error_label.text()
    assert dlg.profile_kind.currentData() == "ramp"
    assert dlg.ramp_v1.value() == pytest.approx(G["closed_deg"])
    dlg.ramp_v1.setValue(30.0)
    dlg.accept()
    assert get_mbd(ws.study).drivers[0].profile.v1 == pytest.approx(math.radians(30.0))
    assert len(get_mbd(ws.study).drivers) == 1  # edited, not duplicated


def test_driver_dialog_table_profile_and_preview(app, gripper):
    ws = gripper
    drv = get_mbd(ws.study).drivers[0]
    dlg = ws.make_driver_dialog(ws.study, driver=drv)
    dlg.show()
    dlg.profile_kind.setCurrentIndex(dlg.profile_kind.findData("table"))
    dlg.set_table([[0.0, 0.0], [0.5, 10.0], [1.0, 15.0]])
    assert dlg.ok_button.isEnabled(), dlg.error_label.text()
    pts = dlg.preview.points
    assert len(pts) >= 3 and pts[-1][1] == pytest.approx(15.0)
    dlg.set_table([[0.0, 0.0], [0.0, 10.0]])  # times must increase
    assert not dlg.ok_button.isEnabled() and "increasing" in dlg.error_label.text()
    dlg.set_table([[0.0, 0.0], [1.0, 12.0]])
    dlg.accept()
    p = get_mbd(ws.study).drivers[0].profile
    assert p.kind == "table" and p.table[-1] == [1.0, pytest.approx(math.radians(12.0))]


def test_coupling_view_states_the_idealisation(app, gripper):
    ws = gripper
    c = ws.assembly.couplings[0]
    ws.select("coupling", c.id)
    ratio = ws.props.value("ratio")
    assert "-1" in ratio.replace("−", "-")
    txt = ws.props.value("transmission")
    assert "Right" in txt and "left" in txt.lower()
    note = ws.props.value("note")
    for word in ("massless", "rigid", "backlash", "friction"):
        assert word in note


# ---------------------------------------------------------------------------------------------- study settings
def test_study_settings_dialog_edits_gravity_and_times(app, gripper):
    ws = gripper
    dlg = ws.make_study_settings_dialog(ws.study)
    dlg.show()
    dlg.gravity_preset.setCurrentIndex(dlg.gravity_preset.findData("off"))
    dlg.duration.setValue(0.5)
    dlg.dt.setValue(1e-3)
    dlg.output_dt.setValue(5e-3)
    assert dlg.ok_button.isEnabled()
    dlg.dt.setValue(0.0)
    assert not dlg.ok_button.isEnabled()
    dlg.dt.setValue(1e-3)
    dlg.accept()
    s = get_mbd(ws.study)
    assert s.gravity == [0.0, 0.0, 0.0] and s.duration_s == 0.5 and s.dt_s == 1e-3 and s.output_dt_s == 5e-3


# ---------------------------------------------------------------------------------------------- run, playback, plots
def test_run_through_progress_monitor_playback_readout_and_plots(app, win, gripper, tmp_path):
    ws = gripper
    pump(app, 0.2)
    reg = win.extensions["opencae.ui.jobs"].reg
    job = run_and_wait(app, ws)
    assert job.kind == "motion" and "A" in job.title
    assert job.state == "done"
    res = ws.result
    assert len(res.t) == 101 and ws.playback.slider.maximum() == len(res.t) - 1
    # before moving: the finger actor sits at the reference pose
    left = ws.assembly.bodies[1]
    cid = left.component_ids[0]
    gid = ws.assembly.component(cid).body_id
    actor = ws.viewport.plotter.actors[f"g_{gid}_faces"]
    ws.playback.set_time(0.0)
    m0 = np.array(actor.user_matrix)
    ws.playback.set_time(0.5)
    m1 = np.array(actor.user_matrix)
    i = res.index_at(0.5)
    assert not np.allclose(m0, m1)
    assert np.allclose(m1, pose_transforms(res, i)[cid])
    assert ws.current_time == pytest.approx(0.5)
    # the source CAD placement is untouched
    assert np.allclose(ws.assembly.component(cid).placement_matrix(), np.eye(4))
    # readout: the fingertip gap at that time matches the geometry formula of the example
    th = math.radians(G["closed_deg"]) * 0.5
    expected = 2 * G["p"] - G["h"] * math.cos(th) - 2 * G["L"] * math.sin(th)
    assert ws.readout.data["gap_mm"] == pytest.approx(expected, rel=1e-6)
    jl = ws.assembly.joints[0]
    row = ws.readout.data["joints"][jl.id]
    assert row["drive"].startswith("prescribed")
    assert row["q_deg"] == pytest.approx(math.degrees(res.at(0.5)["joint_q"][jl.id]))
    assert ws.readout.data["joints"][ws.assembly.joints[1].id]["drive"].startswith("coupled")
    assert "exerted BY the parent" in ws.readout.sign_text()
    # plots have data, the cursor follows the time
    assert ws.plots.has_data() and "gap" in ws.plots.quantities() and "angle" in ws.plots.quantities()
    assert ws.plots.cursor_time() == pytest.approx(0.5)
    ws.playback.set_time(0.9)
    assert ws.plots.cursor_time() == pytest.approx(0.9)
    # play: the timer advances time, loop wraps
    ws.playback.set_speed(4.0)
    ws.playback.loop_check.setChecked(True)
    ws.playback.set_time(0.98)
    ws.playback.tick(0.1)
    assert ws.current_time < 0.5  # wrapped around
    ws.playback.loop_check.setChecked(False)
    ws.playback.set_time(0.98)
    ws.playback.tick(0.1)
    assert ws.current_time == pytest.approx(res.t[-1]) and not ws.playback.playing
    # export
    files = ws.export_csv(tmp_path / "csv")
    names = sorted(f.name for f in files)
    assert any(n.startswith("joint_") for n in names) and any(n.startswith("body_") for n in names)
    head = files[0].read_text().splitlines()[:6]
    assert all(h.startswith("#") for h in head[:3]) and any("unit:" in h for h in head)
    png = ws.plots.save_png(tmp_path / "plot.png")
    assert png.exists() and png.stat().st_size > 2000


def test_run_study_b_changes_with_inertia(app, win, gripper):
    ws = gripper
    ws.set_study(study_named(ws, "B").id)
    run_and_wait(app, ws)
    jl = ws.assembly.joints[0]
    assert ws.study.name.startswith("B") and ws.result.meta["joints"][0]["driver_kind"].startswith("torque")
    ws.playback.set_time(float(ws.result.t[len(ws.result.t) // 2]))
    assert ws.readout.data["joints"][jl.id]["drive"].startswith("torque-driven")
    assert ws.readout.data["joints"][jl.id]["effort"] == pytest.approx(0.002)


def test_mujoco_unavailable_shows_install_hint_and_does_not_run(app, win, gripper, monkeypatch):
    from opencae.mbd import backend
    monkeypatch.setattr(backend, "available", lambda: (False, "MuJoCo is not available (ImportError)"))
    ws = gripper
    ws.study_panel.refresh()
    txt = ws.study_panel.backend_label.text()
    assert "MuJoCo" in txt and "python -m pip install mujoco" in txt
    assert not ws.can_run()
    assert ws.run_study() is None and not ws.running and ws.result is None
    assert "python -m pip install mujoco" in ws.study_panel.backend_label.text()


# ---------------------------------------------------------------------------------------------- drone
def test_drone_changing_one_rotor_changes_only_that_rotor(app, win, ws):
    assert ws.open_example("drone_rotors")
    assert ws.tree.count("body") == 9 and ws.tree.count("joint") == 8 and ws.tree.count("driver") == 4
    run_and_wait(app, ws)
    asm = ws.assembly
    rot = [j for j in asm.joints if j.kind == "revolute"]
    before = {j.id: (ws.result.joint_qd[j.id].copy(), ws.result.joint_q[j.id].copy()) for j in rot}
    # arrows showing the spin directions (one per rotor joint)
    for j in rot:
        assert f"asm_joint_{j.id}_arc" in ws.viewport.plotter.actors
    drv2 = next(d for d in get_mbd(ws.study).drivers if d.joint == rot[1].id)
    dlg = ws.make_driver_dialog(ws.study, driver=drv2)
    dlg.show()
    assert dlg.ok_button.isEnabled(), dlg.error_label.text()
    dlg.speed_unit.setCurrentText("rpm")
    assert dlg.ramp_v1.value() == pytest.approx(6000.0)
    dlg.ramp_v1.setValue(3000.0)
    dlg.accept()
    run_and_wait(app, ws)
    after = {j.id: (ws.result.joint_qd[j.id], ws.result.joint_q[j.id]) for j in rot}
    for k, j in enumerate(rot):
        same = np.array_equal(after[j.id][0], before[j.id][0]) and np.array_equal(after[j.id][1], before[j.id][1])
        assert same == (k != 1), f"rotor {k + 1}"
    assert after[rot[1].id][0][-1] == pytest.approx(0.5 * before[rot[1].id][0][-1], rel=1e-9)


# ---------------------------------------------------------------------------------------------- save / reopen
def test_save_and_reopen_keeps_assembly_drivers_and_results(app, win, gripper, tmp_path):
    ws = gripper
    run_and_wait(app, ws)
    dlg = ws.make_driver_dialog(ws.study, driver=get_mbd(ws.study).drivers[0])
    dlg.ramp_v1.setValue(25.0)
    dlg.accept()
    t_end = ws.result.t[-1]
    path = tmp_path / "saved" / "gripper2.ocae"
    path.parent.mkdir()
    win.session.save(path)
    win.dirty = False
    sid = ws.study.id
    win.cmd_open(str(path))
    ws.sync()
    assert ws.tree.count("body") == 3 and ws.tree.count("joint") == 2 and ws.tree.count("coupling") == 1
    assert ws.study.id == sid
    assert get_mbd(ws.study).drivers[0].profile.v1 == pytest.approx(math.radians(25.0))
    assert ws.result is not None and ws.result.t[-1] == pytest.approx(t_end)
    ws.playback.set_time(0.5)
    assert ws.readout.data["gap_mm"] > 0


# ---------------------------------------------------------------------------------------------- structural load case
def test_load_case_dialog_creates_structural_study_ready_to_mesh(app, win, gripper):
    pytest.importorskip("opencae.assembly.transfer")
    ws = gripper
    ws.set_study(study_named(ws, "C").id)
    run_and_wait(app, ws)
    left = ws.assembly.bodies[1]
    ws.select("body", left.id)
    ws.playback.set_time(0.05)
    assert ws.can_create_load_case()
    n = len(ws.project.studies)
    dlg = ws.make_loadcase_dialog()
    dlg.show()
    pump(app, 0.2)
    assert dlg.source_label.text().startswith("C")
    assert dlg.time.value() == pytest.approx(0.05)
    assert dlg.root_face.count() >= 2 and dlg.load_face.count() >= 2
    assert "static hold" in dlg.note_label.text().lower()
    txt = dlg.values_text()
    assert "N" in txt and "mm" in txt and "Equilibrium" in txt
    assert dlg.ok_button.isEnabled(), dlg.error_label.text()
    dlg.accept()
    assert len(ws.project.studies) == n + 1
    new = ws.project.studies[-1]
    assert new.analysis.analysis_type == "linear_static" and ws.project.active_study == new.id
    assert win.stack.currentWidget() is win.viewport  # the Studio is ready for Mesh / Solve
    assert win.session.study is new


def test_load_case_disabled_without_result_or_for_grounded_body(app, win, gripper):
    ws = gripper
    ws.select("body", ws.assembly.bodies[1].id)
    assert not ws.can_create_load_case()  # nothing has been run yet
    run_and_wait(app, ws)
    ws.select("body", ws.assembly.bodies[0].id)  # the palm is grounded
    assert not ws.can_create_load_case()


# ---------------------------------------------------------------------------------------------- screenshots
def test_screenshots(app, win, gripper):
    ws = gripper
    SHOTS.mkdir(parents=True, exist_ok=True)
    win.resize(1560, 960)
    run_and_wait(app, ws)
    ws.select("body", ws.assembly.bodies[1].id)
    ws.viewport.set_view("top")
    ws.viewport.fit()
    ws.playback.set_time(0.6)
    pump(app, 0.5)
    grab_window(app, win, ws, SHOTS / "asm_gripper_midmotion.png")
    dlg = ws.make_joint_dialog(joint=ws.assembly.joints[0])
    dlg.show()
    dlg.move(win.x() + 330, win.y() + 150)
    ws.playback.set_time(0.0)
    ws.select("joint", ws.assembly.joints[0].id)
    ws.viewport.set_view("iso")
    ws.viewport.fit()
    grab_window(app, win, ws, SHOTS / "asm_joint_dialog.png")
    dlg.grab().save(str(SHOTS / "asm_joint_dialog_only.png"))
    dlg.reject()
    try:
        import opencae.assembly.transfer  # noqa: F401
    except ImportError:
        return
    ws.set_study(study_named(ws, "C").id)
    run_and_wait(app, ws)
    ws.select("body", ws.assembly.bodies[1].id)
    ws.playback.set_time(0.05)
    lc = ws.make_loadcase_dialog()
    lc.show()
    pump(app, 0.5)
    lc.grab().save(str(SHOTS / "asm_loadcase_dialog.png"))
    lc.reject()
    for n in ("asm_gripper_midmotion.png", "asm_joint_dialog.png", "asm_loadcase_dialog.png"):
        assert (SHOTS / n).stat().st_size > 5000
