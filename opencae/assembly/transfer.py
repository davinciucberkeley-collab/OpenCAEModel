"""Multibody -> structural hand-over: a static-hold load case for one component at one time.

:func:`make_static_hold_study` creates a linear static study in the same project that contains only the selected
component's geometry (in its ORIGINAL CAD placement), its material, a fixed support on the root face and the
applied force(s) of the multibody study, expressed in the component's CAD frame, over the load face. The root
reaction of the multibody run is NOT applied (the support makes its own); it is stored in
``TransferRecord.equilibrium_check`` to show that the handed-over load case is in equilibrium.

Frames. The multibody pose of a body at time t is a rigid motion of its reference configuration: body frame
orientation ``R(t)`` (``result.body_quat``) and component placement ``P`` (4x4, CAD -> assembly reference). An
assembly-frame vector ``v`` is expressed in the CAD frame as ``Rp^T R(t)^T v``; a body-frame load (it rotates
with the body) as ``Rp^T v``. Positions in the CAD frame: ``Rp^T (x_ref - t_p)``.

Validity. A static hold neglects inertia: it is valid only while the component is not accelerating. The
transfer is refused when ``max |q_ddot|`` of the joints that carry the component exceeds ``accel_tol`` (unless
``force_allow=True``); the measured value is stored in the record.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.assembly.model import GROUND, get_mbd
from opencae.mbd.results import MBDResult, quat_to_matrix
from opencae.project.model import (AnalysisSettings, Constraint, EntityRef, Load, MaterialAssignment, MeshSettings,
                                   Study)
from opencae.utilities.units import STANDARD_GRAVITY_SI, get_unit_system

STATIC_HOLD_NOTE = ("static hold: no inertia; valid only when the component is not accelerating - "
                    "check: max |q̈| at t")
ACCEL_TOL = 1e-3  # rad/s^2 (revolute) or project length/s^2 (prismatic)


class TransferError(ValueError):
    """The multibody -> structural transfer cannot be made (the message says why)."""


@dataclass
class TransferRecord:
    """Provenance of a handed-over load case (stored on the structural study, serialisable)."""

    source_study: dict = field(default_factory=dict)  # {"id", "name"}
    time_s: float = 0.0
    body: dict = field(default_factory=dict)  # {"id" rigid body, "name", "geometry_body_id", "component_id"}
    frame: str = ""  # description of the frame of force_N / moment_Nm / region
    region: dict = field(default_factory=dict)  # {"root_face": {..., "area"}, "load_face": {..., "area",
    #                                             "centroid"}, "application_points": [...]}
    force_N: list = field(default_factory=lambda: [0.0, 0.0, 0.0])  # resultant applied force, CAD frame
    moment_Nm: list = field(default_factory=lambda: [0.0, 0.0, 0.0])  # applied moment about the root joint point
    moment_about: dict = field(default_factory=dict)  # {"point": [...] CAD frame, project length, "joint"}
    units: str = "N, mm, MPa"
    equilibrium_check: dict = field(default_factory=dict)  # root_reaction_N, root_moment_Nm, residual {...}
    note: str = STATIC_HOLD_NOTE
    acceleration: dict = field(default_factory=dict)  # {"max_abs_qdd", "tolerance", "joints", "accelerating"}
    loads: list = field(default_factory=list)  # one entry per applied load: name, point, force_N, moment_Nm (CAD)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["TransferRecord"]:
        if not d:
            return None
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in names})


def get_transfer(study) -> Optional[TransferRecord]:
    """The transfer record of a study (``None`` if it was not made by :func:`make_static_hold_study`)."""
    return TransferRecord.from_dict(getattr(study, "transfer", None))


def _vec(v) -> np.ndarray:
    return np.asarray(v, float).reshape(3)


def _face_info(ref: EntityRef, role: str, geometry_id: str, bodies: Optional[dict]) -> dict:
    if not isinstance(ref, EntityRef) or ref.kind != "face":
        raise TransferError(f"the {role} must be a face reference (EntityRef of kind 'face')")
    if ref.body != geometry_id:
        raise TransferError(f"the {role} belongs to body '{ref.body}', not to the selected component "
                            f"('{geometry_id}')")
    area, centroid = None, None
    if bodies and geometry_id in bodies and 0 <= ref.index < len(bodies[geometry_id].faces):
        f = bodies[geometry_id].faces[ref.index]
        area, centroid = float(f.area), [float(x) for x in f.centroid]
    if area is None:
        sig = ref.signature or {}
        if "area" not in sig or "centroid" not in sig:
            raise TransferError(f"the {role} has no area / centroid (no signature and no geometry to read it from)")
        area, centroid = float(sig["area"]), [float(x) for x in sig["centroid"]]
    return {"kind": "face", "body": ref.body, "index": int(ref.index), "area": area, "centroid": centroid}


def _joint_chain(asm, body_id: str) -> list:
    """Joints from the ground down to ``body_id`` (the last one is the root joint of the body)."""
    chain, cur = [], body_id
    while True:
        js = [j for j in asm.joints if j.child == cur]
        if not js:
            break
        chain.append(js[0])
        if js[0].parent == GROUND:
            break
        cur = js[0].parent
    return list(reversed(chain))


def _load_result(project, mbd_study, result, data_dir) -> MBDResult:
    if result is not None:
        return result
    rs = getattr(mbd_study, "results", None)
    if data_dir is None or rs is None or not rs.directory:
        raise TransferError("no multibody result: pass result=... (an MBDResult) or data_dir=... of a project "
                            "whose multibody study has been run")
    return MBDResult.load(Path(data_dir) / rs.directory)


def make_static_hold_study(project, mbd_study, time_s: float, body_id: str, root_face_ref: EntityRef,
                           load_face_ref: EntityRef, name: Optional[str] = None, result: Optional[MBDResult] = None,
                           *, data_dir: Optional[Path] = None, force_allow: bool = False,
                           accel_tol: float = ACCEL_TOL, mesh_divisions: int = 4) -> tuple[Study, TransferRecord]:
    """Create a static-hold structural study of one component from a multibody study at ``time_s``.

    ``body_id``: the rigid body id (or the id of its single geometry body). ``root_face_ref`` /
    ``load_face_ref``: face references on the component's geometry body (fixed support / region over which the
    applied force acts). ``result``: the multibody result (default: loaded from ``data_dir``). ``mesh_divisions``:
    target number of element edges across the smallest bounding-box dimension (mesh ``size_max``). Returns the
    new study (appended to ``project.studies``, not activated) and its :class:`TransferRecord`.
    """
    asm = project.assembly
    res = _load_result(project, mbd_study, result, data_dir)
    t = float(time_s)
    try:
        snap = res.at(t)
    except ValueError as exc:
        raise TransferError(str(exc)) from exc

    # ---- body, component, root joint -------------------------------------------------------------------
    rb = asm.body(body_id) or next((b for b in asm.bodies if any(
        (asm.component(c) is not None and asm.component(c).body_id == body_id) for c in b.component_ids)), None)
    if rb is None:
        raise TransferError(f"body '{body_id}' is not a rigid body (or component geometry) of the assembly")
    if rb.grounded:
        raise TransferError(f"body '{rb.name}' is grounded: it has no joint reaction to hand over")
    if len(rb.component_ids) != 1:
        raise TransferError(f"body '{rb.name}' has {len(rb.component_ids)} components; the static-hold transfer "
                            "supports one component per body")
    comp = asm.component(rb.component_ids[0])
    gid = comp.body_id
    chain = _joint_chain(asm, rb.id)
    if not chain:
        raise TransferError(f"body '{rb.name}' is not attached by a joint")
    root_joint = chain[-1]
    downstream = [j.name for j in asm.joints if j.parent == rb.id]
    if downstream:
        raise TransferError(f"body '{rb.name}' carries other bodies through joints ({', '.join(downstream)}); "
                            "their reactions would also load it - only end links are supported")
    if rb.id not in res.body_quat:
        raise TransferError(f"body '{rb.name}' is not in the multibody result")

    bodies = None
    try:
        from opencae.assembly.massprops import _regenerate
        bodies = _regenerate(project, Path(data_dir) if data_dir else None)
    except Exception:  # the faces' stored signatures are used instead
        bodies = None
    root = _face_info(root_face_ref, "root face", gid, bodies)
    load = _face_info(load_face_ref, "load face", gid, bodies)
    if root["index"] == load["index"]:
        raise TransferError("the root face and the load face are the same face")

    # ---- acceleration check ---------------------------------------------------------------------------
    qdd = {j.name: float(abs(snap["joint_qdd"][j.id])) for j in chain}
    qd = {j.name: float(abs(snap["joint_qd"][j.id])) for j in chain}
    max_qdd, max_qd = max(qdd.values()), max(qd.values())
    accelerating = max_qdd > accel_tol
    warnings: list[str] = []
    if accelerating:
        msg = (f"the component '{rb.name}' is accelerating at t = {t:g} s (max |q̈| = {max_qdd:.4g} > "
               f"{accel_tol:g} rad/s² or length/s²): a static-hold study neglects inertia and is only valid while "
               "the component is not accelerating. Choose a time at rest, or pass force_allow=True to transfer "
               "anyway")
        if not force_allow:
            raise TransferError(msg)
        warnings.append("transferred although the component is accelerating (force_allow): " + msg)
    if max_qd > 1e-6:
        warnings.append(f"the component is moving (max |q̇| = {max_qd:.4g}): centripetal / Coriolis effects are "
                        "neglected by the static hold")

    # ---- frames ---------------------------------------------------------------------------------------
    us = get_unit_system(project.unit_system)
    L2M = 1.0 / us.length_from_si  # project length -> m
    R = quat_to_matrix(snap["body_quat"][rb.id])
    P = comp.placement_matrix()
    Rp, tp = P[:3, :3], P[:3, 3]
    to_cad_asm = Rp.T @ R.T  # assembly-frame vector -> CAD frame
    to_cad_body = Rp.T  # body-frame vector (reference-orientation axes) -> CAD frame

    def point_cad(x_ref):  # reference-configuration point -> CAD frame
        return Rp.T @ (_vec(x_ref) - tp)

    origin_cad = point_cad(root_joint.origin)
    info = next(b for b in res.meta["bodies"] if b["id"] == rb.id)
    com0 = _vec(info["com0"])

    # ---- applied loads of the multibody study ----------------------------------------------------------
    settings = get_mbd(mbd_study)
    mine = [ld for ld in settings.loads if ld.body == rb.id]
    F_tot = np.zeros(3)  # N, CAD
    M_tot = np.zeros(3)  # N m about the root joint point, CAD
    entries, sloads = [], []
    centroid = _vec(load["centroid"])
    tol_pt = 1e-6 * max(1.0, float(np.linalg.norm(centroid)))
    for ld in mine:
        fac = 1.0 if ld.profile is None else float(ld.profile.value_at(t))
        F = fac * _vec(ld.force_N)
        M = fac * _vec(ld.moment_Nm)
        T = to_cad_body if ld.frame == "body" else to_cad_asm
        Fc, Mc = T @ F, T @ M
        pc = point_cad(ld.point)
        F_tot += Fc
        M_tot += Mc + np.cross((pc - origin_cad) * L2M, Fc)
        entries.append({"name": ld.name, "point": pc.tolist(), "force_N": Fc.tolist(), "moment_Nm": Mc.tolist(),
                        "frame_in_mbd": ld.frame, "profile_factor": fac})
        if np.linalg.norm(Fc) > 0:
            mag = float(np.linalg.norm(Fc))
            if np.linalg.norm(pc - centroid) <= tol_pt:
                sloads.append(Load("force", [_ref(load_face_ref)], {"magnitude": mag,
                                                                    "direction": (Fc / mag).tolist()},
                                   name=ld.name or "Applied force"))
            else:  # the force acts away from the face centroid: uniform traction + the moment of the offset
                sloads.append(Load("remote_force", [_ref(load_face_ref)],
                                   {"magnitude": mag, "direction": (Fc / mag).tolist(), "point": pc.tolist()},
                                   name=(ld.name or "Applied force") + " (at its point)"))
        if np.linalg.norm(Mc) > 0:
            mm = float(np.linalg.norm(Mc))
            sloads.append(Load("moment", [_ref(load_face_ref)],
                               {"magnitude": mm * us.length_from_si, "direction": (Mc / mm).tolist()},
                               name=(ld.name or "Applied") + " moment"))
    g = _vec(settings.gravity)
    if np.linalg.norm(g) > 0:  # weight: m g at the COM (also a structural gravity load)
        mass = float(info["mass_kg"])
        W = to_cad_asm @ (mass * g)
        r_w = to_cad_body @ ((com0 - _vec(root_joint.origin)) * L2M)  # COM relative to the joint point (CAD axes)
        F_tot += W
        M_tot += np.cross(r_w, W)
        gmag = float(np.linalg.norm(g))
        sloads.append(Load("gravity", [EntityRef("body", gid)],
                           {"magnitude": gmag * us.accel_from_si, "direction": (to_cad_asm @ g / gmag).tolist()},
                           name="Weight"))
        entries.append({"name": "Weight (gravity)", "point": point_cad(com0).tolist(), "force_N": W.tolist(),
                        "moment_Nm": [0.0, 0.0, 0.0], "frame_in_mbd": "assembly", "profile_factor": 1.0})
    if not sloads:
        raise TransferError(f"the multibody study '{mbd_study.name}' has no applied load on '{rb.name}' "
                            "(and gravity is off): there is nothing to hand over. Add an explicit fingertip load")

    # ---- root reaction (equilibrium check only) -----------------------------------------------------------
    Fr = to_cad_asm @ _vec(snap["reaction_force_total_assembly"][root_joint.id])
    Mr = to_cad_asm @ _vec(snap["reaction_moment_total_assembly"][root_joint.id])
    res_F, res_M = Fr + F_tot, Mr + M_tot  # parent -> child wrench + applied = 0 at rest
    scale_F = max(float(np.linalg.norm(F_tot)), 1e-30)
    scale_M = max(float(np.linalg.norm(M_tot)), 1e-30)
    equilibrium = {
        "root_reaction_N": Fr.tolist(), "root_moment_Nm": Mr.tolist(),
        "applied_force_N": F_tot.tolist(), "applied_moment_Nm": M_tot.tolist(),
        "residual": {"force_N": res_F.tolist(), "moment_Nm": res_M.tolist(),
                     "force_norm_N": float(np.linalg.norm(res_F)), "moment_norm_Nm": float(np.linalg.norm(res_M)),
                     "force_relative": float(np.linalg.norm(res_F)) / scale_F,
                     "moment_relative": float(np.linalg.norm(res_M)) / scale_M},
        "convention": "root reaction = wrench exerted by the parent on the component at the root joint point "
                      "(actuator included), CAD frame; residual = reaction + applied (zero at rest); "
                      "the reaction is not applied in the structural study",
        "ok": bool(np.linalg.norm(res_F) <= 1e-6 * scale_F + 1e-9
                   and np.linalg.norm(res_M) <= 1e-6 * scale_M + 1e-12),
    }

    frame_text = (f"CAD frame of '{rb.name}' (the geometry in its original placement); multibody pose at "
                  f"t = {t:g} s: body orientation R(t) from the result, component placement "
                  f"{'identity' if np.allclose(P, np.eye(4)) else 'applied'}; vectors v_cad = Rp^T R(t)^T v_assembly "
                  "(loads defined in the body frame: Rp^T v)")
    record = TransferRecord(
        source_study={"id": mbd_study.id, "name": mbd_study.name}, time_s=t,
        body={"id": rb.id, "name": rb.name, "geometry_body_id": gid, "component_id": comp.id,
              "root_joint": root_joint.name},
        frame=frame_text,
        region={"root_face": root, "load_face": load,
                "application_points": [e["point"] for e in entries]},
        force_N=F_tot.tolist(), moment_Nm=M_tot.tolist(),
        moment_about={"point": origin_cad.tolist(), "joint": root_joint.name},
        units=f"N, {us.length}, {us.stress}",
        equilibrium_check=equilibrium, note=STATIC_HOLD_NOTE,
        acceleration={"max_abs_qdd": max_qdd, "max_abs_qd": max_qd, "tolerance": accel_tol, "per_joint": qdd,
                      "accelerating": bool(accelerating)},
        loads=entries, warnings=warnings)

    # ---- the structural study ------------------------------------------------------------------------------
    mat = project.material_for_body(gid, mbd_study)
    if mat is None:
        raise TransferError(f"no material is assigned to '{rb.name}' in study '{mbd_study.name}'")
    ms = MeshSettings(body=gid, element_order=2)
    if bodies and gid in bodies:
        lo, hi = bodies[gid].bbox
        ms.size_max = float(np.min(np.asarray(hi) - np.asarray(lo))) / max(int(mesh_divisions), 1)
    study = Study(
        name=name or f"Static hold - {rb.name} @ t={t:g} s",
        body_ids=[gid], assignments=[MaterialAssignment(body=gid, material=mat.id)], mesh_settings=ms,
        constraints=[Constraint("fixed", [_ref(root_face_ref)], name="Root (fixed)")],
        loads=sloads,
        analysis=AnalysisSettings(analysis_type="linear_static", title="Static hold (from multibody)",
                                  threads=mbd_study.analysis.threads),
        notes="")
    study.transfer = record.to_dict()
    study.notes = transfer_summary(record)
    project.studies.append(study)
    return study, record


def _ref(ref: EntityRef) -> EntityRef:
    return EntityRef(ref.kind, ref.body, ref.index, dict(ref.signature))


def transfer_summary(record: TransferRecord) -> str:
    """Plain-text description of the hand-over for the UI (and the study notes)."""
    r = record
    eq = r.equilibrium_check
    f, m = np.asarray(r.force_N), np.asarray(r.moment_Nm)
    lf, rf = r.region.get("load_face", {}), r.region.get("root_face", {})
    res = eq.get("residual", {})
    lines = [
        f"Static-hold load case from multibody study '{r.source_study.get('name', '')}'",
        f"Time: {r.time_s:g} s    Component: {r.body.get('name', '')} (root joint: {r.body.get('root_joint', '')})",
        f"Frame: {r.frame}",
        f"Root (fixed): face {rf.get('index', -1) + 1}, area {rf.get('area', 0):.6g} mm²",
        f"Load region: face {lf.get('index', -1) + 1}, area {lf.get('area', 0):.6g} mm²",
        f"Applied force (CAD frame): ({f[0]:.6g}, {f[1]:.6g}, {f[2]:.6g}) N, |F| = {np.linalg.norm(f):.6g} N",
        f"Applied moment about the root joint point: ({m[0]:.6g}, {m[1]:.6g}, {m[2]:.6g}) N m, "
        f"|M| = {np.linalg.norm(m):.6g} N m",
        f"Units: {r.units}",
        f"Equilibrium check (root reaction from the multibody run, not applied): residual force "
        f"{res.get('force_norm_N', 0.0):.3g} N, residual moment {res.get('moment_norm_Nm', 0.0):.3g} N m",
        f"Acceleration at t: max |q̈| = {r.acceleration.get('max_abs_qdd', 0.0):.3g} "
        f"(tolerance {r.acceleration.get('tolerance', 0.0):g})",
        r.note,
    ]
    lines += [f"Warning: {w}" for w in r.warnings]
    return "\n".join(lines)
