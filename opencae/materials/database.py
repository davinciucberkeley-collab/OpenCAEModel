"""Material database with sources and uncertainty (Material Manager, 0.3).

Polymer data are typical values from manufacturers' technical data sheets (TDS) for
**3D-printed specimens** (100 % infill unless stated), with the scatter the TDS reports. They
describe the manufacturer's test prints, not your printer, settings or filament batch: use them
for screening and replace them with your own measurements for design.

Conventions
-----------
* SI units (Pa, kg/m^3, 1/K, W/(m K), J/(kg K)).
* ``uncertainty[prop]`` is the relative 1-sigma scatter reported by the source (std / mean).
* ``estimated`` lists properties that the source does *not* give; their values are generic
  assumptions and are flagged in the GUI and in reports.
* Print frame for orthotropic data: axes 1 and 2 lie in the layer plane, axis 3 is the build
  (Z) direction. E1 = E2 = flat (XY) specimen modulus, E3 = upright (Z) specimen modulus.
  Shear moduli and Poisson ratios are not reported by these sources and are estimated.
* Strength keys: ``tensile_yield_<o>``, ``tensile_break_<o>`` with o = xy (flat), yz (on its
  side, mostly perimeters), z (upright, interlayer).
"""
from __future__ import annotations

import copy
import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Optional

from opencae.project.model import ORTHO_KEYS, Material

MPa = 1e6

_UM = "UltiMaker {m} Technical Data Sheet ({v}). Printed specimens, UltiMaker S5, 0.15 mm layers, AA 0.4 " \
      "nozzle, 100 % infill, ASTM D3039 tensile tests. {url}"
SOURCES = {
    "pla_um": _UM.format(m="PLA", v="v2.00, 2022-04-20",
                         url="https://www.alloys.com.au/Documents/Ultimaker-PLA-TDS-v5.00.pdf"),
    "abs_um": _UM.format(m="ABS", v="2022", url="https://www.alloys.com.au/Documents/Ultimaker-ABS-TDS-v5.00.pdf"),
    "petg_um": _UM.format(m="PETG", v="v1.00, 2022-04-29",
                          url="https://um-support-files.ultimaker.com/materials/2.85mm/tds/PETG/"
                              "Ultimaker-PETG-TDS-v1.00.pdf"),
    "tpu_um": _UM.format(m="TPU 95A", v="v2.01, 2025-09-15",
                         url="https://um-support-files.ultimaker.com/materials/2.85mm/tds/TPU-95A/"
                             "TDS-TPU95A-V2.01-250915.pdf"),
    "pa_um": "UltiMaker Nylon Technical Data Sheet (v3.011, 2017-05-16). Printed in the XY plane, Ultimaker 2+, "
             "0.4 mm nozzle, 90 % infill, ISO 527. "
             "https://shop3duniverse.com/cdn/shop/t/137/assets/Ultimaker_TDS_Nylon.pdf",
    "asa_pm": "Polymaker PolyLite ASA Technical Data Sheet (V5.4). Printed specimens, 2 shells, 3 top/bottom "
              "layers, 100 % infill, 260 °C, ISO 527. "
              "https://polymaker.com/wp-content/uploads/lana-downloads/PolyLite_ASA_TDS_EN_V5.4.pdf",
    "plaplus_esun": "eSUN 3D Printing Material Datasheet (undated; test standards not stated). "
                    "https://cdn.shopify.com/s/files/1/1745/6181/files/eSUN_3D_Printing_Filament_Datasheet.pdf",
    "pla_thermal": "Thermal conductivity 0.111 W/(m K) at 48 °C and specific heat 1590 J/(kg K) at 55 °C from "
                   "Farah, Anderson & Langer (2016), as cited by Tychanicz-Kwiecien, Smusz & Gil, Thermal Science "
                   "23 (Suppl. 4) S1079 (2019), who measured 0.22 W/(m K) on a solid printed sample. "
                   "https://thermalscience.vinca.rs/pdfs/papers-2019/TSCI19S4079T.pdf",
}

FDM_NOTE = ("Typical values of the manufacturer's printed test specimens; real parts depend on printer, "
            "settings, orientation and moisture. Interlayer (Z) strength is the weakest direction.")


def _ortho(e_xy: float, e_z: float, nu: float) -> dict:
    """Print-frame orthotropic constants from flat and upright moduli; nu and G estimated
    (isotropic relation per plane), which is admissible for any positive moduli."""
    return {"E1": e_xy, "E2": e_xy, "E3": e_z, "nu12": nu, "nu13": nu, "nu23": nu,
            "G12": e_xy / (2 * (1 + nu)), "G13": (2 * e_xy * e_z / (e_xy + e_z)) / (2 * (1 + nu)),
            "G23": (2 * e_xy * e_z / (e_xy + e_z)) / (2 * (1 + nu))}


