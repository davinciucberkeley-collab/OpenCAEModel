# Review R5: Results Hub (V1–V4), CFD foamToVTK fix (C0), residual history, 2026-11-26

Reviewer: R5, a fresh Opus instance in the reviewer role. Batch: `git diff 81375e4..HEAD` (commits 60b91ff..5c06c31, 29 files,
+9 166 / −49) plus commits 81375e4 (C0: VTKflow without `-poly-decomp`, a failed volume export keeps the solve) and
2be4d94 (CFD stores `extra.residual_history`). Inputs: `AGENTS.md`, `standards/agents.md` §Reviews,
`plans/2026-11-26-results-hub.md` (H1–H8), `plans/2026-11-26-results-hub/contract.md`, and the conductor's criteria 1–8.

**Standards gaps (reported, not invented around):** `standards/review-scorecard.md` still does not exist, so I used the layout
of `reviews/2026-11-25-mbd-panels.md`. `plane validate` (named in `standards/agents.md`) does not apply to this repo, so the
validator is the conductor's pytest command. I edited nothing outside `reviews/`. My throwaway scripts are in my scratchpad.

## 1. Validators

The brief's command, as given (`test_hub_backend, test_hub_frontend, test_hub_motion, test_hub_ui, test_drone_cfd_solver`):
```
1 failed, 113 passed, 56 errors in 73.78s      EXIT 1   (run twice, same result)
FAILED opencae/tests/test_hub_ui.py::test_saved_layout_wins_over_the_default
ERROR  every MainWindow test in test_hub_ui.py (setup and teardown), and every test of test_drone_cfd_solver.py (teardown)
E   AttributeError: 'NoneType' object has no attribute 'add_observer'      opencae/ui/viewport.py:169
    (stderr: "QOpenGLWidget is not supported on this platform.")
E   AttributeError: ... closeEvent(): 'MainWindow' object has no attribute '_drone'   (teardown of the half-built window)
```
Isolation:

| Run | Result |
|---|---|
| `test_hub_ui.py` + `test_drone_cfd_solver.py` | 38 passed, 1 skipped, EXIT 0 |
| `test_hub_motion.py` + `test_hub_frontend.py` + `test_hub_ui.py` | 87 passed, 1 skipped (screenshots need `OPENCAE_TEST_SHOTS`), EXIT 0 |
| `test_hub_frontend.py` + `test_hub_ui.py` | 61 passed, 1 skipped |
| `test_hub_motion.py` + `test_hub_ui.py` | 52 passed, 1 skipped |
| **`test_hub_backend.py` + `test_hub_ui.py`** | **1 failed, 40 passed, 44 errors** |

Root cause: `opencae/hub/render.py:91` sets the process-wide `pyvista.OFF_SCREEN = True` on the first PNG render. Every
`QtInteractor` made after that takes `off_screen = pyvista.OFF_SCREEN` (pyvistaqt `plotting.py:249-250`) and gets
`iren = None`. This is a product bug, not a test-order quirk (see B1). The full suite will show the same errors for every GUI
test that runs after `test_hub_backend.py`.

Hand checks (scripts in my scratchpad):
- `offscreen_leak.py` (xvfb, xcb platform, like the app): a `Viewport` before a render is fine. After one call of
  `render._draw(...)`: `pv.OFF_SCREEN` is True, a new `Interactor` has `iren = None`, and `Viewport()` raises
  `AttributeError: 'NoneType' object has no attribute 'add_observer'`.
- `err_run.py`: (a) a drone project with runs `r1 done`, `r2 error`. The Overview of `latest` (r2) shows r1's mass card under
  run r2, and `compare(r1, r2)` lists r1's values as r2's. (b) `motion.motion(entry, "<../ path>")` read a `results.json`
  from a folder outside the project ("missing extra.t, extra.pos, extra.att" came from the outside file).
- A truncated `.npz` raises `zipfile.BadZipFile`, which is not one of the exceptions `adapters._drone_parts` catches.

