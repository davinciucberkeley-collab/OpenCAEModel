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


def write_mesh_block(mesh: FEMesh) -> str:
    out = io.StringIO()
    out.write("*NODE, NSET=NALL\n")
    for i, (x, y, z) in enumerate(mesh.nodes, start=1):
        out.write(f"{i}, {_fmt(x)}, {_fmt(y)}, {_fmt(z)}\n")
    out.write(f"*ELEMENT, TYPE={mesh.element_type}, ELSET=EALL\n")
    el = mesh.elements + 1
    for i, row in enumerate(el, start=1):  # <= 11 entries: fits CalculiX's 16-entry line limit
        out.write(f"{i}, " + ", ".join(map(str, row)) + "\n")
    return out.getvalue()


def write_deck(project: Project, mesh: FEMesh, rm: ResolvedModel, material: Material, us: UnitSystem) -> str:
    mp = material_in_units(material, us)
    out = io.StringIO()
    out.write("*HEADING\n")
    out.write(f"OpenCAE Studio - {project.name} - {project.analysis.title} - units {us.key} - "
              f"{datetime.now():%Y-%m-%d %H:%M}\n")
    out.write(write_mesh_block(mesh))
    # constraint node sets (for reaction totals)
    set_names = {}
    for name, nodes in rm.spc_sets.items():
        sn = "BC_" + _safe(name)
        while sn in set_names.values():
            sn += "_"
        set_names[name] = sn
        out.write(f"*NSET, NSET={sn}\n{_id_lines(nodes + 1)}\n")
    mname = _safe(material.name)[:70]
    out.write(f"*MATERIAL, NAME={mname}\n*ELASTIC\n{_fmt(mp['E'])}, {_fmt(mp['nu'])}\n")
    if mp["rho"]:
        out.write(f"*DENSITY\n{_fmt(mp['rho'])}\n")
    out.write(f"*SOLID SECTION, ELSET=EALL, MATERIAL={mname}\n")
    out.write("*STEP\n*STATIC\n")
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
    for name, sn in set_names.items():
        out.write(f"*NODE PRINT, NSET={sn}, TOTALS=ONLY\nRF\n")
    if project.analysis.write_ip_stress:
        out.write("*EL PRINT, ELSET=EALL\nS\n")
    out.write("*END STEP\n")
    return out.getvalue()
