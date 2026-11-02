"""Unit tests: units, project IO, materials, results math, deck formatting."""
import json
import math

import numpy as np
import pytest

from opencae.materials.library import builtin_materials, material_in_units
from opencae.postprocessing.results import principal, von_mises
from opencae.project import io as pio
from opencae.project.model import Constraint, EntityRef, Feature, Load, Project
from opencae.solver.calculix.deck import MAX_FIELD, _fmt
from opencae.utilities.units import UNIT_SYSTEMS, check_consistency, get_unit_system


@pytest.mark.parametrize("key", list(UNIT_SYSTEMS))
def test_unit_systems_consistent(key):
    assert check_consistency(get_unit_system(key))


def test_material_conversion():
    steel = builtin_materials()[0]
    mm = material_in_units(steel, get_unit_system("mm-N-s"))
    si = material_in_units(steel, get_unit_system("m-N-s"))
    assert mm["E"] == pytest.approx(200e3)  # MPa
    assert si["E"] == pytest.approx(200e9)  # Pa
    assert mm["rho"] == pytest.approx(7.85e-9)  # t/mm^3
    assert all(not m.validate() for m in builtin_materials())
    assert all(m.is_example for m in builtin_materials())


def test_material_validation_rejects_bad_values():
    m = builtin_materials()[0]
    m.poissons_ratio = 0.5
    m.youngs_modulus = -1
    errs = m.validate()
    assert any("Poisson" in e for e in errs) and any("Young" in e for e in errs)


def test_project_roundtrip(tmp_path):
    p = Project(name="RT")
    p.materials = builtin_materials()
    f = Feature("primitive", {"shape": "box", "dx": 1.0, "dy": 2.0, "dz": 3.0}, name="b")
    p.geometry.features.append(f)
    p.constraints.append(Constraint("fixed", [EntityRef("face", f.id, 0, {"area": 6.0})], name="fx"))
    p.loads.append(Load("force", [EntityRef("face", f.id, 1)], {"magnitude": 5.0, "direction": [0, 0, -1]}))
    path = pio.save_project(p, tmp_path / "rt.ocae")
    q = pio.load_project(path)
    assert pio.project_to_dict(q) == pio.project_to_dict(p)
    assert isinstance(q.constraints[0].targets[0], EntityRef)
    assert q.geometry.revision_hash() == p.geometry.revision_hash()


def test_project_schema_guard(tmp_path):
    doc = pio.project_to_dict(Project())
    doc["schema_version"] = pio.SCHEMA_VERSION + 1
    (tmp_path / "x.ocae").write_text(json.dumps(doc))
    with pytest.raises(pio.ProjectFormatError):
        pio.load_project(tmp_path / "x.ocae")
    (tmp_path / "y.ocae").write_text(json.dumps({"format": "other"}))
    with pytest.raises(pio.ProjectFormatError):
        pio.load_project(tmp_path / "y.ocae")


def test_von_mises_and_principal():
    # uniaxial
    s = np.array([[100.0, 0, 0, 0, 0, 0]])
    assert von_mises(s)[0] == pytest.approx(100.0)
    # pure shear tau -> vm = sqrt(3) tau, principals (tau, 0, -tau)
    s = np.array([[0, 0, 0, 50.0, 0, 0]])
    assert von_mises(s)[0] == pytest.approx(50 * math.sqrt(3))
    assert principal(s)[0] == pytest.approx([50, 0, -50])
    # hydrostatic -> vm = 0
    assert von_mises(np.array([[7.0, 7, 7, 0, 0, 0]]))[0] == pytest.approx(0, abs=1e-12)
    # random tensor: principals are invariant (trace preserved)
    r = np.random.default_rng(1).normal(size=(20, 6))
    assert np.allclose(principal(r).sum(axis=1), r[:, :3].sum(axis=1))


@pytest.mark.parametrize("v", [-1.93089988442807e-12, 1e-300, -1.234567890123456e-100, 189.607695154586,
                               0.0, 123456789.123456789, -0.1 + 1e-17, 7.85e-09])
def test_deck_number_fits_calculix_field(v):
    s = _fmt(v)
    assert len(s) <= MAX_FIELD
    assert float(s) == pytest.approx(v, rel=1e-12, abs=0)


def test_no_short_hex_colours_in_ui():
    """PyVista accepts only 6/8-digit hex strings; 3-digit ones crash at runtime."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    bad = []
    for p in list((root / "ui").glob("*.py")) + list((root / "visualization").glob("*.py")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"(color|colour)\w*\s*=\s*[\"']#[0-9a-fA-F]{3}[\"']", line):
                bad.append(f"{p.name}:{i}")
    assert not bad, bad
