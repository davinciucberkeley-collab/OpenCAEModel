"""Drone mode UX: start page, new-drone wizard, parameter sheet, run options, report export, web fallback. Offscreen."""
import json
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui
SHOTS = Path("/tmp/claude-0/-home-claude/cca99fdc-b1aa-5a4c-a668-10c4bdf36bbb/scratchpad")
QUAD = Path(__file__).resolve().parents[1] / "examples" / "drones" / "quad250"


def _solid(name, x):
    return (f"solid {name}\nfacet normal 0 0 1\n outer loop\n  vertex {x} 0 0\n  vertex {x + 10} 0 0\n"
            f"  vertex {x} 10 0\n endloop\nendfacet\nfacet normal 0 0 1\n outer loop\n  vertex {x + 10} 10 0\n"
            f"  vertex {x + 10} 0 0\n  vertex {x} 10 0\n endloop\nendfacet\nendsolid {name}\n")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication
    from opencae.ui.theme import apply_theme
    if QApplication.instance() is None:
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    a = QApplication.instance() or QApplication([])
    apply_theme(a)
    return a


@pytest.fixture
def project(tmp_path):
    dst = tmp_path / "proj"
    shutil.copytree(QUAD, dst, ignore=shutil.ignore_patterns("results"))
    return dst


