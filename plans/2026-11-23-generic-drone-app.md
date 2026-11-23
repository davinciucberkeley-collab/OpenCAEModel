# Generic drone app, CLI and modes in one window: implementation plan

> Conductor: main session (Opus). Standard: `standards/agents.md` v0.2. Builders leave changes uncommitted;
> the conductor commits. After any compaction, reread this file first.

## Request (task file, in the user's words; status kept up to date)

| # | Request | Status |
|---|---|---|
| U1 | "Put a CLI on OpenCAE so an agent can run a full simulation and design iteration without editing code" | open (B4) |
| U2 | "Consolidate the interfaces into one app: one design language, one surface, different modes" | open (B5) |
| U3 | "Try Superpowers on the current OpenCAE problem" | in progress: writing-plans done, subagent-driven build, verification-before-completion |
| U4 | "Adopt James's shared agents MD for multi-agent runs" (DBF Agent Roles – Quick Guide) | done: `standards/agents.md`, `.claude/agents/`, `AGENTS.md` pointer file |
| U5 | "I don't want a separate page. I want everything to run … in a application that is already made for OpenCAE" | open (B5: Drone mode inside the main window; dashboard embedded only while running) |
| U6 | "do not work major changes in the application … keep the essence of the software" | constraint on B5 |
| U7 | "whatever the drone may be … just import the file and define it as a set of parameters" | open (B1, B2) |
| U8 | "storage is an issue … optimizing the file, try running more in the backgrounds" | done for K09-L (541aa95: half-float fields, run folders deleted, website not shipped); B1 for new projects; background runs in B3/B5 |
| U9 | "use multi-agents … Opus, Sonnet, Fable … Haiku" | agent table below |
| U10 | "more interactive, more visually attentive … not so much technical stuff" | B3, B5 |
| U11 | "I don't want to do a half fluid analysis. I want to do a full fluid analysis" | open (B2: whole aircraft only) |
| U12 | "a dashboard … run on a separate server that is created while the app is on run mode … is also good" | open (B3 server, started by B5's Drone mode) |

## Failure modes (thought through before launch)

1. Builders edit each other's files → strict file ownership per row; shared types frozen in `opencae/drone/types.py`.
2. Two CFD/FEA runs at once exhaust 8 GB → only B2 runs solvers, inside `solver_lock`.
3. The app change grows into a redesign (U6) → B5 limited to a stacked centre area + one ribbon page.
4. Container restarts kill agents mid-task → builders write tests first; the conductor commits after each wave.
5. Generic analyses silently assume values → evidence rules in `types.py`; the reviewer checks them.
6. CFD of an arbitrary CAD fails to mesh (open surfaces) → `error` result with the log, never a number.

## Open decisions and permissions

None open: the user asked not to be disturbed. Decisions taken: the generic pipeline sits beside the K09-L
one (K09-L keeps its detailed analyses); the dashboard is shown inside the app and only opens in a browser
when QtWebEngine is missing. Deploying to `E:\Calculix\OpenCAE-Studio-0.1.0` is already permitted.

## Agent table

| Step | Role | Tier | Model | Input | Output | Pass criterion |
|---|---|---|---|---|---|---|
| B1 | builder | hard | sonnet | this plan Task 1, `types.py` | `opencae/drone/{spec,geometry,templates,pipeline}.py`, `analyses/{__init__,mass,propulsion,checks}.py`, `examples/drones/**`, `docs/DRONE_PROJECTS.md` | `pytest opencae/tests/test_drone_core.py` exit 0 |
| B2 | builder | hard | sonnet | Task 2, `types.py`, `runlock.py`, `engineering/field_analyses.py` (read only) | `analyses/{structure,cfd,flight}.py` | `pytest opencae/tests/test_drone_solvers.py` exit 0, incl. one real coarse CFD run |
| B3 | builder | hard | sonnet | Task 3, partial `opencae/web/server.py` | `web/server.py`, `web/dashboard/*` | `pytest opencae/tests/test_dashboard_server.py` exit 0 + screenshots |
| B4 | builder | easy | haiku | Task 4a, partial `opencae/cli.py`, `test_cli.py` | `opencae/cli.py`, `opencae/__main__.py` | `pytest opencae/tests/test_cli.py` exit 0 |
| B5 | builder | hard | sonnet | Task 4b, partial `opencae/ui/drone/*`, `ui/main_window.py` | `ui/drone/*`, small edits to `ui/main_window.py`, `app/main.py` | `pytest opencae/tests/test_drone_window.py` exit 0 + screenshot |
| R1 | reviewer | hard | opus | plan, `standards/agents.md`, AGENTS.md rules, diff of wave 1 | `reviews/2026-11-23-wave1.md` scorecard | every criterion PASS/FAIL with a reason |
| B6 | builder | hard | sonnet | R1 blocking issues | fixes in the named files | the failing checks pass |


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any drone runs through OpenCAE from a CAD file plus a parameter file, from the app, the CLI or the
in-app dashboard, without code changes.

**Architecture:** New package `opencae/drone/` (spec, geometry import, analyses, pipeline) built on the shared
types in `opencae/drone/types.py`. A stdlib HTTP server serves a dashboard and JSON API; the Qt app embeds it.
K09-L stays on `opencae.engineering` and is reached through an `"engine": "k09l"` project.

**Tech Stack:** Python 3.11, numpy, OCP (STEP), gmsh, CalculiX (`ccx`), OpenFOAM v1912+ (`simpleFoam`,
`snappyHexMesh`), PySide6 + QtWebEngine, three.js r128 (vendored in `opencae/web/vendor`).

**Spec:** `docs/superpowers/specs/2026-11-23-generic-drone-app-design.md`

## Global Constraints

- Never fabricate outputs; a missing input gives `not_evaluated` with the key in `missing`.
- Estimate-based results can be at best `conditional` in checks; `pass` needs `measured` or `datasheet` inputs.
- Solver failures are `error` results with the log tail; never hidden.
- No result is flight approval; every dashboard page carries that line.
- Whole-aircraft CFD only (no symmetry plane).
- One heavy solver at a time: wrap every OpenFOAM / CalculiX run in `solver_lock()`.
- Do not change K09-L behaviour; existing tests (`opencae/tests`) keep passing.
- Field files via `types.save_field` (float16). Solver scratch deleted after success unless `keep_cases`.
- Plain-language summaries (one or two sentences, no unexplained jargon).
- Windows paths and `qt_test` conda env (Python 3.11) must work; no new mandatory dependency.

## Review Focus

1. A CAD file with unnamed parts (binary STL): parts get `part_1…`, role `other`, and the analyses that
   need a role report `not_evaluated` naming the role, not a crash. (Task 1 test.)
2. Millimetre vs metre CAD: `geometry.units` respected; a 0.25 m quad is not read as 0.25 mm. (Task 1 test.)
3. OpenFOAM or ccx not installed: `error`/`not_evaluated` result saying which program is missing. (Task 2 test.)
4. A parameter edit from the dashboard or `--set` with a bad key or a non-number: rejected with a message,
   the file unchanged. (Task 1 `apply_overrides` test, Task 3 API test.)
5. Re-running with nothing changed: cached analyses are skipped (input hash), so a dashboard click does not
   start a 40-minute CFD again. (Task 1 pipeline test.)

---

### Task 1: Spec, geometry import, mass and propulsion, checks, pipeline, examples (owner: agent "core")

**Files:**
- Create: `opencae/drone/__init__.py`, `spec.py`, `geometry.py`, `analyses/__init__.py`, `analyses/mass.py`,
  `analyses/propulsion.py`, `analyses/checks.py`, `pipeline.py`, `templates.py`
- Create: `opencae/examples/drones/quad250/{drone.json, airframe.stl, make_geometry.py}`,
  `opencae/examples/drones/k09l/drone.json`
- Test: `opencae/tests/test_drone_core.py`

**Interfaces:**
- Consumes: `opencae/drone/types.py` (Param, Part, Aircraft, RunContext, result, metric, not_evaluated,
  save_field, statuses, ROLES, TYPES, ANALYSES).
- Produces:
  - `spec.load(path: Path) -> dict` (path = project dir or drone.json); `spec.save(spec, path)`;
    `spec.param(spec, "propulsion.motor.kv") -> Param | None`;
    `spec.apply_overrides(spec, {"dotted.key": value}) -> dict` (deep copy; raises `KeyError`/`ValueError`);
    `spec.validate(spec) -> list[str]`; `spec.input_hash(spec, keys: list[str]) -> str`.
  - `geometry.load(project_dir: Path, spec: dict) -> Aircraft` (STL ascii multi-solid + binary split by
    connectivity, OBJ `o`/`g`, STEP via `opencae.geometry.occ_kernel.read_step` tessellated per solid);
    `geometry.guess_roles(names: list[str]) -> dict[str, list[str]]`.
  - Analysis modules each expose `ID: str`, `TITLE: str`, `NEEDS: list[str]` (dotted spec keys for the cache
    hash) and `run(ctx: RunContext) -> dict` (a `types.result`).
  - `analyses.REGISTRY: dict[str, module]` — includes `structure`, `cfd`, `flight` from Task 2 if importable.
  - `pipeline.run_project(project_dir, only=None, overrides=None, variant="baseline", progress=None,
    cancelled=None, keep_cases=False, force=False) -> dict` writes `results/<variant>/results.json`
    `{"project", "variant", "spec_used", "analyses": {id: result}, "checks": [...], "verdict", "generated",
    "storage_bytes"}` and `results/<variant>/geometry.npz` (parts: points float32, triangles int32, names,
    roles); returns that dict. Engine `"k09l"` delegates to `opencae.engineering.study.Study` and returns a
    summary in the same shape.
  - `pipeline.status(project_dir) -> dict`, `pipeline.clean(project_dir, results=False) -> int` (bytes freed),
    `pipeline.new_project(dir, cad_file, name, type) -> Path`, `pipeline.iterate(project_dir, overrides,
    name, only=None) -> dict` (runs a variant, returns `{"variant", "results", "compare": rows}`),
    `pipeline.variants(project_dir) -> list[str]`.

- [ ] Write tests: `test_spec_param_and_overrides` (bare number → Param status "estimate"; bad key raises
  KeyError; non-number to numeric raises ValueError; file unchanged), `test_geometry_units_and_roles`
  (quad250 STL loads with ≥ 13 parts, roles arm×4 motor×4 prop×4, bbox diagonal 200–400 mm; same file with
  `units: "m"` scaled ×1000), `test_unnamed_parts` (binary STL → `part_1…`, structure reports
  not_evaluated naming role "arm"), `test_mass_propulsion_quad` (T/W between 3 and 15, hover throttle 0.1–0.6,
  endurance > 0; every metric basis "estimate"), `test_checks_never_pass_on_estimates`,
  `test_pipeline_cache_skips_unchanged` (second run marks analyses `cached: true` in extra),
  `test_new_project_and_iterate` (new_project from the quad STL; iterate with battery capacity ×2 → compare
  row endurance up), `test_k09l_project_status` (engine k09l status lists the K09-L heavy analyses).
- [ ] Run them, see them fail. Implement. Run `pytest opencae/tests/test_drone_core.py -q` → all pass.
- [ ] quad250 inputs: typical 5-inch class values, every one `status: "estimate"` with source
  "typical 5-inch FPV class value, not a datasheet" unless a real datasheet is cited.

### Task 2: Generic solvers: structure (CalculiX), CFD (OpenFOAM, whole aircraft), flight (6-DOF hover) (owner: agent "solvers")

**Files:**
- Create: `opencae/drone/analyses/structure.py`, `cfd.py`, `flight.py`
- Test: `opencae/tests/test_drone_solvers.py` (solver tests marked `@pytest.mark.solver` /
  `@pytest.mark.openfoam` + `@pytest.mark.slow`)

**Interfaces:**
- Consumes: types.py; `ctx.aircraft.role_parts(...)`, `ctx.spec` via `opencae.drone.spec.param` (Task 1; until
  it exists read the dict directly with the documented keys), `ctx.previous["mass"]["metrics"]`
  (`mass_kg`, `cg_mm` list, `inertia_kgm2` 3×3) and `ctx.previous["propulsion"]["metrics"]`
  (`thrust_max_n` per rotor, `hover_throttle`), `runlock.solver_lock`.
- Produces: modules with `ID`, `TITLE`, `NEEDS`, `run(ctx)` as in Task 1. Field files via `types.save_field`
  into `ctx.out_dir`: structure `structure_<part>.npz` (von Mises MPa on surface points, displacement mm);
  cfd `cfd_cp.npz` (+ `cfd_<quantity>.npz` for p, |U| near wall, wall shear; slices and streamlines as
  extra arrays). Metrics: structure `safety_factor`, `max_stress_mpa`, `max_disp_mm`; cfd `cl`, `cd`, `ld`,
  `lift_n`, `drag_n`, `converged`, `cells`; flight `max_tilt_deg`, `settle_time_s`, `position_error_m`,
  `throttle_margin`.
- Rules: CFD domain from the bounding box (≈3 L upstream, 6 L downstream, 2.5 span sideways and vertically),
  `locationInMesh` computed outside the aircraft, coarse/medium/fine presets ≤ 0.4 / 1.0 / 2.0 M cells,
  procs = min(cores, 4); reuse helpers from `opencae/engineering/field_analyses.py` by import (do not edit
  K09-L behaviour). Missing `ccx`/`simpleFoam` → result `error` naming the program. Flight applies to
  multirotor/vtol/tailsitter in hover; fixed wing → not_evaluated with reason.
- [ ] Tests: `test_structure_missing_material_is_not_evaluated`, `test_structure_arm_runs` (solver; SF > 0,
  field file written, float16), `test_flight_hover_quad` (no solver; settles, tilt < 30°),
  `test_flight_fixed_wing_not_evaluated`, `test_cfd_case_dictionaries` (writes the case without running:
  domain contains the aircraft, locationInMesh outside every part), `test_cfd_missing_openfoam_reports_error`
  (monkeypatch PATH), `test_cfd_quad_runs` (openfoam, slow, 30 iterations).

### Task 3: Dashboard server and page (owner: agent "web")

**Files:**
- Create: `opencae/web/server.py`, `opencae/web/dashboard/{index.html, dash.js, dash.css}`
- Test: `opencae/tests/test_dashboard_server.py`

**Interfaces:**
- Consumes: `pipeline.run_project/status/variants/iterate`, `spec.load/save/apply_overrides`,
  `types.load_field`; results.json and geometry.npz layout from Task 1; field files from Task 2.
- Produces: `DashboardServer(project_dir: Path, host="127.0.0.1", port=0)` with `.start() -> str` (URL),
  `.stop()`, `.url`, `.jobs` (one background run at a time; `progress` dict `{running, step, frac, log[],
  error}`). API: `GET /api/project`, `GET /api/results?variant=`, `GET /api/geometry?variant=` (JSON, parts
  decimated to ≤ 200 k triangles total), `GET /api/field?variant=&file=`, `GET /api/progress`,
  `POST /api/run {only?, overrides?, variant?}`, `POST /api/params {overrides}` (validates, saves), `POST
  /api/iterate {overrides, name}`, `POST /api/stop`. `/k09l/` serves the K09-L site for engine k09l (build
  on demand with `opencae.web.build.build_sites`). `main(argv)` for `python -m opencae.web.server DIR`.
- Page: visual first. Sections: Aircraft (3D model coloured by role, click part → info), Results (cards
  with traffic-light status and one-sentence summaries, big numbers), Contours (CFD / stress on the 3D
  model, quantity selector, legend), Flight (hover replay), Parameters (grouped editable form, Run button,
  live progress bar), Iterations (variant compare table and bar chart). Uses `opencae/web/vendor` three.js
  and the colour tokens of `opencae/web/templates/style.css`. Footer: "Simulation results plan the tests;
  they are not flight approval."
- [ ] Tests (no browser): server starts on a free port; `/api/project` returns the quad spec; `POST
  /api/params` with a bad key → 400 and drone.json unchanged; `/api/results` after a mass+propulsion run;
  `/` serves HTML containing `dash.js`; static path traversal (`/../`) → 404.

### Task 4a: CLI (B4)

**Files:** `opencae/cli.py`, `opencae/__main__.py`, `opencae/tests/test_cli.py`, `pyproject.toml`
(`opencae = "opencae.cli:main"`). A partial version from the stopped first attempt is in the working tree:
finish it, do not restart. Commands, flags, exit codes and JSON rule as in AGENTS.md (CLI section) plus
`dashboard DIR [--port N] [--no-browser]` and `doctor` (ccx, OpenFOAM via the detection in
`engineering/field_analyses.py`, gmsh, OCP, PySide6, QtWebEngine, free disk in the home folder).
- [ ] Tests: run `--only mass,propulsion --json` on a tmp copy of quad250; `--set bad.key=1` exits 2 and leaves
  drone.json unchanged; `status --json`; `doctor --json` keys; `python -m opencae --help`.

### Task 4b: One window, different modes (B5)

**Files:** `opencae/ui/drone/__init__.py`, `opencae/ui/drone/workspace.py` (from the partial `window.py`),
`opencae/ui/drone/new_dialog.py`, `opencae/tests/test_drone_window.py`; small edits to
`opencae/ui/main_window.py` and `opencae/app/main.py`.

**Design (U2, U5, U6):** the main window's centre becomes a `QStackedWidget` holding the existing 3D viewport
(mode "Studio", unchanged and the default) and a `DroneWorkspace` (mode "Drone"). A ribbon page "Drone" holds
New drone project…, Open drone project…, Open example ▸ (quad250, K09-L), Run all, Run selected ▸, Stop,
and Back to Studio. Switching ribbon page to "Drone" shows the Drone mode; any other page shows the viewport.
The K09-L platform and Design Monitor keep their existing commands but open as further modes in the same stack
(`setWindowFlags(Qt.Widget)` on the existing QMainWindow classes) instead of separate windows.

`DroneWorkspace(QWidget)`: left, the parameter sheet built from drone.json (grouped by section; editable
numbers with unit and an evidence chip estimate/datasheet/measured/user; Save validates through
`spec.apply_overrides` + `spec.save`, errors shown in red, file unchanged); right, a large
QWebEngineView showing the dashboard from `DashboardServer` (started when a project opens, i.e. run mode,
and stopped when the project closes or the app quits); if QtWebEngine is missing, a panel with a button that
opens the same URL in the browser. Runs go through the server's job manager so the app's status-bar progress
and the embedded dashboard show the same run. Colours and fonts from `opencae.ui.theme`. Examples are copied
to `~/.opencae/projects/<name>` before opening so the package folder is never written to.
`app/main.py`: `--drone DIR` opens the main window in Drone mode on that project.
- [ ] Tests (offscreen): the main window has the "Drone" ribbon page; opening a tmp project switches the stack
  to the workspace and the server answers `GET /api/project` 200; switching to "Home" shows the viewport;
  closing the project stops the server; NewDroneDialog builds a project from the quad STL without `exec_`;
  K09-L monitor opens as a mode (stack page), not a top-level window. Screenshot to the scratchpad.

### Task 5: Storage, AGENTS.md, docs (conductor; storage and AGENTS.md done in 541aa95 and the adoption commit)

**Files:** `opencae/engineering/field_analyses.py` (save_field float16 + compressed; load casts up),
`opencae/engineering/study.py` (delete the run folder after a successful run unless `keep`), `.gitignore`,
`opencae/examples/k09l/website/` removed from git, `open_website.bat` (build if missing),
`opencae/examples/k09l/fields/*.npz` re-saved float16, `AGENTS.md`, `START_HERE.md`, `docs/DRONE_PROJECTS.md`.
- [ ] Field round-trip test (float16 relative error < 1e-3 of range); existing K09-L tests pass; website
  test builds into tmp.

### Task 6: Integration and verification (owner: lead)
- [ ] Full test run (`xvfb-run -a python -m pytest opencae/tests -q`), quad250 full run including CFD
  (coarse) through the CLI, dashboard screenshot check with Playwright, app screenshot offscreen.
- [ ] Commit, push, deploy to `E:\Calculix\OpenCAE-Studio-0.1.0`, run `opencae doctor` and the quad on the
  user's PC in `qt_test`.
