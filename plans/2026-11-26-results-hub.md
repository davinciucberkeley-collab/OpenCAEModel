# Results Hub, viewing, motion simulator, help: plan

Conductor: main session. Standard: `standards/agents.md` v0.2. Builders leave changes uncommitted; the conductor
commits. Interfaces: `plans/2026-11-26-results-hub/contract.md` (read it with this file).

## Request (user, 2026-11-26 00:27, verbatim where quoted)

"i want a genralised viewable dashboard that stores results same dashobard for k09 tilt wing quad 250 gripper and
all the projects you can use the dashboard refernence of K09L earlier built it seems detailed an nice. also the app
is working fine but the results are not being visible for example after cfd analysis for the drone is done there is
no option of visualising it or auditing it such that OPenFOAM is running but i cannot see contours and plots.
i also rearrange the viewspace of open cae to something like the image i have attached ... improve the viewing
interface such that anyone could easily access it you could add a help button on top that would store all the
reference commands needed fir the command console . Also i added motuion study newly it should be there on the
dashbaord along with a simulator showing how the body would work, for example a hovering simulation for drone ,
motion simmulation for gripper etc. ... IMPROVE THE VISUAL SPACES AND VIEWING SCOPE SO THAT I CAN ESTABLISH STRONG
RESULTS"

Screenshot layout (keep as the default): Progress Monitor left; Drone parameters table centre-left; dashboard
(3D + Results/Contours/Flight/Parameters/Iterations) centre-right; Command Console along the bottom.

| # | Request | Owner | Status |
|---|---|---|---|
| H1 | one dashboard for every project (K09-L, quad250, gripper, Studio projects), same look (K09-L website) | V1 + V2 | |
| H2 | results stored: run history per project, open any past run, compare two runs | V1 + V2 | |
| H3 | CFD visible: surface contours (Cp, p, wall shear, near-wall speed), slices, streamlines, residual + C_L/C_D plots | C0 + V1 + V2 | C0 done (root cause) |
| H4 | audit: solver, version, mesh quality, BCs/inputs with evidence status, convergence, log tail, files | V1 + V2 | |
| H5 | motion study on the dashboard with a simulator: gripper MBD playback, drone hover, K09-L flight replay | V3 | |
| H6 | default layout like the screenshot; viewing easy for anyone | V4 | |
| H7 | Help button on top: every console command, searchable, click inserts into the console | V4 | |
| H8 | 3D works on the user's PC (WebGL failed inside the app: "THREE.WebGLRenderer: Error creating WebGL context") | V4 + V2 | |

## Evidence from the user's PC (2026-11-26)

- `projects/quad250/results/runs.jsonl`: every CFD run `error`. Cause: `foamToVTK -poly-decomp` (VTKflow)
  segfaults in OpenFOAM v2412 (conda-forge) after a finished 300-iteration solve. Reproduced here on the user's case
  (staged `cfd_case_min.tgz`); cell-data export without `-poly-decomp` works and slices/streamlines build from it.
  **C0 (conductor, done):** `FA.CFD_VTK` VTKflow without `-poly-decomp`; a failed volume export no longer discards the
  solve (forces and surface fields kept, problem shown in the summary and `extra.postprocess_problems`).
- `logs/opencae.log`: `THREE.WebGLRenderer: Error creating WebGL context` on every dashboard load since the Windows
  Chromium flags became `--disable-gpu --disable-gpu-compositing --enable-unsafe-swiftshader`; earlier
  `CONTEXT_LOST_WEBGL` and "D3D11 device was removed" with the GPU path. So WebGL inside the app is unreliable on that
  PC either way: every 3D view needs a fallback that does not depend on WebGL (V2: server-rendered images; V3: 2-D
  canvas animation; V4: native VTK viewer in the app and "open in browser").
- Console: user typed `open new` while the console was in Python mode → SyntaxError. Commands must work in either mode.

## Agent table

| Step | Role | Model | Input | Output | Pass criterion |
|---|---|---|---|---|---|
| V1 | builder | sonnet | contract | `opencae/hub/*` (registry, adapters, snapshots, render), hub API in `opencae/web/server.py` | `test_hub_backend.py` exit 0 |
| V2 | builder | sonnet | contract | `opencae/web/hub/*` front end (K09-L look) | `test_hub_frontend.py` exit 0 + Playwright screenshots |
| V3 | builder | sonnet | contract | `opencae/hub/motion.py`, `opencae/web/hub/motion.js` | `test_hub_motion.py` exit 0 + screenshots |
| V4 | builder | sonnet | contract | `opencae/ui/hub_view.py`, `opencae/ui/help.py`, console fix, layout, WebGL flags | `test_hub_ui.py` exit 0 + screenshots |
| R5 | reviewer | opus | diff, plan | `reviews/2026-11-26-results-hub.md` | criteria PASS/FAIL |
