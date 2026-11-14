"""Fabrication & readiness review report (HTML): component list, assembly breakdown, manufacturing
workflow, BOM with price evidence, verification matrix, requirement status, gates, risks and the
provenance of every number (configuration and geometry hashes, evidence classes)."""
from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Optional

from opencae.engineering import approval as AP
from opencae.engineering import assembly as AS
from opencae.engineering import manufacturing as MF
from opencae.engineering.graph import Engine
from opencae.engineering.quantities import CLASSES, SHORT

CSS = """body{font:14px/1.5 system-ui,Segoe UI,Arial,sans-serif;max-width:1180px;margin:24px auto;padding:0 16px;
color:#121619}h1{font-size:1.6rem}h2{border-bottom:1px solid #cbd2db;padding-bottom:4px;margin-top:30px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}th,td{border-bottom:1px solid #e3e7ed;
padding:4px 7px;text-align:left;vertical-align:top}th{background:#eef1f5}td.n{text-align:right;white-space:nowrap}
.PASS{color:#1A6E3A;font-weight:600}.FAIL{color:#AD2A1F;font-weight:600}.COND{color:#8A5600;font-weight:600}
.NOT{color:#6B7280;font-weight:600}.est{color:#8A5600}.note{background:#fbefd6;border-left:3px solid #8A5600;
padding:8px 12px}"""


def _cls(status: str) -> str:
    return status.split()[0].replace("/", "")[:4] if status else "NOT"


