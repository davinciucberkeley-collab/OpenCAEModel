# Windows setup (Miniforge, two environments)

This is the layout OpenCAE Studio is developed against on Windows:

| Environment | Contents | Used for |
|---|---|---|
| `qt_test` | Python 3.11+, PySide6, VTK/PyVista, OCP, Gmsh, NumPy/SciPy, meshio, Matplotlib | the GUI process |
| `opencae` | conda-forge `calculix` (2.21) | the CalculiX solver process only |
| (optional) SU2 folder | `SU2_CFD.exe` from the SU2 release ZIP | flow (CFD) studies only |

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

## SU2 for flow studies (optional)

SU2 is not on conda-forge for Windows. To use flow studies:

1. Download the Windows release ZIP from <https://github.com/su2code/SU2/releases> (for example
   `SU2-v8.x.x-win64.zip`) and unzip it anywhere, for example `C:\SU2`.
2. In **Tools → Settings**, set *SU2* to `...\bin\SU2_CFD.exe`, press **Test SU2**, then **Save**.

You do **not** need to edit PATH, set `SU2_RUN` or install MPI: OpenCAE starts `SU2_CFD.exe` serially in its
own process environment. The SU2 test solves a laminar channel and compares the pressure gradient with the
exact solution. SU2 is LGPL-2.1 software and runs as a separate program.
