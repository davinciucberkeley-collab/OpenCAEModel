# Contract: assembly + MBD (read before building; do not change names without the conductor)

Repo `/home/claude/OpenCAE-Studio`, Python `/home/claude/venv/bin/python` (MuJoCo 4.0.1 installed there).
Tests: `cd /home/claude/OpenCAE-Studio && PYTHONPATH=$PWD QT_QPA_PLATFORM=offscreen xvfb-run -a /home/claude/venv/bin/python -m pytest <files> -q`.
Rules: AGENTS.md engineering rules (no fabricated numbers, failures visible, assumptions stated); tests first; do
not commit; one heavy solver at a time; keep unrelated refactoring out; old projects must keep loading.

## Units and frames

- Assembly geometry, placements, joint origins: project length unit (mm in `mm-N-s`); angles in the model in
  radians, shown in degrees in the UI; mass kg, inertia kg·m² (always SI for mass properties, labelled).
- The MBD backend works in SI (m, kg, s, N, N·m); conversion happens only in `opencae/mbd/build.py`.
- Assembly frame = project frame (z up unless the project says otherwise); gravity vector set per study.
- Joint frame: origin `origin` (assembly frame), z-axis = `axis` (unit, reversed if `reversed`), x-axis = any
  perpendicular, chosen deterministically (stored once computed: `x_axis`). Revolute q>0 = right-hand rotation
  about the joint axis; prismatic q>0 = translation along the axis.
- Reaction sign: force/moment exerted by the parent body ON the child body at the joint origin, expressed in the
  joint frame (and also in the assembly frame). Units N and N·m.

## Model (`opencae/assembly/model.py`) — dataclasses with `to_dict`/`from_dict`

```
Component(id, name, body_id, placement: 4x4 list, source: str)     # body_id = GeometryModel body; placement in project units
MassProps(mass_kg, com: [x,y,z] (project length unit, assembly frame), inertia_kgm2: 3x3 about COM in assembly axes,
          source: "cad"|"manual"|"cad+manual", density_kg_m3 | None, note)
RigidBody(id, name, component_ids: list, grounded: bool, mass: MassProps | None, manual: MassProps | None)
          # effective_mass() -> manual if given else mass (computed)
Joint(id, name, kind: "fixed"|"revolute"|"prismatic", parent: body id | "ground", child: body id,
      origin: [3], axis: [3], reversed: bool, x_axis: [3] | None, limits: [lo, hi] | None,
      initial_q: float = 0, initial_qd: float = 0, damping: float = 0)
Coupling(id, name, leader: joint id, follower: joint id, ratio: float, note)    # q_follower = ratio * q_leader
Profile(kind: "constant"|"ramp"|"table", value: float | None, t0, t1, v0, v1, table: [[t, v], ...] | None)
          # value(t) -> float; linear interpolation for table; ramp v0->v1 between t0..t1, constant outside
Driver(id, name, joint: joint id, mode: "position"|"velocity"|"effort", profile: Profile)
          # position/velocity = PRESCRIBED (kinematic), effort = torque (revolute, N·m) / force (prismatic, N) = DYNAMIC
AppliedLoad(id, name, body: body id, point: [3] (assembly frame), force_N: [3], moment_Nm: [3], frame: "assembly"|"body",
            profile: Profile | None, note)          # explicit external load (e.g. fingertip load); never inferred
ThrustModel(id, rotor_joint, kt, kq, units_note, validity, source: "prescribed"|"cfd")  # stored only; not used for flight yet
Assembly(components, bodies, joints, couplings, thrust_models; helpers: body(id), joint(id), component(id),
         body_of_component(cid), validate() -> list[str] problems)
MBDSettings(gravity: [3] m/s², duration_s, dt_s, output_dt_s, drivers: list[Driver], loads: list[AppliedLoad],
            integrator "RK4"|"implicitfast", notes)    # stored in Study.analysis.extra["mbd"] or a new field
```
`Project.assembly: Assembly` (empty by default). Schema version 3; `io.MIGRATIONS[2]` adds an empty assembly.

## Mass properties (`opencae/assembly/massprops.py`)

`compute_massprops(project, body_ids, density_kg_m3) -> MassProps` from the OCC B-rep (BRepGProp volume
properties: mass, centre, matrix of inertia), converted to kg / kg·m² with the project length unit; density from the
assigned material (missing density → returns None and the UI says which material lacks it; never assumed).

## Backend (`opencae/mbd/`)

- `backend.available() -> (bool, reason)`; `backend.version()`.
- `build.build_model(project, study) -> BuiltModel` (MuJoCo model + maps: body id ↔ mj body, joint id ↔ mj joint,
  driver kinds); constraint errors raise `MBDModelError(message)` listing the problem (unconnected body, loop not
  supported, missing mass, zero inertia, ...).
- `run.run_study(project, study, progress=None, cancelled=None) -> MBDResult`; writes
  `<project>_data/results/<study id>/mbd.npz` + `mbd.json` (metadata: units, frames, sign conventions, driver modes,
  assumptions, backend version, constraint violation max).
- `MBDResult`: `t (n)`, `body_pos[b] (n,3) project unit`, `body_quat[b] (n,4)`, `joint_q[j] (n)`, `joint_qd[j]`,
  `joint_qdd[j]`, `joint_effort[j]` (actuator torque/force applied; inverse-dynamics effort for prescribed),
  `reaction_force_joint[j] (n,3) N`, `reaction_moment_joint[j] (n,3) N·m` (joint frame), `reaction_*_assembly`,
  `driver_command[d] (n)`, `constraint_error (n)`, `meta`. `MBDResult.load(path)`; `.at(time) -> snapshot dict`;
  `.export_csv(dir, joints=..., bodies=...)` writing labelled CSV (header lines with units, frame, sign).
- `pose_transforms(result, i) -> {component_id: 4x4}` for animation (component placement × body motion).

## Examples (`opencae/examples/mbd_examples.py`)

`build_gripper(dir) -> Path(.ocae)` and `build_drone_rotors(dir) -> Path(.ocae)`, parametric geometry via the
existing Session/feature path (boxes/cylinders), materials assigned, assembly defined, studies:
gripper "A – prescribed closing" (position ramp), "B – torque-driven" (effort constant),
"C – static hold with fingertip load" (prescribed hold + AppliedLoad on each fingertip), drone "Rotor spin-up"
(velocity drivers, frame grounded). Saved in `opencae/examples/projects/`. Geometry parameters in a module constant
`GRIPPER` (palm, finger length L, width b, thickness h, pivot spacing, closed angle) used by the tests' hand calcs.

## Transfer (`opencae/assembly/transfer.py`, M2)

`make_static_hold_study(project, mbd_study, time_s, body_id, root_face_ref, load_face_ref, name=None) ->
(Study, TransferRecord)`; `TransferRecord(source_study, time_s, body, frame, region, force_N, moment_Nm, units,
equilibrium_check: {root_reaction_N, root_moment_Nm, residual}, note "static hold")` stored on the new study.

## UI (`opencae/ui/assembly/`, M3)

Installed through `MainWindow.EXTENSIONS` (add `"opencae.ui.assembly"`; the conductor edits the tuple) as an
"Assembly" ribbon page and a stack page like Drone mode. Uses the APIs above only.
