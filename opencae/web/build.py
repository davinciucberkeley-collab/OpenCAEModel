"""Build the K09-L website: a static, self-contained front end generated from the engineering backend.

    python -m opencae.web.build [--study PATH] [--variant NAME] [--out DIR]

Output (open index.html directly from disk, or publish the folder):
    index.html          one page around one shared 3D viewer: Overview, Design (exploded view, electrical
                        routing, fabrication steps, cost map), Analyses (contours, critical regions, mesh
                        profiles, CFD flow), Flight (6-DOF replay at real scale), Components (checks and
                        suggestions per part), Decisions (gates, what to do to pass), Process (knowledge
                        graph, process flows), Audit (evidence tables)
    assets/             three.js r128, cytoscape.js 3.30, dagre 0.8 (all MIT) and the page's script and styles
    data/*.js           aircraft geometry, contour fields and the engineering summary as script files
                        (script files load from disk without a web server)
    img/                contour thumbnails rendered by the backend

Every number comes from the study (engine results, solver records, approval logic); nothing is typed in.
"""
from __future__ import annotations

import argparse
import base64
import json
import shutil
import time
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.engineering import approval as AP
from opencae.engineering import catalog as C
from opencae.engineering import field_analyses as FA
from opencae.engineering import study as ST
from opencae.engineering.graph import CURRENT

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = Path.home() / ".opencae" / "k09l_website"

STATUS = {AP.PASS: "pass", AP.COND: "cond", AP.FAIL: "fail", AP.NOT_EV: "nev"}
DISCIPLINE = {"fea_motor_tube": "structural", "fea_spar": "structural", "fea_foot": "structural",
              "thermal_mount": "thermal", "cfd_cruise": "fluid"}


