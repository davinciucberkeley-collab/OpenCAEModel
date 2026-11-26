"""Help: the searchable command reference, a getting-started guide, the shortcuts and where things are stored.

``install(main_window)`` (called by ``MainWindow._install_extensions``) adds

* a **Help** button in the top-right corner of the ribbon (visible on every ribbon page),
* **F1** (the Studio "User guide" action now opens this dialog; the full manual stays in Help > User guide),
* Help menu entries.

The command list is generated from the console grammar (``opencae.ui.commands.grammar.VERBS`` and
``LEGACY_COMMANDS``, plus the console's own CLI table), so a command that is added there shows up here with no
change to this file (unknown ones land in "More"). Clicking a command puts its example into the console input and
puts the cursor there; nothing runs until you press Enter.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton, QStackedWidget,
                               QTextBrowser, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from opencae.ui import theme

log = logging.getLogger("opencae.ui")

GROUPS = ("Projects", "Run", "Results", "Motion", "Parameters", "View", "Python", "More")
SECTIONS = ("commands", "start", "shortcuts", "stored")
SECTION_TITLES = {"commands": "Commands", "start": "Getting started", "shortcuts": "Keyboard shortcuts",
                  "stored": "Where things are stored"}

# which group a command belongs to (grammar verb or CLI command name); anything not listed lands in "More"
VERB_GROUP = {
    "open": "Projects", "new": "Projects", "example": "Projects", "mode": "Projects",
    "run": "Run", "stop": "Run", "retry": "Run", "status": "Run", "clean": "Run", "setup": "Run", "doctor": "Run",
    "results": "Results", "checks": "Results", "history": "Results", "compare": "Results", "variants": "Results",
    "dashboard": "Results",
    "select": "Parameters", "set": "Parameters", "get": "Parameters", "params": "Parameters", "iterate": "Parameters",
    "show": "View", "clear": "View", "help": "View",
    "python": "Python",
}
# what `show <word>` means, in plain words; words not listed get a generic sentence
SHOW_TEXT = {
    "displacement": "How far the part moves (colour map on the model).",
    "stress": "Von Mises stress: where the material works hardest.",
    "principal": "Principal stress (the largest pull or push at each point).",
    "strain": "How much the material stretches.",
    "temperature": "Temperature colour map.",
    "frequency": "Natural frequencies and mode shapes.",
    "velocity": "Air speed around the model.",
    "pressure": "Air pressure on the model.",
    "mesh": "The calculation mesh and its quality.",
    "geometry": "The plain CAD model without results.",
    "results": "The results panel.",
    "table": "The case table of a parameter study.",
    "plot": "A chart of the results.",
    "csv": "Export the numbers to a spreadsheet file (CSV).",
    "jobs": "The Progress Monitor (what is running).",
    "playback": "Play the motion study as an animation.",
    "dashboard": "The project dashboard.",
}
SHOW_GROUP = {"mesh": "View", "geometry": "View", "jobs": "View", "dashboard": "View", "playback": "Motion"}
RUN_EXAMPLES = {"structural": "run structural on left finger", "thermal": "run thermal on motor housing 1",
                "modal": "run modal on left finger", "fluid": "run fluid around assembly",
                "motion": "run motion on gripper"}


@dataclass
class Entry:
    group: str
    syntax: str
    example: str
    text: str
    key: str = ""        # the grammar verb / CLI command this entry documents

    def haystack(self) -> str:
        return " ".join((self.group, self.syntax, self.example, self.text)).lower()


# ------------------------------------------------------------------------------------------------ the data
def command_reference() -> list:
    """Every console command as :class:`Entry` rows, generated from the grammar and the console's CLI table."""
    from opencae.ui.commands import grammar as G
    from opencae.ui import console as C
    out: list = []
    seen: set = set()

    def add(group, syntax, example, text, key):
        sig = (syntax, example)
        if sig in seen:
            return
        seen.add(sig)
        out.append(Entry(group if group in GROUPS else "More", syntax, example, text, key))

    for verb, (usage, text, example) in G.VERBS.items():
        add(VERB_GROUP.get(verb, "More"), usage, example, text, verb)
    for name in G.LEGACY_COMMANDS:
        if name in C.COMMANDS:
            usage, text, example = C.COMMANDS[name]
        else:
            usage, text, example = name, f"Drone project command '{name}'.", name
        natural = name in G.VERBS and (usage, example) != tuple(G.VERBS[name][i] for i in (0, 2))
        add(VERB_GROUP.get(name, "More"), usage, example,
            ("Drone projects: " + text) if natural else text, name)
    for key, label in G.ANALYSIS_LABEL.items():           # one line per analysis type
        group = "Motion" if key == "motion" else "Run"
        ex = RUN_EXAMPLES.get(key, f"run {key}")
        add(group, f"run {key} [on <component>]", ex, f"Run the {label.lower()} analysis.", "run")
    for word in G.SHOW_WORDS:
        add(SHOW_GROUP.get(word, "Results"), f"show {word}", f"show {word}",
            SHOW_TEXT.get(word, f"Show {word}."), "show")
    add("Motion", "show playback", "show playback", "Play the motion study (gripper closing, drone moving).", "show")
    add("Results", "open dashboard", "open dashboard", "Open the project dashboard in your browser.", "open")
    scope = ", ".join(C.PY_SCOPE)
    add("Python", ">>> <python code>", ">>> spec['name']",
        f"Run your own Python. In scope: {scope}.", "python")
    add("Python", "!<command>", "!status", "While in Python mode, start a line with ! to run a command.", "python")
    out.sort(key=lambda e: GROUPS.index(e.group))
    return out