## 2. Criteria

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | No fabricated numbers | **FAIL** (should-fix severity) | Adapters, render and K09-L replay read only stored data. Derived hover rpm is labelled as such (`motion.py:635-657`, channel note "full-throttle rpm x sqrt(thrust fraction)"). Missing data gives a note: `adapters.py:361,401,412,423`, `motion.py:552-575`, `render.py:186-187`. **But** a run that ended with `status: error` is shown with the previous run's numbers (`adapters.py:222-225`, S1). |
| 2 | Evidence basis end to end; failures visible | **FAIL** | **B2:** the audit labels bare numbers `user`; the project's rule is that a bare number is an `estimate` (`adapters.py:565,579` vs `docs/DRONE_PROJECTS.md:27`, `types.py:44`). Studio cards and audit are always `ok`, even for out-of-date results (`adapters.py:827,1023`, S3). Studio FEA cards drop the material basis (S9). Done well: drone failures are visible on Overview (`hub.js:188-189`), 3D notes (`adapters.py:416-417,450-452`), Audit (`adapters.py:640-643`, `hub.js:533-534`). The C0 change keeps forces and states the failed export in the summary and in `extra.postprocess_problems` (`cfd.py` `_to_vtk`/`_run_foam`), and the hub shows it as 3D notes and audit problems. |
| 3 | Never presented as flight approval | PASS | Footer line in `index.html` (in every view of the single page, `test_hub_frontend` `footer_line`). Drone motion disclaimer `motion.py:37`, shown for hover/flight in `motion.js:865`. K09-L basis "plans tests, not flight evidence". Nit N2 (verdict chip "Pass"). |
| 4 | Local server security | **FAIL** (should-fix severity) | Hub API is host-checked (`server.py` `_hub_get`: `_host_ok` before any `/api/hub/` route). POST is host- and origin-checked. `/hub/` static uses `_static`, whose resolve and parent check is safe. `pid` is matched against the registry only. `field` and `view` are looked up or validated. `opencae://` interception only emits `show3d` and refuses other `opencae:` URLs (`hub_view.py` `HubPage.acceptNavigationRequest`). The page uses `textContent` everywhere (only `plots.js:182,192` uses `innerHTML`, with `esc`). **But** `/api/hub/motion?run=` is joined into a file path unchecked (`motion.py:504`, S2). `/api/hub/client` logs CR/LF unescaped (N1). |
| 5 | Robustness | **FAIL** | **B1** (in-app PNG fallback breaks every later 3D view). Corrupt `geometry.npz` → 500 for scene and image (S4). Motion caches are unlocked, and K09-L and studio builds are not deduplicated (S7). Done well: K09-L motion payload < 2.6 MB with decimation (`test_real_k09l_payload_size_bound`); runs.jsonl lines without snapshots give a notice; corrupt JSON lines are skipped; studio projects without results list 0 runs. The native viewer uses `opencae.ui.viewport.Interactor` (`hub_view.py:218`) and is closed at window close and at `aboutToQuit`. |
| 6 | Windows specifics | PASS (logic). H8 unverified | `crash.chromium_flags` follows `webview_graphics` (software/gpu/off) and `OPENCAE_WEBVIEW_GRAPHICS`. User `QTWEBENGINE_CHROMIUM_FLAGS` always wins (`crash.py` `prepare_qt_environment`). Tests are parametrised over the setting. Browser opening uses `QDesktopServices.openUrl` with the http URL. No file URLs or WSL paths are involved. The new flag set (`--use-angle=swiftshader …`) has not been run on Windows. Nit N6 (safe mode on Windows + `gpu`). |
| 7 | Contract + H1–H8 reachable | **FAIL** (via B1) | H1: one page for drone, studio and K09-L. H2: history, open run, compare (studio: only the latest result per study). H3: surface fields, slices, streamlines, residuals (stored from 2be4d94 on), C_L/C_D. H4: Audit tab. H5: MBD, hover and K09-L replay. H6: `apply_default_layout` + View > Reset layout, and a saved layout wins. H7: Help button, F1, generated from `grammar.VERBS` (`test_help_lists_every_grammar_command`); commands run in Python mode (`console.py` `_is_command`). H8: three fallbacks (PNG, native viewer, browser), **but the first one (PNG) breaks the second (native viewer), and also the Motion workspace and K09-L platform 3D views (B1)**. |
| 8 | Tests test behaviour | **FAIL** | The brief's command fails (above). Backend and motion tests use real fixtures and the real pipeline snapshot hook (good). But `test_audit_inputs_carry_evidence_status_and_group` asserts the wrong evidence rule (`test_hub_backend.py:723-724,732`). The UI tests stub the backend (`fake_hub`, `test_hub_ui.py:273-296`), and the front-end tests drive the page against a stub server (`test_hub_frontend.py` docstring). No test runs the real chain app → in-process server → PNG → native viewer, which is where B1 lives (S6). Hard-coded session scratch paths are committed (S5). |

