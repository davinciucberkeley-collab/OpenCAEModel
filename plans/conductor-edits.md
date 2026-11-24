| Date | File | Reason |
|---|---|---|
| 2026-11-23 | opencae/web/dashboard/dash.js, dash.css | R2 non-blocking 2: headline number neutral (not green) when the result is conditional; one-line change, no builder needed |
| 2026-11-23 | opencae/utilities/threads.py, ui/solver_settings.py, ui/platform/monitor.py, ui/main_window.py, web/server.py, ui/drone/workspace.py, tests/test_threads_shutdown.py | Release blocker found in the full-suite run: Qt aborts at exit when a QThread (CalculiX self-test, K09-L run) still runs (reproduced, exit 134); likely the user's "exits with an error". Narrow fix; shortcuts dialog made non-modal (a modal exec hung a test). |
| 2026-11-24 | opencae/drone/analyses/structure.py, cfd.py | User report: on Windows the drone analyses did not find CalculiX/OpenFOAM; use the app's solver discovery (settings, conda scan, solver environment). One-function fix. |
| 2026-11-24 | drone/analyses/cfd_su2.py, utilities/crash.py, tests | User reports: SU2 CFD failed "signal only works in main thread" (gmsh interruptible=False); QQuickWidget no QRhi on Windows (Qt Quick forced to OpenGL like VTK). One-line fixes with regression tests. |
