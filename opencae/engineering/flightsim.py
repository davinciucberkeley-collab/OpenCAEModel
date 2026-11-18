"""Reduced-order 6-DOF flight simulation of the K09-L quad tailsitter (hover, transition, wing-borne).

THIS IS A REDUCED-ORDER MODEL BUILT ON PROVISIONAL COEFFICIENTS. Its results are not evidence of flight
stability, controllability or performance; they show what this model predicts for the current
configuration and are intended for trend studies, controller prototyping and finding problems early.

Frames
------
* World frame W: ENU-like, x East, y North, z Up, origin on flat ground (z = 0). ``pos[:, 2]`` is height.
* Body frame B = aircraft frame of the engineering model: x aft, y right, z up in cruise attitude, origin
  at the CG. Standing nose-up on the tail the body x axis points down (-z_W). Moments in body axes:
  +M_y = nose up (pitch), +M_x = right wing up, +M_z = nose left. In hover body x is vertical, so M_x acts
  as hover yaw, M_y tilts the thrust axis toward the belly/back and M_z tilts it sideways.
* Attitude: unit quaternion q = [w, x, y, z] rotating B into W (R = R(q), v_W = R v_B).
  ``euler`` is reported as ZYX (roll, pitch, yaw, deg) of the conventional forward-right-down body axes
  (f = -x_B, r = y_B, d = -z_B) relative to North-East-Down; pitch = +90 deg in hover, where roll and yaw
  are singular (use ``quat``).

Equations (integrated with fixed-step RK4, control held over the step)
--------------------------------------------------------------------
  p' = v,  m v' = R (F_prop + F_aero) + F_ground - m g z_W
  q' = 0.5 q (x) [0, w],  I w' = M_prop + M_aero + M_ground - w x I w   (I about the CG, body axes)
Propulsion, per motor i (thrust along -x_B at the motor origin from the model joints):
  advance ratio J = V_ax / (n D), V_ax = local inflow along the thrust axis (>= 0 used)
  C_T = C_T0 (1 - J / J0) (>= -0.2 C_T0),  C_P = C_P0 (1 - 0.8 (J / J0)^2) (>= 0.1 C_P0),
  J0 = 1.35 pitch / D                                                                       [provisional]
  T = C_T rho n^2 D^4,  Q = C_P rho n^2 D^5 / (2 pi)
  motor (K_v in rad/s/V): V_m = I R_m + w / K_v,  I = K_v (Q + J_r w') + I_0 tanh(w / 50)
  speed: w' = (w_ss(u V_batt) - w) / tau, w_ss solving R_m (K_v k_Q w^2 + I_0) + w / K_v = u V_batt,
  w' limited so that |I| <= 2 I_max(catalog 30-s rating) (provisional ESC limit; regenerative braking)
  reaction torque on the airframe: +Q x_B for props CCW seen from the nose (spin vector -x_B), -Q x_B for CW
Battery (simplified): V_oc = v_low + (v_full - v_low) SOC, SOC = 1 - charge / (capacity usable) over the
  usable capacity only; V_batt = V_oc - I_batt R_int with the previous step's current (breaks the
  algebraic loop); P_batt = sum(V_m I) / eta_esc, I_batt = P_batt / V_batt; energy_used = int P_batt dt.
Motor thermal (lumped, provisional): C_th dT' = I^2 R_m + I_0 V_m - dT / R_th.
Aerodynamics: strip model, 10 wing strips + 2 fins (flat plates in the x-z plane at y = +-0.32 m). Each
  panel sees the local air velocity (wind + gust - v - w x r, plus slipstream where immersed) in its own
  plane; alpha = atan2(-u_n, -u_c) over the full +-180 deg range.
  sigma(a) = Beard-McLain blend about a_stall;  C_L = (1-s)(C_L0 + C_La a) + s 2 sin a cos a
  C_D = (1-s)(C_D0 + C_Llin^2 / (pi AR e)) + s (C_D0 + 2 sin^2 a); centre of pressure moves from the
  neutral point to (0.25 + 0.25 |sin a|) c in separated flow; elevon: dC_L = tau C_La delta cos^2 a at 0.5 c;
  extra pitch damping M = 0.25 rho V S c^2 C_mq q; C_m0 about the neutral point.
  Slipstream: momentum theory v_i = sqrt(T / (2 rho A)), wake radius at the wing station from the contracting
  wake; the immersed fraction of each wing strip is computed from the geometry. For the K09-L the prop
  disks (z = +-0.206 m, R = 0.14 m) do not intersect the wing plane, so the immersed fraction is zero and
  the elevons have no authority in hover; ``slipstream_fraction`` overrides it for sensitivity studies.
Ground: spring-damper at each contact point of calcs.contact_points (k, c per point), Coulomb friction
  regularised F_t = -mu F_n v_t / sqrt(|v_t|^2 + eps^2). Only those points touch the ground.
Control (cascaded): position PID -> desired thrust vector (tilt limited) -> desired attitude (thrust axis
  -x_B along it, belly direction from the heading) -> attitude error e_R = 0.5 vee(R_d^T R - R^T R_d) (Lee
  et al.) -> rate PI -> moment demand. Mixer: [T_tot, M_x, M_y, M_z] = B T_i with B rows 1, s_i k_Q,
  -r_z,i, r_y,i (r_i motor position about the CG), solved exactly; on saturation the M_x (hover yaw)
  part is scaled down first, a negative thrust is removed by adding collective along the zero-moment
  direction ("air mode"), then thrusts are clipped. Throttle from inverting the nominal motor/prop model
  (closed form; the controller does not know about injected motor degradation).
  Elevons take a share of M_x/M_y growing with dynamic pressure (5 Pa -> 40 Pa); motors take the rest.
  Transition: attitude pitched over open-loop at a fixed rate, thrust holds altitude using an aerodynamic
  lift feed-forward (idealised) with a thrust floor (0.6 W forward, 0.5 W back) that keeps differential
  thrust authority; then a wing-borne law (pitch from altitude PID, thrust from airspeed PI, wings level).
  Back-transition: the reverse ramp to nose-up, then hover position hold once ground speed < 3 m/s.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from typing import Optional, Union

import numpy as np

from opencae.engineering import calcs

ASSUMPTIONS = [
    "Rigid body; props, elevons and battery do not move the CG; prop gyroscopic moments neglected.",
    "Propeller: static C_T0/C_P0 from the catalog (provisional) with advance-ratio corrections "
    "C_T = C_T0 (1 - J/J0), C_P = C_P0 (1 - 0.8 (J/J0)^2), J0 = 1.35 pitch/D (shape chosen to resemble "
    "published APC thin-electric data, not fitted to this prop); prop in-plane (H) force, normal force in "
    "crossflow and gyroscopic moments neglected.",
    "Motor speed: first-order lag to the steady-state speed, time constant tau = 0.05 s (provisional, "
    "not measured); rotor inertia J_r = 1.4e-4 kg m^2 (estimate) adds the acceleration current; the "
    "motor current is limited to +-2 x the catalog 30-s rating (provisional ESC limit), which also "
    "limits spin-up/spin-down; the ESC brakes regeneratively (damped mode); I_0 fades to 0 as the rotor stops.",
    "ESC efficiency 0.95 (provisional), applied to battery power.",
    "Battery: open-circuit voltage linear between v_full and v_low over the usable capacity (simplified, "
    "real LiPo curves are not linear); R_int provisional; voltage uses the previous step's current.",
    "Motor thermal: lumped R_th = 2.5 K/W, C_th = 45 J/K (provisional, not measured).",
    "Wing: strip theory with the finite-wing lift slope on every strip, C_L0 = 0.05, C_m0 = +0.01 "
    "(MH60-like, provisional), stall blend at 12 deg, flat-plate post-stall C_L = 2 sin a cos a, "
    "C_D = C_D0 + 2 sin^2 a; C_D0 = model 'cd0' (whole aircraft, provisional); neutral point = model "
    "'np_frac' (provisional).",
    "Elevons: dC_L = tau C_La delta cos^2(alpha) with tau = 0.45 at 0.5 chord (provisional); hinge "
    "moments and servo loads not modelled; rate limit 300 deg/s.",
    "Pitch damping C_mq = -1.0 (provisional) in addition to the strip-model rotation terms.",
    "Fins: two flat plates (from CAD extents) with the same coefficient model, C_D0 = 0 (included in cd0).",
    "Slipstream from momentum theory applied only to wing strips geometrically inside the contracted wake "
    "(none for the K09-L geometry); swirl and slipstream on the fins neglected.",
    "Ground: flat, spring-damper k = 2e4 N/m, c = 60 N s/m per contact point, Coulomb mu = 0.6 "
    "(regularised); only the landing contact points can touch the ground.",
    "Atmosphere: uniform density; wind steady plus a 1-cos discrete gust; no turbulence, no ground effect.",
    "Controller: idealised (perfect state knowledge, no sensor noise or delay, runs every step) and uses "
    "the nominal motor model for thrust inversion; it is not the flight controller firmware.",
]

VALIDITY = ("Reduced-order simulation on provisional aerodynamic, propeller, motor-dynamics, battery and "
            "thermal coefficients. No part of the flight envelope has been validated against test or CFD, so "
            "every time step is flagged out_of_envelope (accuracy unsupported). The results are NOT evidence "
            "of flight stability, controllability or performance of the K09-L.")

COMMON = dict(duration=10.0, start="air", altitude=5.0, heading_deg=0.0, wind=(0.0, 0.0, 0.0), gust=None,
              rho=None, temperature_C=None, pressure_Pa=None, tau_motor=0.05, payload=0.0, soc0=1.0,
              motor_failure=None, aero=True, ground=True, controller=True, open_loop_throttle=None,
              slipstream_fraction=None, envelope=None, gains=None)

SCENARIOS: dict[str, dict] = {
    "takeoff_hover": dict(duration=12.0, start="ground", altitude=5.0, climb_rate=1.0, arm_time=0.5),
    "hover": dict(duration=10.0, start="air", altitude=5.0),
    "land": dict(duration=14.0, start="air", altitude=5.0, descent_rate=0.6, land_start=1.0),
    "gust_response": dict(duration=12.0, start="air", altitude=5.0,
                          gust=dict(amplitude=4.0, duration=1.5, start=2.0, direction=(1.0, 0.0, 0.0))),
    "payload": dict(duration=10.0, start="air", altitude=5.0, payload=0.3),
    "motor_degradation": dict(duration=10.0, start="air", altitude=5.0,
                              motor_failure=dict(motor=1, factor=0.7, time=2.0)),
    "battery_decline": dict(duration=10.0, start="air", altitude=5.0, soc0=0.05),
    "transition_forward": dict(duration=16.0, start="air", altitude=30.0, transition_start=2.0,
                               pitch_rate_deg=45.0, pitch_end_deg=75.0, target_airspeed=14.0,
                               transition_timeout=8.0),
    "transition_back": dict(duration=14.0, start="cruise", altitude=30.0, airspeed=14.0, back_start=3.0,
                            pitch_rate_deg=45.0),
    "waypoints": dict(duration=30.0, start="air", altitude=5.0, speed=2.0, accept_radius=0.3,
                      waypoints=[(4.0, 0.0, 5.0), (4.0, 4.0, 6.0), (0.0, 4.0, 5.0), (0.0, 0.0, 5.0)]),
}
"""Failure-injection / environment parameters accepted by every scenario (``simulate(..., key=value)``):
wind (3, m/s world), gust {amplitude m/s, duration s, start s, direction world vector}, rho or
temperature_C + pressure_Pa, payload kg (point mass at the battery), soc0 (usable state of charge at
start), motor_failure {motor 1..4, factor on that motor's thrust, time s}, tau_motor s, aero/ground/
controller switches, open_loop_throttle (4 values, controller off), slipstream_fraction (override the
geometric immersed fraction), envelope {alpha_deg: (lo, hi), airspeed: (lo, hi)} once a validated
envelope exists, gains {name: value} (see _Controller.G)."""

DEFAULT_GAINS = dict(kp_xy=1.2, kd_xy=2.0, ki_xy=0.15, kp_z=2.0, kd_z=2.5, ki_z=0.5, a_max_xy=4.0, a_max_z=5.0,
                     tilt_max_deg=35.0, kR=(5.0, 6.0, 6.0), kw=(14.0, 14.0, 14.0), kwi=(4.0, 4.0, 4.0),
                     k_h=0.08, k_hd=0.10, k_hi=0.02, k_v=1.5, k_vi=0.6)

N_STRIP = 10
J_ROTOR = 1.4e-4
ETA_ESC = 0.95
R_TH, C_TH = 2.5, 45.0
CL0, CM0, A_STALL, M_BLEND, TAU_ELEVON, CMQ = 0.05, 0.01, math.radians(12.0), 50.0, 0.45, -1.0
K_GROUND, C_GROUND, MU_GROUND, V_EPS = 2.0e4, 60.0, 0.6, 0.05
ELEVON_RATE = math.radians(300.0)


# --------------------------------------------------------------------------- small math helpers
def _param(model, key: str, default: float) -> float:
    q = model.params.get(key)
    return float(q.value) if q is not None and q.value is not None else default


def _qmul(a, b):
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return np.array([w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2, w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                     w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2, w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2])


def _q2R(q):
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


def _R2q(R):
    tr = R[0, 0] + R[1, 1] + R[2, 2]
    if tr > 0:
        s = 2 * math.sqrt(tr + 1)
        q = [0.25 * s, (R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s]
    else:
        i = int(np.argmax(np.diag(R)))
        j, k = (i + 1) % 3, (i + 2) % 3
        s = 2 * math.sqrt(1 + R[i, i] - R[j, j] - R[k, k])
        q = [0.0] * 4
        q[0] = (R[k, j] - R[j, k]) / s
        q[1 + i] = 0.25 * s
        q[1 + j] = (R[j, i] + R[i, j]) / s
        q[1 + k] = (R[k, i] + R[i, k]) / s
    q = np.array(q)
    return q / np.linalg.norm(q)


def _cross(a, b):
    return np.array([a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]])


def _rc(a, b):
    """Row-wise cross product of (N,3) / (3,) arrays (much cheaper than np.cross for small N)."""
    a0, a1, a2 = a[..., 0], a[..., 1], a[..., 2]
    b0, b1, b2 = b[..., 0], b[..., 1], b[..., 2]
    return np.stack([a1 * b2 - a2 * b1, a2 * b0 - a0 * b2, a0 * b1 - a1 * b0], axis=-1)


def _rot_y(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def hover_frame(heading_deg: float) -> np.ndarray:
    """Body->world rotation standing nose-up: nose (-x_B) up, belly (-z_B) toward the heading (the
    direction of forward flight after transition), heading measured from world x (East) toward y."""
    psi = math.radians(heading_deg)
    x = np.array([0.0, 0.0, -1.0])
    z = -np.array([math.cos(psi), math.sin(psi), 0.0])
    return np.column_stack([x, _cross(z, x), z])


def euler_frd_ned(q) -> np.ndarray:
    """ZYX (roll, pitch, yaw) in deg of the forward-right-down axes relative to North-East-Down."""
    R = _q2R(q)
    T = np.array([[0.0, 1, 0], [1, 0, 0], [0, 0, -1]])
    C = T @ R @ np.diag([-1.0, 1.0, -1.0])
    pitch = math.asin(max(-1.0, min(1.0, -C[2, 0])))
    return np.degrees([math.atan2(C[2, 1], C[2, 2]), pitch, math.atan2(C[1, 0], C[0, 0])])


# --------------------------------------------------------------------------- plant
class _Plant:
    """Constants of the configured aircraft (taken from the engineering model when created)."""

    def __init__(self, model, P: dict):
        mp = calcs.mass_properties(model)
        m = mp["mass_total"].value
        cg = np.array(mp["cg"].value, float)
        I = np.array(mp["inertia"].value, float)
        pay = float(P["payload"] or 0.0)
        if pay > 0:  # point mass at the battery centroid (as calcs does for the model 'payload' parameter)
            bat = [b for b in model.active_bodies() if b.component.startswith("LiPo")]
            rp = (bat[0].centroid * 1e-3) if bat else cg.copy()
            cg_new = (m * cg + pay * rp) / (m + pay)
            d0, d1 = cg - cg_new, rp - cg_new
            I = (I + m * (d0 @ d0 * np.eye(3) - np.outer(d0, d0)) + pay * (d1 @ d1 * np.eye(3) - np.outer(d1, d1)))
            m, cg = m + pay, cg_new
        self.m, self.cg, self.I = m, cg, I
        self.Iinv = np.linalg.inv(I)
        self.g = model.params["g"].value
        rho = P["rho"] or model.params["rho"].value
        if P["temperature_C"] is not None and P["pressure_Pa"] is not None:
            rho = P["pressure_Pa"] / (287.05 * (P["temperature_C"] + 273.15))
        self.rho = rho
        # propulsion
        mot, prop, bat = model.option("motor"), model.option("prop"), model.option("battery")
        self.kv = mot.q("kv").value * 2 * math.pi / 60.0
        self.rm, self.i0 = mot.q("rm").value, mot.q("i0").value
        self.imax = mot.q("imax").value or 25.0  # catalog 30-s rating
        lim = model.params.get("throttle_max")
        self.u_max = float(lim.value) if lim is not None and lim.value else 1.0  # flight-controller output limit
        self.i_peak = 2.0 * self.imax  # provisional transient current limit
        self.D, self.ct0, self.cp0 = prop.q("diameter").value, prop.q("ct0").value, prop.q("cp0").value
        pitch = prop.q("pitch").value or 0.4 * self.D
        self.J0 = 1.35 * pitch / self.D
        self.kP = 0.8
        self.kQ = self.cp0 * self.D / (2 * math.pi * self.ct0)  # torque / thrust (static), m
        joints = [j for n, j in model.joints.items() if n.startswith("Prop M")]
        joints.sort(key=lambda j: j["name"])
        self.r_mot = np.array([np.array(j["origin_mm"]) * 1e-3 - cg for j in joints])  # about CG
        self.spin = np.array([1.0 if "CCW" in j["name"] else -1.0 for j in joints])  # +1: reaction +x_B
        self.motor_names = [j["name"] for j in joints]
        self.v_full, self.v_low = bat.q("v_full").value, bat.q("v_low").value
        self.r_int = bat.q("r_int").value
        self.q_usable = bat.q("capacity").value * bat.q("usable").value
        # wing strips
        wr = calcs.wing_reference(model)
        self.S, self.b, self.c = wr["area"].value, wr["span"].value, wr["chord"].value
        self.x_le = wr["x_le"].value
        self.AR = self.b ** 2 / self.S
        self.CLa = 2 * math.pi * self.AR / (2 + math.sqrt(self.AR ** 2 + 4)) * _param(model, "cla_factor", 1.0)
        self.CL0 = _param(model, "cl0", CL0)
        self.k_ind = 1 / (math.pi * self.AR * model.params["oswald"].value)
        self.cd0 = model.params["cd0"].value
        self.x_np = self.x_le + model.params["np_frac"].value * self.c
        edges = np.linspace(-self.b / 2, self.b / 2, N_STRIP + 1)
        yc = 0.5 * (edges[1:] + edges[:-1])
        dy = np.diff(edges)
        z_w = 0.0
        # panels: N_STRIP wing strips (normal z) + one flat plate per fin (normal y, extents from the CAD)
        fin_ext = [(P.min(0), P.max(0)) for P in (model.body_mesh(b)[0] * 1e-3 for b in model.active_bodies()
                                                    if b.component.startswith("Fin Depron"))]
        npan = N_STRIP + len(fin_ext)
        ch_f = np.array([hi[0] - lo[0] for lo, hi in fin_ext])
        h_f = np.array([hi[2] - lo[2] for lo, hi in fin_ext])
        ar_f = h_f / ch_f
        wing_r = np.column_stack([np.full(N_STRIP, self.x_np), yc, np.full(N_STRIP, z_w)])
        fin_r = np.array([[lo[0] + 0.25 * (hi[0] - lo[0]), 0.5 * (lo[1] + hi[1]), 0.5 * (lo[2] + hi[2])]
                          for lo, hi in fin_ext]).reshape(-1, 3)
        self.pan_r = np.vstack([wing_r, fin_r]) - cg
        self.pan_n = np.vstack([np.tile([0.0, 0, 1], (N_STRIP, 1)), np.tile([0.0, 1, 0], (len(fin_ext), 1))])
        self.pan_S = np.concatenate([dy * self.c, h_f * ch_f])
        self.pan_c = np.concatenate([np.full(N_STRIP, self.c), ch_f])
        self.pan_xle = np.concatenate([np.full(N_STRIP, self.x_le), [lo[0] for lo, _ in fin_ext]]) - cg[0]
        self.pan_cd0 = np.concatenate([np.full(N_STRIP, self.cd0), np.zeros(len(fin_ext))])
        self.pan_cl0 = np.concatenate([np.full(N_STRIP, self.CL0), np.zeros(len(fin_ext))])
        self.pan_cla = np.concatenate([np.full(N_STRIP, self.CLa), 2 * np.pi * ar_f / (2 + np.sqrt(ar_f ** 2 + 4))])
        self.pan_k = np.concatenate([np.full(N_STRIP, self.k_ind), 1 / (np.pi * ar_f * 0.8)])
        self.n_wing = N_STRIP
        # elevons (from the CAD elevon bodies' spanwise extent)
        ev = [b for b in model.active_bodies() if b.component.startswith("Elevon")]
        ys = np.hstack([model.body_mesh(b)[0][:, 1] * 1e-3 for b in ev]) if ev else np.zeros(1)
        y_in, y_out = np.abs(ys).min(), np.abs(ys).max()
        on = (np.abs(yc) > y_in) & (np.abs(yc) < y_out)
        self.elev_side = np.zeros(npan)  # +1 right (elevon R), -1 left, 0 none
        self.elev_side[:N_STRIP][on] = np.sign(yc[on])
        self.x_elev = self.x_le + 0.5 * self.c - cg[0]
        self.pan_has_e = (self.elev_side != 0).astype(float)
        self.pan_e_onehot = np.column_stack([self.elev_side < 0, self.elev_side > 0]).astype(float)
        self.pan_re = self.pan_r.copy()
        self.pan_re[:, 0] = self.x_elev
        self.pan_cm0 = np.where(np.arange(npan) < N_STRIP, CM0, 0.0)
        self._xh = np.array([1.0, 0.0, 0.0])
        # slipstream: immersed fraction of each wing strip (wake cylinder radius at the wing c/4 station)
        R = self.D / 2
        frac = np.zeros(npan)
        for rm_ in self.r_mot:
            xd = (self.x_le + 0.25 * self.c) - (rm_[0] + cg[0])
            rw = R / math.sqrt(1 + (xd / R) / math.sqrt(1 + (xd / R) ** 2))
            dz = abs((rm_[2] + cg[2]) - z_w)
            hw = math.sqrt(max(rw * rw - dz * dz, 0.0))
            ym = rm_[1] + cg[1]
            lo_, hi_ = edges[:-1], edges[1:]
            ov = np.clip(np.minimum(hi_, ym + hw) - np.maximum(lo_, ym - hw), 0, None) / dy
            frac[:N_STRIP] += ov
        self.slip_frac_geom = np.clip(frac, 0, 1)
        self.slip_frac = self.slip_frac_geom.copy()
        if P["slipstream_fraction"] is not None:
            self.slip_frac[:N_STRIP] = float(P["slipstream_fraction"])
        self.slip_xd = (self.x_le + 0.25 * self.c) - (self.r_mot[:, 0].mean() + cg[0])
        self.contacts = calcs.contact_points(model) - cg
        self.health = np.ones(4)
        self.aero, self.ground = bool(P["aero"]), bool(P["ground"])
        self.tau = float(P["tau_motor"])
        # mixer
        self.B = np.vstack([np.ones(4), self.spin * self.kQ, -self.r_mot[:, 2], self.r_mot[:, 1]])
        self.Binv = np.linalg.inv(self.B)

    # ---- propeller / motor
    def prop_coeffs(self, Vax, n):
        J = np.clip(Vax / (np.maximum(n, 5.0) * self.D), 0.0, 2 * self.J0)
        fT = np.maximum(1 - J / self.J0, -0.2)
        fP = np.maximum(1 - self.kP * (J / self.J0) ** 2, 0.1)
        return fT, fP

    def w_ss(self, Vm, fP):
        kq = self.cp0 * fP * self.rho * self.D ** 5 / (2 * math.pi) ** 3  # Q = kq w^2
        a = self.rm * self.kv * kq
        c = self.rm * self.i0 - Vm
        disc = (1 / self.kv) ** 2 - 4 * a * c
        w = (-1 / self.kv + np.sqrt(np.maximum(disc, 0))) / (2 * a)
        return np.where(Vm > self.rm * self.i0, w, 0.0)

    def throttle_for(self, T, Vax, n, Vb):
        """Throttle giving steady thrust T (nominal motor, inflow Vax) at battery voltage Vb. With
        C_T = C_T0 (1 - J/J0), T = a n^2 - b n is solved for n in closed form."""
        a = self.ct0 * self.rho * self.D ** 4
        b = a * np.maximum(Vax, 0.0) / (self.D * self.J0)
        Tp = np.maximum(T, 0.0)
        n = (b + np.sqrt(b * b + 4 * a * Tp)) / (2 * a)
        w = 2 * math.pi * n
        fT, fP = self.prop_coeffs(Vax, n)
        kq = self.cp0 * fP * self.rho * self.D ** 5 / (2 * math.pi) ** 3
        Vm = self.rm * (self.kv * kq * w * w + self.i0) + w / self.kv
        return np.clip(np.where(T > 0, Vm / Vb, 0.0), 0.0, self.u_max)

    def thrust_max(self, Vax, n, Vb):
        fT, fP = self.prop_coeffs(Vax, n)
        w = self.w_ss(np.full(4, Vb * self.u_max), fP)
        return self.ct0 * fT * self.rho * self.D ** 4 * (w / (2 * math.pi)) ** 2

    # ---- aerodynamics (all panels vectorised)
    def coeffs(self, a):
        s = (1 + np.exp(-M_BLEND * (a - A_STALL)) + np.exp(M_BLEND * (a + A_STALL))) / \
            ((1 + np.exp(-M_BLEND * (a - A_STALL))) * (1 + np.exp(M_BLEND * (a + A_STALL))))
        sa, ca = np.sin(a), np.cos(a)
        cl_lin = self.pan_cl0 + self.pan_cla * a
        cl = (1 - s) * cl_lin + s * 2 * sa * ca
        cd = (1 - s) * (self.pan_cd0 + self.pan_k * cl_lin ** 2) + s * (self.pan_cd0 + 2 * sa * sa)
        return cl, cd, s

    def aero_fm(self, va_b, om, elev, T):
        """Aerodynamic force and moment (body, about CG) and elevon effectiveness (3x2 per rad).
        va_b: aircraft velocity relative to the air mass (body)."""
        u = va_b[None, :] + _rc(om[None, :], self.pan_r)  # local aircraft velocity rel. air
        if self.slip_frac.any():
            vi = np.sqrt(np.maximum(T.mean(), 0) / (2 * self.rho * math.pi * (self.D / 2) ** 2))
            ks = 1 + (self.slip_xd / (self.D / 2)) / math.sqrt(1 + (self.slip_xd / (self.D / 2)) ** 2)
            us = u.copy()
            us[:, 0] -= ks * vi  # air moves aft over the immersed part: aircraft moves "forward" relative
            F1, M1, B1 = self._panels(u, elev)
            F2, M2, B2 = self._panels(us, elev)
            f = self.slip_frac[:, None]
            Fp = (1 - f) * F1 + f * F2
            return Fp.sum(0), (1 - f[:, 0]) @ M1 + f[:, 0] @ M2, (B1 + B2) / 2
        F, M, Be = self._panels(u, elev)
        return F.sum(0), M.sum(0), Be

    def _panels(self, u, elev):
        n = self.pan_n
        uc = u[:, 0]
        un = (u * n).sum(1)
        V2 = uc * uc + un * un
        V = np.sqrt(V2) + 1e-9
        a = np.arctan2(-un, -uc)
        cl, cd, s = self.coeffs(a)
        qS = 0.5 * self.rho * V2 * self.pan_S
        dc, dn = -uc / V, -un / V  # drag direction (along the airflow)
        Ld = (-dn)[:, None] * self._xh + dc[:, None] * n  # lift direction (perpendicular, in plane)
        Dd = dc[:, None] * self._xh + dn[:, None] * n
        F = qS[:, None] * (cl[:, None] * Ld + cd[:, None] * Dd)
        # centre of pressure: neutral point (attached) -> (0.25 + 0.25|sin a|) c (separated)
        r = self.pan_r.copy()
        r[:, 0] = (1 - s) * r[:, 0] + s * (self.pan_xle + (0.25 + 0.25 * np.abs(np.sin(a))) * self.pan_c)
        M = _rc(r, F)
        M[:, 1] += (1 - s) * qS * self.pan_c * self.pan_cm0  # C_m0 about the neutral point (wing)
        # elevons: lift increment at 0.5 c on the strips carrying an elevon
        dF = (TAU_ELEVON * self.pan_cla * np.cos(a) ** 2 * qS * self.pan_has_e)[:, None] * Ld
        dM = _rc(self.pan_re, dF)
        de = self.pan_e_onehot @ elev
        F += dF * de[:, None]
        M += dM * de[:, None]
        return F, M, dM.T @ self.pan_e_onehot  # Be columns: left, right elevon (per rad)


# --------------------------------------------------------------------------- environment
def _gust(t, g):
    if not g:
        return np.zeros(3)
    t0, T = g.get("start", 0.0), g.get("duration", 1.0)
    if t < t0 or t > t0 + T:
        return np.zeros(3)
    d = np.asarray(g.get("direction", (1, 0, 0)), float)
    d = d / (np.linalg.norm(d) + 1e-12)
    return 0.5 * g.get("amplitude", 0.0) * (1 - math.cos(2 * math.pi * (t - t0) / T)) * d


# --------------------------------------------------------------------------- dynamics
IDX_P, IDX_V, IDX_Q, IDX_W, IDX_M = slice(0, 3), slice(3, 6), slice(6, 10), slice(10, 13), slice(13, 17)
I_CHG, I_EN, IDX_TH, I_DIST = 17, 18, slice(19, 23), 23
NSTATE = 24


def _deriv(x, t, u, elev, Vb, pl: _Plant, P, aux=False):
    p, v, q, om, wm = x[IDX_P], x[IDX_V], x[IDX_Q], x[IDX_W], x[IDX_M]
    R = _q2R(q)
    wind = np.asarray(P["wind"], float) + _gust(t, P["gust"])
    va_b = R.T @ (v - wind)
    # propulsion
    vloc = va_b[None, :] + _rc(om[None, :], pl.r_mot)
    Vax = -vloc[:, 0]
    n = wm / (2 * math.pi)
    fT, fP = pl.prop_coeffs(Vax, n)
    T = pl.ct0 * fT * pl.rho * n * n * pl.D ** 4 * pl.health
    Qp = pl.cp0 * fP * pl.rho * n * n * pl.D ** 5 / (2 * math.pi)
    Vm = u * Vb
    i0 = pl.i0 * np.tanh(wm / 50.0)  # no-load current, faded out as the rotor stops
    qa = (pl.i_peak - i0) / pl.kv  # motor torque at the transient current limit
    wdot = np.clip((pl.w_ss(Vm, fP) - wm) / pl.tau, (-qa - Qp) / J_ROTOR, (qa - Qp) / J_ROTOR)
    I_m = pl.kv * (Qp + J_ROTOR * wdot) + i0
    Pm = I_m * (I_m * pl.rm + wm / pl.kv)
    Pb = Pm.sum() / ETA_ESC
    Ib = Pb / Vb
    Fb = np.array([-T.sum(), 0.0, 0.0])
    Mb = np.array([(pl.spin * Qp).sum(), -(pl.r_mot[:, 2] * T).sum(), (pl.r_mot[:, 1] * T).sum()])
    Fa = Ma = np.zeros(3)
    Be = np.zeros((3, 2))
    if pl.aero:
        Fa, Ma, Be = pl.aero_fm(va_b, om, elev, T)
        Fb = Fb + Fa
        Mb = Mb + Ma
    Fw = R @ Fb
    Fw[2] -= pl.m * pl.g
    Fg = np.zeros(3)
    if pl.ground:
        rcw = pl.contacts @ R.T
        zc = p[2] + rcw[:, 2]
        hit = zc < 0
        if hit.any():
            omw = R @ om
            vc = v[None, :] + _rc(omw[None, :], rcw[hit])
            Fn = np.maximum(-K_GROUND * zc[hit] - C_GROUND * vc[:, 2], 0.0)
            vt = vc[:, :2]
            fr = -MU_GROUND * Fn[:, None] * vt / np.sqrt((vt * vt).sum(1) + V_EPS ** 2)[:, None]
            Fc = np.column_stack([fr, Fn])
            Fg = Fc.sum(0)
            Mb = Mb + R.T @ _rc(rcw[hit], Fc).sum(0)
            Fw = Fw + Fg
    acc = Fw / pl.m
    omd = pl.Iinv @ (Mb - _cross(om, pl.I @ om))
    qd = 0.5 * _qmul(q, np.array([0.0, om[0], om[1], om[2]]))
    Ploss = I_m * I_m * pl.rm + i0 * wm / pl.kv
    thd = (Ploss - x[IDX_TH] / R_TH) / C_TH
    dx = np.empty(NSTATE)
    dx[IDX_P], dx[IDX_V], dx[IDX_Q], dx[IDX_W], dx[IDX_M] = v, acc, qd, omd, wdot
    dx[I_CHG], dx[I_EN], dx[IDX_TH] = Ib, Pb, thd
    dx[I_DIST] = math.hypot(v[0], v[1])
    if not aux:
        return dx
    return dx, dict(T=T, rpm=wm * 60 / (2 * math.pi), I_m=I_m, Ib=Ib, Pb=Pb, Fa=Fa, Ma=Ma, va_b=va_b, Be=Be,
                    Vax=Vax, n=n, Fg=Fg, acc=acc)


# --------------------------------------------------------------------------- controller
class _Controller:
    def __init__(self, pl: _Plant, P: dict, scen: str):
        self.pl, self.P, self.scen = pl, P, scen
        self.G = dict(DEFAULT_GAINS, **(P["gains"] or {}))
        self.Rh = hover_frame(P["heading_deg"])
        psi = math.radians(P["heading_deg"])
        self.fwd = np.array([math.cos(psi), math.sin(psi), 0.0])
        self.mode = "hover"
        self.int_p = np.zeros(3)
        self.int_w = np.zeros(3)
        self.p_sp = None
        self.v_sp = np.zeros(3)
        self.phi = 0.0  # pitch-over angle (0 hover, 90 deg nose horizontal)
        self.int_h = self.int_v = 0.0
        self.T_cmd = pl.m * pl.g
        self.h_sp = P["altitude"]
        self.v_target = P.get("target_airspeed", P.get("airspeed", 14.0))
        self.events: list = []
        self.wp_i = 0
        self.landed = False
        self.t_touch = None

    def attitude_moment(self, R, om, Rd, dt, freeze):
        G = self.G
        E = Rd.T @ R - R.T @ Rd
        eR = 0.5 * np.array([E[2, 1], E[0, 2], E[1, 0]])
        wd = -np.asarray(G["kR"]) * eR
        ew = wd - om
        if not freeze:  # integral term kept in moment units (N m), clipped
            self.int_w = np.clip(self.int_w + self.pl.I @ (np.asarray(G["kwi"]) * ew) * dt, -1.0, 1.0)
        return self.pl.I @ (np.asarray(G["kw"]) * ew) + self.int_w + _cross(om, self.pl.I @ om)

    def hover_thrust_vector(self, p, v, dt, freeze):
        G, pl = self.G, self.pl
        e = self.p_sp - p
        ev = self.v_sp - v
        if not freeze:
            self.int_p = np.clip(self.int_p + e * dt, -3.0, 3.0)
        a = np.empty(3)
        a[:2] = G["kp_xy"] * e[:2] + G["kd_xy"] * ev[:2] + G["ki_xy"] * self.int_p[:2]
        a[2] = G["kp_z"] * e[2] + G["kd_z"] * ev[2] + G["ki_z"] * self.int_p[2]
        nh = np.linalg.norm(a[:2])
        if nh > G["a_max_xy"]:
            a[:2] *= G["a_max_xy"] / nh
        a[2] = np.clip(a[2], -G["a_max_z"], G["a_max_z"])
        F = pl.m * (a + np.array([0, 0, pl.g]))
        F[2] = max(F[2], 0.2 * pl.m * pl.g)
        tmax = math.tan(math.radians(G["tilt_max_deg"]))
        h = np.linalg.norm(F[:2])
        if h > tmax * F[2]:
            F[:2] *= tmax * F[2] / h
        return F

    def rd_from_thrust(self, F):
        xb = -F / np.linalg.norm(F)
        h = self.Rh[:, 2]
        zb = h - (h @ xb) * xb
        zb /= np.linalg.norm(zb)
        return np.column_stack([xb, _cross(zb, xb), zb])

    def update(self, t, x, dt, on_ground, aux):
        """Returns (desired total thrust, desired body moment, transition elevon share)."""
        pl, P, G = self.pl, self.P, self.G
        p, v, q, om = x[IDX_P], x[IDX_V], x[IDX_Q], x[IDX_W]
        R = _q2R(q)
        if self.p_sp is None:
            self.p_sp = p.copy()
            self.ground_z = p[2]
        freeze = on_ground
        sc = self.scen
        self.v_sp = np.zeros(3)
        # ---- scenario set-point logic
        if sc == "takeoff_hover":
            if t < P["arm_time"]:
                return None
            tc = t - P["arm_time"]
            self.p_sp[2] = min(self.ground_z + P["climb_rate"] * tc, P["altitude"])
            self.v_sp[2] = P["climb_rate"] if self.p_sp[2] < P["altitude"] else 0.0
        elif sc == "land":
            if self.landed:
                return None
            if t > P["land_start"]:
                self.p_sp[2] = max(P["altitude"] - P["descent_rate"] * (t - P["land_start"]), -0.5)
                self.v_sp[2] = -P["descent_rate"]
                if on_ground and self.t_touch is None:
                    self.t_touch = t
                    self.events.append((t, f"touchdown at vertical speed {v[2]:.2f} m/s"))
                if self.t_touch is not None and t - self.t_touch > 0.3:
                    self.landed = True
                    self.events.append((t, "motors stopped after touchdown"))
                    return None
        elif sc == "waypoints":
            wps = [np.asarray(w, float) for w in P["waypoints"]]
            if self.wp_i < len(wps):
                tgt = wps[self.wp_i]
                d = tgt - self.p_sp
                step = P["speed"] * dt
                if np.linalg.norm(d) > step:
                    self.v_sp = d / np.linalg.norm(d) * P["speed"]
                    self.p_sp = self.p_sp + self.v_sp * dt
                else:
                    self.p_sp = tgt.copy()
                if np.linalg.norm(tgt - p) < P["accept_radius"]:
                    self.events.append((t, f"waypoint {self.wp_i + 1} reached"))
                    self.wp_i += 1
        elif sc == "transition_forward":
            if self.mode == "hover" and t >= P["transition_start"]:
                self.mode = "transition"
                self.h_sp = p[2]
                self.events.append((t, "transition start"))
            if self.mode == "transition":
                self.phi = min(self.phi + math.radians(P["pitch_rate_deg"]) * dt, math.radians(P["pitch_end_deg"]))
                Vair = np.linalg.norm(aux["va_b"])
                if Vair >= self.v_target and self.phi >= math.radians(P["pitch_end_deg"]) - 1e-6:
                    self.mode = "wing"
                    self.int_h, self.theta0 = 0.0, math.pi / 2 - self.phi
                    self.events.append((t, f"wing-borne at airspeed {Vair:.1f} m/s"))
                elif t - P["transition_start"] > P["transition_timeout"]:
                    self.mode = "wing"
                    self.int_h, self.theta0 = 0.0, math.pi / 2 - self.phi
                    self.events.append((t, f"WARNING transition did not reach {self.v_target:.1f} m/s within "
                                           f"{P['transition_timeout']:.0f} s (airspeed {Vair:.1f} m/s)"))
        elif sc == "transition_back":
            if self.mode == "wing" and t >= P["back_start"]:
                self.mode = "back"
                self.h_sp = P["altitude"]
                self.events.append((t, "back-transition start"))
            if self.mode == "back":
                self.phi = max(self.phi - math.radians(P["pitch_rate_deg"]) * dt, 0.0)
                if self.phi <= 0.0 and math.hypot(v[0], v[1]) < 3.0:
                    self.mode = "hover"
                    self.p_sp = p + v * 0.3
                    self.p_sp[2] = P["altitude"]
                    self.int_p[:] = 0
                    self.events.append((t, f"hover mode at ground speed {np.linalg.norm(v):.1f} m/s"))
        # ---- laws
        share = 0.0
        if self.mode == "hover":
            F = self.hover_thrust_vector(p, v, dt, freeze)
            Rd = self.rd_from_thrust(F)
            T = max(F @ (-R[:, 0]), 0.0)
        elif self.mode in ("transition", "back"):
            Rd = self.Rh @ _rot_y(-self.phi)
            el = math.pi / 2 - self.phi  # nominal nose elevation
            eh = self.h_sp - p[2]
            self.int_h = np.clip(self.int_h + eh * dt, -5, 5)
            az = G["kp_z"] * eh - G["kd_z"] * v[2] + G["ki_z"] * self.int_h
            nose = -R[:, 0]
            Fz_aero = float((R @ aux["Fa"])[2])  # aerodynamic lift feed-forward (idealised estimate)
            T = max(pl.m * (pl.g + az) - Fz_aero, 0.0) / max(nose[2], 0.25)
            T = max(T, pl.m * pl.g * (0.6 if self.mode == "transition" else 0.5))  # keep control authority
        else:  # wing-borne
            eh = self.h_sp - p[2]
            self.int_h = np.clip(self.int_h + eh * dt, -20, 20)
            th = self.theta0 + G["k_h"] * eh - G["k_hd"] * v[2] + G["k_hi"] * self.int_h
            th = float(np.clip(th, math.radians(-5), math.radians(35)))
            Rd = self.Rh @ _rot_y(-(math.pi / 2 - th))
            Vair = np.linalg.norm(aux["va_b"])
            ev = self.v_target - Vair
            self.int_v = np.clip(self.int_v + ev * dt, -20, 20)
            T = max(self.T_cmd_wing + pl.m * (G["k_v"] * ev + G["k_vi"] * self.int_v), 0.0)
        if self.mode in ("transition", "back", "wing"):
            qbar = 0.5 * pl.rho * float(aux["va_b"] @ aux["va_b"])
            share = float(np.clip((qbar - 5.0) / 35.0, 0.0, 1.0))
        M = self.attitude_moment(R, om, Rd, dt, freeze)
        self.T_cmd = T
        self.Rd = Rd
        return T, M, share


def _mix(pl: _Plant, T, M, Tmax):
    """Exact 4x4 inverse of the mixer; on saturation the M_x (hover-yaw) part is reduced first, a negative
    thrust is removed by adding collective (moments kept), then thrusts are clipped to [0, T_max]."""
    base = pl.Binv @ np.array([T, 0.0, M[1], M[2]])
    yaw = pl.Binv @ np.array([0.0, M[0], 0.0, 0.0])
    lam = 1.0
    for b, y, tm in zip(base, yaw, Tmax):
        if y > 1e-12 and b + y > tm:
            lam = min(lam, max((tm - b) / y, 0.0))
        elif y < -1e-12 and b + y < 0:
            lam = min(lam, max(b / -y, 0.0))
    Tv = base + lam * yaw
    if Tv.min() < 0:  # "air mode": add collective along the zero-moment direction instead of clipping moments
        c = pl.Binv[:, 0]
        Tv = Tv + max(-Tv / c) * c
    return np.clip(Tv, 0.0, Tmax)


def mixer_matrix(model) -> dict:
    """The hover/forward-flight mixer of the current configuration: rows [T_tot, M_x, M_y, M_z] (body),
    columns motors in joint-name order (M1..M4), plus motor positions about the CG and spin signs."""
    pl = _Plant(model, dict(COMMON))
    return {"B": pl.B, "motors": pl.motor_names, "r": pl.r_mot, "spin": pl.spin, "k_Q": pl.kQ}


# --------------------------------------------------------------------------- result
CHANNELS = ["t", "pos", "vel", "acc", "quat", "euler", "omega", "thrust", "rpm", "motor_current", "current",
            "power", "v_batt", "soc", "energy_used", "aero_force", "aero_moment", "elevon", "cmd", "airspeed",
            "alpha", "distance", "motor_temp_rise", "out_of_envelope", "outside_model_range"]


@dataclass
class SimResult:
    t: np.ndarray
    pos: np.ndarray
    vel: np.ndarray
    acc: np.ndarray
    quat: np.ndarray
    euler: np.ndarray
    omega: np.ndarray
    thrust: np.ndarray
    rpm: np.ndarray
    motor_current: np.ndarray  # (N, 4) A, motor (phase-side DC-equivalent) currents
    current: np.ndarray  # (N,) A, battery current
    power: np.ndarray  # (N,) W, battery terminal power
    v_batt: np.ndarray
    soc: np.ndarray
    energy_used: np.ndarray
    aero_force: np.ndarray
    aero_moment: np.ndarray
    elevon: np.ndarray
    cmd: np.ndarray
    airspeed: np.ndarray
    alpha: np.ndarray
    distance: np.ndarray
    motor_temp_rise: np.ndarray
    out_of_envelope: np.ndarray
    outside_model_range: np.ndarray  # airspeed >= 0.5 m/s and attached-flow range (|alpha| <= 12 deg, V >= 3) left
    warnings: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_csv(self, path) -> None:
        cols, data = [], []
        for name in CHANNELS:
            a = np.asarray(getattr(self, name), float)
            if a.ndim == 1:
                cols.append(name)
                data.append(a[:, None])
            else:
                cols += [f"{name}_{i}" for i in range(a.shape[1])]
                data.append(a)
        M = np.hstack(data)
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            f.write(f"# K09-L flightsim scenario={self.meta.get('scenario')} config_hash="
                    f"{self.meta.get('config_hash')} -- {VALIDITY}\n")
            w.writerow(cols)
            w.writerows(M.tolist())


def compare(a: SimResult, b: SimResult) -> dict:
    """Maximum absolute difference per channel, b interpolated onto a's time grid (overlap only)."""
    t = a.t[(a.t >= b.t[0]) & (a.t <= b.t[-1])]
    out = {}
    for name in CHANNELS[1:]:
        A, Bv = np.asarray(getattr(a, name), float), np.asarray(getattr(b, name), float)
        A = A[(a.t >= b.t[0]) & (a.t <= b.t[-1])]
        if A.ndim == 1:
            Bi = np.interp(t, b.t, Bv)
        else:
            Bi = np.column_stack([np.interp(t, b.t, Bv[:, i]) for i in range(Bv.shape[1])])
        out[name] = float(np.max(np.abs(A - Bi))) if len(t) else float("nan")
    return out


# --------------------------------------------------------------------------- simulation driver
def _params(scenario, duration, overrides) -> tuple[str, dict]:
    if isinstance(scenario, str):
        if scenario not in SCENARIOS:
            raise KeyError(f"unknown scenario '{scenario}' (known: {', '.join(SCENARIOS)})")
        name, base = scenario, SCENARIOS[scenario]
    else:  # a dict: 'name' selects the scenario logic (and its defaults), the other keys override them
        base = dict(scenario)
        name = base.pop("name", "custom")
        base = {**SCENARIOS.get(name, {}), **base}
    P = dict(COMMON)
    P.update(base)
    P.update(overrides)
    if duration is not None:
        P["duration"] = float(duration)
    return name, P


def _initial_state(pl: _Plant, P, ctrl: _Controller):
    x = np.zeros(NSTATE)
    Rh = ctrl.Rh
    if P["start"] == "ground":
        R = Rh
        low = -(pl.contacts @ R.T)[:, 2].max()  # height of CG above the lowest contact point
        x[IDX_P] = (0, 0, low - pl.m * pl.g / (K_GROUND * max(1, len(pl.contacts) // 2)))
        x[IDX_Q] = _R2q(R)
        return x
    if P["start"] == "cruise":
        V = P["airspeed"]
        qbar = 0.5 * pl.rho * V * V
        cl = pl.m * pl.g / (qbar * pl.S)
        a = (cl - pl.CL0) / pl.CLa
        Dg = qbar * pl.S * (pl.cd0 + pl.k_ind * cl * cl)
        R = Rh @ _rot_y(-(math.pi / 2 - a))
        x[IDX_P] = (0, 0, P["altitude"])
        x[IDX_V] = ctrl.fwd * V
        x[IDX_Q] = _R2q(R)
        ctrl.mode, ctrl.phi, ctrl.theta0 = "wing", math.pi / 2 - a, a
        ctrl.T_cmd_wing = Dg
        Tm = np.full(4, Dg / 4)
        Vax = np.full(4, V * math.cos(a))
    else:
        x[IDX_P] = (0, 0, P["altitude"])
        x[IDX_Q] = _R2q(Rh)
        Tm = pl.Binv @ np.array([pl.m * pl.g, 0, 0, 0])
        Vax = np.zeros(4)
    a_ = pl.ct0 * pl.rho * pl.D ** 4
    b_ = a_ * Vax / (pl.D * pl.J0)
    n = (b_ + np.sqrt(b_ * b_ + 4 * a_ * Tm)) / (2 * a_)  # steady speed for thrust Tm (see throttle_for)
    if not P["controller"] and P["open_loop_throttle"] is None:
        n[:] = 0.0  # controller off and no throttle given: motors stopped
    x[IDX_M] = 2 * math.pi * n
    return x


def simulate(model, scenario: Union[str, dict] = "hover", duration: Optional[float] = None, dt: float = 0.002,
             log_every: int = 10, **overrides) -> SimResult:
    """Run one scenario on the current configuration of ``model`` (see SCENARIOS and the module doc).
    ``overrides`` replace scenario parameters (failure injection, environment, gains)."""
    name, P = _params(scenario, duration, overrides)
    # the transition procedure is a design input of the model (revisions change it); explicit overrides win
    proc = {"transition_forward": {"pitch_end_deg": "transition_pitch_end_deg",
                                   "pitch_rate_deg": "transition_pitch_rate_deg",
                                   "target_airspeed": "transition_airspeed"},
            "transition_back": {"pitch_rate_deg": "transition_pitch_rate_deg", "airspeed": "transition_airspeed"}}
    for key, pname in proc.get(name, {}).items():
        q = model.params.get(pname)
        if key not in overrides and q is not None and q.value is not None:
            P[key] = float(q.value)
    pl = _Plant(model, P)
    ctrl = _Controller(pl, P, name)
    if name == "transition_forward":
        ctrl.T_cmd_wing = 0.5 * pl.rho * ctrl.v_target ** 2 * pl.S * (pl.cd0 + pl.k_ind * (
            pl.m * pl.g / (0.5 * pl.rho * ctrl.v_target ** 2 * pl.S)) ** 2)
    x = _initial_state(pl, P, ctrl)
    soc0 = float(P["soc0"])
    x[I_CHG] = (1 - soc0) * pl.q_usable
    nsteps = int(round(P["duration"] / dt))
    nlog = nsteps // log_every + 1
    L = {k: [] for k in CHANNELS}
    warnings: list = []
    elev = np.zeros(2)
    Ib_prev = 0.0
    u = np.zeros(4)
    fail = P["motor_failure"]
    fail_on = False
    sat_t = np.zeros(4)
    sat_warned = [False] * 4
    flags = dict(uv=False, soc=False, temp=False, impact=False, tip=False, diverge=False, asym=False, imax=False)
    u_base = None
    on_ground = P["start"] == "ground"
    env = P["envelope"]
    lo_soc = False
    for k in range(nsteps + 1):
        t = k * dt
        if fail and not fail_on and t >= fail.get("time", 0.0):
            fail_on = True
            pl.health[int(fail["motor"]) - 1] = float(fail["factor"])  # controller has not reacted yet this step
            warnings.append((t, f"failure injected: motor M{int(fail['motor'])} thrust x{fail['factor']:.2f}"))
        soc = 1 - x[I_CHG] / pl.q_usable
        if soc <= 0 and not lo_soc:
            lo_soc = True
            warnings.append((t, "usable battery capacity exhausted (SOC model clipped at v_low)"))
        Voc = pl.v_low + (pl.v_full - pl.v_low) * min(max(soc, 0.0), 1.0)
        Vb = max(Voc - Ib_prev * pl.r_int, 0.3 * pl.v_low)
        dx0, aux = _deriv(x, t, u, elev, Vb, pl, P, aux=True)
        Ib_prev = aux["Ib"]
        on_ground = bool(aux["Fg"][2] > 0.05 * pl.m * pl.g)
        # ---- control
        e_cmd = np.zeros(2)
        if P["open_loop_throttle"] is not None:
            u = np.clip(np.asarray(P["open_loop_throttle"], float), 0, pl.u_max)
        elif P["controller"]:
            out = ctrl.update(t, x, dt, on_ground, aux)
            if out is None:
                u = np.zeros(4)
            else:
                T, M, share = out
                if share > 0:
                    Be = aux["Be"][:2]
                    try:
                        e_cmd = np.linalg.solve(Be, share * M[:2])
                    except np.linalg.LinAlgError:
                        e_cmd = np.zeros(2)
                    e_cmd = np.clip(e_cmd, -math.radians(30), math.radians(30))
                Mm = M - aux["Be"] @ elev
                Tmax = pl.thrust_max(aux["Vax"], aux["n"], Vb)
                Ti = _mix(pl, T, Mm, Tmax)
                u = pl.throttle_for(Ti, aux["Vax"], aux["n"], Vb)
        else:
            u = np.zeros(4)
        elev = elev + np.clip(e_cmd - elev, -ELEVON_RATE * dt, ELEVON_RATE * dt)
        # ---- monitoring
        sat = u >= 0.995 * pl.u_max
        sat_t = np.where(sat, sat_t + dt, 0.0)
        for i in range(4):
            if sat_t[i] > 0.5 and not sat_warned[i]:
                sat_warned[i] = True
                warnings.append((t, f"motor M{i + 1} throttle saturated > 0.5 s (control authority lost)"))
        if fail_on and u_base is None:
            u_base = u / max(u.mean(), 1e-9)  # throttle distribution when the failure is injected
        if u_base is not None and not flags["asym"] and u.mean() > 0.05:
            d = u / u.mean() - u_base
            if np.abs(d).max() > 0.05:
                flags["asym"] = True
                i = int(np.argmax(np.abs(d)))
                warnings.append((t, f"throttle distribution changed by {d[i] * 100:+.0f} % on M{i + 1} "
                                    "(degraded motor or CG shift detected)"))
        if Vb < pl.v_low and not flags["uv"] and k > 0:
            flags["uv"] = True
            warnings.append((t, f"battery voltage under load {Vb:.2f} V below v_low {pl.v_low:.1f} V"))
        if soc < 0.1 and not flags["soc"]:
            flags["soc"] = True
            warnings.append((t, f"battery state of charge low ({soc * 100:.0f} % of usable)"))
        if aux["I_m"].max() > pl.imax and not flags["imax"]:
            flags["imax"] = True
            i = int(np.argmax(aux["I_m"]))
            warnings.append((t, f"motor M{i + 1} current {aux['I_m'][i]:.1f} A above the catalog 30-s rating "
                                f"{pl.imax:.0f} A"))
        if x[IDX_TH].max() > 60 and not flags["temp"]:
            flags["temp"] = True
            warnings.append((t, "motor temperature rise > 60 K (provisional thermal model)"))
        if on_ground and x[IDX_V][2] < -1.5 and not flags["impact"]:
            flags["impact"] = True
            warnings.append((t, f"hard ground contact at {x[IDX_V][2]:.2f} m/s"))
        if on_ground and t > 0.5 and (-_q2R(x[IDX_Q])[:, 0])[2] < 0.5 and not flags["tip"]:
            flags["tip"] = True
            warnings.append((t, "tip-over on the ground (nose > 60 deg from vertical)"))
        if ctrl.mode == "hover" and P["controller"] and ctrl.p_sp is not None and not flags["diverge"] \
                and np.linalg.norm(ctrl.p_sp - x[IDX_P]) > 10:
            flags["diverge"] = True
            warnings.append((t, "position error > 10 m (hover control diverged)"))
        if not np.all(np.isfinite(x)):
            warnings.append((t, "state became non-finite; simulation stopped"))
            break
        # ---- log
        if k % log_every == 0:
            va = aux["va_b"]
            Va = float(np.linalg.norm(va))
            al = math.degrees(math.atan2(-va[2], -va[0])) if Va > 1e-3 else 0.0
            row = dict(t=t, pos=x[IDX_P], vel=x[IDX_V], acc=aux["acc"], quat=x[IDX_Q], euler=euler_frd_ned(x[IDX_Q]),
                       omega=x[IDX_W], thrust=aux["T"], rpm=aux["rpm"], motor_current=aux["I_m"], current=aux["Ib"],
                       power=aux["Pb"], v_batt=Vb, soc=soc, energy_used=x[I_EN], aero_force=aux["Fa"],
                       aero_moment=aux["Ma"], elevon=np.degrees(elev), cmd=u, airspeed=Va, alpha=al,
                       distance=x[I_DIST], motor_temp_rise=x[IDX_TH])
            for key, val in row.items():
                L[key].append(np.array(val, float))
            if env:
                a_lo, a_hi = env.get("alpha_deg", (-1e9, 1e9))
                v_lo, v_hi = env.get("airspeed", (-1e9, 1e9))
                L["out_of_envelope"].append(not (a_lo <= al <= a_hi and v_lo <= Va <= v_hi))
            else:
                L["out_of_envelope"].append(True)  # no validated envelope exists yet
            L["outside_model_range"].append(bool(Va >= 0.5 and (abs(al) > 12 or Va < 3.0)))
        if k == nsteps:
            break
        # ---- RK4
        k1 = dx0
        k2 = _deriv(x + 0.5 * dt * k1, t + 0.5 * dt, u, elev, Vb, pl, P)
        k3 = _deriv(x + 0.5 * dt * k2, t + 0.5 * dt, u, elev, Vb, pl, P)
        k4 = _deriv(x + dt * k3, t + dt, u, elev, Vb, pl, P)
        x = x + dt / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
        x[IDX_Q] /= np.linalg.norm(x[IDX_Q])
        x[IDX_M] = np.maximum(x[IDX_M], 0.0)
    warnings += [(t_, s) for t_, s in ctrl.events if s.startswith("WARNING")]
    arr = {k: np.asarray(v, float) for k, v in L.items()}
    arr["out_of_envelope"] = np.asarray(L["out_of_envelope"], bool)
    arr["outside_model_range"] = np.asarray(L["outside_model_range"], bool)
    outcome = _outcome(name, P, arr, ctrl)
    meta = dict(scenario=name, parameters=P, config_hash=model.config_hash(), config=dict(model.config),
                mass=pl.m, cg=pl.cg.tolist(), inertia=pl.I.tolist(), rho=pl.rho, dt=dt, log_every=log_every,
                motors=pl.motor_names, spin_reaction_sign=pl.spin.tolist(),
                slipstream_fraction_geometric=float(pl.slip_frac_geom[:pl.n_wing].mean()),
                events=list(ctrl.events), outcome=outcome, assumptions=list(ASSUMPTIONS), validity=VALIDITY,
                envelope=env or "none validated: every step flagged out_of_envelope",
                evidence_class="reduced_order (provisional inputs)")
    return SimResult(warnings=sorted(warnings, key=lambda w: w[0]), meta=meta, **arr)


def _outcome(name, P, a, ctrl) -> dict:
    out: dict = {"final_mode": ctrl.mode}
    pos = a["pos"]
    if len(a["t"]) == 0:
        return out
    if P["start"] == "air" and name in ("hover", "gust_response", "payload", "motor_degradation",
                                         "battery_decline"):
        sp = np.array([0, 0, P["altitude"]])
        out["max_position_error"] = float(np.max(np.linalg.norm(pos - sp, axis=1)))
        out["final_position_error"] = float(np.linalg.norm(pos[-1] - sp))
    if name == "transition_forward":
        conv = any("wing-borne" in s for _, s in ctrl.events)
        out.update(transition_converged=conv, final_airspeed=float(a["airspeed"][-1]),
                   altitude_change=float(pos[-1, 2] - pos[0, 2]), min_altitude=float(pos[:, 2].min()),
                   max_alpha_deg_above_3ms=float(np.max(np.abs(a["alpha"][a["airspeed"] > 3.0]), initial=0.0)))
    if name == "transition_back":
        conv = any("hover mode" in s for _, s in ctrl.events)
        out.update(back_transition_converged=conv, final_ground_speed=float(np.linalg.norm(a["vel"][-1])),
                   altitude_change=float(pos[-1, 2] - pos[0, 2]))
    if name == "waypoints":
        out["waypoints_reached"] = ctrl.wp_i
    if name == "land":
        out["touchdown_time"] = ctrl.t_touch
    out["energy_used_J"] = float(a["energy_used"][-1])
    return out
