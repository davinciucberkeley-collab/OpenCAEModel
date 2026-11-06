"""Intelligent Mesh Manager (0.3): geometry analysis, presets sized from the geometry, cost
estimates, budget handling, recorded defeaturing and the automatic convergence loop."""
import math

import numpy as np
import pytest

from opencae.geometry.analysis import analyse_body
from opencae.geometry.references import find_planar_face, make_ref
from opencae.meshing import intelligent as im
from opencae.meshing import simplify
from opencae.project.model import Constraint, Feature, Load


def _plate(s, hole=True):
    a = s.add_feature(Feature("primitive", {"shape": "box", "dx": 100, "dy": 50, "dz": 5})).id
    if hole:
        c = s.add_feature(Feature("primitive", {"shape": "cylinder", "radius": 5, "height": 5,
                                                "position": [50, 25, 0]})).id
        s.add_feature(Feature("boolean", {"target": a, "tool": c, "op": "subtract"}))
    return s.analysis_body()


def test_geometry_analysis_measures_thickness_holes_and_reentrant_edges(session):
    b = _plate(session)
    rep = analyse_body(b)
    assert rep.min_thickness == pytest.approx(5.0, rel=1e-6)
    assert rep.holes and rep.holes[0][1] == pytest.approx(5.0)
    top = next(f for f in rep.faces if f.surface_type == "plane" and abs(f.area - (5000 - 25 * math.pi)) < 1e-6)
    assert top.thickness == pytest.approx(5.0, rel=1e-6)
    s2 = type(session)()
    a = s2.add_feature(Feature("primitive", {"shape": "box", "dx": 60, "dy": 20, "dz": 6})).id
    c = s2.add_feature(Feature("primitive", {"shape": "box", "dx": 6, "dy": 20, "dz": 40})).id
    s2.add_feature(Feature("boolean", {"target": a, "tool": c, "op": "union"}))
    rep2 = analyse_body(s2.analysis_body())
    assert len(rep2.concave_edges) == 1  # the inner corner of the L: a stress singularity


def test_presets_are_sized_from_the_geometry_not_fixed(session):
    b = _plate(session)
    rep = analyse_body(b)
    q, bal, val = (im.plan_mesh(rep, p, body=b) for p in ("quick", "balanced", "validation"))
    assert q.global_size == pytest.approx(rep.size / 12) and val.global_size == pytest.approx(rep.size / 40)
    hole = [ls for ls in bal.local if "hole" in ls.reason][0]
    assert hole.size == pytest.approx(2 * math.pi * 5 / 24)  # 24 elements around the hole
    thick = [ls for ls in bal.local if "wall thickness" in ls.reason][0]
    assert thick.size == pytest.approx(5.0 / 2)  # two quadratic elements through the 5 mm plate
    # the same plan scaled 10x: every size scales 10x
    s10 = type(session)()
    a = s10.add_feature(Feature("primitive", {"shape": "box", "dx": 1000, "dy": 500, "dz": 50})).id
    c = s10.add_feature(Feature("primitive", {"shape": "cylinder", "radius": 50, "height": 50,
                                              "position": [500, 250, 0]})).id
    s10.add_feature(Feature("boolean", {"target": a, "tool": c, "op": "subtract"}))
    b10 = s10.analysis_body()
    bal10 = im.plan_mesh(analyse_body(b10), "balanced", body=b10)
    assert bal10.global_size == pytest.approx(10 * bal.global_size)
    assert sorted(ls.size for ls in bal10.local) == pytest.approx(sorted(10 * ls.size for ls in bal.local))


def test_estimate_is_within_factor_two_of_gmsh(session):
    b = _plate(session)
    rep = analyse_body(b)
    for preset in ("quick", "balanced"):
        pl = im.plan_mesh(rep, preset, body=b)
        im.apply_plan(session, pl, b.id)
        m = session.generate_mesh()
        assert 0.5 < m.n_elements / pl.estimated_elements < 2.0, (preset, m.n_elements, pl.estimated_elements)


