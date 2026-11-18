"""Extra website data for the single-page K09-L design review: one 3D viewer plus panels.

``site_extra(study, rid, fields_summary)`` returns JSON-serialisable data built only from what the
engineering model computes or carries (engine results, heavy-analysis records, approval logic, the
assembly / manufacturing modules and the flight simulation). Nothing is typed in:

* every part reference is a BOM component key (``Component.key``), the key ``build.aircraft_data``
  uses for ``parts`` and for each body's ``"part"``, so the viewer can highlight it;
* every number keeps its evidence class (``source``); numbers obtained by re-running the level-1 model
  on a scratch engine with one input changed are labelled ``model re-evaluation (estimate)``;
* statuses follow approval.py: a requirement is PASS only on direct evidence; gates G5/G6 are never GO
  (at most GO WITH CONDITIONS, pending tests and a recorded human decision); simulation is not flight
  evidence.

Plain status vocabulary (``plain_status``):
    pass  - criterion met on evidence the approval rules accept
    ok    - predicted to meet the criterion, not proven (CONDITIONAL / estimate-based / test pending)
    watch - meets the prediction with a thin margin (< 20 %, a display rule) or an open issue to review
    fail  - criterion missed
    nev   - not evaluated (no analysis, no measurement or missing data)

Each section is computed independently; a section that raises returns ``{"error": "..."}`` (logged)
instead of breaking the site build.
"""
from __future__ import annotations

import copy
import logging
import math
import re
import time
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

import numpy as np

from opencae.engineering import approval as AP
from opencae.engineering import assembly as AS
from opencae.engineering import catalog as C
from opencae.engineering import manufacturing as MF
from opencae.engineering.graph import CURRENT, FAILED, HEAVY, INPUT_OF_PARAM, NOT_RUN, STALE, Engine
from opencae.engineering.quantities import ESTIMATE, MISSING, REDUCED, SHORT, Q

log = logging.getLogger(__name__)

REEVAL = "model re-evaluation (estimate)"
THIN = 1.2  # display rule: a ratio margin below 1.2 (20 %) is flagged 'watch'
_RANK = {"fail": 4, "watch": 3, "nev": 2, "ok": 1, "pass": 0}
STRONG = AP.STRONG

# requirement -> gates whose criteria cite it (from approval.gates)
REQ_GATES = {"R1": ["G2", "G5"], "R2": ["G2", "G5"], "R3": ["G2", "G4"], "R4": ["G2", "G5"], "R5": ["G2"],
             "R6": ["G3"], "R7": ["G2", "G5"], "R8": ["G2"], "R9": ["G2", "G5"], "R10": ["G3"], "R11": ["G3"],
             "R12": ["G2"]}
# heavy analyses that are G3 criteria
G3_HEAVY = ["fea_motor_tube", "assembly_check", "flight_hover", "fea_spar", "flight_transition", "fea_foot",
            "thermal_mount", "cfd_cruise"]
_CRIT_HEAVY = [("Motor-tube FEA", "fea_motor_tube"), ("Assembly check", "assembly_check"),
               ("Hover simulation", "flight_hover"), ("Spar FEA", "fea_spar"),
               ("Transition simulation", "flight_transition"), ("Foot FEA", "fea_foot"),
               ("Motor-mount thermal", "thermal_mount"), ("Cruise CFD", "cfd_cruise"),
               ("No unexplained interferences", "assembly_check"), ("Electrical interfaces", "assembly_check"),
               ("Props turn freely", "assembly_check")]


# =============================================================================================== status helpers
def plain_status(s) -> str:
    """Map engine / approval / analysis status strings to 'pass' | 'ok' | 'watch' | 'fail' | 'nev'."""
    if s is None:
        return "nev"
    t = str(s).strip()
    low = t.lower()
    table = {
        AP.PASS.lower(): "pass", AP.FAIL.lower(): "fail", AP.COND.lower(): "ok", AP.NOT_EV.lower(): "nev",
        "pass": "pass", "fail": "fail", "cond": "ok", "conditional": "ok", "nev": "nev", "ok": "ok",
        "watch": "watch", "warning": "watch", "error": "fail",
        CURRENT: "ok", STALE: "watch", NOT_RUN: "nev", FAILED: "fail", "missing": "nev",
        "predicted pass": "ok", "predicted fail": "fail", "unit mismatch": "watch",
    }
    if low in table:
        return table[low]
    if low.startswith("predicted pass"):
        return "ok"
    if low.startswith("predicted fail"):
        return "fail"
    if low.startswith("recorded"):
        return "ok"
    return "nev"


def worst(statuses) -> str:
    sts = [s for s in statuses if s in _RANK]
    return max(sts, key=lambda s: _RANK[s]) if sts else "nev"


def _ratio_status(ratio: Optional[float], src: Optional[str], req_status: Optional[str] = None) -> str:
    """Status of a 'value / limit >= 1' check: fail below 1, watch below THIN, otherwise the requirement's
    status (if the check is a requirement) or pass/ok by evidence class."""
    if ratio is None or not isinstance(ratio, (int, float)) or not math.isfinite(ratio):
        return "nev"
    if ratio < 1.0:
        return "fail"
    base = plain_status(req_status) if req_status else ("pass" if src in STRONG else "ok")
    if ratio < THIN:
        return worst([base, "watch"])
    return base


def _f(x, nd: Optional[int] = None):
    if isinstance(x, (np.floating, np.integer)):
        x = x.item()
    if isinstance(x, bool) or x is None:
        return x
    if isinstance(x, (int, float)):
        if not math.isfinite(float(x)):
            return None
        return round(float(x), nd) if nd is not None else float(x)
    return x


def _clean(o):
    """JSON-safe deep copy (numpy, Q, tuples, sets, non-finite floats)."""
    if isinstance(o, Q):
        return {"value": _clean(o.value), "unit": o.unit, "source": o.source, "ref": o.ref}
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return _clean(o.tolist())
    if isinstance(o, (np.floating, np.integer, np.bool_)):
        return _clean(o.item())
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if o is None or isinstance(o, (str, int, bool)):
        return o
    return str(o)


def _fmt(v, nd=2, unit=""):
    if v is None:
        return "—"
    return f"{v:.{nd}f}{(' ' + unit) if unit else ''}"


def _src_label(src: Optional[str]) -> str:
    return SHORT.get(src, src or "?")


def _section(fn, *a):
    t0 = time.perf_counter()
    try:
        out = fn(*a)
    except Exception as exc:  # one section must never break the site build
        log.error("site_extra section %s failed: %s\n%s", getattr(fn, "__name__", fn), exc, traceback.format_exc())
        return {"error": f"{type(exc).__name__}: {exc}"}, time.perf_counter() - t0
    return out, time.perf_counter() - t0


# =============================================================================================== context
class _Ctx:
    """Everything the sections share (computed once)."""

    def __init__(self, study, rid: str, fields: dict):
        self.study, self.rid = study, rid
        study.select(rid)
        self.e = study.engine
        self.m = self.e.model
        self.r = self.e.results
        self.fields = fields or {}
        self.snap = study.snapshots()[rid]
        study.select(rid)
        self.variant = study.variant(rid)
        self.reqs = {x["id"]: x for x in AP.requirements_table(self.e)}
        self.gates = AP.gates(self.e)
        self.tests = MF.test_matrix(self.m, self.r)
        self.heavy = {nid: self.e.heavy_status(nid) for nid in self.e.heavy}
        ac = self.heavy.get("assembly_check") or {}
        self.ac = (ac.get("record") or {}) if ac.get("status") == CURRENT else {}
        self.keys = set(self.m.components)
        self.active_idx = {b.index for b in self.m.active_bodies()}
        self._steps = None
        self._scratch = None
        self._plan = None
        self._tprefix = None

    # ---- shared, lazily computed
    def steps(self):
        if self._steps is None:
            self._steps = AS.assembly_steps(self.m)
        return self._steps

    def plan(self):
        if self._plan is None:
            self._plan = MF.process_plan(self.m)
        return self._plan

    def req_tests(self, rid: str) -> list[str]:
        return [t["id"] for t in self.tests if rid in t["requirement_ids"] and t["id"] != "T13"]

    def comp_tests(self, key: str) -> list[dict]:
        """Tests that name this component. manufacturing.test_matrix matches by name prefix ('ESC' also
        catches 'ESC deck'); here a component only counts for its most specific prefix, and a made part is
        dropped when the same prefix also names a bought part (the test is about the bought one)."""
        if self._tprefix is None:
            self._tprefix = {t[0]: [w.lower() for w in t[4] if w != "all"] for t in MF._TESTS}
        low = key.lower()
        allp = {w for ws in self._tprefix.values() for w in ws}
        mine = [w for w in allp if low.startswith(w)]
        if not mine:
            return []
        best = max(mine, key=len)
        c = self.m.components[key]
        made = c.pn.startswith("K09L-")
        if made and any(k.lower().startswith(best) and not self.m.components[k].pn.startswith("K09L-")
                        for k in self.keys if self.bodies_of(k)):
            return []
        return [t for t in self.tests if best in self._tprefix.get(t["id"], [])]

    def res(self, k):
        q = self.r.get(k)
        return None if q is None else q.value

    def src(self, k):
        q = self.r.get(k)
        return None if q is None else q.source

    def bodies_of(self, key: str):
        c = self.m.components[key]
        return [self.m.bodies[i] for i in c.bodies if i in self.active_idx]

    def centroid(self, bodies) -> Optional[list]:
        if not bodies:
            return None
        w = np.array([max(b.area, 1e-9) for b in bodies])
        P = np.array([b.centroid for b in bodies])
        return [round(float(x), 1) for x in (P * w[:, None]).sum(0) / w.sum()]

    # ---- re-evaluation on a scratch engine (never touches the study's engine)
    def reeval(self, options: Optional[dict] = None, params: Optional[dict] = None) -> dict:
        if self._scratch is None:
            self._scratch = Engine()
        e = self._scratch
        m = e.model
        m.config = dict(self.variant["config"])
        m.params = {k: copy.deepcopy(q) for k, q in m.params_default.items()}
        m.params.update({k: Q.from_dict(d) for k, d in self.variant["params"].items()})
        for k, v in (options or {}).items():
            m.config[k] = v
        for k, v in (params or {}).items():
            q = m.params[k]
            m.params[k] = Q(v, q.unit, q.source, q.ref, q.uncertainty, f"what-if (was {q.value})")
        e.recompute()
        m.suppressed_bodies()
        return {k: q.value for k, q in e.results.items()}