## 3. Blocking issues

**B1. Rendering one server PNG switches every later 3D view of the app off-screen.** `opencae/hub/render.py:91`
`pv.OFF_SCREEN = True` is global. The in-app hub uses the in-process `DashboardServer` (`hub_view.py` `ensure_server`), so
the PNG fallback runs inside the app. That fallback is exactly the path on the user's PC, where WebGL fails (`hub.js:405-411`).
After it, every `QtInteractor` made in the process is off-screen:
- **Show in 3D viewer** (`hub_view.py:218`): `iren = None`, nothing appears on screen, and no error is shown, even though this
  viewer is meant to be the WebGL-free fallback.
- The **Motion workspace** (`assembly/workspace.py:441`, built lazily, `:1595`) and **K09-L platform** (`main_window.py:1445`,
  `platform/window.py:169`, `contours.py:345`) get `AttributeError` at `viewport.py:169`.

The off-screen `vtkRenderWindow` also renders in a server thread while the GUI thread renders VTK. This app already had
several GL crash fixes.

*Failure:* open Results Hub on the user's PC → 3D tab (PNG) → Motion → gripper: the workspace fails to build. The test
command above fails the same way.

*Smallest fix:* delete line 91 (`pv.Plotter(off_screen=True)` at line 92 already renders off-screen). Then add a test:
`render_png` followed by `Interactor()` has a non-None `iren`. Better: render in a subprocess, or post the render to the GUI
thread, so VTK never runs on two threads.