def _fdm(id_, name, cat, src, rho, e, nu, strengths, unc, ortho=None, extra_est=(), notes="", **kw) -> Material:
    est = ["poissons_ratio"] + list(extra_est)
    if ortho is not None:
        est += ["orthotropic: nu12, nu13, nu23, G12, G13, G23"]
    y = strengths.get("tensile_yield_xy")
    return Material(name=name, youngs_modulus=e, poissons_ratio=nu, density=rho, yield_strength=y,
                    thermal_expansion=None, id=id_, is_example=False, category=cat, source=src,
                    estimated=est, uncertainty=unc, strengths=strengths, orthotropic=ortho,
                    notes=(FDM_NOTE + " " + notes).strip(), **kw)


def database_materials() -> list[Material]:
    """Fresh copies of all database records (sourced polymers + example metals / CFRP)."""
    pla = _fdm("db_pla_um", "PLA, printed (UltiMaker PLA TDS)", "PLA", SOURCES["pla_um"] + " | " +
               SOURCES["pla_thermal"], 1240.0, 3250 * MPa, 0.36,
               {"tensile_yield_xy": 52.5 * MPa, "tensile_yield_yz": 59.0 * MPa, "tensile_break_xy": 45.5 * MPa,
                "tensile_break_yz": 56.0 * MPa, "tensile_break_z": 33.1 * MPa},
               {"youngs_modulus": 119 / 3250, "E3": 181 / 3071, "tensile_yield_xy": 0.9 / 52.5,
                "tensile_break_z": 2.8 / 33.1, "thermal_conductivity": 0.5},
               ortho=_ortho(3250 * MPa, 3071 * MPa, 0.36), thermal_conductivity=0.111, specific_heat=1590.0,
               notes="Glass transition 59 °C, HDT 58.8 °C at 0.455 MPa: stiffness drops sharply above ~50 °C. "
                     "Thermal conductivity of printed parts varies (0.11–0.22 W/(m K)).")
    petg = _fdm("db_petg_um", "PETG, printed (UltiMaker PETG TDS)", "PETG", SOURCES["petg_um"], 1270.0,
                1939 * MPa, 0.38,
                {"tensile_yield_xy": 46.2 * MPa, "tensile_yield_yz": 50.3 * MPa, "tensile_break_xy": 38.5 * MPa,
                 "tensile_break_yz": 44.0 * MPa, "tensile_break_z": 19.0 * MPa},
                {"youngs_modulus": 28 / 1939, "E3": 45 / 1711, "tensile_yield_xy": 0.8 / 46.2,
                 "tensile_break_z": 6.4 / 19.0},
                ortho=_ortho(1939 * MPa, 1711 * MPa, 0.38), notes="Glass transition 77.4 °C, HDT 76.2 °C.")
    abs_ = _fdm("db_abs_um", "ABS, printed (UltiMaker ABS TDS)", "ABS", SOURCES["abs_um"], 1100.0, 1962 * MPa, 0.35,
                {"tensile_yield_xy": 38.1 * MPa, "tensile_yield_yz": 38.2 * MPa, "tensile_break_xy": 33.9 * MPa,
                 "tensile_break_yz": 35.7 * MPa, "tensile_break_z": 19.0 * MPa},
                {"youngs_modulus": 31 / 1962, "E3": 113 / 1699, "tensile_yield_xy": 0.3 / 38.1,
                 "tensile_break_z": 0.6 / 19.0},
                ortho=_ortho(1962 * MPa, 1699 * MPa, 0.35), notes="Glass transition 100.5 °C, HDT 86.6 °C.")
    asa = _fdm("db_asa_pm", "ASA, printed (Polymaker PolyLite ASA TDS)", "ASA", SOURCES["asa_pm"], 1130.0,
               2379 * MPa, 0.35, {"tensile_break_xy": 43.8 * MPa, "tensile_break_z": 32.0 * MPa},
               {"youngs_modulus": 157 / 2379, "E3": 136 / 1965, "tensile_break_xy": 0.8 / 43.8,
                "tensile_break_z": 1.8 / 32.0},
               ortho=_ortho(2379 * MPa, 1965 * MPa, 0.35),
               notes="TDS gives tensile strength without a yield value; the XY tensile strength is used as the "
                     "strength. Glass transition 98 °C, HDT 103 °C at 0.45 MPa.")
    asa.yield_strength = 43.8 * MPa
    asa.estimated.append("yield_strength (= tensile strength)")
    pa = _fdm("db_pa_um", "Nylon (PA), printed (UltiMaker Nylon TDS)", "Nylon", SOURCES["pa_um"], 1140.0,
              579 * MPa, 0.39, {"tensile_yield_xy": 27.8 * MPa, "tensile_break_xy": 34.4 * MPa}, {},
              notes="Only flat (XY) specimens at 90 % infill are reported; no Z data, so no orthotropic model. "
                    "Nylon absorbs moisture, which lowers stiffness.")
    tpu = _fdm("db_tpu95a_um", "TPU 95A, printed (UltiMaker TPU 95A TDS)", "TPU", SOURCES["tpu_um"], 1220.0,
               67 * MPa, 0.45, {"tensile_break_xy": 23.7 * MPa, "tensile_break_yz": 37.9 * MPa,
                                "tensile_break_z": 6.4 * MPa},
               {"youngs_modulus": 6 / 67, "E3": 3 / 56}, ortho=_ortho(67 * MPa, 56 * MPa, 0.45),
               notes="Elastomer (>500 % elongation): a linear-elastic model is valid for small strains only. "
                     "No yield point.")
    plap = _fdm("db_plaplus_esun", "PLA+ (eSUN datasheet)", "PLA+", SOURCES["plaplus_esun"], 1250.0, 2102 * MPa,
                0.36, {"tensile_break_xy": 65 * MPa}, {}, extra_est=["youngs_modulus (= flexural modulus)",
                                                                     "yield_strength (= tensile strength)"],
                notes="The datasheet gives no tensile modulus, print orientation or test standard; the flexural "
                      "modulus is used as Young's modulus. Treat as indicative only.")
    plap.yield_strength = 65 * MPa
    out = [pla, plap, petg, abs_, asa, pa, tpu]
    # example metals / composite (handbook-type values), kept from 0.2 and labelled as examples
    from opencae.materials.library import builtin_materials, drone_materials
    for m in builtin_materials() + drone_materials():
        if m.id in ("mat_pla", "mat_pa12"):
            continue  # replaced by sourced printed-polymer records
        m = copy.deepcopy(m)
        m.category = {"mat_steel": "Steel", "mat_al6061": "Aluminium alloy", "mat_ti64": "Titanium alloy",
                      "mat_cfrp_qi": "CFRP"}.get(m.id, "Other")
        m.source = "Typical handbook value (example, not a certified source)"
        m.estimated = ["all values (example)"]
        out.append(m)
    return out