# =============================================================================================== 1 components
def _chk(name, value, unit, limit, status, evidence, analysis_id=None, info=False, source=None, nd=2):
    return {"name": name, "value": _f(value, nd) if isinstance(value, (int, float, np.floating)) else value,
            "unit": unit, "limit": limit, "status": status, "evidence": evidence, "analysis_id": analysis_id,
            "source": source, "info": bool(info)}


def _first(key: str) -> str:
    return key.split(" — ")[0].lower()


def _field_check(ctx: _Ctx, fid: str, name: str) -> Optional[dict]:
    f = (ctx.fields.get(fid) or {}).get("failure") or {}
    if not f:
        return None
    val = f.get("safety_factor") if f.get("safety_factor") is not None else f.get("value")
    if f.get("safety_factor") is not None:
        lim = f"SF ≥ {f.get('line')}"
        unit = "-"
    else:
        lim = f"≤ {f.get('allowable')} {f.get('unit')} (margin line {f.get('line')})"
        unit = f.get("unit")
    return _chk(name, val, unit, lim, plain_status(f.get("status")), f.get("text", ""), fid,
                source="simulation on estimated inputs")


def _rotor_props(ctx: _Ctx) -> dict[str, list[dict]]:
    """Rotor-sweep rows of the (current) assembly check per prop component key (bodies on the rotor joint)."""
    out = defaultdict(list)
    for row in ctx.ac.get("rotor_sweep") or []:
        jn = row["rotor"]
        keys = {b.component for b in ctx.m.active_bodies() if b.joint == jn and b.component.startswith("Prop APC")}
        for k in keys:
            out[k].append(row)
    return out


def _gap_harnesses(ctx: _Ctx) -> dict[str, str]:
    return {g["interface"]: g["issue"] for g in ctx.ac.get("electrical_gaps") or [] if g["interface"] in ctx.keys}


def components_section(ctx: _Ctx) -> list[dict]:
    m, r = ctx.m, ctx.r
    mot, esc, bat = m.option("motor"), m.option("esc"), m.option("battery")
    req = ctx.reqs
    rob = ctx.snap.get("robustness") or []
    rotor_rows = _rotor_props(ctx)
    gaps = _gap_harnesses(ctx)
    interf = defaultdict(list)
    for it in ctx.ac.get("interferences") or []:
        for k in it["components"]:
            interf[k].append(it)
    plan = {p["component"]: p for p in ctx.plan()}
    used = defaultdict(int)
    for s in ctx.steps():
        for k, n in s["fastener_counts"].items():
            used[k] += n
    analysed = defaultdict(list)
    for fid, f in ctx.fields.items():
        if f.get("part"):
            analysed[f["part"]].append(fid)
    if "cfd_cruise" in ctx.fields:  # same rule as build.aircraft_data: CFD covers the outer surfaces
        for c in m.components.values():
            if c.name.startswith(("Wing core", "Elevon", "Fin Depron", "Pod shell", "Pod hatch")) \
                    and "cfd_cruise" not in analysed[c.key]:
                analysed[c.key].append("cfd_cruise")
    trans = ctx.heavy.get("flight_transition") or {}
    trec = (trans.get("record") or {}) if trans.get("status") == CURRENT else {}
    t_warn = [w for _, w in trec.get("warnings") or []]
    therm = ctx.fields.get("thermal_mount") or {}
    t_motor = (therm.get("extra") or {}).get("t_motor_C")
    out = []
    for c in m.components.values():
        if not ctx.bodies_of(c.key):
            continue
        k, f = c.key, _first(c.key)
        proc, mat, prov = MF.process_of(m, c)
        made = MF._is_made(m, c)
        mq, pq = m.component_mass(c), m.component_price(c)
        qty = m.effective_qty(c)
        checks: list[dict] = []
        sugg: list[str] = []
        # ---- propulsion / electrical
        if k == mot.bom_component:
            imax = mot.q("imax").value
            checks.append(_chk("Full-throttle current vs 30-s rating", ctx.res("i_motor_max_full"), "A",
                               f"≤ {imax:g} A (supplier)",
                               _ratio_status(ctx.res("motor_current_margin"), ctx.src("motor_current_margin"),
                                             req["R7"]["status"]),
                               f"margin {_fmt(ctx.res('motor_current_margin'))} (R7, {req['R7']['status']}); "
                               f"{r['i_motor_max_full'].ref}", source=ctx.src("i_motor_max_full")))
            pmax = mot.q("pmax").value
            pm = ctx.res("motor_power_margin")
            checks.append(_chk("Full-throttle electrical power vs max power", pmax / pm if pm else None, "W",
                               f"≤ {pmax:g} W (supplier)", _ratio_status(pm, ctx.src("motor_power_margin")),
                               f"margin {_fmt(pm)}; {r['motor_power_margin'].ref}", source=ctx.src("motor_power_margin")))
            checks.append(_chk("Cell count vs motor rating", bat.q("cells").value, "S",
                               f"≤ {mot.q('cells_max').value:g} S", plain_status(req["R12"]["status"]),
                               r["cells_ok"].ref, source=ctx.src("cells_ok"), nd=0))
            if t_motor is not None:
                checks.append(_chk("Motor temperature, steady hover on a 35 °C day", t_motor, "°C",
                                   "≤ 80 °C (T06 guidance, no supplier limit in the files)",
                                   "watch" if t_motor > 70 else "ok",
                                   "motor base temperature of the motor-mount thermal model (hover loss x 2.5 K/W, "
                                   "estimates)", "thermal_mount", source=ESTIMATE, nd=1))
            nom = next((x for x in rob if x["case"] == "nominal"), None)
            if nom is not None or t_warn:
                over = [w for w in t_warn if "above the catalog" in w]
                checks.append(_chk("Current above the 30-s rating during the nominal transition (6-DOF)",
                                   nom["time_above_motor_rating_s"] if nom else None, "s",
                                   "no excursion expected (30-s rating)", "watch" if (over or (nom and nom[
                                       "time_above_motor_rating_s"] > 0)) else "ok",
                                   "; ".join(over) or "reduced-order transition simulation", "flight_transition",
                                   source=REDUCED))
            checks.append(_chk("Hover current per motor", ctx.res("i_hover_motor"), "A", None, "ok",
                               r["i_hover_motor"].ref, info=True, source=ctx.src("i_hover_motor")))
            checks.append(_chk("Heat per motor at full stick", ctx.res("motor_loss_max"), "W", None, "nev",
                               r["motor_loss_max"].ref + " (no limit in the model)", info=True,
                               source=ctx.src("motor_loss_max"), nd=1))
        elif k == esc.bom_component:
            ic = esc.q("i_cont").value
            em = ctx.res("esc_margin")
            checks.append(_chk("Per-channel rating vs max(motor rating, full-throttle current)", ic, "A",
                               f"≥ {ic / em:.1f} A" if em else None,
                               _ratio_status(em, ctx.src("esc_margin"), req["R8"]["status"]),
                               f"margin {_fmt(em)} (R8, {req['R8']['status']}); {r['esc_margin'].ref}",
                               source=ctx.src("esc_margin"), nd=0))
            checks.append(_chk("Cell count vs ESC range", bat.q("cells").value, "S",
                               f"{esc.q('cells_min').value:g}-{esc.q('cells_max').value:g} S",
                               plain_status(req["R12"]["status"]), r["cells_ok"].ref, source=ctx.src("cells_ok"), nd=0))
        elif k == bat.bom_component:
            cm = ctx.res("battery_c_margin")
            imax_b = bat.q("c_rate").value * bat.q("capacity").value / 3600.0
            checks.append(_chk("Full-throttle battery current vs C rating", ctx.res("i_battery_max"), "A",
                               f"≤ {imax_b:.0f} A (advertised {bat.q('c_rate').value:g} C)",
                               _ratio_status(cm, ctx.src("battery_c_margin"), req["R9"]["status"]),
                               f"margin {_fmt(cm)} (R9, {req['R9']['status']}); {r['battery_c_margin'].ref}",
                               source=ctx.src("i_battery_max"), nd=1))
            mm = ctx.res("mission_margin")
            checks.append(_chk("Usable energy vs mission (2 min hover + 5 min cruise)", mm, "-", "≥ 1",
                               _ratio_status(mm, ctx.src("mission_margin"), req["R4"]["status"]),
                               f"R4 {req['R4']['status']}; {r['mission_margin'].ref}", source=ctx.src("mission_margin")))
            checks.append(_chk("Hover endurance", (ctx.res("endurance_hover") or 0) / 60, "min", None, "ok",
                               r["endurance_hover"].ref, info=True, source=ctx.src("endurance_hover"), nd=1))
            low = [x for x in rob if x.get("ground_contact")]
            if rob:
                checks.append(_chk("Transition robustness cases ending in ground contact", len(low), "cases",
                                   f"0 of {len(rob)}", "watch" if low else "ok",
                                   "; ".join(x["case"] for x in low) + " (operating limits restrict charge/gusts)"
                                   if low else "none", "flight_transition", source=REDUCED, nd=0))
            checks.append(_chk("Battery heat at hover", ctx.res("battery_heat_hover"), "W", None, "nev",
                               r["battery_heat_hover"].ref + " (no limit in the model)", info=True,
                               source=ctx.src("battery_heat_hover"), nd=1))
        elif f.startswith("prop apc"):
            rows = rotor_rows.get(k, [])
            if rows:
                worst_row = min(rows, key=lambda x: x["min_clearance_mm"])
                checks.append(_chk("Blade clearance, full-revolution sweep", worst_row["min_clearance_mm"], "mm",
                                   f"≥ {AS.PROP_CLEARANCE_MIN_MM:g} mm",
                                   {"ok": "pass", "warning": "watch", "error": "fail"}[worst_row["severity"]],
                                   "; ".join(x["text"] for x in rows), "assembly_check", source="analytical", nd=1))
            tw = ctx.res("tw_low")
            checks.append(_chk("Thrust-to-weight at low battery (4 motors)", tw, "-", "≥ 1.6",
                               _ratio_status(tw / 1.6 if tw else None, ctx.src("tw_low"), req["R2"]["status"]),
                               f"R2 {req['R2']['status']}; prop C_T, C_P are provisional estimates",
                               source=ctx.src("tw_low")))
        # ---- structure (field analyses)
        if k in analysed:
            for fid in analysed[k]:
                if (ctx.fields.get(fid) or {}).get("part") == k:
                    fc = _field_check(ctx, fid, {"fea_motor_tube": "Axial stress SF at full thrust (FEA)",
                                                 "fea_spar": "Axial stress SF under lift (FEA)",
                                                 "fea_foot": "Von Mises SF at 1 m/s touchdown (FEA)",
                                                 "thermal_mount": "Hottest point vs material limit (thermal)",
                                                 "cfd_cruise": "Lift at trim / lift needed (CFD)"}.get(fid, fid))
                    if fc:
                        if fid == "fea_motor_tube":
                            fc["evidence"] += f" (R11 {req['R11']['status']})"
                            fc["status"] = worst([fc["status"], plain_status(req["R11"]["status"])])
                        checks.append(fc)
        if f.startswith("foot "):
            fs = ctx.res("foot_sf")
            if fs is not None:
                checks.append(_chk("Outrigger root bending SF (hand calc)", fs, "-", "SF ≥ 2.0 (printed parts)",
                                   _ratio_status(fs / 2.0, ctx.src("foot_sf")), r["foot_sf"].ref, source=ctx.src("foot_sf")))
            td, lean = ctx.res("tipover_deg"), m.params["gust_lean_deg"].value
            checks.append(_chk("Tip-over angle standing vs gust lean", td, "deg", f"> {lean:g} deg",
                               _ratio_status(td / lean if td and lean else None, ctx.src("tipover_deg"),
                                             req["R6"]["status"]),
                               f"R6 {req['R6']['status']}; {r['tipover_margin'].ref}", source=ctx.src("tipover_deg"), nd=1))
        if f.startswith("motor mount"):
            checks.append(_chk("Structural strength of the printed elbow", None, "", None, "nev",
                               "no FEA of the elbow itself: the motor-tube FEA represents it as a remote load; "
                               "printed-part strengths are not tested (unresolved input)"))
            for it in ctx.ac.get("incompatible_mounting") or []:
                if it.get("selected"):
                    checks.append(_chk("Motor bolt pattern vs mount holes", None, "", "measured pattern", "nev",
                                       f"{it['issue']}: {it.get('note', '')}", "assembly_check"))
        if "cfd_cruise" in analysed.get(k, []) and (ctx.fields.get("cfd_cruise") or {}).get("part") != k:
            checks.append(_chk("Outer surface in the cruise CFD", None, "", None, "nev",
                               "covered by the CFD surface; the CFD lift criterion is judged on the wing core",
                               "cfd_cruise", info=True))
        for it in interf.get(k, []):
            other = [x for x in it["components"] if x != k][0]
            checks.append(_chk(f"Interference with {other.split(' — ')[0]}", it["penetration_mm"], "mm", "no overlap",
                               "watch" if "minor" in str(it.get("severity")) else "fail",
                               f"{it.get('severity')}; sampled penetration (lower bound)", "assembly_check",
                               source="analytical", nd=3))
        # ---- wiring
        if c.group == "wiring" or f.startswith(("harness", "plug ", "connector ", "servo extension", "bullet")):
            if k in gaps:
                checks.append(_chk("Both ends terminate (CAD)", None, "", "two terminating parts", "watch", gaps[k],
                                   "assembly_check"))
            elif ctx.ac:
                checks.append(_chk("Both ends terminate (CAD)", None, "", "two terminating parts", "ok",
                                   "no harness gap reported by the assembly check (geometry only, not an electrical test)",
                                   "assembly_check"))
            awg = re.search(r"(\d+)\s*AWG", c.name)
            cur = None
            if f.startswith(("harness battery pigtail", "harness xt60", "harness pdb", "connector pair amass")):
                cur = ("i_battery_max", "full-throttle battery current")
            elif f.startswith(("harness motor", "bullet")):
                cur = ("i_motor_max_full", "full-throttle motor current")
            if cur:
                checks.append(_chk(f"Current carried ({cur[1]})", ctx.res(cur[0]), "A",
                                   None, "nev", ("wire " + awg.group(0) + ": " if awg else "") +
                                   "no wire / connector current rating in the model - not checked",
                                   source=ctx.src(cur[0]), nd=1))
        # ---- fasteners
        if AS.is_fastener(c):
            n = used.get(k, 0)
            checks.append(_chk("BOM quantity vs fasteners found in the CAD (assembly sequence)", n, "pcs",
                               f"= {c.qty} (BOM)", "pass" if n == c.qty else "watch",
                               "fastener bodies matched to the parts they join by proximity", nd=0))
            checks.append(_chk("Strength / preload", None, "", None, "nev",
                               "no analysis: holes, bolt bearing and preload are not modelled"))
        # ---- printed / made parts without an analysis
        if made and proc.startswith("FDM") and not any(not x["info"] and x["analysis_id"] in
                                                       ("fea_foot", "thermal_mount") for x in checks) \
                and not f.startswith("motor mount"):
            checks.append(_chk("Printed-part strength", None, "", None, "nev",
                               "no analysis; PETG strength is a provisional estimate (unresolved input)"))
        if f.startswith("servo kst"):
            checks.append(_chk("Servo torque vs elevon hinge moment", None, "", None, "nev",
                               "no hinge-moment analysis in the model"))
        if f.startswith("fc corewing"):
            checks.append(_chk("FC mounting confirmed", None, "", "measured", "nev",
                               "G4 MEASURE item open: FC 30.5 x 26.5 from a manual sketch"))
        if not [x for x in checks if not x["info"]]:
            checks.append(_chk("Engineering check", None, "", None, "nev",
                               "no analysis or check in the model covers this part"))
        # ---- mass (info)
        checks.append(_chk("Unit mass (model)", (mq.value or 0) * 1000, "g",
                           "measure in T01 (each part within 10 % of BOM, guidance)", "nev",
                           f"{mq.note or mq.ref} [{_src_label(mq.source)}]"
                           + (f"; BOM line mass {c.unit_mass.value * 1000:.2f} g" if mq is not c.unit_mass
                              and c.unit_mass.value else ""), info=True, source=mq.source))
        status = worst([x["status"] for x in checks if not x["info"]])
        for x in checks:
            if x["status"] in ("fail", "watch") and not x["info"]:
                sugg.append(f"{x['name']}: {x['evidence'][:160]}")
        for t in ctx.comp_tests(k):
            sugg.append(f"Verify by {t['id']}: {t['title']}")
        if k in plan:
            sugg.append("Inspect after fabrication: " + "; ".join(plan[k]["inspection"][:2]))
        p = plan.get(k)
        ft = None
        if p and p["time_per_unit"].value is not None:
            ft = {"per_unit_min": round(p["time_per_unit"].value * 60, 1),
                  "total_min": round(p["time_total"].value * 60, 1), "source": p["time_per_unit"].source,
                  "basis": p["time_per_unit"].note}
        ids = sorted({x["analysis_id"] for x in checks if x["analysis_id"]} | set(analysed.get(k, [])))
        out.append({
            "key": k, "name": c.name, "pn": c.pn, "group": c.group,
            "subsystem": AS.subassembly_of(c), "subsystem_name": dict(AS.SUBASSEMBLIES)[AS.subassembly_of(c)],
            "qty": qty, "mass_g": _f((mq.value or 0) * 1000, 2), "mass_source": mq.source,
            "unit_cost_usd": _f(pq.value, 2), "cost_usd": _f(None if pq.value is None else pq.value * qty, 2),
            "cost_source": pq.source, "material": mat, "process": proc, "process_source": prov,
            "make_or_buy": "make" if made else "buy", "fab_time_min": ft,
            "revision": c.revision, "modified_geometry": any(i in m.modified_bodies() for i in c.bodies),
            "checks": checks, "analyses": ids, "status": status, "suggestions": sugg[:8],
        })
    return out


