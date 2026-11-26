# Review R4: assembly + MBD (M1–M3), Progress Monitor (J1), Command Console (J2), 2026-11-25

Reviewer: R4, a fresh Opus instance in the reviewer role. Batch: `git diff 620a11c HEAD -- opencae` (commits a28df47..c866820,
45 files, +17 427 / -67). Inputs: `plans/2026-11-25-assembly-mbd.md` (A1–A16, P1–P7), `plans/2026-11-25-assembly-mbd/contract.md`,
`plans/2026-11-25-assembly-mbd/user-brief-panels.md`, `AGENTS.md`, `plans/conductor-edits.md`, `standards/agents.md` §Reviews,
`.claude/agents/reviewer.md`. Criteria R1–R9 come from the conductor's brief.

**Standards gaps (stop rule, reported):** `standards/review-scorecard.md` does not exist (cited by `standards/agents.md` and
`.claude/agents/reviewer.md`), so I used the layout of `reviews/2026-11-23-wave3.md`. `plans/agent-log.jsonl` does not exist, so
the Process section below comes only from the git log and the plan. R1–R9 are defined in the brief; no criterion was invented.

## 1. Validators

The brief's command, as given:
```
$ cd /home/claude/OpenCAE-Studio && PYTHONPATH=$PWD QT_QPA_PLATFORM=offscreen timeout 2400 xvfb-run -a /home/claude/venv/bin/python \
    -m pytest opencae/tests/test_assembly_model.py opencae/tests/test_mbd_gripper.py opencae/tests/test_mbd_drone.py \
    opencae/tests/test_mbd_transfer.py opencae/tests/test_assembly_ui.py opencae/tests/test_job_monitor.py \
    opencae/tests/test_console_commands.py opencae/tests/test_console.py -q
........................................................................ [ 42%]
.......................................................................Segmentation fault
EXIT 139
```
168 tests were collected. The process crashed after 143 had passed, with no failures before the crash. A second run with `-v` crashed in the same place:
```
opencae/tests/test_console_commands.py::test_layout_survives_closing_and_reopening PASSED [ 85%]
opencae/tests/test_console_commands.py::test_open_gripper_then_select_left_finger_is_the_same_selection_as_a_pick Segmentation fault
EXIT 139
```
I isolated it as follows:

| Run | Result |
|---|---|
| `test_console_commands.py` alone | 44 passed, 61 s, EXIT 0 |
| `test_job_monitor.py` + `test_console_commands.py` | 82 passed, 94 s, EXIT 0 |
| `test_assembly_ui.py` + `test_console_commands.py` | segfault, EXIT 139 |
| **Minimal pair:** `test_assembly_ui.py::test_install_adds_ribbon_page_and_stack_page` + `test_console_commands.py::test_open_gripper_then_select_left_finger_is_the_same_selection_as_a_pick` | segfault, EXIT 139 (also with `test_open_gripper_example_fills_the_tree` as the first test) |

Native stack with `PYTHONFAULTHANDLER=1 -s`, preceded by `vtkShaderProgram: Could not create shader object`:
```
pyvista/plotting/plotter.py:2566 render <- opencae/ui/viewport.py:122 _add <- viewport.py:187 show_geometry
<- main_window.py:639 refresh_view <- main_window.py:1274 _reset_after_load <- ui/assembly/workspace.py:1591 after_load
<- main_window.py:1292 cmd_open <- ui/assembly/workspace.py:460 open_path <- workspace.py:449 open_example
<- ui/commands/actions.py:484 open_target <- console.py:839 execute <- test_console_commands.py:319
```
The rest of the suite never ran: `test_console_commands` from test 145 on, and all of `test_console.py`. Nothing hung.

