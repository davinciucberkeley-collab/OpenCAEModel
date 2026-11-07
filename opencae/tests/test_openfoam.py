"""OpenFOAM adapter (0.3, priority 2): installation handling, log parsing, self-test, and
validation of complete studies (internal laminar pipe flow against Hagen-Poiseuille with an exact
momentum balance; external flow past a sphere with wall layers against the standard drag curve)."""
import os
from pathlib import Path

import numpy as np
import pytest

from opencae.cfd.openfoam.backend import OpenFOAMBackend, parse_check_mesh, parse_layers
from opencae.cfd.openfoam.case import refinement_level
from opencae.cfd.openfoam.install import FoamInstall, to_wsl_path, version_number
from opencae.geometry.references import find_planar_face, make_ref
from opencae.project.model import Constraint, Feature, Load, MeshRefinement


def test_paths_versions_and_levels():
    assert to_wsl_path("C:\\Users\\me\\proj") == "/mnt/c/Users/me/proj"
    assert to_wsl_path("/home/me/x") == "/home/me/x"
    assert version_number("v2412") == 2412 and version_number("") == 0
    assert refinement_level(6.0, 0.75) == 3 and refinement_level(6.0, 7.0) == 0
    c = FoamInstall("conda", "/opt/env with space")
    assert "'/opt/env with space'" in c.prelude()


def test_log_parsing_and_known_failures(tmp_path):
    chk = parse_check_mesh("    cells:            12352\n    Mesh non-orthogonality Max: 33.3 average: 7\n"
                           "    Max skewness = 0.50 OK.\n\nMesh OK.\n")
    assert chk["ok"] and chk["cells"] == 12352 and chk["max_non_ortho"] == pytest.approx(33.3)
    bad = parse_check_mesh(" ***Max skewness = 5.2, 3 highly skew faces detected\nFailed 1 mesh checks.\n")
    assert not bad["ok"] and bad["failed"] == 1 and bad["messages"]
    new = ("x\n\npatch faces        layers        overall thickness\n               target   mesh     [m]       [%]\n"
           "----- -----    -----    ----     ---       ---\nwalls 4464     6        4.5      0.000496  75     \n\n")
    old = ("x\n\npatch faces    layers   overall thickness\n                         [m]       [%]\n"
           "----- -----    ------   ---       ---\nwall  100      3.2      0.001     80\n\n")
    assert parse_layers(new) == [{"patch": "walls", "faces": 4464, "target": 6.0, "layers": 4.5,
                                  "thickness_m": 0.000496, "thickness_pct": 75.0}]
    assert parse_layers(old)[0]["layers"] == pytest.approx(3.2)
    be = OpenFOAMBackend.__new__(OpenFOAMBackend)
    ok, errs, _ = be.analyse('--> FOAM FATAL IO ERROR: \nerror in IOstream "sha1" for operation x\n\nFOAM exiting',
                             1, tmp_path, "j")
    assert not ok and any("function objects" in e for e in errs)
    ok, errs, _ = be.analyse("sigFpe : Enabling floating point exception trapping (FOAM_SIGFPE).\nEnd\n", 0,
                             tmp_path, "j")
    assert ok and not errs  # the banner is not a crash


@pytest.mark.openfoam
def test_selftest_passes():
    be = OpenFOAMBackend(os.environ.get("OPENCAE_OPENFOAM", ""))
    rep = be.selftest()
    assert rep.ok, rep.summary()
    assert be.verified and "function objects work" in rep.summary()


def _pipe(s, solver="openfoam"):
    s.add_feature(Feature("primitive", {"shape": "cylinder", "radius": 10, "height": 300}))
    b = s.analysis_body()
    s.new_study("pipe", "cfd_incompressible", [b.id])
    a = s.project.analysis
    a.cfd_solver = solver
    a.fluid_density, a.fluid_viscosity, a.flow_model, a.cfd_iterations = 1.2, 1.8e-5, "laminar", 1500
    fin = make_ref(b, "face", find_planar_face(b, [0, 0, -1], [0, 0, 0]))
    fout = make_ref(b, "face", find_planar_face(b, [0, 0, 1], [0, 0, 300]))
    s.project.loads = [Load("velocity_inlet", [fin], {"magnitude": 0.05, "direction": "normal"}, name="inlet"),
                       Load("pressure_outlet", [fout], {"magnitude": 0.0}, name="outlet")]
    wall = next(f.index for f in b.faces if f.surface_type != "plane")
    ms = s.project.mesh_settings
    ms.size_max = 2.5
    ms.refinements = [MeshRefinement([make_ref(b, "face", wall)], 0.7, name="wall")]
    return b


