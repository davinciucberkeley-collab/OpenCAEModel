# Installing OpenCAE Studio on Windows (Miniforge)

This guide installs the complete application from `OpenCAE-Studio-0.2.0.zip`. It uses two conda
environments: `qt_test` for the GUI and `opencae` for the CalculiX solver. Steps 1 and 2 only install
packages into those two environments; nothing outside them and no system setting is changed.

If you already have working `qt_test` and `opencae` environments, skip to step 3. Your installed packages are
not upgraded or reinstalled.

## 0. What you need

* Windows 10 or 11 (64-bit) with Miniforge (https://github.com/conda-forge/miniforge), installed in the
  default place (`%USERPROFILE%\miniforge3`).
* About 3 GB of free disk space for the two environments.
* Optional, for flow (CFD) studies only: SU2 8.x for Windows (step 6).

All commands below are typed in the **Miniforge Prompt** (Start menu).

## 1. GUI environment `qt_test` (skip if it exists)

```bat
conda create -n qt_test -c conda-forge python=3.12 -y
conda activate qt_test
pip install PySide6==6.12.0 pyvista==0.49.1 pyvistaqt==0.13.1 vtk==9.6.2 gmsh==4.15.2 meshio==5.3.5 ^
            numpy==2.4.6 scipy==1.17.1 matplotlib==3.11.2 cadquery-ocp==8.0.1.1.0 pytest
```

Python 3.12 or newer is recommended. Python 3.11 works too: with PySide6 6.12 on 3.11 OpenCAE detects a
reference-counting defect in PySide6 at start-up and compensates (`--doctor` reports it).

To check which environments exist: `conda env list`.

## 2. Solver environment `opencae` with CalculiX (skip if it exists)

```bat
conda create -n opencae -c conda-forge calculix -y
```

OpenCAE finds `ccx.exe` in this environment automatically. It runs CalculiX as a separate process, with the
solver's own DLL folders first, which solves the `-1073741515` (DLL not found) error. Your `PATH` is not
changed.

## 3. Unpack the application

1. Unzip `OpenCAE-Studio-0.2.0.zip`, for example to `E:\Calculix\` (giving
   `E:\Calculix\OpenCAE-Studio-0.2.0\`).
2. Keep your old folder (`E:\Calculix\OpenCAE-Studio-0.1.0`) until the new one passes step 4. Projects
   (`.ocae` files) from 0.1 open in 0.2 and are migrated automatically. Save them under a new name if you
   want to keep the 0.1 version.

## 4. Check the installation

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

If `qt_test` or Miniforge are somewhere else, set them for that window only before running the launcher:

```bat
set OPENCAE_CONDA_ROOT=D:\miniforge3
set OPENCAE_GUI_ENV=my_gui_env
launch_opencae.bat --doctor
```

## 5. Start the application

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

## 6. Optional: SU2 for flow (CFD) studies

1. Download the Windows release ZIP of SU2 8.x from https://github.com/su2code/SU2/releases.
2. Unzip it, for example to `C:\SU2`, so that `C:\SU2\bin\SU2_CFD.exe` exists.
3. In OpenCAE, open **Tools → Settings**. In the SU2 section, click **Browse…**, choose `bin\SU2_CFD.exe`, then
   **Test SU2** and **Save**. The test runs a 2-D channel flow and checks the pressure drop against the exact
   solution. No `PATH` or `SU2_RUN` change is needed.

Without SU2, everything except flow studies works.

## 7. Optional: run the automated tests and benchmarks

```bat
conda activate qt_test
cd /d E:\Calculix\OpenCAE-Studio-0.2.0
python -m pytest -q                                          :: about 6 minutes
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
| Application closes unexpectedly | Log: `%USERPROFILE%\.opencae\logs\opencae.log`. Each solve also writes `solver.log` in its job folder |

More detail: `docs\WINDOWS_SETUP.md`, `docs\USER_GUIDE.md`, `docs\CFD.md`, `docs\VALIDATION.md`.
