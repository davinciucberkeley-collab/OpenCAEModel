"""Level-1 (rapid analytical) engineering calculations on the common model.

Each function returns a dict of :class:`Q` results (SI units) whose evidence class follows from its
inputs (see quantities.derived). Formulas are standard and cited in the function docstrings.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

from opencae.engineering.model import Body, EngineeringModel
from opencae.engineering.quantities import ANALYTICAL, ESTIMATE, MISSING, REDUCED, SUPPLIER, USER, Q, derived, weakest

MM = 1e-3


# --------------------------------------------------------------------------- mass properties
def body_masses(m: EngineeringModel) -> dict[int, float]:
    """Mass of every active body (kg): each component's BOM mass x quantity, spread over its bodies in
    proportion to their volume (closed bodies) or surface area (open bodies such as wires)."""
    out: dict[int, float] = {}
    active = {b.index for b in m.active_bodies()}
    for c in m.active_components():
        bs = [m.bodies[i] for i in c.bodies if i in active]
        if not bs:
            continue
        mass = m.component_mass(c).value or 0.0
        w = np.array([b.volume if b.closed and b.volume > 0 else 0.0 for b in bs])
        if w.sum() <= 0:
            w = np.array([b.area for b in bs])
        if w.sum() <= 0:
            w = np.ones(len(bs))
        for b, f in zip(bs, w / w.sum()):
            out[b.index] = mass * m.effective_qty(c) * f
    return out


def mass_properties(m: EngineeringModel) -> dict[str, Q]:
    """Total mass, centre of gravity and inertia tensor about the CG (aircraft frame, SI).
    Closed bodies carry their own inertia (uniform density); open ones are point masses."""
    bm = body_masses(m)
    extra = []  # BOM mass not attached to geometry (laminating film, spare pack excluded below)
    for c in m.active_components():
        if not c.bodies and c.key not in m.spare_keys:
            extra.append((c, (m.component_mass(c).value or 0.0) * m.effective_qty(c)))
    M = sum(bm.values())
    r = sum(bm[i] * m.bodies[i].centroid for i in bm) * MM
    film = sum(x for _, x in extra)
    # mass-only items: the laminating film is spread like the wing skin (at the wing core's centroid)
    wing = next((b for b in m.bodies if "Wing core" in b.name), None)
    if film > 0 and wing is not None:
        r = r + film * wing.centroid * MM
    pay = m.params["payload"].value or 0.0
    cg_pay = None
    if pay > 0:
        batt = [b for b in m.active_bodies() if b.component.startswith("LiPo")]
        cg_pay = (batt[0].centroid if batt else np.zeros(3)) * MM
        r = r + pay * cg_pay
    Mt = M + film + pay
    cg = r / Mt
    I = np.zeros((3, 3))
    for i, mi in bm.items():
        b = m.bodies[i]
        d = b.centroid * MM - cg
        I += mi * (b.inertia_unit * MM * MM) + mi * (np.dot(d, d) * np.eye(3) - np.outer(d, d))
    if film > 0 and wing is not None:
        d = wing.centroid * MM - cg
        I += film * (wing.inertia_unit * MM * MM + np.dot(d, d) * np.eye(3) - np.outer(d, d))
    if cg_pay is not None:
        d = cg_pay - cg
        I += pay * (np.dot(d, d) * np.eye(3) - np.outer(d, d))
    srcs = [m.component_mass(c).source for c in m.active_components()]
    ref = "BOM masses on the Fusion bodies (volume-weighted), mesh inertia"
    src = weakest([ANALYTICAL] + srcs)
    return {"mass_total": Q(Mt, "kg", src, ref, note=f"{len(bm)} bodies + {film * 1000:.1f} g mass-only items"),
            "cg": Q(cg.tolist(), "m", src, ref), "inertia": Q(I.tolist(), "kg m^2", src, ref),
            "weight": Q(Mt * m.params["g"].value, "N", src, ref)}


# --------------------------------------------------------------------------- geometry checks
def wing_reference(m: EngineeringModel) -> dict[str, Q]:
    """Span, chord, area and leading edge from the wing bodies of the CAD."""
    wb = [b for b in m.bodies if b.group == "airframe" and ("Wing core" in b.name or "Elevon" in b.name)]
    P = np.vstack([m.body_mesh(b)[0] for b in wb])
    span = float(np.ptp(P[:, 1])) * MM
    core = next(b for b in m.bodies if "Wing core" in b.name)
    Pc = m.body_mesh(core)[0]
    le = float(P[:, 0].min()) * MM
    chord = float(np.ptp(P[:, 0])) * MM
    ref = "Fusion bodies: wing core + elevons"
    return {"span": Q(span, "m", ANALYTICAL, ref), "chord": Q(chord, "m", ANALYTICAL, ref),
            "area": Q(span * chord, "m^2", ANALYTICAL, ref + " (rectangular planform)"),
            "x_le": Q(le, "m", ANALYTICAL, ref), "core_te": Q(float(Pc[:, 0].max()) * MM, "m", ANALYTICAL, ref)}


def box_fit(m: EngineeringModel, box=(1.25, 0.55, 0.45)) -> dict[str, Q]:
    """Does the assembled aircraft fit the transport box in some axis-aligned orientation?"""
    P = np.vstack([m.body_mesh(b)[0] for b in m.active_bodies() if not b.component.startswith("Prop ")])
    ext = np.sort(np.ptp(P, axis=0) * MM)[::-1]
    bx = np.sort(np.asarray(box))[::-1]
    ratio = float(np.min(bx / ext))
    # the fins (with feet, clips and motor sets) are clip-on parts: envelope with them taken off as well
    off = ("Fin ", "Foot ", "Fin clip", "Motor tube", "Motor mount", "Motor SunnySky", "Prop ", "Harness motor")
    P2 = np.vstack([m.body_mesh(b)[0] for b in m.active_bodies() if not b.component.startswith(off)
                    and not b.name.startswith(("Wire 18AWG", "Bullet"))])
    ext2 = np.sort(np.ptp(P2, axis=0) * MM)[::-1]
    return {"envelope": Q(ext.tolist(), "m", ANALYTICAL, "bounding box of the active bodies, props excluded "
                          "(props come off for transport)"),
            "box_fit": Q(ratio, "-", ANALYTICAL, f"min(box / envelope), box {box} m", note=">= 1 fits"),
            "envelope_fins_off": Q(ext2.tolist(), "m", ANALYTICAL, "fins, feet, motor sets and props removed"),
            "box_fit_fins_off": Q(float(np.min(bx / ext2)), "-", ANALYTICAL,
                                  "same box, fins and motor sets removed (they are clip-on; C3 asks for one piece)")}


def contact_points(m: EngineeringModel) -> np.ndarray:
    """Ground-contact points when standing nose-up (hover attitude): the aft-most points of the landing
    components (feet), or of the fins when the feet are removed. Aircraft frame, m."""
    land = m.option("landing")
    if land.geometry == "suppress":
        cand = [b for b in m.active_bodies() if b.component.startswith("Fin Depron")]
    else:
        cand = [b for b in m.active_bodies() if b.component == land.bom_component]
    pts = []
    for b in cand:
        P = m.body_mesh(b)[0]
        if land.geometry == "suppress":  # a fin spans above and below the wing: one tip on each side
            for half in (P[P[:, 2] > 0], P[P[:, 2] <= 0]):
                if len(half):
                    pts.append(half[np.argmax(half[:, 0])])
        else:
            pts.append(P[np.argmax(P[:, 0])])
    return np.asarray(pts) * MM


def contact_patch(m: EngineeringModel) -> np.ndarray:
    """All ground-contact vertices when standing: for each landing body, every vertex of its aft face
    (within 0.5 mm of its aft-most x); for bare fin tips, the tip points. Aircraft frame, m."""
    land = m.option("landing")
    if land.geometry == "suppress":
        return contact_points(m)
    pts = []
    for b in m.active_bodies():
        if b.component != land.bom_component:
            continue
        P = m.body_mesh(b)[0]
        pts.append(P[P[:, 0] >= P[:, 0].max() - 0.5])
    return np.vstack(pts) * MM if pts else np.zeros((0, 3))


def tipover(m: EngineeringModel, mp: dict) -> dict[str, Q]:
    """Static tip-over angle standing on the contact points (ground plane normal = -x, nose up):
    the smallest angle between the vertical through the CG and the line from the CG to a support-polygon
    edge, i.e. atan(distance from the CG projection to the edge / CG height)."""
    cg = np.asarray(mp["cg"].value)
    pts = contact_patch(m)
    if len(pts) < 3:
        return {"tipover_deg": Q(None, "deg", MISSING, "fewer than 3 contact points")}
    x_ground = pts[:, 0].max()
    h = x_ground - cg[0]
    from scipy.spatial import ConvexHull
    pts = pts[pts[:, 0] >= x_ground - 1e-3]  # only what touches the ground plane
    yz = pts[:, 1:]
    c = cg[1:]
    hull = ConvexHull(yz)
    # signed distance from the CG projection to each hull edge (positive inside); the smallest one governs
    dmin = min(-(eq[0] * c[0] + eq[1] * c[1] + eq[2]) for eq in hull.equations)
    tip = math.degrees(math.atan2(dmin, h))  # negative when the CG is outside the support polygon
    lean = m.params["gust_lean_deg"]
    return {"tipover_deg": derived(tip, "deg", "support polygon of the contact points vs CG height",
                                   {"cg": mp["cg"]}),
            "cg_height_standing": derived(h, "m", "contact plane to CG", {"cg": mp["cg"]}),
            "tipover_margin": derived(tip - lean.value, "deg", "tip-over angle - lean in the 8 m/s gust",
                                      {"cg": mp["cg"], "lean": lean})}


def static_margin(m: EngineeringModel, mp: dict, wr: dict) -> dict[str, Q]:
    """CG position in % chord from the wing leading edge; static margin = neutral point - CG (both in
    chord fractions). The neutral point is a provisional estimate (no aero data)."""
    cg = np.asarray(mp["cg"].value)
    x_c = (cg[0] - wr["x_le"].value) / wr["chord"].value
    npq = m.params["np_frac"]
    return {"cg_frac": derived(x_c, "-", "CG aft of the leading edge / chord", {"cg": mp["cg"], "c": wr["chord"]}),
            "static_margin": derived(npq.value - x_c, "-", "neutral point - CG", {"np": npq, "cg": mp["cg"]})}


# --------------------------------------------------------------------------- propulsion and electrical
def motor_point(m: EngineeringModel, V: float, throttle: float = 1.0, thrust_req: Optional[float] = None,
                rho: Optional[float] = None) -> dict:
    """Motor-propeller operating point. Motor: V_m = I R_m + w / K_v, torque (I - I0) / K_v (SI K_v in
    rad/s/V). Prop (static): T = C_T rho n^2 D^4, P = C_P rho n^3 D^5, torque = P / w. Either full
    throttle at supply V (voltage seen by the motor = throttle x V) or the throttle that gives
    ``thrust_req``. Returns floats (SI)."""
    mot, prop = m.option("motor"), m.option("prop")
    kv = mot.q("kv").value * 2 * math.pi / 60.0
    rm, i0 = mot.q("rm").value, mot.q("i0").value
    D, ct, cp = prop.q("diameter").value, prop.q("ct0").value, prop.q("cp0").value
    rho = rho or m.params["rho"].value

    def at_speed(w):
        n = w / (2 * math.pi)
        T = ct * rho * n * n * D ** 4
        Pp = cp * rho * n ** 3 * D ** 5
        Qp = Pp / max(w, 1e-9)
        I = Qp * kv + i0
        Vm = I * rm + w / kv
        return T, Pp, I, Vm

    if thrust_req is not None:
        n = math.sqrt(thrust_req / (ct * rho * D ** 4))
        w = 2 * math.pi * n
    else:
        lo, hi = 0.0, kv * V * throttle
        for _ in range(80):
            w = 0.5 * (lo + hi)
            _, _, _, Vm = at_speed(w)
            lo, hi = (w, hi) if Vm < V * throttle else (lo, w)
        w = 0.5 * (lo + hi)
    T, Pp, I, Vm = at_speed(w)
    return {"thrust": T, "rpm": w * 60 / (2 * math.pi), "current": I, "p_shaft": Pp, "p_elec": Vm * I,
            "v_motor": Vm, "throttle": Vm / V if V > 0 else float("nan"), "eff": Pp / max(Vm * I, 1e-9)}


def propulsion(m: EngineeringModel, mp: dict) -> dict[str, Q]:
    """Hover and full-throttle operating points (4 motors), with battery sag through its internal
    resistance; thrust-to-weight at full and low battery voltage; disk loading; hover endurance."""
    mot, prop, bat, esc = (m.option(k) for k in ("motor", "prop", "battery", "esc"))
    W = mp["weight"].value
    n_mot = 4
    vfull, vlow, rint = bat.q("v_full").value, bat.q("v_low").value, bat.q("r_int").value
    cap = m.params["throttle_max"].value or 1.0
    hov = motor_point(m, vfull, thrust_req=W / n_mot)
    i_h = hov["current"] * n_mot
    v_h = bat.q("v_nom").value
    res = {}
    full = []
    for tag, v0 in (("full", vfull), ("low", vlow)):
        v = v0
        for _ in range(30):  # sag: battery voltage under the full-throttle battery current (ESC eta 0.95)
            op = motor_point(m, v, cap)  # full stick = the flight controller's output limit
            v = v0 - op["p_elec"] * n_mot / (0.95 * max(v, 1e-9)) * rint
        full.append((tag, op, v))
    ins = {"kv": mot.q("kv"), "rm": mot.q("rm"), "ct": prop.q("ct0"), "cp": prop.q("cp0"), "r_int": bat.q("r_int"),
           "mass": mp["mass_total"], "throttle_max": m.params["throttle_max"]}
    ref = "motor (K_v, R_m, I_0) + static prop (C_T, C_P) operating point, 4 motors" + (
        f", motor output limited to {cap:.2f}" if cap < 1.0 else "")
    for tag, op, v in full:
        res[f"thrust_max_{tag}"] = derived(op["thrust"] * n_mot, "N", ref + f", battery {tag} under load", ins)
        res[f"tw_{tag}"] = derived(op["thrust"] * n_mot / W, "-", ref, ins)
        res[f"i_motor_max_{tag}"] = derived(op["current"], "A", ref, ins)
        res[f"rpm_max_{tag}"] = derived(op["rpm"], "rpm", ref, ins)
        res[f"v_batt_max_{tag}"] = derived(v, "V", "battery under full-throttle current", ins)
    i_max = max(op["current"] for _, op, _ in full)
    eta_esc = 0.95  # ESC efficiency (working value, as in the flight simulation)
    i_bat_h = hov["p_elec"] * n_mot / (eta_esc * max(v_h, 1e-9))
    for _ in range(20):  # battery voltage under the hover current
        v_h = bat.q("v_nom").value - i_bat_h * rint
        i_bat_h = hov["p_elec"] * n_mot / (eta_esc * v_h)
    res["i_hover_motor"] = derived(hov["current"], "A", ref + ", hover", ins)
    res["i_hover_total"] = derived(i_h, "A", ref + ", hover: sum of the motor (phase-side) currents", ins)
    res["i_hover_battery"] = derived(i_bat_h, "A", "hover electrical power / (ESC efficiency 0.95 x battery "
                                     "voltage)", ins)
    res["p_hover_elec"] = derived(hov["p_elec"] * n_mot, "W", ref + ", hover", ins)
    res["hover_throttle"] = derived(hov["v_motor"] / v_h, "-", "motor voltage / battery voltage at hover", ins)
    # control headroom at the end of the battery: limit / hover throttle at low voltage under the hover load
    v_hl = vlow - i_bat_h * rint
    res["throttle_headroom_low"] = derived(cap * v_hl / max(hov["v_motor"], 1e-9), "-",
                                           "throttle limit / hover throttle, battery at v_low under hover load "
                                           "(> 1.3 leaves attitude-control authority)", ins)
    res["p_hover_battery"] = derived(i_bat_h * v_h, "W", "battery power at hover", ins)
    res["rpm_hover"] = derived(hov["rpm"], "rpm", ref, ins)
    A = n_mot * math.pi * prop.q("diameter").value ** 2 / 4
    res["disk_loading"] = derived(W / A, "N/m^2", "weight / total disk area", {"mass": mp["mass_total"]})
    # momentum-theory cross-check of the hover power with the figure of merit
    rho = m.params["rho"].value
    p_ideal = W ** 1.5 / math.sqrt(2 * rho * A)
    res["p_hover_momentum"] = derived(p_ideal / m.params["fm"].value / 0.85, "W",
                                      "momentum theory / FM / motor+ESC efficiency 0.85 (cross-check)",
                                      {"fm": m.params["fm"], "mass": mp["mass_total"]}, kind=REDUCED)
    cap = bat.q("capacity").value * bat.q("usable").value
    res["endurance_hover"] = derived(cap / i_bat_h, "s", "usable capacity / hover battery current", ins)
    # limits
    res["motor_current_margin"] = derived(mot.q("imax").value / i_max, "-", "motor 30-s max current / full-throttle "
                                          "current", {**ins, "imax": mot.q("imax")})
    res["esc_margin"] = derived(esc.q("i_cont").value / max(mot.q("imax").value, i_max), "-",
                                "ESC per-channel rating / max(motor rating, full-throttle current)",
                                {**ins, "esc": esc.q("i_cont"), "imax": mot.q("imax")})
    p_max = max(op["p_elec"] for _, op, _ in full)
    res["motor_power_margin"] = derived(mot.q("pmax").value / p_max, "-", "motor max power / full-throttle "
                                        "electrical power", {**ins, "pmax": mot.q("pmax")})
    c_max = bat.q("c_rate").value * bat.q("capacity").value / 3600.0
    i_bat_max = max(op["p_elec"] * n_mot / (0.95 * v) for _, op, v in full)
    res["i_battery_max"] = derived(i_bat_max, "A", "full-throttle battery current (ESC eta 0.95)", ins)
    res["battery_c_margin"] = derived(c_max / i_bat_max, "-", "advertised C x capacity / full-throttle battery current",
                                      {**ins, "c": bat.q("c_rate")})
    cells = bat.q("cells").value
    ok = (cells <= mot.q("cells_max").value and esc.q("cells_min").value <= cells <= esc.q("cells_max").value)
    res["cells_ok"] = derived(1.0 if ok else 0.0, "-", f"battery {cells:.0f}S vs motor <= "
                              f"{mot.q('cells_max').value:.0f}S, ESC {esc.q('cells_min').value:.0f}-"
                              f"{esc.q('cells_max').value:.0f}S",
                              {"m": mot.q("cells_max"), "e": esc.q("cells_max"), "b": bat.q("cells")})
    # thermal: dissipation in each motor (copper + no-load) and in the battery
    op_max = max((op for _, op, _ in full), key=lambda o: o["current"])
    for tag, op in (("hover", hov), ("max", op_max)):
        I = op["current"]
        emf = op["v_motor"] - I * mot.q("rm").value
        res[f"motor_loss_{tag}"] = derived(I * I * mot.q("rm").value + mot.q("i0").value * emf, "W",
                                           "I^2 R_m + I_0 x back-EMF per motor", ins)
    res["battery_heat_hover"] = derived(i_bat_h * i_bat_h * rint, "W", "I^2 R_int at hover (battery current)", ins)
    return res


def cruise(m: EngineeringModel, mp: dict, wr: dict) -> dict[str, Q]:
    """Reduced-order cruise estimate: speed for minimum power (parabolic polar C_D = C_D0 + C_L^2 /
    (pi A e)), power through a cruise propeller efficiency, and the mission energy check."""
    rho, W = m.params["rho"].value, mp["weight"].value
    S, b = wr["area"].value, wr["span"].value
    AR = b * b / S
    cd0, e = m.params["cd0"].value, m.params["oswald"].value
    k = 1 / (math.pi * AR * e)
    cl_mp = math.sqrt(3 * cd0 / k)  # minimum power
    clmax = m.params["cl_max"].value
    cl = min(cl_mp, clmax / 1.2 ** 2)  # keep a 1.2 V_stall margin
    V = math.sqrt(2 * W / (rho * S * cl))
    cd = cd0 + k * cl * cl
    D = 0.5 * rho * V * V * S * cd
    P = D * V / m.params["eta_prop_cruise"].value / 0.85
    ins = {"cd0": m.params["cd0"], "e": m.params["oswald"], "eta": m.params["eta_prop_cruise"],
           "mass": mp["mass_total"], "S": wr["area"], "clmax": m.params["cl_max"]}
    bat = m.option("battery")
    v = bat.q("v_nom").value
    e_avail = bat.q("capacity").value * bat.q("usable").value * v
    prop = propulsion(m, mp)
    e_need = prop["p_hover_battery"].value * m.params["hover_min"].value + P * m.params["cruise_min"].value
    ref = ("parabolic polar, min-power speed" + (" limited to 1.2 V_stall" if cl < cl_mp else "")
           + ", eta_prop x 0.85")
    return {"v_cruise": derived(V, "m/s", ref, ins, kind=REDUCED),
            "cl_cruise": derived(cl, "-", ref, ins, kind=REDUCED),
            "p_cruise_elec": derived(P, "W", ref, ins, kind=REDUCED),
            "ld_cruise": derived(cl / cd, "-", ref, ins, kind=REDUCED),
            "endurance_cruise": derived(e_avail / P, "s", "usable energy / cruise power", ins, kind=REDUCED),
            "mission_margin": derived(e_avail / e_need, "-", "usable energy / (2 min hover + 5 min cruise)",
                                      {**ins, "hover": prop["p_hover_battery"]}, kind=REDUCED),
            "aspect_ratio": derived(AR, "-", "span^2 / area", {"S": wr["area"]})}


def landing_impact(m: EngineeringModel, mp: dict, v_touch: float = 1.0) -> dict[str, Q]:
    """Energy method for a vertical touchdown at ``v_touch`` (m/s): average deceleration over the
    stroke of the landing contact, n = 1 + v^2 / (2 g s). Stroke is a provisional estimate per option."""
    land = m.option("landing")
    s = land.q("stroke")
    g = m.params["g"].value
    if s.missing:
        return {"landing_n": Q(None, "-", MISSING, "no stroke for this landing option")}
    n = 1 + v_touch ** 2 / (2 * g * s.value)
    F = n * mp["weight"].value
    ins = {"stroke": s, "mass": mp["mass_total"]}
    out = {}
    sig = land.q("petg_strength")
    if land.geometry == "modify" and not sig.missing:
        # outrigger root: the whole touchdown load on one foot (tilted touchdown), acting at the foot's outer
        # edge, bending the foot section at the fin tip (cantilever, rectangular section of the foot body)
        fin = [b for b in m.bodies if b.component.startswith("Fin Depron")]
        z_tip = max(float(np.abs(m.body_mesh(b)[0][:, 2]).max()) for b in fin) * MM
        feet = [b for b in m.active_bodies() if b.component == land.bom_component and "PETG" in b.name]
        P = m.body_mesh(feet[0])[0] * MM
        arm = float(np.abs(P[:, 2]).max()) - z_tip
        t_fin = float(np.ptp(m.body_mesh(fin[0])[0][:, 1])) * MM
        bw, hx = float(np.ptp(P[:, 1])) - t_fin, float(np.ptp(P[:, 0]))  # fin slot taken out (conservative)
        Z = bw * hx * hx / 6.0
        stress = F * max(arm, 0.0) / Z
        ins2 = {**ins, "petg": sig}
        out = {"foot_overhang": derived(arm, "m", "foot outer edge beyond the fin tip (CAD)", {}),
               "foot_root_stress": derived(stress, "Pa", f"cantilever {arm * 1000:.0f} mm, section "
                                           f"{bw * 1000:.0f} (walls, slot removed) x {hx * 1000:.0f} mm, whole touchdown load "
                                           f"on one foot",
                                           ins2, kind=REDUCED),
               "foot_sf": derived(sig.value / max(stress, 1e-9), "-", "PETG strength (estimate) / root stress",
                                  ins2, kind=REDUCED)}
    return {**out,
            "landing_n": derived(n, "g", f"energy method, {v_touch} m/s touchdown, constant deceleration", ins,
                                 kind=REDUCED),
            "landing_force_total": derived(F, "N", "n x weight", ins, kind=REDUCED),
            "landing_force_per_contact": derived(F / 4, "N", "n x weight / 4 (level touchdown)", ins, kind=REDUCED)}


def costs(m: EngineeringModel) -> dict[str, Q]:
    """BOM cost per group and in total, tax, contingency, reserve against the budget. Prices marked
    ESTIMATE in the BOM keep that class."""
    by_group: dict[str, float] = {}
    srcs = []
    for c in m.active_components():
        p = m.component_price(c)
        if p.value is None:
            srcs.append(MISSING)
            continue
        by_group[c.group] = by_group.get(c.group, 0.0) + p.value * m.effective_qty(c)
        srcs.append(p.source)
    parts = sum(by_group.values())
    tax = parts * m.params["tax_rate"].value
    cont = parts * m.params["contingency"].value
    ship = m.params["shipping"]
    total = parts + tax + cont + (ship.value or 0.0)
    src = weakest(srcs + [ANALYTICAL])
    res = {f"cost_{g}": Q(v, "USD", src, "BOM lines of this group") for g, v in by_group.items()}
    note = "shipping missing (not included)" if ship.missing else ""
    tsrc = MISSING if ship.missing else src  # an unknown cost item makes the total incomplete, never zero
    res.update({"cost_parts": Q(parts, "USD", src, "sum of BOM lines (qty x unit price)"),
                "cost_tax": Q(tax, "USD", src, "parts x tax rate"),
                "cost_contingency": Q(cont, "USD", src, "parts x contingency"),
                "cost_total": Q(total, "USD", tsrc, "parts + tax + contingency + shipping", note=note),
                "reserve": Q(m.params["budget"].value - total, "USD", tsrc, "budget - total", note=note)})
    n_est = sum(1 for c in m.active_components() if m.component_price(c).source == ESTIMATE)
    res["cost_estimated_lines"] = Q(float(n_est), "-", ANALYTICAL, "BOM lines whose price is an estimate")
    return res


def evaluate_all(m: EngineeringModel) -> dict[str, Q]:
    """All level-1 results for the current configuration (used by the dependency engine)."""
    mp = mass_properties(m)
    wr = wing_reference(m)
    out = dict(mp)
    out.update(wr)
    out.update(box_fit(m))
    out.update(tipover(m, mp))
    out.update(static_margin(m, mp, wr))
    out.update(propulsion(m, mp))
    out.update(cruise(m, mp, wr))
    out.update(landing_impact(m, mp))
    out.update(costs(m))
    return out
