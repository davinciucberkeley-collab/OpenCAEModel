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
    for item in items:
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
