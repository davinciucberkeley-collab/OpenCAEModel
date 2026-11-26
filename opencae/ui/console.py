"""Command console: the right-hand dock of the main window where you type commands (or Python) and they run.

``install(main_window)`` is called by ``MainWindow._install_extensions``. Two modes:

* Commands (default): natural lines such as ``open gripper``, ``select left finger``, ``run structural on left
  finger``, ``run motion on gripper``, ``iterate finger thickness from 2 mm to 5 mm in 10 steps``, ``show
  displacement``, ``open dashboard`` (grammar in ``opencae.ui.commands.grammar``, executed through the existing
  services by ``opencae.ui.commands.actions``) and the original CLI-style commands (``run mass propulsion``,
  ``set key value``, ``iterate name key=value``, ...) for the drone project. Missing information is asked in the
  panel (choices or a short form), replies are one line with a link to the job in the Progress Monitor, and clickable
  suggestions appear under the input. Ctrl+K opens the same commands as a palette.
* Python (prefix ``>>>`` or the Python toggle): runs your own code with ``app, win, project, spec, pipeline,
  spec_mod, geometry, np, results`` in scope. It is your local app, so your code runs as written; long-running
  code blocks the window until it ends. A line that is not valid Python but is a console command (``open new``,
  ``run cfd``, ``help``) runs as a command, with a small note.

Default layout (also View > Reset layout): Progress Monitor on the left (~260 px), the centre stack (Studio viewport,
Drone mode, Results Hub), the Command Console along the bottom over the full width (~180 px). A layout saved by an
earlier session always wins at start.
"""
from __future__ import annotations

import ast
import codeop
import contextlib
import difflib
import html
import io
import json
import os
import shlex
import sys
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (QDockWidget, QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QListWidget, QPushButton,
                               QTextBrowser, QVBoxLayout, QWidget)

from opencae.ui.commands.actions import cmd_link  # noqa: E402

HISTORY_MAX = 500
PY_SCOPE = ("app", "win", "project", "spec", "pipeline", "spec_mod", "geometry", "np", "results")
DEFAULT_LEFT_WIDTH = 260       # Progress Monitor (screenshot layout of 2026-11-26)
DEFAULT_CONSOLE_HEIGHT = 180   # Command Console along the bottom, full width
ANALYSES = ("mass", "propulsion", "structure", "cfd", "flight")
VERDICT_TAGS = {"ready for physical tests": "ok", "conditional": "warn", "not ready": "err"}
STATUSES = ("estimate", "datasheet", "measured", "user")
COLORS = {"cmd": "#7aa2f7", "ok": "#9ece6a", "err": "#f7768e", "info": "#c0caf5", "dim": "#7f849c",
          "warn": "#e0af68", "progress": "#73daca"}

# name -> (usage, one-line description, example)
COMMANDS: dict = {
    "help": ("help [command]", "List every command, or explain one.", "help run"),
    "open": ("open <dir>", "Open a drone project (a folder with drone.json) in Drone mode.", "open C:\\drones\\my_quad"),
    "new": ("new <dir> --cad <file> [--type multirotor|fixed_wing|tailsitter|vtol] [--name n]",
            "Create a project from a CAD file (STEP, STL, OBJ) and open it.", "new mydrone --cad frame.stl"),
    "example": ("example quad250|k09l", "Open a bundled example project.", "example quad250"),
    "run": ("run [mass propulsion structure cfd flight] [--preset coarse|medium|fine] [--force] [--variant v]",
            "Run the analyses (all when none are named). Cached ones are skipped unless --force.",
            "run mass propulsion"),
    "stop": ("stop", "Stop the running analysis after its current step.", "stop"),
    "status": ("status [dir]", "What has been run and what is out of date.", "status"),
    "results": ("results [analysis] [--variant v]", "Plain-language summary and the key numbers.",
                "results propulsion"),
    "checks": ("checks [--variant v]", "Requirement checks and the overall verdict.", "checks"),
    "set": ("set <key> <value> [estimate|datasheet|measured|user]",
            "Change one parameter in drone.json (validated, saved, sheet refreshed).",
            "set propulsion.battery.capacity_mah 2200 datasheet"),
    "get": ("get <key>", "Show one parameter with its unit and evidence status.", "get propulsion.battery.cells"),
    "params": ("params [filter]", "List the parameters (optionally only keys containing the filter).",
               "params battery"),
    "iterate": ("iterate <name> <key>=<value> ...", "Run a named design variant with parameter changes.",
                "iterate big_battery propulsion.battery.capacity_mah=2200"),
    "compare": ("compare <variant>", "Numbers of a variant next to the baseline.", "compare big_battery"),
    "variants": ("variants", "List the variants that have results.", "variants"),
    "history": ("history", "Earlier runs: when, how long, verdict.", "history"),
    "clean": ("clean [--results]", "Delete solver scratch folders (and with --results, all results).",
              "clean --results"),
    "setup": ("setup [openfoam]", "Show the airflow solver; 'setup openfoam' (about 1-2 GB; on Windows inside WSL) "
              "downloads the open-source solver into your OpenCAE folder and tests it.", "setup openfoam"),
    "doctor": ("doctor", "Check which solvers and libraries are installed.", "doctor"),
    "mode": ("mode studio|drone", "Switch the application between Studio and Drone mode.", "mode drone"),
    "dashboard": ("dashboard", "Open the project dashboard in your browser.", "dashboard"),
    "clear": ("clear", "Clear this window.", "clear"),
    "python": ("python on|off   (or start a line with >>>)", "Python mode: type code; app, win, project, spec, "
               "pipeline, spec_mod, geometry, np, results are in scope.", ">>> spec['name']"),
}


# ------------------------------------------------------------------------------------------------ helpers
def history_path() -> Path:
    base = os.environ.get("OPENCAE_HOME") or str(Path.home() / ".opencae")
    return Path(base) / "console_history.txt"


def load_history() -> list:
    try:
        lines = history_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [ln for ln in lines if ln.strip()][-HISTORY_MAX:]


def save_history(lines: list) -> None:
    try:
        p = history_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines[-HISTORY_MAX:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def leaf_keys(spec: dict) -> list:
    """Dotted keys of the editable values in a drone.json dict (numbers/text/booleans/null and {value:...})."""
    out: list = []

    def walk(node, prefix):
        if isinstance(node, dict):
            if "value" in node and not any(isinstance(v, dict) for v in node.values()):
                out.append(prefix)
                return
            for k, v in node.items():
                if str(k).startswith("_"):
                    continue
                walk(v, f"{prefix}.{k}" if prefix else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, (dict, list)):
                    walk(v, f"{prefix}.{i}")
        elif prefix:
            out.append(prefix)
    walk(spec, "")
    return sorted(set(out))


def split_line(line: str) -> list:
    try:
        toks = shlex.split(line, posix=(os.name != "nt"))
    except ValueError:
        toks = line.split()
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in toks]


def _num(v) -> str:
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def _verdict_tag(verdict) -> str:
    return VERDICT_TAGS.get(str(verdict), "info")


class _Fail(Exception):
    """A user-facing command error (printed in red, never a traceback)."""


# ------------------------------------------------------------------------------------------------ widgets
MONO = "Consolas,'DejaVu Sans Mono',monospace"


class FlowLayout(QLayout):
    """Chips that wrap onto the next line when the panel is narrow."""

    def __init__(self, parent=None, hspacing: int = 6, vspacing: int = 6):
        super().__init__(parent)
        self._items: list = []
        self._h, self._v = hspacing, vspacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):  # noqa: N802
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):  # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):  # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self):  # noqa: N802
        return True

    def heightForWidth(self, w):  # noqa: N802
        return self._layout(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _layout(self, rect, test: bool) -> int:
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = r.x(), r.y(), 0
        for it in self._items:
            w = it.sizeHint()
            if x + w.width() > r.right() + 1 and line_h > 0:
                x, y, line_h = r.x(), y + line_h + self._v, 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), w))
            x += w.width() + self._h
            line_h = max(line_h, w.height())
        return y + line_h - rect.y() + m.bottom()


