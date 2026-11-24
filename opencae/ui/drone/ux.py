"""Drone mode helpers that make the application friendlier: the run-options dialog, the shortcuts popup, the HTML
report export, a plain-text summary, and ``install(main_window)`` which adds the ribbon buttons and keeps the
Drone-page keyboard shortcuts from colliding with the Studio ones.

Everything here is optional: if this module fails to load, Drone mode still works (the main window logs it).
"""
from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QSpinBox, QVBoxLayout)

from opencae.ui import theme

DISCLAIMER = "Simulation results plan the tests; they are not flight approval."

# (id, title, one-line explanation, typical (min, max) minutes, typical text)
ANALYSES = [
    ("mass", "Mass and balance", "Adds up the part masses and finds the centre of gravity and inertia.",
     (0.0, 0.1), "a few seconds"),
    ("propulsion", "Propulsion", "Thrust, hover throttle, battery current and hover time from motor, prop and battery.",
     (0.0, 0.1), "a few seconds"),
    ("structure", "Structure (strength)", "Stress and deflection of the arms and frame under a load.",
     (1.0, 5.0), "1 to 5 minutes"),
    ("cfd", "Air flow (CFD)", "Lift, drag and pressure on the airframe in cruise. The slow one.",
     (5.0, 20.0), "depends on the quality"),
    ("flight", "Flight (hover stability)", "A simulated hover with a knock, to see if the aircraft recovers.",
     (0.1, 0.5), "under a minute"),
]
PRESETS = [
    ("coarse", "Coarse", "up to about 400 thousand cells", (5.0, 20.0), "about 5 to 20 minutes"),
    ("medium", "Medium", "up to about 1 million cells", (20.0, 60.0), "about 20 to 60 minutes"),
    ("fine", "Fine", "up to about 2 million cells", (60.0, 240.0), "about 1 to 4 hours"),
]

SHORTCUTS = [
    ("F5", "Run all analyses"),
    ("Shift+F5", "Run with options (choose analyses, CFD quality)"),
    ("Esc", "Stop the running analysis (asks first)"),
    ("Ctrl+N", "New drone from CAD"),
    ("Ctrl+O", "Open a drone project"),
    ("Ctrl+F", "Search the parameters"),
    ("Ctrl+S", "Save the changed parameters"),
    ("Ctrl+Z / Ctrl+Y", "Undo / redo a parameter edit"),
    ("F1", "Show this list"),
]


def _minutes_text(lo: float, hi: float) -> str:
    def f(m: float) -> str:
        return f"{m * 60:.0f} s" if m < 1 else f"{m:.0f} min" if m < 90 else f"{m / 60:.1f} h"
    return f"{f(lo)} to {f(hi)}"


