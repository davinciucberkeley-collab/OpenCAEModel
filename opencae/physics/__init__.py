"""Analysis physics plug-ins.

Each analysis type is handled by a physics object that turns the active study
into a solver model and back:

* ``resolve(session, bodies, mesh)`` -> model with ``issues`` (validation);
* ``write_input(session, job_dir, jobname, model)`` -> solver input files;
* ``read_results(session, job_dir, jobname)`` / ``load_results(npz, mesh)``.

New analysis types (or other solver back-ends) register here without changes to
the GUI or the session.
"""
from __future__ import annotations

from typing import Protocol


class Physics(Protocol):
    name: str
    analysis_types: tuple[str, ...]

    def resolve(self, session, bodies, mesh): ...

    def write_input(self, session, job_dir, jobname, model) -> None: ...

    def read_results(self, session, job_dir, jobname): ...

    def load_results(self, path, mesh): ...


_REGISTRY: dict[str, "Physics"] = {}


def register(ph) -> None:
    for t in ph.analysis_types:
        _REGISTRY[t] = ph


_LOADED = False


def physics_for(analysis_type: str):
    _load()
    try:
        return _REGISTRY[analysis_type]
    except KeyError as exc:
        raise KeyError(f"No solver workflow is available for analysis type '{analysis_type}'.") from exc


def available_analysis_types() -> list[str]:
    _load()
    return list(_REGISTRY)


def _load() -> None:
    """Import the built-in physics modules once (each registers itself)."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    import importlib
    import logging
    for mod in ("structural", "thermal", "modal", "cfd"):
        try:
            importlib.import_module(f"opencae.physics.{mod}")
        except Exception:  # pragma: no cover - a broken optional module must not hide the others
            if mod == "structural":
                raise
            logging.getLogger(__name__).exception("physics module '%s' could not be loaded", mod)
