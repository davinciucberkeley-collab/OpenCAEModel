"""Propulsion: motor and propeller in equilibrium, thrust at full throttle, the hover point and the endurance.

Model (all rotors identical):
  propeller   thrust  T = ct * rho * n^2 * D^4        power  P = cp * rho * n^3 * D^5      (n in rev/s, D in m)
  motor       back-EMF Ve = n * 60 / kv              current I = (Vm - Ve) / R              shaft power (I - i0) * Ve
  equilibrium shaft power = propeller power at the motor voltage Vm = throttle * V_batt (solved by bisection)
  battery     V_batt = cells * 3.7 - I_batt * R_int,   I_batt = rotors * throttle * I_motor / esc_efficiency
Hover = the throttle at which the total thrust equals the weight. Endurance = usable capacity / hover current.
"""
from __future__ import annotations

import math

import numpy as np

from opencae.drone import spec as S
from opencae.drone.types import (CONDITIONAL, FAIL, OK, WATCH, RunContext, metric, not_evaluated, result)

ID = "propulsion"
TITLE = "Propulsion"
VERSION = "1"
DEPENDS = ["mass"]
NEEDS = ["geometry.units", "geometry.forward", "geometry.up", "roles", "type", "environment",
         "propulsion", "mass.payload_g", "mission.payload_g"]

V_CELL = 3.7  # nominal LiPo cell voltage

_REQUIRED = ["propulsion.motor.kv", "propulsion.motor.r_ohm", "propulsion.motor.i0_a", "propulsion.motor.i_max_a",
             "propulsion.prop.diameter_in", "propulsion.prop.ct", "propulsion.prop.cp",
             "propulsion.battery.cells", "propulsion.battery.capacity_mah", "propulsion.battery.c_rating",
             "propulsion.battery.r_int_mohm_per_cell", "propulsion.battery.usable_fraction",
             "propulsion.esc.i_cont_a", "propulsion.esc.efficiency"]
_THRUST_KEYS = ["propulsion.motor.kv", "propulsion.motor.r_ohm", "propulsion.motor.i0_a", "propulsion.prop.diameter_in",
                "propulsion.prop.ct", "propulsion.prop.cp", "propulsion.battery.cells",
                "propulsion.battery.r_int_mohm_per_cell", "propulsion.esc.efficiency"]


def motor_prop(Vm, kv, R, i0, ct, cp, D, rho):
    """Equilibrium of one motor and propeller at motor voltage Vm: (n rev/s, motor current A, thrust N)."""
    ve_max = Vm - i0 * R
    if ve_max <= 0:
        return 0.0, 0.0, 0.0
    k = 60.0 / kv
    pp = lambda n: cp * rho * n ** 3 * D ** 5

    def f(n):
        ve = n * k
        return ((Vm - ve) / R - i0) * ve - pp(n)

    hi = ve_max / k
    lo = hi * 1e-9
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    n = 0.5 * (lo + hi)
    return n, (Vm - n * k) / R, ct * rho * n ** 2 * D ** 4


def system(throttle, N, kv, R, i0, ct, cp, D, rho, cells, r_int, eff):
    """All rotors at one throttle with the loaded battery: dict(n, i_motor, thrust, i_batt, v_batt, p_elec)."""
    v = cells * V_CELL
    for _ in range(200):
        n, im, T = motor_prop(throttle * v, kv, R, i0, ct, cp, D, rho)
        ib = N * throttle * im / eff
        v_new = cells * V_CELL - ib * r_int
        if abs(v_new - v) < 1e-10:
            v = v_new
            break
        v = 0.5 * v + 0.5 * v_new
    return {"n": n, "i_motor": im, "thrust": T, "i_batt": ib, "v_batt": v, "p_elec": v * ib}


def _rotors(ctx: RunContext, type_: str):
    r = (ctx.spec.get("propulsion") or {}).get("rotors", "auto")
    if isinstance(r, list) and r:
        return [{"position_mm": list(map(float, x["position_mm"])), "axis": x.get("axis", [0, 0, 1]),
                 "spin": x.get("spin", "cw")} for x in r]
    motors = ctx.aircraft.role_parts("motor")
    axis = [1, 0, 0] if type_ in ("tailsitter", "fixed_wing") else [0, 0, 1]
    order = sorted(range(len(motors)), key=lambda i: math.atan2(motors[i].centroid[1], motors[i].centroid[0]))
    out = [None] * len(motors)
    for rank, i in enumerate(order):
        out[i] = {"position_mm": [round(float(x), 3) for x in motors[i].centroid], "axis": axis,
                  "spin": "ccw" if rank % 2 == 0 else "cw"}
    return out


