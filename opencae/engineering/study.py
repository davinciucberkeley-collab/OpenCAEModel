"""Design study: several K09-L design variants measured with one set of monitoring parameters.

A variant is a saved revision of the engineering model (catalog selections + parameters, including the
parametric geometry parameters). Every variant is measured with the same requirements R1-R12, gates G1-G6,
key metrics and heavy analyses, so the variants are directly comparable. Heavy-analysis records stay
attached to the exact inputs they were computed for (engine history, matched by input hash): switching
variants never shows another variant's solver results as current.

The study is one JSON file (the engine state: revisions = variants, heavy-analysis history) and can be
exported as a self-contained HTML dashboard report (``write_dashboard``).
"""
from __future__ import annotations

import base64
import html
import json
import math
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from opencae.engineering import approval as AP
from opencae.engineering import catalog as C
from opencae.engineering.graph import CURRENT, Engine
from opencae.engineering.quantities import Q

DATA = Path(__file__).resolve().parent.parent / "examples" / "k09l"
SEED = DATA / "k09l_study.json"  # bundled study: revisions A, B (+ example variant) with their solver runs
USER_STUDY = Path.home() / ".opencae" / "k09l_study.json"

# ---------------------------------------------------------------------------------------------- what is monitored
# (key, label, group, scale, unit, decimals, better)
METRICS = [
    ("mass_total", "Take-off mass", "Mass & balance", 1000, "g", 1, "lower"),
    ("cg_x", "CG aft of wing leading edge", "Mass & balance", 1000, "mm", 1, None),
    ("static_margin", "Static margin", "Mass & balance", 100, "%", 2, "higher"),
    ("tipover_deg", "Tip-over angle standing", "Ground & transport", 1, "°", 1, "higher"),
    ("tipover_margin", "Margin over the gust lean", "Ground & transport", 1, "°", 1, "higher"),
    ("box_fit", "Transport-box fit (>= 1 fits)", "Ground & transport", 1, "", 3, "higher"),
    ("height_mm", "Aircraft height (props off)", "Ground & transport", 1, "mm", 0, "lower"),
    ("tw_full", "Thrust-to-weight, full battery", "Propulsion & electrical", 1, "", 2, None),
    ("tw_low", "Thrust-to-weight, end of battery", "Propulsion & electrical", 1, "", 2, "higher"),
    ("i_motor_max_full", "Motor current at full stick", "Propulsion & electrical", 1, "A", 1, "lower"),
    ("motor_current_margin", "Motor current margin", "Propulsion & electrical", 1, "", 2, "higher"),
    ("motor_power_margin", "Motor power margin", "Propulsion & electrical", 1, "", 2, "higher"),
    ("esc_margin", "ESC margin", "Propulsion & electrical", 1, "", 2, "higher"),
    ("battery_c_margin", "Battery C margin", "Propulsion & electrical", 1, "", 2, "higher"),
    ("throttle_headroom_low", "Control headroom, end of battery", "Propulsion & electrical", 1, "", 2, "higher"),
    ("hover_throttle", "Hover throttle", "Propulsion & electrical", 100, "%", 0, "lower"),
    ("motor_loss_max", "Motor heat at full stick", "Propulsion & electrical", 1, "W", 0, "lower"),
    ("endurance_hover", "Hover endurance", "Mission", 1 / 60, "min", 1, "higher"),
    ("v_cruise", "Cruise speed (min power)", "Mission", 1, "m/s", 1, None),
    ("mission_margin", "Mission energy margin", "Mission", 1, "", 2, "higher"),
    ("landing_n", "Landing load factor (1 m/s)", "Structure", 1, "g", 1, "lower"),
    ("foot_sf", "Foot outrigger safety factor", "Structure", 1, "", 1, "higher"),
    ("motor_tube_sf", "Motor tube safety factor (FEA)", "Structure", 1, "", 1, "higher"),
    ("foot_fea_sf", "Foot safety factor (FEA)", "Structure", 1, "", 1, "higher"),
    ("mount_t_max", "Motor mount hottest point (hover, 35 °C day)", "Thermal", 1, "°C", 1, "lower"),
    ("mount_t_margin", "Motor mount temperature margin", "Thermal", 1, "K", 1, "higher"),
    ("cfd_lift_ratio", "Cruise lift at trim / lift needed (CFD)", "Aerodynamics (CFD)", 100, "%", 0, "higher"),
    ("cfd_ld", "Cruise L/D (CFD, coarse)", "Aerodynamics (CFD)", 1, "", 1, "higher"),
    ("cost_total", "Total cost", "Cost", 1, "USD", 0, "lower"),
    ("reserve", "Budget reserve", "Cost", 1, "USD", 0, "higher"),
]
HEAVY = [("fea_motor_tube", "FEA motor tube"), ("fea_spar", "FEA wing spar"), ("fea_foot", "FEA outrigger foot"),
         ("thermal_mount", "Thermal motor mount"), ("cfd_cruise", "CFD cruise"), ("assembly_check", "Assembly check"),
         ("flight_hover", "Hover simulation"), ("flight_transition", "Transition simulation")]
FIELD_NODES = ("fea_motor_tube", "fea_spar", "fea_foot", "thermal_mount", "cfd_cruise")

# parameters a variant can change in the editor: (key, label, group)
EDITABLE = [
    ("foot_outrigger_mm", "Foot outrigger (feet_b)", "Geometry"),
    ("foot_shorten_mm", "Foot aft shortening (feet_b)", "Geometry"),
    ("motor_inboard_mm", "Motor sets inboard (inboard_b)", "Geometry"),
    ("throttle_max", "Motor output limit", "Flight controller & procedure"),
    ("transition_pitch_end_deg", "Transition pitch-over angle", "Flight controller & procedure"),
    ("transition_pitch_rate_deg", "Transition pitch rate", "Flight controller & procedure"),
    ("transition_airspeed", "Transition airspeed", "Flight controller & procedure"),
    ("payload", "Payload", "Mission & environment"),
    ("hover_min", "Mission hover time", "Mission & environment"),
    ("cruise_min", "Mission cruise time", "Mission & environment"),
    ("rho", "Air density", "Mission & environment"),
    ("gust_lean_deg", "Lean in the 8 m/s gust", "Mission & environment"),
    ("cd0", "Zero-lift drag C_D0", "Aerodynamic assumptions"),
    ("oswald", "Span efficiency e", "Aerodynamic assumptions"),
    ("np_frac", "Neutral point (fraction of chord)", "Aerodynamic assumptions"),
    ("cl_max", "Wing C_L,max", "Aerodynamic assumptions"),
    ("eta_prop_cruise", "Prop efficiency in cruise", "Aerodynamic assumptions"),
    ("shipping", "Shipping", "Cost"),
    ("budget", "Budget", "Cost"),
    ("contingency", "Contingency", "Cost"),
    ("tax_rate", "Tax rate", "Cost"),
]
PARAM_RANGES = {k: (v[1], v[2]) for k, v in C.GEOMETRY_PARAMS.items()}
PARAM_RANGES.update({"throttle_max": (0.3, 1.0), "transition_pitch_end_deg": (45.0, 89.0),
                     "transition_pitch_rate_deg": (5.0, 120.0), "transition_airspeed": (8.0, 25.0),
                     "payload": (0.0, 1.0), "rho": (0.9, 1.35), "cd0": (0.01, 0.1), "oswald": (0.5, 1.0),
                     "np_frac": (0.1, 0.4), "cl_max": (0.4, 1.4), "eta_prop_cruise": (0.2, 0.85),
                     "contingency": (0.0, 0.5), "tax_rate": (0.0, 0.3), "gust_lean_deg": (0.0, 60.0)})