*Note:* while I was writing this review, an uncommitted change to `opencae/hub/render.py` and
`opencae/tests/test_hub_backend.py` appeared in the working tree. It removes line 91 and renders in a child process when a
QApplication exists. I reviewed HEAD, not that change. On re-review, check three things: the change has a test that
creates an Interactor after a render; `python -m opencae.hub.render` exists and works on Windows (`CREATE_NO_WINDOW`, the
venv's `sys.executable`); and the child process's failure text reaches `message_png`.

**B2. The audit shows bare numbers (which are estimates) as `user` evidence.** `adapters.py:565`
(`node.get("status") or st or "user"`) and `:579` (`st or "user"`). `docs/DRONE_PROJECTS.md:27`: "A bare number counts as
an **estimate**". `types.Param.status` defaults to `ESTIMATE`. `user` means "a choice you made", and `_RANK_BASIS` ranks
`user` with `measured` (`adapters.py:30`).

*Failure:* `"kv": 2400` in drone.json shows in Audit → Inputs as `basis: user`, so an estimate looks like a deliberate value.
This breaks "Never present an estimate-based number as a verified result". The test enshrines it
(`test_hub_backend.py:723-724`).

*Smallest fix:* default to `"estimate"` in both places. List `analyses.<id>.*` solver settings (iterations, preset) as
`configuration` (no basis) instead of `user`. Update the test.

## 4. Non-blocking issues

**Should-fix**

- **S1. An errored run shows the previous run's numbers.** `adapters.py:222-225` treats the last row of a variant as "live"
  even when its `status` is `error`. The pipeline writes no `results.json` then (`pipeline.py:309-312`, and
  `snapshot_run` is skipped for this reason, `pipeline.py:273`). Failure: Overview of run r2 (error) shows r1's cards, and
  Compare r1↔r2 shows a delta of 0 for every metric. The notice does name the error. Fix: in `_DroneRun`, use the live file
  only when `row.status != "error"`; otherwise `results=None` with the notice.
- **S2. Path traversal through `run` in `/api/hub/motion`.** `motion.py:504`
  `_read_json(rdir / "history" / run / "results.json")` with `run` taken from the query string unchecked. Unlike
  `adapters._drone_resolve`, motion does not first match `run` against runs.jsonl. `adapters.motion` passes it straight
  through (`adapters.py:1314-1319`). Shown by `err_run.py`. The impact is local only and limited to files named
  `results.json`, but the criterion says "impossible". Fix: look up the row in `_runs(path)` first and use its `run_id`, or
  reject `run` unless it matches `^[\w.:@-]+$` without `..`.
- **S3. Studio results are always `ok`, and failed solves are invisible.** `_fea_card` and `_studio_audit` hard-code
  `"status": "ok"` (`adapters.py:827,1023`). They ignore `results.completed` and `results_are_current()`, which the rest of
  the app uses (`postprocessing/report.py:298`, `model_tree.py:21`). A failed solve resets `ResultsInfo()`
  (`session.py:776`), so the study simply disappears from the hub's runs (`_rdir`, `adapters.py:688-697`). Fix:
  `status = "watch"` + text "out of date" when `not project.results_are_current(st)`. List a study with a `results/run_*`
  folder but `completed=False` as `error`, with the `.sta`/`.log` tail.
- **S4. A corrupt `.npz` gives a 500 error and a broken `<img>`.** `adapters.py:352` catches `(OSError, KeyError, ValueError)`
  but a truncated npz raises `zipfile.BadZipFile`. `render.py:184` calls `adapters.scene` outside its `try`, so the
  "always a readable PNG" promise (`render.py:5-6`) breaks. This is likely on the user's disk-short PC. Fix:
  `except Exception` with the note, and move line 184 into the `try` with `message_png`.
- **S5. Session scratch paths are committed in tests.** `test_hub_frontend.py:23` and `test_hub_motion.py:32` default to
  `/tmp/claude-0/-home-claude/cca99fdc-…/scratchpad/...`. Fix: `tmp_path_factory`, or an env var with a tmp default.
- **S6. No end-to-end test of the in-app chain.** Add one GUI test: real `DashboardServer(None)`, a real fixture project,
  `HubView.open_project`, GET `image.png`, then `show_native()` draws with a non-None `iren`. That test would have caught
  B1. Optionally also drive the real page against the real backend once (the stub server can drift from the adapters' shapes,
  e.g. `location`, `studies`).
- **S7. Unlocked caches and duplicate heavy builds under `ThreadingHTTPServer`.** `motion._SESSIONS`/`_K09L` (`motion.py:273,683`)
  are bare `OrderedDict`s mutated from request threads. `_session` opens the real project in parallel for the concurrent
  `/motion` and `/plots` requests the Motion tab sends. `_k09l_bundle` (`adapters.py:1039-1064`) has no in-flight
  deduplication, so opening K09-L builds the bundle once per concurrent request (project/scene/plots/audit) on an 8 GB
  machine. Fix: one lock + "building" event per key, as `_studio_geometry` half-does.
- **S8. A stale K09-L analysis with a PASS verdict shows `ok`.** `adapters.py:1155-1157` prefers the verdict over the
  engine status. Only `not run`/`failed` override it, and `stale` does not. Fix: `stale` → `watch`, and keep the reason in
  the text.
- **S9. Studio FEA cards have no evidence basis.** `adapters.py:821-827` sets `basis: ""`, although the audit knows each
  material property's status (`adapters.py:1000`). Fix: card basis = `_weakest` of the assigned materials' statuses.
- **S10. One quantity, two names.** `adapters.py:543` labels `extra.motor` as "motor commands / Command". The flight model
  stores the lagged thrust divided by `tmax` (`flight.py:241`), which `motion.py` correctly calls "thrust (fraction of the
  maximum)". Fix: use the motion wording.

