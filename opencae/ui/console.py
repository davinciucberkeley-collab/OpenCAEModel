"""Command console: a dock inside the main window where you type commands (or Python) and they run.

``install(main_window)`` is called by ``MainWindow._install_extensions``. Two modes:

* Commands (default): the ``opencae`` CLI vocabulary applied to the project open in Drone mode.
* Python (prefix ``>>>`` or the Python toggle): runs your own code with ``app, win, project, spec, pipeline,
  spec_mod, geometry, np, results`` in scope. It is your local app, so your code runs as written; long-running
  code blocks the window until it ends.
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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QDockWidget, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextEdit, QVBoxLayout,
                               QWidget)

HISTORY_MAX = 500
ANALYSES = ("mass", "propulsion", "structure", "cfd", "flight")
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


class _Fail(Exception):
    """A user-facing command error (printed in red, never a traceback)."""


# ------------------------------------------------------------------------------------------------ widgets
class _Input(QLineEdit):
    """One-line input: Up/Down history, Tab completion, multi-line paste."""

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
        if e.key() == Qt.Key_Up:
            self.console.history_step(-1)
        elif e.key() == Qt.Key_Down:
            self.console.history_step(1)
        elif e.matches(QKeySequence.Paste):
            from PySide6.QtWidgets import QApplication
            text = QApplication.clipboard().text()
            if "\n" in text.strip("\n"):
                self.console.run_block(text)
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
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setObjectName("consoleOutput")
        self.output.setStyleSheet("QTextEdit { background:#11131a; color:#c0caf5; "
                                  "font-family:Consolas,'DejaVu Sans Mono',monospace; font-size:10pt; }")
        lay.addWidget(self.output, 1)
        row = QHBoxLayout()
        self.prompt = QLabel()
        self.prompt.setStyleSheet("font-family:Consolas,'DejaVu Sans Mono',monospace; color:#7aa2f7;")
        self.input = _Input(self)
        self.input.setStyleSheet("QLineEdit { background:#11131a; color:#c0caf5; border:1px solid #2a2e3f; "
                                 "font-family:Consolas,'DejaVu Sans Mono',monospace; font-size:10pt; padding:3px; }")
        self.input.returnPressed.connect(self._enter)
        self.toggle = QPushButton("Python")
        self.toggle.setCheckable(True)
        self.toggle.setToolTip("Switch between commands and Python (or start a line with >>>)")
        self.toggle.toggled.connect(self.set_python_mode)
        row.addWidget(self.prompt)
        row.addWidget(self.input, 1)
        row.addWidget(self.toggle)
        lay.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self._tick)
        self._update_prompt()
        self.say("OpenCAE console. Type 'help' for the commands, or switch to Python with the toggle (or >>>).",
                 "dim")

    # -------------------------------------------------------------- output
    def say(self, text: str, kind: str = "info") -> None:
        color = COLORS.get(kind, COLORS["info"])
        for line in str(text).split("\n"):
            self.output.append(f'<span style="color:{color}; white-space:pre;">{html.escape(line) or "&nbsp;"}</span>')
        sb = self.output.verticalScrollBar()
        sb.setValue(sb.maximum())

    def text(self) -> str:
        return self.output.toPlainText()

    @property
    def busy(self) -> bool:
        return self._watching

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
            self.prompt.setText(f"{p.name} >" if p else "opencae >")

    def set_python_mode(self, on: bool) -> None:
        self.python_mode = bool(on)
        self._buf = []
        if self.toggle.isChecked() != self.python_mode:
            self.toggle.blockSignals(True)
            self.toggle.setChecked(self.python_mode)
            self.toggle.blockSignals(False)
        self._update_prompt()
        self.say("Python mode. Your code runs as written; long-running code blocks the window until it ends. "
                 "Start a line with ! to run a command." if on else "Command mode.", "dim")

    # -------------------------------------------------------------- history / completion
    def history_step(self, d: int) -> None:
        if not self.history:
            return
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
            return sorted(c for c in COMMANDS if c.startswith(word))
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
            pool = list(ANALYSES) + (["--preset", "--force", "--variant"] if cmd == "run" else ["--variant"])
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
        text = self.input.text()
        cands = self.completions(text)
        if not cands:
            return
        head = text[: len(text) - len(text.split(" ")[-1])]
        common = os.path.commonprefix(cands)
        if len(cands) == 1:
            self.input.setText(head + cands[0] + ("" if cands[0].endswith(("=", os.sep)) else " "))
        else:
            self.input.setText(head + common)
            self.say("  ".join(cands[:40]) + (" ..." if len(cands) > 40 else ""), "dim")

    # -------------------------------------------------------------- entry points
    def _enter(self) -> None:
        line = self.input.text()
        self.input.clear()
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
        self._run_python(code)

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
        if args:
            c = args[0].lower()
            if c not in COMMANDS:
                raise _Fail(f"No command '{c}'. Type 'help'.")
            u, d, ex = COMMANDS[c]
            self.say(f"{u}\n  {d}\n  example: {ex}")
            return
        self.say("Commands (the project open in Drone mode is used; Tab completes names and parameter keys, "
                 "Up/Down recalls earlier lines):")
        for c, (u, d, ex) in COMMANDS.items():
            self.say(f"  {c:<10} {d}")
            self.say(f"             e.g. {ex}", "dim")
        self.say("Python: start a line with >>> or press the Python button. In scope: app, win, project, spec, "
                 "pipeline, spec_mod, geometry, np, results. Long-running code freezes the window until it "
                 "finishes; use 'run' for analyses.", "dim")

    def c_clear(self, args: list) -> None:
        self.output.clear()

    def c_python(self, args: list) -> None:
        on = (args[0].lower() if args else "on") != "off"
        self.toggle.setChecked(on)

    def c_open(self, args: list) -> None:
        if not args:
            raise _Fail("Usage: open <dir>")
        p = Path(" ".join(args)).expanduser()
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
                                            and res.get("verdict") else ""), "ok")
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
            self.say(f"Verdict: {res.get('verdict', 'n/a')}", "ok")
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
        self.say(f"Verdict: {res.get('verdict', 'n/a')}", "ok")

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
        self.say(f"Verdict: baseline {base.get('verdict')}  |  {args[0]} {var.get('verdict')}", "ok")

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
                holder, parent = S._get(new, key)
                if isinstance(holder, dict):
                    holder["status"] = status
                else:  # a bare number becomes {value, unit, status, source}
                    last = key.split(".")[-1]
                    parent[int(last) if isinstance(parent, list) else last] = {
                        "value": holder, "unit": "", "status": status, "source": ""}
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


# ------------------------------------------------------------------------------------------------ install
def install(win):
    console = Console(win)
    dock = QDockWidget("Command console", win)
    dock.setObjectName("commandConsoleDock")
    dock.setWidget(console)
    win.addDockWidget(Qt.BottomDockWidgetArea, dock)
    dock.hide()
    console.dock = dock

    def toggle():
        if dock.isVisible():
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
    console.toggle_action = act
    console.toggle_dock = toggle
    return console
