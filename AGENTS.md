# AGENTS.md: rules for agents working on OpenCAE Studio

This file is for coding agents (and people) running several workers on this repository at once. It is
written so a shared team file (for example James's) can be merged into it: keep the section headings.

## The engineering rules (never relaxed)

- Never fabricate a simulation output. A number in a result comes from a run that happened.
- Never present a placeholder or estimate-based number as a verified result. Inputs carry a status
  (`estimate`, `datasheet`, `measured`, `user`); a requirement passes only on `measured` or `datasheet`
  evidence. Everything else is at best conditional.
- Never hide a solver failure: report it with the log tail.
- Never assume a missing material property or parameter: report the analysis as not evaluated and name
  the missing key.
- A successful simulation is not proof of safe flight. Nothing in this code grants flight approval.

## How to run things (for agents)

Use the CLI. You do not need to edit code to simulate or iterate a design.

```
opencae doctor --json                               # which solvers are available
opencae new  projects/my_drone --cad airframe.step --type multirotor
opencae run  projects/my_drone --json               # every analysis; cached ones are skipped
opencae run  projects/my_drone --only mass,propulsion --json
opencae iterate projects/my_drone --set propulsion.battery.capacity_mah=2200 --name big_battery --json
opencae status projects/my_drone --json
opencae dashboard projects/my_drone                 # local dashboard in the browser
opencae clean projects/my_drone                     # delete solver scratch
```

`python -m opencae ...` is the same as `opencae ...`. With `--json` the command prints exactly one JSON object
on stdout; progress goes to stderr. Exit codes: 0 done, 1 a solver failed (see the result), 2 bad input.

The parameter file is `drone.json` (format `opencae-drone/1`, documented in `docs/DRONE_PROJECTS.md`).
K09-L is the project `opencae/examples/drones/k09l` and runs on its own detailed pipeline.

## Multi-agent rules

1. **Own your files.** Each worker gets a list of files it may create or change. Do not edit files outside
   it; if you need a change elsewhere, say so in your report.
2. **Shared interfaces live in `opencae/drone/types.py`.** Change them only through the lead.
3. **One heavy solver per machine.** OpenFOAM, snappyHexMesh, CalculiX and gmsh volume meshing run inside
   `opencae.drone.runlock.solver_lock()`. Never start two CFD runs at once. The development machine has
   8 GB of memory and no swap.
4. **Small test runs.** Use the coarse presets and few iterations in tests; mark solver tests
   `@pytest.mark.solver` or `@pytest.mark.openfoam` and long ones `@pytest.mark.slow`.
5. **Commits are made by the lead.** Workers leave their changes in the working tree and report the files
   they touched and the test command that proves them.
6. **Storage.** Write result fields with `opencae.drone.types.save_field` (float16, compressed). Delete solver
   scratch after a successful run. Do not commit generated websites, solver cases or large binaries.
7. **Model choice.** Search and audit with a small, fast model; building and reviewing with a capable one.
   Every task ends with a verification step (run the tests, look at the output) before it is reported done.
8. **Process.** Plan first (`docs/superpowers/plans/`), then build task by task with tests first, then verify
   before claiming completion.

## Tests

```
python -m pytest opencae/tests -q                  # everything (solver tests skip if the solver is missing)
python -m pytest opencae/tests -q -m "not slow"    # quick
```
On Linux without a display, prefix with `xvfb-run -a`.