# =============================================================================================== 2 remedies
_ALIAS = {"lean": "gust_lean_deg", "e": "oswald", "clmax": "cl_max", "np": "np_frac", "eta": "eta_prop_cruise",
          "fm": "fm", "throttle_max": "throttle_max"}


def _estimated_inputs(ctx: _Ctx, metric: str) -> list[str]:
    """Estimated / missing inputs a result names (Q.inputs of the result, resolved to model parameters and the
    selected catalog options' quantities). Mass / CG inputs are reported as one line (estimated BOM masses)."""
    m = ctx.m
    q = ctx.r.get(metric)
    if q is None:
        return []
    names = set(q.inputs or [])
    out = []
    for n in sorted(names):
        p = _ALIAS.get(n, n)
        if p in m.params and m.params[p].source in (ESTIMATE, MISSING):
            out.append(f"{p} ({m.params[p].ref or 'estimate'})")
    for kind in C.KINDS:
        o = m.option(kind)
        for qn, oq in getattr(o, "data", {}).items():
            if oq.source in (ESTIMATE, MISSING) and (qn in names or qn.rstrip("0") in names
                                                      or qn.split("_")[0] in names):
                out.append(f"{o.name}: {qn} ({_src_label(oq.source)})")
    if names & {"mass", "cg"}:
        est = [c for c in m.active_components() if m.component_mass(c).source in (ESTIMATE, MISSING)]
        if est:
            out.append("take-off mass / CG: " + ", ".join(c.name for c in est[:4])
                       + " masses are estimates (volume ratio of modified parts)")
    return out


def _act(action, effect, effort, verify_by, source=None):
    return {"action": action, "effect": effect, "effort": effort, "verify_by": verify_by, "source": source}