def _f(x):
    return float(x) if isinstance(x, (int, float)) and not (isinstance(x, float) and math.isnan(x)) else None


def snapshot(engine: Engine) -> dict:
    """Everything the monitor shows for the engine's current configuration (plain JSON data)."""
    m, r = engine.model, engine.results
    met = {}
    for key, *_ in METRICS:
        if key == "cg_x":
            q = r.get("cg")
            met[key] = None if q is None else {"value": q.value[0], "unit": "m", "source": q.source, "ref": q.ref,
                                                "note": q.note}
            continue
        if key == "height_mm":
            q = r.get("envelope")
            met[key] = None if q is None else {"value": sorted(q.value)[0] * 1000, "unit": "mm", "source": q.source,
                                                "ref": "smallest side of the props-off envelope", "note": ""}
            continue
        q = r.get(key)
        met[key] = None if q is None else {"value": _f(q.value), "unit": q.unit, "source": q.source, "ref": q.ref,
                                            "note": q.note, "uncertainty": q.uncertainty}
    for key, nid, f in (("foot_fea_sf", "fea_foot", lambda fl: fl.get("safety_factor")),
                        ("mount_t_max", "thermal_mount", lambda fl: fl.get("value")),
                        ("mount_t_margin", "thermal_mount", lambda fl: fl.get("margin")),
                        ("cfd_lift_ratio", "cfd_cruise", lambda fl: fl.get("safety_factor")),
                        ("cfd_ld", "cfd_cruise", lambda fl: (fl.get("value") or 0) / max(
                            ((engine.heavy[nid].get("record") or {}).get("extra") or {}).get("cd") or 1e9, 1e-9))):
        h = engine.heavy_status(nid) if nid in engine.heavy else {"status": "not run"}
        fl = ((h.get("record") or {}).get("failure") or {}) if h["status"] == CURRENT else {}
        val = f(fl) if fl else None
        met[key] = None if val is None else {"value": float(val), "unit": "", "source": "simulation",
                                             "ref": fl.get("text", ""), "note": "", "uncertainty": None}
    reqs = [{k: v for k, v in x.items() if isinstance(v, (str, int, float, type(None)))}
            for x in AP.requirements_table(engine)]
    gates = [{"id": g["id"], "name": g["name"], "status": g["status"],
              "criteria": [{"text": c["text"], "status": c["status"], "why": c.get("why", "")} for c in g["criteria"]],
              "open": list(g["open"])} for g in AP.gates(engine)]
    heavy = {}
    for nid, label in HEAVY:
        h = engine.heavy_status(nid)
        rec = h.get("record") or {}
        heavy[nid] = {"label": label, "status": h["status"], "reason": h.get("reason", ""), "time": rec.get("time"),
                      "summary": _heavy_summary(nid, rec) if h["status"] == CURRENT else {}}
    defn = {"config": dict(m.config),
            "options": {k: C.option(m.catalog, k, m.config[k]).name for k in C.KINDS},
            "params": {k: {"value": q.value, "unit": q.unit, "source": q.source, "ref": q.ref}
                       for k, q in m.params.items()},
            "modified_bodies": len(m.modified_bodies()),
            "changed_parts": sorted({f"{c.name} — {c.pn}" for c in m.components.values() if c.revision == "B"})}
    counts = {s: sum(1 for x in reqs if x["status"] == s) for s in (AP.PASS, AP.COND, AP.FAIL, AP.NOT_EV)}
    return {"metrics": met, "requirements": reqs, "gates": gates, "heavy": heavy, "definition": defn,
            "counts": counts, "config_hash": m.config_hash(), "geometry_hash": m.geometry_hash(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S")}


def _heavy_summary(nid: str, rec: dict) -> dict:
    if nid in ("fea_foot", "thermal_mount", "cfd_cruise"):
        f = rec.get("failure") or {}
        return {"criterion": f.get("criterion"), "value": f.get("value"), "allowable": f.get("allowable"),
                "safety_factor": f.get("safety_factor"), "margin": f.get("margin"), "status": f.get("status"),
                "text": f.get("text"), "unit": f.get("unit"), "seconds": rec.get("seconds")}
    if nid.startswith("fea"):
        res = rec.get("result") or {}
        sf = res.get("safety_factor") or {}
        return {"safety_factor": _f(sf.get("value")), "governing_stress_MPa": _f(res.get("governing_axial_stress_MPa")),
                "max_displacement_mm": _f(res.get("max_displacement_mm")), "converged": res.get("converged"),
                "seconds": rec.get("seconds")}
    if nid == "assembly_check":
        sw = rec.get("rotor_sweep") or []
        return {"interferences": len(rec.get("interferences", [])), "harness_gaps": len(rec.get("electrical_gaps", [])),
                "expected_contacts": len(rec.get("expected_contacts", [])),
                "prop_clearance_mm": min((r["min_clearance_mm"] for r in sw), default=None),
                "prop_strikes": sum(1 for r in sw if r["severity"] == "error")}
    out = rec.get("outcome") or {}
    if nid == "flight_hover":
        return {"max_position_error_mm": _f((out.get("max_position_error") or 0) * 1000),
                "max_battery_current_A": _f(rec.get("max_current")), "warnings": len(rec.get("warnings") or [])}
    a0, amin = rec.get("altitude_start"), rec.get("min_altitude")
    return {"converged": out.get("transition_converged"),
            "height_loss_m": _f(a0 - amin) if a0 is not None and amin is not None else None,
            "max_battery_current_A": _f(rec.get("max_current")), "final_airspeed": _f(out.get("final_airspeed")),
            "warnings": len(rec.get("warnings") or [])}


class Study:
    """Variants of the K09-L on one engine. All variant switching goes through ``select``."""

    def __init__(self, engine: Optional[Engine] = None, path: Optional[Path] = None):
        self.engine = engine or Engine()
        self.path = Path(path) if path else None
        self.title = "K09-L design study"
        self.baseline: Optional[str] = None
        self.current: Optional[str] = None
        self._snap: dict[str, tuple] = {}
        if not self.engine.model.revisions:
            rev = self.engine.model.save_revision("Revision A (as imported)")
            rev.update(name="Revision A", description="The K09-L as imported from the Fusion model.", base=None)
        for rev in self.variants:
            rev.setdefault("name", rev["note"].split(":")[0].split("(")[0].strip() or rev["id"])
            rev.setdefault("description", rev["note"])
        self.baseline = self.baseline or self.variants[0]["id"]
        self.select(self.variants[-1]["id"])

    # ------------------------------------------------------------------ persistence
    @classmethod
    def open(cls, path: Path) -> "Study":
        e = Engine()
        e.load_state(Path(path))
        d = json.loads(Path(path).read_text())
        st = cls(e, path)
        st.title = d.get("study_title", st.title)
        if d.get("study_baseline") in st.ids():
            st.baseline = d["study_baseline"]
        if d.get("study_current") in st.ids():
            st.select(d["study_current"])
        return st

    @classmethod
    def open_default(cls) -> "Study":
        """The user's study (~/.opencae/k09l_study.json); created from the bundled seed the first time."""
        if not USER_STUDY.exists():
            USER_STUDY.parent.mkdir(parents=True, exist_ok=True)
            if SEED.exists():
                shutil.copyfile(SEED, USER_STUDY)
            else:
                st = cls()
                st.save(USER_STUDY)
                return st
        return cls.open(USER_STUDY)

    def save(self, path: Optional[Path] = None) -> Path:
        path = Path(path or self.path or USER_STUDY)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine.save_state(path)
        d = json.loads(path.read_text())
        d.update(study_title=self.title, study_baseline=self.baseline, study_current=self.current,
                 format="opencae-k09l-study/1")
        path.write_text(json.dumps(d, indent=1))
        self.path = path
        return path

    # ------------------------------------------------------------------ variants
    @property
    def fields_dir(self) -> Path:
        """Where contour fields of this study are kept: next to the study file."""
        base = Path(self.path).parent if self.path else USER_STUDY.parent
        return base / ("fields" if base == DATA else "k09l_fields")

    @property
    def variants(self) -> list[dict]:
        return self.engine.model.revisions

    def ids(self) -> list[str]:
        return [v["id"] for v in self.variants]

    def variant(self, rid: str) -> dict:
        return next(v for v in self.variants if v["id"] == rid)

    def select(self, rid: str) -> None:
        if self.current != rid or self._differs(rid):
            self.engine.restore_revision(rid)
        self.current = rid

    def _differs(self, rid: str) -> bool:
        v, m = self.variant(rid), self.engine.model
        return v["config"] != m.config or any(m.params[k].value != Q.from_dict(d).value for k, d in v["params"].items()
                                               if k in m.params)

    def add_variant(self, name: str, from_id: Optional[str] = None, description: str = "") -> dict:
        base = from_id or self.current
        self.select(base)
        rev = self.engine.model.save_revision(name)
        rev.update(name=name, description=description or f"Copy of {self.variant(base)['name']}", base=base)
        self.current = rev["id"]
        return rev

    def delete_variant(self, rid: str) -> None:
        if len(self.variants) <= 1:
            raise ValueError("a study keeps at least one variant")
        self.engine.model.revisions[:] = [v for v in self.variants if v["id"] != rid]
        self._snap.pop(rid, None)
        if self.baseline == rid:
            self.baseline = self.variants[0]["id"]
        if self.current == rid:
            self.select(self.variants[-1]["id"])

    def rename(self, rid: str, name: str, description: Optional[str] = None) -> None:
        v = self.variant(rid)
        v["name"], v["note"] = name, name
        if description is not None:
            v["description"] = description

    def edit(self, rid: str, options: Optional[dict] = None, params: Optional[dict] = None) -> list[dict]:
        """Change a variant: catalog selections {kind: option id} and parameters {name: value or
        (value, evidence class, reference)}. Values are range-checked; the evidence class of an edited
        parameter is kept unless a new class is given. Returns the change events."""
        self.select(rid)
        evs = []
        for kind, oid in (options or {}).items():
            ev = self.engine.set_option(kind, oid)
            if not ev.get("ok", True):
                raise ValueError(f"{kind}: {ev.get('reason')}")
            evs.append(ev)
        for name, val in (params or {}).items():
            src, ref = None, ""
            if isinstance(val, (tuple, list)):
                val, src, ref = (list(val) + [None, ""])[:3]
            lo, hi = PARAM_RANGES.get(name, (-math.inf, math.inf))
            if val is not None and not (lo <= float(val) <= hi):
                raise ValueError(f"{name} = {val} is outside {lo:g} .. {hi:g}")
            if self.engine.model.params[name].value != val or src:
                evs.append(self.engine.set_param(name, val, source=src or None, ref=ref or ""))
        self._store(rid)
        return evs

    def _store(self, rid: str) -> None:
        m, v = self.engine.model, self.variant(rid)
        v["config"] = dict(m.config)
        v["params"] = {k: q.to_dict() for k, q in m.params.items()}
        v["config_hash"], v["geometry_hash"] = m.config_hash(), m.geometry_hash()
        v["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        v["results"] = {k: q.value for k, q in self.engine.results.items() if isinstance(q.value, (int, float))}
        if v.get("robustness") and v.get("robustness_hash") != self._robust_hash():
            v.pop("robustness", None)  # computed for other inputs: never shown as current

    def _robust_hash(self) -> str:
        return self.engine.node_hash("flight_transition")

    def diff(self, rid: str, against: Optional[str] = None) -> list[tuple]:
        """(what, baseline value, variant value) for every option and parameter that differs."""
        a, b = self.variant(against or self.baseline), self.variant(rid)
        out = []
        cat = self.engine.model.catalog
        for k in C.KINDS:
            if a["config"].get(k) != b["config"].get(k):
                out.append((k, C.option(cat, k, a["config"][k]).name, C.option(cat, k, b["config"][k]).name))
        pd = self.engine.model.params_default
        for k in sorted(set(a["params"]) | set(b["params"])):
            va = Q.from_dict(a["params"][k]) if k in a["params"] else pd.get(k)
            vb = Q.from_dict(b["params"][k]) if k in b["params"] else pd.get(k)
            if va is None or vb is None:
                continue
            if va.value != vb.value:
                out.append((k, _fmtq(va), _fmtq(vb)))
        return out

    # ------------------------------------------------------------------ monitoring
    def snapshot(self, rid: str) -> dict:
        v = self.variant(rid)
        key = json.dumps([v["config"], {k: d.get("value") for k, d in v["params"].items()}], sort_keys=True,
                         default=str) + f"|{len(self.engine.history)}"
        hit = self._snap.get(rid)
        if hit and hit[0] == key:
            return hit[1]
        cur = self.current
        self.select(rid)
        snap = snapshot(self.engine)
        snap["robustness"] = v.get("robustness") if v.get("robustness_hash") == self._robust_hash() else None
        self._snap[rid] = (key, snap)
        if cur and cur != rid and cur in self.ids():
            self.select(cur)
        return snap

    def snapshots(self) -> dict[str, dict]:
        cur = self.current
        out = {rid: self.snapshot(rid) for rid in self.ids()}
        if cur in self.ids():
            self.select(cur)
        return out

    def cached(self, rid: str) -> Optional[dict]:
        hit = self._snap.get(rid)
        return hit[1] if hit else None

    def stale_analyses(self, rid: str) -> list[str]:
        return [nid for nid, h in self.snapshot(rid)["heavy"].items() if h["status"] != CURRENT]

    # ------------------------------------------------------------------ heavy jobs (call from one worker thread)
    def run_analyses(self, rid: str, workdir: Optional[Path] = None, only_missing: bool = True,
                     progress: Optional[Callable[[str, int, int], None]] = None,
                     cancelled: Callable[[], bool] = lambda: False) -> dict:
        """Run the five heavy analyses of a variant (those not current, unless only_missing=False). Solver
        failures are recorded by the services and reported, never hidden."""
        from opencae.engineering import services as S
        self.select(rid)
        wd = Path(workdir or (Path.home() / ".opencae" / "k09l_runs")) / f"{rid}_{time.strftime('%Y%m%d_%H%M%S')}"
        jobs = [("fea_motor_tube", lambda: S.run_fea(self.engine, S.MOTOR_TUBE, workdir=wd / "fea_motor_tube")),
                ("fea_spar", lambda: S.run_fea(self.engine, S.SPAR_TUBE, workdir=wd / "fea_spar")),
                ("fea_foot", lambda: S.run_field(self.engine, "fea_foot", wd / "fea_foot", self.fields_dir)),
                ("thermal_mount", lambda: S.run_field(self.engine, "thermal_mount", wd / "thermal_mount",
                                                      self.fields_dir)),
                ("cfd_cruise", lambda: S.run_field(self.engine, "cfd_cruise", wd / "cfd_cruise", self.fields_dir)),
                ("assembly_check", lambda: S.run_assembly_check(self.engine)),
                ("flight_hover", lambda: S.run_flight(self.engine, "hover")),
                ("flight_transition", lambda: S.run_flight(self.engine, "transition_forward"))]
        todo = [(n, f) for n, f in jobs if not only_missing or self.engine.heavy_status(n)["status"] != CURRENT]
        done, errors = [], {}
        for i, (nid, fn) in enumerate(todo):
            if cancelled():
                break
            if progress:
                progress(nid, i, len(todo))
            try:
                fn()
                if nid in ("fea_motor_tube", "fea_spar"):
                    S.attach_fea_field(self.engine, nid, self.fields_dir)  # contours for the website
                done.append(nid)
            except Exception as exc:  # recorded as FAILED by the service; keep going with the others
                errors[nid] = f"{type(exc).__name__}: {exc}"
        if progress:
            progress("done", len(todo), len(todo))
        self._store(rid)
        self._snap.pop(rid, None)
        return {"done": done, "errors": errors, "workdir": str(wd)}

    def run_robustness(self, rid: str, progress: Optional[Callable[[str, int, int], None]] = None,
                       cancelled: Callable[[], bool] = lambda: False) -> list[dict]:
        """The forward transition under the off-nominal cases of revision B's review, for this variant
        (reduced-order evidence; serial, on a separate engine)."""
        import numpy as np

        from opencae.engineering import flightsim as FS
        from opencae.engineering import revision_b as RB
        v = self.variant(rid)
        e = Engine()
        e.model.config = dict(v["config"])
        e.model.params.update({k: Q.from_dict(d) for k, d in v["params"].items()})
        e.recompute()
        imax = e.model.option("motor").q("imax").value
        rows = []
        cases = list(RB.ROBUSTNESS_CASES.items())
        for i, (name, kw) in enumerate(cases):
            if cancelled():
                break
            if progress:
                progress(name, i, len(cases))
            s = FS.simulate(e.model, "transition_forward", dt=0.004, **kw)
            o = s.meta.get("outcome", {})
            im = np.asarray(s.motor_current)
            dt = np.diff(s.t, prepend=s.t[0])
            zmin = float(s.pos[:, 2].min())
            rows.append({"case": name, "converged": bool(o.get("transition_converged")),
                         "altitude_loss": round(float(s.pos[0, 2]) - zmin, 2), "ground_contact": bool(zmin < 0.5),
                         "time_above_motor_rating_s": round(float(((im > imax).any(1) * dt).sum()), 2),
                         "saturation_warnings": sum("saturated" in w for _, w in s.warnings)})
        if progress:
            progress("done", len(cases), len(cases))
        if len(rows) == len(cases):
            self.select(rid)
            v["robustness"], v["robustness_hash"] = rows, self._robust_hash()
            self._snap.pop(rid, None)
        return rows


def _fmtq(q: Q) -> str:
    if q.value is None:
        return "missing"
    v = q.value
    return (f"{v:.4g}" if isinstance(v, float) else str(v)) + (f" {q.unit}" if q.unit and q.unit != "-" else "")


# =============================================================================================== HTML dashboard
_ST = {AP.PASS: ("pass", "Pass", "✓"), AP.FAIL: ("fail", "Fail", "✕"), AP.COND: ("cond", "Conditional", "◐"),
       AP.NOT_EV: ("nev", "Not evaluated", "○")}
_HS = {"current": ("pass", "current"), "stale": ("cond", "stale"), "not run": ("nev", "not run"),
       "failed": ("fail", "failed")}
_EV = {"user_input": "input", "supplier_spec": "supplier", "analytical": "analytical", "simulation": "simulation",
       "reduced_order": "reduced-order", "test_measurement": "measured", "provisional_estimate": "estimate",
       "missing": "missing"}


def _e(x) -> str:
    return html.escape(str(x))


def _st(s, label=True):
    c, t, i = _ST.get(s, ("nev", s, "○"))
    return f'<span class="st st-{c}"><i>{i}</i>{t if label else ""}</span>'


def _fmt(snap, key, scale, unit, nd):
    q = snap["metrics"].get(key)
    if not q or q.get("value") is None:
        return "—", None
    v = q["value"] * scale
    return f"{v:,.{nd}f}{(' ' + unit) if unit else ''}", v


def _render_variant(study: Study, rid: str, path: Path) -> bool:
    """Isometric render of a variant, modified bodies highlighted (offscreen VTK). False if unavailable."""
    try:
        import numpy as np
        import pyvista as pv

        from opencae.ui.platform import scene as SC
        study.select(rid)
        m = study.engine.model
        pd = SC.aircraft_polydata(m)
        rgb = np.asarray(pd.cell_data["rgb"]).copy()
        mod = m.modified_bodies()
        if mod:
            rgb[np.isin(np.asarray(pd.cell_data["body"]), list(mod))] = [217, 101, 28]
        pd.cell_data["rgb"] = rgb
        p = pv.Plotter(off_screen=True, window_size=(900, 620))
        p.set_background("white")
        p.add_mesh(pd, scalars="rgb", rgb=True)
        c = pd.center
        p.camera_position = [(c[0] - 900, c[1] - 1100, c[2] + 700), c, (0, 0, 1)]
        p.camera.zoom(1.25)
        p.screenshot(str(path))
        p.close()
        return True
    except Exception:
        return False


def write_dashboard(study: Study, path: Path, renders: bool = True,
                    progress: Optional[Callable[[str, int, int], None]] = None) -> Path:
    """Self-contained HTML dashboard of every variant against the same monitoring parameters."""
    path = Path(path)
    ids = study.ids()
    cur = study.current
    snaps = {}
    for i, rid in enumerate(ids):
        if progress:
            progress(f"measuring {study.variant(rid)['name']}", i, len(ids) * (2 if renders else 1))
        snaps[rid] = study.snapshot(rid)
    imgs = {}
    if renders:
        tmp = Path(tempfile.mkdtemp(prefix="k09l_dash_"))
        for i, rid in enumerate(ids):
            if progress:
                progress(f"rendering {study.variant(rid)['name']}", len(ids) + i, len(ids) * 2)
            f = tmp / f"{rid}.png"
            if _render_variant(study, rid, f):
                try:
                    from PIL import Image
                    im = Image.open(f).convert("RGB")
                    f2 = tmp / f"{rid}.jpg"
                    im.save(f2, quality=80, optimize=True)
                    imgs[rid] = "data:image/jpeg;base64," + base64.b64encode(f2.read_bytes()).decode()
                except Exception:
                    imgs[rid] = "data:image/png;base64," + base64.b64encode(f.read_bytes()).decode()
        shutil.rmtree(tmp, ignore_errors=True)
    if cur in ids:
        study.select(cur)
    names = {rid: study.variant(rid)["name"] for rid in ids}
    base = study.baseline if study.baseline in ids else ids[0]
    reqdefs = {r.id: r for r in study.engine.model.requirements}
    metric_req = {r.metric: r for r in study.engine.model.requirements}

    # ---- overview cards
    cards = []
    for rid in ids:
        s = snaps[rid]
        c = s["counts"]
        nheavy = sum(1 for h in s["heavy"].values() if h["status"] == CURRENT)
        kp = []
        for key, lab, sc, un, nd in (("mass_total", "Mass", 1000, "g", 0), ("tipover_deg", "Tip-over", 1, "°", 1),
                                     ("box_fit", "Box fit", 1, "", 3), ("motor_current_margin", "Current margin", 1, "", 2),
                                     ("static_margin", "Static margin", 100, "%", 1), ("reserve", "Reserve", 1, "USD", 0)):
            t, _ = _fmt(s, key, sc, un, nd)
            kp.append(f"<div><span>{lab}</span><b>{t}</b></div>")
        verdict = "fail" if c[AP.FAIL] else ("cond" if c[AP.COND] or c[AP.NOT_EV] else "pass")
        img = f'<img src="{imgs[rid]}" alt="{_e(names[rid])} render">' if rid in imgs else ""
        cards.append(f"""<article class="card v-{verdict}" data-rid="{rid}">
<header><b>{_e(names[rid])}</b>{'<span class="basetag">baseline</span>' if rid == base else ''}</header>
{img}
<div class="counts">{''.join(f'<span class="cnt st-{_ST[k][0]}" title="{_ST[k][1]}">{_ST[k][2]} {c[k]}</span>' for k in (AP.PASS, AP.COND, AP.FAIL, AP.NOT_EV))}
<span class="cnt hv" title="heavy analyses current">⚙ {nheavy}/5</span></div>
<div class="kp">{''.join(kp)}</div>
<p class="desc">{_e(study.variant(rid).get('description', ''))}</p></article>""")

    # ---- requirement heatmap
    head = "".join(f"<th>{_e(names[r])}</th>" for r in ids)
    rows = []
    for rq in study.engine.model.requirements:
        cells = []
        for rid in ids:
            x = next(t for t in snaps[rid]["requirements"] if t["id"] == rq.id)
            p = x.get("predicted")
            pv_ = "—" if p is None else (f"{p:.3g}" if isinstance(p, float) else str(p))
            cells.append(f'<td class="hm hm-{_ST.get(x["status"], ("nev",))[0]}" title="{_e(x.get("why", ""))}">'
                         f'{_st(x["status"], False)}<span class="num">{pv_}</span></td>')
        rows.append(f"<tr><th scope='row'><span class='mono'>{rq.id}</span> {_e(rq.text)}<div class='sub'>"
                    f"{rq.op} {rq.threshold.value:g} · {rq.verification}</div></th>{''.join(cells)}</tr>")
    req_table = f"<table class='heat'><thead><tr><th>Requirement</th>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"

    # ---- metric small multiples (one scale per metric, threshold marked when a requirement uses it)
    groups: dict[str, list] = {}
    for key, label, group, sc, unit, nd, better in METRICS:
        vals = [(rid, *_fmt(snaps[rid], key, sc, unit, nd)) for rid in ids]
        nums = [v for _, _, v in vals if v is not None]
        rq = metric_req.get(key)
        thr = rq.threshold.value * sc if rq and rq.threshold.value is not None else None
        hi = max(nums + ([thr] if thr is not None else []) + [0]) or 1.0
        lo = min(nums + ([thr] if thr is not None else []) + [0])
        span = (hi - lo) or 1.0
        bars = []
        for rid, txt, v in vals:
            src = (snaps[rid]["metrics"].get(key) or {}).get("source", "missing")
            if v is None:
                bars.append(f"<div class='mrow'><span class='vn'>{_e(names[rid])}</span><div class='track'></div>"
                            f"<span class='vv muted'>—</span></div>")
                continue
            ok = None
            if rq is not None and thr is not None:
                ok = (v >= thr) if rq.op == ">=" else (v <= thr)
            w = (v - lo) / span * 100
            z = (0 - lo) / span * 100
            left, width = (min(z, w), abs(w - z))
            cls = "base" if rid == base else ("bad" if ok is False else "var")
            bars.append(f"<div class='mrow'><span class='vn'>{_e(names[rid])}</span><div class='track'>"
                        f"<span class='b {cls}' style='left:{left:.1f}%;width:{max(width, 0.8):.1f}%'></span>"
                        + (f"<span class='thr' style='left:{(thr - lo) / span * 100:.1f}%'></span>" if thr is not None else "")
                        + f"</div><span class='vv'>{txt} <em class='ev ev-{_EV.get(src, 'missing')}'>{_EV.get(src, src)}</em></span></div>")
        note = f"<span class='thrlab'>limit {rq.op} {thr:g} ({rq.id})</span>" if thr is not None else ""
        groups.setdefault(group, []).append(f"<div class='metric'><h4>{_e(label)}{note}</h4>{''.join(bars)}</div>")
    metrics_html = "".join(f"<h3>{_e(g)}</h3><div class='mgrid'>{''.join(v)}</div>" for g, v in groups.items())

    # ---- gates and analyses
    grow = []
    for i, g in enumerate(snaps[ids[0]]["gates"]):
        cells = "".join(f"<td class='hm hm-{_ST.get(snaps[r]['gates'][i]['status'], ('nev',))[0]}'>"
                        f"{_st(snaps[r]['gates'][i]['status'])}</td>" for r in ids)
        grow.append(f"<tr><th scope='row'><span class='mono'>{g['id']}</span> {_e(g['name'])}</th>{cells}</tr>")
    gates_table = f"<table class='heat'><thead><tr><th>Gate</th>{head}</tr></thead><tbody>{''.join(grow)}</tbody></table>"
    hrow = []
    for nid, label in HEAVY:
        cells = []
        for rid in ids:
            h = snaps[rid]["heavy"][nid]
            c, t = _HS.get(h["status"], ("nev", h["status"]))
            sm = h["summary"]
            if nid in ("fea_foot", "thermal_mount", "cfd_cruise") and sm:
                det = _e(sm.get("text") or "")[:110]
            elif nid.startswith("fea") and sm and sm.get("governing_stress_MPa") is not None:
                det = f"SF {sm['safety_factor']:.1f} · {sm['governing_stress_MPa']:.1f} MPa"
            elif nid == "assembly_check" and sm:
                det = f"{sm['interferences']} interference(s) · {sm['harness_gaps']} harness gap(s)"
            elif nid == "flight_hover" and sm:
                det = f"position error {sm['max_position_error_mm']:.1f} mm"
            elif nid == "flight_transition" and sm:
                det = (f"height loss {sm['height_loss_m']:.2f} m" if sm.get("height_loss_m") is not None else "") + \
                      ("" if sm.get("converged") else " · not converged")
            else:
                det = _e(h.get("reason", ""))[:90]
            cells.append(f"<td><span class='st st-{c}'>{t}</span><div class='sub'>{det}</div></td>")
        hrow.append(f"<tr><th scope='row'>{label}</th>{''.join(cells)}</tr>")
    heavy_table = f"<table class='heat'><thead><tr><th>Analysis</th>{head}</tr></thead><tbody>{''.join(hrow)}</tbody></table>"

    # ---- robustness (variants that ran it)
    rob_ids = [r for r in ids if snaps[r].get("robustness")]
    if rob_ids:
        cases = [x["case"] for x in snaps[rob_ids[0]]["robustness"]]
        rr = []
        for c in cases:
            cells = []
            for rid in rob_ids:
                x = next((y for y in snaps[rid]["robustness"] if y["case"] == c), None)
                if x is None:
                    cells.append("<td>—</td>")
                    continue
                bad = x["ground_contact"] or x["altitude_loss"] > AP.TRANSITION_MAX_ALT_LOSS
                lab = "ground contact" if x["ground_contact"] else f"{x['altitude_loss']:.1f} m"
                w = min(x["altitude_loss"] / 30.25, 1) * 100
                cells.append(f"<td><div class='bar {'bad' if bad else 'ok'}'><span style='width:{max(w, 1.5):.0f}%'></span>"
                             f"<em>{lab}</em></div></td>")
            rr.append(f"<tr><th scope='row'>{_e(c)}</th>{''.join(cells)}</tr>")
        rob_html = (f"<div class='tw'><table class='heat'><thead><tr><th>Case (height lost, 30 m start)</th>"
                    f"{''.join(f'<th>{_e(names[r])}</th>' for r in rob_ids)}</tr></thead><tbody>{''.join(rr)}</tbody></table></div>")
    else:
        rob_html = "<p class='muted'>No variant has a current robustness run (Design Monitor: Run robustness).</p>"

    # ---- per-variant detail
    details = []
    for rid in ids:
        s = snaps[rid]
        diff = study.diff(rid, base) if rid != base else []
        drows = "".join(f"<tr><td class='mono'>{_e(k)}</td><td>{_e(a)}</td><td><b>{_e(b)}</b></td></tr>" for k, a, b in diff) \
            or "<tr><td colspan='3' class='muted'>baseline (no differences)</td></tr>"
        mrows = []
        for key, label, group, sc, unit, nd, better in METRICS:
            t, _ = _fmt(s, key, sc, unit, nd)
            q = s["metrics"].get(key) or {}
            mrows.append(f"<tr><td>{_e(label)}</td><td class='num'>{t}</td><td><em class='ev ev-{_EV.get(q.get('source'), 'missing')}'>"
                         f"{_EV.get(q.get('source'), '—')}</em></td><td class='sub'>{_e(q.get('ref', ''))}</td></tr>")
        greq = "".join(f"<tr><td class='mono'>{x['id']}</td><td>{_st(x['status'])}</td><td class='sub'>{_e(x.get('why', ''))}"
                       f"{(' — next: ' + _e(x['action'])) if x.get('action') else ''}</td></tr>" for x in s["requirements"])
        gat = "".join(f"<details><summary>{_st(g['status'], False)} <span class='mono'>{g['id']}</span> {_e(g['name'])}</summary><ul>"
                      + "".join(f"<li>{_st(c['status'], False)} {_e(c['text'])} <span class='sub'>{_e(c.get('why', ''))[:200]}</span></li>"
                                for c in g["criteria"]) + "</ul></details>" for g in s["gates"])
        parts = "".join(f"<li>{_e(p)}</li>" for p in s["definition"]["changed_parts"]) or "<li class='muted'>none</li>"
        opts = "".join(f"<tr><td class='mono'>{k}</td><td>{_e(v)}</td></tr>" for k, v in s["definition"]["options"].items())
        details.append(f"""<section class="detail" id="d-{rid}" data-rid="{rid}" {'hidden' if rid != ids[-1] else ''}>
<h3>{_e(names[rid])}</h3><p class="desc">{_e(study.variant(rid).get('description', ''))}</p>
<div class="two"><div><h4>Differences from {_e(names[base])}</h4><div class="tw"><table><thead><tr><th>Item</th><th>{_e(names[base])}</th><th>{_e(names[rid])}</th></tr></thead><tbody>{drows}</tbody></table></div>
<h4>Configuration</h4><div class="tw"><table><tbody>{opts}</tbody></table></div><h4>Parts changed from the imported CAD</h4><ul class="parts">{parts}</ul></div>
<div><h4>Requirements</h4><div class="tw"><table><tbody>{greq}</tbody></table></div><h4>Gates</h4>{gat}</div></div>
<h4>All monitored quantities</h4><div class="tw"><table><thead><tr><th>Quantity</th><th>Value</th><th>Evidence</th><th>Basis</th></tr></thead><tbody>{''.join(mrows)}</tbody></table></div>
</section>""")
    tabs = "".join(f"<button type='button' class='tab' data-rid='{rid}' aria-pressed='{str(rid == ids[-1]).lower()}'>{_e(names[rid])}</button>" for rid in ids)

    gen = time.strftime("%Y-%m-%d %H:%M")
    doc = _DASH.replace("{{TITLE}}", _e(study.title)).replace("{{GEN}}", gen).replace("{{N}}", str(len(ids))) \
        .replace("{{BASE}}", _e(names[base])).replace("{{CARDS}}", "".join(cards)).replace("{{REQ}}", req_table) \
        .replace("{{METRICS}}", metrics_html).replace("{{GATES}}", gates_table).replace("{{HEAVY}}", heavy_table) \
        .replace("{{ROB}}", rob_html).replace("{{TABS}}", tabs).replace("{{DETAILS}}", "".join(details)) \
        .replace("{{JSON}}", json.dumps({"variants": {rid: {"name": names[rid], "definition": snaps[rid]["definition"]["config"]}
                                                     for rid in ids}}).replace("</", "<\\/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    if progress:
        progress("done", 1, 1)
    return path


_DASH = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{TITLE}}</title>
<style>
/* Dashboard: overview cards, then requirement / gate / analysis matrices (variants as columns), metric small multiples, per-variant detail tabs. */
:root{--bg:#f2f4f6;--panel:#fff;--ink:#16212b;--ink2:#4b5966;--ink3:#7a8792;--rule:#d6dde2;--rule2:#e8edf0;--accent:#d9651c;--base:#5b6b78;--var:#2d6aa3;
--pass:#2f7d4a;--passbg:#e3f2e7;--cond:#946200;--condbg:#fbf0d4;--fail:#b3302b;--failbg:#f9e1df;--nev:#5d6a75;--nevbg:#e9edf0;
--f:"Segoe UI",system-ui,-apple-system,sans-serif;--fm:Consolas,"Cascadia Mono",ui-monospace,Menlo,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0f151a;--panel:#161e25;--ink:#e3e9ee;--ink2:#a9b6c1;--ink3:#7e8b96;--rule:#2c3943;--rule2:#222d36;--accent:#f08a43;--base:#8d9aa6;--var:#6fa6dc;
--pass:#6cc48a;--passbg:#173123;--cond:#e6b443;--condbg:#33280e;--fail:#ef7d76;--failbg:#3a1a19;--nev:#9aa7b2;--nevbg:#232e37;color-scheme:dark}}
:root[data-theme=dark]{--bg:#0f151a;--panel:#161e25;--ink:#e3e9ee;--ink2:#a9b6c1;--ink3:#7e8b96;--rule:#2c3943;--rule2:#222d36;--accent:#f08a43;--base:#8d9aa6;--var:#6fa6dc;
--pass:#6cc48a;--passbg:#173123;--cond:#e6b443;--condbg:#33280e;--fail:#ef7d76;--failbg:#3a1a19;--nev:#9aa7b2;--nevbg:#232e37;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--f);padding:24px max(16px,env(safe-area-inset-right,0px)) 48px max(16px,env(safe-area-inset-left,0px))}
.wrap{max-width:1320px;margin:0 auto;display:grid;gap:22px}.wrap>*{min-width:0}
h1{margin:0;font-size:28px;letter-spacing:-.01em}h2{margin:0 0 4px;font-size:19px}h3{margin:14px 0 8px;font-size:15px;color:var(--ink2);text-transform:uppercase;letter-spacing:.06em}
h4{margin:10px 0 6px;font-size:13px;color:var(--ink2)}p{margin:0;max-width:80ch}
.top{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:end;justify-content:space-between;border-bottom:2px solid var(--ink);padding-bottom:12px}
.meta{font:12px var(--fm);color:var(--ink3);display:flex;gap:16px;flex-wrap:wrap}
.panel{background:var(--panel);border:1px solid var(--rule);border-radius:6px;padding:18px;display:grid;gap:10px;min-width:0}
.panel>*{min-width:0}.muted,.sub{color:var(--ink3)}.sub{font-size:12px}.mono,.num{font-family:var(--fm);font-variant-numeric:tabular-nums}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,270px),1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--rule);border-top:4px solid var(--nev);border-radius:6px;padding:12px;display:grid;gap:8px;align-content:start;cursor:pointer}
.card.v-pass{border-top-color:var(--pass)}.card.v-cond{border-top-color:var(--cond)}.card.v-fail{border-top-color:var(--fail)}
.card:focus-visible,.tab:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.card header{display:flex;justify-content:space-between;gap:8px;align-items:center;font-size:15px}
.basetag{font:11px var(--fm);border:1px solid var(--rule);padding:0 6px;border-radius:9px;color:var(--ink3)}
.card img{width:100%;border-radius:4px;border:1px solid var(--rule2);background:#fff}
.counts{display:flex;gap:4px;flex-wrap:wrap}.cnt{font:600 12px var(--fm);padding:1px 7px;border-radius:9px}.cnt.hv{background:var(--nevbg);color:var(--ink2)}
.kp{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;background:var(--rule2);border:1px solid var(--rule2)}
.kp div{background:var(--panel);padding:5px 7px;display:grid}.kp span{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--ink3)}.kp b{font:600 13px var(--fm)}
.desc{font-size:12.5px;color:var(--ink2)}
.st{display:inline-flex;gap:4px;align-items:center;font-weight:600;font-size:12px;padding:0 7px 0 5px;border-radius:9px;white-space:nowrap}.st i{font-style:normal}
.st-pass{color:var(--pass);background:var(--passbg)}.st-cond{color:var(--cond);background:var(--condbg)}.st-fail{color:var(--fail);background:var(--failbg)}.st-nev{color:var(--nev);background:var(--nevbg)}
.tw{overflow-x:auto;border:1px solid var(--rule);border-radius:4px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--rule2);vertical-align:top}
thead th{font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--ink3);background:var(--bg)}
.heat th[scope=row]{font-weight:500;min-width:260px}.heat td{min-width:130px}
.hm .num{display:block;font-size:12px;color:var(--ink2);margin-top:2px}
.hm-fail{background:var(--failbg)}.hm-pass{background:color-mix(in srgb,var(--passbg) 55%,transparent)}
.mgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,400px),1fr));gap:12px}
.metric{border:1px solid var(--rule2);border-radius:4px;padding:8px 10px;min-width:0}
.metric h4{margin:0 0 6px;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}.thrlab{font:11px var(--fm);color:var(--fail)}
.mrow{display:grid;grid-template-columns:minmax(70px,120px) minmax(60px,1fr) auto;gap:8px;align-items:center;font-size:12.5px;padding:2px 0}
.vn{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.vv{font:12px var(--fm);white-space:nowrap}
.track{position:relative;height:12px;background:var(--rule2);border-radius:2px}
.track .b{position:absolute;top:1px;bottom:1px;border-radius:2px}.b.base{background:var(--base)}.b.var{background:var(--var)}.b.bad{background:var(--fail)}
.thr{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--fail)}
.ev{font:10.5px var(--fm);font-style:normal;padding:0 4px;border:1px solid var(--rule);border-radius:2px;color:var(--ink3)}
.ev-estimate{color:var(--cond);border-color:var(--cond)}.ev-missing{color:var(--fail);border-color:var(--fail)}
.bar{position:relative;height:18px;background:var(--rule2);border-radius:2px;min-width:120px}.bar span{position:absolute;inset:0 auto 0 0;border-radius:2px}
.bar.ok span{background:var(--pass)}.bar.bad span{background:var(--fail)}.bar em{position:absolute;left:6px;top:0;font:11.5px var(--fm);font-style:normal}.bar.bad em{color:#fff}.bar.ok em{left:auto;right:6px}
.tabs{display:flex;flex-wrap:wrap;gap:6px}.tab{font:inherit;border:1px solid var(--rule);background:var(--panel);color:var(--ink);padding:5px 12px;border-radius:14px;cursor:pointer}
.tab[aria-pressed=true]{background:var(--ink);color:var(--panel);border-color:var(--ink)}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:18px}.two>*{min-width:0}
details{border:1px solid var(--rule2);border-radius:4px;margin-top:-1px}summary{cursor:pointer;padding:6px 10px}details ul{margin:0;padding:4px 14px 10px 30px;display:grid;gap:4px;font-size:13px}
.parts{margin:0;padding-left:18px;font-size:13px}
.legend{display:flex;gap:8px;flex-wrap:wrap;font-size:12px}
.note{font-size:12.5px;color:var(--ink2);border-left:3px solid var(--cond);background:var(--condbg);padding:8px 12px;border-radius:0 4px 4px 0}
</style></head><body><div class="wrap">
<header class="top"><div><h1>{{TITLE}}</h1><p class="muted">Every variant is measured with the same requirements R1–R12, gates G1–G6, key metrics and analyses.</p></div>
<div class="meta"><span>{{N}} variants</span><span>baseline: {{BASE}}</span><span>generated {{GEN}}</span><span>OpenCAE Studio</span></div></header>
<div class="legend"><span class="st st-pass"><i>✓</i>Pass</span><span class="st st-cond"><i>◐</i>Conditional (predicted to pass; estimates or test pending)</span><span class="st st-fail"><i>✕</i>Fail</span><span class="st st-nev"><i>○</i>Not evaluated</span></div>
<section class="panel"><h2>Variants</h2><div class="cards">{{CARDS}}</div></section>
<section class="panel"><h2>Requirements</h2><div class="tw">{{REQ}}</div></section>
<section class="panel"><h2>Monitored quantities</h2><p class="sub">One scale per quantity; grey bar = baseline, blue = variant, red = misses its requirement limit (red tick). The evidence tag is the weakest input behind the number.</p>{{METRICS}}</section>
<section class="panel"><h2>Analyses (solver runs)</h2><p class="sub">A run counts as current only for the exact inputs it was computed with; after a change it shows as stale or not run.</p><div class="tw">{{HEAVY}}</div></section>
<section class="panel"><h2>Readiness gates</h2><div class="tw">{{GATES}}</div></section>
<section class="panel"><h2>Transition robustness</h2><p class="sub">Forward transition from 30 m in the 6-DOF model, each variant with its own procedure and output limit. Every case is applied on top of the variant (the payload case adds 0.3 kg to the variant's own payload). Green: within the 5 m acceptance of gate G3.</p>{{ROB}}</section>
<section class="panel"><h2>Variant detail</h2><div class="tabs" role="group" aria-label="Variant">{{TABS}}</div>{{DETAILS}}</section>
<p class="note">Predictions rest on estimated propeller, aerodynamic and material data; a requirement shows Pass only on direct evidence. Simulation results plan tests; they are not flight evidence, and no variant is approved to fly without the physical tests and a recorded decision (gates G5, G6).</p>
</div>
<script id="data" type="application/json">{{JSON}}</script>
<script>
(function(){
 function show(rid){document.querySelectorAll('.detail').forEach(function(d){d.hidden=d.dataset.rid!==rid;});
  document.querySelectorAll('.tab').forEach(function(t){t.setAttribute('aria-pressed',String(t.dataset.rid===rid));});}
 document.querySelectorAll('.tab').forEach(function(t){t.addEventListener('click',function(){show(t.dataset.rid);});});
 document.querySelectorAll('.card').forEach(function(c){c.tabIndex=0;function go(){show(c.dataset.rid);var d=document.getElementById('d-'+c.dataset.rid);if(d)d.scrollIntoView({behavior:'smooth',block:'start'});}
  c.addEventListener('click',go);c.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();go();}});});
})();
</script></body></html>"""
