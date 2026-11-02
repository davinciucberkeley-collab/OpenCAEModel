"""Material database.

The built-in values are *representative textbook examples* (room temperature,
typical handbook figures) intended for demonstration only. They are flagged
``is_example=True`` and the UI warns about them; users must verify properties
against certified data for their specific alloy, temper and product form
before using results for engineering decisions.

A user library is stored as JSON in ``~/.opencae/materials.json``.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from opencae.project.model import Material
from opencae.utilities.units import UnitSystem

EXAMPLE_WARNING = (
    "Example values (typical handbook figures). Verify against certified material data "
    "for your alloy, temper and temperature before relying on results."
)


def builtin_materials() -> list[Material]:
    return [
        Material(name="Structural steel (example, S235/ASTM A36-like)", youngs_modulus=200e9,
                 poissons_ratio=0.30, density=7850.0, yield_strength=250e6, thermal_expansion=12e-6,
                 is_example=True, notes=EXAMPLE_WARNING, id="mat_steel"),
        Material(name="Aluminium alloy (example, 6061-T6-like)", youngs_modulus=68.9e9,
                 poissons_ratio=0.33, density=2700.0, yield_strength=276e6, thermal_expansion=23.6e-6,
                 is_example=True, notes=EXAMPLE_WARNING, id="mat_al6061"),
        Material(name="Titanium alloy (example, Ti-6Al-4V-like)", youngs_modulus=113.8e9,
                 poissons_ratio=0.342, density=4430.0, yield_strength=880e6, thermal_expansion=8.6e-6,
                 is_example=True, notes=EXAMPLE_WARNING, id="mat_ti64"),
    ]


def custom_material() -> Material:
    return Material(name="Custom material", youngs_modulus=100e9, poissons_ratio=0.3, density=5000.0,
                    yield_strength=None, thermal_expansion=None, is_example=False,
                    notes="User-defined. Enter verified properties.")


def user_library_path() -> Path:
    base = os.environ.get("OPENCAE_HOME") or str(Path.home() / ".opencae")
    return Path(base) / "materials.json"


def load_user_library() -> list[Material]:
    p = user_library_path()
    if not p.exists():
        return []
    try:
        return [Material(**d) for d in json.loads(p.read_text())]
    except (json.JSONDecodeError, TypeError):
        return []


def save_user_library(materials: list[Material]) -> None:
    p = user_library_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([asdict(m) for m in materials], indent=2))


# --------------------------------------------------------------------------- unit conversion
def material_in_units(mat: Material, us: UnitSystem) -> dict[str, float | None]:
    """Return E, nu, rho, yield, alpha in the given consistent unit system."""
    return {
        "E": mat.youngs_modulus * us.stress_from_si,
        "nu": mat.poissons_ratio,
        "rho": mat.density * us.density_from_si if mat.density is not None else None,
        "yield": mat.yield_strength * us.stress_from_si if mat.yield_strength else None,
        "alpha": mat.thermal_expansion,
    }
