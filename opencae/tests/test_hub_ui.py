"""Results Hub in the app, Help dialog, console in Python mode, default layout, Chromium flags. Offscreen Qt.

The conftest creates the QApplication. ``opencae.ui.hub_view`` / ``opencae.ui.help`` are installed here by hand
(``install(win)``), so these tests do not depend on ``MainWindow.EXTENSIONS``.
"""
import base64
import importlib
import os
import sys
import types
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.gui
SHOTS = Path(os.environ["OPENCAE_TEST_SHOTS"]) if os.environ.get("OPENCAE_TEST_SHOTS") else None


def shot(widget, name):
    if SHOTS is not None:
        SHOTS.mkdir(parents=True, exist_ok=True)
        widget.grab().save(str(SHOTS / name))


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance()


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QMessageBox
    from opencae.ui.main_window import MainWindow
    monkeypatch.setenv("OPENCAE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Discard))
    w = MainWindow(settings=QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat))
    w.resize(1560, 960)
    for mod in ("opencae.ui.hub_view", "opencae.ui.help"):
        w.extensions[mod] = importlib.import_module(mod).install(w)
    w.show()
    app.processEvents()
    yield w
    w.dirty = False
    w.close()


@pytest.fixture
def con(win):
    return win.extensions["opencae.ui.console"]


def pump(app, n=3):
    for _ in range(n):
        app.processEvents()


# ------------------------------------------------------------------------------------------------ install
def test_install_adds_buttons_menu_and_shortcut(win):
    from PySide6.QtCore import Qt
    hub = win.extensions["opencae.ui.hub_view"]
    for page in ("Home", "Drone", "Motion"):
        assert hub.action in win.ribbon_bars[page].actions(), page
    view = next(a.menu() for a in win.menuBar().actions() if a.text().replace("&", "") == "View")
    assert hub.action in view.actions()
    assert hub.action.shortcut().toString() == "Ctrl+Shift+R"
    assert hub.action.shortcutContext() == Qt.WindowShortcut
    assert "Results" in hub.action.iconText()
    # Help: a button in the ribbon's top-right corner (every page) and F1
    btn = win.ribbon.cornerWidget(Qt.TopRightCorner)
    assert btn is not None and btn.text() == "Help"
    assert win.A["guide"].shortcut().toString() == "F1"
    assert win.extensions["opencae.ui.help"].action is win.A["guide"]
    assert btn.isVisible()
    for name in ("Home", "Geometry", "Drone"):         # the corner widget belongs to the ribbon: every page
        win.ribbon.setCurrentIndex(list(win.ribbon_bars).index(name))
        assert btn.isVisible(), name
    # install twice changes nothing
    again = importlib.import_module("opencae.ui.hub_view").install(win)
    assert again is hub


# ------------------------------------------------------------------------------------------------ help
def test_help_lists_every_grammar_command(win):
    from opencae.ui.commands import grammar as G
    from opencae.ui import console as C
    from opencae.ui.help import GROUPS, command_reference
    entries = command_reference()
    keys = {e.key for e in entries}
    for verb in G.VERBS:
        assert verb in keys, verb
    for name in G.LEGACY_COMMANDS:
        assert name in keys, name
    assert {e.group for e in entries} <= set(GROUPS)
    for g in ("Projects", "Run", "Results", "Motion", "Parameters", "View", "Python"):
        assert any(e.group == g for e in entries), g
    assert all(e.syntax and e.example and e.text for e in entries)
    dlg = win.extensions["opencae.ui.help"].show()
    shown = {e.key for e in dlg.visible_entries()}
    assert shown >= set(G.VERBS) | set(G.LEGACY_COMMANDS)
    # the table of the console's own CLI commands is covered too
    assert set(C.COMMANDS) <= keys | {"python"}


