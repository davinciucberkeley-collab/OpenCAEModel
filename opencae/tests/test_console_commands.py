"""Natural console commands: grammar (Qt-free) and the right-hand Command Console on a real main window. Offscreen.

The grammar tests need no display. The window tests copy the bundled gripper example into a temporary OPENCAE_HOME,
run the real CalculiX / MuJoCo services (mark ``solver``) and check that the console, the ribbon and the Progress
Monitor share one selection, one project and one job registry.
"""
import os
import time
from pathlib import Path

import pytest

from opencae.ui.commands import grammar as G

# screenshots are written only when OPENCAE_TEST_SHOTS names a folder (never to a fixed path)
SHOTS_DIR = os.environ.get("OPENCAE_TEST_SHOTS", "")
E = G.Entity


# ------------------------------------------------------------------------------------------------ grammar (no Qt)
def ctx():
    comps = [E("Palm", "component", "g_palm"), E("Left finger", "component", "g_left"),
             E("Right finger", "component", "g_right"), E("Motor housing 1", "component", "m1"),
             E("Motor housing 2", "component", "m2"), E("Motor housing 3", "component", "m3")]
    params = [E("finger thickness", "parameter", "group:finger:thickness", {"unit": "mm", "value": 4.0}),
              E("Left finger thickness", "parameter", "f1:dx", {"unit": "mm", "value": 4.0}),
              E("Right finger thickness", "parameter", "f2:dx", {"unit": "mm", "value": 4.0}),
              E("Left finger length", "parameter", "f1:dy", {"unit": "mm", "value": 60.0})]
    return G.Context(components=comps, parameters=params,
                     studies=[E("A - prescribed closing", "study", "s1")],
                     projects=[E("gripper", "example", "gripper"), E("drone rotors", "example", "drone_rotors")],
                     project_name="Two-finger gripper")


@pytest.mark.parametrize("line,verb,fields", [
    ("open gripper", "open", {"entity": "gripper"}),
    ("select left finger", "select", {"entity": "Left finger"}),
    ("run structural on left finger", "run", {"analysis": "structural", "entity": "Left finger"}),
    ("run thermal on motor housing 2", "run", {"analysis": "thermal", "entity": "Motor housing 2"}),
    ("run fluid around assembly", "run", {"analysis": "fluid", "assembly": True}),
    ("run motion on gripper", "run", {"analysis": "motion", "assembly": True}),
    ("run modal for the left finger", "run", {"analysis": "modal", "entity": "Left finger"}),
    ("run stress on left finger", "run", {"analysis": "structural", "entity": "Left finger"}),
    ("show displacement", "show", {"what": "displacement"}),
    ("show von mises", "show", {"what": "stress"}),
    ("open dashboard", "dashboard", {}),
    ("stop", "stop", {}),
    ("cancel job #3", "stop", {"job": 3}),
    ("retry", "retry", {}),
    ("status", "status", {}),
    ("set mesh coarse", "set", {"what": "mesh", "level": "coarse"}),
    ("help", "help", {}),
    ("clear", "clear", {}),
    ("history", "history", {}),
])
def test_example_commands_parse_to_the_right_intent(line, verb, fields):
    it = G.parse(line, ctx())
    assert it.verb == verb, (it.verb, it.message)
    for k, v in fields.items():
        got = getattr(it, k)
        got = got.name if isinstance(got, G.Entity) else got
        assert got == v, (k, got)
    assert it.ok or verb in ("stop",), it.message


def test_iterate_range_with_units_and_steps():
    it = G.parse("iterate finger thickness from 2 mm to 5 mm in 10 steps", ctx())
    assert it.verb == "iterate" and it.ok
    assert it.param.name == "finger thickness"          # the exact group parameter wins over "Left finger thickness"
    assert (it.start.value, it.stop.value, it.unit, it.n) == (2.0, 5.0, "mm", 10)
    assert it.values[0] == 2.0 and it.values[-1] == 5.0 and len(it.values) == 10
    assert it.values[1] == pytest.approx(2.0 + 3.0 / 9.0)


