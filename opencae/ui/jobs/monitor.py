"""The Progress Monitor dock: every job (Studio, Drone, K09-L, multibody, setup) in one friendly list.

``JobsExtension`` is what ``opencae.ui.jobs.install`` returns. It owns the shared :class:`JobRegistry` (made the
default one), the dock on the left (stacked under the model tree, never replacing it), the View menu entry, the
status-bar counter, and the hooks the Studio mesh / solve handlers call (``studio_event``).
"""
from __future__ import annotations

import html
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QRectF, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QDockWidget, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QSizePolicy, QTextEdit, QToolButton, QVBoxLayout, QWidget)

from opencae.ui import theme
from opencae.ui.jobs import registry as R
from opencae.ui.jobs.registry import Job, JobRegistry

log = logging.getLogger("opencae.jobs")

KIND_ICON = {"mesh": "mesh", "structural": "deform", "thermal": "contour", "modal": "animate", "fluid": "perspective",
             "motion": "transform", "drone": "app", "parameter_study": "quality", "setup": "settings",
             "other": "solve"}
KIND_NAME = {"mesh": "Mesh", "structural": "Structural", "thermal": "Thermal", "modal": "Modal", "fluid": "Fluid flow",
             "motion": "Motion", "drone": "Drone analysis", "parameter_study": "Parameter study",
             "setup": "Solver setup", "other": "Job"}
STATE_TEXT = {"queued": "Waiting", "running": "Running", "paused": "Paused", "done": "Done", "failed": "Failed",
              "cancelled": "Cancelled"}
STATE_COLOR = {"queued": theme.FG_DIM, "running": theme.ACCENT, "paused": theme.WARN, "done": theme.OK,
               "failed": theme.ERR, "cancelled": theme.ACCENT_2}
STAGE_GLYPH = {"pending": ("○", theme.FG_DIM), "running": ("◐", theme.ACCENT), "done": ("✔", theme.OK),
               "failed": ("✖", theme.ERR), "skipped": ("–", theme.FG_DIM)}
CASE_GLYPH = {"queued": ("○", theme.FG_DIM), "running": ("◐", theme.ACCENT), "done": ("✔", theme.OK),
              "failed": ("✖", theme.ERR), "cancelled": ("–", theme.FG_DIM)}
TYPE_NAME = {"linear_static": "static structural", "modal": "natural frequencies", "buckling": "buckling",
             "thermal_steady": "steady thermal", "thermal_transient": "transient thermal",
             "thermal_stress": "thermal stress", "cfd_incompressible": "airflow (CFD)", "mbd": "multibody motion",
             "k09l": "K09-L design review"}
MAX_CARDS = 40


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
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


# ------------------------------------------------------------------------------------------------ small widgets
class Elided(QLabel):
    """A label that shortens its text with an ellipsis instead of widening the dock."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(10)
        self.setTextFormat(Qt.PlainText)
        super().setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text
        self.setToolTip(text)
        self.update()

    def text(self) -> str:
        return self._full

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        p.setFont(self.font())
        fm = QFontMetrics(self.font())
        p.drawText(self.rect(), int(self.alignment()) | Qt.AlignVCenter,
                   fm.elidedText(self._full, Qt.ElideRight, self.width()))

    def sizeHint(self):  # noqa: N802
        s = super().sizeHint()
        s.setWidth(QFontMetrics(self.font()).horizontalAdvance(self._full))
        return s


class Clickable(QWidget):
    clicked = Signal()

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(ev)


class StageStrip(QWidget):
    """One small segment per stage; the running one pulses."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(8)
        self.stages: list = []
        self.phase = 0

    def set_stages(self, stages: list) -> None:
        self.stages = [(s["name"], s["state"]) for s in stages]
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        if not self.stages:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        n, gap = len(self.stages), 3
        w = (self.width() - gap * (n - 1)) / n
        for i, (_, st) in enumerate(self.stages):
            c = QColor({"done": theme.OK, "running": theme.ACCENT, "failed": theme.ERR, "skipped": theme.BORDER,
                        "pending": theme.BORDER}.get(st, theme.BORDER))
            if st == "running":
                c.setAlphaF(0.55 + 0.45 * (0.5 + 0.5 * __import__("math").sin(self.phase / 2.2)))
            elif st == "skipped":
                c.setAlphaF(0.5)
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(QRectF(i * (w + gap), 1, w, 6), 3, 3)


