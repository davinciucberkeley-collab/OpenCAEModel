# OpenCAE Studio — User guide

## 1. The window

| Area | Contents |
|---|---|
| **Menus** | File, Edit, View, Geometry, Mesh, Materials, Loads, Boundary Conditions, Analysis, Results, Tools, Help |
| **Ribbon** | Home, Geometry, Assembly, Materials, Mesh, Physics & Setup, Boundary Conditions, Solve, Results, Reports, View, Tools. Commands that do not apply to the active study (e.g. thermal loads in a structural study) are greyed out with the reason in the tooltip |
| **Workflow bar** | Geometry › Materials › Mesh › Physics › Constraints & Loads › Validation › Solve › Results › Report for the **active study**; each stage is *not started*, *in progress*, *ready*, *completed*, *invalid* or *failed*, derived from the model (never just from having clicked a button) |
| **Model tree** (left) | Geometry (feature history, bodies), Materials, **Studies**: the active study (▶) shows its bodies, mesh, conditions, analysis settings and results; other studies show their status. Double-click a study to activate it; right-click for new / duplicate / rename / delete / choose bodies |
| **Viewport** (centre) | 3-D model, orientation axes and orientation widget |
| **Properties / Results display** (right) | Editable properties of the selected item; results controls |
| **Output** (bottom) | Console, Solver progress, Mesh diagnostics, Errors & warnings, Simulation summary |

Commands that are unavailable in the current state are greyed out. Their tooltip says why (for example
"no results - run the solver").

## 2. Viewport controls

| Action | Mouse / key |
|---|---|
| Rotate | left-drag |
| Pan | middle-drag or Shift + left-drag |
| Zoom | wheel or right-drag |
| Pick | left-click (in the active selection mode) |
| Add / remove from selection | Ctrl + click |
| Clear selection | Esc |
| Fit to view | F |
| Standard views | Ctrl+1 front, Ctrl+2 back, Ctrl+3 top, Ctrl+4 bottom, Ctrl+5 left, Ctrl+6 right, Ctrl+0 iso |
| Show geometry / mesh / results | G / M / R |

Selection modes (View → Selection mode, or the View ribbon): body, face, edge, vertex, node, element. Node and
element picking switch to the mesh view. Picked entities are highlighted in orange and described in Properties,
including area, normal and centroid for faces, or coordinates, displacement and quality for nodes and elements.

## 3. Units

Each project uses one **consistent** unit system, chosen at **File → New project** and fixed afterwards:

| System | Length | Force | Stress / pressure | Density | Acceleration |
|---|---|---|---|---|---|
| mm-N-s | mm | N | MPa | t/mm³ | mm/s² |
| m-N-s | m | N | Pa | kg/m³ | m/s² |

All inputs and results use the project system, and units are shown next to every field and in every legend.
Materials are stored in SI internally and converted for display and solving. STEP/IGES files are read in mm and
scaled into the project system.

## 4. Geometry

* **Primitives** (Geometry menu / ribbon): box, cylinder, sphere, cone, rectangular plate, beam (rectangle,
  I-section, tube) and the cantilever template. Each dialog shows how position and orientation are defined.
* **Operations**: transform (scale, then rotate, then translate), boolean union / subtract / intersect (the tool
  body is consumed), fillet / chamfer, and extrude. For fillets and chamfers, select edges first in edge mode;
  with none checked, all edges are processed.
* **Parametric editing**: select a feature in *Parts / feature history* to edit its parameters and **Apply**.
  The whole history is regenerated. Failed features are marked ⚠ with the OpenCascade message, and Edit → Undo
  restores the previous state. Features can be suppressed or deleted from the context menu.
* **Import / export**: STEP, IGES and BREP are exact. **STL** has no exact geometry: it is imported as a
  *tessellated* body for display. Answer *Yes* at import, or use **Geometry → Convert STL body to solid**, to
  reconstruct a planar-faceted solid. This requires a watertight, manifold STL; otherwise you get an error with
  the boundary-edge count.
* **Check geometry** reports validity, closedness and topology counts for each body.

The **analysis body** (★ in the tree) is the body that gets meshed and solved. Change it with the body context
menu → *Set as analysis body*.