def test_help_search_and_click_inserts_example_into_console(win, con):
    ctl = win.extensions["opencae.ui.help"]
    dlg = ctl.show()
    assert dlg.isVisible()
    n_all = len(dlg.visible_entries())
    dlg.search.setText("motion")
    vis = dlg.visible_entries()
    assert 0 < len(vis) < n_all and all("motion" in e.haystack() for e in vis)
    dlg.search.setText("zzzzqqq")
    assert dlg.visible_entries() == [] and not dlg.empty.isHidden()
    dlg.search.setText("")
    assert len(dlg.visible_entries()) == n_all
    # a click puts the example in the console input, focused, in Commands mode
    con.set_python_mode(True)
    entry = next(e for e in dlg.visible_entries() if e.example == "run structural on left finger")
    item = next(dlg.tree.topLevelItem(i).child(j) for i in range(dlg.tree.topLevelItemCount())
                for j in range(dlg.tree.topLevelItem(i).childCount())
                if dlg.tree.topLevelItem(i).child(j).data(0, 0x0100) is entry)
    dlg.tree.itemClicked.emit(item, 0)
    assert con.input.text() == "run structural on left finger"
    assert not con.python_mode and con.dock.isVisible()
    assert con.input.cursorPosition() == len(con.input.text())
    dlg.tree.itemDoubleClicked.emit(item, 0)           # double click: same, never runs it
    assert con.input.text() == "run structural on left finger" and "run structural" not in con.text().replace(
        con.input.text(), "")


def test_help_sections_have_content(win):
    dlg = win.extensions["opencae.ui.help"].show("start")
    assert dlg.nav.currentRow() == 1
    text = dlg.browsers["start"][0].toPlainText()
    assert text.count("\n") >= 4 and "Results" in text and "help" in text
    steps = dlg.browsers["start"][0].toHtml().count("<li")
    assert steps == 5
    dlg.show_section("shortcuts")
    assert "Ctrl+Shift+R" in dlg.browsers["shortcuts"][0].toPlainText()
    dlg.show_section("stored")
    st = dlg.browsers["stored"][0].toPlainText()
    assert "opencae.log" in st and "webview_graphics" in st


def test_help_command_and_question_mark_open_the_dialog_in_both_modes(win, con):
    ctl = win.extensions["opencae.ui.help"]
    con.execute("help")
    assert ctl.dialog is not None and ctl.dialog.isVisible()
    ctl.dialog.hide()
    con.set_python_mode(True)
    con.execute("help")                                # not Python's builtin help
    assert ctl.dialog.isVisible() and "Type help()" not in con.text()
    ctl.dialog.hide()
    con.execute("?")
    assert ctl.dialog.isVisible()


def test_f1_in_drone_mode_shows_the_help_shortcuts(win):
    win.enter_drone_mode()
    win._drone.show_shortcuts()
    dlg = win.extensions["opencae.ui.help"].dialog
    assert dlg is not None and dlg.isVisible() and dlg.nav.currentRow() == 2


# ------------------------------------------------------------------------------------------------ console
def test_mode_buttons_are_labelled_and_in_sync(win, con):
    assert (con.commands_btn.text(), con.toggle.text()) == ("Commands", "Python")
    assert con.commands_btn.isChecked() and not con.toggle.isChecked()
    con.toggle.setChecked(True)
    assert con.python_mode and con.commands_btn.isChecked() is False
    con.commands_btn.click()
    assert not con.python_mode and not con.toggle.isChecked() and con.commands_btn.isChecked()


def test_python_mode_runs_commands_as_commands_and_hints_on_real_syntax_errors(win, con):
    con.set_python_mode(True)
    con.output.clear()
    con.execute("status")                              # a bare command word (a NameError in Python)
    assert "ran as a command" in con.text() and "NameError" not in con.text()
    con.output.clear()
    con.execute("open no_such_project_here")           # a SyntaxError in Python, a command here
    t = con.text()
    assert "ran as a command" in t and "SyntaxError" not in t
    con.output.clear()
    con.execute("run cfd")
    assert "ran as a command" in con.text() and "SyntaxError" not in con.text()
    # real Python is still Python
    con.output.clear()
    con.execute("1 + 1")
    assert "2" in con.text() and "ran as a command" not in con.text()
    con.execute("open")                                # the Python builtin name is not a command here
    con.output.clear()
    con.execute("x = = 3")                             # a real syntax error: one-line hint
    t = con.text()
    assert "SyntaxError" in t and "Type `help` for commands, or switch to Commands mode." in t
    assert "Traceback" not in t and "ran as a command" not in t
    con.output.clear()
    con.execute(">>> open no_such_project_here")       # the >>> prefix behaves the same in either mode
    assert "ran as a command" in con.text()