Things I checked by hand, with scripts in my scratchpad:
- **Independent physics check** (`calc1.py`, built a fresh gripper and ran A/B/C through `run_study`):
  - B: my hand value α = τ/(2·I_pivot) = 128.4581 rad/s². MuJoCo gives q̈(0) = 128.4709, an error of +1.0e-4 relative. θ(0.02 s) agrees to 2e-8 relative. The follower is exactly −q_leader. The coupling violation is 1.0e-9 rad. The left hinge reaction at t = 0 is Fx = 0.024975 N, against my hand value of m·α·L/2 = 0.024972 N.
  - C: the hold effort is 0.6000000000 N·m, equal to the hand value 2FL = 0.6. The left-hinge reaction in the assembly frame is (4.6985, −1.7101, 0) N, which equals −R(−20°)·(−5, 0) N. Magnitude and sign match the "BY parent ON child" convention.
  - A: q(1 s) = 0.34906585039887 rad, exactly 20°. The inverse-dynamics effort at mid-ramp is 1e-14 N·m. That is correct: the hinge axis is parallel to gravity and the ramp has q̈ = 0.
- **Gap formula** (test docstring `test_mbd_gripper.py:100-106`): I re-derived gap = 2p − h·cosθ − 2L·sinθ by rotating the inner corner (h/2, L) by −θ about the left pivot. At 20° it gives 15.2 mm, and I agree with the formula.
- **Cantilever hand value**: δ = F·L³/(3EI) = 5·60³/(3·68 900·(10·4³/12)) = 0.098 mm, consistent with `test_mbd_transfer.py:188-207`.
- **Old projects**: I loaded and re-saved copies of `tiltwing_vtol.ocae` (v2) and `l_bracket.ocae` / `cantilever_beam.ocae` (v1). They migrate to v3. The re-saved v2 file loses `studies[0].mesh.stats`, but the baseline 620a11c (checked in a temporary worktree, since removed) loses exactly the same keys, because the copy has no `_data`. This is **not a regression**.
- **Files**: `git status` is clean after the runs, and `~/.opencae` and `~/.config/OpenCAE` are untouched (mtimes predate my session).

