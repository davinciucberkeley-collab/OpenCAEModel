"""Dockable panels: properties, console, solver, mesh diagnostics, issues,
summary, workflow navigator and results controls."""
from __future__ import annotations

import html
import logging
from typing import Any, Optional

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
                               QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QToolButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from opencae.ui import theme
from opencae.ui.forms import Field, FormWidget
from opencae.ui.icons import icon

MONO = QFont("DejaVu Sans Mono")
MONO.setStyleHint(QFont.Monospace)
MONO.setPointSize(9)


# --------------------------------------------------------------------------- properties
class PropertiesPanel(QWidget):
    applied = Signal(str, str, dict)  # kind, id, values

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.title = QLabel("Nothing selected")
        self.title.setStyleSheet("font-weight:600; font-size:13.5px; padding:4px 2px;")
        self.title.setWordWrap(True)
        lay.addWidget(self.title)
        self.note = QLabel("")
        self.note.setObjectName("hint")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        lay.addWidget(self.scroll, 1)
        self.error = QLabel("")
        self.error.setObjectName("error")
        self.error.setWordWrap(True)
        lay.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch(1)
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setObjectName("primary")
        self.apply_btn.clicked.connect(self._apply)
        row.addWidget(self.apply_btn)
        lay.addLayout(row)
        self._kind, self._id = "", ""
        self.form: Optional[FormWidget] = None
        self.show_fields("Nothing selected", "", "", [], "Select an item in the model tree or the viewport.")

    def show_fields(self, title: str, kind: str, ident: str, fields: list[Field], note: str = "") -> None:
        self._kind, self._id = kind, ident
        self.title.setText(title)
        self.note.setText(note)
        self.note.setVisible(bool(note))
        self.error.setText("")
        self.form = FormWidget(fields)
        self.form.changed.connect(lambda: self.error.setText(""))
        self.scroll.setWidget(self.form)
        editable = any(not f.readonly and f.kind != "text" for f in fields)
        self.apply_btn.setVisible(editable)

    def _apply(self) -> None:
        if self.form is None:
            return
        try:
            vals = self.form.values()
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.applied.emit(self._kind, self._id, vals)


# --------------------------------------------------------------------------- console / logging
class _LogBridge(QObject):
    record = Signal(str, int)


class QtLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.bridge = _LogBridge()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.bridge.record.emit(self.format(record), record.levelno)
        except RuntimeError:  # widget deleted at shutdown
            pass


