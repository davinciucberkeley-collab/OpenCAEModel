"""Per-run snapshots of a drone project.

After every pipeline run ``results/<variant>/results.json`` is copied to ``results/history/<run_id>/results.json``
(a few kB to a few hundred kB). Field files stay only for the latest run of each variant, so an older run shows its
numbers and audit but no contours.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

HISTORY = "history"


def snapshot_run(project_dir, run_id: str, variant: str) -> Optional[Path]:
    """Copy the variant's ``results.json`` to ``results/history/<run_id>/``. Returns the new path, or None when
    there is nothing to copy or the copy failed (a snapshot is a convenience: it never fails a run)."""
    try:
        src = Path(project_dir) / "results" / variant / "results.json"
        if not src.is_file():
            return None
        dst = Path(project_dir) / "results" / HISTORY / run_id / "results.json"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return dst
    except OSError:
        return None


def snapshot_path(project_dir, run_id: str) -> Path:
    return Path(project_dir) / "results" / HISTORY / run_id / "results.json"
