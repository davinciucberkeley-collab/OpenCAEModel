# Packaging

## One-folder executable (PyInstaller) — tested on Linux x86-64

```bash
pip install pyinstaller
pyinstaller packaging/opencae_studio.spec --noconfirm --distpath dist --workpath build
./dist/OpenCAE-Studio/OpenCAE-Studio                     # start the application
./dist/OpenCAE-Studio/OpenCAE-Studio --selftest /tmp/st  # installation self-test (needs a display)
```

The self-test drives the real GUI through the cantilever workflow (mesh → solve → results → report). It writes
`selftest.json` and a screenshot, and returns exit code 0 on success. Verified results for the bundle built
from this repository (PyInstaller 6.22, Python 3.13, Ubuntu 24.04):

* `--selftest`: passed. Max |U| = 1.00267 mm, identical to the source run.
* `--worker opencae.examples.run_validation --quick`: benchmarks A–E all PASS inside the bundle.
* Bundle size: about 1.6 GB uncompressed, dominated by Qt (PySide6) and VTK.

How it works:

* Background meshing and the benchmark runner normally start `python -m <module>`. A frozen app has no
  interpreter, so `opencae.app.main.worker_command` re-launches the executable with `--worker <module>`.
* `libgmsh` is placed next to the frozen `gmsh` module, where `gmsh.py` looks for it. The OpenCascade libraries
  from `cadquery_ocp.libs` are collected explicitly.

## CalculiX is not bundled

CalculiX is a separate GPL-2.0 program. Install `ccx` on the target machine and the application finds it on
`PATH`. Alternatively, set `OPENCAE_CCX` or configure the path in Tools → Settings. If you redistribute a
`ccx` binary with the bundle, you must also meet GPL-2.0 obligations, including offering its source.

## Other platforms

The same spec should work on Windows and macOS with that platform's Python wheels (all dependencies publish
wheels), but **this has not been tested**. Known points to check:

* Windows: `gmsh-4.15.dll` is collected by the same glob. Use `ccx.exe` from the CalculiX or PrePoMax
  distributions.
* macOS: code signing and notarisation are required for distribution; build a `.app` with `BUNDLE(...)`.
* conda-forge provides every dependency, including `calculix`, and is a simpler route for managed
  environments.