def getting_started_html(mw=None) -> str:
    steps = [
        ("Open a project.", "Home > Open or Example for a Studio project, Drone > New drone or Open for a drone. "
                            "Or type <code>open gripper</code> in the console at the bottom."),
        ("Check the numbers.", "In Drone mode the table on the left lists every parameter with a status "
                               "(estimate, datasheet, measured, user). Change what you know; the rest stays marked "
                               "as an estimate."),
        ("Run it.", "Press <b>Run all</b> (F5) in Drone mode, or <b>Solve</b> in Studio, or type "
                    "<code>run structural on left finger</code>. The <b>Progress Monitor</b> on the left shows "
                    "every step and any failure."),
        ("Look at the results.", "Press the <b>Results</b> button (Ctrl+Shift+R). The Results Hub has the "
                                 "contours, plots, the audit (solver, mesh, inputs, log) and every earlier run. "
                                 "If a 3D view stays black, use <b>Open in browser</b> or <b>Show in 3D viewer</b>."),
        ("Change something and compare.", "Edit a parameter and run again, or "
                                          "<code>iterate finger thickness from 2 mm to 5 mm in 10 steps</code>. "
                                          "The History tab of the Results Hub compares two runs."),
    ]
    items = "".join(f"<li><b>{html.escape(t)}</b> {d}</li>" for t, d in steps)
    return (f"<h3>Getting started</h3><ol>{items}</ol>"
            "<p>Stuck? Type <code>help</code> or press F1 any time. Simulation results plan the tests; they are "
            "not flight approval.</p>")


def shortcut_rows(mw=None) -> list:
    """[(section, keys, what)]"""
    rows = [("Everywhere", "F1", "This help"),
            ("Everywhere", "Ctrl+K", "Command palette: type a few letters, press Enter"),
            ("Everywhere", "Ctrl+` or F12", "Show or hide the command console"),
            ("Everywhere", "Ctrl+Shift+R", "Open the Results Hub"),
            ("Console", "Tab", "Complete the command"),
            ("Console", "Up / Down", "Recall earlier commands"),
            ("Console", "Enter", "Run the line")]
    try:
        from opencae.ui.drone.ux import SHORTCUTS
        rows += [("Drone mode", k, v) for k, v in SHORTCUTS if k != "F1"]
    except Exception:  # noqa: BLE001
        pass
    A = getattr(mw, "A", None) or {}
    seen = {r[1] for r in rows}
    for a in A.values():
        k = a.shortcut().toString() if hasattr(a, "shortcut") else ""
        if k and k not in seen and k != "F1":
            seen.add(k)
            rows.append(("Studio", k, a.text().replace("&", "").replace("…", "")))
    return rows


def shortcuts_html(mw=None) -> str:
    out, last = ["<h3>Keyboard shortcuts</h3>"], None
    for sec, keys, what in shortcut_rows(mw):
        if sec != last:
            if last is not None:
                out.append("</table>")
            out.append(f"<h4>{html.escape(sec)}</h4><table cellpadding='3'>")
            last = sec
        out.append(f"<tr><td><code>{html.escape(keys)}</code></td><td>{html.escape(what)}</td></tr>")
    out.append("</table>")
    return "".join(out)