## 2. Criteria

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| R1 | Prescribed vs torque-driven | PASS | **Prescribed:** joints are kinematic. q, q̇ and q̈ are set from the profile each step, and the effort is solved by Newton iteration so that q̈ = commanded (`mbd/run.py:62-69, 108-117, 139-174`). Limits are checked on the command, not on a servo (`build.py:187-200, 248-250`). **Torque-driven:** forward dynamics with `qfrc_applied` (`run.py:135-137`). The labels are stored per joint (`run.py:254-257`). Caveat (N7): a position ramp has q̈ = 0 between kinks (`run.py:66`), so the impulsive effort at a kink is not reported. |
| R1 | Coupling constraint | PASS (with caveat) | A joint equality constraint with `polycoef 0 r` and unit scaling `ratio·qf/ql` (`build.py:279-286`). It starts on the manifold, with a warning if the input was inconsistent (`run.py:96-105`). Violation is recorded per sample (`run.py:184-187`). **Caveat:** it is a soft constraint (`SOLIMP 0.9999`, `build.py:25`). The constraint softness inflates the reported q̈ and reaction by about 1e-4 relative (B case above). The 1e-3 test tolerance (`test_mbd_gripper.py:182`) hides this, and it is not documented (N7). |
| R1 | Reaction frame and sign | PASS | `cfrc_int` of the child, moved from the subtree COM to the joint origin, then into the joint frame carried by the parent (`run.py:213-219`). Actuator effort is removed along the axis (`run.py:220-224`), and the totals are kept as well. Both my numbers above confirm the "BY parent ON child" sign. Caveat: the "actuator effort excluded" reaction still contains damping and the coupling-constraint torque along the axis. For a coupled follower, z is the transmission torque. This is not stated in `results.py:164` or `run.py:283-285` (N7). |
| R1 | Gap formula | PASS | Analytic formula in the test (see above). The UI derives the inner tip corner from bbox corners and the joint origins (`workspace.py:1328-1364`) and applies `pose_transforms`. It is labelled "distance between the inner fingertip corners" (`playback.py:249`). |
| R1 | Transfer frame (Rᵀ F) | PASS | CAD vector = Rpᵀ·R(t)ᵀ·v_assembly, and a body-frame load = Rpᵀ·v (`transfer.py:198-205, 223-224`). The moment is about the root joint in CAD axes (`transfer.py:227, 249-251`). |
| R1 | Equilibrium check not applied | PASS | The root reaction goes only into `equilibrium_check`, with residual = reaction + applied (`transfer.py:262-280`). The structural study contains only the fixed root, the applied loads and gravity (`transfer.py:309-316`). |
| R1 | Static-hold acceleration guard | PASS (with caveat) | The transfer is refused if max\|q̈\| along the chain exceeds `ACCEL_TOL`, unless `force_allow`. If \|q̇\| > 1e-6 there is only a warning (`transfer.py:177-193`). Caveats: the tolerance is 1e-3 in mixed units (rad/s² or mm/s², `transfer.py:34`). The guard reads q̈, which is 0 by construction on prescribed constant-velocity ramps, so a moving finger in study A passes with only the centripetal warning. |
| R1 | Units at every boundary | PASS (with nits) | **UI:** dialogs convert deg→rad and rpm→rad/s with explicit factors (`dialogs.py:29, 269, 315, 659-670`). **MJCF:** mm→m only in `build.py` (`build.py:217, 250, 256, 266, 283`). **Results:** back to the project unit (`run.py:193-201`). **CSV:** a unit is given per column (`results.py:153-205`). **Transfer:** N·mm moment and mm/s² gravity (`transfer.py:243, 254`). **Nits:** `constraint_error` is in "m for prismatic" while every other prismatic quantity is in the project unit (`results.py:179`). The transfer summary hard-codes "mm²" (`transfer.py:338-339`). |
| R2 | No fabricated numbers; grip force only with an explicit load | PASS | No contact model, stated in `run.py:30-31` and `backend.py`. Study C uses explicit `AppliedLoad`s (`mbd_examples.py:133-139`). The transfer refuses a case with no load (`transfer.py:258-260`). |
| R2 | Drone not a flight prediction; prescribed thrust vs CFD | PARTIAL (non-blocking) | The model and the example state it (`model.py` ThrustModel `source`, `mbd_examples.py:150-152, 174-177`, `run.py:37-38`), and the assumptions are shown after a run (`workspace.py:203`). However, the UI never shows the thrust-model source (prescribed vs CFD): `ui/assembly/*` has no occurrence of "thrust", "cfd" or "flight". The rotor assumption is printed for every study, including the gripper (`run.py:37-38`) (N5). |
| R2 | Failures and constraint errors visible | PARTIAL (non-blocking) | Model errors list every problem (`build.py:205-211`), and instability raises with the time and the cause (`run.py:176-182`). Failures reach the job card and the Study tab (`workspace.py:1318-1325`). `meta.warnings` (for example "initial q of the follower was reset") and `constraint_violation_max` are stored but not shown in the UI: no occurrence in `ui/assembly/*` (N5). |
| R3 | v1/v2 load and solve; v3 migration safe; no loss on save | PASS | `io.py:47-56` (`setdefault`, studies untouched). New `Study.mbd/transfer` fields default to empty. Tests: `test_assembly_model.py:112-178` (load and solve, `solver`-marked). My round-trip of v1/v2 files shows no regression against the baseline. Note: files saved by this build are v3, which older builds refuse. That is expected but should go in the release notes. |
| R4 | Console, Run button and Motion mode use the same services and registry | PASS | The console's `run motion` calls `ws.run_study()` (`actions.py:1074-1081`), the same method as the ribbon Run and the top-bar Run (`workspace.py:311, 1567-1568`). That method uses `track_callable` with target `st.name` (`workspace.py:1264-1272`). The duplicate check uses the same (kind, target, project) key (`actions.py:1067`, `monitor.py:1113-1118`). Structural runs go through `win.cmd_generate_mesh`/`cmd_solve` (`actions.py:971-986`). The workspace project IS the session project (`workspace.py:4-5`). `test_console_run_is_the_same_service_job_as_the_run_button` passes in isolation. |
| R4 | Results in the existing viewer | PASS | Structural results use the Studio viewer. Motion results are opened by the registered `mbd.playback` opener (`workspace.py:1534-1538, 1620-1626`). |
| R5 | Off the GUI thread; QThreads tracked | PASS | `_CallableThread` is tracked with cancel (`monitor.py:1155-1163`). The fallback thread is tracked (`workspace.py:1277-1298`). Registry events re-enter the GUI thread through signals (`monitor.py:607, 776`, `actions.py:302`). Callbacks are bound methods, so they are queued (`monitor.py:1157-1159`). |
| R5 | No Qt calls from workers | PASS (with caveat) | The worker only emits `bridge.progress` (`workspace.py:1255-1257`). Caveat: the worker reads the live `project`/`study` and writes `study.results` (`run.py:355-363`) while the GUI stays editable. The joint and motor dialogs are not blocked during a run. A deleted joint makes `_meta` raise (`run.py:259-261`) (N8). |
| R5 | Refcount reserve covers new signals | PASS | The reserve now covers None, True and False (`qt_guard.py:47-51`). The new signals carry `(float, str)`, `(str)`, `(object)` and bool `toggled` lambdas (`dialogs.py`), all covered. |
| R5 | No modal dialog that can hang | PASS (with caveat) | Every new dialog is shown with `.show()` (`workspace.py:976-1446`). The remaining modal calls are `QMessageBox.warning/question` in `workspace.warn/confirm` (`workspace.py:572-576`) and file dialogs. The tests patch them. |
| R5 | Stability | **FAIL** | The validator suite segfaults in native VTK rendering when a console test opens the gripper after any `test_assembly_ui` test (§1). The cause is not confirmed. The Motion workspace creates a second `Viewport` (`workspace.py:403`), which the main window's `closeEvent` never closes: it closes only `self.viewport` (`main_window.py:2914`). My one experiment, closing that plotter in `closeEvent` via a pytest plugin, did **not** stop the crash, so that is not the whole cause (B1). |
| R6 | Panels resizable, collapsible, restorable; layout saved; existing docks kept | PASS | Left monitor and right console docks with View entries. `saveState(2)`/`restoreState(2)` (`monitor.py:916-931`, `main_window.py` `_install_extensions`). Tests pass: `test_monitor_dock_sits_left_without_hiding_the_model_tree`, `test_collapsing_the_monitor_gives_the_room_back`, `test_layout_survives_closing_and_reopening` (both files). |
| R6 | Concise default; clarifications; real progress only; per-project history; parameter study display | PASS | Studio mesh and solve report no fraction (activity indicator only). The MBD fraction is step/n (`run.py:340-341`). The drone fraction is i/n analyses (`platform/monitor.py:735`). History: `<data>/jobs.jsonl` (`registry.py:271-311`). Cases are expandable (`test_cases_expand_individually`). Clarifications are inline (`actions.py:444-447, 598, 698`). |
| R6 | Motion-mode click cost | PASS (friction noted) | **Open the gripper:** Motion tab, then Gripper = **2**. **Set a motor command:** select the motor in the tree, Properties "Edit…", type, OK = **3** + typing (no double-click edit, N10). **Play:** Run, then Play = **2**. **Plot the gap:** Plots tab, then the Gap check = **2**. **Create a load case:** pick study C in the selector (1–2), Run, select the finger, Load case, OK; the faces are pre-selected (`dialogs.py:1175-1189`) = **5–6**. Total is about 15. Acceptable. The main friction is that the load case needs a time at rest, and that is not explained until the error appears. |
| R6 | Side effect on the user's study | FAIL (non-blocking) | A console parameter study sets `mesh_settings.size_max` permanently (`actions.py:1604-1606`). The geometry is restored, but this setting is not (N9). |
| R7 | MuJoCo missing: message + hint | PASS | `backend.py` gives the reason and `pip install mujoco`. The UI shows `python -m pip install mujoco` and does not run (`workspace.py:1226-1231`, test `test_mujoco_unavailable_…`). The console says it in one sentence (`actions.py:1061-1065`). |
| R7 | Windows install path | **FAIL** | `mujoco` is missing from `requirements.txt`, from `pyproject.toml:13-24`, from the `INSTALL_WINDOWS.md:26` pip line and from the PyInstaller spec (`packaging/opencae_studio.spec:37`). `opencae doctor` does not report it. A user who follows the documented install gets a Motion mode that can never run. The MBD tests do not skip without it (`test_mbd_gripper.py:68-72` asserts it is available), so a `pytest` run on that machine fails about 40 tests (B2). |
| R7 | Paths and file locks; QtWebEngine | PASS | Paths use `Path`. `jobs.jsonl` is opened and closed per write (`registry.py:306-311`). MuJoCo warnings are captured instead of being written to `MUJOCO_LOG.TXT` in the cwd (`run.py:42-55`). The batch uses no WebEngine. |
| R8 | Simplicity / duplication | FAIL (non-blocking) | `examples_dir`/`projects_home`/`copy_example` are duplicated verbatim (`workspace.py:42-65` and `actions.py:54-76`). Unit tables are duplicated (`grammar.py:98-116`, `dialogs.py:29`, `plots.py:25`). Dead code: `… if False else …` (`actions.py:1096, 1555`). The gap heuristic lives in the Qt widget (`workspace.py:1328-1385`), so the examples and the console cannot reuse it. The load-case preview deep-copies the project and regenerates the B-rep on every spin-box change (`workspace.py:1489-1498`, `transfer.py:166-171`). |
| R9 | Tests meaningful and isolated | PASS (with caveats) | Hand calculations with stated tolerances. QSettings are isolated (conftest and per-test ini files) and `OPENCAE_HOME` is set to `tmp_path`. Caveats: the screenshot paths are hard-coded to an agent scratchpad (`test_assembly_ui.py:32`, `test_console_commands.py:15`, `test_console.py:10`), which on Windows creates `C:\tmp\claude-0\…` (`SHOTS.mkdir`). The 1e-3 tolerance on q̈ hides the 1e-4 constraint softness. The suite is not robust to file order (B1). |

