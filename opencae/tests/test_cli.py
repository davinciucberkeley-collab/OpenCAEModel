"""CLI tests. Tests that need the pipeline (agent "core") skip until opencae.drone.pipeline exists."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from opencae import cli

ROOT = Path(__file__).resolve().parents[2]
QUAD = ROOT / "opencae" / "examples" / "drones" / "quad250"


@pytest.fixture
def quad(tmp_path):
    pytest.importorskip("opencae.drone.pipeline")
    pytest.importorskip("opencae.drone.spec")
    if not QUAD.exists():
        pytest.skip("quad250 example not there yet")
    dst = tmp_path / "quad"
    shutil.copytree(QUAD, dst, ignore=shutil.ignore_patterns("results"))
    return dst


def _json(capsys):
    out = capsys.readouterr()
    return json.loads(out.out), out.err


def test_parse_set_values():
    assert cli.parse_set(["a.b=3", "c=2.5", "d=true", "e=False", "f=abc", "g=1e3"]) == {
        "a.b": 3, "c": 2.5, "d": True, "e": False, "f": "abc", "g": 1000.0}
    assert isinstance(cli.parse_set(["a=3"])["a"], int)
    with pytest.raises(ValueError):
        cli.parse_set(["nokey"])
    with pytest.raises(ValueError):
        cli.parse_set(["=3"])


def test_doctor_json_keys(capsys):
    assert cli.main(["doctor", "--json"]) == 0
    d, _ = _json(capsys)
    for k in ("python", "ccx", "openfoam", "gmsh", "ocp", "pyside6", "qtwebengine", "disk_free_gb"):
        assert k in d
    assert "available" in d["ccx"]


def test_missing_project_exits_2(tmp_path, capsys):
    assert cli.main(["status", str(tmp_path / "nope"), "--json"]) == 2
    out = capsys.readouterr()
    assert json.loads(out.out)["error"]


def test_bad_set_syntax_exits_2(tmp_path, capsys):
    assert cli.main(["run", str(tmp_path), "--set", "oops", "--json"]) == 2


def test_python_m_opencae_help():
    r = subprocess.run([sys.executable, "-m", "opencae", "--help"], capture_output=True, text=True, cwd=ROOT,
                       timeout=120)
    assert r.returncode == 0
    assert "doctor" in r.stdout and "dashboard" in r.stdout


def test_run_only_json(quad, capsys):
    assert cli.main(["run", str(quad), "--only", "mass,propulsion", "--json"]) == 0
    d, err = _json(capsys)
    assert "verdict" in d
    assert {"mass", "propulsion"} <= set(d["analyses"])
    assert err  # progress went to stderr


def test_unknown_analysis_exits_2(quad, capsys):
    assert cli.main(["run", str(quad), "--only", "warp", "--json"]) == 2


def test_bad_key_exits_2_and_file_unchanged(quad, capsys):
    before = (quad / "drone.json").read_bytes()
    assert cli.main(["run", str(quad), "--only", "mass", "--set", "bad.key=1", "--json"]) == 2
    d, _ = _json(capsys)
    assert d["error"]
    assert (quad / "drone.json").read_bytes() == before


def test_status_json_and_human(quad, capsys):
    assert cli.main(["status", str(quad), "--json"]) == 0
    d, _ = _json(capsys)
    assert isinstance(d, dict)
    assert cli.main(["run", str(quad), "--only", "mass"]) == 0
    out = capsys.readouterr().out
    assert "mass" in out.lower()


def test_clean(quad, capsys):
    assert cli.main(["clean", str(quad), "--json"]) == 0
    d, _ = _json(capsys)
    assert "freed_bytes" in d
