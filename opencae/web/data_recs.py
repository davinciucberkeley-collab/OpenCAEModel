"""Design-recommendation data for the website: revision C (recommended) against revision B.

``site_recs(study, rid_b, rid_c)`` returns JSON-serialisable data built only from what the engine computes for
the two variants of the study (study snapshots: level-1 results, requirement and gate statuses, current heavy
analyses) and from what revision_c / manufacturing declare (changes, closure of requirements by measurement,
datasheet checks). Nothing is typed in here:

* ``changes``        - revision C's changes with their parameters and the before / after values of the metrics
                       each change addresses (both from the study snapshots of B and C);
* ``comparison``     - every requirement, gate and key metric of the study for B and C with their statuses;
* ``evidence_needed``- for every requirement of the selected variant that is not PASS: why, what closes it (the
                       tests, quantities and pass line of manufacturing.CLOSURE, and the supplier data that
                       would replace the estimated inputs), the current prediction and its margin;
* ``datasheet_upgrades`` - the inputs upgraded to supplier datasheets (with refs) and the ones checked but not
                       confirmed;
* ``not_applied``    - recommendations not incorporated in revision C and why.

A requirement shows PASS only where approval.py says so (a measurement for test-verified requirements); a
snapshot never mixes another variant's analyses (study.snapshot keeps them per input hash).
"""
from __future__ import annotations

import math
from typing import Optional

from opencae.engineering import approval as AP
from opencae.engineering import catalog as C
from opencae.engineering import manufacturing as MF
from opencae.engineering import revision_c as RC
from opencae.engineering import study as ST
from opencae.engineering.quantities import ESTIMATE, MISSING

_ALIAS = {"ct": ("prop", "ct0"), "cp": ("prop", "cp0"), "r_int": ("battery", "r_int"), "kv": ("motor", "kv"),
          "rm": ("motor", "rm"), "imax": ("motor", "imax"), "esc": ("esc", "i_cont"), "c": ("battery", "c_rate"),
          "pmax": ("motor", "pmax"), "stroke": ("landing", "stroke"), "petg": ("landing", "petg_strength")}
_PARAM_ALIAS = {"lean": "gust_lean_deg", "e": "oswald", "clmax": "cl_max", "np": "np_frac", "eta": "eta_prop_cruise"}


def _num(x, nd: int = 4):
    if isinstance(x, bool) or x is None:
        return x
    if isinstance(x, (int, float)):
        return None if (isinstance(x, float) and not math.isfinite(x)) else round(float(x), nd)
    return x


def estimate_inputs(engine, metric: str) -> list[dict]:
    """The provisional-estimate / missing inputs behind a result of the engine's current configuration (its
    Q.inputs resolved to model parameters, catalog quantities and component masses; cost results: shipping and
    the BOM prices marked ESTIMATE)."""
    m, r = engine.model, engine.results
    q = r.get(metric)
    if q is None:
        return []
    names = set(q.inputs or [])
    out = []
    for n in sorted(names):
        p = _PARAM_ALIAS.get(n, n)
        if p in m.params and m.params[p].source in (ESTIMATE, MISSING):
            out.append({"input": p, "class": m.params[p].source, "ref": m.params[p].ref})
        if n in _ALIAS:
            kind, key = _ALIAS[n]
            oq = m.option(kind).q(key)
            if oq.source in (ESTIMATE, MISSING):
                out.append({"input": f"{m.option(kind).name}: {key}", "class": oq.source,
                            "ref": oq.ref, "note": oq.note})
    if names & {"mass", "cg", "W", "T4"} or metric in ("mass_total", "weight", "cg"):
        est = [c.name for c in m.active_components() if m.component_mass(c).source in (ESTIMATE, MISSING)]
        if est:
            out.append({"input": "component masses", "class": ESTIMATE,
                        "ref": "volume-ratio masses of modified parts: " + ", ".join(est)})
    if metric in ("reserve", "cost_total"):
        sh = m.params["shipping"]
        if sh.source in (ESTIMATE, MISSING):
            out.append({"input": "shipping", "class": sh.source, "ref": sh.ref})
        est = [c.name for c in m.active_components() if m.component_price(c).source == ESTIMATE]
        if est:
            out.append({"input": f"{len(est)} BOM prices", "class": ESTIMATE, "ref": ", ".join(est)})
    return out


