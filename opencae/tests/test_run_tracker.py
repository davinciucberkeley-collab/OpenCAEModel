"""Run progress tracker: pipeline events, runs.jsonl, history, ETA, server progress, the Qt dock."""
import json
import shutil
import time
import urllib.request
from pathlib import Path

import pytest

QUAD = Path(__file__).resolve().parents[1] / "examples" / "drones" / "quad250"


@pytest.fixture
def proj(tmp_path):
    dst = tmp_path / "quad"
    shutil.copytree(QUAD, dst, ignore=shutil.ignore_patterns("results"))
    return dst


# ------------------------------------------------------------------------------------------------ pipeline
def test_events_order_and_content(proj):
    from opencae.drone import pipeline
    evs, prog = [], []
    out = pipeline.run_project(proj, only=["mass", "propulsion"], events=evs.append,
                               progress=lambda m, f: prog.append((m, f)))
    assert prog and prog[-1] == ("done", 1.0)  # the progress callback still works
    kinds = [e["kind"] for e in evs]
    assert kinds[0] == "run_start" and kinds[-1] == "run_done"
    assert len({e["run_id"] for e in evs}) == 1
    starts = [e["id"] for e in evs if e["kind"] == "analysis_start"]
    dones = [e for e in evs if e["kind"] == "analysis_done"]
    assert starts == ["mass", "propulsion"] and [d["id"] for d in dones] == ["mass", "propulsion"]
    assert all(isinstance(d["duration_s"], float) and d["cached"] is False for d in dones)
    assert kinds.index("analysis_done") < kinds.index("analysis_start", kinds.index("analysis_start") + 1)
    assert any(e["kind"] == "step" and e["id"] == "mass" for e in evs)
    fr = [e["frac"] for e in evs]
    assert fr == sorted(fr) and fr[0] == 0.0 and fr[-1] == 1.0
    for e in evs:
        assert {"t", "kind", "run_id", "id", "title", "status", "frac", "message", "elapsed_s"} <= set(e)
    last = evs[-1]
    assert last["status"] == "done" and last["verdict"] == out["verdict"] and last["error"] is None
    assert [e["plan"] for e in evs if e["kind"] == "run_start"][0][0]["id"] == "mass"
    # second run reuses both: cached flag
    evs2 = []
    pipeline.run_project(proj, only=["mass", "propulsion"], events=evs2.append)
    assert [e["cached"] for e in evs2 if e["kind"] == "analysis_done"] == [True, True]


