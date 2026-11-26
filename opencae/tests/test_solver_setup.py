"""Solver set-up: status shape, SU2 download from a local fake server, bad zips, Windows without WSL, CLI, console."""
from __future__ import annotations

import http.server
import json
import socketserver
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from opencae.drone import solver_setup as S


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCAE_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "www"
    root.mkdir()

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def log_message(self, *a):
            pass
    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _zip(path: Path, names):
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, "#!/bin/sh\necho SU2\n")


def test_status_shape(home):
    st = S.status()
    assert set(st) >= {"su2", "openfoam", "platform", "wsl"}
    assert set(st["su2"]) >= {"found", "path"} and set(st["openfoam"]) >= {"found", "where"}
    assert set(st["wsl"]) == {"available", "distros"}
    assert "SU2" in S.describe(st)


def test_status_downloads_nothing(home):
    S.status()
    assert not (home / "solvers").exists()


def test_solvers_dir_honours_home(home):
    assert S.solvers_dir() == home / "solvers"


def test_install_su2_from_local_server(home, server, monkeypatch):
    root, base = server
    exe = "SU2_CFD.exe" if sys.platform == "win32" else "SU2_CFD"
    _zip(root / "su2.zip", [f"bin/{exe}", "bin/SU2_SOL", "README"])
    monkeypatch.setattr(S, "su2_url", lambda: base + "/su2.zip")

    class Rep:
        ok, version = True, "8.1.0"

        def summary(self):
            return "PASSED"
    from opencae.cfd import su2 as su2mod
    monkeypatch.setattr(su2mod.SU2Backend, "selftest", lambda self, keep_dir=None: Rep())
    msgs = []
    r = S.install_su2(progress=lambda m, f: msgs.append((m, f)))
    assert r["ok"], r
    assert Path(r["path"]).is_file() and str(home / "solvers") in r["path"]
    assert r["downloaded_bytes"] > 0 and r["disk_bytes"] > 0 and "Downloaded" in r["message"]
    assert msgs and msgs[-1][1] == 1.0
    from opencae.utilities.settings import get_setting
    assert get_setting("su2_path") == r["path"]
    assert not list((home / "solvers").glob("*.zip")) and not list((home / "solvers").glob("*.part"))


def test_install_su2_failed_selftest_is_not_saved(home, server, monkeypatch):
    root, base = server
    exe = "SU2_CFD.exe" if sys.platform == "win32" else "SU2_CFD"
    _zip(root / "su2.zip", [f"SU2-x/bin/{exe}"])
    monkeypatch.setattr(S, "su2_url", lambda: base + "/su2.zip")

    class Rep:
        ok, version = False, ""

        def summary(self):
            return "FAILED details"
    from opencae.cfd import su2 as su2mod
    monkeypatch.setattr(su2mod.SU2Backend, "selftest", lambda self, keep_dir=None: Rep())
    r = S.install_su2()
    assert r["ok"] is False and "self-test" in r["message"] and "FAILED details" in r["log"]
    from opencae.utilities.settings import get_setting
    assert not get_setting("su2_path")


def test_install_su2_bad_zip(home, server, monkeypatch):
    root, base = server
    (root / "bad.zip").write_bytes(b"this is not a zip")
    monkeypatch.setattr(S, "su2_url", lambda: base + "/bad.zip")
    r = S.install_su2()
    assert r["ok"] is False and "zip" in r["message"] and "log" in r
    assert not list((home / "solvers").glob("SU2-*"))


def test_install_su2_zip_without_binary(home, server, monkeypatch):
    root, base = server
    _zip(root / "empty.zip", ["README", "bin/other"])
    monkeypatch.setattr(S, "su2_url", lambda: base + "/empty.zip")
    r = S.install_su2()
    assert r["ok"] is False and "SU2_CFD" in r["message"]


def test_install_su2_download_error_never_raises(home, server, monkeypatch):
    _root, base = server
    monkeypatch.setattr(S, "su2_url", lambda: base + "/missing.zip")
    r = S.install_su2()
    assert r["ok"] is False and r["message"]


def test_install_su2_cancel(home, server, monkeypatch):
    root, base = server
    _zip(root / "su2.zip", ["bin/SU2_CFD"])
    monkeypatch.setattr(S, "su2_url", lambda: base + "/su2.zip")
    r = S.install_su2(cancelled=lambda: True)
    assert r["ok"] is False and "Cancelled" in r["message"]


def test_su2_urls_are_official_and_pinned(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert S.su2_url() == f"https://github.com/su2code/SU2/releases/download/v{S.SU2_VERSION}/SU2-v{S.SU2_VERSION}-linux64.zip"
    monkeypatch.setattr(sys, "platform", "win32")
    assert S.su2_url().endswith(f"SU2-v{S.SU2_VERSION}-win64.zip")


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
    assert "MB" in S.plan("su2")["size"]
    with pytest.raises(ValueError):
        S.plan("fluent")


# ------------------------------------------------------------------------------------------------ CLI
def test_cli_setup_json_status(home, capsys):
    from opencae import cli
    assert cli.main(["setup", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert "su2" in d and "openfoam" in d and "wsl" in d


def test_cli_setup_without_yes_only_plans(home, capsys):
    from opencae import cli
    assert cli.main(["setup", "su2", "--json"]) == 2
    d = json.loads(capsys.readouterr().out)
    assert d["confirmed"] is False and "MB" in d["message"]
    assert not (home / "solvers").exists()


def test_cli_setup_yes_runs_install(home, capsys, monkeypatch):
    from opencae import cli
    monkeypatch.setattr(S, "install", lambda t, progress=None, cancelled=None: {"ok": True, "message": "done " + t,
                                                                                 "log": ""})
    assert cli.main(["setup", "openfoam", "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["message"] == "done openfoam"
    monkeypatch.setattr(S, "install", lambda t, progress=None, cancelled=None: {"ok": False, "message": "no",
                                                                                 "log": "x"})
    assert cli.main(["setup", "su2", "--yes", "--json"]) == 1


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
