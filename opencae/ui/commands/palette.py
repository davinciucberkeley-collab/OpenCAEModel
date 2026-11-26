"""Ctrl+K command palette: the console's commands as a small searchable popup.

It offers the commands that make sense for the project that is open now (examples, components, analyses, results,
parameters) and runs the chosen one through ``Console.execute_command``: the same grammar, services, jobs and
clarification questions as typing it. Entries that still need your numbers (a parameter range) are put into the
console input for you to check instead of running at once.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout

from opencae.ui import theme
from opencae.ui.commands import grammar as G


@dataclass
class Entry:
    command: str
    hint: str = ""
    edit_only: bool = False        # put into the console input instead of running

    @property
    def label(self) -> str:
        return self.command + (f"    {self.hint}" if self.hint else "")


def score(query: str, text: str) -> float:
    """0..1: every typed word found in the text (prefix > inside), else a fuzzy ratio."""
    q = G.norm(query)
    if not q:
        return 0.5
    t = G.norm(text)
    qt, tt = q.split(), t.split()
    if t.startswith(q):
        return 1.0
    if all(any(w.startswith(x) for w in tt) for x in qt):
        return 0.92 - 0.01 * min(len(tt), 8)
    if all(x in t for x in qt):
        return 0.8
    per = [max(difflib.SequenceMatcher(None, x, w).ratio() for w in tt) for x in qt]      # typos: word by word
    whole = difflib.SequenceMatcher(None, q, t[: len(q) + 6]).ratio() * 0.7
    return max(whole, 0.85 * sum(per) / len(per) if min(per) > 0.6 else 0.0)


class CommandPalette(QDialog):
    def __init__(self, win, console):
        super().__init__(win, Qt.Popup | Qt.FramelessWindowHint)
        self.win, self.console = win, console
        self.setObjectName("commandPalette")
        self.setStyleSheet(f"QDialog#commandPalette {{ background:{theme.BG_2}; border:1px solid {theme.ACCENT}; }} "
                           f"QLineEdit {{ font-size:14px; padding:7px; }} "
                           f"QListWidget {{ border:none; font-size:12.5px; }} "
                           f"QListWidget::item {{ padding:4px 8px; }}")
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Type a command: run, open, select, show, iterate ...")
        self.list = QListWidget()
        self.list.setFocusPolicy(Qt.NoFocus)
        self.hintlabel = QLabel("Enter runs it · Up/Down chooses · Esc closes")
        self.hintlabel.setStyleSheet(f"color:{theme.FG_DIM}; font-size:11px;")
        v.addWidget(self.edit)
        v.addWidget(self.list, 1)
        v.addWidget(self.hintlabel)
        self.edit.textChanged.connect(self.refresh)
        self.edit.installEventFilter(self)
        self.list.itemActivated.connect(lambda _it: self.run_current())
        self.list.itemClicked.connect(lambda _it: self.run_current())
        self._entries: list = []
        self.resize(560, 380)

    # ---- what the palette offers
    def entries(self) -> list:
        ctx = self.console.actions.context()
        out: list = []
        for e in ctx.projects:
            out.append(Entry(f"open {e.name}", "open a project or example"))
        comps = [c.name for c in ctx.components]
        for n in comps[:12]:
            out.append(Entry(f"select {n}", "select"))
        for a in ("structural", "thermal", "modal"):
            for n in comps[:12]:
                out.append(Entry(f"run {a} on {n}", G.ANALYSIS_LABEL[a].lower()))
        if ctx.components:
            out.append(Entry("run fluid around assembly", "needs the OpenFOAM solver"))
        if self.console.actions.session.project.assembly.joints and ctx.project_name:
            out.append(Entry(f"run motion on {ctx.project_name}", "multibody motion"))
        elif any(s.data.get("type") == "mbd" for s in ctx.studies):
            out.append(Entry("run motion", "multibody motion"))
        for w in ("displacement", "stress", "temperature", "mesh", "geometry", "results", "table", "plot"):
            out.append(Entry(f"show {w}", "show"))
        for c in ("open dashboard", "open results", "status", "stop", "retry", "help",
                  "set mesh coarse", "set mesh medium", "set mesh fine", "export csv"):
            out.append(Entry(c))
        for p in ctx.parameters[:40]:
            v = float(p.data.get("value", 0) or 0)
            u = p.data.get("unit", "")
            if v:
                lo, hi = v * 0.5, v * 1.5
                out.append(Entry(f"iterate {p.name} from {lo:g} {u} to {hi:g} {u} in 5 steps".replace("  ", " "),
                                 "parameter study: check the range", True))
        for h in reversed(self.console.history[-8:]):
            if h.strip():
                out.append(Entry(h.strip(), "earlier"))
        seen, uniq = set(), []
        for e in out:
            if e.command not in seen:
                seen.add(e.command)
                uniq.append(e)
        return uniq

    def refresh(self, text: str = "") -> None:
        q = self.edit.text()
        ranked = sorted(((score(q, e.command), i, e) for i, e in enumerate(self._entries)),
                        key=lambda x: (-x[0], x[1]))
        self.list.clear()
        for s, _, e in ranked:
            if q.strip() and s < 0.5:
                continue
            it = QListWidgetItem(e.label)
            it.setData(Qt.UserRole, (e.command, e.edit_only))
            self.list.addItem(it)
            if self.list.count() >= 30:
                break
        if self.list.count():
            self.list.setCurrentRow(0)

    def open(self) -> None:
        self._entries = self.entries()
        self.edit.clear()
        self.refresh()
        g = self.win.geometry()
        self.move(g.x() + (g.width() - self.width()) // 2, g.y() + 90)
        self.show()
        self.edit.setFocus()

    def current_command(self):
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it is not None else None

    def run_current(self) -> bool:
        cur = self.current_command()
        typed = self.edit.text().strip()
        if cur is None:
            if not typed:
                return False
            cur = (typed, False)          # nothing matches: the console still understands free text
        cmd, edit_only = cur
        self.hide()
        con = self.console
        con.dock.show()
        con.dock.raise_()
        if edit_only:
            con.input.setText(cmd)
            con.input.setFocus()
            con.input.setCursorPosition(len(cmd))
        else:
            con.execute_command(cmd)
        return True

    def eventFilter(self, obj, ev):  # noqa: N802
        if obj is self.edit and ev.type() == QEvent.KeyPress:
            k = ev.key()
            n = self.list.count()
            if k in (Qt.Key_Down, Qt.Key_Up) and n:
                row = self.list.currentRow() + (1 if k == Qt.Key_Down else -1)
                self.list.setCurrentRow(max(0, min(n - 1, row)))
                return True
            if k in (Qt.Key_Return, Qt.Key_Enter):
                self.run_current()
                return True
            if k == Qt.Key_Escape:
                self.hide()
                return True
        return super().eventFilter(obj, ev)


def install_palette(win, console) -> CommandPalette:
    pal = CommandPalette(win, console)
    act = QAction("Command Palette…", win)
    act.setShortcut(QKeySequence("Ctrl+K"))
    act.setShortcutContext(Qt.WindowShortcut)
    act.setStatusTip("Search every command (Ctrl+K)")
    act.triggered.connect(pal.open)
    win.addAction(act)
    for a in win.menuBar().actions():
        if a.text().replace("&", "") == "View" and a.menu() is not None:
            a.menu().addAction(act)
            break
    pal.action = act
    return pal
