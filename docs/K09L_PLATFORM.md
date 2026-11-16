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