# ------------------------------------------------------------------------------------------------ run options
class RunOptionsDialog(QDialog):
    """Choose what to run. ``options()`` returns ``{only, overrides, force, keep_cases, remember}``:
    ``only`` is None when everything is ticked; ``overrides`` holds the CFD preset / iterations when they differ
    from drone.json (a run-only change unless ``remember`` is ticked)."""

    def __init__(self, spec: Optional[dict] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Run options")
        self.spec = spec or {}
        cfd = (self.spec.get("analyses") or {}).get("cfd") or {}
        self._saved_preset = str(cfd.get("preset") or "coarse")
        it = cfd.get("iterations")
        it = it.get("value") if isinstance(it, dict) else it
        self._saved_iters = int(it) if isinstance(it, (int, float)) and not isinstance(it, bool) else 300
        lay = QVBoxLayout(self)
        intro = QLabel("Pick the analyses to run. Unchanged analyses are skipped unless you force a re-run.")
        intro.setWordWrap(True)
        intro.setObjectName("hint")
        lay.addWidget(intro)

        box = QGroupBox("Analyses")
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 1)
        self.checks: dict = {}
        analyses = self.spec.get("analyses") or {}
        for r, (aid, title, why, _mins, typical) in enumerate(ANALYSES):
            cb = QCheckBox(title)
            en = (analyses.get(aid) or {}).get("enabled") if isinstance(analyses.get(aid), dict) else None
            cb.setChecked(en is not False)
            cb.setToolTip(f"{why} Typical time: {typical}.")
            cb.toggled.connect(self._update)
            self.checks[aid] = cb
            grid.addWidget(cb, r, 0)
            lab = QLabel(why)
            lab.setWordWrap(True)
            lab.setObjectName("hint")
            grid.addWidget(lab, r, 1)
            t = QLabel(typical)
            t.setObjectName("hint")
            t.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(t, r, 2)
            if aid == "cfd":
                self.cfd_time = t
        lay.addWidget(box)

        self.cfd_box = QGroupBox("Air flow quality")
        form = QFormLayout(self.cfd_box)
        self.preset = QComboBox()
        for key, name, cells, _m, when in PRESETS:
            self.preset.addItem(f"{name}: {cells}, {when}", key)
        self.preset.setCurrentIndex(max(0, [p[0] for p in PRESETS].index(self._saved_preset)
                                        if self._saved_preset in [p[0] for p in PRESETS] else 0))
        self.preset.currentIndexChanged.connect(self._update)
        self.preset.setToolTip("More cells give a more detailed answer and take longer. Start coarse.")
        self.iterations = QSpinBox()
        self.iterations.setRange(20, 20000)
        self.iterations.setSingleStep(50)
        self.iterations.setValue(self._saved_iters)
        self.iterations.setToolTip("Solver iterations. More can help it settle, at the cost of time.")
        form.addRow("Quality", self.preset)
        form.addRow("Iterations", self.iterations)
        lay.addWidget(self.cfd_box)

        self.keep = QCheckBox("Keep the solver files (uses disk space, useful for debugging)")
        self.force = QCheckBox("Force a re-run (ignore results that are already up to date)")
        self.remember = QCheckBox("Remember the air-flow settings in drone.json")
        self.remember.setToolTip("Without this the quality and iterations apply to this run only.")
        for c in (self.keep, self.force, self.remember):
            lay.addWidget(c)
        self.estimate = QLabel("")
        self.estimate.setObjectName("hint")
        self.estimate.setWordWrap(True)
        lay.addWidget(self.estimate)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.run_btn = self.buttons.button(QDialogButtonBox.Ok)
        self.run_btn.setText("Run")
        self.run_btn.setObjectName("primary")
        self.run_btn.setToolTip("Start the run with these options")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self.setMinimumWidth(640)
        self._update()

    def _preset_key(self) -> str:
        return self.preset.currentData()

    def _update(self, *_):
        cfd_on = self.checks["cfd"].isChecked()
        self.cfd_box.setEnabled(cfd_on)
        lo = hi = 0.0
        for aid, _t, _w, (a, b), _x in ANALYSES:
            if not self.checks[aid].isChecked():
                continue
            if aid == "cfd":
                a, b = next(p[3] for p in PRESETS if p[0] == self._preset_key())
            lo, hi = lo + a, hi + b
        for p in PRESETS:
            if p[0] == self._preset_key():
                self.cfd_time.setText(p[4].replace("about ", ""))
        any_on = any(c.isChecked() for c in self.checks.values())
        self.run_btn.setEnabled(any_on)
        self.estimate.setText("Choose at least one analysis." if not any_on else
                              f"Typical total on a laptop: {_minutes_text(lo, hi)}. Your drone may differ.")

    def options(self) -> dict:
        ids = [a[0] for a in ANALYSES]
        only = [a for a in ids if self.checks[a].isChecked()]
        over: dict = {}
        if self.checks["cfd"].isChecked():
            if self._preset_key() != self._saved_preset:
                over["analyses.cfd.preset"] = self._preset_key()
            if self.iterations.value() != self._saved_iters:
                over["analyses.cfd.iterations"] = self.iterations.value()
        return {"only": None if len(only) == len(ids) else only, "overrides": over,
                "force": self.force.isChecked(), "keep_cases": self.keep.isChecked(),
                "remember": self.remember.isChecked()}


# ------------------------------------------------------------------------------------------------ shortcuts
class ShortcutsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        lay = QVBoxLayout(self)
        grid = QGridLayout()
        for r, (k, what) in enumerate(SHORTCUTS):
            key = QLabel(k)
            key.setStyleSheet(f"font-weight:600;color:{theme.ACCENT}")
            grid.addWidget(key, r, 0)
            grid.addWidget(QLabel(what), r, 1)
        lay.addLayout(grid)
        note = QLabel("These work while the Drone page is shown.")
        note.setObjectName("hint")
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.Close).clicked.connect(self.accept)
        lay.addWidget(bb)


