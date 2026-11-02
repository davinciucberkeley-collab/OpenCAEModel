"""Blocking run of any solver backend (command / environment / analyse interface)."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from opencae.solver.calculix.runner import SolveOutcome, write_solver_log
from opencae.solver.launcher import popen_kwargs


def run_backend_blocking(backend, workdir: Path, jobname: str, threads: int = 1,
                         on_line: Optional[Callable[[str], None]] = None,
                         cancel: Optional[Callable[[], bool]] = None, timeout: float = 24 * 3600) -> SolveOutcome:
    t0 = time.time()
    cmd = backend.command(jobname)
    env = backend.environment(threads)
    proc = subprocess.Popen(cmd, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env=env, errors="replace", **popen_kwargs())
    lines, cancelled = [], False
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
            lines.append("ERROR: solver timeout\n")
            break
    rc = proc.wait()
    text = "".join(lines)
    ok, errs, warns = backend.analyse(text, rc, Path(workdir), jobname)
    if cancelled:
        ok, errs = False, ["Cancelled by user."]
    write_solver_log(Path(workdir), jobname, cmd, env, rc, text, errs, time.time() - t0)
    return SolveOutcome(ok, rc, time.time() - t0, text, errs, warns, cancelled)