def _cfd_actions(ctx: _Ctx) -> tuple[str, list[dict], list[str]]:
    from opencae.engineering import flightsim as FS
    f = ctx.fields["cfd_cruise"]
    fl, ex = f.get("failure") or {}, f.get("extra") or {}
    cl, clr, a_deg, V = ex.get("cl"), ex.get("cl_required"), ex.get("alpha_deg"), ex.get("V")
    problem = fl.get("text", "")
    acts = []
    if None in (cl, clr, a_deg, V):
        return problem, [_act("Re-run the cruise CFD", "record lacks C_L / trim data", "medium", "cfd_cruise")], []
    v_need = V * math.sqrt(clr / cl)
    base = ctx.reeval()
    alt = ctx.reeval(params={"cl_max": cl * 1.2 ** 2})
    acts.append(_act(
        f"Cruise faster: fly the trim point at about {v_need:.1f} m/s instead of {V:.1f} m/s",
        f"required C_L falls to the CFD value {cl:.3f} at {v_need:.1f} m/s (C_L ∝ 1/V², same α {a_deg:.1f}°). "
        f"{REEVAL} with the usable C_L set to the CFD value (cl_max = {cl * 1.44:.3f}, 1.2 V_stall kept): "
        f"cruise {_fmt(base.get('v_cruise'), 1)} → {_fmt(alt.get('v_cruise'), 1)} m/s, cruise power "
        f"{_fmt(base.get('p_cruise_elec'), 0)} → {_fmt(alt.get('p_cruise_elec'), 0)} W, mission margin (R4) "
        f"{_fmt(base.get('mission_margin'))} → {_fmt(alt.get('mission_margin'))}. The revision-B transition already "
        f"declares wing-borne flight at {ctx.m.params['transition_airspeed'].value:g} m/s.",
        "low", "cfd_cruise", REEVAL))
    S = ctx.r["area"].value
    acts.append(_act(
        f"More wing area: about {S * clr / cl:.3f} m² instead of {S:.3f} m² (+{(clr / cl - 1) * 100:.0f} %) to "
        f"cruise at {V:.1f} m/s and α {a_deg:.1f}°",
        f"lift ∝ area at fixed C_L (arithmetic on the CFD C_L, unchanged mass: an estimate); a larger wing adds "
        f"mass and changes CG, box fit (R5, margin {_fmt(ctx.res('box_fit'), 3)}) and the structural analyses - "
        f"not re-evaluated (no parametric wing in the model)", "high", "cfd_cruise", "arithmetic (estimate)"))
    slope = (cl - FS.CL0) / math.radians(a_deg)
    a_need = math.degrees((clr - FS.CL0) / slope)
    AR = ctx.r["aspect_ratio"].value
    cla_fm = 2 * math.pi * AR / (2 + math.sqrt(AR ** 2 + 4))  # flightsim._Plant.CLa
    acts.append(_act(
        "Trim at a higher angle of attack / reflex-elevon trim setting",
        f"CFD lift slope {slope:.2f}/rad (flight model {cla_fm:.2f}/rad, Helmbold on AR {AR:.2f}) needs α ≈ {a_need:.1f}° for C_L {clr:.3f} at {V:.1f} m/s, "
        + ("beyond" if a_need > math.degrees(FS.A_STALL) else "within")
        + f" the {math.degrees(FS.A_STALL):.0f}° stall angle of the flight model; elevon trim effect on C_L and "
          "C_m is not in the CFD (elevons neutral) - run a CFD case with the trim deflection",
        "medium", "cfd_cruise", "arithmetic on the CFD record (estimate)"))
    acts.append(_act(
        "Verify the lift slope and C_D0 in the wind tunnel (or a finer CFD mesh with prism layers), then update "
        "the flight model's lift slope and re-run the transition simulation",
        "replaces the provisional C_Lα 3.81/rad of the flight model; " + str((f.get("mesh") or {}).get("note", "")),
        "medium", "T09"))
    return problem, acts, [k for k in ctx.keys if _first(k).startswith(("wing core", "elevon"))]


def remedies_section(ctx: _Ctx, comps: Any) -> list[dict]:
    m, r = ctx.m, ctx.r
    out = []
    mot = m.option("motor")
    part_of_req = {
        "R1": [], "R2": [k for k in ctx.keys if _first(k).startswith(("prop apc", "motor sunnysky"))],
        "R3": [], "R4": [m.option("battery").bom_component], "R5": [k for k in ctx.keys if _first(k).startswith(
            ("motor tube", "motor mount", "fin depron"))],
        "R6": [m.option("landing").bom_component], "R7": [mot.bom_component], "R8": [m.option("esc").bom_component],
        "R9": [m.option("battery").bom_component], "R10": [m.option("battery").bom_component],
        "R11": [k for k in ctx.keys if _first(k).startswith("motor tube")],
        "R12": [mot.bom_component, m.option("esc").bom_component, m.option("battery").bom_component]}

    # ---- requirement-specific model re-evaluations (what-ifs on a scratch engine)
    def whatif(rid):
        acts = []
        try:
            if rid == "R7":
                cur = m.params["throttle_max"].value
                for lim in (round(cur - 0.04, 2),):
                    x = ctx.reeval(params={"throttle_max": lim})
                    acts.append(_act(f"Lower the FC motor output limit to {lim:.2f} (now {cur:.2f})",
                                     f"{REEVAL}: motor current margin {_fmt(ctx.res('motor_current_margin'))} → "
                                     f"{_fmt(x.get('motor_current_margin'))}, T/W at low battery "
                                     f"{_fmt(ctx.res('tw_low'))} → {_fmt(x.get('tw_low'))} (R2 ≥ 1.6); transition "
                                     f"behaviour not re-simulated", "low", "T03", REEVAL))
            if rid == "R6":
                cur = m.params["foot_outrigger_mm"].value
                lo, hi = C.GEOMETRY_PARAMS["foot_outrigger_mm"][1:3]
                new = min(hi, cur + 5.0)
                if new != cur:
                    x = ctx.reeval(params={"foot_outrigger_mm": new})
                    acts.append(_act(f"Widen the foot outrigger to {new:g} mm (now {cur:g} mm)",
                                     f"{REEVAL}: tip-over margin {_fmt(ctx.res('tipover_margin'), 1)} → "
                                     f"{_fmt(x.get('tipover_margin'), 1)} deg, box fit {_fmt(ctx.res('box_fit'), 3)} → "
                                     f"{_fmt(x.get('box_fit'), 3)}, static margin {_fmt((ctx.res('static_margin') or 0) * 100)} → "
                                     f"{_fmt((x.get('static_margin') or 0) * 100)} %; foot FEA becomes stale",
                                     "medium", "T11", REEVAL))
            if rid == "R10":
                q = m.params["np_frac"]
                lo = q.value * (1 - (q.uncertainty or 0.2))
                x = ctx.reeval(params={"np_frac": lo})
                acts.append(_act("Measure the CG (T02) and the neutral point (T09) before flight",
                                 f"{REEVAL}: if the neutral point is {(q.uncertainty or 0.2) * 100:.0f} % further forward "
                                 f"(its stated uncertainty, {q.value:.3f} → {lo:.3f} chord) the static margin is "
                                 f"{_fmt((x.get('static_margin') or 0) * 100)} % instead of "
                                 f"{_fmt((ctx.res('static_margin') or 0) * 100)} %", "medium", "T02", REEVAL))
            if rid == "R3":
                q = m.params["shipping"]
                if q.value is not None:
                    x = ctx.reeval(params={"shipping": q.value * 1.5})
                    acts.append(_act("Replace the shipping estimate with the real order totals (T15)",
                                     f"{REEVAL}: at +50 % shipping (${q.value * 1.5:.0f}) the reserve is "
                                     f"${_fmt(x.get('reserve'), 0)} (now ${_fmt(ctx.res('reserve'), 0)}, limit $309)",
                                     "low", "T15", REEVAL))
        except Exception as exc:  # a what-if never hides the remedy itself
            log.warning("what-if for %s failed: %s", rid, exc)
        return acts

    for rid, x in ctx.reqs.items():
        st = x["status"]
        if st == AP.PASS:
            continue
        tests = ctx.req_tests(rid)
        thr = x["threshold"]
        prob = (f"{rid} {x['text']}: predicted {_fmt(x['predicted'], 4) if isinstance(x['predicted'], float) else x['predicted']} "
                f"{x['op']} {thr:g} {x['unit']} ({x['predicted_class']}) - {x['why']}")
        acts = whatif(rid)
        if x["verification"] == "test" or st == AP.NOT_EV:
            acts.append(_act(f"Measure {x['metric']} in {', '.join(tests) or 'the linked test'} and import the result",
                             "a measurement is the only evidence that turns this requirement PASS (approval rules)",
                             "medium", tests[0] if tests else None))
        est = _estimated_inputs(ctx, x["metric"])
        if not est and x["predicted_class"] in (ESTIMATE, REDUCED, MISSING):
            q = r.get(x["metric"])
            est = [f"{x['metric']}: {(q.ref + ' ' + q.note).strip() if q else 'estimate'}"]
            if x["metric"] == "mass_total":
                est.append("component masses: " + ", ".join(c.name for c in m.active_components()
                                                            if m.component_mass(c).source == ESTIMATE)[:200])
            if x["metric"] == "motor_tube_sf":
                f = (ctx.fields.get("fea_motor_tube") or {}).get("failure") or {}
                est.append(f.get("text", "")[:200])
        if est:
            acts.append(_act("Replace the estimated inputs with supplier data or measurements: " + "; ".join(est[:6]),
                             f"the prediction is no stronger than its weakest input ({x['predicted_class']})",
                             "medium", tests[0] if tests else None))
        if st == AP.FAIL:
            acts.insert(0, _act(x["action"], "criterion missed: change the configuration or re-negotiate", "high",
                                tests[0] if tests else None))
        out.append({"id": rid, "kind": "requirement", "title": x["text"], "status": plain_status(st),
                    "status_raw": st, "problem": prob, "actions": acts,
                    "parts": [k for k in part_of_req.get(rid, []) if k in ctx.keys]})

    # ---- field analyses with a failure status fail / watch
    for fid, f in ctx.fields.items():
        fl = f.get("failure") or {}
        if fl.get("status") not in ("fail", "watch"):
            continue
        if fid == "cfd_cruise":
            prob, acts, parts = _cfd_actions(ctx)
        else:
            prob, acts, parts = fl.get("text", ""), [_act("Review the analysis and its estimated inputs", fl.get(
                "text", ""), "medium", fid)], [f["part"]] if f.get("part") in ctx.keys else []
        out.append({"id": fid, "kind": "analysis", "title": f.get("title") or fid, "status": plain_status(fl["status"]),
                    "status_raw": fl["status"], "problem": prob, "actions": acts, "parts": sorted(parts)})

    # ---- gate criteria not PASS
    req_ids = set(ctx.reqs)
    for g in ctx.gates:
        tests_g = sorted({t for rid in req_ids if g["id"] in REQ_GATES.get(rid, []) for t in ctx.req_tests(rid)})
        for i, c in enumerate(g["criteria"]):
            if c["status"] == AP.PASS:
                continue
            txt, why = c["text"], c.get("why", "")
            mr = re.match(r"(R\d+)\b", txt)
            parts: list[str] = []
            if mr:
                rid = mr.group(1)
                acts = [_act(f"Close requirement {rid} (see its remedy)", ctx.reqs[rid]["why"], "medium",
                             (ctx.req_tests(rid) or [None])[0])]
                parts = [k for k in part_of_req.get(rid, []) if k in ctx.keys]
            elif txt.startswith("Cruise CFD"):
                acts = [_act("See analysis remedy cfd_cruise (cruise faster / more area / trim / wind tunnel)", why,
                             "medium", "cfd_cruise")]
                parts = [k for k in ctx.keys if _first(k).startswith(("wing core", "elevon"))]
            elif txt.startswith("Transition simulation"):
                acts = [_act("Review the simulation warnings; measure the motor current at the 0.66 output limit on "
                             "the thrust stand", why, "low", "T03"),
                        _act("Keep the revision-B operating limits (charge / gusts) on the flight card",
                             "; ".join(_rb_limits()[:1]), "low", "T13")]
                parts = [mot.bom_component]
            elif txt.startswith("No unexplained interferences"):
                its = ctx.ac.get("interferences") or []
                acts = [_act("Confirm the fit at assembly (dry fit before bonding)",
                             "; ".join(f"{a.split(' — ')[0]} / {b.split(' — ')[0]}: {it['penetration_mm']} mm "
                                       f"({it.get('severity')})" for it in its for a, b in [it["components"]]),
                             "low", "assembly_check")]
                parts = sorted({k for it in its for k in it["components"]})
            elif txt.startswith("Electrical interfaces"):
                gaps = ctx.ac.get("electrical_gaps") or []
                acts = [_act(f"Route / terminate {gp['interface']} in the CAD and re-run the assembly check",
                             gp["issue"], "low", "assembly_check") for gp in gaps]
                parts = [gp["interface"] for gp in gaps if gp["interface"] in ctx.keys]
            elif txt.startswith(("Foot FEA", "Motor-mount thermal")):
                fid = "fea_foot" if txt.startswith("Foot") else "thermal_mount"
                acts = [_act("Measure the printed-material property the analysis assumes "
                             + ("(PETG strength, T05 proof load)" if fid == "fea_foot" else "(motor temperature, T06)"),
                             f"analysis on estimated inputs is never a plain PASS: {why}", "medium",
                             "T05" if fid == "fea_foot" else "T06")]
                parts = [ctx.fields.get(fid, {}).get("part")] if ctx.fields.get(fid, {}).get("part") in ctx.keys else []
            elif txt.startswith("Unresolved engineering inputs"):
                acts = [_act(f"Close: {u[0]}", u[1], "medium", None) for u in m.unresolved]
            elif txt.startswith("G3 passed"):
                acts = [_act("Clear the G3 blockers (see G3 remedies)", why or "G3 not passed", "high", None)]
            elif txt.startswith("G5 passed"):
                acts = [_act("Complete the ground tests of G5", why or "G5 not passed", "high", "T03")]
            elif txt.startswith("Motor bolt pattern"):
                acts = [_act("Measure the motor bolt pattern against the mount before printing",
                             "catalog bolt pattern is MISSING (modelled 4 x M3 on 19 x 25 mm)", "low", "T05")]
                parts = [k for k in ctx.keys if _first(k).startswith(("motor mount", "motor sunnysky"))]
            elif txt.startswith("FC / servo mounting"):
                acts = [_act("Measure FC and servo mounting against the parts", "FC 30.5 x 26.5 from a manual sketch",
                             "low", "T10")]
                parts = [k for k in ctx.keys if _first(k).startswith(("fc corewing", "servo kst"))]
            elif txt.startswith("Process plan"):
                acts = [_act("Confirm print settings and times on first articles", why, "low", "T01")]
            elif txt.startswith("Tethered"):
                acts = [_act("Fly the tethered / low hover test after G5 and log it", "no flight-test record", "high",
                             "T12")]
            elif txt.startswith("Independent engineering"):
                acts = [_act("Record an independent engineering & safety review decision (a person decides)",
                             "only a recorded APPROVE clears this; the software never approves", "medium", "T13")]
            elif txt.startswith("Simulation results are not flight evidence"):
                acts = [_act("Standing condition: plan flight tests from the simulation, never approve from it",
                             why, "low", "T12")]
            else:
                acts = [_act("Provide the missing evidence", why or "not evaluated", "medium",
                             tests_g[0] if tests_g else None)]
            out.append({"id": f"{g['id']}.{i + 1}", "kind": "gate", "title": f"{g['id']}: {txt}",
                        "status": plain_status(c["status"]), "status_raw": c["status"],
                        "problem": f"{g['id']} {g['name']} - {txt}: {why or c['status']}", "actions": acts,
                        "parts": [p for p in parts if p in ctx.keys]})

    # ---- component checks fail / watch
    if isinstance(comps, list):
        for cp in comps:
            for j, x in enumerate(cp["checks"]):
                if x["info"] or x["status"] not in ("fail", "watch"):
                    continue
                tests = [t["id"] for t in ctx.comp_tests(cp["key"])]
                out.append({"id": f"C:{cp['key']}#{j + 1}", "kind": "component",
                            "title": f"{cp['name']}: {x['name']}", "status": x["status"], "status_raw": x["status"],
                            "problem": f"{x['name']}: {x['value']} {x['unit'] or ''} vs {x['limit']} - {x['evidence']}",
                            "actions": [_act("Review / verify this check", x["evidence"][:200], "low",
                                             x["analysis_id"] or (tests[0] if tests else None))],
                            "parts": [cp["key"]]})
    return out