def _metric(snap: dict, key: str) -> Optional[dict]:
    return snap["metrics"].get(key)


def _results(study, rid: str, keys: set) -> dict:
    """Level-1 results of a variant for keys the study snapshot does not monitor ({key: {value, source}})."""
    cur = study.current
    study.select(rid)
    try:
        r = study.engine.results
        return {k: {"value": r[k].value, "source": r[k].source} for k in keys
                if k in r and isinstance(r[k].value, (int, float))}
    finally:
        if cur and cur in study.ids():
            study.select(cur)


def _changes(study, sb: dict, sc: dict, rid_b: str, rid_c: str) -> list[dict]:
    vb, vc = study.variant(rid_b), study.variant(rid_c)
    extra = {k for ch in RC.CHANGES for k, *_ in ch.get("effects", []) if k not in sb["metrics"]}
    xb, xc = _results(study, rid_b, extra), _results(study, rid_c, extra)
    out = []
    for ch in RC.CHANGES:
        params = []
        for p in ch.get("params", []):
            b, c = vb["params"].get(p), vc["params"].get(p)
            params.append({"param": p, "B": _num(b and b.get("value")), "C": _num(c and c.get("value")),
                           "unit": (c or b or {}).get("unit"), "class_C": (c or {}).get("source"),
                           "ref_C": (c or {}).get("ref")})
        effects = []
        for key, label, scale, unit in ch.get("effects", []):
            mb, mc = (_metric(sb, key), _metric(sc, key)) if key in sb["metrics"] else (xb.get(key), xc.get(key))
            effects.append({"metric": key, "label": label, "unit": unit,
                            "B": None if not mb or mb.get("value") is None else _num(mb["value"] * scale),
                            "C": None if not mc or mc.get("value") is None else _num(mc["value"] * scale),
                            "class_B": (mb or {}).get("source"), "class_C": (mc or {}).get("source")})
        heavy = []
        for nid in ch.get("analyses", []):
            hb, hc = sb["heavy"].get(nid, {}), sc["heavy"].get(nid, {})
            heavy.append({"analysis": nid, "status_B": hb.get("status"), "status_C": hc.get("status"),
                          "B": hb.get("summary") or {}, "C": hc.get("summary") or {},
                          "note": "" if hc.get("status") == "current" else
                          "not run for revision C yet: run the study's analyses (build_study) before relying on it"})
        out.append({k: ch[k] for k in ("id", "title", "requirement", "analysis", "what", "why", "evidence")}
                   | {"params": params, "effects": effects, "analyses": heavy})
    return out


def _comparison(sb: dict, sc: dict) -> list[dict]:
    rows = []
    reqb = {x["id"]: x for x in sb["requirements"]}
    for x in sc["requirements"]:
        b = reqb.get(x["id"], {})
        rows.append({"kind": "requirement", "id": x["id"], "label": x["text"], "unit": x["unit"],
                     "criterion": f"{x['op']} {x['threshold']:g}", "B": _num(b.get("predicted")),
                     "C": _num(x.get("predicted")), "class_B": b.get("predicted_class"),
                     "class_C": x.get("predicted_class"), "status_B": b.get("status"), "status_C": x["status"],
                     "why_C": x.get("why")})
    gb = {g["id"]: g for g in sb["gates"]}
    for g in sc["gates"]:
        rows.append({"kind": "gate", "id": g["id"], "label": g["name"], "unit": "", "criterion": "",
                     "B": None, "C": None, "status_B": gb.get(g["id"], {}).get("status"), "status_C": g["status"]})
    for key, label, group, scale, unit, nd, better in ST.METRICS:
        mb, mc = _metric(sb, key), _metric(sc, key)
        vb = None if not mb or mb.get("value") is None else mb["value"] * scale
        vc = None if not mc or mc.get("value") is None else mc["value"] * scale
        rows.append({"kind": "metric", "id": key, "label": label, "group": group, "unit": unit, "criterion": "",
                     "B": _num(vb, nd + 2), "C": _num(vc, nd + 2), "class_B": (mb or {}).get("source"),
                     "class_C": (mc or {}).get("source"), "better": better, "status_B": None, "status_C": None})
    return rows


