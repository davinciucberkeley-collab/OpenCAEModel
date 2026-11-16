"""Requirements evaluation, design-readiness gates, trade studies and the readiness summary.

Status rules (never relaxed):

* PASS needs evidence that supports the criterion: a physical measurement for requirements verified by
  test; an analysis or inspection result that is not a provisional estimate for the others.
* A prediction that meets the criterion but rests on estimates, or a test-verified requirement with only
  a prediction, is CONDITIONAL / REVIEW REQUIRED.
* A prediction or measurement that misses the criterion is FAIL.
* Missing data is NOT EVALUATED - never a pass.

Gates are decision support for a human reviewer; a gate is only marked APPROVED by a recorded decision,
and gates 5-6 cannot pass on simulated data at all.
"""
from __future__ import annotations

import itertools
import time
from typing import Optional

from opencae.engineering import calcs
from opencae.engineering import catalog as C
from opencae.engineering.graph import CURRENT, Engine
from opencae.engineering.quantities import ANALYTICAL, ESTIMATE, MISSING, REDUCED, SIMULATION, SUPPLIER, TEST, \
    USER, Q

PASS, FAIL, COND, NOT_EV = "PASS", "FAIL", "CONDITIONAL / REVIEW REQUIRED", "NOT EVALUATED"
_OPS = {"<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b, "==": lambda a, b: abs(a - b) < 1e-9}
STRONG = {TEST, SUPPLIER, USER, ANALYTICAL, SIMULATION}


def evaluate_requirement(req, value: Optional[Q], measured: Optional[dict] = None) -> dict:
    """Status of one requirement from its predicted value and, if any, a measurement record
    ({'value': float, 'unit': ..., 'test_id': ...} in the requirement's units)."""
    thr = req.threshold.value
    row = {"id": req.id, "text": req.text, "metric": req.metric, "op": req.op, "threshold": thr,
           "unit": req.threshold.unit, "verification": req.verification, "kind": req.kind,
           "predicted": None if value is None else value.value, "predicted_class": None if value is None else
           value.source, "measured": None, "status": NOT_EV, "why": "", "action": ""}
    if measured is not None and measured.get("value") is not None:
        ok = _OPS[req.op](measured["value"], thr)
        row.update(measured=measured["value"], status=PASS if ok else FAIL,
                   why=f"measured in {measured.get('test_id', 'a test')}")
        if not ok:
            row["action"] = "Measured value misses the criterion: redesign or re-plan, then re-test."
        return row
    if value is None or value.missing or not isinstance(value.value, (int, float)):
        row.update(why="no prediction (input data missing)" if value is None or value.missing else "not scalar",
                   action="Supply the missing input or run the analysis that produces it.")
        return row
    ok = _OPS[req.op](value.value, thr)
    if not ok:
        row.update(status=FAIL, why=f"predicted {value.value:.4g} {req.op} {thr:g} is not met ({value.source})",
                   action="Change the configuration (see trade study) or the requirement owner must re-negotiate.")
    elif req.verification == "test":
        row.update(status=COND, why="predicted to pass; this requirement is verified by test - no measurement yet",
                   action="Run the linked physical test and record the measurement.")
    elif value.source not in STRONG:
        row.update(status=COND, why=f"predicted to pass, but the prediction rests on {value.source.replace('_', ' ')} "
                                    "inputs", action="Replace the estimated inputs with supplier data or tests.")
    else:
        row.update(status=PASS, why=f"{req.verification} result ({value.source})")
    return row


def requirements_table(engine: Engine, measurements: Optional[dict] = None) -> list[dict]:
    measurements = measurements or {}
    return [evaluate_requirement(r, engine.results.get(r.metric), measurements.get(r.id))
            for r in engine.model.requirements]


def _worst(statuses: list[str]) -> str:
    if not statuses:
        return NOT_EV
    if FAIL in statuses:
        return FAIL
    if NOT_EV in statuses:  # missing evidence is never shown as progress
        return NOT_EV
    if COND in statuses:
        return COND
    return PASS


def gates(engine: Engine, measurements: Optional[dict] = None, decisions: Optional[dict] = None) -> list[dict]:
    """The six readiness gates with criteria, evidence, open issues and the recorded decision."""
    req = {r["id"]: r for r in requirements_table(engine, measurements)}
    decisions = decisions or {}
    m = engine.model
    unresolved = list(m.unresolved)
    G = []

    def gate(gid, name, crit_rows, extra_open=None, evidence=None, cannot_pass_on_sim=False):
        st = _worst([c["status"] for c in crit_rows])
        if cannot_pass_on_sim and st == PASS and not any(c.get("measured_evidence") for c in crit_rows):
            st = COND
        dec = decisions.get(gid)
        G.append({"id": gid, "name": name, "status": st, "criteria": crit_rows, "open": extra_open or [],
                  "evidence": evidence or [], "decision": dec})

    # G1 requirements defined
    crit = [{"text": f"{r.id} has a numeric threshold and a source",
             "status": PASS if r.threshold.value is not None and r.source else FAIL} for r in m.requirements]
    crit.append({"text": "Unresolved engineering inputs registered and owned",
                 "status": COND if unresolved else PASS,
                 "why": f"{len(unresolved)} unresolved inputs (see list)"})
    gate("G1", "Requirements sufficiently defined", crit, [u[0] + ": " + u[1] for u in unresolved],
         ["Decision page constraints C1-C5", "requirements R1-R12 in the model"])
    # G2 preliminary design feasible
    ids = ["R1", "R2", "R3", "R4", "R5", "R7", "R8", "R9", "R12"]
    crit = [{"text": f"{i}: {req[i]['text']}", "status": req[i]["status"], "why": req[i]["why"]} for i in ids]
    gate("G2", "Preliminary design feasible", crit, [f"{i}: {req[i]['action']}" for i in ids if req[i]["action"]],
         ["Level-1 calculations of the current configuration"])
    # G3 detailed verification
    crit = []
    for nid, label in (("fea_motor_tube", "Motor-tube FEA current"), ("assembly_check", "Assembly check current"),
                       ("flight_hover", "Hover simulation current"), ("fea_spar", "Spar FEA current"),
                       ("flight_transition", "Transition simulation current: converges, altitude loss <= "
                                             f"{TRANSITION_MAX_ALT_LOSS:.0f} m")):
        h = engine.heavy_status(nid)
        st, why = (PASS if h["status"] == CURRENT else NOT_EV), h["status"] + (f" - {h['reason']}" if h["reason"] else "")
        rec = h.get("record") or {}
        if st == PASS and nid == "flight_transition":  # acceptance first, so a warning never hides a loss
            out = rec.get("outcome") or {}
            a0, amin = rec.get("altitude_start"), rec.get("min_altitude")
            loss = (a0 - amin) if a0 is not None and amin is not None else -(out.get("altitude_change") or 0.0)
            if not out.get("transition_converged"):
                st, why = FAIL, f"transition did not converge: {out}"
            elif loss > TRANSITION_MAX_ALT_LOSS:
                st, why = FAIL, f"altitude loss {loss:.1f} m during the transition (limit {TRANSITION_MAX_ALT_LOSS} m)"
            else:
                why = f"altitude loss {loss:.1f} m"
        if st == PASS and nid.startswith("flight"):
            out = rec.get("outcome") or {}
            bad = [w for _, w in rec.get("warnings", [])] if rec.get("warnings") else []
            if out.get("max_position_error", 0) > 1.0 or any(("crash" in str(w).lower() or "hard ground contact"
                                                              in str(w).lower()) for w in bad):
                st, why = FAIL, f"simulation outcome: {out}"
            elif bad:
                st, why = COND, (why + "; " if nid == "flight_transition" else "") + \
                    f"{len(bad)} simulation warning(s) to review, e.g. {bad[0]}"
        if st == PASS and nid.startswith("fea") and not (rec.get("result") or {}).get("converged", True):
            st, why = FAIL, "solver did not converge"
        crit.append({"text": label, "status": st, "why": why})
    ac = engine.heavy.get("assembly_check", {}).get("record") or {}
    if ac:
        n_int = len(ac.get("interferences", []))
        n_el = len(ac.get("electrical_gaps", []))
        crit.append({"text": "No unexplained interferences", "status": PASS if n_int == 0 else COND,
                     "why": f"{n_int} interference(s) to review"})
        crit.append({"text": "Electrical interfaces complete", "status": PASS if n_el == 0 else COND,
                     "why": f"{n_el} harness gap(s) to review"})
    crit.append({"text": "R11: " + req["R11"]["text"], "status": req["R11"]["status"], "why": req["R11"]["why"]})
    for rid in ("R6", "R10"):
        crit.append({"text": f"{rid}: {req[rid]['text']}", "status": req[rid]["status"], "why": req[rid]["why"]})
    gate("G3", "Detailed design verification complete", crit,
         [c["text"] + " - " + c.get("why", "") for c in crit if c["status"] != PASS])
    # G4 approved for fabrication
    crit = [{"text": "G3 passed", "status": G[2]["status"]},
            {"text": "Motor bolt pattern measured", "status": NOT_EV, "why": "MEASURE item open"},
            {"text": "FC / servo mounting confirmed", "status": NOT_EV, "why": "MEASURE item open"},
            {"text": "R3: " + req["R3"]["text"], "status": req["R3"]["status"], "why": req["R3"]["why"]},
            {"text": "Process plan for every made part", "status": COND,
             "why": "plan exists; times and print settings are estimates / guidance"}]
    gate("G4", "Approved for fabrication", crit, [c["text"] for c in crit if c["status"] != PASS])
    # G5 ground-test evidence
    tests = ["R1", "R2", "R7", "R9", "R4"]
    crit = [{"text": f"{i} measured: {req[i]['text']}", "status": req[i]["status"] if req[i]["measured"] is not None
             else NOT_EV, "why": req[i]["why"] if req[i]["measured"] is not None else "no measurement",
             "measured_evidence": req[i]["measured"] is not None} for i in tests]
    gate("G5", "Ground-test evidence sufficient", crit, [c["text"] for c in crit if c["status"] != PASS],
         cannot_pass_on_sim=True)
    # G6 flight-test review
    crit = [{"text": "G5 passed", "status": G[4]["status"]},
            {"text": "Tethered / low hover test logged", "status": NOT_EV, "why": "no flight-test record"},
            {"text": "Independent engineering & safety review recorded (APPROVE)",
             "status": PASS if (decisions.get("G6") or {}).get("decision") == "APPROVE" else
             (FAIL if (decisions.get("G6") or {}).get("decision") == "REJECT" else NOT_EV)},
            {"text": "Simulation results are not flight evidence", "status": COND,
             "why": "virtual flight uses provisional aerodynamics; it can only plan tests"}]
    gate("G6", "Ready for controlled flight-test review", crit, [c["text"] for c in crit if c["status"] != PASS],
         cannot_pass_on_sim=True)
    return G


def record_decision(decisions: dict, gate_id: str, reviewer: str, decision: str, notes: str = "") -> dict:
    """Store a reviewer's decision (APPROVE / REJECT / DEFER) for a gate; the gate status itself is not
    changed by this - it stays what the evidence says."""
    decisions[gate_id] = {"reviewer": reviewer, "decision": decision, "notes": notes,
                          "time": time.strftime("%Y-%m-%d %H:%M")}
    return decisions[gate_id]


# --------------------------------------------------------------------------- trade study
OBJECTIVES = {"landing_load": ("landing_n", min), "mass": ("mass_total", min), "cost": ("cost_total", min), "endurance": ("endurance_hover", max),
              "tw": ("tw_low", max), "tipover": ("tipover_deg", max), "motor_margin": ("motor_current_margin", max)}
TRANSITION_MAX_ALT_LOSS = 5.0  # m, transition simulation acceptance (G3)
CONSTRAINTS = ["R1", "R2", "R7", "R8", "R9", "R12"]  # R3 needs shipping data; cost is an objective


def trade_study(engine: Engine, objectives=("mass", "cost", "endurance"), constraints=CONSTRAINTS) -> dict:
    """Enumerate every compatible combination of catalog options (motor x battery x landing x prop x ESC),
    evaluate the level-1 model for each, drop the ones that FAIL a constraint (a CONDITIONAL prediction
    stays, marked), and return all candidates with the Pareto-optimal set for the chosen objectives. The
    configuration of the engine is restored afterwards."""
    m = engine.model
    saved = dict(m.config)
    kinds = list(C.KINDS)
    choices = [[o.id for o in m.catalog[k] if o.compatible] for k in kinds]
    rows = []
    try:
        for combo in itertools.product(*choices):
            for k, oid in zip(kinds, combo):
                m.config[k] = oid
            res = calcs.evaluate_all(m)
            reqs = {r.id: evaluate_requirement(r, res.get(r.metric)) for r in m.requirements}
            failed = [i for i in constraints if reqs[i]["status"] == FAIL]
            cond = [i for i in constraints if reqs[i]["status"] == COND]
            nev = [i for i in constraints if reqs[i]["status"] == NOT_EV]
            obj = {name: res[OBJECTIVES[name][0]].value for name in objectives}
            rows.append({"config": dict(zip(kinds, combo)), "objectives": obj, "feasible": not failed,
                         "failed": failed, "conditional": cond, "not_evaluated": nev,
                         "landing_n": res["landing_n"].value,
                         "classes": {n: res[OBJECTIVES[n][0]].source for n in objectives}})
    finally:
        m.config = saved
        m.suppressed_bodies()  # refresh the suppression cache for the restored configuration
    # ranked: feasible, all objectives known, and no constraint left without any evidence
    feas = [r for r in rows if r["feasible"] and all(v is not None for v in r["objectives"].values())
            and not r["not_evaluated"]]

    def dominates(a, b):
        better = False
        for n in objectives:
            sense = OBJECTIVES[n][1]
            va, vb = a["objectives"][n], b["objectives"][n]
            if (va > vb if sense is min else va < vb):
                return False
            if va != vb:
                better = True
        return better

    for r in rows:
        r["pareto"] = r in feas and not any(dominates(o, r) for o in feas if o is not r)
    return {"objectives": list(objectives), "constraints": list(constraints), "candidates": rows,
            "note": "Discrete enumeration of the catalog (small catalog: add verified options to widen it). "
                    "Objective values carry their evidence classes; estimates are not ranked as facts."}


def readiness_summary(engine: Engine, measurements: Optional[dict] = None, decisions: Optional[dict] = None) -> dict:
    G = gates(engine, measurements, decisions)
    req = requirements_table(engine, measurements)
    stage = next((g for g in G if g["status"] != PASS), None)
    risks = []
    r = engine.results
    if r.get("motor_current_margin") and r["motor_current_margin"].value is not None \
            and r["motor_current_margin"].value < 1:
        risks.append(f"Full-throttle motor current {r['i_motor_max_full'].value:.1f} A exceeds the motor's 30-s "
                     f"rating (margin {r['motor_current_margin'].value:.2f}); provisional prop data - thrust-stand test")
    if r.get("tipover_margin") and r["tipover_margin"].value is not None and r["tipover_margin"].value < 0:
        risks.append(f"Standing aircraft tips over at {r['tipover_deg'].value:.1f} deg, below the "
                     f"{engine.model.params['gust_lean_deg'].value:.0f} deg lean of the 8 m/s gust")
    if r.get("static_margin") and r["static_margin"].value is not None and r["static_margin"].value < 0.08:
        risks.append(f"Static margin {r['static_margin'].value * 100:.1f} % (neutral point estimated)")
    risks.append("Any single motor/ESC failure is a crash in both modes (accepted in the decision)")
    risks.append("No validated aerodynamic data: transition behaviour is unverified")
    return {"stage": "all gates passed" if stage is None else f"{stage['id']} - {stage['name']}",
            "gates": [(g["id"], g["name"], g["status"]) for g in G],
            "failed": [x for x in req if x["status"] == FAIL],
            "missing_evidence": [x for x in req if x["status"] in (COND, NOT_EV)],
            "risks": risks,
            "cost": {k: engine.results[k].value for k in ("cost_parts", "cost_total", "reserve")},
            "next_steps": [x["action"] for x in req if x["action"]][:8] +
                          [f"Close unresolved input: {u[0]}" for u in engine.model.unresolved[:4]]}
