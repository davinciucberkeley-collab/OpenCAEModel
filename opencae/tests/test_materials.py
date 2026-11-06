"""Material Manager (0.3): sourced database, comparison, import/export, orthotropic print-frame
materials in CalculiX, temperature-dependent data, effective FDM properties, invalidation."""
import copy

import numpy as np
import pytest

from opencae.materials import database as db
from opencae.manufacturing.effective import core_stiffness_ratios, effective_material
from opencae.manufacturing.print_settings import PrintSettings
from opencae.project.model import Constraint, Feature, Load, Material
from opencae.geometry.references import find_planar_face, make_ref

MPa = 1e6


def test_database_records_are_valid_and_sourced():
    mats = db.database_materials()
    cats = {m.category for m in mats}
    for c in ("PLA", "PLA+", "PETG", "ABS", "ASA", "Nylon", "TPU", "Aluminium alloy", "Steel", "CFRP"):
        assert c in cats
    for m in mats:
        assert m.validate() == [], m.name
        assert m.source, m.name
        if not m.is_example:
            assert "poissons_ratio" in m.estimated  # no datasheet gives nu: always flagged
    pla = db.find("db_pla_um")
    assert pla.youngs_modulus == 3250 * MPa and pla.orthotropic["E3"] == 3071 * MPa
    assert db.min_strength(pla) == (33.1 * MPa, "tensile_break_z")  # interlayer is the weakest
    assert pla.thermal_expansion is None  # not given by the cited sources: not invented


def test_compare_and_import_export_roundtrip(tmp_path):
    mats = [db.find("db_pla_um"), db.find("db_petg_um")]
    rows = db.compare(mats)
    tab = {r[0]: r[1:] for r in rows}
    assert tab["Young's modulus [MPa]"] == pytest.approx([3250, 1939])
    assert tab["Specific stiffness E/ρ [MJ/kg]"][0] == pytest.approx(3250e6 / 1240 / 1e6)
    back = db.import_json(db.export_json(mats, tmp_path / "m.json"))
    assert [b.youngs_modulus for b in back] == [m.youngs_modulus for m in mats]
    assert back[0].orthotropic == mats[0].orthotropic and back[0].source == mats[0].source
    csv_back = db.import_csv(db.export_csv(mats, tmp_path / "m.csv"))
    assert csv_back[0].orthotropic["E3"] == pytest.approx(3071 * MPa)
    assert csv_back[1].density == pytest.approx(1270.0)
    (tmp_path / "bad.csv").write_text("name,E_MPa,nu,colour\nx,1,0.3,red\n")
    with pytest.raises(ValueError, match="unknown CSV columns"):
        db.import_csv(tmp_path / "bad.csv")


def test_orthotropic_admissibility_check():
    m = copy.deepcopy(db.find("db_pla_um"))
    m.orthotropic = dict(m.orthotropic, nu12=0.9, nu13=0.9, nu23=0.9)
    assert any("not physically admissible" in e for e in m.validate())


def _axial_bar(s, mat, L=100.0, a=10.0, F=1000.0):
    s.add_feature(Feature("primitive", {"shape": "box", "dx": L, "dy": a, "dz": a}))
    b = s.analysis_body()
    if mat.id not in [m.id for m in s.project.materials]:
        s.project.materials.append(mat)
    s.project.assign_material(b.id, mat.id)
    f = lambda n, p: make_ref(b, "face", find_planar_face(b, n, p))  # noqa: E731
    # uniaxial stress: symmetry on three orthogonal faces, axial force on the end
    s.project.constraints = [Constraint("symmetry", [f([-1, 0, 0], [0, 0, 0])], name="sx"),
                             Constraint("symmetry", [f([0, -1, 0], [0, 0, 0])], name="sy"),
                             Constraint("symmetry", [f([0, 0, -1], [0, 0, 0])], name="sz")]
    s.project.loads = [Load("force", [f([1, 0, 0], [L, 0, 0])], {"magnitude": F, "direction": [1, 0, 0]}, name="F")]
    s.project.mesh_settings.size_max = 10.0
    return b


def _tip(s, L=100.0):
    out, issues = s.run_solver()
    assert out and out.success, [str(i) for i in issues]
    return s.results.displacement[s.mesh.nodes[:, 0] > L - 1e-6, 0].mean()


