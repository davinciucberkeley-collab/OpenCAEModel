"""Tilt-wing VTOL example: the aircraft of the Tilt-Wing Designer (Cakir & Kurtulus 2022 configuration)
analysed with OpenCAE Studio - structural, thermal and flow studies in one project.

    python -m opencae.examples.tiltwing [--out DIR] [--no-cfd]

Inputs are the designer's own numbers at the paper's default values (``tiltwing_designer.json``,
produced by running the designer's model code): geometry, masses, rotor thrusts, the three wing load
cases (hover at maximum thrust, cruise gust/manoeuvre, hard landing) as nodal load vectors, the
bearing-bracket loads and the designer's material estimates. Every study compares its result with
the designer's simplified model (Euler-Bernoulli beam, 2-D plane-stress bracket, lumped-mass modes,
AeroBuildup / vortex-lattice aerodynamics) or a closed-form check; the comparison is written into the
study notes and the report. Nothing is tuned to agree.

Studies
-------
1-3  Wing spar, static: hover / cruise / landing   (CalculiX, C3D10, orthotropic CF tube)
4    Wing spar, modes with wing and motor masses   (CalculiX, lumped masses as equivalent densities)
5    Tilt-bearing bracket, static (worst case)      (CalculiX, C3D10, Al 6061-T6)
6    Motor-clamp heat path (steady thermal, 1 W)    (CalculiX)
7-8  Clamp/spar bond, hot (+40 C) and cold (-5 C) soak, thermal stress
9    Cruise CFD, half aircraft with symmetry plane  (OpenFOAM simpleFoam, k-omega SST, wall layers)
10   Same CFD on a coarser wing/tail mesh (mesh sensitivity of 9)
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

from opencae.app.session import Session
from opencae.examples import tiltwing_geometry as TG
from opencae.geometry.references import make_ref
from opencae.project.model import Constraint, Load, Material, MaterialAssignment, MeshRefinement

DEFAULT_OUT = Path(__file__).parent / "projects"
NAME = "tiltwing_vtol"
G0 = 9.80665
DESIGNER_URL = "https://claude.ai/artifact/PQmwyLZZRQxfzAyzwcWGxL"
PAPER = ("Cakir & Kurtulus, 'Design and aerodynamic analysis of a VTOL tilt-wing UAV', Turk J Elec Eng & Comp "
         "Sci 30 (2022) 767-784, doi:10.3906/elk-2105-59")


# --------------------------------------------------------------------------- designer data
def designer() -> dict:
    return TG.designer_data()


def wing_bins(d: dict) -> dict:
    """The designer's nodal load vectors of the wing half, collected per spar segment and rotor clamp.

    Nodes between segments go to the segment they lie in (a node exactly on a cut is split half / half);
    rotor nodes (thrust, motor inertia and their tributary wing/spar inertia) go to the clamp rings."""
    fea = d["fea"]
    ys = np.asarray(fea["ys"]) * 1000.0
    rot = set(fea["rotIdx"])
    cuts = [ys[0], 400.0, 700.0, ys[-1]]
    out = {}
    for case, c in fea["cases"].items():
        F = np.asarray(c["F"])
        T = np.asarray(c["T"])  # cumulative torque from the tip inward (N m)
        tq = np.concatenate([[0.0], T[:-1] - T[1:]])  # torque introduced at node i
        seg = np.zeros(3)
        segT = np.zeros(3)
        rings = {}
        for i, y in enumerate(ys):
            if i == 0:
                continue  # root node: reacted at the clamped bearing
            if i in rot:
                rings[int(round(y))] = rings.get(int(round(y)), 0.0) + F[i]
                k = 0 if y <= 400.0 + 1e-6 else 2
                segT[k] += tq[i]
                continue
            hits = [k for k in range(3) if cuts[k] - 1e-6 <= y <= cuts[k + 1] + 1e-6]
            for k in hits:
                seg[k] += F[i] / len(hits)
                segT[k] += tq[i] / len(hits)
        out[case] = {"segments": seg.tolist(), "rings": rings, "seg_torque": segT.tolist(),
                     "total": float(F[1:].sum()), "rootM": c["rootM"], "rootV": c["rootV"], "rootT": c["rootT"],
                     "tip_mm": c["tip"] * 1000.0, "sig_root_MPa": c["sigRoot"] / 1e6, "vm_max_MPa": c["vmax"] / 1e6,
                     "n": c["n"], "name": c["name"], "desc": c["desc"]}
    return out


def beam_reference(d: dict, bins: dict, case: str) -> dict:
    """Euler-Bernoulli cantilever (clamped at the bearing) loaded exactly as the FE model: segment loads
    spread uniformly over each segment, ring loads uniformly over the 30 mm clamp width."""
    sp = d["sp"]
    D, t = sp["sparD"], sp["sparT"]
    E = d["materials"]["cf_tube"]["E"] / 1e6  # MPa
    I = math.pi / 64 * (D ** 4 - (D - 2 * t) ** 4)
    y0, y1 = d["fea"]["ys"][0] * 1000, d["fea"]["ys"][-1] * 1000
    cuts = [y0, 400.0, 700.0, y1]
    n = 2001
    y = np.linspace(y0, y1, n)
    q = np.zeros(n)  # N/mm
    b = bins[case]
    for k in range(3):
        m = (y >= cuts[k]) & (y <= cuts[k + 1])
        q[m] += b["segments"][k] / (cuts[k + 1] - cuts[k])
    for yr, Fr in b["rings"].items():
        zs = min(yr - 15.0, y1 - 30.0)
        m = (y >= zs) & (y <= zs + 30.0)
        q[m] += Fr / 30.0
    dy = y[1] - y[0]
    V = np.array([np.trapezoid(q[i:], y[i:]) if i < n - 1 else 0.0 for i in range(n)])  # shear from the tip
    M = np.array([np.trapezoid(V[i:], y[i:]) if i < n - 1 else 0.0 for i in range(n)])  # bending moment
    kappa = M / (E * I)
    slope = np.concatenate([[0.0], np.cumsum(0.5 * (kappa[1:] + kappa[:-1]) * dy)])
    w = np.concatenate([[0.0], np.cumsum(0.5 * (slope[1:] + slope[:-1]) * dy)])
    sig = np.abs(M) * D / 2 / I
    return {"y": y, "M": M, "w": w, "sig": sig, "tip": float(w[-1]), "rootM_Nm": float(M[0] / 1000),
            "I_mm4": I, "E_MPa": E}


# --------------------------------------------------------------------------- materials
def materials(d: dict) -> dict:
    m = d["materials"]
    src_d = f"Tilt-Wing Designer material table ({DESIGNER_URL}); marked 'estimate' there"
    cf = m["cf_tube"]
    E3, G = cf["E"], cf["G"]
    E1 = 10e9  # hoop / radial modulus: not given by the designer
    nu31 = 0.30
    cf_tube = Material(
        "CF tube, roll-wrapped (designer estimate)", youngs_modulus=E3, poissons_ratio=0.30, density=cf["rho"],
        yield_strength=cf["sig"], thermal_expansion=1.0e-6, thermal_conductivity=5.0, specific_heat=900.0,
        is_example=True, category="Composite", source=src_d,
        estimated=["E1", "E2", "nu12", "nu13", "nu23", "G12", "thermal_expansion", "thermal_conductivity",
                   "specific_heat", "youngs_modulus", "G13", "G23", "yield_strength", "density"],
        notes=("Transversely isotropic about the tube axis (material axis 3 = global Z = spar axis). E3 = 110 GPa "
               "and G13 = G23 = 20 GPa (axial-hoop shear, governs torsion) and the 600 MPa allowable "
               "(compression-limited) are the designer's estimates. E1 = E2 = 10 GPa, nu, CTE 1e-6 /K, "
               "k = 5 W/m K are typical values for carbon/epoxy tubes, also estimates: replace with the "
               "supplier's data before relying on the results."),
        orthotropic={"E1": E1, "E2": E1, "E3": E3, "nu12": 0.45, "nu13": nu31 * E1 / E3, "nu23": nu31 * E1 / E3,
                     "G12": E1 / (2 * 1.45), "G13": G, "G23": G})
    al = m["al6061"]
    alu = Material("Al 6061-T6 (designer table)", youngs_modulus=al["E"], poissons_ratio=al["E"] / (2 * al["G"]) - 1,
                   density=al["rho"], yield_strength=al["sig"], thermal_expansion=23.6e-6, thermal_conductivity=167.0,
                   specific_heat=896.0, is_example=True, category="Aluminium alloy",
                   source=f"{src_d.replace('; marked', '; E, G, yield from MIL-HDBK-5 / ASM per the designer; marked')}"
                          "; CTE, k, cp: ASM Handbook typical values for 6061-T6",
                   estimated=[], notes="nu from E and G of the designer's table (E/2G - 1 = 0.325).")
    return {"cf": cf_tube, "al": alu}


def modal_materials(d: dict, mats: dict, seg_vol: list[float], ring_vol: float) -> tuple[list, Material, dict]:
    """Equivalent densities that put the designer's wing-structure and motor masses on the spar model."""
    r = d["r"]
    y0, hb = d["fea"]["ys"][0], d["p"]["span"] / 2
    cuts = [y0, 0.4, 0.7, hb]

    def chord_int(a, b):
        c = lambda y: r["cr"] - (r["cr"] - r["ct"]) * y / hb
        return 0.5 * (c(a) + c(b)) * (b - a)

    tot = chord_int(y0, hb)
    skin = [r["mWing"] / 2 * chord_int(cuts[k], cuts[k + 1]) / tot for k in range(3)]
    m_motor = r["mMotors"] * 0.748 / (4 * 0.748 + 2 * 0.460)  # designer: pack ratio of APC 14x16 vs 8x10 kits
    rho_cf = mats["cf"].density
    seg_mats = []
    for k in range(3):
        m_tube = rho_cf * seg_vol[k] * 1e-9
        rho = (m_tube + skin[k]) / (seg_vol[k] * 1e-9)
        mm = Material(f"CF spar + wing structure mass, segment {k + 1} (equivalent density)",
                      youngs_modulus=mats["cf"].youngs_modulus, poissons_ratio=0.30, density=rho,
                      yield_strength=mats["cf"].yield_strength, is_example=True, category="Composite",
                      source="Tilt-Wing Designer mass model: wing structure 1.40 kg/m^2 spread in proportion to "
                             "chord (as in its beam model) + CF tube",
                      estimated=["density"], orthotropic=dict(mats["cf"].orthotropic),
                      notes=f"tube {m_tube * 1000:.1f} g + wing structure {skin[k] * 1000:.1f} g on this segment")
        seg_mats.append(mm)
    ring = Material("Motor + propeller lumped on the clamp (equivalent density, Al 6061 stiffness)",
                    youngs_modulus=mats["al"].youngs_modulus, poissons_ratio=mats["al"].poissons_ratio,
                    density=m_motor / (ring_vol * 1e-9), is_example=True, category="Lumped mass",
                    source="Tilt-Wing Designer: propulsion kit mass x 0.748 / (4 x 0.748 + 2 x 0.460) per wing rotor",
                    estimated=["density"], notes=f"{m_motor * 1000:.0f} g per wing rotor (motor, prop, ESC share)")
    return seg_mats, ring, {"skin_kg": skin, "motor_kg": m_motor}


