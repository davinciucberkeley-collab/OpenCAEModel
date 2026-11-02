# OpenCAE Studio — User guide

## 1. The window

| Area | Contents |
|---|---|
| **Menus** | File, Edit, View, Geometry, Mesh, Materials, Loads, Boundary Conditions, Analysis, Results, Tools, Help |
| **Ribbon** | Context tabs (Home, Geometry, Mesh, Setup, Solve, Results, View). The tab switches automatically when you show the mesh or results |
| **Workflow bar** | Project › Geometry › Material › Mesh › Constraints › Loads › Analysis › Solve › Results › Report. ✓ done, ○ pending, ✕ invalid or out of date. Click a stage to jump to it |
| **Model tree** (left) | Geometry (feature history, bodies with faces / edges / vertices), Mesh (controls, nodes, elements, named selections), Materials, Analysis (constraints, loads, steps, solver), Results |
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

## 6. Materials

The library contains structural steel, aluminium 6061-T6 and Ti-6Al-4V with **example** handbook values. These
are flagged in the tree, in the properties panel, in validation warnings and in reports. Use **Materials → New
custom material** for verified data. Required properties are E and ν (with −1 < ν < 0.5), plus density for
gravity loads. Yield strength is optional and used for the reported safety factor.

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

## 8. Solving

**Analysis → Solve** (F5) validates the model, writes the CalculiX deck (`results/run_*/job.inp` in the project
data folder), and runs `ccx` in the background. *Solver progress* streams the solver output, and **Cancel**
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
| Report | HTML (self-contained, with figures) or PDF |

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
| Feature marked ⚠ after an edit | the OpenCascade operation failed (e.g. fillet radius too large). Undo or change parameters |
| "face … changed shape/position; please re-select it" | a geometry edit changed a referenced face; edit the load or constraint and re-select |
| "insufficiently constrained" | add supports that prevent the listed rigid-body motions |
| "mesh is out of date" | regenerate the mesh after geometry or mesh-setting changes |
| Peak stress keeps rising with refinement | it is a singularity (sharp corner or clamped edge); evaluate stresses away from it or add fillets |
