"""Crash guard: make every exit of the application diagnosable.

* ``faulthandler`` writes a stack trace to ``<home>/logs/crash.log`` on a hard crash.
* ``sys.excepthook`` / ``threading.excepthook`` log the traceback and show a non-fatal
  message box (rate limited); the application never exits because of them.
* Qt warnings, criticals and fatals are routed to ``opencae.log`` (fatal also to crash.log).
* A session sentinel ``<home>/session.lock`` is written at start and removed on a clean exit.
  A stale sentinel at the next start means the last run died; that run starts in *safe mode*:
  the embedded dashboard (QtWebEngine) is not used and software OpenGL is requested.

Public helpers for the UI: ``safe_mode()``, ``web_view_allowed()``, ``last_crash_report()``,
``clear_safe_mode()``.  ``<home>`` is ``$OPENCAE_HOME`` or ``~/.opencae``.
"""
from __future__ import annotations

import atexit
import faulthandler
import importlib.util
import json
import os
import platform
import sys
import threading
import time
import traceback
from pathlib import Path

from opencae.utilities.logging_setup import get_logger, log_dir, setup_logging

log = get_logger("crash")

HEADER = "=== OpenCAE session"
DIALOG_INTERVAL_S = 5.0
_GPU_FLAGS = "--disable-gpu --disable-gpu-compositing"

_state = {
    "installed": False, "safe": False, "stale": False, "stale_info": "", "notice_shown": False,
    "webengine_ok": None, "fault_file": None, "last_dialog": 0.0, "session": False, "failed": False,
    "prev_excepthook": None, "prev_threading_hook": None, "qt_handler": None, "notifier": None,
}


# ---------------------------------------------------------------- paths
def home_dir() -> Path:
    return Path(os.environ.get("OPENCAE_HOME") or (Path.home() / ".opencae"))


def crash_log_path() -> Path:
    return log_dir() / "crash.log"


def lock_path() -> Path:
    p = home_dir()
    p.mkdir(parents=True, exist_ok=True)
    return p / "session.lock"


