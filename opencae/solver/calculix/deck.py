"""CalculiX input deck (.inp) writer for linear static analysis.

Node ids are 1-based (mesh index + 1); element ids are 1-based. The unit
system is the project's consistent system; values are written unconverted.
"""
from __future__ import annotations

import io
from datetime import datetime

import numpy as np

from opencae.boundary_conditions.resolve import ResolvedModel
from opencae.materials.library import material_in_units
from opencae.meshing.mesh_data import FEMesh
from opencae.project.model import Material, Project
from opencae.utilities.units import UnitSystem


ALL_BC_SET = "BC_ALL_SUPPORTS"
MAX_FIELD = 20  # CalculiX reads at most 20 characters per numeric field


def _fmt(v: float) -> str:
    """Full-precision number that fits CalculiX's 20-character field limit.
    (Longer fields are silently truncated by ccx, e.g. '-1.93089988442807e-12'
    would be read as -0.193.)"""
    v = float(v)
    s = f"{v:.15g}"
    if len(s) > MAX_FIELD:
        s = f"{v:.12e}"  # at most 1+1+1+12+1+1+3 = 20 chars
    if len(s) > MAX_FIELD:  # pragma: no cover - defensive
        raise ValueError(f"cannot format {v!r} within {MAX_FIELD} characters")
    return s


def _id_lines(ids, per_line: int = 16) -> str:
    ids = list(ids)
    return "\n".join(", ".join(str(i) for i in ids[k:k + per_line]) for k in range(0, len(ids), per_line))


def _safe(name: str) -> str:
    s = "".join(ch if ch.isalnum() else "_" for ch in name.upper())[:70]
    return s or "SET"


def write_mesh_block(mesh: FEMesh, element_name: str | None = None) -> str:
    """*NODE and *ELEMENT blocks. ``element_name`` selects a CalculiX variant of the
    mesh topology (e.g. C3D8I for C3D8, C3D20R for C3D20)."""
    out = io.StringIO()
    out.write("*NODE, NSET=NALL\n")
    for i, (x, y, z) in enumerate(mesh.nodes, start=1):
        out.write(f"{i}, {_fmt(x)}, {_fmt(y)}, {_fmt(z)}\n")
    out.write(f"*ELEMENT, TYPE={element_name or mesh.element_type}, ELSET=EALL\n")
    el = mesh.elements + 1
    for i, row in enumerate(el, start=1):
        ids = [str(i)] + [str(v) for v in row]
        # CalculiX reads at most 16 entries per line; longer records continue on the next line
        lines = [", ".join(ids[k:k + 16]) for k in range(0, len(ids), 16)]
        out.write(",\n".join(lines) + "\n")
    return out.getvalue()


def material_block(mname: str, material: Material, us: UnitSystem, thermal: bool = False,
                   expansion_zero: float | None = None) -> str:
    mp = material_in_units(material, us)
    out = io.StringIO()
    out.write(f"*MATERIAL, NAME={mname}\n")
    if not thermal or expansion_zero is not None:
        out.write(f"*ELASTIC\n{_fmt(mp['E'])}, {_fmt(mp['nu'])}\n")
    if mp["rho"]:
        out.write(f"*DENSITY\n{_fmt(mp['rho'])}\n")
    if expansion_zero is not None and material.thermal_expansion:
        out.write(f"*EXPANSION, ZERO={_fmt(expansion_zero)}\n{_fmt(material.thermal_expansion)}\n")
    if thermal:
        k = getattr(material, "thermal_conductivity", None)
        c = getattr(material, "specific_heat", None)
        if k:
            out.write(f"*CONDUCTIVITY\n{_fmt(k * us.conductivity_from_si)}\n")
        if c:
            out.write(f"*SPECIFIC HEAT\n{_fmt(c * us.specific_heat_from_si)}\n")
    return out.getvalue()


def section_blocks(mesh: FEMesh, materials, us: UnitSystem, thermal: bool = False,
                   expansion_zero: float | None = None) -> str:
    """Materials and *SOLID SECTION(s). ``materials``: one Material, or {body slot: Material}."""
    if not isinstance(materials, dict):
        materials = {0: materials}
    out = io.StringIO()
    names: dict[str, str] = {}
    for slot, mat in materials.items():
        if mat.id not in names:
            mname = _safe(mat.name)[:60]
            while mname in names.values():
                mname += "_"
            names[mat.id] = mname
            out.write(material_block(mname, mat, us, thermal, expansion_zero))
    if len(materials) == 1:
        out.write(f"*SOLID SECTION, ELSET=EALL, MATERIAL={names[next(iter(materials.values())).id]}\n")
    else:
        for slot, mat in materials.items():
            els = np.nonzero(mesh.element_body == slot)[0] + 1
            out.write(f"*ELSET, ELSET=BODY{slot + 1}\n{_id_lines(els)}\n")
            out.write(f"*SOLID SECTION, ELSET=BODY{slot + 1}, MATERIAL={names[mat.id]}\n")
    return out.getvalue()


