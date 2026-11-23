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


class Cancelled(RuntimeError):
    """The user cancelled while a solver subprocess was running."""


def kill_tree(p) -> None:
    """Terminate a subprocess and its children (process group on POSIX, ``taskkill /T`` on Windows)."""
    import signal
    import subprocess
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        p.kill()
    except OSError:
        pass


def run_cancellable(argv, cancelled=None, timeout: float = 7200, cwd=None, env=None, stdout=None,
                    poll: float = 0.3) -> int:
    """Run ``argv`` and return its exit code. While it runs, ``cancelled()`` is polled; when it turns true the
    whole process tree is terminated within a few seconds and :class:`Cancelled` is raised."""
    import subprocess
    kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000200}  # new process group
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT if stdout else None, **kw)
    t0 = time.time()
    try:
        while True:
            try:
                return p.wait(timeout=poll)
            except subprocess.TimeoutExpired:
                pass
            if cancelled is not None and cancelled():
                kill_tree(p)
                p.wait()
                raise Cancelled()
            if time.time() - t0 > timeout:
                kill_tree(p)
                p.wait()
                raise subprocess.TimeoutExpired(argv, timeout)
    except BaseException:
        if p.poll() is None:
            kill_tree(p)
        raise