## 5. Mesh

**Mesh → Mesh settings** (or the *Mesh controls* tree item):

* Element type: **C3D10** (quadratic tetrahedron, recommended) or **C3D4** (linear). Linear tetrahedra are far
  too stiff in bending (see benchmark B); use them only for checks.
* Max / min element size (0 = automatic, about 1/20 of the model size), curvature refinement, optimisation.
* **Local refinement**: select faces or edges, then **Mesh → Local refinement on selection**.

**Mesh → Generate mesh** runs Gmsh in a separate process, so the UI stays responsive and the job can be
cancelled. *Mesh diagnostics* lists node and element counts, edge lengths, gamma, SICN, minimum scaled Jacobian,
aspect ratio, and counts of invalid / degenerate / poor elements, with histograms. Colour the mesh by quality or
element size from the Mesh menu. Elements with a non-positive Jacobian block solving.

The mesh is linked to the geometry and mesh settings. If either changes, the mesh is marked *out of date*, and
the solver refuses to run until you regenerate it.

### Intelligent Mesh Manager (Mesh → Intelligent Mesh Manager)

The manager measures the study's bodies and proposes a mesh. It never changes the geometry's scale.

* **Geometry analysis**: size, local wall thickness (rays cast through the part from its surface), holes and
  their radii, fillets, sharp re-entrant edges, thin tapering regions (e.g. trailing edges), small edges and
  faces.
* **Presets** set resolution targets, not fixed sizes. All sizes follow from the measured geometry:

  | target | Quick | Balanced | Validation |
  |---|---|---|---|
  | elements along the part | 12 | 25 | 40 |
  | quadratic elements through a wall | 1 | 2 | 3 |
  | elements around a hole | 12 | 24 | 36 |
  | elements per 90° of a concave fillet | 2 | 4 | 6 |
  | loaded / support / interface faces | global | global/2 | global/3 |

  Flow studies get wall sizes from the immersed body and a first-cell height for a target y⁺, computed from the
  flow speed and viscosity.