def interaction_blocks(mesh: FEMesh, inter: list[dict], material, us: UnitSystem) -> str:
    """Element-face surfaces and *TIE / *CONTACT PAIR definitions (before the step)."""
    from opencae.materials.library import material_in_units
    out = io.StringIO()
    mats = material.values() if isinstance(material, dict) else [material]
    E = max(material_in_units(m, us)["E"] for m in mats if m is not None)
    from opencae.meshing.quality import element_metrics
    h = float(np.mean(element_metrics(mesh.nodes, mesh.elements[: min(2000, len(mesh.elements))],
                                      mesh.element_type)["size"]))
    tol = 0.05 * h  # tie position tolerance: gaps from non-matching faceting
    for k, it in enumerate(inter, start=1):
        for side in ("slave", "master"):
            out.write(f"*SURFACE, NAME=I{k}_{side.upper()}, TYPE=ELEMENT\n")
            out.write("".join(f"{e + 1}, S{f}\n" for e, f in it[side]))
        if it["kind"] == "bonded":
            out.write(f"*TIE, NAME=TIE{k}, POSITION TOLERANCE={_fmt(tol)}, ADJUST=NO\nI{k}_SLAVE, I{k}_MASTER\n")
        else:
            K = float(it["params"].get("stiffness") or 0.0) or 10.0 * E / h
            mu = float(it["params"].get("friction", 0.0))
            out.write(f"*SURFACE INTERACTION, NAME=SI{k}\n*SURFACE BEHAVIOR, PRESSURE-OVERCLOSURE=LINEAR\n"
                      f"{_fmt(K)}, {_fmt(1e-3 * E)}\n")
            if mu > 0:
                out.write(f"*FRICTION\n{_fmt(mu)}, {_fmt(K)}\n")
            out.write(f"*CONTACT PAIR, INTERACTION=SI{k}, TYPE=SURFACE TO SURFACE\nI{k}_SLAVE, I{k}_MASTER\n")
    return out.getvalue()


def write_deck(project: Project, mesh: FEMesh, rm: ResolvedModel, material, us: UnitSystem) -> str:
    """Linear static deck. ``material``: Material, or {body slot: Material} for multi-body meshes."""
    out = io.StringIO()
    out.write("*HEADING\n")
    out.write(f"OpenCAE Studio - {project.name} - {project.analysis.title} - units {us.key} - "
              f"{datetime.now():%Y-%m-%d %H:%M}\n")
    out.write(write_mesh_block(mesh, project.mesh_settings.solver_element(mesh.element_type)))
    # constraint node sets (for reaction totals)
    set_names = {}
    for name, nodes in rm.spc_sets.items():
        sn = "BC_" + _safe(name)
        while sn in set_names.values():
            sn += "_"
        set_names[name] = sn
        out.write(f"*NSET, NSET={sn}\n{_id_lines(nodes + 1)}\n")
    if rm.spc:
        out.write(f"*NSET, NSET={ALL_BC_SET}\n{_id_lines(np.array(sorted(rm.spc)) + 1)}\n")
    T = getattr(rm, "node_temperature", None)
    if T is not None:  # thermal stress: stress-free state at the reference temperature
        Tref = rm.reference_temperature
        out.write(section_blocks(mesh, material, us, expansion_zero=Tref))
        out.write(f"*INITIAL CONDITIONS, TYPE=TEMPERATURE\nNALL, {_fmt(Tref)}\n")
    else:
        out.write(section_blocks(mesh, material, us))
    inter = getattr(rm, "interactions", []) or []
    has_contact = any(i["kind"] == "contact" for i in inter)
    if inter:
        out.write(interaction_blocks(mesh, inter, material, us))
    if has_contact:  # contact is nonlinear: incremental static step
        out.write("*STEP" + (", NLGEOM" if project.analysis.nonlinear_geometry else "") + ", INC=200\n"
                  "*STATIC\n0.1, 1.0, 1e-5, 1.0\n")
    else:
        out.write("*STEP" + (", NLGEOM" if project.analysis.nonlinear_geometry else "") + "\n*STATIC\n")
    if T is not None:
        out.write("*TEMPERATURE\n" + "".join(f"{n + 1}, {_fmt(t)}\n" for n, t in enumerate(T)))
    # boundary conditions
    out.write("*BOUNDARY\n")
    for n in sorted(rm.spc):
        for d, v in sorted(rm.spc[n].items()):
            if v == 0.0:
                out.write(f"{n + 1}, {d}, {d}\n")
            else:
                out.write(f"{n + 1}, {d}, {d}, {_fmt(v)}\n")
    # nodal forces
    f = rm.nodal_forces
    if f is not None and np.any(f != 0):
        out.write("*CLOAD\n")
        nz = np.argwhere(f != 0)
        for n, d in nz:
            out.write(f"{n + 1}, {d + 1}, {_fmt(f[n, d])}\n")
    if rm.pressures:
        out.write("*DLOAD\n")
        for e, face, val in rm.pressures:
            out.write(f"{e + 1}, P{face}, {_fmt(val)}\n")
    # gravity is included in the nodal forces above as exactly integrated consistent loads
    # (rather than *DLOAD GRAV) so the load acting on constrained DOFs is known for the
    # equilibrium check.
    # output requests
    out.write("*NODE FILE\nU, RF\n*EL FILE\nS, E\n")
    if has_contact:
        out.write("*CONTACT FILE\nCSTR, CDIS\n")
    for name, sn in set_names.items():
        out.write(f"*NODE PRINT, NSET={sn}, TOTALS=ONLY\nRF\n")
    if rm.spc:  # union of all supported nodes: exact total reaction (sets may share nodes)
        out.write(f"*NODE PRINT, NSET={ALL_BC_SET}, TOTALS=ONLY\nRF\n")
    if project.analysis.write_ip_stress:
        out.write("*EL PRINT, ELSET=EALL\nS\n")
    out.write("*END STEP\n")
    return out.getvalue()