def test_iterate_variants_by_step_and_unit_conversion():
    it = G.parse("iterate finger thickness from 2 to 5 mm by 1 mm", ctx())
    assert it.values == [2.0, 3.0, 4.0, 5.0] and it.unit == "mm"
    it = G.parse("iterate finger thickness from 0.002 m to 0.005 m in 4 steps", ctx())
    assert it.unit == "mm" and it.values == pytest.approx([2.0, 3.0, 4.0, 5.0])   # converted to the parameter's unit
    it = G.parse("iterate finger thickness from 2 mm to 5 mm in 3 steps", ctx())
    assert it.values == [2.0, 3.5, 5.0]
    it = G.parse("iterate finger thickness 2 to 5 mm in 3 steps", ctx())          # "from" may be left out
    assert it.ok and it.values == [2.0, 3.5, 5.0]


def test_iterate_asks_for_what_is_missing():
    it = G.parse("iterate finger thickness", ctx())
    assert "range" in it.missing
    it = G.parse("iterate finger thickness from 2 mm to 5 mm", ctx())
    assert "steps" in it.missing and "How many steps" in it.message
    it = G.parse("iterate banana from 1 to 2 in 3 steps", ctx())
    assert "parameter" in it.missing and "cannot find a parameter" in it.message


def test_units_parse_and_convert():
    q = G.parse_quantity("2 mm")
    assert (q.value, q.unit, q.dim) == (2.0, "mm", "length") and q.si == pytest.approx(0.002)
    assert G.parse_quantity("30deg").to("rad") == pytest.approx(0.5235987756)
    assert G.parse_quantity("6000 rpm").si == pytest.approx(628.3185307)
    assert G.parse_quantity("5 N·m").unit == "nm" and G.parse_quantity("5 N*m").dim == "torque"
    assert G.parse_quantity("10 N").dim == "force" and G.parse_quantity("0.5 s").unit == "s"
    assert G.parse_quantity("2 millimeters").unit == "mm"
    assert G.parse_quantity("0.004 m").to("mm") == pytest.approx(4.0)
    assert G.parse_quantity("abc") is None and G.parse_quantity("5 furlongs") is None
    assert G.parse_quantity("7").unit == ""
    with pytest.raises(ValueError):
        G.parse_quantity("2 mm").to("deg")


def test_fuzzy_names_and_ambiguity():
    c = ctx()
    it = G.parse("select left fingr", c)                  # a typo still finds the part, and says so
    assert it.entity.name == "Left finger" and "Using 'Left finger'" in it.notes[0]
    it = G.parse("select LEFT   Finger", c)
    assert it.entity.name == "Left finger" and not it.notes
    it = G.parse("select finger", c)                       # two equally good matches: ask, do not guess
    assert it.entity is None and {e.name for e in it.candidates} == {"Left finger", "Right finger"}
    assert "target" in it.missing and "select Left finger" in it.suggestions
    it = G.parse("run thermal on motor housing", c)
    assert len(it.candidates) == 3 and it.suggestions[0] == "run thermal on Motor housing 1"
    it = G.parse("select banana", c)
    assert it.message.startswith("I cannot find 'banana'")
    it = G.parse("select pam", c)                          # near miss of 'Palm'
    assert it.entity is not None and it.entity.name == "Palm" or "Palm" in " ".join(it.suggestions)


def test_unknown_words_give_did_you_mean_lines():
    it = G.parse("runn structural on left finger", ctx())
    assert it.verb == "unknown" and "Did you mean" in it.message
    assert "run structural on left finger" in it.suggestions
    it = G.parse("left finger", ctx())
    assert it.verb == "unknown" and it.suggestions[0] == "select Left finger"
    it = G.parse("run", ctx())
    assert "analysis" in it.missing and len(it.suggestions) == 5
    it = G.parse("run thermall on left finger", ctx())
    assert it.analysis == "" and "thermal" in it.message


def test_cli_style_commands_stay_legacy():
    c = ctx()
    for line in ("run mass propulsion", "run --force", "iterate big_battery propulsion.battery.capacity_mah=2200",
                 "set propulsion.battery.cells 4", "results propulsion", "example quad250", "params battery",
                 "open C:\\drones\\my_quad", "open /tmp/some/dir", "doctor", "mode drone"):
        assert G.parse(line, c).verb == "legacy", line
    d = G.Context(mode="drone", drone=True)
    assert G.parse("run", d).verb == "legacy" and G.parse("run cfd", d).verb == "legacy"
    assert G.parse("run cfd", ctx()).analysis == "fluid"   # without a drone project it is the fluid analysis


