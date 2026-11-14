"""Component analysis on the K09-L engineering model: selection engine, rebuilt-solid FEA and its checks."""
from __future__ import annotations

import pytest

from opencae.engineering import component_analysis as CA
from opencae.engineering.model import EngineeringModel
from opencae.engineering.quantities import ESTIMATE


@pytest.fixture(scope="module")
def model():
    return EngineeringModel()


@pytest.fixture(scope="module")
def tube_ctx(model):
    return CA.analysis_context(model, CA.MOTOR_TUBE)


def test_dimensions_from_part_name():
    d = CA.parse_dimensions(CA.MOTOR_TUBE)
    assert (d["od"], d["id"], d["length"]) == (16.0, 13.08, 380.0)
    assert d["vendor"]["od"] == pytest.approx(0.625 * 25.4)
    assert CA.parse_dimensions(CA.SPAR_JOINER)["kind"] == "rod"


def test_motor_tube_context_selects_load_path(model, tube_ctx):
    ctx = tube_ctx
    inst = ctx["instance"]
    assert inst["side"].startswith("right")
    assert any(n.startswith("Prop M1") for n in inst["carries"])
    assert any(n.startswith("Prop M4") for n in inst["carries"])
    roles = {}
    for c in ctx["components"]:
        assert c["reason"], c
        roles.setdefault(c["role"], []).append(c["key"])
    assert roles["analysed part"] == [CA.MOTOR_TUBE]
    mounts = [c for c in ctx["components"] if c["key"].startswith("Motor mount")]
    assert len(mounts) == 2 and all(c["role"] == "load introduction" for c in mounts)
    assert any(k.startswith("Fin Depron") for k in roles["support path"])
    assert len([k for k in roles["support"] if k.startswith("Fin clip")]) == 4
    # supports and loads
    band = ctx["supports"][0]["s_range"]
    assert band[0] < 0 < band[1]
    names = [ld["name"] for ld in ctx["loads"]["max_thrust"]]
    assert {"M1 thrust (max)", "M4 thrust (max)", "M1 reaction torque", "M4 reaction torque"} <= set(names)
    T4 = ctx["load_inputs"]["thrust_max_full_total"]
    f = next(ld for ld in ctx["loads"]["max_thrust"] if ld["name"] == "M1 thrust (max)")
    assert f["value_N"] == pytest.approx(T4.value / 4)
    assert f["q"].source == ESTIMATE  # provisional prop coefficients: never shown as fact
    # landing: the ground contact does not pass through the tube; motor-set inertia does
    assert ctx["landing_in_load_path"] is False
    assert len(ctx["loads"]["landing"]) == 2
    # materials: CF laminate data missing -> estimated and registered as unresolved
    assert any("Carbon fibre" == m["material"] and m["status"] == ESTIMATE for m in ctx["materials"])
    assert any(u[0] == "CF tube laminate properties" for u in model.unresolved)
    # geometry cross-check against the mesh body
    g = ctx["geometry"]
    assert abs(g["diff_pct"]["od"]) < 1.0 and abs(g["diff_pct"]["length"]) < 0.5


def test_motor_option_changes_loads():
    m = EngineeringModel()
    a = CA.analysis_context(m, CA.MOTOR_TUBE)
    ok, why = m.set_option("motor", "X2814V3-900")
    assert ok, why
    b = CA.analysis_context(m, CA.MOTOR_TUBE)

    def val(ctx, name):
        return next(ld["value_N"] if "value_N" in ld else ld["value_Nmm"] for ld in ctx["loads"]["max_thrust"]
                    if ld["name"] == name)
    assert val(a, "M1 thrust (max)") != pytest.approx(val(b, "M1 thrust (max)"), rel=1e-3)
    assert val(a, "M1 reaction torque") != pytest.approx(val(b, "M1 reaction torque"), rel=1e-3)
    assert a["config_hash"] != b["config_hash"]


def test_cf_material_is_marked_estimated(model):
    mat = CA.cf_tube_material()
    assert mat.category == "Composite" and "yield_strength" in mat.estimated and "E3" in mat.estimated
    assert "estimate" in mat.source.lower()


@pytest.mark.solver
def test_motor_tube_fea_against_beam(model, tmp_path):
    r = CA.run_component_analysis(model, CA.MOTOR_TUBE, tmp_path)
    assert r["converged"] and not r["solver_errors"]
    assert r["timing_s"]["total"] < 180.0
    eq = r["equilibrium"]
    assert eq["force_residual_pct"] < 1.0
    assert eq["moment_residual_pct"] < 1.0
    assert sum(abs(x) for x in eq["applied_force_N"]) > 1.0
    assert len(r["beam_check"]) == 2  # both cantilever halves (M1 below, M4 above the fin clips)
    for side in r["beam_check"]:
        # 1.5 OD away from the rigid clamp edge (the clamp edge itself is a stress singularity)
        assert abs(side["diff_sigma_pct"]) < 10.0, side
        # Euler-Bernoulli plus the shear term of the (estimated, low) axial shear modulus
        assert abs(side["diff_defl_vs_EB_shear_pct"]) < 10.0, side
    assert r["max_von_mises_nodal_MPa"] > 0 and r["max_von_mises_integration_point_MPa"] > 0
    assert r["safety_factor"]["source"] == ESTIMATE  # estimated allowable -> estimate
    assert r["config_hash"] == model.config_hash() and r["fe_geometry_hash"]
    assert r["solver_log"].endswith("solver.log")


def test_no_template_for_unruled_component(model, tmp_path):
    with pytest.raises(NotImplementedError):
        CA.run_component_analysis(model, CA.SPAR_JOINER, tmp_path)
