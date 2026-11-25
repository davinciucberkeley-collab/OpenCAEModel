"""Run progress tracker: follows the run of the open drone project and reports it to the Progress Monitor.

``install(main_window)`` is called by ``MainWindow._install_extensions``. It reads
``main_window.drone_workspace().server.jobs.progress`` once a second, only when the workspace already exists and a
project is open (it never creates the workspace itself), and reports every Drone / dashboard run as a job in the
shared registry (``opencae.ui.jobs``): the Progress Monitor dock shows it, with stages, log, cancel and retry.

The "Run progress" dock of the first version is kept for its run history but is no longer shown by default;
``show_dock()`` now shows the Progress Monitor (``show_history()`` shows the old dock on its History tab).
"""
from __future__ import annotations

import calendar
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QDockWidget, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QPlainTextEdit, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from opencae.ui import theme

ORDER = ("mass", "propulsion", "structure", "cfd", "flight")
NAMES = {"mass": "Mass", "propulsion": "Propulsion", "structure": "Structure", "cfd": "Airflow (CFD)",
         "flight": "Flight"}
# state -> (glyph, colour, short text)
STATES = {"waiting": ("○", theme.FG_DIM, "Waiting"), "running": ("◐", theme.ACCENT, "Running"),
          "done": ("✔", theme.OK, "Done"), "cached": ("↺", theme.ACCENT, "Reused"),
          "error": ("✖", theme.ERR, "Error"), "skipped": ("–", theme.FG_DIM, "Skipped")}
SPINNER = "◐◓◑◒"


