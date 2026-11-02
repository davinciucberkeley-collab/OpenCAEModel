"""Environment diagnostics:  python -m opencae.app.doctor [--no-solver] [--json FILE]

Checks the Python interpreter, the GUI/geometry/mesh libraries, and CalculiX
(discovery + a real test analysis in an isolated child process). Installs
nothing and changes no environment variables. Exit code 0 = everything needed
to run OpenCAE Studio works; 1 = something is missing (each line says what).
"""
from __future__ import annotations

import argparse
import importlib
import json
import platform
import sys
from pathlib import Path

REQUIRED = [("PySide6.QtCore", "PySide6", "Qt GUI"), ("vtkmodules.vtkCommonCore", "vtk", "3-D rendering"),
            ("pyvista", "pyvista", "3-D rendering"), ("pyvistaqt", "pyvistaqt", "Qt viewport"),
            ("OCP.TopoDS", "cadquery-ocp", "OpenCascade CAD kernel"), ("gmsh", "gmsh", "mesher"),
            ("numpy", "numpy", "numerics"), ("scipy", "scipy", "numerics"), ("meshio", "meshio", "mesh export"),
            ("matplotlib", "matplotlib", "plots")]


def _version(dist: str) -> str:
    try:
        from importlib.metadata import version
        return version(dist)
    except Exception:
        return "?"


def run(check_solver: bool = True) -> dict:
    res = {"python": sys.version.split()[0], "executable": sys.executable, "platform": platform.platform(),
           "modules": [], "ok": True}
    print(f"Python      {res['python']}  ({sys.executable})")
    if sys.version_info < (3, 11):
        print("  !! Python 3.11 or newer is required")
        res["ok"] = False
    for mod, dist, role in REQUIRED:
        try:
            importlib.import_module(mod)
            print(f"  ok   {dist:14s} {_version(dist):12s} {role}")
            res["modules"].append({"name": dist, "ok": True, "version": _version(dist)})
        except Exception as exc:
            print(f"  FAIL {dist:14s} {'':12s} {role}: {type(exc).__name__}: {exc}")
            res["modules"].append({"name": dist, "ok": False, "error": str(exc)})
            res["ok"] = False
    if sys.version_info < (3, 12) and res["ok"]:
        try:  # PySide6 builds that lose references to None on Python 3.11 (see utilities/qt_guard.py)
            import os
            if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
                os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PySide6.QtWidgets import QApplication
            _app = QApplication.instance() or QApplication([])
            from opencae.utilities.qt_guard import none_refs_lost_per_call
            lost = none_refs_lost_per_call()
            res["pyside_none_refcount_defect"] = lost >= 0.5
            if lost >= 0.5:
                print("  note PySide6 on this Python loses a reference to None per Qt call; OpenCAE compensates "
                      "at start-up (Python >= 3.12 is not affected)")
        except Exception as exc:  # pragma: no cover
            print(f"  note could not probe PySide6 reference counting: {exc}")
    try:
        import opencae
        print(f"  ok   opencae-studio {opencae.__version__:12s} from {Path(opencae.__file__).parent}")
    except Exception as exc:  # pragma: no cover
        print(f"  FAIL opencae: {exc}")
        res["ok"] = False
    if check_solver:
        from opencae.solver.launcher import discover
        from opencae.solver.calculix.selftest import run_selftest
        from opencae.utilities.settings import get_setting, settings_path
        installs = discover(get_setting("ccx_path"), get_setting("ccx_extra_dll_dirs") or [])
        print(f"CalculiX    settings file: {settings_path()}")
        if not installs:
            print("  FAIL no ccx executable found (set it in Tools > Settings or OPENCAE_CCX)")
            res["solver"] = {"ok": False}
            res["ok"] = False
        else:
            for i in installs:
                print(f"  found {i.label}  [{i.source}]")
            rep = run_selftest(installs[0])
            print("\n".join("  " + ln for ln in rep.summary().splitlines()))
            res["solver"] = rep.to_dict()
            if not rep.ok:
                res["ok"] = False
        # SU2 is optional (flow studies only): report, test if present, never fail the doctor for its absence
        from opencae.cfd.su2 import SU2Backend
        su2 = SU2Backend()
        if su2.install is None:
            print("SU2         not found (optional; needed only for flow studies - see docs/WINDOWS_SETUP.md)")
            res["su2"] = {"found": False}
        else:
            print(f"SU2         found {su2.install.exe}  [{su2.install.source}]")
            rep = su2.selftest()
            print("\n".join("  " + ln for ln in rep.summary().splitlines()))
            res["su2"] = {"found": True, "ok": rep.ok, "summary": rep.summary()}
    print("RESULT: " + ("all checks passed" if res["ok"] else "problems found (see FAIL lines above)"))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-solver", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    res = run(not a.no_solver)
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=2, default=str))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