## 3. Blocking issues (release blockers)

**B1. The validator suite crashes (segfault, exit 139). The batch pass criterion is not met.**
- **Reproduction:** `test_assembly_ui.py::test_install_adds_ribbon_page_and_stack_page` followed by `test_console_commands.py::test_open_gripper_then_select_left_finger_is_the_same_selection_as_a_pick`. Stack: §1.
- **Effect:** 25 tests never ran, all of `test_console.py` included.
- **Fix:**
  1. Give the Motion workspace a teardown. In `AssemblyExtension`, hook the main window close: call `ws.viewport.close_plotter()`, stop the playback timer, and drop `mw._reset_after_load` wrapping (`workspace.py:1584-1592`). The wrapper closes over a dead workspace.
  2. Find why a second window's first `render()` gets "Could not create shader object". A likely culprit is the shared GL context of the closed workspace's QtInteractor, given `AA_ShareOpenGLContexts`.
  3. Until that is fixed, at minimum run the GUI test files in separate processes, and add a regression test that creates, closes and re-creates a `MainWindow` with the Motion page visited and then opens the gripper.
- **Real-app impact:** a user has one window, so the impact may be small. But the same leak could crash at exit or on project reopen, and nobody has shown that it doesn't.

**B2. The motion backend is not part of the documented install (Windows).**
- **Problem:** see R7 for where MuJoCo is missing and what fails without it.
- **Fix:**
  1. Add `mujoco==4.0.1` to `requirements.txt` and the `INSTALL_WINDOWS.md` pip line, and `mujoco>=3` (or an `mbd` extra) to `pyproject.toml`.
  2. Add a MuJoCo row to `opencae doctor`.
  3. Add a `pytest.importorskip("mujoco")` (or a `skipif(not available())`) in the MBD test modules.
  4. Add the hidden imports and data files to the spec.

