"""Persistent user settings (JSON in ~/.opencae/settings.json).

Used by the GUI, the command line and the Windows launcher alike, so a solver
path configured once in Tools > Settings is found everywhere. Nothing here
touches global or system environment variables.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def settings_path() -> Path:
    base = os.environ.get("OPENCAE_HOME") or str(Path.home() / ".opencae")
    return Path(base) / "settings.json"


def load_settings() -> dict[str, Any]:
    p = settings_path()
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def get_setting(key: str, default: Any = None) -> Any:
    return load_settings().get(key, default)


def set_settings(**values: Any) -> None:
    data = load_settings()
    for k, v in values.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)