CHIP_CSS = ("QPushButton#chip { background:#252a33; color:#c0caf5; border:1px solid #3a414d; border-radius:10px; "
            "padding:2px 10px; font-size:11px; } "
            "QPushButton#chip:hover { background:#2f4a66; border-color:#4c9be8; color:white; } "
            "QPushButton#chip:pressed { background:#4c9be8; }")


def make_chip(label: str, command: str, on_click: Callable) -> QPushButton:
    b = QPushButton(label)
    b.setObjectName("chip")
    b.setCursor(Qt.PointingHandCursor)
    b.setFocusPolicy(Qt.NoFocus)
    b.setToolTip(command if command != label else "Click to run")
    b.setStyleSheet(CHIP_CSS)
    b.clicked.connect(lambda: on_click(command))
    return b


class ChipBar(QWidget):
    """Clickable suggestions under the input: 'did you mean ...', next steps, choices."""

    def __init__(self, on_click: Callable, parent=None):
        super().__init__(parent)
        self.on_click = on_click
        self.flow = FlowLayout(self)
        self.chips: list = []
        self.setVisible(False)

    def set_chips(self, items: list) -> None:
        for b in self.chips:
            self.flow.removeWidget(b)
            b.deleteLater()
        self.chips = []
        seen: set = set()
        for it in items:
            label, command = (it, it) if isinstance(it, str) else it
            if command in seen:
                continue
            seen.add(command)
            b = make_chip(label, command, self.on_click)
            self.flow.addWidget(b)
            self.chips.append(b)
        self.setVisible(bool(self.chips))
        self.updateGeometry()

    def labels(self) -> list:
        return [b.text() for b in self.chips]


class ClarifyCard(QFrame):
    """The question panel: choices as chips, or a short inline form (Start / Cancel)."""

    def __init__(self, console):
        super().__init__()
        self.console = console
        self.setObjectName("clarifyCard")
        self.setStyleSheet("QFrame#clarifyCard { background:#1b2230; border:1px solid #3a5a80; "
                           "border-left:3px solid #4c9be8; border-radius:4px; } QLabel { background:transparent; }")
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(5)
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-weight:600; color:#c0caf5;")
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color:#8b95a5; font-size:11px;")
        self.body = QVBoxLayout()
        self.body.setSpacing(4)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color:#f7768e;")
        self.error.setVisible(False)
        self.buttons = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.setStyleSheet("QPushButton { background:#4c9be8; color:white; border:none; border-radius:4px; "
                                     "padding:4px 14px; font-weight:600; } QPushButton:hover { background:#6aaef0; }")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setToolTip("Drop this question (Esc)")
        self.cancel_btn.setStyleSheet("QPushButton { background:transparent; color:#8b95a5; border:1px solid #3a414d; "
                                      "border-radius:4px; padding:4px 10px; } QPushButton:hover { color:white; }")
        self.start_btn.clicked.connect(self._submit)
        self.cancel_btn.clicked.connect(lambda: console.cancel_pending())
        self.buttons.addStretch(1)
        self.buttons.addWidget(self.cancel_btn)
        self.buttons.addWidget(self.start_btn)
        v.addWidget(self.title)
        v.addWidget(self.note)
        v.addLayout(self.body)
        v.addWidget(self.error)
        v.addLayout(self.buttons)
        self.form = None
        self.chips: Optional[ChipBar] = None
        self.setVisible(False)

    def _clear_body(self) -> None:
        while self.body.count():
            it = self.body.takeAt(0)
            if it.widget() is not None:
                it.widget().deleteLater()
        self.form, self.chips = None, None
        self.error.setVisible(False)

    def show_choices(self, question: str, choices: list, on_pick: Callable) -> None:
        self._clear_body()
        self.title.setText(question)
        self.note.setVisible(False)
        self.chips = ChipBar(on_pick)
        self.chips.set_chips([(label, value) for label, value in choices])
        self.body.addWidget(self.chips)
        self.start_btn.setVisible(False)
        self.setVisible(True)

    def show_form(self, title: str, fields: list, note: str = "") -> None:
        from opencae.ui.forms import FormWidget
        self._clear_body()
        self.title.setText(title)
        self.note.setText(note)
        self.note.setVisible(bool(note))
        self.form = FormWidget(fields)
        self.form.setStyleSheet("QWidget { background:transparent; }")
        self.body.addWidget(self.form)
        self.start_btn.setVisible(True)
        self.setVisible(True)

    def clear(self) -> None:
        self._clear_body()
        self.setVisible(False)

    def _submit(self) -> None:
        self.console.submit_form()

    def show_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.setVisible(bool(text))


class _Pending:
    def __init__(self, kind: str, question: str, choices=None, on_choice=None, on_cancel=None, submit=None):
        self.kind, self.question, self.choices = kind, question, choices or []
        self.on_choice, self.on_cancel, self.submit = on_choice, on_cancel, submit


class _Output(QTextBrowser):
    def __init__(self):
        super().__init__()
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setObjectName("consoleOutput")
        self.setStyleSheet(f"QTextBrowser {{ background:#11131a; color:#c0caf5; font-family:{MONO}; font-size:10pt; }}")
        self.document().setDefaultStyleSheet("a { color:#7aa2f7; text-decoration:none; } "
                                             "th { color:#7f849c; font-weight:normal; }")


class _Input(QLineEdit):
    """One-line input: Up/Down history (or the suggestion list), Tab completion, multi-line paste."""

    def __init__(self, console):
        super().__init__()
        self.console = console
        self.setFocusPolicy(Qt.StrongFocus)

    def event(self, e):  # Tab must reach us before the focus chain takes it
        if e.type() == e.Type.KeyPress and e.key() == Qt.Key_Tab:
            self.console.tab_complete()
            return True
        return super().event(e)

    def keyPressEvent(self, e):
        c = self.console
        pop = c.popup
        if pop.isVisible():
            if e.key() in (Qt.Key_Up, Qt.Key_Down):
                n = pop.count()
                row = pop.currentRow()
                row = (0 if e.key() == Qt.Key_Down else n - 1) if row < 0 else \
                    max(0, min(n - 1, row + (1 if e.key() == Qt.Key_Down else -1)))
                pop.setCurrentRow(row)
                c._navigated = True
                return
            if e.key() in (Qt.Key_Return, Qt.Key_Enter) and c._navigated and pop.currentItem() is not None:
                c.accept_suggestion(pop.currentItem().text())
                return
            if e.key() == Qt.Key_Escape:
                c.hide_popup()
                return
        if e.key() == Qt.Key_Escape and c.pending is not None:
            c.cancel_pending()
            return
        if e.key() == Qt.Key_Up:
            c.history_step(-1)
        elif e.key() == Qt.Key_Down:
            c.history_step(1)
        elif e.matches(QKeySequence.Paste):
            from PySide6.QtWidgets import QApplication
            text = QApplication.clipboard().text()
            if "\n" in text.strip("\n"):
                c.run_block(text)
            else:
                super().keyPressEvent(e)
        else:
            super().keyPressEvent(e)


