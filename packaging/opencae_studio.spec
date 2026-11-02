# PyInstaller spec for OpenCAE Studio (one-folder bundle).
#   pyinstaller packaging/opencae_studio.spec --distpath dist --workpath build
# CalculiX is NOT bundled (separate GPL-2 program): install ccx on the target machine
# or set OPENCAE_CCX.
import ctypes.util
import glob
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

root = Path(SPECPATH).parent
datas, binaries, hidden = [], [], []
for pkg in ("OCP", "vtkmodules", "pyvista", "pyvistaqt", "meshio", "scooby"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hidden += h
hidden += collect_submodules("opencae")

# Gmsh: gmsh.py loads libgmsh from its own directory (sys._MEIPASS in the bundle)
import gmsh as _g
_gdir = Path(_g.__file__).parent
for cand in [*_gdir.glob("libgmsh*"), *(_gdir.parent.parent / "lib").glob("libgmsh*"),
             *(_gdir.parent.parent.parent / "lib").glob("libgmsh*"), *_gdir.glob("gmsh*.dll")]:
    binaries.append((str(cand), "."))
# OCP's bundled OpenCascade libraries
import OCP as _ocp
_libs = Path(_ocp.__file__).parent.parent / "cadquery_ocp.libs"
if _libs.exists():
    binaries += [(str(p), "cadquery_ocp.libs") for p in _libs.iterdir()]

datas += [(str(root / "docs"), "docs"), (str(root / "opencae" / "examples" / "projects"), "opencae/examples/projects")]

a = Analysis([str(root / "packaging" / "launch.py")], pathex=[str(root)], binaries=binaries, datas=datas,
             hiddenimports=hidden + ["gmsh", "matplotlib.backends.backend_qtagg"],
             excludes=["tkinter", "pytest", "IPython", "trame", "trame_vtk", "trame_vuetify"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="OpenCAE-Studio", console=False, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="OpenCAE-Studio")