def test_budget_coarsens_and_records(session):
    b = _plate(session)
    rep = analyse_body(b)
    big = im.plan_mesh(rep, "validation", body=b)
    small = im.plan_mesh(rep, "validation", body=b, max_elements=big.estimated_elements // 8)
    assert small.global_size == pytest.approx(2 * big.global_size, rel=0.25)
    assert any("resource budget" in n for n in small.notes)
    assert im.check_budget(big, {"memory_mb": 1.0, "seconds": 1e9})  # warns when over budget
    mem, sec = im.solver_cost(204_000)
    assert mem == pytest.approx(2050, rel=0.15) and sec == pytest.approx(23, rel=0.25)  # measured calibration


def test_cfd_plan_wall_layers_from_flow(session):
    b = _plate(session, hole=False)
    rep = analyse_body(b)
    pl = im.plan_mesh(rep, "balanced", "cfd", body=b, wall_faces=[0, 1],
                      flow={"speed": 15.0, "nu": 1.46e-5, "length": 300.0, "turbulent": True, "y_plus": 1.0})
    re = 15.0 * 0.3 / 1.46e-5
    u_tau = 15.0 * math.sqrt(0.026 * re ** (-1 / 7) / 2)
    assert pl.bl_first == pytest.approx(1.46e-5 / u_tau * 1e3, rel=1e-9)  # y+ = 1, in mm
    assert pl.element_order == 1 and pl.bl_layers >= 3


def test_defeaturing_is_recorded_and_reversible(session):
    s = session
    a = s.add_feature(Feature("primitive", {"shape": "box", "dx": 100, "dy": 40, "dz": 10})).id
    h = s.add_feature(Feature("primitive", {"shape": "cylinder", "radius": 4, "height": 10,
                                            "position": [50, 20, 0]})).id
    s.add_feature(Feature("boolean", {"target": a, "tool": h, "op": "subtract"}))
    s.add_feature(Feature("fillet", {"body": a, "radius": 0.8, "edges": [0]}))
    b = s.analysis_body()
    v0, nf0 = b.volume, len(b.faces)
    cands = simplify.candidates(analyse_body(b))
    assert [c.face for c in cands] and all("convex fillet" in c.reason for c in cands)  # never the hole
    res = simplify.defeature(s, b.id, [c.face for c in cands], [c.reason for c in cands])
    # removing a 0.8 mm convex fillet on a 10 mm edge restores r^2 (1 - pi/4) x 10 of material
    assert res["volume_after"] - res["volume_before"] == pytest.approx(0.8 ** 2 * (1 - math.pi / 4) * 10, rel=1e-6)
    assert res["faces_after"] == nf0 - 1
    feat = next(f for f in s.project.geometry.features if f.kind == "defeature")
    assert feat.params["original"]["volume"] == pytest.approx(v0)
    feat.suppressed = True
    s.regenerate()
    assert s.analysis_body().volume == pytest.approx(v0) and len(s.analysis_body().faces) == nf0


@pytest.mark.solver
def test_automatic_convergence_loop_cantilever(session):
    """Tip deflection of a cantilever converges to Timoshenko beam theory; the loop stops on the
    criterion and reports order and GCI. A singular peak-stress monitor is flagged."""
    from opencae.postprocessing.convergence import check_monitors, run_auto
    s = session
    s.add_feature(Feature("primitive", {"shape": "box", "dx": 200, "dy": 20, "dz": 20}))
    b = s.analysis_body()
    s.project.assign_material(b.id, "mat_steel")
    f = lambda n, p: make_ref(b, "face", find_planar_face(b, n, p))  # noqa: E731
    s.project.constraints = [Constraint("fixed", [f([-1, 0, 0], [0, 0, 0])], name="root")]
    s.project.loads = [Load("force", [f([1, 0, 0], [200, 0, 0])], {"magnitude": 1000.0, "direction": [0, 0, -1]},
                            name="P")]
    s.project.mesh_settings.size_max = 20.0
    mons = [{"kind": "probe", "quantity": "U_z", "point": [200, 10, 10], "name": "tip U_z"}]
    res = run_auto(s, mons, tol=0.005, max_levels=5, budget={"memory_mb": 1e5, "seconds": 1e5})
    assert res["converged"], res["stop_reason"]
    assert len(res["sizes"]) >= 3
    tip = res["quantities"]["tip U_z"][-1]
    E, I, G, A = 200e3, 20 ** 4 / 12, 200e3 / 2.6, 400.0
    timo = 1000 * 200 ** 3 / (3 * E * I) + 1000 * 200 / (5 / 6 * G * A)
    assert -tip == pytest.approx(timo, rel=0.02)
    a = res["assessment"]["tip U_z"]
    assert a["gci"] is not None or any("no observed order or GCI" in w for w in res["warnings"])
    assert s.project.study.name != "(auto convergence)" and len(s.project.studies) == 1  # user's study unchanged
    assert check_monitors([{"kind": "summary", "key": "max_von_mises_nodal"}])
    # a tiny budget stops before the next level
    res2 = run_auto(s, mons, tol=1e-9, max_levels=6, budget={"memory_mb": 50.0, "seconds": 1e5})
    assert res2["stop_reason"].startswith("resource limit")