@pytest.fixture
def ws(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from opencae.ui.drone.workspace import DroneWorkspace
    monkeypatch.setenv("OPENCAE_HOME", str(tmp_path / "home"))
    w = DroneWorkspace(start_server=False)
    w.settings = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    w.resize(1400, 850)
    w.show()
    yield w
    w.close()


def shot(widget, name):
    if SHOTS.exists():
        widget.grab().save(str(SHOTS / name))


def kv(ws):
    return ws.sheet._items["propulsion.motor.kv"]


def test_start_page_cards_and_recent(ws, project):
    assert ws.pages.currentWidget() is ws.start
    assert set(ws.start.cards) == {"new", "open", "quad", "k09l"}
    assert "New drone from CAD" in ws.start.cards["new"].title_label.text()
    assert ws.start.recent_list.count() == 0
    shot(ws, "ux_start_empty.png")
    assert ws.open_project(project)
    ws.close_project()
    assert ws.pages.currentWidget() is ws.start
    assert ws.start.recent_list.count() == 1
    assert ws.recent_projects()[0]["path"] == str(project)
    shot(ws, "ux_start.png")
    opened = []
    ws.open_project = lambda p, **k: opened.append(str(p)) or True
    ws.start.recent_list.itemClicked.emit(ws.start.recent_list.item(0))
    assert opened == [str(project)]
    ws.forget_recent(project)
    assert ws.start.recent_list.count() == 0


def test_recent_list_keeps_eight(ws, tmp_path):
    for i in range(10):
        ws.remember_recent(tmp_path / f"p{i}")
    r = ws.recent_projects()
    assert len(r) == 8 and r[0]["path"].endswith("p9")


def test_drop_cad_starts_wizard_and_folder_opens(ws, project, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(type(ws), "new_project", lambda self, cad="": seen.setdefault("cad", cad))
    cad = tmp_path / "x.stl"
    cad.write_text(_solid("a", 0))
    ws.handle_drop(cad)
    assert seen["cad"] == str(cad)
    ws.handle_drop(project)
    assert ws.project_dir == project


def test_wizard_creates_project_with_role_and_parameter(app, tmp_path, monkeypatch):
    pytest.importorskip("opencae.drone.pipeline")
    from opencae.drone import spec as S
    from opencae.ui.drone.new_dialog import NewDroneDialog
    cad = tmp_path / "f.stl"
    cad.write_text(_solid("frame_main", 0) + _solid("gizmo", 20))
    d = NewDroneDialog()
    d.set_values(cad, tmp_path / "np", "demo", "multirotor")
    assert d.check_cad() == ""
    d.show()
    shot(d, "ux_wizard_p1.png")
    d.next()
    assert d.currentId() == 1
    assert d.roles_table.rowCount() == 2
    assert "2 part(s)" in d.roles_note.text()
    assert d.set_role("gizmo", "payload")
    d.set_param("propulsion.motor.kv", 2300)
    d.set_param("propulsion.battery.cells", 4)
    d.set_material("Carbon fibre plate")
    shot(d, "ux_wizard_p2.png")
    d.next()
    shot(d, "ux_wizard_p3.png")
    out = d.create()
    assert out is not None, d.message.text()
    spec = S.load(out)
    assert S.val(spec, "propulsion.motor.kv") == 2300
    assert S.val(spec, "propulsion.battery.cells") == 4
    assert S.param(spec, "propulsion.motor.kv").status == "estimate"
    assert S.val(spec, "propulsion.prop.diameter_in") is None       # "I don't know yet"
    assert "gizmo" in spec["roles"]["payload"][0]
    assert S.val(spec, "materials.carbon_plate.density_kg_m3") == 1600


def test_wizard_rejects_bad_parameter_before_creating(app, tmp_path):
    pytest.importorskip("opencae.drone.pipeline")
    from opencae.ui.drone.new_dialog import NewDroneDialog
    cad = tmp_path / "f.stl"
    cad.write_text(_solid("frame_main", 0))
    d = NewDroneDialog()
    d.set_values(cad, tmp_path / "np2", "demo", "multirotor")
    d.set_param("propulsion.motor.kv", -5)
    assert d.create() is None and "greater than 0" in d.param_message.text()
    assert not (tmp_path / "np2").exists()


def test_filter_hides_rows(ws, project):
    ws.open_project(project)
    sheet = ws.sheet
    ws.search.setText("battery")
    vis = [k for k, it in sheet._items.items() if not it.isHidden()]
    assert vis and all("battery" in k or "battery" in sheet._items[k].text(0).lower() or
                       "battery" in k for k in vis) or vis
    assert kv(ws).isHidden() and not sheet._items["propulsion.battery.capacity_mah"].isHidden()
    ws.search.setText("zzzzqq")
    assert all(it.isHidden() for it in sheet._items.values()) and "No parameter" in ws.msg.text()
    shot_ws = ws
    ws.search.setText("battery")
    shot(shot_ws, "ux_sheet_filter.png")
    ws.search.setText("")
    assert not kv(ws).isHidden()


def test_negative_kv_rejected_and_file_unchanged(ws, project):
    ws.open_project(project)
    before = (project / "drone.json").read_bytes()
    kv(ws).setText(1, "-100")
    assert "propulsion.motor.kv" in ws.sheet.errors()
    assert "greater than 0" in ws.msg.text()
    assert ws.save_parameters() is False
    assert (project / "drone.json").read_bytes() == before
    kv(ws).setText(1, "0")
    assert ws.save_parameters() is False
    kv(ws).setText(1, "2400.5")
    assert not ws.sheet.errors()


def test_undo_redo_and_save(ws, project):
    from opencae.drone import spec as S
    ws.open_project(project)
    old = kv(ws).text(1)
    kv(ws).setText(1, "3000")
    assert ws.sheet.is_dirty() and ws.save_btn.isEnabled() and ws.revert_btn.isEnabled()
    assert "1 unsaved" in ws.change_label.text()
    ws.sheet.undo_stack.undo()
    assert kv(ws).text(1) == old and not ws.sheet.is_dirty()
    ws.sheet.undo_stack.redo()
    assert kv(ws).text(1) == "3000"
    assert ws.save_parameters() is True
    assert S.val(S.load(project), "propulsion.motor.kv") == 3000
    assert not ws.sheet.is_dirty()
    kv(ws).setText(1, "100")
    ws.revert_parameters()
    assert kv(ws).text(1) == "3000"


def test_reset_row_and_reload(ws, project):
    from opencae.drone import spec as S
    ws.open_project(project)
    ws.sheet._items["environment.rho"].setText(1, "9")
    ws.sheet.reset_row("environment.rho")
    assert ws.sheet._items["environment.rho"].text(1) == "1.225"
    spec = S.load(project)
    spec["propulsion"]["motor"]["kv"]["value"] = 1234
    S.save(spec, project)
    ws.reload()
    assert kv(ws).text(1) == "1234"


def test_run_options_only_and_overrides(app, project):
    from opencae.drone import spec as S
    from opencae.ui.drone.ux import RunOptionsDialog
    d = RunOptionsDialog(S.load(project))
    o = d.options()
    assert o["only"] is None and o["overrides"] == {} and not o["force"]
    d.checks["cfd"].setChecked(False)
    d.checks["flight"].setChecked(False)
    d.preset.setCurrentIndex(1)
    assert d.options()["overrides"] == {}                           # CFD off: nothing to override
    d.checks["cfd"].setChecked(True)
    d.iterations.setValue(450)
    d.force.setChecked(True)
    d.keep.setChecked(True)
    o = d.options()
    assert o["only"] == ["mass", "propulsion", "structure", "cfd"]
    assert o["overrides"] == {"analyses.cfd.preset": "medium", "analyses.cfd.iterations": 450}
    assert o["force"] and o["keep_cases"] and not o["remember"]
    for c in d.checks.values():
        c.setChecked(False)
    assert not d.run_btn.isEnabled()
    for c in d.checks.values():
        c.setChecked(True)
    shot(d, "ux_run_options.png")


def test_run_with_remember_writes_file_else_not(ws, project, monkeypatch):
    from opencae.drone import spec as S
    ws.open_project(project)
    calls = []
    monkeypatch.setattr(type(ws), "run", lambda self, only=None, overrides=None, force=False, keep_cases=False:
                        calls.append((only, overrides, force, keep_cases)) or True)
    ws.run_with({"only": ["mass"], "overrides": {"analyses.cfd.preset": "fine"}, "force": True,
                 "keep_cases": False, "remember": False})
    assert calls[-1] == (["mass"], {"analyses.cfd.preset": "fine"}, True, False)
    assert S.val(S.load(project), "analyses.cfd.preset") != "fine"
    ws.run_with({"only": None, "overrides": {"analyses.cfd.preset": "fine"}, "force": False,
                 "keep_cases": False, "remember": True})
    assert calls[-1][1] is None
    assert S.val(S.load(project), "analyses.cfd.preset") == "fine"


def _results(project):
    d = project / "results" / "baseline"
    d.mkdir(parents=True)
    res = {"project": "quad250", "variant": "baseline", "verdict": "conditional", "generated": "2026-11-23",
           "analyses": {"mass": {"id": "mass", "title": "Mass <& balance>", "status": "ok",
                                 "summary": "Total 0.4 kg.", "missing": [],
                                 "metrics": {"mass_kg": {"value": 0.4, "unit": "kg", "basis": "estimate",
                                                         "label": "Total mass"}}}},
           "checks": [{"id": "R-TW", "label": "Thrust at least 2x weight", "metric": "propulsion.tw_ratio",
                       "op": ">=", "target": 2.0, "value": 4.1, "status": "conditional", "why": "estimates only"}]}
    (d / "results.json").write_text(json.dumps(res))


def test_export_report_summary_and_folder(ws, project, tmp_path):
    ws.open_project(project)
    assert ws.export_report(tmp_path / "r.html") is None and "no results" in ws.msg.text().lower()
    _results(project)
    out = ws.export_report(tmp_path / "r.html")
    html = out.read_text(encoding="utf-8")
    assert "conditional" in html and "Simulation results plan the tests; they are not flight approval." in html
    assert "Mass &lt;&amp; balance&gt;" in html and "Thrust at least 2x weight" in html and "<script" not in html
    assert "conditional" in ws.copy_summary()
    from PySide6.QtWidgets import QApplication
    assert "not flight approval" in QApplication.clipboard().text()
    ws.start.set_recent([])
    ws.refresh_start_page()


def test_web_view_not_allowed_gives_fallback(app, monkeypatch, tmp_path):
    from opencae.utilities import crash
    from opencae.ui.drone.workspace import DroneWorkspace
    monkeypatch.setattr(crash, "web_view_allowed", lambda: False)
    w = DroneWorkspace(start_server=False)
    try:
        assert w.web is None and w.browser_btn.text() == "Open dashboard in browser"
        assert w.fallback_label.text()
    finally:
        w.close()


def test_render_process_terminated_swaps_view(app, monkeypatch):
    from opencae.ui.drone.workspace import DroneWorkspace, web_allowed
    from opencae.ui.drone import workspace as W
    if not (W.HAVE_WEB and web_allowed() and W.web_available()):
        pytest.skip("no web view in this session")
    w = DroneWorkspace(start_server=False)
    try:
        assert w.web is not None
        w._swap_to_fallback("stopped")
        assert w.web is None and w.browser_btn
    finally:
        w.close()


def test_esc_asks_before_stopping_and_shortcuts(ws, project, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    ws.open_project(project)
    assert ws.request_stop() is False          # nothing runs
    ws.server = type("S", (), {"jobs": type("J", (), {"running": True})(), "stop_job": lambda self: stopped.append(1)})()
    stopped = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    assert ws.request_stop() is False and not stopped
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    assert ws.request_stop() is True and stopped
    assert {"F5", "Shift+F5", "Esc", "Ctrl+N", "Ctrl+O", "Ctrl+F", "Ctrl+S"} <= set(ws.shortcuts)
    ws.server = None


def test_install_adds_buttons_to_main_window(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from opencae.ui.main_window import MainWindow
    monkeypatch.setenv("OPENCAE_HOME", str(tmp_path / "home"))
    w = MainWindow(settings=QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat))
    try:
        names = [a.objectName() for a in w.ribbon_bars["Drone"].actions()]
        for n in ("drone_run_options", "drone_report", "drone_folder", "drone_shortcuts"):
            assert n in names
        assert names.index("drone_run_options") == names.index("drone_run") + 1
        w.enter_drone_mode()
        assert w.A["new"].shortcut().isEmpty()      # handed over to the Drone page
        w.cmd_drone_back()
        assert w.A["new"].shortcut().toString() == "Ctrl+N"
    finally:
        w.dirty = False
        w.close()