def _rb_limits() -> list[str]:
    from opencae.engineering import revision_b as RB
    return list(RB.OPERATING_LIMITS)


# =============================================================================================== 3 gates
def gates_section(ctx: _Ctx) -> list[dict]:
    out = []
    for g in ctx.gates:
        st, dec = g["status"], g.get("decision") or {}
        if g["id"] in ("G5", "G6"):
            if st == AP.FAIL:
                d = "NO-GO"
            elif st == AP.PASS and dec.get("decision") == "APPROVE":
                d = "GO WITH CONDITIONS"
            else:
                d = "NOT READY"
            dt = ("Decided by a person (test engineer / flight-test lead and an independent engineering & safety "
                  "review) on physical measurements; simulation cannot pass this gate. At most GO WITH CONDITIONS "
                  "here, never GO." + (f" Recorded: {dec}" if dec else " No decision recorded."))
        else:
            if st == AP.FAIL:
                d = "NO-GO"
            elif st == AP.NOT_EV:
                d = "NOT READY"
            elif st == AP.PASS and dec.get("decision") == "APPROVE":
                d = "GO"
            else:
                d = "GO WITH CONDITIONS"
            dt = ("Decision support only: the design reviewer records the decision; the evidence says "
                  f"{st}." + (f" Recorded: {dec}" if dec else " No decision recorded."))
        crit = []
        for c in g["criteria"]:
            ev = ""
            mr = re.match(r"(R\d+)\b", c["text"])
            if mr and "numeric threshold" in c["text"]:
                rq = next(x for x in ctx.m.requirements if x.id == mr.group(1))
                ev = f"{rq.metric} {rq.op} {rq.threshold.value:g} {rq.threshold.unit}; source: {rq.source}"
            elif mr and mr.group(1) in ctx.reqs:
                x = ctx.reqs[mr.group(1)]
                ev = (f"predicted {x['predicted']:.4g} {x['op']} {x['threshold']:g} ({x['predicted_class']})"
                      if isinstance(x["predicted"], (int, float)) else "no prediction")
                if x.get("measured") is not None:
                    ev += f"; measured {x['measured']}"
            else:
                nid = next((n for p, n in _CRIT_HEAVY if c["text"].startswith(p)), None)
                if nid:
                    h = ctx.heavy.get(nid) or {}
                    ev = f"{nid}: {h.get('status')} (run {((h.get('record') or {}).get('time'))})"
            crit.append({"text": c["text"], "status": plain_status(c["status"]), "status_raw": c["status"],
                         "why": c.get("why", ""), "evidence": ev})
        out.append({"id": g["id"], "name": g["name"], "status": plain_status(st), "status_raw": st, "decision": d,
                    "decision_text": dt, "criteria": crit,
                    "blocking": [c["text"] for c in g["criteria"] if c["status"] in (AP.FAIL, AP.NOT_EV)],
                    "conditions": [c["text"] for c in g["criteria"] if c["status"] == AP.COND],
                    "open": list(g.get("open") or [])})
    return out


# =============================================================================================== 4 knowledge graph
def _hstatus(ctx: _Ctx, nid: str) -> tuple[str, str]:
    h = ctx.heavy.get(nid) or {}
    st = h.get("status")
    if st == CURRENT:
        f = ((ctx.fields.get(nid) or {}).get("failure") or {})
        if f:
            return plain_status(f.get("status")), f.get("text", "")
        sm = (ctx.snap["heavy"].get(nid) or {}).get("summary") or {}
        return "ok", ", ".join(f"{k} {(round(v, 3) if isinstance(v, float) else v)}" for k, v in list(sm.items())[:4])
    return plain_status(st), str(h.get("reason") or st)