class Console(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.history: list = load_history()
        self._hpos = len(self.history)
        self.python_mode = False
        self._buf: list = []          # pending lines of an unfinished Python block
        self._ns: dict = {}
        self._watch_log = 0
        self._watching = False
        self._watch_kind = ""
        self._watch_proj: Optional[Path] = None
        self.pending: Optional[_Pending] = None
        self._navigated = False
        self._ctx_cache: tuple = (0.0, None)
        self.setMinimumWidth(280)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        self.output = _Output()
        self.output.anchorClicked.connect(self._link)
        lay.addWidget(self.output, 1)
        self.card = ClarifyCard(self)
        lay.addWidget(self.card)
        row = QHBoxLayout()
        self.prompt = QLabel()
        self.prompt.setStyleSheet(f"font-family:{MONO}; color:#7aa2f7;")
        self.input = _Input(self)
        self.input.setPlaceholderText("Type a command, e.g. run structural on left finger  (Tab completes)")
        self.input.setStyleSheet(f"QLineEdit {{ background:#11131a; color:#c0caf5; border:1px solid #2a2e3f; "
                                 f"font-family:{MONO}; font-size:10pt; padding:4px; }} "
                                 "QLineEdit:focus { border-color:#4c9be8; }")
        self.input.returnPressed.connect(self._enter)
        self.input.textEdited.connect(self._refresh_popup)
        self.commands_btn = QPushButton("Commands")
        self.commands_btn.setCheckable(True)
        self.commands_btn.setChecked(True)
        self.commands_btn.setToolTip("Commands mode: type plain commands such as run cfd or open gripper")
        self.commands_btn.clicked.connect(lambda checked=False: self.set_python_mode(False))
        self.toggle = QPushButton("Python")     # the Python end of the Commands | Python pair (checked = Python mode)
        self.toggle.setCheckable(True)
        self.toggle.setToolTip("Python mode: type Python code (or start any line with >>>). "
                               "Commands such as open new still work.")
        self.toggle.toggled.connect(self.set_python_mode)
        row.addWidget(self.prompt)
        row.addWidget(self.input, 1)
        row.addWidget(self.commands_btn)
        row.addWidget(self.toggle)
        lay.addLayout(row)
        self.chips = ChipBar(self.execute)
        lay.addWidget(self.chips)
        self.popup = QListWidget(self)
        self.popup.setObjectName("consolePopup")
        self.popup.setFocusPolicy(Qt.NoFocus)
        self.popup.setStyleSheet(f"QListWidget {{ background:#171a20; color:#c0caf5; border:1px solid #4c9be8; "
                                 f"font-family:{MONO}; font-size:10pt; }} "
                                 "QListWidget::item { padding:2px 6px; } "
                                 "QListWidget::item:selected { background:#2f4a66; color:white; }")
        self.popup.itemClicked.connect(lambda it: self.accept_suggestion(it.text()))
        self.popup.hide()
        self.timer = QTimer(self)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self._tick)
        from opencae.ui.commands.actions import Actions
        self.actions = Actions(self)
        self.palette = None
        self._update_prompt()
        self.say("OpenCAE console. Type a command like 'run structural on left finger', or 'help'. "
                 "Click the suggestions below the input.", "dim")
        self.set_chips(["help", "open gripper", "open dashboard"])

    # -------------------------------------------------------------- output
    def say(self, text: str, kind: str = "info") -> None:
        color = COLORS.get(kind, COLORS["info"])
        for line in str(text).split("\n"):
            self.output.append(f'<span style="color:{color}; white-space:pre-wrap;">'
                               f'{html.escape(line) or "&nbsp;"}</span>')
        self._scroll()

    def say_html(self, markup: str, kind: str = "info") -> None:
        color = COLORS.get(kind, COLORS["info"])
        self.output.append(f'<span style="color:{color};">{markup}</span>')
        self._scroll()

    def _scroll(self) -> None:
        sb = self.output.verticalScrollBar()
        sb.setValue(sb.maximum())

    def reply(self, text: str, kind: str = "info", html: bool = False, chips: Optional[list] = None) -> None:
        """One concise line (HTML when it carries a job link) and, optionally, the suggestions under the input."""
        if html:
            self.say_html(text, kind)
        else:
            self.say(text, kind)
        if chips is not None:
            self.set_chips(chips)

    def set_chips(self, items: list) -> None:
        self.chips.set_chips(items)

    def text(self) -> str:
        return self.output.toPlainText()

    @property
    def busy(self) -> bool:
        return self._watching

    def execute_command(self, line: str) -> None:
        """Run a line as if typed (chips, links and the palette use this)."""
        self.execute(line)

    def _link(self, url) -> None:
        s = url.toString(QUrl.FullyDecoded)
        scheme, _, rest = s.partition(":")
        if scheme == "job":
            ext = self.actions.ext
            if ext is not None:
                ext.show_monitor(rest)
        elif scheme == "cmd":
            self.execute(rest)
        elif scheme == "folder":
            QDesktopServices.openUrl(QUrl.fromLocalFile(rest))

    # -------------------------------------------------------------- questions (clarification)
    def ask(self, question: str, choices: list, on_choice: Callable, on_cancel: Optional[Callable] = None) -> None:
        """Ask in the panel with clickable choices ``[(label, value)]``; ``on_choice(value)`` continues."""
        self.cancel_pending(silent=True)
        self.pending = _Pending("choice", question, list(choices), on_choice, on_cancel)
        self.say(question, "warn")
        self.card.show_choices(question, choices, self._answer)
        self.set_chips([])

    def ask_form(self, title: str, fields: list, submit: Callable, note: str = "",
                 on_cancel: Optional[Callable] = None) -> None:
        """A short inline form; ``submit(values)`` returns an error text (the form stays) or '' (accepted)."""
        self.cancel_pending(silent=True)
        self.pending = _Pending("form", title, on_cancel=on_cancel, submit=submit)
        self.say(title + " — fill in the form below, then Start.", "warn")
        self.card.show_form(title, fields, note)
        self.set_chips([])

    def _answer(self, value) -> None:
        p = self.pending
        if p is None or p.kind != "choice":
            return
        label = next((lb for lb, v in p.choices if v == value), str(value))
        self.pending = None
        self.card.clear()
        self.say(f"> {label}", "cmd")
        p.on_choice(value)

    def submit_form(self) -> None:
        p = self.pending
        if p is None or p.kind != "form":
            return
        try:
            values = self.card.form.values()
        except ValueError as exc:
            self.card.show_error(str(exc))
            return
        try:
            err = p.submit(values)
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        if err:
            self.card.show_error(err)
        elif self.pending is p:       # nothing new was asked meanwhile
            self.pending = None
            self.card.clear()

    def cancel_pending(self, silent: bool = False) -> None:
        p, self.pending = self.pending, None
        self.card.clear()
        if p is not None:
            if not silent:
                self.say("Cancelled.", "dim")
            if p.on_cancel:
                p.on_cancel()

    def _match_choice(self, text: str):
        p = self.pending
        t = text.strip().lower()
        if p is None or p.kind != "choice":
            return None
        for label, value in p.choices:
            if t in (str(label).lower(), str(value).lower()):
                return value
        from opencae.ui.commands import grammar as G
        ents = [G.Entity(str(lb), "choice", str(v)) for lb, v in p.choices]
        r = G.resolve(t, ents, 0.8)
        return r.entity.id if r.entity is not None else None

    # -------------------------------------------------------------- suggestions (autocomplete popup)
    def _context(self, fresh: bool = False):
        now = time.time()
        if not fresh and self._ctx_cache[1] is not None and now - self._ctx_cache[0] < 0.5:
            return self._ctx_cache[1]
        ctx = self.actions.context()
        self._ctx_cache = (now, ctx)
        return ctx

    def suggestions(self, text: str, limit: int = 8) -> list:
        """Full command lines for what is typed: verbs, analyses, component and parameter names (fuzzy)."""
        if self.python_mode or text.lstrip().startswith(">>>") or not text.strip():
            return []
        from opencae.ui.commands import grammar as G
        try:
            nat = G.complete(text, self._context(), limit)
        except Exception:  # noqa: BLE001 - completion must never break typing
            nat = []
        head = text[: len(text) - len(text.split(" ")[-1])]
        leg = [head + w + ("" if w.endswith(("=", os.sep)) else "") for w in self.completions(text)]
        words = text.split()
        first = words[0].lower() if words else ""
        legacy_first = ("." in text.split(" ")[-1] or "=" in text or first in (
            "example", "mode", "python", "help", "compare", "checks", "clean", "setup", "new", "get", "params",
            "results"))
        merged = (leg + nat) if legacy_first else (nat + leg)
        out: list = []
        for m in merged:
            if m not in out and m.strip() != text.strip():
                out.append(m)
        return out[:limit]

    def _refresh_popup(self, text: str) -> None:
        self._navigated = False
        cands = self.suggestions(text)
        if not cands:
            self.hide_popup()
            return
        self.popup.clear()
        for c in cands:
            self.popup.addItem(c)
        self.popup.setCurrentRow(-1)
        row_h = max(self.popup.sizeHintForRow(0), 18)
        h = min(len(cands), 8) * row_h + 6
        pos = self.input.mapTo(self, QPoint(0, 0))
        self.popup.setGeometry(pos.x(), max(0, pos.y() - h - 2), max(self.input.width(), 220), h)
        self.popup.raise_()
        self.popup.show()

    def hide_popup(self) -> None:
        self.popup.hide()
        self._navigated = False

    def accept_suggestion(self, line: str) -> None:
        self.input.setText(line)
        self.input.setFocus()
        self.hide_popup()
        self._refresh_popup(line)

    # -------------------------------------------------------------- state
    def workspace(self, create: bool = False):
        ws = getattr(self.win, "_drone", None)
        if ws is None and create:
            ws = self.win.drone_workspace()
        return ws

    def project(self) -> Optional[Path]:
        ws = self.workspace()
        return Path(ws.project_dir) if ws is not None and ws.project_dir else None

    def _need_project(self, args: Optional[list] = None) -> Path:
        if args:  # a path argument: the last argument that is a project folder
            for a in reversed(args):
                p = Path(a)
                if p.is_file() and p.name == "drone.json":
                    p = p.parent
                if p.is_dir() and (p / "drone.json").exists():
                    args.remove(a)
                    return p
        p = self.project()
        if p is None:
            raise _Fail("No project open. Use: open <dir>  or  example quad250")
        return p

    def _need_server(self):
        ws = self.workspace()
        if ws is None or ws.project_dir is None:
            raise _Fail("No project open. Use: open <dir>  or  example quad250")
        if ws.server is None:
            raise _Fail("The dashboard server is not running for this project, so runs cannot start here.")
        return ws.server

    def _update_prompt(self) -> None:
        if self._buf:
            self.prompt.setText("...")
        elif self.python_mode:
            self.prompt.setText(">>>")
        else:
            p = self.project()
            name = p.name if p else (self.win.session.project.name if getattr(self.win, "mode", "") != "drone"
                                     and getattr(self.win.session.project, "studies", None) else "")
            name = name if name and name != "Untitled" else ""
            self.prompt.setText(f"{name[:18]} >" if name else "opencae >")

    def set_python_mode(self, on: bool) -> None:
        self.python_mode = bool(on)
        self._buf = []
        for btn, checked in ((self.toggle, self.python_mode), (self.commands_btn, not self.python_mode)):
            if btn.isChecked() != checked:
                btn.blockSignals(True)
                btn.setChecked(checked)
                btn.blockSignals(False)
        self._update_prompt()
        self.input.setPlaceholderText("Type Python code. Commands such as open new also work  (type help)"
                                      if self.python_mode else
                                      "Type a command, e.g. run structural on left finger  (Tab completes, help lists all)")
        self.say("Python mode. Your code runs as written; long-running code blocks the window until it ends. "
                 "Lines that are commands (open new, run cfd, help) still run as commands." if on else
                 "Commands mode.", "dim")

    # -------------------------------------------------------------- history / completion
    def history_step(self, d: int) -> None:
        if not self.history:
            return
        self.hide_popup()
        self._hpos = max(0, min(len(self.history), self._hpos + d))
        self.input.setText(self.history[self._hpos] if self._hpos < len(self.history) else "")

    def _remember(self, line: str) -> None:
        if line.strip() and (not self.history or self.history[-1] != line):
            self.history.append(line)
            del self.history[:-HISTORY_MAX]
            save_history(self.history)
        self._hpos = len(self.history)

    def _spec(self) -> dict:
        p = self.project()
        if p is None:
            return {}
        try:
            from opencae.drone import spec as S
            return S.load(p)
        except Exception:
            return {}

    def completions(self, text: str) -> list:
        """Candidates for the last word of ``text`` (full replacement words)."""
        if text.lstrip().startswith(">>>") or self.python_mode:
            return []
        parts = text.split(" ")
        word, before = parts[-1], [p for p in parts[:-1] if p]
        if not before:
            from opencae.ui.commands.grammar import VERBS
            return sorted(c for c in dict.fromkeys(list(COMMANDS) + list(VERBS)) if c.startswith(word))
        cmd = before[0]
        pool: list = []
        if cmd in ("set", "get", "params") and len(before) == 1:
            pool = leaf_keys(self._spec())
        elif cmd == "set" and len(before) == 3:
            pool = list(STATUSES)
        elif cmd == "iterate":
            if len(before) >= 2:
                pool = [k + "=" for k in leaf_keys(self._spec())]
        elif cmd in ("run", "results"):
            pool = list(ANALYSES) + (["--preset", "--force", "--variant", "structural", "thermal", "modal",
                                      "fluid", "motion"] if cmd == "run" else ["--variant"])
            if before[-1] == "--preset":
                pool = ["coarse", "medium", "fine"]
        elif cmd == "example":
            pool = ["quad250", "k09l"]
        elif cmd == "mode":
            pool = ["studio", "drone"]
        elif cmd == "python":
            pool = ["on", "off"]
        elif cmd == "help":
            pool = list(COMMANDS)
        elif cmd in ("compare", "checks"):
            pool = self._variants()
        elif cmd in ("open", "new", "status"):
            return self._paths(word)
        elif cmd == "clean":
            pool = ["--results"]
        return sorted(c for c in pool if c.startswith(word))

    def _paths(self, word: str) -> list:
        base = Path(word).expanduser()
        d, prefix = (base, "") if word.endswith(("/", "\\")) else (base.parent, base.name)
        try:
            return sorted(str(d / e.name) + (os.sep if e.is_dir() else "") for e in d.iterdir()
                          if e.name.startswith(prefix))[:50]
        except OSError:
            return []

    def tab_complete(self) -> None:
        """Tab: the highlighted suggestion, the only one, or the longest common start of several."""
        text = self.input.text()
        if self.popup.isVisible() and self.popup.currentItem() is not None:
            return self.accept_suggestion(self.popup.currentItem().text())
        cands = self.suggestions(text)
        if not cands:
            return
        if len(cands) == 1:
            line = cands[0]
            self.input.setText(line + ("" if line.endswith((" ", "=", os.sep)) else " "))
            self.hide_popup()
            self._refresh_popup(self.input.text())
            return
        common = os.path.commonprefix(cands)
        if len(common) > len(text):
            self.input.setText(common)
        self._refresh_popup(self.input.text())
        if self.popup.count():
            self.popup.setCurrentRow(0)
            self._navigated = True

    # -------------------------------------------------------------- entry points
    def _enter(self) -> None:
        line = self.input.text()
        self.input.clear()
        self.hide_popup()
        self.execute(line)

    def run_block(self, text: str) -> None:
        """A multi-line paste: Python runs as one block, commands run line by line."""
        lines = text.replace("\r\n", "\n").strip("\n").split("\n")
        if self.python_mode or lines[0].lstrip().startswith(">>>"):
            self._remember(lines[0].strip())
            src = "\n".join(ln[3:].lstrip(" ") if ln.lstrip().startswith(">>>") and i == 0 else ln
                            for i, ln in enumerate(lines))
            self.say(">>> " + "\n... ".join(src.split("\n")), "cmd")
            self._run_python(src)
        else:
            for ln in lines:
                self.execute(ln)

    def execute(self, line: str) -> None:
        """Run one typed line. Never raises."""
        try:
            self._execute(line)
        except _Fail as e:
            self.say(str(e), "err")
        except Exception as e:  # noqa: BLE001 - the console must never take the application down
            self.say(f"Something went wrong: {type(e).__name__}: {e}", "err")
        self._update_prompt()

    def _execute(self, line: str) -> None:
        stripped = line.strip()
        if self._buf:  # continuing a Python block
            if stripped:
                self._buf.append(line)
                self.say("... " + line, "cmd")
                if self._incomplete("\n".join(self._buf)):
                    return
            else:
                self.say("...", "cmd")
            src, self._buf = "\n".join(self._buf), []
            self._run_python(src)
            return
        if not stripped:
            return
        self._remember(line)
        self.set_chips([])
        if self.pending is not None and not self.python_mode and not stripped.startswith(">>>"):
            if stripped.lower() in ("cancel", "no", "stop", "never mind", "nevermind", "esc"):
                self.say(f"> {stripped}", "cmd")
                self.cancel_pending()
                return
            value = self._match_choice(stripped)
            if value is not None:
                self.say(f"> {stripped}", "cmd")
                p = self.pending
                self.pending = None
                self.card.clear()
                p.on_choice(value)
                return
            self.cancel_pending(silent=True)          # a new command replaces the open question
        if stripped.startswith(">>>"):
            code = stripped[3:].lstrip()
            self._python_line(code)
        elif self.python_mode:
            if stripped.startswith("!"):
                self.say(f"> {stripped[1:].strip()}", "cmd")
                self._command(stripped[1:].strip())
            else:
                self._python_line(line)
        else:
            self.say(f"> {stripped}", "cmd")
            self._command(stripped)

    # -------------------------------------------------------------- python
    @staticmethod
    def _incomplete(src: str) -> bool:
        try:
            return codeop.compile_command(src, "<console>", "exec") is None
        except (SyntaxError, ValueError, OverflowError):
            return False

    def _python_line(self, code: str) -> None:
        self.say(">>> " + code, "cmd")
        if self._incomplete(code):
            self._buf = [code]
            return
        if self._is_command(code):
            self.say("(ran as a command)", "dim")
            self._command(code.strip())
            return
        self._run_python(code)

    def _is_command(self, code: str) -> bool:
        """True when a line typed in Python mode is a console command and not Python: ``open new``, ``run cfd``,
        ``help`` and ``?`` (a SyntaxError in Python), or a bare command word that is not a name here (``status``)."""
        s = code.strip()
        if not s or s.startswith(("!", "#")):
            return False
        low = s.lower()
        if low in ("help", "?"):
            return True
        try:
            tree = ast.parse(s, "<console>", "exec")
            if not (len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr)
                    and isinstance(tree.body[0].value, ast.Name)):
                return False                      # valid Python that is more than a bare word: Python wins
            name = tree.body[0].value.id
            import builtins
            if name in self._ns or hasattr(builtins, name) or name in PY_SCOPE:
                return False
        except SyntaxError:
            pass
        try:
            from opencae.ui.commands import grammar as G
            return G.parse(s, self._context()).verb not in ("unknown", "empty", "python")
        except Exception:  # noqa: BLE001 - the grammar must never break Python mode
            return False

    def _namespace(self) -> dict:
        ns = self._ns
        from PySide6.QtWidgets import QApplication
        proj = self.project()
        ns.update(app=QApplication.instance(), win=self.win, project=proj)
        try:
            import numpy as np
            ns["np"] = np
        except Exception:
            pass
        for name, mod in (("pipeline", "opencae.drone.pipeline"), ("spec_mod", "opencae.drone.spec"),
                          ("geometry", "opencae.drone.geometry")):
            try:
                ns[name] = __import__(mod, fromlist=["x"])
            except Exception:
                ns.setdefault(name, None)
        ns["spec"] = self._spec() if proj else None
        ns["results"] = self._load_results(proj, "baseline") if proj else None
        return ns

    def _run_python(self, src: str) -> None:
        ns = self._namespace()
        out = io.StringIO()
        try:
            tree = ast.parse(src, "<console>", "exec")
            last = None
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                last = ast.Expression(tree.body.pop().value)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                exec(compile(tree, "<console>", "exec"), ns)
                if last is not None:
                    value = eval(compile(last, "<console>", "eval"), ns)
                    if value is not None:
                        ns["_"] = value
                        print(repr(value))
        except SystemExit:
            out.write("(exit() is ignored here; close the window to quit)\n")
        except SyntaxError as e:
            where = f" (line {e.lineno})" if e.lineno and "\n" in src.strip() else ""
            self.say(f"SyntaxError: {e.msg}{where}", "err")
            self.say("Type `help` for commands, or switch to Commands mode.", "warn")
            return
        except BaseException:  # noqa: BLE001 - user code may raise anything; show it, keep running
            tb = traceback.format_exc().split("\n")
            # drop the console's own frames (everything above the first <console> frame)
            keep = [ln for i, ln in enumerate(tb) if i == 0 or "console.py" not in ln]
            out.write("\n".join(keep))
        text = out.getvalue().rstrip("\n")
        if text:
            self.say(text, "err" if "Traceback (most recent call last)" in text else "ok")

    # -------------------------------------------------------------- commands
    def _command(self, line: str) -> None:
        """Parse the line with the command grammar and run it; the CLI-style commands keep their own handlers."""
        from opencae.ui.commands import grammar as G
        ctx = self._context(fresh=True)
        it = G.parse(line, ctx)
        v = it.verb
        if v == "empty":
            return
        if v == "legacy":
            return self._legacy(it.legacy or line)
        if v == "help":
            return self.c_help([it.target] if it.target else [])
        if v == "clear":
            return self.c_clear([])
        if v == "history" and ctx.drone and it.target != "commands":
            return self.c_history([])
        if v == "stop":
            th = getattr(self, "_setup_thread", None)
            if th is not None and th.is_alive():
                return self.c_stop([])
        if v == "status":
            self.actions.status(it)
            if ctx.drone:
                self.c_status([])
            return
        self.actions.execute(it)

    def _legacy(self, line: str) -> None:
        toks = split_line(line)
        if not toks:
            return
        name, args = toks[0].lower(), toks[1:]
        fn: Optional[Callable] = getattr(self, "c_" + name, None) if name in COMMANDS else None
        if fn is None:
            near = difflib.get_close_matches(name, list(COMMANDS), n=2, cutoff=0.5)
            hint = f" Did you mean: {', '.join(near)}?" if near else ""
            raise _Fail(f"I do not know the command '{name}'.{hint} Type 'help' to see them all.")
        fn(args)

    @staticmethod
    def _flag(args: list, name: str, value: bool = False):
        """Remove ``--name`` (and its value) from args; returns True/the value/None."""
        if name in args:
            i = args.index(name)
            args.pop(i)
            if value:
                if i >= len(args):
                    raise _Fail(f"{name} needs a value")
                return args.pop(i)
            return True
        return None if value else False

    def c_help(self, args: list) -> None:
        from opencae.ui.commands.grammar import VERBS, VERB_WORDS
        if args:
            c = args[0].lower()
            c = VERB_WORDS.get(c, c)
            if c in VERBS:
                u, d, ex = VERBS[c]
                self.say_html(f"{html.escape(u)}<br>&nbsp;&nbsp;{html.escape(d)}<br>&nbsp;&nbsp;example: "
                              f"{cmd_link(ex)}")
                return
            if c not in COMMANDS:
                raise _Fail(f"No command '{c}'. Type 'help'.")
            u, d, ex = COMMANDS[c]
            self.say(f"{u}\n  {d}\n  example: {ex}")
            return
        self.say("Type what you want; Tab completes, Up/Down recalls, click the suggestions. Examples (click to run):",
                 "dim")
        for _, (u, d, ex) in VERBS.items():
            if _ in ("help", "clear", "history"):
                continue
            self.say_html(f"&nbsp;&nbsp;{cmd_link(ex)} <span style='color:#7f849c;'>- {html.escape(d)}</span>")
        self.say("Drone projects also take the CLI commands (open <dir>, example quad250, run mass propulsion, "
                 "set <key> <value>, iterate <name> <key>=<value>, results, checks, params, compare, variants, "
                 "clean, setup, doctor, mode, dashboard). 'help <command>' explains one. Ctrl+K opens the "
                 "command palette.", "dim")
        self.say("Python: start a line with >>> or press the Python button. In scope: app, win, project, spec, "
                 "pipeline, spec_mod, geometry, np, results. Long-running code freezes the window until it "
                 "finishes; use 'run' for analyses.", "dim")
        self.set_chips(["open gripper", "run structural on left finger", "run motion on gripper",
                        "open dashboard", "status"])
        ctl = self.help_controller()
        if ctl is not None:
            ctl.show("commands")        # the searchable reference; click a command to put it in this input

    def help_controller(self):
        ext = getattr(self.win, "extensions", {}).get("opencae.ui.help")
        return ext if ext is not None else getattr(self.win, "_help_controller", None)

    def insert_example(self, text: str) -> None:
        """Put ``text`` in the input (Help dialog): shows the console, leaves Python mode for a plain command, and
        focuses the input with the cursor at the end. Nothing runs until Enter."""
        dock = getattr(self, "dock", None)
        if dock is not None:
            dock.show()
            dock.raise_()
        if self.python_mode and not text.lstrip().startswith((">>>", "!")):
            self.set_python_mode(False)
        self.input.setText(text)
        self.input.setFocus()
        self.input.setCursorPosition(len(text))
        self._refresh_popup(text)
        self.win.activateWindow()

    def c_clear(self, args: list) -> None:
        self.output.clear()

    def c_python(self, args: list) -> None:
        on = (args[0].lower() if args else "on") != "off"
        self.toggle.setChecked(on)

    def c_open(self, args: list) -> None:
        if not args:
            raise _Fail("Usage: open <dir>")
        p = Path(" ".join(args)).expanduser()
        if p.is_file() and p.suffix.lower() == ".ocae":        # a Studio / assembly project
            self.actions.go_studio()
            self.win.cmd_open(str(p))
            self.say(f"Opened {p}", "ok")
            return
        if not (p / "drone.json").exists() and not (p.is_file() and p.name == "drone.json"):
            raise _Fail(f"No drone.json in {p}")
        if self._watching:
            raise _Fail("A run is in progress. Use 'stop' first.")
        if self.win.open_drone_project(p):
            self.say(f"Opened {self.project()}", "ok")
        else:
            raise _Fail(f"Could not open {p}")

    def c_example(self, args: list) -> None:
        key = args[0] if args else ""
        if key not in ("quad250", "k09l"):
            raise _Fail("Usage: example quad250|k09l")
        self.win.enter_drone_mode()
        if self.workspace(True).open_example(key):
            self.say(f"Opened {self.project()}", "ok")
        else:
            raise _Fail("Could not open the example.")

    def c_new(self, args: list) -> None:
        args = list(args)
        cad = self._flag(args, "--cad", True)
        typ = self._flag(args, "--type", True) or "multirotor"
        name = self._flag(args, "--name", True)
        if not args or not cad:
            raise _Fail("Usage: new <dir> --cad <file> [--type multirotor] [--name n]")
        from opencae.drone import pipeline
        if not Path(cad).exists():
            raise _Fail(f"CAD file not found: {cad}")
        try:
            proj = pipeline.new_project(Path(args[0]), Path(cad), name or Path(args[0]).name, typ)
        except (ValueError, KeyError, OSError) as e:
            raise _Fail(str(e).strip("'\""))
        self.say(f"Created {proj}", "ok")
        self.win.open_drone_project(proj, show_roles=True)

    # ---- running
    def _start_watch(self, kind: str, proj: Path) -> None:
        self._watching, self._watch_kind, self._watch_log, self._watch_proj = True, kind, 0, proj
        self.timer.start()

    def _tick(self) -> None:
        ws = self.workspace()
        srv = getattr(ws, "server", None) if ws is not None else None
        if srv is None:
            self._watching = False
            self.timer.stop()
            return
        try:
            snap = srv.jobs.snapshot()
        except Exception:
            return
        log = snap.get("log") or []
        if len(log) < self._watch_log:
            self._watch_log = 0
        for ln in log[self._watch_log:]:
            self.say(f"  [{int(100 * float(snap.get('frac') or 0)):3d}%] {ln}", "progress")
        self._watch_log = len(log)
        if not snap.get("running"):
            self._watching = False
            self.timer.stop()
            err = snap.get("error")
            if err:
                self.say(f"Run ended: {err}", "err" if err != "Stopped." else "warn")
            else:
                res = snap.get("result") or {}
                self.say("Run finished." + (f" Verdict: {res['verdict']}" if isinstance(res, dict)
                                            and res.get("verdict") else ""),
                         _verdict_tag(res.get("verdict") if isinstance(res, dict) else None))
                self.say("Type 'results' for the numbers or 'checks' for the requirement checks.", "dim")
            reload = getattr(ws, "_reload_sheet", None)
            if callable(reload):
                reload()
            self._update_prompt()

    def c_run(self, args: list) -> None:
        args = list(args)
        force = self._flag(args, "--force")
        preset = self._flag(args, "--preset", True)
        variant = self._flag(args, "--variant", True) or "baseline"
        bad = [a for a in args if a not in ANALYSES]
        if bad:
            raise _Fail(f"Unknown analysis {', '.join(bad)}. Choose from {', '.join(ANALYSES)}.")
        if preset and preset not in ("coarse", "medium", "fine"):
            raise _Fail("--preset must be coarse, medium or fine")
        srv = self._need_server()
        if self._watching or srv.jobs.running:
            raise _Fail("A run is already in progress. Use 'stop' to stop it.")
        body: dict = {"force": force, "variant": variant}
        if args:
            body["only"] = args
        if preset:
            body["overrides"] = {"analyses.cfd.preset": preset}
        try:
            srv.run(body)
        except Exception as e:  # BadRequest etc.
            raise _Fail(f"Could not start the run: {e}")
        self.say("Run started" + (f": {', '.join(args)}" if args else " (all analyses)")
                 + ". The window stays usable; progress appears here.", "ok")
        self._start_watch("run", self.project())

    def c_stop(self, args: list) -> None:
        th = getattr(self, "_setup_thread", None)
        if th is not None and th.is_alive():
            self._setup_stop.set()
            self.say("Cancelling the solver set-up.", "warn")
            return
        srv = self._need_server()
        self.say("Stopping after the current step." if srv.stop_job().get("stopping") else "Nothing is running.",
                 "warn")

    def c_iterate(self, args: list) -> None:
        if len(args) < 2 or any("=" not in a for a in args[1:]):
            raise _Fail("Usage: iterate <name> <key>=<value> ...")
        from opencae import cli
        try:
            overrides = cli.parse_set(args[1:])
            cli._check_overrides(self._need_project(), overrides)
        except (cli.BadInput, ValueError) as e:
            raise _Fail(str(e))
        srv = self._need_server()
        if self._watching or srv.jobs.running:
            raise _Fail("A run is already in progress. Use 'stop' to stop it.")
        try:
            srv.iterate({"overrides": overrides, "name": args[0]})
        except Exception as e:
            raise _Fail(f"Could not start the variant: {e}")
        self.say(f"Variant '{args[0]}' started.", "ok")
        self._start_watch("iterate", self.project())

    # ---- information
    def c_status(self, args: list) -> None:
        proj = self._need_project(list(args))
        from opencae.drone import pipeline
        st = pipeline.status(proj)
        self.say(f"{st.get('project')}  ({st.get('type')}, engine {st.get('engine')})")
        for vname, v in (st.get("variants") or {}).items():
            self.say(f"  variant {vname}: {v.get('verdict') or 'no results yet'}", "info")
            for aid, a in (v.get("analyses") or {}).items():
                flag = " (out of date)" if a.get("stale") else ""
                self.say(f"    {aid:<11} {a.get('status', '?')}{flag}", "dim" if a.get("status") == "not run" else "info")

    @staticmethod
    def _load_results(proj: Path, variant: str) -> Optional[dict]:
        try:
            return json.loads((proj / "results" / variant / "results.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _results_or_fail(self, args: list) -> tuple:
        args = list(args)
        variant = self._flag(args, "--variant", True) or "baseline"
        proj = self._need_project(args)
        res = self._load_results(proj, variant)
        if res is None:
            raise _Fail(f"No results for '{variant}' yet. Use: run")
        return res, args

    def c_results(self, args: list) -> None:
        res, rest = self._results_or_fail(args)
        want = rest[0] if rest else None
        analyses = res.get("analyses") or {}
        if want and want not in analyses:
            raise _Fail(f"No results for '{want}'. Available: {', '.join(analyses) or 'none'}")
        for aid, a in analyses.items():
            if want and aid != want:
                continue
            kind = "ok" if a.get("status") in ("ok", "pass") else "err" if a.get("status") == "error" else "warn"
            self.say(f"{a.get('title', aid)}: {a.get('status')}", kind)
            if a.get("summary"):
                self.say("  " + a["summary"])
            for key, m in (a.get("metrics") or {}).items():
                if isinstance(m, dict) and m.get("value") is not None:
                    label = m.get("label") or key
                    self.say(f"    {label} [{key}] = {_num(m['value'])} {m.get('unit', '')}".rstrip(), "dim")
        if not want:
            self.say(f"Verdict: {res.get('verdict', 'n/a')}", _verdict_tag(res.get("verdict")))
            self.say("Simulation results plan the tests; they are not flight approval.", "dim")

    def c_checks(self, args: list) -> None:
        res, _ = self._results_or_fail(args)
        for c in res.get("checks") or []:
            st = c.get("status", "?")
            kind = {"pass": "ok", "fail": "err"}.get(st, "warn")
            self.say(f"  {st:<14} {c.get('label') or c.get('id')}: {_num(c.get('value'))} "
                     f"{c.get('op', '')} {_num(c.get('target'))}", kind)
            if c.get("why") and st != "pass":
                self.say(f"                 {c['why']}", "dim")
        self.say(f"Verdict: {res.get('verdict', 'n/a')}", _verdict_tag(res.get("verdict")))

    def c_history(self, args: list) -> None:
        proj = self._need_project(list(args))
        path = proj / "results" / "runs.jsonl"
        try:
            rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        except (OSError, ValueError):
            rows = []
        if not rows:
            self.say("No runs recorded yet.", "dim")
        for r in rows[-20:]:
            self.say(f"  {r.get('started', '?')}  {r.get('variant', '')}  {_num(r.get('duration_s', 0))} s  "
                     f"{r.get('verdict') or r.get('error') or ''}")

    def _variants(self) -> list:
        p = self.project()
        if p is None:
            return []
        try:
            from opencae.drone import pipeline
            return list(pipeline.variants(p))
        except Exception:
            return []

    def c_variants(self, args: list) -> None:
        proj = self._need_project(list(args))
        from opencae.drone import pipeline
        names = pipeline.variants(proj)
        self.say("  " + ", ".join(names) if names else "No variants with results yet.", "info")

    def c_compare(self, args: list) -> None:
        args = list(args)
        proj = self._need_project(args)
        if not args:
            raise _Fail("Usage: compare <variant>")
        base, var = self._load_results(proj, "baseline"), self._load_results(proj, args[0])
        if base is None or var is None:
            raise _Fail(f"Need results for baseline and '{args[0]}'. Use 'variants' to see what exists.")
        self.say(f"{'metric':<34} {'baseline':>12} {args[0]:>12}")
        for aid, a in (var.get("analyses") or {}).items():
            for key, m in (a.get("metrics") or {}).items():
                b = (((base.get("analyses") or {}).get(aid) or {}).get("metrics") or {}).get(key)
                if isinstance(m, dict) and isinstance(b, dict) and isinstance(m.get("value"), (int, float)) \
                        and isinstance(b.get("value"), (int, float)):
                    if m["value"] != b["value"]:
                        self.say(f"{aid + '.' + key:<34} {_num(b['value']):>12} {_num(m['value']):>12}  "
                                 f"{m.get('unit', '')}", "info")
        self.say(f"Verdict: baseline {base.get('verdict')}  |  {args[0]} {var.get('verdict')}",
                 _verdict_tag(var.get("verdict")))

    # ---- parameters
    def c_get(self, args: list) -> None:
        if len(args) != 1:
            raise _Fail("Usage: get <key>")
        from opencae.drone import spec as S
        sp = self._spec()
        if not sp:
            self._need_project()
        raw = S.get(sp, args[0], "<missing>")
        if isinstance(raw, str) and raw == "<missing>":
            near = difflib.get_close_matches(args[0], leaf_keys(sp), n=3, cutoff=0.6)
            raise _Fail(f"Unknown key '{args[0]}'." + (f" Did you mean: {', '.join(near)}?" if near else ""))
        p = S.param(sp, args[0])
        if p is None:
            self.say(f"{args[0]} = {json.dumps(raw)[:300]}" + ("   (not filled in yet)" if raw is None else ""))
        else:
            self.say(f"{args[0]} = {p.value} {p.unit}   [{p.status}]" + (f"  source: {p.source}" if p.source else ""))

    def c_params(self, args: list) -> None:
        from opencae.drone import spec as S
        sp = self._spec()
        if not sp:
            self._need_project()
        flt = args[0].lower() if args else ""
        n = 0
        for k in leaf_keys(sp):
            if flt and flt not in k.lower():
                continue
            p = S.param(sp, k)
            self.say(f"  {k:<44} " + (f"{p.value} {p.unit}  [{p.status}]" if p else "(empty)"))
            n += 1
        self.say(f"{n} parameter(s).", "dim")

    def c_set(self, args: list) -> None:
        if len(args) not in (2, 3):
            raise _Fail("Usage: set <key> <value> [estimate|datasheet|measured|user]")
        key, raw = args[0], args[1]
        status = args[2] if len(args) == 3 else None
        if status and status not in STATUSES:
            raise _Fail(f"The evidence status must be one of {', '.join(STATUSES)}")
        proj = self._need_project()
        if self._watching:
            raise _Fail("A run is in progress; change parameters after it ends (or use 'stop').")
        from opencae import cli
        from opencae.drone import spec as S
        value = cli.parse_value(raw)
        try:
            new = S.apply_overrides(S.load(proj), {key: value})
            if status:
                S.set_status(new, key, status)
            problems = S.validate(new)
            if problems:
                raise ValueError("; ".join(problems))
        except (KeyError, ValueError, TypeError) as e:
            raise _Fail("Not changed: " + (str(e.args[0]) if e.args else str(e)))
        S.save(new, proj / "drone.json")
        self.say(f"{key} = {value}" + (f"  [{status}]" if status else "") + "  (saved)", "ok")
        ws = self.workspace()
        if ws is not None:
            reload = getattr(ws, "reload", None) or getattr(ws, "_reload_sheet", None)
            if callable(reload):
                reload()
            else:
                ws.open_project(proj)

    # ---- housekeeping
    def c_clean(self, args: list) -> None:
        args = list(args)
        results = self._flag(args, "--results")
        proj = self._need_project(args)
        if self._watching:
            raise _Fail("A run is in progress. Use 'stop' first.")
        from opencae.drone import pipeline
        n = pipeline.clean(proj, results=results)
        self.say(f"Freed {n / 1e6:.1f} MB", "ok")

    def c_doctor(self, args: list) -> None:
        from opencae import cli
        for k, v in cli.doctor_report().items():
            if k == "free_disk_gb":
                self.say(f"  {'disk free':<12} {v} GB")
            else:
                ok = v.get("available")
                self.say(f"  {k:<12} {'ok     ' if ok else 'MISSING'} {v.get('version', '')} {v.get('note', '')}".rstrip(),
                         "ok" if ok else "warn")

    def c_setup(self, args: list) -> None:
        from opencae.drone import solver_setup as S
        if not args:
            self.say(S.describe(), "info")
            return
        target = args[0].lower()
        if target != "openfoam":
            raise _Fail("Usage: setup [openfoam]")
        if getattr(self, "_setup_thread", None) is not None and self._setup_thread.is_alive():
            raise _Fail("A solver set-up is already running.")
        pl = S.plan(target)
        self.say(f"Setting up {target}: downloading {pl['size']} from {pl['source']} into {pl['into']}. "
                 "The window stays usable.", "ok")
        import queue
        import threading
        self._setup_q: "queue.Queue" = queue.Queue()
        q = self._setup_q
        stop = threading.Event()
        self._setup_stop = stop

        def work():
            last = [""]

            def prog(msg, frac=0.0):
                if msg != last[0]:
                    last[0] = msg
                    q.put(("p", f"  [{int(100 * frac):3d}%] {msg}"))
            q.put(("d", S.install(target, progress=prog, cancelled=stop.is_set)))
        self._setup_thread = threading.Thread(target=work, daemon=True)
        self._setup_timer = QTimer(self)
        self._setup_timer.setInterval(300)
        self._setup_timer.timeout.connect(self._setup_tick)
        self._setup_timer.start()
        self._setup_thread.start()

    def _setup_tick(self) -> None:
        q = self._setup_q
        shown = 0
        while not q.empty() and shown < 20:
            kind, v = q.get()
            if kind == "p":
                self.say(v, "progress")
                shown += 1
            else:
                self._setup_timer.stop()
                self.say(v.get("message", ""), "ok" if v.get("ok") else "err")
                if not v.get("ok") and v.get("log"):
                    self.say(str(v["log"])[-800:], "dim")

    def c_mode(self, args: list) -> None:
        m = args[0].lower() if args else ""
        if m == "drone":
            self.win.enter_drone_mode()
        elif m == "studio":
            self.win.cmd_drone_back()
        else:
            raise _Fail("Usage: mode studio|drone")
        self.say(f"Mode: {self.win.mode}", "ok")

    def c_dashboard(self, args: list) -> None:
        ws = self.workspace()
        if ws is None or not getattr(ws, "url", ""):
            raise _Fail("No dashboard yet: open a project first.")
        ws.open_in_browser()
        self.say(f"Opening {ws.url} in your browser.", "ok")


# ------------------------------------------------------------------------------------------------ layout
def apply_default_layout(win, console_dock=None) -> None:
    """The default window layout (the one of the user's screenshot): Progress Monitor (and the Studio model tree)
    in a ~260 px column on the left, Properties / Results on the right, the Command Console along the bottom over
    the full width (~180 px, next to the Studio Output panel), the centre stack in between. Used for the first
    start and by View > Reset layout. Docks that are hidden only because Drone mode / the Results Hub is showing
    stay hidden until the Studio comes back."""
    console_dock = console_dock or getattr(getattr(win, "extensions", {}).get("opencae.ui.console"), "dock", None) \
        or getattr(win, "_console_dock", None)
    jobs = getattr(win, "extensions", {}).get("opencae.ui.jobs")
    monitor = getattr(jobs, "dock", None)
    tree, props, results, out = (getattr(win, n, None) for n in ("tree_dock", "props_dock", "results_dock",
                                                                 "bottom_dock"))
    win.setCorner(Qt.BottomLeftCorner, Qt.BottomDockWidgetArea)      # the bottom row spans the whole window
    win.setCorner(Qt.BottomRightCorner, Qt.BottomDockWidgetArea)
    studio_docks = [d for d in (tree, props, results, out) if d is not None]
    hidden = list(getattr(win, "_hidden_docks", []))
    in_studio = win.stack.currentWidget() is getattr(win, "viewport", None)
    for d in studio_docks + [monitor, console_dock]:
        if d is not None:
            if d.isFloating():
                d.setFloating(False)
            win.removeDockWidget(d)
    if tree is not None:
        win.addDockWidget(Qt.LeftDockWidgetArea, tree)
    if monitor is not None:
        win.addDockWidget(Qt.LeftDockWidgetArea, monitor)
        if tree is not None:
            win.splitDockWidget(tree, monitor, Qt.Vertical)
    if props is not None:
        win.addDockWidget(Qt.RightDockWidgetArea, props)
    if results is not None:
        win.addDockWidget(Qt.RightDockWidgetArea, results)
        if props is not None:
            win.tabifyDockWidget(props, results)
            props.raise_()
    if out is not None:
        win.addDockWidget(Qt.BottomDockWidgetArea, out)
    if console_dock is not None:
        win.addDockWidget(Qt.BottomDockWidgetArea, console_dock)
        if out is not None:
            win.splitDockWidget(out, console_dock, Qt.Horizontal)
    for d in studio_docks + [monitor, console_dock]:
        if d is None:
            continue
        if d in studio_docks and not in_studio:
            if d not in hidden:
                hidden.append(d)
            d.hide()
        else:
            d.show()
    if not in_studio:
        win._hidden_docks = hidden
    else:
        for d in hidden:
            d.show()
        win._hidden_docks = []
    _apply_sizes(win, tree, monitor, props, out, console_dock)


def _apply_sizes(win, tree, monitor, props, out, console_dock) -> None:
    try:
        left = [d for d in (tree, monitor) if d is not None and d.isVisible()]
        if left:
            win.resizeDocks(left, [DEFAULT_LEFT_WIDTH] * len(left), Qt.Horizontal)
        if tree is not None and monitor is not None and tree.isVisible() and monitor.isVisible():
            win.resizeDocks([tree, monitor], [300, 420], Qt.Vertical)
        if props is not None and props.isVisible():
            win.resizeDocks([props], [360], Qt.Horizontal)
        if console_dock is not None and console_dock.isVisible():
            win.resizeDocks([console_dock], [DEFAULT_CONSOLE_HEIGHT], Qt.Vertical)
            if out is not None and out.isVisible():
                total = max(600, win.width() - 2 * DEFAULT_LEFT_WIDTH)
                win.resizeDocks([out, console_dock], [int(total * 0.4), int(total * 0.6)], Qt.Horizontal)
    except Exception:  # noqa: BLE001 - layout is cosmetic
        pass


# ------------------------------------------------------------------------------------------------ install
def install(win):
    console = Console(win)
    dock = QDockWidget("Command Console", win)
    dock.setObjectName("commandConsoleDock")
    dock.setWidget(console)
    dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable | QDockWidget.DockWidgetClosable)
    dock.setToolTip("Type commands: run structural on left finger, run motion on gripper, iterate ... (Ctrl+` or F12)")
    win.addDockWidget(Qt.BottomDockWidgetArea, dock)               # along the bottom, full width
    win._console_dock = dock
    console.dock = dock
    win.setCorner(Qt.BottomLeftCorner, Qt.BottomDockWidgetArea)
    win.setCorner(Qt.BottomRightCorner, Qt.BottomDockWidgetArea)
    if getattr(win, "props_dock", None) is not None:
        # without a saved layout the screenshot layout is the default (a saved one, restored by the Progress Monitor
        # extension after every extension is installed, always wins)
        try:
            fresh = win.settings.value("window_state") is None
        except Exception:  # noqa: BLE001
            fresh = True
        if fresh:
            def _first_run_sizes():
                # the window has its real size now; only sizes (never visibility: the user may already have closed a dock)
                try:
                    if win.settings.value("window_state") is None:
                        ext = getattr(win, "extensions", {}).get("opencae.ui.jobs")
                        _apply_sizes(win, getattr(win, "tree_dock", None), getattr(ext, "dock", None),
                                     win.props_dock, getattr(win, "bottom_dock", None), dock)
                except Exception:  # noqa: BLE001 - layout is cosmetic
                    pass
            apply_default_layout(win, dock)
            QTimer.singleShot(0, _first_run_sizes)
    dock.setMinimumWidth(280)
    dock.setMinimumHeight(110)

    def toggle():
        if dock.isVisible() and console.input.hasFocus():
            dock.hide()
        else:
            dock.show()
            dock.raise_()
            console.input.setFocus()

    act = QAction("Console", win)
    act.setToolTip("Show or hide the command console (Ctrl+` or F12)")
    act.setShortcuts([QKeySequence("Ctrl+`"), QKeySequence("F12")])
    act.setShortcutContext(Qt.WindowShortcut)
    act.triggered.connect(toggle)
    win.addAction(act)
    for page in ("Home", "Drone"):
        bar = getattr(win, "ribbon_bars", {}).get(page)
        if bar is not None:
            bar.addSeparator()
            bar.addAction(act)
    view = dock.toggleViewAction()          # View > Command Console: shows, hides and restores the dock
    view.setText("Command Console")
    view.setStatusTip("Show or hide the Command Console")
    for a in win.menuBar().actions():
        if a.text().replace("&", "") == "View" and a.menu() is not None:
            a.menu().addAction(view)
            break
    reset = QAction("Reset layout", win)
    reset.setStatusTip("Put the Progress Monitor, the centre view and the Command Console back where they start")
    reset.triggered.connect(lambda checked=False: apply_default_layout(win, dock))
    for a in win.menuBar().actions():
        if a.text().replace("&", "") == "View" and a.menu() is not None:
            a.menu().addAction(reset)
            break
    console.reset_action = reset
    console.toggle_action = act
    console.toggle_dock = toggle
    console.view_action = view
    from opencae.ui.commands.palette import install_palette
    console.palette = install_palette(win, console)
    return console
