"""Solver discovery, isolated child environment and Windows DLL diagnostics.

The PE fixtures in tests/data/pe were cross-compiled with MinGW:
fakeccx.exe -> libmid.dll -> libdep-1.dll (+ msvcrt/kernel32), reproducing the
conda-forge layout where ccx.exe lives in Library/bin and its runtime in
Library/mingw-w64/bin.
"""
import os
import shutil
import sys
from pathlib import Path

import pytest

from opencae.solver import launcher as L

PE = Path(__file__).parent / "data" / "pe"


def make_conda_env(root: Path, name: str = "opencae") -> Path:
    """Fake conda env with the Windows layout of conda-forge CalculiX."""
    prefix = root / "envs" / name
    (prefix / "conda-meta").mkdir(parents=True)
    lib_bin = prefix / "Library" / "bin"
    mingw = prefix / "Library" / "mingw-w64" / "bin"
    lib_bin.mkdir(parents=True)
    mingw.mkdir(parents=True)
    shutil.copy(PE / "fakeccx.exe", lib_bin / "ccx.exe")
    shutil.copy(PE / "libmid.dll", lib_bin / "libmid.dll")
    shutil.copy(PE / "libdep-1.dll", mingw / "libdep-1.dll")
    return prefix


def test_exit_code_decoding():
    msg = L.describe_exit_code(-1073741515)
    assert "0xC0000135" in msg and "STATUS_DLL_NOT_FOUND" in msg and "mingw-w64" in msg
    assert "0xC0000139" in L.describe_exit_code(0xC0000139)
    assert L.describe_exit_code(1) is None and L.describe_exit_code(0) is None


def test_pe_import_table():
    assert sorted(x.lower() for x in L.pe_imports(PE / "libmid.dll")) == ["kernel32.dll", "libdep-1.dll", "msvcrt.dll"]
    imps = [x.lower() for x in L.pe_imports(PE / "fakeccx.exe")]
    assert "libmid.dll" in imps
    with pytest.raises(L.PEError):
        L.pe_imports(Path(__file__))


def test_conda_prefix_and_dll_dirs(tmp_path):
    prefix = make_conda_env(tmp_path)
    exe = prefix / "Library" / "bin" / "ccx.exe"
    assert L.conda_prefix_of(exe) == prefix.resolve()
    dirs = L.solver_dll_dirs(exe)
    assert dirs[0] == str(exe.resolve().parent)
    assert str(prefix.resolve() / "Library" / "mingw-w64" / "bin") in dirs


def test_dependency_check_finds_runtime_only_with_solver_dirs(tmp_path):
    """The exact failure mode: with only the exe folder (what a GUI-launched process
    effectively sees), libdep-1.dll from mingw-w64/bin is missing; with the solver
    search folders it resolves."""
    prefix = make_conda_env(tmp_path)
    exe = prefix / "Library" / "bin" / "ccx.exe"
    sysdir = [str(tmp_path / "nosys")]
    narrow = L.check_dependencies(exe, [str(exe.parent)], system_dirs=sysdir)
    assert "libdep-1.dll" in {k.lower() for k in narrow.missing}
    assert narrow.missing[next(k for k in narrow.missing if k.lower() == "libdep-1.dll")] == ["libmid.dll"]
    full = L.check_dependencies(exe, L.solver_dll_dirs(exe), system_dirs=sysdir)
    assert "libdep-1.dll" in full.resolved and "libmid.dll" in full.resolved
    assert not [k for k in full.missing if k.lower().startswith("lib")]


def test_isolated_environment_does_not_touch_gui_env(tmp_path, monkeypatch):
    prefix = make_conda_env(tmp_path)
    inst = L.SolverInstall(str(prefix / "Library" / "bin" / "ccx.exe"), "test", str(prefix),
                           L.solver_dll_dirs(prefix / "Library" / "bin" / "ccx.exe"))
    gui_bin = str(Path(sys.prefix) / "Library" / "bin")
    base = {"PATH": os.pathsep.join([gui_bin, "/usr/bin", str(Path(sys.prefix) / "bin")]),
            "PYTHONPATH": "x", "QT_PLUGIN_PATH": "y", "HOME": "/h"}
    before = dict(os.environ)
    env = L.solver_environment(inst, threads=3, base=base)
    assert dict(os.environ) == before  # GUI process environment unchanged
    parts = env["PATH"].split(os.pathsep)
    assert parts[:len(inst.dll_dirs)] == inst.dll_dirs  # solver folders first
    assert gui_bin not in parts and str(Path(sys.prefix) / "bin") not in parts  # GUI env folders removed
    assert "/usr/bin" in parts
    assert "PYTHONPATH" not in env and "QT_PLUGIN_PATH" not in env
    assert env["OMP_NUM_THREADS"] == "3" and env["CONDA_PREFIX"] == str(prefix)


