"""Crash guard: sentinel, safe mode, hooks, Qt message handler, --diagnose, faulthandler."""
import faulthandler
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from opencae.utilities import crash

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCAE_HOME", str(tmp_path))
    monkeypatch.delenv("OPENCAE_SAFE_MODE", raising=False)
    monkeypatch.delenv("OPENCAE_NO_WEBVIEW", raising=False)
    yield tmp_path
    crash.uninstall()


def test_stale_sentinel_enables_safe_mode(home):
    (home / "session.lock").write_text(json.dumps({"pid": 2 ** 22 + 12345, "started": "x"}))
    assert crash.install() is True
    assert crash.safe_mode() and not crash.web_view_allowed()
    assert os.environ["OPENCAE_SAFE_MODE"] == "1"
    note = crash.safe_mode_notice()
    assert note and "safe mode" in note and "crash report" in note
    assert crash.safe_mode_notice() is None  # one time only
    crash.clear_safe_mode()
    assert not crash.safe_mode()


def test_clean_start_and_stop_removes_sentinel(home):
    assert crash.install() is False
    assert (home / "session.lock").exists()
    assert not crash.safe_mode()
    crash.end_session()
    assert not (home / "session.lock").exists()


def test_force_safe_and_no_webview_env(home, monkeypatch):
    crash.install(force_safe=True)
    assert crash.safe_mode() and not crash.web_view_allowed()
    crash.uninstall()
    monkeypatch.setenv("OPENCAE_NO_WEBVIEW", "1")
    crash.install()
    assert not crash.web_view_allowed()


def test_faulthandler_file_opened(home):
    crash.install()
    assert faulthandler.is_enabled()
    text = (home / "logs" / "crash.log").read_text()
    assert text.startswith(crash.HEADER) and "python=" in text and "numpy=" in text
    assert crash.last_crash_report() is None


def test_excepthook_logs_and_does_not_raise(home, caplog):
    crash.install()
    caplog.set_level(logging.ERROR)
    try:
        raise ValueError("boom-123")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    assert "boom-123" in (home / "logs" / "crash.log").read_text()
    assert "boom-123" in caplog.text
    assert crash.last_crash_report() is not None

    def worker():
        raise RuntimeError("thread-boom")
    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert "thread-boom" in (home / "logs" / "crash.log").read_text()


def test_dialog_rate_limit(home):
    crash.install()
    assert crash._should_show_dialog(100.0) is True
    assert crash._should_show_dialog(102.0) is False
    assert crash._should_show_dialog(104.9) is False
    assert crash._should_show_dialog(105.1) is True


def test_excepthook_shows_dialog_once_per_interval(home, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    crash.install()
    shown = []
    monkeypatch.setattr(crash, "_show_dialog", lambda d: shown.append(d))
    for _ in range(5):
        try:
            raise KeyError("repeat")
        except KeyError:
            sys.excepthook(*sys.exc_info())
    assert len(shown) == 1 and "repeat" in shown[0]
    assert app is not None


def test_qt_warning_reaches_log(home, caplog):
    from PySide6 import QtCore
    crash.install()
    caplog.set_level(logging.INFO)
    QtCore.qWarning("qt-warning-xyz")
    assert "qt-warning-xyz" in caplog.text


def test_message_dialog_builds_offscreen(home):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    crash._show_dialog("details here")
    app.processEvents()
    box = crash._state.get("open_box")
    assert box is not None and box.detailedText() == "details here"
    box.close()


def test_diagnose_subprocess(tmp_path):
    env = dict(os.environ, OPENCAE_HOME=str(tmp_path), PYTHONPATH=str(REPO), QT_QPA_PLATFORM="offscreen")
    r = subprocess.run([sys.executable, "-m", "opencae.app.main", "--diagnose"], env=env, cwd=REPO,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    start = r.stdout.index("{")
    data = json.loads(r.stdout[start:])
    for k in ("python", "pyside6", "webengine", "ccx", "openfoam", "logs", "safe_mode"):
        assert k in data
    assert not (tmp_path / "session.lock").exists()


def test_qt_guard_always_reserves_on_known_pair(monkeypatch):
    from opencae.utilities import qt_guard
    monkeypatch.setattr(qt_guard, "_done", False)
    monkeypatch.setattr(qt_guard, "_known_affected", lambda: True)
    called = []
    monkeypatch.setattr(qt_guard, "_apply_reserve", lambda why: called.append(why) or True)
    assert qt_guard.apply_reserve_early() is True and called


def test_normal_start_leaves_chromium_flags_alone_off_windows(home, monkeypatch):
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)
    monkeypatch.setattr(crash.sys, "platform", "linux")
    crash.install()
    crash.prepare_qt_environment()
    assert "QTWEBENGINE_CHROMIUM_FLAGS" not in os.environ


def test_windows_dashboard_renders_in_software(home, monkeypatch):
    """User's PC: Chromium's Direct3D (ANGLE) device was lost inside the OpenGL app window ('The D3D11 device was
    removed', EGL_CONTEXT_LOST). On Windows the embedded dashboard renders in software with software WebGL."""
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)
    monkeypatch.setattr(crash.sys, "platform", "win32")
    crash.install()
    crash.prepare_qt_environment()
    flags = os.environ["QTWEBENGINE_CHROMIUM_FLAGS"].split()
    assert "--disable-gpu" in flags and "--enable-unsafe-swiftshader" in flags
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "--mine")
    crash.prepare_qt_environment()
    assert os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] == "--mine"  # the user's own flags always win