# ------------------------------------------------------------------------------------------------ layout
def test_default_layout_and_reset(win, app):
    from PySide6.QtCore import Qt
    con = win.extensions["opencae.ui.console"]
    mon = win.extensions["opencae.ui.jobs"]
    pump(app)
    assert win.dockWidgetArea(con.dock) == Qt.BottomDockWidgetArea
    assert win.dockWidgetArea(mon.dock) == Qt.LeftDockWidgetArea
    # the console runs under everything: full window width, below the Progress Monitor
    # the bottom row (Studio Output + console) spans the whole window, under both side columns
    assert win.bottom_dock.geometry().left() <= mon.dock.geometry().left() + 4
    assert con.dock.geometry().right() >= win.props_dock.geometry().right() - 4
    assert con.dock.geometry().top() >= mon.dock.geometry().bottom() - 4
    # mess the layout up, then Reset layout restores it
    win.addDockWidget(Qt.RightDockWidgetArea, con.dock)
    win.addDockWidget(Qt.RightDockWidgetArea, mon.dock)
    mon.dock.hide()
    con.dock.hide()
    pump(app)
    assert win.dockWidgetArea(con.dock) == Qt.RightDockWidgetArea
    view = next(a.menu() for a in win.menuBar().actions() if a.text().replace("&", "") == "View")
    reset = next(a for a in view.actions() if a.text() == "Reset layout")
    reset.trigger()
    pump(app)
    assert win.dockWidgetArea(con.dock) == Qt.BottomDockWidgetArea and con.dock.isVisible()
    assert win.dockWidgetArea(mon.dock) == Qt.LeftDockWidgetArea and mon.dock.isVisible()
    assert win.dockWidgetArea(win.tree_dock) == Qt.LeftDockWidgetArea
    assert win.dockWidgetArea(win.props_dock) == Qt.RightDockWidgetArea
    assert abs(mon.dock.width() - 260) < 60 and 120 <= con.dock.height() <= 260


def test_reset_layout_in_drone_mode_keeps_studio_docks_hidden_until_back(win, app):
    from PySide6.QtCore import Qt
    win.enter_drone_mode()
    pump(app)
    reset = win.extensions["opencae.ui.console"].reset_action
    reset.trigger()
    pump(app)
    assert not win.tree_dock.isVisible() and not win.props_dock.isVisible()
    assert win.extensions["opencae.ui.console"].dock.isVisible() and win.extensions["opencae.ui.jobs"].dock.isVisible()
    win.cmd_drone_back()
    pump(app)
    assert win.tree_dock.isVisible() and win.props_dock.isVisible()
    assert win.dockWidgetArea(win.extensions["opencae.ui.console"].dock) == Qt.BottomDockWidgetArea


