# K09-L Tailsitter Engineering Platform

One window with six connected pages, all on one engineering model and one dependency engine. Open it with
**File → K09-L Tailsitter engineering platform**, the **K09-L** ribbon button, or
`launch_opencae.bat --example k09l` (`python -m opencae.ui.platform.window` runs it on its own).

## 1. Audit (Phase 0)

**Existing software (baseline).** OpenCAE Studio 0.3.0-dev, branch `k09l-platform` created from commit
`01ce83e`. Python 3.11, PySide6, VTK/PyVista, OCP (OpenCascade), Gmsh, CalculiX, plus optional SU2 and
OpenFOAM. The baseline test suite passed 184 of 184 in 945 s before any change.

**K09-L model inventory.** The source is the *K09-L Tailsitter Model* artifact (Fusion assembly, CAD rev A,
2026-11-11) and the *Tailsitter Decision* page (requirements C1–C5, gates, sizing). Copied into
`opencae/examples/k09l/`:

| File | Content |
|---|---|
| `k09l_mesh.npz` | 418 tessellated bodies, 162 820 vertices, 147 006 triangles. Converted from the viewer frame to the aircraft frame (x aft, y right, z up, mm). |
| `model_meta.json` | Per body: name, component, group, joint, colour, centroid. Also 10 joints (4 prop revolutes with origin and axis, 2 elevons, 2 servo horns, 2 linkages). |
| `bom.json` | 92 BOM lines (88 with geometry). Each line has vendor, part number, quantity, unit mass, unit price and a price note (`ESTIMATE` where applicable). |
| `sheet1-4*.svg`, `pod.png`, `hover.png` | Drawings (GA, exploded, BOM, wiring) and renders. |

408 of the 418 bodies are closed after welding duplicate vertices. The 10 open ones (wires, ESC deck) are
treated as surface masses.

**Missing or incompatible inputs.** These are registered in `EngineeringModel.unresolved` and shown on page 1:

- **B-rep CAD:** the STEP and `.f3d` files are on the team PC (`C:\Users\trist\K09L_build`). FEA rebuilds
  exact solids from the part-name dimensions instead. For example, `Motor tube CF 16x13.08x380` is
  cross-checked against the mesh to within 0.3 %.
- **Motor bolt pattern:** marked MEASURE in the model.
- **FC and servo mounting:** marked MEASURE in the model.
- **Propeller performance file:** APC's 11x4.5MR file could not be reached, so C_T and C_P are provisional.
- **Battery internal resistance:** not published; provisional value used.
- **Aerodynamic coefficients:** no wind-tunnel or CFD data, so C_D0, e, the neutral point and C_L,max are
  provisional.
- **Material strengths:** PETG, XPS, Depron and the CF tube laminates have no tested strengths.
- **Shipping cost:** not in the K09-L files.

**Import cross-checks against the K09-L page.** The imported model reproduces the page's numbers:

| Quantity | Model page | Platform |
|---|---|---|
| Modelled mass | 1 982 g | 1 982.17 g (BOM sum) |
| CG | 60.0 mm, 20 % chord | 59.3 mm, 19.7 % chord |
| Static margin | 5.0 % | 5.3 % (NP estimated at 25 % c) |
| Tip-over angle | 34.7° | 34.66° (support polygon vs CG height) |
| Parts cost | $905 | $904.66 |
| Span / chord | 1150 / 300 mm | 1150 / 300 mm |

## 2. Architecture

| Layer | Module |
|---|---|
| Presentation | `opencae/ui/platform/window.py` (6 pages), `scene.py` (VTK scenes), existing OpenCAE main window (menu entry) |
| Engineering data | `engineering/model.py` (components, bodies, joints, requirements, configuration, revisions), `catalog.py` (options with sources), `quantities.py` (`Q`: value, unit, evidence class, source, uncertainty) |
| Dependency & workflow | `engineering/graph.py`: 28-node graph, invalidation, per-analysis input hashes, run history |
| Physics services | `calcs.py` (level 1), `flightsim.py` (6-DOF, level 2), `component_analysis.py` (CalculiX FEA, level 3), existing OpenCAE solvers |
| Optimization | `approval.trade_study` (constrained enumeration of the catalog, Pareto set) |
| Manufacturing & cost | `manufacturing.py` (process plan, test matrix, measurement import), `assembly.py` (subassemblies, sequence, explode, check, BOM), `calcs.costs` |
| Reporting & decision | `approval.py` (requirements, gates G1–G6, readiness summary), `report.py` (HTML review) |
| Services | `services.py` runs FEA, assembly check and flight simulation; the GUI calls it from worker threads |