def knowledge_graph_section(ctx: _Ctx) -> dict:
    e, m = ctx.e, ctx.m
    nodes, edges = [], []

    def node(id_, label, kind, status, value_text="", group=None, **kw):
        nodes.append({"id": id_, "label": label, "kind": kind, "status": status, "value_text": value_text,
                      "group": group or kind, **kw})

    for n in e.graph.values():
        if n.kind == "input":
            if n.id in C.KINDS:
                o = m.option(n.id)
                vt, srcs = o.name, [q.source for q in getattr(o, "data", {}).values()]
            else:
                ps = [(k, q) for k, q in m.params.items() if INPUT_OF_PARAM.get(k, "aero_params") == n.id]
                vt = ", ".join(f"{k}={q.value:g}" if isinstance(q.value, (int, float)) else f"{k}=missing"
                               for k, q in ps[:5])
                srcs = [q.source for _, q in ps]
            st = "nev" if MISSING in srcs else ("ok" if ESTIMATE in srcs else "pass")
            node(n.id, n.label, "input", st, vt, "inputs")
        elif n.cost == HEAVY:
            st, vt = _hstatus(ctx, n.id)
            node(n.id, n.label, "heavy", st, vt[:200], "analyses", why=n.why)
        else:
            qs = [ctx.r[k] for k in n.produces if k in ctx.r]
            srcs = [q.source for q in qs]
            st = "nev" if not qs or MISSING in srcs else ("ok" if (ESTIMATE in srcs or REDUCED in srcs) else "pass")
            vt = "; ".join(f"{k} {q.value:.4g} {q.unit}" for k, q in zip(n.produces, qs)
                           if isinstance(q.value, (int, float)))[:200]
            node(n.id, n.label, "calc" if n.kind != "gate" else "evaluation", st, vt, "calcs")
        for rd in n.reads:
            edges.append({"source": rd, "target": n.id, "label": "reads"})
    for rq in m.requirements:
        x = ctx.reqs[rq.id]
        mid = f"metric:{rq.metric}"
        q = ctx.r.get(rq.metric)
        node(mid, rq.metric, "metric", "nev" if q is None or q.missing else ("pass" if q.source in STRONG else "ok"),
             "" if q is None or not isinstance(q.value, (int, float)) else f"{q.value:.4g} {q.unit} ({_src_label(q.source)})",
             "metrics")
        src = e.node_of_result(rq.metric) or ("fea_motor_tube" if rq.metric == "motor_tube_sf" else None)
        if src:
            edges.append({"source": src, "target": mid, "label": "produces"})
        node(rq.id, f"{rq.id} {rq.text}", "requirement", plain_status(x["status"]),
             f"{rq.op} {rq.threshold.value:g} {rq.threshold.unit} · {x['status']}", "requirements",
             verification=rq.verification)
        edges.append({"source": mid, "target": rq.id, "label": f"{rq.op} {rq.threshold.value:g}"})
        for gid in REQ_GATES.get(rq.id, []):
            edges.append({"source": rq.id, "target": gid, "label": "criterion"})
    for g in ctx.gates:
        node(g["id"], f"{g['id']} {g['name']}", "gate", plain_status(g["status"]), g["status"], "gates")
    for nid in G3_HEAVY:
        if nid in e.graph:
            edges.append({"source": nid, "target": "G3", "label": "criterion"})
    edges += [{"source": "G3", "target": "G4", "label": "G3 passed"}, {"source": "G5", "target": "G6", "label": "G5 passed"}]
    for t in ctx.tests:
        if not t["requirement_ids"] or t["id"] == "T13":
            continue
        node(t["id"], f"{t['id']} {t['title']}", "test", plain_status(t["status"]), t["acceptance"]["text"][:160],
             "tests")
        for rid in t["requirement_ids"]:
            edges.append({"source": t["id"], "target": rid, "label": "verifies"})
    for fid, f in ctx.fields.items():
        k = f.get("part")
        if k in ctx.keys and fid in e.graph:
            cid = f"part:{k}"
            if not any(n["id"] == cid for n in nodes):
                node(cid, k.split(" — ")[0], "component", "nev", k, "components", part=k)
            edges.append({"source": cid, "target": fid, "label": "analysed by"})
    ids = {n["id"] for n in nodes}
    edges = [x for x in edges if x["source"] in ids and x["target"] in ids]
    return {"nodes": nodes, "edges": edges,
            "note": "inputs -> level-1 calculations -> heavy analyses (engine graph); result metrics -> requirements "
                    "R1-R12 -> gates (approval.py criteria); tests -> the requirements they verify."}


# =============================================================================================== 5 process flows
_PROC_ORDER = ["Cut bought CF", "Hot-wire", "Laser", "Cut / drill G10", "FDM", "Cut and bend music wire", "Cut EVA",
               "Cut hook-and-loop", "Make harness"]


def _proc_group(p: str) -> str:
    return next((k for k in _PROC_ORDER if p.startswith(k)), p)


def process_flows_section(ctx: _Ctx) -> list[dict]:
    m, e = ctx.m, ctx.e
    flows = []
    # (a) analysis pipeline
    fl = ctx.fields
    meshes = "; ".join(f"{fid}: {(f.get('mesh') or {}).get('type')} "
                       f"{(f.get('mesh') or {}).get('elements') or (f.get('mesh') or {}).get('cells')} "
                       f"{'cells' if (f.get('mesh') or {}).get('cells') else 'elements'}" for fid, f in fl.items())
    solvers = "; ".join(f"{fid}: {(f.get('solver') or {}).get('name')} {(f.get('solver') or {}).get('seconds')} s"
                        for fid, f in fl.items())
    fails = "; ".join(f"{fid}: {(f.get('failure') or {}).get('status')}" for fid, f in fl.items())
    cnt = defaultdict(int)
    for x in ctx.reqs.values():
        cnt[plain_status(x["status"])] += 1
    steps = [
        {"id": "cad", "label": "CAD import", "status": "ok",
         "detail": f"tessellated Fusion export: {len(m.bodies)} bodies, {len(m.components)} BOM lines, "
                   f"{len(m.joints)} joints (B-rep STEP unresolved)"},
        {"id": "params", "label": "Geometry parameters", "status": "ok",
         "detail": ", ".join(f"{k} = {m.params[k].value:g} mm" for k in C.GEOMETRY_PARAMS)
                   + f"; {len(m.modified_bodies())} bodies modified from the imported CAD"},
        {"id": "mesh", "label": "Mesh (gmsh tetra / snappyHexMesh)", "status": "ok", "detail": meshes,
         "parts": sorted({f["part"] for f in fl.values() if f.get("part") in ctx.keys})},
        {"id": "solve", "label": "Solve (CalculiX / OpenFOAM / 6-DOF)",
         "status": worst([_hstatus(ctx, n)[0] if (ctx.heavy.get(n) or {}).get("status") != CURRENT else "ok"
                          for n in ctx.heavy]), "detail": solvers + "; flight: reduced-order 6-DOF (flightsim)"},
        {"id": "post", "label": "Post-process (fields, critical regions)", "status": "ok",
         "detail": f"{len(fl)} contour fields with worst location and critical area"},
        {"id": "failure", "label": "Failure parameters", "status": worst([plain_status((f.get('failure') or {}).get(
            "status")) for f in fl.values()]), "detail": fails},
        {"id": "requirements", "label": "Requirements R1-R12", "status": worst([plain_status(x["status"])
                                                                              for x in ctx.reqs.values()]),
         "detail": ", ".join(f"{v} {k}" for k, v in sorted(cnt.items()))},
        {"id": "gates", "label": "Gates G1-G6", "status": worst([plain_status(g["status"]) for g in ctx.gates]),
         "detail": ", ".join(f"{g['id']} {g['status']}" for g in ctx.gates)},
        {"id": "decision", "label": "Human decision", "status": "nev",
         "detail": "recorded by the reviewer; the software never approves flight"},
    ]
    flows.append({"id": "analysis", "title": "Analysis pipeline", "steps": steps,
                  "edges": [[a["id"], b["id"]] for a, b in zip(steps, steps[1:])]})
    # (b) design change
    diff = ctx.study.diff(ctx.rid, ctx.study.baseline) if ctx.rid != ctx.study.baseline else []
    inputs = sorted({k if k in C.KINDS else INPUT_OF_PARAM.get(k, "aero_params") for k, *_ in diff})
    affected = sorted({n for i in inputs if i in e.graph for n in e.downstream(i)})
    heavy_aff = [n for n in affected if n in e.heavy]
    steps = [
        {"id": "edit", "label": "Edit variant", "detail": "; ".join(f"{k}: {a} → {b}" for k, a, b in diff) or
         "baseline (no differences)", "status": "ok"},
        {"id": "recompute", "label": "Recompute graph (light nodes)", "status": "ok",
         "detail": f"inputs {', '.join(inputs) or '-'} affect {len(affected)} nodes: "
                   + ", ".join(n for n in affected if n not in e.heavy)},
        {"id": "stale", "label": "Heavy analyses marked stale",
         "status": worst(["watch" if (ctx.heavy.get(n) or {}).get("status") != CURRENT else "ok" for n in heavy_aff]),
         "detail": ", ".join(f"{n} ({(ctx.heavy.get(n) or {}).get('status')})" for n in heavy_aff)},
        {"id": "rerun", "label": "Re-run (FEA, thermal, CFD, assembly, flight)", "status": "ok",
         "detail": "; ".join(f"{n}: run {((ctx.heavy.get(n) or {}).get('record') or {}).get('time')}" for n in heavy_aff)},
        {"id": "website", "label": "Rebuild website", "status": "ok",
         "detail": "python -m opencae.web.build: only current records are shown"},
    ]
    flows.append({"id": "design_change", "title": "Design change propagation", "steps": steps,
                  "edges": [[a["id"], b["id"]] for a, b in zip(steps, steps[1:])]})
    # (c) build
    wf = {w["id"]: w for w in MF.fabrication_workflow()}
    groups = defaultdict(list)
    for p in ctx.plan():
        groups[_proc_group(p["process"])].append(p)
    steps = [{"id": "prep", "label": wf["manufacturing_preparation"]["stage"],
              "detail": "; ".join(wf["manufacturing_preparation"]["checklist"]), "status": "nev"}]
    edges = []
    for gname in sorted(groups, key=lambda g: _PROC_ORDER.index(g) if g in _PROC_ORDER else 99):
        rows = groups[gname]
        tmin = sum((p["time_total"].value or 0) for p in rows) * 60
        sid = "fab:" + re.sub(r"[^a-z0-9]+", "_", gname.lower()).strip("_")
        steps.append({"id": sid, "label": gname, "status": "nev", "parts": [p["component"] for p in rows],
                      "detail": f"{len(rows)} parts, ~{tmin:.0f} min (ESTIMATE); tools: "
                                + ", ".join(sorted({t for p in rows for t in p["tools"]}))[:300]})
        edges += [["prep", sid], [sid, "inspection"]]
    steps.append({"id": "inspection", "label": wf["inspection"]["stage"], "status": "nev",
                  "detail": "; ".join(wf["inspection"]["checklist"])})
    prev = "inspection"
    for s in ctx.steps():
        sid = f"asm:{s['step']}"
        steps.append({"id": sid, "label": f"Assembly {s['step']}: {s['title']}", "status": "nev",
                      "parts": s["components"] + s["fasteners"], "detail": "; ".join(s["checks"])[:300]})
        edges.append([prev, sid])
        prev = sid
    steps.append({"id": "ground_testing", "label": wf["ground_testing"]["stage"], "status": "nev",
                  "detail": "; ".join(wf["ground_testing"]["checklist"])})
    edges.append([prev, "ground_testing"])
    flows.append({"id": "build", "title": "Fabrication and assembly", "steps": steps, "edges": edges})
    # (d) verification by gate
    steps, edges = [], []
    order = ["G3", "G4", "G5", "G6"]
    by_gate = defaultdict(list)
    for t in ctx.tests:
        if t["id"] in ("T12", "T13"):
            gid = "G6"
        else:
            gs = {g for rid in t["requirement_ids"] for g in REQ_GATES.get(rid, [])}
            gid = next((g for g in ("G5", "G4", "G3") if g in gs), "G5")
        by_gate[gid].append(t)
    gst = {g["id"]: g for g in ctx.gates}
    for i, gid in enumerate(order):
        for t in by_gate.get(gid, []):
            steps.append({"id": t["id"], "label": f"{t['id']} {t['title']}", "status": plain_status(t["status"]),
                          "detail": t["acceptance"]["text"][:300],
                          "parts": [k for k in t["components"] if k in ctx.keys],
                          "gate_inferred": not t["requirement_ids"]})
            edges.append([t["id"], gid])
        steps.append({"id": gid, "label": f"{gid} {gst[gid]['name']}", "status": plain_status(gst[gid]["status"]),
                      "detail": gst[gid]["status"]})
        if i:
            edges.append([order[i - 1], gid])
    flows.append({"id": "verification", "title": "Verification tests by gate", "steps": steps, "edges": edges,
                  "note": "tests with no linked requirement (vibration, controls) are placed before G5 (inferred)"})
    return flows


