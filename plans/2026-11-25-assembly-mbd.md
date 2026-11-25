# Assembly, joints, motion (MBD) and load transfer: plan

Conductor: main session. Standard: `standards/agents.md` v0.2. Builders leave changes uncommitted; the conductor
commits. Interfaces: `plans/2026-11-25-assembly-mbd/contract.md` (read it with this file).

## Request (the user's prompt, condensed; status kept up to date)

The user supplied a long prompt "to adapt suggestions from … to increase the capability". Items in its words:

| # | Request | Status |
|---|---|---|
| A1 | "load a mechanical assembly, define how its components connect, drive a joint with a motor command, simulate the motion, and use the resulting loads for structural analysis" | open (M1–M3) |
| A2 | "Use the gripper to prove that the workflow works end to end. Use the drone to verify that the underlying implementation is reusable" | open (M1 examples) |
| A3 | "Run the existing structural example before editing and record its result as a baseline" | done: `run_validation` A–E + X all PASS, 98 s (2026-11-25, before edits) |
| A4 | tree ↔ viewport selection; ground; rigid bodies; mass/COM/inertia/frame with source and units; manual values | open (M1 model, M3 UI) |
| A5 | fixed / revolute / prismatic joints with origin+axis preview, reverse axis, travel limits | open |
| A6 | motor command: angle, speed or torque, constant or time history; prescribed vs torque-driven distinguished; applied once | open |
| A7 | gravity, initial conditions, duration, time step; run, pause, scrub, inspect motion and joint loads at an instant | open |
| A8 | save, reopen, reproduce; old structural projects keep working | open (schema v3 + migration) |
| A9 | gripper: palm grounded, two revolute fingers, one motor, documented ideal transmission; study A prescribed (gap vs geometry at start/mid/end); study B torque (inertia changes acceleration; export angle, ω, reaction force/moment with frame, units, sign) | open |
| A10 | gripping force only with contact or an explicit fingertip load; known-load example for structural transfer | open |
| A11 | MBD → structural static-hold load case with visible handoff (time, component, frame, region, units); root fixed, tip force on a face; root reaction only as equilibrium check; compare with cantilever hand calc | open (M2) |
| A12 | drone: frame, fixed motor housings, revolute rotors, same implementation; change one rotor's speed in the UI; no flight prediction from animation; prescribed thrust distinguishable from CFD | open |
| A13 | common study selector; a study is available only when its backend can solve it | open (M3) |
| A14 | MBD backend: maintained, compatible, documented, verified by one small benchmark independent of the UI | done: MuJoCo 4.0.1 (Apache-2.0, pip wheels incl. Windows py3.11); torque-driven θ exact to 3e-14, hinge reaction exact, pendulum period 2.4e-6 |
| A15 | staged thermal (motor housing), fluid (stationary component), one-way temperature→structure and pressure→structure transfers with net force/moment checks | after the vertical slice (milestone M4/M5) |
| A16 | verify through the normal app; deliver commands, clicks, files, feature table, benchmarks | M6 + final report |

## Decisions

- **One authoritative assembly** in the project (`Project.assembly`), stable IDs (`new_id`), placements as
  explicit 4×4 transforms in the project length unit; source CAD and its placement are never modified
  (animation applies transforms to actors only). Schema v2 → v3 migration adds an empty assembly.
- **MBD study** = a normal `Study` with `analysis.analysis_type == "mbd"`; it references the assembly and stores
  its own settings, drivers, applied loads and results. Old studies are untouched.
- **Backend MuJoCo** behind `opencae/mbd/backend.py`; the study is "available" only if `import mujoco` works.
  Joint reactions from MuJoCo constraint forces, reported in the child body's joint frame (documented sign:
  force/moment exerted BY the parent ON the child).
- **Prescribed motion** = a position/velocity servo is NOT used (it would be a controller with its own dynamics);
  prescribed joints are kinematic: the coordinate is set from the command each step and the required actuator
  effort is computed by inverse dynamics (`mj_inverse`). Torque-driven joints are forward dynamics. The result
  labels each driven joint `prescribed` or `torque-driven`.
- **Gripper transmission**: ideal equality constraint `q_follower = ratio·q_leader` (ratio −1), massless,
  rigid, lossless, no backlash, no friction (stated in the study and the report).
- **Load transfer** is a static hold: the selected component becomes a structural study (root fixed, tip load on a
  chosen face). The MBD root reaction is shown as an equilibrium check, not applied.
- **Plots** with matplotlib (already in qt_test); viewport is the existing pyvista `Viewport`.

## Agent table

| Step | Role | Tier | Model | Input | Output | Pass criterion |
|---|---|---|---|---|---|---|
| S2 | scout | easy | haiku | repo | internals map (done) | report with file:line |
| M1 | builder | hard | sonnet | contract.md, this plan | `opencae/assembly/*`, `opencae/mbd/*`, schema v3, gripper + drone examples, benchmarks | `test_assembly_model.py`, `test_mbd_gripper.py`, `test_mbd_drone.py` exit 0; old-project tests pass |
| M2 | builder | hard | sonnet | contract, M1 API | `opencae/assembly/transfer.py`, static-hold example + cantilever comparison | `test_mbd_transfer.py` exit 0 |
| M3 | builder | hard | sonnet | contract, M1/M2 API | `opencae/ui/assembly/*` Assembly mode | `test_assembly_ui.py` exit 0 + screenshots |
| R4 | reviewer | hard | opus | diff, plan | `reviews/2026-11-25-mbd.md` | criteria PASS/FAIL |
| M4 | builder | hard | sonnet | after R4 | thermal motor housing + temperature→structure transfer | benchmark vs 1-D conduction |
| M5 | builder | hard | sonnet | after M4 | fluid stationary component + pressure→structure mapping with net force/moment check | force conserved within tolerance |
