# Roadmap

## Done (v0.1)

Phases 1–7 of the original plan: desktop foundation, parametric CAD, Gmsh meshing, analysis setup, CalculiX
linear static solving, post-processing, and validation benchmarks A–E. Phase 8 covers documentation, examples
and packaging notes.

## Next (v0.2): robustness and usability

* Local coordinate systems for loads and constraints (`*TRANSFORM`), which also enables symmetry on oblique
  planes.
* Edge loads (line force) and remote force / moment via distributing couplings.
* Several analysis bodies with per-body materials and bonded / tied interfaces, with explicit interface validation.
* Sketch-based extrusion (2-D sketcher with constraints) and revolve.
* Mesh-convergence wizard in the GUI, reusing `run_validation` logic on any model.
* Persistent per-user material library editor (the storage API exists in `materials/library.py`).
* Cleanup of unreferenced `mesh_*.npz` and `results/run_*` files on save.
* Section plots and path plots (stress along a line).

## Analysis types (v0.3+)

Interfaces are prepared in `solver/base.py` (`SUPPORTED_ANALYSES`) and `ResultSet` (named quantities).

* Modal analysis (`*FREQUENCY`): mode shapes and frequencies, using existing mass/density data.
* Linear buckling (`*BUCKLE`).
* Thermal stress (uniform or imported temperature field; α is already in the material model).
* Nonlinear materials (elastic–plastic) and geometric nonlinearity (`NLGEOM`) with increment control.
* Contact (surface-to-surface) — requires surface-pair definitions in the model.

## Automation and AI

* Python console inside the application, bound to the running `Session`.
* Batch / parametric studies: parameter sweeps over feature parameters with result tables.
* Optimisation loop (size optimisation via SciPy) on top of batch studies.
* AI-assisted setup and explanations of solver warnings, using the structured `Issue` objects and captured
  solver logs.

## Packaging

* Signed Windows and macOS installers (PyInstaller or Briefcase), with an optional managed CalculiX download.
* conda-forge recipe: all dependencies, including `ccx`, are available there.
