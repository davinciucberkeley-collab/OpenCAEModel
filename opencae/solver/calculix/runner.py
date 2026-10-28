"""Managed CalculiX (ccx) executable.

The GUI starts ccx through ``QProcess`` (non-blocking, cancellable); tests and
the CLI use :func:`run_blocking`. Both use the isolated child environment from
:mod:`opencae.solver.launcher` (never the GUI's own PATH). Output is scanned
for error patterns, Windows exit codes are decoded, and every run writes
``solver.log`` next to the input deck.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

INSTALL_HELP = """CalculiX (ccx) was not found, so solving is disabled.

If CalculiX is installed in a conda environment (e.g. 'opencae'), OpenCAE Studio
finds it automatically; otherwise set the path in Tools > Settings, then press
'Test Solver'. Install options:
  * Debian/Ubuntu:  sudo apt install calculix-ccx
  * Fedora:         sudo dnf install calculix-ccx
  * macOS:          brew install calculix-ccx   (or via MacPorts)
  * Windows:        download ccx from http://www.calculix.de or the
                    bConverged / PrePoMax distributions and point
                    OPENCAE_CCX to ccx.exe
  * conda:          conda install -c conda-forge calculix

Alternatively set the environment variable OPENCAE_CCX to the full path."""

ERROR_PATTERNS = [
    (re.compile(r"\*ERROR.*", re.I), "error"),
    (re.compile(r"zero pivot", re.I), "singular"),
    (re.compile(r"singular", re.I), "singular"),
    (re.compile(r"\*WARNING.*", re.I), "warning"),
]


def find_ccx(configured: str | None = None) -> Optional[str]:
    from opencae.solver.launcher import find_install
    inst = find_install(configured)
    return inst.exe if inst else None


def ccx_version(install) -> str:
    """Version reported by ``ccx -v`` run in the isolated solver environment."""
    from opencae.solver.launcher import SolverInstall, popen_kwargs, solver_dll_dirs, solver_environment
    if isinstance(install, str):
        install = SolverInstall(install, "given", None, solver_dll_dirs(Path(install)))
    try:
        r = subprocess.run([install.exe, "-v"], capture_output=True, text=True, timeout=15,
                           env=solver_environment(install), **popen_kwargs())
        m = re.search(r"Version\s+([\d.]+)", (r.stdout or "") + (r.stderr or ""))
        return m.group(1) if m else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def command_line(exe: str, jobname: str) -> list[str]:
    return [exe, "-i", jobname]


@dataclass
class SolveOutcome:
    success: bool
    returncode: int
    wall_time: float
    log: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False


def analyse_output(text: str, returncode: int, frd: Path) -> tuple[bool, list[str], list[str]]:
    from opencae.solver.launcher import describe_exit_code
    errors, warnings = [], []
    why = describe_exit_code(returncode) if returncode else None
    if why:
        errors.append(f"CalculiX did not run: {why}")
    for line in text.splitlines():
        for pat, kind in ERROR_PATTERNS:
            if pat.search(line):
                (warnings if kind == "warning" else errors).append(line.strip())
                break
    finished = "Job finished" in text
    if returncode != 0 and not errors:
        errors.append(f"ccx exited with code {returncode}.")
    if not finished and not errors:
        errors.append("ccx did not report 'Job finished'.")
    if finished and not frd.exists():
        errors.append("Result file (.frd) was not written.")
    return (not errors) and finished, errors, warnings


def write_solver_log(workdir: Path, jobname: str, cmd: list[str], env: dict, returncode: int, text: str,
                     errors: list[str], wall: float) -> Path:
    """Persist everything needed to diagnose a run (command, solver PATH, exit code, output, .sta)."""
    from opencae.solver.launcher import describe_exit_code
    workdir = Path(workdir)
    sta = workdir / f"{jobname}.sta"
    parts = [f"command: {' '.join(cmd)}", f"working directory: {workdir}",
             f"return code: {returncode}" + (f"  ({describe_exit_code(returncode)})" if describe_exit_code(returncode) else ""),
             f"wall time: {wall:.2f} s", "solver PATH (child process only):"]
    parts += [f"  {p}" for p in env.get("PATH", "").split(os.pathsep)[:25]]
    parts += ["errors:"] + [f"  {e}" for e in errors] + ["----- solver output -----", text]
    if sta.exists():
        parts += ["----- .sta -----", sta.read_text(errors="replace")]
    p = workdir / "solver.log"
    p.write_text("\n".join(parts), encoding="utf-8")
    return p


def run_blocking(install, workdir: Path, jobname: str, threads: int = 1,
                 on_line: Callable[[str], None] | None = None,
                 cancel: Callable[[], bool] | None = None, timeout: float = 3600) -> SolveOutcome:
    from opencae.solver.launcher import SolverInstall, popen_kwargs, solver_dll_dirs, solver_environment
    if isinstance(install, (str, Path)):
        install = SolverInstall(str(install), "given", None, solver_dll_dirs(Path(install)))
    t0 = time.time()
    cmd = command_line(install.exe, jobname)
    env = solver_environment(install, threads)
    proc = subprocess.Popen(cmd, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env=env, errors="replace", **popen_kwargs())
    lines = []
    cancelled = False
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line)
        if on_line:
            on_line(line.rstrip())
        if cancel and cancel():
            proc.kill()
            cancelled = True
            break
        if time.time() - t0 > timeout:
            proc.kill()
            lines.append("*ERROR: solver timeout\n")
            break
    rc = proc.wait()
    text = "".join(lines)
    ok, errs, warns = analyse_output(text, rc, Path(workdir) / f"{jobname}.frd")
    if cancelled:
        ok, errs = False, ["Cancelled by user."]
    write_solver_log(Path(workdir), jobname, cmd, env, rc, text, errs, time.time() - t0)
    return SolveOutcome(ok, rc, time.time() - t0, text, errs, warns, cancelled)
