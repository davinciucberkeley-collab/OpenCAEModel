"""Results Hub: one view of the stored results of every project (drone folders, Studio projects, the K09-L study).

* ``registry``  which projects exist (registered + auto-discovered), stable ids
* ``adapters``  each project kind's stored results as the hub's JSON shapes (projects, project + cards, scene,
  plots, audit, compare)
* ``snapshots`` per-run copies of a drone run's ``results.json`` so past runs stay readable
* ``render``    server-rendered PNG contours (the fallback for a browser without WebGL)

Every number comes from a stored result; a missing piece gives an empty list and a note that says what is missing.
"""
from __future__ import annotations

import os
from pathlib import Path

FOOTER = "Simulation results plan the tests; they are not flight approval."


def home() -> Path:
    """``$OPENCAE_HOME`` or ``~/.opencae`` (the same rule as the rest of the application)."""
    return Path(os.environ.get("OPENCAE_HOME") or (Path.home() / ".opencae"))


class HubError(Exception):
    """A request the hub cannot answer; ``status`` is the HTTP code (404 unknown project or run, 400 bad request)."""

    def __init__(self, message: str, status: int = 404):
        super().__init__(message)
        self.status = status
