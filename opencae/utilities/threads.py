"""Background Qt threads are stopped before the application quits.

Qt aborts the whole process ("QThread: Destroyed while thread is still running") when a running QThread is
destroyed, which is what happens at exit if a worker (the CalculiX self-test that starts with the main window, a
K09-L analysis in the Design Monitor) is still busy. Every such thread is registered here; ``shutdown_all`` asks
each to stop, waits, and only as a last resort terminates it, so quitting never aborts. It runs on
``QApplication.aboutToQuit`` and at interpreter exit.
"""
from __future__ import annotations

import atexit
import logging
from typing import Callable, Optional

log = logging.getLogger("opencae.threads")
_threads: list = []  # [(QThread, cancel callback or None)]
_installed = False


def track(thread, cancel: Optional[Callable[[], None]] = None):
    """Register a QThread (and an optional callback that asks its work to stop). Returns the thread."""
    _install()
    _threads.append((thread, cancel))
    try:
        thread.finished.connect(lambda t=thread: _forget(t))
    except (RuntimeError, AttributeError):
        pass
    return thread


def _forget(thread) -> None:
    _threads[:] = [(t, c) for t, c in _threads if t is not thread]


def running() -> list:
    out = []
    for t, _ in list(_threads):
        try:
            if t.isRunning():
                out.append(t)
        except RuntimeError:  # the C++ object is already gone
            _forget(t)
    return out


def shutdown_all(timeout_ms: int = 20000) -> int:
    """Stop every registered thread that still runs. Returns how many had to be terminated (0 normally)."""
    forced = 0
    for t, cancel in list(_threads):
        try:
            if not t.isRunning():
                continue
            log.info("Waiting for a background task to finish before quitting")
            if cancel:
                try:
                    cancel()
                except Exception:  # noqa: BLE001
                    log.exception("cancel callback failed")
            t.quit()
            if not t.wait(timeout_ms):
                log.error("A background task did not stop within %.0f s; terminating it", timeout_ms / 1000)
                t.terminate()
                t.wait(3000)
                forced += 1
        except RuntimeError:
            pass
    _threads.clear()
    return forced


def _install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    atexit.register(shutdown_all)
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(shutdown_all)
    except Exception:  # noqa: BLE001 - no Qt: atexit still covers it
        pass
