"""Managed CalculiX (ccx) executable.

The GUI starts ccx through ``QProcess`` (non-blocking, cancellable) using
:func:`command_line`; tests and the CLI use :func:`run_blocking`. Output is
scanned for error patterns so failures are reported, never hidden.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

CANDIDATES = ("ccx", "ccx_2.22", "ccx_2.21", "ccx_2.20", "ccx_2.19", "ccx_static", "CalculiX", "ccx.exe")

INSTALL_HELP = """CalculiX (ccx) was not found, so solving is disabled.

Install it, then restart OpenCAE Studio or set the path in Tools > Settings:
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
    for c in (configured, os.environ.get("OPENCAE_CCX")):
        if c and Path(c).exists() and os.access(c, os.X_OK):
            return str(Path(c))
    for c in CANDIDATES:
        p = shutil.which(c)
        if p:
            return p
    return None


def ccx_version(exe: str) -> str:
    try:
        r = subprocess.run([exe, "-v"], capture_output=True, text=True, timeout=10)
        m = re.search(r"Version\s+([\d.]+)", r.stdout + r.stderr)
        return m.group(1) if m else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def command_line(exe: str, jobname: str) -> list[str]:
    return [exe, "-i", jobname]


def solver_env(threads: int) -> dict[str, str]:
    env = dict(os.environ)
    env["OMP_NUM_THREADS"] = str(max(1, threads))
    env["CCX_NPROC_EQUATION_SOLVER"] = str(max(1, threads))
    env["CCX_NPROC_STIFFNESS"] = str(max(1, threads))
    return env


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
    errors, warnings = [], []
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


def run_blocking(exe: str, workdir: Path, jobname: str, threads: int = 1,
                 on_line: Callable[[str], None] | None = None,
                 cancel: Callable[[], bool] | None = None, timeout: float = 3600) -> SolveOutcome:
    t0 = time.time()
    proc = subprocess.Popen(command_line(exe, jobname), cwd=workdir, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env=solver_env(threads))
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
    return SolveOutcome(ok, rc, time.time() - t0, text, errs, warns, cancelled)
