"""Assembly + multibody examples: a two-finger gripper and a four-rotor drone assembly.

    python -m opencae.examples.mbd_examples [--out DIR]

writes ``gripper.ocae`` and ``drone_rotors.ocae`` (with their ``_data`` folders) to ``opencae/examples/projects``.
Studies are saved unsolved: results are produced by running them (``opencae.mbd.run_study``).

Both examples use the same implementation: bodies from CAD primitives, mass properties from the B-rep and the
material density, joints, drivers, couplings, applied loads. Geometry parameters are in the module constants
:data:`GRIPPER` and :data:`DRONE` (the tests' hand calculations use them).
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np

from opencae.app.session import Session
from opencae.assembly import (AppliedLoad, Assembly, Component, Coupling, Driver, Joint, MBDSettings, Profile,
                              RigidBody, ThrustModel, set_mbd)
from opencae.assembly.massprops import update_body_masses
from opencae.project.model import Feature

DEFAULT_OUT = Path(__file__).parent / "projects"

# Gripper geometry (mm, N, s). The hinge axes are the palm normal (z). The fingers hang along +y from the pivots
# (+-p, 0, 0); thickness h is the in-plane dimension, width b is along the hinge axis.
GRIPPER = dict(
    palm=(80.0, 20.0, 20.0),  # box dx, dy, dz; its front face is y = 0, centred on x = 0 and z = 0
    L=60.0, b=10.0, h=4.0,  # finger length (y), width along the hinge axis (z), thickness in the plane (x)
    p=30.0,  # pivot at x = -p (left finger) and x = +p (right finger)
    closed_deg=20.0,  # closing angle of study A and the hold angle of study C
    limit_open_deg=10.0, limit_closed_deg=30.0,  # joint limits of the left finger [-10, +30] deg (right mirrored)
    motor_torque_Nm=0.002,  # study B: constant motor torque on the left finger joint
    tip_force_N=5.0,  # study C: outward load on each fingertip
    material="mat_al6061",
    ratio=-1.0,
)

# Drone assembly (mm): square frame plate, four fixed motor housings, four rotor discs.
DRONE = dict(
    plate=(120.0, 120.0, 6.0),  # box dx, dy, dz centred on x = y = 0, z in [0, 6]
    arm=40.0,  # housings / rotors at (+-arm, +-arm)
    housing_r=8.0, housing_h=12.0,  # cylinder on the plate, z in [6, 18]
    rotor_r=25.0, rotor_t=2.0,  # disc on the housing, z in [18, 20]
    rpm=6000.0, ramp_s=0.5, duration_s=1.0,
    material="mat_al6061",
)

TRANSMISSION_NOTE = ("Ideal transmission: q_right = ratio x q_left (ratio -1: mirror motion). The coupling is "
                     "massless, rigid, lossless, without backlash and without friction; the right finger is "
                     "driven by the constraint, not by a second motor. The motor effort reported on the left "
                     "joint includes the effort that the constraint transmits to the right finger.")


# --------------------------------------------------------------------------------------- fingertip gap (pure)
def gripper_gap_mm(theta_rad: float, g: dict = GRIPPER) -> float:
    """Distance between the inner fingertip corners (mm) when both fingers have closed by ``theta_rad``.

    The inner tip corners start at (-p + h/2, L) and (p - h/2, L); each finger turns by theta about its pivot, which
    gives x_r - x_l = 2 p - h cos(theta) - 2 L sin(theta) (the tips stay at equal y)."""
    return 2.0 * g["p"] - g["h"] * math.cos(theta_rad) - 2.0 * g["L"] * math.sin(theta_rad)


def _unit(v) -> np.ndarray:
    v = np.asarray(v, float)
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else np.array([0.0, 0.0, 1.0])


def inner_tip_pairs(assembly, bodies):
    """Two ``(component id, CAD-frame point)`` pairs, the inner tip corners of the two fingers of the one coupling
    of two revolute joints on the same parent, or ``None`` when the assembly is not such a gripper.

    ``bodies`` maps a feature id to an object with ``bbox`` (``(lo, hi)`` corners) and ``size`` (session bodies).
    The tip is the bbox face farthest from the pivot, its inner corner the one nearest the other pivot."""
    if len(assembly.couplings) != 1:
        return None
    c = assembly.couplings[0]
    jl, jf = assembly.joint(c.leader), assembly.joint(c.follower)
    if not (jl and jf and jl.kind == jf.kind == "revolute" and jl.parent == jf.parent):
        return None
    pts = []
    for j, other in ((jl, jf), (jf, jl)):
        rb = assembly.body(j.child)
        comp = assembly.component(rb.component_ids[0]) if rb and len(rb.component_ids) == 1 else None
        gb = bodies.get(comp.body_id) if comp else None
        if gb is None:
            return None
        lo, hi = gb.bbox
        cad = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        P = comp.placement_matrix()
        ref = (P[:3, :3] @ cad.T).T + P[:3, 3]
        o = np.asarray(j.origin, float)
        u = _unit(ref.mean(axis=0) - o)
        tol = 1e-6 * max(gb.size, 1.0)
        far = (ref - o) @ u
        cand = np.nonzero(far >= far.max() - tol)[0]
        e = _unit(np.asarray(other.origin, float) - o)
        score = ref[cand] @ e
        inner = cand[score >= score.max() - tol]
        pts.append((comp.id, cad[inner].mean(axis=0)))
    return pts


def tip_gap_from_poses(pairs, transforms: dict) -> float:
    """Distance between the two tip points moved by ``transforms`` (``{component id: 4x4}``, e.g. from
    :func:`opencae.mbd.pose_transforms`), in the project length unit."""
    (c1, p1), (c2, p2) = pairs
    a = transforms[c1] @ np.append(np.asarray(p1, float), 1.0)
    b = transforms[c2] @ np.append(np.asarray(p2, float), 1.0)
    return float(np.linalg.norm(a[:3] - b[:3]))


def _body(s: Session, name: str, shape: str, params: dict, material: str, grounded: bool = False):
    f = s.add_feature(Feature("primitive", {"shape": shape, **params}), name)
    s.project.assign_material(f.id, material)
    comp = Component(name=name, body_id=f.id, source=f"{shape} primitive")
    rb = RigidBody(name=name, component_ids=[comp.id], grounded=grounded)
    s.project.assembly.components.append(comp)
    s.project.assembly.bodies.append(rb)
    return f, rb


def _mbd_study(s: Session, name: str, first: bool, notes: str, settings: MBDSettings):
    bodies = [f.id for f in s.project.geometry.features if f.creates_body()]
    if first:  # turn the project's default study into the first multibody study
        st = s.study
        st.name, st.analysis.analysis_type, st.analysis.title = name, "mbd", "Multibody dynamics"
        st.body_ids = bodies
        st.mesh_settings.body = bodies[0]
    else:
        st = s.new_study(name, "mbd", body_ids=bodies, activate=False)
    st.notes = notes
    set_mbd(st, settings)
    return st


def build_gripper(directory: Path = DEFAULT_OUT, name: str = "gripper") -> Path:
    g = GRIPPER
    L, b, h, p = g["L"], g["b"], g["h"], g["p"]
    s = Session()
    s.new("Two-finger gripper", "mm-N-s")
    s.project.description = (
        f"Palm (grounded) with two {L:g} x {b:g} x {h:g} mm aluminium fingers on revolute hinges at x = +-{p:g} mm "
        "(axes along the palm normal), one motor on the left finger and an ideal mirror transmission to the right "
        "finger. Study A: prescribed closing. B: constant motor torque. C: static hold with explicit fingertip loads.")
    px, py, pz = g["palm"]
    palm_f, palm = _body(s, "Palm", "box", {"dx": px, "dy": py, "dz": pz, "position": [-px / 2, -py, -pz / 2]},
                         g["material"], grounded=True)
    left_f, left = _body(s, "Left finger", "box", {"dx": h, "dy": L, "dz": b, "position": [-p - h / 2, 0, -b / 2]},
                         g["material"])
    right_f, right = _body(s, "Right finger", "box", {"dx": h, "dy": L, "dz": b, "position": [p - h / 2, 0, -b / 2]},
                           g["material"])
    lo, hi = math.radians(g["limit_open_deg"]), math.radians(g["limit_closed_deg"])
    # axis +z reversed: a positive coordinate rotates the fingertips towards each other (closing)
    jl = Joint(name="Left hinge", kind="revolute", parent=palm.id, child=left.id, origin=[-p, 0, 0],
               axis=[0, 0, 1], reversed=True, limits=[-lo, hi])
    jr = Joint(name="Right hinge", kind="revolute", parent=palm.id, child=right.id, origin=[p, 0, 0],
               axis=[0, 0, 1], reversed=True, limits=[-hi, lo])
    asm: Assembly = s.project.assembly
    asm.joints += [jl, jr]
    asm.couplings.append(Coupling(name="Finger transmission", leader=jl.id, follower=jr.id, ratio=g["ratio"],
                                  note=TRANSMISSION_NOTE))
    problems = update_body_masses(s.project, bodies=s.bodies)
    assert not problems, problems

    closed = math.radians(g["closed_deg"])
    tip = [(-p, L, 0.0), (p, L, 0.0)]
    F = g["tip_force_N"]
    _mbd_study(s, "A - prescribed closing", True,
               "Position ramp 0 -> closing angle on the left finger over 1 s (prescribed, kinematic); the right "
               "finger follows through the ideal transmission. " + TRANSMISSION_NOTE
               + " The actuator effort is the inverse-dynamics effort (no motor model).",
               MBDSettings(drivers=[Driver(name="Motor (left finger): closing ramp", joint=jl.id, mode="position",
                                           profile=Profile("ramp", t0=0.0, t1=1.0, v0=0.0, v1=closed))],
                           duration_s=1.0, dt_s=1e-4, output_dt_s=1e-2, gravity=[0.0, 0.0, -9.80665]))
    _mbd_study(s, "B - torque-driven", False,
               "Constant motor torque on the left finger (torque-driven, forward dynamics), gravity off, "
               "start at rest. The initial angular acceleration is torque / (I_left + ratio^2 I_right) about the "
               "pivots. " + TRANSMISSION_NOTE,
               MBDSettings(drivers=[Driver(name="Motor (left finger): constant torque", joint=jl.id, mode="effort",
                                           profile=Profile("constant", value=g["motor_torque_Nm"]))],
                           duration_s=0.08, dt_s=1e-4, output_dt_s=2e-3, gravity=[0.0, 0.0, 0.0]))
    _mbd_study(s, "C - static hold with fingertip load", False,
               "Prescribed hold at the closing angle (velocity zero) with an explicit outward force on each "
               "fingertip, as if squeezing an object (no contact model). The loads are in the finger frame, "
               "perpendicular to the finger, so the lever arm is the finger length. Gravity off. The joint "
               "reactions are the load for a structural static-hold study. " + TRANSMISSION_NOTE,
               MBDSettings(drivers=[Driver(name="Motor (left finger): hold", joint=jl.id, mode="position",
                                           profile=Profile("constant", value=closed))],
                           loads=[AppliedLoad(name="Left fingertip load", body=left.id, point=list(tip[0]),
                                              force_N=[-F, 0.0, 0.0], frame="body",
                                              note="outward (away from the other finger)"),
                                  AppliedLoad(name="Right fingertip load", body=right.id, point=list(tip[1]),
                                              force_N=[F, 0.0, 0.0], frame="body",
                                              note="outward (away from the other finger)")],
                           duration_s=0.1, dt_s=1e-5, output_dt_s=1e-3, gravity=[0.0, 0.0, 0.0]))
    return s.save(Path(directory) / f"{name}.ocae")


def build_drone_rotors(directory: Path = DEFAULT_OUT, name: str = "drone_rotors") -> Path:
    d = DRONE
    s = Session()
    s.new("Drone rotor assembly", "mm-N-s")
    s.project.description = (
        "Frame plate (grounded), four motor housings fixed to it and four rotor discs on revolute joints "
        "(alternating CCW / CW), velocity-driven up to the commanded speed. The rotor spin-up is a kinematic "
        "animation of the commanded rotation: it does not predict thrust or flight. Thrust coefficients are "
        "stored as prescribed inputs (not CFD).")
    pdx, pdy, pdz = d["plate"]
    _, frame = _body(s, "Frame", "box", {"dx": pdx, "dy": pdy, "dz": pdz, "position": [-pdx / 2, -pdy / 2, 0]},
                     d["material"], grounded=True)
    asm: Assembly = s.project.assembly
    omega = d["rpm"] * 2 * math.pi / 60
    drivers, thrust = [], []
    a = d["arm"]
    for k, (sx, sy, cw) in enumerate(((1, 1, False), (-1, 1, True), (-1, -1, False), (1, -1, True)), start=1):
        x, y = sx * a, sy * a
        _, hous = _body(s, f"Motor housing {k}", "cylinder",
                        {"radius": d["housing_r"], "height": d["housing_h"], "position": [x, y, pdz]}, d["material"])
        z_rot = pdz + d["housing_h"]
        _, rotor = _body(s, f"Rotor {k}", "cylinder",
                         {"radius": d["rotor_r"], "height": d["rotor_t"], "position": [x, y, z_rot]}, d["material"])
        asm.joints.append(Joint(name=f"Housing {k} mount", kind="fixed", parent=frame.id, child=hous.id,
                                origin=[x, y, pdz]))
        jr = Joint(name=f"Rotor {k} axis", kind="revolute", parent=hous.id, child=rotor.id,
                   origin=[x, y, z_rot], axis=[0, 0, 1], reversed=cw)  # positive speed = CW when reversed
        asm.joints.append(jr)
        drivers.append(Driver(name=f"Rotor {k} speed ({'CW' if cw else 'CCW'})", joint=jr.id, mode="velocity",
                              profile=Profile("ramp", t0=0.0, t1=d["ramp_s"], v0=0.0, v1=omega)))
        thrust.append(ThrustModel(rotor_joint=jr.id, kt=1.0e-8, kq=2.0e-10, source="prescribed",
                                  units_note="thrust = kt omega^2 [N], drag torque = kq omega^2 [N m], omega in rad/s",
                                  validity="illustrative prescribed coefficients, not measured and not CFD; the "
                                           "rotor animation does not predict thrust or flight"))
    asm.thrust_models += thrust
    problems = update_body_masses(s.project, bodies=s.bodies)
    assert not problems, problems
    _mbd_study(s, "Rotor spin-up", True,
               f"Velocity ramp 0 -> {d['rpm']:g} rpm in {d['ramp_s']:g} s on every rotor (prescribed, kinematic); "
               "the frame is grounded. Change one rotor's speed by editing only its driver profile. The motion "
               "is an animation of the commanded rotation, not a flight prediction.",
               MBDSettings(drivers=drivers, duration_s=d["duration_s"], dt_s=1e-3, output_dt_s=1e-2,
                           gravity=[0.0, 0.0, -9.80665]))
    return s.save(Path(directory) / f"{name}.ocae")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args(argv)
    for fn in (build_gripper, build_drone_rotors):
        print(fn(Path(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
