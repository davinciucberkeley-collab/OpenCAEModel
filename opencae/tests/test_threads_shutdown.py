"""Quitting never aborts: background QThreads are stopped and waited for (opencae.utilities.threads)."""
import subprocess
import sys
import textwrap


def test_running_qthread_at_exit_does_not_abort(tmp_path):
    # without the guard Qt aborts with "QThread: Destroyed while thread is still running" (exit code 134 / -6)
    code = textwrap.dedent("""
        import time
        from PySide6.QtCore import QObject, QThread
        from PySide6.QtWidgets import QApplication
        from opencae.utilities import threads
        app = QApplication([])
        class W(QObject):
            def run(self):
                time.sleep(1.0)
        th = QThread(app); w = W(); w.moveToThread(th); th.started.connect(w.run); th._w = w
        threads.track(th); th.start()
        time.sleep(0.1)
        print("exiting while the thread runs")
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                       env={**__import__("os").environ, "QT_QPA_PLATFORM": "offscreen"})
    assert r.returncode == 0, r.stderr[-2000:]
    assert "Destroyed while thread" not in r.stderr


def test_shutdown_all_calls_cancel_and_waits():
    from PySide6.QtCore import QObject, QThread
    from PySide6.QtWidgets import QApplication
    from opencae.utilities import threads
    app = QApplication.instance() or QApplication([])
    flag = {"stop": False}

    class W(QObject):
        def run(self):
            import time
            while not flag["stop"]:
                time.sleep(0.01)

    th = QThread()
    w = W()
    w.moveToThread(th)
    th.started.connect(w.run)
    threads.track(th, cancel=lambda: flag.__setitem__("stop", True))
    th.start()
    assert threads.shutdown_all(timeout_ms=5000) == 0
    assert not th.isRunning() and flag["stop"]