class SlimBar(QWidget):
    """A thin progress bar: filled to a real fraction, or a sliding block when the amount is not known."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(6)
        self.value: Optional[float] = None
        self.color = theme.ACCENT
        self.phase = 0

    def set_state(self, value: Optional[float], color: str) -> None:
        self.value, self.color = value, color
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0, 0, self.width(), self.height())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#171a20"))
        p.drawRoundedRect(r, 3, 3)
        p.setBrush(QColor(self.color))
        if self.value is not None:
            p.drawRoundedRect(QRectF(0, 0, max(6.0, r.width() * self.value), r.height()), 3, 3)
        else:
            bw = r.width() * 0.28
            x = ((self.phase * 6) % (r.width() + bw)) - bw
            p.setClipRect(r)
            p.drawRoundedRect(QRectF(x, 0, bw, r.height()), 3, 3)


def _btn(text: str, tip: str, primary: bool = False) -> QPushButton:
    b = QPushButton(text)
    b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    b.setFixedHeight(22)
    col = theme.ACCENT if primary else theme.FG
    b.setStyleSheet(f"QPushButton {{ padding:0 9px; border:1px solid {theme.BORDER}; border-radius:4px; "
                    f"background:{theme.BG_3}; color:{col}; font-size:11px; }} "
                    f"QPushButton:hover {{ border-color:{theme.ACCENT}; }}")
    return b


class CaseRow(QWidget):
    """One case of a parameter study; click to see its detail."""

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.head = Clickable()
        h = QHBoxLayout(self.head)
        h.setContentsMargins(0, 1, 0, 1)
        h.setSpacing(5)
        self.glyph = QLabel()
        self.glyph.setFixedWidth(14)
        self.name = Elided()
        self.state = QLabel()
        self.arrow = QLabel("▸")
        self.arrow.setStyleSheet(f"color:{theme.FG_DIM};")
        for w in (self.glyph, self.name, self.state, self.arrow):
            h.addWidget(w)
        h.setStretch(1, 1)
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.RichText)
        self.body.setContentsMargins(19, 0, 0, 3)
        self.body.hide()
        v.addWidget(self.head)
        v.addWidget(self.body)
        self.head.clicked.connect(self._toggle)
        self._expanded = False

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self.body.setVisible(self._expanded)
        self.arrow.setText("▾" if self._expanded else "▸")

    def update_case(self, c: dict) -> None:
        g, col = CASE_GLYPH.get(c["state"], CASE_GLYPH["queued"])
        self.glyph.setText(g)
        self.glyph.setStyleSheet(f"color:{col};")
        self.name.setText(c["name"])
        self.state.setText({"queued": "waiting", "running": "running", "done": "done", "failed": "failed",
                            "cancelled": "stopped"}.get(c["state"], c["state"]))
        self.state.setStyleSheet(f"color:{col}; font-size:11px;")
        parts = []
        if c.get("detail"):
            parts.append(html.escape(c["detail"]))
        if c.get("error"):
            parts.append(f"<span style='color:{theme.ERR}'>{html.escape(c['error'])}</span>")
        if c.get("started") and c.get("finished"):
            parts.append(f"took {fmt_dur(c['finished'] - c['started'])}")
        self.body.setText("<br>".join(parts) or "<span style='color:%s'>No more detail.</span>" % theme.FG_DIM)


# ------------------------------------------------------------------------------------------------ the card
class JobCard(QFrame):
    def __init__(self, monitor: "JobMonitor", job_id: str):
        super().__init__()
        self.mon, self.job_id = monitor, job_id
        self.setObjectName("jobcard")
        self.expanded = False
        self._snap: dict = {}
        self._n_cases = -1
        self.case_rows: list = []
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)
        self.head = Clickable()
        self.head.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(self.head)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(7)
        self.icon = QLabel()
        self.icon.setFixedSize(22, 22)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = Elided()
        self.title.setStyleSheet("font-weight:600;")
        self.sub = Elided()
        self.sub.setStyleSheet(f"color:{theme.FG_DIM}; font-size:11px;")
        titles.addWidget(self.title)
        titles.addWidget(self.sub)
        self.chip = QLabel()
        self.arrow = QLabel("▸")
        self.arrow.setStyleSheet(f"color:{theme.FG_DIM};")
        h.addWidget(self.icon)
        h.addLayout(titles, 1)
        h.addWidget(self.chip)
        h.addWidget(self.arrow)
        v.addWidget(self.head)
        self.strip = StageStrip()
        v.addWidget(self.strip)
        meta = QHBoxLayout()
        meta.setSpacing(6)
        self.meta = Elided()
        self.meta.setStyleSheet(f"color:{theme.FG}; font-size:11px;")
        self.elapsed = QLabel()
        self.elapsed.setStyleSheet(f"color:{theme.FG_DIM}; font-size:11px;")
        meta.addWidget(self.meta, 1)
        meta.addWidget(self.elapsed)
        v.addLayout(meta)
        self.bar = SlimBar()
        v.addWidget(self.bar)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color:{theme.ERR}; font-size:11px;")
        v.addWidget(self.error)
        brow = QHBoxLayout()
        brow.setSpacing(5)
        self.b_cancel = _btn("Cancel", "Stop this job")
        self.b_retry = _btn("Retry", "Start this job again with the same setup", True)
        self.b_open = _btn("Open results", "Show the results of this job", True)
        self.b_pause = _btn("Pause", "Pause this job")
        for b in (self.b_cancel, self.b_retry, self.b_open, self.b_pause):
            brow.addWidget(b)
        brow.addStretch(1)
        self.btn_box = QWidget()
        self.btn_box.setStyleSheet("background:transparent;")
        self.btn_box.setLayout(brow)
        brow.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self.btn_box)
        # details (click the header)
        self.details = QWidget()
        self.details.setStyleSheet("background:transparent;")
        d = QVBoxLayout(self.details)
        d.setContentsMargins(0, 2, 0, 0)
        d.setSpacing(5)
        self.stages_label = QLabel()
        self.stages_label.setTextFormat(Qt.RichText)
        self.stages_label.setWordWrap(True)
        d.addWidget(self.stages_label)
        self.cases_box = QVBoxLayout()
        self.cases_box.setSpacing(0)
        self.cases_head = QLabel()
        self.cases_head.setStyleSheet("font-weight:600;")
        d.addWidget(self.cases_head)
        d.addLayout(self.cases_box)
        self.log_label = QLabel("Log")
        self.log_label.setStyleSheet(f"color:{theme.FG_DIM}; font-size:11px;")
        d.addWidget(self.log_label)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(130)
        self.log.setLineWrapMode(QTextEdit.NoWrap)
        self.log.setStyleSheet(f"QTextEdit {{ background:#171a20; border:1px solid {theme.BORDER}; "
                               f"border-radius:4px; font-family:Consolas,'DejaVu Sans Mono',monospace; "
                               f"font-size:10px; }}")
        d.addWidget(self.log)
        self.io = QLabel()
        self.io.setTextFormat(Qt.RichText)
        self.io.setWordWrap(True)
        self.io.setTextInteractionFlags(Qt.TextSelectableByMouse)
        d.addWidget(self.io)
        self.details.hide()
        v.addWidget(self.details)
        self.head.clicked.connect(self.toggle)
        self.b_cancel.clicked.connect(lambda: monitor.reg.cancel(job_id))
        self.b_retry.clicked.connect(lambda: monitor.retry(job_id))
        self.b_open.clicked.connect(lambda: monitor.open_results(job_id))
        self.b_pause.clicked.connect(self._pause)

    def _pause(self) -> None:
        j = self._snap
        if j.get("state") == "paused":
            self.mon.reg.resume(self.job_id)
        else:
            self.mon.reg.pause(self.job_id)

    def toggle(self, on: Optional[bool] = None) -> None:
        self.expanded = (not self.expanded) if on is None else bool(on)
        self.details.setVisible(self.expanded)
        self.arrow.setText("▾" if self.expanded else "▸")
        if self.expanded and self._snap:
            self._fill_details(self._snap)

    # -- content
    def update_from(self, j: dict, active_project: Optional[str], show_project: bool, now: float) -> None:
        self._snap = j
        state = j["state"]
        col = STATE_COLOR.get(state, theme.FG_DIM)
        from opencae.ui.icons import icon
        self.icon.setPixmap(icon(KIND_ICON.get(j["kind"], "solve")).pixmap(20, 20))
        self.title.setText(j["title"])
        sub = [j["target"]] if j["target"] else []
        at = TYPE_NAME.get(j["analysis_type"], j["analysis_type"].replace("_", " "))
        if at:
            sub.append(at)
        if show_project and j.get("project_name"):
            sub.insert(0, j["project_name"])
        self.sub.setText("  ·  ".join(sub) or KIND_NAME.get(j["kind"], ""))
        self.chip.setText(STATE_TEXT.get(state, state))
        self.chip.setStyleSheet(f"color:{col}; border:1px solid {col}; border-radius:8px; padding:0 7px; "
                                f"font-size:10px; font-weight:600;")
        self.setStyleSheet(f"QFrame#jobcard {{ background:{theme.BG_2}; border:1px solid "
                           f"{col if state in ('failed',) else theme.BORDER}; border-radius:6px; }} "
                           f"QFrame#jobcard QLabel {{ background:transparent; }}")
        stages = [s for s in j["stages"] if s["state"] != "skipped" or state != "done"]
        self.strip.setVisible(bool(stages))
        self.strip.set_stages(stages)
        self.strip.setToolTip("\n".join(f"{STAGE_GLYPH[s['state']][0]} {s['name']}" for s in stages))
        cur = next((s for s in j["stages"] if s["state"] == "running"), None)
        counts = _counts(j)
        # status line
        if counts:
            line = f"{counts['done'] + counts['failed']}/{counts['total']} cases"
            if counts["failed"]:
                line += f", {counts['failed']} failed"
            cs = next((c for c in j["children"] if c["state"] == "running"), None)
            if cs and state == "running":
                line += f" · now: {cs['name']}"
        elif state == "queued":
            line = "Waiting to start"
        elif state == "paused":
            line = "Paused"
        elif state == "running":
            line = (cur["name"] + (" – " + cur["detail"] if cur["detail"] else "")) if cur else "Working…"
        elif state == "done":
            line = "Finished" + (" – " + _last_detail(j) if _last_detail(j) else "")
        elif state == "cancelled":
            line = "Stopped before it finished"
        else:
            line = "Did not finish"
        self.meta.setText(line)
        frac = j["progress"]
        if frac is None and cur is not None:
            frac = cur["progress"]
        pct = f"{frac * 100:.0f}% · " if (frac is not None and state in ("running", "paused")) else ""
        self.elapsed.setText(pct + fmt_dur(_elapsed(j, now)) if j["started"] else "")
        self.bar.setVisible(state in ("running", "paused", "queued"))
        self.bar.set_state(frac, col if state != "queued" else theme.BORDER)
        self.error.setVisible(state in ("failed",) and bool(j["error"]))
        self.error.setText(j["error"][:400])
        self.b_cancel.setVisible(j.get("can_cancel", False))
        self.b_retry.setVisible(j.get("can_retry", False))
        self.b_open.setVisible(j.get("has_opener", False))
        self.b_pause.setVisible(j.get("can_pause", False) or j.get("can_resume", False))
        self.b_pause.setText("Resume" if state == "paused" else "Pause")
        self.btn_box.setVisible(any(b.isVisibleTo(self) for b in (self.b_cancel, self.b_retry, self.b_open,
                                                                    self.b_pause)))
        if self.expanded:
            self._fill_details(j)

    def tick(self, now: float, phase: int) -> None:
        j = self._snap
        if not j or j["state"] not in ("running", "paused", "queued"):
            return
        self.strip.phase = phase
        self.bar.phase = phase
        self.strip.update()
        if j["state"] == "running":
            self.bar.update()
            frac = j["progress"]
            cur = next((s for s in j["stages"] if s["state"] == "running"), None)
            if frac is None and cur is not None:
                frac = cur["progress"]
            pct = f"{frac * 100:.0f}% · " if frac is not None else ""
            self.elapsed.setText(pct + fmt_dur(_elapsed(j, now)))

    def _fill_details(self, j: dict) -> None:
        now = time.time()
        rows = []
        for s in j["stages"]:
            g, c = STAGE_GLYPH.get(s["state"], STAGE_GLYPH["pending"])
            dur = ""
            if s["started"]:
                dur = fmt_dur(((s["finished"] or now) - s["started"]))
            det = f" <span style='color:{theme.FG_DIM}'>{html.escape(s['detail'])}</span>" if s["detail"] else ""
            pr = f" {s['progress'] * 100:.0f}%" if (s["progress"] is not None and s["state"] == "running") else ""
            rows.append(f"<tr><td style='color:{c}'>{g}&nbsp;</td><td>{html.escape(s['name'])}{pr}{det}</td>"
                        f"<td align='right' style='color:{theme.FG_DIM}'>&nbsp;{dur}</td></tr>")
        self.stages_label.setText("<table cellspacing='1'>" + "".join(rows) + "</table>" if rows else "")
        self.stages_label.setVisible(bool(rows))
        # cases
        ch = j["children"]
        if len(ch) != self._n_cases:
            while self.case_rows:
                w = self.case_rows.pop()
                self.cases_box.removeWidget(w)
                w.deleteLater()
            for _ in ch:
                w = CaseRow()
                self.cases_box.addWidget(w)
                self.case_rows.append(w)
            self._n_cases = len(ch)
        for w, c in zip(self.case_rows, ch):
            w.update_case(c)
        cc = _counts(j)
        self.cases_head.setVisible(bool(ch))
        if cc:
            self.cases_head.setText(f"Cases: {cc['done']} done, {cc['failed']} failed, "
                                    f"{cc['running']} running, {cc['queued']} waiting")
        # log (warnings highlighted)
        entries = j["log"][-120:]
        parts = []
        for lv, text in entries:
            colr = theme.ERR if lv == "error" else theme.WARN if lv == "warning" else theme.FG
            parts.append(f"<span style='color:{colr}; white-space:pre'>{html.escape(text)}</span>")
        sb = self.log.verticalScrollBar()
        at_end = sb.value() >= sb.maximum() - 4
        self.log.setHtml("<br>".join(parts))
        if at_end:
            sb.setValue(sb.maximum())
        self.log.setVisible(bool(parts))
        self.log_label.setVisible(bool(parts))
        self.log_label.setText(f"Log (last {len(entries)} lines)" + (f" · {len(j['warnings'])} warning(s)"
                                                                      if j["warnings"] else ""))
        # inputs / outputs
        def kv(title, d):
            if not d:
                return ""
            items = "".join(f"<tr><td style='color:{theme.FG_DIM}'>{html.escape(str(k).replace('_', ' '))}&nbsp;</td>"
                            f"<td>{html.escape(_short(v))}</td></tr>" for k, v in d.items() if k != "opener")
            return f"<b>{title}</b><table cellspacing='1'>{items}</table>" if items else ""
        self.io.setText(kv("Inputs", j["inputs"]) + kv("Outputs", j["outputs"]) +
                        (f"<span style='color:{theme.FG_DIM}'>Started from the {_SRC.get(j['source'], j['source'])}"
                         f"{' · retry of an earlier job' if j.get('retry_of') else ''}</span>"))


_SRC = {"gui": "buttons", "console": "command console", "cli": "command line", "dashboard": "dashboard"}


def _short(v) -> str:
    s = str(v)
    return s if len(s) <= 90 else s[:40] + " … " + s[-40:]


def _elapsed(j: dict, now: float) -> float:
    return max(0.0, ((j["finished"] if j["finished"] is not None else now) - j["started"]) if j["started"] else 0.0)


def _counts(j: dict) -> Optional[dict]:
    ch = j["children"]
    if not ch:
        return None
    c = {"total": len(ch), "done": 0, "failed": 0, "running": 0, "queued": 0, "cancelled": 0}
    for x in ch:
        c[x["state"]] = c.get(x["state"], 0) + 1
    return c


def _last_detail(j: dict) -> str:
    for s in reversed(j["stages"]):
        if s["state"] == "done" and s["detail"]:
            return s["detail"]
    return ""


# ------------------------------------------------------------------------------------------------ the dock content
class JobMonitor(QWidget):
    changed = Signal(str)        # registry events re-queued into the GUI thread

    def __init__(self, reg: JobRegistry, parent=None):
        super().__init__(parent)
        self.reg = reg
        self.cards: dict[str, JobCard] = {}
        self._order: list[str] = []
        self._dirty: set = set()
        self._dismissed: set = set()
        self._phase = 0
        self.show_other_history = False
        self.on_retry: Optional[Callable] = None
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 6)
        v.setSpacing(6)
        head = QFrame()
        head.setObjectName("jobhead")
        head.setStyleSheet(f"QFrame#jobhead {{ background:{theme.BG_2}; border:1px solid {theme.BORDER}; "
                           f"border-radius:6px; }} QLabel {{ background:transparent; }}")
        hv = QVBoxLayout(head)
        hv.setContentsMargins(9, 6, 9, 6)
        hv.setSpacing(2)
        self.project_label = Elided()
        self.project_label.setStyleSheet("font-weight:700; font-size:13px;")
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.RichText)
        self.now_label = Elided()
        self.now_label.setStyleSheet(f"color:{theme.FG_DIM}; font-size:11px;")
        hv.addWidget(self.project_label)
        hv.addWidget(self.status_label)
        hv.addWidget(self.now_label)
        v.addWidget(head)
        tools = QHBoxLayout()
        tools.setSpacing(6)
        self.other_check = QCheckBox("Other projects")
        self.other_check.setToolTip("Also list the finished jobs of other projects. Jobs that are still running in "
                                    "another project are always listed.")
        self.other_check.setStyleSheet("font-size:11px;")
        self.other_check.toggled.connect(self._toggle_other)
        self.clear_btn = _btn("Clear finished", "Hide finished jobs from this list (the history is kept on disk)")
        self.clear_btn.clicked.connect(self.clear_finished)
        tools.addWidget(self.other_check)
        tools.addStretch(1)
        tools.addWidget(self.clear_btn)
        v.addLayout(tools)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget()
        self.list_layout = QVBoxLayout(host)
        self.list_layout.setContentsMargins(0, 0, 4, 0)
        self.list_layout.setSpacing(6)
        self.empty = QLabel("Nothing is running.\n\nWhen you mesh, solve or run a study it appears here with its "
                            "stages and live progress. Finished jobs stay here, and so do failed ones, so you can see "
                            "what went wrong and try again.")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.empty.setStyleSheet(f"color:{theme.FG_DIM}; padding:6px;")
        self.list_layout.addWidget(self.empty)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(host)
        v.addWidget(self.scroll, 1)
        self.changed.connect(self._on_changed, Qt.QueuedConnection)
        self._off = reg.subscribe(lambda ev: self.changed.emit(ev.job_id))
        self.flush_timer = QTimer(self)
        self.flush_timer.setSingleShot(True)
        self.flush_timer.setInterval(70)
        self.flush_timer.timeout.connect(self.flush)
        self.tick_timer = QTimer(self)
        self.tick_timer.setInterval(140)
        self.tick_timer.timeout.connect(self._tick)
        self.refresh()

    # -- events
    @Slot(str)
    def _on_changed(self, job_id: str) -> None:
        self._dirty.add(job_id)
        if not self.flush_timer.isActive():
            self.flush_timer.start()

    def _toggle_other(self, on: bool) -> None:
        self.show_other_history = on
        self.refresh()

    def clear_finished(self) -> None:
        for j in self.reg.jobs():
            if not j.active:
                self._dismissed.add(j.id)
        self.refresh()

    def retry(self, job_id: str) -> None:
        new = self.reg.retry(job_id)
        if new is not None:
            self.focus_job(new.id)

    def open_results(self, job_id: str) -> None:
        if not self.reg.open_results(job_id):
            win = self.window()
            if hasattr(win, "statusBar"):
                win.statusBar().showMessage("These results cannot be opened from here (open the project first).", 6000)

    # -- list
    def _visible(self) -> list:
        ap = self.reg.active_project
        out = []
        for j in self.reg.jobs():
            if j.id in self._dismissed and not j.active:
                continue
            if j.project_path in (ap, "") or ap is None or j.active or self.show_other_history:
                out.append(j.id)
        return out[:MAX_CARDS]

    def refresh(self) -> None:
        """Re-read everything (project switch, a job appeared or went)."""
        ids = self._visible()
        for jid in list(self.cards):
            if jid not in ids:
                c = self.cards.pop(jid)
                self.list_layout.removeWidget(c)
                c.deleteLater()
        if ids != self._order:
            for jid in ids:
                if jid not in self.cards:
                    self.cards[jid] = JobCard(self, jid)
            for jid in self._order:
                if jid in self.cards:
                    self.list_layout.removeWidget(self.cards[jid])
            for i, jid in enumerate(ids):
                self.list_layout.insertWidget(i, self.cards[jid])
            self._order = ids
        self.empty.setVisible(not ids)
        self._dirty.update(ids)
        self.flush()

    def flush(self) -> None:
        self.flush_timer.stop()
        ids = self._visible()
        if ids != self._order or any(j not in self.cards for j in self._dirty if j in ids):
            self._dirty.clear()
            return self.refresh()
        ap = self.reg.active_project
        now = time.time()
        for jid in list(self._dirty):
            c = self.cards.get(jid)
            snap = self.reg.snapshot(jid)
            if c is not None and snap is not None:
                c.update_from(snap, ap, snap["project_path"] not in (ap, ""), now)
        self._dirty.clear()
        self._update_header()
        running = any(j.state == "running" for j in self.reg.jobs())
        if running and not self.tick_timer.isActive():
            self.tick_timer.start()
        elif not running and self.tick_timer.isActive():
            self.tick_timer.stop()

    def _tick(self) -> None:
        self._phase += 1
        now = time.time()
        for c in self.cards.values():
            c.tick(now, self._phase)
        if self._phase % 7 == 0:
            self._update_header()

    def _update_header(self) -> None:
        ap = self.reg.active_project
        self.project_label.setText(("Project: " + self.reg.project_name(ap)) if ap else "No project open")
        every = self.reg.jobs()
        c = {"running": 0, "queued": 0, "paused": 0, "failed": 0}
        for j in every:
            if j.project_path == ap or j.active:
                if j.state in c:
                    c[j.state] += 1
        bits = []
        if c["running"]:
            bits.append(f"<span style='color:{theme.ACCENT}'>● {c['running']} running</span>")
        if c["paused"]:
            bits.append(f"<span style='color:{theme.WARN}'>❚❚ {c['paused']} paused</span>")
        if c["queued"]:
            bits.append(f"<span style='color:{theme.FG_DIM}'>○ {c['queued']} waiting</span>")
        if c["failed"]:
            bits.append(f"<span style='color:{theme.ERR}'>✖ {c['failed']} failed</span>")
        done = sum(1 for j in every if j.state == "done" and j.project_path == ap)
        if done and not (c["running"] or c["queued"]):
            bits.append(f"<span style='color:{theme.OK}'>✔ {done} done</span>")
        self.status_label.setText("   ".join(bits) or f"<span style='color:{theme.FG_DIM}'>All quiet</span>")
        act = [j for j in every if j.state == "running"]
        if act:
            j = act[0]
            st = j.current_stage()
            elapsed = fmt_dur(j.elapsed())
            other = f" ({j.project_name})" if j.project_path != ap else ""
            self.now_label.setText(f"Now: {j.title}{other} · {st.name if st else 'working'} · {elapsed}")
        else:
            self.now_label.setText("")
        self.now_label.setVisible(bool(act))

    def tick_now(self) -> None:
        """Test helper: run one animation/elapsed tick."""
        self._tick()

    # -- focus
    def focus_job(self, job_id: str) -> None:
        self._dismissed.discard(job_id)
        self.refresh()
        c = self.cards.get(job_id)
        if c is not None:
            c.toggle(True)
            QTimer.singleShot(0, lambda c=c: self.scroll.ensureWidgetVisible(c))


def _counts_from(reg: JobRegistry) -> dict:
    return reg.counts()


# ------------------------------------------------------------------------------------------------ the extension
class JobsExtension(QObject):
    """Dock, menu entry, status-bar counter and the Studio hooks."""

    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        self.reg = JobRegistry()
        R.set_default(self.reg)
        self._cur: dict = {}                 # Studio process kind -> Job
        self._live: list = []                # running callable threads
        self._seen_failed: set = set()
        self._build()
        self.reg.register_opener("studio.results", self._open_studio_results)
        self.reg.register_opener("studio.mesh", self._open_studio_mesh)
        self.reg.register_opener("drone.dashboard", self._open_drone)
        self.sync_project()
        self.reg.subscribe(lambda ev: self.counter_sig.emit())
        self.counter_sig.connect(self.update_counter, Qt.QueuedConnection)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.sync_project)
        self.timer.start()
        mw.stack.currentChanged.connect(lambda *_: self.sync_project())
        self.monitor.refresh()
        self.update_counter()

    counter_sig = Signal()

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        mw = self.mw
        self.monitor = JobMonitor(self.reg)
        self.monitor.setMinimumWidth(250)
        self.dock = QDockWidget("Progress Monitor", mw)
        self.dock.setObjectName("progress_monitor_dock")
        self.dock.setWidget(self.monitor)
        self.dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable |
                              QDockWidget.DockWidgetClosable)
        self.dock.setToolTip("Everything that is running or has run: meshing, solving, studies and more")
        mw.addDockWidget(Qt.LeftDockWidgetArea, self.dock)
        tree = getattr(mw, "tree_dock", None)
        if tree is not None:      # stack under the model tree: both stay visible, neither is replaced
            mw.setCorner(Qt.BottomLeftCorner, Qt.LeftDockWidgetArea)   # the left column runs down to the status bar
            mw.splitDockWidget(tree, self.dock, Qt.Vertical)
            mw.resizeDocks([tree, self.dock], [300, 420], Qt.Vertical)
        self.action = self.dock.toggleViewAction()
        self.action.setText("Progress Monitor")
        self.action.setStatusTip("Show or hide the Progress Monitor")
        for a in mw.menuBar().actions():
            if a.text().replace("&", "") == "View" and a.menu() is not None:
                a.menu().addSeparator()
                a.menu().addAction(self.action)
                break
        self.counter = QPushButton()
        self.counter.setFlat(True)
        self.counter.setCursor(Qt.PointingHandCursor)
        self.counter.clicked.connect(lambda: self.show_monitor(mark_seen=True))
        mw.statusBar().addPermanentWidget(self.counter)
        self.dock.visibilityChanged.connect(lambda v: self._mark_seen() if v else None)

    def show_monitor(self, job_id: Optional[str] = None, mark_seen: bool = False) -> None:
        self.dock.show()
        self.dock.raise_()
        if job_id:
            self.monitor.focus_job(job_id)
        if mark_seen:
            self._mark_seen()

    def _mark_seen(self) -> None:
        self._seen_failed |= {j.id for j in self.reg.jobs() if j.state == "failed"}
        self.update_counter()

    def update_counter(self) -> None:
        c = self.reg.counts()
        unseen = [j for j in self.reg.jobs() if j.state == "failed" and j.id not in self._seen_failed
                  and (self.dock.isVisible() is False)]
        if c["running"] or c["paused"]:
            n = c["running"] + c["paused"]
            txt, col = f"● {n} running", theme.ACCENT
        elif c["queued"]:
            txt, col = f"○ {c['queued']} waiting", theme.FG_DIM
        elif unseen:
            txt, col = f"✖ {len(unseen)} failed", theme.ERR
        else:
            txt, col = "No jobs running", theme.FG_DIM
        self.counter.setText(txt)
        self.counter.setToolTip("Open the Progress Monitor: every job, its stages and its history")
        self.counter.setStyleSheet(f"QPushButton {{ border:none; color:{col}; padding:0 8px; }} "
                                   f"QPushButton:hover {{ color:{theme.FG}; }}")

    # ------------------------------------------------------------------ project tracking
    def studio_key(self) -> tuple:
        s = self.mw.session
        if s.path:
            return str(s.path), s.project.name, Path(s.path).with_name(Path(s.path).stem + "_data")
        return "(unsaved project)", s.project.name or "Untitled", None

    def sync_project(self) -> str:
        """Tell the registry which project is open (Studio or Drone) and make its history available."""
        mw = self.mw
        key = None
        try:
            ws = getattr(mw, "_drone", None)
            if mw.mode == "drone" and ws is not None and getattr(ws, "project_dir", None):
                d = Path(ws.project_dir)
                key, name, dd = str(d), d.name, d / "results"
            else:
                key, name, dd = self.studio_key()
            if key != "(unsaved project)" and "(unsaved project)" in self.reg.projects() \
                    and mw.mode == "studio":
                self.reg.rename_project("(unsaved project)", key, name, dd)
            self.reg.register_project(key, name, dd, activate=True)
        except Exception:  # noqa: BLE001
            log.exception("job monitor: project sync failed")
            return key or ""
        if self.reg.active_project != key:
            self.reg.set_active_project(key)
        if getattr(self.monitor, "_shown_project", None) != key:
            self.monitor._shown_project = key
            self.monitor.refresh()
        return key

    def drone_key(self) -> tuple:
        ws = getattr(self.mw, "_drone", None)
        pd = getattr(ws, "project_dir", None)
        if pd:
            d = Path(pd)
            return str(d), d.name, d / "results"
        return "(drone project)", "Drone project", None

    # ------------------------------------------------------------------ openers
    def _open_studio_results(self, job: Job) -> None:
        mw = self.mw
        if mw.mode != "studio":
            mw.stack.setCurrentWidget(mw.viewport)
        if mw.session.results is not None:
            mw.set_view_mode("results")
        else:
            mw.statusBar().showMessage("Open the project and study of this job to see its results.", 6000)

    def _open_studio_mesh(self, job: Job) -> None:
        mw = self.mw
        if mw.mode != "studio":
            mw.stack.setCurrentWidget(mw.viewport)
        if mw.session.mesh is not None:
            mw.set_view_mode("mesh")

    def _open_drone(self, job: Job) -> None:
        d = job.outputs.get("project_dir") or job.project_path
        ws = getattr(self.mw, "_drone", None)
        if ws is not None and str(getattr(ws, "project_dir", "")) == str(d):
            self.mw.enter_drone_mode()
        elif d and Path(d).exists():
            self.mw.open_drone_project(Path(d))

    # ------------------------------------------------------------------ layout persistence
    def save_layout(self) -> None:
        try:
            for d in getattr(self.mw, "_hidden_docks", []):   # docks hidden only for Drone mode come back next time
                d.show()
            self.mw.settings.setValue("window_state", self.mw.saveState(2))
        except Exception:  # noqa: BLE001
            log.exception("could not save the window layout")

    def restore_layout(self) -> bool:
        try:
            st = self.mw.settings.value("window_state")
            if st:
                return bool(self.mw.restoreState(st, 2))
        except Exception:  # noqa: BLE001
            log.exception("could not restore the window layout")
        return False

    # ------------------------------------------------------------------ Studio mesh / solve hooks
    def _analysis_kind(self) -> str:
        t = self.mw.session.project.analysis.analysis_type
        return ("fluid" if t.startswith("cfd") else "thermal" if t.startswith("thermal") else
                "modal" if t in ("modal", "buckling") else "motion" if t == "mbd" else "structural")

    def studio_event(self, event: str, **kw) -> None:
        try:
            getattr(self, "_ev_" + event)(**kw)
        except Exception:  # noqa: BLE001 - reporting must never break a run
            log.exception("job report %s failed", event)

    def _retry_cb(self, kind: str) -> Callable:
        mw = self.mw

        def retry():
            before = self._cur.get(kind)
            (mw.cmd_generate_mesh if kind == "mesh" else mw.cmd_solve)()
            now = self._cur.get(kind)
            return now if now is not before else None
        return retry

    def _base_inputs(self) -> dict:
        s = self.mw.session
        d = {"study": s.study.name, "analysis": TYPE_NAME.get(s.project.analysis.analysis_type,
                                                              s.project.analysis.analysis_type)}
        if s.mesh is not None:
            d["mesh"] = f"{s.mesh.n_nodes} nodes, {s.mesh.n_elements} elements"
        return d

    def _ev_started(self, kind: str) -> None:
        mw, s = self.mw, self.mw.session
        key, name, dd = self.studio_key()
        self.reg.register_project(key, name, dd, activate=True)
        study = s.study.name
        at = s.project.analysis.analysis_type
        if kind == "mesh":
            job = self.reg.create_job(key, "mesh", f"Mesh: {study}", study, at, "gui", inputs=self._base_inputs(),
                                      cancel=mw.cmd_cancel, retry=self._retry_cb("mesh"))
            self.reg.stage(job, R.S_GEOMETRY, "done", detail="Mesh job prepared")
            self.reg.stage(job, R.S_MESH, "running", detail="Starting the mesher")
        elif kind == "solve":
            jk = self._analysis_kind()
            inputs = self._base_inputs()
            be = mw._job.get("backend")
            inputs.update(solver=getattr(be, "name", "solver"), folder=mw._job.get("dir", ""))
            inputs["threads"] = s.project.analysis.threads
            job = self.reg.create_job(key, jk, f"Solve: {study}", study, at, "gui",
                                      stages=(R.S_MESH, R.S_SOLVER, R.S_POST, R.S_READY), inputs=inputs,
                                      cancel=mw.cmd_cancel, retry=self._retry_cb("solve"))
            if s.mesh is not None:
                self.reg.stage(job, R.S_MESH, "done", detail=f"Using the current mesh ({s.mesh.n_elements} elements)")
            else:
                self.reg.stage(job, R.S_MESH, "skipped")
            self.reg.stage(job, R.S_SOLVER, "running", detail="Starting the solver")
        elif kind == "convergence":
            out = Path(mw._job.get("out", ""))
            job = self.reg.create_job(key, "parameter_study", "Mesh convergence study", study, at, "gui", stages=[],
                                      cancel=mw.cmd_cancel)
            try:
                import json
                sizes = json.loads((out.parent / "convergence.job.json").read_text()).get("sizes")
                if sizes:
                    self.reg.set_cases(job, [f"Element size {h:g}" for h in sizes])
            except (OSError, ValueError):
                pass
        else:
            job = self.reg.create_job(key, "other", "Solver benchmarks" if kind == "benchmarks" else kind.title(),
                                      "benchmarks", "", "gui", stages=[], cancel=mw.cmd_cancel)
        self._cur[kind] = job

    def _ev_line(self, kind: str, line: str) -> None:
        job = self._cur.get(kind)
        if job is None or not job.active:
            return
        low = line.lstrip()
        lv = "error" if low.startswith(("*ERROR", "ERROR")) else \
            "warning" if ("*WARNING" in low or low.startswith("WARNING")) else "info"
        self.reg.log(job, line.replace("PROGRESS: ", ""), lv)
        text = line.replace("PROGRESS: ", "").strip()[:80]
        if kind == "mesh":
            if line.startswith("PROGRESS:"):
                self.reg.stage(job, R.S_MESH, "running", detail=text)
        elif kind == "solve":
            from opencae.ui.main_window import SOLVER_STAGES
            if any(k in line for k in SOLVER_STAGES):
                self.reg.stage(job, R.S_SOLVER, "running", detail=text)
        elif kind == "convergence" and line.startswith("PROGRESS: level "):
            import re
            m = re.match(r"PROGRESS: level (\d+)/(\d+): element size", line)
            if m and job.children:
                k = int(m.group(1)) - 1
                for i, c in enumerate(job.children):
                    if i < k and c.state != "done":
                        self.reg.case(job, c.name, "done")
                    elif i == k:
                        self.reg.case(job, c.name, "running")

    def _ev_exited(self, kind: str, code: int, cancelled: bool) -> None:
        job = self._cur.get(kind)
        if job is None or not job.active or cancelled or code != 0:
            return
        if kind == "solve":
            self.reg.stage(job, R.S_SOLVER, "done", detail="Solver finished")
            self.reg.stage(job, R.S_POST, "running", detail="Reading the results")
        elif kind == "mesh":
            self.reg.stage(job, R.S_MESH, "done", detail="Mesh written")

    def _ev_finished(self, kind: str, code: int, cancelled: bool, text: str = "") -> None:
        mw, s = self.mw, self.mw.session
        job = self._cur.get(kind)
        if job is None:
            return
        err = ""
        outputs: dict = {}
        ok = False
        if cancelled:
            pass
        elif kind == "mesh":
            out = Path(mw._job.get("out", ""))
            ok = code == 0 and out.exists() and s.mesh is not None
            if ok:
                st = s.mesh.stats()
                outputs = {"opener": "studio.mesh", "mesh_file": str(out), "nodes": st["nodes"],
                           "elements": st["elements"], "element_type": st["element_type"]}
                self.reg.stage(job, R.S_MESH, "done", detail=f"{st['nodes']} nodes, {st['elements']} elements")
                self.reg.stage(job, R.S_READY, "done", detail="Mesh ready")
            else:
                err = next((ln for ln in text.splitlines() if ln.startswith("ERROR")), f"The mesher stopped "
                           f"(exit code {code}).").replace("ERROR: ", "")
        elif kind == "solve":
            res_dir = s.project.results.directory
            jd = Path(mw._job.get("dir", ""))
            ok = (code == 0 and not s.last_solve_error and bool(res_dir) and s.results is not None and
                  (s.data_dir / res_dir).resolve() == jd.resolve())
            if ok:
                from opencae.postprocessing.summary import headline
                outputs = {"opener": "studio.results", "results_folder": str(jd), "summary": headline(s)}
                self.reg.stage(job, R.S_POST, "done", detail="Results loaded")
                self.reg.stage(job, R.S_READY, "done", detail=outputs["summary"][:90])
            else:
                err = s.last_solve_error or (f"The solver stopped (exit code {code})." if code else
                                             "The results could not be read.")
        elif kind == "convergence":
            ok = code == 0 and Path(mw._job.get("out", "")).exists()
            if ok:
                for c in list(job.children):
                    self.reg.case(job, c.name, "done")
            else:
                err = next((ln for ln in text.splitlines() if ln.startswith("ERROR")),
                           f"The study stopped (exit code {code}).").replace("ERROR: ", "")
        else:
            ok = code == 0
            err = "" if ok else f"Stopped (exit code {code})."
        self.reg.finish(job, ok, error=err or None, outputs=outputs, cancelled=cancelled)

    def _ev_process_failed(self, kind: str, error: str) -> None:
        job = self._cur.get(kind)
        if job is not None:
            self.reg.finish(job, False, error=error)

    def _ev_refused(self, kind: str, reason: str, stage: str = R.S_VALIDATE) -> None:
        """The run could not even start (model errors, solver missing): keep a readable failed job."""
        mw, s = self.mw, self.mw.session
        key, name, dd = self.studio_key()
        self.reg.register_project(key, name, dd, activate=True)
        study, at = s.study.name, s.project.analysis.analysis_type
        jk = "mesh" if kind == "mesh" else self._analysis_kind()
        title = f"Mesh: {study}" if kind == "mesh" else f"Solve: {study}"
        job = self.reg.create_job(key, jk, title, study, at, "gui", stages=[stage], inputs=self._base_inputs(),
                                  cancel=None, retry=self._retry_cb("mesh" if kind == "mesh" else "solve"))
        self.reg.stage(job, stage, "failed", detail="Could not start")
        self.reg.finish(job, False, error=reason)
        self._cur[kind] = job

    # ------------------------------------------------------------------ background callables
    def run_callable(self, kind, title, target, fn, stages, source, analysis_type, inputs, project_path, retry,
                     allow_duplicate, on_done, on_failed, opener) -> Job:
        reg = self.reg
        key = project_path or self.sync_project()
        if not allow_duplicate:
            dup = reg.find_running(kind, target, key)
            if dup is not None:
                self.show_monitor(dup.id)
                self.mw.statusBar().showMessage(f"'{title}' is already running; showing it.", 6000)
                return dup
        cancel_flag = {"stop": False}
        names = R.standard_stages(kind) if stages is None else tuple(stages)

        def cancel():
            cancel_flag["stop"] = True

        def again():
            return self.run_callable(kind, title, target, fn, stages, source, analysis_type, inputs, project_path,
                                     retry, True, on_done, on_failed, opener)
        job = reg.create_job(key, kind, title, target, analysis_type, source, stages=names, inputs=inputs,
                             cancel=cancel, retry=again if retry else None, opener=opener)
        if names:
            reg.stage(job, names[0], "running", detail="Starting")

        def advance(stage_name: str, frac, msg: str) -> None:
            if stage_name not in names:
                if not names:
                    return
                stage_name = names[0]
            idx = names.index(stage_name)
            for earlier in names[:idx]:
                st = job.stage_named(earlier)
                if st is not None and st.state in ("pending", "running"):
                    reg.stage(job, earlier, "done")
            reg.stage(job, stage_name, "running", progress=frac, detail=msg or None)

        def progress(frac=None, message: str = "", stage: Optional[str] = None) -> None:
            if frac is not None:
                frac = max(0.0, min(1.0, float(frac)))
                reg.set_progress(job, frac)
            if message:
                reg.log(job, message)
            target_stage = stage or (R.S_SOLVER if R.S_SOLVER in names else (names[0] if names else None))
            if target_stage:
                advance(target_stage, frac, message)

        th = _CallableThread(fn, progress, lambda: cancel_flag["stop"])
        th.ctx = (job, on_done, on_failed, cancel_flag)
        # bound methods of this QObject: Qt queues them into the GUI thread (a lambda would run in the worker)
        th.done.connect(self._thread_done)
        th.failed.connect(self._thread_failed)
        self._live.append(th)
        from opencae.utilities import threads
        threads.track(th, cancel=cancel)
        th.start()
        return job

    @Slot(object)
    def _thread_done(self, res) -> None:
        th = self.sender()
        job, on_done, _, cancel_flag = th.ctx
        self._callable_done(job, th, res, on_done, cancel_flag)

    @Slot(str)
    def _thread_failed(self, tb: str) -> None:
        th = self.sender()
        job, _, on_failed, cancel_flag = th.ctx
        self._callable_failed(job, th, tb, on_failed, cancel_flag)

    def _callable_done(self, job, th, res, on_done, cancel_flag) -> None:
        th.wait(5000)
        self._forget(th)
        names = [s.name for s in job.stages]
        outputs = dict(res) if isinstance(res, dict) else {}
        if cancel_flag["stop"] or job.state == "cancelled":
            self.reg.finish(job, False, error="Cancelled.", cancelled=True, outputs=outputs)
        else:
            for n in names:
                st = job.stage_named(n)
                if st.state in ("pending", "running"):
                    self.reg.stage(job, n, "done", detail="Results ready" if n == R.S_READY else None)
            self.reg.finish(job, True, outputs=outputs)
        if on_done:
            on_done(res)

    def _callable_failed(self, job, th, tb: str, on_failed, cancel_flag) -> None:
        th.wait(5000)
        self._forget(th)
        lines = [ln for ln in tb.strip().splitlines() if ln.strip()]
        last = lines[-1] if lines else "The job failed."
        for ln in lines[-12:]:
            self.reg.log(job, ln, "error")
        cancelled = cancel_flag["stop"] or "Cancel" in last.split(":")[0]
        if cancelled:
            self.reg.finish(job, False, error="Cancelled.", cancelled=True)
        else:
            self.reg.finish(job, False, error=last.split(": ", 1)[-1] if ": " in last else last)
        if on_failed:
            on_failed(last)

    def _forget(self, th) -> None:
        if th in self._live:
            self._live.remove(th)


class _CallableThread(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, progress, cancelled):
        super().__init__()
        self._fn, self._progress, self._cancelled = fn, progress, cancelled

    def run(self) -> None:
        import traceback
        try:
            res = self._fn(self._progress, self._cancelled)
        except BaseException:  # noqa: BLE001 - reported, never raised into Qt
            self.failed.emit(traceback.format_exc())
            return
        self.done.emit(res)


def track_callable(main_window, kind, title, target, fn, *, stages=None, source="gui", analysis_type="",
                   inputs=None, project_path=None, retry=True, allow_duplicate=False, on_done=None, on_failed=None,
                   opener=None, outputs_from=None) -> Job:
    ext = main_window.extensions.get("opencae.ui.jobs")
    if ext is None:
        raise RuntimeError("the Progress Monitor is not installed")
    return ext.run_callable(kind, title, target, fn, stages, source, analysis_type, inputs, project_path, retry,
                            allow_duplicate, on_done, on_failed, opener)
