# Contract: Results Hub (read before building; do not change names without the conductor)

Repo `/home/claude/OpenCAE-Studio`, Python `/home/claude/venv/bin/python`.
Tests: `cd /home/claude/OpenCAE-Studio && PYTHONPATH=$PWD QT_QPA_PLATFORM=offscreen xvfb-run -a /home/claude/venv/bin/python -m pytest <files> -q -p no:cacheprovider`.
Rules (AGENTS.md): never fabricate numbers (every value shown comes from a stored result; nothing typed into the page);
every status keeps its evidence basis (estimate/datasheet/measured/user); failures are shown, never hidden; a
simulation is never presented as flight approval (keep the line "Simulation results plan the tests; they are not
flight approval." on every page). Tests first. Do not commit. Do not touch files owned by another builder.

## Ownership

- V1: `opencae/hub/__init__.py, registry.py, adapters.py, snapshots.py, render.py`, hub routes in `opencae/web/server.py`
  (a `_hub_get` dispatcher called from `do_GET` for paths starting `/api/hub/` and `/hub/`), `opencae/cli.py` (`opencae hub`),
  `opencae/tests/test_hub_backend.py`, `opencae/tests/fixtures/hub/*` (small fixture projects).
- V2: `opencae/web/hub/index.html, hub.js, hub.css, plots.js, viewer.js`, `opencae/tests/test_hub_frontend.py`.
- V3: `opencae/hub/motion.py`, `opencae/web/hub/motion.js`, `opencae/tests/test_hub_motion.py`.
- V4: `opencae/ui/hub_view.py, opencae/ui/help.py`, `opencae/ui/console.py`, `opencae/ui/drone/workspace.py` (layout,
  link to hub), `opencae/utilities/crash.py` (Chromium flags), `opencae/tests/test_hub_ui.py`. Main-window wiring only via
  `install(main_window)` in an extension module; the conductor adds `"opencae.ui.hub_view"` and `"opencae.ui.help"` to
  `MainWindow.EXTENSIONS`.

## Projects and runs (V1)

Project kinds: `drone` (a folder with `drone.json`: quad250, k09l, any new drone), `studio` (an `.ocae` file: gripper,
drone_rotors, any Studio project with FEA/CFD/MBD studies), `k09l-study` (the K09-L engineering study,
`~/.opencae/k09l_study.json`, the data behind the K09-L website).

`registry.py`: `~/.opencae/hub/projects.json` = list of `{"id", "kind", "path", "name", "added"}`. `id` = stable
short hash of the resolved path. `register(path, kind=None) -> dict` (kind guessed), `projects() -> list` (registered
+ auto-discovered: every folder in `~/.opencae/projects/*` with drone.json, the k09l study if present, the shipped
examples `opencae/examples/projects/*.ocae`; missing paths dropped silently from the listing but kept in the file).
`OPENCAE_HOME` overrides `~/.opencae` everywhere.

Runs: drone → one per line of `results/runs.jsonl` (`run_id`); studio → one per study result folder
(`<data dir>/results/<study id>/`), run id = `"<study id>@<mtime>"`; k09l-study → one run `"current"`.
`snapshots.py`: after every drone pipeline run, copy `results/<variant>/results.json` to
`results/history/<run_id>/results.json` (small; fields are kept only for the latest run of each variant, stated in
the UI as "fields kept for the latest run only"). `snapshot_run(project_dir, run_id, variant)`; hook it where
runs.jsonl is appended (`opencae/drone/pipeline.py`) — V1 may edit that one call site only.

## API (all GET, JSON unless stated; `pid` = project id, `run` = run id or `latest`)

- `/api/hub/projects` → `[{id, kind, name, path, updated (iso), verdict ("pass"|"conditional"|"fail"|"not ready"|null),
  runs (int), disciplines: ["structural","cfd","flight","mbd","mass","propulsion", ...]}]`
- `/api/hub/project?pid=&run=` → `{id, kind, name, run, runs: [{run, started, verdict, only, duration_s}],
  cards: [Card], analyses: [{id, title, discipline, status, summary}], notice}`
  `Card = {id, title, discipline, status, basis, headline: {value, unit, label} | null, text, metrics: [{label, value,
  unit, status, basis}]}`. Status words: `ok|watch|conditional|fail|error|not_evaluated` (drone statuses as they are).
- `/api/hub/scene?pid=&run=` → `{units: "mm", parts: [{name, role, points: b64 f32 (n*3), triangles: b64 i32 (m*3)}],
  fields: [{id, label, discipline, unit, quantity, mesh: part-name | "own", points?, triangles?, values: b64 f32,
  min, max, displacement?: b64 f32, scale_hint?}], slices: [{name, points, triangles, values, label, unit}],
  streamlines: {points: b64 f32, offsets: b64 i32, values: b64 f32, unit} | null, notes: [str]}`
  (b64 = base64 of little-endian arrays). Empty lists when the run has none, with a note saying why
  (e.g. "CFD failed: …", "fields kept for the latest run only").
- `/api/hub/plots?pid=&run=` → `[{id, title, discipline, x: {label, unit}, y: {label, unit, log: bool},
  series: [{name, x: [..], y: [..]}], note}]`: CFD residual history (parsed from the solver log when kept, else the
  stored final residuals as a bar-type single point with a note), C_L/C_D vs iteration (`extra.history`), FEA
  mesh-convergence if present, flight time histories, MBD channels (V3 adds through `motion.plots()`).
- `/api/hub/audit?pid=&run=` → `[{analysis, title, status, solver: {...}, mesh: {...}, inputs: [{name, value, unit,
  status, source}], checks: [{name, value, limit, unit, ok}], convergence: {...}, assumptions: [..], problems: [..],
  log_tail: str, files: [{name, bytes}]}]`
- `/api/hub/motion?pid=&run=` → V3 (below).
- `/api/hub/image.png?pid=&run=&field=&view=iso|top|front|side` → PNG rendered by the backend (pyvista off-screen,
  `render.py`), with a colour bar, title, units; the WebGL-free fallback for every contour.
- `/api/hub/compare?pid=&a=&b=` → `{metrics: [{analysis, label, unit, a, b, delta, delta_pct}]}`
- Static: `/hub/` → `opencae/web/hub/index.html` (+ its assets), vendor files as today (`/vendor/...`).
- POST `/api/hub/client` body `{webgl: bool, error, ua}` → logged to opencae.log (`opencae.hub`) so a WebGL failure on
  the user's PC is visible in the log.

`opencae hub [--open] [--port N]` serves the hub for all projects (no project dir needed).
The in-app server (`DashboardServer`) serves the same routes, so the hub is reachable from the app's existing server.

## Motion (V3)

`/api/hub/motion?pid=&run=` → `{kind: "mbd"|"hover"|"flight"|null, title, basis, notes: [..], t: [..] (s),
bodies: [{id, name, points: b64 f32, triangles: b64 i32, color}], poses: {body_id: b64 f32 (n*7: x,y,z mm, qw,qx,qy,qz)},
joints: [{id, name, kind, mode: "prescribed"|"torque-driven"|null}], channels: [{name, unit, values: [..], group}],
rotors: [{body_id, axis: [3], speed_rpm: [..]}] | null}`.
- studio MBD: from `opencae.mbd.run`/`MBDResult.load` + `pose_transforms`; bodies' meshes from the project geometry.
- drone hover: from the flight analysis result (6-DOF hover time history in results / fields); the aircraft mesh from
  `geometry_cache.npz` / `/api/geometry`; rotors spin with the recorded or computed speed; when the flight analysis
  stored no time history, `kind: null` with a note naming what is missing — never a made-up trajectory.
- K09-L study: the existing 6-DOF flight replay data.
`motion.plots(project, run) -> list` (same shape as /api/hub/plots items) is merged into /api/hub/plots by V1.
Front end: `window.HubMotion.mount(el, data, {webgl: bool})` → controls play/pause, speed (0.25–4×), scrubber with
time, synced channel plot with a moving cursor, "prescribed"/"torque-driven" labels; WebGL → three.js; no WebGL →
2-D canvas animation (orthographic side and top views of the triangle meshes).

## Front end (V2)

`/hub/` page, K09-L website look (`opencae/web/templates/style.css` tokens, dark theme): left project list (search,
kind badge, verdict, last run), header with project name, run picker, compare picker, "Open in browser" link;
tabs: Overview (cards like the drone Results tab), 3D & Contours (field picker, colour map, range, deformation scale,
slices/streamlines toggles, part visibility; WebGL viewer `viewer.js`, else server PNGs with view buttons), Plots
(`plots.js`, canvas 2-D: zoom, hover values, log axis, download CSV), Motion (`HubMotion`), Audit (tables per
analysis, expandable log, problems in red), History (runs table, verdict per analysis, open run, compare two).
`?pid=&run=&tab=` in the URL selects state (so the app can open a project directly). WebGL detection at load:
post `/api/hub/client`; on failure the 3D tab shows the PNG fallback with a one-line note, never a blank canvas.
Plain words, units on every number, evidence basis badges, the flight-approval line in the footer.