class ConsolePanel(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(MONO)
        self.setMaximumBlockCount(5000)

    def append(self, text: str, level: int = logging.INFO) -> None:
        col = {logging.ERROR: theme.ERR, logging.WARNING: theme.WARN}.get(level, theme.FG)
        self.appendHtml(f"<span style='color:{col}'>{html.escape(text)}</span>")
        self.moveCursor(QTextCursor.End)


class SolverPanel(QWidget):
    cancel = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        row = QHBoxLayout()
        self.state = QLabel("Idle")
        self.state.setStyleSheet("font-weight:600;")
        row.addWidget(self.state)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        row.addWidget(self.bar, 1)
        self.cancel_btn = QPushButton(icon("stop"), "Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(lambda *_: self.cancel.emit())
        row.addWidget(self.cancel_btn)
        lay.addLayout(row)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(MONO)
        self.log.setMaximumBlockCount(20000)
        lay.addWidget(self.log, 1)

    def start(self, what: str) -> None:
        self.state.setText(what)
        self.bar.setRange(0, 0)  # busy indicator
        self.cancel_btn.setEnabled(True)
        self.log.appendPlainText(f"===== {what} =====")

    def line(self, text: str) -> None:
        self.log.appendPlainText(text)

    def stage(self, text: str, percent: Optional[int] = None) -> None:
        self.state.setText(text)
        if percent is not None:
            self.bar.setRange(0, 100)
            self.bar.setValue(percent)

    def finish(self, ok: bool, text: str) -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(100 if ok else 0)
        self.cancel_btn.setEnabled(False)
        self.state.setText(text)
        self.state.setStyleSheet(f"font-weight:600; color:{theme.OK if ok else theme.ERR};")


class MeshDiagnostics(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Metric", "Value"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self.table, 2)
        self.canvas = None
        try:
            from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
            from matplotlib.figure import Figure
            self.fig = Figure(figsize=(5, 2.2), facecolor=theme.BG_2)
            self.canvas = FigureCanvasQTAgg(self.fig)
            lay.addWidget(self.canvas, 3)
        except Exception:  # pragma: no cover
            pass

    def show_mesh(self, mesh, unit: str) -> None:
        self.table.setRowCount(0)
        if mesh is None:
            if self.canvas:
                self.fig.clear()
                self.canvas.draw_idle()
            return
        st = mesh.stats()
        rows = [("Element type", st["element_type"]), ("Nodes", st["nodes"]),
                ("Elements", f"{st['elements']}" + (f"  (target {st['target_elements']}, "
                                                     f"{100 * (st['elements'] - st['target_elements']) / st['target_elements']:+.1f} %)"
                                                     if st.get("target_elements") else "")),
                ("Min / max edge length", f"{st.get('edge_min_min', 0):.4g} / {st.get('edge_max_max', 0):.4g} {unit}"),
                ("Gamma min / mean", f"{st['gamma_min']:.3f} / {st['gamma_mean']:.3f}" if "gamma_min" in st
                 else "n/a (hexahedra: see SICN)"),
                ("Min scaled Jacobian", f"{st.get('sj_min', 0):.3f}"),
                ("SICN min", f"{st.get('sicn_min', 0):.3f}"),
                ("Aspect ratio max / mean", f"{st.get('aspect_ratio_max', 0):.3g} / {st.get('aspect_ratio_mean', 0):.3g}"),
                ("Invalid (Jacobian <= 0)", st.get("invalid_elements", 0)),
                ("Degenerate (gamma < 1e-3)", st.get("degenerate_elements", 0)),
                ("Poor (gamma < 0.1)", st.get("poor_elements_gamma_lt_0.1", 0)),
                ("Mesh volume", f"{st.get('total_volume', 0):.6g} {unit}^3"),
                ("CAD faces mapped", f"{st['mapped_faces']}" + (f" (unmapped: {st['unmapped_faces']})" if st['unmapped_faces'] else ""))]
        self.table.setRowCount(len(rows))
        for i, (k, v) in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(k))
            it = QTableWidgetItem(str(v))
            if (k.startswith("Invalid") or k.startswith("Degenerate")) and str(v) != "0":
                it.setForeground(QColor(theme.ERR))
            self.table.setItem(i, 1, it)
        self.table.resizeColumnToContents(0)
        if self.canvas:
            self.fig.clear()
            qk = ("gamma", "Gamma quality") if "gamma" in mesh.quality else ("sicn", "SICN quality")
            for k, (key, title) in enumerate((qk, ("aspect_ratio", "Aspect ratio"))):
                if key not in mesh.quality:
                    continue
                ax = self.fig.add_subplot(1, 2, k + 1)
                data = np.asarray(mesh.quality[key])
                data = data[np.isfinite(data)]
                if key == "aspect_ratio":
                    data = np.clip(data, 1, 10)
                ax.hist(data, bins=30, color=theme.ACCENT, edgecolor=theme.BG)
                ax.set_title(title, color=theme.FG, fontsize=9)
                ax.set_facecolor(theme.BG)
                ax.tick_params(colors=theme.FG_DIM, labelsize=7)
                for s in ax.spines.values():
                    s.set_color(theme.BORDER)
            self.fig.tight_layout()
            self.canvas.draw_idle()


class IssuesPanel(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["Severity", "Source", "Message"])
        self.setRootIsDecorated(False)
        self.setColumnWidth(0, 80)
        self.setColumnWidth(1, 140)

    def show_issues(self, issues) -> None:
        self.clear()
        for i in issues:
            it = QTreeWidgetItem([i.severity.upper(), i.source, i.message])
            col = {"error": theme.ERR, "warning": theme.WARN}.get(i.severity, theme.FG)
            it.setForeground(0, QColor(col))
            it.setToolTip(2, i.message)
            self.addTopLevelItem(it)
        if not issues:
            self.addTopLevelItem(QTreeWidgetItem(["OK", "", "No problems detected."]))


class SummaryPanel(QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenExternalLinks(True)
        self.document().setDefaultStyleSheet(
            f"table{{border-collapse:collapse}} td,th{{border:1px solid {theme.BORDER};padding:3px 8px}}"
            f"th{{background:{theme.BG_3}}} .ok{{color:{theme.OK}}} .err{{color:{theme.ERR}}} .warn{{color:{theme.WARN}}}")


# --------------------------------------------------------------------------- workflow navigator
STATE_STYLE = {
    "not_started": (theme.FG_DIM, "○", "not started"),
    "in_progress": (theme.ACCENT, "◐", "in progress"),
    "ready": ("#8fd3ff", "●", "ready"),
    "completed": (theme.OK, "✓", "completed"),
    "invalid": (theme.WARN, "!", "invalid"),
    "failed": (theme.ERR, "✕", "failed"),
}


class WorkflowBar(QWidget):
    clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(6, 3, 6, 3)
        self.lay.setSpacing(2)
        self.buttons: dict[str, QToolButton] = {}

    def update_status(self, status: list[tuple[str, str, str]]) -> None:
        if not self.buttons:
            for i, (stage, _, _) in enumerate(status):
                if i:
                    arrow = QLabel("›")
                    arrow.setStyleSheet(f"color:{theme.FG_DIM}; font-size:16px;")
                    self.lay.addWidget(arrow)
                b = QToolButton()
                b.setToolButtonStyle(Qt.ToolButtonTextOnly)
                b.clicked.connect(lambda _=False, s=stage: self.clicked.emit(s))
                self.lay.addWidget(b)
                self.buttons[stage] = b
            self.lay.addStretch(1)
        for stage, state, msg in status:
            col, sym, word = STATE_STYLE.get(state, STATE_STYLE["not_started"])
            b = self.buttons[stage]
            b.setText(f"{sym} {stage}")
            b.setToolTip(f"{stage}: {word}\n{msg}")
            b.setStyleSheet(f"QToolButton {{ color:{col}; font-weight:600; padding:3px 8px; }}")


# --------------------------------------------------------------------------- results controls
class ResultsPanel(QWidget):
    changed = Signal()
    animate = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QGridLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        r = 0
        lay.addWidget(QLabel("Quantity"), r, 0)
        self.quantity = QComboBox()
        lay.addWidget(self.quantity, r, 1, 1, 2)
        r += 1
        self.frame_label = QLabel("Mode / time")
        lay.addWidget(self.frame_label, r, 0)
        self.frame = QComboBox()
        self.frame.setToolTip("Mode shape (modal analysis) or stored time (transient analysis) to display")
        lay.addWidget(self.frame, r, 1, 1, 2)
        self.frame_label.hide()
        self.frame.hide()
        r += 1
        self.location = QLabel("")
        self.location.setObjectName("hint")
        self.location.setWordWrap(True)
        lay.addWidget(self.location, r, 0, 1, 3)
        r += 1
        lay.addWidget(QLabel("Deformation scale"), r, 0)
        self.scale_mode = QComboBox()
        self.scale_mode.addItems(["Auto", "True scale (1.0)", "Undeformed (0)", "Custom"])
        lay.addWidget(self.scale_mode, r, 1)
        self.scale = QLineEdit("1")
        self.scale.setEnabled(False)
        lay.addWidget(self.scale, r, 2)
        r += 1
        self.undeformed = QCheckBox("Show undeformed outline")
        self.undeformed.setChecked(True)
        lay.addWidget(self.undeformed, r, 0, 1, 3)
        r += 1
        self.edges = QCheckBox("Show mesh overlay")
        lay.addWidget(self.edges, r, 0, 1, 3)
        r += 1
        self.minmax = QCheckBox("Show min / max markers")
        self.minmax.setChecked(True)
        lay.addWidget(self.minmax, r, 0, 1, 3)
        r += 1
        self.manual = QCheckBox("Manual contour limits")
        lay.addWidget(self.manual, r, 0, 1, 3)
        r += 1
        lay.addWidget(QLabel("Min / max"), r, 0)
        self.cmin = QLineEdit()
        self.cmax = QLineEdit()
        self.cmin.setEnabled(False)
        self.cmax.setEnabled(False)
        lay.addWidget(self.cmin, r, 1)
        lay.addWidget(self.cmax, r, 2)
        r += 1
        lay.addWidget(QLabel("Colour map / bands"), r, 0)
        self.cmap = QComboBox()
        self.cmap.addItems(["jet", "turbo", "viridis", "coolwarm", "plasma", "rainbow"])
        lay.addWidget(self.cmap, r, 1)
        self.bands = QSpinBox()
        self.bands.setRange(2, 256)
        self.bands.setValue(12)
        lay.addWidget(self.bands, r, 2)
        r += 1
        self.section = QCheckBox("Section plane")
        self.section.setToolTip("Cut the model with a plane normal to the chosen axis; elements cut by the plane "
                                "are shown whole so probed values are true nodal values.")
        lay.addWidget(self.section, r, 0)
        self.section_axis = QComboBox()
        self.section_axis.addItems(["X", "Y", "Z", "-X", "-Y", "-Z"])
        lay.addWidget(self.section_axis, r, 1)
        self.section_pos = QSlider(Qt.Horizontal)
        self.section_pos.setRange(0, 100)
        self.section_pos.setValue(50)
        lay.addWidget(self.section_pos, r, 2)
        r += 1
        self.vectors = QCheckBox("Velocity vectors")
        self.streamlines = QCheckBox("Streamlines")
        lay.addWidget(self.vectors, r, 0)
        lay.addWidget(self.streamlines, r, 1)
        self.vectors.hide()
        self.streamlines.hide()
        r += 1
        self.anim_btn = QPushButton(icon("animate"), "Animate deformation")
        self.anim_btn.setCheckable(True)
        lay.addWidget(self.anim_btn, r, 0, 1, 3)
        r += 1
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.info.setStyleSheet(f"background:{theme.BG_3}; padding:6px; border-radius:4px;")
        lay.addWidget(self.info, r, 0, 1, 3)
        r += 1
        self.probe = QLabel("Probe: click on the model to read nodal values.")
        self.probe.setWordWrap(True)
        self.probe.setObjectName("hint")
        self.probe.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.probe, r, 0, 1, 3)
        lay.setRowStretch(r + 1, 1)

        self.quantity.currentIndexChanged.connect(self._quantity_changed)
        self.frame.currentIndexChanged.connect(self._quantity_changed)
        self.scale_mode.currentIndexChanged.connect(self._scale_mode)
        for w in (self.undeformed, self.edges, self.minmax):
            w.toggled.connect(lambda *_: self.changed.emit())
        self.manual.toggled.connect(self._manual)
        self.scale.editingFinished.connect(lambda: self.changed.emit())
        self.cmin.editingFinished.connect(lambda: self.changed.emit())
        self.cmax.editingFinished.connect(lambda: self.changed.emit())
        self.cmap.currentIndexChanged.connect(lambda *_: self.changed.emit())
        self.bands.valueChanged.connect(lambda *_: self.changed.emit())
        self.anim_btn.toggled.connect(lambda on: self.animate.emit(bool(on)))
        self.section.toggled.connect(lambda *_: self.changed.emit())
        self.vectors.toggled.connect(lambda *_: self.changed.emit())
        self.streamlines.toggled.connect(lambda *_: self.changed.emit())
        self.section_axis.currentIndexChanged.connect(lambda *_: self.section.isChecked() and self.changed.emit())
        self.section_pos.valueChanged.connect(lambda *_: self.section.isChecked() and self.changed.emit())

    def set_quantities(self, items: list[tuple[str, str]], current: str | None = None) -> None:
        self.quantity.blockSignals(True)
        self.quantity.clear()
        for key, label in items:
            self.quantity.addItem(label, key)
        if current:
            i = self.quantity.findData(current)
            if i >= 0:
                self.quantity.setCurrentIndex(i)
        self.quantity.blockSignals(False)

    def set_frames(self, labels: list[str], current: int = 0) -> None:
        """Frame selector (modes, time steps); hidden for single-frame results."""
        self.frame.blockSignals(True)
        self.frame.clear()
        self.frame.addItems(labels)
        if labels:
            self.frame.setCurrentIndex(min(max(current, 0), len(labels) - 1))
        self.frame.blockSignals(False)
        self.frame.setVisible(bool(labels))
        self.frame_label.setVisible(bool(labels))

    def section_plane(self, bbox_min, bbox_max):
        """(origin, normal) of the section plane, or None."""
        if not self.section.isChecked():
            return None
        ax = self.section_axis.currentText()
        k = "XYZ".index(ax[-1])
        n = [0.0, 0.0, 0.0]
        n[k] = -1.0 if ax.startswith("-") else 1.0
        o = [(a + b) / 2 for a, b in zip(bbox_min, bbox_max)]
        o[k] = bbox_min[k] + (bbox_max[k] - bbox_min[k]) * self.section_pos.value() / 100.0
        return o, n

    def frame_index(self) -> int | None:
        return self.frame.currentIndex() if self.frame.count() else None

    def set_flow_controls(self, on: bool) -> None:
        self.vectors.setVisible(on)
        self.streamlines.setVisible(on)

    def set_deformation_available(self, on: bool, label: str = "Animate deformation") -> None:
        for w in (self.scale_mode, self.undeformed, self.anim_btn):
            w.setEnabled(on)
        self.scale.setEnabled(on and self.scale_mode.currentIndex() == 3)
        self.anim_btn.setText(label)

    def _quantity_changed(self) -> None:
        self.manual.setChecked(False)
        self.changed.emit()

    def _scale_mode(self) -> None:
        self.scale.setEnabled(self.scale_mode.currentIndex() == 3)
        self.changed.emit()

    def _manual(self, on: bool) -> None:
        self.cmin.setEnabled(on)
        self.cmax.setEnabled(on)
        self.changed.emit()

    def current_key(self) -> str | None:
        return self.quantity.currentData()

    def scale_value(self, auto: float) -> float:
        i = self.scale_mode.currentIndex()
        if i == 0:
            return auto
        if i == 1:
            return 1.0
        if i == 2:
            return 0.0
        try:
            return float(self.scale.text())
        except ValueError:
            return auto

    def clim(self) -> tuple[float, float] | None:
        if not self.manual.isChecked():
            return None
        try:
            a, b = float(self.cmin.text()), float(self.cmax.text())
            return (a, b) if b > a else None
        except ValueError:
            return None
