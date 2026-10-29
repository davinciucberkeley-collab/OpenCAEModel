"""SU2 adapter: discovery, isolated launching, configuration, results.

SU2 (https://su2code.github.io, LGPL-2.1) is not bundled. OpenCAE looks for
``SU2_CFD`` (``SU2_CFD.exe`` on Windows) in: the path configured in Tools > Settings,
``OPENCAE_SU2``, the ``SU2_RUN`` folder, PATH and conda environments. Like CalculiX it
runs in a child-process environment built for it; no system variable is changed.

Readiness is decided by a real run (:func:`selftest`), never by the file existing.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.solver.launcher import (IS_WINDOWS, SolverInstall, describe_exit_code, discover, popen_kwargs,
                                     solver_environment)

SU2_NAMES = ("SU2_CFD",)
INSTALL_HELP = """SU2 (open-source CFD, LGPL-2.1) is needed for flow studies; it is not bundled.

Windows: download the Windows release ZIP from https://github.com/su2code/SU2/releases
(e.g. SU2-v8.x.x-win64.zip), unzip it anywhere (e.g. C:\\SU2) and select bin\\SU2_CFD.exe in
Tools > Settings > SU2 path, then press Test SU2. You do NOT need to edit PATH or set SU2_RUN:
OpenCAE starts SU2 in its own process environment. MPI is not required (serial runs).

