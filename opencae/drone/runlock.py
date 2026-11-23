"""One heavy solver at a time per machine (OpenFOAM, CalculiX): a file lock in the temp folder.

Small machines (8 GB, two cores) run out of memory when two meshers or solvers overlap, so every heavy run
goes through :func:`solver_lock`. Works on Windows (msvcrt) and Linux (fcntl)."""
from __future__ import annotations

import contextlib
import os
import tempfile
import time
from pathlib import Path

LOCK = Path(tempfile.gettempdir()) / "opencae-solver.lock"


@contextlib.contextmanager
def solver_lock(what: str = "", timeout: float = 6 * 3600, poll: float = 2.0, on_wait=None):
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOCK, "a+")
    t0 = time.time()
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() - t0 > timeout:
                    raise TimeoutError(f"another solver run holds {LOCK} (waited {timeout:.0f} s)")
                if on_wait:
                    on_wait(f"waiting for another solver run to finish before {what}")
                time.sleep(poll)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()
