"""Timeline, animation and the read-out of Assembly mode.

``PlaybackBar``: slider over the stored samples, play / pause, speed 0.25x to 4x, loop. Scrubbing asks the workspace
to move the picture of the bodies (the source CAD is never changed). ``ReadoutPanel``: the values at the current
time: joint coordinate, speed, acceleration (in degrees), effort, reaction force and moment with frame, units and the
sign convention in words; prescribed and dynamic joints are labelled.
"""
from __future__ import annotations

import math
import time as _time

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel, QPushButton,
                               QSlider, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from opencae.mbd.results import quat_to_matrix
from opencae.ui import theme

SPEEDS = (0.25, 0.5, 1.0, 2.0, 4.0)
DEG = 180.0 / math.pi
SHORT_DRIVE = {"prescribed (kinematic)": "prescribed", "torque-driven (dynamic)": "dynamic",
               "coupled follower (driven by the constraint)": "follower (coupled)", "free": "free",
               "fixed (welded)": "fixed"}


class PlaybackBar(QWidget):
    def __init__(self, ws) -> None:
        super().__init__()
        self.ws = ws
        self.playing = False
        self.speed = 1.0
        self._last = 0.0
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 3, 6, 3)
        self.start_btn = QPushButton("⏮")
        self.start_btn.setToolTip("Back to the start")
        self.start_btn.setFixedWidth(34)
        self.play_btn = QPushButton("Play")
        self.play_btn.setToolTip("Play or pause the animation (Space)")
        self.play_btn.setMinimumWidth(70)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setToolTip("Drag to move through time: the bodies, the read-out and the plot cursor follow")
        self.time_label = QLabel("run the study to see the motion")
        self.time_label.setMinimumWidth(190)
        self.speed_combo = QComboBox()
        for s in SPEEDS:
            self.speed_combo.addItem(f"{s:g}×", s)
        self.speed_combo.setCurrentIndex(SPEEDS.index(1.0))
        self.speed_combo.setToolTip("Playback speed (simulated time per real time)")
        self.loop_check = QCheckBox("Loop")
        lay.addWidget(self.start_btn)
        lay.addWidget(self.play_btn)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.time_label)
        lay.addWidget(self.speed_combo)
        lay.addWidget(self.loop_check)
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self._on_timer)
        self.play_btn.clicked.connect(self.toggle)
        self.start_btn.clicked.connect(lambda: self.set_time(0.0))
        self.slider.valueChanged.connect(self._slider_moved)
        self.speed_combo.currentIndexChanged.connect(lambda _i: setattr(self, "speed", self.speed_combo.currentData()))
        self.set_enabled(False)

    # ---- state ---------------------------------------------------------
    def set_enabled(self, on: bool) -> None:
        for w in (self.start_btn, self.play_btn, self.slider, self.speed_combo, self.loop_check):
            w.setEnabled(on)
        if not on:
            self.pause()
            self.slider.blockSignals(True)
            self.slider.setRange(0, 0)
            self.slider.setValue(0)
            self.slider.blockSignals(False)
            self.time_label.setText("run the study to see the motion")

    def configure(self, res, sample: int = 0) -> None:
        self.slider.blockSignals(True)
        self.slider.setRange(0, len(res.t) - 1)
        self.slider.setValue(int(sample))
        self.slider.blockSignals(False)
        self.set_enabled(True)
        self.set_label(float(res.t[int(sample)]), float(res.t[-1]))

    def set_label(self, t: float, t_end: float) -> None:
        self.time_label.setText(f"t = {t:.4g} s  /  {t_end:.4g} s")

    # ---- control ---------------------------------------------------------
    def set_speed(self, s: float) -> None:
        self.speed = float(s)
        k = self.speed_combo.findData(self.speed)
        if k >= 0:
            self.speed_combo.setCurrentIndex(k)

    def set_time(self, t: float) -> None:
        res = self.ws.result
        if res is None:
            return
        i = int(np.argmin(np.abs(res.t - t)))
        self.slider.blockSignals(True)
        self.slider.setValue(i)
        self.slider.blockSignals(False)
        self.ws.set_sample(i)

    def _slider_moved(self, i: int) -> None:
        self.ws.set_sample(int(i))

    def toggle(self) -> None:
        self.pause() if self.playing else self.play()

    def play(self) -> None:
        res = self.ws.result
        if res is None:
            return
        if self.ws.current_time >= float(res.t[-1]) - 1e-12:
            self.set_time(0.0)
        self.playing = True
        self._last = _time.monotonic()
        self.play_btn.setText("Pause")
        self.timer.start()

    def pause(self) -> None:
        self.playing = False
        self.timer.stop()
        self.play_btn.setText("Play")

    def _on_timer(self) -> None:
        now = _time.monotonic()
        dt, self._last = now - self._last, now
        self.tick(dt)

    def tick(self, dt_wall: float) -> None:
        """Advance by ``dt_wall`` real seconds (x speed). Loops or stops at the end."""
        res = self.ws.result
        if res is None:
            return
        t_end = float(res.t[-1])
        t = self.ws.current_time + dt_wall * self.speed
        if t > t_end + 1e-12:
            if self.loop_check.isChecked() and t_end > 0:
                t = t - t_end * math.floor(t / t_end)
            else:
                t = t_end
                self.pause()
        self.set_time(t)


