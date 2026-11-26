"""Solver set-up: status shape, OpenFOAM set-up refusals, Windows without WSL, CLI, console."""
from __future__ import annotations

import json
import sys

import pytest

from opencae.drone import solver_setup as S


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCAE_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_status_shape(home):
    st = S.status()
    assert set(st) == {"openfoam", "mujoco", "platform", "wsl", "solvers_dir"}
    assert set(st["openfoam"]) >= {"found", "where"}
    assert set(st["wsl"]) == {"available", "distros"}
    assert "OpenFOAM" in S.describe(st)


def test_status_downloads_nothing(home):
    S.status()
    assert not (home / "solvers").exists()


def test_solvers_dir_honours_home(home):
    assert S.solvers_dir() == home / "solvers"


def test_openfoam_on_windows_without_wsl_needs_wsl(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("opencae.cfd.openfoam.install.wsl_distros", lambda: [])
    monkeypatch.setattr(S.shutil, "which", lambda n: None)
    r = S.install_openfoam()
    assert r["ok"] is False and r["needs"] == "wsl"
    assert "wsl --install -d Ubuntu-24.04" in r["message"] and "administrator" in r["message"]


def test_openfoam_low_disk_is_refused(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    class U:
        free = 1024 ** 3
    monkeypatch.setattr(S.shutil, "disk_usage", lambda p: U)
    r = S.install_openfoam()
    assert r["ok"] is False and "disk space" in r["message"]


def test_plan_names_size_and_source(home):
    p = S.plan("openfoam")
    assert "GB" in p["size"] and "conda-forge" in p["source"]
    with pytest.raises(ValueError):
        S.plan("fluent")


# ------------------------------------------------------------------------------------------------ CLI
def test_cli_setup_json_status(home, capsys):
    from opencae import cli
    assert cli.main(["setup", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert set(d) >= {"openfoam", "mujoco", "wsl"} and "wsl" in d


def test_cli_setup_without_yes_only_plans(home, capsys):
    from opencae import cli
    assert cli.main(["setup", "openfoam", "--json"]) == 2
    d = json.loads(capsys.readouterr().out)
    assert d["confirmed"] is False and "GB" in d["message"]
    assert not (home / "solvers").exists()


def test_cli_setup_yes_runs_install(home, capsys, monkeypatch):
    from opencae import cli
    monkeypatch.setattr(S, "install", lambda t, progress=None, cancelled=None: {"ok": True, "message": "done " + t,
                                                                                 "log": ""})
    assert cli.main(["setup", "openfoam", "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["message"] == "done openfoam"
    monkeypatch.setattr(S, "install", lambda t, progress=None, cancelled=None: {"ok": False, "message": "no",
                                                                                 "log": "x"})
    assert cli.main(["setup", "openfoam", "--yes", "--json"]) == 1


# ------------------------------------------------------------------------------------------------ MuJoCo (motion)
FAKE_MUJOCO_PY = ("import sys, types; m = types.ModuleType('mujoco'); m.__version__ = '9.9.9'; "
                  "sys.modules['mujoco'] = m; print('9.9.9')")


def test_status_and_plan_know_mujoco(home):
    st = S.status()
    assert set(st["mujoco"]) >= {"found", "version"}
    assert "MuJoCo" in S.describe(st)
    p = S.plan("mujoco")
    assert "MB" in p["size"] and "pip install mujoco" in p["message"] and sys.executable in p["message"]
    assert not (home / "solvers").exists()


def test_install_mujoco_uses_this_python_and_verifies_the_import(home, monkeypatch):
    seen = {}

    def fake_stream(argv, progress, cancelled, label, lo, hi, env=None, timeout=0):
        seen["argv"] = argv
        return 0, "Downloading mujoco-9.9.9-cp311-manylinux.whl (6.5 MB)\nSuccessfully installed mujoco-9.9.9"
    monkeypatch.setattr(S, "_stream", fake_stream)
    monkeypatch.setattr(S, "_verify_mujoco", lambda: (True, "9.9.9", 52 * 1024 ** 2, ""))
    r = S.install("mujoco")
    assert seen["argv"] == [sys.executable, "-m", "pip", "install", "mujoco"]
    assert r["ok"] and r["version"] == "9.9.9" and r["disk_bytes"] == 52 * 1024 ** 2
    assert "6.2 MB" in r["message"] and "52.0 MB" in r["message"] and "9.9.9" in r["message"]


def test_install_mujoco_pip_failure_and_failed_import_are_reported(home, monkeypatch):
    monkeypatch.setattr(S, "_stream", lambda *a, **k: (1, "ERROR: no network"))
    r = S.install_mujoco()
    assert r["ok"] is False and "no network" in r["log"] and "nothing" in r["message"].lower()
    monkeypatch.setattr(S, "_stream", lambda *a, **k: (0, "Successfully installed mujoco-9"))
    monkeypatch.setattr(S, "_verify_mujoco", lambda: (False, "", 0, "ImportError: boom"))
    r = S.install_mujoco()
    assert r["ok"] is False and "import" in r["message"] and "boom" in r["log"]


def test_verify_mujoco_runs_in_a_subprocess(monkeypatch):
    monkeypatch.setattr(S, "_VERIFY_CODE", FAKE_MUJOCO_PY)
    ok, ver, _size, _err = S._verify_mujoco()
    assert ok and ver == "9.9.9"
    monkeypatch.setattr(S, "_VERIFY_CODE", "raise ImportError('no mujoco here')")
    ok, ver, _size, err = S._verify_mujoco()
    assert not ok and "no mujoco here" in err


def test_cli_setup_mujoco_needs_yes(home, capsys):
    from opencae import cli
    assert cli.main(["setup", "mujoco", "--json"]) == 2
    d = json.loads(capsys.readouterr().out)
    assert d["confirmed"] is False and "pip install mujoco" in d["message"]
