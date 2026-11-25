"""Mass properties of rigid bodies from the OpenCASCADE B-rep (volume properties).

Mass, centre of mass and the inertia tensor about the COM (in assembly axes) of one or more geometry bodies,
converted to kg, project length unit and kg m^2. The density comes from the explicit argument or from the
material assigned to each body; a missing density never gets a default: the function returns ``None`` and
:func:`missing_density` names the bodies / materials that lack it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import numpy as np

from opencae.assembly.model import Assembly, MassProps
from opencae.utilities.units import get_unit_system


class MassPropsError(ValueError):
    """The geometry cannot give mass properties (not a solid, empty shape, ...)."""


def _length_to_m(project) -> float:
    return 1.0 / get_unit_system(project.unit_system).length_from_si


def _study(project, study):
    return study if study is not None else project.study


def _density_for(project, body_id: str, density, study) -> Optional[float]:
    if density is not None:
        return float(density)
    mat = project.material_for_body(body_id, _study(project, study))
    if mat is None or mat.density is None or not mat.density > 0:
        return None
    return float(mat.density)


def missing_density(project, body_ids: list[str], density_kg_m3: Optional[float] = None, study=None) -> list[str]:
    """Names of the bodies without a usable density (no material assigned, or the material has no density)."""
    out = []
    for bid in body_ids:
        if _density_for(project, bid, density_kg_m3, study) is None:
            mat = project.material_for_body(bid, _study(project, study))
            name = project.geometry.body_names.get(bid, bid)
            out.append(f"{name}: material '{mat.name}' has no density" if mat is not None
                       else f"{name}: no material assigned")
    return out


def _regenerate(project, data_dir: Optional[Path]) -> dict[str, Any]:
    from opencae.geometry.occ_kernel import OccKernel
    scale = 1e-3 if project.unit_system.split("-")[0] == "m" else 1.0
    g = project.geometry
    regen = OccKernel().regenerate(g.features, Path(data_dir) if data_dir else Path("."), scale, g.body_names)
    return regen.bodies


def _transform_shape(shape, placement):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf
    T = np.asarray(placement, float).reshape(4, 4)
    t = gp_Trsf()
    t.SetValues(T[0, 0], T[0, 1], T[0, 2], T[0, 3], T[1, 0], T[1, 1], T[1, 2], T[1, 3],
                T[2, 0], T[2, 1], T[2, 2], T[2, 3])
    return BRepBuilderAPI_Transform(shape, t, True).Shape()


def _volume_props(shape) -> tuple[float, np.ndarray, np.ndarray]:
    """(volume, COM, inertia about the COM per unit density), all in the shape's length unit."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    p = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, p)
    c = p.CentreOfMass()
    m = p.MatrixOfInertia()  # about the COM, global axes (verified in the tests against closed forms)
    I = np.array([[m.Value(i + 1, j + 1) for j in range(3)] for i in range(3)])
    return p.Mass(), np.array([c.X(), c.Y(), c.Z()]), I


def compute_massprops(project, body_ids: list[str], density_kg_m3: Optional[float] = None, *, bodies=None,
                      placements: Optional[dict] = None, data_dir=None, study=None) -> Optional[MassProps]:
    """Mass properties of the union of ``body_ids`` (no overlap check: bodies are summed).

    ``bodies``: ``{id: Body}`` from a session (else the geometry is regenerated); ``placements``: optional
    ``{body id: 4x4}`` in project units applied to each shape first; ``density_kg_m3``: one density for all
    bodies, else each body's assigned material. Returns ``None`` when a density is missing."""
    if not body_ids:
        raise MassPropsError("no geometry bodies given")
    if density_kg_m3 is not None and not density_kg_m3 > 0:
        raise MassPropsError("density must be > 0")
    if any(_density_for(project, b, density_kg_m3, study) is None for b in body_ids):
        return None
    if bodies is None:
        bodies = _regenerate(project, data_dir)
    s = _length_to_m(project)
    mass, mcom, parts = 0.0, np.zeros(3), []
    for bid in body_ids:
        body = bodies.get(bid)
        if body is None or body.shape is None or body.kind != "solid":
            raise MassPropsError(f"body '{project.geometry.body_names.get(bid, bid)}' is not an exact solid "
                                 "(mass properties need a B-rep solid)")
        shape = body.shape
        if placements and bid in placements:
            shape = _transform_shape(shape, placements[bid])
        vol, com, I = _volume_props(shape)
        if not vol > 0:
            raise MassPropsError(f"body '{project.geometry.body_names.get(bid, bid)}' has no volume")
        rho = _density_for(project, bid, density_kg_m3, study)
        m_i = rho * vol * s ** 3
        parts.append((m_i, com, rho * I * s ** 5))
        mass += m_i
        mcom += m_i * com
    com_all = mcom / mass  # project length unit
    inertia = np.zeros((3, 3))
    for m_i, com, I in parts:
        d = (com - com_all) * s
        inertia += I + m_i * (np.dot(d, d) * np.eye(3) - np.outer(d, d))
    rhos = {round(_density_for(project, b, density_kg_m3, study), 9) for b in body_ids}
    return MassProps(mass_kg=float(mass), com=com_all.tolist(), inertia_kgm2=inertia.tolist(), source="cad",
                     density_kg_m3=rhos.pop() if len(rhos) == 1 else None,
                     note=f"BRepGProp volume properties of {len(body_ids)} body(ies); "
                          + ("uniform density" if density_kg_m3 is not None else "density of the assigned material"))


def update_body_masses(project, bodies=None, data_dir=None, study=None) -> list[str]:
    """Compute ``RigidBody.mass`` for every body of the assembly from its components' geometry (with the
    component placements). Bodies whose density is missing keep ``mass = None``; the returned list names them."""
    asm: Assembly = project.assembly
    if bodies is None:
        bodies = _regenerate(project, data_dir)
    problems: list[str] = []
    for rb in asm.bodies:
        comps = [asm.component(c) for c in rb.component_ids]
        comps = [c for c in comps if c is not None]
        if not comps:
            problems.append(f"{rb.name}: no components")
            continue
        miss = missing_density(project, [c.body_id for c in comps], None, study)
        if miss:
            problems += [f"{rb.name}: {m}" for m in miss]
            continue
        # one pass per component so that each keeps its own placement and material
        parts = [compute_massprops(project, [c.body_id], None, bodies=bodies,
                                   placements={c.body_id: c.placement_matrix()}, study=study) for c in comps]
        rb.mass = _combine(parts, _length_to_m(project))
    return problems


def _combine(parts: list[MassProps], s: float) -> MassProps:
    if len(parts) == 1:
        return parts[0]
    mass = sum(p.mass_kg for p in parts)
    com = sum(p.mass_kg * np.asarray(p.com, float) for p in parts) / mass
    inertia = np.zeros((3, 3))
    for p in parts:
        d = (np.asarray(p.com, float) - com) * s
        inertia += p.inertia_matrix() + p.mass_kg * (np.dot(d, d) * np.eye(3) - np.outer(d, d))
    return MassProps(mass_kg=float(mass), com=com.tolist(), inertia_kgm2=inertia.tolist(), source="cad",
                     density_kg_m3=None, note=f"sum of {len(parts)} components")
