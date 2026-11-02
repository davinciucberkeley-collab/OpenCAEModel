"""Isolated, cross-platform launcher for external solver executables.

Why this exists (Windows)
-------------------------
A conda-forge CalculiX lives in ``<env>\\Library\\bin\\ccx.exe`` but its
Fortran/OpenMP runtime (libgfortran, libgomp, libquadmath, libarpack, ...) is in
``<env>\\Library\\mingw-w64\\bin``. That folder is on PATH only while that env is
*activated*. Started from the GUI's own env (e.g. ``qt_test``), Windows cannot
resolve those DLLs and the process dies before ``main`` with exit code
-1073741515 = 0xC0000135 STATUS_DLL_NOT_FOUND.

Activating the solver env globally is not an option: its Library\\bin carries
its own Qt/ICU/MSVC runtime DLLs that break PySide6 in the GUI process.

The fix: the GUI process environment is never modified. For each solver
*child process only*, a fresh environment is built whose PATH starts with the
solver's own DLL directories (derived from the conda prefix the executable
belongs to, plus user-configured extras), followed by the Windows system
folders, followed by the inherited PATH with the GUI env's own folders
removed. Dependencies can be checked statically by reading the PE import
tables (:func:`check_dependencies`), so a missing DLL is named instead of
reported as a bare exit code.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

IS_WINDOWS = sys.platform.startswith("win")
CREATE_NO_WINDOW = 0x08000000  # no console window flashing up for child processes on Windows

CCX_NAMES = ("ccx", "ccx_2.23", "ccx_2.22", "ccx_2.21", "ccx_2.20", "ccx_2.19", "ccx_static", "CalculiX")

# ---------------------------------------------------------------------------- exit-code decoding
NTSTATUS = {
    0xC0000135: ("STATUS_DLL_NOT_FOUND",
                 "A DLL required by the solver could not be found. The solver's runtime libraries are not on the "
                 "search path of the solver process (typically <env>\\Library\\mingw-w64\\bin for conda-forge "
                 "CalculiX). Run Tools > Settings > Test Solver to see which DLL is missing, and add its folder "
                 "under 'Extra DLL folders' if it is not detected automatically."),
    0xC0000139: ("STATUS_ENTRYPOINT_NOT_FOUND",
                 "A DLL was found but it is the wrong version (a function is missing). Usually an older/newer "
                 "copy of the same DLL from another program or environment is found first on PATH."),
    0xC000007B: ("STATUS_INVALID_IMAGE_FORMAT",
                 "32-bit / 64-bit mismatch between the solver and one of its DLLs, or a corrupt DLL."),
    0xC0000005: ("STATUS_ACCESS_VIOLATION",
                 "The solver crashed (access violation). Check the input deck; for very large models this can "
                 "also be an out-of-memory condition."),
    0xC00000FD: ("STATUS_STACK_OVERFLOW",
                 "The solver ran out of stack. This can happen on Windows with large models; try fewer threads "
                 "or a smaller mesh."),
    0xC0000409: ("STATUS_STACK_BUFFER_OVERRUN", "The solver aborted with a fatal runtime error."),
    0xC000013A: ("STATUS_CONTROL_C_EXIT", "The solver was interrupted."),
    0xC0000017: ("STATUS_NO_MEMORY", "Not enough memory for the solver. Reduce the mesh size."),
}


def describe_exit_code(code: int) -> Optional[str]:
    """Human explanation for Windows NTSTATUS exit codes (also given as negative int32)."""
    u = code & 0xFFFFFFFF
    if u in NTSTATUS:
        name, text = NTSTATUS[u]
        return f"exit code {code} = 0x{u:08X} {name}: {text}"
    if u >= 0xC0000000:
        return f"exit code {code} = 0x{u:08X} (Windows NTSTATUS error)"
    return None


# ---------------------------------------------------------------------------- installs
@dataclass
class SolverInstall:
    exe: str
    source: str  # configured | OPENCAE_CCX | PATH | conda env scan | ...
    conda_prefix: Optional[str] = None
    dll_dirs: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        env = f" (conda env '{Path(self.conda_prefix).name}')" if self.conda_prefix else ""
        return f"{self.exe}{env}"


def conda_prefix_of(exe: Path) -> Optional[Path]:
    """Conda prefix that owns an executable (dir containing ``conda-meta``), if any."""
    p = Path(exe).resolve().parent
    for _ in range(4):
        if (p / "conda-meta").is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def solver_dll_dirs(exe: Path, extra: Iterable[str] = ()) -> list[str]:
    """Folders that must be searched for the solver's shared libraries, solver-first."""
    exe = Path(exe)
    dirs: list[Path] = [exe.resolve().parent]
    prefix = conda_prefix_of(exe)
    if prefix is not None:
        if IS_WINDOWS or (prefix / "Library").is_dir():
            dirs += [prefix / "Library" / "mingw-w64" / "bin", prefix / "Library" / "usr" / "bin",
                     prefix / "Library" / "bin", prefix / "Scripts", prefix / "bin", prefix]
        else:
            dirs += [prefix / "bin", prefix / "lib"]
    dirs += [Path(e) for e in extra if e]
    out, seen = [], set()
    for d in dirs:
        key = os.path.normcase(str(d))
        if key not in seen and d.is_dir():
            seen.add(key)
            out.append(str(d))
    return out


