"""Job registry (Qt-free core) and the Progress Monitor dock. Offscreen."""
import json
import threading
import time
from pathlib import Path

import pytest

from opencae.ui.jobs.registry import JobRegistry


# ------------------------------------------------------------------------------------------------ registry core
def test_job_states_stages_and_no_invented_progress():
    r = JobRegistry()
    j = r.create_job("/p/a.ocae", "structural", "Static: Beam", "Beam", "linear_static", start=False)
    assert j.state == "queued" and j.started is None and j.progress is None
    assert [s.name for s in j.stages] == ["Input validation", "Geometry preparation", "Meshing", "Solver",
                                          "Postprocessing", "Results ready"]
    r.start(j)
    assert j.state == "running" and j.started is not None
    r.stage(j, "Input validation", "running")
    r.stage(j, "Input validation", "done")
    r.stage(j, "Meshing", "running", detail="surfaces")
    r.stage(j, "Solver", "running")      # starting a stage closes the running one before it
    assert j.stage_named("Meshing").state == "done" and j.stage_named("Meshing").finished is not None
    assert j.stage_named("Solver").state == "running"
    assert j.progress is None and j.stage_named("Solver").progress is None   # never guessed
    r.set_progress(j, 0.4)
    assert j.progress == 0.4
    r.log(j, "line 1")
    r.log(j, "careful", "warning")
    r.log(j, "boom", "error")
    assert j.log == ["line 1", "careful", "boom"] and j.warnings == ["careful"]
    r.finish(j, True, outputs={"results": "/x"})
    assert j.state == "done" and j.finished and j.progress == 1.0 and j.outputs["results"] == "/x"
    assert j.stage_named("Solver").state == "done" and j.stage_named("Geometry preparation").state == "skipped"
    assert r.active() == []


def test_kinds_have_their_own_stage_lists_and_log_is_bounded():
    r = JobRegistry()
    assert [s.name for s in r.create_job("p", "mesh", "m").stages] == ["Geometry preparation", "Meshing",
                                                                         "Results ready"]
    assert r.create_job("p", "drone", "d").stages == []
    j = r.create_job("p", "other", "x")
    for i in range(2000):
        r.log(j, f"l{i}")
    assert len(j.log) == 500 and j.log[-1] == "l1999"


def test_failed_job_keeps_error_and_marks_stage():
    r = JobRegistry()
    j = r.create_job("p", "structural", "s", "t")
    r.stage(j, "Solver", "running")
    r.finish(j, False, error="singular matrix")
    assert j.state == "failed" and j.error == "singular matrix" and j.stage_named("Solver").state == "failed"


def test_parameter_study_cases_and_countable_progress():
    r = JobRegistry()
    j = r.create_job("p", "parameter_study", "Thickness 2 to 5 mm", "finger")
    r.set_cases(j, [f"t = {t}" for t in (2, 3, 4, 5)])
    assert j.progress == 0.0 and j.case_counts()["total"] == 4
    r.case(j, "t = 2", "running")
    r.case(j, "t = 2", "done")
    r.case(j, "t = 3", "failed", error="mesh failed")
    assert j.progress == 0.5
    assert j.case_counts() == {"total": 4, "done": 1, "failed": 1, "running": 0, "queued": 2, "cancelled": 0}