# =============================================================================================== 6 fabrication
def fabrication_section(ctx: _Ctx) -> dict:
    rows = sorted(ctx.plan(), key=lambda p: (_PROC_ORDER.index(_proc_group(p["process"]))
                                             if _proc_group(p["process"]) in _PROC_ORDER else 99, p["component"]))
    steps = []
    for i, p in enumerate(rows, start=1):
        t = p["time_per_unit"]
        steps.append({"id": f"F{i:02d}", "title": p["component"].split(" — ")[0], "process": p["process"],
                      "material": p["material"], "parts": [p["component"]], "qty": p["qty"], "tools": p["tools"],
                      "time_min": None if t.value is None else round(p["time_total"].value * 60, 1),
                      "time_per_unit_min": None if t.value is None else round(t.value * 60, 1),
                      "time_source": t.source, "time_basis": t.note, "inspection": p["inspection"],
                      "post_processing": p["post_processing"], "risks": p["risks"],
                      "print": p.get("fdm"), "notes": p.get("notes"), "process_source": p["process_source"]})
    asm = [{"id": f"A{s['step']}", "title": s["title"], "subassembly": s["subassembly"], "parts": s["components"],
            "fasteners": [{"key": k, "count": n} for k, n in s["fastener_counts"].items()],
            "tools": s["tools"], "checks": s["checks"],
            "notes": [s["tolerances_note"]] + list(s.get("notes", [])) +
                     [f"fastener count differs from BOM: {r}" for r in s.get("fastener_reconciliation") or []],
            "class": s["tools_class"]} for s in ctx.steps()]
    return {"steps": steps, "assembly": asm, "workflow": MF.fabrication_workflow(),
            "total_time_min": round(sum(s["time_min"] or 0 for s in steps), 0),
            "note": "fabrication times are ESTIMATES with their basis; tools, print settings and checks are guidance"}


# =============================================================================================== 7 explode
def explode_section(ctx: _Ctx) -> dict:
    ev = AS.explode_vectors(ctx.m)
    act = ctx.m.active_bodies()
    return {"body_vectors": [[round(float(x), 2) for x in ev.get(b.index, np.zeros(3))] for b in act],
            "body_index": [b.index for b in act], "step_mm": AS.EXPLODE_STEP_MM,
            "note": "mm at factor 1, aligned with model.active_bodies() (the body order of aircraft_data); "
                    "directions are a display choice (guidance)"}


# =============================================================================================== 8 wiring
def wiring_section(ctx: _Ctx) -> dict:
    m, r = ctx.m, ctx.r
    keys = ctx.keys

    def find(prefix):
        return next((k for k in sorted(keys) if _first(k).startswith(prefix.lower()) and ctx.bodies_of(k)), None)

    gaps = _gap_harnesses(ctx)
    touch = defaultdict(set)
    for cpair in ctx.ac.get("contacts") or []:
        a, b = cpair["components"]
        touch[a].add(b)
        touch[b].add(a)
    for x in ctx.ac.get("expected_contacts") or []:
        a, b = x["components"]
        touch[a].add(b)
        touch[b].add(a)
    nodes = []

    def add(id_, label, kind, key, bodies=None):
        if key is None and bodies is None:
            return
        bs = bodies if bodies is not None else ctx.bodies_of(key)
        nodes.append({"id": id_, "label": label, "kind": kind, "part": key, "pos_mm": ctx.centroid(bs)})

    bat, esc, fc = m.option("battery").bom_component, m.option("esc").bom_component, find("FC CoreWing")
    add("battery", "LiPo battery", "battery", bat)
    add("xt60", "XT60 connector", "connector", find("Connector pair AMASS"))
    add("fc", "FC stack with PDB", "fc", fc)
    add("esc", "4-in-1 ESC", "esc", esc)
    add("cap", "Capacitor", "capacitor", find("Capacitor"))
    add("rx", "Receiver", "receiver", find("Receiver"))
    add("gps", "GPS", "gps", find("GPS Holybro"))
    add("aspd", "Airspeed sensor", "sensor", find("Airspeed sensor"))
    add("usb", "USB extender", "usb", find("USB extender"))
    add("balance", "Balance plug", "connector", find("Plug JST-XH"))
    srv = find("Servo KST")
    if srv:
        sb = ctx.bodies_of(srv)
        add("servo_L", "Servo L (S5)", "servo", srv, [b for b in sb if b.centroid[1] < 0])
        add("servo_R", "Servo R (S6)", "servo", srv, [b for b in sb if b.centroid[1] >= 0])
    # motors: the four instances by quadrant, matched to the motor harness that touches them (geometry)
    mot = m.option("motor").bom_component
    mb = ctx.bodies_of(mot)
    quads = defaultdict(list)
    for b in mb:
        quads[(b.centroid[1] >= 0, b.centroid[2] >= 0)].append(b)
    harn_m = sorted(k for k in keys if _first(k).startswith("harness motor"))
    for hk in harn_m:
        hc = np.array(ctx.centroid(ctx.bodies_of(hk)) or [0, 0, 0])
        qk = min(quads, key=lambda q: np.linalg.norm(np.array(ctx.centroid(quads[q])) - hc)) if quads else None
        mname = re.search(r"(M\d)", hk).group(1)
        if qk is not None:
            add(f"motor_{mname}", f"Motor {mname} ({hk.split('motor ', 1)[1]})", "motor", mot, quads[qk])
    nid = {n["id"] for n in nodes}
    links = []

    def link(src, tgt, kind, hk, cur=None, extra_parts=(), note=None):
        if src not in nid or tgt not in nid:
            return
        c = m.components.get(hk) if hk else None
        awg = re.search(r"(\d+)\s*AWG", c.name) if c else None
        seen = sorted(o.split(" — ")[0] for o in touch.get(hk, set()) if not AS.is_fastener(m.components[o])) if hk else []
        st = "watch" if hk in gaps else ("ok" if ctx.ac else "nev")
        d = {"source": src, "target": tgt, "kind": kind, "part": hk if hk in keys else None,
             "parts": [p for p in [hk, *extra_parts] if p in keys], "wire": awg.group(0) if awg else None,
             "status": st,
             "note": (note or "endpoints inferred from the BOM (harness name)")
                     + (f"; CAD terminations: {', '.join(seen)}" if seen else "")
                     + (f"; GAP: {gaps[hk]}" if hk in gaps else "")}
        if cur:
            q = r.get(cur[0])
            d.update(current_A=_f(q.value if q else None, 1), current_case=cur[1],
                     current_source=q.source if q else None,
                     current_note="wire / connector current rating not in the model - not checked")
        links.append(d)

    H = lambda p: find(p)  # noqa: E731
    link("battery", "xt60", "power", H("Harness battery pigtail"), ("i_battery_max", "full throttle"))
    link("xt60", "fc", "power", H("Harness XT60 to PDB"), ("i_battery_max", "full throttle"), [find("Connector pair AMASS")])
    link("fc", "esc", "power", H("Harness PDB to ESC"), ("i_battery_max", "full throttle"))
    link("esc", "cap", "power", H("Harness ESC capacitor"))
    bullets = find("Bullet connector")
    for hk in harn_m:
        mname = re.search(r"(M\d)", hk).group(1)
        link("esc", f"motor_{mname}", "phase", hk, ("i_motor_max_full", "full throttle, per motor"), [bullets])
    link("fc", "esc", "signal", H("Harness ESC signal"), None, [find("Plug JST-SH")])
    link("fc", "rx", "signal", H("Harness receiver"))
    link("fc", "gps", "signal", H("Harness GPS"), None, [find("Plug JST-GH 6P")])
    link("fc", "aspd", "signal", H("Harness airspeed"), None, [find("Plug JST-GH 4P")])
    link("fc", "usb", "signal", H("Harness USB extender"), None, [find("Plug JST-SH")])
    ext = find("Servo extension")
    link("fc", "servo_L", "signal", H("Harness servo L"), None, [ext])
    link("fc", "servo_R", "signal", H("Harness servo R"), None, [ext])
    link("battery", "balance", "signal", H("Harness battery balance"), None, [],
         "balance lead to the balance plug (charging / cell monitoring); inferred from the BOM")
    harness = sorted(k for k in keys if ctx.bodies_of(k) and (m.components[k].group == "wiring" or _first(k).startswith(
        ("plug ", "connector ", "servo extension", "bullet"))))
    checks = {"electrical_gaps": ctx.ac.get("electrical_gaps") or [],
              "rule": "each harness must touch two other parts (0.5 mm); each motor one motor harness; one servo "
                      "harness per servo (assembly._electrical_check)",
              "assembly_check_time": ctx.ac.get("time"), "cells_ok": _f(ctx.res("cells_ok")),
              "cells_text": r["cells_ok"].ref if "cells_ok" in r else None}
    svg = None
    p = Path(m.data_dir) / "sheet4_wiring.svg"
    if p.exists():
        svg = p.read_text(encoding="utf-8")
    return {"nodes": nodes, "links": links, "harness_parts": harness, "checks": checks, "wiring_svg": svg}


