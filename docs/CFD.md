# Flow (CFD) studies

CalculiX is a structural and thermal finite-element solver. It is **not** a general-purpose CFD solver, so flow
studies use a separate open-source program: **SU2** (native Windows, incompressible finite-volume solver) or,
from 0.3, **OpenFOAM** (ESI v2006 or later, on Linux or inside WSL on Windows). The solver is chosen per study
in **Analysis settings → CFD solver**. Both read the same study: fluid body, flow conditions, fluid, model.

## SU2 and OpenFOAM compared

| | SU2 8.x | OpenFOAM |
|---|---|---|
| Native Windows build | yes: official release ZIP with `SU2_CFD.exe`, runs serially without MPI | no official native build (WSL / Docker / third-party builds) |
| Input | one text config file + one mesh file (native `.su2`, written directly by OpenCAE) | case directory with many dictionaries, `polyMesh` conversion |
| Incompressible laminar / RANS (SA, SST) | yes (`INC_NAVIER_STOKES`, `INC_RANS`) | yes (wide range) |
| License | LGPL-2.1, separate process | GPL-3.0, separate process |
| Install footprint | ~100 MB unzipped, no installer | WSL distribution + OpenFOAM packages |

SU2 fits the Windows / Miniforge setup without system changes. OpenFOAM needs WSL on Windows but adds
**automatic boundary-layer meshing** (snappyHexMesh prism layers on walls), y⁺ output and per-wall forces, which
is what external drone aerodynamics needs.

## OpenFOAM studies (0.3)

**Install** (nothing is installed automatically; see [WINDOWS_SETUP.md](WINDOWS_SETUP.md#openfoam-in-wsl-optional)):
the tested route is the conda-forge package `openfoam=2412` in a Miniforge environment inside Linux/WSL. The
openfoam.com precompiled packages (v2006+) also work. Ubuntu's own `openfoam` apt package (v1912) is detected
and refused: its function objects (forces, y⁺, residuals) abort with an `IOstream "sha1"` error.
**Tools → Settings → OpenFOAM** takes the environment folder or `etc/bashrc`, the WSL distribution and the
number of MPI processes (1 = serial, the default). **Test OpenFOAM** runs a laminar channel with function objects
and compares the pressure gradient with the exact value 12νU/h².

**Mesh** (Mesh → Generate in an OpenFOAM study) runs, each as a separate logged step: `blockMesh` (background
hexahedra of size *Max size*) → `surfaceFeatureExtract` → `snappyHexMesh` (local refinements on faces become
surface refinement levels, size / 2^level; wall layers from *Boundary layers*: count, first-layer thickness,
growth) → `checkMesh` → `foamToVTK` for display. The mesh log reports cells, checkMesh failures and **wall-layer
coverage per patch**; coverage below 80 % is a mesh warning. The Intelligent Mesh Manager fills these controls
for flow studies (first layer from the target y⁺ and a flat-plate skin-friction estimate).

Layer heuristics, stated as setup warnings and never applied silently: snappyHexMesh adds few or no layers when
the first layer is thinner than about 5 % of the wall cell, or when the whole layer stack is thicker than the wall
cell. In tests a sphere with 1 mm wall cells got 0 % layers with a 0.02 mm first layer and 100 % with 0.05 mm.

**Solve**: `simpleFoam` (steady, incompressible), laminar, Spalart-Allmaras or k-ω SST with Spalding wall
functions (valid for any y⁺, most accurate near y⁺ ≈ 1 or 30-100). The run stops at the residual target or the
iteration limit; non-convergence is flagged as for SU2. **Results**: velocity, pressure (Pa, ρ·p_kinematic),
eddy viscosity, total and per-wall forces with pressure/viscous split, coefficients, y⁺ (min/mean/max per wall),
flow rate per patch, flow balance and pressure drop.

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

OpenFOAM v2412 (benchmark F5, automated in `test_openfoam.py`):

* Self-test channel: pressure gradient −2.5 % from 12νU/h².
* Laminar pipe through the full study path: pressure gradient −1.3 % from Hagen-Poiseuille, flow balance
  0.34 %, wall force equal to Δp·A − ρQU/3 (momentum balance with the uniform inlet profile).
* Sphere at Re = 20 with 6 wall layers (100 % coverage), domain walls at 8 D: C_D = 2.753 vs 2.725 from the
  Pruppacher correlation (+1.0 %); with walls at 4 D, +3.3 % (blockage, as expected).
* k-ω SST sphere at Re = 2000 runs to convergence with y⁺ reported (functional check only, no accuracy claim).

## Known limitations

* Steady, incompressible, isothermal flow only (no heat transfer, compressibility, free surfaces or rotating
  frames; propellers and rotors would need MRF/actuator-disk modelling, not available).
* Uniform inlet profiles: on coarse meshes the inlet nodes on the walls are at rest, so the delivered flow rate is
  a little below the nominal value. Both numbers are reported.
* SU2: RANS wall treatment needs fine wall meshes (y+ ≈ 1) and there is no automatic 3-D boundary-layer
  meshing; force results on coarse wall meshes are not reliable (half-wing tutorial: L/D about 2.7, far below
  the real wing). Use OpenFOAM with wall layers for 3-D external aerodynamics and check the reported y⁺ and
  layer coverage.
* OpenFOAM: 3-D only, one fluid body, Linux or WSL; snappyHexMesh layer coverage depends on the wall cell size
  (see above) and is reported, not guaranteed. Parallel runs need an MPI-enabled OpenFOAM build.
* 2-D boundary layers: local edge refinement and trailing-edge fans are supported, but with a local size field
  active Gmsh builds only part of the requested layer cells, and more than about 10 rows with a first cell
  below 0.05 mm made SU2 diverge in tests.
* Low-Reynolds-number (Stokes, Re ≪ 1) cases converge slowly with the default CFL.
* SU2 runs serially (MPI is not used).
