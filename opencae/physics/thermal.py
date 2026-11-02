"""Heat conduction with CalculiX: steady-state and transient.

Thermal boundary conditions are stored with SI values (W, W/m², W/(m²·K),
W/m³, °C) whatever the project unit system, and converted to the project's
consistent units when the deck is written. Temperatures are in °C (CalculiX
works with any temperature scale; radiation uses ABSOLUTE ZERO = -273.15).

Supported (study items):

* Constraint ``temperature``: params ``value`` (°C) on faces/edges/vertices.
* Load ``heat_flux``: ``magnitude`` W/m², positive = heat ENTERING the body
  (CalculiX *DFLUX convention, verified).
* Load ``heat_power``: total ``magnitude`` W into the selected faces (uniform flux).
* Load ``convection``: ``film`` h W/(m²·K), ``ambient`` °C (*FILM).
* Load ``radiation``: ``emissivity`` (0..1), ``ambient`` °C (*RADIATE, grey body
  to an enclosure at ambient temperature).
* Load ``heat_generation``: ``magnitude`` W/m³ in the bodies given as ``body``
  targets (all study bodies when no target).

Faces without a condition are adiabatic (insulated).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.boundary_conditions.resolve import (ERROR, WARNING, Issue, boundary_face_lookup, mesh_components,
                                                 tri_areas)
from opencae.geometry.references import resolve_ref
from opencae.meshing.elements import nodal_volume_weights as _nvw, surface_integrals
from opencae.physics import register
from opencae.solver.calculix.deck import _fmt, _id_lines, section_blocks, write_mesh_block

STEFAN_BOLTZMANN = 5.670374419e-8  # W/(m^2 K^4)
THERMAL_CONSTRAINTS = ("temperature",)
THERMAL_LOADS = ("heat_flux", "heat_power", "convection", "radiation", "heat_generation")
ALL_FIXED = "T_ALL_FIXED"
FACE_ONLY = ("heat_flux", "heat_power", "convection", "radiation")


@dataclass
class ThermalModel:
    fixed_T: dict[int, float] = field(default_factory=dict)  # node -> °C
    fixed_sets: dict[str, np.ndarray] = field(default_factory=dict)
    dflux: list[tuple[int, int, float]] = field(default_factory=list)  # elem, face, q (project units)
    bflux: list[tuple[np.ndarray, float]] = field(default_factory=list)  # elements, q_vol (project units)
    films: list[tuple[int, int, float, float]] = field(default_factory=list)  # elem, face, T_amb, h
    radiates: list[tuple[int, int, float, float]] = field(default_factory=list)  # elem, face, T_amb, eps
    heat_input_W: float = 0.0  # prescribed heat input (flux + power + generation), SI
    nodal_heat_W: Optional[np.ndarray] = None  # consistent nodal share of the prescribed heat input
    issues: list[Issue] = field(default_factory=list)
    glyphs: list[dict] = field(default_factory=list)
    interactions: list[dict] = field(default_factory=list)  # bonded (tied) faces of separate meshes

    @property
    def ok(self) -> bool:
        return not any(i.severity == ERROR for i in self.issues)


def _targets(item, allowed, mesh, bodies, body_ids, add):
    out = []
    if not item.targets and item.kind != "heat_generation":
        add(ERROR, "no geometry selected", item.name)
    for ref in item.targets:
        if ref.kind not in allowed:
            add(ERROR, f"{ref.kind} targets are not allowed (use {', '.join(allowed)})", item.name)
            continue
        if ref.body not in body_ids:
            add(ERROR, "target belongs to a body that is not part of this study", item.name)
            continue
        if ref.kind == "body":
            out.append((ref, None))
            continue
        st = resolve_ref(ref, bodies)
        if not st.ok:
            add(ERROR, f"{ref.label()}: {st.message}", item.name)
            continue
        key = mesh.key(ref.body, st.index)
        out.append((ref, key))
    return out


def resolve_thermal(session, bodies_list, mesh) -> ThermalModel:
    p = session.project
    us = session.units
    tm = ThermalModel()
    body_ids = {b.id for b in bodies_list}

    def add(sev, msg, src=""):
        tm.issues.append(Issue(sev, msg, src))

    transient = p.analysis.analysis_type == "thermal_transient"
    tm.nodal_heat_W = np.zeros(mesh.n_nodes)
    for b in bodies_list:
        mat = p.material_for_body(b.id)
        if mat is None:
            continue
        if not mat.thermal_conductivity:
            add(ERROR, f"material '{mat.name}' has no thermal conductivity", "Material")
        if transient and (not mat.specific_heat or not mat.density):
            add(ERROR, f"transient analysis needs density and specific heat for '{mat.name}'", "Material")
    for c in p.constraints:
        if c.suppressed:
            continue
        if c.kind not in THERMAL_CONSTRAINTS:
            add(WARNING, f"'{c.kind}' is a structural support and is ignored in a thermal analysis", c.name)
            continue
        T = c.params.get("value")
        if T is None:
            add(ERROR, "temperature value missing", c.name)
            continue
        nodes_all = []
        for ref, key in _targets(c, ("face", "edge", "vertex"), mesh, session.bodies, body_ids, add):
            if ref.kind == "face":
                nodes = mesh.face_nodes(key) if key in mesh.face_tris else None
            elif ref.kind == "edge":
                nodes = mesh.edge_nodes.get(key)
            else:
                nodes = np.array([mesh.vertex_nodes[key]]) if key in mesh.vertex_nodes else None
            if nodes is None or not len(nodes):
                add(ERROR, f"{ref.label()} is not mapped to the mesh", c.name)
                continue
            for n in nodes:
                n = int(n)
                if n in tm.fixed_T and abs(tm.fixed_T[n] - float(T)) > 1e-12:
                    add(ERROR, f"conflicting temperatures on node {n + 1}", c.name)
                tm.fixed_T[n] = float(T)
            nodes_all.append(nodes)
        if nodes_all:
            tm.fixed_sets[c.name or c.id] = np.unique(np.concatenate(nodes_all))
    lookup = None
    has_exchange = bool(tm.fixed_T)
    for ld in p.loads:
        if ld.suppressed:
            continue
        if ld.kind not in THERMAL_LOADS:
            add(WARNING, f"'{ld.kind}' is a structural load and is ignored in a thermal analysis", ld.name)
            continue
        prm = ld.params
        if ld.kind == "heat_generation":
            q = float(prm.get("magnitude", 0.0))
            tg = _targets(ld, ("body",), mesh, session.bodies, body_ids, add) if ld.targets else []
            slots = [mesh.slot(r.body) for r, _ in tg] if tg else list(range(len(mesh.body_ids)))
            els = np.nonzero(np.isin(mesh.element_body, slots))[0]
            tm.bflux.append((els, q * us.heat_gen_from_si))
            w, vol = _nvw(mesh.nodes, mesh.elements[els], mesh.element_type)
            vol_si = vol / us.length_from_si ** 3
            tm.nodal_heat_W += w * q / us.length_from_si ** 3
            tm.heat_input_W += q * vol_si
            continue
        tg = _targets(ld, ("face",), mesh, session.bodies, body_ids, add)
        if lookup is None and tg:
            lookup = boundary_face_lookup(mesh)
        faces = []
        for ref, key in tg:
            if key not in mesh.face_tris:
                add(ERROR, f"{ref.label()} is not mapped to the mesh", ld.name)
                continue
            faces.append(mesh.face_tris[key])
        if not faces:
            continue
        area = sum(tri_areas(mesh.nodes, f).sum() for f in faces)  # project units
        area_si = area / us.length_from_si ** 2
        if ld.kind == "heat_power":
            P = float(prm.get("magnitude", 0.0))
            q_si = P / area_si if area_si > 0 else 0.0
            kind = "heat_flux"
        else:
            q_si = float(prm.get("magnitude", 0.0))
            kind = ld.kind
        nc = 3 if mesh.shape == "tet" else 4
        missing = 0
        for f in faces:
            for fe in f:
                hit = lookup.get(tuple(int(v) for v in np.sort(fe[:nc])))
                if hit is None:
                    missing += 1
                    continue
                e, fn = hit
                if kind == "heat_flux":
                    tm.dflux.append((e, fn, q_si * us.heat_flux_from_si))
                elif kind == "convection":
                    h = float(prm.get("film", 10.0))
                    if h <= 0:
                        add(ERROR, "film coefficient must be > 0", ld.name)
                        break
                    tm.films.append((e, fn, float(prm.get("ambient", 20.0)), h * us.film_from_si))
                    has_exchange = True
                elif kind == "radiation":
                    eps = float(prm.get("emissivity", 0.9))
                    if not 0 < eps <= 1:
                        add(ERROR, "emissivity must be in (0, 1]", ld.name)
                        break
                    tm.radiates.append((e, fn, float(prm.get("ambient", 20.0)), eps))
                    has_exchange = True
        if missing:
            add(ERROR, f"{missing} face elements do not coincide with element faces", ld.name)
        if kind == "heat_flux":
            tm.heat_input_W += q_si * area_si
            for fc in faces:
                wN, _, _ = surface_integrals(mesh.nodes, fc)
                np.add.at(tm.nodal_heat_W, fc, wN * q_si / us.length_from_si ** 2)
        tm.glyphs.append({"kind": ld.kind, "faces": [r.index for r, _ in tg]})
    from opencae.boundary_conditions.resolve import _resolve_interactions
    links = _resolve_interactions(p, mesh, session.bodies, body_ids, tm, add)
    for i in tm.interactions:
        if i["kind"] == "contact":
            add(ERROR, "thermal contact (gap conductance) is not available; use a bonded interaction (perfect "
                       "thermal contact)", i["name"])
    tm.interactions = [i for i in tm.interactions if i["kind"] == "bonded"]
    if not tm.fixed_T and not tm.films and not tm.radiates and not transient:
        add(ERROR, "steady-state heat conduction needs at least one prescribed temperature, convection or "
                   "radiation boundary; otherwise the temperature level is undetermined.")
    elif not transient:
        ncomp, node_comp = mesh_components(mesh)
        parent = list(range(ncomp))

        def find(x):
            while parent[x] != x:
                x = parent[x]
            return x
        for a_n, b_n in links:  # bodies joined by bonded interactions conduct heat to each other
            ra, rb = find(int(node_comp[a_n])), find(int(node_comp[b_n]))
            if ra != rb:
                parent[ra] = rb
        node_comp = np.array([find(int(c)) if c >= 0 else -1 for c in node_comp])
        ncomp = len(np.unique(node_comp[node_comp >= 0]))
        if ncomp > 1:
            anchored = set()
            for n in tm.fixed_T:
                anchored.add(node_comp[n])
            for e, *_ in tm.films + tm.radiates:
                anchored.add(node_comp[mesh.elements[e, 0]])
            if len(anchored) < ncomp:
                add(ERROR, "a body that is not connected to the rest has no temperature, convection or radiation "
                           "boundary; its temperature level is undetermined.")
    if not has_exchange and tm.heat_input_W != 0 and not transient:
        add(ERROR, "heat is added but cannot leave the model (no temperature/convection/radiation boundary).")
    if not tm.dflux and not tm.bflux and not tm.films and not tm.radiates and \
            len({round(v, 12) for v in tm.fixed_T.values()}) <= 1 and not transient:
        add(WARNING, "no heat loads and a single prescribed temperature: the solution is uniform.")
    return tm


def write_thermal_deck(session, tm: ThermalModel) -> str:
    p = session.project
    us = session.units
    mesh = session.mesh
    a = p.analysis
    transient = a.analysis_type == "thermal_transient"
    out = [f"*HEADING\nOpenCAE Studio - {p.name} - {a.title} - units {us.key}, temperatures in degC\n",
           write_mesh_block(mesh, p.mesh_settings.solver_element(mesh.element_type))]
    names = {}
    for name, nodes in tm.fixed_sets.items():
        sn = "T_" + "".join(ch if ch.isalnum() else "_" for ch in name.upper())[:60]
        while sn in names.values():
            sn += "_"
        names[name] = sn
        out.append(f"*NSET, NSET={sn}\n{_id_lines(nodes + 1)}\n")
    if tm.fixed_T:
        out.append(f"*NSET, NSET={ALL_FIXED}\n{_id_lines(np.array(sorted(tm.fixed_T)) + 1)}\n")
    out.append(section_blocks(mesh, session.body_materials(), us, thermal=True))
    if tm.interactions:
        from opencae.solver.calculix.deck import interaction_blocks
        out.append(interaction_blocks(mesh, tm.interactions, session.body_materials(), us))
    if tm.radiates:
        out.append(f"*PHYSICAL CONSTANTS, ABSOLUTE ZERO=-273.15, STEFAN BOLTZMANN="
                   f"{_fmt(STEFAN_BOLTZMANN * us.heat_flux_from_si)}\n")
    out.append(f"*INITIAL CONDITIONS, TYPE=TEMPERATURE\nNALL, {_fmt(a.initial_temperature)}\n")
    if transient:
        n_inc = max(1, int(round(a.time_total / max(a.time_step, 1e-30))))
        freq = max(1, n_inc // 50)  # store at most ~50 frames
        out.append(f"*STEP, INC={n_inc + 10}\n*HEAT TRANSFER, DIRECT\n{_fmt(a.time_step)}, {_fmt(a.time_total)}\n")
    else:
        out.append("*STEP\n*HEAT TRANSFER, STEADY STATE\n1., 1.\n")
        freq = 1
    if tm.fixed_T:
        out.append("*BOUNDARY\n" + "".join(f"{n + 1}, 11, 11, {_fmt(T)}\n" for n, T in sorted(tm.fixed_T.items())))
    if tm.dflux or tm.bflux:
        out.append("*DFLUX\n")
        out += [f"{e + 1}, S{f}, {_fmt(q)}\n" for e, f, q in tm.dflux]
        for els, q in tm.bflux:
            out += [f"{e + 1}, BF, {_fmt(q)}\n" for e in els]
    if tm.films:
        out.append("*FILM\n" + "".join(f"{e + 1}, F{f}, {_fmt(T)}, {_fmt(h)}\n" for e, f, T, h in tm.films))
    if tm.radiates:
        out.append("*RADIATE\n" + "".join(f"{e + 1}, R{f}, {_fmt(T)}, {_fmt(eps)}\n" for e, f, T, eps in tm.radiates))
    out.append(f"*NODE FILE, FREQUENCY={freq}\nNT\n*EL FILE, FREQUENCY={freq}\nHFL\n")
    for name, sn in names.items():
        out.append(f"*NODE PRINT, NSET={sn}, TOTALS=ONLY\nRFL\n")
    if tm.fixed_T:  # union: exact total even when sets share nodes
        out.append(f"*NODE PRINT, NSET={ALL_FIXED}, TOTALS=ONLY\nRFL\n")
    out.append("*END STEP\n")
    return "".join(out)


# ----------------------------------------------------------------------------- results
@dataclass
class TQuantity:
    key: str
    label: str
    unit_kind: str
    location: str


THERMAL_QUANTITIES = {
    "T": TQuantity("T", "Temperature", "temperature", "nodal"),
    "HF_mag": TQuantity("HF_mag", "Heat flux magnitude", "heat_flux", "nodal-averaged"),
    "HF_x": TQuantity("HF_x", "Heat flux X", "heat_flux", "nodal-averaged"),
    "HF_y": TQuantity("HF_y", "Heat flux Y", "heat_flux", "nodal-averaged"),
    "HF_z": TQuantity("HF_z", "Heat flux Z", "heat_flux", "nodal-averaged"),
    "gradT_mag": TQuantity("gradT_mag", "Temperature gradient magnitude", "temperature_gradient", "nodal-averaged"),
}


class ThermalResultSet:
    """Temperatures (°C) and heat flux (W/m², SI) per stored time frame."""

    kind = "thermal"

    def __init__(self, mesh, times, temperature, flux, conductivity_nodal=None, fixed_heat=None):
        self.mesh = mesh
        self.times = np.asarray(times, float)
        self.temperature = np.asarray(temperature, float)  # (F, N)
        self.flux = None if flux is None else np.asarray(flux, float)  # (F, N, 3) W/m^2
        self.k_nodal = conductivity_nodal  # W/(m K) per node (for the gradient)
        self.frame = len(self.times) - 1
        self.fixed_heat = fixed_heat or {}
        self.displacement = np.zeros_like(mesh.nodes)
        self.ip_stress = None
        self.stress = None

    # common result-set interface
    def frames(self) -> list[str]:
        if len(self.times) <= 1:
            return []
        return [f"t = {t:.6g} s" for t in self.times]

    def set_frame(self, i: int) -> None:
        self.frame = int(i)

    def info(self, key):
        return THERMAL_QUANTITIES[key]

    def available(self) -> list[str]:
        keys = ["T"]
        if self.flux is not None:
            keys += ["HF_mag", "HF_x", "HF_y", "HF_z"]
            if self.k_nodal is not None:
                keys.append("gradT_mag")
        return keys

    def quantity(self, key: str, frame: Optional[int] = None) -> np.ndarray:
        f = self.frame if frame is None else frame
        if key == "T":
            return self.temperature[f]
        q = self.flux[f]
        if key == "HF_mag":
            return np.linalg.norm(q, axis=1)
        if key in ("HF_x", "HF_y", "HF_z"):
            return q[:, "xyz".index(key[-1])]
        if key == "gradT_mag":
            return np.linalg.norm(q, axis=1) / self.k_nodal  # K/m
        raise KeyError(key)

    def history(self, node: int) -> tuple[np.ndarray, np.ndarray]:
        return self.times, self.temperature[:, node]

    def summary(self) -> dict:
        T = self.temperature[-1]
        i, j = int(np.argmax(T)), int(np.argmin(T))
        s = {"kind": "thermal", "max_temperature": float(T[i]), "max_temperature_node": i + 1,
             "max_temperature_location": self.mesh.nodes[i].tolist(),
             "min_temperature": float(T[j]), "min_temperature_node": j + 1, "frames": len(self.times),
             "final_time": float(self.times[-1])}
        if self.flux is not None:
            s["max_heat_flux_W_m2"] = float(np.linalg.norm(self.flux[-1], axis=1).max())
        if self.fixed_heat:
            s["heat_at_fixed_temperatures_W"] = self.fixed_heat
        return s

    def save(self, path: Path) -> None:
        d = {"kind": np.array("thermal"), "times": self.times, "temperature": self.temperature,
             "fixed_heat": np.array(json.dumps(self.fixed_heat))}
        if self.flux is not None:
            d["flux"] = self.flux
        if self.k_nodal is not None:
            d["k_nodal"] = self.k_nodal
        np.savez_compressed(path, **d)

    @classmethod
    def load(cls, path: Path, mesh) -> "ThermalResultSet":
        z = np.load(path, allow_pickle=False)
        return cls(mesh, z["times"], z["temperature"], z["flux"] if "flux" in z.files else None,
                   z["k_nodal"] if "k_nodal" in z.files else None, json.loads(str(z["fixed_heat"])))


def read_thermal(session, job_dir: Path, jobname: str) -> ThermalResultSet:
    from opencae.solver.calculix.dat import read_dat
    from opencae.solver.calculix.frd import read_frd
    mesh = session.mesh
    us = session.units
    frd = read_frd(Path(job_dir) / f"{jobname}.frd")
    N = mesh.n_nodes
    if len(frd.node_ids) != N:
        raise ValueError(f"Result file has {len(frd.node_ids)} nodes, mesh has {N}.")
    tb = [b for b in frd.blocks if b.name == "NDTEMP"]
    fb = [b for b in frd.blocks if b.name == "FLUX"]
    if not tb:
        raise ValueError("No temperature (NDTEMP) results in the .frd file.")
    times = [b.step_time for b in tb]
    T = np.zeros((len(tb), N))
    for k, b in enumerate(tb):
        T[k, b.node_ids - 1] = b.values[:, 0]
    flux = None
    if fb and len(fb) == len(tb):
        flux = np.zeros((len(fb), N, 3))
        for k, b in enumerate(fb):
            flux[k, b.node_ids - 1] = b.values[:, :3] / us.heat_flux_from_si  # -> W/m^2
    k_nodal = np.zeros(N)
    for slot, b in enumerate(mesh.body_ids):
        mat = session.project.material_for_body(b)
        nodes = np.unique(mesh.elements[mesh.element_body == slot])
        k_nodal[nodes] = mat.thermal_conductivity if mat and mat.thermal_conductivity else np.nan
    fixed_heat = {}
    dat = Path(job_dir) / f"{jobname}.dat"
    if dat.exists():
        import re
        text = dat.read_text(errors="replace")
        for name, val in re.findall(r"total heat generation for set (\S+) and time.*?\n\s*\n\s*(\S+)", text, re.S):
            fixed_heat[name] = float(val) / us.power_from_si  # W (last increment wins)
    return ThermalResultSet(mesh, times, T, flux, k_nodal, fixed_heat)


def heat_balance(summary: dict, rel_tol: float = 1e-4) -> Optional[dict]:
    """Steady-state energy balance from the solver's heat at fixed-temperature nodes.

    CalculiX reports the heat at prescribed-temperature nodes without the share of the
    prescribed input that acts directly on those nodes, so
    ``Q_fixed + Q_in - Q_in_on_fixed = 0`` when no convection/radiation is present."""
    fh = summary.get("heat_at_fixed_temperatures_W", {})
    if ALL_FIXED not in fh or summary.get("heat_exchange_boundaries") or "heat_input_W" not in summary:
        return None
    if summary.get("frames", 1) > 1:
        return None
    q_in = summary["heat_input_W"]
    res = fh[ALL_FIXED] + q_in - summary.get("heat_input_on_fixed_W", 0.0)
    scale = max(abs(q_in), abs(fh[ALL_FIXED]), 1e-30)
    return {"residual_W": res, "relative": abs(res) / scale, "ok": abs(res) / scale < rel_tol}


class ThermalPhysics:
    name = "Heat conduction"
    analysis_types = ("thermal_steady", "thermal_transient")
    equations = [
        ("Energy (conduction)", "ρ c ∂T/∂t = ∇·(k ∇T) + q_v   (steady state: ∂T/∂t = 0)"),
        ("Fourier's law", "q = −k ∇T"),
        ("Boundary conditions", "T = T̄;  −k ∂T/∂n = −q̄ (flux into body);  convection q = h (T − T∞);  "
                                "radiation q = ε σ (T⁴ − T∞⁴)"),
    ]
    assumptions = ["Isotropic, temperature-independent conductivity and specific heat.",
                   "Unspecified surfaces are adiabatic (insulated).",
                   "Bodies in one study are in perfect thermal contact where they touch (no contact resistance).",
                   "Radiation exchanges heat with a black enclosure at the ambient temperature (no view factors "
                   "between model surfaces)."]

    def resolve(self, session, bodies, mesh):
        return resolve_thermal(session, bodies, mesh)

    def write_input(self, session, job_dir, jobname, tm) -> None:
        (Path(job_dir) / f"{jobname}.inp").write_text(write_thermal_deck(session, tm), encoding="utf-8", errors="replace")
        on_fixed = float(tm.nodal_heat_W[list(tm.fixed_T)].sum()) if tm.fixed_T else 0.0
        (Path(job_dir) / "resolved_loads.json").write_text(json.dumps(
            {"heat_input_W": tm.heat_input_W, "heat_input_on_fixed_W": on_fixed,
             "heat_exchange_boundaries": bool(tm.films or tm.radiates)}))

    def read_results(self, session, job_dir, jobname):
        return read_thermal(session, Path(job_dir), jobname)

    def load_results(self, path, mesh):
        return ThermalResultSet.load(path, mesh)


register(ThermalPhysics())
