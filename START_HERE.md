# Start here

OpenCAE Studio is one application with several modes: **Studio** (CAD, meshing, FEA), **Drone** (any drone,
from a CAD file and a parameter sheet) and the detailed **K09-L** pages. Everything runs inside the app.

## 0. Any drone, no code (Drone mode)

1. Start the app (`launch_opencae.bat`) and click the **Drone** tab in the ribbon.
2. **New drone** → pick the CAD file (STEP, STL or OBJ), a folder, a name and the type (multirotor, fixed wing,
   tailsitter, VTOL). Parts are sorted into roles (arms, motors, props, battery …) from their names.
   Or open an example: **Quad 250** or **K09-L**.
3. Fill the **parameter sheet** on the left: motor, propeller, battery, materials, masses, cruise speed. Every
   value carries its evidence: *estimate*, *datasheet*, *measured*. Empty values are never guessed: the
   analysis that needs them says "not evaluated" and names what is missing.
4. **Run all**. Mass, propulsion (thrust, hover throttle, endurance), arm or wing stress (CalculiX), the
   **whole-aircraft** airflow (OpenFOAM) and a hover flight simulation run in the background; the dashboard on
   the right shows the 3D model, the results, the contours, the flight replay and design iterations. It is
   served by the app only while the project is open.
5. **Iterations** tab: try a change (for example a bigger battery) and compare with the baseline.

Results that rest on estimates are shown as **Conditional – needs a test**, never as a pass. Nothing here is
flight approval. Results take about 2 MB per run; solver scratch folders are deleted after each run.

Agents use the same engine from the command line: see `AGENTS.md` (`opencae run`, `opencae iterate` …).
The parameter file is described in `docs/DRONE_PROJECTS.md`.

## 1. The K09-L design review

In the app: **Drone → K09-L** shows the K09-L design review inside the app. Or double-click
**`open_website.bat`** (the site is built into `%USERPROFILE%\.opencae\k09l_website` the first time). The
switch at the top left changes between **Revision B** (the current design) and **Revision C ★** (the
recommended one).

Everything is on one page around one 3D viewer that every section drives. You never need to open
separate files.

| Section | What it shows |
|---|---|
| **Overview** | The verdict, the readiness gates, six key numbers, what has to change (with suggested fixes), every analysis, cost, what changed from revision A and the limits before the first flight. |
| **Design** | Assembled, **exploded** (slider), **electrical routing** (3D wires coloured power / phase / signal, with currents), **fabrication** (step through 40 make steps and 8 assembly steps; the parts of each step light up), **cost map**, **check status** and the four **drawing sheets** (general arrangement, exploded, BOM, wiring) in a zoomable viewer. |
| **Analyses** | Contour plots: the aircraft-level runs (motor tube, spar, foot, motor mount, whole-aircraft CFD) and one **component screening** run per load-carrying part (fins, elevons, horns, clips, plates, mast, joiner, torque rod …). **Show** switches the quantity: von Mises stress, displacement, strain, max principal stress, temperature, heat flux, pressure coefficient, static pressure, near-wall velocity, wall shear, y+. Structural runs have **Deformed** (scale slider) and **Animate loading**. The critical region keeps its colours and the rest turns grey; the red dot is the worst point. Tick **Mesh** to see the mesh. Each run has its failure parameter, load case, mesh profile, solver convergence and what to do if it does not pass. The CFD adds four flow slices and 100 streamlines. |
| **Flight** | The **full mission** from the ground (take-off from the pad, climb to 30 m, transition, wing-borne cruise, back-transition, descent, landing), plus the hover and transition cases, replayed at real scale in a virtual field (1 m grid, runway, trees about 8 m tall, a 30 m pole with 5 m marks). Cameras: follow (drag to orbit, no lag), ground observer, side view of the whole path, top. The timeline is coloured by phase; click a phase to jump to it. |
| **Components** | Every BOM component with its checks: structural screening FEA, rating checks (motor, ESC, battery, capacitor), wire ampacity, fastener checks, mass; cost, process and what to do when a check fails. Click any part on the model or search the list. |
| **Decisions** | Gate decisions G1–G6 (GO / GO with conditions / NO-GO / not ready), what blocks each gate, **how each conditional becomes a pass** (the test and measurement that closes it, with a form to check measured values and copy them for import), and every item that is not a clean pass with actions, their computed effect and the test that verifies them. |
| **Recommendations** | Revision C against revision B: each change with its reason and before/after numbers, the full B-vs-C comparison of requirements, gates and key quantities, the revision-C flight card, the estimates checked against datasheets, and the recommendations that need new CAD. |
| **Process** | Process flows (analysis pipeline, design-change propagation, fabrication and assembly, verification tests by gate) and the knowledge graph from inputs through calculations and solver runs to requirements and gates. |
| **Audit** | Every requirement, solver run, estimated input, monitored quantity and the test matrix. |

### Colours

| Colour | Meaning |
|---|---|
| Green **OK** | Inside the limit, with margin |
| Amber **Conditional / watch** | Predicted to pass, but the prediction rests on estimated data, or the margin is thin |
| Red **Fail** | Outside its limit: fix it, or prove the prediction wrong |
| Grey **Not run / not evaluated** | No evidence yet |

## 2. The analyses and their failure parameters