**Evidence classes.** Every value carries one of eight classes: user input, supplier specification,
analytical calculation, numerical simulation, reduced-order prediction, test measurement, provisional
estimate, or missing. A calculated value takes the weakest class of its inputs. A requirement shows
**PASS** only with adequate evidence:

- Test-verified requirements need a measurement.
- Other requirements need a result that is not a provisional estimate.

Requirements and gates take four states: PASS, FAIL, CONDITIONAL / REVIEW REQUIRED, and NOT EVALUATED.
Missing data is never a pass.

**Dependency engine.** Changing an input (catalog option or parameter) does four things:

1. Recomputes all light results (about 0.1 s).
2. Marks every heavy analysis downstream as STALE, giving the path in the reason (for example,
   `motor -> propulsion -> fea_motor_tube`).
3. Highlights the affected nodes in the graph on page 1.
4. Records the change.

Each heavy result stores the hash of exactly the inputs it depends on. A result therefore goes stale even when
the model was changed outside the engine, and an unrelated edit, such as the budget, does not invalidate it.

**Analysis selection (`component_analysis.analysis_context`).** Rules R1–R6 choose the analysed part, the
parts that introduce load, the support path and the load cases. For example, the motor tube is analysed with
its two motor mounts as load introductions (thrust plus reaction torque at the prop joints) and the fin clips
as supports. Fasteners and wiring are excluded, and the reason is listed.

**Multi-agent development.** Separate engineers each owned one new module and its tests:

- Flight dynamics: `flightsim.py`
- CAD, assembly and manufacturing: `assembly.py`, `manufacturing.py`
- Structures: `component_analysis.py`
- Systems architect (integration): model, calcs, graph, approval, UI

An independent reviewer who had not seen the code checked the calculations afterwards. It found 3 errors and
8 significant issues, among them:

- the props were not excluded from the transport-box check;
- the ESC margin was shown as stronger evidence than its inputs;
- editing a parameter upgraded its evidence class;
- the tip-over angle had no sign;
- stale FEA was still counted;
- missing shipping was treated as $0;
- the cruise point was beyond stall.

All of them were fixed and are guarded by `test_review_findings_stay_fixed`.

## 3. Tool decisions

| Tool | Decision | Reason |
|---|---|---|
| OCCT/OCP, VTK/PyVista, Gmsh, CalculiX, PySide6 | kept | Already working; authoritative geometry stays the Fusion export |
| Qt Widgets | used | The app is Qt Widgets. A web dashboard or QML would split the UI and need a second rendering path for the CAD viewport. |
| SciPy / NumPy | used | Hull, solvers, integration (already dependencies) |
| matplotlib (Qt backend) | used | Already a dependency; telemetry and trade plots |
| OpenFOAM / SU2 | available, not used for K09-L | No validated cruise case yet. CFD on this geometry is a next step (OpenFOAM path exists from 0.3). |
| JSBSim / PX4 SITL / Gazebo | not integrated | One coherent primary simulator was asked for. A self-contained Python 6-DOF tied to the engineering model was chosen because the K09-L's mass, inertia, motors and joints come straight from the model. PX4/ArduPilot SITL is the right next step for controller validation (the team plans ArduPilot SITL). |
| OpenMDAO | not integrated | The catalog holds only discrete choices. Exhaustive enumeration with constraints is exact for it, and OpenMDAO becomes worthwhile when continuous design variables (span, chord, prop diameter) are parametrised. |
| FreeCAD, CadQuery, build123d, OpenVSP, Kratos, Elmer | not integrated | No measurable advantage for the present scope; OCP already provides B-rep rebuild |

No new packages were added, so the dependency and licence set is unchanged.

## 4. Pages

