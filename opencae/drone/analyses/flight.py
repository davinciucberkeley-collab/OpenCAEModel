"""Hover flight simulation of a rotorcraft: a compact 6-DOF rigid-body model with attitude and position hold.

Standalone numpy code (RK4, dt 2 ms, 8 s). Inputs come from the mass and propulsion analyses. At t = 1 s the
attitude is kicked by ``analyses.flight.disturbance_deg`` (default 15 deg roll) and the controller has to
bring the aircraft back to its hover point.

Frame: x forward, y LEFT, z up (right-handed). This is the aircraft frame of the project, so nothing is mirrored.
Outputs (pos, att) are in that frame.

Estimates (stated, not measured): motor lag 0.05 s, rotor reaction-torque ratio k_q = cp / (2 pi ct) * D,
thrust proportional to the commanded thrust fraction, no aerodynamic drag, no wind.
"""
from __future__ import annotations

import math

import numpy as np

from opencae.drone import types as T

ID = "flight"
TITLE = "Hover flight"
NEEDS = ["type", "analyses.flight.disturbance_deg", "propulsion.rotors", "propulsion.prop.diameter_in",
         "propulsion.prop.ct", "propulsion.prop.cp", "environment.g"]

DT = 0.002
T_END = 8.0
T_KICK = 1.0
MOTOR_LAG_S = 0.05
DEFAULT_DISTURBANCE_DEG = 15.0
G_DEFAULT = 9.80665
MAX_SAMPLES = 400
SETTLE_TILT_DEG = 2.0
SETTLE_POS_M = 0.15


# ----------------------------------------------------------------------------------------------- inputs
def _leaf(node):
    """(value, status) of a bare number or a {value, ...} dict; (None, '') when absent."""
    if isinstance(node, dict):
        return node.get("value"), node.get("status", T.ESTIMATE)
    if node is None:
        return None, ""
    return node, T.ESTIMATE


def _p(spec: dict, dotted: str):
    cur = spec
    for k in dotted.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None, ""
        cur = cur[k]
    return _leaf(cur)


def _num(spec, dotted):
    v, s = _p(spec, dotted)
    try:
        return (float(v), s) if v is not None else (None, s)
    except (TypeError, ValueError):
        return None, s


def _mv(res: dict, name: str):
    m = ((res or {}).get("metrics") or {}).get(name)
    return m.get("value") if isinstance(m, dict) else m