CATEGORIES = ("PLA", "PLA+", "PETG", "ABS", "ASA", "Nylon", "TPU", "Aluminium alloy", "Steel", "Titanium alloy",
              "CFRP", "Other")


def find(material_id: str) -> Optional[Material]:
    return next((m for m in database_materials() if m.id == material_id), None)


def by_category(category: str) -> list[Material]:
    return [m for m in database_materials() if m.category == category]


def min_strength(m: Material) -> tuple[Optional[float], str]:
    """Lowest reported tensile strength over all print orientations (conservative screening value)."""
    vals = [(v, k) for k, v in (m.strengths or {}).items() if v]
    if not vals:
        return (m.yield_strength, "yield_strength") if m.yield_strength else (None, "")
    v, k = min(vals)
    return v, k


# ------------------------------------------------------------------------- comparison
COMPARE_ROWS = [
    ("Category", lambda m: m.category or "-"),
    ("Density [kg/m³]", lambda m: m.density),
    ("Young's modulus [MPa]", lambda m: m.youngs_modulus / MPa),
    ("Build-direction modulus E3 [MPa]", lambda m: m.orthotropic["E3"] / MPa if m.orthotropic else None),
    ("Poisson's ratio", lambda m: m.poissons_ratio),
    ("Strength used for safety factor [MPa]", lambda m: m.yield_strength / MPa if m.yield_strength else None),
    ("Weakest reported strength [MPa]", lambda m: (min_strength(m)[0] or 0) / MPa or None),
    ("Specific stiffness E/ρ [MJ/kg]", lambda m: m.youngs_modulus / m.density / 1e6 if m.density else None),
    ("Specific strength σ/ρ [kJ/kg]", lambda m: m.yield_strength / m.density / 1e3
     if m.yield_strength and m.density else None),
    ("Thermal conductivity [W/(m K)]", lambda m: m.thermal_conductivity),
    ("Specific heat [J/(kg K)]", lambda m: m.specific_heat),
    ("Thermal expansion [1e-6/K]", lambda m: m.thermal_expansion * 1e6 if m.thermal_expansion else None),
    ("Modulus scatter (1σ) [%]", lambda m: 100 * m.uncertainty["youngs_modulus"]
     if (m.uncertainty or {}).get("youngs_modulus") else None),
    ("Estimated (not from source)", lambda m: ", ".join(m.estimated) or "-"),
    ("Source", lambda m: m.source or ("example values" if m.is_example else "user-defined")),
]