def _b64(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()


def _js(name: str, obj) -> str:
    return f"window.{name} = " + json.dumps(obj, separators=(",", ":"), default=_default) + ";\n"


def _default(o):
    if isinstance(o, (np.floating, np.integer, np.bool_)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


# ------------------------------------------------------------------------------------------------ aircraft
def _part_key(c) -> str:
    return c.key


def aircraft_data(study: ST.Study, fields: dict) -> dict:
    """Every active body as an indexed mesh (mm, aircraft frame), grouped by BOM component; the rotors with
    their joint axes and the hover rpm of the simulation (for the spinning props)."""
    from opencae.ui.platform import scene as SC
    m = study.engine.model
    m.modified_bodies()
    analysed: dict[str, list[str]] = {}
    for fid, f in fields.items():
        pk = f.get("component") or f.get("part")
        if pk:
            analysed.setdefault(pk, []).append(fid)
    # the CFD covers the outer surfaces it modelled
    if "cfd_cruise" in fields:
        for c in m.components.values():
            if c.name.startswith(("Wing core", "Elevon", "Fin Depron", "Pod shell", "Pod hatch")):
                analysed.setdefault(c.key, [])
                if "cfd_cruise" not in analysed[c.key]:
                    analysed[c.key].append("cfd_cruise")
    rot = SC.rotor_groups(m)
    rot_of = {b.index: i for i, r in enumerate(rot) for b in r["bodies"]}
    parts, bodies = {}, []
    for b in m.active_bodies():
        c = m.components[b.component]
        P, T = m.body_mesh(b)
        pk = _part_key(c)
        if pk not in parts:
            parts[pk] = {"key": pk, "name": c.name, "pn": c.pn, "group": b.group, "qty": m.effective_qty(c),
                         "mass_g": round(m.component_mass(c).value * 1000, 2) if m.component_mass(c).value else None,
                         "mass_source": m.component_mass(c).source, "revision": c.revision,
                         "analyses": analysed.get(pk, []), "notes": (c.notes or "")[:400]}
        bodies.append({"part": pk, "name": b.name, "rgb": list(b.colour[:3]), "rotor": rot_of.get(b.index),
                       "pos": _b64(P.astype(np.float32)), "idx": _b64(T.astype(np.uint16 if b.vc < 65535 else np.uint32)),
                       "i32": b.vc >= 65535, "modified": b.index in m.modified_bodies()})
    hover = study.engine.heavy_status("flight_hover").get("record") or {}
    rpm = None
    try:
        from opencae.engineering import flightsim as FS
        s = FS.simulate(m, "hover", duration=2.0)
        rpm = [float(np.median(s.rpm[:, i])) for i in range(s.rpm.shape[1])]
    except Exception:  # the model still shows; props then spin at a nominal rate
        rpm = None
    return {"parts": parts, "bodies": bodies,
            "rotors": [{"name": r["name"], "origin": r["origin"].tolist(), "axis": r["axis"].tolist(),
                        "sense": r["sense"]} for r in rot],
            "hover_rpm": rpm, "spin_display": SC.SPIN_DISPLAY, "cg_mm": (np.asarray(study.engine.results["cg"].value)
                                                                        * 1000).tolist(),
            "hover_record_time": hover.get("time")}


# ------------------------------------------------------------------------------------------------ fields
def _field_sources(e):
    """(id, field summary, heavy record, group, component key) of every current contour field of the variant:
    the aircraft-level analyses and the per-component screening runs."""
    for nid in ST.FIELD_NODES:
        h = e.heavy_status(nid)
        if h["status"] != CURRENT:
            continue
        rec = h.get("record") or {}
        s = rec.get("field") if nid in ("fea_motor_tube", "fea_spar") else rec
        if s and s.get("field_name"):
            yield nid, s, rec, "aircraft", None
    if "component_screening" in e.heavy:
        h = e.heavy_status("component_screening")
        if h["status"] == CURRENT:
            rec = h.get("record") or {}
            for key, c in sorted((rec.get("components") or {}).items()):
                f = (c or {}).get("field")
                if f and f.get("field_name"):
                    yield f.get("id") or ("cs_" + str(abs(hash(key)) % 10 ** 8)), f, rec, "component", key


def field_data(study: ST.Study) -> tuple[dict, dict]:
    """The contour fields of the current variant (current records only): arrays + summaries. Arrays carry the
    primary field, the secondary fields (sec) and the nodal displacement vectors (disp) when the run has them."""
    e = study.engine
    out, summ = {}, {}
    for fid, s, rec, group, ckey in _field_sources(e):
        arr = FA.load_field(s)
        if arr is None:
            continue
        pts = np.asarray(arr["points_mm"], np.float32)
        tri = np.asarray(arr["tri"], np.uint32)
        d = {"pos": _b64(pts), "idx": _b64(tri), "val": _b64(np.asarray(arr["values"], np.float32)),
             "crit": _b64(np.asarray(arr["critical"], np.uint8)), "n": int(len(pts)),
             "sec": {k: _b64(np.asarray(v, np.float32)) for k, v in (arr.get("fields") or {}).items()}}
        if arr.get("disp") is not None:
            d["disp"] = _b64(np.asarray(arr["disp"], np.float32))
        out[fid] = d
        sm = {k: s.get(k) for k in ("id", "title", "discipline", "part", "quantity", "unit", "range",
                                    "worst_location_mm", "critical", "failure", "solver", "mesh", "load_case",
                                    "assumptions", "evidence", "extra", "time", "fields", "displacement", "flow")}
        sm["id"] = fid
        sm["group"], sm["component"] = group, ckey
        sm["record_time"] = rec.get("time")
        sm["node_hash"] = rec.get("node_hash")
        if sm.get("extra"):
            sm["extra"] = {k: v for k, v in sm["extra"].items() if k != "history"} | (
                {"history": sm["extra"].get("history")} if fid == "cfd_cruise" else {})
        summ[fid] = sm
    return out, summ


def render_thumb(arr: dict, summ: dict, path: Path) -> bool:
    try:
        import pyvista as pv
        pts = np.asarray(arr["points_mm"])
        tri = np.asarray(arr["tri"])
        mesh = pv.PolyData(pts, np.hstack([np.full((len(tri), 1), 3), tri]).ravel())
        mesh.point_data["v"] = np.asarray(arr["values"])
        p = pv.Plotter(off_screen=True, window_size=(720, 440))
        p.set_background("white")
        cmap = "coolwarm" if summ["discipline"] == "fluid" else ("YlOrRd" if summ["discipline"] == "thermal" else "turbo")
        p.add_mesh(mesh, scalars="v", cmap=cmap, show_scalar_bar=False, smooth_shading=True)
        # frame the part: look along its thinnest direction, tilted 30 deg so it reads as 3D, longest side horizontal
        ext = pts.max(0) - pts.min(0)
        order = np.argsort(ext)
        thin, long_ = order[0], order[2]
        d = np.zeros(3); d[thin] = 1.0; d[order[1]] = 0.55; d[long_] = 0.25
        up = np.zeros(3); up[order[1]] = 1.0
        up = up - d * (up @ d) / (d @ d)
        dn = d / np.linalg.norm(d)
        p.camera.up = up
        if ext[long_] > 4 * ext[order[1]] and summ.get("worst_location_mm"):
            # long thin part (tube, spar): a close-up of the critical end reads better than the whole length
            w = np.asarray(summ["worst_location_mm"], float)
            p.camera.focal_point = w
            p.camera.position = w + dn * float(ext[order[1]]) * 9
        else:
            c = pts.mean(0)
            p.camera.focal_point = c
            p.camera.position = c + dn * float(ext.max()) * 3
            p.reset_camera()
            p.camera.zoom(1.1)
        p.reset_camera_clipping_range()
        p.screenshot(str(path))
        p.close()
        from PIL import Image
        Image.open(path).convert("RGB").save(path.with_suffix(".jpg"), quality=82)
        path.unlink()
        return True
    except Exception:
        return False


# ------------------------------------------------------------------------------------------------ summary
def _plain_reason(h: dict) -> str:
    """One short sentence a reader without the engine's vocabulary understands."""
    st, why = h.get("status"), str(h.get("reason") or "")
    if st == CURRENT:
        return "up to date with this design"
    if st == "stale":
        return "out of date: the design changed after this run" + (f" ({why.split(':', 1)[-1].strip()})" if "changed" in why else "")
    if st == "failed":
        return "the last run failed: " + why[:160]
    return "not run for this design yet"


def summary_data(study: ST.Study, rid: str, fields: dict) -> dict:
    from opencae.engineering import manufacturing as MF
    from opencae.engineering import revision_b as RB
    e, m = study.engine, study.engine.model
    snaps = study.snapshots()
    study.select(rid)
    s = snaps[rid]
    base = study.baseline
    v = study.variant(rid)
    met = s["metrics"]

    def mv(k, sc=1.0, nd=1):
        q = met.get(k)
        return None if not q or q.get("value") is None else round(q["value"] * sc, nd)

    reqs = {r["id"]: r for r in s["requirements"]}
    kpis = [
        {"label": "Take-off mass", "value": mv("mass_total", 1000, 0), "unit": "g", "limit": "≤ 2 600 g", "req": "R1"},
        {"label": "Tip-over angle", "value": mv("tipover_deg", 1, 1), "unit": "°", "limit": "> 36° gust lean", "req": "R6"},
        {"label": "Fits the transport box", "value": mv("box_fit", 1, 3), "unit": "", "limit": "≥ 1", "req": "R5"},
        {"label": "Hover endurance", "value": mv("endurance_hover", 1 / 60, 1), "unit": "min", "limit": "mission 2 + 5 min",
         "req": "R4"},
        {"label": "Motor current margin", "value": mv("motor_current_margin", 1, 2), "unit": "", "limit": "≥ 1", "req": "R7"},
        {"label": "Budget reserve", "value": mv("reserve", 1, 0), "unit": "USD", "limit": "≥ $309", "req": "R3"},
    ]
    for k in kpis:
        k["status"] = STATUS.get(reqs.get(k["req"], {}).get("status"), "nev")
    analyses = []
    for nid, label in ST.HEAVY:
        h = s["heavy"][nid]
        f = (fields.get(nid) or {}).get("failure") or {}
        analyses.append({"id": nid, "label": label, "status": h["status"], "reason": _plain_reason(h),
                         "time": h.get("time"), "summary": h.get("summary"), "discipline": DISCIPLINE.get(nid, "system"),
                         "has_field": nid in fields, "result": f.get("text"), "verdict": f.get("status")})
    variants = []
    for r in study.ids():
        sn = snaps[r]
        variants.append({"id": r, "name": study.variant(r)["name"], "counts": {STATUS[k]: n for k, n in sn["counts"].items()},
                         "mass_g": None if not sn["metrics"].get("mass_total") else round(sn["metrics"]["mass_total"]["value"] * 1000),
                         "tipover": None if not sn["metrics"].get("tipover_deg") else round(sn["metrics"]["tipover_deg"]["value"], 1),
                         "box_fit": None if not sn["metrics"].get("box_fit") else round(sn["metrics"]["box_fit"]["value"], 3),
                         "current_margin": None if not sn["metrics"].get("motor_current_margin") else round(sn["metrics"]["motor_current_margin"]["value"], 2),
                         "static_margin": None if not sn["metrics"].get("static_margin") else round(sn["metrics"]["static_margin"]["value"] * 100, 2),
                         "analyses_current": sum(1 for h in sn["heavy"].values() if h["status"] == CURRENT),
                         "selected": r == rid, "baseline": r == base})
    estimates = [{"name": k, "value": d.get("value"), "unit": d.get("unit"), "source": d.get("source"), "ref": d.get("ref")}
                 for k, d in s["definition"]["params"].items() if d.get("source") in ("provisional_estimate", "missing")]
    tests = MF.test_matrix(m, e.results)
    rob = s.get("robustness") or []
    return {
        "generated": time.strftime("%Y-%m-%d %H:%M"), "study": study.title, "variant": v["name"],
        "variant_description": v.get("description", ""), "baseline": study.variant(base)["name"],
        "verdict": {"fail": s["counts"][AP.FAIL], "cond": s["counts"][AP.COND], "pass": s["counts"][AP.PASS],
                    "nev": s["counts"][AP.NOT_EV]},
        "kpis": kpis, "requirements": s["requirements"],
        "gates": [{"id": g["id"], "name": g["name"], "status": STATUS.get(g["status"], "nev"),
                   "criteria": [{"text": c["text"], "status": STATUS.get(c["status"], "nev"), "why": c.get("why", "")}
                                for c in g["criteria"]]} for g in s["gates"]],
        "analyses": analyses, "metrics": [{"key": k, "label": lab, "group": grp, "value": mv(k, sc, nd), "unit": un,
                                           "source": (met.get(k) or {}).get("source"), "ref": (met.get(k) or {}).get("ref")}
                                          for k, lab, grp, sc, un, nd, _ in ST.METRICS],
        "variants": variants, "diff": [list(x) for x in study.diff(rid, base)] if rid != base else [],
        "changes": RB.CHANGES if m.config.get("landing") == "feet_b" else [],
        "limits": RB.OPERATING_LIMITS if m.config.get("landing") == "feet_b" else [],
        "estimates": estimates, "unresolved": [{"item": a, "why": b} for a, b in m.unresolved],
        "tests": [{"id": t["id"], "title": t["title"], "reqs": t["requirement_ids"], "acceptance": t["acceptance"]["text"]}
                  for t in tests],
        "robustness": rob, "config": {k: C.option(m.catalog, k, m.config[k]).name for k in C.KINDS},
        "assembly": s["heavy"]["assembly_check"]["summary"],
    }


# ------------------------------------------------------------------------------------------------ extra
def extra_data(study: ST.Study, rid: str, fsum: dict) -> dict:
    """Components, remedies, gates, knowledge graph, process flows, fabrication, wiring, cost, flight tracks
    and mesh profiles (opencae.web.data_extra). A section that cannot be built carries {"error": ...}."""
    try:
        from opencae.web import data_extra as DX
    except ImportError as exc:  # the site still builds; the sections say there is no data
        return {"error": f"data_extra unavailable: {exc}"}
    return DX.site_extra(study, rid, fsum)


def flow_data(study: ST.Study) -> dict:
    """CFD flow visualisation (slice plane and streamlines) of the current cruise CFD record, if it has one."""
    h = study.engine.heavy_status("cfd_cruise")
    if h["status"] != CURRENT or not hasattr(FA, "load_flow"):
        return {}
    try:
        fl = FA.load_flow(h.get("record") or {})
    except Exception:  # noqa: BLE001 - an old record without flow arrays: no flow layer
        return {}
    if not fl:
        return {}
    out = {"vmax": float(fl.get("freestream", 0) or 0)}
    vmax = out["vmax"]
    slices = fl.get("slices") or ([dict(fl["slice"], name="slice")] if fl.get("slice") else [])
    out["slices"] = []
    for i, sl in enumerate(slices):
        sp = np.asarray(sl["speed"], np.float32)
        if not len(sp):
            continue
        vmax = max(vmax, float(np.percentile(sp, 99)))
        item = {"name": sl.get("name") or f"slice {i + 1}", "pos": _b64(np.asarray(sl["points_mm"], np.float32)),
                "idx": _b64(np.asarray(sl["tri"], np.uint32)), "speed": _b64(sp)}
        if sl.get("cp") is not None:
            item["cp"] = _b64(np.asarray(sl["cp"], np.float32))
        out["slices"].append(item)
    if fl.get("streamlines"):
        pts, spd, off = [], [], [0]
        for line in fl["streamlines"]:
            p = np.asarray(line["points_mm"], np.float32)
            if len(p) < 2:
                continue
            pts.append(p)
            spd.append(np.asarray(line["speed"], np.float32))
            off.append(off[-1] + len(p))
        if pts:
            out["streamlines"] = {"pos": _b64(np.concatenate(pts)), "speed": _b64(np.concatenate(spd)), "offsets": off}
    out["vmax"] = round(vmax * 1.05, 2) or 20.0
    return out


# ------------------------------------------------------------------------------------------------ build
def build_sites(study: Optional[ST.Study] = None, out: Path = DEFAULT_OUT, progress=None) -> Path:
    """The design-review site of revision B at ``out`` and, when the study has it, the recommended revision C
    at ``out/revC`` (sharing out/assets), cross-linked from the header."""
    study = study or ST.Study.open_default()
    names = [study.variant(r)["name"] for r in study.ids()]
    b = next((n for n in names if n.startswith("Revision B")), None)
    c = next((n for n in names if n.startswith("Revision C")), None)
    links = [{"name": b, "href": "index.html", "sub": ""}] + ([{"name": c, "href": "revC/index.html", "sub": "revC/"}] if c else [])
    p = build_site(study, out, b, progress, variant_links=links)
    if c:
        build_site(study, Path(out) / "revC", c, progress, variant_links=links, shared_assets="../assets/",
                   compare_with=b)
    return p


def build_site(study: Optional[ST.Study] = None, out: Path = DEFAULT_OUT, variant: Optional[str] = None,
               progress=None, variant_links: Optional[list] = None, shared_assets: Optional[str] = None,
               compare_with: Optional[str] = None) -> Path:
    study = study or ST.Study.open_default()
    rid = next((r for r in study.ids() if study.variant(r)["name"] == variant), None) if variant else None
    rid = rid or next((r for r in reversed(study.ids()) if study.variant(r)["name"].startswith("Revision B")),
                      study.current)
    cur = study.current
    out = Path(out)
    if out.exists():
        if shared_assets:
            shutil.rmtree(out)
        else:  # keep a nested variant site (revC/) only when it is rebuilt separately
            for sub in ("assets", "data", "img", "index.html"):
                q = out / sub
                if q.is_dir():
                    shutil.rmtree(q)
                elif q.exists():
                    q.unlink()
    out.mkdir(parents=True, exist_ok=True)
    (out / "assets").mkdir(exist_ok=True)
    (out / "data").mkdir()
    (out / "img").mkdir()
    for f in () if shared_assets else ("three.min.js", "OrbitControls.js", "THREE-LICENSE.txt", "cytoscape.min.js", "dagre.min.js",
              "cytoscape-dagre.js", "GRAPH-LICENSES.txt"):
        shutil.copyfile(HERE / "vendor" / f, out / "assets" / f)
    for f in () if shared_assets else ("app.js", "style.css"):
        shutil.copyfile(HERE / "templates" / f, out / "assets" / f)
    if shared_assets:
        (out / "assets").rmdir()
    if progress:
        progress("summary", 0, 4)
    study.select(rid)
    farr, fsum = field_data(study)
    summary = summary_data(study, rid, fsum)
    study.select(rid)
    if progress:
        progress("aircraft", 1, 4)
    air = aircraft_data(study, fsum)
    if progress:
        progress("thumbnails", 2, 4)
    for fid, src, _rec, _g, _k in _field_sources(study.engine):
        if fid not in fsum:
            continue
        arr = FA.load_field(src)
        if arr is not None and render_thumb(arr, fsum[fid], out / "img" / f"{fid}.png"):
            fsum[fid]["thumb"] = f"img/{fid}.jpg"
    drawings = []
    for src, title in (("sheet1_ga.svg", "General arrangement"), ("sheet2_exploded.svg", "Exploded assembly"),
                       ("sheet3_bom.svg", "Bill of materials"), ("sheet4_wiring.svg", "Wiring diagram")):
        p = HERE.parent / "examples" / "k09l" / src
        if p.exists():
            shutil.copyfile(p, out / "img" / src)
            drawings.append({"title": title, "src": f"img/{src}"})
    summary["drawings"] = drawings
    (out / "data" / "aircraft.js").write_text(_js("K09L_AIRCRAFT", air), encoding="utf-8")
    (out / "data" / "fields").mkdir(exist_ok=True)
    for fid, d in farr.items():  # one file per field, loaded when the field is opened
        (out / "data" / "fields" / f"{fid}.js").write_text(
            "window.K09L_FIELDS = window.K09L_FIELDS || {};\nwindow.K09L_FIELDS[" + json.dumps(fid) + "] = " +
            json.dumps(d, separators=(",", ":")) + ";\n", encoding="utf-8")
    if progress:
        progress("design data", 3, 4)
    study.select(rid)
    (out / "data" / "extra.js").write_text(_js("K09L_EXTRA", extra_data(study, rid, fsum)), encoding="utf-8")
    flow = flow_data(study)
    (out / "data" / "flow.js").write_text(_js("K09L_FLOW", {"cfd_cruise": flow} if flow else {}), encoding="utf-8")
    summary["variant_links"] = [{"name": v["name"], "href": (("../" + v["href"]) if shared_assets else v["href"]),
                                 "current": v["name"] == summary["variant"]} for v in (variant_links or [])]
    if compare_with or len(variant_links or []) > 1:
        try:
            from opencae.web import data_recs as DR
            rb = next(r for r in study.ids() if study.variant(r)["name"] == (variant_links[0]["name"]))
            rc = next(r for r in study.ids() if study.variant(r)["name"] == variant_links[1]["name"])
            (out / "data" / "recs.js").write_text(_js("K09L_RECS", DR.site_recs(study, rb, rc)), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - the page says the comparison is unavailable
            (out / "data" / "recs.js").write_text(_js("K09L_RECS", {"error": f"{type(exc).__name__}: {exc}"}), encoding="utf-8")
        study.select(rid)
    else:
        (out / "data" / "recs.js").write_text(_js("K09L_RECS", {}), encoding="utf-8")
    (out / "data" / "summary.js").write_text(_js("K09L_SUMMARY", summary) + _js("K09L_FIELD_INFO", fsum), encoding="utf-8")
    html = (HERE / "templates" / "index.html").read_text(encoding="utf-8")
    if shared_assets:
        html = html.replace('"assets/', '"' + shared_assets)
    html = html.replace("{{VARIANT}}", summary["variant"]).replace("{{GENERATED}}", summary["generated"])
    (out / "index.html").write_text(html, encoding="utf-8")
    if cur in study.ids():
        study.select(cur)
    if progress:
        progress("done", 4, 4)
    return out / "index.html"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--study", default=None, help="study file (default: your ~/.opencae study, or the bundled one)")
    ap.add_argument("--variant", default=None, help="variant name (default: Revision B)")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    a = ap.parse_args(argv)
    st = ST.Study.open(Path(a.study)) if a.study else ST.Study.open_default()
    pr = lambda w, i, n: print(f"  {w}")  # noqa: E731
    p = build_site(st, Path(a.out), a.variant, progress=pr) if a.variant else build_sites(st, Path(a.out), progress=pr)
    print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