| Analysis | Solver | Failure parameter | Fails when |
|---|---|---|---|
| Motor tube, full thrust | CalculiX FEA | Safety factor = allowable / governing stress | SF < 1.5 shows watch, SF < 1 fails |
| Wing spar, 4 g | CalculiX FEA | Safety factor | Same |
| Outrigger foot, 1 m/s touchdown | CalculiX FEA | Safety factor against the printed-part strength | Same |
| Motor mount, hover on a 35 °C day | CalculiX heat transfer | Temperature margin = material limit − hottest point | Margin < 0 |
| Aircraft cruise aerodynamics | OpenFOAM RANS | Lift ratio = C_L at the flight-model trim angle / C_L that level cruise needs | Below 0.9 |
| Assembly check | Geometry sweep | Interferences, and prop clearance over a full revolution | Any prop strike |
| Hover and transition | 6-DOF flight model | Position error, height lost in the transition | Ground contact or more than 30 m lost |

None of these is flight evidence. Material, propeller and aerodynamic inputs are estimates (listed on the
Audit page). A requirement is marked **Pass** only on direct evidence, such as geometry or a datasheet. Every
other requirement stays **Conditional** until the physical test in the test matrix confirms it.

## 3. Work in the application

| You want to | Do this |
|---|---|
| Start the app | `launch_opencae.bat` |
| Compare variants, run solvers, rebuild the website | `launch_opencae.bat --example k09l-monitor`, then use **Run analyses** and **Build website** (Ctrl+B) |
| Inspect one variant page by page (geometry, mass, propulsion, flight, approval) | In the Design Monitor, click **Open in platform** |
| See the contours component by component in the app | Platform page **2 · Engineering Analysis**, tab **Contours** (every aircraft-level and component screening run; quantity, critical region only, mesh, deformed shape, mesh profile) |
| Turn a conditional requirement into a pass | Do the test, fill `opencae\examples\k09l\measurements_template_revB.csv` (or copy the values from the website's Decisions page), then **Import measurements…** in the Design Monitor. A requirement passes only when every input it needs was measured. |
| Rebuild the website from the command line | `python -m opencae.web.build` (writes `%USERPROFILE%\.opencae\k09l_website`) |
| Rebuild the bundled study and website (all solvers, about 2 hours on 2 cores) | `python -m opencae.examples.k09l.build_study` |
| Run the tests | `python -m pytest opencae/tests -q` |

## 4. Where things are

| Path | Contents |
|---|---|
| `opencae/engineering/` | The engineering model: catalog, calculations, dependency graph, approval rules, study (variants) |
| `opencae/engineering/field_analyses.py` | Foot FEA, mount thermal and whole-aircraft cruise CFD, which produce the contour data |
| `opencae/engineering/component_screening.py` | The screening of every component (FEA of load-carrying parts, rating, wiring and fastener checks) |
| `opencae/engineering/revision_c.py` | Revision C (recommended): changes C1, C2, the evaluated-but-not-applied list N1–N11 |
| `opencae/engineering/revision_b.py` | Design revision B: changes B1 to B6 and why each was made |
| `opencae/web/` | Website builder (`build.py`) and page templates |
| `opencae/ui/platform/` | Desktop UI: the six-page platform and the Design Monitor |
| `opencae/examples/k09l/` | Bundled K09-L data, the study file, the contour fields and the built website |
| `docs/K09L_PLATFORM.md` | Full engineering record: audit, architecture, decisions, test results |

## 5. Current status

| Check | Revision B (current) | Revision C ★ (recommended) |
|---|---|---|
| Requirements | 2 pass, 10 conditional, 0 fail | same: C is the same hardware, flown differently |
| Whole-aircraft CFD at the cruise trim | **C_L 0.345 vs 0.590 needed (58 %) at 12.5 m/s: fail** | C_L 0.341 vs 0.341 needed at 16.4 m/s (100 %): watch, because 18 % of the surface still has separated flow |
| Motor tube / spar / foot / motor mount | SF 50.6 / 4.86 / 4.80 / 18.4 K margin: OK | same |
| Component screening (92 components) | 33 ok, 11 watch, **4 fail**, 44 not evaluated | same parts; the control horn is worse (higher design speed) |
| Full mission (ground to ground) | every phase completed in the model; touchdown 0.47 m/s; 4.96 Wh | |
| Gates | G1, G2 GO with conditions; G3, G4 NO-GO; G5, G6 not ready | same |

**What fails and what to do.** The website's Decisions page shows each item with its computed effect.

- **Cruise lift (CFD).** The wing makes only 58 % of the lift the flight model assumed. Revision C takes the
  CFD lift slope as its design basis: cruise moves to 16.4 m/s at 94 W, cruise endurance to 30.2 min, and the
  transition becomes gentler. At that trim the whole-aircraft CFD gives the lift that is needed.
- **Depron fin.** Under the screening touchdown load the whole foot force goes into 6 mm foam: SF 0.03.
  Clamp the foot to the motor tube instead, or bond a G10/CF doubler (needs new CAD; revision C item N9).
- **Elevons and control horn.**
  - The elevons fail against the foam's compressive strength but have about 3× margin in bending.
  - The PETG horn fails in its weakest print direction (about 1.1 in-layer).
  - Fixes (N10):
    - print the horn flat and thicker, or cut it from G10
    - add a hard strip along the hinge line
    - pull-test it (T05)
- **Power wires.** Above their continuous rating at full throttle, within it at hover: watch (N11).

**Why the conditionals are still conditional.** Every one rests on an estimate or a test that has not been
done. No model change can turn them into a pass. Do the tests in the measurement template and import the
values; the requirements then pass or fail on the measurements. The main estimates were checked against
manufacturer sources: the ESC ratings are now confirmed by Holybro's datasheet, and the rest is listed on the
Recommendations page with what is still needed.

The team's current design in the DBF-Combined repository is **Concept A**: 0.90 m wing, four MN2212 motors.
The K09-L was dropped there because its cheapest build costs more than the $2,000 cap. This platform still models
the K09-L. Any new concept goes through the same pipeline: model, solver runs, then the website.

