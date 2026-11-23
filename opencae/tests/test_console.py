"""Command console dock: commands, Python mode, history, completion, a real run through server.jobs. Offscreen."""
import json
import shutil
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui
SHOT = "/tmp/claude-0/-home-claude/cca99fdc-b1aa-5a4c-a668-10c4bdf36bbb/scratchpad/console.png"


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
    src = Path(__file__).resolve().parents[1] / "examples" / "drones" / "quad250"
    dst = tmp_path / "quad250"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("results", "__pycache__"))
    return dst


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
def con(win):
    c = win.extensions["opencae.ui.console"]
    return c


def spec_of(project):
    return json.loads((project / "drone.json").read_text())


def wait(app, cond, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_install_adds_dock_and_help(win, con):
    from PySide6.QtWidgets import QDockWidget
    docks = [d for d in win.findChildren(QDockWidget) if d.windowTitle() == "Command console"]
    assert len(docks) == 1
    con.execute("help")
    for c in ("open", "run", "set", "results", "iterate", "doctor"):
        assert c in con.text()
    con.execute("runn")
    assert "Did you mean" in con.text() and "run" in con.text()
    con.toggle_dock()
    assert con.dock.isVisible()


def test_open_set_get(win, con, project):
    con.execute(f"open {project}")
    assert win.mode == "drone" and "Opened" in con.text()
    con.execute("set propulsion.battery.capacity_mah 2200")
    assert "saved" in con.text()
    from opencae.drone import spec as S
    assert S.param(spec_of(project), "propulsion.battery.capacity_mah").value == 2200
    before = (project / "drone.json").read_bytes()
    con.execute("set propulsion.nonsense 5")
    assert "Not changed" in con.text()
    con.execute("set propulsion.battery.cells abc")
    assert (project / "drone.json").read_bytes() == before
    con.execute("get propulsion.battery.capacity_mah")
    assert "2200" in con.text()
    con.execute("set propulsion.battery.capacity_mah 2300 datasheet")
    assert S.param(spec_of(project), "propulsion.battery.capacity_mah").status == "datasheet"
    con.execute("params battery")
    assert "propulsion.battery" in con.text()


def test_completion_and_history(win, con, project, tmp_path):
    con.execute(f"open {project}")
    assert "help" in con.completions("he")
    keys = con.completions("set propulsion.battery.")
    assert "propulsion.battery.capacity_mah" in keys
    assert "propulsion" in con.completions("run pro")
    assert con.completions("example q") == ["quad250"]
    con.input.setText("set propulsion.battery.cap")
    con.tab_complete()
    assert con.input.text().startswith("set propulsion.battery.capacity_mah")
    con.execute("status")
    hist = (tmp_path / "home" / "console_history.txt").read_text().splitlines()
    assert hist[-1] == "status" and f"open {project}" in hist
    con.input.clear()
    con.history_step(-1)
    assert con.input.text() == "status"


def test_python_mode(win, con, project):
    con.execute(">>> 1+1")
    assert "2" in con.text().splitlines()[-1]
    con.execute(f"open {project}")
    con.execute('>>> spec["name"]')
    assert "'quad250'" in con.text()
    con.execute(">>> 1/0")
    assert "ZeroDivisionError" in con.text() and "Traceback" in con.text()
    con.execute(">>> x = 5")
    con.execute(">>> x * 2")
    assert "10" in con.text().splitlines()[-1]
    con.execute(">>> for i in range(2):")
    con.execute("    print('row', i)")
    con.execute("")
    assert "row 1" in con.text()
    con.run_block(">>> a = 3\nb = a + 4\nb")
    assert "7" in con.text().splitlines()[-1]
    con.set_python_mode(True)
    con.execute("project.name")
    assert "quad250" in con.text()
    con.execute("!help")
    con.set_python_mode(False)
    con.execute("get name")
    assert "quad250" in con.text()


def test_run_through_jobs_and_results(app, win, con, project):
    con.execute(f"open {project}")
    con.execute("run mass propulsion")
    assert "Run started" in con.text()
    assert wait(app, lambda: not con.busy), "run did not finish"
    assert "Run finished" in con.text() or "Run ended" in con.text()
    assert not win.drone_workspace().server.jobs.running
    con.execute("results propulsion")
    assert "tw_ratio" in con.text()
    con.execute("checks")
    assert "Verdict" in con.text()
    con.execute("variants")
    assert "baseline" in con.text()
    con.execute("status")
    assert "propulsion" in con.text()
    con.output.clear()
    win.resize(1200, 800)
    con.execute("help")
    con.execute("results propulsion")
    con.execute(">>> results['verdict']")
    con.dock.show()
    app.processEvents()
    con.dock.grab().save(SHOT)
