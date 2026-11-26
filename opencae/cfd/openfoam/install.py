"""Finding and running OpenFOAM (ESI / openfoam.com, v2006 or later).

Supported installations
-----------------------
* Linux (or WSL on Windows): ``/usr/lib/openfoam/openfoam<ver>/etc/bashrc`` (openfoam.com Debian /
  Ubuntu packages), ``/opt/OpenFOAM*/etc/bashrc`` or ``~/OpenFOAM/OpenFOAM-v*/etc/bashrc`` (source
  builds), or a conda environment with the conda-forge ``openfoam`` package.
* Windows: the same, inside a WSL distribution; OpenCAE calls ``wsl.exe`` and converts paths
  (``C:\\x`` -> ``/mnt/c/x``). Nothing is installed or configured automatically.

Ubuntu's own ``openfoam`` package (v1912 on Ubuntu 22.04/24.04) is **not** usable: built with a
newer compiler, every function object (forces, y+, residuals) aborts with
``error in IOstream "sha1"``. ``selftest()`` runs a small case with function objects and reports
this instead of trusting the executable's presence.
"""
from __future__ import annotations

import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Optional

INSTALL_HELP = """OpenFOAM (ESI, openfoam.com) v2006 or later is needed for OpenFOAM flow studies.
Easiest: press Solvers > Install OpenFOAM (Drone menu) or run `opencae setup openfoam`; on Windows this
installs OpenFOAM inside WSL. By hand (Linux, or Ubuntu inside WSL on Windows), with Miniforge installed there:
  conda create -n openfoam -c conda-forge openfoam=2412
Alternatively the openfoam.com precompiled packages:
  https://gitlab.com/openfoam/core/openfoam/-/wikis/precompiled (installs to /usr/lib/openfoam/openfoam<ver>).
Windows: install WSL with Ubuntu first ('wsl --install -d Ubuntu-24.04' in an administrator PowerShell);
  OpenCAE runs OpenFOAM through wsl.exe.
Do NOT use Ubuntu's own 'openfoam' apt package (v1912): its function objects abort ("sha1" error).
Set Tools > Settings > OpenFOAM environment (the conda environment folder or an etc/bashrc file) if it is
not found automatically, then press Test OpenFOAM."""


@dataclass
class FoamInstall:
    kind: str  # bashrc | conda
    path: str  # bashrc file or conda prefix (Linux path, also inside WSL)
    wsl_distro: str = ""  # Windows: WSL distribution name ("" = default)
    version: str = ""

    def prelude(self) -> str:
        """Shell commands that set up the OpenFOAM environment."""
        if self.kind == "conda":
            p = shlex.quote(self.path)
            return (f"export CONDA_PREFIX={p}; export PATH={p}/bin:$PATH; "
                    f"source {p}/etc/conda/activate.d/openfoam_activate.sh >/dev/null 2>&1")
        return f"source {shlex.quote(self.path)} >/dev/null 2>&1"

    def argv(self, workdir: str | Path, script: str) -> list[str]:
        """Command line that runs ``script`` (e.g. './Allrun') in ``workdir`` with OpenFOAM set up."""
        if sys.platform == "win32":
            cmd = f"{self.prelude()}; cd {shlex.quote(to_wsl_path(workdir))} && {script}"
            return wsl_bash(self.wsl_distro, cmd)
        return ["bash", "-c", f"{self.prelude()}; cd {shlex.quote(str(workdir))} && {script}"]

    def describe(self) -> str:
        where = f"WSL '{self.wsl_distro or 'default'}': " if sys.platform == "win32" else ""
        return f"OpenFOAM {self.version or '?'} ({where}{self.kind} {self.path})"


def wsl_bash(distro: str, script: str, login: bool = False) -> list[str]:
    """wsl.exe command line that runs ``script`` with bash inside WSL, exactly as written.

    ``--exec`` is essential: with plain ``wsl.exe ... -- bash -c script`` WSL hands the whole command line to the
    distribution's default shell first, which expands every ``$VAR`` in the script (to the outer shell's values,
    mostly empty) before bash runs it. Seen on the user's PC: ``mkdir: cannot create directory ''``."""
    return ["wsl.exe"] + (["-d", distro] if distro else []) + ["--exec", "bash", "-lc" if login else "-c", script]


def to_wsl_path(p: str | Path) -> str:
    """C:\\Users\\x -> /mnt/c/Users/x (Windows paths); Linux paths are returned unchanged."""
    s = str(p)
    if re.match(r"^[A-Za-z]:[\\/]", s):
        w = PureWindowsPath(s)
        return "/mnt/" + w.drive[0].lower() + "/" + "/".join(w.parts[1:])
    return s.replace("\\", "/")


