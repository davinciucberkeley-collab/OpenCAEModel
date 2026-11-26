# Installing OpenCAE Studio on Windows (Miniforge)

This guide installs the complete application on a new Windows PC, including the K09-L platform and the
Design Monitor. It reproduces the setup verified on the original PC (November 2026): two conda environments,
`qt_test` (Python 3.11.18 and the GUI libraries) and `opencae` (the CalculiX 2.21 solver). Nothing outside
these two environments and no system setting is changed.

## 0. What you need

* Windows 10 or 11 (64-bit), about 4 GB of free disk space, an internet connection for the installation.
* A graphics driver with OpenGL 3.2 or newer (any recent Intel, AMD or NVIDIA driver). Over Remote Desktop or in
  a virtual machine the 3D views may not work; everything else does.
* The application zip, e.g. `OpenCAE-Studio-0.1.0-k09l-monitor.zip`.

## 1. Install Miniforge (skip if `Miniforge Prompt` is already in the Start menu)

1. Download `Miniforge3-Windows-x86_64.exe` from https://github.com/conda-forge/miniforge/releases (latest).
2. Run it: **Just Me**, default folder (`C:\Users\<you>\miniforge3`). Leave "Add to PATH" unticked.
3. Open **Miniforge Prompt** from the Start menu. All commands below are typed there, one line at a time.

## 2. GUI environment `qt_test`

```bat
conda create -n qt_test -c conda-forge python=3.11 pip -y
conda activate qt_test
pip install PySide6==6.12.0 pyvista==0.49.1 pyvistaqt==0.13.1 vtk==9.6.2 gmsh==4.15.2 meshio==5.3.5 numpy==2.4.6 scipy==1.17.1 matplotlib==3.11.2 cadquery-ocp==8.0.1.1.0 mujoco==4.0.1 pytest
```

`mujoco` is the physics engine of Motion mode (assemblies in motion). The app starts without it, but Motion
studies then cannot run. You can also add it later with `python -m pip install mujoco` (or `opencae setup mujoco --yes`).

These are the versions the application was verified with (`requirements.txt` lists the same). If an
environment called `qt_test` already exists with other content, use another name, e.g. `opencae_gui`, and set
`OPENCAE_GUI_ENV` as in step 5.

## 3. Solver environment `opencae` with CalculiX

```bat
conda create -n opencae -c conda-forge calculix=2.21 -y
```

OpenCAE finds `ccx.exe` in this environment by itself and runs it as a separate process with the solver's own
DLL folders first (no `PATH` change; this avoids the `-1073741515` "DLL not found" error).

## 4. Unpack the application

Unzip the application zip to a folder of your choice, e.g. `E:\Calculix\`, giving
`E:\Calculix\OpenCAE-Studio-0.1.0\` with `launch_opencae.bat`, `opencae\`, `docs\` inside. Avoid folders
synchronised by OneDrive.

## 5. Check the installation

Double-click, or run from a Command Prompt in the new folder:

```bat
launch_opencae.bat --doctor
```

This lists Python and every library with its version, finds CalculiX and runs a real test analysis
(static, thermal and frequency checks against exact values). It ends with `RESULT: all checks passed` or names
what is missing. Then run:

```bat
launch_opencae.bat --acceptance
```

This builds the cantilever in the GUI, meshes and solves it, compares it with beam theory, saves and reopens
it and writes a report. It ends with `ACCEPTANCE TEST PASSED`, and the details are in
`acceptance_output\selftest.json`.

If Miniforge is somewhere else or the GUI environment has another name, set them for that window only:

```bat
set OPENCAE_CONDA_ROOT=D:\miniforge3
set OPENCAE_GUI_ENV=my_gui_env
launch_opencae.bat --doctor
```

## 6. Start the application

```bat
launch_opencae.bat                 :: start
launch_opencae.bat myproject.ocae  :: open a project
```

In the app, **Tools → Settings → Test Solver** repeats the CalculiX test at any time. A first run:
**File → Open cantilever example**, **Mesh → Generate** (Ctrl+M), **Solve** (F5), then
**Results → Compare with theory** (tip deflection ≈ 1.000 mm).

**VS Code:** open the folder (File → Open Folder). The workspace selects
`%USERPROFILE%\miniforge3\envs\qt_test\python.exe` (edit `.vscode\settings.json` if it is elsewhere). It
offers debug configurations (*OpenCAE Studio*, *Doctor*, *Acceptance test*, *Validation benchmarks*) and the
tasks *OpenCAE: Start / Doctor / Acceptance / Run tests*.

## 7. The K09-L platform and Design Monitor

```bat
launch_opencae.bat --example k09l            :: six-page engineering platform
launch_opencae.bat --example k09l-revB       :: platform with design revision B and its solver runs
launch_opencae.bat --example k09l-monitor    :: Design Monitor: variants, monitoring, dashboard report
```

The first start of the Design Monitor creates your study `%USERPROFILE%\.opencae\k09l_study.json` from the
bundled one (revisions A, B and variant C, all analyses current). Check: all three variants show `⚙5/5`, and
**Run analyses** on a new variant finishes in about 1.5 minutes. To bring your own variants from another PC,
copy that file to the same place on the new PC (or use **Open study…**).

## 8. Optional: SU2 for flow (CFD) studies

1. Download the Windows release ZIP of SU2 8.x from https://github.com/su2code/SU2/releases.
2. Unzip it, for example to `C:\SU2`, so that `C:\SU2\bin\SU2_CFD.exe` exists.
3. In OpenCAE, open **Tools → Settings**. In the SU2 section, click **Browse…**, choose `bin\SU2_CFD.exe`, then
   **Test SU2** and **Save**. The test runs a 2-D channel flow and checks the pressure drop against the exact
   solution. No `PATH` or `SU2_RUN` change is needed.

Without SU2, everything except flow studies works.

## 9. Optional: run the automated tests and benchmarks

```bat
conda activate qt_test
cd /d E:\Calculix\OpenCAE-Studio-0.1.0
python -m pytest -q                                          :: about 10 minutes
python -m opencae.examples.run_validation --quick            :: benchmarks, about 2 minutes
```

The tests that need SU2 are skipped when SU2 is not set up.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `conda.bat not found` / `environment not found` | Set `OPENCAE_CONDA_ROOT` / `OPENCAE_GUI_ENV` as in step 4 |
| Doctor: `FAIL <library>` | `conda activate qt_test` and `pip install` that library (versions in step 1) |
| Doctor: no ccx executable | Step 2, or set the path to `ccx.exe` in Tools → Settings |
| Solver exit code `-1073741515` | Run `--doctor`: it lists the missing DLL and the folder it looked in. Send that output |
| Design Monitor: "3D view unavailable" | No OpenGL 3.2 (remote desktop / VM / old driver): update the graphics driver or run locally; the rest works |
| Application closes unexpectedly | Log: `%USERPROFILE%\.opencae\logs\opencae.log`. Each solve also writes `solver.log` in its job folder |

More detail: `docs\WINDOWS_SETUP.md`, `docs\USER_GUIDE.md`, `docs\CFD.md`, `docs\VALIDATION.md`.