def test_runs_jsonl_history_and_cancel(proj):
    from opencae.drone import pipeline
    pipeline.run_project(proj, only=["mass", "propulsion"])
    flag = {"stop": False}

    def ev(e):
        if e["kind"] == "analysis_done" and e["id"] == "mass":
            flag["stop"] = True

    pipeline.run_project(proj, only=["mass", "propulsion"], variant="second", force=True, events=ev,
                         cancelled=lambda: flag["stop"])
    hist = pipeline.run_history(proj)
    assert len(hist) == 2
    a, b = hist
    assert a["status"] == "done" and set(a["analyses"]) == {"mass", "propulsion"} and a["variant"] == "baseline"
    assert a["analyses"]["mass"]["duration_s"] >= 0 and a["analyses"]["mass"]["cached"] is False
    assert a["only"] == ["mass", "propulsion"] and a["verdict"] and a["duration_s"] >= 0 and a["error"] is None
    assert b["status"] == "cancelled" and list(b["analyses"]) == ["mass"] and b["variant"] == "second"
    lines = (proj / "results" / "runs.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2 and all(json.loads(x) for x in lines)


def test_error_run_is_recorded(proj):
    from opencae.drone import pipeline
    (proj / "results").mkdir(exist_ok=True)
    spec = json.loads((proj / "drone.json").read_text())
    spec["geometry"]["file"] = "missing.step"
    (proj / "drone.json").write_text(json.dumps(spec))
    evs = []
    with pytest.raises(Exception):
        pipeline.run_project(proj, only=["mass"], events=evs.append)
    assert evs[-1]["kind"] == "run_done" and evs[-1]["status"] == "error" and evs[-1]["error"]
    assert pipeline.run_history(proj)[-1]["status"] == "error"


def test_eta_from_fake_history(tmp_path):
    from opencae.drone import pipeline
    d = tmp_path / "p"
    (d / "results").mkdir(parents=True)
    rows = [{"run_id": "a", "analyses": {"mass": {"status": "ok", "duration_s": 1.0, "cached": False},
                                         "cfd": {"status": "ok", "duration_s": 100.0, "cached": False}}},
            {"run_id": "b", "analyses": {"mass": {"status": "ok", "duration_s": 3.0, "cached": False},
                                         "cfd": {"status": "ok", "duration_s": 300.0, "cached": False}}},
            {"run_id": "c", "analyses": {"mass": {"status": "ok", "duration_s": 99.0, "cached": True},
                                         "cfd": {"status": "error", "duration_s": 5.0, "cached": False}}}]
    (d / "results" / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    assert len(pipeline.run_history(d)) == 3
    assert pipeline.typical_durations(d) == {"mass": 2.0, "cfd": 200.0}
    assert pipeline.run_history(tmp_path / "nothing") == []
    from opencae.web.server import JobManager
    jm = JobManager(d)
    jm.progress["running"] = True
    jm.on_event({"kind": "run_start", "run_id": "r", "t": time.time(), "frac": 0,
                 "plan": [{"id": "mass", "title": "Mass"}, {"id": "cfd", "title": "CFD"},
                          {"id": "flight", "title": "Flight"}]})
    assert jm.snapshot()["eta_s"] is None  # flight has no history: "first run, no estimate yet"
    jm.on_event({"kind": "analysis_done", "id": "flight", "status": "ok", "duration_s": 1, "t": time.time()})
    jm.on_event({"kind": "analysis_start", "id": "mass", "t": time.time()})
    snap = jm.snapshot()
    assert snap["analyses"]["mass"]["state"] == "running" and snap["analyses"]["cfd"]["state"] == "waiting"
    assert snap["analyses"]["structure"]["state"] == "skipped"
    assert 197 < snap["eta_s"] <= 202  # ~2 s left of mass + 200 s of cfd


# ------------------------------------------------------------------------------------------------ server
def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def test_server_progress_has_states_and_history(proj):
    from opencae.web.server import DashboardServer
    s = DashboardServer(proj)
    s.start()
    try:
        assert _get(s.url + "api/history") == []
        req = urllib.request.Request(s.url + "api/run", json.dumps({"only": ["mass", "propulsion"]}).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req).read()
        s.jobs.join(120)
        p = _get(s.url + "api/progress")
        assert not p["running"] and not p["error"], p
        assert p["run_id"] and p["eta_s"] is None
        assert p["analyses"]["mass"]["state"] == "done" and p["analyses"]["propulsion"]["state"] == "done"
        assert p["analyses"]["cfd"]["state"] == "skipped" and p["analyses"]["mass"]["duration_s"] >= 0
        kinds = [e["kind"] for e in p["events"]]
        assert kinds[0] == "run_start" and kinds[-1] == "run_done" and p["frac"] == 1.0
        h = _get(s.url + "api/history")
        assert len(h) == 1 and h[0]["run_id"] == p["run_id"] and h[0]["status"] == "done"
    finally:
        s.stop()


# ------------------------------------------------------------------------------------------------ Qt dock
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


def _fake_progress(now):
    return {"running": True, "kind": "run", "step": "Propulsion: thrust", "frac": 0.4, "log": ["a", "b"],
            "error": "", "started": now - 65, "finished": None, "run_id": "r1", "eta_s": 125.0, "events": [],
            "analyses": {
                "mass": {"title": "Mass", "state": "done", "status": "ok", "started": now - 65, "duration_s": 0.4},
                "propulsion": {"title": "Propulsion", "state": "running", "status": None, "started": now - 5,
                               "duration_s": None},
                "structure": {"title": "Structure", "state": "waiting", "status": None, "started": None,
                              "duration_s": None},
                "cfd": {"title": "CFD", "state": "skipped", "status": None, "started": None, "duration_s": None},
                "flight": {"title": "Flight", "state": "cached", "status": "ok", "started": None,
                           "duration_s": 0.0}}}


def test_tracker_installs_and_updates(win):
    from opencae.ui.drone import tracker
    t = win.extensions["opencae.ui.drone.tracker"]
    assert isinstance(t, tracker.RunTracker)
    assert win.drone_workspace.__self__ is win and win._drone is None  # install must not create the workspace
    now = time.time()
    t.apply_progress(_fake_progress(now))
    assert t.dock.isVisible()
    assert t.bar.value() == 400 and "40" in t.bar.format()
    states = {a: t.row_state(a) for a in ("mass", "propulsion", "structure", "cfd", "flight")}
    assert states == {"mass": "done", "propulsion": "running", "structure": "waiting", "cfd": "skipped",
                      "flight": "cached"}
    assert "Propulsion" in t.step_label.text()
    assert "2 min 5 s" in t.eta_label.text() or "2:05" in t.eta_label.text()
    t.apply_progress({"running": False, "error": "", "frac": 1.0, "analyses": _fake_progress(now)["analyses"],
                      "started": now - 348, "finished": now, "step": "Done", "log": [],
                      "result": {"verdict": "conditional"}, "run_id": "r1", "eta_s": None})
    assert "5 min 48 s" in win.statusBar().currentMessage() and "conditional" in win.statusBar().currentMessage()


def test_tracker_history_rows(win, tmp_path):
    t = win.extensions["opencae.ui.drone.tracker"]
    rows = [{"run_id": "a", "variant": "baseline", "started": "2026-11-23 10:00:00", "duration_s": 348.0,
             "only": [], "verdict": "conditional", "status": "done",
             "analyses": {"mass": {"status": "ok", "duration_s": 1, "cached": False},
                          "cfd": {"status": "ok", "duration_s": 300, "cached": False}}},
            {"run_id": "b", "variant": "v2", "started": "2026-11-23 11:00:00", "duration_s": 3.0,
             "only": ["mass"], "verdict": None, "status": "cancelled", "analyses": {"mass": {"status": "ok",
                                                                                              "duration_s": 1,
                                                                                              "cached": True}}}]
    t.set_history(rows)
    assert t.history.rowCount() == 2
    first = [t.history.item(0, c).text() for c in range(t.history.columnCount())]
    assert "baseline" in " ".join(first) or "v2" in " ".join(first)
    txt = " ".join(t.history.item(r, c).text() for r in range(2) for c in range(t.history.columnCount()))
    assert "5 min 48 s" in txt and "conditional" in txt and "cancelled" in txt.lower()


def test_tracker_follows_real_workspace(win, proj):
    t = win.extensions["opencae.ui.drone.tracker"]
    assert win.open_drone_project(proj)
    ws = win.drone_workspace()
    ws.server.run({"only": ["mass", "propulsion"]})
    t.refresh()
    assert t.dock.isVisible()
    ws.server.jobs.join(120)
    t.refresh()
    assert t.row_state("mass") == "done" and t.row_state("propulsion") == "done"
    assert t.history.rowCount() == 1
