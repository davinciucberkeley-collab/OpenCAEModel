"""Copy a bundled Assembly / Motion example to the user's project folder (one implementation for the Motion
workspace and the console)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def examples_dir() -> Path:
    return Path(__file__).resolve().parent / "projects"


def projects_home() -> Path:
    return Path(os.environ.get("OPENCAE_HOME") or Path.home() / ".opencae") / "projects"


def copy_example(key: str) -> Path:
    """Copy a bundled example to ``<OPENCAE_HOME>/projects/<key>/`` once (the package folder is never written)."""
    src = examples_dir() / f"{key}.ocae"
    if not src.exists():
        raise FileNotFoundError(f"example not found: {src}")
    dst_dir = projects_home() / key
    dst = dst_dir / f"{key}.ocae"
    if not dst.exists():
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        data = examples_dir() / f"{key}_data"
        if data.exists():
            shutil.copytree(data, dst_dir / f"{key}_data", dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("results", "__pycache__"))
    return dst
