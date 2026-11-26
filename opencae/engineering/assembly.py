"""Assembly engineering on the common model: subassembly tree, exploded view, assembly sequence,
assembly checks (missing geometry, interference, clearance, joints, mounting, electrical interfaces)
and the configured bill of materials.

Everything here *reads* the :class:`~opencae.engineering.model.EngineeringModel`; nothing modifies
its geometry (``model.points`` is authoritative). Derived data that are not in a source document
(tool lists, tolerance notes, explode directions) are marked as ``guidance``.

Geometry convention (from the model): aircraft frame, x aft, y right, z up, millimetres.

Interference / contact rules (used by :func:`check_assembly`)
------------------------------------------------------------
1. Pairs are only tested between bodies of *different* components (bodies of one BOM line, e.g. the
   PCBs of the FC stack, are one purchased part).
2. Prefilter: axis-aligned bounding boxes, expanded by the clearance, must overlap.
3. Inside test: sample points of body A (mesh vertices plus triangle centroids, at most
   ``SAMPLE_CAP`` per body pair, seeded random subset when larger) are classified against the closed
   surface of body B with ``vtkSelectEnclosedPoints`` (ray casting; independent of the triangle
   orientation), and vice versa. Open (non-watertight) bodies are only used as point sources.
4. Distance: unsigned distance of every sample to the other surface (``vtkImplicitPolyDataDistance``).
   The penetration estimate is the largest distance of an *inside* sample (a lower bound of the true
   penetration, because only samples are tested); the gap is the smallest distance of an outside
   sample (an upper bound of the true minimum gap).
5. Classification of a pair: penetration > ``CONTACT_TOL_MM`` -> overlap; inside samples with
   penetration <= tolerance, or gap <= tolerance -> contact (touching faces, normal for an
   assembly); gap < clearance -> tight clearance.
6. An overlap is an *expected contact* (not an interference) when one of these rules holds:
   - ``fastener``: one side is fastener hardware (screw, SHCS, nut, locknut, washer, standoff, prop
     nut / washer): fasteners sit in holes and clamp parts; the thread / hole is not modelled
     with clearance.
   - ``termination``: a conductor or connector (wiring group, plug, XT60 / bullet connector, servo
     extension, motor / battery leads, pressure tubing) overlapping the electrical / pneumatic part
     it terminates in (avionics, motor leads, connectors, wiring). Conductors overlapping *structure*
     are NOT covered by this rule and are reported as interference (routing problem).
   - ``fit``: a declared press / slip / bonded fit between two component families (listed in
     ``FIT_PAIRS`` with the reason), e.g. the spar joiner inside the spar tube, the spar in its
     wing-core slot, the motor tube in the motor mount, the prop adapter on the motor shaft.
   Anything else is reported as an interference with its penetration estimate.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Optional

import numpy as np

from opencae.engineering.model import Body, Component, EngineeringModel

CONTACT_TOL_MM = 0.02  # deeper than this is an overlap (sampled lower bound)
SAMPLE_CAP = 3000  # sample points per (body, body) direction
GUIDANCE = "guidance (not from a K09-L source document)"

# --------------------------------------------------------------------------- classification helpers

FASTENER_PREFIXES = ("screw ", "shcs ", "nut ", "locknut ", "washer ", "standoff ", "prop locknut ")


def _low(c: Component) -> str:
    return (c.key + " " + c.name).lower()


def is_fastener(c: Component) -> bool:
    """Fastener hardware: screws, SHCS, nuts, locknuts, washers, standoffs (incl. the prop nut and M5
    washer of the motor kit). Rule: the component name (before ' — ') starts with one of
    FASTENER_PREFIXES."""
    return (c.key.split(" — ")[0].lower() + " ").startswith(FASTENER_PREFIXES)


def is_not_modelled_by_design(m: EngineeringModel, c: Component) -> Optional[str]:
    """Reason a BOM line has no geometry by design (mass-only, consumable, spare), else None."""
    k = c.key.lower()
    if "(mass only)" in k:
        return "mass-only item (spread over the wing in the mass model)"
    if k.startswith("(consumable)"):
        return "consumable (adhesive)"
    if c.key in m.spare_keys or k.startswith("(spare"):
        return "spare part, not installed"
    return None


# subassembly id -> (name, explode direction rule)
SUBASSEMBLIES = [
    ("wing", "Wing (core, spar, joiner, elevons, hinge tape, laminating film)"),
    ("fins", "Fins & feet (fins, fin clips, feet)"),
    ("propulsion", "Propulsion pods (motor tubes, mounts, motors, props, adapters)"),
    ("pod", "Pod & avionics (pod shell, hatch, FC, ESC, RX, GPS, airspeed, battery, saddle, strap)"),
    ("controls", "Controls (servos, arms, horns, pushrods, clevises, doublers, torque rods, fin bearings)"),
    ("wiring", "Wiring harnesses (harnesses, connectors, plugs, servo extensions)"),
    ("fasteners", "Fasteners (screws, nuts, locknuts, washers, standoffs)"),
    ("consumables", "Consumables & spares (adhesives, spare battery; not installed geometry)"),
]
_SUB_NAME = dict(SUBASSEMBLIES)


def subassembly_of(c: Component) -> str:
    """Subassembly id of one component (rules on group and name; documented in SUBASSEMBLIES)."""
    k = _low(c)
    first = c.key.split(" — ")[0].lower()
    if k.startswith("(consumable)") or k.startswith("(spare"):
        return "consumables"
    if first.startswith("prop ") or "prop adapter" in first or first.startswith("washer m5"):
        return "propulsion"
    if is_fastener(c):
        return "fasteners"
    if c.group == "wiring" or first.startswith(("plug ", "connector ", "servo extension")):
        return "wiring"
    if any(w in first for w in ("wing core", "spar", "elevon", "hinge tape", "laminating film")):
        return "wing"
    if any(w in first for w in ("fin depron", "fin clip", "foot ")):
        return "fins"
    if c.group == "propulsion" or "motor tube" in first:
        return "propulsion"
    if c.group == "controls" or any(w in first for w in ("control horn", "torque rod", "fin bearing")):
        return "controls"
    return "pod"  # pod shell, hatch, ESC deck, saddle, strap, avionics group


def subassemblies(model: EngineeringModel) -> list[dict]:
    """Subassembly tree: every active component in exactly one subassembly (empty ones omitted)."""
    out = {sid: {"id": sid, "name": name, "components": []} for sid, name in SUBASSEMBLIES}
    for c in model.active_components():
        out[subassembly_of(c)]["components"].append(c.key)
    return [v for v in out.values() if v["components"]]


# --------------------------------------------------------------------------- geometry helpers

def _bbox(model: EngineeringModel, b: Body) -> tuple[np.ndarray, np.ndarray]:
    P = model.body_mesh(b)[0]
    return P.min(0), P.max(0)


def _bbox_dist(lo1, hi1, lo2, hi2) -> float:
    d = np.maximum(0.0, np.maximum(lo1 - hi2, lo2 - hi1))
    return float(np.linalg.norm(d))


# --------------------------------------------------------------------------- exploded view

EXPLODE_STEP_MM = 120.0  # explode distance per unit factor (guidance: a display choice)


def _sgn(v: float) -> float:
    return 1.0 if v >= 0 else -1.0


def _explode_dir(sub: str, c: Component, b: Body) -> np.ndarray:
    """Display direction x magnitude (in EXPLODE_STEP units) for one non-fastener body."""
    first = c.key.split(" — ")[0].lower()
    cy, cz = b.centroid[1], b.centroid[2]
    if sub == "wing":
        if "wing core" in first or "laminating" in first:
            return np.zeros(3)
        if "spar" in first:  # spar tubes and joiner drop out downwards (joiner further)
            return np.array([0.0, 0.0, -1.0]) * (1.6 if "joiner" in first else 1.0)
        if "hinge tape" in first:
            return np.array([0.6, 0.0, 0.0])
        return np.array([1.2, 0.0, 0.0])  # elevons aft
    if sub == "fins":
        out = np.array([0.0, _sgn(cy), 0.0])
        if "clip" in first:
            return out * 0.6 + np.array([0.0, 0.0, 0.5 * _sgn(cz)])
        if "foot" in first:
            return out * 1.0 + np.array([0.9, 0.0, 0.0])
        return out * 1.0
    if sub == "propulsion":
        d = np.array([-1.0, 0.6 * _sgn(cy), 0.6 * _sgn(cz)])
        d /= np.linalg.norm(d)
        mult = 0.6
        for w, f in (("motor tube", 0.4), ("motor mount", 0.8), ("motor ", 1.2), ("adapter", 1.7),
                     ("washer m5", 1.9), ("prop ", 2.2)):
            if w in first + " ":
                mult = f
                break
        return d * mult
    if sub == "pod":
        mult = 1.0
        for w, f in (("pod shell", 0.4), ("esc deck", 0.6), ("pod hatch", 2.0), ("gps", 2.4),
                     ("lipo", 1.4), ("battery", 1.2), ("receiver", 1.6), ("airspeed", 1.2), ("pitot", 1.2)):
            if w in first:
                mult = f
                break
        return np.array([0.0, 0.0, mult])
    if sub == "controls":
        if any(w in first for w in ("control horn", "torque rod", "fin bearing")):
            return np.array([1.0, 0.0, -0.6])  # moves with the elevon (aft) and down
        return np.array([0.0, 0.0, -1.0])  # servos and linkage down
    if sub == "wiring":
        return np.array([0.0, 0.0, 0.7])
    return np.zeros(3)


def explode_vectors(model: EngineeringModel) -> dict[int, np.ndarray]:
    """Deterministic explode offset (mm, factor 1) of every active body, by subassembly: propulsion
    forward and outward, avionics up, spar down, elevons aft, fins outward, servos/linkage down,
    wiring up. A fastener follows the nearest non-fastener body (bounding-box distance), pulled 30 %
    further. Directions and distances are a display choice (guidance)."""
    bodies = model.active_bodies()
    comps = model.components
    vec: dict[int, np.ndarray] = {}
    hosts = [b for b in bodies if not is_fastener(comps[b.component])]
    hbox = [_bbox(model, b) for b in hosts]
    for b in hosts:
        c = comps[b.component]
        vec[b.index] = _explode_dir(subassembly_of(c), c, b) * EXPLODE_STEP_MM
    for b in bodies:
        c = comps[b.component]
        if not is_fastener(c):
            continue
        lo, hi = _bbox(model, b)
        dists = [_bbox_dist(lo, hi, l2, h2) for l2, h2 in hbox]
        # tie-break deterministically by host index
        j = min(range(len(hosts)), key=lambda i: (round(dists[i], 6), hosts[i].index))
        vec[b.index] = vec[hosts[j].index] * 1.3
    return vec


def exploded_points(model: EngineeringModel, factor: float = 1.0) -> np.ndarray:
    """A transformed *copy* of ``model.points`` with every active body moved by factor x its explode
    vector. ``model.points`` itself is never modified."""
    model.modified_bodies()  # configuration geometry first
    P = np.array(model.points, copy=True)
    for i, v in explode_vectors(model).items():
        b = model.bodies[i]
        P[b.vo:b.vo + b.vc] += factor * v
    return P


# --------------------------------------------------------------------------- pair scan (shared engine)

_CACHE: dict = {}


def _surface(model: EngineeringModel, b: Body):
    import pyvista as pv
    import vtk

    P, T = model.body_mesh(b)
    pd = pv.PolyData(np.ascontiguousarray(P), np.hstack([np.full((len(T), 1), 3), T]).ravel())
    cl = vtk.vtkCleanPolyData()  # merge the per-face duplicated vertices of the export
    cl.SetInputData(pd)
    cl.SetTolerance(0.0)
    # A triangle that collapses when vertices merge must be dropped, not turned into a line or vertex cell:
    # vtkImplicitPolyDataDistance (VTK 9.6) reads every closest cell as a triangle, so a line cell makes it
    # read past the cell's point ids and crash at random (segfault in compute_implicit_distance).
    cl.ConvertPolysToLinesOff()
    cl.ConvertLinesToPointsOff()
    cl.Update()
    return pv.wrap(cl.GetOutput())


def _samples(model: EngineeringModel, b: Body) -> np.ndarray:
    P, T = model.body_mesh(b)
    S = np.vstack([P, P[T].mean(1)])
    return np.unique(np.round(S, 4), axis=0)


def _pair_scan(model: EngineeringModel, margin: float, only: Optional[set] = None) -> dict:
    """Sampled inside / distance data of all bounding-box candidate pairs of active bodies of different
    components (rules 1-4 of the module docstring). ``only``: restrict to pairs with at least one body
    index in this set. Returns {(i, j) i<j: dict(n, n_in, pen, gap)} plus stats."""
    import pyvista as pv
    import vtk
    from vtk.util.numpy_support import vtk_to_numpy

    key = (id(model), model.geometry_hash(), round(margin, 6), None if only is None else frozenset(only))
    full = (id(model), model.geometry_hash(), round(margin, 6), None)
    if key in _CACHE:
        return _CACHE[key]
    if only is not None and full in _CACHE:
        r = _CACHE[full]
        return {"pairs": {k: v for k, v in r["pairs"].items() if k[0] in only or k[1] in only},
                "stats": r["stats"]}
    t0 = time.perf_counter()
    bodies = model.active_bodies()
    idx = np.array([b.index for b in bodies])
    lo = np.array([_bbox(model, b)[0] for b in bodies])
    hi = np.array([_bbox(model, b)[1] for b in bodies])
    comp = [b.component for b in bodies]
    cand: dict[int, list[int]] = defaultdict(list)  # position -> partner positions
    n_pairs = 0
    for i in range(len(bodies)):
        ov = (lo[i + 1:] <= hi[i] + margin).all(1) & (hi[i + 1:] >= lo[i] - margin).all(1)
        for j in np.nonzero(ov)[0] + i + 1:
            if comp[i] == comp[j]:
                continue
            if only is not None and idx[i] not in only and idx[j] not in only:
                continue
            cand[i].append(j)
            cand[j].append(i)
            n_pairs += 1
    rng = np.random.default_rng(0)
    samp: dict[int, np.ndarray] = {}
    raw: dict[tuple, tuple] = {}
    sampled_dirs = 0
    for bpos in sorted(cand):
        partners = cand[bpos]
        S = _surface(model, bodies[bpos])
        pts, own = [], []
        for a in partners:
            if a not in samp:
                samp[a] = _samples(model, bodies[a])
            X = samp[a]
            X = X[((X >= lo[bpos] - margin) & (X <= hi[bpos] + margin)).all(1)]
            if len(X) > SAMPLE_CAP:
                X = X[rng.choice(len(X), SAMPLE_CAP, replace=False)]
                sampled_dirs += 1
            pts.append(X)
            own.append(np.full(len(X), a))
        X = np.vstack(pts)
        own = np.concatenate(own)
        if not len(X):
            continue
        pp = pv.PolyData(X)
        d = np.abs(np.asarray(pp.compute_implicit_distance(S)["implicit_distance"]))
        if bodies[bpos].closed:
            sel = vtk.vtkSelectEnclosedPoints()
            sel.SetInputData(pp)
            sel.SetSurfaceData(S)
            sel.SetTolerance(1e-6)
            sel.Update()
            ins = vtk_to_numpy(sel.GetOutput().GetPointData().GetArray("SelectedPoints")).astype(bool)
        else:
            ins = np.zeros(len(X), bool)
        for a in np.unique(own):
            k = own == a
            di, ii = d[k], ins[k]
            raw[(a, bpos)] = (int(k.sum()), int(ii.sum()), float(di[ii].max()) if ii.any() else 0.0,
                              float(di[~ii].min()) if (~ii).any() else float("inf"))
    pairs: dict[tuple, dict] = {}
    for (a, bpos), (n, n_in, pen, gap) in raw.items():
        i, j = sorted((int(idx[a]), int(idx[bpos])))
        r = pairs.setdefault((i, j), {"n": 0, "n_in": 0, "pen": 0.0, "gap": float("inf")})
        r["n"] += n
        r["n_in"] += n_in
        r["pen"] = max(r["pen"], pen)
        r["gap"] = min(r["gap"], gap)
    out = {"pairs": pairs, "stats": {"bodies": len(bodies), "candidate_pairs": n_pairs,
                                     "sampled_directions": sampled_dirs, "sample_cap": SAMPLE_CAP,
                                     "seconds": time.perf_counter() - t0}}
    _CACHE[key] = out
    return out


# --------------------------------------------------------------------------- expected-contact rules

# declared fits: (keywords of side A, keywords of side B, reason). Keywords match the part before " — ".
FIT_PAIRS = [
    (("spar joiner",), ("spar tube",), "joiner rod bonded inside the spar tube (slip / bonded fit)"),
    (("spar tube", "spar joiner"), ("wing core",), "spar bonded in its hot-wire slot in the XPS core"),
    (("motor tube",), ("motor mount",), "motor tube pressed / bonded into the motor-mount socket"),
    (("motor tube",), ("fin depron", "fin clip"), "motor tube bonded along the fin / clamped by the clips"),
    (("fin clip",), ("fin depron", "wing core"), "clips clamp the fin and seat on the wing core"),
    (("fin bearing",), ("fin depron", "torque rod", "elevon"), "bushing bonded in the fin, torque rod runs in it"),
    (("torque rod",), ("elevon", "control horn", "wing core", "hinge tape"), "torque rod bonded in the elevon"),
    (("hinge tape",), ("wing core", "elevon"), "tape bonded on the hinge line"),
    (("prop adapter", "prop ", "washer m5"), ("motor ", "prop adapter", "prop ", "washer m5"),
     "prop collet / ring / washer clamp stack on the motor shaft"),
    (("motor mount",), ("motor ",), "motor base seated on the mount face"),
    (("servo arm",), ("servo ",), "arm pressed on the servo output spline"),
    (("servo doubler",), ("servo ", "wing core"), "G10 doubler bonded under the servo in its pocket"),
    (("servo ",), ("wing core",), "servo seated in its pocket in the core"),
    (("pushrod",), ("clevis", "servo arm", "control horn"), "pushrod Z-bend / clevis pin connections"),
    (("clevis",), ("control horn",), "clevis pin in the horn hole"),
    (("foam tape",), ("",), "adhesive foam pad compressed under the part it holds"),
    (("battery strap", "battery saddle"), ("lipo", "pod shell", "battery saddle"),
     "compliant strap / EVA saddle preload on the battery and pod floor"),
    (("pod hatch",), ("pod shell", "gps mast"), "hatch seated on the shell lip; mast bonded on the hatch"),
    (("esc deck", "adapter plate"), ("pod shell", "esc ", "fc ", "standoff"), "deck / plate seated in the pod"),
    (("silicone pressure tubing",), ("pitot", "airspeed"), "tubing pushed over the pressure barbs"),
]

_CONDUCTOR = ("harness", "plug ", "connector ", "bullet connector", "servo extension", "silicone pressure")
_ELECTRICAL_TARGETS = ("harness", "plug ", "connector ", "bullet", "servo extension", "esc ", "fc ", "receiver",
                       "gps ", "airspeed", "capacitor", "usb extender", "lipo", "motor sunnysky", "servo kst",
                       "pitot", "silicone pressure")


def _first(c: Component) -> str:
    return c.key.split(" — ")[0].lower() + " "


def expected_contact_rule(ca: Component, cb: Component) -> Optional[tuple[str, str]]:
    """(rule, reason) when an overlap between these components is expected by design, else None."""
    if is_fastener(ca) or is_fastener(cb):
        return "fastener", "fastener in its hole / clamped joint (threads and holes not modelled with clearance)"
    fa, fb = _first(ca), _first(cb)
    for x, y in ((fa, fb), (fb, fa)):
        if (x.startswith(_CONDUCTOR) or "harness" in x) and any(t in y for t in _ELECTRICAL_TARGETS):
            return "termination", "conductor / tube terminates in a connector, pad, lead or barb"
    for ka, kb, why in FIT_PAIRS:
        for x, y in ((fa, fb), (fb, fa)):
            if any(k in x for k in ka) and any(k in y for k in kb):
                return "fit", why
    return None


# --------------------------------------------------------------------------- assembly sequence

STEPS = [
    ("Wing core prep and spar", "wing",
     lambda f: any(w in f for w in ("wing core", "spar", "laminating")) or f.startswith("(consumable)"),
     ["hot-wire foam cutter (slot)", "sanding block", "5-min epoxy / foam-safe CA", "laminating iron",
      "steel rule", "weights for bonding"],
     ["spar slot depth and straightness before bonding", "joiner centred (50 mm each side)",
      "wing core weighed before / after laminating (compare BOM masses)"],
     "guidance: spar flush or <= 0.5 mm below the core surface; joiner bonded full length"),
    ("Elevons and hinges", "wing", lambda f: "elevon" in f or "hinge tape" in f,
     ["sanding block (bevel)", "knife", "steel rule"],
     ["hinge line straight; elevon deflects +/-30 deg (joint limits) without binding",
      "hinge gap even along the span"],
     "guidance: hinge bevel to clear +/-30 deg (the joint limits are from the CAD)"),
    ("Fins, fin clips and feet", "fins", lambda f: any(w in f for w in ("fin depron", "fin clip", "foot ")),
     ["hex key 2.5 mm (M3 SHCS)", "8 mm nut driver / spanner (M3 locknut)", "square"],
     ["fins perpendicular to the wing", "clip screws tight, Depron not crushed",
      "feet: all four contact points in one plane (stand-up check)"],
     "guidance: fin square to the wing within ~1 deg; do not over-tighten clips on foam"),
    ("Motor tubes, motor mounts and motors", "propulsion",
     lambda f: any(w in f for w in ("motor tube", "motor mount", "motor sunnysky")),
     ["hex key 2.5 mm", "spanner for M3 locknuts", "threadlocker (medium, metal-to-metal only)", "square"],
     ["motor bolt pattern measured against the mount BEFORE printing (bolt pattern is MISSING in the "
      "catalog)", "motor screws do not touch the windings (screw length vs mount thickness)",
      "thrust axes parallel; mounts square to the tube"],
     "guidance: screw engagement >= 1.5 x d into the motor base; check with the motor's own drawing"),
    ("Pod and avionics", "pod", lambda f: True,
     ["Phillips / hex drivers for M2-M3", "nylon screw driver (no over-torque)", "multimeter", "USB cable"],
     ["FC orientation arrow forward (aircraft frame)", "no shorts on PDB/ESC pads (continuity check)",
      "battery fits saddle, strap holds it; CG check with battery in place"],
     "guidance: nylon M3 screws hand-tight only; keep the FC soft-mounted"),
    ("Servos and linkage", "controls", lambda f: True,
     ["servo tester", "small Phillips", "pliers for Z-bend", "protractor / throw meter"],
     ["servo centred before fitting the arm", "elevon neutral at servo neutral",
      "throw +/-30 deg elevon <-> ~+/-30 deg servo (coupling table in the joints)", "no binding at the ends"],
     "guidance: pushrod length 49.5 mm (from the CAD linkage); set with the clevis"),
    ("Wiring harnesses", "wiring", lambda f: True,
     ["soldering iron (temperature-controlled, 60 W+)", "wire strippers", "heat gun", "multimeter",
      "smoke stopper / current-limited supply"],
     ["polarity of XT60 and PDB leads", "continuity and no short (+ to -)", "motor phase order sets the "
      "rotation direction (CW/CCW per the motor naming)", "first power-up through a smoke stopper"],
     "guidance: solder joints fully wetted; heat-shrink over every joint"),
    ("Propellers (last, after all ground tests without props)", "propulsion",
     lambda f: f.startswith(("prop ", "prop adapter", "washer m5")),
     ["prop wrench / spanner for the M5 nut", "prop balancer"],
     ["CCW props (MR) on M1/M2, CW props (MRP) on M3/M4 as named in the CAD",
      "props balanced; nut tight; spin-up test restrained"],
     "guidance: props fitted only after the motor-direction and failsafe checks"),
]


def _fastener_hosts(model: EngineeringModel, touch_mm: float = 0.5) -> dict[int, set[str]]:
    """For every active fastener body, the non-fastener components it joins: components whose bodies it
    overlaps or touches (gap <= touch_mm, sampled), propagated through fastener stacks (screw - washer
    - nut). Computed from the geometry, no counts are assumed."""
    comps = model.components
    fbodies = {b.index for b in model.active_bodies() if is_fastener(comps[b.component])}
    scan = _pair_scan(model, max(touch_mm, 0.5), only=fbodies)
    adj: dict[int, set[int]] = defaultdict(set)
    hosts: dict[int, set[str]] = {i: set() for i in fbodies}
    for (i, j), r in scan["pairs"].items():
        if not (r["n_in"] > 0 or r["gap"] <= touch_mm):
            continue
        for a, b in ((i, j), (j, i)):
            if a in fbodies:
                if b in fbodies:
                    adj[a].add(b)
                else:
                    hosts[a].add(model.bodies[b].component)
    # propagate through fastener-fastener contacts (connected stacks)
    seen: set[int] = set()
    for s in sorted(fbodies):
        if s in seen:
            continue
        stack, comp_ = [s], []
        seen.add(s)
        while stack:
            u = stack.pop()
            comp_.append(u)
            for v in adj[u]:
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        joined = set().union(*(hosts[u] for u in comp_))
        for u in comp_:
            hosts[u] = set(joined)
    return hosts


def assembly_steps(model: EngineeringModel) -> list[dict]:
    """Build order for the K09-L with the fasteners of each step matched by proximity of the fastener
    bodies to the parts they join. A fastener goes to the *latest* step among the parts it joins
    (it can only be fitted when all of them are present). Tool, check and tolerance notes are
    guidance unless stated otherwise."""
    comps = model.components
    active = {c.key: c for c in model.active_components()}
    remaining = {k for k, c in active.items() if not is_fastener(c)
                 and not (c.key in model.spare_keys or c.key.lower().startswith("(spare"))}
    steps = []
    owner: dict[str, int] = {}
    for n, (title, sub, pred, tools, checks, tol) in enumerate(STEPS, start=1):
        keys = sorted(k for k in remaining if pred(_first(active[k])) and (
            subassembly_of(active[k]) == sub or (n == 1 and subassembly_of(active[k]) == "consumables")))
        remaining -= set(keys)
        for k in keys:
            owner[k] = n
        steps.append({"step": n, "title": title, "subassembly": sub, "subassembly_name": _SUB_NAME[sub],
                      "components": keys, "fasteners": [], "fastener_counts": {}, "tools": tools,
                      "tools_class": GUIDANCE, "checks": checks, "checks_class": GUIDANCE,
                      "tolerances_note": tol})
    leftovers = sorted(remaining)  # anything not caught by a step rule goes to the pod/avionics step
    for k in leftovers:
        steps[4]["components"].append(k)
        owner[k] = 5
    # fasteners by proximity
    hosts = _fastener_hosts(model)
    per_step: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    unmatched: dict[str, int] = defaultdict(int)
    for i, joined in hosts.items():
        b = model.bodies[i]
        st = max((owner[k] for k in joined if k in owner), default=None)
        if st is None:
            unmatched[b.component] += 1
        else:
            per_step[st][b.component] += 1
    for s in steps:
        cnt = {}
        for fk, nb in per_step[s["step"]].items():
            c = comps[fk]
            bodies_per_piece = max(1, round(len(c.bodies) / max(c.qty, 1)))
            cnt[fk] = int(round(nb / bodies_per_piece))
        s["fasteners"] = sorted(cnt)
        s["fastener_counts"] = cnt
    # count reconciliation against the BOM quantity
    recon = []
    for k, c in active.items():
        if not is_fastener(c):
            continue
        used = sum(s["fastener_counts"].get(k, 0) for s in steps)
        if used != c.qty or unmatched.get(k):
            recon.append({"component": k, "bom_qty": c.qty, "matched": used,
                          "unmatched_bodies": unmatched.get(k, 0)})
    for s in steps:
        s["fastener_reconciliation"] = [r for r in recon if r["component"] in s["fasteners"]]
    if unmatched:
        steps[-1].setdefault("notes", []).append(
            "fastener bodies touching no part (not matched): " + ", ".join(f"{k} x{v}" for k, v in unmatched.items()))
    return steps


# --------------------------------------------------------------------------- checks

def _joint_check(model: EngineeringModel) -> list[dict]:
    active = {b.index for b in model.active_bodies()}
    out = []
    for name, j in model.joints.items():
        problems = []
        if not j.get("origin_mm"):
            if name in model.links:
                problems.append("no origin_mm (scripted linkage: endpoints available in model.links "
                                f"{model.links[name]})")
            else:
                problems.append("no origin_mm and no link endpoints")
        if j.get("type") == "revolute" and not j.get("axis"):
            problems.append("revolute joint without axis")
        missing_moves = []
        for mv in j.get("moves", []):
            leaf = re.split(r"(?<=\S)\+(?=\S)", mv)[-1]  # occurrence path; keys may contain " + "
            ck = leaf.rsplit(":", 1)[0]
            if ck not in model.components:
                missing_moves.append(f"{mv} (component not in the BOM)")
                continue
            if not any(model.bodies[i].joint == name and i in active for i in model.components[ck].bodies):
                missing_moves.append(f"{mv} (no active body of {ck} on this joint)")
        if missing_moves:
            problems.append("moves reference missing bodies: " + "; ".join(missing_moves))
        if problems:
            sev = "info" if all(p.startswith("no origin_mm (scripted") for p in problems) else "error"
            out.append({"joint": name, "type": j.get("type"), "problems": problems, "severity": sev})
    return out


def _mounting_check(model: EngineeringModel) -> list[dict]:
    out = []
    for o in model.catalog["motor"]:
        sel = model.config["motor"] == o.id
        bp = o.q("bolt_pattern")
        if bp.missing:
            out.append({"item": o.name, "selected": sel, "status": "unverified",
                        "issue": "motor bolt pattern MISSING: mount holes cannot be verified",
                        "note": bp.note})
        if o.geometry == "mass_only":
            out.append({"item": o.name, "selected": sel, "status": "CAD envelope not updated",
                        "issue": "option has no CAD: the K09-L motor envelope and mount are kept; only the "
                                 "mass changes", "note": o.notes})
    for kind in ("battery", "esc", "prop"):
        o = model.option(kind)
        if o.geometry == "mass_only":
            out.append({"item": o.name, "selected": True, "status": "CAD envelope not updated",
                        "issue": f"selected {kind} has no CAD in the assembly", "note": o.notes})
    return out


def _electrical_check(model: EngineeringModel, pairs: dict, tol: float) -> list[dict]:
    active_b = {b.index for b in model.active_bodies()}
    comps = {c.key: c for c in model.active_components()}
    out = []
    harness = {k: c for k, c in comps.items() if k.startswith("Harness")}
    for k, c in harness.items():
        if not [i for i in c.bodies if i in active_b]:
            out.append({"interface": k, "issue": "harness has no geometry"})
    # terminations: each harness must touch / overlap at least two other components (both ends)
    touch: dict[str, set[str]] = defaultdict(set)
    for (i, j), r in pairs.items():
        if r["n_in"] > 0 or r["gap"] <= 0.5:
            ci, cj = model.bodies[i].component, model.bodies[j].component
            touch[ci].add(cj)
            touch[cj].add(ci)
    for k in harness:
        others = {o for o in touch.get(k, set()) if not is_fastener(model.components[o])}
        if len(others) < 2:
            # look further (5 mm) to say what the open end is near
            hb = {i for i in harness[k].bodies if i in active_b}
            wide = _pair_scan(model, 5.0, only=hb)["pairs"]
            near = sorted(((r["gap"], model.bodies[j if i in hb else i].component)
                           for (i, j), r in wide.items()
                           if r["n_in"] == 0 and 0.5 < r["gap"] < float("inf")
                           and model.bodies[j if i in hb else i].component not in others), key=lambda t: t[0])
            hint = (f"; nearest other part {near[0][1]!r} at {near[0][0]:.2f} mm (sampled)" if near
                    else "; no other part within 5 mm")
            out.append({"interface": k, "issue": f"only {len(others)} terminating part(s) within 0.5 mm "
                                                 f"({sorted(others)}); a harness needs two ends" + hint})
    # motors: each motor instance (clustered by quadrant) needs a motor harness touching it
    mot_key = next((k for k in comps if k.startswith("Motor ") and comps[k].group == "propulsion"), None)
    if mot_key:
        mc = comps[mot_key]
        quads: dict[tuple, list[int]] = defaultdict(list)
        for i in mc.bodies:
            if i in active_b:
                cen = model.bodies[i].centroid
                quads[(cen[1] >= 0, cen[2] >= 0)].append(i)
        if len(quads) != mc.qty:
            out.append({"interface": mot_key, "issue": f"{len(quads)} motor instances found in the geometry "
                                                       f"for BOM qty {mc.qty}"})
        mh = {k: c for k, c in harness.items() if k.lower().startswith("harness motor")}
        for q, bl in sorted(quads.items()):
            near = set()
            for (i, j), r in pairs.items():
                if (i in bl or j in bl) and (r["n_in"] > 0 or r["gap"] <= 0.5):
                    other = model.bodies[j if i in bl else i].component
                    if other in mh:
                        near.add(other)
            where = f"motor at y{'+' if q[0] else '-'} z{'+' if q[1] else '-'}"
            if not near:
                out.append({"interface": where, "issue": "no motor harness connected (no harness body within 0.5 mm)"})
            elif len(near) > 1:
                out.append({"interface": where, "issue": f"several motor harnesses touch it: {sorted(near)}"})
        if len(mh) != mc.qty:
            out.append({"interface": "motor harnesses", "issue": f"{len(mh)} motor harnesses for {mc.qty} motors"})
    # servos: one servo harness per servo
    srv = next((k for k in comps if k.startswith("Servo KST")), None)
    if srv:
        sh = [k for k in harness if k.lower().startswith("harness servo")]
        if len(sh) != comps[srv].qty:
            out.append({"interface": "servo harnesses", "issue": f"{len(sh)} servo harnesses for {comps[srv].qty} servos"})
    return out


PROP_CLEARANCE_MIN_MM = 25.0  # blade-tip gap the K09-L requirements set (DBF-Combined R-PROP-CLEAR)


def rotor_sweep(model: EngineeringModel, step_deg: float = 5.0) -> list[dict]:
    """Turn each prop (with its hub hardware) through a full revolution about its joint axis and find its
    closest approach to every part that does not turn with it (its own motor excepted: the bell spins).
    A negative or zero clearance means the prop cannot spin."""
    from scipy.spatial import cKDTree
    act = model.active_bodies()
    out = []
    for name in sorted(n for n in model.joints if n.startswith("Prop M")):
        j = model.joints[name]
        o = np.asarray(j["origin_mm"], float)
        a = np.asarray(j["axis"], float)
        a = a / np.linalg.norm(a)
        mov = [b for b in act if b.joint == name]
        blades = [b for b in mov if b.component.startswith("Prop APC") or b.name == "Prop"] or mov
        own_motor = [b for b in act if b not in mov and "Motor SunnySky" in b.component
                     and np.linalg.norm(b.centroid - o - a * ((b.centroid - o) @ a)) < 25]
        others = [b for b in act if b not in mov and b not in own_motor]
        if not blades or not others:
            continue
        Q = np.vstack([model.body_mesh(b)[0] for b in others])
        owner = np.concatenate([[b.index] * b.vc for b in others])
        tree = cKDTree(Q)
        P = np.vstack([model.body_mesh(b)[0] for b in blades])
        K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
        best = (np.inf, None, 0.0)
        for th in np.radians(np.arange(0.0, 360.0, step_deg)):
            R = np.eye(3) + np.sin(th) * K + (1 - np.cos(th)) * K @ K
            d, i = tree.query((P - o) @ R.T + o)
            k = int(np.argmin(d))
            if d[k] < best[0]:
                best = (float(d[k]), int(owner[i[k]]), float(np.degrees(th)))
        nb = model.bodies[best[1]]
        sev = "error" if best[0] <= 0.5 else ("warning" if best[0] < PROP_CLEARANCE_MIN_MM else "ok")
        out.append({"rotor": name, "min_clearance_mm": round(best[0], 1), "nearest": f"{nb.component} | {nb.name}",
                    "angle_deg": best[2], "severity": sev,
                    "text": (f"{name}: turns freely, closest approach {best[0]:.1f} mm to {nb.component}"
                             if sev != "error" else f"{name}: STRIKES {nb.component} - the prop cannot spin")})
    return out


def check_assembly(model: EngineeringModel, clearance_mm: float = 0.5) -> dict:
    """Assembly checks. Returns a dict with the keys:

    ``missing`` (BOM lines without geometry that should have it), ``not_modelled_by_design``
    (mass-only, consumable and spare lines), ``open_bodies`` (non-watertight bodies, only used as
    point sources), ``interferences``, ``expected_contacts``, ``contacts`` (touching, count only per
    component pair), ``tight_clearances``, ``unresolved_joints``, ``incompatible_mounting``,
    ``electrical_gaps``, ``orphan_fasteners`` (fasteners joining nothing in this configuration),
    ``rules`` (the classification rules) and ``stats`` (runtime, sampling).
    See the module docstring for the interference rules."""
    t0 = time.perf_counter()
    comps = model.components
    missing, by_design = [], []
    for c in model.active_components():
        if c.bodies:
            continue
        why = is_not_modelled_by_design(model, c)
        (by_design if why else missing).append({"component": c.key, "name": c.name, "qty": c.qty,
                                                **({"reason": why} if why else {})})
    open_b = [{"body": b.index, "name": b.name, "component": b.component} for b in model.active_bodies()
              if not b.closed]
    scan = _pair_scan(model, max(clearance_mm, 0.5))
    inter, expected, tight = [], [], []
    contacts: dict[tuple, int] = defaultdict(int)
    for (i, j), r in sorted(scan["pairs"].items()):
        bi, bj = model.bodies[i], model.bodies[j]
        ci, cj = comps[bi.component], comps[bj.component]
        rec = {"bodies": (i, j), "names": (bi.name, bj.name), "components": (ci.key, cj.key),
               "penetration_mm": round(r["pen"], 4), "inside_samples": r["n_in"], "samples": r["n"],
               "min_gap_mm": None if r["gap"] == float("inf") else round(r["gap"], 4)}
        if r["n_in"] > 0 and r["pen"] > CONTACT_TOL_MM:
            rule = expected_contact_rule(ci, cj)
            if rule:
                expected.append({**rec, "rule": rule[0], "reason": rule[1]})
            else:
                soft = any(w in _first(c) for c in (ci, cj)
                           for w in ("xps", "depron", "eva", "foam", "wing core", "fin depron"))
                rec["severity"] = "minor (within foam compliance; confirm at assembly)" \
                    if soft and r["pen"] < 0.25 else "major"
                inter.append(rec)
        elif r["n_in"] > 0 or r["gap"] <= CONTACT_TOL_MM:
            contacts[tuple(sorted((ci.key, cj.key)))] += 1
        elif r["gap"] < clearance_mm:
            rule = expected_contact_rule(ci, cj)
            tight.append({**rec, "expected_by_rule": rule[0] if rule else None})
    inter.sort(key=lambda r: -r["penetration_mm"])
    tight.sort(key=lambda r: r["min_gap_mm"])
    orphans: dict[str, int] = defaultdict(int)
    for i, joined in _fastener_hosts(model).items():
        if not joined:
            orphans[model.bodies[i].component] += 1
    # fasteners that served a part the configuration suppresses (bounding-box contact, 0.5 mm)
    active_idx = {b.index for b in model.active_bodies()}
    supp = [b for b in model.bodies if b.index not in active_idx]
    served: dict[str, int] = defaultdict(int)
    if supp:
        sbox = [_bbox(model, b) for b in supp]
        for b in model.active_bodies():
            if is_fastener(comps[b.component]):
                lo, hi = _bbox(model, b)
                if any(_bbox_dist(lo, hi, l2, h2) <= 0.5 for l2, h2 in sbox):
                    served[b.component] += 1
    res = {
        "missing": missing,
        "not_modelled_by_design": by_design,
        "open_bodies": open_b,
        "interferences": inter,
        "expected_contacts": expected,
        "contacts": [{"components": k, "body_pairs": v} for k, v in sorted(contacts.items())],
        "tight_clearances": tight,
        "unresolved_joints": _joint_check(model),
        "rotor_sweep": rotor_sweep(model),
        "incompatible_mounting": _mounting_check(model),
        "electrical_gaps": _electrical_check(model, scan["pairs"], CONTACT_TOL_MM),
        "orphan_fasteners": [{"component": k, "bodies": v, "issue": "fastener touches no part (e.g. its part "
                              "was suppressed by the configuration): remove from the BOM or re-use"}
                             for k, v in sorted(orphans.items())]
        + [{"component": k, "bodies": v, "issue": "fastener served a part suppressed in this configuration "
            f"({', '.join(sorted({b.component for b in supp}))}); it stays in the BOM - review quantity"}
           for k, v in sorted(served.items())],
        "rules": {
            "overlap": f"sampled penetration > {CONTACT_TOL_MM} mm (vertices + triangle centroids, "
                       f"<= {SAMPLE_CAP} per pair direction; vtkSelectEnclosedPoints + unsigned distance)",
            "contact": f"inside samples with penetration <= {CONTACT_TOL_MM} mm or gap <= {CONTACT_TOL_MM} mm",
            "tight": f"{CONTACT_TOL_MM} mm < sampled gap < {clearance_mm} mm",
            "expected_contact": ["fastener: one side is fastener hardware (in its hole / clamped)",
                                 "termination: conductor/connector/tube overlapping the electrical or "
                                 "pneumatic part it ends in (NOT structure)",
                                 "fit: declared press/slip/bonded fits: "
                                 + "; ".join(f"{'/'.join(a)} in {'/'.join(b) or 'any'} ({w})"
                                             for a, b, w in FIT_PAIRS)],
            "limits": "sample-based: an edge-through-face crossing with no sample inside is missed; "
                      "penetration is a lower bound and the gap an upper bound",
        },
        "stats": {**scan["stats"], "clearance_mm": clearance_mm, "seconds_total": time.perf_counter() - t0},
    }
    return res


# --------------------------------------------------------------------------- BOM

def bill_of_materials(model: EngineeringModel) -> list[dict]:
    """Configured BOM: one row per active component with unit / extended mass and price (configuration
    overrides applied), the evidence class of the price and mass, vendor, url and subassembly."""
    rows = []
    for c in model.active_components():
        m = model.component_mass(c)
        p = model.component_price(c)
        q = model.effective_qty(c)
        rows.append({
            "key": c.key, "name": c.name, "pn": c.pn, "group": c.group, "qty": q,
            "unit_mass_kg": m.value, "ext_mass_kg": None if m.value is None else m.value * q,
            "mass_class": m.source,
            "unit_price_usd": p.value, "ext_price_usd": None if p.value is None else p.value * q,
            "price_class": p.source, "price_note": p.note,
            "vendor": c.vendor, "url": c.url, "process": c.process,
            "subassembly": subassembly_of(c), "has_geometry": bool(c.bodies),
        })
    return rows