def _rot_from_to(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    v, c = np.cross(a, b), float(a @ b)
    if np.linalg.norm(v) < 1e-9:
        if c > 0:
            return np.eye(3)
        ax = np.eye(3)[int(np.argmin(np.abs(a)))]
        v = np.cross(a, ax)
        v /= np.linalg.norm(v)
        return 2 * np.outer(v, v) - np.eye(3)
    K = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + K + K @ K / (1 + c)


# --------------------------------------------------------------------------------------------- rigid body
def _q_mul(a, b):
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return np.array([w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2, w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                     w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2, w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2])


def _q_rot(q) -> np.ndarray:
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


def _q_axis_angle(ax, ang):
    ax = np.asarray(ax, float) / np.linalg.norm(ax)
    return np.concatenate([[math.cos(ang / 2)], math.sin(ang / 2) * ax])


def _euler(q) -> tuple:
    w, x, y, z = q
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return roll, pitch, yaw


class _Model:
    def __init__(self, mass, inertia, pos, axes, spin, tmax, kq, g, tau):
        self.m, self.I, self.Iinv = mass, inertia, np.linalg.inv(inertia)
        self.r, self.a, self.tmax, self.g, self.tau = pos, axes, tmax, g, tau
        self.n = len(pos)
        # one column per rotor: force (3) and moment (3) per newton of thrust
        self.F = axes.T                                            # (3, n)
        self.M = (np.cross(pos, axes) + (spin * kq)[:, None] * axes).T  # (3, n)
        B = np.vstack([self.F[2:3], self.M])                       # thrust-z, roll, pitch, yaw
        self.B = B
        self.rank = int(np.linalg.matrix_rank(B))
        self.mix = np.linalg.pinv(B)                               # (n, 4)

    def deriv(self, s, u_thrust):
        """State s = [pos3, vel3, quat4, omega3, thrust n]; u_thrust = commanded thrust (N) per rotor."""
        n = self.n
        v, q, w, th = s[3:6], s[6:10], s[10:13], s[13:13 + n]
        R = _q_rot(q)
        force = R @ (self.F @ th) + np.array([0.0, 0.0, -self.m * self.g])
        mom = self.M @ th
        dw = self.Iinv @ (mom - np.cross(w, self.I @ w))
        wq = np.concatenate([[0.0], w])
        dq = 0.5 * _q_mul(q, wq)
        return np.concatenate([v, force / self.m, dq, dw, (u_thrust - th) / self.tau])


class _Controller:
    """Cascade: position P -> velocity P -> tilt command; angle P -> rate PID -> torque; mixer pseudo-inverse."""

    def __init__(self, mdl: _Model, tau: float):
        self.mdl = mdl
        self.k_pos, self.k_vel = 1.0, 2.0
        self.k_ang = np.array([7.0, 7.0, 3.0])
        w_r = 1.0 / (2.5 * tau)
        self.kp_rate = np.array([w_r, w_r, 0.6 * w_r])
        self.ki_rate = self.kp_rate * 0.5
        self.kd_rate = self.kp_rate * 0.02
        self.int_rate = np.zeros(3)
        self.int_vel = np.zeros(3)
        self.prev_w = np.zeros(3)
        self.dfilt = np.zeros(3)
        self.tilt_max = math.radians(25)

    def step(self, s, dt):
        mdl = self.mdl
        p, v, q, w = s[0:3], s[3:6], s[6:10], s[10:13]
        # position hold: velocity setpoint, then horizontal acceleration command -> tilt command
        v_sp = -self.k_pos * p
        v_sp = np.clip(v_sp, -2.0, 2.0)
        self.int_vel = np.clip(self.int_vel + (v_sp - v) * dt * 0.3, -1.0, 1.0)
        a_cmd = self.k_vel * (v_sp - v) + self.int_vel
        # desired thrust direction in the world frame
        f_des = np.array([a_cmd[0], a_cmd[1], mdl.g + a_cmd[2]])
        z_des = f_des / np.linalg.norm(f_des)
        ang = math.acos(min(1.0, z_des[2]))
        if ang > self.tilt_max:
            hor = z_des[:2] / max(np.linalg.norm(z_des[:2]), 1e-9)
            z_des = np.array([hor[0] * math.sin(self.tilt_max), hor[1] * math.sin(self.tilt_max),
                              math.cos(self.tilt_max)])
        R = _q_rot(q)
        zb = R[:, 2]
        # attitude error (small-rotation vector in the body frame): tilt from the thrust axes, yaw from heading
        e_tilt = np.cross(zb, z_des)
        e_b = R.T @ e_tilt
        xb = R[:, 0]
        yaw_err = math.atan2(xb[1], xb[0])
        e_b[2] = -yaw_err
        w_sp = self.k_ang * e_b
        err = w_sp - w
        self.int_rate = np.clip(self.int_rate + err * dt, -2.0, 2.0)
        dw_meas = (w - self.prev_w) / dt
        self.prev_w = w.copy()
        self.dfilt += 0.2 * (dw_meas - self.dfilt)
        alpha = self.kp_rate * err + self.ki_rate * self.int_rate - self.kd_rate * self.dfilt
        torque = mdl.I @ alpha + np.cross(w, mdl.I @ w)
        thrust_total = mdl.m * float(f_des @ zb) if f_des @ zb > 0 else mdl.m * mdl.g
        # the collective command is the thrust along the body z axis (tilt compensated by the dot product above)
        tcmd = mdl.mix @ np.array([thrust_total, *torque])
        return tcmd


# ----------------------------------------------------------------------------------------------- the run
def simulate(mass, inertia, cg_mm, rotors, thrust_max_n, kq_ratio, g=G_DEFAULT, disturbance_deg=15.0,
             tau=MOTOR_LAG_S, t_end=T_END, dt=DT) -> dict:
    """Run the hover simulation. ``rotors``: [{position_mm, axis, spin}] in the aircraft frame (y left)."""
    pos = np.array([(np.asarray(r["position_mm"], float) - np.asarray(cg_mm, float)) / 1000.0 for r in rotors])
    axes = np.array([np.asarray(r["axis"], float) / np.linalg.norm(r["axis"]) for r in rotors])
    spin = np.array([1.0 if str(r.get("spin", "cw")).lower() == "cw" else -1.0 for r in rotors])
    I = np.asarray(inertia, float)
    Rh = _rot_from_to(axes.mean(0), np.array([0.0, 0.0, 1.0]))  # hover attitude: mean thrust axis up
    pos, axes, I = pos @ Rh.T, axes @ Rh.T, Rh @ I @ Rh.T
    mdl = _Model(float(mass), I, pos, axes, spin, float(thrust_max_n), float(kq_ratio), g, tau)
    if mdl.rank < 4:
        raise ValueError("the rotor layout cannot control thrust, roll, pitch and yaw at once "
                         f"(control effectiveness matrix rank {mdl.rank} of 4)")
    ctl = _Controller(mdl, tau)
    n = mdl.n
    th_h = mdl.mix @ np.array([mass * g, 0, 0, 0])
    s = np.concatenate([np.zeros(3), np.zeros(3), [1.0, 0, 0, 0], np.zeros(3), th_h])
    steps = int(round(t_end / dt))
    kick = int(round(T_KICK / dt))
    every = max(1, steps // MAX_SAMPLES)
    ts, ps, ats, ms = [], [], [], []
    tilt_max = 0.0
    tilt_hist = np.zeros(steps)
    perr = np.zeros(steps)
    cmd_max = 0.0
    unclipped_max = -1e9
    for k in range(steps):
        if k == kick:
            s[6:10] = _q_mul(_q_axis_angle([1, 0, 0], math.radians(disturbance_deg)), s[6:10])
            s[6:10] /= np.linalg.norm(s[6:10])
        tcmd = ctl.step(s, dt)
        unclipped_max = max(unclipped_max, float(tcmd.max()) / mdl.tmax)
        u = np.clip(tcmd, 0.0, mdl.tmax)
        cmd_max = max(cmd_max, float(u.max()) / mdl.tmax)
        k1 = mdl.deriv(s, u)
        k2 = mdl.deriv(s + 0.5 * dt * k1, u)
        k3 = mdl.deriv(s + 0.5 * dt * k2, u)
        k4 = mdl.deriv(s + dt * k3, u)
        s = s + dt / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
        s[6:10] /= np.linalg.norm(s[6:10])
        zb = _q_rot(s[6:10])[:, 2]
        tilt = math.degrees(math.acos(max(-1.0, min(1.0, zb[2]))))
        tilt_hist[k] = tilt
        perr[k] = float(np.linalg.norm(s[0:3]))
        if k % every == 0:
            ts.append((k + 1) * dt)
            ps.append(s[0:3].copy())
            ats.append([math.degrees(a) for a in _euler(s[6:10])])
            ms.append(np.clip(s[13:13 + n] / mdl.tmax, 0, None))
        if not np.all(np.isfinite(s)) or abs(s[2]) > 50:
            tilt_hist[k:] = 180.0
            perr[k:] = 50.0
            break
    after = np.arange(steps) >= kick
    bad = (tilt_hist > SETTLE_TILT_DEG) | (perr > SETTLE_POS_M)
    bad_after = np.where(bad & after)[0]
    if len(bad_after) == 0:
        settle = 0.0
    elif bad_after[-1] >= steps - 1:
        settle = None  # still outside the band at the end
    else:
        settle = float((bad_after[-1] + 1) * dt - T_KICK)
    return {"max_tilt_deg": float(tilt_hist.max()), "settle_time_s": settle,
            "position_error_m": float(perr.max()), "throttle_margin": 1.0 - unclipped_max,
            "peak_command": cmd_max, "hover_command": float(th_h.max() / mdl.tmax), "rank": mdl.rank,
            "t": ts, "pos": ps, "att": ats, "motor": ms, "rotors": n}


def run(ctx: T.RunContext) -> dict:
    spec = ctx.spec or {}
    typ = spec.get("type", "multirotor")
    if typ == "fixed_wing":
        return T.not_evaluated(ID, TITLE, ["type"], "Not evaluated: the hover simulation applies to rotorcraft; "
                               "fixed-wing flight is not simulated generically.")
    prev = ctx.previous or {}
    mass_res, prop_res = prev.get("mass"), prev.get("propulsion")
    mass, cg, inertia = (_mv(mass_res, "mass_kg"), _mv(mass_res, "cg_mm"), _mv(mass_res, "inertia_kgm2"))
    thrust = _mv(prop_res, "thrust_max_n")
    rotors = ((prop_res or {}).get("extra") or {}).get("rotors")
    missing = []
    if mass is None or cg is None or inertia is None:
        missing.append("mass (mass_kg, cg_mm, inertia_kgm2 from the mass analysis)")
    if thrust is None:
        missing.append("propulsion.thrust_max_n")
    if not rotors:
        missing.append("propulsion.rotors")
    D, _ = _num(spec, "propulsion.prop.diameter_in")
    ct, _ = _num(spec, "propulsion.prop.ct")
    cp, _ = _num(spec, "propulsion.prop.cp")
    for key, v in (("propulsion.prop.diameter_in", D), ("propulsion.prop.ct", ct), ("propulsion.prop.cp", cp)):
        if v is None:
            missing.append(key)
    if missing:
        return T.not_evaluated(ID, TITLE, missing)
    if ct <= 0 or cp <= 0 or D <= 0:
        return T.not_evaluated(ID, TITLE, ["propulsion.prop.ct/cp/diameter_in must be positive"])
    kq = cp / (2 * math.pi * ct) * D * 0.0254
    g, _ = _num(spec, "environment.g")
    g = g or G_DEFAULT
    dist, dst = _num(spec, "analyses.flight.disturbance_deg")
    dist_note = "" if dist is not None else f" (default {DEFAULT_DISTURBANCE_DEG:g} deg roll; no value in the file)"
    dist = DEFAULT_DISTURBANCE_DEG if dist is None else dist
    try:
        sim = simulate(mass, inertia, cg, rotors, thrust, kq, g, dist)
    except ValueError as exc:
        return T.not_evaluated(ID, TITLE, ["propulsion.rotors"], f"Not evaluated: {exc}.")
    settle, tilt, perr, marg = sim["settle_time_s"], sim["max_tilt_deg"], sim["position_error_m"], \
        sim["throttle_margin"]
    if settle is None or tilt > 45 or perr > 5:
        st = T.FAIL
    elif settle > 3.0 or marg < 0.1 or tilt > 30:
        st = T.WATCH
    else:
        st = T.OK
    sat = marg < 0
    basis = "estimate"  # motor lag and rotor reaction torque are estimates (see summary)
    if settle is None:
        summ = (f"After a {dist:g} degree kick the aircraft did not return to hover within the 8 s run "
                f"(peak tilt {tilt:.0f} degrees).")
    else:
        summ = (f"After a {dist:g} degree roll kick{dist_note} the aircraft levelled out in {settle:.1f} s, peak tilt "
                f"{tilt:.0f} degrees, drifting at most {perr * 100:.0f} cm; throttle headroom {marg * 100:.0f} %"
                f"{' (motors saturate)' if sat else ''}.")
    summ += (f" Model: motor lag {MOTOR_LAG_S * 1000:.0f} ms and rotor torque ratio from cp/ct are estimates; "
             "no drag or wind.")
    metrics = {"max_tilt_deg": T.metric(tilt, "deg", st, basis, "Peak tilt"),
               "settle_time_s": T.metric(settle, "s", st if settle is None else T.OK, basis, "Settle time"),
               "position_error_m": T.metric(perr, "m", T.OK, basis, "Peak position error"),
               "throttle_margin": T.metric(marg, "", T.FAIL if sat else (T.WATCH if marg < 0.1 else T.OK), basis,
                                           "Throttle headroom")}
    arr = lambda a, nd: np.round(np.asarray(a, float), nd).tolist()  # noqa: E731
    extra = {"t": arr(sim["t"], 4), "pos": arr(sim["pos"], 5), "att": arr(sim["att"], 3),
             "motor": arr(sim["motor"], 4), "frame": "x forward, y left, z up; att = roll, pitch, yaw (deg)",
             "disturbance_deg": dist, "kick_time_s": T_KICK, "motor_lag_s": MOTOR_LAG_S, "kq_m": kq,
             "hover_command": sim["hover_command"], "peak_command": sim["peak_command"],
             "rotor_count": sim["rotors"],
             "assumptions": ["motor lag 0.05 s (estimate)", "rotor reaction torque k_q = cp / (2 pi ct) * D "
                             "(estimate)", "thrust proportional to the command", "no drag, no wind",
                             f"settled = tilt < {SETTLE_TILT_DEG:g} deg and position error < {SETTLE_POS_M} m"]}
    return T.result(ID, TITLE, st, summ, metrics, solver={"name": "6-DOF rigid body, RK4", "dt_s": DT,
                    "duration_s": T_END, "converged": None}, extra=extra)