def run(ctx: RunContext) -> dict:
    spec = ctx.spec
    type_ = spec.get("type", "multirotor")
    missing = [k for k in _REQUIRED if S.param(spec, k) is None]
    rotors = _rotors(ctx, type_)
    if not rotors:
        missing.append("propulsion.rotors")
    if missing:
        why = "Not evaluated: missing inputs: " + ", ".join(missing) + "."
        if "propulsion.rotors" in missing:
            why += (" No part has the role 'motor', so the rotors cannot be placed automatically: name the motors "
                    "or list propulsion.rotors.")
        return not_evaluated(ID, TITLE, missing, why)
    v = lambda k: float(S.param(spec, k).value)
    kv, R, i0, imax = v("propulsion.motor.kv"), v("propulsion.motor.r_ohm"), v("propulsion.motor.i0_a"), \
        v("propulsion.motor.i_max_a")
    D = v("propulsion.prop.diameter_in") * 0.0254
    ct, cp = v("propulsion.prop.ct"), v("propulsion.prop.cp")
    cells, cap, crate = v("propulsion.battery.cells"), v("propulsion.battery.capacity_mah"), \
        v("propulsion.battery.c_rating")
    r_int = v("propulsion.battery.r_int_mohm_per_cell") * cells / 1000.0
    usable, icont, eff = v("propulsion.battery.usable_fraction"), v("propulsion.esc.i_cont_a"), \
        v("propulsion.esc.efficiency")
    rho = float(S.val(spec, "environment.rho", 1.225))
    g = float(S.val(spec, "environment.g", 9.80665))
    N = len(rotors)
    args = (N, kv, R, i0, ct, cp, D, rho, cells, r_int, eff)
    ctx.progress("propulsion", 0.3)
    full = system(1.0, *args)
    t_max = full["thrust"]
    b_thrust = S.basis(spec, _THRUST_KEYS)
    b_hover = S.basis(spec, _THRUST_KEYS + ["propulsion.battery.capacity_mah", "propulsion.battery.usable_fraction"])
    m: dict = {}
    i_margin = imax / max(full["i_motor"], 1e-9)
    esc_margin = icont / max(full["i_motor"], 1e-9)
    bat_margin = (cap / 1000.0 * crate) / max(full["i_batt"], 1e-9)
    m["rotor_count"] = metric(N, "", OK, "", "Number of rotors")
    m["thrust_max_n"] = metric(t_max, "N", OK, b_thrust, "Thrust of one rotor at full throttle (static)")
    m["thrust_total_max_n"] = metric(t_max * N, "N", OK, b_thrust, "Total thrust at full throttle (static)")
    m["full_throttle_current_a"] = metric(full["i_batt"], "A", OK, b_thrust, "Battery current at full throttle")
    m["motor_current_margin"] = metric(i_margin, "", OK if i_margin >= 1 else FAIL, b_thrust,
                                       "Motor rating divided by motor current at full throttle")
    m["esc_current_margin"] = metric(esc_margin, "", OK if esc_margin >= 1 else FAIL, b_thrust,
                                     "Speed-controller rating divided by motor current at full throttle")
    m["battery_current_margin"] = metric(bat_margin, "", OK if bat_margin >= 1 else FAIL, b_thrust,
                                         "Battery rating (capacity x C) divided by current at full throttle")
    extra = {"rotors": rotors, "full_throttle": {k: float(x) for k, x in full.items()},
             "prop_diameter_m": D, "rpm_full": full["n"] * 60.0}
    mass = (ctx.previous.get("mass") or {}).get("metrics", {}).get("mass_kg")
    hover_ok = type_ != "fixed_wing"
    summary = (f"At full throttle each rotor pulls {t_max:.1f} N ({t_max * N:.0f} N in total, "
               f"{full['i_batt']:.0f} A from the battery).")
    hover_note = ""
    if hover_ok and mass and mass.get("value"):
        W = mass["value"] * g
        b_h = S.weakest([b_hover, mass.get("basis") or "estimate"])
        m["tw_ratio"] = metric(t_max * N / W, "", OK if t_max * N / W >= 1 else FAIL, b_h,
                               "Thrust to weight at full throttle")
        if t_max * N < W:
            m["hover_throttle"] = metric(None, "", FAIL, b_h, "Throttle needed to hover")
            summary += " The thrust is lower than the weight, so the aircraft cannot hover."
        else:
            lo, hi = 0.0, 1.0
            for _ in range(60):
                mid = 0.5 * (lo + hi)
                if N * system(mid, *args)["thrust"] >= W:
                    hi = mid
                else:
                    lo = mid
            h = system(hi, *args)
            end_min = cap / 1000.0 * usable / max(h["i_batt"], 1e-9) * 60.0
            m["hover_throttle"] = metric(hi, "", OK if hi <= 0.7 else WATCH, b_h, "Throttle needed to hover")
            m["hover_current_a"] = metric(h["i_batt"], "A", OK, b_h, "Battery current in hover")
            m["hover_power_w"] = metric(h["p_elec"], "W", OK, b_h, "Electrical power in hover")
            m["hover_endurance_min"] = metric(end_min, "min", OK, b_h, "Hover time on the usable battery")
            extra["hover"] = {k: float(x) for k, x in h.items()}
            summary += (f" It hovers at {hi * 100:.0f} % throttle using {h['i_batt']:.1f} A, which gives about "
                        f"{end_min:.0f} minutes on the usable battery.")
    elif hover_ok:
        hover_note = " Hover figures need the mass analysis, which is not available."
    status = FAIL if any(x["status"] == FAIL for x in m.values()) else \
        CONDITIONAL if "estimate" in {x["basis"] for x in m.values()} else OK
    if b_thrust == "estimate":
        summary += " These numbers rest on typical values, not datasheets."
    return result(ID, TITLE, status, summary + hover_note, metrics=m, extra=extra)