## 4. Non-blocking issues

- **N1** Remove the duplicated `examples_dir`/`projects_home`/`copy_example` (`ui/commands/actions.py:54-76`) and import them from `ui/assembly/workspace.py` (or move them to `opencae/examples/__init__.py`).
- **N2** Move the fingertip-gap computation (`workspace.py:1328-1385`) into a Qt-free `opencae/assembly/measure.py`, so the console (`show gap`), the examples and the tests use one implementation. The test helper `tip_gap_mm` (`test_mbd_gripper.py:88-96`) can then check it against the analytic formula.
- **N3** Use one unit table: `dialogs.RPM`, `plots.RPM_PER_RAD_S` and `grammar.UNITS` should come from `opencae.utilities.units`.
- **N4** Delete the dead `if False` branches (`actions.py:1096, 1555`).
- **N5** Honesty display:
  - Show `meta.warnings` and `constraint_violation_max` in the Study tab next to the assumptions (`workspace.py:203`).
  - Show each `ThrustModel.source` ("prescribed, not CFD") on the rotor joints.
  - Print the rotor/flight assumption only when the assembly has rotors or thrust models (`run.py:37-38`).
- **N6** In `transfer_summary` use `record.units` instead of the hard-coded "mm²" (`transfer.py:338-339`), and give `ACCEL_TOL` per joint kind in stated units (`transfer.py:34`).
- **N7** Document three things in the result metadata and the CSV sign text (`run.py:283-286`, `results.py:164`):
  - The constraint softness: q̈ and reactions carry about 1e-4 relative error from `SOLIMP 0.9999`.
  - The "effort excluded" reaction still includes damping and the coupling torque along the axis.
  - A position ramp implies a velocity step whose impulsive effort is not reported.
  Also tighten the B-test tolerance to about 3e-4 so that a regression in the constraint is caught.