def compare(materials: Iterable[Material]) -> list[list]:
    """Rows [label, value per material] (None = not available)."""
    mats = list(materials)
    rows = [["Material"] + [m.name for m in mats]]
    for label, fn in COMPARE_ROWS:
        row = [label]
        for m in mats:
            try:
                row.append(fn(m))
            except (KeyError, TypeError, ZeroDivisionError):
                row.append(None)
        rows.append(row)
    return rows


# ------------------------------------------------------------------------- import / export
def export_json(materials: Iterable[Material], path: str | Path) -> Path:
    p = Path(path)
    p.write_text(json.dumps([asdict(m) for m in materials], indent=2), encoding="utf-8")
    return p


def import_json(path: str | Path) -> list[Material]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = [data]
    known = set(Material.__dataclass_fields__)
    out = []
    for d in data:
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown material fields: {', '.join(sorted(unknown))}")
        m = Material(**d)
        errs = m.validate()
        if errs:
            raise ValueError("; ".join(errs))
        out.append(m)
    return out


CSV_COLUMNS = {  # column -> (attribute, factor to SI)
    "name": ("name", None), "category": ("category", None),
    "E_MPa": ("youngs_modulus", MPa), "nu": ("poissons_ratio", 1.0), "density_kg_m3": ("density", 1.0),
    "yield_MPa": ("yield_strength", MPa), "alpha_1_K": ("thermal_expansion", 1.0),
    "k_W_mK": ("thermal_conductivity", 1.0), "cp_J_kgK": ("specific_heat", 1.0), "source": ("source", None),
}


def import_csv(path: str | Path) -> list[Material]:
    """One material per row; columns as in ``CSV_COLUMNS`` (unknown columns are an error).
    Optional orthotropic columns E1_MPa..G23_MPa / nu12..nu23 in the print frame."""
    out = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        ortho_cols = {f"{k}_MPa" if k.startswith(("E", "G")) else k: k for k in ORTHO_KEYS}
        unknown = set(rd.fieldnames or []) - set(CSV_COLUMNS) - set(ortho_cols)
        if unknown:
            raise ValueError(f"unknown CSV columns: {', '.join(sorted(unknown))}")
        for n, row in enumerate(rd, start=2):
            kw = {}
            for col, (attr, f) in CSV_COLUMNS.items():
                v = (row.get(col) or "").strip()
                if v:
                    kw[attr] = v if f is None else float(v) * f
            if "name" not in kw or "youngs_modulus" not in kw or "poissons_ratio" not in kw:
                raise ValueError(f"row {n}: name, E_MPa and nu are required")
            kw.setdefault("density", None)
            o = {}
            for col, k in ortho_cols.items():
                v = (row.get(col) or "").strip()
                if v:
                    o[k] = float(v) * (MPa if k.startswith(("E", "G")) else 1.0)
            m = Material(is_example=False, orthotropic=o or None, **kw)
            errs = m.validate()
            if errs:
                raise ValueError(f"row {n}: " + "; ".join(errs))
            out.append(m)
    return out


def export_csv(materials: Iterable[Material], path: str | Path) -> Path:
    p = Path(path)
    with open(p, "w", newline="", encoding="utf-8-sig") as fh:
        cols = list(CSV_COLUMNS) + [f"{k}_MPa" if k.startswith(("E", "G")) else k for k in ORTHO_KEYS]
        w = csv.writer(fh)
        w.writerow(cols)
        for m in materials:
            row = []
            for col, (attr, f) in CSV_COLUMNS.items():
                v = getattr(m, attr)
                row.append("" if v is None else (v if f is None else f"{v / f:.10g}"))
            for k in ORTHO_KEYS:
                v = (m.orthotropic or {}).get(k)
                row.append("" if v is None else f"{v / (MPa if k.startswith(('E', 'G')) else 1.0):.10g}")
            w.writerow(row)
    return p
