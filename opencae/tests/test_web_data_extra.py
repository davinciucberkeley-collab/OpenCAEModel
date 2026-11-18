"""site_extra (single-page website data) on the bundled K09-L study, revision B."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from opencae.engineering import study as ST
from opencae.web import build as B
from opencae.web import data_extra as DX

STUDY = Path(__file__).resolve().parents[1] / "examples" / "k09l" / "k09l_study.json"
KEYS = ["components", "remedies", "gates", "knowledge_graph", "process_flows", "fabrication", "explode", "wiring",
        "cost", "flight", "mesh_profiles"]
STATUSES = {"pass", "ok", "watch", "fail", "nev"}


@pytest.fixture(scope="module")
def built():
    st = ST.Study.open(STUDY)
    rid = next(r for r in st.ids() if st.variant(r)["name"] == "Revision B")
    st.select(rid)
    _, fsum = B.field_data(st)
    data = DX.site_extra(st, rid, fsum)
    return st, rid, fsum, data


def _part_refs(d):
    """Every component key the data references (fields that carry part keys by contract)."""
    refs = []
    for c in d["components"]:
        refs.append(c["key"])
    for r in d["remedies"]:
        refs += r["parts"]
    for f in d["process_flows"]:
        for s in f["steps"]:
            refs += s.get("parts") or []
    for s in d["fabrication"]["steps"]:
        refs += s["parts"]
    for s in d["fabrication"]["assembly"]:
        refs += s["parts"] + [x["key"] for x in s["fasteners"]]
    for n in d["wiring"]["nodes"]:
        if n["part"]:
            refs.append(n["part"])
    for lk in d["wiring"]["links"]:
        refs += lk["parts"] + ([lk["part"]] if lk["part"] else [])
    refs += d["wiring"]["harness_parts"]
    refs += [x["key"] for x in d["cost"]["top_items"]]
    refs += [n["part"] for n in d["knowledge_graph"]["nodes"] if n.get("part")]
    refs += [p["part"] for p in d["mesh_profiles"].values() if p["part"]]
    return refs


def test_schema_and_json(built):
    st, rid, fsum, d = built
    for k in KEYS:
        assert k in d, k
        assert not (isinstance(d[k], dict) and "error" in d[k]), (k, d[k])
    json.dumps(d, allow_nan=False)
    assert d["meta"]["seconds"] < 60
    c = d["components"][0]
    for k in ("key", "name", "pn", "subsystem", "qty", "mass_g", "mass_source", "unit_cost_usd", "cost_usd",
              "cost_source", "material", "process", "make_or_buy", "fab_time_min", "checks", "analyses", "status",
              "suggestions"):
        assert k in c
    for c in d["components"]:
        assert c["status"] in STATUSES and c["checks"]
        for x in c["checks"]:
            assert x["status"] in STATUSES
            assert set(x) >= {"name", "value", "unit", "limit", "status", "evidence", "analysis_id"}
    assert {"nodes", "edges"} <= set(d["knowledge_graph"])
    assert len(d["knowledge_graph"]["nodes"]) <= 150
    ids = {n["id"] for n in d["knowledge_graph"]["nodes"]}
    assert {"R1", "R12", "G1", "G6", "cfd_cruise", "propulsion"} <= ids
    assert all(e["source"] in ids and e["target"] in ids for e in d["knowledge_graph"]["edges"])
    assert {f["id"] for f in d["process_flows"]} == {"analysis", "design_change", "build", "verification"}
    assert {"total_usd", "budget_usd", "reserve_usd", "by_subsystem", "by_category", "top_items"} <= set(d["cost"])
    assert set(d["mesh_profiles"]) == set(fsum)


def test_part_keys_exist(built):
    st, rid, fsum, d = built
    comps = set(st.engine.model.components)
    bad = [k for k in _part_refs(d) if k not in comps]
    assert not bad, bad[:5]
    # components are exactly the BOM lines with active bodies
    m = st.engine.model
    act = {b.component for b in m.active_bodies()}
    assert {c["key"] for c in d["components"]} == act


def test_cfd_remedy(built):
    st, rid, fsum, d = built
    assert (fsum["cfd_cruise"]["failure"] or {}).get("status") == "fail"
    r = next(x for x in d["remedies"] if x["id"] == "cfd_cruise")
    assert r["kind"] == "analysis" and r["status"] == "fail"
    assert len(r["actions"]) >= 2
    assert any("faster" in a["action"].lower() for a in r["actions"])
    assert any(a.get("source") == DX.REEVAL for a in r["actions"])
    for a in r["actions"]:
        assert a["effort"] in ("low", "medium", "high")
    # every non-clean item has a remedy with at least one action
    assert all(x["actions"] for x in d["remedies"])
    req_ids = {x["id"] for x in d["remedies"] if x["kind"] == "requirement"}
    snap = st.snapshots()[rid]
    assert req_ids == {x["id"] for x in snap["requirements"] if x["status"] != "PASS"}


def test_gates_never_approve_flight(built):
    st, rid, fsum, d = built
    g = {x["id"]: x for x in d["gates"]}
    assert set(g) == {f"G{i}" for i in range(1, 7)}
    for gid in ("G5", "G6"):
        assert g[gid]["decision"] != "GO"
        assert g[gid]["decision"] in ("NOT READY", "GO WITH CONDITIONS", "NO-GO")
    assert g["G3"]["decision"] == "NO-GO"  # the CFD lift criterion fails
    for x in d["gates"]:
        assert x["decision"] in ("GO", "GO WITH CONDITIONS", "NO-GO", "NOT READY")
        if x["status"] != "pass":
            assert x["decision"] != "GO"


def test_flight_tracks(built):
    st, rid, fsum, d = built
    for name in ("hover", "transition"):
        tr = d["flight"][name]
        t = tr["t"]
        assert len(t) > 50
        assert all(b > a for a, b in zip(t, t[1:]))
        assert 20 <= tr["sample_hz"] <= 30
        n = len(t)
        for k in ("pos_mm", "quat", "m", "rpm"):
            assert len(tr[k]) == n, k
        assert all(math.isfinite(v) for p in tr["pos_mm"] for v in p)
        assert all(len(mm) == 16 and math.isfinite(sum(mm)) for mm in tr["m"])
        # m maps the CG (model frame, mm) to pos_mm
        cg = tr["cg_mm"]
        for i in (0, n // 2, n - 1):
            M = tr["m"][i]
            w = [sum(M[4 * r + c] * v for c, v in enumerate(cg + [1.0])) for r in range(3)]
            assert all(abs(a - b) < 0.5 for a, b in zip(w, tr["pos_mm"][i]))
        assert tr["world"]["up"] == "z" and len(tr["world"]["extent_mm"]) == 6
        assert tr["outcome"]
    assert d["flight"]["transition"]["outcome"].get("transition_converged") is not None


def test_explode_alignment(built):
    st, rid, fsum, d = built
    m = st.engine.model
    assert len(d["explode"]["body_vectors"]) == len(m.active_bodies())
    assert d["explode"]["body_index"] == [b.index for b in m.active_bodies()]


def test_plain_status():
    from opencae.engineering import approval as AP
    assert DX.plain_status(AP.PASS) == "pass"
    assert DX.plain_status(AP.FAIL) == "fail"
    assert DX.plain_status(AP.COND) == "ok"
    assert DX.plain_status(AP.NOT_EV) == "nev"
    assert DX.plain_status("watch") == "watch"
    assert DX.plain_status("stale") == "watch"
    assert DX.plain_status(None) == "nev"
