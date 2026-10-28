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


def physics_for(analysis_type: str):
    if not _REGISTRY:
        _load()
    try:
        return _REGISTRY[analysis_type]
    except KeyError as exc:
        raise KeyError(f"No solver workflow is available for analysis type '{analysis_type}'.") from exc


def available_analysis_types() -> list[str]:
    if not _REGISTRY:
        _load()
    return list(_REGISTRY)


def _load() -> None:
    from opencae.physics import structural  # noqa: F401  (registers itself)
    try:
        from opencae.physics import thermal  # noqa: F401
    except ImportError:  # pragma: no cover
        pass
    try:
        from opencae.physics import modal  # noqa: F401
    except ImportError:  # pragma: no cover
        pass
