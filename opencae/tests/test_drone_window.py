"""Drone mode inside the main window (one app, several modes): ribbon page, stack pages, server lifecycle,
parameter sheet, new-project dialog. Offscreen."""
import json
import shutil
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

STL = """solid box
facet normal 0 0 1
 outer loop
  vertex 0 0 0
  vertex 10 0 0
  vertex 0 10 0
 endloop
endfacet
endsolid box
"""


@pytest.fixture(scope="module")
def app():
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication
    from opencae.ui.theme import apply_theme
    if QApplication.instance() is None:
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)  # as app/main.py does; enables QtWebEngine
    a = QApplication.instance() or QApplication([])
    apply_theme(a)
    return a


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QMessageBox
    from opencae.ui.main_window import MainWindow
    monkeypatch.setenv("OPENCAE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Discard))
    w = MainWindow(settings=QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat))
    w.show()
    yield w
    w.dirty = False
    w.close()


@pytest.fixture
def project(tmp_path):
    """A tmp project: a copy of the quad250 example when it exists, else a minimal drone.json."""
    src = Path(__file__).resolve().parents[1] / "examples" / "drones" / "quad250"
    dst = tmp_path / "proj"
    if src.exists():
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("results"))
    else:
        dst.mkdir()
        (dst / "a.stl").write_text(STL)
        (dst / "drone.json").write_text(json.dumps({
            "format": "opencae-drone/1", "name": "t", "type": "multirotor",
            "geometry": {"file": "a.stl", "units": "mm"},
            "propulsion": {"motor": {"kv": {"value": 2300, "unit": "rpm/V", "status": "datasheet", "source": "x"}}},
            "environment": {"rho": 1.225, "g": 9.81}}))
    return dst


def pump(app, sec=0.2):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents()
        time.sleep(0.01)


def get(url, path):
    with urllib.request.urlopen(url.rstrip("/") + path, timeout=10) as r:
        return r.status


def test_main_window_has_drone_page_and_studio_is_default(win):
    assert "Drone" in list(win.ribbon_bars)
    assert win.stack.currentWidget() is win.viewport and win.mode == "studio"
    names = [a.objectName() for a in win.ribbon_bars["Drone"].actions()]
    for n in ("drone_new", "drone_open", "drone_example_quad", "drone_example_k09l", "drone_run", "drone_stop",
              "drone_back"):
        assert n in names
    assert "k09l_monitor" in [a.objectName() for a in win.ribbon_bars["Home"].actions()]


def test_open_project_switches_to_workspace_and_server_answers(win, app, project):
    pytest.importorskip("opencae.drone.spec")
    assert win.open_drone_project(project)
    ws = win._drone
    assert win.stack.currentWidget() is ws and win.mode == "drone"
    assert win.ribbon.tabText(win.ribbon.currentIndex()) == "Drone"
    assert ws.server is not None and get(ws.url, "/api/project") == 200
    assert ws.sheet.topLevelItemCount() > 0
    url = ws.url
    # back to another ribbon page shows the viewport; the project stays open
    win.ribbon.setCurrentIndex(0)
    assert win.stack.currentWidget() is win.viewport
    assert ws.server is not None
    win.ribbon.setCurrentIndex(list(win.ribbon_bars).index("Drone"))
    assert win.stack.currentWidget() is ws
    win.cmd_drone_back()
    assert win.stack.currentWidget() is win.viewport
    ws.close_project()
    assert ws.server is None
    with pytest.raises(Exception):
        get(url, "/api/project")


def test_close_window_stops_server(win, app, project):
    pytest.importorskip("opencae.drone.spec")
    win.open_drone_project(project)
    ws, url = win._drone, win._drone.url
    assert url and get(url, "/api/project") == 200
    win.dirty = False
    win.close()
    assert ws.server is None
    with pytest.raises(Exception):
        get(url, "/api/project")


def test_not_a_drone_project_is_reported(win, tmp_path):
    win.enter_drone_mode()
    assert win._drone.open_project(tmp_path) is False
    assert win._drone.server is None and "No drone.json" in win._drone.msg.text()


def test_bad_value_is_red_and_file_unchanged(win, app, project):
    pytest.importorskip("opencae.drone.spec")
    win.open_drone_project(project)
    ws = win._drone
    before = (project / "drone.json").read_bytes()
    key = next(iter(ws.sheet._items))
    ws.sheet._items[key].setText(1, "abc")
    assert ws.save_parameters() is False
    assert (project / "drone.json").read_bytes() == before
    assert "not a number" in ws.msg.text() and "color" in ws.msg.styleSheet()


def test_edit_value_and_status_is_saved(win, app, project):
    spec = pytest.importorskip("opencae.drone.spec")
    win.open_drone_project(project)
    ws = win._drone
    key = "propulsion.motor.kv" if "propulsion.motor.kv" in ws.sheet._items else next(iter(ws.sheet._items))
    old = ws.sheet._orig[key][0]
    ws.sheet._items[key].setText(1, str(old + 1))
    ws.sheet._combos[key].setCurrentText("measured")
    assert ws.save_parameters() is True
    p = spec.param(spec.load(project), key)
    assert float(p.value) == float(old) + 1 and p.status == "measured"


def test_new_dialog_builds_project_without_exec(app, tmp_path):
    pytest.importorskip("opencae.drone.pipeline")
    from opencae.ui.drone.new_dialog import NewDroneDialog
    cad = tmp_path / "frame.stl"
    cad.write_text(STL)
    d = NewDroneDialog()
    d.set_values(cad, tmp_path / "newproj", "frame", "multirotor")
    out = d.create()
    assert out is not None and (out / "drone.json").exists()
    d.cad.setText(str(tmp_path / "missing.stl"))
    assert d.create() is None and d.message.text()