def test_safe_mode_sets_gpu_flags_only_when_unset(home, monkeypatch):
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)
    monkeypatch.setattr(crash.sys, "platform", "linux")
    crash.install(force_safe=True)
    crash.prepare_qt_environment()
    assert os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] == "--disable-gpu --disable-gpu-compositing"
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "--mine")
    crash.prepare_qt_environment()
    assert os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] == "--mine"


def test_prepare_environment_does_not_import_webengine(home):
    import subprocess
    import sys
    code = ("import sys\nfrom opencae.utilities import crash\ncrash.prepare_qt_environment()\n"
            "crash.web_view_allowed()\nprint('PySide6.QtWebEngineWidgets' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=dict(os.environ, OPENCAE_HOME=str(home)), timeout=60)
    assert out.stdout.strip() == "False", out.stderr


def test_leave_safe_mode_clears_sentinel_for_next_start(home):
    (home / "session.lock").write_text(json.dumps({"pid": 2 ** 22 + 12345, "started": "x"}))
    assert crash.install() is True
    crash._state["failed"] = True            # a failed start keeps the sentinel on exit...
    crash.clear_safe_mode()                  # ...unless the person leaves safe mode
    assert not crash.safe_mode()
    crash.end_session()
    assert not (home / "session.lock").exists()
    crash.uninstall()
    assert crash.install() is False and not crash.safe_mode()


def test_safe_mode_notice_does_not_claim_software_3d(home):
    (home / "session.lock").write_text(json.dumps({"pid": 2 ** 22 + 12345, "started": "x"}))
    crash.install()
    assert "software" not in crash.safe_mode_notice()


def test_quick_uses_opengl_like_the_vtk_viewport(monkeypatch):
    """The embedded dashboard (Qt Quick) and the VTK viewport must share OpenGL (user's Windows log: QQuickWidget
    'Failed to get a QRhi from the top-level widget's window')."""
    import subprocess, sys, os
    code = ("import os; from opencae.utilities import crash; crash.prepare_qt_environment(); "
            "from PySide6.QtQuick import QQuickWindow, QSGRendererInterface as R; "
            "print(os.environ['QSG_RHI_BACKEND'], QQuickWindow.graphicsApi() == R.GraphicsApi.OpenGL)")
    env = {k: v for k, v in os.environ.items() if k != "QSG_RHI_BACKEND"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env=env)
    assert out.stdout.split() == ["opengl", "True"], out.stderr[-1500:]


def test_reserve_covers_true_and_false():
    """PySide6 6.12 on Python 3.11 also drops True/False references on bool signal arguments (seen as a
    'bool_dealloc' abort at exit): the reserve protects None, True and False together."""
    import subprocess, sys, os
    code = ("import sys; from opencae.utilities import qt_guard; applied = qt_guard.apply_reserve_early(); "
            "print(applied, sys.getrefcount(True) > 1 << 39, sys.getrefcount(False) > 1 << 39, "
            "sys.getrefcount(None) > 1 << 39)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    words = out.stdout.split()
    if words and words[0] == "False":
        import pytest
        pytest.skip("this Python/PySide6 pair is not affected")
    assert words == ["True", "True", "True", "True"], out.stderr[-1000:]