def fmt_dur(s: Optional[float]) -> str:
    """5 min 48 s, 42 s, 3.5 s, 1 h 05 min."""
    if s is None:
        return "-"
    s = max(float(s), 0.0)
    if s < 10:
        return f"{s:.1f} s"
    s = int(round(s))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s".replace(" 0", " ", 1) if s % 60 < 10 else f"{s // 60} min {s % 60} s"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


def _when(text: str) -> str:
    """The pipeline stores UTC 'YYYY-MM-DD HH:MM:SS'; show local time."""
    try:
        t = calendar.timegm(time.strptime(str(text), "%Y-%m-%d %H:%M:%S"))
        return time.strftime("%d %b %H:%M", time.localtime(t))
    except (ValueError, TypeError):
        return str(text or "")


class RunTracker:
    def __init__(self, main_window):
        self.mw = main_window
        self._was_running = False
        self._seen_run: Optional[str] = None
        self._hist_run: Optional[str] = None
        self._hist_project = None
        self._nlog = 0
        self._log_run: Optional[str] = None
        self._tray = None
        self._frame = 0
        self._sized = False
        self._rows: dict = {}
        self._job = None          # the registry job that follows the current run
        self._job_state: dict = {}
        self._job_nlog = 0
        self._closed: set = set()  # run ids already closed in the registry (cancelled from there)
        self._build()
        self.timer = QTimer(self.dock)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.spin = QTimer(self.dock)
        self.spin.setInterval(180)
        self.spin.timeout.connect(self._animate)

    # ------------------------------------------------------------------ layout
    def _build(self):
        mw = self.mw
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        # progress tab: bar + rows on the left, live log on the right
        page = QWidget()
        h = QHBoxLayout(page)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(5)
        self.state_label = QLabel("No run yet")
        self.state_label.setStyleSheet("font-weight:600; font-size:14px;")
        top = QHBoxLayout()
        top.addWidget(self.state_label, 1)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setToolTip("Stop after the step that is running")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        top.addWidget(self.stop_btn)
        left.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setFormat("0%")
        self.bar.setMinimumHeight(26)
        self.bar.setStyleSheet(
            f"QProgressBar {{ background:#171a20; border:1px solid {theme.BORDER}; border-radius:5px; "
            f"text-align:center; font-weight:700; font-size:13px; }}"
            f"QProgressBar::chunk {{ background:{theme.ACCENT}; border-radius:4px; }}")
        left.addWidget(self.bar)
        times = QHBoxLayout()
        self.elapsed_label = QLabel("")
        self.eta_label = QLabel("")
        self.eta_label.setAlignment(Qt.AlignRight)
        for lb in (self.elapsed_label, self.eta_label):
            lb.setStyleSheet(f"color:{theme.FG_DIM};")
        times.addWidget(self.elapsed_label)
        times.addWidget(self.eta_label)
        left.addLayout(times)
        grid_host = QFrame()
        grid_host.setStyleSheet(f"QFrame {{ background:{theme.BG_2}; border:1px solid {theme.BORDER}; "
                                f"border-radius:5px; }} QLabel {{ background:transparent; border:none; }}")
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(8, 4, 8, 4)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        for r, aid in enumerate(ORDER):
            icon, name, st, dur, step = QLabel(), QLabel(NAMES[aid]), QLabel(), QLabel(), QLabel()
            icon.setFixedWidth(20)
            icon.setAlignment(Qt.AlignCenter)
            icon.setStyleSheet("font-size:16px;")
            name.setStyleSheet("font-weight:600;")
            name.setMinimumWidth(96)
            st.setMinimumWidth(60)
            dur.setMinimumWidth(52)
            dur.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            step.setStyleSheet(f"color:{theme.FG_DIM};")
            for c, w in enumerate((icon, name, st, dur, step)):
                grid.addWidget(w, r, c)
            grid.setColumnStretch(4, 1)
            self._rows[aid] = {"icon": icon, "name": name, "state": st, "dur": dur, "step": step, "s": "waiting"}
        left.addWidget(grid_host)
        self.step_label = QLabel("")
        self.step_label.setStyleSheet(f"color:{theme.FG_DIM};")
        left.addWidget(self.step_label)
        left.addStretch(1)
        h.addLayout(left, 3)
        right = QVBoxLayout()
        lh = QHBoxLayout()
        lh.addWidget(QLabel("Log"))
        lh.addStretch(1)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.clicked.connect(self._copy_log)
        lh.addWidget(self.copy_btn)
        right.addLayout(lh)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        right.addWidget(self.log, 1)
        h.addLayout(right, 2)
        tabs.addTab(page, "Progress")
        # history tab
        self.history = QTableWidget(0, 5)
        self.history.setHorizontalHeaderLabels(["When", "Variant", "What ran", "Took", "Verdict"])
        self.history.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history.verticalHeader().setVisible(False)
        self.history.setAlternatingRowColors(True)
        hh = self.history.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        self.history_empty = QLabel("No runs yet. Press Run and every run will be listed here.")
        self.history_empty.setAlignment(Qt.AlignCenter)
        self.history_empty.setStyleSheet(f"color:{theme.FG_DIM};")
        hp = QWidget()
        hl = QVBoxLayout(hp)
        hl.setContentsMargins(6, 6, 6, 6)
        hl.addWidget(self.history, 1)
        hl.addWidget(self.history_empty)
        tabs.addTab(hp, "History")
        self.tabs = tabs
        self.dock = QDockWidget("Run progress", mw)
        self.dock.setObjectName("run_progress")
        self.dock.setWidget(tabs)
        self.dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable |
                              QDockWidget.DockWidgetClosable)
        self.dock.setFloating(True)   # not part of the window layout: the Progress Monitor replaced it
        self.dock.hide()              # it stays, as a floating window, for the history of earlier runs
        for a in mw.menuBar().actions():
            if a.text().replace("&", "") == "View" and a.menu() is not None:
                self.history_action = QAction("Drone run history", mw)
                self.history_action.setToolTip("Show the list of earlier Drone runs")
                self.history_action.triggered.connect(self.show_history)
                a.menu().addAction(self.history_action)
                break
        bar = getattr(mw, "ribbon_bars", {}).get("Drone")
        self.action = QAction("Progress", mw)
        self.action.setToolTip("Show the Progress Monitor: the run, its stages and the history")
        try:
            from opencae.ui.icons import icon
            self.action.setIcon(icon("solve"))
        except Exception:
            pass
        self.action.triggered.connect(self._on_button)
        if bar is not None:
            bar.addAction(self.action)
        self.set_history([])

    # ------------------------------------------------------------------ actions
    def _ext(self):
        return getattr(self.mw, "extensions", {}).get("opencae.ui.jobs")

    def show_dock(self, *_):
        """Show the Progress Monitor (it replaced the old Run progress dock)."""
        ext = self._ext()
        if ext is not None:
            ext.show_monitor(self._job.id if self._job is not None else None)
            return
        self.dock.show()  # no monitor installed: fall back to the old dock
        self.dock.raise_()
        self.tabs.setCurrentIndex(0)

    def show_history(self, *_):
        """The old dock, on its History tab."""
        if not self.dock.isVisible():
            self.dock.resize(760, 320)
        self.dock.show()
        self.dock.raise_()
        self.tabs.setCurrentIndex(1)
        self.refresh()

    def _on_button(self, *_):
        self.show_dock()
        self.refresh()

    def _stop(self):
        ws = getattr(self.mw, "_drone", None)
        if ws is not None and ws.server is not None:
            ws.server.stop_job()
            self.stop_btn.setEnabled(False)
            self.step_label.setText("Stopping after the current step...")

    def _copy_log(self):
        QGuiApplication.clipboard().setText(self.log.toPlainText())

    # ------------------------------------------------------------------ reading state
    def refresh(self):
        ws = getattr(self.mw, "_drone", None)  # never create the workspace from here
        if ws is None or getattr(ws, "project_dir", None) is None or getattr(ws, "server", None) is None:
            return
        if ws.project_dir != self._hist_project:
            self._hist_project = ws.project_dir
            self._hist_run = None
            self._reset_rows()
            self.log.clear()
            self._nlog = 0
            self._load_history(ws)
        try:
            p = ws.server.jobs.snapshot()
        except Exception:
            return
        self.apply_progress(p)
        if not p.get("running") and p.get("run_id") != self._hist_run:
            self._hist_run = p.get("run_id")
            self._load_history(ws)

    def _load_history(self, ws):
        try:
            from opencae.drone import pipeline
            self.set_history(pipeline.run_history(ws.project_dir))
        except Exception:
            self.set_history([])

    def row_state(self, aid: str) -> str:
        return self._rows[aid]["s"]

    def _reset_rows(self):
        for aid in ORDER:
            self._set_row(aid, {"state": "waiting"}, "")

    def apply_progress(self, p: dict) -> None:
        running = bool(p.get("running"))
        run_id = p.get("run_id")
        new_run = running and not self._was_running or (run_id and run_id != self._seen_run)
        if new_run:
            self._seen_run = run_id
            self.log.clear()
            self._nlog = 0
            self.show_dock()
        now = time.time()
        frac = max(0.0, min(1.0, float(p.get("frac") or 0)))
        self.bar.setValue(int(round(frac * 1000)))
        self.bar.setFormat(f"{frac * 100:.0f}%")
        started, finished = p.get("started"), p.get("finished")
        analyses = p.get("analyses") or {}
        step = str(p.get("step") or "")
        for aid in ORDER:
            self._set_row(aid, analyses.get(aid) or {"state": "waiting"}, step)
        any_running = any(self._rows[a]["s"] == "running" for a in ORDER)
        if any_running and not self.spin.isActive():
            self.spin.start()
        elif not any_running and self.spin.isActive():
            self.spin.stop()
        err = str(p.get("error") or "")
        if running:
            self.state_label.setText("Running")
            self.elapsed_label.setText("Elapsed " + fmt_dur(now - started) if started else "")
            eta = p.get("eta_s")
            self.eta_label.setText("First run, no estimate yet" if eta is None else
                                   ("Almost done" if eta < 2 else "About " + fmt_dur(eta) + " left"))
            self.step_label.setText(step)
        elif started and finished:
            took = fmt_dur(finished - started)
            self.state_label.setText("Stopped" if err == "Stopped." else "Failed" if err else "Finished")
            self.elapsed_label.setText("Took " + took)
            self.eta_label.setText(err if err and err != "Stopped." else "")
            self.step_label.setText("")
        self.stop_btn.setEnabled(running)
        log = p.get("log") or []
        if run_id != self._log_run or len(log) < self._nlog:
            self._log_run = run_id
            self.log.clear()
            self._nlog = 0
        for line in log[self._nlog:]:
            self.log.appendPlainText(str(line))
        self._nlog = len(log)
        if self._was_running and not running:
            self._notify(p, err, (finished - started) if started and finished else 0.0)
        self._was_running = running
        try:
            self._report(p, running, run_id, err, started, finished, log)
        except Exception:  # noqa: BLE001 - reporting must never break the tracker
            import logging
            logging.getLogger("opencae.ui").exception("job report failed")

    # ------------------------------------------------------------------ reporting to the Progress Monitor
    def _report(self, p: dict, running: bool, run_id, err: str, started, finished, log: list) -> None:
        ext = self._ext()
        if ext is None:
            return
        from opencae.ui.jobs import registry as R
        reg = ext.reg
        job = self._job
        if job is not None and job.active and run_id and getattr(job, "_run_id", None) not in (None, run_id) and running:
            reg.finish(job, True)     # a new run began before the old one was closed
            job = self._job = None
        if (job is None or not job.active) and running and run_id not in self._closed:
            key, name, dd = ext.drone_key()
            reg.register_project(key, name, dd, activate=getattr(self.mw, "mode", "") == "drone")
            kind = str(p.get("kind") or "run")
            title = {"run": "Drone analysis run", "iterate": "Drone design iteration"}.get(kind, "Drone " + kind)
            job = reg.create_job(key, "drone", title, name, "drone", "dashboard", stages=[],
                                 inputs={"project": key, "kind": kind},
                                 cancel=lambda: self._cancel_job(), retry=None)
            job._run_id = run_id
            self._job, self._job_state, self._job_nlog = job, {}, 0
        if job is None:
            return
        if run_id and getattr(job, "_run_id", None) is None:
            job._run_id = run_id
        if not job.active:
            return
        analyses = p.get("analyses") or {}
        only = []
        for aid in ORDER:
            a = analyses.get(aid) or {}
            st = a.get("state") or "waiting"
            if st == "skipped":
                if aid in self._job_state:   # it looked planned before the plan was known
                    self._job_state.pop(aid)
                    reg.drop_stage(job, NAMES[aid])
                continue
            only.append(aid)
            mapped = {"waiting": "pending", "running": "running", "done": "done", "cached": "done",
                      "error": "failed"}.get(st, "pending")
            detail = ("Reused: nothing changed" if st == "cached" else str(p.get("step") or "") if st == "running"
                      else str(a.get("status") or "") if st == "done" else "")
            if self._job_state.get(aid) != (mapped, detail):
                self._job_state[aid] = (mapped, detail)
                reg.stage(job, NAMES[aid], mapped, detail=detail)
        frac = p.get("frac")
        if frac is not None and self._job_state.get("_frac") != frac:
            self._job_state["_frac"] = frac
            reg.set_progress(job, frac)
        lines = log or []
        if len(lines) < self._job_nlog:
            self._job_nlog = 0
        for ln in lines[self._job_nlog:]:
            reg.log(job, str(ln), "error" if str(ln).lower().startswith("error") else "info")
        self._job_nlog = len(lines)
        job.inputs["analyses"] = ", ".join(NAMES[a] for a in only)
        if not running and (finished or err or p.get("result") is not None or run_id):
            stopped = err == "Stopped."
            outputs = {"opener": "drone.dashboard", "project_dir": job.project_path, "run_id": run_id}
            verdict = (p.get("result") or {}).get("verdict")
            if verdict:
                outputs["verdict"] = verdict
            only_ids = list(only)
            reg.set_callbacks(job, retry=lambda ids=only_ids: self._retry_run(ids))
            reg.finish(job, not err, error=err or None, outputs=outputs, cancelled=stopped)

    def _cancel_job(self) -> None:
        job = self._job
        if job is not None and getattr(job, "_run_id", None):
            self._closed.add(job._run_id)
        self._stop()

    def _retry_run(self, ids: list):
        ws = getattr(self.mw, "_drone", None)
        if ws is None or ws.server is None:
            return None
        ws.server.run({"only": ids} if ids else {})
        self._closed.clear()
        self._job = None
        try:
            self._report_now()
        except Exception:  # noqa: BLE001
            pass
        return self._job

    def _report_now(self) -> None:
        ws = getattr(self.mw, "_drone", None)
        if ws is not None and ws.server is not None:
            self.apply_progress(ws.server.jobs.snapshot())

    def _set_row(self, aid: str, a: dict, step: str):
        r = self._rows[aid]
        state = a.get("state") or "waiting"
        glyph, colour, text = STATES.get(state, STATES["waiting"])
        r["s"] = state
        r["glyph"], r["colour"] = glyph, colour
        r["icon"].setText(SPINNER[self._frame % 4] if state == "running" else glyph)
        r["icon"].setStyleSheet(f"font-size:16px; color:{colour};")
        r["state"].setText(text)
        r["state"].setStyleSheet(f"color:{colour}; font-weight:600;")
        if state == "running" and a.get("started"):
            r["dur"].setText(fmt_dur(time.time() - a["started"]))
        else:
            r["dur"].setText(fmt_dur(a["duration_s"]) if a.get("duration_s") is not None and
                             state in ("done", "error") else "")
        if state == "running":
            r["step"].setText(step)
        elif state == "cached":
            r["step"].setText("Reused: nothing changed since the last run")
        elif state == "skipped":
            r["step"].setText("Not part of this run")
        elif state == "error":
            r["step"].setText("Did not finish, see the log")
        elif state == "done":
            r["step"].setText(str(a.get("status") or ""))
        else:
            r["step"].setText("")

    def _animate(self):
        self._frame += 1
        for r in self._rows.values():
            if r["s"] == "running":
                r["icon"].setText(SPINNER[self._frame % 4])

    # ------------------------------------------------------------------ finish
    def _notify(self, p: dict, err: str, took: float):
        verdict = (p.get("result") or {}).get("verdict")
        if err == "Stopped.":
            msg = f"Run stopped after {fmt_dur(took)}"
        elif err:
            msg = f"Run failed after {fmt_dur(took)}: {err}"
        else:
            msg = f"Run finished in {fmt_dur(took)}" + (f" — verdict: {verdict}" if verdict else "")
        self.mw.statusBar().showMessage(msg, 20000)
        try:
            from PySide6.QtWidgets import QSystemTrayIcon
            if QSystemTrayIcon.isSystemTrayAvailable():
                if self._tray is None:
                    self._tray = QSystemTrayIcon(self.mw.windowIcon(), self.mw)
                self._tray.show()
                self._tray.showMessage("OpenCAE run", msg, QSystemTrayIcon.Information, 8000)
        except Exception:
            pass

    # ------------------------------------------------------------------ history
    def set_history(self, rows: list) -> None:
        rows = list(reversed(rows or []))  # newest first
        t = self.history
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            an = r.get("analyses") or {}
            what = ", ".join(NAMES.get(a, a) + (" (reused)" if (v or {}).get("cached") else "")
                             for a, v in an.items()) or ("All analyses" if not r.get("only") else
                                                         ", ".join(r["only"]))
            status = r.get("status")
            verdict = r.get("verdict") or ("stopped" if status == "cancelled" else
                                           "failed" if status == "error" else "-")
            if status == "cancelled" and r.get("verdict"):
                verdict = "stopped (" + str(r["verdict"]) + ")"
            vals = [_when(r.get("started")), r.get("variant") or "", what, fmt_dur(r.get("duration_s")), verdict]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c == 4:
                    low = str(v).lower()
                    it.setForeground(Qt.red if status == "error" else
                                     Qt.green if "ready" in low and "not" not in low else Qt.yellow
                                     if "condition" in low or "stopped" in low else Qt.red if "not ready" in low
                                     else Qt.gray)
                if r.get("error"):
                    it.setToolTip(str(r["error"]))
                t.setItem(i, c, it)
        t.setVisible(bool(rows))
        self.history_empty.setVisible(not rows)


def install(main_window) -> RunTracker:
    return RunTracker(main_window)