def stored_paths(mw=None) -> list:
    """[(what, path)] for the places a person may want to look at."""
    from opencae.utilities.crash import home_dir
    from opencae.utilities.logging_setup import log_dir
    home = home_dir()
    rows = []
    proj = None
    ws = getattr(mw, "_drone", None)
    if ws is not None and getattr(ws, "project_dir", None):
        proj = Path(ws.project_dir)
    if proj is not None:
        rows += [("Results of this drone (one folder per variant)", str(proj / "results")),
                 ("Run history of this drone", str(proj / "results" / "runs.jsonl")),
                 ("Parameters of this drone", str(proj / "drone.json"))]
    else:
        rows.append(("Results of a drone project", "<project folder>/results/<variant>/results.json"))
    rows += [("Results of a Studio project", "<name>.ocae_data/results/ next to your .ocae file"),
             ("Projects listed in the Results Hub", str(home / "hub" / "projects.json")),
             ("Log file (send this when something goes wrong)", str(log_dir() / "opencae.log")),
             ("Crash report", str(log_dir() / "crash.log")),
             ("Console history", str(home / "console_history.txt")),
             ("Settings (solver paths, webview_graphics)", str(home / "settings.json"))]
    return rows


def stored_html(mw=None) -> str:
    rows = "".join(f"<tr><td>{html.escape(w)}</td><td><code>{html.escape(p)}</code></td></tr>"
                   for w, p in stored_paths(mw))
    return ("<h3>Where things are stored</h3>"
            f"<table cellpadding='4'>{rows}</table>"
            "<p>Everything lives in your OpenCAE folder and in your own project folders. Nothing is sent "
            "anywhere. If the 3D view inside the window is blank on your PC, set "
            "<code>\"webview_graphics\": \"software\"</code> (default on Windows), <code>\"gpu\"</code> or "
            "<code>\"off\"</code> in the settings file and restart; the Results Hub can also be opened in your "
            "browser.</p>")


# ------------------------------------------------------------------------------------------------ the dialog
class HelpDialog(QDialog):
    """Non-modal. ``insert_requested(text)`` goes to the console (the controller connects it)."""

    def __init__(self, mw=None, insert=None, parent=None):
        super().__init__(parent or mw)
        self.mw = mw
        self._insert = insert
        self.setWindowTitle("OpenCAE Help")
        self.setObjectName("helpDialog")
        self.resize(980, 640)
        self.entries = command_reference()
        root = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search the commands: run, motion, iterate, open ...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        top.addWidget(self.search, 1)
        self.guide_btn = QPushButton("Full user guide")
        self.guide_btn.setToolTip("Open the whole manual")
        self.guide_btn.clicked.connect(self._open_guide)
        top.addWidget(self.guide_btn)
        root.addLayout(top)
        body = QHBoxLayout()
        self.nav = QListWidget()
        self.nav.setMaximumWidth(190)
        for key in SECTIONS:
            self.nav.addItem(SECTION_TITLES[key])
        body.addWidget(self.nav)
        self.pages = QStackedWidget()
        body.addWidget(self.pages, 1)
        root.addLayout(body, 1)

        cmd = QWidget()
        cl = QVBoxLayout(cmd)
        cl.setContentsMargins(0, 0, 0, 0)
        self.hint = QLabel("Click a command to put its example in the console, then press Enter to run it.")
        self.hint.setStyleSheet(f"color:{theme.FG_DIM}")
        cl.addWidget(self.hint)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Command", "Example", "What it does"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setWordWrap(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setColumnWidth(0, 330)
        self.tree.setColumnWidth(1, 260)
        self.tree.itemClicked.connect(self._clicked)
        self.tree.itemDoubleClicked.connect(self._clicked)
        self.tree.itemActivated.connect(self._clicked)
        self.empty = QLabel("No command matches. Try one word, such as run or show.")
        self.empty.setStyleSheet(f"color:{theme.FG_DIM}")
        self.empty.hide()
        cl.addWidget(self.tree, 1)
        cl.addWidget(self.empty)
        self.pages.addWidget(cmd)
        self.browsers: dict = {}
        for key, fn in (("start", getting_started_html), ("shortcuts", shortcuts_html), ("stored", stored_html)):
            tb = QTextBrowser()
            tb.setOpenExternalLinks(True)
            self.browsers[key] = (tb, fn)
            self.pages.addWidget(tb)
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self._build_tree()
        self.refresh_pages()
        self.nav.setCurrentRow(0)

    # ---- content
    def refresh_pages(self) -> None:
        for tb, fn in self.browsers.values():
            tb.setHtml(fn(self.mw))

    def _build_tree(self) -> None:
        self.tree.clear()
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.Monospace)
        groups: dict = {}
        for e in self.entries:
            g = groups.get(e.group)
            if g is None:
                g = QTreeWidgetItem([e.group])
                f = g.font(0)
                f.setBold(True)
                g.setFont(0, f)
                g.setFlags(Qt.ItemIsEnabled)
                self.tree.addTopLevelItem(g)
                groups[e.group] = g
            it = QTreeWidgetItem([e.syntax, e.example, e.text])
            it.setData(0, Qt.UserRole, e)
            it.setFont(0, mono)
            it.setFont(1, mono)
            it.setToolTip(2, e.text)
            it.setToolTip(1, "Click to put this in the console")
            g.addChild(it)
        self.tree.expandAll()
        self.tree.resizeColumnToContents(1)

    def visible_entries(self) -> list:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            for j in range(g.childCount()):
                c = g.child(j)
                if not c.isHidden():
                    out.append(c.data(0, Qt.UserRole))
        return out

    def _filter(self, text: str) -> None:
        words = text.lower().split()
        if words:
            self.nav.setCurrentRow(0)
        shown = 0
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            n = 0
            for j in range(g.childCount()):
                c = g.child(j)
                ok = all(w in c.data(0, Qt.UserRole).haystack() for w in words)
                c.setHidden(not ok)
                n += ok
            g.setHidden(n == 0)
            shown += n
        self.empty.setVisible(shown == 0)

    # ---- actions
    def show_section(self, key: str = "commands") -> None:
        i = SECTIONS.index(key) if key in SECTIONS else 0
        self.nav.setCurrentRow(i)
        self.show()
        self.raise_()
        self.activateWindow()
        if i == 0:
            self.search.setFocus()

    def insert_example(self, entry: Entry) -> None:
        if self._insert is not None:
            self._insert(entry.example)

    def _clicked(self, item, _col=0) -> None:
        e = item.data(0, Qt.UserRole)
        if isinstance(e, Entry):
            self.insert_example(e)

    def _open_guide(self) -> None:
        try:
            self.mw.cmd_guide()
        except Exception:  # noqa: BLE001
            log.exception("could not open the user guide")


