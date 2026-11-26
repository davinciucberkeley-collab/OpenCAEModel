"""Flow studies run on OpenFOAM only: the solver choice, old projects that name a retired flow solver, and the
2-D mode that OpenFOAM does not offer yet. (The solver runs against exact laminar solutions are in test_openfoam.py.)"""
import json

import pytest

from opencae.app.session import Session
from opencae.geometry.references import find_planar_face, make_ref
from opencae.project.io import LEGACY_FLOW_SOLVER_NOTE, project_from_dict, project_to_dict
from opencae.project.model import AnalysisSettings, Feature, Load


def _duct():
    s = Session()
    s.new("flow")
    s.add_feature(Feature("primitive", {"shape": "box", "dx": 10, "dy": 10, "dz": 50}))
    b = s.analysis_body()
    a = s.project.analysis
    a.analysis_type = "cfd_incompressible"
    f = lambda n, p: make_ref(b, "face", find_planar_face(b, n, p))  # noqa: E731
    s.project.loads = [Load("velocity_inlet", [f([0, 0, -1], [0, 0, 0])], {"magnitude": 0.01, "direction": "normal"},
                            name="Inlet"),
                       Load("pressure_outlet", [f([0, 0, 1], [0, 0, 50])], {"magnitude": 0.0}, name="Outlet")]
    return s


def test_flow_studies_use_openfoam_and_default_to_it():
    assert AnalysisSettings().cfd_solver == "openfoam"
    s = _duct()
    assert s.is_openfoam_study()
    be = s.solver_backend()
    assert type(be).__name__ == "OpenFOAMBackend"
    from opencae.physics import physics_for
    assert physics_for("cfd_incompressible").solver == "openfoam"


def test_structural_study_is_not_a_flow_study():
    s = Session()
    s.new("beam")
    assert not s.is_openfoam_study()


def test_two_d_flow_is_reported_as_unavailable_with_openfoam():
    s = _duct()
    s.project.analysis.cfd_dimension = "2d"
    from opencae.cfd.openfoam import study as foam_study
    _, issues = foam_study.build(s, None, write=False)
    assert any("2-D flow is not available with OpenFOAM yet" in i.message and i.severity == "error"
               for i in issues), issues


def test_old_project_naming_the_retired_flow_solver_loads_as_openfoam():
    s = _duct()
    doc = json.loads(json.dumps(project_to_dict(s.project)))
    for st in doc["project"]["studies"]:
        st["analysis"]["cfd_solver"] = "su2"
    p = project_from_dict(doc)
    for st in p.studies:
        assert st.analysis.cfd_solver == "openfoam"
    assert LEGACY_FLOW_SOLVER_NOTE in p.study.notes
    # structural studies carrying the old default are switched silently (no flow note)
    doc2 = json.loads(json.dumps(project_to_dict(Session().project)))
    for st in doc2["project"]["studies"]:
        st["analysis"]["cfd_solver"] = "su2"
    p2 = project_from_dict(doc2)
    assert p2.study.analysis.cfd_solver == "openfoam" and LEGACY_FLOW_SOLVER_NOTE not in p2.study.notes


def test_old_file_without_a_solver_key_loads():
    s = _duct()
    doc = json.loads(json.dumps(project_to_dict(s.project)))
    for st in doc["project"]["studies"]:
        st["analysis"].pop("cfd_solver", None)
    assert project_from_dict(doc).study.analysis.cfd_solver == "openfoam"
