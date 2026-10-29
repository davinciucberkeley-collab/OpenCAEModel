"""CalculiX installation self-test ("Test Solver").

A solver is reported ready only if it actually runs. Steps:

1. executable found and readable;
2. (Windows) static DLL-dependency check against the isolated search path;
3. process start-up: ``ccx -v`` in the isolated environment;
4. a real analysis with an exact answer: one 8-node brick (unit cube, E = 1000,
   nu = 0.3) under uniaxial tension with symmetry supports, total force 1. The
   exact solution is u_x = F L / (E A) = 1e-3 and u_y = u_z = -nu * 1e-3 (a
   trilinear element reproduces this linear field exactly). The .frd is parsed
   with the application's own reader, so this also verifies result parsing.
5. the eigenvalue solver (ARPACK + LAPACK/BLAS, which conda-forge builds forward
   to MKL or OpenBLAS DLLs): the same cube with lateral motion suppressed and
   density 1. Its first mode is uniform axial motion of the free face with the
   exact eigenvalue  omega^2 = 3 M / (rho L^2),  M = E (1-nu) / ((1+nu)(1-2 nu)).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.solver.launcher import (IS_WINDOWS, SolverInstall, check_dependencies, describe_exit_code,
                                     popen_kwargs, solver_environment)

UNIT_CUBE_DECK = """*HEADING
OpenCAE Studio solver self-test: unit cube in uniaxial tension (exact u_x = 1e-3)
*NODE, NSET=NALL
1, 0, 0, 0
2, 1, 0, 0
3, 1, 1, 0
4, 0, 1, 0
5, 0, 0, 1
6, 1, 0, 1
7, 1, 1, 1
8, 0, 1, 1
*ELEMENT, TYPE=C3D8, ELSET=EALL
1, 1, 2, 3, 4, 5, 6, 7, 8
*NSET, NSET=X0
1, 4, 5, 8
*NSET, NSET=Y0
1, 2, 5, 6
*NSET, NSET=Z0
1, 2, 3, 4
*MATERIAL, NAME=TESTMAT
*ELASTIC
1000., 0.3
*SOLID SECTION, ELSET=EALL, MATERIAL=TESTMAT
*STEP
*STATIC
*BOUNDARY
X0, 1, 1
Y0, 2, 2
Z0, 3, 3
*CLOAD
2, 1, 0.25
3, 1, 0.25
6, 1, 0.25
7, 1, 0.25
*NODE FILE
U
*EL FILE
S
*END STEP
"""


EIGEN_DECK = """*HEADING
OpenCAE Studio solver self-test: eigenvalue solver (exact omega^2 = 4038.4615)
*NODE, NSET=NALL
1, 0, 0, 0
2, 1, 0, 0
3, 1, 1, 0
4, 0, 1, 0
5, 0, 0, 1
6, 1, 0, 1
7, 1, 1, 1
8, 0, 1, 1
*ELEMENT, TYPE=C3D8, ELSET=EALL
1, 1, 2, 3, 4, 5, 6, 7, 8
*NSET, NSET=X0
1, 4, 5, 8
*MATERIAL, NAME=TESTMAT
*ELASTIC
1000., 0.3
*DENSITY
1.
*SOLID SECTION, ELSET=EALL, MATERIAL=TESTMAT
*STEP
*FREQUENCY
1
*BOUNDARY
X0, 1, 1
NALL, 2, 3
*NODE FILE
U
*END STEP
"""
EIGEN_EXACT = 3 * 1000.0 * 0.7 / (1.3 * 0.4)


def _run_job(exe, work, name, env, timeout):
    r = subprocess.run([str(exe), "-i", name], cwd=work, capture_output=True, text=True, timeout=timeout,
                       env=env, **popen_kwargs())
    log = (r.stdout or "") + (r.stderr or "")
    (work / f"{name}.log").write_text(log)
    ok = r.returncode == 0 and "Job finished" in log and (work / f"{name}.frd").exists()
    if ok:
        return True, ""
    why = describe_exit_code(r.returncode) or f"exit code {r.returncode}"
    errs = [ln.strip() for ln in log.splitlines() if "*ERROR" in ln or "unimplemented" in ln.lower()]
    return False, why + ("\n" + "\n".join(errs[:5]) if errs else "") + f"\nlog: {work / (name + '.log')}"


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0


@dataclass
class SelfTestReport:
    exe: str
    ok: bool = False
    version: str = ""
    steps: list[Step] = field(default_factory=list)
    workdir: str = ""
    dll_dirs: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Solver: {self.exe}", f"Result: {'PASSED' if self.ok else 'FAILED'}"]
        for s in self.steps:
            lines.append(f"  [{'OK' if s.ok else 'FAIL'}] {s.name} ({s.seconds:.2f} s)")
            if s.detail:
                lines += [f"        {ln}" for ln in s.detail.splitlines()]
        if self.dll_dirs:
            lines.append("  Solver DLL search folders (solver process only):")
            lines += [f"        {d}" for d in self.dll_dirs]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return asdict(self)


def run_selftest(install: Optional[SolverInstall], keep_dir: Optional[Path] = None,
                 timeout: float = 120.0) -> SelfTestReport:
    if install is None:
        r = SelfTestReport(exe="(none)")
        r.steps.append(Step("Find CalculiX executable", False,
                            "No ccx executable found. Set its path in Tools > Settings or OPENCAE_CCX."))
        return r
    rep = SelfTestReport(exe=install.exe, dll_dirs=list(install.dll_dirs))
    exe = Path(install.exe)
    t = time.time()
    ok = exe.is_file()
    rep.steps.append(Step("Find CalculiX executable", ok, f"{install.exe}  [{install.source}]"
                          + (f"\nconda environment: {install.conda_prefix}" if install.conda_prefix else ""),
                          time.time() - t))
    if not ok:
        return rep

    if IS_WINDOWS or exe.suffix.lower() == ".exe":
        t = time.time()
        try:
            dep = check_dependencies(exe, install.dll_dirs)
            if dep.ok:
                detail = f"{len(dep.resolved)} DLLs resolved in the solver folders, {len(dep.system)} from Windows."
            else:
                detail = "Missing DLLs (not found in the solver search folders or Windows):\n" + "\n".join(
                    f"{dll}  (needed by {', '.join(sorted(set(by)))})" for dll, by in dep.missing.items())
            rep.steps.append(Step("Check DLL dependencies", dep.ok, detail, time.time() - t))
        except Exception as exc:  # the static check is advisory; the real run below decides
            rep.steps.append(Step("Check DLL dependencies", True, f"skipped ({exc})", time.time() - t))

    env = solver_environment(install, threads=1)
    t = time.time()
    try:
        r = subprocess.run([str(exe), "-v"], capture_output=True, text=True, timeout=30, env=env, **popen_kwargs())
        out = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"Version\s+([\d.]+)", out)
        rep.version = m.group(1) if m else ""
        why = describe_exit_code(r.returncode)
        ok = bool(m) and (r.returncode == 0 or why is None)
        detail = f"CalculiX {rep.version}" if m else (why or f"exit code {r.returncode}; output: {out.strip()[:400]}")
        rep.steps.append(Step("Start solver process (ccx -v)", ok, detail, time.time() - t))
    except (OSError, subprocess.TimeoutExpired) as exc:
        rep.steps.append(Step("Start solver process (ccx -v)", False, f"{type(exc).__name__}: {exc}", time.time() - t))
        return rep
    if not ok:
        return rep

    work = Path(keep_dir) if keep_dir else Path(tempfile.mkdtemp(prefix="opencae_ccx_test_"))
    work.mkdir(parents=True, exist_ok=True)
    rep.workdir = str(work)
    (work / "selftest.inp").write_text(UNIT_CUBE_DECK)
    t = time.time()
    try:
        r = subprocess.run([str(exe), "-i", "selftest"], cwd=work, capture_output=True, text=True, timeout=timeout,
                           env=env, **popen_kwargs())
        log = (r.stdout or "") + (r.stderr or "")
        (work / "selftest.log").write_text(log)
        frd = work / "selftest.frd"
        if r.returncode != 0 or "Job finished" not in log or not frd.exists():
            why = describe_exit_code(r.returncode) or f"exit code {r.returncode}"
            errs = [ln.strip() for ln in log.splitlines() if "*ERROR" in ln]
            rep.steps.append(Step("Run test analysis", False, why + ("\n" + "\n".join(errs[:5]) if errs else "")
                                  + f"\nlog: {work / 'selftest.log'}", time.time() - t))
            return rep
        rep.steps.append(Step("Run test analysis", True, f"Job finished, output in {work}", time.time() - t))
    except (OSError, subprocess.TimeoutExpired) as exc:
        rep.steps.append(Step("Run test analysis", False, f"{type(exc).__name__}: {exc}", time.time() - t))
        return rep

    t = time.time()
    try:
        from opencae.solver.calculix.frd import read_frd
        f = read_frd(work / "selftest.frd")
        disp = f.get("DISP")
        u = np.zeros((8, 3))
        u[disp.node_ids - 1] = disp.values[:, :3]
        ux = u[[1, 2, 5, 6], 0]
        uy = u[[2, 3, 6, 7], 1]
        err_x = float(np.max(np.abs(ux - 1e-3)) / 1e-3)
        err_y = float(np.max(np.abs(uy + 0.3e-3)) / 0.3e-3)
        ok = err_x < 1e-4 and err_y < 1e-4
        rep.steps.append(Step("Verify results against exact solution", ok,
                              f"u_x = {ux.mean():.6e} (exact 1.000000e-03, rel. error {err_x:.1e}); "
                              f"u_y = {uy.mean():.6e} (exact -3.000000e-04, rel. error {err_y:.1e})",
                              time.time() - t))
    except Exception as exc:
        rep.steps.append(Step("Verify results against exact solution", False, f"{type(exc).__name__}: {exc}",
                              time.time() - t))
        return rep

    t = time.time()
    (work / "eigen.inp").write_text(EIGEN_DECK)
    try:
        ok, detail = _run_job(exe, work, "eigen", env, timeout)
        if ok:
            from opencae.physics.modal import _table
            eig = _table((work / "eigen.dat").read_text(errors="replace"), "E I G E N V A L U E   O U T P U T", 4)
            lam = eig[0][0] if eig else float("nan")
            err = abs(lam / EIGEN_EXACT - 1)
            ok = err < 1e-5
            detail = f"omega^2 = {lam:.7g} (exact {EIGEN_EXACT:.7g}, rel. error {err:.1e})"
        rep.steps.append(Step("Eigenvalue solver (modal analyses)", ok, detail, time.time() - t))
    except Exception as exc:
        rep.steps.append(Step("Eigenvalue solver (modal analyses)", False, f"{type(exc).__name__}: {exc}",
                              time.time() - t))
    rep.ok = all(s.ok for s in rep.steps)
    if rep.ok and keep_dir is None:
        shutil.rmtree(work, ignore_errors=True)
        rep.workdir = ""
    return rep