| Page | What it does |
|---|---|
| 1 Configuration & Design | 3-D model (pick parts, isolate, hide, frames), assembly tree, catalog selections (motor, prop, ESC, battery, landing, payload; incompatible options refused with a reason), component sheet, dependency graph, change log, suggested actions, Compare configurations |
| 2 Engineering Analysis | Results by category with fidelity level (L1 analytical / L2 reduced-order / L3 FEA), evidence class, method, inputs and the linked requirement; scope follows the selected component; thrust/current envelopes; solver-backed runs with status and reason |
| 3 Virtual Flight | Garden test field, 6-DOF hover / takeoff / landing / gust / payload / motor degradation / battery decline / transitions / waypoints; wind, gust, temperature (density); live telemetry, synchronized plots, play / pause / scrub / overlay; validity statement |
| 4 Fabrication & V&V | 12-stage process graph with checklists and gates, manufacturing plan per made part (process, print guidance, tools, time ESTIMATE, risks), verification matrix T01–T15 covering R1–R12, measurement CSV import and template; simulation verification kept separate from physical validation |
| 5 Assembly & Integration | Exploded / transparent / section views, labels, subassembly filter, 8-step sequence with fasteners matched from geometry, Check Assembly (interference, clearances, joints, mounting, electrical), BOM export |
| 6 Cost & Approval | Readiness summary, cost by group with evidence, requirements, gates G1–G6 with criteria, recorded reviewer decisions, trade study with Pareto chart, revisions, HTML readiness report |

Keyboard: Ctrl+1 … Ctrl+6 switch pages, Ctrl+S saves the state.

**Modes.** Beginner hides methods. Engineer is the default. Expert shows everything.

## 5. Acceptance tests (executed)

These are automated in `opencae/tests/test_k09l_platform.py`. They run in about 90 s.

| Test | Result |
|---|---|
| A: landing feet → bare fin tips | passed |
| B: motor X3108S → X2814 V3 | passed |
| C: selected component analysis | passed |
| D: virtual flight, hover | passed |
| E: fabrication & readiness review | passed |
| F: existing functionality | passed (the full earlier suite runs unchanged, see the commit) |
| GUI | passed |

**A: landing feet → bare fin tips.**
- **Geometry:** the feet and their fasteners (24 bodies) are removed.
- **Mass and CG:** mass drops from 1982 g to 1894 g, and the CG moves.
- **Loads:** the landing load factor rises from 18 g to 52 g (provisional stroke).
- **Stale analyses:** FEA and the assembly check go stale, with the reason given.
- **Downstream updates:** the process plan, cost, assembly sequence and tip-over angle all update.
- **Refused options:** wheels and skids are refused, with the reason.

**B: motor X3108S → X2814 V3.**
- **Mass:** rises by 106 g.
- **Electrical:** hover current, endurance and motor heat change, and the motor-current margin rises from
  0.64 to 1.06.
- **Compatibility:** the ESC margin is re-evaluated, and the mount is flagged "CAD envelope not updated".
- **Stale analyses:** the hover simulation and FEA go stale, and the simulation then runs with the new
  config hash.

**C: selected component analysis.**
- **Context:** for the motor tube, the selection engine includes the mounts and fin clips.
- **Solver:** a CalculiX C3D10 run converges. Equilibrium holds to 2e-4 %, and the result agrees with
  beam theory within 0.5 % away from the clamp.
- **Recording:** the safety factor is recorded as ESTIMATE, because the CF properties are provisional.

**D: virtual flight, hover.**
- **Thrust:** thrust equals weight within 3 %.
- **Position:** position is held.
- **Energy:** the integrated power matches the energy used.
- **Telemetry:** every channel is finite and has the same length.
- **Recording:** the run is stored with its config hash.

**E: fabrication & readiness review.**
- **Coverage:** components, subassemblies, workflow, BOM (estimates flagged), verification matrix,
  gates and risks are all present.
- **Report:** the HTML report is generated.

**GUI.** The real window was driven through the pages, the motor was changed from the combo box, and the
report was exported.

## 6. What the platform says about K09-L today

These results come from provisional data and point to the physical tests that would settle them.

- **R7 FAIL (motor current).** With the provisional prop coefficients, full throttle at 4S draws about 39 A per
  motor, against the X3108S rating of 25 A for 30 s. Its 370 W limit is also exceeded. Run a thrust-stand
  test (T03) before ordering. The X2814 V3 alternative passes.
- **R6 FAIL (tip-over).** Standing on its feet, the aircraft tips over at 34.7°, below the 36° lean the
  wind study gives for an 8 m/s gust (an estimate).
