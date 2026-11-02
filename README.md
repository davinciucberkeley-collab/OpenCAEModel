# OpenCAE Studio

An open-source desktop application that takes a part from **CAD geometry → tetrahedral mesh → linear static
finite element analysis → results and report** in one workflow.

![Plate with hole, von Mises stress](docs/images/plate_with_hole_von_mises.png)

| Layer | Library (tested version) | Role |
|---|---|---|
| UI | PySide6 6.12 (Qt) | dark-themed main window, ribbon, docks |
| 3-D view | VTK 9.6 / PyVista 0.49 / pyvistaqt 0.13 | rendering, picking, contours |
| CAD kernel | OpenCascade via OCP 8.0 (`cadquery-ocp`) | B-rep primitives, booleans, fillets, STEP/IGES |
| Mesher | Gmsh 4.15 Python API | C3D4 / C3D10 tetrahedra, local refinement |
| Solver | CalculiX 2.21 (`ccx`, external process) | linear static structural |
| Data | NumPy, SciPy, meshio, Matplotlib | arrays, export, plots |

Everything shown by the application is computed by the real pipeline. No result is predetermined, and the
validation suite checks results against closed-form solutions (see [Validation](#validation)).

## Installation

Requirements: Python ≥ 3.11 and the CalculiX `ccx` executable.

```bash
# 1. CalculiX (solver) - system package
sudo apt install calculix-ccx            # Debian/Ubuntu   (Fedora: dnf install calculix-ccx,
                                         # macOS: brew install calculix-ccx, conda: -c conda-forge calculix)
# 2. OpenCAE Studio
python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # or: pip install -r requirements.txt && pip install -e .
# Linux desktops may also need Qt's xcb libraries: sudo apt install libxcb-cursor0 libxkbcommon-x11-0
```

If `ccx` is not on `PATH`, set `OPENCAE_CCX=/path/to/ccx` or use **Tools → Settings**. Without CalculiX the
application still runs: modelling and meshing work, and solving is disabled with an explanation.

## Running

```bash
opencae-studio                                  # start the GUI
opencae-studio --example cantilever             # start with the cantilever demonstration
opencae-studio opencae/examples/projects/plate_with_hole.ocae
python -m opencae.examples.run_validation --out validation_output   # benchmarks A-E
python -m opencae.examples.build_examples       # rebuild + solve the example projects
pytest                                          # tests (GUI tests need a display: xvfb-run pytest)
```

### First demonstration: cantilever beam (≈ 1 minute)

1. **File → Open cantilever example** (or Geometry → Cantilever beam…). This creates a 200 × 20 × 20 mm steel
   beam, fixes the `x = 0` face and applies 1000 N in −Z on the tip face.
2. **Mesh → Generate mesh** (Ctrl+M). Meshing runs in a background process; see *Mesh diagnostics*.
3. **Analysis → Solve** (F5). CalculiX runs in the background and can be cancelled.
4. Results open automatically. Switch quantities in *Results display*, click the model to probe values, and use
   **Results → Compare with theory**: FE tip deflection ≈ 1.0001 mm vs Euler–Bernoulli 1.000 mm.
5. **File → Save project** and reopen it. Mesh and results are restored and marked current.
6. **File → Export report** writes an HTML or PDF report with figures, equilibrium check and theory comparison.

## Feature status

✅ implemented and covered by automated tests · 🟡 implemented with stated limitations · ⬜ not yet implemented

| Area | Status |
|---|---|
| Primitives: box, cylinder, sphere, cone, plate, beam (rectangle / I-section / tube), cantilever template | ✅ |
| Transform (translate, rotate, uniform scale), boolean union / subtract / intersect | ✅ |
| Fillet, chamfer (selected or all edges), extrude (rectangle / circle profile; polygon via API) | ✅ |
| Parametric feature history: edit, suppress, delete, regenerate; validity checks | ✅ |
| STEP and IGES import / export (exact), BREP | ✅ |
| STL import as *tessellated* body, watertightness check, facet-to-solid reconstruction | ✅ |
| Body / face / edge / vertex / node / element picking with highlight | ✅ |
| Views (front/back/top/bottom/left/right/iso), ortho / perspective, shaded / edges / wireframe, background, PNG | ✅ |
| Tet meshing C3D4 / C3D10, global size, curvature sizing, local refinement on faces / edges | ✅ |
| Mesh quality (gamma, SICN, scaled Jacobian, aspect ratio, size), invalid / degenerate warnings | ✅ |
| Validated CAD ↔ mesh mapping (faces, edges, vertices) by signature matching | ✅ |
| Materials: library with flagged example values, custom materials, assignment, validation | ✅ |
| Fixed support, prescribed displacement (per component), symmetry on axis-aligned planes | ✅ / 🟡 oblique symmetry planes not supported |
| Face force (consistent nodal loads), vertex force, pressure, gravity | ✅ / 🟡 edge loads and user-defined local CS not supported |
| Under-constraint detection (rigid-body-mode rank), empty selections, stale mesh, unit sanity | ✅ |
| CalculiX deck, background solve, progress, cancel, error capture, results parsing | ✅ |
| Displacement, stress / strain components, von Mises, principal, Tresca, equivalent strain | ✅ |
| Contours, legend with units, contour limits, deformation scale, undeformed overlay, mesh overlay | ✅ |
| Probe, min/max markers, deformation animation | ✅ |
| Nodal-averaged vs integration-point stresses clearly distinguished | ✅ |
| Global equilibrium check, comparison with beam theory | ✅ |
| Project save / open / save as / recent, versioned schema, undo / redo | ✅ |
| Export geometry, mesh (.inp / .vtu / .msh), results (.vtu / .csv / .json), report (HTML / PDF) | ✅ |
| Workflow navigator (done / pending / invalid) | ✅ |
| Several bodies in one analysis | 🟡 one analysis body at a time; a multi-solid body is meshed as a bonded (fragmented) assembly, not separately validated |
| Packaged executable | 🟡 see [docs/PACKAGING.md](docs/PACKAGING.md) |
| Modal, buckling, thermal, nonlinear, contact, optimisation | ⬜ (interfaces prepared, see roadmap) |

## Validation

`python -m opencae.examples.run_validation` runs five benchmarks through the full pipeline. The full run takes
about 35 s, and the report is in [docs/validation/validation_report.html](docs/validation/validation_report.html).

| Benchmark | Result |
|---|---|
| A. Axial bar, u = FL/(AE), σ = F/A | error < 1e-5 % on every mesh, C3D4 and C3D10 (linear exact solution) |
| B. Cantilever, δ = FL³/(3EI) | C3D10: 0.9982 → 1.0005 mm over 1k–40k nodes (EB 1.0000, Timoshenko 1.0077); C3D4 shows shear locking (−52 % → −4.8 %) |
| C. Convergence | C3D10 observed order 3.6, Richardson tip deflection 1.00063 mm, GCI 0.012 %; mid-span bending stress within 0.1 % of Mc/I |
| D. Under-constraint | all three under-constrained cases blocked before solving. With the check bypassed, CalculiX "succeeds" with displacements of ~10¹⁰ mm |
| E. Units / serialization | mm-N-s vs m-N-s identical to 1e-6; reopened project reproduces inputs, mesh and results exactly |

Details and assumptions: [docs/VALIDATION.md](docs/VALIDATION.md).

## Documentation

* [User guide](docs/USER_GUIDE.md) — workflow, controls, units, conventions
* [Architecture](docs/ARCHITECTURE.md) — modules, data flow, extension points
* [Validation](docs/VALIDATION.md) — benchmark results and limitations
* [Roadmap](docs/ROADMAP.md)
* [Packaging](docs/PACKAGING.md)

## Important limitations

* Linear static analysis only: small strains and rotations, isotropic linear-elastic material, no contact.
* Built-in material values are representative examples and are flagged as such in the UI and reports. Verify
  them before any engineering use.
* Peak stresses at re-entrant corners and clamped edges are singular and grow with mesh refinement. Use the
  convergence tools and judge stresses away from singularities.

## License

OpenCAE Studio is licensed under **GPL-3.0-or-later** (see `LICENSE`). This follows from the in-process use of
Gmsh (GPL-2.0-or-later). Other dependencies are compatible: PySide6 (LGPL-3.0), VTK (BSD-3), PyVista and
pyvistaqt (MIT), OCP (Apache-2.0) over OpenCascade (LGPL-2.1 with exception), meshio (MIT), NumPy/SciPy (BSD),
Matplotlib (PSF-based). CalculiX (GPL-2.0) runs as a separate program and is not bundled.