def _conda_roots() -> list[Path]:
    roots = []
    for var in ("CONDA_EXE", "MAMBA_EXE"):
        v = os.environ.get(var)
        if v:
            roots.append(Path(v).resolve().parent.parent)
    sp = Path(sys.prefix)
    roots += [sp, sp.parent.parent] if sp.parent.name == "envs" else [sp]
    home = Path.home()
    for n in ("miniforge3", "mambaforge", "miniconda3", "anaconda3", "Miniforge3", "Miniconda3", "Anaconda3"):
        roots.append(home / n)
    if IS_WINDOWS:
        for base in (os.environ.get("ProgramData"), os.environ.get("LOCALAPPDATA")):
            if base:
                roots += [Path(base) / "miniforge3", Path(base) / "miniconda3", Path(base) / "anaconda3"]
    out, seen = [], set()
    for r in roots:
        k = os.path.normcase(str(r))
        if k not in seen and r.is_dir():
            seen.add(k)
            out.append(r)
    return out


def _exe_names(names: Iterable[str] = CCX_NAMES) -> list[str]:
    return [n + ".exe" for n in names] if IS_WINDOWS else list(names)


def scan_conda_envs(names: Iterable[str] = CCX_NAMES) -> list[Path]:
    """Solver executables (default: ccx) inside conda environments (base env and envs/*)."""
    found = []
    for root in _conda_roots():
        prefixes = [root] + (sorted(p for p in (root / "envs").iterdir() if p.is_dir()) if (root / "envs").is_dir() else [])
        for pref in prefixes:
            for sub in (("Library", "bin"), ("bin",)):
                for name in _exe_names(names):
                    cand = pref.joinpath(*sub, name)
                    if cand.is_file():
                        found.append(cand)
    uniq, seen = [], set()
    for f in found:
        k = os.path.normcase(str(f.resolve()))
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def _known_windows_installs() -> list[Path]:
    if not IS_WINDOWS:
        return []
    cands = []
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), "C:\\"):
        if not base:
            continue
        for pat in ("PrePoMax*/Solver/ccx*.exe", "bConverged/CalculiX/ccx/ccx*.exe", "CalculiX*/ccx*.exe",
                    "CalculiX*/bin/ccx*.exe"):
            cands += list(Path(base).glob(pat))
    return cands


def discover(configured: Optional[str] = None, extra_dll_dirs: Iterable[str] = (), names: Iterable[str] = CCX_NAMES,
             env_var: str = "OPENCAE_CCX", extra_candidates: Iterable[tuple[Path, str]] = ()) -> list[SolverInstall]:
    """All candidate installations of a solver (default CalculiX), in priority order (configured first)."""
    names = tuple(names)
    cands: list[tuple[Path, str]] = []
    if configured:
        cands.append((Path(configured), "configured in settings"))
    if os.environ.get(env_var):
        cands.append((Path(os.environ[env_var]), env_var))
    for n in names:
        w = shutil.which(n)
        if w:
            cands.append((Path(w), "PATH"))
    cands += [(p, "conda environment scan") for p in scan_conda_envs(names)]
    cands += list(extra_candidates)
    if names == CCX_NAMES:
        cands += [(p, "standard install folder") for p in _known_windows_installs()]
    out, seen = [], set()
    for p, src in cands:
        if not p.is_file():
            continue
        k = os.path.normcase(str(p.resolve()))
        if k in seen:
            continue
        seen.add(k)
        pref = conda_prefix_of(p)
        out.append(SolverInstall(str(p), src, str(pref) if pref else None, solver_dll_dirs(p, extra_dll_dirs)))
    return out


