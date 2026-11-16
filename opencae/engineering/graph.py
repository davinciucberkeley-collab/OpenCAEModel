"""Engineering dependency graph and the engine that keeps results consistent with the configuration.

Nodes are design inputs (catalog selections, parameters), calculations, analyses, cost, manufacturing
and acceptance criteria; edges say what each one reads. When an input changes the engine

* finds everything downstream (``downstream``),
* recomputes the light nodes at once (level-1 calculations, cost, BOM, gates),
* marks heavy nodes (FEA, flight simulation, assembly check) STALE with the reason, without running them,
* keeps every result tagged with the configuration and geometry hash it was computed for, so an old
  result can never be shown as current, and keeps a run history for comparison between revisions.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from opencae.engineering import calcs
from opencae.engineering.model import EngineeringModel
from opencae.engineering.quantities import Q

LIGHT, HEAVY = "light", "heavy"
CURRENT, STALE, NOT_RUN, FAILED = "current", "stale", "not run", "failed"


@dataclass
class Node:
    id: str
    label: str
    kind: str  # input | calc | analysis | cost | manufacturing | assembly | gate
    cost: str = LIGHT
    reads: list = field(default_factory=list)  # upstream node ids
    produces: list = field(default_factory=list)  # result keys (light nodes)
    why: str = ""  # why this analysis is needed


def build_graph() -> dict[str, Node]:
    N = [
        # inputs
        Node("motor", "Motor selection", "input"),
        Node("prop", "Propeller selection", "input"),
        Node("esc", "ESC selection", "input"),
        Node("battery", "Battery selection", "input"),
        Node("landing", "Landing configuration", "input"),
        Node("mount", "Motor mount configuration", "input"),
        Node("fc_limits", "Flight-controller motor output limit", "input"),
        Node("flight_procedure", "Transition procedure (pitch-over angle, rate, airspeed)", "input"),
        Node("payload", "Payload", "input"),
        Node("environment", "Air density / gravity", "input"),
        Node("aero_params", "Aerodynamic assumptions (C_D0, e, NP)", "input"),
        Node("budget_params", "Budget, tax, contingency, shipping", "input"),
        Node("mission", "Mission profile (hover / cruise time)", "input"),
        Node("limits", "Operating limits (flight card gust lean)", "input"),
        # light calculations
        Node("geometry", "Active geometry (configuration)", "calc", reads=["landing", "mount"],
             produces=["envelope", "box_fit", "span", "chord", "area", "x_le", "core_te"]),
        Node("mass", "Mass, CG, inertia", "calc", reads=["geometry", "motor", "battery", "payload", "environment"],
             produces=["mass_total", "cg", "inertia", "weight"]),
        Node("ground", "Ground stability (tip-over)", "calc", reads=["mass", "landing", "limits"],
             produces=["tipover_deg", "cg_height_standing", "tipover_margin"]),
        Node("stability", "Static margin", "calc", reads=["mass", "aero_params"],
             produces=["cg_frac", "static_margin"]),
        Node("propulsion", "Propulsion operating points", "calc",
             reads=["mass", "motor", "prop", "battery", "environment", "fc_limits"],
             produces=["thrust_max_full", "tw_full", "thrust_max_low", "tw_low", "i_motor_max_full",
                       "i_motor_max_low", "rpm_max_full", "rpm_hover", "disk_loading", "p_hover_elec",
                       "p_hover_momentum", "hover_throttle", "i_hover_motor", "i_hover_total", "i_hover_battery",
                       "p_hover_battery", "throttle_headroom_low"]),
        Node("electrical", "Electrical compatibility & limits", "calc", reads=["propulsion", "esc", "battery"],
             produces=["motor_current_margin", "esc_margin", "battery_c_margin", "cells_ok", "motor_power_margin",
                       "i_battery_max"]),
        Node("thermal", "Motor / battery heat (level 1)", "calc", reads=["propulsion"],
             produces=["motor_loss_hover", "motor_loss_max", "battery_heat_hover"]),
        Node("endurance", "Endurance & mission energy", "calc", reads=["propulsion", "aero_params", "battery", "mission"],
             produces=["endurance_hover", "v_cruise", "cl_cruise", "p_cruise_elec", "ld_cruise", "endurance_cruise",
                       "mission_margin", "aspect_ratio"]),
        Node("landing_loads", "Landing impact load", "calc", reads=["mass", "landing"],
             produces=["landing_n", "landing_force_total", "landing_force_per_contact", "foot_overhang",
                       "foot_root_stress", "foot_sf"]),
        Node("cost", "BOM cost & reserve", "cost", reads=["geometry", "motor", "battery", "budget_params"],
             produces=["cost_parts", "cost_tax", "cost_contingency", "cost_total", "reserve", "cost_estimated_lines"]),
        Node("manufacturing", "Fabrication plan & test matrix", "manufacturing", reads=["geometry"]),
        # heavy analyses
        Node("fea_motor_tube", "FEA: motor tube at full thrust (CalculiX)", "analysis", HEAVY,
             reads=["propulsion", "geometry"],
             why="R11 needs the motor-tube safety factor; thrust and torque come from the propulsion point."),
        Node("fea_spar", "FEA: wing spar under lift (CalculiX)", "analysis", HEAVY, reads=["mass", "geometry"],
             why="Spar bending margin at the assumed load factor depends on the aircraft weight."),
        Node("assembly_check", "Assembly check (interference, interfaces)", "assembly", HEAVY,
             reads=["geometry", "motor", "prop", "battery", "esc"],
             why="Geometry, mounting or component changes can create interferences or gaps."),
        Node("flight_hover", "Flight simulation: hover", "analysis", HEAVY,
             reads=["mass", "propulsion", "environment", "aero_params"],
             why="Telemetry (current, SOC, motor temperature) follows mass, inertia and propulsion."),
        Node("flight_transition", "Flight simulation: transition", "analysis", HEAVY,
             reads=["mass", "propulsion", "aero_params", "environment", "flight_procedure"],
             why="Transition depends on thrust margin and the provisional aerodynamic model."),
        Node("gates", "Requirements & readiness gates", "gate",
             reads=["mass", "propulsion", "electrical", "endurance", "ground", "stability", "cost", "geometry",
                    "fea_motor_tube"]),
    ]
    return {n.id: n for n in N}


INPUT_OF_PARAM = {"payload": "payload", "rho": "environment", "g": "environment", "cd0": "aero_params",
                  "oswald": "aero_params", "eta_prop_cruise": "aero_params", "np_frac": "aero_params",
                  "fm": "aero_params", "tax_rate": "budget_params", "shipping": "budget_params",
                  "budget": "budget_params", "contingency": "budget_params", "crash_reserve": "budget_params",
                  "hover_min": "mission", "cruise_min": "mission", "gust_lean_deg": "limits", "cl_max": "aero_params",
                  "throttle_max": "fc_limits", "transition_pitch_end_deg": "flight_procedure",
                  "transition_pitch_rate_deg": "flight_procedure", "transition_airspeed": "flight_procedure",
                  "foot_outrigger_mm": "landing", "foot_shorten_mm": "landing", "motor_inboard_mm": "mount"}


class Engine:
    """Owns the model, the graph, the current light results and the heavy-analysis records."""

    def __init__(self, model: Optional[EngineeringModel] = None):
        self.model = model or EngineeringModel()
        self.graph = build_graph()
        self.results: dict[str, Q] = {}
        self.heavy: dict[str, dict] = {nid: {"status": NOT_RUN, "reason": "never run", "record": None}
                                       for nid, n in self.graph.items() if n.cost == HEAVY}
        self.history: list[dict] = []  # every heavy run, with hashes
        self.log: list[str] = []
        self.listeners: list[Callable[[dict], None]] = []
        self.recompute()

    # ------------------------------------------------------------------ graph queries
    def downstream(self, nid: str) -> list[str]:
        out, todo = [], [nid]
        while todo:
            cur = todo.pop()
            for n in self.graph.values():
                if cur in n.reads and n.id not in out:
                    out.append(n.id)
                    todo.append(n.id)
        return out

    def upstream(self, nid: str) -> list[str]:
        out, todo = [], [nid]
        while todo:
            for r in self.graph[todo.pop()].reads:
                if r not in out:
                    out.append(r)
                    todo.append(r)
        return out

    def node_of_result(self, key: str) -> Optional[str]:
        return next((n.id for n in self.graph.values() if key in n.produces), None)

    def edges(self) -> list[tuple[str, str]]:
        return [(r, n.id) for n in self.graph.values() for r in n.reads]

    # ------------------------------------------------------------------ changes
    def set_option(self, kind: str, oid: str) -> dict:
        old = self.model.config.get(kind)
        ok, why = self.model.set_option(kind, oid)
        if not ok:
            return {"ok": False, "reason": why}
        if old == oid:
            return {"ok": True, "changed": []}
        return self._changed(kind, f"{kind}: {old} -> {oid}")

    def set_param(self, name: str, value: float, source: Optional[str] = None, ref: str = "") -> dict:
        """Change a parameter. Its evidence class and uncertainty stay as they were (editing an estimate does
        not make it a fact) unless ``source`` re-classifies it, e.g. with a datasheet or test reference."""
        q = self.model.params[name]
        old = q.value
        src = source or (q.source if q.source != "missing" else "user_input")
        self.model.params[name] = Q(value, q.unit, src, ref or q.ref, uncertainty=None if source else q.uncertainty,
                                    note=f"edited (was {old})")
        return self._changed(INPUT_OF_PARAM.get(name, "aero_params"), f"{name}: {old} -> {value}")

    def _changed(self, input_node: str, text: str) -> dict:
        before = dict(self.results)
        affected = self.downstream(input_node)
        for nid in affected:
            if nid in self.heavy and self.heavy[nid]["status"] in (CURRENT, FAILED):
                self.heavy[nid]["status"] = STALE
                self.heavy[nid]["reason"] = f"upstream change ({text}) via {self._path(input_node, nid)}"
            elif nid in self.heavy and self.heavy[nid]["status"] == NOT_RUN:
                self.heavy[nid]["reason"] = f"never run; also affected by {text}"
        self.recompute()
        delta = {k: (before[k].value, q.value) for k, q in self.results.items()
                 if k in before and before[k].value != q.value}
        ev = {"ok": True, "change": text, "input": input_node, "affected": affected, "delta": delta,
              "stale": [n for n in affected if n in self.heavy and self.heavy[n]["status"] == STALE],
              "config_hash": self.model.config_hash()}
        self.log.append(f"{time.strftime('%H:%M:%S')} {text}: {len(affected)} nodes affected, "
                        f"{len(ev['stale'])} analyses stale")
        for f in self.listeners:
            f(ev)
        return ev

    def _path(self, a: str, b: str) -> str:
        """A shortest dependency path from a to b, for explanations."""
        prev, todo = {a: None}, [a]
        while todo:
            cur = todo.pop(0)
            if cur == b:
                break
            for n in self.graph.values():
                if cur in n.reads and n.id not in prev:
                    prev[n.id] = cur
                    todo.append(n.id)
        path, cur = [], b
        while cur is not None:
            path.append(cur)
            cur = prev.get(cur)
        return " -> ".join(reversed(path))

    def node_hash(self, nid: str) -> str:
        """Hash of exactly the design inputs a node depends on (catalog selections and parameters upstream)."""
        import hashlib
        import json
        ups = set(self.upstream(nid))
        data = {k: v for k, v in self.model.config.items() if k in ups}
        data.update({p: q.value for p, q in self.model.params.items() if INPUT_OF_PARAM.get(p, "aero_params") in ups})
        data["geometry"] = self.model.geometry_hash() if "geometry" in ups else None
        return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:12]

    def recompute(self) -> None:
        self.results = calcs.evaluate_all(self.model)
        # attach the R11 result of an FEA record that is current for this configuration
        if "fea_motor_tube" in self.heavy and self.heavy_status("fea_motor_tube")["status"] == CURRENT:
            sf = (self.heavy["fea_motor_tube"].get("record") or {}).get("sf_q")
            if sf is not None:
                self.results["motor_tube_sf"] = sf

    # ------------------------------------------------------------------ heavy analyses
    def record_heavy(self, nid: str, record: dict, ok: bool = True) -> None:
        rec = dict(record)
        rec.update(config_hash=self.model.config_hash(), geometry_hash=self.model.geometry_hash(),
                   node_hash=self.node_hash(nid),
                   time=time.strftime("%Y-%m-%d %H:%M:%S"), config=dict(self.model.config))
        self.heavy[nid] = {"status": CURRENT if ok else FAILED, "reason": "" if ok else rec.get("error", "failed"),
                           "record": rec}
        self.history.append({"node": nid, **rec})
        self.recompute()

    # ------------------------------------------------------------------ persistence
    def save_state(self, path) -> None:
        """Model state (configuration, parameters, revisions) plus the heavy-analysis records, each with
        the hashes of the inputs it was computed for (so a reloaded record is current only if they match)."""
        import json
        from pathlib import Path as _P
        self.model.save(_P(path))
        d = json.loads(_P(path).read_text())
        d["heavy"] = {nid: {"status": h["status"], "reason": h["reason"], "record": _jsonable(h.get("record"))}
                      for nid, h in self.heavy.items()}
        d["history"] = _jsonable(self.history)
        _P(path).write_text(json.dumps(d, indent=1))

    def load_state(self, path) -> None:
        import json
        from pathlib import Path as _P
        self.model.load_state(_P(path))
        d = json.loads(_P(path).read_text())
        for nid, h in (d.get("heavy") or {}).items():
            if nid in self.heavy:
                self.heavy[nid] = {"status": h["status"], "reason": h["reason"], "record": _unjson(h["record"])}
        self.history = _unjson(d.get("history") or [])
        self.recompute()
        for nid in self.heavy:
            self.heavy_status(nid)  # demote a record whose inputs differ from the loaded configuration

    def heavy_status(self, nid: str) -> dict:
        """Status of a heavy analysis for the current inputs. A record counts as current only when the hash
        of exactly its inputs matches; when the inputs return to those of an earlier successful run (e.g.
        switching back to a revision), that run is taken from the history, never re-labelled."""
        h = self.heavy[nid]
        rec = h.get("record") or {}
        cur = self.node_hash(nid)
        if rec.get("node_hash") == cur:
            if h["status"] == STALE and not rec.get("error"):
                h["status"], h["reason"] = CURRENT, ""
            return h
        match = next((r for r in reversed(self.history) if r.get("node") == nid and r.get("node_hash") == cur
                      and not r.get("error")), None)
        if match is not None:
            h["record"] = {k: v for k, v in match.items() if k != "node"}
            h["status"], h["reason"] = CURRENT, f"run of {match.get('time', '?')} for these inputs (history)"
        elif h["status"] == CURRENT:
            h["status"], h["reason"] = STALE, "an input of this analysis differs from the one analysed"
        return h

    def restore_revision(self, rev_id: str) -> list[dict]:
        """Return to a saved revision: its catalog selections and its parameters (values and evidence
        classes), through the normal change path so every affected node is updated or marked stale."""
        m = self.model
        rev = next(r for r in m.revisions if r["id"] == rev_id)
        evs = []
        for k, v in rev["config"].items():
            if m.config.get(k) != v:
                evs.append(self.set_option(k, v))
        wanted = {k: Q.from_dict(d) for k, d in rev["params"].items()}
        for k, q in m.params_default.items():  # parameters added after the revision was saved: model default
            wanted.setdefault(k, q)
        for name, q in wanted.items():
            old = m.params.get(name)
            if old is None or old.value != q.value or old.source != q.source:
                m.params[name] = q
                evs.append(self._changed(INPUT_OF_PARAM.get(name, "aero_params"),
                                         f"{name}: {old.value if old else None} -> {q.value} (revision {rev_id})"))
        return evs

    def explain(self, nid: str) -> str:
        n = self.graph[nid]
        up = ", ".join(self.graph[u].label for u in n.reads) or "nothing (design input)"
        down = ", ".join(self.graph[d].label for d in self.downstream(nid)) or "nothing"
        txt = f"{n.label}: reads {up}; affects {down}."
        if n.why:
            txt += f" Needed because: {n.why}"
        if nid in self.heavy:
            h = self.heavy_status(nid)
            txt += f" Status: {h['status']}" + (f" ({h['reason']})" if h["reason"] else "") + "."
        return txt


def _jsonable(x):
    """JSON-safe copy of a heavy-analysis record (quantities are tagged so they come back as Q)."""
    import numpy as np
    if isinstance(x, Q):
        return {"__Q__": x.to_dict()}
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, (np.floating, np.integer, np.bool_)):
        return x.item()
    if x is None or isinstance(x, (str, int, float, bool)):
        return x
    return str(x)


def _unjson(x):
    if isinstance(x, dict):
        if set(x) == {"__Q__"}:
            return Q.from_dict(x["__Q__"])
        return {k: _unjson(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_unjson(v) for v in x]
    return x