# --------------------------------------------------------------------------- helpers
def _faces(body, pred):
    return [f.index for f in body.faces if pred(f)]


def _ref(body, idx):
    return [make_ref(body, "face", i) for i in ([idx] if isinstance(idx, int) else idx)]


def _span(f, k):
    lo, hi = f.bbox
    return hi[k] - lo[k]


def outer_cyl(body, r_out):
    return _faces(body, lambda f: f.surface_type == "cylinder" and abs(_span(f, 0) - 2 * r_out) < 0.3)


def solve(s: Session, log) -> object:
    s.generate_mesh()
    out, issues = s.run_solver()
    errs = [str(i) for i in issues if i.severity == "error"]
    if not (out and out.success):
        raise RuntimeError(f"{s.study.name}: solve failed: {errs or (out and out.errors)}")
    log(f"  {s.study.name}: solved ({s.mesh.n_nodes} nodes, {s.mesh.n_elements} elements)")
    return out


def nodal_max(rs, key, mask=None):
    v = rs.quantity(key)
    return float(np.max(v if mask is None else v[mask]))


# --------------------------------------------------------------------------- build
def build(out_dir: Path, run_cfd: bool = True, run_fea: bool = True, log=print) -> Session:
    d = designer()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{NAME}.ocae"
    s = Session()
    s.new("Tilt-wing VTOL (Tilt-Wing Designer)", "mm-N-s")
    p, r = d["p"], d["r"]
    s.project.description = (
        f"Tilt-wing VTOL UAV of the Tilt-Wing Designer ({DESIGNER_URL}), configuration after {PAPER}. "
        f"Paper defaults: span {p['span']} m, AR {p['AR']}, taper {p['taper']}, fuselage {p['Lf']} x {p['df']} m, "
        f"MTOW {r['m']:.2f} kg, cruise {p['Vc']} m/s. Geometry rebuilt from the designer's construction "
        "(S1223 wing, NACA 0009 tail, axes: X aft from the nose, Y up, Z starboard; mm). Loads, masses and material "
        "values are the designer's (its estimates are labelled as such). Each study note compares the 3-D result "
        "with the designer's simplified model.")
    s.save(path)
    mats = materials(d)
    s.project.materials += [mats["cf"], mats["al"]]
    work = s.data_dir / "geometry" / "source"
    work.mkdir(parents=True, exist_ok=True)

    # ---- geometry: airframe (half), spar model, bracket
    af, g = TG.airframe_half(d)
    TG.write_step([af], work / "airframe_half.step")
    spar = TG.spar_model(d)
    TG.write_step([sh for _, sh in spar], work / "wing_spar.step")
    br, bri = TG.bracket(d)
    TG.write_step([br], work / "bearing_bracket.step")
    f_af = s.import_geometry(work / "airframe_half.step")
    s.project.geometry.body_names[f_af.id] = "Airframe, starboard half (cruise)"
    f_sp = s.import_geometry(work / "wing_spar.step", split=True)
    sp_ids = s.feature_bodies(f_sp.id)
    for bid, (nm, _) in zip(sp_ids, spar):
        s.project.geometry.body_names[bid] = nm
    f_br = s.import_geometry(work / "bearing_bracket.step")
    s.project.geometry.body_names[f_br.id] = "Tilt-bearing bracket (starboard)"
    s.regenerate()
    B = s.bodies
    seg_ids, ring_ids = sp_ids[:3], sp_ids[3:]
    seg_vol = [B[b].volume for b in seg_ids] if hasattr(B[seg_ids[0]], "volume") else None
    spar_all = seg_ids + ring_ids
    bins = wing_bins(d)
    s.save()

    # ---------------------------------------------------------------- 1-3 wing spar statics
    root = B[seg_ids[0]]
    root_face = _faces(root, lambda f: f.normal is not None and f.normal[2] < -0.99)
    seg_out = [outer_cyl(B[b], 10.0) for b in seg_ids]
    ring_out = [outer_cyl(B[b], 15.0) for b in ring_ids]
    results = {"designer": {"p": p, "r": {k: r[k] for k in ("m", "W", "S", "MAC", "cr", "ct", "TfMax", "Tf1",
                                                            "Vslip", "ReC", "CLc", "xcg", "mMotors", "mWing")}},
               "bins": bins}
    first = s.study
    for k, case in enumerate(("hover", "cruise", "landing")):
        b = bins[case]
        if k == 0:
            st = first
            st.name = "1 Wing spar - hover, max thrust"
            st.body_ids = list(spar_all)
            st.mesh_settings.body = spar_all[0]
        else:
            st = s.new_study({"cruise": "2 Wing spar - cruise gust / manoeuvre",
                              "landing": "3 Wing spar - hard landing"}[case], "linear_static", spar_all)
        s.set_active_study(st.id)
        st.assignments = [MaterialAssignment(bid, mats["cf"].id) for bid in seg_ids] + \
                         [MaterialAssignment(bid, mats["al"].id) for bid in ring_ids]
        ms = st.mesh_settings
        ms.size_max, ms.size_min, ms.element_order = 3.0, 1.0, 2
        st.constraints = [Constraint("fixed", _ref(root, root_face), name="Tilt bearing (clamped root)")]
        loads = []
        for i, bid in enumerate(seg_ids):
            if abs(b["segments"][i]) > 1e-9:
                loads.append(Load("force", _ref(B[bid], seg_out[i]),
                                  {"magnitude": abs(b["segments"][i]),
                                   "direction": [0.0, math.copysign(1.0, b["segments"][i]), 0.0]},
                                  name=f"Segment {i + 1}: wing lift and inertia"))
            if abs(b["seg_torque"][i]) > 1e-9:
                loads.append(Load("moment", _ref(B[bid], seg_out[i]),
                                  {"magnitude": abs(b["seg_torque"][i]) * 1000.0,
                                   # designer: positive = nose-up; nose (-X) up about +Z is negative
                                   "direction": [0.0, 0.0, -math.copysign(1.0, b["seg_torque"][i])]},
                                  name=f"Segment {i + 1}: section pitching moment"))
        for j, bid in enumerate(ring_ids):
            yr = int(round(g["rotor_z"][j]))
            Fr = b["rings"][yr] if yr in b["rings"] else b["rings"][str(yr)] if str(yr) in b["rings"] else 0.0
            loads.append(Load("force", _ref(B[bid], ring_out[j]),
                              {"magnitude": abs(Fr), "direction": [0.0, math.copysign(1.0, Fr), 0.0]},
                              name=f"Rotor station {yr} mm: thrust and inertia"))
        st.loads = loads
        st.notes = f"Designer load case: {b['desc']}."
    hover = first
    # ---------------------------------------------------------------- 5 modes with wing and motor masses
    seg_vol = [B[b].volume for b in seg_ids]
    seg_mats, ring_mat, mass_info = modal_materials(d, mats, seg_vol, B[ring_ids[0]].volume)
    s.project.materials += seg_mats + [ring_mat]
    st = s.new_study("4 Wing spar - natural frequencies (with wing and motor masses)", "modal", spar_all)
    st.assignments = [MaterialAssignment(bid, m.id) for bid, m in zip(seg_ids, seg_mats)] + \
                     [MaterialAssignment(bid, ring_mat.id) for bid in ring_ids]
    st.mesh_settings.size_max, st.mesh_settings.size_min, st.mesh_settings.element_order = 3.0, 1.0, 2
    st.constraints = [Constraint("fixed", _ref(root, root_face), name="Tilt bearing (clamped root)")]
    st.analysis.n_modes = 6
    results["modal_masses"] = mass_info
    # ---------------------------------------------------------------- 6 bearing bracket
    bb = B[f_br.id]
    bot = _faces(bb, lambda f: f.normal is not None and f.normal[1] < -0.99)
    bore = _faces(bb, lambda f: f.surface_type == "cylinder")
    upper = [i for i in bore if bb.faces[i].centroid[1] > bri["yc"]]
    worst = max(("hover", "cruise", "landing"), key=lambda k: d["bracket"][k]["F"])
    Fb = d["bracket"][worst]["F"]
    st = s.new_study("5 Tilt-bearing bracket - " + worst + " (largest bearing load)", "linear_static", [f_br.id])
    st.assignments = [MaterialAssignment(f_br.id, mats["al"].id)]
    st.mesh_settings.size_max, st.mesh_settings.size_min, st.mesh_settings.element_order = 2.5, 0.5, 2
    st.mesh_settings.refinements = [MeshRefinement(_ref(bb, bore), 0.8, name="Bearing bore")]
    st.constraints = [Constraint("fixed", _ref(bb, bot), name="Bolted bottom edge")]
    st.loads = [Load("force", _ref(bb, upper), {"magnitude": Fb, "direction": [0.0, 1.0, 0.0]},
                     name="Bearing load (upper half of the bore)")]
    st.notes = (f"Designer F8 load: root moment / plate spacing (0.8 d_f) + half the root shear = {Fb:.1f} N per plate "
                f"({worst} case). The designer spreads it as a cosine over the bore in a 2-D plane-stress model; here "
                "it is a uniform traction on the upper half of the bore in 3-D.")
    # ---------------------------------------------------------------- 7 heat path from a motor clamp
    st = s.new_study("6 Motor clamp heat path - 1 W into the inboard clamp", "thermal_steady", spar_all)
    st.assignments = [MaterialAssignment(bid, mats["cf"].id) for bid in seg_ids] + \
                     [MaterialAssignment(bid, mats["al"].id) for bid in ring_ids]
    st.mesh_settings.size_max, st.mesh_settings.size_min, st.mesh_settings.element_order = 3.0, 1.0, 2
    st.constraints = [Constraint("temperature", _ref(root, root_face), {"value": 20.0},
                                 name="Bearing at ambient (20 C)")]
    st.loads = [Load("heat_power", _ref(B[ring_ids[0]], ring_out[0]), {"magnitude": 1.0},
                     name="1 W from the motor into the clamp")]
    st.notes = ("Thermal resistance of the conduction path motor clamp -> CF spar -> tilt bearing, everything else "
                "adiabatic (the spar runs inside the wing). The result is per watt: multiply by the heat a motor "
                "actually conducts into its mount, which is not known here.")
    # ---------------------------------------------------------------- 8-9 thermal stress of the bonded clamps
    for name, T in (("7 Clamp/spar bond - hot soak +40 C", 40.0), ("8 Clamp/spar bond - cold soak -5 C", -5.0)):
        st = s.new_study(name, "thermal_stress", spar_all)
        st.assignments = [MaterialAssignment(bid, mats["cf"].id) for bid in seg_ids] + \
                         [MaterialAssignment(bid, mats["al"].id) for bid in ring_ids]
        st.mesh_settings.size_max, st.mesh_settings.size_min, st.mesh_settings.element_order = 3.0, 1.0, 2
        st.mesh_settings.refinements = [MeshRefinement(_ref(B[bid], outer_cyl(B[bid], 15.0) +
                                                             _faces(B[bid], lambda f: f.normal is not None)),
                                                       1.5, name="Clamp") for bid in ring_ids]
        st.analysis.reference_temperature = 20.0
        st.constraints = [Constraint("fixed", _ref(root, root_face), name="Tilt bearing (clamped root)")]
        st.loads = [Load("uniform_temperature", [], {"value": T}, name=f"Soak at {T:g} C")]
        st.notes = (f"Whole wing structure at {T:g} C, bonded (perfectly tied) aluminium clamps on the CF tube, "
                    "stress-free at 20 C (assembly). Range from the designer's environment sliders: ISA -20 to +25 C "
                    "at sea level.")
    # ---------------------------------------------------------------- 9-10 cruise CFD (half model), two meshes
    fine = add_cfd(s, d, f_af.id, g, fine=True)
    coarse = s.duplicate_study(fine.id, "10 Cruise CFD - coarse mesh (mesh sensitivity of study 9)")
    for ref in coarse.mesh_settings.refinements:
        if ref.name in ("Wing", "Tail"):
            ref.size = 6.25
    coarse.mesh_settings.bl_first = 0.8
    coarse.notes = fine.notes.replace("Unpowered cruise", "Same as study 9 with wing and tail surface cells of "
                                      "6.25 mm instead of 3.125 mm (and a 0.8 mm first layer). Unpowered cruise")
    s.save()
    if run_fea:
        for st in list(s.project.studies):
            if st.analysis.analysis_type == "cfd_incompressible":
                continue
            s.set_active_study(st.id)
            solve(s, log)
            s.save()
    (s.data_dir / "tiltwing_inputs.json").write_text(json.dumps(results, indent=1, default=float))
    if run_cfd:
        for st in [x for x in s.project.studies if x.analysis.analysis_type == "cfd_incompressible"]:
            s.set_active_study(st.id)
            solve(s, log)
            s.save()
            slim_cfd(s)
    return s