# ------------------------------------------------------------------------------------------------ read-out
class ReadoutPanel(QWidget):
    """Values at the current time. ``data`` holds the numbers (also for tests): ``t``, ``gap_mm`` (if the gripper
    helper applies) and ``joints[joint id]`` with q, q_deg, qd, effort, reaction vectors and the drive label."""

    ROWS = ["Drive", "Angle / position", "Speed", "Acceleration", "Effort", "Reaction force", "Reaction moment"]

    def __init__(self, ws) -> None:
        super().__init__()
        self.ws = ws
        self.data: dict = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.header = QLabel("No results yet. Run the study (Run) to see values here.")
        self.header.setWordWrap(True)
        lay.addWidget(self.header)
        self.gap_label = QLabel("")
        self.gap_label.setObjectName("hint")
        self.gap_label.setWordWrap(True)
        lay.addWidget(self.gap_label)
        self.table = QTableWidget(len(self.ROWS), 0)  # one column per joint, one row per quantity
        self.table.setVerticalHeaderLabels(self.ROWS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectColumns)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setMaximumHeight(70 + 30 * len(self.ROWS))
        self.table.setWordWrap(True)
        lay.addWidget(self.table)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail.setAlignment(Qt.AlignTop)
        lay.addWidget(self.detail, 1)
        self.sign = QLabel("")
        self.sign.setObjectName("hint")
        self.sign.setWordWrap(True)
        lay.addWidget(self.sign)
        self.table.horizontalHeader().sectionClicked.connect(self._row_selected)
        self._rows: list[str] = []

    def sign_text(self) -> str:
        return self.sign.text()

    def clear(self) -> None:
        self.data = {}
        self.table.setColumnCount(0)
        self.header.setText("No results yet. Run the study (Run) to see values here.")
        self.gap_label.setText("")
        self.detail.setText("")
        self.sign.setText("")

    def _row_selected(self, col: int = -1) -> None:
        if 0 <= col < len(self._rows) and self.ws.selected != ("joint", self._rows[col]):
            self.ws.select("joint", self._rows[col])

    # ---- compute ---------------------------------------------------------
    def update_for(self, i: int) -> None:
        ws = self.ws
        res = ws.result
        asm = ws.assembly
        if res is None:
            self.clear()
            return
        i = int(min(max(i, 0), len(res.t) - 1))
        t = float(res.t[i])
        infos = {j["id"]: j for j in res.meta.get("joints", [])}
        u = ws.length_unit
        data: dict = {"t": t, "index": i, "joints": {}}
        for j in asm.joints:
            info = infos.get(j.id)
            if info is None or j.id not in res.joint_q:
                continue
            prism = j.kind == "prismatic"
            q, qd, qdd = float(res.joint_q[j.id][i]), float(res.joint_qd[j.id][i]), float(res.joint_qdd[j.id][i])
            e = float(res.joint_effort[j.id][i])
            Fj, Mj = res.reaction_force_joint[j.id][i], res.reaction_moment_joint[j.id][i]
            row = {"name": j.name, "kind": j.kind, "drive": info["driver_kind"], "q": q, "qd": qd, "qdd": qdd,
                   "effort": e, "F_joint": Fj.tolist(), "M_joint": Mj.tolist(),
                   "F_assembly": res.reaction_force_assembly[j.id][i].tolist(),
                   "M_assembly": res.reaction_moment_assembly[j.id][i].tolist(),
                   "F_total_joint": res.reaction_force_total_joint[j.id][i].tolist(),
                   "M_total_joint": res.reaction_moment_total_joint[j.id][i].tolist(),
                   "force_N": float(np.linalg.norm(Fj)), "moment_Nm": float(np.linalg.norm(Mj)),
                   "unit_q": u if prism else "deg", "unit_qd": f"{u}/s" if prism else "deg/s",
                   "unit_qdd": f"{u}/s²" if prism else "deg/s²", "unit_effort": "N" if prism else "N·m"}
            if not prism:
                row.update(q_deg=q * DEG, qd_deg_s=qd * DEG, qdd_deg_s2=qdd * DEG)
            data["joints"][j.id] = row
        gap = ws.fingertip_gap_mm(i)
        if gap is not None:
            data["gap_mm"] = gap
        self.data = data
        self.header.setText(f"<b>t = {t:.4g} s</b> of {float(res.t[-1]):.4g} s — study '{res.meta.get('study', '')}'"
                            + ("  <span style='color:%s'>(model changed since this run: run again)</span>" % theme.WARN
                               if ws.result_stale else ""))
        self.gap_label.setText(f"Fingertip gap: {gap:.4f} {u} (distance between the inner fingertip corners)"
                               if gap is not None else "")
        self._fill_table(data)
        self._fill_detail(data, i)
        self.sign.setText(self._sign_text(res))

    def _fmt(self, v: float, unit: str) -> str:
        return f"{0.0 if abs(v) < 1e-12 else v:.4g} {unit}"

    def _fill_table(self, data: dict) -> None:
        asm = self.ws.assembly
        sel = self.ws.selected
        ids = [j.id for j in asm.joints if j.id in data["joints"] and (j.kind != "fixed" or sel == ("joint", j.id))]
        self._rows = ids
        self.table.blockSignals(True)
        self.table.setColumnCount(len(ids))
        self.table.setRowCount(len(self.ROWS))
        self.table.setVerticalHeaderLabels(self.ROWS)
        self.table.setHorizontalHeaderLabels([data["joints"][i]["name"] for i in ids])
        for c, jid in enumerate(ids):
            d = data["joints"][jid]
            q = d["q_deg"] if "q_deg" in d else d["q"]
            qd = d["qd_deg_s"] if "qd_deg_s" in d else d["qd"]
            qdd = d["qdd_deg_s2"] if "qdd_deg_s2" in d else d["qdd"]
            drive = SHORT_DRIVE.get(d["drive"], d["drive"])
            cells = [drive, self._fmt(q, d["unit_q"]), self._fmt(qd, d["unit_qd"]), self._fmt(qdd, d["unit_qdd"]),
                     self._fmt(d["effort"], d["unit_effort"]) if d["kind"] != "fixed" else "\u2014",
                     self._fmt(d["force_N"], "N"), self._fmt(d["moment_Nm"], "N\u00b7m")]
            for r, txt in enumerate(cells):
                it = QTableWidgetItem(txt)
                it.setTextAlignment(Qt.AlignCenter)
                if r == 0:
                    it.setForeground(QColor(theme.ACCENT) if drive == "prescribed" else
                                     QColor(theme.ACCENT_2) if drive == "dynamic" else QColor(theme.FG_DIM))
                    it.setToolTip({"prescribed": "Prescribed motion: the joint follows its command exactly",
                                   "dynamic": "Dynamic: the motor applies a torque / force and the motion is computed",
                                   }.get(drive, "Moves with the rest of the mechanism"))
                self.table.setItem(r, c, it)
            if sel == ("joint", jid):
                self.table.selectColumn(c)
        self.table.resizeRowsToContents()
        self.table.blockSignals(False)

    def _focus_joint(self):
        ws = self.ws
        asm = ws.assembly
        kind, ident = ws.selected if ws.selected else ("", "")
        if kind == "joint":
            return asm.joint(ident)
        if kind == "driver":
            d = ws.driver_by_id(ident)
            return asm.joint(d.joint) if d else None
        if kind == "body":
            return next((j for j in asm.joints if j.child == ident), None)
        return None

    def _fill_detail(self, data: dict, i: int) -> None:
        ws = self.ws
        res = ws.result
        j = self._focus_joint()
        parts = []
        if j is not None and j.id in data["joints"]:
            d = data["joints"][j.id]
            info = next(x for x in res.meta["joints"] if x["id"] == j.id)
            f = lambda v: "(" + ", ".join(f"{x:.4g}" for x in v) + ")"  # noqa: E731
            parts.append(f"<b>{j.name}</b> — reaction at the joint origin, exerted BY '{info['parent_name']}' "
                         f"ON '{info['child_name']}':")
            parts.append(f"in the joint frame (z = joint axis): F = {f(d['F_joint'])} N, M = {f(d['M_joint'])} N·m")
            parts.append(f"in the assembly frame: F = {f(d['F_assembly'])} N, M = {f(d['M_assembly'])} N·m")
            if d["kind"] != "fixed":
                parts.append(f"motor effort along the axis: {d['effort']:.4g} {d['unit_effort']} "
                             f"({SHORT_DRIVE.get(d['drive'], d['drive'])}); transmitted incl. the motor: "
                             f"F = {f(d['F_total_joint'])} N, M = {f(d['M_total_joint'])} N·m")
        kind, ident = ws.selected if ws.selected else ("", "")
        if kind == "body" and ident in res.body_quat:
            R = quat_to_matrix(res.body_quat[ident][i])
            ang = math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(R) - 1) / 2))))
            pos = res.body_pos[ident][i]
            b = ws.assembly.body(ident)
            parts.append(f"<b>{b.name if b else ident}</b>: centre of mass at {fvec(pos)} {ws.length_unit}, "
                         f"turned {ang:.4g} deg from the reference pose")
        self.detail.setText("<br>".join(parts) if parts else "Select a joint or a body to see its details here.")

    def _sign_text(self, res) -> str:
        sc = res.meta.get("sign_conventions", {})
        txt = ("Signs: " + sc.get("reaction", "") + ". Angle: " + sc.get("q", "") + ". Effort: "
               + sc.get("effort", "") + ".")
        return txt


def fvec(v, sig: int = 4) -> str:
    return "(" + ", ".join(f"{float(x):.{sig}g}" for x in v) + ")"
