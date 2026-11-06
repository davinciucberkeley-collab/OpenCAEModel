"""Effective (homogenised) properties of FDM-printed parts.

Model
-----
A printed part is a solid **shell** (perimeters, top/bottom layers) around a cellular **infill
core**. The solid printed material (the database values for printed specimens at 100 % infill)
is used for the shell. The core is a lattice of the same material with relative density
phi = infill / 100, whose stiffness follows scaling laws of cellular solids:

=============  =====================================  ===================  ======================================
pattern        in-plane stiffness E*/E                build-direction      source
=============  =====================================  ===================  ======================================
triangles      phi / 3 (stretch-dominated, isotropic)  phi                  Gibson & Ashby, Cellular Solids (1997)
honeycomb      1.5 phi^3 (bending-dominated)           phi                  Gibson & Ashby (1997), regular hexagons
grid,          phi / 2 along the lines, ~phi^3 at      phi                  square lattice: half the walls carry load
rectilinear,   45 deg; the lower (off-axis) value is                         along each line direction; bending off-
concentric     used (direction not tracked)                                  axis (Gibson & Ashby scaling)
cubic          phi^2                                   phi^2                3-D lattice, bending-dominated bound
gyroid         phi^2                                   phi^2                Gibson & Ashby open-cell law (C = 1)
=============  =====================================  ===================  ======================================

The build-direction value for 2.5-D patterns (walls continuous through the layers) is the
prismatic (Voigt) law. Strength is *not* scaled with infill: the reported strength is the
weakest printed-specimen strength (normally interlayer, Z), the conservative screening value.

Shell and core are combined with the volume fraction of the shell, f = V_shell / V
(computed from the geometry by an inward offset of the wall thickness): E = f E_shell +
(1 - f) E_core (Voigt). For bending of thin-walled parts this underestimates the stiffness
(the shell sits far from the neutral axis); Level 3 models shell and core explicitly.

All results are approximations and are labelled as such in the GUI and in reports.
"""
from __future__ import annotations

import copy
from typing import Optional

from opencae.manufacturing.print_settings import PrintSettings
from opencae.project.model import Material

REFERENCE = "Gibson L.J., Ashby M.F., Cellular Solids: Structure and Properties, 2nd ed., Cambridge UP (1997)"


def core_stiffness_ratios(pattern: str, phi: float) -> tuple[float, float, str]:
    """(E_inplane/E, E_build/E, law) for an infill core of relative density phi."""
    phi = min(max(phi, 0.0), 1.0)
    if phi >= 1.0:
        return 1.0, 1.0, "solid"
    if pattern == "triangles":
        return phi / 3.0, phi, "stretch-dominated triangular lattice: E/Es = phi/3 in plane, phi along Z"
    if pattern == "honeycomb":
        return 1.5 * phi ** 3, phi, "hexagonal honeycomb: E/Es = 1.5 phi^3 in plane, phi along Z"
    if pattern in ("cubic", "gyroid"):
        return phi ** 2, phi ** 2, "bending-dominated 3-D lattice: E/Es = phi^2"
    # grid, rectilinear, concentric: square lattice, direction not tracked -> off-axis (lower) value
    return min(phi / 2.0, phi ** 3), phi, ("square lattice, off-axis (conservative): E/Es = phi^3 in plane "
                                         "(phi/2 along the lines), phi along Z")


def effective_material(material: Material, ps: dict | PrintSettings,
                       shell_fraction: Optional[float] = None) -> Material:
    """Material with effective properties for the given print settings.

    ``shell_fraction``: volume fraction of the solid shell (from the geometry). If it is None
    and the infill is below 100 %, the stored value ``ps.extra['shell_fraction']`` is used;
    without it the whole body is treated as core (a lower bound) and this is stated."""
    s = ps if isinstance(ps, PrintSettings) else PrintSettings.from_dict(ps)
    m = copy.deepcopy(material)
    phi = s.infill_percent / 100.0
    notes = [f"Effective FDM properties ({['', 'level 1', 'level 2', 'level 3'][s.level]}, infill "
             f"{s.infill_percent:g} % {s.infill_pattern}, {s.perimeters} perimeters)."]
    E_s = material.youngs_modulus
    o = material.orthotropic
    E3_s = o["E3"] if o else E_s
    if phi < 1.0:
        f = shell_fraction if shell_fraction is not None else (s.extra or {}).get("shell_fraction")
        r_in, r_z, law = core_stiffness_ratios(s.infill_pattern, phi)
        if f is None:
            f = 0.0
            notes.append("Shell fraction unknown: whole body treated as infill core (lower bound).")
        else:
            notes.append(f"Shell volume fraction {f:.3f} (perimeters and top/bottom layers).")
        notes.append(f"Core law: {law} [{REFERENCE}]. Infill does not scale stiffness proportionally.")
        e_in = f * E_s + (1 - f) * r_in * E_s
        e_z = f * E3_s + (1 - f) * r_z * E3_s
        m.density = material.density * (f + (1 - f) * phi) if material.density else material.density
    else:
        e_in, e_z = E_s, E3_s
    if s.level == 1:
        # isotropic screening value: the lower of in-plane and build-direction stiffness
        m.youngs_modulus = min(e_in, e_z)
        m.orthotropic = None
        notes.append("Isotropic screening approximation: the lower of the in-plane and build-direction "
                     "moduli is used in all directions.")
    else:
        nu = material.poissons_ratio
        if o is None:
            notes.append("No orthotropic data for this material: isotropic properties are used.")
            m.youngs_modulus = min(e_in, e_z)
        else:
            from opencae.materials.database import _ortho
            m.orthotropic = _ortho(e_in, e_z, nu)
            m.youngs_modulus = e_in
    from opencae.materials.database import min_strength
    smin, key = min_strength(material)
    if smin:
        m.yield_strength = smin
        notes.append(f"Strength for safety factors: weakest printed-specimen value ({key}, "
                     f"{smin / 1e6:.3g} MPa), not reduced for infill.")
    m.id = f"{material.id}__fdm"
    m.name = f"{material.name} [printed, {s.infill_percent:g} % infill, level {s.level}]"
    m.notes = (material.notes + " " + " ".join(notes)).strip()
    if "effective properties (model)" not in m.estimated:
        m.estimated = list(m.estimated) + ["effective properties (model)"]
    return m
