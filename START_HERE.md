# Start here

OpenCAE Studio is the engineering backend for the K09-L tailsitter. It holds one engineering model of the
aircraft, runs the solvers on it, and writes a **design-review website**. The website is what you look at and
share. The application is where an engineer runs, audits and changes things.

## 1. Look at the results (no Python needed)

Double-click **`open_website.bat`**, or open `opencae\examples\k09l\website\index.html` in a browser.

| Page | What it shows |
|---|---|
| **Overview** | The verdict, six key numbers, one card per analysis, what changed from revision A, and the limits before the first flight. The props spin at 1/40 of their hover speed. |
| **Results** | Contour plots. Pick an analysis from the list or click a coloured part on the 3D aircraft. The critical region keeps its colours and everything else turns grey. The red dot marks the worst point. Click the contour to read a value. The right panel gives the failure parameter, the limit, the load case and the assumptions. |
| **Audit** | Every requirement, the readiness gates, the solver runs and their dates, the estimated inputs, all monitored quantities and the test matrix. |

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
| Rebuild the website from the command line | `python -m opencae.web.build` (writes `%USERPROFILE%\.opencae\k09l_website`) |
| Rebuild the bundled study and website (all solvers, about 1 hour) | `python -m opencae.examples.k09l.build_study` |
| Run the tests | `python -m pytest opencae/tests -q` |

## 4. Where things are

| Path | Contents |
|---|---|
| `opencae/engineering/` | The engineering model: catalog, calculations, dependency graph, approval rules, study (variants) |
| `opencae/engineering/field_analyses.py` | Foot FEA, mount thermal and cruise CFD, which produce the contour data |
| `opencae/engineering/revision_b.py` | Design revision B: changes B1 to B6 and why each was made |
| `opencae/web/` | Website builder (`build.py`) and page templates |
| `opencae/ui/platform/` | Desktop UI: the six-page platform and the Design Monitor |
| `opencae/examples/k09l/` | Bundled K09-L data, the study file, the contour fields and the built website |
| `docs/K09L_PLATFORM.md` | Full engineering record: audit, architecture, decisions, test results |

## 5. Current status (revision B)

| Check | Result |
|---|---|
| Requirements | 2 pass, 10 conditional (predictions on estimated data), 0 fail |
| Motor tube, full thrust | SF 50.6: OK |
| Wing spar, 4 g | SF 4.86: OK |
| Outrigger foot, touchdown | SF 4.80: OK |
| Motor mount, hover heat | 66.6 °C against the ASA limit of 85 °C, a margin of 18.4 K: OK. In PETG it would fail, which is why change B6 moves the mounts to ASA. |
| Props | All four spin freely, with a minimum clearance of 29.5 mm over a full revolution. In the app they only looked stuck because they were never animated. They are animated now. |
| Hover and transition | Holds position within 0.9 mm. Loses 0.37 m in the transition. |
| **Cruise aerodynamics (CFD)** | **Open problem.** At the flight model's cruise trim the wing makes only about 62 % of the lift that level flight needs. Two mesh levels agree within 5 %. Either cruise is faster (about 16 m/s) or the wing flies at a higher angle (about 14°), so the cruise and endurance predictions are optimistic. Settle the wing's lift slope with a wind-tunnel or flight test before relying on them. |

The team's current design in the DBF-Combined repository is **Concept A**: 0.90 m wing, four MN2212 motors.
The K09-L was dropped there because its cheapest build costs more than the $2,000 cap. This platform still models
the K09-L. Any new concept goes through the same pipeline: model, solver runs, then the website.