- **R5 FAIL (transport box).** Assembled with the props off, the aircraft measures 1.15 × 0.48 × 0.45 m: 4 mm
  over the 0.45 m box depth. It fits with the clip-on fins removed, but C3 asks for one piece.
- **R3 NOT EVALUATED.** Shipping is unknown. Parts, tax and 5 % contingency come to $1 043.
- **R11 NOT EVALUATED until the FEA is run.** The motor tube's safety factor is about 29 on estimated CF
  properties.
- **Gates G5 and G6** stay NOT EVALUATED until there are measurements and a recorded flight-test review.
  Simulation cannot pass them.

## 7. Known limitations

- **Geometry:** it is the tessellated Fusion export. Parametric dimension edits rebuild only tubes, for FEA.
  There is no B-rep editing of K09-L parts.
- **Catalog:** small (2 motors, 1 prop, 1 ESC, 1 battery, 4 landing options, 2 of them not offered). Add
  options only with sourced data.
- **Flight model:** reduced-order with provisional aerodynamics. Prop wash does not reach the wing (the props
  sit above and below it), so hover control is by differential thrust only. Transition and back-transition
  converge with a designed control law, but that is not evidence of real behaviour.
- **Assembly check:** works on sampled points (the penetration is a lower bound), with joints in the neutral
  position only.
- **Spar FEA:** uses an assumed load factor of 4. No load-factor requirement exists yet.
- **Not modelled:** CFD of K09-L, coupled thermal-structural analysis, and fatigue.

## 8. Design revision B (2026-11-15)

Revision A (the Fusion import) failed R5 (transport box, 454 mm high), R6 (tip-over 34.7° against the 36° gust lean)
and R7 (38.5 A per motor at full stick against the 25 A / 30 s rating), and R3 could not be evaluated (no shipping).
Revision B (`opencae/engineering/revision_b.py`) applies five changes through the engine:

| | Change | Requirement | Mechanism in the model |
|---|---|---|---|
| B1 | Outrigger feet K09L-AF-FOOT-B (outer edge 198 → 220 mm, aft wall 6 mm thinner, 5 mm side walls, M3x25 clamp) | R6 | landing option `feet_b`, parametric stretch of foot A beyond its fin slot |
| B2 | Motor sets 6 mm inboard: motor tube cut 368 mm, fin notch 6 mm deeper (K09L-AF-FIN-B) | R5 | mount option `inboard_b`, rigid translation + joints moved |
| B3 | Flight-controller motor output limit 0.66 | R7 | parameter `throttle_max` (calcs and 6-DOF model) |
| B4 | Shipping: 22 orders × $14, ESTIMATE ±50 % | R3 | parameter `shipping` |
| B5 | Transition procedure 80° pitch-over at 60°/s, wing-borne at 16 m/s | G3 | parameters `transition_*` (input node `flight_procedure`) |

Parametric changes act on a copy of the imported points (`EngineeringModel._sync_geometry`); the imported CAD is
restored bit for bit when the option is deselected, and modified masses scale with volume as ESTIMATE.

Result: R5 and R12 PASS, all others CONDITIONAL (predictions on estimated inputs or awaiting test); no FAIL.
Mass 1977.5 g, static margin 5.31 %, tip-over 38.4°, box fit 1.018, motor current margin 1.07, T/W at end of battery
1.90, reserve $649. CalculiX: motor tube SF 50.6 (A 29.6), spar 4.9. Assembly check: no interference beyond A's.
Transition from 30 m: B loses 0.4 m in calm air; in 11 off-nominal cases B stays within 5 m in 8, A in 4. Flight-card
limits follow from that sweep (`revision_b.OPERATING_LIMITS`). G3 now also checks the transition (≤ 5 m height
loss, no ground contact). Revision B is ready to build and test; it is not approved to fly (G5/G6 need the tests).

Open it: `python -m opencae.app.main --example k09l-revB`, or **Open revision B** in the platform toolbar. The
bundled `opencae/examples/k09l/k09l_revB_state.json` holds revisions A and B with all their solver runs; switch with
the Revisions box. Full review: `docs/k09l_revB/K09L_revision_B_review.html`.

## 9. Design Monitor (variants, monitoring, dashboard report)

