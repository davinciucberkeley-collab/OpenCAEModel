"""Guard against a reference-counting defect in some PySide6 builds on Python < 3.12.

Observed with PySide6 6.12.0 on CPython 3.11: every call to a Qt method that returns
nothing (``QLabel.setText``, ``QApplication.processEvents``, ...) removes one reference
from ``None``. Python 3.12+ makes ``None`` immortal, so there it is harmless; on 3.11 the
count eventually reaches zero and the interpreter aborts ("none_dealloc: deallocating
None"), typically after a long GUI session or at exit.

``apply_reserve_early()`` always adds the reserve for PySide6 6.12.x on Python < 3.12 (the probe can
miss paths such as QtWebEngine or QTimer callbacks). ``check_and_guard()`` measures the effect with a probe widget. Only if references are
being lost does it add a large reserve of references to ``None`` (which only means that
``None`` is never freed, exactly as on Python 3.12+) and log what it did. Nothing is
changed on Python 3.12+ or with unaffected PySide6 builds.
"""
from __future__ import annotations

import ctypes
import gc
import sys

from opencae.utilities.logging_setup import get_logger

log = get_logger(__name__)
_RESERVE = 1 << 40
_done = False


def none_refs_lost_per_call(n: int = 200) -> float:
    """References of None lost per void Qt call (0 when unaffected). Needs a QApplication."""
    from PySide6.QtWidgets import QLabel
    lab = QLabel()
    gc.collect()
    r0 = sys.getrefcount(None)
    for _ in range(n):
        lab.setText("")
    gc.collect()
    lost = r0 - sys.getrefcount(None)
    lab.deleteLater()
    return max(0.0, lost / n)


def _apply_reserve(why: str) -> bool:
    """Add the None reference reserve (CPython only, once). Returns True when applied now."""
    global _done
    if _done or sys.implementation.name != "cpython":
        return False
    # ob_refcnt is the first field of every CPython object
    ctypes.c_ssize_t.from_address(id(None)).value += _RESERVE
    _done = True
    log.warning("None reference reserve applied (%s) on Python %s; the interpreter cannot abort from the "
                "PySide6 reference-count defect. Python >= 3.12 avoids it.", why, sys.version.split()[0])
    return True


def _known_affected() -> bool:
    if sys.version_info >= (3, 12):
        return False
    try:
        from importlib import metadata
        return metadata.version("PySide6").startswith("6.12.")
    except Exception:
        return False


def apply_reserve_early() -> bool:
    """Apply the reserve before any Qt object exists, for the known-bad pair (Python < 3.12 with
    PySide6 6.12.x). The probe in check_and_guard() can miss paths (QtWebEngine, QTimer callbacks),
    so for this pair the reserve is always applied."""
    if _known_affected():
        return _apply_reserve("PySide6 6.12.x on Python < 3.12, applied unconditionally")
    return False


def check_and_guard() -> dict:
    """Detect the defect and, if present, protect None. Returns a description for diagnostics."""
    import PySide6
    info = {"python": sys.version.split()[0], "pyside6": PySide6.__version__, "affected": False, "guarded": _done}
    if sys.version_info >= (3, 12):
        return info  # None is immortal
    if _known_affected():
        apply_reserve_early()
        info["guarded"] = _done
        info["always_applied"] = True
    try:
        lost = none_refs_lost_per_call()
    except Exception as exc:  # pragma: no cover - no QApplication / no display
        info["error"] = str(exc)
        return info
    info["lost_per_call"] = lost
    if lost >= 0.5:
        info["affected"] = True
        if not _done and _apply_reserve(f"probe lost {lost:.2f} refs per call"):
            info["guarded"] = True
    return info
