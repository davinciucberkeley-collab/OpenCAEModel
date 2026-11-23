# AGENTS.md

Read this first. It points to the rules; it does not repeat them.

| When | Read |
|---|---|
| Any multi-agent task (planning, launching, reviewing) | `standards/agents.md`: the team's Agent Standard v0.2 (DBF agent roles), adopted unchanged from `davinciucberkeley-collab/aircraft-platform` @ 5653727 |
| Launching an agent | its role definition in `.claude/agents/` (builder, scout, tactician, reviewer, advisor, auditor) |
| Planning or resuming work | the current plan in `plans/` (request checklist, failure modes, agent table) |
| Running a simulation or a design iteration | the CLI below; never edit code to run or change a design |
| Writing or reading a drone parameter file | `docs/DRONE_PROJECTS.md` |

## OpenCAE specifics (in addition to the standard)

**Engineering rules, never relaxed.**
- Never fabricate a simulation output.
- Never present an estimate-based number as a verified result. A requirement passes only on `measured` or
  `datasheet` evidence.
- Never hide a solver failure.
- Never assume a missing material property: the analysis is "not evaluated" and the missing key is named.
- A successful simulation is not flight approval.

**One heavy solver per machine.** OpenFOAM, snappyHexMesh, CalculiX and gmsh volume meshing run inside
`opencae.drone.runlock.solver_lock()`. The development machine has 8 GB and no swap; the user's PC is short on
disk. Use the coarse presets in tests, and mark solver tests `solver` / `openfoam`, plus `slow` for long ones.

**Storage.** Write fields with `opencae.drone.types.save_field` (half floats, compressed). Solver folders are
deleted after a successful run. Never commit generated websites, solver cases or result folders.

**Commits.** Builders leave changes in the working tree and report files and the pass check; the conductor
commits. Hooks from the aircraft-platform harness (`tools/agent_hooks.py`) are not installed here yet; the
auditor and reviewer enforce the standard by hand.

## CLI (for agents)

```
opencae doctor --json
opencae new  projects/my_drone --cad airframe.step --type multirotor
opencae run  projects/my_drone --json                 # all analyses; unchanged ones are skipped
opencae run  projects/my_drone --only mass,propulsion --json
opencae iterate projects/my_drone --set propulsion.battery.capacity_mah=2200 --name big_battery --json
opencae status projects/my_drone --json
opencae clean projects/my_drone
```
`python -m opencae …` is the same. `--json` prints one JSON object on stdout; progress goes to stderr. Exit codes:
0 done, 1 a solver failed (see the result), 2 bad input.

## Tests

```
python -m pytest opencae/tests -q                    # solver tests skip when the solver is missing
python -m pytest opencae/tests -q -m "not slow"
```
On Linux without a display, prefix with `xvfb-run -a`.