**Nits**

- N1 `server.py:647`: `error`/`ua` are logged with CR/LF intact, so a local page could forge log lines. Use `%r` or strip
  `\r\n`. The POST body has no size cap (`server.py` `do_POST`, existing code).
- N2 `hub.js:66-70`: "ready for physical tests" is shown as **Pass**. Say "Ready for physical tests". "not ready" gets the
  grey `nev` class instead of `fail`.
- N3 `hub.js:187`: a run-level error notice is drawn as an `info` banner. Use `err` when the run status is error.
- N4 `index.html` "Open in browser" (`target=_blank`) does nothing inside `QWebEngineView` (no `createWindow`). Hide it
  in-app (the native button exists), or intercept it like `opencae://`.
- N5 `motion.js:1011`: notes and basis are collapsed by default. The hover payload's `status`/`summary`
  (`motion.py`, end of `_hover`) are never shown, so a flight result with status `fail` animates with no word about it. If
  the mass CG is missing, poses use `[0,0,0]` silently (`motion.py` `_hover`, `cg = ...`).
- N6 `crash.py:493`: on Windows, safe mode with `webview_graphics=gpu` leaves Chromium on the GPU. Safe mode should be the
  conservative path on every platform.
- N7 For an older run where CFD failed, the 3D tab says only "fields kept for the latest run only" (`adapters.py:405-407`).
  Add the failed analyses' summaries as well.
- N8 `results/history/<run>/results.json` grows with every run and nothing prunes it. That matters on a disk-short PC:
  keep the last N, or let `opencae clean` prune it.
- N9 `render.py:194`: the image title says "run latest" instead of the resolved run id.
- N10 `adapters.py:524-526`: "final residuals" are plotted at x = 0 when `iterations` is missing. Better: no x, plus the note.

## 5. Could it be simpler or removed?

- `adapters._parse_residuals` (`adapters.py:459-486`) duplicates `cfd.residual_history` (2be4d94). Keep one: call
  `cfd.residual_history(log, max_points=None)`.
- There are two ways to open a Studio project for display: `adapters._studio_geometry` (temporary copy, locked cache) and
  `motion._session` (in place, unlocked LRU). One shared helper would fix S7 and the inconsistency.
- `hub_view.start_hub_server` falls back to `DashboardServer(home_dir())`. That server would present `~/.opencae` as a
  drone project, and `DashboardServer(None)` cannot fail where the fallback would succeed. Remove the loop.
- `render.py:91`: see B1. Removing the line is the fix.

## 6. Process

- The plan's H1–H8 Status column is still empty except C0. It should be filled before the merge.
- Commit 6b7f749 is a "WIP … (in progress)" commit on the branch. That is acceptable, since the conductor commits.
- The builders reported their per-file pass checks green. That is true for each file alone, but the brief's combined command
  fails (B1). File-order effects like this are why the pass check should be the combined command.

## Verdict

**CHANGES REQUIRED: 2 blockers (B1, B2), 10 should-fix, 10 nits.** The structure is sound and most of the engineering rules
hold: missing data gives notes, drone failures are visible on all three surfaces, the C0 fix keeps a finished solve, and
there is no `innerHTML` injection. B1 is a one-line fix plus a test, and it currently breaks the very fallback chain built
for the user's PC (H8) and the brief's test command. B2 misstates evidence in the audit, against `AGENTS.md`. Re-review:
check B1 and B2 first, then S1–S4.
