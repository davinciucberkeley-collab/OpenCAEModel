"""One-click setup of the open-source airflow solvers (SU2 and OpenFOAM) for people who have neither.

Rules (never relaxed):
* only official open-source sources (SU2 release zips on github.com/su2code, micromamba from github.com/mamba-org,
  OpenFOAM from the conda-forge channel);
* nothing installs unless the user asked for it (a button or a command): importing this module or calling
  :func:`status` downloads and changes nothing;
* everything goes under ``~/.opencae/solvers/`` (``OPENCAE_HOME`` is honoured); no system folders, no
  administrator rights, no PATH or system-variable changes;
* MuJoCo (the multibody engine of Motion studies) is the one exception to "under ~/.opencae/solvers": it is a Python
  package, so it goes into the CURRENT Python environment (``sys.executable -m pip install mujoco``), and only after
  the user's click or ``--yes``;
* on success the path is saved in the OpenCAE settings and the solver's own self-test is run, so "installed" means
  a real run worked;
* every function returns a dict and never raises to the caller: ``{"ok": False, "message": plain words, "log": tail}``.

``progress(msg, frac)`` and ``cancelled() -> bool`` are optional callbacks.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

SU2_VERSION = "8.1.0"
SU2_URL = "https://github.com/su2code/SU2/releases/download/v{ver}/SU2-v{ver}-{plat}.zip"
MICROMAMBA_URL = "https://github.com/mamba-org/micromamba-releases/releases/latest/download/micromamba-linux-64"
SU2_SIZE_TEXT = "about 40 MB"
OPENFOAM_SIZE_TEXT = "about 1-2 GB on disk, after a download of about 250 MB"
SU2_SOURCE = "the official SU2 release on github.com/su2code/SU2"
OPENFOAM_SOURCE = "the conda-forge channel (ESI OpenFOAM, open source), fetched with micromamba from github.com/mamba-org"
WSL_HELP = ("OpenFOAM runs inside WSL (Windows Subsystem for Linux), which is not set up on this PC. Installing WSL "
            "needs administrator rights and a restart, so OpenCAE does not do it: run `wsl --install -d "
            "Ubuntu-24.04` in an administrator PowerShell, restart, then click Set up again.")
MUJOCO_SIZE_TEXT = "about 25 MB, about 50-60 MB on disk afterwards, with its small dependencies"
MUJOCO_SOURCE = "PyPI (the open-source MuJoCo physics engine, Apache-2.0), installed with pip"
MIN_FREE_GB = {"su2": 0.3, "openfoam": 6.0, "mujoco": 0.3}


class _Stop(Exception):
    """The user cancelled."""


def solvers_dir() -> Path:
    base = os.environ.get("OPENCAE_HOME") or str(Path.home() / ".opencae")
    return Path(base) / "solvers"


def _human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} GB"


def _dir_size(p: Path) -> int:
    tot = 0
    for root, _d, files in os.walk(p):
        for f in files:
            try:
                tot += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return tot


def _archives_size(p: Path) -> int:
    tot = 0
    if p.is_dir():
        for f in list(p.rglob("*.conda")) + list(p.rglob("*.tar.bz2")):
            try:
                tot += f.stat().st_size
            except OSError:
                pass
    return tot


def _fail(msg: str, log: str = "", **extra) -> dict:
    return {"ok": False, "message": msg, "log": (log or "")[-3000:], **extra}


def _say(progress, msg: str, frac: float) -> None:
    if progress:
        try:
            progress(msg, max(0.0, min(1.0, frac)))
        except Exception:  # a broken callback must not break the install
            pass


def _check(cancelled) -> None:
    if cancelled and cancelled():
        raise _Stop()


# ------------------------------------------------------------------------------------------------ status
def status() -> dict:
    """What is available now. Never downloads or changes anything."""
    from opencae.utilities.settings import get_setting
    out = {"su2": {"found": False, "path": "", "version": ""},
           "openfoam": {"found": False, "where": "", "version": ""},
           "mujoco": {"found": False, "version": ""},
           "platform": sys.platform, "wsl": {"available": False, "distros": []}, "solvers_dir": str(solvers_dir())}
    try:
        from opencae.mbd import backend as _mbd
        if _mbd.available()[0]:
            out["mujoco"] = {"found": True, "version": _mbd.version()}
    except Exception:
        pass
    try:
        from opencae.cfd.su2 import SU2Backend
        b = SU2Backend()
        if b.install:
            out["su2"] = {"found": True, "path": b.install.exe, "version": b.version()}
    except Exception:
        pass
    try:
        from opencae.cfd.openfoam.install import find_openfoam, wsl_distros
        if sys.platform == "win32":
            d = wsl_distros()
            out["wsl"] = {"available": bool(d), "distros": d}
        inst = find_openfoam(get_setting("openfoam_env") or "", get_setting("openfoam_wsl_distro") or "")
        if inst:
            from opencae.cfd.openfoam.install import version_number
            old = bool(inst.version) and version_number(inst.version) < 2006
            out["openfoam"] = {"found": not old, "where": inst.describe() + (
                " - too old to use (v2006 or newer is needed); set up OpenFOAM below" if old else ""),
                "version": inst.version}
    except Exception:
        pass
    return out


def describe(st: Optional[dict] = None) -> str:
    """The status as a few plain lines."""
    st = st or status()
    s, f = st["su2"], st["openfoam"]
    lines = [f"SU2: {'found ' + s['path'] + (' (v' + s['version'] + ')' if s.get('version') else '') if s['found'] else 'not installed'}",
             f"OpenFOAM: {'found - ' + f['where'] if f['found'] else 'not installed'}"]
    m = st.get("mujoco", {})
    lines.append("MuJoCo (motion): " + (f"found, version {m.get('version') or '?'}" if m.get("found") else
                                       "not installed - Motion studies are unavailable; `opencae setup mujoco` "
                                       "installs it into this Python"))
    if st["platform"] == "win32":
        w = st["wsl"]
        lines.append("WSL: " + (", ".join(w["distros"]) if w["available"] else "not set up (OpenFOAM needs it on Windows)"))
    if not (s["found"] or f["found"]):
        lines.append("No airflow solver yet. `opencae setup su2` (" + SU2_SIZE_TEXT + ") is the quick one; "
                     "`opencae setup openfoam` (" + OPENFOAM_SIZE_TEXT + ") the thorough one.")
    return "\n".join(lines)


def plan(target: str) -> dict:
    """What ``install_<target>`` would download and where it would put it (nothing is downloaded)."""
    if target == "su2":
        return {"target": "su2", "size": SU2_SIZE_TEXT, "source": SU2_SOURCE, "url": su2_url(),
                "into": str(solvers_dir() / f"SU2-v{SU2_VERSION}")}
    if target == "openfoam":
        into = "inside WSL, in ~/.opencae-openfoam" if sys.platform == "win32" else str(solvers_dir() / "openfoam")
        return {"target": "openfoam", "size": OPENFOAM_SIZE_TEXT, "source": OPENFOAM_SOURCE, "url": MICROMAMBA_URL,
                "into": into}
    if target == "mujoco":
        return {"target": "mujoco", "size": MUJOCO_SIZE_TEXT, "source": MUJOCO_SOURCE, "url": "https://pypi.org/project/mujoco/",
                "into": f"the current Python environment ({sys.executable})",
                "message": f"Would run `{' '.join(_mujoco_argv())}` ({MUJOCO_SIZE_TEXT}) from {MUJOCO_SOURCE} into "
                           f"the current Python environment ({sys.executable}). Run again with --yes to do it."}
    raise ValueError("target must be su2, openfoam or mujoco")


def install(target: str, progress: Optional[Callable] = None, cancelled: Optional[Callable] = None) -> dict:
    if target == "su2":
        return install_su2(progress, cancelled)
    if target == "openfoam":
        return install_openfoam(progress, cancelled)
    if target == "mujoco":
        return install_mujoco(progress, cancelled)
    return _fail("Choose su2, openfoam or mujoco.")


# -------------------------------------------------------------------------------------------- downloading
def _download(url: str, dest: Path, progress, cancelled, label: str, lo: float, hi: float) -> int:
    """Stream ``url`` into ``dest`` (written as .part, then renamed). Returns the bytes downloaded."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "OpenCAE-Studio-setup"})
    got = 0
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            last = 0.0
            while True:
                _check(cancelled)
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if time.time() - last > 0.2:
                    last = time.time()
                    frac = lo + (hi - lo) * (got / total if total else 0.5)
                    _say(progress, f"{label}: {_human(got)}" + (f" of {_human(total)}" if total else ""), frac)
        part.replace(dest)
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
    return got


