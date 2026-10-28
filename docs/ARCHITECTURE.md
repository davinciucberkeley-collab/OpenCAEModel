# Architecture

```
opencae/
  app/            session.py (Qt-free application core), main.py (entry point)
  ui/             main_window.py (commands), viewport.py, model_tree.py, panels.py, dialogs.py, forms.py,
                  theme.py, icons.py (QPainter-drawn icons)
  geometry/       kernel.py (kernel-neutral interface + data classes), occ_kernel.py (OpenCascade backend),
                  references.py (topology reference validation), _occ.py (OCP 7/8 compatibility)
  meshing/        mesh_data.py (FEMesh), gmsh_mesher.py, quality.py, worker.py (out-of-process mesher)
  materials/      library.py (database, unit conversion)
  boundary_conditions/ resolve.py (CAD -> mesh mapping of loads/BCs, model validation)
  solver/         base.py (SolverBackend interface), calculix/{deck,runner,frd,dat}.py
  postprocessing/ results.py (ResultSet, derived quantities), theory.py, report.py
  visualization/  scene.py (PyVista datasets, no Qt)
  project/        model.py (data model), io.py (versioned JSON), undo.py
  utilities/      units.py, logging_setup.py
  examples/       run_validation.py, build_examples.py, projects/
  tests/          pytest suite (unit, integration, solver, GUI)
```

## Layers and data flow

```
Project (pure data, JSON)            Session (no Qt)                    UI (Qt)
  features ───────────────► GeometryKernel.regenerate ──► Bodies ──► Viewport / tree
  mesh settings ──────────► mesh_job → worker process (Gmsh) ──► FEMesh (.npz)
  materials, BCs, loads ──► resolve_model(project, body, mesh) ──► ResolvedModel + Issues
                            SolverBackend.write_input → ccx (QProcess / subprocess)
                            SolverBackend.read_results ──► ResultSet ──► contours, report
```

* **Model/view separation**: `project.model` holds plain dataclasses only. `app.session.Session` implements
  every workflow step synchronously and is used directly by tests, scripts and the validation suite.
  `ui.main_window.MainWindow` turns user actions into Session calls and runs the slow steps (Gmsh, CalculiX) as
  `QProcess`es, so the UI never blocks and jobs can be cancelled.
* **Heavy data in files**: B-rep (`.brep`), meshes (`mesh_<id>.npz`) and solver runs (`results/run_*`) live in
  the project data folder. The JSON stores relative paths plus identity hashes: `MeshInfo.mesh_id`,
  `geometry_hash` and `settings_hash`, and `ResultsInfo.mesh_id` and `setup_hash`. Staleness is detected exactly:
  a mesh is current only if it was built from the current geometry and settings, and results are current only if
  they were computed on the current mesh with the current setup. File names are versioned, so undo/redo can
  restore an earlier mesh or result consistently.
* **Undo/redo**: snapshot-based (`project.undo.UndoStack`). Each edit stores a deep copy of the small project
  model, and heavy files are not duplicated.

## Key engineering decisions

| Topic | Decision |
|---|---|
| Topology references | `EntityRef(kind, body, index, signature)`. Indices follow OpenCascade `TopExp::MapShapes` order (deterministic for a given history). After regeneration the signature (type, area/length, centroid, normal) is re-checked. A unique match elsewhere is remapped with a warning; otherwise the reference is reported broken. Nothing is attached by proximity |
| CAD → mesh mapping | Gmsh imports the exact `.brep`. Each CAD face, edge and vertex is matched to a Gmsh entity by normalised (area, centre of mass), (length, centre of mass) or coordinates, and accepted only if the match is one-to-one within 1e-5 relative tolerance. Unmapped faces are reported and cannot carry loads |
| Element ordering | Gmsh tet10 mid-side order (01, 12, 02, 03, 23, 13) is converted to CalculiX C3D10 (01, 12, 20, 03, 13, 23) by swapping nodes 8 and 9. This was found empirically, is checked at run time (`verify_c3d10_midsides`) and is tested. Negative-volume elements are reoriented |
| Deck numbers | CalculiX truncates numeric fields at 20 characters (`-1.93089988442807e-12` would be read as −0.193). All numbers go through `deck._fmt`, which guarantees ≤ 20 characters at full precision. This was found during development by a result/mesh coordinate cross-check that remains in the results reader |
| Face loads | uniform traction with consistent nodal forces (tri3: A/3 per corner; tri6: 0 at corners and A/3 per mid-side). The applied total equals the requested force exactly |
| Pressure | `*DLOAD Pn` on element faces found topologically: the boundary triangle's corner nodes are matched to the unique boundary face of an element |
| Gravity | consistent nodal loads integrated exactly (4-point Gauss with the actual, possibly curved, C3D10 geometry), not `*DLOAD GRAV`. The load acting on constrained DOFs is then known, so the equilibrium check (`sum RF + F_applied − F_on_constrained = 0`) is exact. CalculiX reaction forces exclude external load at constrained nodes |
| Under-constraint | rank of the 6 rigid-body modes over the constrained DOFs. With the check bypassed, CalculiX/SPOOLES returns a "successful" solution with displacements of ~1e10 for singular models (benchmark D), so the model is blocked before solving |
| Stresses | `.frd` stresses are nodal-averaged extrapolations and are labelled as such. Raw integration-point stresses come from `*EL PRINT` in `.dat` |
| Unit system | one consistent system per project; no conversions inside the deck |

## Extension points

* **Geometry kernel**: implement `geometry.kernel.GeometryKernel` (regenerate, export, write_native) and return
  `Body` objects. The UI and mesher only use `Body.faces / edges / vertices / tessellation / check`.
* **Mesher**: any function returning `FEMesh` in CalculiX node order with `face_tris / edge_nodes / vertex_nodes`.
* **Solver**: implement `solver.base.SolverBackend` (`write_input`, `command`, `read_results`). Analysis types
  are gated by `SUPPORTED_ANALYSES`.
* **Analysis types** (modal, buckling, thermal stress): add the type to `AnalysisSettings.analysis_type`, a
  deck-writer branch (`*FREQUENCY`, `*BUCKLE`, `*COUPLED TEMPERATURE-DISPLACEMENT`) and result readers for the
  extra `.frd` blocks. `ResultSet` is already keyed by named quantities.
* **Scripting / batch / parametric studies**: drive `Session` directly; see `examples/build_examples.py` and
  `examples/run_validation.py` for parametric loops over mesh size.
* **AI assistance**: issues are structured (`Issue(severity, message, source)`), and solver output is captured
  per run, which gives natural inputs for explanation features.
