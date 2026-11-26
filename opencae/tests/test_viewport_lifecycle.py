"""Viewport lifecycle regression tests (review R4, blocker B1).

Root cause of B1: without ``Qt.AA_ShareOpenGLContexts`` (an application or test that made its own QApplication),
Qt 6 builds the viewport's ``QOpenGLWidget`` context while the window is still hidden, then on the Show event
destroys it and makes a new one (the "did grabFramebuffer() for a hidden widget" path of ``QOpenGLWidget::event``).
pyvistaqt's ``aboutToBeDestroyed`` handler removes the render-window observers that make VTK's context current and
never adds them back, so every later render ran with no current GL context: "Could not create shader object",
then a segfault inside ``render()``. The same happens whenever Qt re-creates the context (reparenting to another
window). ``opencae.ui.viewport`` puts the observers back when the context is re-created.

Each scenario runs in a child process, so a crash is a failed test and not a crashed test session.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyvistaqt")

pytestmark = pytest.mark.gui

ROOT = Path(__file__).resolve().parents[2]

PRELUDE = textwrap.dedent("""
    import gc, os, sys, tempfile, time
    from pathlib import Path
    from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    tmp = tempfile.mkdtemp()
    os.environ["OPENCAE_HOME"] = tmp + "/home"
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Discard)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    if SHARE:
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication([])
    from opencae.ui.theme import apply_theme
    apply_theme(app)
    from opencae.ui import assembly
    from opencae.ui.main_window import MainWindow

    def pump(sec=0.3):
        t = time.time()
        while time.time() - t < sec:
            app.processEvents()
            time.sleep(0.01)

    def wait_for(cond, timeout=120):
        t = time.time()
        while not cond() and time.time() - t < timeout:
            app.processEvents()
            time.sleep(0.02)
        assert cond(), "timed out"

    def make(name):
        w = MainWindow(settings=QSettings(f"{tmp}/{name}.ini", QSettings.IniFormat))
        w.resize(1500, 920)
        w.show()
        pump()
        return w

    def dispose(w):
        w.dirty = False
        w.close()
        w.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
        gc.collect()

    def studio_shows_bodies(w, n):
        names = [k for k in w.viewport.plotter.actors if k.startswith("g_") and k.endswith("_faces")]
        assert len(names) >= n, names
        w.viewport.plotter.render()
""")

SCENARIOS = {
    # the reviewer's minimal pair: the Motion page of one window, then the gripper in the next window's Studio
    "two_windows": """
        w1 = make("a")
        w1.extensions[assembly.KEY].workspace()
        pump()
        dispose(w1)
        w2 = make("b")
        w2.extensions["opencae.ui.console"].execute("open gripper")
        pump(0.5)
        studio_shows_bodies(w2, 3)
        dispose(w2)
    """,
    # one window, the way a user works: Motion + gripper, run, play, Studio, open again two ways, Motion, close
    "one_window_real_sequence": """
        w = make("a")
        ext = w.extensions[assembly.KEY]
        ext.show_mode()
        ws = ext.workspace()
        assert ws.open_example("gripper")
        pump()
        job = ws.run_study()
        assert job is not None
        wait_for(lambda: not ws.running)
        assert ws.result is not None
        ws.playback.set_speed(4.0)
        ws.playback.play()
        pump(0.5)
        ws.playback.pause()
        ws.playback.set_time(0.5)
        w.ribbon.setCurrentIndex(0)
        assert w.stack.currentWidget() is w.viewport
        pump()
        w.extensions["opencae.ui.console"].execute("open gripper")
        pump(0.5)
        w.ribbon.setCurrentIndex(0)
        w.stack.setCurrentWidget(w.viewport)
        pump()
        studio_shows_bodies(w, 3)
        path = str(w.session.path)
        QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (path, ""))
        w.cmd_open()                                   # File > Open
        pump(0.5)
        studio_shows_bodies(w, 3)
        ext.show_mode()                                # back to Motion
        pump()
        assert w.stack.currentWidget() is ws
        ws.viewport.plotter.render()
        dispose(w)
        w2 = make("b")                                 # and a fresh window after all that
        w2.extensions["opencae.ui.console"].execute("open gripper")
        pump(0.5)
        studio_shows_bodies(w2, 3)
        dispose(w2)
    """,
}


def _run(scenario: str, share: bool) -> subprocess.CompletedProcess:
    code = f"SHARE = {share!r}\n" + PRELUDE + textwrap.dedent(SCENARIOS[scenario]) + "\nprint('SCENARIO-OK')\n"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env["PYTHONFAULTHANDLER"] = "1"
    return subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True,
                          timeout=600)


@pytest.mark.parametrize("share", [False, True], ids=["own_qapplication", "app_like_shared_contexts"])
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_viewport_survives_window_and_mode_sequence(scenario, share):
    r = _run(scenario, share)
    tail = "\n".join((r.stdout + r.stderr).splitlines()[-40:])
    assert r.returncode == 0, f"exit {r.returncode} (139/-11 = segfault)\n{tail}"
    assert "SCENARIO-OK" in r.stdout, tail
    assert "Could not create shader object" not in r.stderr, tail


@pytest.mark.parametrize("share", [False, True], ids=["own_qapplication", "app_like_shared_contexts"])
def test_render_window_observers_survive_context_recreation(share):
    """The direct check: after Qt has re-created the viewport's GL context the render window still has the
    make-current observer, and the context VTK renders with is the widget's own."""
    code = f"SHARE = {share!r}\n" + PRELUDE + textwrap.dedent("""
        from PySide6.QtGui import QOpenGLContext
        w = make("a")
        it = w.viewport.plotter
        rw = it.render_window
        assert rw.HasObserver("WindowMakeCurrentEvent"), "make-current observer lost"
        assert rw.HasObserver("WindowIsCurrentEvent"), "is-current observer lost"
        it.render()
        rw.MakeCurrent()
        assert it._ctx is not None and QOpenGLContext.currentContext() is it._ctx
        dispose(w)
        print("SCENARIO-OK")
    """)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True,
                       timeout=300)
    tail = "\n".join((r.stdout + r.stderr).splitlines()[-30:])
    assert r.returncode == 0 and "SCENARIO-OK" in r.stdout, f"exit {r.returncode}\n{tail}"
