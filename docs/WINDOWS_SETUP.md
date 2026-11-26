# Windows setup (Miniforge, two environments)

This is the layout OpenCAE Studio is developed against on Windows:

| Environment | Contents | Used for |
|---|---|---|
| `qt_test` | Python 3.11+, PySide6, VTK/PyVista, OCP, Gmsh, NumPy/SciPy, meshio, Matplotlib | the GUI process |
| `opencae` | conda-forge `calculix` (2.21) | the CalculiX solver process only |
| (optional) OpenFOAM inside WSL | ESI OpenFOAM, installed by the **Solvers** button | flow (CFD) studies and drone airflow |

Nothing is installed or upgraded by OpenCAE Studio, and no system environment variable is changed.

## Starting

```bat
launch_opencae.bat               :: start the GUI (activates qt_test only for this window)
launch_opencae.bat --doctor      :: check Python, packages, Gmsh, OCP and run the CalculiX self-test
launch_opencae.bat --acceptance  :: cantilever acceptance test: create, mesh, solve, check, save/reopen, report
```

Optional variables (set them in the shell before calling the script if your layout differs):
`OPENCAE_CONDA_ROOT` (default: auto-detected, e.g. `%USERPROFILE%\miniforge3`), `OPENCAE_GUI_ENV`
(default `qt_test`).

In VS Code, the tasks *OpenCAE: Start / Doctor / Acceptance / Run tests* and the debug configuration
*OpenCAE Studio* use the same script and interpreter.

## Why CalculiX failed with exit code -1073741515

`-1073741515` is `0xC0000135 STATUS_DLL_NOT_FOUND`: Windows could not load a DLL that `ccx.exe` needs, so the
solver died before reading the input. conda-forge's `ccx.exe` lives in `opencae\Library\bin`, but its Fortran /
OpenMP runtime (`libgfortran-3.dll`, `libgomp-1.dll`, `libquadmath-0.dll`, `libarpack.dll`,
`libgcc_s_seh-1.dll`, `libwinpthread-1.dll`) is in `opencae\Library\mingw-w64\bin`. That folder is only on PATH
while the `opencae` env is *activated*, and the GUI runs from `qt_test`.

Activating `opencae` globally is not a fix: its `Library\bin` contains other Qt/ICU/MSVC DLLs that break the GUI.
Instead, OpenCAE Studio builds an environment **for the solver child process only**: the solver's own folders
(`Library\bin`, `Library\mingw-w64\bin`, `Library\usr\bin`, `Scripts`, the env root) come first, then the
Windows system folders, then the rest of PATH without the GUI environment's folders. The GUI process is not
modified.

This was checked with your actual `ccx.exe` 2.21 and its DLLs (run under Wine on Linux): started without the
`mingw-w64` folder it fails with exactly `-1073741515`, and started with the solver environment it gives the
same results as a Linux CalculiX build. `liblapack.dll` and `libblas.dll` forward every routine to MKL
(`mkl_rt.3.dll`, also in `opencae\Library\bin`). The static DLL check follows those forwarders.

## Tools → Settings → Test Solver

The test does not trust the presence of `ccx.exe`. It:

1. checks the executable and resolves every DLL it needs (imports, delay-loads and export forwarders) in the
   solver folders, naming any missing DLL;
2. starts `ccx -v` in the isolated environment;
3. solves a unit cube in tension and compares the displacements with the exact solution;
4. solves a one-element eigenvalue problem (ARPACK + LAPACK/MKL, used by modal and buckling analyses) and
   compares the eigenvalue with the exact value.

The solver is shown as *verified* only when all steps pass. Every solve also writes `solver.log` (command,
solver PATH, exit code with its meaning, full output) into the run folder.

## OpenFOAM in WSL (optional)

All flow studies (Studio and the drone airflow analysis) run OpenFOAM inside WSL; the **Solvers** button
(`Drone > Solvers...`, or `opencae setup openfoam --yes`) does steps 1 to 3 below for you, except installing WSL itself. This installs a Linux distribution and, inside it, a separate
Miniforge; nothing is changed in your Windows Miniforge, PATH or environment variables.

1. Administrator PowerShell: `wsl --install -d Ubuntu-24.04`, reboot if asked, create the Linux user.
2. In the Ubuntu terminal:

   ```bash
   curl -L -O https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
   bash Miniforge3-Linux-x86_64.sh -b
   ~/miniforge3/bin/conda create -y -n openfoam -c conda-forge openfoam=2412
   ```

   (about 1 GB). Do **not** use `sudo apt install openfoam` (Ubuntu's v1912; OpenCAE refuses it).
3. In OpenCAE, **Tools → Settings → OpenFOAM**: leave the environment empty (it is found in
   `~/miniforge3/envs/openfoam`) or enter `/home/<user>/miniforge3/envs/openfoam`; set the WSL distribution if it
   is not the default; press **Test OpenFOAM** (about a minute), then **Save**.
4. `python -m opencae.app.doctor` also reports and tests OpenFOAM.

Cases are written in the project's data folder on the Windows drive and run through `/mnt/<drive>/...`; that
is slower than the Linux file system but keeps all results with the project.
