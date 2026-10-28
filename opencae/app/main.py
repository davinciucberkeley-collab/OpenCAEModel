"""OpenCAE Studio entry point.

    opencae-studio [project.ocae]
    python -m opencae.app.main [project.ocae] [--example cantilever]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def worker_command(module: str, args: list[str]) -> tuple[str, list[str]]:
    """Program + arguments to run a background worker, frozen-app aware.

    From source: ``python -m <module> args``. In a PyInstaller bundle there is no
    interpreter, so the application binary re-dispatches ``--worker <module>``."""
    if getattr(sys, "frozen", False):
        return sys.executable, ["--worker", module, *args]
    return sys.executable, ["-m", module, *args]


WORKERS = {"opencae.meshing.worker", "opencae.examples.run_validation"}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) >= 2 and argv[0] == "--worker" and argv[1] in WORKERS:
        import importlib
        return int(importlib.import_module(argv[1]).main(argv[2:]) or 0)
    ap = argparse.ArgumentParser(prog="opencae-studio", description="OpenCAE Studio - CAD, meshing and FEA")
    ap.add_argument("project", nargs="?", help="project file (.ocae) to open")
    ap.add_argument("--example", choices=["cantilever"], help="start with an example model")
    ap.add_argument("--selftest", metavar="DIR", help="run the cantilever workflow in the GUI (mesh, solve, "
                                                      "results, report), write DIR/selftest.json and exit")
    args = ap.parse_args(argv)

    os.environ.setdefault("QT_API", "pyside6")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from opencae.utilities.logging_setup import setup_logging
    setup_logging()
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("OpenCAE Studio")
    app.setOrganizationName("OpenCAE")
    from opencae.ui.theme import apply_theme
    apply_theme(app)
    from opencae.ui.main_window import MainWindow
    win = MainWindow()
    win.show()
    if args.selftest:
        return _selftest(app, win, Path(args.selftest))
    if args.project:
        win.cmd_open(str(Path(args.project).resolve()))
    elif args.example == "cantilever":
        win.cmd_example()
    return app.exec()


def _selftest(app, win, out: Path) -> int:
    """Installation self-test: drive the real GUI through the cantilever workflow."""
    import json
    import time
    out.mkdir(parents=True, exist_ok=True)
    status = {"ok": False, "steps": []}

    def wait():
        t = time.time()
        while win.proc is not None and time.time() - t < 600:
            app.processEvents()
            time.sleep(0.02)

    try:
        win.cmd_example()
        status["steps"].append("example")
        win.cmd_generate_mesh()
        wait()
        assert win.session.mesh is not None, "meshing failed"
        status["steps"].append(f"mesh {win.session.mesh.n_nodes} nodes")
        win.cmd_solve()
        wait()
        assert win.session.results is not None, "solve failed"
        sm = win.session.results.summary()
        status["steps"].append(f"solve max|U| = {sm['max_displacement']:.6g}")
        for _ in range(30):
            app.processEvents()
            time.sleep(0.02)
        app.primaryScreen().grabWindow(0).save(str(out / "selftest.png"))
        from opencae.postprocessing.report import write_report
        write_report(win.session, out / "selftest_report.html")
        status["steps"].append("report")
        status["max_displacement"] = sm["max_displacement"]
        status["ok"] = True
    except Exception as exc:  # report, never hang
        status["error"] = f"{type(exc).__name__}: {exc}"
    (out / "selftest.json").write_text(json.dumps(status, indent=2))
    win.dirty = False
    win.close()
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