def test_persistence_round_trip_and_project_switching(tmp_path):
    a, b = tmp_path / "a_data", tmp_path / "b_data"
    r = JobRegistry()
    r.register_project("/p/a.ocae", "Alpha", a, activate=True)
    r.register_project("/p/b.ocae", "Beta", b)
    j1 = r.create_job("/p/a.ocae", "mesh", "Mesh A", "bodyA")
    r.log(j1, "hello")
    r.finish(j1, True, outputs={"elements": 12})
    j2 = r.create_job("/p/b.ocae", "structural", "Solve B", "bodyB")
    r.finish(j2, False, error="nope")
    j3 = r.create_job("/p/a.ocae", "structural", "still running", "bodyA")   # never finished: not saved
    lines = (a / "jobs.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["title"] == "Mesh A"
    assert (b / "jobs.jsonl").exists()
    r2 = JobRegistry()                                    # a restart
    r2.register_project("/p/a.ocae", "Alpha", a)
    assert [j.title for j in r2.jobs("/p/a.ocae")] == ["Mesh A"] and r2.jobs("/p/b.ocae") == []
    loaded = r2.jobs("/p/a.ocae")[0]
    assert loaded.state == "done" and loaded.outputs == {"elements": 12} and loaded.log == ["hello"]
    r2.register_project("/p/b.ocae", "Beta", b)           # switching keeps both histories
    r2.set_active_project("/p/b.ocae")
    assert [j.title for j in r2.jobs("/p/b.ocae")] == ["Solve B"] and len(r2.jobs()) == 2
    assert r2.jobs("/p/b.ocae")[0].error == "nope" and r2.jobs("/p/b.ocae")[0].project_name == "Beta"
    r2.register_project("/p/a.ocae", "Alpha", a)          # registering twice does not duplicate
    assert len(r2.jobs("/p/a.ocae")) == 1


def test_corrupt_history_lines_are_skipped(tmp_path):
    d = tmp_path / "x_data"
    d.mkdir()
    good = JobRegistry()
    good.register_project("k", "K", d)
    good.finish(good.create_job("k", "other", "ok"), True)
    with open(d / "jobs.jsonl", "a") as f:
        f.write("not json\n{\"id\": 3}\n")
    r = JobRegistry()
    r.register_project("k", "K", d)
    assert [j.title for j in r.jobs("k")] == ["ok"]


def test_duplicate_guard_finds_the_running_job():
    r = JobRegistry()
    r.register_project("pa", "A", activate=True)
    j = r.create_job("pa", "structural", "Solve", "finger")
    assert r.find_running("structural", "finger") is j and r.find_running("structural", "finger", "pa") is j
    assert r.find_running("thermal", "finger") is None and r.find_running("structural", "other") is None
    assert r.find_running("structural", "finger", "elsewhere") is None
    r.finish(j, True)
    assert r.find_running("structural", "finger") is None


def test_cancel_calls_callback_and_marks_cancelled():
    r = JobRegistry()
    hits = []
    j = r.create_job("p", "structural", "s", "t", cancel=lambda: hits.append(1))
    assert r.cancel(j.id) and hits == [1] and j.state == "cancelled" and j.finished
    r.finish(j, True)                       # a late "done" from the worker does not undo it
    assert j.state == "cancelled"
    assert not r.cancel(j.id)               # already over
    nocb = r.create_job("p", "structural", "s", "t2")
    assert not r.cancel(nocb.id) and nocb.state == "running"


def test_retry_links_old_and_new_job():
    r = JobRegistry()

    def again():
        return r.create_job("p", "structural", "s again", "t")

    j = r.create_job("p", "structural", "s", "t", retry=again)
    assert r.retry(j.id) is None            # only failed or cancelled jobs are retried
    r.finish(j, False, error="x")
    new = r.retry(j.id)
    assert new is not None and new.retry_of == j.id and j.retried_as == new.id and new.state == "running"
    plain = r.create_job("p", "structural", "s", "t")
    r.finish(plain, False, error="x")
    assert r.retry(plain.id) is None        # nothing registered to start it again


def test_pause_only_when_supported():
    r = JobRegistry()
    j = r.create_job("p", "other", "x")
    assert not r.pause(j.id) and not j.to_dict()["can_pause"]
    state = []
    k = r.create_job("p", "other", "y", pause=lambda: state.append("p"), resume=lambda: state.append("r"))
    assert k.to_dict()["can_pause"] and r.pause(k.id) and k.state == "paused" and k.active
    assert r.resume(k.id) and k.state == "running" and state == ["p", "r"]


def test_open_results_uses_job_callback_or_registered_opener():
    r = JobRegistry()
    seen = []
    r.register_opener("viewer", lambda job: seen.append(job.id))
    j = r.create_job("p", "structural", "s", "t")
    assert not r.open_results(j.id)         # not done
    r.finish(j, True, outputs={"opener": "viewer"})
    assert r.can_open(j) and r.open_results(j.id) and seen == [j.id]
    k = r.create_job("p", "structural", "s", "t", opener=lambda job: seen.append("cb"))
    r.finish(k, True)
    assert r.open_results(k.id) and seen[-1] == "cb"
    m = r.create_job("p", "structural", "s", "t")
    r.finish(m, True)
    assert not r.can_open(m)


def test_events_from_worker_threads_are_all_delivered():
    r = JobRegistry()
    got = []
    lock = threading.Lock()

    def cb(ev):
        with lock:
            got.append((ev.kind, ev.job_id))
    off = r.subscribe(cb)
    jobs = [r.create_job("p", "other", f"j{i}", f"t{i}") for i in range(4)]

    def work(j):
        for i in range(50):
            r.log(j, f"line {i}")
            r.stage(j, "Solver", "running", progress=i / 50)
        r.finish(j, True)
    ts = [threading.Thread(target=work, args=(j,)) for j in jobs]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(1 for k, _ in got if k == "finished") == 4
    assert sum(1 for k, _ in got if k == "log") == 200
    assert all(len(j.log) == 50 and j.state == "done" for j in jobs)
    off()
    n = len(got)
    r.create_job("p", "other", "again")
    assert len(got) == n


def test_a_failing_listener_does_not_break_the_run():
    r = JobRegistry()
    r.subscribe(lambda ev: 1 / 0)
    j = r.create_job("p", "other", "x")
    r.finish(j, True)
    assert j.state == "done"


def test_unsaved_project_jobs_follow_the_first_save(tmp_path):
    r = JobRegistry()
    r.register_project("(unsaved)", "Untitled", None, activate=True)
    j = r.create_job("(unsaved)", "mesh", "Mesh", "b")
    r.finish(j, True)
    d = tmp_path / "saved_data"
    r.rename_project("(unsaved)", "/p/saved.ocae", "Saved", d)
    assert r.active_project == "/p/saved.ocae" and j.project_path == "/p/saved.ocae"
    assert len((d / "jobs.jsonl").read_text().splitlines()) == 1


# ------------------------------------------------------------------------------------------------ Qt: the dock
QUAD = Path(__file__).resolve().parents[1] / "examples" / "drones" / "quad250"
gui = pytest.mark.gui


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


def _make_win(tmp_path, monkeypatch, name="s.ini"):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QMessageBox
    from opencae.ui.main_window import MainWindow
    monkeypatch.setenv("OPENCAE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Discard))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    w = MainWindow(settings=QSettings(str(tmp_path / name), QSettings.IniFormat))
    w.show()
    return w


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    w = _make_win(tmp_path, monkeypatch)
    yield w
    w.dirty = False
    w.close()


def pump(app, sec=0.3):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents()
        time.sleep(0.01)


def wait_for(app, cond, timeout=60):
    t = time.time()
    while not cond() and time.time() - t < timeout:
        app.processEvents()
        time.sleep(0.02)
    assert cond(), "timed out"


def jobs_ext(w):
    return w.extensions["opencae.ui.jobs"]


@gui
def test_monitor_dock_sits_left_without_hiding_the_model_tree(app, win):
    from PySide6.QtCore import Qt
    ext = jobs_ext(win)
    assert win.EXTENSIONS[0] == "opencae.ui.jobs"
    pump(app)
    assert win.dockWidgetArea(ext.dock) == Qt.LeftDockWidgetArea
    assert win.dockWidgetArea(win.tree_dock) == Qt.LeftDockWidgetArea
    assert ext.dock.isVisible() and win.tree_dock.isVisible()
    assert not win.tree_dock.geometry().intersects(ext.dock.geometry())      # stacked, neither covers the other
    assert win.props_dock.isVisible() and win.bottom_dock.isVisible() and win.viewport.isVisible()
    assert ext.dock.width() >= 250 and ext.dock.height() > 150
    from PySide6.QtWidgets import QDockWidget
    assert ext.dock.features() & QDockWidget.DockWidgetClosable and ext.dock.features() & QDockWidget.DockWidgetMovable


@gui
def test_view_menu_entry_toggles_the_monitor(app, win):
    ext = jobs_ext(win)
    view = next(a.menu() for a in win.menuBar().actions() if a.text().replace("&", "") == "View")
    entry = next(a for a in view.actions() if a.text() == "Progress Monitor")
    assert entry.isChecked() and ext.dock.isVisible()
    entry.trigger()
    pump(app, 0.1)
    assert not ext.dock.isVisible() and not entry.isChecked()
    w_before = win.viewport.width()
    entry.trigger()
    pump(app, 0.1)
    assert ext.dock.isVisible() and entry.isChecked()
    assert win.viewport.width() <= w_before + 1       # the workspace gave room, it does not grow past its size


@gui
def test_collapsing_the_monitor_gives_the_room_back(app, win):
    ext = jobs_ext(win)
    pump(app, 0.2)
    w_open = win.viewport.width()
    ext.dock.close()
    pump(app, 0.3)
    assert win.viewport.width() >= w_open
    ext.show_monitor()
    pump(app, 0.2)
    assert ext.dock.isVisible()


@gui
def test_layout_survives_closing_and_reopening(app, tmp_path, monkeypatch):
    w1 = _make_win(tmp_path, monkeypatch)
    ext = jobs_ext(w1)
    ext.dock.close()                                   # the user closed the monitor ...
    w1.resizeDocks([w1.props_dock], [420], __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.Horizontal)
    pump(app, 0.2)
    w1.dirty = False
    w1.close()                                         # ... and quit: the layout is saved
    w2 = _make_win(tmp_path, monkeypatch)              # same settings file
    try:
        pump(app, 0.3)
        assert not jobs_ext(w2).dock.isVisible()
        assert w2.tree_dock.isVisible()
        jobs_ext(w2).show_monitor()
        assert jobs_ext(w2).dock.isVisible()
    finally:
        w2.dirty = False
        w2.close()
    # a restart with the monitor left open keeps it open, still on the left
    from PySide6.QtCore import Qt
    w3 = _make_win(tmp_path, monkeypatch, name="other.ini")
    try:
        w3.dirty = False
        w3.close()
        w4 = _make_win(tmp_path, monkeypatch, name="other.ini")
        pump(app, 0.2)
        assert jobs_ext(w4).dock.isVisible() and w4.dockWidgetArea(jobs_ext(w4).dock) == Qt.LeftDockWidgetArea
        w4.dirty = False
        w4.close()
    finally:
        pass


@gui
def test_multiple_jobs_show_as_cards_newest_first(app, win):
    ext = jobs_ext(win)
    reg = ext.reg
    key = ext.sync_project()
    a = reg.create_job(key, "mesh", "Mesh: A", "A")
    b = reg.create_job(key, "structural", "Solve: B", "B")
    c = reg.create_job(key, "parameter_study", "Thickness sweep", "finger")
    reg.set_cases(c, ["t = 2", "t = 3", "t = 4"])
    reg.case(c, "t = 2", "done")
    reg.case(c, "t = 3", "failed", error="mesh failed")
    reg.finish(a, True)
    pump(app, 0.3)
    mon = ext.monitor
    assert mon._order == [c.id, b.id, a.id]
    assert mon.cards[b.id].chip.text() == "Running" and mon.cards[a.id].chip.text() == "Done"
    assert "2/3 cases, 1 failed" in mon.cards[c.id].meta.text()
    assert "2 running" in mon.status_label.text() and "1 running" not in mon.status_label.text()
    assert ext.counter.text() == "● 2 running"
    for j in (b, c):
        reg.finish(j, True)
    pump(app, 0.3)
    assert ext.counter.text() in ("No jobs running",) and "done" in mon.status_label.text()


@gui
def test_cases_expand_individually(app, win):
    ext = jobs_ext(win)
    reg = ext.reg
    c = reg.create_job(ext.sync_project(), "parameter_study", "Sweep", "finger", stages=[])
    reg.set_cases(c, ["t = 2", "t = 3"])
    reg.case(c, "t = 2", "failed", error="meshing failed: self-intersection")
    pump(app, 0.2)
    card = ext.monitor.cards[c.id]
    card.toggle(True)
    pump(app, 0.2)
    assert len(card.case_rows) == 2
    row = card.case_rows[0]
    assert not row.body.isVisible()
    row._toggle()
    assert row.body.isVisible() and "self-intersection" in row.body.text()


@gui
def test_failed_job_shows_error_and_retry(app, win):
    ext = jobs_ext(win)
    reg = ext.reg
    started = []
    j = reg.create_job(ext.sync_project(), "structural", "Solve: Beam", "Beam", "linear_static",
                       retry=lambda: started.append(1) or reg.create_job(ext.sync_project(), "structural",
                                                                          "Solve: Beam", "Beam"))
    reg.stage(j, "Solver", "running")
    reg.log(j, "*WARNING in e_c3d: distorted element", "warning")
    reg.finish(j, False, error="Solver stopped: singular stiffness matrix (rigid body motion).")
    pump(app, 0.3)
    card = ext.monitor.cards[j.id]
    assert card.chip.text() == "Failed" and card.error.isVisibleTo(card) and "singular" in card.error.text()
    assert card.b_retry.isVisibleTo(card) and not card.b_cancel.isVisibleTo(card)
    assert not card.b_open.isVisibleTo(card)
    card.toggle(True)
    pump(app, 0.2)
    assert "distorted element" in card.log.toPlainText()
    assert "Solver" in card.stages_label.text()
    card.b_retry.click()
    pump(app, 0.3)
    assert started == [1] and j.retried_as is not None
    assert len(ext.monitor.cards) == 2
    assert ext.counter.text() == "● 1 running"


@gui
def test_open_results_button_uses_the_registered_opener(app, win):
    ext = jobs_ext(win)
    seen = []
    ext.reg.register_opener("mbd.playback", lambda job: seen.append(job.title))
    j = ext.reg.create_job(ext.sync_project(), "motion", "Motion: gripper", "gripper")
    ext.reg.finish(j, True, outputs={"opener": "mbd.playback"})
    pump(app, 0.3)
    card = ext.monitor.cards[j.id]
    assert card.b_open.isVisibleTo(card)
    card.b_open.click()
    assert seen == ["Motion: gripper"]


@gui
def test_pause_button_only_when_the_job_supports_it(app, win):
    ext = jobs_ext(win)
    a = ext.reg.create_job(ext.sync_project(), "other", "no pause", "x")
    b = ext.reg.create_job(ext.sync_project(), "other", "can pause", "y", pause=lambda: None, resume=lambda: None)
    pump(app, 0.3)
    assert not ext.monitor.cards[a.id].b_pause.isVisibleTo(ext.monitor.cards[a.id])
    cb = ext.monitor.cards[b.id]
    assert cb.b_pause.isVisibleTo(cb) and cb.b_pause.text() == "Pause"
    cb.b_pause.click()
    pump(app, 0.2)
    assert b.state == "paused" and cb.b_pause.text() == "Resume" and cb.chip.text() == "Paused"


@gui
def test_progress_bar_only_shows_a_fraction_when_one_is_known(app, win):
    ext = jobs_ext(win)
    j = ext.reg.create_job(ext.sync_project(), "structural", "Solve", "x")
    ext.reg.stage(j, "Solver", "running")
    pump(app, 0.2)
    card = ext.monitor.cards[j.id]
    assert card.bar.value is None                      # busy indicator, no made-up percentage
    ext.reg.set_progress(j, 0.25)
    pump(app, 0.2)
    assert card.bar.value == 0.25 and "25%" in card.elapsed.text()


@gui
def test_background_job_of_another_project_stays_visible_with_its_name(app, win, tmp_path):
    ext = jobs_ext(win)
    reg = ext.reg
    key = ext.sync_project()
    reg.register_project("/other/robot.ocae", "Robot arm", tmp_path / "robot_data")
    bg = reg.create_job("/other/robot.ocae", "structural", "Solve: link", "link")
    old = reg.create_job("/other/robot.ocae", "mesh", "Mesh: old", "old")
    reg.finish(old, True)
    here = reg.create_job(key, "mesh", "Mesh: here", "here")
    reg.finish(here, True)
    pump(app, 0.3)
    mon = ext.monitor
    assert bg.id in mon.cards and here.id in mon.cards and old.id not in mon.cards
    assert "Robot arm" in mon.cards[bg.id].sub.text()
    mon.other_check.setChecked(True)
    pump(app, 0.3)
    assert old.id in mon.cards
    assert (tmp_path / "robot_data" / "jobs.jsonl").exists()


@gui
def test_status_bar_counter_opens_the_monitor(app, win):
    ext = jobs_ext(win)
    ext.dock.close()
    pump(app, 0.1)
    j = ext.reg.create_job(ext.sync_project(), "structural", "Solve", "x")
    pump(app, 0.2)
    assert ext.counter.text() == "● 1 running"
    ext.reg.finish(j, False, error="boom")
    pump(app, 0.2)
    assert ext.counter.text() == "✖ 1 failed"
    ext.counter.click()
    pump(app, 0.2)
    assert ext.dock.isVisible() and ext.counter.text() == "No jobs running"


# ------------------------------------------------------------------------------------------------ callables / cancel
@gui
def test_track_callable_runs_in_a_thread_with_real_progress(app, win):
    from opencae.ui.jobs import track_callable
    gate = threading.Event()
    done = []

    def work(progress, cancelled):
        progress(0.0, "starting")
        gate.wait(10)
        progress(0.5, "halfway")
        progress(1.0, "end")
        return {"answer": 42}

    job = track_callable(win, "motion", "Motion: demo", "demo", work,
                         stages=("Input validation", "Solver", "Postprocessing", "Results ready"),
                         source="console", on_done=done.append)
    wait_for(app, lambda: job.stage_named("Solver").state == "running")
    assert job.state == "running" and job.source == "console" and job.stage_named("Input validation").state == "done"
    assert job.progress is not None
    # the same analysis is not started twice: the running job is returned and focused
    again = track_callable(win, "motion", "Motion: demo", "demo", work, stages=None)
    assert again is job
    gate.set()
    wait_for(app, lambda: job.state == "done")
    assert [s.state for s in job.stages] == ["done"] * 4 and job.outputs["answer"] == 42 and job.progress == 1.0
    assert done == [{"answer": 42}]
    wait_for(app, lambda: not jobs_ext(win)._live)       # the worker thread has ended and is released


@gui
def test_cancel_during_a_long_job_shows_cancelled(app, win):
    from opencae.ui.jobs import track_callable
    started = threading.Event()

    def long_job(progress, cancelled):
        started.set()
        t0 = time.time()
        while not cancelled():
            if time.time() - t0 > 30:
                return {}
            progress(None, "")
            time.sleep(0.02)
        raise RuntimeError("MBDCancelled: the run was cancelled")

    job = track_callable(win, "motion", "Long run", "long", long_job, stages=("Input validation", "Solver"))
    wait_for(app, started.is_set)
    pump(app, 0.3)
    card = jobs_ext(win).monitor.cards[job.id]
    assert card.b_cancel.isVisibleTo(card)
    card.b_cancel.click()
    pump(app, 0.2)
    assert job.state == "cancelled" and card.chip.text() == "Cancelled"
    wait_for(app, lambda: not any(t.isRunning() for t in jobs_ext(win)._live))
    assert job.state == "cancelled" and jobs_ext(win).reg.find_running("motion", "long") is None


@gui
def test_failing_callable_reports_readable_error_and_retry_works(app, win):
    from opencae.ui.jobs import track_callable
    calls = []

    def flaky(progress, cancelled):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("the model has no fixed support")
        return {"ok": True}

    job = track_callable(win, "motion", "Flaky", "flaky", flaky)
    wait_for(app, lambda: job.state == "failed")
    assert job.error == "the model has no fixed support"
    assert any("Traceback" in t or "ValueError" in t for t in job.log)
    new = jobs_ext(win).reg.retry(job.id)
    assert new is not None and new.retry_of == job.id
    wait_for(app, lambda: new.state == "done")
    assert len(calls) == 2


@gui
def test_mbd_adapter_reports_backend_progress(app, win, monkeypatch):
    import types, sys
    from opencae.ui import jobs

    def fake_run(project, study, progress=None, cancelled=None, data_dir=None):
        for i in range(5):
            progress(i / 5, f"t = {i} s")
        return None
    mod = types.ModuleType("opencae.mbd.run")
    mod.run_study = fake_run
    monkeypatch.setitem(sys.modules, "opencae.mbd.run", mod)
    study = types.SimpleNamespace(name="Gripper close", id="s1")
    job = jobs.track_mbd_run(win, object(), study)
    wait_for(app, lambda: job.state == "done")
    assert job.kind == "motion" and job.outputs["opener"] == "mbd.playback" and job.stage_named("Solver").state == "done"
    assert any("t = 3 s" in t for t in job.log)


# ------------------------------------------------------------------------------------------------ Studio integration
def _cantilever(app, win):
    win.cmd_example()
    pump(app, 0.2)


def _wait_proc(app, w, timeout=240):
    t = time.time()
    while w.proc is not None and time.time() - t < timeout:
        app.processEvents()
        time.sleep(0.02)
    assert w.proc is None, "background job did not finish"
    pump(app, 0.2)


@gui
@pytest.mark.solver
def test_studio_mesh_and_solve_are_one_monitored_workflow(app, win):
    ext = jobs_ext(win)
    _cantilever(app, win)
    win.cmd_generate_mesh()
    pump(app, 0.3)
    mesh_job = ext.reg.jobs()[0]
    assert mesh_job.kind == "mesh" and mesh_job.state == "running" and mesh_job.stage_named("Meshing").state == "running"
    assert ext.counter.text() == "● 1 running"
    _wait_proc(app, win)
    assert mesh_job.state == "done" and mesh_job.outputs["elements"] > 0
    assert [s.name for s in mesh_job.stages] == ["Geometry preparation", "Meshing", "Results ready"]
    win.cmd_solve()
    pump(app, 0.3)
    job = ext.reg.jobs()[0]
    assert job.kind == "structural" and job.state == "running" and job.id != mesh_job.id
    card = ext.monitor.cards[job.id]
    assert card.b_cancel.isVisibleTo(card)
    _wait_proc(app, win)
    assert job.state == "done", job.error
    assert [s.name for s in job.stages] == ["Meshing", "Solver", "Postprocessing", "Results ready"]
    assert [s.state for s in job.stages] == ["done"] * 4
    assert job.progress is None                       # ccx reports no percentage: none is invented
    assert job.outputs["opener"] == "studio.results" and Path(job.outputs["results_folder"]).exists()
    assert any("Job finished" in ln or "job finished" in ln.lower() for ln in job.log)
    assert job.target == "Study 1" or job.target
    assert len([j for j in ext.reg.jobs() if j.kind == "structural"]) == 1
    assert win.session.results is not None
    win.set_view_mode("geometry")
    pump(app, 0.2)
    card = ext.monitor.cards[job.id]
    card.b_open.click()                                # "Open results" goes to the existing results viewer
    pump(app, 0.3)
    assert win.view_mode == "results"
    assert ext.counter.text() == "No jobs running"


@gui
def test_studio_run_that_cannot_start_leaves_a_readable_failed_job(app, win):
    ext = jobs_ext(win)
    win.cmd_example()
    pump(app, 0.2)
    win.session.project.studies[0].loads.clear() if hasattr(win.session.project.studies[0], "loads") else None
    # no mesh yet: the model is not ready to solve
    win.cmd_solve()
    pump(app, 0.3)
    jobs = [j for j in ext.reg.jobs() if j.kind == "structural"]
    assert len(jobs) == 1 and jobs[0].state == "failed" and jobs[0].error
    card = ext.monitor.cards[jobs[0].id]
    assert card.chip.text() == "Failed" and card.b_retry.isVisibleTo(card)
    assert jobs[0].stage_named("Input validation").state == "failed"
    win.cmd_generate_mesh()            # retry path of a different kind keeps working
    _wait_proc(app, win)
    assert [j for j in ext.reg.jobs() if j.kind == "mesh"][0].state == "done"


@gui
@pytest.mark.solver
def test_studio_cancel_marks_the_job_cancelled(app, win):
    ext = jobs_ext(win)
    _cantilever(app, win)
    win.cmd_generate_mesh()
    _wait_proc(app, win)
    win.cmd_solve()
    pump(app, 0.2)
    job = ext.reg.jobs()[0]
    ext.reg.cancel(job.id)             # the Cancel button of the card
    _wait_proc(app, win)
    assert job.state == "cancelled" and win.proc is None
    card = ext.monitor.cards[job.id]
    assert card.chip.text() == "Cancelled" and card.b_retry.isVisibleTo(card)


@gui
def test_jobs_follow_the_project_and_history_is_saved_per_project(app, win, tmp_path):
    ext = jobs_ext(win)
    win.cmd_example()
    p1 = tmp_path / "one.ocae"
    win.session.save(p1)
    key = ext.sync_project()
    assert key == str(p1) and ext.reg.active_project == key
    j = ext.reg.create_job(key, "mesh", "Mesh: one", "one")
    ext.reg.finish(j, True)
    assert (tmp_path / "one_data" / "jobs.jsonl").exists()
    win.session.new("Second")
    key2 = ext.sync_project()
    assert key2 != key and ext.reg.active_project == key2
    pump(app, 0.3)
    assert j.id not in ext.monitor.cards                      # other project's history is not mixed in ...
    ext.monitor.other_check.setChecked(True)
    pump(app, 0.2)
    assert j.id in ext.monitor.cards                           # ... unless asked for
    win.session.open(p1)
    ext.sync_project()
    assert ext.reg.active_project == key and "Project: Cantilever example" in ext.monitor.project_label.text()


# ------------------------------------------------------------------------------------------------ Drone
@gui
def test_drone_mass_and_propulsion_run_appears_in_the_monitor(app, win, tmp_path):
    import shutil
    proj = tmp_path / "quad"
    shutil.copytree(QUAD, proj, ignore=shutil.ignore_patterns("results"))
    ext = jobs_ext(win)
    t = win.extensions["opencae.ui.drone.tracker"]
    assert win.open_drone_project(proj)
    ws = win.drone_workspace()
    ws.server.run({"only": ["mass", "propulsion"]})
    t.refresh()
    pump(app, 0.2)
    drone = [j for j in ext.reg.jobs() if j.kind == "drone"]
    assert len(drone) == 1 and ext.reg.active_project == str(proj)
    assert ext.dock.isVisible() and drone[0].id in ext.monitor.cards
    ws.server.jobs.join(120)
    t.refresh()
    pump(app, 0.3)
    j = drone[0]
    assert j.state == "done", j.error
    assert [s.name for s in j.stages] == ["Mass", "Propulsion"] and all(s.state == "done" for s in j.stages)
    assert j.progress == 1.0 and j.outputs["opener"] == "drone.dashboard"
    assert j.source == "dashboard" and (proj / "results" / "jobs.jsonl").exists()
    card = ext.monitor.cards[j.id]
    assert card.chip.text() == "Done" and card.b_open.isVisibleTo(card)
    # a fresh app session sees the saved history of that project
    r = JobRegistry()
    r.register_project(str(proj), "quad", proj / "results")
    assert [x.title for x in r.jobs(str(proj))] == ["Drone analysis run"]


@gui
def test_old_tracker_api_still_works_and_show_dock_shows_the_monitor(app, win):
    t = win.extensions["opencae.ui.drone.tracker"]
    ext = jobs_ext(win)
    ext.dock.close()
    pump(app, 0.1)
    t.show_dock()
    pump(app, 0.1)
    assert ext.dock.isVisible() and not t.dock.isVisible()
    t.show_history()
    pump(app, 0.1)
    assert t.dock.isVisible() and t.tabs.currentIndex() == 1


@gui
def test_solver_setup_install_is_reported(app, win, monkeypatch):
    from opencae.drone import solver_setup as S
    from opencae.ui.drone import ux
    ext = jobs_ext(win)

    def fake_install(progress, cancelled):
        progress("downloading", 0.3)
        progress("testing", 0.9)
        return {"ok": True, "message": "SU2 works"}
    monkeypatch.setattr(S, "install_su2", fake_install)
    dlg = ux.SolverDialog(win)
    assert dlg.start("su2", confirmed=True)
    wait_for(app, lambda: dlg.worker is None)
    pump(app, 0.3)
    job = next(j for j in ext.reg.jobs() if j.kind == "setup")
    assert job.title == "Install SU2" and job.state == "done" and "downloading" in job.log
    assert job.id in ext.monitor.cards                       # global jobs are always listed
    dlg.close()


@gui
def test_k09l_design_monitor_run_is_reported(app, tmp_path):
    from opencae.ui.jobs import registry as R
    from opencae.ui.platform.monitor import MonitorWindow
    from opencae.engineering import study as ST
    reg = JobRegistry()
    R.set_default(reg)
    try:
        s = ST.Study()
        p = s.save(tmp_path / "study.json")
        w = MonitorWindow(study_path=p)
        w.show()
        w.run_analyses()
        wait_for(app, lambda: not w.busy, 120)
        pump(app, 0.2)
        jobs = reg.jobs()
        assert len(jobs) == 1 and jobs[0].kind == "drone" and jobs[0].analysis_type == "k09l"
        assert jobs[0].state == "done" and jobs[0].title.startswith("Running the analyses")
        w.close()
    finally:
        R.set_default(None)