def test_range_values():
    assert G.range_values(2, 5, n=4) == [2, 3, 4, 5]
    assert G.range_values(5, 2, step=1) == [5, 4, 3, 2]
    assert G.range_values(0, 1, n=1) == [0]
    assert G.range_values(0, 1, step=0.25) == [0, 0.25, 0.5, 0.75, 1.0]


def test_completion_is_fuzzy_and_context_aware():
    c = ctx()
    assert "run " in G.complete("ru", c)
    assert any(x.startswith("run structural") for x in G.complete("run str", c))
    assert G.complete("run structural ", c) == ["run structural on "]
    assert "run structural on Left finger" in G.complete("run structural on le", c)
    assert "run motion on assembly" in G.complete("run motion on as", c)
    assert "select Left finger" in G.complete("select lft", c)     # fuzzy, not only prefix
    assert any("finger thickness" in x for x in G.complete("iterate fing", c))
    assert G.complete("iterate finger thickness ", c) == ["iterate finger thickness from "]
    assert any(x.startswith("show displacement") for x in G.complete("show dis", c))
    assert G.complete("", c) == [] and G.complete(">>> 1+", c) == []


# ------------------------------------------------------------------------------------------------ window tests
pytestmark_gui = pytest.mark.gui


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


def make_win(tmp_path, monkeypatch, name="s.ini"):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QMessageBox
    from opencae.ui.main_window import MainWindow
    monkeypatch.setenv("OPENCAE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Discard))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    w = MainWindow(settings=QSettings(str(tmp_path / name), QSettings.IniFormat))
    w.resize(1560, 960)
    w.show()
    return w


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    w = make_win(tmp_path, monkeypatch)
    pump(app, 0.4)
    yield w
    w.dirty = False
    w.close()


@pytest.fixture
def con(win):
    return win.extensions["opencae.ui.console"]


def pump(app, sec=0.3):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents()
        time.sleep(0.01)


def wait_for(app, cond, timeout=180):
    t = time.time()
    while not cond() and time.time() - t < timeout:
        app.processEvents()
        time.sleep(0.03)
    assert cond(), "timed out"


def reg_of(w):
    return w.extensions["opencae.ui.jobs"].reg


def idle(w):
    reg = reg_of(w)
    return lambda: not reg.active() and w.proc is None


def shot(win, name):
    if SHOTS_DIR:
        Path(SHOTS_DIR).mkdir(parents=True, exist_ok=True)
        win.grab().save(str(Path(SHOTS_DIR) / f"console2_{name}.png"))


def submit_defaults(con):
    """Press Start on the inline set-up form with its default choices."""
    assert con.pending is not None and con.pending.kind == "form"
    con.submit_form()


# ---- layout
@pytest.mark.gui
def test_console_dock_is_on_the_right_and_nothing_else_is_hidden(app, win, con):
    from PySide6.QtCore import Qt
    ext = win.extensions["opencae.ui.jobs"]
    pump(app)
    assert win.dockWidgetArea(con.dock) == Qt.RightDockWidgetArea
    assert con.dock.windowTitle() == "Command Console"
    for d in (con.dock, ext.dock, win.tree_dock, win.props_dock):
        assert d.isVisible(), d.windowTitle()
    assert win.dockWidgetArea(ext.dock) == win.dockWidgetArea(win.tree_dock) == Qt.LeftDockWidgetArea
    assert not con.dock.geometry().intersects(win.props_dock.geometry())    # stacked: both reachable
    assert not ext.dock.geometry().intersects(win.tree_dock.geometry())
    assert con.dock.geometry().left() > win.viewport.geometry().left()
    assert con.dock.width() >= 280 and con.dock.height() > 200 and win.viewport.isVisible()
    from PySide6.QtWidgets import QDockWidget
    assert con.dock.features() & QDockWidget.DockWidgetClosable and con.dock.features() & QDockWidget.DockWidgetMovable


@pytest.mark.gui
def test_view_menu_restores_the_console_and_collapse_returns_space(app, win, con):
    view = next(a.menu() for a in win.menuBar().actions() if a.text().replace("&", "") == "View")
    entry = next(a for a in view.actions() if a.text() == "Command Console")
    assert entry.isCheckable() and entry.isChecked()
    before = win.props_dock.height()
    entry.trigger()
    pump(app, 0.3)
    assert not con.dock.isVisible() and not entry.isChecked()
    assert win.props_dock.height() > before + 100                  # Properties takes the room back
    narrow = win.viewport.width()
    win.props_dock.hide()                                          # with the whole right column collapsed ...
    win.results_dock.hide()
    pump(app, 0.4)
    assert win.viewport.width() > narrow + 200                     # ... the viewport gets all of the width
    win.results_dock.show()
    win.props_dock.show()
    entry.trigger()
    pump(app, 0.3)
    assert con.dock.isVisible() and entry.isChecked()
    con.dock.hide()
    con.toggle_dock()                                  # Ctrl+` / F12 path
    assert con.dock.isVisible()
    assert any(a.shortcut().toString() == "Ctrl+K" for a in win.actions())


@pytest.mark.gui
def test_layout_survives_closing_and_reopening(app, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    w = make_win(tmp_path, monkeypatch, "keep.ini")
    pump(app, 0.3)
    c = w.extensions["opencae.ui.console"]
    c.dock.hide()
    w.extensions["opencae.ui.jobs"].save_layout()
    w.dirty = False
    w.close()
    w2 = make_win(tmp_path, monkeypatch, "keep.ini")
    pump(app, 0.4)
    c2 = w2.extensions["opencae.ui.console"]
    assert not c2.dock.isVisible() and w2.dockWidgetArea(c2.dock) == Qt.RightDockWidgetArea
    assert w2.extensions["opencae.ui.jobs"].dock.isVisible() and w2.tree_dock.isVisible()
    c2.view_action.trigger()
    pump(app, 0.3)
    assert c2.dock.isVisible()
    w2.dirty = False
    w2.close()


# ---- open / select share the GUI's state
@pytest.mark.gui
def test_open_gripper_then_select_left_finger_is_the_same_selection_as_a_pick(app, win, con):
    from opencae.ui.viewport import Pick
    con.execute("open gripper")
    pump(app, 0.5)
    assert "Opened gripper" in con.text()
    names = {b.name: gid for gid, b in win.session.bodies.items()}
    assert {"Palm", "Left finger", "Right finger"} <= set(names)
    con.execute("select left finger")
    keys = [p.key() for p in win.selection]
    assert keys == [("body", names["Left finger"], -1)]
    reference = Pick("body", names["Left finger"])                 # what the viewport hands to on_pick
    assert win.selection[0].key() == reference.key()
    item = win.tree.currentItem()
    assert item is not None and "Left finger" in item.text(0)       # the model tree followed
    assert "Selected Left finger" in con.text()
    assert "run structural on Left finger" in con.chips.labels()
    con.execute("select finger")                                   # ambiguous: asks, selection untouched
    assert con.pending is not None and con.pending.kind == "choice"
    assert {lb for lb, _ in con.pending.choices} == {"Left finger", "Right finger"}
    con.execute("right finger")                                    # answered by typing the choice
    assert [p.body for p in win.selection] == [names["Right finger"]] and con.pending is None
    con.execute("select nothing")
    assert win.selection == []


# ---- clarification
@pytest.mark.gui
def test_run_without_selection_asks_which_component_with_choices(app, win, con):
    con.execute("open gripper")
    pump(app, 0.4)
    con.execute("run structural")
    assert con.pending is not None and con.pending.kind == "choice" and con.card.isVisible()
    labels = {lb for lb, _ in con.pending.choices}
    assert {"Left finger", "Right finger", "Palm", "Whole assembly"} <= labels
    assert not reg_of(win).jobs()                                  # nothing started before the answer
    shot(win, "question_component")
    con.cancel_pending()
    assert con.pending is None and not con.card.isVisible() and "Cancelled" in con.text()
    con.execute("run")                                             # which analysis? as chips
    assert con.pending is not None and {"Static structural", "Motion"} <= {lb for lb, _ in con.pending.choices}
    con.execute("cancel")                                          # typing cancel works too
    assert con.pending is None
    con.execute("select left finger")
    con.execute("run structural")                                  # now the selection is used
    assert con.pending is not None and con.pending.kind == "form"


@pytest.mark.gui
def test_missing_load_is_asked_in_an_inline_form_and_nothing_runs_before_start(app, win, con):
    con.execute("open gripper")
    pump(app, 0.4)
    con.execute("run structural on left finger")
    assert con.pending is not None and con.pending.kind == "form" and con.card.isVisible()
    form = con.card.form
    assert {"fixed", "load_face", "force", "direction"} <= set(form.editors)
    assert form.values()["force"] == 10.0 and form.values()["fixed"] != form.values()["load_face"]
    assert reg_of(win).active() == [] and win.proc is None                      # still nothing running
    study = win.session.study
    assert study.name == "Structural: Left finger" and study.analysis.analysis_type == "linear_static"
    assert study.constraints == [] and study.loads == []                        # nothing was assumed
    shot(win, "question_setup")
    con.cancel_pending()
    assert reg_of(win).jobs() == []
    con.execute("run structural on left finger 25 N")                          # the typed force is the default
    assert con.card.form.values()["force"] == 25.0


# ---- the run itself
@pytest.mark.gui
@pytest.mark.solver
def test_console_run_is_the_same_service_job_as_the_run_button(app, win, con):
    reg = reg_of(win)
    con.execute("open gripper")
    pump(app, 0.4)
    con.execute("select left finger")
    con.execute("run structural on left finger")
    submit_defaults(con)
    pump(app, 0.3)
    assert con.pending is None and win.proc is not None
    mesh_job = reg.jobs()[0]
    assert mesh_job.kind == "mesh" and mesh_job.source == "console" and mesh_job.state == "running"
    n = len(reg.jobs())
    con.execute("run structural on left finger")                               # identical command: no second launch
    assert len(reg.jobs()) == n
    assert f"Already running — see job #{mesh_job.seq}" in con.text()
    assert 'href="job:' in con.output.toHtml() or "job:" in con.output.toHtml()
    shot(win, "midrun")
    wait_for(app, idle(win))
    wait_for(app, lambda: "max displacement" in con.text(), 30)
    jobs = reg.jobs()
    solve = next(j for j in jobs if j.kind == "structural")
    assert solve.state == "done", solve.error
    assert solve.source == "console" and solve.outputs["opener"] == "studio.results"
    assert [s.name for s in solve.stages] == ["Meshing", "Solver", "Postprocessing", "Results ready"]
    assert win.view_mode == "results" and win.session.results is not None      # the existing results viewer
    assert "max displacement" in con.text() and f"job #{solve.seq}" in con.text()
    assert {"show displacement", "show stress", "open results"} <= set(con.chips.labels())
    # the ribbon's Solve button goes through the same handler and makes the same kind of job with the same stages
    win.A["solve"].trigger()
    wait_for(app, lambda: win.proc is None and not reg.active())
    pump(app, 0.3)
    again = reg.jobs()[0]
    assert again.id != solve.id and again.kind == solve.kind and again.source == "gui"
    assert [s.name for s in again.stages] == [s.name for s in solve.stages]
    shot(win, "done")
    # show displacement / stress switch the existing results display
    win.set_view_mode("geometry")
    con.execute("show displacement")
    assert win.view_mode == "results" and win._current_result == "U_mag"
    con.execute("show stress")
    assert win._current_result == "S_vm"
    con.execute("show temperature")
    assert "no temperature result" in con.text()
    win.set_view_mode("geometry")
    con.execute("open results")
    assert win.view_mode == "results"


@pytest.mark.gui
def test_cannot_run_is_said_in_one_sentence_with_a_fix(app, win, con, monkeypatch):
    con.execute("open gripper")
    pump(app, 0.4)
    from opencae.solver.base import CalculixBackend
    monkeypatch.setattr(CalculixBackend, "executable", lambda self: None)
    con.execute("select left finger")
    con.execute("run modal on left finger")
    assert reg_of(win).jobs() == [] and win.proc is None
    assert "is not installed, so this cannot run" in con.text() and "doctor" in con.chips.labels()
    con.execute("run banana")
    assert "I do not know the analysis 'banana'" in con.text()
    con.execute("run structural on banana")
    assert "cannot find 'banana'" in con.text() and con.pending is None
    con.execute("show displacement")
    assert "no results" in con.text().lower()
    con.execute("stop")
    assert "Nothing is running" in con.text()


@pytest.mark.gui
def test_motion_run_through_the_monitor_with_open_results(app, win, con):
    from opencae.mbd import backend
    ok, why = backend.available()
    if not ok:
        pytest.skip(f"MuJoCo not available: {why}")
    reg = reg_of(win)
    con.execute("open gripper")
    pump(app, 0.5)
    con.execute("run motion on gripper")
    pump(app, 0.2)
    job = reg.jobs()[0]
    assert job.kind == "motion" and job.source == "console"
    con.execute("run motion on gripper")                                        # no duplicate while it runs
    if job.active:
        assert [j.kind for j in reg.jobs()].count("motion") == 1 and "Already running" in con.text()
    wait_for(app, lambda: job.state in ("done", "failed"), 120)
    pump(app, 0.3)
    assert job.state == "done", job.error
    assert job.outputs["opener"] == "mbd.playback" and reg.can_open(job)
    assert "open results" in con.chips.labels() and f"job #{job.seq}" in con.text()
    win.cmd_drone_back()
    con.execute("open results")
    pump(app, 0.3)
    ext = win.extensions["opencae.ui.assembly"]
    assert win.stack.currentWidget() is ext.workspace()
    con.execute("status")
    assert "1 done" in con.text() or "done" in con.text()
    shot(win, "motion_done")


# ---- parameter study
@pytest.mark.gui
@pytest.mark.solver
def test_iterate_runs_cases_records_results_and_restores_the_value(app, win, con, tmp_path):
    reg = reg_of(win)
    con.execute("open gripper")
    pump(app, 0.4)
    gid = next(g for g, b in win.session.bodies.items() if b.name == "Left finger")
    feat = next(f for f in win.session.project.geometry.features if f.id == gid)
    assert feat.params["dx"] == 4.0
    pos0 = list(feat.params["position"])
    con.execute("iterate finger thickness from 2 mm to 5 mm in 3 steps")
    assert con.pending is not None and con.pending.kind == "form"               # same set-up question as 'run'
    con.submit_form()
    pump(app, 0.5)
    study_job = next(j for j in reg.jobs() if j.kind == "parameter_study")
    assert study_job.source == "console" and study_job.state == "running"
    assert [c.name for c in study_job.children] == ["finger thickness = 2 mm", "finger thickness = 3.5 mm",
                                                    "finger thickness = 5 mm"]
    assert study_job.progress == 0.0 and study_job.case_counts()["total"] == 3
    n_jobs = len(reg.jobs())
    con.execute("iterate finger thickness from 2 mm to 5 mm in 3 steps")      # no second study while one runs
    assert len(reg.jobs()) == n_jobs and "already running" in con.text().lower()
    shot(win, "study_running")
    wait_for(app, lambda: study_job.state in ("done", "failed", "cancelled") and win.proc is None, 300)
    pump(app, 0.4)
    assert study_job.state == "done", study_job.error
    assert [c.state for c in study_job.children] == ["done"] * 3 and study_job.progress == 1.0
    assert all(c.detail and "mm" in c.detail for c in study_job.children)     # key result per case
    assert not [j for j in reg.jobs() if j.kind in ("mesh", "structural")
                and j.created >= study_job.created]                           # cases are not separate clutter
    # the geometry is back to the original
    assert feat.params["dx"] == 4.0 and feat.params["position"] == pos0
    assert "Restored finger thickness to 4 mm" in con.text()
    assert win.session.bodies[gid].bbox[1][0] - win.session.bodies[gid].bbox[0][0] == pytest.approx(4.0)
    # results: thicker is stiffer
    rec = con.actions.last_study
    disp = [r["metrics"]["Max displacement"][0] for r in rec.rows]
    stress = [r["metrics"]["Max von Mises stress"][0] for r in rec.rows]
    assert disp[0] > disp[1] > disp[2] > 0 and stress[0] > stress[1] > stress[2] > 0
    assert disp[0] / disp[2] > 10                                              # ~ (5/2)^3 for a bending beam
    assert {"show table", "show plot", "export csv"} <= set(con.chips.labels())
    assert reg.can_open(study_job)
    con.execute("show table")
    html = con.output.toHtml()
    assert "finger thickness [mm]" in html and "Max displacement [mm]" in html
    con.execute("export csv")
    csvs = list((win.session.data_dir / "studies").glob("*.csv"))
    assert csvs and csvs[0].read_text().splitlines()[0].startswith("finger thickness [mm]")
    con.execute("show plot")
    assert "Plot opened" in con.text() and any(d.isVisible() for d in win._console_plots)
    pump(app, 0.3)
    shot(win, "study_result")
    for d in win._console_plots:
        d.close()
    jobs_file = Path(str(win.session.path)).with_name(Path(str(win.session.path)).stem + "_data") / "jobs.jsonl"
    assert jobs_file.exists() and "parameter_study" in jobs_file.read_text()


@pytest.mark.gui
@pytest.mark.solver
def test_cancelling_a_study_stops_after_the_current_case_and_restores(app, win, con):
    reg = reg_of(win)
    con.execute("open gripper")
    pump(app, 0.4)
    feat = next(f for f in win.session.project.geometry.features
                if win.session.project.geometry.body_names.get(f.id) == "Left finger")
    con.execute("iterate left finger thickness from 2 mm to 6 mm in 5 steps")
    con.submit_form()
    pump(app, 0.4)
    job = next(j for j in reg.jobs() if j.kind == "parameter_study")
    con.execute("stop")
    assert job.state == "cancelled" or job.state == "running"
    wait_for(app, lambda: win.proc is None and not con.actions._study, 120)
    pump(app, 0.3)
    assert job.state == "cancelled" and job.case_counts()["done"] <= 2
    assert feat.params["dx"] == 4.0
    assert "cancel" in con.text().lower() and "Restored" in con.text()


@pytest.mark.gui
@pytest.mark.solver
@pytest.mark.parametrize("failure", ["no_mesh_process", "mesher_raises"])
def test_a_study_gives_back_the_mesh_settings_even_when_it_fails(app, win, con, monkeypatch, failure):
    """Review N9: the coarse mesh the study picks for its cases must not stay in the user's project."""
    reg = reg_of(win)
    con.execute("open gripper")
    pump(app, 0.4)
    sizes = lambda: [(st.name, st.mesh_settings.size_max) for st in win.session.project.studies]  # noqa: E731
    before = sizes()
    assert all(not v for _n, v in before)

    def boom(*a, **k):
        raise RuntimeError("mesher exploded")
    monkeypatch.setattr(win, "cmd_generate_mesh", (lambda *a, **k: None) if failure == "no_mesh_process" else boom)
    con.execute("iterate finger thickness from 2 mm to 4 mm in 2 steps")
    con.submit_form()
    wait_for(app, lambda: con.actions._study is None, 60)
    pump(app, 0.3)
    job = next(j for j in reg.jobs() if j.kind == "parameter_study")
    assert job.state in ("failed", "cancelled")                   # every case failed: said, not hidden
    assert not any(v for _n, v in sizes())                          # mesh settings are the user's again (also the new study's)
    assert "Restored finger thickness" in con.text()
    feat = next(f for f in win.session.project.geometry.features
                if win.session.project.geometry.body_names.get(f.id) == "Left finger")
    assert feat.params["dx"] == 4.0


# ---- history, popup, chips, palette
@pytest.mark.gui
def test_history_persists_and_up_down_recall(app, win, con, tmp_path):
    con.execute("status")
    con.execute("help")
    hist = (tmp_path / "home" / "console_history.txt").read_text().splitlines()
    assert hist[-2:] == ["status", "help"]
    from opencae.ui.console import Console
    again = Console(win)                                   # a reopened console loads it
    assert again.history[-2:] == ["status", "help"]
    con.input.clear()
    con.history_step(-1)
    assert con.input.text() == "help"
    con.history_step(-1)
    assert con.input.text() == "status"
    con.history_step(1)
    assert con.input.text() == "help"


@pytest.mark.gui
def test_popup_shows_fuzzy_suggestions_and_tab_completes(app, win, con):
    con.execute("open gripper")
    pump(app, 0.4)
    con.input.setText("run st")
    con.input.textEdited.emit("run st")
    assert con.popup.isVisible() and con.popup.item(0).text().startswith("run structural")
    con.tab_complete()
    assert con.popup.isVisible() or con.input.text().startswith("run structural")
    con.input.setText("run structural on le")
    con.input.textEdited.emit("run structural on le")
    assert [con.popup.item(i).text() for i in range(con.popup.count())][0] == "run structural on Left finger"
    con.tab_complete()
    assert con.input.text().startswith("run structural on Left finger")
    con.input.setText("select lft")                         # fuzzy
    con.input.textEdited.emit("select lft")
    assert "select Left finger" in [con.popup.item(i).text() for i in range(con.popup.count())]
    con.input.setText("iterate fing")
    con.input.textEdited.emit("iterate fing")
    assert any("finger thickness" in con.popup.item(i).text() for i in range(con.popup.count()))
    con.input.clear()
    con.hide_popup()
    assert not con.popup.isVisible()


@pytest.mark.gui
def test_did_you_mean_chips_run_the_suggestion_and_replies_link_jobs(app, win, con):
    con.execute("open gripper")
    pump(app, 0.4)
    con.execute("selct left finger")
    assert "Did you mean" in con.text() and "select left finger" in con.chips.labels()
    chip = next(b for b in con.chips.chips if b.text() == "select left finger")
    chip.click()
    assert [win.session.bodies[p.body].name for p in win.selection] == ["Left finger"]
    # a job link in a reply focuses the card in the Progress Monitor
    ext = win.extensions["opencae.ui.jobs"]
    job = ext.reg.create_job(ext.sync_project(), "other", "Demo job", "demo")
    ext.dock.hide()
    from PySide6.QtCore import QUrl
    con._link(QUrl(f"job:{job.id}"))
    pump(app, 0.2)
    assert ext.dock.isVisible() and job.id in ext.monitor.cards
    ext.reg.finish(job, True)


@pytest.mark.gui
def test_palette_runs_a_command_through_the_console(app, win, con):
    con.execute("open gripper")
    pump(app, 0.4)
    pal = con.palette
    assert pal.action.shortcut().toString() == "Ctrl+K"
    pal.open()
    assert pal.isVisible() and pal.list.count() > 10
    pal.edit.setText("select left finger")
    assert pal.list.item(0).text().startswith("select Left finger")
    pal.edit.setText("selct lft fingr")                         # fuzzy search
    assert "Left finger" in pal.list.item(0).text()
    pal.edit.setText("select left finger")
    assert pal.run_current()
    assert not pal.isVisible()
    assert [win.session.bodies[p.body].name for p in win.selection] == ["Left finger"]
    assert "> select Left finger" in con.text()
    pal.open()
    pal.edit.setText("iterate finger")                         # needs numbers: goes to the input for checking
    assert pal.list.item(0).text().startswith("iterate finger thickness from")
    pal.run_current()
    assert con.input.text().startswith("iterate finger thickness from") and not reg_of(win).jobs()
    con.input.clear()


@pytest.mark.gui
def test_cli_commands_and_python_still_work_next_to_the_natural_ones(app, win, con):
    con.execute("help")
    for w in ("open gripper", "run structural on left finger", "iterate finger thickness", "run mass propulsion"):
        assert w in con.text() or w.split()[0] in con.text()
    con.execute("help run")
    assert "run <structural" in con.text()
    con.execute("open /no/such/dir")
    assert "No drone.json" in con.text()
    con.execute(">>> 1 + 1")
    assert "2" in con.text().splitlines()[-1]
    con.execute("status")
    assert "No jobs yet" in con.text()
    con.execute("clear")
    assert con.text().strip() == ""