def test_saved_layout_wins_over_the_default(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings, Qt
    from opencae.ui.main_window import MainWindow
    monkeypatch.setenv("OPENCAE_HOME", str(tmp_path / "home"))
    ini = str(tmp_path / "keep.ini")
    w = MainWindow(settings=QSettings(ini, QSettings.IniFormat))
    w.show()
    c = w.extensions["opencae.ui.console"]
    w.addDockWidget(Qt.RightDockWidgetArea, c.dock)
    w.extensions["opencae.ui.jobs"].save_layout()
    w.dirty = False
    w.close()
    w2 = MainWindow(settings=QSettings(ini, QSettings.IniFormat))
    w2.show()
    pump(app, 5)
    assert w2.dockWidgetArea(w2.extensions["opencae.ui.console"].dock) == Qt.RightDockWidgetArea
    w2.dirty = False
    w2.close()


# ------------------------------------------------------------------------------------------------ hub page
@pytest.fixture
def fake_hub(monkeypatch):
    """A stand-in for V1's backend: opencae.hub.registry.register and a server URL."""
    hub = types.ModuleType("opencae.hub")
    hub.__path__ = []
    reg = types.ModuleType("opencae.hub.registry")
    calls = []

    def register(path, kind=None):
        calls.append(str(path))
        return {"id": "abc123", "kind": "drone", "path": str(path), "name": "quad250"}
    reg.register = register
    hub.registry = reg
    monkeypatch.setitem(sys.modules, "opencae.hub", hub)
    monkeypatch.setitem(sys.modules, "opencae.hub.registry", reg)

    class Srv:
        url = "http://127.0.0.1:9/"
        stopped = False

        def stop(self):
            Srv.stopped = True
    from opencae.ui import hub_view
    monkeypatch.setattr(hub_view, "start_hub_server", lambda: (Srv(), Srv.url))
    return calls


def _drone_dir(tmp_path):
    d = tmp_path / "quad250"
    d.mkdir()
    (d / "drone.json").write_text("{}")
    return d


def test_hub_view_degrades_cleanly_when_the_backend_is_missing(win, monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "opencae.hub", None)             # import raises ImportError
    monkeypatch.setitem(sys.modules, "opencae.hub.registry", None)
    ctl = win.extensions["opencae.ui.hub_view"]
    view = ctl.show(project=_drone_dir(tmp_path))
    assert win.stack.currentWidget() is view and view.hub_missing
    assert "not part of this installation" in view.message.text() and view.pages.currentIndex() == 0
    assert not view.browser_btn.isEnabled() and not view.viewer_btn.isEnabled()
    assert view.open_in_browser() is False                             # nothing to open, nothing raised
    view.back_btn.click()
    assert win.stack.currentWidget() is win.viewport


def test_hub_opens_current_drone_project_with_pid_and_tab(win, fake_hub, tmp_path, monkeypatch, app):
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    proj = _drone_dir(tmp_path)
    ws = win.drone_workspace()
    ws.project_dir = proj
    win.enter_drone_mode()
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda u: opened.append(u.toString()) or True))
    ctl = win.extensions["opencae.ui.hub_view"]
    view = ctl.show(tab="audit")
    assert fake_hub == [str(proj)]
    assert view.pid == "abc123" and view.url == "http://127.0.0.1:9/hub/?pid=abc123&tab=audit"
    assert win.stack.currentWidget() is view and not view.hub_missing
    assert "quad250" in view.title.text()
    if view.web is not None:
        assert view.pages.currentWidget() is view.web
        assert view.web.url().toString().startswith("http://127.0.0.1:9/hub/?pid=abc123")
    assert view.browser_btn.isEnabled()
    view.browser_btn.click()
    assert opened == [view.url]
    view.back_btn.click()
    assert win.stack.currentWidget() is ws
    # the Drone toolbar button and the action open the same page
    ws.hub_btn.click()
    assert win.stack.currentWidget() is view
    view.shutdown()