def _evidence_needed(study, rid: str, snap: dict) -> list[dict]:
    cur = study.current
    study.select(rid)
    e = study.engine
    m = e.model
    out = []
    try:
        for x in snap["requirements"]:
            if x["status"] == AP.PASS:
                continue
            sp = MF.closure_spec(m, x["id"])
            thr, pred = x["threshold"], x.get("predicted")
            margin = None
            if isinstance(pred, (int, float)) and thr is not None:
                margin = (pred - thr) if x["op"] == ">=" else ((thr - pred) if x["op"] == "<=" else None)
            req = next(r for r in m.requirements if r.id == x["id"])
            est = estimate_inputs(e, req.metric)
            datasheets = RC.DATASHEETS_NEEDED.get(x["id"], [])
            out.append({
                "id": x["id"], "title": x["text"], "status": x["status"], "verification": x["verification"],
                "why_conditional": x.get("why"),
                "closes_by_test": [{"test_id": t, "quantity": q, "unit": u, "how": how}
                                   for t, q, u, how, _p in sp["inputs"]],
                "pass_line": MF.pass_line(m, x["id"], e.results),
                "closes_by_data": datasheets if req.verification != "test" else [],
                "note": ("verified by test: only the measurement closes it" if req.verification == "test" else
                         "closed by the measurement, or by replacing every estimated input with supplier data "
                         "(then the analysis / inspection result is evidence)"),
                "estimated_inputs": est,
                "prediction": {"value": _num(pred), "unit": x["unit"], "class": x.get("predicted_class"),
                               "criterion": f"{x['op']} {thr:g}"},
                "margin": _num(margin), "margin_pct": _num(margin / abs(thr) * 100.0, 1)
                if margin is not None and thr else None})
    finally:
        if cur and cur in study.ids():
            study.select(cur)
    return out


def site_recs(study, rid_b: str, rid_c: str, selected: Optional[str] = None) -> dict:
    """Revision C recommendations against revision B (see the module docstring). ``selected`` is the variant
    whose open requirements are listed in evidence_needed (default: rid_c)."""
    sel = selected or rid_c
    sb, sc = study.snapshot(rid_b), study.snapshot(rid_c)
    ss = sc if sel == rid_c else (sb if sel == rid_b else study.snapshot(sel))
    return {
        "variants": {"B": {"id": rid_b, "name": study.variant(rid_b)["name"]},
                     "C": {"id": rid_c, "name": study.variant(rid_c)["name"]}, "selected": sel},
        "changes": _changes(study, sb, sc, rid_b, rid_c),
        "comparison": _comparison(sb, sc),
        "evidence_needed": _evidence_needed(study, sel, ss),
        "datasheet_upgrades": [dict(d) for d in RC.DATASHEET_UPGRADES],
        "datasheets_checked_not_upgraded": [dict(d) for d in RC.DATASHEET_NOT_CONFIRMED],
        "not_applied": [dict(d) for d in RC.NOT_APPLIED],
        "operating_limits": list(RC.OPERATING_LIMITS),
        "catalog_refs": {"esc": C.HOLYBRO_TEKKO_SPEC, "prop_data": C.APC_PER3_11x45MR},
        "note": "Statuses come from approval.py: a test-verified requirement stays CONDITIONAL until a measurement "
                "is imported (manufacturing.write_measurement_template / requirement_measurements). Simulation "
                "results plan tests; they are not flight evidence and grant no approval.",
    }