An application window for design studies: every K09-L variant is measured with the same monitoring
parameters (requirements R1–R12, gates G1–G6, 25 key metrics, the five heavy analyses and the transition
robustness cases), so variants are directly comparable.

Start it: `python -m opencae.app.main --example k09l-monitor`, **Monitor** in the OpenCAE main window, or
**Design monitor** in the platform toolbar. It opens the user's study `~/.opencae/k09l_study.json` (created from
the bundled `opencae/examples/k09l/k09l_study.json`: A, B and example variant C, with all solver runs and
robustness cases).

| Area | What it does |
|---|---|
| Variants (left) | One line per variant: fails / conditionals / passes / not evaluated and how many of the 5 analyses are current. Double-click to edit. |
| Monitor tab | Matrix of every monitored parameter (rows) × variant (columns): requirement status with predicted value, metrics with evidence class (a miss of its requirement limit in red), analysis status with key result, gate status. Filters: only rows that differ, only rows that need attention. |
| Variant tab | Differences from the baseline, analyses with reasons, parts changed from the imported CAD, gates with every criterion, 3D view with the changed parts in orange. |
| Activity log | Every edit, which requirement statuses it changed, which analyses it made stale, job progress. |
| New variant / Edit variant | Clone any variant, then change catalog selections (motor, prop, ESC, battery, landing, motor mounting) and parameters: geometry (`foot_outrigger_mm` 0–40, `foot_shorten_mm` 0–10, `motor_inboard_mm` 0–12), motor output limit, transition procedure, payload, mission, air density, aerodynamic assumptions, cost. Values are range-checked; an edited value keeps its evidence class unless re-classified (a supplier value needs a reference). |
| Run analyses / Run all stale | CalculiX FEA of motor tube and spar, assembly check, hover and transition simulation for the variant (only those not current), in the background with progress; Cancel stops after the running step. |
| Run robustness | The 11 off-nominal forward transitions of the revision-B review for the selected variant. |
| Export dashboard report | One self-contained HTML file: variant cards with renders, requirement and gate matrices, metric small multiples with requirement limits, analyses, robustness, and a detail tab per variant (differences, requirements, gates, every metric with its evidence basis). |

Integrity: solver results are matched to the exact inputs they were computed for (engine history, input hash):
switching or editing a variant shows a result as current only if its inputs are identical; robustness results
are dropped when the transition inputs change; switching back to a variant restores the imported CAD bit for bit
before applying that variant's parametric changes. Code: `opencae/engineering/study.py`,
`opencae/ui/platform/monitor.py`; rebuild the bundled study with `python -m opencae.examples.k09l.build_study`.

## 10. Contour analyses, spinning props and the design-review website (2026-11-17)

**Website.** `opencae/web/build.py` writes a static site (three.js; it opens from disk and needs no server) for
one variant. It has three pages: **Overview** (verdict, key numbers, analysis cards), **Results** (contour plots
with the critical region and the failure parameter of each analysis, a clickable 3D aircraft and a value probe)
and **Audit** (requirements, gates, solver runs, estimated inputs, metrics, test matrix). You can build it from
**Build website** in the Design Monitor (Ctrl+B), with `python -m opencae.web.build`, or as part of
`build_study`, which writes the bundled copy to `opencae/examples/k09l/website/`. The data comes only from
recorded solver runs. An analysis that is not current for the variant appears as "not run" or "out of date".
It is never shown with an older result.

**New heavy analyses** (`opencae/engineering/field_analyses.py`). Each one records a surface field, a critical
region, the worst point and a failure parameter.

| Node | Model | Failure parameter |
|---|---|---|
| `fea_foot` | Right upper PETG outrigger foot, C3D10. The whole 1 m/s touchdown load goes in through the pad face, and the foot is held at the fin-slot walls. | SF = 25 MPa (estimated printed PETG strength) / peak von Mises. The 99th percentile is reported as well, because the support corner is a singularity. |
| `thermal_mount` | Motor mount in steady hover on a 35 °C day. The face against the motor base is held at T_amb + motor loss × 2.5 K/W, with convection at h = 25 W/m²K elsewhere. | Margin = material limit (`mount_material`: PETG 65 °C, ASA 85 °C) − hottest point |
| `cfd_cruise` | OpenFOAM simpleFoam k-ω SST, half model with a symmetry plane, snappyHexMesh surface level 4/5 (about 350 k cells). The aircraft is at the flight-model cruise trim (C_L = 0.05 + CLα·α). Forces and wall shear are computed from foamToVTK output, because the Ubuntu v1912 build aborts in function objects. | Lift ratio = C_L at the trim α / C_L that level cruise needs. Separated area comes from reversed wall shear. |

