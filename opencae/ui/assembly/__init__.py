"""Assembly mode of the main window: assembly tree, joints, motors, motion studies, playback, plots and the hand-over
of motion loads to a structural study.

``MainWindow._install_extensions`` calls :func:`install`. The ribbon page is called "Motion" (the Studio already has
an "Assembly" page for ties and contact). Everything is optional: if this package fails to import the rest of the
application is unaffected.
"""
from __future__ import annotations

KEY = "opencae.ui.assembly"
PAGE = "Motion"


def install(main_window):
    existing = getattr(main_window, "extensions", {}).get(KEY)
    if existing is not None:
        return existing
    from opencae.ui.assembly.workspace import AssemblyExtension
    ext = AssemblyExtension(main_window)
    if isinstance(getattr(main_window, "extensions", None), dict):
        main_window.extensions[KEY] = ext
    return ext
