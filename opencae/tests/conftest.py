import os
import shutil
from pathlib import Path

import pytest

os.environ.setdefault("OPENCAE_HOME", str(Path(__file__).parent / ".opencae_test_home"))


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def pytest_collection_modifyitems(config, items):
    ccx = shutil.which("ccx") or os.environ.get("OPENCAE_CCX")
    from opencae.cfd.su2 import find_su2
    su2 = find_su2()
    foam = None
    if any("openfoam" in it.keywords for it in items):
        from opencae.cfd.openfoam.install import find_openfoam, version_number
        f = find_openfoam()
        foam = f if f is not None and version_number(f.version) >= 2006 else None
    for item in items:
        if "openfoam" in item.keywords and not foam:
            item.add_marker(pytest.mark.skip(reason="OpenFOAM v2006+ not found (set OPENCAE_OPENFOAM)"))
        if "cfd" in item.keywords and not su2:
            item.add_marker(pytest.mark.skip(reason="SU2 (SU2_CFD) not installed"))
        if "solver" in item.keywords and not ccx:
            item.add_marker(pytest.mark.skip(reason="CalculiX (ccx) not installed"))
        if "gui" in item.keywords and not _has_display():
            item.add_marker(pytest.mark.skip(reason="no display (run under xvfb-run)"))


@pytest.fixture
def session():
    from opencae.app.session import Session
    return Session()


@pytest.fixture
def cantilever(session):
    session.create_cantilever(length=200.0, width=20.0, height=20.0, force=1000.0)
    session.project.mesh_settings.size_max = 6.0
    return session


@pytest.fixture(scope="session", autouse=True)
def _isolated_qsettings(tmp_path_factory):
    """Tests must never write the user's real QSettings (Windows: the registry)."""
    home = tmp_path_factory.mktemp("qsettings")
    os.environ["XDG_CONFIG_HOME"] = str(home)
    try:
        from PySide6.QtCore import QSettings
    except ImportError:
        yield
        return
    for fmt in (QSettings.NativeFormat, QSettings.IniFormat):
        QSettings.setPath(fmt, QSettings.UserScope, str(home))
    yield