# ------------------------------------------------------------------------------------------------- SU2
def su2_url() -> str:
    plat = "win64" if sys.platform == "win32" else "linux64"
    return SU2_URL.format(ver=SU2_VERSION, plat=plat)


def _safe_extract(zf: zipfile.ZipFile, target: Path, progress, cancelled, lo: float, hi: float) -> None:
    root = target.resolve()
    infos = zf.infolist()
    for i, info in enumerate(infos):
        _check(cancelled)
        dest = (target / info.filename).resolve()
        if root != dest and root not in dest.parents:
            raise ValueError(f"the zip contains an unsafe path ({info.filename})")
        zf.extract(info, target)
        if i % 50 == 0:
            _say(progress, f"Unpacking SU2 ({i + 1}/{len(infos)})", lo + (hi - lo) * i / max(len(infos), 1))


def install_su2(progress: Optional[Callable] = None, cancelled: Optional[Callable] = None) -> dict:
    """Download the official SU2 release into ~/.opencae/solvers/SU2-v<ver>/, save su2_path, run SU2's self-test."""
    target = None
    try:
        if sys.platform not in ("win32", "linux"):
            return _fail("An official SU2 download exists for Windows and Linux only. On other systems install SU2 "
                         "yourself (https://su2code.github.io/download.html) and set its path in Tools > Settings.")
        base = solvers_dir()
        base.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(base).free
        if free < MIN_FREE_GB["su2"] * 1024 ** 3:
            return _fail(f"Not enough free disk space: {_human(free)} free, SU2 needs about 300 MB.")
        target = base / f"SU2-v{SU2_VERSION}"
        exe_name = "SU2_CFD.exe" if sys.platform == "win32" else "SU2_CFD"
        url = su2_url()
        zpath = base / f"SU2-v{SU2_VERSION}.zip"
        _say(progress, f"Downloading SU2 {SU2_VERSION} from {SU2_SOURCE}", 0.0)
        try:
            downloaded = _download(url, zpath, progress, cancelled, "Downloading SU2", 0.0, 0.7)
        except _Stop:
            raise
        except Exception as exc:
            return _fail(f"The SU2 download failed ({type(exc).__name__}: {exc}). Check the internet connection "
                         f"and try again. Source: {url}")
        try:
            if not zipfile.is_zipfile(zpath):
                return _fail("The downloaded SU2 file is not a valid zip archive, so nothing was installed.")
            with zipfile.ZipFile(zpath) as zf:
                names = zf.namelist()
                if not any(n.replace("\\", "/").endswith("bin/" + exe_name) for n in names):
                    return _fail(f"The downloaded zip does not contain bin/{exe_name}, so nothing was installed.")
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                target.mkdir(parents=True)
                _safe_extract(zf, target, progress, cancelled, 0.7, 0.9)
        finally:
            try:
                zpath.unlink()
            except OSError:
                pass
        exes = sorted(target.glob(f"**/bin/{exe_name}"))
        if not exes:
            shutil.rmtree(target, ignore_errors=True)
            return _fail(f"SU2 was unpacked but bin/{exe_name} was not found.")
        for f in exes[0].parent.iterdir():
            try:
                f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError:
                pass
        exe = exes[0]
        _check(cancelled)
        _say(progress, "Checking SU2 with a small test flow", 0.92)
        from opencae.cfd.su2 import SU2Backend
        b = SU2Backend(str(exe))
        rep = b.selftest()
        if not rep.ok:
            return _fail("SU2 was downloaded but its self-test failed, so it was not set up. Details are in the log.",
                         rep.summary(), path=str(exe), downloaded_bytes=downloaded)
        from opencae.utilities.settings import set_settings
        set_settings(su2_path=str(exe))
        disk = _dir_size(target)
        _say(progress, "SU2 is ready", 1.0)
        return {"ok": True, "path": str(exe), "version": rep.version or SU2_VERSION, "downloaded_bytes": downloaded,
                "disk_bytes": disk,
                "message": f"SU2 {rep.version or SU2_VERSION} is installed in {target} and passed its self-test. "
                           f"Downloaded {_human(downloaded)}, uses {_human(disk)} on disk.", "log": rep.summary()}
    except _Stop:
        if target is not None:
            shutil.rmtree(target, ignore_errors=True)
        return _fail("Cancelled. Nothing was installed.")
    except Exception as exc:  # never raise to the UI
        return _fail(f"The SU2 setup stopped: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------------------------- MuJoCo
_VERIFY_CODE = "import mujoco, os; print(mujoco.__version__); print(os.path.dirname(mujoco.__file__))"


def _mujoco_argv() -> list:
    """The one command that installs MuJoCo: into the Python that runs OpenCAE, not into a new environment."""
    return [sys.executable, "-m", "pip", "install", "mujoco"]


def _verify_mujoco() -> tuple:
    """``(ok, version, disk_bytes, error)`` from ``import mujoco`` in a fresh subprocess (the running process may
    have cached a failed import)."""
    try:
        r = subprocess.run([sys.executable, "-c", _VERIFY_CODE], capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return False, "", 0, f"{type(exc).__name__}: {exc}"
    if r.returncode != 0:
        return False, "", 0, (r.stderr or r.stdout or "").strip()[-1500:]
    lines = (r.stdout or "").strip().splitlines()
    ver = lines[0].strip() if lines else ""
    disk = _dir_size(Path(lines[1].strip())) if len(lines) > 1 and Path(lines[1].strip()).is_dir() else 0
    return True, ver, disk, ""


def _pip_download_bytes(out: str) -> int:
    tot = 0.0
    for num, unit in re.findall(r"Downloading \S+ \(([\d.]+) (kB|MB|GB)\)", out):
        tot += float(num) * {"kB": 1e3, "MB": 1e6, "GB": 1e9}[unit]
    return int(tot)


def install_mujoco(progress: Optional[Callable] = None, cancelled: Optional[Callable] = None) -> dict:
    """``python -m pip install mujoco`` into the current Python environment, then ``import mujoco`` in a subprocess."""
    try:
        free = shutil.disk_usage(Path(sys.executable).parent).free
        if free < MIN_FREE_GB["mujoco"] * 1024 ** 3:
            return _fail(f"Not enough free disk space: {_human(free)} free, MuJoCo needs about 100 MB.")
        _say(progress, f"Installing MuJoCo into {sys.executable} ({MUJOCO_SIZE_TEXT})", 0.0)
        rc, out = _stream(_mujoco_argv(), progress, cancelled, "Installing MuJoCo (pip)", 0.0, 0.85, timeout=1800)
        if rc != 0:
            return _fail("pip could not install MuJoCo (exit code " + str(rc) + "), so nothing was installed. The "
                         "log shows why (often: no internet connection, or this Python has no pip).", out)
        _check(cancelled)
        _say(progress, "Checking that MuJoCo can be imported", 0.9)
        ok, ver, disk, err = _verify_mujoco()
        if not ok:
            return _fail("pip finished but `import mujoco` fails in this Python, so Motion studies are still "
                         "unavailable. Details are in the log.", err)
        got = _pip_download_bytes(out)
        _say(progress, "MuJoCo is ready", 1.0)
        return {"ok": True, "version": ver, "downloaded_bytes": got, "disk_bytes": disk, "path": sys.executable,
                "message": f"MuJoCo {ver} is installed in the Python environment {sys.executable} and `import mujoco` "
                           f"works. Downloaded {_human(got) if got else 'size not reported by pip'}, uses "
                           f"{_human(disk) if disk else 'an unknown amount of'} on disk. Restart OpenCAE to use "
                           "Motion studies.", "log": out[-3000:]}
    except _Stop:
        return _fail("Cancelled. If pip had not finished, MuJoCo was not installed.")
    except Exception as exc:
        return _fail(f"The MuJoCo setup stopped: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------------------------- OpenFOAM
def _stream(argv: list, progress, cancelled, label: str, lo: float, hi: float, env=None, timeout=4 * 3600) -> tuple:
    """Run a command, forward its output lines as progress, honour cancel. Returns (returncode, last output)."""
    kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000200}
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                         env=env, **kw)
    lines: list = []
    t0 = time.time()
    import threading

    def pump():
        for ln in p.stdout:
            lines.append(ln.rstrip())
            del lines[:-200]
    th = threading.Thread(target=pump, daemon=True)
    th.start()
    seen = 0
    try:
        while th.is_alive():
            th.join(0.4)
            if cancelled and cancelled():
                _kill(p)
                raise _Stop()
            if time.time() - t0 > timeout:
                _kill(p)
                return 124, "\n".join(lines) + "\n(timed out)"
            if lines and len(lines) != seen:
                seen = len(lines)
                tail = next((x for x in reversed(lines) if x.strip()), "")
                frac = lo + (hi - lo) * min(0.95, (time.time() - t0) / 900.0)
                _say(progress, f"{label}: {tail[:110]}", frac)
        p.wait()
    finally:
        if p.poll() is None:
            _kill(p)
    return p.returncode, "\n".join(lines)


def _kill(p) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        else:
            import signal
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def _foam_selftest(env_path: str, distro: str, progress, cancelled) -> tuple:
    _say(progress, "Checking OpenFOAM with a small test flow", 0.95)
    from opencae.cfd.openfoam.backend import OpenFOAMBackend
    b = OpenFOAMBackend(env_path, distro)
    if not b.install or not b.install.version:
        return False, "OpenFOAM was installed but could not be started (no version reported).", b
    rep = b.selftest()
    return rep.ok, rep.summary(), b


def install_openfoam(progress: Optional[Callable] = None, cancelled: Optional[Callable] = None) -> dict:
    """OpenFOAM (ESI, conda-forge) into ~/.opencae/solvers/openfoam (Linux) or inside WSL (Windows)."""
    try:
        if sys.platform == "win32":
            return _install_openfoam_wsl(progress, cancelled)
        if sys.platform != "linux":
            return _fail("OpenFOAM from conda-forge is set up here on Linux and, through WSL, on Windows. On other "
                         "systems install OpenFOAM yourself and set it in Tools > Settings.")
        return _install_openfoam_linux(progress, cancelled)
    except _Stop:
        return _fail("Cancelled. OpenFOAM was not set up (any partly downloaded files are in "
                     f"{solvers_dir()}; delete that folder to free the space).")
    except Exception as exc:
        return _fail(f"The OpenFOAM setup stopped: {type(exc).__name__}: {exc}")


def _install_openfoam_linux(progress, cancelled) -> dict:
    base = solvers_dir()
    base.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(base).free
    if free < MIN_FREE_GB["openfoam"] * 1024 ** 3:
        return _fail(f"Not enough free disk space: {_human(free)} free; OpenFOAM needs about "
                     f"{MIN_FREE_GB['openfoam']:.0f} GB while it installs ({OPENFOAM_SIZE_TEXT}).")
    mm = base / "micromamba"
    env = base / "openfoam"
    root = base / "mamba-root"
    downloaded = 0
    if not (mm.is_file() and os.access(mm, os.X_OK)):
        _say(progress, "Downloading micromamba (the open-source package installer)", 0.0)
        try:
            downloaded += _download(MICROMAMBA_URL, mm, progress, cancelled, "Downloading micromamba", 0.0, 0.05)
        except _Stop:
            raise
        except Exception as exc:
            return _fail(f"The micromamba download failed ({type(exc).__name__}: {exc}). Check the internet "
                         f"connection and try again. Source: {MICROMAMBA_URL}")
        mm.chmod(mm.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    penv = {k: v for k, v in os.environ.items() if not k.startswith(("CONDA", "MAMBA"))}
    penv["MAMBA_ROOT_PREFIX"] = str(root)
    _say(progress, f"Installing OpenFOAM from conda-forge ({OPENFOAM_SIZE_TEXT}); this takes a while", 0.06)
    rc, out = _stream([str(mm), "create", "-y", "-p", str(env), "-c", "conda-forge", "--override-channels", "openfoam"],
                      progress, cancelled, "Installing OpenFOAM", 0.06, 0.9, env=penv)
    if rc != 0:
        return _fail("The OpenFOAM installation did not finish (micromamba exit code "
                     f"{rc}). The log shows why; nothing was set up.", out)
    downloaded += _archives_size(root / "pkgs")  # the package archives that were fetched
    ok, text, b = _foam_selftest(str(env), "", progress, cancelled)
    if not ok:
        return _fail("OpenFOAM was installed but its self-test failed, so it was not set up. Details are in the log.",
                     text, path=str(env))
    from opencae.utilities.settings import set_settings
    set_settings(openfoam_env=str(env), openfoam_wsl_distro=None)
    disk = _dir_size(base)
    _say(progress, "OpenFOAM is ready", 1.0)
    return {"ok": True, "path": str(env), "version": b.install.version, "downloaded_bytes": downloaded,
            "disk_bytes": disk,
            "message": f"OpenFOAM {b.install.version} is installed in {env} and passed its self-test. "
                       f"Downloaded about {_human(downloaded)}, uses {_human(disk)} on disk "
                       f"(the package cache in {root} can be deleted to free space).", "log": text}


WSL_SCRIPT = r"""set -e
D="$HOME/.opencae-openfoam"
mkdir -p "$D"
if [ ! -x "$D/micromamba" ]; then
  if command -v curl >/dev/null 2>&1; then curl -fL "%(url)s" -o "$D/micromamba"; else wget -O "$D/micromamba" "%(url)s"; fi
  chmod +x "$D/micromamba"
fi
export MAMBA_ROOT_PREFIX="$D/mamba-root"
"$D/micromamba" create -y -p "$D/env" -c conda-forge --override-channels openfoam
echo "OPENCAE_ENV=$D/env"
du -sk "$D" | cut -f1 | sed 's/^/OPENCAE_KB=/'
"""


def _install_openfoam_wsl(progress, cancelled) -> dict:
    from opencae.cfd.openfoam.install import wsl_distros
    distros = wsl_distros()
    if not distros or not shutil.which("wsl.exe"):
        return {"ok": False, "needs": "wsl", "message": WSL_HELP, "log": ""}
    from opencae.utilities.settings import get_setting, set_settings
    pref = get_setting("openfoam_wsl_distro") or ""
    distro = pref if pref in distros else distros[0]
    _say(progress, f"Installing OpenFOAM inside WSL '{distro}' ({OPENFOAM_SIZE_TEXT}); this takes a while", 0.02)
    script = WSL_SCRIPT % {"url": MICROMAMBA_URL}
    rc, out = _stream(["wsl.exe", "-d", distro, "--", "bash", "-lc", script], progress, cancelled,
                      "Installing OpenFOAM in WSL", 0.02, 0.9)
    m = re.search(r"OPENCAE_ENV=(\S+)", out)
    if rc != 0 or not m:
        return _fail(f"The OpenFOAM installation inside WSL '{distro}' did not finish (exit code {rc}). The log shows "
                     "why; nothing was set up.", out)
    env_path = m.group(1)
    kb = re.search(r"OPENCAE_KB=(\d+)", out)
    disk = int(kb.group(1)) * 1024 if kb else 0
    ok, text, b = _foam_selftest(env_path, distro, progress, cancelled)
    if not ok:
        return _fail("OpenFOAM was installed in WSL but its self-test failed, so it was not set up. Details are in "
                     "the log.", text, path=env_path)
    set_settings(openfoam_env=env_path, openfoam_wsl_distro=distro)
    _say(progress, "OpenFOAM is ready", 1.0)
    return {"ok": True, "path": env_path, "version": b.install.version, "distro": distro, "disk_bytes": disk,
            "downloaded_bytes": disk,
            "message": f"OpenFOAM {b.install.version} is installed inside WSL '{distro}' ({env_path}) and passed its "
                       f"self-test. Uses about {_human(disk)} on disk.", "log": text}