def _t(rows: list[list], head: list[str]) -> str:
    out = ["<table><tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in head) + "</tr>"]
    for r in rows:
        out.append("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>")
    return "".join(out) + "</table>"


def write_report(engine: Engine, path: Path, measurements: Optional[dict] = None,
                 decisions: Optional[dict] = None) -> Path:
    m = engine.model
    r = engine.results
    e = html.escape
    rs = AP.readiness_summary(engine, measurements, decisions)
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>K09-L readiness review</title>"
             f"<style>{CSS}</style></head><body><h1>K09-L Tailsitter - fabrication & readiness review</h1>",
             f"<p>Generated {time.strftime('%Y-%m-%d %H:%M')} · configuration <code>{m.config_hash()}</code> · "
             f"geometry <code>{m.geometry_hash()}</code> · {e(json.dumps(m.config))}</p>",
             "<p class='note'>Decision support, not certification. Values carry their evidence class: "
             + ", ".join(f"{SHORT[k]} = {v}" for k, v in CLASSES.items()) + ". Simulation is verification "
             "against models; only measurements validate.</p>"]
    parts.append(f"<h2>Readiness summary</h2><p><b>Stage:</b> {e(rs['stage'])}</p>" + _t(
        [[g, e(n), f"<span class='{_cls(s)}'>{e(s)}</span>"] for g, n, s in rs["gates"]], ["Gate", "Name", "Status"]))
    parts.append("<h3>Critical risks</h3><ul>" + "".join(f"<li>{e(x)}</li>" for x in rs["risks"]) + "</ul>")
    parts.append("<h3>Required next steps</h3><ol>" + "".join(f"<li>{e(x)}</li>" for x in rs["next_steps"]) + "</ol>")
    req = AP.requirements_table(engine, measurements)
    parts.append("<h2>Requirements</h2>" + _t([[x["id"], e(x["text"]), f"{x['op']} {x['threshold']:g} {e(x['unit'])}",
                                                "—" if x["predicted"] is None or isinstance(x["predicted"], list)
                                                else f"{x['predicted']:.4g}", SHORT.get(x["predicted_class"], ""),
                                                "—" if x["measured"] is None else f"{x['measured']:.4g}",
                                                f"<span class='{_cls(x['status'])}'>{e(x['status'])}</span>",
                                                e(x["why"]), e(x["action"])] for x in req],
                                              ["Req", "Requirement", "Criterion", "Predicted", "Evidence", "Measured",
                                               "Status", "Why", "Corrective action"]))
    G = AP.gates(engine, measurements, decisions)
    for g in G:
        parts.append(f"<h3>{g['id']} {e(g['name'])}: <span class='{_cls(g['status'])}'>{e(g['status'])}</span></h3>"
                     + _t([[e(c['text']), f"<span class='{_cls(c['status'])}'>{e(c['status'])}</span>",
                            e(c.get('why', ''))] for c in g["criteria"]], ["Criterion", "Status", "Why"])
                     + (f"<p>Decision: {e(json.dumps(g['decision']))}</p>" if g["decision"] else
                        "<p>No reviewer decision recorded.</p>"))
    key = ["mass_total", "cg_frac", "static_margin", "tw_full", "tw_low", "i_hover_battery", "endurance_hover",
           "motor_current_margin", "esc_margin", "battery_c_margin", "tipover_deg", "landing_n", "box_fit",
           "box_fit_fins_off", "mission_margin", "cost_total", "reserve"]
    if "motor_tube_sf" in r:
        key.append("motor_tube_sf")
    parts.append("<h2>Key engineering results</h2>" + _t(
        [[k, e(r[k].fmt(4)), SHORT.get(r[k].source, ""), e(r[k].ref), e(r[k].note)] for k in key if k in r],
        ["Quantity", "Value", "Evidence", "Method", "Note"]))
    heavy = [[e(engine.graph[n].label), e(engine.heavy_status(n)["status"]), e(engine.heavy_status(n)["reason"]),
              e((engine.heavy[n].get("record") or {}).get("config_hash", ""))] for n in engine.heavy]
    parts.append("<h2>Solver-backed analyses</h2>" + _t(heavy, ["Analysis", "Status", "Reason", "Config analysed"]))
    bom = AS.bill_of_materials(m)
    tot = sum(b["ext_price_usd"] or 0 for b in bom)
    parts.append(f"<h2>Bill of materials ({len(bom)} lines, ${tot:.2f} parts pre-tax)</h2>" + _t(
        [[e(b["name"]), e(b["pn"]), e(b["subassembly"]), b["qty"],
          "—" if b["ext_mass_kg"] is None else f"{b['ext_mass_kg'] * 1000:.1f}",
          "—" if b["unit_price_usd"] is None else f"{b['unit_price_usd']:.2f}",
          "—" if b["ext_price_usd"] is None else f"{b['ext_price_usd']:.2f}",
          f"<span class='{'est' if b['price_class'] == 'provisional_estimate' else ''}'>{SHORT.get(b['price_class'], '')}"
          f"</span>", e(b["vendor"])[:40]] for b in bom],
        ["Part", "P/N", "Subassembly", "Qty", "Mass g", "Unit $", "Ext $", "Price", "Vendor"]))
    subs = AS.subassemblies(m)
    parts.append("<h2>Assembly breakdown</h2>" + _t([[e(s["name"]), len(s["components"]),
                                                       e(", ".join(k.split(' — ')[0] for k in s["components"][:12]))]
                                                      for s in subs], ["Subassembly", "Lines", "Components"]))
    steps = AS.assembly_steps(m)
    parts.append("<h2>Assembly sequence</h2>" + _t([[s["step"], e(s["title"]), len(s.get("components", [])),
                                                      e(str(s.get("tools", "")))[:80], e(str(s.get("checks", "")))[:120]]
                                                     for s in steps], ["Step", "Title", "Parts", "Tools", "Checks"]))
    plan = MF.process_plan(m)
    parts.append("<h2>Manufacturing plan (times are estimates)</h2>" + _t(
        [[e(p["component"].split(" — ")[0]), p["qty"], e(p["process"]), e(p["material"]),
          "—" if p["time_per_unit"].value is None else f"{p['time_per_unit'].value:.2f} h",
          e("; ".join(p["risks"]))[:120]] for p in plan], ["Part", "Qty", "Process", "Material", "Time/unit", "Risks"]))
    wf = MF.fabrication_workflow()
    parts.append("<h2>Fabrication workflow</h2>" + _t([[e(w["stage"]), e(", ".join(w["depends_on"])), e(w["owner"]),
                                                         e(str(w["gate"]))] for w in wf],
                                                       ["Stage", "Depends on", "Owner", "Gate"]))
    tm = MF.test_matrix(m, r)
    parts.append("<h2>Verification matrix</h2>" + _t(
        [[t["id"], e(t["title"]), e(", ".join(t["requirement_ids"])), e(t["acceptance"]["text"])[:160],
          "NOT EVALUATED" if not measurements else "see requirements"] for t in tm],
        ["Test", "Title", "Requirements", "Acceptance", "Status"]))
    parts.append("<h2>Unresolved engineering inputs</h2><ul>" + "".join(
        f"<li><b>{e(a)}</b>: {e(b)}</li>" for a, b in m.unresolved) + "</ul></body></html>")
    path = Path(path)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path
