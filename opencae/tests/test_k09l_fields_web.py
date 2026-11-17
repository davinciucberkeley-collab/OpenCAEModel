"""Field analyses (foot FEA, mount thermal, cruise CFD), the spinning props and the design-review website."""
import json
import math
import re

import numpy as np
import pytest

from opencae.engineering import assembly as AS
from opencae.engineering import field_analyses as FA
from opencae.engineering import revision_b as RB
from opencae.engineering import study as ST
from opencae.engineering.graph import Engine


@pytest.fixture(scope="module")
def eng_b():
    e = Engine()
    RB.apply(e)
    return e


# ------------------------------------------------------------------------------------------- props spin
def test_every_prop_turns_freely(eng_b):
    sweep = AS.rotor_sweep(eng_b.model, step_deg=10)
    assert len(sweep) == 4
    for r in sweep:
        assert r["min_clearance_mm"] > 0, r["text"]  # no strike anywhere in the revolution
    assert min(r["min_clearance_mm"] for r in sweep) >= AS.PROP_CLEARANCE_MIN_MM


def test_rotor_angles_integrate_rpm():
    from opencae.ui.platform import scene as SC
    t = np.linspace(0, 2, 201)
    rpm = np.full((len(t), 4), 6000.0)
    a = SC.rotor_angles(t, rpm)
    assert a.shape == (201, 4)
    assert np.all(np.diff(a[:, 0]) >= 0)
    assert a[-1, 0] == pytest.approx(6000 * 2 * math.pi / 60 * 2 * SC.SPIN_DISPLAY, rel=1e-6)


def test_spin_matrix_rotates_about_the_joint_line():
    from opencae.ui.platform import scene as SC
    o, ax = np.array([10.0, 20.0, 30.0]), np.array([1.0, 0, 0])
    M = SC.spin_matrix(o, ax, math.pi / 2)
    on_axis = M @ np.r_[o + 5 * ax, 1]
    assert np.allclose(on_axis[:3], o + 5 * ax)  # the axis itself does not move
    p = M @ np.r_[o + [0, 1, 0], 1]
    assert np.allclose(p[:3], o + [0, 0, 1], atol=1e-12)


def test_rotor_groups_carry_the_props(eng_b):
    from opencae.ui.platform import scene as SC
    g = SC.rotor_groups(eng_b.model)
    assert len(g) == 4
    for r in g:
        assert r["bodies"] and abs(np.linalg.norm(r["axis"]) - 1) < 1e-9


def test_g3_has_the_prop_sweep_and_field_criteria():
    from opencae.engineering import approval as AP
    src = open(AP.__file__, encoding="utf-8").read()
    assert "Props turn freely" in src
    for nid in ("fea_foot", "thermal_mount", "cfd_cruise"):
        assert nid in src


# ------------------------------------------------------------------------------------------- field runs
def test_critical_region_is_never_a_single_node():
    v = np.r_[np.zeros(999), 100.0]  # one singular node
    c = FA._critical(v, None)
    assert c["node_mask"].sum() >= 1
    v = np.linspace(0, 1, 1000)
    c = FA._critical(v, None, frac=0.99)
    assert c["node_mask"].sum() >= 30  # at least the worst 3 %


@pytest.mark.solver
def test_foot_fea_contour(eng_b, tmp_path):
    rec = FA.run_foot_fea(eng_b, tmp_path / "foot", size=3.0)
    f = rec["failure"]
    assert f["safety_factor"] > 1.5 and f["status"] in ("ok", "watch")
    sf = rec["surface"]
    assert len(sf["values"]) == len(sf["points_mm"]) and rec["critical"]["node_mask"].any()
    s = FA.save_field(rec, tmp_path / "fields")
    arr = FA.load_field({**s, "field_file": str(tmp_path / "fields" / s["field_name"])})
    assert arr is not None and len(arr["values"]) == len(sf["values"])
    json.dumps(s)  # the recorded summary is JSON-safe


@pytest.mark.solver
def test_mount_thermal_petg_fails_asa_passes(tmp_path):
    e = Engine()
    RB.apply(e)
    e.set_option("mount_material", "petg")
    petg = FA.run_mount_thermal(e, tmp_path / "petg")
    e.set_option("mount_material", "asa")
    asa = FA.run_mount_thermal(e, tmp_path / "asa")
    assert petg["failure"]["margin"] < 0 <= asa["failure"]["margin"]
    assert asa["failure"]["value"] == pytest.approx(petg["failure"]["value"], abs=3.0)  # same heat, new limit


@pytest.mark.openfoam
@pytest.mark.slow
def test_cfd_cruise_runs_and_reports_lift(eng_b, tmp_path):
    rec = FA.run_cfd_cruise(eng_b, tmp_path / "cfd", iterations=30, procs=1)
    ex = rec["extra"]
    assert 0 < ex["cl"] < 2 and ex["cd"] > 0
    assert rec["failure"]["status"] in ("ok", "watch", "fail")
    assert "coarse" in rec["mesh"]["note"]  # never presented as a converged, validated result


# ------------------------------------------------------------------------------------------- website
def test_website_build(tmp_path):
    from opencae.web import build as WB
    st = ST.Study.open_default()
    p = WB.build_site(st, tmp_path / "site")
    html = p.read_text(encoding="utf-8")
    assert "{{" not in html
    for f in ("assets/three.min.js", "assets/OrbitControls.js", "assets/app.js", "assets/style.css",
              "data/summary.js", "data/aircraft.js", "data/fields.js"):
        assert (p.parent / f).is_file(), f
    js = (p.parent / "data" / "summary.js").read_text(encoding="utf-8")
    m = re.match(r"window\.K09L_SUMMARY\s*=\s*(\{.*?\});\n", js, re.S)
    summary = json.loads(m.group(1))
    assert summary["variant"].startswith("Revision B")
    assert {a["id"] for a in summary["analyses"]} == {n for n, _ in ST.HEAVY}
    for a in summary["analyses"]:
        assert "never run" not in (a["reason"] or "") and "affected by" not in (a["reason"] or "")
    air = (p.parent / "data" / "aircraft.js").read_text(encoding="utf-8")
    assert '"rotors"' in air