def test_new_project_opens_in_workspace_with_roles(win, app, tmp_path):
    pytest.importorskip("opencae.drone.pipeline")
    pytest.importorskip("opencae.drone.spec")
    from opencae.ui.drone.new_dialog import NewDroneDialog
    cad = tmp_path / "frame.stl"
    cad.write_text(STL)
    d = NewDroneDialog()
    d.set_values(cad, tmp_path / "np", "frame", "multirotor")
    proj = d.create()
    assert win.open_drone_project(proj, show_roles=True)
    assert win._drone.roles_text
    assert "part" in win._drone.msg.text()


def test_examples_are_copied_not_opened_in_place(win, tmp_path):
    pytest.importorskip("opencae.drone.spec")
    root = Path(__file__).resolve().parents[1] / "examples" / "drones" / "quad250"
    if not root.exists():
        pytest.skip("quad250 example not written yet")
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    win.cmd_drone_example("quad250")
    ws = win._drone
    assert ws.project_dir == tmp_path / "home" / "projects" / "quad250"
    assert not (ws.project_dir / "results").exists()
    assert (ws.project_dir / "drone.json").exists() and ws.server is not None
    assert before == sorted(str(p.relative_to(root)) for p in root.rglob("*"))  # package folder untouched


def test_run_progress_goes_to_status_bar(win, app, project, monkeypatch):
    pytest.importorskip("opencae.drone.spec")
    win.open_drone_project(project)
    ws = win._drone
    seen = []
    ws.progress.connect(lambda r, f, s: seen.append((r, f, s)))
    ws.server.jobs.progress.update({"running": True, "step": "mass", "frac": 0.5})
    ws._poll()
    assert win.drone_progress.value() == 500 and not win.drone_progress.isHidden()
    ws.server.jobs.progress["running"] = False
    ws._poll()
    assert win.drone_progress.isHidden()


def test_k09l_monitor_is_a_mode_not_a_window(win, app):
    from opencae.ui.platform.monitor import MonitorWindow
    win.cmd_k09l_monitor()
    m = win._k09l_monitor
    assert isinstance(m, MonitorWindow) and not m.isWindow()
    assert win.stack.currentWidget() is m and win.mode == "k09l_monitor"
    win.ribbon.setCurrentIndex(1)
    assert win.stack.currentWidget() is win.viewport
    win.cmd_k09l_platform()
    assert not win._k09l.isWindow() and win.stack.currentWidget() is win._k09l
    win.cmd_drone_back()
    assert win.stack.currentWidget() is win.viewport


def test_screenshot_drone_mode(win, app, project):
    pytest.importorskip("opencae.drone.spec")
    win.open_drone_project(project)
    pump(app, 1.0)
    out = Path("/tmp/claude-0/-home-claude/cca99fdc-b1aa-5a4c-a668-10c4bdf36bbb/scratchpad/fix_app.png")
    if out.parent.exists():
        win.resize(1400, 850)
        pump(app, 0.5)
        win.grab().save(str(out))


def test_run_through_server_jobs_writes_results(win, app, project):
    pytest.importorskip("opencae.drone.pipeline")
    if not (project / "airframe.stl").exists():
        pytest.skip("quad250 example not written yet")
    win.open_drone_project(project)
    ws = win._drone
    assert ws.run(["mass", "propulsion"])
    t = time.time()
    while ws.server.jobs.running and time.time() - t < 120:
        pump(app, 0.2)
    pump(app, 0.7)  # one more poll sees the end of the run
    assert not ws.server.jobs.progress["error"], ws.server.jobs.progress["error"]
    assert (project / "results" / "baseline" / "results.json").exists()
    assert win.drone_progress.isHidden()
    assert "Run finished" in ws.log.toPlainText()


def test_workflow_bar_hidden_outside_studio(win, app, project):
    pytest.importorskip("opencae.drone.spec")
    assert win.workflow_toolbar.isVisible()
    win.open_drone_project(project)
    assert not win.workflow_toolbar.isVisible()
    win.cmd_drone_back()
    assert win.workflow_toolbar.isVisible()
    win.cmd_k09l_monitor()
    assert not win.workflow_toolbar.isVisible()
    win.cmd_drone_back()
    assert win.workflow_toolbar.isVisible()


def test_parameter_sheet_labels_tooltips_and_width(win, app, project):
    pytest.importorskip("opencae.drone.spec")
    from opencae.ui.drone.workspace import friendly_label
    assert friendly_label("propulsion.motor.kv") == "Motor KV"
    assert friendly_label("propulsion.prop.diameter_in") == "Prop diameter"
    assert friendly_label("propulsion.battery.capacity_mah") == "Battery capacity"
    assert friendly_label("materials.carbon_plate.density_kg_m3") == "Carbon plate – density"
    win.resize(1400, 850)
    win.open_drone_project(project)
    pump(app, 0.3)
    sheet = win._drone.sheet
    it = sheet._items["propulsion.motor.kv"]
    assert it.text(0) == "Motor KV" and it.toolTip(0) == "propulsion.motor.kv"
    assert sheet.columnWidth(0) >= 220
    fm = sheet.fontMetrics()
    longest = max(fm.horizontalAdvance(i.text(0)) for i in sheet._items.values())
    assert sheet.columnWidth(0) >= longest  # nothing truncated
    assert sheet.viewport().width() >= sheet.columnWidth(0) + 150  # the other columns still fit