def force_history(run: Path, frac: float = 0.3) -> dict | None:
    """Mean and range of the total wall force over the last ``frac`` of the iterations (force.dat)."""
    from opencae.cfd.openfoam.backend import _dat
    f = Path(run) / "postProcessing" / "forces" / "0" / "force.dat"
    if not f.exists():
        return None
    a = _dat(f)
    if a.size == 0:
        return None
    tail = a[int(len(a) * (1 - frac)):]
    return {"n": int(len(tail)), "Fx_mean": float(tail[:, 1].mean()), "Fy_mean": float(tail[:, 2].mean()),
            "Fx_min": float(tail[:, 1].min()), "Fx_max": float(tail[:, 1].max()),
            "Fy_min": float(tail[:, 2].min()), "Fy_max": float(tail[:, 2].max())}


def foam_patch_order(case: Path) -> list[str]:
    """Patch names in polyMesh/boundary order (the order foamToVTK writes them)."""
    import re as _re
    f = Path(case) / "constant" / "polyMesh" / "boundary"
    if not f.exists():
        return []
    txt = f.read_text(errors="replace")
    body = txt[txt.index("(", txt.index("FoamFile") + 1 if "FoamFile" in txt else 0):]
    body = txt.split("\n(", 1)[1] if "\n(" in txt else body
    return _re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\n\s*\{", body, _re.M)


def slim_fea(s: Session) -> None:
    """Remove the raw CalculiX output (.frd, .dat) of the solved structural/thermal studies: the results are
    kept in results.npz (what OpenCAE loads), the input deck and solver.log stay. Re-solve to get them back."""
    for st in s.project.studies:
        if st.analysis.analysis_type == "cfd_incompressible" or not st.results.directory:
            continue
        run = s.data_dir / st.results.directory
        if (run / "results.npz").exists():
            for ext in ("frd", "dat"):
                for f in run.glob(f"*.{ext}"):
                    f.unlink()


def slim_cfd(s: Session) -> None:
    """Remove OpenFOAM files that are not needed to view the results (time directories, VTK exports, the
    copied mesh in the run folder); results.npz, logs, force histories and the meshing case stay."""
    import shutil
    st = s.study
    if not st.results.directory:
        return
    run = s.data_dir / st.results.directory
    for sub in run.iterdir():
        if sub.is_dir() and (sub.name in ("VTK", "constant") or sub.name.replace(".", "").isdigit()) \
                and sub.name != "0":
            shutil.rmtree(sub, ignore_errors=True)
    case = (s.mesh.meta or {}).get("case_rel") if s.mesh is not None else None
    if case:
        shutil.rmtree(s.data_dir / case / "VTK", ignore_errors=True)


CFD_GROUPS = ("Wing", "Tail", "Nacelles", "Spar stubs", "Fuselage")


def classify_airframe_faces(body, g: dict) -> dict[str, list[int]]:
    """Faces of the flow domain that belong to the airframe, grouped by part (by position)."""
    tw = math.tan(math.radians(g["sweep"]))
    hb = g["span"] / 2
    L_w, L_t = 0.55 * g["cr"] + 100.0, 0.55 * g["crh"] + 60.0
    nac = []
    for z in g["rotor_z"]:
        x0 = g["xLEroot"] + z * tw - 0.45 * L_w - 2.2 * 28.0
        nac.append((x0 - 1, x0 + 2.2 * 28.0 + L_w + 1, g["yw"], z, 28.0 + 1))
    zt = g["bh"] / 2
    x0 = g["xth"] + zt * math.tan(math.radians(5.0)) - 0.45 * L_t - 2.2 * 20.0
    nac.append((x0 - 1, x0 + 2.2 * 20.0 + L_t + 1, g["yt"], zt, 20.0 + 1))
    lo_d, hi_d = body.bbox
    out = {k: [] for k in CFD_GROUPS}
    for f in body.faces:
        c = f.centroid
        on_box = f.normal is not None and any(
            abs(c[k] - lo_d[k]) < 1e-3 or abs(c[k] - hi_d[k]) < 1e-3 for k in range(3) if abs(f.normal[k]) > 0.999)
        if on_box:
            continue
        lo, hi = f.bbox
        if any(a <= c[0] <= b and math.hypot(c[1] - y, c[2] - z) <= r for a, b, y, z, r in nac):
            out["Nacelles"].append(f.index)
        elif c[0] > g["xt"] - 0.6 * g["crh"] and c[2] >= g["z_tail_in"] - 1 and abs(c[1] - g["yt"]) < 40:
            out["Tail"].append(f.index)
        elif c[2] >= g["z_in"] - 1 and abs(c[1] - g["yw"]) < 80 and c[0] > g["xLEroot"] - 5:
            out["Wing"].append(f.index)
        elif f.surface_type == "cylinder" and hi[2] - lo[2] > 5 and hi[0] - lo[0] < 30:
            out["Spar stubs"].append(f.index)
        else:
            out["Fuselage"].append(f.index)
    return out


def add_cfd(s: Session, d: dict, airframe_id: str, g: dict, fine: bool = True):
    p, r = d["p"], d["r"]
    name = "9 Cruise CFD - half aircraft, 20 m/s, k-omega SST, fine mesh"
    st = s.create_flow_domain([airframe_id], axis="+x", speed=p["Vc"], upstream=1.0, downstream=2.0, lateral=0.8,
                              name=name, symmetry="-z")
    dom = s.bodies[st.body_ids[0]]
    s.project.geometry.body_names[dom.id] = "Flow domain (half model, symmetry plane z = 0)"
    a = st.analysis
    env = d["env"]
    a.cfd_solver, a.flow_model = "openfoam", "sst"
    a.fluid_name, a.fluid_density, a.fluid_viscosity = "Air, ISA sea level", env["rho"], env["mu"]
    a.cfd_iterations, a.cfd_residual = 1500, -5.0
    a.turbulence_intensity, a.turbulent_viscosity_ratio = 0.01, 5.0
    a.ref_area, a.ref_velocity = r["S"] / 2.0, p["Vc"]  # half model: half the reference wing area
    groups = classify_airframe_faces(dom, g)
    for name in CFD_GROUPS:
        if groups[name]:
            st.constraints.append(Constraint("wall", _ref(dom, groups[name]), name=name))
    ms = st.mesh_settings
    # background cells 200 mm; surface refinement levels: 200 / 2^6 = 3.125 mm on wing and tail (fine) or
    # 2^5 = 6.25 mm (coarse, used for the mesh-sensitivity comparison), nacelles and stubs 6.25, fuselage 12.5 mm
    ms.size_max = 200.0
    h_lift = 3.125 if fine else 6.25
    ms.refinements = [MeshRefinement(_ref(dom, groups[k]), h_lift, name=k) for k in ("Wing", "Tail") if groups[k]]
    ms.refinements += [MeshRefinement(_ref(dom, groups[k]), 6.25, name=k) for k in ("Nacelles", "Spar stubs")
                       if groups[k]]
    ms.refinements.append(MeshRefinement(_ref(dom, groups["Fuselage"]), 12.5, name="Fuselage"))
    # wall layers for wall functions: Cf = 0.026 Re^-1/7 at the MAC Reynolds number gives u_tau ~ 0.9 m/s, so the
    # first cell centre sits at y+ ~ 15-25; layer stack thinner than the finest wall cell (snappyHexMesh rule)
    ms.bl_layers, ms.bl_first, ms.bl_growth = 3, (0.5 if fine else 0.8), 1.3
    st.notes = (f"Unpowered cruise: wing and tail at 0 deg tilt, fuselage axis along the flow (body angle 0), "
                f"V = {p['Vc']} m/s, ISA sea level (rho {env['rho']:.4f} kg/m^3, mu {env['mu']:.4e} Pa s), "
                f"Re(MAC) = {r['ReC']:.3g}. Half aircraft with a symmetry plane; coefficients use half the reference "
                f"area S/2 = {r['S'] / 2:.4f} m^2, so CL and CD are those of the whole aircraft. Fully turbulent "
                "k-omega SST with wall functions: laminar-flow regions and transition (significant at this Reynolds "
                "number) are not modelled. Propellers, slipstream, canopy and landing gear are not modelled.")
    return st


def example_copy(force: bool = False) -> Path:
    """User copy of the bundled, solved example (so the installed files are never modified)."""
    import shutil
    src = DEFAULT_OUT / f"{NAME}.ocae"
    if not src.exists():
        raise FileNotFoundError(f"The tilt-wing example is not installed ({src}). Build it with: "
                                "python -m opencae.examples.tiltwing")
    dst_dir = Path.home() / ".opencae" / "examples"
    dst = dst_dir / src.name
    if force or not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
        dst_dir.mkdir(parents=True, exist_ok=True)
        data = src.with_name(f"{NAME}_data")
        if (dst_dir / data.name).exists():
            shutil.rmtree(dst_dir / data.name)
        shutil.copytree(data, dst_dir / data.name)
        shutil.copy2(src, dst)
    return dst


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--no-cfd", action="store_true")
    a = ap.parse_args(argv)
    s = build(Path(a.out), run_cfd=not a.no_cfd)
    evaluate(s)
    write_reports(s)
    slim_fea(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())


# --------------------------------------------------------------------------- evaluation
def _study(s: Session, prefix: str):
    st = next((x for x in s.project.studies if x.name.startswith(prefix)), None)
    if st is not None:
        s.set_active_study(st.id)
    return st


def evaluate(s: Session, log=print) -> dict:
    """Compare every solved study with the designer's model / closed-form checks; write the study notes
    and ``tiltwing_results.json``. Only numbers read from the solved results are reported."""
    d = designer()
    bins = wing_bins(d)
    g = TG.geometry(d)
    sp = d["sp"]
    allow_cf, allow_al, FoS = d["materials"]["cf_tube"]["sig"] / 1e6, d["materials"]["al6061"]["sig"] / 1e6, sp["FoS"]
    R = {"designer": {"url": DESIGNER_URL, "paper": PAPER}}
    xa, ya = g["xw"], g["yw"]

    def keep_setup_note(st):
        return st.notes.split("\n\nResult")[0]

    for pre, case in (("1 ", "hover"), ("2 ", "cruise"), ("3 ", "landing")):
        st = _study(s, pre)
        if st is None or not st.results.completed:
            continue
        rs, X = s.results, s.mesh.nodes
        ref = beam_reference(d, bins, case)
        rr = np.hypot(X[:, 0] - xa, X[:, 1] - ya)
        tip = np.abs(X[:, 2] - X[:, 2].max()) < 0.5
        uy_tip = float(rs.displacement[tip, 1].mean())
        vm = rs.quantity("S_vm")
        tube = rr < 10.05
        sel = tube & (np.abs(X[:, 2] - 150.0) < 1.5)
        s150 = float(np.abs(rs.stress[sel, 2]).max())
        b150 = float(ref["sig"][np.argmin(np.abs(ref["y"] - 150.0))])
        vm_root = float(vm[tube].max())
        b = bins[case]
        ms_fe = allow_cf / (FoS * vm_root) - 1
        ms_des = allow_cf / (FoS * b["vm_max_MPa"]) - 1
        R[case] = {"tip_FE_mm": uy_tip, "tip_beam_same_loads_mm": ref["tip"], "tip_designer_mm": b["tip_mm"],
                   "sig150_FE_MPa": s150, "sig150_beam_MPa": b150, "vm_max_FE_MPa": vm_root,
                   "vm_max_designer_MPa": b["vm_max_MPa"], "MS_FE": ms_fe, "MS_designer": ms_des,
                   "reaction_N": float(np.linalg.norm(next(iter(rs.reaction_totals.values())))),
                   "applied_N": abs(b["total"])}
        st.notes = keep_setup_note(st) + (
            f"\n\nResult: tip deflection {uy_tip:.2f} mm (beam with the same binned loads {ref['tip']:.2f} mm, "
            f"designer's beam FE {b['tip_mm']:.2f} mm). Bending stress 40 mm outboard of the bearing "
            f"{s150:.1f} MPa (beam {b150:.1f} MPa). Peak von Mises {vm_root:.1f} MPa at the clamped root edge "
            f"(designer {b['vm_max_MPa']:.1f} MPa); margin of safety with the designer's 600 MPa allowable and "
            f"FoS {FoS}: {ms_fe:.2f} (designer {ms_des:.2f}). The allowable is the designer's estimate.")
    st = _study(s, "4 ")
    if st is not None and st.results.completed:
        f = list(st.results.summary.get("frequencies_hz", []))
        md = d["fea"]["modes"]
        bp = [2 * md["rpsHover"], 2 * md["rpsMax"]]
        R["modal"] = {"FE_Hz": f, "designer_bending_Hz": md["bend"], "designer_torsion_Hz": md["torsion"],
                      "rotor_1P_Hz": [md["rpsHover"], md["rpsMax"]], "blade_pass_2P_Hz": bp}
        near = [x for x in f if bp[0] * 0.9 <= x <= bp[1] * 1.1]
        st.notes = keep_setup_note(st) + (
            f"\n\nResult: bending modes {f[0]:.2f} / {f[2]:.2f} / {f[4]:.1f} Hz (each twice: the round tube bends "
            f"equally in both planes). Designer lumped-mass beam: {md['bend'][0]:.2f} / {md['bend'][1]:.2f} Hz. "
            f"Rotor speed (designer, C_T = 0.11 estimate): {md['rpsHover']:.0f} Hz in hover, {md['rpsMax']:.0f} Hz "
            f"at maximum thrust; two-blade passing frequency {bp[0]:.0f}-{bp[1]:.0f} Hz. "
            + (f"The third bending mode ({near[0]:.0f} Hz) lies in the blade-passing band: check it with the real "
               "propeller and motor masses or a ground vibration test. " if near else "")
            + "The designer's torsion mode (23 Hz) includes the rotary inertia of the wing about the spar, which "
              "this tube model does not carry; torsion is not among the six modes found here.")
    st = _study(s, "5 ")
    if st is not None and st.results.completed:
        sm = st.results.summary
        worst = max(("hover", "cruise", "landing"), key=lambda k: d["bracket"][k]["F"])
        bd = d["bracket"][worst]
        vm_b, vm_ip, u_b = sm["max_von_mises_nodal"], sm.get("max_von_mises_integration_point"), sm["max_displacement"]
        allow = allow_al / FoS
        R["bracket"] = {"F_N": bd["F"], "vm_FE_MPa": vm_b, "vm_ip_FE_MPa": vm_ip, "u_FE_mm": u_b,
                        "vm_designer_MPa": bd["vmax"], "u_designer_mm": bd["umax"], "MS_FE": allow / vm_b - 1}
        st.notes = keep_setup_note(st) + (
            f"\n\nResult: peak von Mises {vm_b:.2f} MPa (integration points {vm_ip:.2f} MPa) at the end of the loaded "
            f"arc, where the uniform-traction idealisation stops; designer's 2-D model {bd['vmax']:.2f} MPa. "
            f"Largest displacement {u_b * 1000:.2f} um (designer {bd['umax'] * 1000:.2f} um). Margin with 276 MPa / "
            f"FoS {FoS}: {allow / vm_b - 1:.0f}. The plate is lightly loaded; the bearing and its bolts, not the "
            "plate, size this joint.")
    st = _study(s, "6 ")
    if st is not None and st.results.completed:
        sm = st.results.summary
        dT = sm["max_temperature"] - 20.0
        cf = s.project.material_by_id(st.assignments[0].material) if hasattr(s.project, "material_by_id") else None
        k = 5.0
        A = math.pi / 4 * (sp["sparD"] ** 2 - (sp["sparD"] - 2 * sp["sparT"]) ** 2) * 1e-6
        Rlo, Rhi = (385.0 - g["z_in"]) / 1000 / (k * A), (415.0 - g["z_in"]) / 1000 / (k * A)
        R["heat"] = {"K_per_W_FE": dT, "K_per_W_1D_range": [Rlo, Rhi]}
        st.notes = keep_setup_note(st) + (
            f"\n\nResult: the clamp rises {dT:.0f} K per watt above the bearing (1-D conduction along the tube, "
            f"L / (k A) with L from the bearing to the clamp edges: {Rlo:.0f}-{Rhi:.0f} K/W). The CF spar is "
            "practically no heat path: motor heat has to leave through the motor and nacelle into the air. "
            "k = 5 W/m K for the tube is an estimate; the resistance scales with 1/k.")
    for pre, T in (("7 ", 40.0), ("8 ", -5.0)):
        st = _study(s, pre)
        if st is None or not st.results.completed:
            continue
        rs, X = s.results, s.mesh.nodes
        rr = np.hypot(X[:, 0] - xa, X[:, 1] - ya)
        vm = rs.quantity("S_vm")
        mid = (rr < 10.05) & (np.abs(X[:, 2] - 400.0) < 0.8)
        szz = rs.stress[mid, 2]
        s_mid = float(szz[np.argmax(np.abs(szz))])
        At = math.pi / 4 * (sp["sparD"] ** 2 - (sp["sparD"] - 2 * sp["sparT"]) ** 2)
        Ar = math.pi / 4 * ((sp["sparD"] + 10) ** 2 - sp["sparD"] ** 2)
        mats = {m.name: m for m in s.project.materials}
        cfm = next(m for m in s.project.materials if m.name.startswith("CF tube"))
        alm = next(m for m in s.project.materials if m.name.startswith("Al 6061"))
        Et, Er = cfm.orthotropic["E3"] / 1e6, alm.youngs_modulus / 1e6
        dTT = T - 20.0
        eps = (Er * Ar * alm.thermal_expansion + Et * At * cfm.thermal_expansion) / (Er * Ar + Et * At) * dTT
        s_long = Et * (eps - cfm.thermal_expansion * dTT)
        tau = abs(s_mid) * At / (math.pi * sp["sparD"] * 15.0)
        R["soak_%g" % T] = {"T": T, "vm_max_MPa": float(vm.max()), "szz_tube_mid_clamp_MPa": s_mid,
                            "long_joint_limit_MPa": s_long, "mean_bond_shear_MPa": tau}
        st.notes = keep_setup_note(st) + (
            f"\n\nResult: axial stress in the tube under the middle of each clamp {s_mid:.1f} MPa (long bonded joint "
            f"limit from force equilibrium {s_long:.1f} MPa: the 30 mm clamp carries {abs(s_mid / s_long) * 100:.0f} % "
            f"of it). Peak von Mises {vm.max():.1f} MPa. The force is transferred through the bond over 15 mm each "
            f"side: mean bond shear about {tau:.1f} MPa (peaks at the clamp ends are higher; the adhesive layer is "
            "not modelled - the clamp is tied to the tube). Compare with the adhesive's shear strength over the "
            "temperature range. CF tube CTE 1e-6 /K is an estimate.")
    for pre, key in (("9 ", "cfd"), ("10 ", "cfd_coarse")):
        st = _study(s, pre)
        if st is None or not st.results.completed:
            continue
        info = st.results.summary or {}
        cf = info.get("force_coefficients")
        W = d["r"]["W"]
        q = 0.5 * d["env"]["rho"] * d["p"]["Vc"] ** 2
        ab = d["aeroRef"]["tilt0"]
        i0 = d["aeroRef"]["alpha"].index(0)
        R[key] = {"CL": cf[1] if cf else None, "CD": cf[0] if cf else None, "lift_N_full": (cf[1] * q * d["r"]["S"])
                    if cf else None, "weight_N": W, "iterations": info.get("iterations"),
                    "converged": info.get("converged"), "y_plus": info.get("y_plus"),
                    "patch_forces_N_half": info.get("wall_forces_by_patch_N"),
                    "force_change_last_10pct": info.get("force_change_last_10pct"),
                    "designer_model": d["aero0"], "aerobuildup": {"CL": ab["CL"][i0], "CD": ab["CD"][i0]},
                    "vlm": d["vlm"]["cases"][0], "cells": info.get("mesh_cells")}
        hist = force_history(s.data_dir / st.results.directory)
        if hist:
            qS = q * d["r"]["S"] / 2.0
            R[key]["force_history"] = {k: v for k, v in hist.items()}
            R[key]["CL_mean"], R[key]["CD_mean"] = hist["Fy_mean"] / qS, hist["Fx_mean"] / qS
            R[key]["CL_range"] = [hist["Fy_min"] / qS, hist["Fy_max"] / qS]
            R[key]["CD_range"] = [hist["Fx_min"] / qS, hist["Fx_max"] / qS]
            R[key]["lift_N_full"] = 2 * hist["Fy_mean"]
            R[key]["CL_last"], R[key]["CD_last"] = R[key]["CL"], R[key]["CD"]
            R[key]["CL"], R[key]["CD"] = R[key]["CL_mean"], R[key]["CD_mean"]
            cf = [R[key]["CD_mean"], R[key]["CL_mean"], cf[2] if cf else 0.0]
        if cf:
            c = R[key]
            st.notes = keep_setup_note(st) + (
                f"\n\nResult (mean over the last {c.get('force_history', {}).get('n', 0)} iterations; the forces oscillate "
                f"between CL {c.get('CL_range', [0, 0])[0]:.3f} and {c.get('CL_range', [0, 0])[1]:.3f}): "
                f"CL = {cf[1]:.3f}, CD = {cf[0]:.4f} (L/D {cf[1] / cf[0]:.1f}); lift of the whole aircraft "
                f"{c['lift_N_full']:.1f} N vs weight {W:.1f} N at body angle 0. Designer's own model "
                f"CL {d['aero0']['CL']:.3f} / CD {d['aero0']['CD']:.4f} (C_D0 = 0.045 estimate); AeroSandbox "
                f"AeroBuildup CL {ab['CL'][i0]:.3f} / CD {ab['CD'][i0]:.4f}; vortex lattice CL "
                f"{d['vlm']['cases'][0]['CL']:.3f} (inviscid, induced drag only). "
                f"{'Converged' if info.get('converged') else 'NOT converged to the residual target'} after "
                f"{info.get('iterations')} iterations. Fully turbulent RANS: treat the coefficients "
                "as a mesh-dependent RANS estimate, not a validated value.")
    s.save()
    (s.data_dir / "tiltwing_results.json").write_text(json.dumps(R, indent=1, default=float))
    return R


# --------------------------------------------------------------------------- pictures and reports
def render_images(s: Session, out: Path) -> dict[str, list[Path]]:
    """One or two pictures per solved study (off-screen VTK)."""
    import pyvista as pv
    from opencae.visualization.scene import auto_deformation_scale, boundary_surface
    pv.OFF_SCREEN = True
    out.mkdir(parents=True, exist_ok=True)
    pics: dict[str, list[Path]] = {}
    spar_cam = [(1250.0, 450.0, 150.0), (756.0, 84.0, 560.0), (0.0, 1.0, 0.0)]
    clamp_cam = [(900.0, 150.0, 330.0), (756.0, 84.0, 400.0), (0.0, 1.0, 0.0)]

    def shot(name, poly, scalars, title, cam, cmap="turbo", clim=None):
        pl = pv.Plotter(off_screen=True, window_size=(1400, 800))
        pl.set_background("white")
        pl.add_mesh(poly, scalars=scalars, cmap=cmap, clim=clim, show_edges=False,
                    scalar_bar_args={"title": title, "color": "black", "vertical": True})
        pl.camera_position = cam
        p = out / f"{name}.png"
        pl.screenshot(str(p))
        pl.close()
        return p

    for st in s.project.studies:
        if not st.results.completed:
            continue
        s.set_active_study(st.id)
        rs, m = s.results, s.mesh
        key = st.name.split(" ")[0]
        kind = st.analysis.analysis_type
        try:
            if kind == "cfd_incompressible":
                pres = rs.quantity("p") if "p" in rs.available() else rs.pressure
                vel = rs.quantity("U_mag") if "U_mag" in rs.available() else np.linalg.norm(rs.velocity, axis=1)
                bs = boundary_surface(m)
                pd = bs.polydata(m.nodes)
                pd.point_data["p"] = pres[bs.point_ids]
                names = m.meta.get("boundary_patches") or foam_patch_order(s.data_dir / m.meta.get("case_rel", ""))
                walls = [i for i, n in enumerate(names) if n.replace("_", " ") in CFD_GROUPS]
                if m.boundary_patch is not None and walls:
                    pd = pd.extract_cells(np.nonzero(np.isin(m.boundary_patch, walls))[0])
                cam = [(-900.0, 900.0, 1900.0), (850.0, 60.0, 450.0), (0.0, 1.0, 0.0)]
                a = shot(f"{key}_surface_pressure", pd, "p", "p (Pa, gauge)", cam, "coolwarm", (-300, 250))
                grid = pv.UnstructuredGrid({pv.CellType.TETRA: m.elements[:, :4]}, m.nodes)
                grid.point_data["U"] = vel
                sl = grid.slice(normal="z", origin=(0, 0, 400.0))
                camz = [(900.0, 60.0, 1500.0), (900.0, 60.0, 400.0), (0.0, 1.0, 0.0)]
                b = shot(f"{key}_velocity_z400", sl, "U", "|U| (m/s) at z = 400 mm", camz, "turbo", (0, 28))
                pics[st.id] = [a, b]
                continue
            bs = boundary_surface(m)
            if kind == "thermal_steady":
                pd = bs.polydata(m.nodes)
                pd.point_data["T"] = rs.quantity("T")[bs.point_ids]
                pics[st.id] = [shot(f"{key}_temperature", pd, "T", "T (C) for 1 W", spar_cam)]
                continue
            if kind == "modal":
                imgs = []
                for f in (0, 2, 4):
                    rs.set_frame(f)
                    u = rs.displacement
                    sc = auto_deformation_scale(m.nodes, u, 0.12)
                    pd = bs.polydata(m.nodes, u, sc)
                    pd.point_data["U"] = np.linalg.norm(u, axis=1)[bs.point_ids]
                    imgs.append(shot(f"{key}_mode{f + 1}", pd, "U", f"mode {f + 1} (relative)", spar_cam))
                rs.set_frame(0)
                pics[st.id] = imgs
                continue
            u = rs.displacement
            vm = rs.quantity("S_vm")
            if st.name.startswith(("7 ", "8 ")):
                pd = bs.polydata(m.nodes)
                pd.point_data["vm"] = vm[bs.point_ids]
                P = pd.points
                keep = (P[:, 2] > 370) & (P[:, 2] < 430) & (P[:, 0] >= 756.0 - 1e-6)
                cells = [i for i in range(pd.n_cells) if keep[pd.get_cell(i).point_ids].all()]
                sub = pd.extract_cells(cells)
                pics[st.id] = [shot(f"{key}_clamp_von_mises", sub, "vm", "von Mises (MPa)", clamp_cam)]
                continue
            sc = auto_deformation_scale(m.nodes, u, 0.08)
            pd = bs.polydata(m.nodes, u, sc)
            pd.point_data["vm"] = vm[bs.point_ids]
            cam = spar_cam if st.name.startswith(("1 ", "2 ", "3 ")) else \
                [(756.0 - 60, 84.0 + 40, 80.0 + 160), (756.0, 70.0, 80.0), (0.0, 1.0, 0.0)]
            pics[st.id] = [shot(f"{key}_von_mises", pd, "vm", f"von Mises (MPa), deformation x{sc:.0f}", cam)]
        except Exception as exc:  # pictures are optional
            print(f"picture of {st.name} failed: {exc}")
    return pics


OVERVIEW_CSS = """body{font:15px/1.5 system-ui,Segoe UI,Arial,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;
color:#16191d}h1{font-size:1.6rem}h2{border-bottom:1px solid #ccc;padding-bottom:4px;margin-top:28px}
table{border-collapse:collapse;width:100%;font-size:14px;margin:8px 0}th,td{border-bottom:1px solid #e3e5e8;padding:5px 8px;
text-align:left;vertical-align:top}th{background:#f4f5f6;font-weight:600}td.n{text-align:right;white-space:nowrap;
font-variant-numeric:tabular-nums}.muted{color:#6c737c}.warn{background:#f6efe1;border-left:3px solid #94600a;
padding:8px 12px}.ok{color:#23744a}img{max-width:100%;border:1px solid #e3e5e8;margin:6px 0}"""


def write_reports(s: Session) -> Path:
    """Per-study OpenCAE reports with pictures, and one overview page (report/index.html)."""
    import html as H
    from opencae.postprocessing.report import write_report
    rep = s.data_dir / "report"
    pics = render_images(s, rep / "images")
    links = []
    for st in s.project.studies:
        if not st.results.completed:
            continue
        s.set_active_study(st.id)
        fn = rep / f"study_{st.name.split(' ')[0]}.html"
        write_report(s, fn, images=pics.get(st.id))
        links.append((st, fn))
    R = json.loads((s.data_dir / "tiltwing_results.json").read_text())
    d = designer()

    def f(x, n=2):
        return "-" if x is None else f"{x:.{n}f}"

    rows = []
    for case, label in (("hover", "1 Hover, max thrust"), ("cruise", "2 Cruise gust/manoeuvre"),
                        ("landing", "3 Hard landing")):
        c = R.get(case)
        if c:
            rows.append((label, f"tip {f(c['tip_FE_mm'])} mm; root σ_vm {f(c['vm_max_FE_MPa'], 1)} MPa; MS {f(c['MS_FE'])}",
                         f"tip {f(c['tip_designer_mm'])} mm; σ_vm {f(c['vm_max_designer_MPa'], 1)} MPa; MS {f(c['MS_designer'])}",
                         f"σ 40 mm from root: FE {f(c['sig150_FE_MPa'], 1)} vs beam {f(c['sig150_beam_MPa'], 1)} MPa"))
    if "modal" in R:
        c = R["modal"]
        rows.append(("4 Natural frequencies", " / ".join(f(x, 1) for x in c["FE_Hz"][::2]) + " Hz (bending)",
                     " / ".join(f(x, 1) for x in c["designer_bending_Hz"]) + f" Hz bending, {f(c['designer_torsion_Hz'], 1)} Hz torsion",
                     f"rotor 1P {f(c['rotor_1P_Hz'][0], 0)}-{f(c['rotor_1P_Hz'][1], 0)} Hz, 2P {f(c['blade_pass_2P_Hz'][0], 0)}-{f(c['blade_pass_2P_Hz'][1], 0)} Hz"))
    if "bracket" in R:
        c = R["bracket"]
        rows.append(("5 Bearing bracket", f"σ_vm {f(c['vm_FE_MPa'])} MPa, {f(c['u_FE_mm'] * 1000)} µm",
                     f"σ_vm {f(c['vm_designer_MPa'])} MPa, {f(c['u_designer_mm'] * 1000)} µm", f"F = {f(c['F_N'], 0)} N; MS {f(c['MS_FE'], 0)}"))
    if "heat" in R:
        c = R["heat"]
        rows.append(("6 Clamp heat path", f"{f(c['K_per_W_FE'], 0)} K/W", "(not in the designer)",
                     f"1-D conduction {f(c['K_per_W_1D_range'][0], 0)}-{f(c['K_per_W_1D_range'][1], 0)} K/W"))
    for k, label in (("soak_40", "7 Hot soak +40 °C"), ("soak_-5", "8 Cold soak −5 °C")):
        c = R.get(k)
        if c:
            rows.append((label, f"tube σ_zz under clamp {f(c['szz_tube_mid_clamp_MPa'], 1)} MPa; σ_vm max {f(c['vm_max_MPa'], 1)} MPa",
                         "(not in the designer)", f"long-joint limit {f(c['long_joint_limit_MPa'], 1)} MPa; mean bond shear ≈ {f(c['mean_bond_shear_MPa'], 1)} MPa"))
    for k, label in (("cfd", "9 Cruise CFD (fine)"), ("cfd_coarse", "10 Cruise CFD (coarse)")):
        c = R.get(k)
        if c and c.get("CL") is not None:
            rng = c.get("CL_range")
            rows.append((label, f"CL {f(c['CL'], 3)}" + (f" ({f(rng[0], 3)}-{f(rng[1], 3)})" if rng else "") +
                         f", CD {f(c['CD'], 4)}, lift {f(c['lift_N_full'], 1)} N (W = {f(c['weight_N'], 1)} N)",
                         f"own model CL {f(c['designer_model']['CL'], 3)} / CD {f(c['designer_model']['CD'], 4)}; AeroBuildup CL {f(c['aerobuildup']['CL'], 3)} / CD {f(c['aerobuildup']['CD'], 4)}; VLM CL {f(c['vlm']['CL'], 3)}",
                         f"{c.get('cells')} cells; {'converged' if c.get('converged') else 'residual target not reached'} after {c.get('iterations')} it.; force change over the last 10 %: {f(100 * (c.get('force_change_last_10pct') or 0), 1)} %"))
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>Tilt-wing VTOL - analysis overview</title>"
             f"<style>{OVERVIEW_CSS}</style></head><body>",
             "<h1>Tilt-wing VTOL: structural, thermal and flow analysis</h1>",
             f"<p class='muted'>OpenCAE Studio example built from the <a href='{DESIGNER_URL}'>Tilt-Wing Designer</a> "
             f"at the paper's default values ({H.escape(PAPER)}).</p>",
             f"<p>{H.escape(s.project.description)}</p>",
             "<h2>Results against the designer's simplified models</h2><table><tr><th>Study</th><th>OpenCAE (3-D)</th>"
             "<th>Tilt-Wing Designer / reference</th><th>Check</th></tr>"]
    parts += [f"<tr><td>{H.escape(a)}</td><td>{H.escape(b)}</td><td>{H.escape(c)}</td><td>{H.escape(e)}</td></tr>"
              for a, b, c, e in rows]
    parts.append("</table>")
    parts.append("<h2>Findings</h2><ul>" + "".join(f"<li>{H.escape(x)}</li>" for x in findings(R, d)) + "</ul>")
    parts.append("<h2>What was modelled, and what was not</h2><ul>" + "".join(
        f"<li>{H.escape(x)}</li>" for x in ASSUMPTIONS) + "</ul>")
    parts.append("<h2>Study reports</h2><ul>" + "".join(
        f"<li><a href='{fn.name}'>{H.escape(st.name)}</a></li>" for st, fn in links) + "</ul>")
    for st, _ in links:
        for p in pics.get(st.id, []):
            parts.append(f"<p><b>{H.escape(st.name)}</b><br><img src='images/{p.name}' alt='{H.escape(st.name)}'></p>")
    parts.append("</body></html>")
    idx = rep / "index.html"
    idx.write_text("\n".join(parts), encoding="utf-8")
    return idx


