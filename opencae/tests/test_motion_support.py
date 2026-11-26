"""Pieces the Motion mode shares with the rest of the app. None of these needs MuJoCo or a display."""
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from opencae.app.session import Session
from opencae.examples import example_copy
from opencae.examples.mbd_examples import GRIPPER as G
from opencae.examples.mbd_examples import build_gripper, gripper_gap_mm, inner_tip_pairs, tip_gap_from_poses
from opencae.utilities import units


# ---- one example-copy helper (the Motion workspace and the console call the same function)
def test_console_uses_the_shared_example_copy():
    from opencae.ui.commands import actions
    assert actions.copy_example is example_copy.copy_example
    assert actions.examples_dir is example_copy.examples_dir and actions.projects_home is example_copy.projects_home
    src = Path(actions.__file__).read_text()
    assert "def copy_example" not in src and "def examples_dir" not in src


def test_copy_example_copies_once_into_opencae_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCAE_HOME", str(tmp_path))
    dst = example_copy.copy_example("gripper")
    assert dst == tmp_path / "projects" / "gripper" / "gripper.ocae" and dst.exists()
    dst.write_text("edited")
    assert example_copy.copy_example("gripper").read_text() == "edited"  # never overwritten
    with pytest.raises(FileNotFoundError):
        example_copy.copy_example("nope")


# ---- one unit table
def test_grammar_uses_the_shared_unit_table():
    from opencae.ui.commands import grammar
    assert grammar.UNITS is units.UNIT_TABLE
    assert units.UNIT_TABLE["rpm"] == ("speed", units.RPM_TO_RAD_S) and units.UNIT_TABLE["deg"][1] == units.DEG_TO_RAD
    assert units.RPM_TO_RAD_S * units.RAD_S_TO_RPM == pytest.approx(1.0, rel=1e-15)
    assert 60.0 * units.RPM_TO_RAD_S == pytest.approx(2.0 * math.pi, rel=1e-15)
    assert 180.0 * units.DEG_TO_RAD == pytest.approx(math.pi, rel=1e-15)
    assert units.DEG_S_TO_RAD_S == units.DEG_TO_RAD


# ---- fingertip gap without Qt
@pytest.fixture(scope="module")
def gripper(tmp_path_factory):
    s = Session()
    s.open(build_gripper(tmp_path_factory.mktemp("gap")))
    return s


def test_gap_formula_values():
    assert gripper_gap_mm(0.0) == pytest.approx(2 * G["p"] - G["h"], abs=1e-12)  # 56 mm
    th = math.radians(G["closed_deg"])
    assert gripper_gap_mm(th) == pytest.approx(2 * G["p"] - G["h"] * math.cos(th) - 2 * G["L"] * math.sin(th))
    assert gripper_gap_mm(th) == pytest.approx(15.2, abs=0.05)  # reviewer's hand value at 20 deg


def test_tip_pairs_found_from_the_assembly_and_gap_from_identity_poses(gripper):
    asm = gripper.project.assembly
    pairs = inner_tip_pairs(asm, gripper.bodies)
    assert pairs is not None and len(pairs) == 2
    assert pairs[0][1] == pytest.approx([-G["p"] + G["h"] / 2, G["L"], 0.0], abs=1e-9)
    assert pairs[1][1] == pytest.approx([G["p"] - G["h"] / 2, G["L"], 0.0], abs=1e-9)
    ident = {c.id: np.eye(4) for c in asm.components}
    assert tip_gap_from_poses(pairs, ident) == pytest.approx(gripper_gap_mm(0.0), abs=1e-9)


def test_gap_from_rotated_poses_equals_the_formula(gripper):
    asm = gripper.project.assembly
    pairs = inner_tip_pairs(asm, gripper.bodies)
    th = math.radians(G["closed_deg"])
    T = {c.id: np.eye(4) for c in asm.components}
    for (cid, _), pivot, phi in ((pairs[0], np.array([-G["p"], 0, 0]), -th), (pairs[1], np.array([G["p"], 0, 0]), th)):
        R = np.array([[math.cos(phi), -math.sin(phi), 0], [math.sin(phi), math.cos(phi), 0], [0, 0, 1]])
        M = np.eye(4)
        M[:3, :3], M[:3, 3] = R, pivot - R @ pivot
        T[cid] = M
    assert tip_gap_from_poses(pairs, T) == pytest.approx(gripper_gap_mm(th), abs=1e-9)


def test_tip_pairs_none_for_a_drone_assembly(tmp_path):
    from opencae.examples.mbd_examples import build_drone_rotors
    s = Session()
    s.open(build_drone_rotors(tmp_path))
    assert inner_tip_pairs(s.project.assembly, s.bodies) is None
