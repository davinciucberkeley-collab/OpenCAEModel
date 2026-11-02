# Flow (CFD) studies

CalculiX is a structural and thermal finite-element solver. It is **not** a general-purpose CFD solver, so flow
studies use a separate open-source program, **SU2** (incompressible finite-volume solver). OpenCAE writes the
SU2 mesh and configuration, runs `SU2_CFD` in an isolated process, and reads the results back onto the study
mesh.

## Why SU2 (and not OpenFOAM) for this application

| | SU2 8.x | OpenFOAM |
|---|---|---|
| Native Windows build | yes: official release ZIP with `SU2_CFD.exe`, runs serially without MPI | no official native build (WSL / Docker / third-party builds) |
| Input | one text config file + one mesh file (native `.su2`, written directly by OpenCAE) | case directory with many dictionaries, `polyMesh` conversion |
| Incompressible laminar / RANS (SA, SST) | yes (`INC_NAVIER_STOKES`, `INC_RANS`) | yes (wide range) |
| License | LGPL-2.1, separate process | GPL-3.0, separate process |
| Install footprint | ~100 MB unzipped, no installer | WSL distribution + OpenFOAM packages |

SU2 fits the Windows / Miniforge setup without system changes. OpenFOAM is more capable (multiphase, moving
meshes, more turbulence models) but on Windows it requires WSL. An OpenFOAM adapter could be added later behind
the same backend interface.

## Workflow

1. **New study → Incompressible Flow (CFD, SU2)**. The study bodies are the **fluid**:
   * internal flow (ducts, pipes, manifolds): use the solid body that has the shape of the fluid region;
   * external flow (drone arm, motor pod, airfoil section): **Physics & Setup → External flow domain** builds a box
     around the selected parts minus the parts (the parts are kept for structural studies) and sets up a study
     with an inlet, an outlet, slip side walls and no-slip walls on the parts. **Symmetry plane**: for a half
     model (e.g. a left wing whose root lies on the aircraft's plane of symmetry) that side of the box is placed
     exactly at the parts and becomes a slip (symmetry) plane. Its face, with the root section cut out, can also
     serve as the flow face of a 2-D section study (see [WING_TUTORIAL.md](WING_TUTORIAL.md)).
2. **Analysis settings**: fluid (air, water, or custom density and viscosity), flow model (laminar, Spalart-Allmaras,
   k-ω SST), iterations, CFL, convergence drop, inlet turbulence, reference area and velocity for coefficients.
   **Dimension**: 3-D (volume mesh) or 2-D planar (mesh of one flat face, conditions on its edges, results per
   metre of depth).
3. **Conditions** (Boundary Conditions tab, Flow group): velocity inlet (m/s, normal to the faces or a vector),
   pressure outlet (gauge Pa), no-slip wall, slip wall / symmetry. Faces or edges without a condition are no-slip
   walls; the validation lists how many.
4. **Mesh**: linear cells are required (C3D4 tetrahedra, C3D8 hexahedra for box-like volumes, or 2-D
   triangles/quadrilaterals). New flow studies select linear elements automatically. Refine near walls and in
   wakes (Mesh → Local refinement); check the result with **Mesh convergence**.
5. **Solve**. The run converges when the pressure and velocity residuals drop by the requested number of orders
   of magnitude. If the iteration limit is reached first, the result is loaded but flagged **not converged**.
6. **Results**: velocity magnitude/components, gauge pressure, total pressure, eddy viscosity (RANS); velocity
   vectors and streamlines; section plane; Plot Explorer line plots; summary with flow rate per inlet/outlet,
   delivered vs nominal inflow, pressure drop, wall force (and coefficients), iterations and convergence checks.

## Units

Flow inputs and results are SI (m/s, Pa, kg/m³, Pa·s, N) regardless of the project unit system. Geometry stays in
project units and is scaled to metres for SU2.

## Validation

See [VALIDATION.md](VALIDATION.md) (benchmarks F1–F4). Summary:

* 2-D plane Poiseuille channel (quadrilaterals): pressure gradient within 1.6 % (8 cells across), 0.4 % (16),
  second-order convergence.
* Square duct (hexahedra): f·Re = 56.91 reproduced within 3.3 % (10 cells across) and 0.8 % (20).
* Circular pipe (unstructured tetrahedra, D/h = 20): Hagen-Poiseuille pressure gradient and centre-line velocity
  within a few percent.
* Sphere at Re = 20 (external flow, enclosure tool): drag coefficient within a few percent of the
  Schiller-Naumann correlation.

## Known limitations

* Steady, incompressible, isothermal flow only (no heat transfer, compressibility, free surfaces or rotating
  frames; propellers and rotors would need MRF/actuator-disk modelling, not available).
* Uniform inlet profiles: on coarse meshes the inlet nodes on the walls are at rest, so the delivered flow rate is
  a little below the nominal value. Both numbers are reported.
* RANS wall treatment needs fine wall meshes (y+ ≈ 1); there is no automatic boundary-layer (prism) meshing in
  3-D yet. Force results on coarse wall meshes are not reliable: for the half-wing tutorial the 3-D lift/drag
  ratio came out at about 2.7, far below the real wing.
* 2-D boundary layers: local edge refinement and trailing-edge fans are supported, but with a local size field
  active Gmsh builds only part of the requested layer cells, and more than about 10 rows with a first cell
  below 0.05 mm made SU2 diverge in tests.
* Low-Reynolds-number (Stokes, Re ≪ 1) cases converge slowly with the default CFL.
* Serial runs only (MPI is not used).
