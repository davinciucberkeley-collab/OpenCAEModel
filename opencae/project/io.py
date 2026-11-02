"""Project (de)serialisation with a versioned schema.

Layout on disk::

    MyBeam.ocae              <- JSON metadata (this module)
    MyBeam_data/
        geometry/            <- imported CAD files, regenerated B-rep cache
        mesh/mesh.npz        <- FE mesh (nodes, elements, face mappings, quality)
        results/<job>.*      <- CalculiX deck, .frd, .dat, parsed results .npz
        report/              <- exported reports

All paths stored in the JSON are relative to the data directory so a project
folder can be moved or zipped.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any, Callable, get_args, get_origin, get_type_hints

from opencae.project import model as m
from opencae.utilities.logging_setup import get_logger

log = get_logger("project.io")

SCHEMA_VERSION = 2
FILE_FORMAT = "opencae-studio-project"
PROJECT_SUFFIX = ".ocae"

STUDY_KEYS = ("assignments", "mesh_settings", "mesh", "constraints", "loads", "analysis", "results")


def _migrate_1_to_2(doc: dict) -> dict:
    """v1 (0.1): one analysis stored directly on the project. v2: list of studies."""
    proj = dict(doc["project"])
    study = {k: proj.pop(k) for k in STUDY_KEYS if k in proj}
    analysis_type = (study.get("analysis") or {}).get("analysis_type", "linear_static")
    study.update(id="study_1", name={"linear_static": "Static Structural"}.get(analysis_type, analysis_type),
                 body_ids=[], notes="")
    proj["studies"] = [study]
    proj["active_study"] = "study_1"
    return {**doc, "schema_version": 2, "project": proj}


# migrations[v] converts a dict of schema v -> v+1
MIGRATIONS: dict[int, Callable[[dict], dict]] = {1: _migrate_1_to_2}


class ProjectFormatError(Exception):
    pass


def data_dir_for(project_file: Path) -> Path:
    project_file = Path(project_file)
    return project_file.with_name(project_file.stem + "_data")


# --------------------------------------------------------------------------- generic dataclass codec
def _from_value(tp: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(tp)
    if is_dataclass(tp):
        return _from_dict(tp, value)
    if origin in (list, tuple):
        (inner,) = get_args(tp)[:1] or (Any,)
        return [_from_value(inner, v) for v in value]
    if origin is dict:
        kt, vt = get_args(tp)
        return {k: _from_value(vt, v) for k, v in value.items()}
    args = get_args(tp)
    if args and type(None) in args:  # Optional[X]
        inner = next(a for a in args if a is not type(None))
        return _from_value(inner, value)
    return value


def _from_dict(cls: type, data: dict) -> Any:
    hints = get_type_hints(cls, vars(m))
    kwargs = {}
    for f in fields(cls):
        if f.name in data:
            kwargs[f.name] = _from_value(hints[f.name], data[f.name])
    unknown = set(data) - {f.name for f in fields(cls)}
    if unknown:
        log.warning("Ignoring unknown fields for %s: %s", cls.__name__, sorted(unknown))
    return cls(**kwargs)


def project_to_dict(project: m.Project) -> dict:
    return {"format": FILE_FORMAT, "schema_version": SCHEMA_VERSION, "project": asdict(project)}


def project_from_dict(doc: dict) -> m.Project:
    if doc.get("format") != FILE_FORMAT:
        raise ProjectFormatError("Not an OpenCAE Studio project file.")
    version = int(doc.get("schema_version", 0))
    if version > SCHEMA_VERSION:
        raise ProjectFormatError(
            f"Project schema v{version} is newer than this application supports (v{SCHEMA_VERSION}).")
    while version < SCHEMA_VERSION:
        if version not in MIGRATIONS:
            raise ProjectFormatError(f"No migration from schema v{version}.")
        doc = MIGRATIONS[version](doc)
        version += 1
    return _from_dict(m.Project, doc["project"])


# --------------------------------------------------------------------------- file IO
def save_project(project: m.Project, path: Path, previous_path: Path | None = None) -> Path:
    """Write project JSON. When saving to a new location (Save As), the data
    directory of ``previous_path`` is copied so referenced files stay valid."""
    path = Path(path)
    if path.suffix != PROJECT_SUFFIX:
        path = path.with_suffix(PROJECT_SUFFIX)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_data = data_dir_for(path)
    if previous_path is not None:
        old_data = data_dir_for(Path(previous_path))
        if old_data.exists() and old_data.resolve() != new_data.resolve():
            if new_data.exists():
                shutil.rmtree(new_data)
            shutil.copytree(old_data, new_data)
    new_data.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".ocae.tmp")
    tmp.write_text(json.dumps(project_to_dict(project), indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX
    log.info("Saved project to %s", path)
    return path


def load_project(path: Path) -> m.Project:
    path = Path(path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProjectFormatError(f"Corrupt project file: {exc}") from exc
    project = project_from_dict(doc)
    # warn about missing referenced files
    dd = data_dir_for(path)
    for st in project.studies:
        if st.mesh.file and not (dd / st.mesh.file).exists():
            log.warning("Study '%s': mesh file %s missing; mesh must be regenerated.", st.name, st.mesh.file)
            st.mesh = m.MeshInfo()
        if st.results.directory and not (dd / st.results.directory).exists():
            log.warning("Study '%s': results directory missing; results discarded.", st.name)
            st.results = m.ResultsInfo()
    log.info("Loaded project %s", path)
    return project
