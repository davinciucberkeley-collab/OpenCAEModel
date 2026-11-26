"""Plots of Assembly mode: chosen quantities against time, a cursor at the current time, CSV and PNG export.

Quantities with different units get their own stacked axes (shared time axis). Clicking in a plot moves the
playback to that time.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("QtAgg")
import numpy as np  # noqa: E402
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QPushButton,  # noqa: E402
                               QVBoxLayout, QWidget)

from opencae.ui import theme  # noqa: E402

DEG = 180.0 / math.pi
RPM_PER_RAD_S = 60.0 / (2 * math.pi)
PALETTE = ["#4c9be8", "#e8a33d", "#4cbb7f", "#e5534b", "#9a6fe0", "#2fb5a8", "#c9b11d", "#ff7a1a"]
QUANTITIES = [("command", "Commanded motion"), ("angle", "Joint angle / position"), ("gap", "Fingertip gap"),
              ("velocity", "Speed"), ("effort", "Motor effort"), ("force", "Reaction force"),
              ("moment", "Reaction moment")]
TITLES = dict(QUANTITIES)
SHORT = {"command": "Command", "angle": "Angle", "gap": "Gap", "velocity": "Speed", "effort": "Effort",
         "force": "Reaction force", "moment": "Reaction moment"}
NOISE = 1e-9  # effort / reaction series below this are numerical zero: drawn flat and labelled


class PlotCanvas(FigureCanvasQTAgg):
    def __init__(self) -> None:
        self.fig = Figure(figsize=(6, 3), facecolor=theme.BG_2)
        super().__init__(self.fig)
        self.setMinimumHeight(150)


class PlotsPanel(QWidget):
    def __init__(self, ws) -> None:
        super().__init__()
        self.ws = ws
        self._series: list[dict] = []
        self._cursors: list = []
        self._cursor_t = 0.0
        self._auto = True
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        side = QVBoxLayout()
        side.addWidget(QLabel("<b>Plot</b>"))
        self.checks: dict[str, QCheckBox] = {}
        for key, title in QUANTITIES:
            cb = QCheckBox(title)
            cb.toggled.connect(self._user_toggled)
            self.checks[key] = cb
            side.addWidget(cb)
        row = QHBoxLayout()
        row.addWidget(QLabel("Speed in"))
        self.speed_unit = QComboBox()
        self.speed_unit.addItems(["deg/s", "rpm"])
        self.speed_unit.currentIndexChanged.connect(lambda _i: self.refresh())
        row.addWidget(self.speed_unit)
        side.addLayout(row)
        self.csv_btn = QPushButton("Export CSV…")
        self.csv_btn.setToolTip("One labelled CSV file per joint and per body (units, frame and sign in the header)")
        self.png_btn = QPushButton("Save PNG…")
        self.csv_btn.clicked.connect(lambda: self.ws.export_csv_dialog())
        self.png_btn.clicked.connect(lambda: self.save_png())
        side.addWidget(self.csv_btn)
        side.addWidget(self.png_btn)
        side.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(side)
        wrap.setFixedWidth(170)
        lay.addWidget(wrap)
        self.canvas = PlotCanvas()
        self.canvas.mpl_connect("button_press_event", self._clicked)
        lay.addWidget(self.canvas, 1)
        self.empty = QLabel("Run the study to see plots.")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setObjectName("hint")
        self._draw_empty()

    # ---- selection ---------------------------------------------------------
    def quantities(self) -> list[str]:
        return [k for k, cb in self.checks.items() if cb.isChecked()]

    def set_quantities(self, keys) -> None:
        self._auto = False
        for k, cb in self.checks.items():
            cb.blockSignals(True)
            cb.setChecked(k in keys)
            cb.blockSignals(False)
        self.refresh()

    def _user_toggled(self) -> None:
        self._auto = False
        self.refresh()

    def _defaults(self) -> list[str]:
        ws = self.ws
        res = ws.result
        modes = {d["mode"] for d in (res.meta.get("drivers", []) if res else [])}
        keys = []
        if modes == {"velocity"}:
            keys = ["velocity", "effort"]
        else:
            keys = ["angle"]
            if ws.has_gap():
                keys.append("gap")
            keys.append("effort")
        return keys

    def has_data(self) -> bool:
        return bool(self._series) and all(len(s["t"]) > 1 for s in self._series)

    def series(self) -> list[dict]:
        return list(self._series)

    def cursor_time(self) -> float:
        return self._cursor_t

    # ---- build -----------------------------------------------------------
    def _speed_factor(self) -> tuple[str, float]:
        return ("rpm", RPM_PER_RAD_S) if self.speed_unit.currentText() == "rpm" else ("deg/s", DEG)

    def build_series(self) -> list[dict]:
        ws = self.ws
        res = ws.result
        if res is None:
            return []
        asm = ws.assembly
        u = ws.length_unit
        t = np.asarray(res.t, float)
        su, sf = self._speed_factor()
        out: list[dict] = []
        movers = [j for j in asm.joints if j.kind != "fixed" and j.id in res.joint_q]

        def add(key, label, unit, y):
            y = np.asarray(y, float)
            if key in ("effort", "force", "moment") and np.max(np.abs(y)) < NOISE:
                y, label = np.zeros_like(y), f"{label} (zero within {NOISE:g})"  # display only; CSV has the raw values
            out.append({"quantity": key, "label": label, "unit": unit, "t": t, "y": y})
        for key in self.quantities():
            if key == "command":
                for d in res.meta.get("drivers", []):
                    j = asm.joint(d["joint"])
                    if j is None or d["id"] not in res.driver_command:
                        continue
                    y = np.asarray(res.driver_command[d["id"]], float)
                    prism = j.kind == "prismatic"
                    if d["mode"] == "effort":
                        add(key, d["name"], "N" if prism else "N·m", y)
                    elif d["mode"] == "position":
                        add(key, d["name"], u if prism else "deg", y if prism else y * DEG)
                    else:
                        add(key, d["name"], f"{u}/s" if prism else su, y if prism else y * sf)
            elif key == "angle":
                for j in movers:
                    y = res.joint_q[j.id]
                    add(key, j.name, u if j.kind == "prismatic" else "deg", y if j.kind == "prismatic" else y * DEG)
            elif key == "gap":
                g = ws.gap_series()
                if g is not None:
                    add(key, "Fingertip gap", u, g)
            elif key == "velocity":
                for j in movers:
                    y = res.joint_qd[j.id]
                    add(key, j.name, f"{u}/s" if j.kind == "prismatic" else su,
                        y if j.kind == "prismatic" else y * sf)
            elif key == "effort":
                for j in movers:
                    add(key, j.name, "N" if j.kind == "prismatic" else "N·m", res.joint_effort[j.id])
            elif key == "force":
                for j in movers:
                    add(key, j.name, "N", np.linalg.norm(res.reaction_force_joint[j.id], axis=1))
            elif key == "moment":
                for j in movers:
                    add(key, j.name, "N·m", np.linalg.norm(res.reaction_moment_joint[j.id], axis=1))
        return out

    def refresh(self) -> None:
        ws = self.ws
        if ws.result is None:
            self._series = []
            self._draw_empty()
            return
        if self._auto:
            want = self._defaults()
            for k, cb in self.checks.items():
                cb.blockSignals(True)
                cb.setChecked(k in want)
                cb.blockSignals(False)
        for k, cb in self.checks.items():
            if k == "gap":
                cb.setEnabled(ws.has_gap())
                if not ws.has_gap():
                    cb.blockSignals(True)
                    cb.setChecked(False)
                    cb.blockSignals(False)
        self._series = self.build_series()
        self._draw()

    # ---- drawing ---------------------------------------------------------
    def _style(self, ax) -> None:
        ax.set_facecolor("#171a20")
        for s in ax.spines.values():
            s.set_color(theme.BORDER)
        ax.tick_params(colors=theme.FG_DIM, labelsize=7.5)
        ax.yaxis.label.set_color(theme.FG)
        ax.xaxis.label.set_color(theme.FG_DIM)
        ax.grid(True, color="#2a303a", linewidth=0.6)

    def _draw_empty(self) -> None:
        fig = self.canvas.fig
        fig.clear()
        ax = fig.add_subplot(111)
        self._style(ax)
        ax.set_xticks([])
        ax.set_yticks([])
        ax.text(0.5, 0.5, "Run the study to see plots", ha="center", va="center", color=theme.FG_DIM,
                transform=ax.transAxes)
        self._cursors = []
        self.canvas.draw_idle()

    def _draw(self) -> None:
        fig = self.canvas.fig
        fig.clear()
        self._cursors = []
        groups: dict[tuple[str, str], list[dict]] = {}
        for s in self._series:
            groups.setdefault((s["quantity"], s["unit"]), []).append(s)
        if not groups:
            self._draw_empty()
            return
        order = [k for k, _ in QUANTITIES]
        keys = sorted(groups, key=lambda k: (order.index(k[0]), k[1]))
        axes = fig.subplots(len(keys), 1, sharex=True, squeeze=False)[:, 0]
        for ax, key in zip(axes, keys):
            self._style(ax)
            for n, s in enumerate(groups[key]):
                ax.plot(s["t"], s["y"], color=PALETTE[n % len(PALETTE)], linewidth=1.5, label=s["label"])
            ax.set_ylabel(f"{SHORT[key[0]]} ({key[1]})", fontsize=7.5)
            if len(groups[key]) > 1 or key[0] in ("command",) or "zero within" in groups[key][0]["label"]:
                leg = ax.legend(fontsize=6.5, loc="best", frameon=False, labelcolor=theme.FG)
            self._cursors.append(ax.axvline(self._cursor_t, color=theme.ACCENT_2, linewidth=1.2))
        axes[-1].set_xlabel("time (s)", fontsize=8)
        fig.subplots_adjust(left=0.12, right=0.985, top=0.98, bottom=0.14 if len(keys) < 4 else 0.1, hspace=0.18)
        self.canvas.draw_idle()

    def set_cursor(self, t: float) -> None:
        self._cursor_t = float(t)
        for c in self._cursors:
            c.set_xdata([t, t])
        self.canvas.draw_idle()

    def _clicked(self, ev) -> None:
        if ev.inaxes is not None and ev.xdata is not None and self.ws.result is not None:
            self.ws.playback.set_time(float(ev.xdata))

    # ---- export ----------------------------------------------------------
    def save_png(self, path: Optional[Path] = None) -> Optional[Path]:
        if path is None:
            name, _ = QFileDialog.getSaveFileName(self, "Save plot", str(Path.home() / "motion_plot.png"),
                                                  "PNG image (*.png)")
            if not name:
                return None
            path = Path(name)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.canvas.fig.savefig(str(path), dpi=150, facecolor=self.canvas.fig.get_facecolor())
        return path