def test_layer_heuristics_are_warnings(session):
    """Wall layers snappyHexMesh is likely to drop are reported, and the settings are left as given."""
    from opencae.cfd.openfoam.study import build
    _pipe(session)
    ms = session.project.mesh_settings  # wall cell = 2.5 / 2^2 = 0.625
    ms.bl_layers, ms.bl_first, ms.bl_growth = 5, 0.01, 1.3
    _, issues = build(session, None, write=False)
    assert any("under 5% of the wall cell" in str(i) for i in issues)
    ms.bl_layers, ms.bl_first = 12, 0.05
    _, issues = build(session, None, write=False)
    assert any("more than the wall cell size" in str(i) for i in issues)
    ms.bl_layers, ms.bl_first = 4, 0.05
    _, issues = build(session, None, write=False)
    assert not any("wall cell" in str(i) for i in issues)
    assert (ms.bl_layers, ms.bl_first, ms.bl_growth) == (4, 0.05, 1.3)


@pytest.mark.openfoam
def test_laminar_pipe_flow_end_to_end(session, tmp_path):
    """D = 20 mm, Re = 67: developed pressure gradient 8 mu U / R^2 and an exact momentum balance:
    wall shear force = pressure force - change of momentum flux (uniform inlet -> parabolic outlet,
    momentum-flux factor 4/3)."""
    from opencae.postprocessing.sampling import sample_line
    s = session
    b = _pipe(s)
    m = s.generate_mesh()
    assert m.source == "openfoam" and m.meta["check"]["ok"]
    issues, _ = s.validate()
    assert not [i for i in issues if i.severity == "error"], [str(i) for i in issues]
    out, iss = s.run_solver()
    assert out and out.success, (out.errors if out else [str(i) for i in iss])
    sm = s.project.results.summary
    assert sm["converged"]
    mu, U, R, rho = 1.8e-5, 0.05, 0.01, 1.2
    d, p, inside = sample_line(s.mesh, s.results.quantity("P"), [0, 0, 120], [0, 0, 280], 81)
    k = np.polyfit(d[inside] * 1e-3, p[inside], 1)[0]
    assert k == pytest.approx(-8 * mu * U / R ** 2, rel=0.03)
    assert sm["flow_balance_rel"] < 0.01
    Q = sm["inflow_m3s"]
    momentum = sm["pressure_drop_Pa"] * np.pi * R ** 2 - rho * Q * (Q / (np.pi * R ** 2)) * (4 / 3 - 1)
    assert sm["wall_force_N"][2] == pytest.approx(momentum, rel=0.02)
    # changing the flow conditions invalidates the OpenFOAM mesh (its patches no longer match)
    s.project.loads[1].kind = "slip_wall"
    s.project.constraints = []
    issues, _ = s.validate()
    assert any("patches no longer match" in i.message for i in issues)
    s.project.loads[1].kind = "pressure_outlet"
    # saved and reopened: results still current
    s.save(tmp_path / "pipe.ocae")
    from opencae.app.session import Session
    s2 = Session()
    s2.open(tmp_path / "pipe.ocae")
    assert s2.project.results_are_current() and s2.results is not None


@pytest.mark.openfoam
def test_sphere_drag_with_wall_layers():
    """Sphere at Re = 20 (flow-domain tool, slip walls 8 D from the centre), 6 prism layers:
    Cd vs Pruppacher et al.'s fit to numerical solutions, Cd = 24/Re (1 + 0.115 Re^0.802)."""
    from opencae.app.session import Session
    s = Session()
    s.new("sphere")
    s.add_feature(Feature("primitive", {"shape": "sphere", "radius": 5.0}))
    sph = s.analysis_body()
    st = s.create_flow_domain([sph.id], "+x", 0.02, 8.0, 16.0, 8.0)
    a = st.analysis
    a.cfd_solver = "openfoam"
    a.fluid_density, a.fluid_viscosity, a.cfd_iterations = 1000.0, 0.01, 2000
    D = 0.01
    a.ref_area = np.pi * D ** 2 / 4
    dom = s.bodies[st.body_ids[0]]
    ms = st.mesh_settings
    ms.size_max = 6.0
    ms.refinements = [MeshRefinement([make_ref(dom, "face", f.index) for f in dom.faces
                                      if f.surface_type == "sphere"], 0.3, name="sphere")]
    ms.bl_layers, ms.bl_first, ms.bl_growth = 6, 0.05, 1.2
    m = s.generate_mesh()
    assert m.meta["layers"] and m.meta["layers"][0]["thickness_pct"] > 90
    out, iss = s.run_solver()
    assert out and out.success, (out.errors if out else [str(i) for i in iss])
    sm = s.project.results.summary
    Re = 1000.0 * 0.02 * D / 0.01
    ref = 24 / Re * (1 + 0.115 * Re ** 0.802)
    assert sm["converged"]
    assert sm["force_coefficients"][0] == pytest.approx(ref, rel=0.03)
    assert abs(sm["force_coefficients"][1]) < 0.01 * ref
    assert sm["flow_balance_rel"] < 1e-3