def test_hub_without_web_view_keeps_buttons_and_says_why(win, fake_hub, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCAE_NO_WEBVIEW", "1")
    from opencae.ui import hub_view
    ctl = win.extensions["opencae.ui.hub_view"]
    ctl.view = None                                    # a fresh page under the new condition
    view = ctl.show(project=_drone_dir(tmp_path))
    assert view.web is None and "switched off" in view.message.text()
    assert "Results Hub opens in your browser" in view.message.text()
    assert view.browser_btn.isEnabled() and view.viewer_btn.isEnabled()
    view.shutdown()
    assert hub_view.hub_url("http://h:1/", "p", "plots", "r") == "http://h:1/hub/?pid=p&run=r&tab=plots"


def test_show3d_link_opens_the_native_viewer(win, fake_hub, tmp_path, monkeypatch):
    from PySide6.QtCore import QUrl
    from PySide6.QtWebEngineCore import QWebEnginePage
    from opencae.ui.hub_view import parse_show3d
    assert parse_show3d(QUrl("opencae://show3d?pid=a&run=r1&field=cp")) == {"pid": "a", "run": "r1", "field": "cp"}
    assert parse_show3d(QUrl("http://x/hub/")) is None
    ctl = win.extensions["opencae.ui.hub_view"]
    view = ctl.show(project=_drone_dir(tmp_path))
    if view.web is None:
        pytest.skip("no web view in this environment")
    loads = []
    monkeypatch.setattr(view.native, "load", lambda *a: loads.append(a))
    page = view.web.page()
    nav = QWebEnginePage.NavigationType.NavigationTypeLinkClicked
    accepted = page.acceptNavigationRequest(QUrl("opencae://show3d?pid=abc123&run=r7&field=cp"), nav, True)
    assert accepted is False                                  # intercepted, the page does not navigate
    assert loads == [("http://127.0.0.1:9/", "abc123", "r7", "cp")]
    assert view.pages.currentWidget() is view.native
    view.shutdown()


def _b64(a, dt):
    return base64.b64encode(np.asarray(a, dtype=dt).tobytes()).decode()


SCENE = {"units": "mm",
         "parts": [{"name": "frame", "role": "frame",
                    "points": _b64([[0, 0, 0], [10, 0, 0], [0, 10, 0], [10, 10, 5]], "<f4"),
                    "triangles": _b64([[0, 1, 2], [1, 3, 2]], "<i4")}],
         "fields": [{"id": "cp", "label": "Pressure coefficient", "discipline": "cfd", "unit": "-",
                     "quantity": "Cp", "mesh": "frame", "values": _b64([0.1, -0.5, 0.3, 0.9], "<f4"),
                     "min": -0.5, "max": 0.9},
                    {"id": "bad", "label": "Broken", "unit": "Pa", "mesh": "frame", "values": _b64([1, 2, 3], "<f4")},
                    {"id": "tau", "label": "Wall shear", "unit": "Pa", "mesh": "own",
                     "points": _b64([[0, 0, 0], [1, 0, 0], [0, 1, 0]], "<f4"), "triangles": _b64([[0, 1, 2]], "<i4"),
                     "values": _b64([2.0], "<f4")}],
         "slices": [], "streamlines": None, "notes": ["fields kept for the latest run only"]}


def test_scene_decodes_into_meshes_and_reports_what_it_dropped():
    from opencae.ui.hub_view import scene_to_meshes
    m = scene_to_meshes(SCENE)
    assert list(m["parts"]) == ["frame"] and m["parts"]["frame"].n_cells == 2
    ids = [f["id"] for f in m["fields"]]
    assert ids == ["cp", "tau"]                               # "bad": 3 values fit neither 4 points nor 2 triangles
    cp = m["fields"][0]
    assert cp["mesh"].point_data["values"].tolist() == pytest.approx([0.1, -0.5, 0.3, 0.9])
    assert (cp["min"], cp["max"]) == (-0.5, 0.9)
    assert m["fields"][1]["mesh"].cell_data["values"].tolist() == [2.0]
    assert "fields kept for the latest run only" in m["notes"]
    assert any("Broken" in n for n in m["notes"])             # dropped with a reason, never invented


def test_native_viewer_lists_fields_and_draws(win, app):
    view = win.extensions["opencae.ui.hub_view"].ensure_view()
    v = view.native
    v.set_scene(SCENE)
    assert v.picker.count() == 2 and v.picker.itemData(0) == "cp"
    assert "Pressure coefficient" in v.picker.itemText(0)
    assert v.current_field()["label"] == "Pressure coefficient"
    assert v.status.text()                                     # notes or the range, or why the viewer cannot start
    v.picker.setCurrentIndex(1)
    assert v.current_field()["id"] == "tau"
    view.shutdown()                                            # the window closing does the same (event filter)


# ------------------------------------------------------------------------------------------------ drone workspace
def test_drone_workspace_button_and_webgl_banner(win, app):
    ws = win.drone_workspace()
    win.enter_drone_mode()
    assert ws.hub_btn.text() == "Results hub" and ws.hub_btn.isEnabled()
    assert ws.webgl_banner.isHidden()
    ws.note_webgl_unavailable("test")
    assert not ws.webgl_banner.isHidden()
    assert "3D view unavailable in the app on this PC" in ws.webgl_label.text()
    assert ws.webgl_browser_btn.text() == "Open in browser" and ws.webgl_viewer_btn.text() == "Show in 3D viewer"
    ws.webgl_banner.hide()
    if ws.web is not None:                                     # the page reports the console error the PC logs
        from PySide6.QtWebEngineCore import QWebEnginePage
        lvl = QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel
        ws.web.page().javaScriptConsoleMessage(lvl, "THREE.WebGLRenderer: Error creating WebGL context.", 1, "")
        for _ in range(5):
            app.processEvents()
        assert not ws.webgl_banner.isHidden() and ws.webgl_ok is False
    ws.note_webgl_available()
    assert ws.webgl_banner.isHidden()


def test_webgl_failure_messages():
    from opencae.ui.hub_view import webgl_failure
    assert webgl_failure("THREE.WebGLRenderer: Error creating WebGL context.")
    assert webgl_failure("THREE.WebGLRenderer: Context Lost.") and webgl_failure("CONTEXT_LOST_WEBGL")
    assert not webgl_failure("Uncaught TypeError: x is not a function")


# ------------------------------------------------------------------------------------------------ chromium flags
@pytest.fixture
def flags_env(tmp_path, monkeypatch):
    from opencae.utilities import crash
    monkeypatch.setenv("OPENCAE_HOME", str(tmp_path / "ch_home"))
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)
    monkeypatch.delenv("OPENCAE_WEBVIEW_GRAPHICS", raising=False)
    monkeypatch.delenv("OPENCAE_SAFE_MODE", raising=False)
    monkeypatch.setattr(crash, "_state", dict(crash._state, safe=False))
    return crash


