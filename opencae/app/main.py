"""OpenCAE Studio entry point.

    opencae-studio [project.ocae]
    python -m opencae.app.main [project.ocae] [--example cantilever|tiltwing]
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


WORKERS = {"opencae.meshing.worker", "opencae.examples.run_validation", "opencae.postprocessing.convergence"}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) >= 2 and argv[0] == "--worker" and argv[1] in WORKERS:
        import importlib
        return int(importlib.import_module(argv[1]).main(argv[2:]) or 0)
    ap = argparse.ArgumentParser(prog="opencae-studio", description="OpenCAE Studio - CAD, meshing and FEA")
    ap.add_argument("project", nargs="?", help="project file (.ocae) to open")
    ap.add_argument("--example", choices=["cantilever", "tiltwing", "k09l", "k09l-revB", "k09l-monitor"], help="start with an example model (tiltwing: the solved tilt-wing VTOL project; k09l-revB: the K09-L platform with design revision B loaded; k09l-monitor: the K09-L Design Monitor)")
    ap.add_argument("--drone", metavar="DIR", help="open Drone mode on this drone project folder (drone.json)")
    ap.add_argument("--selftest", metavar="DIR", help="run the cantilever workflow in the GUI (mesh, solve, "
                                                      "results, report), write DIR/selftest.json and exit")
    ap.add_argument("--safe", action="store_true", help="force safe mode (dashboard opens in the browser, software graphics)")
    ap.add_argument("--diagnose", action="store_true", help="print a JSON diagnostic report and exit")
    args = ap.parse_args(argv)

    os.environ.setdefault("QT_API", "pyside6")
    if args.diagnose:
        import json
        from opencae.utilities import crash
        print(json.dumps(crash.diagnose(), indent=2))
        return 0
    from opencae.utilities import crash
    crash.install(force_safe=args.safe)  # logging, faulthandler, hooks, session sentinel: before Qt
    try:
        code = _run(args)
    except SystemExit:
        raise
    except BaseException as exc:  # startup/GUI failure: log it, tell the user, never close silently
        crash.report_startup_failure(exc)
        return 1
    return int(code or 0)


def _run(args) -> int:
    from opencae.utilities import crash, qt_guard
    qt_guard.apply_reserve_early()  # Python < 3.12 + PySide6 6.12.x: always, before any Qt object
    crash.prepare_qt_environment()  # QtWebEngine import + Chromium flags must precede QApplication
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    crash.attach_app(app)
    qt_guard.check_and_guard()  # before any heavy UI work (see module docstring)
    app.setApplicationName("OpenCAE Studio")
    app.setOrganizationName("OpenCAE")
    from opencae.ui.theme import apply_theme
    apply_theme(app)
    from opencae.ui.main_window import MainWindow
    win = MainWindow()
    win.show()
    notice = crash.safe_mode_notice()
    if notice:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(400, lambda: _show_safe_notice(win, notice))
    if args.selftest:
        return _selftest(app, win, Path(args.selftest))
    if args.drone:
        win.open_drone_project(Path(args.drone).resolve())
    elif args.project:
        win.cmd_open(str(Path(args.project).resolve()))
    elif args.example == "cantilever":
        win.cmd_example()
    elif args.example == "tiltwing":
        win.cmd_example_tiltwing()
    elif args.example == "k09l":
        win.cmd_k09l_platform()
    elif args.example == "k09l-revB":
        win.cmd_k09l_platform()
        win._k09l.open_revision_b()
    elif args.example == "k09l-monitor":
        win.cmd_k09l_monitor()
    return int(app.exec() or 0)


def _show_safe_notice(win, text: str) -> None:
    from PySide6.QtWidgets import QMessageBox
    win.statusBar().showMessage("Safe mode: " + text, 20000)
    box = QMessageBox(QMessageBox.Information, "OpenCAE Studio", text, QMessageBox.Ok, win)
    box.setModal(False)
    box.show()
    win._safe_notice_box = box


def _selftest(app, win, out: Path) -> int:
    """Acceptance test driven through the real GUI (cantilever 200 x 20 x 20 mm, steel, 1000 N):
    create geometry, mesh, BCs/material, solve with CalculiX from the GUI, import results,
    render displacement and von Mises contours, compare with beam theory, save + reload,
    export a report. Writes DIR/selftest.json (+ screenshots, report) and returns 0 on success."""
    import json
    import time
    out.mkdir(parents=True, exist_ok=True)
    status = {"ok": False, "steps": [], "values": {}}

    def pump(sec=0.3):
        t = time.time()
        while time.time() - t < sec:
            app.processEvents()
            time.sleep(0.02)

    def wait_job():
        t = time.time()
        while win.proc is not None and time.time() - t < 900:
            app.processEvents()
            time.sleep(0.02)

    def step(name, detail=""):
        status["steps"].append(f"OK  {name}" + (f": {detail}" if detail else ""))
        print(status["steps"][-1], flush=True)

    try:
        t = time.time()  # wait for the background solver self-test started by the window
        while win.session.backend.verified is None and win.session.backend.executable() and time.time() - t < 150:
            pump(0.1)
        be = win.session.backend
        status["solver"] = {"exe": be.executable(), "verified": be.verified,
                            "report": be.last_report.summary() if be.last_report else ""}
        if not be.verified:
            raise RuntimeError("CalculiX self-test failed:\n" + status["solver"]["report"])
        step("0 solver self-test", f"{be.status_text()} ({be.executable()})")
        win.cmd_example()
        body = win.session.analysis_body()
        step("1 geometry", f"{body.name}, volume {body.volume:.6g} mm^3")
        win.cmd_generate_mesh()
        wait_job()
        if win.session.mesh is None:
            raise RuntimeError("meshing failed")
        step("2 mesh", f"{win.session.mesh.n_nodes} nodes, {win.session.mesh.n_elements} {win.session.mesh.element_type}")
        p = win.session.project
        step("3 material + BCs", f"{p.material_for_body(body.id).name}; {len(p.constraints)} support, {len(p.loads)} load")
        win.cmd_solve()
        wait_job()
        if win.session.results is None:
            raise RuntimeError("solve failed: " + win.session.last_solve_error)
        step("4 CalculiX run from GUI", p.results.solver_version)
        sm = win.session.results.summary()
        status["values"].update(max_displacement_mm=sm["max_displacement"],
                                max_von_mises_nodal_MPa=sm.get("max_von_mises_nodal"),
                                max_von_mises_ip_MPa=sm.get("max_von_mises_integration_point"))
        step("5 results imported", f"max |U| = {sm['max_displacement']:.6g} mm")
        for key, fname in (("U_mag", "displacement.png"), ("S_vm", "von_mises.png")):
            win.cmd_result(key)
            pump(0.6)
            win.viewport.screenshot(out / fname)
        step("6 contours rendered", "displacement.png, von_mises.png")
        from opencae.postprocessing.theory import cantilever_comparison
        comp = cantilever_comparison(win.session)
        q = comp.quantities[0]
        status["values"].update(tip_deflection_fe_mm=q["fe"], tip_deflection_theory_mm=q["theory"],
                                tip_deflection_diff_pct=q["diff_pct"])
        if abs(q["diff_pct"]) > 3.0:
            raise RuntimeError(f"tip deflection differs from Euler-Bernoulli by {q['diff_pct']:.2f} %")
        step("7 analytical comparison", f"FE {q['fe']:.5f} mm vs FL^3/3EI {q['theory']:.5f} mm ({q['diff_pct']:+.2f} %)")
        proj = win.session.save(out / "acceptance_cantilever.ocae")
        win.dirty = False
        win.cmd_open(str(proj))
        pump(0.5)
        if win.session.results is None or not win.session.project.results_are_current():
            raise RuntimeError("results not restored after reopening")
        u2 = win.session.results.summary()["max_displacement"]
        if abs(u2 - sm["max_displacement"]) > 1e-12:
            raise RuntimeError("reloaded results differ")
        step("8 save + reload", str(proj))
        from opencae.postprocessing.report import write_report
        imgs = [out / "displacement.png", out / "von_mises.png"]
        rep = write_report(win.session, out / "acceptance_report.html", imgs)
        text = rep.read_text(encoding="utf-8")
        if f"{sm['max_displacement']:.6g}" not in text:
            raise RuntimeError("report does not contain the computed maximum displacement")
        step("9 report", str(rep))
        app.primaryScreen().grabWindow(0).save(str(out / "selftest.png"))
        status["ok"] = True
    except Exception as exc:  # report, never hang
        status["error"] = f"{type(exc).__name__}: {exc}"
        print("FAILED:", status["error"], flush=True)
    (out / "selftest.json").write_text(json.dumps(status, indent=2))
    win.dirty = False
    win.close()
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