def find_install(configured: Optional[str] = None, extra_dll_dirs: Iterable[str] = ()) -> Optional[SolverInstall]:
    c = discover(configured, extra_dll_dirs)
    return c[0] if c else None


# ---------------------------------------------------------------------------- isolated environment
def _gui_env_dirs() -> list[str]:
    """Folders belonging to the GUI's own Python environment (kept out of the solver PATH)."""
    p = Path(sys.prefix)
    return [os.path.normcase(str(x)) for x in (p, p / "Library", p / "Scripts", p / "bin", p / "DLLs")]


def solver_environment(install: SolverInstall, threads: int = 1,
                       base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Environment for the solver child process only. ``os.environ`` is not modified."""
    env = dict(os.environ if base is None else base)
    sep = os.pathsep
    path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
    inherited = [p for p in env.get(path_key, "").split(sep) if p]
    solver_dirs = list(install.dll_dirs)
    solver_keys = {os.path.normcase(d) for d in solver_dirs}
    gui = _gui_env_dirs()
    same_env = install.conda_prefix and os.path.normcase(str(Path(install.conda_prefix))) == os.path.normcase(sys.prefix)

    def is_gui_dir(p: str) -> bool:
        n = os.path.normcase(p.rstrip("\\/"))
        return any(n == g or n.startswith(g + os.sep) for g in gui)

    system = []
    if IS_WINDOWS:
        sr = env.get("SystemRoot") or env.get("SYSTEMROOT") or "C:\\Windows"
        system = [os.path.join(sr, "System32"), sr, os.path.join(sr, "System32", "Wbem")]
    rest = [p for p in inherited if os.path.normcase(p) not in solver_keys and (same_env or not is_gui_dir(p))]
    if path_key != "PATH":
        env.pop(path_key)
    env["PATH"] = sep.join(solver_dirs + [s for s in system if os.path.normcase(s) not in
                                          {os.path.normcase(r) for r in rest}] + rest)
    for k in ("PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH"):
        env.pop(k, None)
    if install.conda_prefix:
        env["CONDA_PREFIX"] = install.conda_prefix
    n = str(max(1, int(threads)))
    env["OMP_NUM_THREADS"] = n
    env.setdefault("OMPI_MCA_osc", "^ucx")  # quiet OpenMPI's UCX probe for serial SU2 runs (conda Linux builds)
    env["CCX_NPROC_EQUATION_SOLVER"] = n
    env["CCX_NPROC_STIFFNESS"] = n
    env["CCX_NPROC_RESULTS"] = n
    return env


def popen_kwargs() -> dict:
    return {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}


# ---------------------------------------------------------------------------- PE import analysis
class PEError(Exception):
    pass


def pe_imports(path: Path) -> list[str]:
    """DLL names a Windows PE file needs: normal and delay-load import tables plus the
    targets of forwarded exports.
    Pure Python, so it also works when inspecting Windows binaries from Linux tests."""
    data = Path(path).read_bytes()
    if data[:2] != b"MZ":
        raise PEError(f"{path} is not a PE file")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise PEError(f"{path}: bad PE signature")
    n_sections = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    dd = opt + (112 if magic == 0x20B else 96)  # data directories (PE32+ vs PE32)
    sect = opt + opt_size
    sections = []
    for i in range(n_sections):
        s = sect + 40 * i
        vsize, va, rsize, rptr = struct.unpack_from("<IIII", data, s + 8)
        sections.append((va, max(vsize, rsize), rptr))

    def off(rva: int) -> int:
        for va, size, rptr in sections:
            if va <= rva < va + size:
                return rva - va + rptr
        raise PEError("RVA outside sections")

    def cstr(o: int) -> str:
        e = data.index(b"\0", o)
        return data[o:e].decode("ascii", "replace")

    names = []
    imp_rva, imp_size = struct.unpack_from("<II", data, dd + 8 * 1)
    if imp_rva:
        o = off(imp_rva)
        while True:
            name_rva = struct.unpack_from("<I", data, o + 12)[0]
            if name_rva == 0:
                break
            names.append(cstr(off(name_rva)))
            o += 20
    dl_rva, _ = struct.unpack_from("<II", data, dd + 8 * 13)
    if dl_rva:
        o = off(dl_rva)
        while True:
            name_rva = struct.unpack_from("<I", data, o + 4)[0]
            if name_rva == 0:
                break
            names.append(cstr(off(name_rva)))
            o += 32
    # forwarded exports ("mkl_rt.3.dll.dgemm_" style): the loader needs the target DLL too.
    # conda-forge's libblas/liblapack on Windows forward every routine to MKL or OpenBLAS.
    exp_rva, exp_size = struct.unpack_from("<II", data, dd)
    if exp_rva and exp_size:
        eo = off(exp_rva)
        n_funcs, _n_names, funcs_rva = struct.unpack_from("<III", data, eo + 20)
        seen = set()
        for i in range(min(n_funcs, 100000)):
            frva = struct.unpack_from("<I", data, off(funcs_rva) + 4 * i)[0]
            if exp_rva <= frva < exp_rva + exp_size:  # RVA inside the export directory = forwarder string
                target = cstr(off(frva))
                mod = target.rsplit(".", 1)[0]
                if not mod.lower().endswith(".dll"):
                    mod += ".dll"
                if mod.lower() not in seen:
                    seen.add(mod.lower())
                    names.append(mod)
    return names


def _is_api_set(name: str) -> bool:
    n = name.lower()
    return n.startswith("api-ms-win-") or n.startswith("ext-ms-")


@dataclass
class DependencyReport:
    resolved: dict[str, str]  # dll (lower) -> path where found
    missing: dict[str, list[str]]  # dll -> list of importers
    system: list[str]

    @property
    def ok(self) -> bool:
        return not self.missing


def check_dependencies(exe: Path, search_dirs: Iterable[str], system_dirs: Optional[Iterable[str]] = None,
                       max_files: int = 400) -> DependencyReport:
    """Resolve the solver's DLL import closure against the given folders (the same
    folders the isolated child PATH uses). Windows system folders are trusted."""
    search = [Path(d) for d in search_dirs]
    if system_dirs is None:
        sr = os.environ.get("SystemRoot", "C:\\Windows")
        system_dirs = [os.path.join(sr, "System32"), sr] if IS_WINDOWS else []
    sysd = [Path(d) for d in system_dirs]
    index: dict[str, Path] = {}
    for d in search:  # first folder wins, like the loader's PATH order
        try:
            for f in d.iterdir():
                index.setdefault(f.name.lower(), f)
        except OSError:
            continue
    resolved, missing, system = {}, {}, []
    queue = [(Path(exe), "<solver>")]
    seen = set()
    while queue and len(seen) < max_files:
        f, importer = queue.pop()
        key = os.path.normcase(str(f))
        if key in seen:
            continue
        seen.add(key)
        try:
            imps = pe_imports(f)
        except (PEError, OSError, struct.error, ValueError):
            continue
        for dll in imps:
            low = dll.lower()
            if _is_api_set(low) or low in resolved or low in system:
                continue
            if low in index:
                resolved[low] = str(index[low])
                queue.append((index[low], dll))
                continue
            sys_hit = next((d / dll for d in sysd if (d / dll).exists()), None)
            if sys_hit is not None or (not sysd and _likely_system(low)):
                system.append(low)
                continue
            missing.setdefault(dll, []).append(f.name)
    return DependencyReport(resolved, missing, system)


_WINDOWS_SYSTEM_DLLS = {"kernel32.dll", "user32.dll", "advapi32.dll", "msvcrt.dll", "ws2_32.dll", "shell32.dll",
                        "ole32.dll", "oleaut32.dll", "gdi32.dll", "ntdll.dll", "comdlg32.dll", "shlwapi.dll",
                        "version.dll", "bcrypt.dll", "crypt32.dll", "secur32.dll", "rpcrt4.dll", "psapi.dll",
                        "dbghelp.dll", "winmm.dll", "comctl32.dll", "iphlpapi.dll", "userenv.dll", "setupapi.dll"}


def _likely_system(low: str) -> bool:
    return low in _WINDOWS_SYSTEM_DLLS