- **N8** Block assembly and study edits while a motion run is active (or deep-copy `project`/`study` before starting the worker), and set `study.results` in `_run_finished` on the GUI thread instead of in `run_study` on the worker (`workspace.py:1252-1259`, `run.py:355-363`).
- **N9** A console parameter study should restore `mesh_settings.size_max` with the geometry (`actions.py:1604-1606`), or say in the final reply that it changed.
- **N10** Open the edit dialog on a double-click of a joint or motor in the tree. Disable the Load case action with a tooltip ("choose a time at rest") when the selected time is accelerating, instead of showing the error only inside the dialog.
- **N11** Debounce the load-case preview. It deep-copies the project and regenerates the B-rep on every spin-box step, on the GUI thread (`workspace.py:1489-1498`).
- **N12** Replace the hard-coded agent scratchpad paths in tests with `tmp_path` or an env var (`test_assembly_ui.py:32`, `test_console_commands.py:15`, `test_console.py:10`).
- **N13** In `_iterate_drone`, `ready.wait(5)` followed by `holder["job"]` raises `KeyError` if the GUI is slow (`actions.py:1199-1203`). Pass the job in instead, as `run_callable` already returns it before `start()`.

## 5. Could it be simpler / removed?

- **`physics/mbd.py`** exists only for the study selector. `resolve` and `write_input` refuse, and that is fine. `read_results`/`load_results` are unused by the Studio path and can go.
- **`_run_in_thread` fallback** (`workspace.py:1277-1298`): the jobs extension is always installed first (`main_window.py` EXTENSIONS). The fallback can be removed together with its test, which leaves one run path.
- **`track_mbd_run`** (`ui/jobs/__init__.py:33-49`) duplicates `workspace.run_study`'s job set-up. The console uses it only when there is no Motion workspace, and `assembly_ext().workspace()` always creates one. It can be removed.
- **The 14 reaction arrays** (`run.py:316-321, 350-354`): the `*_assembly` and `*_total_*` groups can be derived from the joint-frame ones and the stored R. Keeping them is defensible for CSV export, but the joint-frame + total pair would suffice.

## 6. Process

Source: `git log 620a11c..HEAD` (12 commits) and the plan's agent table. `plans/agent-log.jsonl` is absent, so models and steps cannot be confirmed per agent.

| Step | Status |
|---|---|
| Plan and contract | a28df47, 38cf849 |
| M1 | 76de517 |
| M2 | 620f110 |
| J1 | 907bcc4 |
| M3 | 4ae9778 |
| J2 | c866820 |
| WIP checkpoints by the conductor | 5 |

Process misses under `standards/agents.md`:
1. There is no agent log.
2. The plan's agent table names the output `reviews/2026-11-25-mbd.md`, but this review was asked to write `…-mbd-panels.md`.
3. The batch combined two waves (more than 10 builder outputs' worth of diff) into one review.
4. `plans/conductor-edits.md` records two conductor code edits in this batch (qt_guard reserve, console layout). Both are small and justified.
