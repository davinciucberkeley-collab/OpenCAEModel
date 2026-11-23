"""The analyses of a drone project. ``REGISTRY`` maps an id to its module; every module has ``ID``, ``TITLE``,
``NEEDS`` (dotted drone.json keys it reads, used for the cache hash) and ``run(ctx) -> types.result``.

mass and propulsion are always present. structure, cfd and flight are imported lazily: if a module is missing
or fails to import, the pipeline records ``not_evaluated`` "analysis module not available"
(the import error is kept in ``IMPORT_ERRORS``)."""
from __future__ import annotations

import importlib

from opencae.drone.types import ANALYSES

REGISTRY: dict = {}
IMPORT_ERRORS: dict[str, str] = {}


def refresh() -> dict:
    """(Re)try to import every analysis module that is not registered yet."""
    for aid in ANALYSES:
        if aid in REGISTRY:
            continue
        try:
            REGISTRY[aid] = importlib.import_module(f"{__name__}.{aid}")
            IMPORT_ERRORS.pop(aid, None)
        except ModuleNotFoundError as exc:
            if exc.name == f"{__name__}.{aid}":
                IMPORT_ERRORS.pop(aid, None)
            else:
                IMPORT_ERRORS[aid] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            IMPORT_ERRORS[aid] = f"{type(exc).__name__}: {exc}"
    return REGISTRY


refresh()
