# Generic drone app, CLI and dashboard mode: implementation plan

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

### Task 4: CLI and app integration (owner: agent "app")

**Files:**
- Create: `opencae/cli.py`, `opencae/__main__.py`, `opencae/ui/drone/__init__.py`, `opencae/ui/drone/window.py`,
  `opencae/ui/drone/new_dialog.py`
- Modify: `pyproject.toml` (`opencae = "opencae.cli:main"`), `opencae/ui/main_window.py` (actions
  `drone_new`, `drone_open` in the Home ribbon next to `k09l_monitor`), `opencae/app/main.py` (`--drone DIR`),
  `launch_opencae.bat` only if needed.
- Test: `opencae/tests/test_cli.py`, `opencae/tests/test_drone_window.py` (xvfb, offscreen).

**Interfaces:**
- Consumes: Task 1 pipeline/spec functions; Task 3 `DashboardServer`.
- Produces: `cli.main(argv) -> int`. Commands: `new DIR --cad FILE [--name] [--type]`, `run DIR [--only a,b]
  [--set k=v ...] [--variant NAME] [--keep-cases] [--force]`, `iterate DIR --set k=v ... --name NAME`,
  `status DIR`, `dashboard DIR [--port N] [--no-browser]`, `clean DIR [--results]`, `doctor` (ccx,
  OpenFOAM, gmsh, OCP, QtWebEngine availability). `--json` everywhere → exactly one JSON object on stdout;
  exit 0 on success, 2 on bad input, 1 on solver error. `DroneWindow(project_dir)`: toolbar New / Open /
  Run all / Stop / Open in browser; left dock parameter sheet (grouped form from drone.json, edits saved
  through `spec.apply_overrides`); centre tabs Dashboard (QWebEngineView on the server URL; fallback label +
  browser button) and Log; status-bar progress from the server's job state; theme from `opencae.ui.theme`.
- [ ] Tests: `opencae new` + `opencae run --only mass,propulsion --json` on a tmp copy of quad250 returns
  verdict and analyses; `--set bad.key=1` exits 2; `status --json`; `doctor --json` keys; DroneWindow opens
  offscreen with a project and closes cleanly (server stopped).

### Task 5: Storage, K09-L as a project, AGENTS.md, docs (owner: lead)

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