# ------------------------------------------------------------------------------------------------ results
def latest_results(project_dir, variant: Optional[str] = None) -> Optional[dict]:
    """results.json of a variant (the newest one when ``variant`` is None); None when there is none."""
    root = Path(project_dir) / "results"
    if variant:
        cands = [root / variant / "results.json"]
    else:
        cands = sorted(root.glob("*/results.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        base = root / "baseline" / "results.json"
        if base.exists():  # the baseline is what the dashboard opens with
            cands = [base] + [c for c in cands if c != base]
    for p in cands:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def _fmt_value(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_fmt_value(x) for x in v) + "]"
    return str(v)


def summary_text(results: Optional[dict], project: str = "") -> str:
    """Plain text for the clipboard."""
    if not results:
        return f"{project or 'Drone project'}: no results yet. Run the analyses first.\n{DISCLAIMER}"
    lines = [f"{results.get('project') or project or 'Drone project'} ({results.get('variant', 'baseline')}): "
             f"{results.get('verdict', 'unknown')}"]
    for aid, r in (results.get("analyses") or {}).items():
        lines.append(f"- {r.get('title', aid)}: {r.get('status', '?')}. {r.get('summary', '')}".rstrip())
    checks = results.get("checks") or []
    if checks:
        n = {s: sum(1 for c in checks if c.get("status") == s) for s in ("pass", "conditional", "fail", "not_evaluated")}
        lines.append(f"Checks: {n['pass']} pass, {n['conditional']} conditional, {n['fail']} fail, "
                     f"{n['not_evaluated']} not evaluated")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


_CSS = """
body{font:15px/1.5 system-ui,Segoe UI,Arial,sans-serif;color:#1d2530;max-width:960px;margin:24px auto;padding:0 16px}
h1{margin:0 0 4px}h2{margin-top:28px;border-bottom:1px solid #d5dbe3;padding-bottom:4px}
.verdict{display:inline-block;padding:6px 14px;border-radius:6px;font-weight:700;color:#fff}
.v-ready{background:#2e8b57}.v-cond{background:#b8860b}.v-not{background:#c0392b}.v-other{background:#667}
.card{border:1px solid #d5dbe3;border-radius:8px;padding:12px 16px;margin:12px 0;background:#fafbfc}
.card h3{margin:0 0 4px}.tag{font-size:12px;border-radius:10px;padding:1px 9px;color:#fff;margin-left:8px}
.pass,.ok{background:#2e8b57}.conditional,.warn{background:#b8860b}.fail,.error{background:#c0392b}
.not_evaluated,.skipped{background:#778}
table{border-collapse:collapse;width:100%;margin:8px 0}th,td{text-align:left;padding:4px 8px;border-bottom:1px solid #e3e7ed;
vertical-align:top}th{background:#eef1f5;font-size:13px}.dim{color:#667}.foot{margin-top:32px;padding-top:10px;
border-top:1px solid #d5dbe3;font-style:italic}
"""


def build_report_html(results: dict, project: str = "", generated: Optional[str] = None) -> str:
    """A self-contained HTML report (inline CSS, no scripts, no links to files) from results.json."""
    e = html.escape
    verdict = str(results.get("verdict", "unknown"))
    vcls = {"ready for physical tests": "v-ready", "conditional": "v-cond", "not ready": "v-not"}.get(verdict, "v-other")
    name = results.get("project") or project or "Drone project"
    out = [f"<!doctype html><html lang=en><head><meta charset=utf-8><title>{e(str(name))} - OpenCAE report</title>"
           f"<style>{_CSS}</style></head><body>",
           f"<h1>{e(str(name))}</h1><div class=dim>Variant {e(str(results.get('variant', 'baseline')))} - "
           f"generated {e(str(results.get('generated') or generated or time.strftime('%Y-%m-%d %H:%M')))}</div>",
           f"<p><span class='verdict {vcls}'>{e(verdict)}</span></p>"]
    analyses = results.get("analyses") or {}
    out.append("<h2>Analyses</h2>")
    for aid, r in analyses.items():
        st = str(r.get("status", "?"))
        out.append(f"<div class=card><h3>{e(str(r.get('title', aid)))}<span class='tag {e(st)}'>{e(st)}</span></h3>"
                   f"<div>{e(str(r.get('summary', '')))}</div>")
        if r.get("missing"):
            out.append(f"<div class=dim>Missing inputs: {e(', '.join(map(str, r['missing'])))}</div>")
        metrics = r.get("metrics") or {}
        rows = [(k, m) for k, m in metrics.items() if isinstance(m, dict)]
        if rows:
            out.append("<table><tr><th>Quantity</th><th>Value</th><th>Unit</th><th>Based on</th></tr>")
            for k, m in rows:
                out.append(f"<tr><td>{e(str(m.get('label') or k.replace('_', ' ')))}</td>"
                           f"<td>{e(_fmt_value(m.get('value')))}</td><td>{e(str(m.get('unit', '')))}</td>"
                           f"<td class=dim>{e(str(m.get('basis', '')))}</td></tr>")
            out.append("</table>")
        out.append("</div>")
    if not analyses:
        out.append("<p class=dim>No analyses in these results.</p>")
    checks = results.get("checks") or []
    out.append("<h2>Requirement checks</h2>")
    if checks:
        out.append("<table><tr><th>Check</th><th>Needs</th><th>Value</th><th>Status</th><th>Why</th></tr>")
        for c in checks:
            st = str(c.get("status", ""))
            out.append(f"<tr><td>{e(str(c.get('label', c.get('id', ''))))}</td>"
                       f"<td>{e(str(c.get('metric', '')))} {e(str(c.get('op', '')))} {e(_fmt_value(c.get('target')))}</td>"
                       f"<td>{e(_fmt_value(c.get('value')))}</td><td><span class='tag {e(st)}'>{e(st)}</span></td>"
                       f"<td class=dim>{e(str(c.get('why', '')))}</td></tr>")
        out.append("</table>")
    else:
        out.append("<p class=dim>No checks.</p>")
    keys = []
    for aid, r in analyses.items():
        for k, m in (r.get("metrics") or {}).items():
            if k in ("mass_kg", "tw_ratio", "hover_endurance_min", "safety_factor", "ld", "max_tilt_deg") \
                    and isinstance(m, dict) and m.get("value") is not None:
                keys.append((m.get("label") or k.replace("_", " "), _fmt_value(m["value"]), m.get("unit", ""),
                             m.get("basis", "")))
    if keys:
        out.append("<h2>Key numbers</h2><table><tr><th>Quantity</th><th>Value</th><th>Unit</th><th>Based on</th></tr>")
        out += [f"<tr><td>{e(str(a))}</td><td>{e(b)}</td><td>{e(str(c))}</td><td class=dim>{e(str(d))}</td></tr>"
                for a, b, c, d in keys]
        out.append("</table>")
    out.append(f"<p class=foot>{e(DISCLAIMER)}</p></body></html>")
    return "".join(out)


def export_report(project_dir, path, variant: Optional[str] = None) -> Path:
    """Write the HTML report of a project's results to ``path``. Raises FileNotFoundError without results."""
    res = latest_results(project_dir, variant)
    if res is None:
        raise FileNotFoundError("There are no results yet. Run the analyses first.")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build_report_html(res, Path(project_dir).name), encoding="utf-8")
    return p


# ------------------------------------------------------------------------------------------------ install
CONFLICTING = ("new", "open", "save", "undo", "redo", "solve", "clear_sel", "guide")  # Studio actions sharing a key


# ------------------------------------------------------------------------------------------------ solver setup
_LIVE: list = []   # running workers stay referenced until they finish


class _SetupWorker(QThread):
    """Runs one setup function off the UI thread; ``progress(msg, frac)`` and ``done(dict)`` are queued signals."""
    progress = Signal(str, float)
    done = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._stop = False

    def cancel(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            res = self._fn(lambda m, f=0.0: self.progress.emit(str(m), float(f)), lambda: self._stop)
        except Exception as exc:  # noqa: BLE001 - never to the UI
            res = {"ok": False, "message": f"The setup stopped: {exc}", "log": ""}
        self.done.emit(res)


class SolverDialog(QDialog):
    """Status of the airflow solvers and one-click set-up of SU2 and OpenFOAM (nothing happens without a click)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Airflow solvers")
        self.setMinimumWidth(560)
        self.worker: Optional[_SetupWorker] = None
        self._status_worker: Optional[_SetupWorker] = None
        v = QVBoxLayout(self)
        intro = QLabel("The airflow (CFD) analysis needs a solver. OpenFOAM is tried first, then SU2. Both are free "
                       "open-source programs; the buttons below download them into your own OpenCAE folder "
                       "(no administrator rights, nothing outside it is changed).")
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.su2_label = QLabel("SU2: checking...")
        self.foam_label = QLabel("OpenFOAM: checking...")
        self.wsl_label = QLabel("")
        for w in (self.su2_label, self.foam_label, self.wsl_label):
            w.setWordWrap(True)
            v.addWidget(w)
        row = QHBoxLayout()
        self.su2_btn = QPushButton("Install SU2 (\u224840 MB)")
        self.su2_btn.setToolTip("Download the official SU2 release from github.com/su2code and test it")
        self.foam_btn = QPushButton("Install OpenFOAM (\u22481\u20132 GB)")
        self.foam_btn.setToolTip("Download OpenFOAM (ESI, open source) from conda-forge and test it")
        self.su2_btn.clicked.connect(lambda checked=False: self.start("su2"))
        self.foam_btn.clicked.connect(lambda checked=False: self.start("openfoam"))
        row.addWidget(self.su2_btn)
        row.addWidget(self.foam_btn)
        v.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        v.addWidget(self.bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(140)
        v.addWidget(self.log)
        btns = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(lambda checked=False: self.cancel())
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        btns.addStretch(1)
        btns.addWidget(self.cancel_btn)
        btns.addWidget(self.close_btn)
        v.addLayout(btns)
        self.refresh()

    # -- status
    def refresh(self) -> None:
        from opencae.drone import solver_setup
        w = self._spawn(lambda progress, cancelled: solver_setup.status(), self._on_status, track_it=False)
        self._status_worker = w

    def _on_status(self, st) -> None:
        s, f = st["su2"], st["openfoam"]
        self.su2_label.setText("SU2: " + (f"found, version {s['version'] or '?'} ({s['path']})" if s["found"]
                                          else "not installed"))
        self.foam_label.setText("OpenFOAM: " + (f"found, version {f['version'] or '?'} ({f['where']})" if f["found"]
                                                else "not installed"))
        if st["platform"] == "win32":
            w = st["wsl"]
            self.wsl_label.setText("WSL: " + (", ".join(w["distros"]) if w["available"] else
                                              "not set up (OpenFOAM on Windows runs inside WSL)"))
        self.status = st

    # -- install
    def confirm(self, which: str) -> bool:
        from opencae.drone import solver_setup as S
        what = ("SU2 " + S.SU2_VERSION, S.SU2_SIZE_TEXT, S.SU2_SOURCE) if which == "su2" else \
            ("OpenFOAM", S.OPENFOAM_SIZE_TEXT, S.OPENFOAM_SOURCE)
        return QMessageBox.question(
            self, "Download " + what[0],
            f"Download {what[0]} ({what[1]}) from {what[2]}?\n\nIt is installed in {S.solvers_dir()}. No "
            "administrator rights are needed and nothing outside that folder changes. When it is done a small test "
            "flow is run to check that it works.") == QMessageBox.Yes

    def start(self, which: str, confirmed: bool = False) -> bool:
        if self.worker is not None:
            return False
        if not confirmed and not self.confirm(which):
            return False
        from opencae.drone import solver_setup as S
        fn = S.install_su2 if which == "su2" else S.install_openfoam
        self.log.appendPlainText(f"Starting the {'SU2' if which == 'su2' else 'OpenFOAM'} set-up...")
        self.su2_btn.setEnabled(False)
        self.foam_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.bar.setValue(0)
        self.worker = self._spawn(lambda progress, cancelled: fn(progress, cancelled), self._finished,
                                  self._on_progress)
        return True

    def _spawn(self, fn, on_done, on_progress=None, track_it: bool = True) -> _SetupWorker:
        w = _SetupWorker(fn)          # no parent: it may outlive the dialog (see _LIVE)
        _LIVE.append(w)

        w.on_done = on_done
        w.done.connect(self._worker_done)    # bound method: delivered on the UI thread
        if on_progress is not None:
            w.progress.connect(on_progress)
        if track_it:
            from opencae.utilities import threads
            threads.track(w, w.cancel)
        w.start()
        return w

    def _worker_done(self, res) -> None:
        w = self.sender()
        try:
            w.on_done(res)
        finally:
            if isinstance(w, _SetupWorker):
                w.wait(5000)          # `done` is its last act; let the thread end, then let go of it
                if w in _LIVE:
                    _LIVE.remove(w)

    def _on_progress(self, msg: str, frac: float) -> None:
        self.bar.setValue(int(frac * 1000))
        if not self.log.toPlainText().endswith(msg):
            self.log.appendPlainText(msg)

    def _finished(self, res) -> None:
        w, self.worker = self.worker, None
        if w is not None:
            w.wait(2000)
        self.cancel_btn.setEnabled(False)
        self.su2_btn.setEnabled(True)
        self.foam_btn.setEnabled(True)
        self.bar.setValue(1000 if res.get("ok") else self.bar.value())
        self.log.appendPlainText(("Done. " if res.get("ok") else "Not done. ") + str(res.get("message", "")))
        if res.get("log") and not res.get("ok"):
            self.log.appendPlainText(str(res["log"])[-1500:])
        self.result = res
        self.refresh()

    def cancel(self) -> None:
        if self.worker is not None:
            self.log.appendPlainText("Cancelling...")
            self.worker.cancel()

    def closeEvent(self, e) -> None:
        for w in (self.worker, self._status_worker):
            if w is not None:
                try:
                    w.done.disconnect()
                except (RuntimeError, TypeError):
                    pass
                w.cancel()
                w.wait(3000)
        super().closeEvent(e)


def open_solver_dialog(parent=None) -> SolverDialog:
    d = SolverDialog(parent)
    d.setAttribute(Qt.WA_DeleteOnClose, False)
    d.show()
    return d


def install(main_window):
    """Ribbon buttons ("Run…", "Report", "Folder", "Shortcuts") on the Drone page, tooltips on the existing ones, and
    the shortcut hand-over between Studio and Drone mode. Returns a small handle dict."""
    A = main_window.A
    bar = main_window.ribbon_bars.get("Drone")

    def ws():
        main_window.enter_drone_mode()
        return main_window._drone

    def act(name, text, short, tip, slot, ico):
        from opencae.ui.icons import icon
        a = main_window._act(name, text, lambda: slot(), ico, "", tip)
        a.setIconText(short)
        return a

    holder: dict = {}

    def open_solvers():
        holder["dlg"] = open_solver_dialog(main_window)

    new = {
        "drone_solvers": act("drone_solvers", "Solvers…", "Solvers",
                             "See which airflow solvers are installed and set up SU2 or OpenFOAM", open_solvers,
                             "settings"),
        "drone_run_options": act("drone_run_options", "Run with options…", "Run…",
                                 "Choose which analyses to run, the air-flow quality and more (Shift+F5)",
                                 lambda: ws().run_options(), "solve"),
        "drone_report": act("drone_report", "Export report…", "Report",
                            "Save an HTML report of the latest results", lambda: ws().export_report(), "report"),
        "drone_folder": act("drone_folder", "Open project folder", "Folder",
                            "Show the project folder in your file manager", lambda: ws().open_folder(), "open"),
        "drone_shortcuts": act("drone_shortcuts", "Keyboard shortcuts", "Shortcuts",
                               "List the keyboard shortcuts of Drone mode (F1)", lambda: ws().show_shortcuts(),
                               "help"),
    }
    tips = {"drone_new": "Create a drone project from a CAD file (Ctrl+N)",
            "drone_open": "Open an existing drone project folder (Ctrl+O)",
            "drone_example_quad": "Open the 5-inch quadcopter example",
            "drone_example_k09l": "Open the K09-L tailsitter example",
            "drone_run": "Save the parameters and run every analysis (F5)",
            "drone_stop": "Stop the running analysis after the current step (Esc)",
            "drone_back": "Return to the Studio"}
    for n, t in tips.items():
        if n in A:
            A[n].setToolTip(t)
            A[n].setStatusTip(t)
    if bar is not None:
        before = A.get("drone_stop")
        if before is not None:
            bar.insertAction(before, new["drone_run_options"])
        else:
            bar.addAction(new["drone_run_options"])
        back = A.get("drone_back")
        for n in ("drone_solvers", "drone_report", "drone_folder", "drone_shortcuts"):
            if back is not None:
                bar.insertAction(back, new[n])
            else:
                bar.addAction(new[n])
        if back is not None:
            bar.insertSeparator(new["drone_solvers"])

    saved: dict = {}

    def sync(*_):
        w = getattr(main_window, "_drone", None)
        on = w is not None and main_window.stack.currentWidget() is w
        if w is not None:
            s = getattr(main_window, "settings", None)
            if s is not None and getattr(w, "_settings", None) is None:
                w.settings = s
        if on:
            for n in CONFLICTING:
                a = A.get(n)
                if a is not None and n not in saved:
                    saved[n] = a.shortcut()
                    a.setShortcut(QKeySequence())
        else:
            for n, sc in list(saved.items()):
                A[n].setShortcut(sc)
            saved.clear()

    main_window.stack.currentChanged.connect(sync)
    return {"actions": new, "sync": sync}
