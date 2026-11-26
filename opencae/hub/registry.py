"""The list of projects the hub knows: registered ones (``<home>/hub/projects.json``) plus auto-discovered ones.

Discovered: every folder in ``<home>/projects/*`` with a ``drone.json``, the K09-L study ``<home>/k09l_study.json``
and the shipped example Studio projects (``opencae/examples/projects/*.ocae``). A path that no longer exists is left
out of the listing but stays in the file (a project on a removed drive comes back when the drive does).
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

from opencae.hub import home

KINDS = ("drone", "studio", "k09l-study")
EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "projects"
K09L_NAME = "K09-L engineering study"


def _file() -> Path:
    return home() / "hub" / "projects.json"


def project_id(path) -> str:
    """Stable short id of a resolved path."""
    return hashlib.sha1(str(Path(path).resolve()).encode("utf-8")).hexdigest()[:10]


def guess_kind(path) -> Optional[str]:
    p = Path(path)
    if p.is_dir() and (p / "drone.json").is_file():
        return "drone"
    if p.is_file() and p.suffix == ".ocae":
        return "studio"
    if p.is_file() and p.suffix == ".json":
        return "k09l-study"
    return None


def _default_name(kind: str, p: Path) -> str:
    if kind == "studio":
        return p.stem
    if kind == "k09l-study":
        return K09L_NAME
    return p.name


def _entry(path, kind: str, added: str = "") -> dict:
    p = Path(path).resolve()
    return {"id": project_id(p), "kind": kind, "path": str(p), "name": _default_name(kind, p), "added": added}


def _read() -> list:
    try:
        rows = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("path") and r.get("kind") in KINDS] \
        if isinstance(rows, list) else []


def _write(rows: list) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    tmp.replace(f)


def register(path, kind: Optional[str] = None) -> dict:
    """Add a project to ``projects.json`` (idempotent) and return its entry. ``kind`` is guessed when omitted.
    Raises ValueError when the path does not exist or is not a project."""
    p = Path(path)
    if not p.exists():
        raise ValueError(f"No such project: {p}")
    kind = kind or guess_kind(p)
    if kind not in KINDS:
        raise ValueError(f"{p} is not a drone folder (drone.json), a Studio project (.ocae) or a K09-L study (.json)")
    e = _entry(p, kind, time.strftime("%Y-%m-%d %H:%M:%S"))
    rows = _read()
    for r in rows:
        if r.get("id") == e["id"]:
            return {**e, **r, "name": r.get("name") or e["name"]}
    rows.append(e)
    _write(rows)
    return e


def _discovered() -> list:
    out = []
    root = home() / "projects"
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir() and (d / "drone.json").is_file():
                out.append(_entry(d, "drone"))
    k = home() / "k09l_study.json"
    if k.is_file():
        out.append(_entry(k, "k09l-study"))
    if EXAMPLES.is_dir():
        for f in sorted(EXAMPLES.glob("*.ocae")):
            out.append(_entry(f, "studio"))
    return out


def projects() -> list:
    """Registered + discovered projects whose path exists, de-duplicated by id (registered first)."""
    seen, out = set(), []
    for r in _read() + _discovered():
        p = Path(r["path"])
        pid = project_id(p) if p.exists() else r.get("id")
        if pid in seen or not p.exists():
            continue
        seen.add(pid)
        out.append({"id": pid, "kind": r["kind"], "path": str(p), "name": r.get("name") or _default_name(r["kind"], p),
                    "added": r.get("added", "")})
    return out


def find(pid: str) -> Optional[dict]:
    for e in projects():
        if e["id"] == pid:
            return e
    return None
