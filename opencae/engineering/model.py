"""The common engineering model of an aircraft: components, bodies (geometry), joints, requirements,
configuration (selected catalog options) and revisions. Every subsystem reads this one object.

Geometry is the authoritative K09-L Fusion export (tessellated bodies, 418 of them, with joints); the
B-rep STEP files are on the team PC and are registered as an unresolved input. Coordinates are the
aircraft frame of the Fusion model: x aft, y right, z up in cruise attitude, millimetres in storage,
metres in calculations.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.engineering import catalog as C
from opencae.engineering.quantities import ANALYTICAL, ESTIMATE, MISSING, SUPPLIER, TEST, USER, Q

DATA = Path(__file__).resolve().parent.parent / "examples" / "k09l"

GROUPS = ["airframe", "propulsion", "controls", "avionics", "wiring", "hardware"]
GROUP_COLOURS = {"airframe": "#4C8DE0", "propulsion": "#E0613A", "controls": "#8E6FD8", "avionics": "#5DB24D",
                 "wiring": "#D45C8E", "hardware": "#7F8C9E"}


@dataclass
class Body:
    index: int
    name: str
    component: str
    group: str
    joint: Optional[str]
    colour: tuple
    vo: int
    vc: int
    io: int
    ic: int
    volume: float = 0.0  # mm^3 (closed bodies)
    area: float = 0.0  # mm^2
    centroid: np.ndarray = field(default_factory=lambda: np.zeros(3))  # mm, aircraft frame
    closed: bool = True
    inertia_unit: np.ndarray = field(default_factory=lambda: np.zeros((3, 3)))  # mm^2 per unit mass about centroid


@dataclass
class Component:
    key: str
    name: str
    pn: str
    group: str
    qty: int
    unit_mass: Q
    unit_price: Q
    vendor: str = ""
    url: str = ""
    price_note: str = ""
    bodies: list = field(default_factory=list)  # body indices
    process: str = ""
    material: str = ""
    revision: str = "A"
    validation: str = "not verified"
    notes: str = ""

    @property
    def purchased(self) -> bool:
        return not self.pn.startswith("K09L-")


@dataclass
class Requirement:
    id: str
    text: str
    metric: str  # key of a calculated result
    op: str  # <= | >= | ==
    threshold: Q
    source: str
    verification: str  # analysis | test | inspection | demonstration
    kind: str = "hard"  # hard | working | watch


def _mesh_props(P: np.ndarray, T: np.ndarray) -> tuple[float, float, np.ndarray, bool, np.ndarray]:
    """Volume, area, centroid, closedness and unit-mass inertia (about the centroid) of a triangle mesh."""
    q = np.round(P / 1e-3).astype(np.int64)
    u, inv = np.unique(q, axis=0, return_inverse=True)
    inv = inv.ravel()
    t2 = inv[T]
    t2 = t2[(t2[:, 0] != t2[:, 1]) & (t2[:, 1] != t2[:, 2]) & (t2[:, 0] != t2[:, 2])]
    e = np.sort(np.concatenate([t2[:, [0, 1]], t2[:, [1, 2]], t2[:, [2, 0]]]), axis=1)
    _, cnt = np.unique(e, axis=0, return_counts=True)
    closed = bool((cnt == 2).all())
    a, b, c = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    cr = np.cross(b - a, c - a)
    area = float(0.5 * np.linalg.norm(cr, axis=1).sum())
    if closed:
        v6 = np.einsum("ij,ij->i", a, np.cross(b, c))  # 6 x signed tetra volumes (origin apex)
        vol = float(v6.sum() / 6.0)
        if abs(vol) > 1e-9:
            cen = ((a + b + c) * v6[:, None]).sum(0) / (4.0 * v6.sum())
            # second moments of the tetrahedra (origin, a, b, c): integral of x_i x_j dV
            S = np.zeros((3, 3))
            for i in range(3):
                for j in range(3):
                    S[i, j] = (v6 / 120.0 * (2 * (a[:, i] * a[:, j] + b[:, i] * b[:, j] + c[:, i] * c[:, j])
                                             + a[:, i] * b[:, j] + a[:, j] * b[:, i] + a[:, i] * c[:, j]
                                             + a[:, j] * c[:, i] + b[:, i] * c[:, j] + b[:, j] * c[:, i])).sum()
            S = S / vol - np.outer(cen, cen)  # per unit volume, about the centroid
            I_unit = np.trace(S) * np.eye(3) - S
            return abs(vol), area, cen, True, I_unit
    w = np.linalg.norm(cr, axis=1)
    cen = ((a + b + c) / 3.0 * w[:, None]).sum(0) / max(w.sum(), 1e-12)
    return 0.0, area, cen, False, np.zeros((3, 3))


def _process_of(c: Component) -> tuple[str, str]:
    n = (c.name + " " + c.key).lower()
    if "xps" in n:
        return "Hot-wire cut XPS foam", "XPS foam"
    if "depron" in n:
        return "Laser / knife cut Depron sheet", "Depron 6 mm"
    if "petg" in n and "tpu" in n:
        return "FDM print (PETG body, TPU pad)", "PETG + TPU"
    if "petg" in n:
        return "FDM print", "PETG"
    if "g10" in n:
        return "Cut / drill G10 sheet", "G10 fibreglass"
    if "cf " in n or "cf rod" in n or "carbon" in n:
        return "Cut bought CF tube / rod to length", "Carbon fibre"
    if "eva" in n:
        return "Cut EVA foam", "EVA foam"
    if c.group == "wiring" or "harness" in n or "wire" in n:
        return "Make harness (cut, strip, solder, heat-shrink)", "Copper / silicone"
    return "Purchase", ""


class EngineeringModel:
    """The K09-L engineering model plus the active configuration."""

    def __init__(self, data_dir: Path = DATA):
        self.data_dir = Path(data_dir)
        meta = json.loads((self.data_dir / "model_meta.json").read_text())
        bom = json.loads((self.data_dir / "bom.json").read_text())
        z = np.load(self.data_dir / "k09l_mesh.npz")
        P3 = z["points"].astype(float)
        self.points = np.column_stack([P3[:, 2], P3[:, 0], P3[:, 1]])  # viewer frame -> aircraft frame (mm)
        self.indices = z["indices"].astype(np.int64)
        self.joints = {j["name"]: j for j in meta["joints"]}
        self.links = meta.get("link", {})
        self.bodies: list[Body] = []
        for i, b in enumerate(meta["bodies"]):
            body = Body(i, b["n"], b["c"], b["g"], b.get("j"), tuple(b["col"]), b["vo"], b["vc"], b["io"], b["ic"])
            P, T = self.body_mesh(body)
            body.volume, body.area, body.centroid, body.closed, body.inertia_unit = _mesh_props(P, T)
            self.bodies.append(body)
        self.components: dict[str, Component] = {}
        for r in bom:
            est = "ESTIMATE" in (r.get("ps") or "").upper()
            key = r["c"]
            if key in self.components:  # BOM lines without geometry may share a placeholder key
                key = f"{r['c']} {r['n']}"
            c = Component(key, r["n"], r["pn"], r["g"], int(r["q"]),
                          Q(r["m"] / 1000.0, "kg", ANALYTICAL if r["pn"].startswith("K09L-") else SUPPLIER, C.K09L_BOM,
                            note="CAD mass (Fusion material density)" if r["pn"].startswith("K09L-")
                            else "BOM line mass (datasheet / vendor)"),
                          Q(r["p"], "USD", ESTIMATE if est else SUPPLIER, C.K09L_BOM, note=r.get("ps", "")),
                          r.get("v", ""), r.get("u", ""), r.get("ps", ""))
            c.process, c.material = _process_of(c)
            self.components[c.key] = c
        for b in self.bodies:
            self.components[b.component].bodies.append(b.index)
        self.spare_keys = {k for k, c in self.components.items() if k.startswith("(spare pack)")}
        # pristine CAD, kept so that parametric design changes (catalog options with geometry "modify")
        # can be applied and removed without ever altering the imported K09-L data
        self._points0 = self.points.copy()
        self._joints0 = copy.deepcopy(self.joints)
        self._props0 = {b.index: (b.volume, b.area, b.centroid.copy(), b.closed, b.inertia_unit.copy())
                        for b in self.bodies}
        self._comp0 = {k: (c.pn, c.revision, c.notes, c.name) for k, c in self.components.items()}
        self._geo_key = None
        self._modified: set[int] = set()
        self._mass_scale: dict[str, float] = {}
        self.catalog = C.catalog()
        self.config: dict = dict(C.DEFAULT_CONFIG)
        self.params: dict[str, Q] = {
            "payload": Q(0.0, "kg", USER, "C2: no payload"),
            "tax_rate": Q(0.1025, "-", USER, "Decision page: cost_model.py tax 10.25 %"),
            "shipping": Q(None, "USD", MISSING, "shipping per order not in the K09-L files"),
            "budget": Q(2000.0, "USD", USER, "C1 hard: $2 000 total"),
            "crash_reserve": Q(309.0, "USD", USER, "Decision gate: reserve covers one $309 crash"),
            "contingency": Q(0.05, "-", USER, "5 % of purchased parts"),
            "rho": Q(1.225, "kg/m^3", USER, "ISA sea level (adjustable)"),
            "g": Q(9.80665, "m/s^2", SUPPLIER, "standard gravity"),
            "hover_min": Q(120.0, "s", USER, "G6 mission: hover 2 min"),
            "cruise_min": Q(300.0, "s", USER, "G6 mission: cruise 5 min"),
            "cd0": Q(0.035, "-", ESTIMATE, "flying wing with pod, fins and four nacelles: zero-lift drag "
                     "coefficient, not measured", uncertainty=0.3),
            "oswald": Q(0.80, "-", ESTIMATE, "span efficiency, typical", uncertainty=0.1),
            "eta_prop_cruise": Q(0.55, "-", ESTIMATE, "11x4.5 static-optimised prop in cruise", uncertainty=0.25),
            "fm": Q(0.65, "-", ESTIMATE, "figure of merit cross-check value", uncertainty=0.15),
            "cl_max": Q(0.85, "-", ESTIMATE, "wing C_L,max ~ 0.05 + 3.81 x 12 deg (stall in the flight model)",
                        uncertainty=0.2),
            "np_frac": Q(0.25, "-", ESTIMATE, "neutral point at 25 % chord (unswept flying wing, props ignored)",
                         uncertainty=0.2),
            "gust_lean_deg": Q(36.0, "deg", ESTIMATE, "Decision page: lean 36 deg at 8 m/s gust (wind study)"),
            "throttle_max": Q(1.0, "-", USER, "flight-controller motor output limit (1.0 = no limit)",
                              note="fraction of the battery voltage applied to the motors at full stick"),
            "transition_pitch_end_deg": Q(75.0, "deg", USER, "transition procedure: pitch-over angle from vertical "
                                          "(flight-simulation scenario default)"),
            "transition_pitch_rate_deg": Q(45.0, "deg/s", USER, "transition procedure: pitch-over rate"),
            "transition_airspeed": Q(14.0, "m/s", USER, "transition procedure: airspeed at which wing-borne flight "
                                     "is declared (forward) / entry airspeed (back)"),
        }
        self.requirements = default_requirements()
        self.revisions: list[dict] = []
        self.unresolved = [
            ("B-rep CAD", "STEP / .f3d files are on the team PC (C:\\Users\\trist\\K09L_build); only the "
                          "tessellated Fusion export is available here. FEA geometry is rebuilt from part dimensions."),
            ("Motor bolt pattern", "Modelled 4 x M3 on 19 x 25 mm; SunnySky drawing not reached. Measure."),
            ("FC and servo mounting", "FC 30.5 x 26.5 from a manual sketch (one vendor lists 30 x 30)."),
            ("Propeller performance", "APC PER3 file for 11x4.5MR not reachable: C_T, C_P are provisional."),
            ("Battery internal resistance", "Not published; provisional 4 mOhm/cell."),
            ("Aerodynamic coefficients", "No wind-tunnel or CFD data for K09-L: C_D0, e, neutral point provisional."),
            ("Material strengths (PETG, XPS, Depron)", "Printed-part and foam strengths not yet tested."),
            ("Shipping", "Not in the K09-L cost files."),
        ]

    # ------------------------------------------------------------------ geometry access
    def body_mesh(self, b: Body) -> tuple[np.ndarray, np.ndarray]:
        if hasattr(self, "_geo_key"):  # not during import
            self._sync_geometry()
        P = self.points[b.vo:b.vo + b.vc]
        T = self.indices[b.io:b.io + b.ic].reshape(-1, 3)
        return P, T

    def option(self, kind: str) -> C.Option:
        return C.option(self.catalog, kind, self.config[kind])

    def _sync_geometry(self) -> None:
        """Apply the parametric geometry changes of the selected options (geometry == "modify") to a copy of
        the pristine CAD; re-derive volume, centroid and inertia of the bodies they move. Called lazily from
        every geometry accessor, so a configuration change (set_option, restore_revision, trade study) is
        always reflected."""
        key = tuple(self.config.get(k) for k in C.GEOMETRY_KINDS)
        if key == self._geo_key:
            return
        self._geo_key = key
        self.points[:] = self._points0
        self.joints.clear()
        self.joints.update(copy.deepcopy(self._joints0))
        for i in self._modified:
            b = self.bodies[i]
            b.volume, b.area, cen, b.closed, I = self._props0[i]
            b.centroid, b.inertia_unit = cen.copy(), I.copy()
        for k, (pn, rev, notes, name) in self._comp0.items():
            c = self.components[k]
            c.pn, c.revision, c.notes, c.name = pn, rev, notes, name
        self._modified, self._mass_scale = set(), {}
        for kind in C.GEOMETRY_KINDS:
            o = C.option(self.catalog, kind, self.config[kind])
            if o.geometry != "modify":
                continue
            moved: set[int] = set()
            for mod in o.mods:
                moved |= self._apply_mod(mod)
            for mod in o.mods:
                for ck in mod["components"]:
                    c = self.components.get(ck)
                    if c is None:
                        continue
                    if o.pn and ck == o.bom_component:
                        c.pn, c.revision = o.pn, "B"
                        c.notes = o.notes
                    if mod.get("pn"):
                        c.pn, c.revision = mod["pn"], "B"
                    if mod.get("name"):
                        c.name = mod["name"]
                    if mod.get("mass") == "volume":
                        self._mass_scale[ck] = None  # computed below from the new volumes
            for i in moved:
                b = self.bodies[i]
                P = self.points[b.vo:b.vo + b.vc]
                T = self.indices[b.io:b.io + b.ic].reshape(-1, 3)
                b.volume, b.area, b.centroid, b.closed, b.inertia_unit = _mesh_props(P, T)
            self._modified |= moved
        for ck in list(self._mass_scale):
            c = self.components[ck]
            v0 = sum(self._props0[i][0] for i in c.bodies)
            v1 = sum(self.bodies[i].volume for i in c.bodies)
            self._mass_scale[ck] = v1 / v0 if v0 > 0 else 1.0
        self._supp_key = None

    def _apply_mod(self, mod: dict) -> set[int]:
        ax, d = int(mod["axis"]), float(mod["delta"])
        bodies = [i for ck in mod["components"] if ck in self.components for i in self.components[ck].bodies]
        if mod.get("hardware_of"):  # fasteners of other components, selected by proximity and side
            r = float(mod.get("within", 4.0))
            ref = [self.bodies[i] for ck in mod["hardware_of"] for i in self.components[ck].bodies]
            boxes = [(self._points0[b.vo:b.vo + b.vc].min(0) - r, self._points0[b.vo:b.vo + b.vc].max(0) + r)
                     for b in ref]  # imported CAD: selection never depends on earlier mods
            w = mod.get("where", {})
            for b in self.bodies:
                if b.group != "hardware" or b.component in mod.get("exclude", []):
                    continue
                c0 = self._props0[b.index][2]
                if not any(np.all(c0 >= lo) and np.all(c0 <= hi) for lo, hi in boxes):
                    continue
                if "abs_lt" in w and not abs(c0[ax]) < w["abs_lt"]:
                    continue
                if "abs_gt" in w and not abs(c0[ax]) > w["abs_gt"]:
                    continue
                bodies.append(b.index)
        if mod["op"] == "translate" and mod.get("hardware_within"):
            r = float(mod["hardware_within"])
            boxes = []
            for i in bodies:
                b = self.bodies[i]
                P = self.points[b.vo:b.vo + b.vc]
                boxes.append((P.min(0) - r, P.max(0) + r))
            for b in self.bodies:
                if b.group == "hardware" and b.index not in bodies and any(
                        np.all(b.centroid >= lo) and np.all(b.centroid <= hi) for lo, hi in boxes):
                    bodies.append(b.index)
        for i in bodies:
            b = self.bodies[i]
            P = self.points[b.vo:b.vo + b.vc]
            if mod["op"] == "stretch":
                c = P[:, ax]
                if mod.get("mirror"):
                    ac = np.abs(c)
                    sel = np.ones(len(c), bool)
                    if "beyond" in mod:
                        sel &= ac > mod["beyond"]
                    if "below" in mod:
                        sel &= ac < mod["below"]
                    P[sel, ax] += np.sign(c[sel]) * d
                else:
                    sel = c > mod["beyond"]
                    P[sel, ax] += d
            elif mod["op"] == "translate":
                if mod.get("mirror"):
                    s_ = np.sign(b.centroid[ax]) if b.centroid[ax] != 0 else 1.0
                    P[:, ax] += s_ * d
                else:
                    P[:, ax] += d
        if mod.get("joints"):
            for name, j in self.joints.items():
                if name.startswith(mod["joints"]) and j.get("origin_mm"):
                    o = list(j["origin_mm"])
                    o[ax] += (np.sign(o[ax]) if mod.get("mirror") else 1.0) * d
                    j["origin_mm"] = o
        return set(bodies)

    def modified_bodies(self) -> set[int]:
        """Bodies whose geometry differs from the imported CAD in the current configuration."""
        self._sync_geometry()
        return set(self._modified)

    def suppressed_bodies(self) -> set[int]:
        """Bodies removed by the configuration: the bodies of a suppressed option's component, and the
        fasteners (hardware bodies) whose centroid lies within 15 mm of those bodies' bounding box -
        the screws, washers and nuts that only served the removed part."""
        self._sync_geometry()
        land = self.option("landing")
        key = (land.id, land.geometry)
        if getattr(self, "_supp_key", None) == key:
            return self._supp
        out: set[int] = set()
        if land.geometry == "suppress" and land.bom_component in self.components:
            gone = [self.bodies[i] for i in self.components[land.bom_component].bodies]
            out |= {b.index for b in gone}
            for b in self.bodies:
                if b.group != "hardware":
                    continue
                for g in gone:
                    P = self.body_mesh(g)[0]
                    lo, hi = P.min(0) - 15.0, P.max(0) + 15.0
                    if np.all(b.centroid >= lo) and np.all(b.centroid <= hi):
                        out.add(b.index)
                        break
        self._supp_key, self._supp = key, out
        return out

    def active_bodies(self) -> list[Body]:
        """Bodies present in the current configuration (suppressed options removed)."""
        gone = self.suppressed_bodies()
        return [b for b in self.bodies if b.index not in gone]

    def effective_qty(self, c: Component) -> int:
        """BOM quantity in the current configuration (reduced when some of its bodies are suppressed)."""
        if not c.bodies:
            return c.qty
        gone = self.suppressed_bodies()
        left = sum(1 for i in c.bodies if i not in gone)
        return int(round(c.qty * left / len(c.bodies)))

    def active_components(self) -> list[Component]:
        return [c for c in self.components.values() if self.effective_qty(c) > 0]

    # ------------------------------------------------------------------ configuration
    def component_mass(self, c: Component) -> Q:
        """Unit mass with configuration overrides (a catalog option replacing the part in this slot)."""
        for kind in ("motor", "battery"):
            o = self.option(kind)
            default = C.option(self.catalog, kind, C.DEFAULT_CONFIG[kind])
            if c.key == default.bom_component and o.id != default.id:
                return o.q("mass")
        self._sync_geometry()
        k = self._mass_scale.get(c.key)
        if k is not None and abs(k - 1.0) > 1e-9:
            return Q(c.unit_mass.value * k, "kg", ESTIMATE, c.unit_mass.ref,
                     note=f"CAD mass of revision A x volume ratio {k:.3f} of the modified geometry (same material "
                          f"and infill assumed)", inputs=["unit_mass (CAD)", "volume ratio"])
        return c.unit_mass

    def component_price(self, c: Component) -> Q:
        for kind in ("motor", "battery"):
            o = self.option(kind)
            default = C.option(self.catalog, kind, C.DEFAULT_CONFIG[kind])
            if c.key == default.bom_component and o.id != default.id:
                return o.q("price")
        return c.unit_price

    def config_hash(self) -> str:
        blob = json.dumps({"config": self.config, "params": {k: v.value for k, v in self.params.items()}},
                          sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()[:12]

    def geometry_hash(self) -> str:
        ids = [b.index for b in self.active_bodies()]
        mods = ",".join(f"{k}={self.config.get(k)}" for k in C.GEOMETRY_KINDS
                        if C.option(self.catalog, k, self.config[k]).geometry == "modify")
        return hashlib.sha1((DATA.name + ":" + ",".join(map(str, ids)) + "|" + mods).encode()).hexdigest()[:12]

    def set_option(self, kind: str, oid: str) -> tuple[bool, str]:
        o = C.option(self.catalog, kind, oid)
        if not o.compatible:
            return False, o.incompatible_reason
        self.config[kind] = oid
        return True, ""

    # ------------------------------------------------------------------ revisions
    def save_revision(self, note: str, results: Optional[dict] = None) -> dict:
        rev = {"id": f"r{len(self.revisions) + 1}", "time": time.strftime("%Y-%m-%d %H:%M:%S"), "note": note,
               "config": dict(self.config), "params": {k: v.to_dict() for k, v in self.params.items()},
               "config_hash": self.config_hash(), "geometry_hash": self.geometry_hash(),
               "results": copy.deepcopy(results or {})}
        self.revisions.append(rev)
        return rev

    def restore_revision(self, rev_id: str) -> None:
        rev = next(r for r in self.revisions if r["id"] == rev_id)
        self.config = dict(rev["config"])
        self.params = {k: Q.from_dict(v) for k, v in rev["params"].items()}

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps({"format": "opencae-engineering/1", "aircraft": "K09-L",
                                          "config": self.config,
                                          "params": {k: v.to_dict() for k, v in self.params.items()},
                                          "revisions": self.revisions}, indent=1, default=float))

    def load_state(self, path: Path) -> None:
        d = json.loads(Path(path).read_text())
        self.config.update(d.get("config", {}))
        self.params.update({k: Q.from_dict(v) for k, v in d.get("params", {}).items()})
        self.revisions = d.get("revisions", [])


def default_requirements() -> list[Requirement]:
    return [
        Requirement("R1", "Take-off mass within the mass gate", "mass_total", "<=", Q(2.6, "kg", USER, C.DECISION),
                    "Decision: mass gate 2.6 kg", "test"),
        Requirement("R2", "Hover thrust-to-weight at low battery voltage", "tw_low", ">=",
                    Q(1.6, "-", USER, C.DECISION), "G6 / working threshold T/W >= 1.6", "test", "working"),
        Requirement("R3", "Project cost leaves the crash reserve", "reserve", ">=", Q(309.0, "USD", USER, C.DECISION),
                    "C1 hard $2 000; reserve >= one $309 crash", "inspection"),
        Requirement("R4", "Energy for the mission (2 min hover + 5 min cruise)", "mission_margin", ">=",
                    Q(1.0, "-", USER, C.DECISION), "G6 mission", "test"),
        Requirement("R5", "Fits the 1.25 x 0.55 x 0.45 m transport box (one piece)", "box_fit", ">=",
                    Q(1.0, "-", USER, C.DECISION), "C3 transport", "inspection"),
        Requirement("R6", "Standing aircraft does not tip over in the 8 m/s flight-card gust", "tipover_margin",
                    ">=", Q(0.0, "deg", USER, C.DECISION), "Flight card: gust <= 8 m/s", "analysis", "watch"),
        Requirement("R7", "Motor current within the motor's rating at full throttle", "motor_current_margin", ">=",
                    Q(1.0, "-", USER, "supplier rating"), "Component limits", "test"),
        Requirement("R8", "ESC rating covers the motor's maximum current", "esc_margin", ">=",
                    Q(1.0, "-", USER, "supplier rating"), "Component limits", "inspection"),
        Requirement("R9", "Battery current within its rating at full throttle", "battery_c_margin", ">=",
                    Q(1.0, "-", USER, "supplier rating"), "Component limits", "test"),
        Requirement("R10", "Static margin positive and at least 5 %", "static_margin", ">=",
                    Q(0.05, "-", USER, C.DECISION), "Decision: SM 5 % (WATCH)", "test", "watch"),
        Requirement("R11", "Motor tube: safety factor >= 1.5 at full thrust (FEA)", "motor_tube_sf", ">=",
                    Q(1.5, "-", USER, "design rule"), "Structural margin", "analysis"),
        Requirement("R12", "Cell count compatible with motor, ESC and battery", "cells_ok", ">=",
                    Q(1.0, "-", USER, "supplier ratings"), "Electrical compatibility", "inspection"),
    ]
