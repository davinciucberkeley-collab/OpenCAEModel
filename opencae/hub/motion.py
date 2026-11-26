"""Motion data for the Results Hub: the stored time histories of a project, ready for the simulator page.

    motion(project, run="latest") -> dict   # /api/hub/motion
    plots(project, run="latest")  -> list   # items merged into /api/hub/plots

``project`` is a registry entry (``{"id", "kind", "path", "name"}``, a dict or an object with those attributes);
``run`` is a run id or ``"latest"``. Nothing is simulated here and no trajectory is ever made up: every pose and
channel is read from a stored result. When a result is missing the answer is ``kind: None`` with a note that names
what is missing. A GET never starts a solver.

Kinds (``kind`` of the answer):

* ``"mbd"``    a Studio project's multibody study (``<project>_data/results/<study id>/mbd.npz``): component meshes
               from the project geometry, poses from :func:`opencae.mbd.pose_transforms`, joint channels;
* ``"hover"``  a drone project's stored hover simulation (``results.json`` -> ``analyses.flight.extra``): the
               aircraft mesh from ``geometry.npz`` (or the CAD file), poses from the stored position and attitude;
* ``"flight"`` the K09-L engineering study's 6-DOF replay (the data the K09-L website is built from).

Pose convention (all kinds): ``poses[body_id]`` is a flat float32 array, 7 numbers per frame
``x, y, z (mm), qw, qx, qy, qz``; a mesh vertex ``v`` (mm, the body's own mesh coordinates) is in the world at
``R(q) v + (x, y, z)``. The world is z up. Quaternions are unit length and sign-continuous.
``pose_of`` (optional) maps a body id to another body id whose pose it shares (rigid airframe parts), so the
poses are stored once.
"""
from __future__ import annotations

import base64
import json
import math
import re
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import numpy as np

MAX_FRAMES = 600
MAX_TRIANGLES = 40000          # all bodies together (simplified above this, and a note says so)
DISCLAIMER = "Animation of computed results; not a flight prediction"
DISCIPLINE = {"mbd": "mbd", "hover": "flight", "flight": "flight"}
LENGTH_MM = {"mm": 1.0, "m": 1000.0, "cm": 10.0, "in": 25.4, "ft": 304.8}
PALETTE = ["#5aa9e6", "#e6a15a", "#7bc67e", "#c47be6", "#e6d35a", "#5ae6d3", "#e65a7b", "#9aa5b4"]
GROUND_COLOR = "#8a94a3"
ROLE_COLOR = {"frame": "#9aa5b4", "arm": "#6f7c8f", "motor": "#e6a15a", "prop": "#5aa9e6", "battery": "#e65a7b",
              "electronics": "#7bc67e", "wing": "#c9d1dc", "fuselage": "#b6c0cd", "payload": "#e6d35a"}


# ------------------------------------------------------------------------------------------------ small helpers
def _b64(a: np.ndarray, dtype) -> str:
    return base64.b64encode(np.ascontiguousarray(a, dtype=dtype).tobytes()).decode("ascii")


def b64_f32(a) -> str:
    """base64 of little-endian float32."""
    return _b64(np.asarray(a, dtype="<f4"), "<f4")


def b64_i32(a) -> str:
    """base64 of little-endian int32."""
    return _b64(np.asarray(a, dtype="<i4"), "<i4")


def _r(a, nd: int = 5) -> list:
    """Rounded list of floats (NaN and inf become None so the JSON stays valid)."""
    flat = np.round(np.asarray(a, dtype=float).ravel(), nd).tolist()
    return [v if math.isfinite(v) else None for v in flat]


def _project(project) -> dict:
    if isinstance(project, dict):
        d = dict(project)
    else:
        d = {k: getattr(project, k, None) for k in ("id", "kind", "path", "name")}
    d["path"] = str(d.get("path") or "")
    d["name"] = d.get("name") or (Path(d["path"]).stem if d["path"] else "project")
    return d


def _null(project: dict, note: str, title: str = "", **extra) -> dict:
    out = {"kind": None, "title": title or project.get("name", ""), "basis": "", "notes": [note], "t": [],
           "bodies": [], "poses": {}, "joints": [], "channels": [], "rotors": None, "disclaimer": None}
    out.update(extra)
    return out


def frame_indices(n: int, max_frames: int = MAX_FRAMES) -> np.ndarray:
    """Evenly spaced sample indices, always including the first and the last sample."""
    if n <= max_frames:
        return np.arange(n)
    return np.unique(np.round(np.linspace(0, n - 1, max_frames)).astype(int))