@pytest.mark.solver
def test_orthotropic_print_frame_in_calculix(session):
    """Strongly orthotropic material: axial stiffness follows E1 (layer plane) or E3 (build axis)
    depending on the print orientation; delta = F L / (E A) exactly in uniaxial stress."""
    s = session
    m = Material("ortho test", 10e9, 0.3, 1000.0, id="mat_ortho", is_example=False,
                 orthotropic={"E1": 10e9, "E2": 10e9, "E3": 2e9, "nu12": 0.3, "nu13": 0.06, "nu23": 0.06,
                              "G12": 3.8e9, "G13": 1.5e9, "G23": 1.5e9})
    b = _axial_bar(s, m)
    s.generate_mesh()
    E1, E3 = 10e3, 2e3  # MPa
    issues, _ = s.validate()
    assert any("without a print orientation" in i.message for i in issues)  # never silent
    assert _tip(s) == pytest.approx(1000 * 100 / (E1 * 100), rel=1e-6)  # build axis = global Z
    s.project.study.print_settings = {b.id: {"level": 2, "build_axis": [1, 0, 0], "raster_axis": [0, 1, 0]}}
    s.project.study.print_settings[b.id]["infill_percent"] = 100.0
    assert not s.project.results_are_current()  # print settings are part of the setup
    assert _tip(s) == pytest.approx(1000 * 100 / (E3 * 100), rel=1e-6)  # bar printed upright: E3


@pytest.mark.solver
def test_material_change_invalidates_and_temperature_table(session):
    s = session
    m = Material("table test", 3e9, 0.3, 1200.0, id="mat_tab", is_example=False,
                 temperature_table=[{"T": 20.0, "E": 3e9}, {"T": 60.0, "E": 1e9}])
    _axial_bar(s, m)
    s.generate_mesh()
    assert _tip(s) == pytest.approx(1000 * 100 / (3e3 * 100), rel=1e-6)  # no operating temperature: base E
    assert s.project.results_are_current()
    s.project.analysis.operating_temperature = 40.0  # E(40 °C) = 2 GPa by linear interpolation
    assert not s.project.results_are_current()
    assert _tip(s) == pytest.approx(1000 * 100 / (2e3 * 100), rel=1e-6)
    s.project.materials[-1].youngs_modulus = 5e9
    s.project.materials[-1].temperature_table = []
    assert not s.project.results_are_current()  # editing the material makes the results out of date


def test_infill_is_not_a_linear_scale_factor():
    r_in, r_z, _ = core_stiffness_ratios("gyroid", 0.2)
    assert r_in == pytest.approx(0.04) and r_in < 0.2
    r_in, r_z, _ = core_stiffness_ratios("triangles", 0.3)
    assert r_in == pytest.approx(0.1) and r_z == pytest.approx(0.3)
    assert core_stiffness_ratios("grid", 1.0)[:2] == (1.0, 1.0)


def test_effective_material_levels_and_shell_fraction(session):
    s = session
    pla = db.find("db_pla_um")
    m1 = effective_material(pla, PrintSettings(level=1).to_dict())
    assert m1.orthotropic is None and m1.youngs_modulus == pla.orthotropic["E3"]  # lower modulus: screening
    assert m1.yield_strength == 33.1 * MPa  # weakest (interlayer) strength
    m2 = effective_material(pla, PrintSettings(level=2, build_axis=[0, 0, 1]).to_dict())
    assert m2.orthotropic["E1"] == 3250 * MPa and m2.orthotropic["E3"] == 3071 * MPa
    # shell fraction of a 100 x 20 x 10 box with 2 x 0.45 mm walls (exact inward offset)
    s.add_feature(Feature("primitive", {"shape": "box", "dx": 100, "dy": 20, "dz": 10}))
    b = s.analysis_body()
    ps = PrintSettings(level=1, infill_percent=20, infill_pattern="gyroid", perimeters=2, line_width=0.45)
    f = s.shell_fraction(b.id, ps.to_dict())
    assert f == pytest.approx(1 - 98.2 * 18.2 * 8.2 / 20000, rel=1e-9)
    m = effective_material(pla, ps.to_dict(), f)
    E3 = pla.orthotropic["E3"]
    assert m.youngs_modulus == pytest.approx(min(f * 3250e6 + (1 - f) * 0.04 * 3250e6, f * E3 + (1 - f) * 0.04 * E3))
    assert m.density == pytest.approx(1240 * (f + (1 - f) * 0.2))
    assert "Infill does not scale stiffness proportionally" in m.notes
    assert "levels 2 and 3 need the build (Z) direction" in " ".join(PrintSettings(level=2).validate())