# ------------------------------------------------------------------------------------------------ the extension
class HelpController(QObject):
    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        self.dialog: Optional[HelpDialog] = None
        self.button: Optional[QPushButton] = None
        self.action: Optional[QAction] = None

    def console(self):
        return getattr(self.mw, "extensions", {}).get("opencae.ui.console")

    def insert_into_console(self, text: str) -> bool:
        con = self.console()
        if con is None or not hasattr(con, "insert_example"):
            return False
        con.insert_example(text)
        return True

    def show(self, section: str = "commands") -> HelpDialog:
        if self.dialog is None:
            self.dialog = HelpDialog(self.mw, insert=self.insert_into_console)
        else:
            self.dialog.refresh_pages()
        self.dialog.show_section(section)
        return self.dialog


def install(mw) -> HelpController:
    existing = getattr(mw, "_help_controller", None)
    if existing is not None:
        return existing
    ctl = HelpController(mw)
    mw._help_controller = ctl
    # the corner button: top-right of the ribbon, on every page
    btn = QPushButton("Help")
    btn.setObjectName("helpButton")
    btn.setToolTip("Commands you can type in the console, getting started, shortcuts (F1)")
    btn.setCursor(Qt.PointingHandCursor)
    btn.clicked.connect(lambda checked=False: ctl.show("commands"))
    ctl.button = btn
    ribbon = getattr(mw, "ribbon", None)
    if ribbon is not None:
        ribbon.setCornerWidget(btn, Qt.TopRightCorner)
        btn.show()
    # F1: the Studio "User guide" action opens this dialog (the manual stays one click away inside it)
    A = getattr(mw, "A", {})
    guide = A.get("guide")
    if guide is not None:
        try:
            guide.triggered.disconnect()
        except (RuntimeError, TypeError):
            pass
        guide.triggered.connect(lambda checked=False: ctl.show("commands"))
        guide.setText("Help: commands and getting started")
        guide.setToolTip("Commands you can type in the console, getting started, shortcuts (F1)")
        ctl.action = guide
    else:
        act = QAction("Help: commands and getting started", mw)
        act.setShortcut(QKeySequence("F1"))
        act.triggered.connect(lambda checked=False: ctl.show("commands"))
        mw.addAction(act)
        ctl.action = act
        for a in mw.menuBar().actions():
            if a.text().replace("&", "") == "Help" and a.menu() is not None:
                a.menu().insertAction(a.menu().actions()[0] if a.menu().actions() else None, act)
    manual = QAction("User guide (full manual)", mw)
    manual.triggered.connect(lambda checked=False: mw.cmd_guide())
    for a in mw.menuBar().actions():
        if a.text().replace("&", "") == "Help" and a.menu() is not None:
            a.menu().insertAction(a.menu().actions()[1] if len(a.menu().actions()) > 1 else None, manual)
            break
    ctl.manual_action = manual
    return ctl
