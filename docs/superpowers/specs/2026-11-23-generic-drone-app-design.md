# Generic drone projects, one app with modes, CLI, dashboard: design

Date 2026-11-23. Source: the user's request of 2026-11-23 and the meeting commitments (CLI for agents; one
app, one design language, different modes; Superpowers workflow; shared AGENTS.md for multi-agent runs).

## What the user asked for

1. Everything runs inside the existing OpenCAE app. No separate dashboard page to open by hand; a dashboard
   served by a local server that the app starts in run mode is fine.
2. Keep the app's essence; optimise, no major redesign.
3. Any drone, without new code: import the CAD file, fill a parameter sheet, run everything.
4. Storage is short on the user's PC: compact result files, clean solver scratch, run in the background.
5. Full (whole-aircraft) CFD, never a half model.
6. Less technical, more visual and interactive.
7. A CLI an agent can use to run a full simulation and a design iteration without editing code.
8. AGENTS.md rules for multi-agent work (James's file was not available; ours is written so his can be merged).

## Decisions

- **A drone project is a folder**: `drone.json` (format `opencae-drone/1`) + the CAD file (STEP, STL or OBJ)
  + `results/` written by the pipeline. Each numeric input is a number or `{value, unit, status, source}` with
  status `estimate | datasheet | measured | user`. Missing inputs make the dependent analysis
  `not_evaluated`; they are never filled in silently.
- **New package `opencae.drone`** holds the generic pipeline. The K09-L pipeline (`opencae.engineering`) stays
  as it is; `examples/drones/k09l/drone.json` with `"engine": "k09l"` routes the CLI, app and dashboard to it,
  so K09-L becomes one example project. A second example, `examples/drones/quad250`, exercises the generic
  path end to end.
- **Generic analyses**, run in order: mass (mass, CG, inertia), propulsion (hover and full-throttle point,
  thrust-to-weight, current, endurance), structure (CalculiX FEA of every `arm` part, or of `wing` parts for
  fixed wings, under the max-thrust or load-factor case), cfd (OpenFOAM whole aircraft, domain sized from the
  bounding box), flight (6-DOF hover with a step disturbance, rotor mixer from rotor positions), then checks
  (requirements to pass / conditional / fail with the evidence rules in `opencae/drone/types.py`).
- **Roles** come from glob patterns on part names in `drone.json`; the new-project command guesses them
  from names (arm, motor, prop, batt, wing, fin, elevon/aileron, leg/foot/skid, frame/body/plate/pod).
- **Results are compact**: float16 field values, float32 points, `savez_compressed`; solver case folders
  are deleted after a successful run (kept on failure for the log, or with `--keep-cases`).
- **Dashboard**: `opencae.web.server.DashboardServer` (stdlib `http.server`, background thread, port 0 =
  free port) serves `opencae/web/dashboard/` and a JSON API. The app shows it in a QWebEngineView tab of
  the Drone window; if QtWebEngine is missing it opens the system browser. For K09-L the server also
  serves the full K09-L design-review site (built on demand).
- **One app, modes**: the main window gets *Drone project* actions (New…, Open…) in the existing ribbon.
  The Drone window has the modes Setup (parameter sheet), Run (progress, log), Dashboard (web view) in the
  existing dark theme (`opencae.ui.theme`).
- **CLI** `opencae` (`opencae/cli.py`, `python -m opencae`): `new`, `run`, `iterate`, `status`,
  `dashboard`, `clean`, `doctor`. `--json` prints one JSON object on stdout; progress goes to stderr.
- **Heavy solvers one at a time** per machine via `opencae.drone.runlock.solver_lock`.
- **Storage on the user's PC**: the built K09-L website leaves the repo (built on demand into
  `%USERPROFILE%\.opencae\k09l_website`); K09-L fields are rewritten as float16; the K09-L Study deletes its
  solver run folders after fields are saved.

## Out of scope

Transition and wing-borne mission simulation for arbitrary aircraft (K09-L keeps its own); component
screening of every part for generic drones (arms / wing only); automatic flight approval (never).