Linux: conda install -c conda-forge su2   (in a separate environment), then select its
bin/SU2_CFD in Tools > Settings.
"""


def _extra_candidates() -> list[tuple[Path, str]]:
    out = []
    run = os.environ.get("SU2_RUN")
    if run:
        for n in ("SU2_CFD.exe", "SU2_CFD"):
            out.append((Path(run) / n, "SU2_RUN"))
    if IS_WINDOWS:
        for base in ("C:\\SU2", os.environ.get("ProgramFiles", "")):
            if base:
                out += [(p, "standard install folder") for p in Path(base).glob("**/bin/SU2_CFD.exe")][:5]
    return out


def find_su2(configured: Optional[str] = None) -> Optional[SolverInstall]:
    c = discover(configured, (), names=SU2_NAMES, env_var="OPENCAE_SU2", extra_candidates=_extra_candidates())
    return c[0] if c else None


ERROR_RE = re.compile(r"(^|\s)(ERROR|Error)[: ]")
NOISE = ("osc_ucx", "OSC UCX", "pml_ucx", "btl_", "mca_")


def analyse_log(text: str, returncode: int, restart: Path) -> tuple[bool, list[str], list[str], bool]:
    """(success, errors, warnings, converged)."""
    errors, warnings = [], []
    why = describe_exit_code(returncode) if returncode else None
    if why:
        errors.append(f"SU2 did not run: {why}")
    for ln in text.splitlines():
        if any(n in ln for n in NOISE):
            continue
        if ERROR_RE.search(ln) and "Error_" not in ln:
            errors.append(ln.strip())
        elif "WARNING" in ln.upper() and "time convergence" not in ln:
            warnings.append(ln.strip())
    finished = "Exit Success (SU2_CFD)" in text
    if returncode != 0 and not errors:
        errors.append(f"SU2_CFD exited with code {returncode}.")
    if not finished and not errors:
        errors.append("SU2_CFD did not report 'Exit Success'.")
    if finished and not restart.exists():
        errors.append("SU2 did not write the solution file (restart.csv).")
    converged = "All convergence criteria satisfied" in text
    if finished and not converged:
        warnings.insert(0, "The flow solution did NOT reach the convergence criterion within the iteration limit; "
                           "results are not converged (increase iterations or CFL, or refine/repair the mesh).")
    return (not errors) and finished, errors, warnings, converged


class SU2Backend:
    """Same interface as the CalculiX backend: install, command, environment, analyse."""

    name = "SU2"

    def __init__(self, configured_path: Optional[str] = None) -> None:
        from opencae.utilities.settings import get_setting
        self.install = find_su2(configured_path or get_setting("su2_path") or None)
        self.verified: Optional[bool] = None
        self._version: Optional[str] = None
        self.last_report = None

    def executable(self) -> Optional[str]:
        return self.install.exe if self.install else None

    def version(self) -> str:
        if self.install and self._version is None:
            try:
                r = subprocess.run([self.install.exe, "-h"], capture_output=True, text=True, timeout=30,
                                   env=self.environment(1), **popen_kwargs())
                m = re.search(r"(?:SU2|Release)\s*v?(\d+\.\d+\.\d+)", (r.stdout or "") + (r.stderr or ""))
                self._version = m.group(1) if m else "unknown"
            except (OSError, subprocess.TimeoutExpired):
                self._version = "unknown"
        return self._version or "not installed"

    def status_text(self) -> str:
        if not self.install:
            return "SU2 not found"
        if self.verified is True:
            return f"SU2 {self._version or ''} verified".replace("  ", " ")
        if self.verified is False:
            return "SU2 FAILED self-test"
        return "SU2 untested (Tools > Settings > Test SU2)"

    def command(self, jobname: str) -> list[str]:
        if not self.install:
            raise RuntimeError("SU2_CFD not found.")
        return [self.install.exe, f"{jobname}.cfg"]

    def environment(self, threads: int) -> dict[str, str]:
        return solver_environment(self.install, threads)

    def analyse(self, text: str, returncode: int, job_dir: Path, jobname: str):
        ok, errs, warns, conv = analyse_log(text, returncode, Path(job_dir) / "restart.csv")
        return ok, errs, warns

    def selftest(self, keep_dir: Optional[Path] = None) -> "SU2TestReport":
        rep = selftest(self, keep_dir)
        self.verified = rep.ok
        self.last_report = rep
        return rep


# ----------------------------------------------------------------------------- configuration
@dataclass
class FlowBC:
    marker: str
    kind: str  # wall | slip_wall | velocity_inlet | pressure_outlet
    faces: np.ndarray  # boundary faces (node ids) of this marker
    speed: float = 0.0  # m/s (inlet)
    direction: Optional[np.ndarray] = None  # unit vector into the domain (inlet)
    pressure: float = 0.0  # Pa gauge (outlet)
    source: str = ""


def write_config(path: Path, mesh_file: str, a, bcs: list[FlowBC], dim: int = 3) -> Path:
    """SU2 configuration for a steady incompressible flow (dimensional SI)."""
    model = a.flow_model
    walls = [b.marker for b in bcs if b.kind == "wall"]
    syms = [b.marker for b in bcs if b.kind == "slip_wall"]
    inlets = [b for b in bcs if b.kind == "velocity_inlet"]
    outlets = [b for b in bcs if b.kind == "pressure_outlet"]
    v0 = np.zeros(3)
    if inlets:
        b0 = max(inlets, key=lambda b: b.speed)
        v0 = b0.speed * np.asarray(b0.direction)
    L = []
    w = L.append
    w("% OpenCAE Studio - SU2 incompressible flow (SI units)")
    w(f"SOLVER= {'INC_NAVIER_STOKES' if model == 'laminar' else 'INC_RANS'}")
    w(f"KIND_TURB_MODEL= {'NONE' if model == 'laminar' else 'SA' if model == 'sa' else 'SST'}")
    w("MATH_PROBLEM= DIRECT\nRESTART_SOL= NO")
    w("INC_NONDIM= DIMENSIONAL\nINC_DENSITY_MODEL= CONSTANT\nINC_ENERGY_EQUATION= NO")
    w(f"INC_DENSITY_INIT= {a.fluid_density:.10g}")
    w(f"INC_VELOCITY_INIT= ( {v0[0]:.10g}, {v0[1]:.10g}, {v0[2]:.10g} )")
    w(f"VISCOSITY_MODEL= CONSTANT_VISCOSITY\nMU_CONSTANT= {a.fluid_viscosity:.10g}")
    if model != "laminar":
        w(f"FREESTREAM_TURBULENCEINTENSITY= {a.turbulence_intensity:.10g}")
        w(f"FREESTREAM_TURB2LAMVISCRATIO= {a.turbulent_viscosity_ratio:.10g}")
    if walls:
        w("MARKER_HEATFLUX= ( " + ", ".join(f"{m}, 0.0" for m in walls) + " )")
    if syms:
        w("MARKER_SYM= ( " + ", ".join(syms) + " )")
    if inlets:
        w("INC_INLET_TYPE= " + " ".join("VELOCITY_INLET" for _ in inlets))
        w("MARKER_INLET= ( " + ", ".join(
            f"{b.marker}, 0.0, {b.speed:.10g}, {b.direction[0]:.10g}, {b.direction[1]:.10g}, {b.direction[2]:.10g}"
            for b in inlets) + " )")
        if model != "laminar":
            w("MARKER_INLET_TURBULENT= ( " + ", ".join(
                f"{b.marker}, {a.turbulence_intensity:.10g}, {a.turbulent_viscosity_ratio:.10g}" if model == "sst"
                else f"{b.marker}, {a.turbulent_viscosity_ratio:.10g}" for b in inlets) + " )")
    if outlets:
        w("INC_OUTLET_TYPE= " + " ".join("PRESSURE_OUTLET" for _ in outlets))
        w("MARKER_OUTLET= ( " + ", ".join(f"{b.marker}, {b.pressure:.10g}" for b in outlets) + " )")
    mon = walls or [b.marker for b in bcs][:1]
    w("MARKER_MONITORING= ( " + ", ".join(mon) + " )")
    w("MARKER_PLOTTING= ( " + ", ".join(mon) + " )")
    w("REF_AREA= 1.0\nREF_LENGTH= 1.0")
    w("NUM_METHOD_GRAD= GREEN_GAUSS")
    w(f"CFL_NUMBER= {a.cfd_cfl:.10g}\nCFL_ADAPT= NO")
    w(f"ITER= {int(a.cfd_iterations)}")
    w("LINEAR_SOLVER= FGMRES\nLINEAR_SOLVER_PREC= ILU\nLINEAR_SOLVER_ERROR= 1E-6\nLINEAR_SOLVER_ITER= 20")
    w("CONV_NUM_METHOD_FLOW= FDS\nMUSCL_FLOW= YES\nSLOPE_LIMITER_FLOW= NONE\nTIME_DISCRE_FLOW= EULER_IMPLICIT")
    if model != "laminar":
        w("CONV_NUM_METHOD_TURB= SCALAR_UPWIND\nMUSCL_TURB= NO\nTIME_DISCRE_TURB= EULER_IMPLICIT")
    # relative residuals (drop below the initial level): absolute RMS residuals of a dimensional
    # incompressible run depend on the pressure scale and are not a convergence measure
    comps = ["X", "Y"] + (["Z"] if dim == 3 else [])
    w("CONV_FIELD= (REL_RMS_PRESSURE, " + ", ".join(f"REL_RMS_VELOCITY-{c}" for c in comps) + ")")
    w(f"CONV_RESIDUAL_MINVAL= {a.cfd_residual:.10g}\nCONV_STARTITER= 10")
    w(f"MESH_FILENAME= {mesh_file}\nMESH_FORMAT= SU2")
    w("OUTPUT_FILES= (RESTART_ASCII)\nVOLUME_OUTPUT= (COORDINATES, SOLUTION, PRIMITIVE)")
    w("RESTART_FILENAME= restart.csv\nCONV_FILENAME= history")
    w("SCREEN_OUTPUT= (INNER_ITER, RMS_PRESSURE, RMS_VELOCITY-X, RMS_VELOCITY-Y, LINSOL_RESIDUAL)")
    w("HISTORY_OUTPUT= (ITER, RMS_RES, REL_RMS_RES, AERO_COEFF)")
    w("SCREEN_WRT_FREQ_INNER= 10")
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")
    return Path(path)


def read_restart(path: Path) -> dict[str, np.ndarray]:
    """SU2 restart CSV -> columns ordered by PointID."""
    import csv
    with open(path, newline="") as fh:
        rd = csv.reader(fh)
        hdr = [h.strip().strip('"') for h in next(rd)]
        rows = np.array([[float(x) for x in r] for r in rd if r])
    order = np.argsort(rows[:, 0])
    rows = rows[order]
    return {h: rows[:, k] for k, h in enumerate(hdr)}


def read_history(path: Path) -> dict[str, np.ndarray]:
    import csv
    with open(path, newline="") as fh:
        rd = csv.reader(fh)
        hdr = [h.strip().strip('"') for h in next(rd)]
        rows = [[float(x) for x in r] for r in rd if r and r[0].strip()]
    a = np.array(rows) if rows else np.zeros((0, len(hdr)))
    return {h: a[:, k] for k, h in enumerate(hdr)}


# ----------------------------------------------------------------------------- self-test
@dataclass
class SU2TestReport:
    exe: str
    ok: bool = False
    version: str = ""
    steps: list = field(default_factory=list)  # (name, ok, detail)
    workdir: str = ""

    def summary(self) -> str:
        out = [f"Solver: {self.exe}", f"Result: {'PASSED' if self.ok else 'FAILED'}"]
        for n, ok, d in self.steps:
            out.append(f"  [{'OK' if ok else 'FAIL'}] {n}")
            out += [f"        {x}" for x in d.splitlines()]
        return "\n".join(out)


def channel_case(n: int = 8, L: float = 0.2, H: float = 0.01):
    """Structured 2-D channel (quads): nodes (N,2), quads, markers inlet/outlet/wall."""
    nx = int(round(n * L / H / 2))
    xs, ys = np.linspace(0, L, nx + 1), np.linspace(0, H, n + 1)
    X, Y = np.meshgrid(xs, ys, indexing="ij")
    nodes = np.c_[X.ravel(), Y.ravel()]
    idx = np.arange(len(nodes)).reshape(nx + 1, n + 1)
    quads = np.array([[idx[i, j], idx[i + 1, j], idx[i + 1, j + 1], idx[i, j + 1]]
                      for i in range(nx) for j in range(n)])
    edges = lambda a: np.c_[a[:-1], a[1:]]  # noqa: E731
    markers = {"inlet": edges(idx[0, :]), "outlet": edges(idx[-1, :]),
               "wall": np.vstack([edges(idx[:, 0]), edges(idx[:, -1])])}
    return nodes, quads, markers, idx


def selftest(backend: SU2Backend, keep_dir: Optional[Path] = None) -> SU2TestReport:
    """Laminar plane Poiseuille flow (Re = 100) on an 8-cell structured channel; the
    pressure gradient must match -12 mu U_mean / H^2 for the delivered flow rate."""
    from types import SimpleNamespace
    from opencae.cfd.su2_mesh import write_su2_2d
    inst = backend.install
    rep = SU2TestReport(exe=inst.exe if inst else "(none)")
    if inst is None:
        rep.steps.append(("Find SU2_CFD", False, "Not found. " + INSTALL_HELP.splitlines()[0]))
        return rep
    rep.steps.append(("Find SU2_CFD", Path(inst.exe).is_file(), f"{inst.exe} [{inst.source}]"))
    work = Path(keep_dir) if keep_dir else Path(tempfile.mkdtemp(prefix="opencae_su2_test_"))
    work.mkdir(parents=True, exist_ok=True)
    rep.workdir = str(work)
    H, mu, rho, U = 0.01, 1e-3, 1000.0, 0.01
    nodes, quads, markers, idx = channel_case(8)
    write_su2_2d(work / "channel.su2", nodes, [quads], markers)
    a = SimpleNamespace(flow_model="laminar", fluid_density=rho, fluid_viscosity=mu, cfd_cfl=400.0,
                        cfd_iterations=1500, cfd_residual=-6.0, turbulence_intensity=0.05,
                        turbulent_viscosity_ratio=10.0)
    bcs = [FlowBC("inlet", "velocity_inlet", markers["inlet"], U, np.array([1.0, 0, 0])),
           FlowBC("outlet", "pressure_outlet", markers["outlet"], pressure=0.0),
           FlowBC("wall", "wall", markers["wall"])]
    write_config(work / "selftest.cfg", "channel.su2", a, bcs, dim=2)
    t0 = time.time()
    try:
        r = subprocess.run(backend.command("selftest"), cwd=work, capture_output=True, text=True, timeout=300,
                           env=backend.environment(1), **popen_kwargs())
        text = (r.stdout or "") + (r.stderr or "")
        (work / "selftest.log").write_text(text)
        ok, errs, warns, conv = analyse_log(text, r.returncode, work / "restart.csv")
        m = re.search(r"SU2 v(\d+\.\d+\.\d+)", text)
        rep.version = m.group(1) if m else ""
        backend._version = rep.version or backend._version
        rep.steps.append(("Run test case (2-D laminar channel)", ok and conv,
                          (f"converged in {time.time() - t0:.1f} s" if ok and conv else
                           "; ".join(errs[:3] + warns[:1]) or "not converged") + f"\nlog: {work / 'selftest.log'}"))
        if not (ok and conv):
            return rep
        d = read_restart(work / "restart.csv")
        x, y, p, u = d["x"], d["y"], d["Pressure"], d["Velocity_x"]
        out = idx[-1, :]
        Um = np.trapezoid(u[out], y[out]) / H
        m = (x > 0.1) & (x < 0.19)
        dpdx = np.polyfit(x[m], p[m], 1)[0]
        exact = -12 * mu * Um / H ** 2
        err = abs(dpdx / exact - 1)
        rep.steps.append(("Verify against plane Poiseuille flow", err < 0.03,
                          f"dp/dx = {dpdx:.5g} Pa/m, exact {exact:.5g} Pa/m for the delivered mean velocity "
                          f"{Um:.5g} m/s (rel. error {err:.2%}, 8 cells across the channel)"))
    except (OSError, subprocess.TimeoutExpired) as exc:
        rep.steps.append(("Run test case (2-D laminar channel)", False, f"{type(exc).__name__}: {exc}"))
        return rep
    rep.ok = all(s[1] for s in rep.steps)
    return rep