def _flags(crash, platform, monkeypatch, setting=None):
    from opencae.utilities import settings
    monkeypatch.setattr(sys, "platform", platform)
    if setting is not None:
        settings.set_settings(webview_graphics=setting)
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)
    crash.prepare_qt_environment()
    return os.environ.pop("QTWEBENGINE_CHROMIUM_FLAGS", "")


@pytest.mark.parametrize("setting,expect", [
    (None, "software"), ("software", "software"), ("gpu", ""), ("off", "off"), ("nonsense", "software")])
def test_windows_flags_follow_the_webview_graphics_setting(flags_env, monkeypatch, setting, expect):
    got = _flags(flags_env, "win32", monkeypatch, setting)
    if expect == "software":
        assert got.split() == ["--use-angle=swiftshader", "--enable-unsafe-swiftshader",
                               "--disable-gpu-compositing", "--ignore-gpu-blocklist"]
        assert "--disable-gpu" not in got.split()
    elif expect == "off":
        assert got == "--disable-gpu --disable-gpu-compositing"
    else:
        assert got == ""                                       # "gpu": Chromium's own choice


def test_other_platforms_leave_chromium_alone_unless_set(flags_env, monkeypatch):
    assert _flags(flags_env, "linux", monkeypatch) == ""
    assert "--use-angle=swiftshader" in _flags(flags_env, "linux", monkeypatch, "software")
    assert _flags(flags_env, "darwin", monkeypatch, "off").startswith("--disable-gpu")


def test_user_flags_and_env_override_win(flags_env, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "--mine")
    flags_env.prepare_qt_environment()
    assert os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] == "--mine"
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS")
    monkeypatch.setenv("OPENCAE_WEBVIEW_GRAPHICS", "gpu")      # one-run override of the setting
    flags_env.prepare_qt_environment()
    assert "QTWEBENGINE_CHROMIUM_FLAGS" not in os.environ
    assert flags_env.webview_graphics() == "gpu"