The motor-tube and spar FEA runs keep their surface von Mises field for the website (`attach_fea_field`). Gate
G3 now needs these three analyses, plus a full-revolution sweep of every prop (`assembly.rotor_sweep`). A strike
fails the gate, and a clearance under 25 mm makes it conditional.

**Props.** In the app the props were drawn but never turned, which is why they looked stuck. The platform and
the website now rotate every prop about its joint axis at the simulated rpm, drawn at 1/40 speed so that the
rotation does not alias at screen frame rates. The physical sweep shows a minimum clearance of 29.5 mm (revision
B), so no prop strikes anything.

**Revision B change B6.** The thermal model puts the PETG mount face at about 66.6 °C against a 65 °C limit,
so it fails. The mounts are now printed in ASA, which has an 85 °C limit and leaves an 18.4 K margin.

**CFD finding (open).** At the flight model's cruise trim (12.5 m/s, α 8.1°) the CFD gives C_L 0.35 to 0.37,
about 60 % of the 0.59 that level cruise needs. Two mesh levels agree within 5 %: surface level 3/4 (143 k cells,
800 iterations) gave C_L 0.350, and level 4/5 (350 k cells, 400 iterations) gave C_L 0.366.

The implied lift slope is about 2.2/rad, compared with 3.8/rad in the flight model. With that slope, level
cruise at 12.5 m/s needs about α 14°, or about 16 m/s at α 8°. The cruise-speed and endurance predictions are
therefore optimistic.

This is a coarse RANS result. It has no prism layers and omits the props. It does not replace a wind-tunnel or
flight measurement of the lift slope. It is shown as a failed analysis until that measurement settles it.

## 11. One-page design review, prism-layer CFD, mesh profiles (2026-11-17)

**Website** (`opencae/web`). The site is now one page built around one shared three.js viewer. The sections are
Overview, Design, Analyses, Flight, Components, Decisions, Process and Audit (see `START_HERE.md`).

`opencae/web/data_extra.py` derives everything beyond the summary from the engineering model:

- per-component checks and suggestions
- remedies, with model re-evaluations of the proposed fixes
- gate decisions
- the knowledge graph and process flows
- fabrication steps, from `manufacturing.process_plan` and `assembly.assembly_steps`
- explode vectors
- electrical routing, from the BOM harnesses; endpoints that are inferred are marked as such
- the cost build-up
- flight tracks, from `flightsim.simulate`, posed with `scene.pose_matrix`
- mesh profiles

The graphs use cytoscape.js 3.30.2, dagre 0.8.5 and cytoscape-dagre 2.5.0 (MIT, vendored in `opencae/web/vendor`).
The site still opens from disk without a server.

**CFD** (`run_cfd_cruise`). The cruise case now has:

- three prism layers on the aircraft (about 95 % of the faces extruded)
- feature-edge snapping (`surfaceFeatureExtract`)
- 564 k cells
- 500 iterations, written every 50
- C_L history and its oscillation recorded
- the mesh profile parsed from checkMesh: cell types, non-orthogonality, skewness, aspect ratio, layer coverage
- flow visualisation data from the volume field: a chordwise slice and 60 streamlines (`load_flow`)

Revision B gives C_L 0.349 at α 8.1° against 0.590 required (59 %), with 17.5 % separated area. C_L varied
by 0.005 over the last 25 % of the iterations. This matches the earlier runs without layers (0.35–0.37), so the
lift-slope finding stands. It is still indicative, with wall functions and no mesh-independence study.

**Mesh profiles.** Every tetrahedral analysis (foot, mount, motor tube, spar) records:

- element and node counts
- target size
- edge-length statistics
- the gmsh SICN quality distribution as a histogram

The website and the desktop app show these with each contour.

**Desktop app.** Platform page 2 has a **Contours** tab (`opencae/ui/platform/contours.py`). It lists each
contour analysis of the loaded design with its status and lets you pick a component. It shows the field with
its colour bar, a critical-region-only toggle, the mesh, the worst point, the failure parameter and the mesh
profile.
