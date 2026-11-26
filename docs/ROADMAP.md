# Roadmap

Status key: ✅ done and tested · 🟡 partly done · ⬜ planned

## Done

* **0.1**: desktop foundation, parametric CAD, Gmsh meshing, CalculiX linear statics, post-processing,
  benchmarks A–E.
* **0.2**: Windows solver fix, ribbon and studies, assemblies (conformal, tie, contact), STL tools, hex and 2-D
  meshes, mesh convergence, modal, buckling, NLGEOM, heat transfer, thermal stress, flow (RANS, first with a 2-D / 3-D finite-volume solver that has since been retired),
  Plot Explorer, reports, launcher, VS Code, 155 tests. Details: [VALIDATION.md](VALIDATION.md).

## 0.3: physics-aware, manufacturing-aware, experimentally validated drone CAE

The architecture is described in [ARCHITECTURE.md](ARCHITECTURE.md#engineering-platform-layer-03-six-managers).
Priorities follow the order given in the requirements. Later priorities build on earlier ones.

| Priority | Content | Status |
|---|---|---|
| 1 Core capability | keep 0.2 working; Material Manager (database with sources and uncertainty, orthotropic and temperature-dependent data, compare, import/export, invalidation on change); Intelligent Mesh Manager (geometry analysis, presets from geometry and physics, automated convergence with stopping and resource rules, recorded defeaturing) | ✅ |
| 2 Automated CFD | OpenFOAM backend (Linux / WSL): blockMesh + snappyHexMesh with wall layers, simpleFoam (laminar, k-ω SST, SA), forces, y⁺, residuals; the only flow solver (the earlier one was retired); validation against exact solutions in `test_openfoam.py` | ✅ |
| 3 Flight-condition physics | ISA atmosphere with overrides; flight condition → relative wind, α, β, q, Re; phase presets; mission profiles (CSV) and flight-condition sweeps; screening models; flight-point dashboard | ⬜ |
| 4 PLA manufacturing | print settings; effective properties levels 1–3; design-for-AM checks; variant comparison (orientation, infill, material, walls) | ⬜ |
| 5 Wind-tunnel validation | tunnel-mode domain with walls, mount and blockage; experiment import and mapping; comparison metrics and uncertainty; validation dashboard | ⬜ |
| 6 Multiphysics | one-way CFD → FEA load transfer (wall pressure and shear) with force/moment conservation checks; flight-condition load cases | ⬜ |
| 7 Autonomous workflow | Smart Analysis Wizard, Engineering Assistant rules, recorded retries, resource budget, Guided / Standard / Expert modes, governing-equations panel, unified dashboard | ⬜ |
| Demonstration | 14-step PLA drone-arm workflow with the user's arm geometry and wind-tunnel data | ⬜ |

Not prioritised (per the requirements): research-level FSI, full-drone transient CFD, rotor-resolved CFD.

## Later

* Local coordinate systems for loads and constraints (`*TRANSFORM`).
* Transient structural dynamics; nonlinear (elastic–plastic) materials.
* Transient flight analysis (time-dependent inflow and rotor conditions) where the solver supports it. A sweep
  of steady points is not presented as transient flight.
* Rotor models (actuator disk / MRF in OpenFOAM).
* Signed installers; conda-forge recipe.