# ------------------------------------------------------------------------------------------------ screenshots
def test_screenshots(win, fake_hub, app, tmp_path):
    if SHOTS is None:
        pytest.skip("set OPENCAE_TEST_SHOTS to write screenshots")
    win.enter_drone_mode()
    pump(app, 5)
    ctl = win.extensions["opencae.ui.help"]
    dlg = ctl.show()
    dlg.resize(1120, 680)
    pump(app, 5)
    shot(win, "main_default_layout.png")
    shot(dlg, "help_dialog.png")
    dlg.search.setText("motion")
    shot(dlg, "help_dialog_search.png")
    dlg.show_section("start")
    shot(dlg, "help_getting_started.png")
    dlg.hide()
    ws = win._drone
    ws.project_dir = _drone_dir(tmp_path)
    ws.note_webgl_unavailable("shot")
    pump(app, 5)
    shot(win, "drone_webgl_banner.png")
    view = win.extensions["opencae.ui.hub_view"].show(project=ws.project_dir)
    pump(app, 8)
    shot(win, "hub_page.png")
    view.show_native()
    view.native.set_scene(SCENE)
    pump(app, 5)
    shot(win, "hub_native_3d.png")
    view.shutdown()


# ------------------------------------------------------------------------------------------------ S6: the real chain
def _backend_helpers():
    """make_drone from test_hub_backend.py (loaded by path: the tests folder is not a package)."""
    import importlib.util
    p = Path(__file__).with_name("test_hub_backend.py")
    spec = importlib.util.spec_from_file_location("_hub_backend_helpers", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_real_chain_image_png_then_native_viewer_then_new_viewport(win, app, tmp_path, monkeypatch):
    """Review B1: rendering /api/hub/image.png must not leave pyvista's global OFF_SCREEN set, or every 3D view made
    afterwards (the native viewer, a new Viewport) comes up without a live interactor."""
    import json
    import urllib.request
    import pyvista as pv
    from opencae.hub import registry
    from opencae.ui import hub_view
    from opencae.ui.viewport import Viewport
    proj = _backend_helpers().make_drone(tmp_path / "home" / "projects" / "quad_chain", "ok")
    off_before = pv.OFF_SCREEN
    pv.OFF_SCREEN = True  # MUTATION
    srv, base = hub_view.start_hub_server()               # the real DashboardServer(None) the app starts
    try:
        pid = registry.register(proj)["id"]
        scene = json.loads(urllib.request.urlopen(f"{base}api/hub/scene?pid={pid}&run=latest", timeout=60).read())
        field = next(f["id"] for f in scene["fields"] if f.get("discipline") == "cfd")
        png = urllib.request.urlopen(f"{base}api/hub/image.png?pid={pid}&run=latest&field={field}&view=iso",
                                     timeout=240).read()
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert pv.OFF_SCREEN == off_before and pv.OFF_SCREEN is not True, "render.py set pyvista's global OFF_SCREEN"
        # the native viewer, for that field, has a live interactor and actors
        view = win.extensions["opencae.ui.hub_view"].ensure_view()
        view.base, view.pid, view.hub_missing = base, pid, False
        view.native.set_scene(scene)
        view.native._want_field = field
        i = view.native.picker.findData(field)
        assert i >= 0
        view.native.picker.setCurrentIndex(i)
        pl = view.native.plotter
        assert pl is not None, view.native.status.text()
        assert pl.iren is not None
        assert len(pl.renderer.actors) > 0
        # and a new main viewport still builds with a live interactor
        vp = Viewport()
        try:
            assert vp.plotter.iren is not None
        finally:
            vp.close_plotter()
            vp.deleteLater()
        from opencae.ui.main_window import MainWindow
        from PySide6.QtCore import QSettings
        w2 = MainWindow(settings=QSettings(str(tmp_path / "w2.ini"), QSettings.IniFormat))
        assert w2.viewport.plotter.iren is not None
        w2.dirty = False
        w2.close()
        view.shutdown()
    finally:
        srv.stop()