def _append_crash(text: str) -> None:
    try:
        with open(crash_log_path(), "a", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
    except OSError:  # pragma: no cover
        pass


# ---------------------------------------------------------------- versions
def _version(dist: str) -> str:
    try:
        from importlib import metadata
        return metadata.version(dist)
    except Exception:
        return "not installed"


def versions() -> dict:
    return {"python": platform.python_version(), "pyside6": _version("PySide6"),
            "vtk": _version("vtk"), "numpy": _version("numpy"),
            "os": f"{platform.system()} {platform.release()} ({platform.version()})"}


def _header_line() -> str:
    v = versions()
    return (f"{HEADER} {time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} python={v['python']} "
            f"PySide6={v['pyside6']} VTK={v['vtk']} numpy={v['numpy']} os={v['os']} ===")


# ---------------------------------------------------------------- safe mode
def safe_mode() -> bool:
    return bool(_state["safe"]) or os.environ.get("OPENCAE_SAFE_MODE") == "1"


def clear_safe_mode() -> None:
    """Leave safe mode: also forget a failed start, so a clean exit removes the sentinel and the next start is normal."""
    _state["safe"] = False
    _state["failed"] = False
    os.environ.pop("OPENCAE_SAFE_MODE", None)


def webengine_importable() -> bool:
    """Whether QtWebEngine is installed. Looks only; nothing is imported until Drone mode creates its view."""
    if _state["webengine_ok"] is None:
        try:
            _state["webengine_ok"] = importlib.util.find_spec("PySide6.QtWebEngineWidgets") is not None
        except Exception:
            _state["webengine_ok"] = False
    return bool(_state["webengine_ok"])


def web_view_allowed() -> bool:
    """True when the embedded dashboard may be used (not safe mode, not disabled, importable)."""
    if safe_mode() or os.environ.get("OPENCAE_NO_WEBVIEW") == "1":
        return False
    return webengine_importable()


def last_crash_report() -> str | None:
    """Path of crash.log when it holds anything besides session header lines, else None."""
    p = crash_log_path()
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip() and not line.startswith(HEADER):
                return str(p)
    except OSError:
        pass
    return None


def last_crash_summary(lines: int = 12) -> str:
    p = last_crash_report()
    if not p:
        return ""
    try:
        body = [l for l in Path(p).read_text(encoding="utf-8", errors="replace").splitlines()
                if l.strip() and not l.startswith(HEADER)]
        return "\n".join(body[-lines:])
    except OSError:
        return ""


def safe_mode_notice() -> str | None:
    """One-time friendly message when this run is in safe mode because the last run died."""
    if not _state["stale"] or _state["notice_shown"]:
        return None
    _state["notice_shown"] = True
    rep = last_crash_report() or str(crash_log_path())
    return ("OpenCAE closed unexpectedly last time. It started in safe mode: the embedded dashboard "
            "opens in your browser instead. "
            f"The crash report is at {rep}")


# ---------------------------------------------------------------- sentinel
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def start_session() -> bool:
    """Write the sentinel. Returns True when a stale one (previous run died) was found."""
    p = lock_path()
    stale = False
    try:
        if p.exists():
            try:
                info = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                info = {}
            pid = int(info.get("pid", 0) or 0)
            if pid != os.getpid() and _pid_alive(pid):
                log.info("Another OpenCAE instance (pid %s) is running; session sentinel left alone.", pid)
                return False
            stale = True
            _state["stale_info"] = f"pid {pid} started {info.get('started', '?')}"
        p.write_text(json.dumps({"pid": os.getpid(), "started": time.strftime("%Y-%m-%d %H:%M:%S")}),
                     encoding="utf-8")
        _state["session"] = True
    except OSError as exc:  # pragma: no cover
        log.warning("Could not write the session sentinel: %s", exc)
    return stale


def end_session() -> None:
    """Remove the sentinel (clean exit). A failed startup keeps it so the next run is in safe mode."""
    if not _state["session"] or _state["failed"]:
        return
    try:
        p = lock_path()
        info = json.loads(p.read_text(encoding="utf-8"))
        if int(info.get("pid", 0)) == os.getpid():
            p.unlink()
    except (OSError, ValueError):
        pass
    _state["session"] = False
    log.info("Clean exit.")


# ---------------------------------------------------------------- error reporting
def _summary(exc_type, exc, tb) -> str:
    return "".join(traceback.format_exception(exc_type, exc, tb))


def _record(kind: str, text: str) -> None:
    log.error("%s\n%s", kind, text)
    _append_crash(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {kind}\n{text}")


def _should_show_dialog(now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    if now - _state["last_dialog"] >= DIALOG_INTERVAL_S:
        _state["last_dialog"] = now
        return True
    return False


def _make_notifier():
    from PySide6.QtCore import QObject, Qt, Signal

    class Notifier(QObject):
        raised = Signal(str)

        def __init__(self):
            super().__init__()
            self.raised.connect(_show_dialog, Qt.QueuedConnection)

    return Notifier()


def _show_dialog(details: str) -> None:
    """Non-blocking 'something went wrong' box with a Copy details button."""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance() is None:
            return
        box = QMessageBox()
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("OpenCAE Studio")
        box.setText("Something went wrong - the app keeps running.")
        box.setInformativeText(f"Details were saved to {crash_log_path()}")
        box.setDetailedText(details)
        copy = box.addButton("Copy details", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Ok)
        copy.clicked.connect(lambda *_: QApplication.clipboard().setText(details))
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.setModal(False)
        box.show()
        _state["open_box"] = box  # keep alive until closed
        box.finished.connect(lambda *_: _state.pop("open_box", None))
    except Exception:  # never let reporting raise
        log.exception("could not show the error dialog")


def _notify(details: str) -> None:
    if not _should_show_dialog():
        return
    try:
        from PySide6.QtWidgets import QApplication
        if QApplication.instance() is None:
            return
        if threading.current_thread() is threading.main_thread():
            _show_dialog(details)
        else:
            if _state["notifier"] is None:
                return
            _state["notifier"].raised.emit(details)
    except Exception:
        pass


def excepthook(exc_type, exc, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    text = _summary(exc_type, exc, tb)
    try:
        _record("Unhandled exception", text)
        _notify(text)
    except Exception:
        pass


def threading_excepthook(args) -> None:
    if args.exc_type is SystemExit:
        return
    name = getattr(args.thread, "name", "?")
    text = _summary(args.exc_type, args.exc_value, args.exc_traceback)
    try:
        _record(f"Unhandled exception in thread {name}", text)
        _notify(text)
    except Exception:
        pass


_QT_LEVELS = {"QtDebugMsg": 10, "QtInfoMsg": 20, "QtWarningMsg": 30, "QtCriticalMsg": 40, "QtFatalMsg": 50}


def qt_message_handler(mode, context, message) -> None:
    import logging
    level = _QT_LEVELS.get(getattr(mode, "name", str(mode)), 30)
    where = ""
    try:
        if context is not None and context.file:
            where = f" ({context.file}:{context.line})"
    except Exception:
        pass
    log.log(level, "Qt: %s%s", message, where)
    if level >= logging.CRITICAL:
        _append_crash(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} Qt fatal: {message}{where}")
        try:
            faulthandler.dump_traceback(file=_state["fault_file"] or sys.stderr)
        except Exception:
            pass


# ---------------------------------------------------------------- install
def install(force_safe: bool = False) -> bool:
    """Call as early as possible (before QApplication). Returns True when this run is in safe mode."""
    if _state["installed"]:
        if force_safe:
            _state["safe"] = True
            os.environ["OPENCAE_SAFE_MODE"] = "1"
        return safe_mode()
    setup_logging()
    try:
        fh = open(crash_log_path(), "a", encoding="utf-8", buffering=1)
        fh.write(_header_line() + "\n")
        fh.flush()
        faulthandler.enable(file=fh, all_threads=True)
        _state["fault_file"] = fh
    except OSError as exc:  # pragma: no cover
        log.warning("faulthandler could not be enabled: %s", exc)
    _state["prev_excepthook"] = sys.excepthook
    _state["prev_threading_hook"] = threading.excepthook
    sys.excepthook = excepthook
    threading.excepthook = threading_excepthook
    try:
        from PySide6.QtCore import qInstallMessageHandler
        _state["qt_handler"] = qt_message_handler
        qInstallMessageHandler(qt_message_handler)
    except Exception as exc:  # pragma: no cover
        log.warning("Qt message handler not installed: %s", exc)
    stale = start_session()
    _state["stale"] = stale
    if stale:
        _state["safe"] = True
        os.environ["OPENCAE_SAFE_MODE"] = "1"
        log.warning("The previous OpenCAE run did not exit cleanly (%s); starting in safe mode. "
                    "Crash report: %s", _state["stale_info"], crash_log_path())
        _append_crash(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} previous run did not exit cleanly "
                      f"({_state['stale_info']}); this run is in safe mode")
    if force_safe:
        _state["safe"] = True
        os.environ["OPENCAE_SAFE_MODE"] = "1"
        log.info("Safe mode forced (--safe).")
    atexit.register(end_session)
    _state["installed"] = True
    return safe_mode()


def uninstall() -> None:
    """Undo install() (tests): restore hooks, close the crash file, drop the sentinel."""
    if _state["prev_excepthook"] is not None:
        sys.excepthook = _state["prev_excepthook"]
    if _state["prev_threading_hook"] is not None:
        threading.excepthook = _state["prev_threading_hook"]
    try:
        from PySide6.QtCore import qInstallMessageHandler
        qInstallMessageHandler(None)
    except Exception:
        pass
    try:
        faulthandler.disable()
    except Exception:
        pass
    if _state["fault_file"] is not None:
        try:
            _state["fault_file"].close()
        except OSError:
            pass
    try:
        atexit.unregister(end_session)
    except Exception:
        pass
    end_session()
    _state.update(installed=False, safe=False, stale=False, stale_info="", notice_shown=False,
                  webengine_ok=None, fault_file=None, last_dialog=0.0, session=False, failed=False,
                  prev_excepthook=None, prev_threading_hook=None, qt_handler=None, notifier=None)
    os.environ.pop("OPENCAE_SAFE_MODE", None)


def prepare_qt_environment() -> None:
    """Before QApplication. Qt Quick (which draws the embedded dashboard) must use the same graphics API as the
    VTK 3D viewport, OpenGL: on Windows Qt Quick defaults to Direct3D, and the dashboard then cannot draw in the
    OpenGL window ("QQuickWidget: Failed to get a QRhi from the top-level widget's window"). A value the user
    set (QSG_RHI_BACKEND, QTWEBENGINE_CHROMIUM_FLAGS) is kept. Chromium GPU flags: see below (Windows, safe mode)."""
    if "QSG_RHI_BACKEND" not in os.environ:
        os.environ["QSG_RHI_BACKEND"] = "opengl"
    try:
        from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
        if os.environ.get("QSG_RHI_BACKEND", "").lower() == "opengl":
            QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.OpenGL)
    except Exception as exc:  # noqa: BLE001 - QtQuick missing: nothing to align
        log.info("Qt Quick graphics API not set: %s", exc)
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS")
    if flags is None and (safe_mode() or sys.platform == "win32"):
        # Windows: Chromium's GPU process renders with Direct3D (ANGLE) while the app window is OpenGL (VTK); the
        # two cannot share textures on many drivers and Chromium loses its device ("The D3D11 device was removed",
        # "eglCreateContext failed with error EGL_CONTEXT_LOST", seen on the user's PC). The embedded dashboard
        # therefore renders in software; its 3D view uses Chromium's software WebGL (SwiftShader), which Chromium
        # only enables on request. The page is OpenCAE's own local dashboard, never remote content.
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = _GPU_FLAGS + (" --enable-unsafe-swiftshader"
                                                                 if sys.platform == "win32" else "")
    if safe_mode() and sys.platform == "win32":
        os.environ.setdefault("QT_OPENGL", "software")


def attach_app(app) -> None:
    """After QApplication exists: clean-exit sentinel removal and thread-safe error dialogs."""
    app.aboutToQuit.connect(end_session)
    try:
        _state["notifier"] = _make_notifier()
    except Exception as exc:  # pragma: no cover
        log.warning("error notifier unavailable: %s", exc)


def report_startup_failure(exc: BaseException) -> None:
    """Log a startup exception, keep the sentinel (next run is safe mode), tell the user."""
    text = _summary(type(exc), exc, exc.__traceback__)
    _state["failed"] = True
    try:
        _record("Startup failure", text)
    except Exception:
        pass
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv[:1])
        box = QMessageBox(QMessageBox.Critical, "OpenCAE Studio",
                          f"OpenCAE could not start: {exc}\n\nDetails were saved to {crash_log_path()}")
        box.setDetailedText(text)
        copy = box.addButton("Copy details", QMessageBox.ActionRole)
        copy.clicked.connect(lambda *_: app.clipboard().setText(text))
        box.addButton(QMessageBox.Ok)
        box.exec()
    except Exception:
        print(text, file=sys.stderr)


# ---------------------------------------------------------------- diagnose
def diagnose() -> dict:
    v = versions()
    ccx = foam = None
    try:
        from opencae.solver.calculix.runner import find_ccx
        ccx = find_ccx()
    except Exception:
        pass
    try:
        from opencae.cfd.openfoam.install import find_openfoam
        inst = find_openfoam()
        foam = str(getattr(inst, "root", None) or inst) if inst else None
    except Exception:
        pass
    try:
        web = importlib.util.find_spec("PySide6.QtWebEngineWidgets") is not None
    except Exception:
        web = False
    return {"python": v["python"], "pyside6": v["pyside6"], "vtk": v["vtk"], "numpy": v["numpy"],
            "os": v["os"], "webengine": web, "ccx": ccx, "openfoam": foam,
            "logs": {"dir": str(log_dir()), "opencae_log": str(log_dir() / "opencae.log"),
                     "crash_log": str(crash_log_path())},
            "safe_mode": safe_mode(), "stale_session": lock_path().exists(),
            "last_crash": last_crash_summary() or None}