def mat_to_quat(R: np.ndarray) -> np.ndarray:
    """Unit quaternion (w, x, y, z) of a rotation matrix (Shepperd's method)."""
    R = np.asarray(R, float)
    tr = R[0, 0] + R[1, 1] + R[2, 2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        q = [0.25 * s, (R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s]
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        q = [(R[2, 1] - R[1, 2]) / s, 0.25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s]
    elif R[1, 1] > R[2, 2]:
        s = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        q = [(R[0, 2] - R[2, 0]) / s, (R[0, 1] + R[1, 0]) / s, 0.25 * s, (R[1, 2] + R[2, 1]) / s]
    else:
        s = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        q = [(R[1, 0] - R[0, 1]) / s, (R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, 0.25 * s]
    q = np.asarray(q, float)
    return q / np.linalg.norm(q)


def quat_to_mat(q) -> np.ndarray:
    w, x, y, z = np.asarray(q, float) / np.linalg.norm(q)
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def euler_to_quat(roll_deg, pitch_deg, yaw_deg) -> np.ndarray:
    """Aerospace ZYX Euler angles (deg) -> quaternion (w, x, y, z); the inverse of the flight module's ``_euler``."""
    r, p, y = (math.radians(float(a)) / 2 for a in (roll_deg, pitch_deg, yaw_deg))
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return np.array([cr * cp * cy + sr * sp * sy, sr * cp * cy - cr * sp * sy,
                     cr * sp * cy + sr * cp * sy, cr * cp * sy - sr * sp * cy])


def pose_array(rotations: list, translations: list) -> np.ndarray:
    """(n, 7) = x y z qw qx qy qz; rotations are 3x3 matrices or quaternions; quaternions come out unit length and
    sign-continuous (so a viewer can interpolate between frames)."""
    out = np.zeros((len(rotations), 7))
    prev = None
    for i, (R, t) in enumerate(zip(rotations, translations)):
        R = np.asarray(R, float)
        q = mat_to_quat(R) if R.shape == (3, 3) else R
        q = q / np.linalg.norm(q)
        if prev is not None and float(q @ prev) < 0:
            q = -q
        prev = q
        out[i, :3], out[i, 3:] = t, q
    return out


def _decimate(points: np.ndarray, tris: np.ndarray, max_tri: int):
    """(points, triangles) with at most about ``max_tri`` triangles: pyvista decimation, else a stride."""
    if len(tris) <= max_tri:
        return points, tris
    try:
        import pyvista as pv
        faces = np.hstack([np.full((len(tris), 1), 3), tris]).ravel().astype(np.int64)
        pd = pv.PolyData(np.asarray(points, float), faces).clean()
        red = 1.0 - max_tri / max(pd.n_cells, 1)
        out = pd.decimate_pro(min(max(red, 0.0), 0.97), preserve_topology=True)
        if out.n_cells >= 4:
            return np.asarray(out.points), np.asarray(out.faces).reshape(-1, 4)[:, 1:].astype(np.int64)
    except Exception:
        pass
    stride = int(math.ceil(len(tris) / max_tri))
    t = tris[::stride]
    used, inv = np.unique(t, return_inverse=True)
    return np.asarray(points)[used], inv.reshape(-1, 3)


def _share_budget(counts: list, budget: int = MAX_TRIANGLES, floor: int = 24) -> list:
    """Triangle allowance per mesh: all fit -> unchanged; else proportional (never below ``floor``)."""
    total = sum(counts)
    if total <= budget:
        return list(counts)
    return [min(c, max(floor, int(c * budget / total))) for c in counts]


def _body_entry(bid: str, name: str, pts: np.ndarray, tris: np.ndarray, color: str, **extra) -> dict:
    e = {"id": bid, "name": name, "points": b64_f32(np.asarray(pts, float).reshape(-1, 3).ravel()),
         "triangles": b64_i32(np.asarray(tris, int).reshape(-1, 3).ravel()), "color": color}
    e.update(extra)
    return e


def _norm3(a) -> np.ndarray:
    return np.linalg.norm(np.asarray(a, float), axis=-1)


class _LRU(OrderedDict):
    def __init__(self, size: int):
        super().__init__()
        self.size = size

    def put(self, key, value):
        self[key] = value
        self.move_to_end(key)
        while len(self) > self.size:
            self.popitem(last=False)
        return value


def _stamp(*paths) -> tuple:
    out = []
    for p in paths:
        try:
            st = Path(p).stat()
            out.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((str(p), 0, 0))
    return tuple(out)


# ================================================================================================== entry points
def motion(project, run: str = "latest") -> dict:
    """The motion payload for ``/api/hub/motion`` (see the module doc and the Results Hub contract)."""
    return _build(_project(project), run or "latest", meshes=True)


def plots(project, run: str = "latest") -> list:
    """Plot items (``/api/hub/plots`` shape) for the motion channels, one per physical quantity."""
    try:
        data = _build(_project(project), run or "latest", meshes=False)
    except Exception:  # plots must never break the plots endpoint
        return []
    return channel_plots(data)


def channel_plots(data: dict) -> list:
    if not data or not data.get("kind") or not data.get("channels"):
        return []
    disc = DISCIPLINE.get(data["kind"], "mbd")
    t = data["t"]
    groups: "OrderedDict[tuple, list]" = OrderedDict()
    for ch in data["channels"]:
        groups.setdefault((ch.get("quantity") or ch["name"], ch["unit"]), []).append(ch)
    out = []
    for i, ((qty, unit), chans) in enumerate(groups.items()):
        names = [c.get("short") or c["name"] for c in chans]
        note = "; ".join(dict.fromkeys(c["note"] for c in chans if c.get("note")))
        slug = re.sub(r"[^a-z0-9]+", "-", str(qty).lower()).strip("-")
        out.append({"id": f"motion-{i}-{slug}", "title": f"{data.get('title', 'Motion')}: {qty}",
                    "discipline": disc, "x": {"label": "time", "unit": "s"},
                    "y": {"label": str(qty), "unit": unit, "log": False},
                    "series": [{"name": n, "x": t, "y": c["values"]} for n, c in zip(names, chans)],
                    "note": note or f"Stored result of the motion study; {len(t)} frames."})
    return out


def _build(project: dict, run: str, meshes: bool) -> dict:
    kind = project.get("kind")
    path = project.get("path", "")
    if not path or not Path(path).exists():
        return _null(project, f"The project path does not exist: {path or '(none)'}.")
    try:
        if kind == "studio":
            return _studio(project, run, meshes)
        if kind == "k09l-study":
            return _k09l(project, run, meshes)
        if kind == "drone":
            spec = _read_json(Path(path) / "drone.json") or {}
            if spec.get("engine") == "k09l":
                study = (Path(path) / str(spec.get("study", ""))).resolve()
                return _k09l(dict(project, path=str(study)), run, meshes)
            return _hover(project, run, meshes)
    except Exception as exc:  # never hide a failure: say what failed
        return _null(project, f"The motion data could not be read: {type(exc).__name__}: {exc}")
    return _null(project, f"Unknown project kind {kind!r}: no motion data.")


def _read_json(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ================================================================================================== studio / MBD
_SESSIONS = _LRU(3)


def _session(path: Path):
    """The project opened once (geometry regenerated), cached by file stamp. Only read, never saved."""
    key = _stamp(path)
    hit = _SESSIONS.get(key)
    if hit is not None:
        return hit
    from opencae.app.session import Session
    s = Session()
    s.open(path)
    return _SESSIONS.put(key, s)


def _result_dir(session, study) -> Path:
    return session.data_dir / "results" / study.id


def _pick_study(session, run: str):
    studies = [s for s in session.project.studies if getattr(s, "mbd", None)]
    if not studies:
        return None, "This project has no motion (multibody) study."
    if run and run != "latest":
        sid = run.split("@", 1)[0]
        for s in studies:
            if s.id == sid or s.name == sid:
                return s, ""
        return None, f"Run '{run}' is not a motion study of this project."
    done = [(s, (_result_dir(session, s) / "mbd.json").stat().st_mtime) for s in studies
            if (_result_dir(session, s) / "mbd.json").exists()]
    if done:
        return max(done, key=lambda x: x[1])[0], ""
    return studies[0], ""


def _studio(project: dict, run: str, meshes: bool) -> dict:
    from opencae.mbd import MBDResult, pose_transforms
    session = _session(Path(project["path"]))
    study, why = _pick_study(session, run)
    if study is None:
        return _null(project, why)
    rdir = _result_dir(session, study)
    title = f"{study.name} ({project['name']})"
    if not (rdir / "mbd.json").exists() or not (rdir / "mbd.npz").exists():
        return _null(project, f"No stored result for the motion study '{study.name}': run the motion study first.",
                     title=title)
    res = MBDResult.load(rdir)
    meta = res.meta
    scale = LENGTH_MM.get(meta.get("units", {}).get("length", "mm"), 1.0)
    n = len(res.t)
    idx = frame_indices(n)
    comps = meta.get("components", {})
    asm = session.project.assembly
    body_of = {c: v["body"] for c, v in comps.items()}

    # ---- component meshes (geometry body coordinates, mm) and one pose track per component
    order = [c for c in asm.components if c.body_id in session.bodies and session.bodies[c.body_id].tessellation]
    counts = [len(session.bodies[c.body_id].tessellation.triangles) for c in order]
    share = _share_budget(counts)
    grounded = {b.id for b in asm.bodies if b.grounded}
    bodies = []
    notes: list = []
    for k, (c, allow) in enumerate(zip(order, share)):
        if not meshes:
            bodies.append({"id": c.id, "name": c.name})
            continue
        tess = session.bodies[c.body_id].tessellation
        pts, tri = _decimate(np.asarray(tess.points, float) * scale, np.asarray(tess.triangles, int), allow)
        is_ground = body_of.get(c.id) in grounded or c.id not in comps
        bodies.append(_body_entry(c.id, c.name, pts, tri, GROUND_COLOR if is_ground else PALETTE[k % len(PALETTE)],
                                  grounded=bool(is_ground), rigid_body=body_of.get(c.id)))
    if sum(counts) > MAX_TRIANGLES:
        notes.append(f"Meshes simplified for display: {sum(counts)} triangles reduced to about {MAX_TRIANGLES}.")
    # rotors: joints with a thrust model, else revolute joints whose child body looks like a rotor. Their pose keeps
    # the motion of the parent but not the spin about the joint axis (at thousands of rpm the stored samples alias):
    # the page spins the rotor from the stored speed instead.
    thrust_j = {tm["rotor_joint"] for tm in meta.get("thrust_models", [])}
    rj = [j for j in meta.get("joints", []) if j["kind"] == "revolute" and
          (j["id"] in thrust_j or (not thrust_j and re.search(r"rotor|prop", j["child_name"], re.I)))]
    strip = {}   # rigid body id -> joint
    for j in rj:
        strip[j["child"]] = j
    com0 = {b["id"]: np.asarray(b["com0"], float) for b in meta.get("bodies", [])}
    rot = {c.id: [] for c in order}
    trn = {c.id: [] for c in order}
    for i in idx:
        T = pose_transforms(res, int(i))
        for c in order:
            M = T.get(c.id)
            if M is None:   # component outside every rigid body: stays at its placement
                M = np.asarray(c.placement, float).reshape(-1, 4)
                M = np.vstack([M, [0, 0, 0, 1]]) if M.shape[0] == 3 else M
            R, tt = M[:3, :3], M[:3, 3]
            j = strip.get(body_of.get(c.id))
            if j is not None:
                Pm = np.asarray(c.placement, float).reshape(-1, 4)
                Rpl, ppl = Pm[:3, :3], Pm[:3, 3]
                Rb = R @ Rpl.T
                bpos = tt - Rb @ (ppl - com0[j["child"]])
                a = np.asarray(j["axis"], float)
                a = a / np.linalg.norm(a)
                q = float(res.joint_q[j["id"]][int(i)])
                K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
                Rq = np.eye(3) + math.sin(-q) * K + (1 - math.cos(-q)) * (K @ K)
                Rb2 = Rb @ Rq
                R, tt = Rb2 @ Rpl, Rb2 @ (ppl - com0[j["child"]]) + bpos
            rot[c.id].append(R)
            trn[c.id].append(tt * scale)
    poses = {cid: b64_f32(pose_array(rot[cid], trn[cid]).ravel()) for cid in rot} if meshes else {}
    t = res.t[idx]

    # ---- joints
    comp_of_body: dict = {}
    for c in order:
        comp_of_body.setdefault(body_of.get(c.id), c.id)
    joints = []
    for j in meta.get("joints", []):
        d = next((d for d in meta.get("drivers", []) if d["joint"] == j["id"]), None)
        mode = None if d is None else ("prescribed" if d["kind"].startswith("prescribed") else "torque-driven")
        joints.append({"id": j["id"], "name": j["name"], "kind": j["kind"], "mode": mode, "label": j["driver_kind"],
                       "axis": j["axis"], "origin": (np.asarray(j["origin"], float) * scale).tolist(),
                       "parent_body": comp_of_body.get(j["parent"]), "child_body": comp_of_body.get(j["child"]),
                       "limits": j.get("limits")})

    # ---- channels
    ch = []
    for j in meta.get("joints", []):
        jid, nm = j["id"], j["name"]
        prism, fixed = j["kind"] == "prismatic", j["kind"] == "fixed"
        if not fixed:
            f = scale if prism else 180.0 / math.pi
            ch.append({"name": f"{nm}: {'position' if prism else 'angle'}", "short": nm,
                       "quantity": "joint position" if prism else "joint angle", "unit": "mm" if prism else "deg",
                       "values": _r(res.joint_q[jid][idx] * f, 5), "group": nm})
            ch.append({"name": f"{nm}: speed", "short": nm, "quantity": "joint speed",
                       "unit": "mm/s" if prism else "deg/s", "values": _r(res.joint_qd[jid][idx] * f, 4),
                       "group": nm})
            ch.append({"name": f"{nm}: {'force' if prism else 'torque'}", "short": nm,
                       "quantity": "joint force" if prism else "joint torque", "unit": "N" if prism else "N m",
                       "values": _r(res.joint_effort[jid][idx], 5), "group": nm,
                       "note": "actuator effort; for prescribed joints the inverse-dynamics effort that realises "
                               "the commanded motion"})
        ch.append({"name": f"{nm}: reaction force", "short": nm, "quantity": "reaction force", "unit": "N",
                   "values": _r(_norm3(res.reaction_force_joint[jid][idx]), 5), "group": nm,
                   "note": "magnitude of the force the parent exerts on the child at the joint origin "
                           "(actuator effort along the axis excluded)"})
        ch.append({"name": f"{nm}: reaction moment", "short": nm, "quantity": "reaction moment", "unit": "N m",
                   "values": _r(_norm3(res.reaction_moment_joint[jid][idx]), 5), "group": nm,
                   "note": "magnitude of the moment the parent exerts on the child at the joint origin "
                           "(actuator effort along the axis excluded)"})
    ch.append({"name": "constraint error", "short": "constraint error", "quantity": "constraint error",
               "unit": "rad (m for prismatic)", "values": _r(res.constraint_error[idx], 9), "group": "model",
               "note": "max |q_follower - ratio q_leader| of the coupling constraints since the previous sample"})

    # ---- rotor entries (axis and origin in the component's mesh coordinates)
    rotors = None
    if rj:
        rotors = []
        for j in rj:
            cid = comp_of_body.get(j["child"])
            comp = next((c for c in order if c.id == cid), None)
            Pm = np.asarray(comp.placement, float).reshape(-1, 4) if comp is not None else np.eye(4)
            Rpl, ppl = Pm[:3, :3], Pm[:3, 3]
            rpm = res.joint_qd[j["id"]][idx] * 60.0 / (2 * math.pi)
            rotors.append({"body_id": cid, "name": j["child_name"],
                           "axis": (Rpl.T @ np.asarray(j["axis"], float)).tolist(),
                           "origin": (Rpl.T @ (np.asarray(j["origin"], float) - ppl) * scale).tolist(),
                           "sense": 1.0, "in_pose": False, "speed_rpm": _r(rpm, 3)})
            ch.append({"name": f"{j['child_name']}: speed", "short": j["child_name"], "quantity": "rotor speed",
                       "unit": "rpm", "values": rotors[-1]["speed_rpm"], "group": "rotors"})
        notes.append("Rotor discs are drawn spinning at a slowed rate proportional to the stored speed (the "
                     "stored samples are too sparse to show thousands of rpm).")

    notes += list(meta.get("assumptions", []))
    notes += [f"Warning from the solver: {w}" for w in meta.get("warnings", [])]
    notes.append(f"Largest coupling-constraint error over the run: {meta.get('constraint_violation_max', 0.0):.3g}.")
    if n > len(idx):
        notes.append(f"Shown at {len(idx)} of {n} stored samples (first and last kept); the plots use the same frames.")
    bk = meta.get("backend", {})
    return {"kind": "mbd", "title": title,
            "basis": (f"Computed: rigid-body dynamics with {bk.get('name', 'the motion backend')} "
                      f"{bk.get('version', '')}; masses from the CAD volume and the assigned material density, "
                      "joints, drivers and loads as set in the project (inputs are the user's model, not "
                      "measurements)").replace("  ", " "),
            "notes": notes, "t": _r(t, 6), "bodies": bodies, "poses": poses, "joints": joints, "channels": ch,
            "rotors": rotors, "disclaimer": None,
            "scene": {"up": "z", "units": "mm", "ground_z_mm": None, "extent_mm": _extent(trn)},
            "study": {"id": study.id, "name": study.name, "duration_s": meta.get("settings", {}).get("duration_s"),
                      "backend": bk, "wall_time_s": meta.get("wall_time_s")},
            "frames": {"shown": int(len(idx)), "stored": int(n), "decimated": bool(n > len(idx))}}


def _extent(trn: dict) -> Optional[list]:
    pts = [np.asarray(v) for v in trn.values() if len(v)]
    if not pts:
        return None
    a = np.vstack(pts)
    return [float(x) for x in np.round(np.concatenate([a.min(0), a.max(0)]), 1)]


# ================================================================================================== drone hover
def _variants(rdir: Path) -> list:
    return sorted((d.name for d in rdir.iterdir() if (d / "results.json").exists()),
                  key=lambda n: (n != "baseline", n)) if rdir.exists() else []


def _runs(path: Path) -> list:
    rows = []
    try:
        for line in (path / "results" / "runs.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return [r for r in rows if isinstance(r, dict)]


def _drone_results(path: Path, run: str):
    """(results dict or None, variant, note). ``run``: ``latest`` (the newest finished run's variant, else the
    baseline), a run id with a stored snapshot (``results/history/<run>/results.json``), or the latest run of a
    variant."""
    rdir = path / "results"
    rows = _runs(path)
    if run and run != "latest":
        data = _read_json(rdir / "history" / run / "results.json")
        if data is not None:
            return data, data.get("variant", "baseline"), f"Run {run} (stored snapshot)."
        row = next((r for r in rows if r.get("run_id") == run), None)
        if row is None:
            return None, "", f"Run '{run}' is not in the run history of this project."
        var = row.get("variant", "baseline")
        same = [r for r in rows if r.get("variant", "baseline") == var and r.get("status") == "done"]
        if same and same[-1].get("run_id") == run:
            return _read_json(rdir / var / "results.json"), var, f"Run {run} (latest results of variant {var})."
        return None, "", (f"Run {run} has no stored snapshot of its flight result (only the latest run of a "
                          "variant keeps its results).")
    var = "baseline"
    done = [r for r in rows if r.get("status") == "done"]
    if done:
        var = done[-1].get("variant", "baseline")
    data = _read_json(rdir / var / "results.json")
    if data is None:
        vs = _variants(rdir)
        if vs:
            var = vs[0]
            data = _read_json(rdir / var / "results.json")
    return data, var, ""


def _aircraft_mesh(path: Path, variant: str):
    """(names, roles, points list, triangles list, source) in the aircraft frame (mm)."""
    for d in (path / "results" / variant, path / "results" / "baseline"):
        f = d / "geometry.npz"
        if f.exists():
            with np.load(f, allow_pickle=False) as z:
                names = [str(s) for s in z["names"]]
                roles = [str(s) for s in z["roles"]] if "roles" in z.files else ["other"] * len(names)
                return (names, roles, [z[f"p{i}"].astype(float) for i in range(len(names))],
                        [z[f"t{i}"].astype(int) for i in range(len(names))], "geometry.npz of the last run")
    from opencae.drone import geometry as G
    from opencae.drone import spec as S
    ac = G.load(path, S.load(path))
    return ([p.name for p in ac.parts], [p.role for p in ac.parts], [p.points for p in ac.parts],
            [p.triangles for p in ac.parts], "the project's CAD file")


def _hover(project: dict, run: str, meshes: bool) -> dict:
    path = Path(project["path"])
    data, variant, rnote = _drone_results(path, run)
    title = f"Hover simulation ({project['name']})"
    if data is None:
        return _null(project, rnote or "No results yet: run the drone analyses first (the hover simulation is part "
                                       "of the flight analysis).", title=title)
    ana = data.get("analyses") or {}
    fl = ana.get("flight")
    if not fl:
        return _null(project, "The flight analysis has not been run for this project: no hover simulation is "
                              "stored. Run the flight analysis.", title=title)
    ex = fl.get("extra") or {}
    if fl.get("status") == "not_evaluated" or not ex:
        missing = ", ".join(fl.get("missing") or []) or "its inputs"
        return _null(project, f"The flight analysis was not evaluated (missing: {missing}), so there is no hover "
                              "time history to animate.", title=title)
    need = [k for k in ("t", "pos", "att") if not ex.get(k)]
    if need:
        return _null(project, "The stored flight result has no hover time history: missing "
                              + ", ".join(f"extra.{k}" for k in need) + " in the flight analysis result "
                              "(results.json).", title=title)
    t_all = np.asarray(ex["t"], float)
    pos = np.asarray(ex["pos"], float)          # m, relative to the hover point
    att = np.asarray(ex["att"], float)          # deg: roll, pitch, yaw
    motor = np.asarray(ex.get("motor") or [], float)   # thrust fraction per rotor
    if not (len(t_all) == len(pos) == len(att) and len(t_all) > 1):
        return _null(project, "The stored hover time history is inconsistent (t, pos and att differ in length).",
                     title=title)
    prop = ana.get("propulsion") or {}
    rotors_in = (prop.get("extra") or {}).get("rotors") or []
    cg = ((ana.get("mass") or {}).get("metrics") or {}).get("cg_mm")
    cg = np.asarray(cg.get("value") if isinstance(cg, dict) else (cg if cg else [0, 0, 0]), float)
    notes = ["Hover simulation of the flight analysis: 6-DOF rigid body, position and attitude hold, a roll kick of "
             f"{ex.get('disturbance_deg', '?')} deg at t = {ex.get('kick_time_s', '?')} s."]
    notes += [str(a) for a in ex.get("assumptions", [])]
    if rnote:
        notes.append(rnote)
    # hover-attitude frame of the simulation (mean thrust axis made vertical): the same step as flight.simulate
    from opencae.drone.analyses import flight as FL
    axes = np.array([np.asarray(r.get("axis", [0, 0, 1]), float) for r in rotors_in]) if rotors_in else \
        np.array([[0.0, 0.0, 1.0]])
    mean_ax = axes.mean(0)
    Rh = FL._rot_from_to(mean_ax / max(np.linalg.norm(mean_ax), 1e-12), np.array([0.0, 0.0, 1.0]))
    idx = frame_indices(len(t_all))
    rots, trns = [], []
    for i in idx:
        R = quat_to_mat(euler_to_quat(*att[i])) @ Rh
        rots.append(R)
        trns.append(pos[i] * 1000.0 - R @ cg)          # v_world = pos + R (v - cg)
    pose = pose_array(rots, trns)
    t = t_all[idx]
    try:
        names, roles, P, Tr, src = _aircraft_mesh(path, variant)
    except Exception as exc:
        return _null(project, "The hover history is stored, but the aircraft geometry could not be read "
                              f"({type(exc).__name__}: {exc}).", title=title)
    share = _share_budget([len(x) for x in Tr])
    rot_pos = np.array([r["position_mm"] for r in rotors_in], float) if rotors_in else np.zeros((0, 3))
    prop_of: dict = {}
    for k, role in enumerate(roles):
        if role == "prop" and len(rot_pos):
            c = P[k].mean(0)
            prop_of.setdefault(int(np.argmin(np.linalg.norm(rot_pos[:, :2] - c[:2], axis=1))), k)
    bodies = []
    for k, (nm, role, p, tr, allow) in enumerate(zip(names, roles, P, Tr, share)):
        if meshes:
            pp, tt = _decimate(p, tr, allow)
            bodies.append(_body_entry(f"part-{k}", nm, pp, tt, ROLE_COLOR.get(role, PALETTE[k % len(PALETTE)]),
                                      role=role))
        else:
            bodies.append({"id": f"part-{k}", "name": nm})
    if sum(len(x) for x in Tr) > MAX_TRIANGLES:
        notes.append(f"Aircraft mesh simplified for display: {sum(len(x) for x in Tr)} triangles reduced to about "
                     f"{MAX_TRIANGLES}.")
    notes.append(f"Aircraft mesh from {src}.")
    # ---- channels
    ch = []
    zb = np.array([R[:, 2] for R in rots])
    tilt = np.degrees(np.arccos(np.clip(zb[:, 2], -1, 1)))
    for k, nm in enumerate(("roll", "pitch", "yaw")):
        ch.append({"name": nm, "short": nm, "quantity": "attitude", "unit": "deg", "values": _r(att[idx, k], 4),
                   "group": "attitude", "note": "Euler angles (roll, pitch, yaw) stored by the flight analysis"})
    ch.append({"name": "tilt", "short": "tilt", "quantity": "tilt", "unit": "deg", "values": _r(tilt, 4),
               "group": "attitude", "note": "angle between the vertical and the body z axis, from the stored "
                                            "attitude"})
    for k, nm in enumerate(("x forward", "y left", "z up")):
        ch.append({"name": f"position {nm}", "short": nm, "quantity": "position from the hover point", "unit": "m",
                   "values": _r(pos[idx, k], 5), "group": "position"})
    rpm_full = ex.get("rpm_full") or (prop.get("extra") or {}).get("rpm_full")
    n_rot = motor.shape[1] if motor.ndim == 2 else 0
    for k in range(n_rot):
        ch.append({"name": f"rotor {k + 1} thrust", "short": f"rotor {k + 1}",
                   "quantity": "thrust (fraction of the maximum)", "unit": "-", "values": _r(motor[idx, k], 4),
                   "group": "rotors", "note": "stored thrust of each rotor divided by its maximum thrust"})
    rotors = None
    if n_rot and rpm_full:
        rotors = []
        for k in range(n_rot):
            ro = rotors_in[k] if k < len(rotors_in) else {}
            rpm = float(rpm_full) * np.sqrt(np.clip(motor[idx, k], 0, None))
            j = prop_of.get(k)
            rotors.append({"body_id": f"part-{j}" if j is not None else None, "name": f"Rotor {k + 1}",
                           "axis": list(map(float, ro.get("axis", [0, 0, 1]))),
                           "origin": list(map(float, ro.get("position_mm", [0, 0, 0]))),
                           "sense": -1.0 if str(ro.get("spin", "cw")).lower() == "cw" else 1.0, "in_pose": False,
                           "speed_rpm": _r(rpm, 1)})
            ch.append({"name": f"rotor {k + 1} speed", "short": f"rotor {k + 1}", "quantity": "rotor speed",
                       "unit": "rpm", "values": rotors[-1]["speed_rpm"], "group": "rotors",
                       "note": "full-throttle rpm x sqrt(thrust fraction) (thrust ~ rpm^2)"})
        notes.append("Rotor speed = full-throttle rpm (propulsion analysis) x sqrt(stored thrust fraction): the "
                     "flight model takes thrust proportional to the command and the propeller model T ~ rpm^2.")
        unplaced = [r["name"] for r in rotors if r["body_id"] is None]
        if unplaced:
            notes.append("No propeller part found near " + ", ".join(unplaced) + ": they are not drawn spinning "
                         "(speed is still plotted).")
    elif n_rot:
        notes.append("Rotor speed is not stored (the propulsion analysis has no full-throttle rpm), so the rotors do "
                     "not spin in the animation; the thrust fractions are plotted.")
    events = [{"t": float(ex["kick_time_s"]), "text": f"roll kick {ex.get('disturbance_deg', '')} deg".strip()}] \
        if ex.get("kick_time_s") is not None else []
    if len(t_all) > len(idx):
        notes.append(f"Shown at {len(idx)} of {len(t_all)} stored samples.")
    return {"kind": "hover", "title": title,
            "basis": ("Estimate: reduced-order 6-DOF hover simulation of the flight analysis (motor lag, rotor torque "
                      "ratio and the controller are estimates; no drag, no wind)"),
            "notes": notes, "t": _r(t, 5), "bodies": bodies, "poses": {"aircraft": b64_f32(pose.ravel())} if meshes else {},
            "pose_of": {b["id"]: "aircraft" for b in bodies}, "joints": [], "channels": ch, "rotors": rotors,
            "events": events, "disclaimer": DISCLAIMER,
            "scene": {"up": "z", "units": "mm", "ground_z_mm": None,
                      "extent_mm": [float(x) for x in np.round(np.concatenate([pose[:, :3].min(0),
                                                                               pose[:, :3].max(0)]), 1)]},
            "frames": {"shown": int(len(idx)), "stored": int(len(t_all)), "decimated": bool(len(t_all) > len(idx))},
            "status": fl.get("status"), "summary": fl.get("summary")}


# ================================================================================================== K09-L study
_K09L = _LRU(2)


def _js_object(path: Path):
    """The JSON object of a ``window.NAME = {...};`` data script (cached by file stamp)."""
    key = _stamp(path)
    hit = _K09L.get(key)
    if hit is not None:
        return hit
    s = path.read_text(encoding="utf-8")
    return _K09L.put(key, json.loads(s[s.index("{"): s.rindex("}") + 1]))


def _k09l_dir(project_path: Path) -> Path:
    base = project_path if project_path.is_dir() else project_path.parent
    return base / "k09l_website" / "data"


def _decode(b: str, dtype) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b), dtype=dtype)


def _k09l(project: dict, run: str, meshes: bool) -> dict:
    d = _k09l_dir(Path(project["path"]))
    title = f"K09-L flight replay ({project['name']})"
    if not (d / "extra.js").exists() or not (d / "aircraft.js").exists():
        return _null(project, "The K09-L flight replay is stored in the built K09-L website data "
                              f"({d / 'extra.js'} and aircraft.js) and that was not found: build the K09-L website "
                              "first (python -m opencae.web.build).", title=title)
    scen = run.split(":", 1)[1] if run and ":" in run else "mission"
    extra = _js_object(d / "extra.js")
    tr = (extra.get("flight") or {}).get(scen)
    if not tr or not tr.get("t"):
        return _null(project, f"The K09-L website data has no stored flight scenario '{scen}'.", title=title)
    air = _js_object(d / "aircraft.js")
    t_all = np.asarray(tr["t"], float)
    idx = frame_indices(len(t_all))
    trans = np.asarray(tr["translation_mm"], float)[idx]
    quat = np.asarray(tr["quat"], float)[idx]
    pose = pose_array(list(quat), list(trans))
    notes = [str(tr.get("convention", "")), str(extra["flight"].get("note", ""))]
    notes += [e["text"] for e in tr.get("events", []) if e.get("kind") == "warning"][:6]
    if tr.get("validity"):
        notes.append(f"Validity: {tr['validity']}.")
    # ---- merged meshes: one body per part, one per rotor group
    groups: "OrderedDict[str, dict]" = OrderedDict()
    for b in air["bodies"]:
        key = f"rotor-{b['rotor']}" if b.get("rotor") is not None else "part-" + b["part"]
        g = groups.setdefault(key, {"name": (air["parts"].get(b["part"]) or {}).get("name", b["name"]),
                                    "rgb": b["rgb"], "P": [], "T": [], "n": 0, "rotor": b.get("rotor")})
        p = _decode(b["pos"], "<f4").reshape(-1, 3).astype(float)
        i = _decode(b["idx"], "<u4" if b.get("i32") else "<u2").reshape(-1, 3).astype(int)
        g["P"].append(p)
        g["T"].append(i + g["n"])
        g["n"] += len(p)
    counts = [sum(len(t) for t in g["T"]) for g in groups.values()]
    share = _share_budget(counts)
    rot_meta = air.get("rotors", [])
    bodies = []
    for (key, g), allow in zip(groups.items(), share):
        if g["rotor"] is not None and g["rotor"] < len(rot_meta):
            g["name"] = rot_meta[g["rotor"]]["name"]
        if meshes:
            pts, tris = _decimate(np.vstack(g["P"]), np.vstack(g["T"]), allow)
            bodies.append(_body_entry(key, g["name"], pts, tris, "#%02x%02x%02x" % tuple(int(c) for c in g["rgb"][:3])))
        else:
            bodies.append({"id": key, "name": g["name"]})
    if sum(counts) > MAX_TRIANGLES:
        notes.append(f"Aircraft mesh simplified for display: {sum(counts)} triangles reduced to about {MAX_TRIANGLES}.")
    rotors = []
    if tr.get("rpm"):
        rpm = np.asarray(tr["rpm"], float)[idx]
        for k, r in enumerate(rot_meta[:rpm.shape[1]]):
            rotors.append({"body_id": f"rotor-{k}", "name": r["name"], "axis": r["axis"], "origin": r["origin"],
                           "sense": float(r.get("sense", 1.0)), "in_pose": False, "speed_rpm": _r(rpm[:, k], 1)})
    # ---- channels
    ch = []

    def col(name, unit, vals, group, qty=None, short=None, note=None):
        c = {"name": name, "short": short or name, "quantity": qty or name, "unit": unit, "values": _r(vals, 4),
             "group": group}
        if note:
            c["note"] = note
        ch.append(c)

    def g(k):
        return np.asarray(tr[k], float)[idx]
    col("altitude", "m", g("altitude_m"), "path", "altitude (CG above ground)")
    col("airspeed", "m/s", g("airspeed"), "path")
    eul = g("euler_deg")
    for k, nm in enumerate(("roll", "pitch", "yaw")):
        col(nm, "deg", eul[:, k], "attitude", "attitude", short=nm,
            note="Euler angles of the stored flight record (they wrap at +-180 deg)")
    col("angle of attack", "deg", g("alpha_deg"), "aero")
    ev = g("elevon_deg")
    for k in range(ev.shape[1] if ev.ndim == 2 else 0):
        col(f"elevon {k + 1}", "deg", ev[:, k], "aero", "elevon deflection", short=f"elevon {k + 1}")
    motors = tr.get("motors") or [r["name"] for r in rot_meta]
    for key, unit, qty in (("rpm", "rpm", "motor speed"), ("thrust_N", "N", "motor thrust"),
                           ("throttle", "-", "throttle command"), ("motor_current_A", "A", "motor current")):
        a = g(key)
        for k in range(a.shape[1] if a.ndim == 2 else 0):
            nm = str(motors[k]) if k < len(motors) else f"motor {k + 1}"
            col(f"{nm} {qty}", unit, a[:, k], "motors", qty, short=nm)
    col("battery current", "A", g("battery_current_A"), "battery")
    col("state of charge", "-", g("soc"), "battery")
    if len(t_all) > len(idx):
        notes.append(f"Shown at {len(idx)} of {len(t_all)} stored samples.")
    w = tr.get("world") or {}
    return {"kind": "flight", "title": title,
            "basis": ("Estimate: reduced-order 6-DOF simulation on provisional coefficients "
                      f"(evidence class {tr.get('evidence_class', 'n/a')}): plans tests, not flight evidence"),
            "notes": [n for n in notes if n], "t": _r(t_all[idx], 4), "bodies": bodies,
            "poses": {"aircraft": b64_f32(pose.ravel())} if meshes else {},
            "pose_of": {b["id"]: "aircraft" for b in bodies}, "joints": [], "channels": ch,
            "rotors": rotors or None, "disclaimer": DISCLAIMER,
            "events": [{"t": e["t"], "text": e["text"]} for e in tr.get("events", [])
                      if e.get("kind") == "event" and not str(e["text"]).startswith("phase:")],
            "phases": [{"id": p["id"], "name": p["name"], "t0": p["t0"], "t1": p["t1"],
                        "completed": p["completed"]} for p in tr.get("phases", [])],
            "scene": {"up": "z", "units": "mm", "ground_z_mm": w.get("ground_z_mm", 0.0),
                      "extent_mm": w.get("extent_mm")},
            "scenario": scen,
            "frames": {"shown": int(len(idx)), "stored": int(len(t_all)), "decimated": bool(len(t_all) > len(idx))}}