def _version_from(install: FoamInstall) -> str:
    try:
        out = subprocess.run(install.argv(Path.home() if sys.platform != "win32" else "C:\\",
                                          "echo FOAMVER=$WM_PROJECT_VERSION"),
                             capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"FOAMVER=(\S+)", out)
    return m.group(1) if m else ""


def _linux_candidates() -> list[FoamInstall]:
    out = []
    for pat in ("/usr/lib/openfoam/openfoam*/etc/bashrc", "/opt/openfoam*/etc/bashrc", "/opt/OpenFOAM*/etc/bashrc",
                os.path.expanduser("~/OpenFOAM/OpenFOAM-v*/etc/bashrc")):
        out += [FoamInstall("bashrc", p) for p in sorted(glob.glob(pat), reverse=True)]
    conda_roots = [os.environ.get("CONDA_PREFIX", "")] + \
        sorted(glob.glob(os.path.expanduser("~/miniforge3/envs/*"))) + \
        sorted(glob.glob(os.path.expanduser("~/mambaforge/envs/*"))) + \
        sorted(glob.glob(os.path.expanduser("~/miniconda3/envs/*"))) + \
        sorted(glob.glob(os.path.expanduser("~/anaconda3/envs/*")))
    for r in conda_roots:
        if r and os.path.exists(os.path.join(r, "etc/conda/activate.d/openfoam_activate.sh")):
            out.append(FoamInstall("conda", r))
    if os.path.exists("/usr/share/openfoam/etc/bashrc"):
        out.append(FoamInstall("bashrc", "/usr/share/openfoam/etc/bashrc"))  # Ubuntu v1912: last resort
    return out


def wsl_distros() -> list[str]:
    if sys.platform != "win32" or not shutil.which("wsl.exe"):
        return []
    try:
        raw = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    txt = raw.decode("utf-16-le", errors="ignore") if b"\x00" in raw else raw.decode(errors="ignore")
    return [d.strip() for d in txt.splitlines() if d.strip() and "docker" not in d.lower()]


def _wsl_candidates() -> list[FoamInstall]:
    probe = ("for f in /usr/lib/openfoam/openfoam*/etc/bashrc /opt/openfoam*/etc/bashrc /opt/OpenFOAM*/etc/bashrc "
             "$HOME/OpenFOAM/OpenFOAM-v*/etc/bashrc; do [ -f \"$f\" ] && echo BASHRC=$f; done; "
             "for d in $HOME/miniforge3/envs/* $HOME/mambaforge/envs/* $HOME/miniconda3/envs/*; do "
             "[ -f \"$d/etc/conda/activate.d/openfoam_activate.sh\" ] && echo CONDA=$d; done; true")
    out = []
    for d in wsl_distros():
        try:
            txt = subprocess.run(wsl_bash(d, probe), capture_output=True, text=True,
                                 timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for line in txt.splitlines():
            if line.startswith("BASHRC="):
                out.append(FoamInstall("bashrc", line[7:].strip(), d))
            elif line.startswith("CONDA="):
                out.append(FoamInstall("conda", line[6:].strip(), d))
    return out


def install_from_setting(value: str, distro: str = "") -> Optional[FoamInstall]:
    """A user setting / OPENCAE_OPENFOAM value: path of etc/bashrc, or of a conda environment."""
    v = (value or "").strip()
    if not v:
        return None
    if v.endswith("bashrc"):
        return FoamInstall("bashrc", v, distro)
    return FoamInstall("conda", v.rstrip("/"), distro)


def find_openfoam(setting: str = "", distro: str = "") -> Optional[FoamInstall]:
    """First usable OpenFOAM: user setting, OPENCAE_OPENFOAM, then standard locations."""
    for v in (setting, os.environ.get("OPENCAE_OPENFOAM", "")):
        inst = install_from_setting(v, distro)
        if inst:
            inst.version = _version_from(inst)
            return inst
    cands = _wsl_candidates() if sys.platform == "win32" else _linux_candidates()
    old = None
    for c in cands:
        c.version = _version_from(c)
        if c.version and version_number(c.version) >= 2006:
            return c
        if c.version and old is None:
            old = c  # e.g. Ubuntu's v1912: returned only so that the problem can be reported
    return old


def version_number(v: str) -> int:
    """'v2412' -> 2412; '' -> 0."""
    m = re.search(r"(\d{4})", v or "")
    return int(m.group(1)) if m else 0