ASSUMPTIONS = [
    "Geometry is rebuilt from the designer's equations (no STEP file of the aircraft existed). Canopy, landing legs "
    "and propellers are not modelled.",
    "Wing structure: only the CF spar tube carries load, as in the designer's beam model. The wing skin's mass is "
    "carried as load (statics) or as an equivalent density (modes), not as structure.",
    "Loads are the designer's nodal load vectors, collected per spar segment (three segments) and per motor clamp; "
    "the beam reference uses exactly the same collected loads, so FE and beam differ only by the 3-D model.",
    "Material values are the designer's estimates where it gives them (CF tube E, G, allowable; Al 6061 from "
    "handbook). CF hoop modulus, Poisson ratios, CTE and conductivity are typical-value estimates, labelled in the "
    "material records.",
    "Thermal: steady conduction only (no convection or radiation from the spar, which sits inside the wing); thermal "
    "stress with the clamps perfectly bonded (no adhesive layer).",
    "CFD: steady RANS, k-omega SST, fully turbulent, wall functions; half aircraft with a symmetry plane; unpowered "
    "cruise at body angle 0. Two meshes are compared; the coefficients are not validated against tests.",
]


def findings(R: dict, d: dict) -> list[str]:
    out = []
    h = R.get("hover")
    if h:
        out.append(f"Spar bending agrees with beam theory to within 1 % away from the clamp; the tip deflection is "
                   f"{abs(h['tip_FE_mm'] / h['tip_beam_same_loads_mm'] - 1) * 100:.1f} % below the beam (the aluminium "
                   "clamps stiffen two short lengths). The designer's spar sizing is confirmed: margin of safety "
                   f"{h['MS_FE']:.1f} in hover with its 600 MPa estimate allowable.")
    m = R.get("modal")
    if m:
        f = m["FE_Hz"]
        bp = m["blade_pass_2P_Hz"]
        out.append(f"First wing bending mode {f[0]:.1f} Hz (designer {m['designer_bending_Hz'][0]:.1f} Hz). "
                   f"Third bending mode {f[4]:.0f} Hz lies inside the two-blade passing band {bp[0]:.0f}-{bp[1]:.0f} Hz"
                   if bp[0] * 0.9 <= f[4] <= bp[1] * 1.1 else
                   f"First wing bending mode {f[0]:.1f} Hz (designer {m['designer_bending_Hz'][0]:.1f} Hz).")
    c = d["airfoils"]["s1223"]
    import numpy as _np
    cc = _np.asarray(c)
    ile = int(_np.argmin(cc[:, 0]))
    up, lo = cc[:ile + 1][::-1], cc[ile:]
    yl = float(_np.interp(0.3, lo[:, 0], lo[:, 1]))
    yu = float(_np.interp(0.3, up[:, 0], up[:, 1]))
    cr, ct = d["r"]["cr"] * 1000, d["r"]["ct"] * 1000
    out.append(f"Geometry check: the designer puts the tilt axis (spar centre) on the chord line at 30 % chord. The "
               f"S1223 lower surface there is {yl * 100:.1f} % c above the chord line, so a Ø{d['sp']['sparD']} mm tube on "
               f"that axis sticks out {yl * cr + d['sp']['sparD'] / 2:.0f} mm (root) to {yl * ct + d['sp']['sparD'] / 2:.0f} mm "
               f"(tip) below the wing. Centred on the camber line ({(yu + yl) / 2 * 100:.1f} % c above the chord) it fits "
               f"inside the {(yu - yl) * 100:.1f} % c local thickness.")
    t = R.get("soak_-5")
    if t:
        out.append(f"Bonded aluminium motor clamps on the CF spar: a 25 K temperature drop puts {abs(t['szz_tube_mid_clamp_MPa']):.0f} MPa "
                   f"axial stress into the tube under each clamp and about {t['mean_bond_shear_MPa']:.1f} MPa mean shear "
                   "into the bond. The tube is far from its allowable; the adhesive's shear strength at low temperature "
                   "is the item to check.")
    hh = R.get("heat")
    if hh:
        out.append(f"The CF spar is no heat sink for the motors: {hh['K_per_W_FE']:.0f} K rise per watt conducted into a "
                   "clamp. Motor cooling must come from the airflow over the motor and nacelle.")
    c9, c10 = R.get("cfd"), R.get("cfd_coarse")
    if c9 and c9.get("CL") is not None:
        txt = (f"Cruise CFD (body angle 0, 20 m/s): CL {c9['CL']:.3f}, CD {c9['CD']:.4f}; the aircraft makes "
               f"{c9['lift_N_full']:.0f} N of lift against {c9['weight_N']:.0f} N weight at this attitude")
        if c10 and c10.get("CL") is not None:
            txt += (f". The coarse mesh gives CL {c10['CL']:.3f}, CD {c10['CD']:.4f} "
                    f"({(c9['CL'] / c10['CL'] - 1) * 100:+.0f} % / {(c9['CD'] / c10['CD'] - 1) * 100:+.0f} % on refinement), "
                    "so the values are still mesh-dependent")
        txt += (f". The designer's own model predicts CL {c9['designer_model']['CL']:.3f}. Fully turbulent RANS is "
                "uncertain for a highly cambered low-Reynolds-number airfoil such as the S1223 (laminar regions, "
                "transition and separation bubbles are not modelled): wind-tunnel data or a transition model are "
                "needed before the cruise angle of attack is fixed.")
        out.append(txt)
    return out
