# Wave 3 brief (read with common.md)

User's words (2026-11-23 14:55): "i want a run progress tracker and also the appplication is exiting with too much
execution as an error the application interface is good but not user friendly make the ui more interactive adding
more options and give me a complete redme file on the working of the file you can akso add command method in the
application such that i could type in codes for it and the command is executed such that it could work better . do
whatever is best make the application an elite version of itself"

User's machine: Windows, conda env `qt_test`, Python 3.11.18, PySide6 6.12.0 (+ pyside6_webengine 6.12.0.140),
VTK 9.6.2, numpy 2.4. That PySide6/Python pair has a known None-refcount defect (`opencae/utilities/qt_guard.py`).
The app exits on the user's PC with no log entry (last `~/.opencae/logs/opencae.log` line is from Nov 18), i.e. a
hard crash or an unhandled exit; the cause has not been reproduced.

## Extension hook (already in place, do not change)

`MainWindow._install_extensions()` imports each module in `MainWindow.EXTENSIONS` =
(`opencae.ui.console`, `opencae.ui.drone.tracker`, `opencae.ui.drone.ux`) and calls `install(main_window)`; the
return value is kept in `main_window.extensions[module]`. A failing install is logged and skipped. Add docks with
`main_window.addDockWidget`, ribbon buttons with `main_window.ribbon_bars["Drone"].addAction(...)` (or another page),
menu entries via `main_window.menuBar()`. Do NOT edit `opencae/ui/main_window.py` unless your brief says so.
Useful members: `main_window.drone_workspace()` (creates/returns the `DroneWorkspace`), `main_window.stack`,
`main_window.statusBar()`, `main_window.open_drone_project(path)`, `main_window.ribbon`.
`DroneWorkspace` members: `project_dir`, `server` (DashboardServer or None), `progress` signal
(running: bool, frac: float, step: str), `message` signal (str).

## Run events (C2 provides; C3 and C4 consume)

`pipeline.run_project(..., events=None)` and `pipeline.iterate(..., events=None)`: `events(ev: dict)` is called with
`{"t": epoch_s, "kind": "run_start"|"analysis_start"|"step"|"analysis_done"|"log"|"run_done", "run_id", "id"
(analysis id or None), "title", "status" (analysis_done/run_done), "frac" (0..1 overall), "message", "elapsed_s"}`.
Each run appends one line per finished run to `<project>/results/runs.jsonl`:
`{"run_id", "variant", "started", "finished", "duration_s", "only", "verdict", "analyses": {id: {"status",
"duration_s", "cached"}}, "error"}`. `pipeline.run_history(project_dir) -> list[dict]` reads it (newest last).
`DashboardServer.jobs.progress` gains `events` (last 500) and `analyses` ({id: {"title", "state": "waiting"|"running"
|"done"|"cached"|"error"|"skipped", "status", "started", "duration_s"}}), `eta_s` (estimate from the previous
durations in runs.jsonl, else None), `run_id`. `GET /api/history` returns run_history.

## Rules

Tests first; offscreen Qt (`QT_QPA_PLATFORM=offscreen`). No solver runs unless your brief says so. Do not commit.
Keep the existing look (opencae.ui.theme colours, the dashboard's dark tokens). Friendly words, not jargon.