def test_discovery_scans_conda_envs(tmp_path, monkeypatch):
    if sys.platform.startswith("win"):
        pytest.skip("layout test uses non-Windows executable names")
    root = tmp_path / "miniforge3"
    prefix = root / "envs" / "opencae"
    (prefix / "conda-meta").mkdir(parents=True)
    (prefix / "bin").mkdir()
    fake = prefix / "bin" / "ccx"
    fake.write_text("#!/bin/sh\necho This is Version 9.99\n")
    fake.chmod(0o755)
    monkeypatch.setenv("CONDA_EXE", str(root / "Scripts" / "conda.exe"))
    monkeypatch.delenv("OPENCAE_CCX", raising=False)
    found = [Path(i.exe) for i in L.discover()]
    assert fake.resolve() in [f.resolve() for f in found]
    configured = L.discover(configured=str(fake))
    assert Path(configured[0].exe) == fake and configured[0].source == "configured in settings"


def test_settings_persist(tmp_path, monkeypatch):
    from opencae.utilities import settings as S
    monkeypatch.setenv("OPENCAE_HOME", str(tmp_path))
    S.set_settings(ccx_path="C:/x/ccx.exe", ccx_extra_dll_dirs=["C:/d"])
    assert S.get_setting("ccx_path") == "C:/x/ccx.exe"
    S.set_settings(ccx_path=None)
    assert S.get_setting("ccx_path") is None and S.get_setting("ccx_extra_dll_dirs") == ["C:/d"]


@pytest.mark.solver
def test_selftest_real_solver(tmp_path):
    from opencae.solver.base import CalculixBackend
    be = CalculixBackend()
    rep = be.selftest(keep_dir=tmp_path)
    assert rep.ok, rep.summary()
    assert be.verified is True and rep.version
    assert any("exact" in s.name for s in rep.steps)


def test_selftest_reports_failure_for_broken_solver(tmp_path):
    """A 'solver' that exists but cannot run must not be reported ready."""
    from opencae.solver.calculix.selftest import run_selftest
    bad = tmp_path / ("ccx.exe" if sys.platform.startswith("win") else "ccx")
    if sys.platform.startswith("win"):
        shutil.copy(PE / "libmid.dll", bad)  # a DLL is not executable
    else:
        bad.write_text("#!/bin/sh\nexit 3\n")
        bad.chmod(0o755)
    rep = run_selftest(L.SolverInstall(str(bad), "test", None, [str(tmp_path)]))
    assert not rep.ok
    assert any(not s.ok for s in rep.steps)


@pytest.mark.solver
def test_solver_log_written(cantilever):
    s = cantilever
    s.generate_mesh()
    out, _ = s.run_solver()
    assert out.success
    log = (s.data_dir / s.project.results.directory / "solver.log").read_text()
    assert "return code: 0" in log and "solver PATH" in log and "Job finished" in log


def test_forwarded_exports_are_dependencies(tmp_path):
    """conda-forge's libblas/liblapack on Windows forward every routine to MKL/OpenBLAS
    (export forwarders, not imports). A missing forward target makes ccx fail as soon as
    it calls LAPACK (e.g. in a modal analysis), so the static check must follow them."""
    assert [x.lower() for x in L.pe_imports(PE / "libfwd.dll")] == ["mkl_fake.dll"]
    d = tmp_path / "bin"
    d.mkdir()
    shutil.copy(PE / "libfwd.dll", d / "libfwd.dll")
    rep = L.check_dependencies(d / "libfwd.dll", [d], [])
    assert list(rep.missing) == ["mkl_fake.dll"] and not rep.ok