* **Cost estimate** of elements, nodes, solver memory and time. It is calibrated against Gmsh meshes and
  CalculiX runs and is accurate to about a factor of 2. Plans above the **resource budget** (half of the
  computer's RAM, 30 min per solve) are flagged before you mesh.
* **Sharp re-entrant edges** are reported as stress singularities: their peak stress grows with refinement.
  Judge stresses away from them, or model the real fillet.
* **Simplify geometry**: proposes removing small convex fillets and tiny cosmetic faces, never holes, concave
  fillets, thin walls or faces carrying conditions. The change is a *Simplify* feature in the history with the
  volume and face changes recorded. Suppress it to restore the original.
* **Automatic convergence** refines a copy of the study (all sizes ÷ 1.5 per level) and re-solves it. It stops
  when every monitored value has changed by less than the tolerance in each of the last two refinements (at
  least 3 meshes), at the level limit, or when the next level would exceed the budget. The stop reason is always
  reported. Monitors are integrated or point values (tip displacement, a probe point, wall force, a frequency).
  A peak stress is flagged as an unsuitable monitor. Where the changes do not shrink steadily, no grid
  convergence index is claimed.

## 6. Materials

**Materials → Material Manager** shows the project's materials, the database and your personal library side by
side. Select a material to see every value with its **source**, the **reported scatter** and the list of
properties that are **estimated** (not given by the source). Ctrl+click several materials and choose
**Compare selected** for a property table (stiffness, strength, density, specific stiffness and strength,
thermal data). **Import** / **Export** use JSON or CSV. **Add to project** and **Assign to body** copy a database
entry into the project.

* Printed polymers (PLA, PLA+, PETG, ABS, ASA, Nylon, TPU 95A) carry manufacturers' datasheet values for
  **printed** specimens, with flat (XY), side (YZ) and upright (Z) results where available. Interlayer (Z)
  strength is the weakest. Poisson's ratio and the orthotropic shear moduli are not in any datasheet and are
  marked as estimated. No thermal expansion coefficient is given where no sourced value was found.
* Metals and the CFRP laminate are example handbook values and are flagged as examples.
* **Orthotropic** materials use print-frame constants (1, 2 in the layer plane, 3 = build direction). The
  build direction of each part comes from its print settings. Without print settings OpenCAE warns and uses
  global Z.
* **Temperature-dependent** tables (E, ν, α against T) are evaluated at the study's *operating temperature*.
  Thermal-stress studies pass the table to CalculiX.
* Changing a material keeps the geometry but makes the results of affected studies out of date.
* Effective FDM properties (infill, walls, levels 1–3) are described in the manufacturing section.

## 7. Boundary conditions and loads

Select target entities in the viewport first (faces, or edges / vertices where allowed), then pick the command.
The dialog pre-checks your selection, and you can also tick entities in its list.

| Item | Targets | Meaning |
|---|---|---|
| Fixed support | faces, edges, vertices | Ux = Uy = Uz = 0 |
| Prescribed displacement | faces, edges, vertices | any of Ux, Uy, Uz prescribed (empty = free). Also serves as a displacement *load* |
| Symmetry | planar faces normal to a global axis | zero normal displacement |
| Force | faces or vertices | **total** force; on faces it becomes a uniform traction with consistent nodal loads. Global direction, or along the outward face normal |
| Pressure | faces | positive pushes into the surface; negative = tension |
| Gravity | whole body | acceleration × direction; needs density |

Loads are mapped only through the validated CAD-to-mesh association, never by nearest-node search.
**Analysis → Validate model** (F7) lists every problem: missing material, empty selections, references to faces
that changed after a geometry edit, a stale mesh, conflicting prescribed values, and **insufficient constraints**.
For the last one, the rank of the six rigid-body modes over all constrained DOFs is computed and the free motions
are named.

## 7a. Studies and analysis types

A project can hold any number of **studies**. Each study has its own bodies (several bodies are meshed together
and bonded where they touch), material assignments, mesh, conditions, analysis settings and results. Create
them with **Physics & Setup → New study** (optionally copying the active study's setup) and compare them in
**Studies dashboard**.

| Analysis type | Conditions | Results |
|---|---|---|
| Static structural (linear; *Large deflection* = NLGEOM) | supports, displacements, symmetry, force, moment, pressure, gravity | displacement, stress, strain, reactions, equilibrium check |
| Modal | supports (none = free-free) | frequencies, mode shapes (frames), effective mass fractions |
| Linear buckling | supports, loads | load factors (critical load = factor × applied), buckling modes |
| Steady / transient thermal | temperature; heat flux (W/m², + into the body), heat power (W), convection (h, T∞), radiation (ε, T∞), heat generation (W/m³) | temperature, heat flux, gradient; time frames; energy balance |
| Thermal stress | supports + temperatures from a solved thermal study (interpolated if the meshes differ) or a uniform temperature; stress-free temperature | as static |
| Incompressible flow (SU2) | velocity inlet, pressure outlet, wall, slip wall; 2-D (edges of a flat face) or 3-D | velocity, pressure, eddy viscosity, vectors, streamlines, flow rates, pressure drop, forces — see [CFD.md](CFD.md) |

Thermal inputs are SI (W, W/m², W/(m²·K), W/m³) whatever the project unit system; temperatures are °C.
Every analysis settings panel lists the governing equations and assumptions, which are also printed in the
report.

**External flow**: Physics & Setup → *External flow domain* creates a fluid box around the selected parts (the
parts stay available for structural studies) and a ready flow study.

## 7b. Assemblies and import

* **Import** accepts several files at once, and files can be dropped on the window. STEP/IGES/BREP files with
  several solids become one body per solid (option). STL files are analysed first (open, non-manifold, degenerate
  and inconsistently oriented edges, number of shells); you choose reconstruction or display-only, splitting into
  shells, and the sewing tolerance. Nothing is joined or repaired without your choice.
* **Assembly → Contacts** reports touching (bonded when meshed together), overlapping (interference — fix the
  geometry) and separated pairs. **Isolate** / **Show all** control visibility.
* Per-body materials: select a body under *Bodies in study* or use Materials → Assign.

### Connecting bodies: conformal mesh or interactions

**Mesh controls → Assembly mesh** decides how touching bodies are joined:

* **Conformal** (default): touching bodies are meshed together and share nodes at the common faces — a perfectly
  bonded joint.
* **Separate**: each body gets its own mesh (sizes may differ, nodes do not match). Bodies are then *not*
  connected until you add interactions; an unconnected, unsupported body is reported as insufficiently
  constrained before solving.

Interactions (Assembly tab):

* **Detect touching faces** lists face pairs of the study bodies that lie on each other (with the shared area).
  Only the pairs you tick become interactions; gaps are never closed automatically.
* **Bonded** (CalculiX `*TIE`): the slave faces follow the master faces. Works in static, thermal-stress, modal,
  buckling and heat-transfer studies (perfect thermal contact).
* **Contact** (surface-to-surface, penalty): the faces can separate and transmit pressure and Coulomb friction.
  The study becomes nonlinear (incremental static step). Penalty stiffness is automatic (10 E / h) or set by you;
  the penetration is about p / K. Contact is not available in modal, buckling or thermal studies (linear
  analyses). Frictionless contact does not stop sliding — supports must do that.
* Choose the finer-meshed (or softer) side as slave. Results include contact pressure, gap/penetration
  (`C_press`, `C_open`) and the summary reports the maximum contact pressure and the number of closed nodes.
* Validation: tie and contact were checked on stacked blocks in compression (σ exact, displacement within the
  penalty penetration), a tied bar with non-matching meshes in tension (exact away from the joint, ±10 % local
  stress disturbance within one element of the joint), a tied bar in heat conduction (exact) and a tied
  cantilever in modal analysis (same frequencies as one body within 0.5 %).

## 8. Solving

**Analysis → Solve** (F5) validates the model, writes the solver input (`results/run_*/` in the project data
folder: `job.inp` for CalculiX, `job.cfg` + `job.su2` for SU2), and runs the solver in the background in its own
process environment. Every run writes `solver.log` (command, solver PATH, exit code and its meaning, output). *Solver progress* streams the solver output, and **Cancel**
stops the job. Solver errors are shown in *Errors & warnings*. Solver threads and integration-point output are
set in Analysis settings.

## 9. Results

When solving finishes, results appear in the viewport and the *Results display* panel:

* **Quantity**: displacement (magnitude, X/Y/Z), stress (von Mises, principal, Tresca, components) and strain
  (components, equivalent). The legend shows units and location. **Nodal-averaged** stresses are extrapolated
  from integration points and averaged by CalculiX. The raw **integration-point** maximum von Mises stress is
  reported separately in the summary.
* **Deformation scale**: Auto (about 8 % of the model size), true scale, undeformed, or custom.
* Undeformed outline, mesh overlay, min / max markers, manual contour limits (out-of-range values are shown
  grey), colour map and number of bands.
* **Probe**: click the model to read the nodal value, coordinates and displacement.
* **Animate deformation** cycles the deformed shape.
* *Simulation summary* gives maximum values, the reaction / applied-load **equilibrium check**, and the yield
  safety factor.
* **Results → Compare with theory** works for the cantilever template.
* **Mode / time** selector: mode shapes (modal, buckling) and stored time steps (transient thermal).
* **Section plane** (X/Y/Z, position slider): shows the model behind a cutting plane; cut elements are shown
  whole, so probed values stay exact nodal values.
* **Velocity vectors / Streamlines** for flow results.
* **Plot Explorer** (Results tab): a quantity along a straight line through the solid (optionally overlaid for
  several studies), the history of a node in a transient study, mode spectra with cumulative effective mass, and
  mesh-convergence charts. Export PNG and CSV.
* **Mesh convergence** (Mesh or Results tab): a copy of the study is meshed and solved at several element sizes in
  the background; the observed order, Richardson extrapolation and GCI are reported, and quantities whose changes
  do not shrink (singular peak stresses) are flagged. Your study is not changed.

Shear strains are **tensor** components (ε_xy = γ_xy / 2), as written by CalculiX.

If you change the model after solving, results are flagged *out of date*: the workflow bar shows ✕, and
reports carry a warning.

## 10. Projects, export and reports

A project is `name.ocae` (JSON, versioned schema) plus a `name_data/` folder for imported CAD files, meshes and
solver runs. Keep the two together. **Save As** copies the data folder. Recent projects are under
File → Recent projects.

| Export | Formats |
|---|---|
| Geometry | STEP, IGES, BREP (exact), STL (tessellated) |
| Mesh | CalculiX/Abaqus `.inp`, VTK `.vtu`, Gmsh `.msh` |
| Results | `.vtu` (all fields, for ParaView), `.csv` (nodal table), `.json` (summary) |
| Report | HTML (self-contained, with figures) or PDF — per study, with equations, assumptions, checks and your engineering notes (asked when exporting, saved with the study) |

## 11. Scripting

All GUI operations are methods of `opencae.app.session.Session`, so models can be scripted:

```python
from opencae.app.session import Session
s = Session()
s.create_cantilever(length=200, width=20, height=20, force=1000)
s.project.mesh_settings.size_max = 4
s.generate_mesh()
outcome, issues = s.run_solver()
print(s.results.summary()["max_displacement"])
s.save("beam.ocae")
```

See `opencae/examples/build_examples.py` for geometry operations, BCs, loads and refinement.

## 12. Troubleshooting

| Symptom | Fix |
|---|---|
| "CalculiX (ccx) not found" | install `calculix-ccx`, or set the path in Tools → Settings / `OPENCAE_CCX` |
| CalculiX exits with -1073741515 / "DLL not found" | Tools → Settings → **Test Solver** names the missing DLL; see [WINDOWS_SETUP.md](WINDOWS_SETUP.md) |
| "SU2 (SU2_CFD) not found" | download the SU2 Windows ZIP, set its `SU2_CFD.exe` in Tools → Settings and press Test SU2 |
| Flow result "NOT converged" | increase iterations or reduce CFL, refine the mesh near walls, check the inlet/outlet placement |
| Feature marked ⚠ after an edit | the OpenCascade operation failed (e.g. fillet radius too large). Undo or change parameters |
| "face … changed shape/position; please re-select it" | a geometry edit changed a referenced face; edit the load or constraint and re-select |
| "insufficiently constrained" | add supports that prevent the listed rigid-body motions |
| "mesh is out of date" | regenerate the mesh after geometry or mesh-setting changes |
| Peak stress keeps rising with refinement | it is a singularity (sharp corner or clamped edge); evaluate stresses away from it or add fillets |

## Tilt-wing VTOL example (structural, thermal, CFD)

**File → Open tilt-wing VTOL example** (or `launch_opencae.bat --example tiltwing`) opens a solved project
built from the Tilt-Wing Designer at the paper's default values (Cakir & Kurtulus 2022 configuration). The
installed copy is never modified: it is copied once to `%USERPROFILE%\.opencae\examples` and opened from there.
Switch studies in the model tree; each study's **notes** compare the 3-D result with the designer's simplified
model, and `report\index.html` in the project folder is an overview with pictures.

| Study | What it checks |
|---|---|
| 1-3 Wing spar | CF tube (Ø20 × 1.5 mm) from the tilt bearing to the tip under the designer's hover, cruise-gust and hard-landing load vectors; FE vs beam with the same loads |
| 4 Modes | wing bending frequencies with wing-structure and motor masses, against rotor 1P / blade-passing frequencies |
| 5 Bracket | tilt-bearing plate (Al 6061) under the largest bearing load |
| 6 Heat path | temperature rise per watt conducted from a motor clamp into the spar |
| 7-8 Thermal stress | bonded aluminium motor clamps on the CF tube, +40 °C and −5 °C soak |
| 9-10 Cruise CFD | half aircraft with a symmetry plane, 20 m/s, OpenFOAM k-ω SST, fine and coarse mesh |

Viewing needs only CalculiX's environment; re-solving the CFD studies needs OpenFOAM (see CFD.md). Rebuild
everything with `python -m opencae.examples.tiltwing` (about 2 hours with OpenFOAM, a few minutes with
`--no-cfd`). Material values marked as estimates in the material records (CF hoop modulus, CTE, conductivity)
are placeholders until supplier data are entered.