# =============================================================================================== 9 cost
def cost_section(ctx: _Ctx) -> dict:
    m, r = ctx.m, ctx.r
    by_sub = defaultdict(float)
    items = []
    for c in m.active_components():
        p = m.component_price(c)
        if p.value is None:
            continue
        v = p.value * m.effective_qty(c)
        by_sub[AS.subassembly_of(c)] += v
        items.append({"key": c.key, "name": c.name, "usd": round(v, 2), "source": p.source})
    items.sort(key=lambda x: -x["usd"])
    ship = m.params["shipping"]
    cat = [{"category": "parts", "usd": _f(r["cost_parts"].value, 2), "source": r["cost_parts"].source},
           {"category": "tax", "usd": _f(r["cost_tax"].value, 2), "source": r["cost_tax"].source,
            "note": f"{m.params['tax_rate'].value * 100:.2f} % of parts"},
           {"category": "contingency", "usd": _f(r["cost_contingency"].value, 2), "source": r["cost_contingency"].source,
            "note": f"{m.params['contingency'].value * 100:.0f} % of parts"},
           {"category": "shipping", "usd": _f(ship.value, 2), "source": ship.source, "note": ship.ref}]
    return {"total_usd": _f(r["cost_total"].value, 2), "total_source": r["cost_total"].source,
            "budget_usd": _f(m.params["budget"].value, 2), "reserve_usd": _f(r["reserve"].value, 2),
            "crash_reserve_usd": _f(m.params["crash_reserve"].value, 2),
            "by_subsystem": [{"subsystem": s, "name": dict(AS.SUBASSEMBLIES)[s], "usd": round(v, 2)}
                             for s, v in sorted(by_sub.items(), key=lambda t: -t[1])],
            "by_group": [{"group": k[5:], "usd": _f(q.value, 2)} for k, q in r.items()
                         if k.startswith("cost_") and k[5:] in ("airframe", "propulsion", "controls", "avionics",
                                                                "wiring", "hardware")],
            "by_category": cat, "top_items": items[:12],
            "sources": [f"{int(r['cost_estimated_lines'].value)} BOM lines priced as ESTIMATE",
                        r["cost_total"].ref + (f" ({r['cost_total'].note})" if r["cost_total"].note else ""),
                        f"reserve vs crash reserve: R3 {ctx.reqs['R3']['status']}"]}


# =============================================================================================== 10 flight
def _track(ctx: _Ctx, scenario: str, sim, hz: float = 25.0) -> dict:
    """One simulated flight as a pose track.

    Convention (identical to the platform's FlightPage.place / scene.pose_matrix): the simulation's body frame
    is the aircraft frame of the model (x aft, y right, z up in cruise attitude); ``quat`` = [w, x, y, z]
    rotates body into world (v_W = R v_B); world is x East, y North, z Up, ground at z = 0. A model-frame
    vertex v_B in mm (as emitted by build.aircraft_data) maps to the world in mm as

        v_W_mm = pos_mm + R (v_B_mm - cg_mm)          i.e.  [v_W_mm, 1] = M [v_B_mm, 1]

    ``m`` is that 4x4 matrix flattened row-major (16 numbers, translation in mm): scene.pose_matrix(pos,
    quat, cg_mm) with its m->mm scale folded out (rotation block unscaled, translation x 1000).
    ``translation_mm`` is M's translation column (where the model origin goes); ``pos_mm`` is the CG.
    """
    m = ctx.m
    s = sim
    t = np.asarray(s.t)
    dt = float(t[1] - t[0]) if len(t) > 1 else 1.0 / hz
    k = max(1, int(round(1.0 / hz / dt)))
    idx = np.arange(0, len(t), k)
    if idx[-1] != len(t) - 1:
        idx = np.append(idx, len(t) - 1)
    cg = np.asarray(ctx.r["cg"].value, float) * 1000.0
    from opencae.ui.platform import scene as SC
    mats, trans = [], []
    for i in idx:
        M = SC.pose_matrix(s.pos[i], s.quat[i], cg)
        M[:3, :] *= 1000.0  # m -> mm (rotation unscaled, translation in mm)
        mats.append([round(float(x), 4) for x in M.ravel()])
        trans.append(M[:3, 3])
    trans = np.asarray(trans)
    pos_mm = np.asarray(s.pos)[idx] * 1000.0
    ext = np.vstack([trans, pos_mm])
    ev = [{"t": round(float(a), 3), "text": str(b), "kind": "event"} for a, b in s.meta.get("events", [])]
    ev += [{"t": round(float(a), 3), "text": str(b), "kind": "warning"} for a, b in s.warnings]
    ev.sort(key=lambda x: x["t"])
    r2 = lambda a, nd=3: np.round(np.asarray(a, float), nd).tolist()  # noqa: E731
    return {
        "scenario": scenario, "t": r2(t[idx], 3), "pos_mm": r2(pos_mm, 1), "translation_mm": r2(trans, 1),
        "quat": r2(np.asarray(s.quat)[idx], 5), "m": mats, "cg_mm": r2(cg, 2),
        "rpm": r2(np.asarray(s.rpm)[idx], 0), "thrust_N": r2(np.asarray(s.thrust)[idx], 2),
        "throttle": r2(np.asarray(s.cmd)[idx], 3), "motor_current_A": r2(np.asarray(s.motor_current)[idx], 2),
        "battery_current_A": r2(np.asarray(s.current)[idx], 2), "soc": r2(np.asarray(s.soc)[idx], 4),
        "airspeed": r2(np.asarray(s.airspeed)[idx], 2), "altitude_m": r2(np.asarray(s.pos)[idx, 2], 3),
        "alpha_deg": r2(np.asarray(s.alpha)[idx], 2), "elevon_deg": r2(np.asarray(s.elevon)[idx], 2),
        "euler_deg": r2(np.asarray(s.euler)[idx], 2), "motor_temp_rise_K": r2(np.asarray(s.motor_temp_rise)[idx], 2),
        "events": ev, "outcome": _clean(s.meta.get("outcome")), "duration": round(float(t[-1]), 3),
        "sample_hz": round(1.0 / (k * dt), 2), "motors": list(s.meta.get("motors") or []),
        "motor_rating_A": m.option("motor").q("imax").value,
        "world": {"up": "z", "units": "mm", "ground_z_mm": 0.0,
                  "extent_mm": [round(float(v), 1) for v in (ext[:, 0].min(), ext[:, 0].max(), ext[:, 1].min(),
                                                            ext[:, 1].max(), ext[:, 2].min(), ext[:, 2].max())],
                  "extent_basis": "matrix translations and CG positions of the samples"},
        "convention": "v_W_mm = M v_B_mm (homogeneous, row-major M); M = [R | pos_mm - R cg_mm]; quat [w,x,y,z] "
                      "body->world; world x East, y North, z Up, ground z = 0; body = model aircraft frame "
                      "(x aft, y right, z up)",
        "validity": s.meta.get("validity"), "evidence_class": s.meta.get("evidence_class"),
    }


SIM_DT = 0.004  # s: the integration step the study's robustness runs use (heavy-analysis records use 0.002)


def _simulations(ctx: _Ctx, jobs: list[tuple[str, Optional[float]]]) -> list:
    """Run the scenarios on the study's model (serial; logged every 0.02 s like the default runs)."""
    from opencae.engineering import flightsim as FS
    return [FS.simulate(ctx.m, sc, duration=du, dt=SIM_DT, log_every=max(1, int(round(0.02 / SIM_DT))))
            for sc, du in jobs]


def flight_section(ctx: _Ctx) -> dict:
    hov, tra = _simulations(ctx, [("hover", 10.0), ("transition_forward", None)])
    return {"hover": _track(ctx, "hover", hov), "transition": _track(ctx, "transition_forward", tra),
            "robustness": ctx.snap.get("robustness") or [], "sim_dt_s": SIM_DT,
            "operating_limits": _rb_limits() if ctx.m.config.get("landing") == "feet_b" else [],
            "note": "reduced-order 6-DOF simulation on provisional coefficients: plans tests, is not flight "
                    "evidence and approves nothing"}


# =============================================================================================== 11 mesh profiles
def _mesh_summary(fid: str, f: dict) -> str:
    me, so = f.get("mesh") or {}, f.get("solver") or {}
    parts = []
    if me.get("cells"):
        parts.append(f"{me['cells']:,} finite-volume cells ({me.get('type')})")
    if me.get("elements"):
        parts.append(f"{me['elements']:,} {me.get('type')} elements on {me.get('nodes', 0):,} nodes")
    if me.get("size_mm"):
        parts.append(f"target size {me['size_mm']} mm")
    if me.get("surface_level"):
        parts.append(f"surface refinement levels {me['surface_level']}")
    if so.get("name"):
        parts.append(f"solved with {so['name']} in {so.get('seconds')} s"
                     + ("" if so.get("converged", True) else " (NOT converged)"))
    if me.get("note"):
        parts.append(me["note"])
    if so.get("checkMesh"):
        parts.append(str(so["checkMesh"]))
    return "; ".join(parts) + "."


def mesh_profiles_section(ctx: _Ctx) -> dict:
    out = {}
    for fid, f in ctx.fields.items():
        me = f.get("mesh") or {}
        out[fid] = {"id": fid, "title": f.get("title"), "part": f.get("part") if f.get("part") in ctx.keys else None,
                    "part_name": f.get("part"), "element_type": me.get("type"), "nodes": me.get("nodes"),
                    "elements": me.get("elements"), "cells": me.get("cells"),
                    "target_size_mm": me.get("size_mm"), "mesh": _clean(me), "solver": _clean(f.get("solver")),
                    "summary": _mesh_summary(fid, f)}
    return out


# =============================================================================================== entry point
def site_extra(study, rid: str, fields_summary: dict) -> dict:
    """All extra single-page data for variant ``rid`` (see module docstring for the conventions).

    ``fields_summary`` is the second return value of ``build.field_data(study)``. The study's selected
    variant is ``rid`` on return. Each top-level key is computed separately; a failing section returns
    {"error": ...} for its key.
    """
    t0 = time.perf_counter()
    ctx = _Ctx(study, rid, fields_summary)
    timing = {}
    out: dict = {}
    out["components"], timing["components"] = _section(components_section, ctx)
    out["remedies"], timing["remedies"] = _section(remedies_section, ctx, out["components"])
    out["gates"], timing["gates"] = _section(gates_section, ctx)
    out["knowledge_graph"], timing["knowledge_graph"] = _section(knowledge_graph_section, ctx)
    out["process_flows"], timing["process_flows"] = _section(process_flows_section, ctx)
    out["fabrication"], timing["fabrication"] = _section(fabrication_section, ctx)
    out["explode"], timing["explode"] = _section(explode_section, ctx)
    out["wiring"], timing["wiring"] = _section(wiring_section, ctx)
    out["cost"], timing["cost"] = _section(cost_section, ctx)
    out["flight"], timing["flight"] = _section(flight_section, ctx)
    out["mesh_profiles"], timing["mesh_profiles"] = _section(mesh_profiles_section, ctx)
    study.select(rid)
    out = _clean(out)
    out["meta"] = {"variant": study.variant(rid)["name"], "rid": rid, "seconds": round(time.perf_counter() - t0, 2),
                   "section_seconds": {k: round(v, 2) for k, v in timing.items()},
                   "status_vocabulary": {"pass": "met on accepted evidence", "ok": "predicted to meet, not proven",
                                         "watch": "thin margin (< 20 %) or open issue to review",
                                         "fail": "criterion missed", "nev": "not evaluated"}}
    return out
