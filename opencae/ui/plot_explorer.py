"""Plot Explorer: Matplotlib charts of solver results.

* Line plot: a result quantity sampled along a straight line through the solid,
  optionally overlaid for several solved studies (study comparison).
* History: a node's value over the stored time frames (transient analyses).
* Spectrum: natural frequencies / buckling load factors (with effective-mass bars).
* Convergence: results of a mesh-convergence run.

Every curve is computed from the stored results; the data behind the chart can be
exported as CSV.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from opencae.postprocessing.sampling import sample_line
from opencae.postprocessing.summary import unit_for


def _vec(text: str) -> np.ndarray:
    v = np.array([float(x) for x in text.replace(";", ",").split(",")])
    if v.shape != (3,):
        raise ValueError("enter three comma-separated coordinates")
    return v


class PlotExplorer(QDialog):
    def __init__(self, session, parent=None, probe_node: Optional[int] = None, convergence: Optional[dict] = None):
        super().__init__(parent)
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
        from matplotlib.figure import Figure
        self.setWindowTitle("Plot Explorer")
        self.resize(1000, 640)
        self.session = session
        self.convergence = convergence
        self.data: list[tuple[str, np.ndarray, np.ndarray]] = []  # (label, x, y) of the current chart
        self.xlabel = self.ylabel = ""
        lay = QHBoxLayout(self)
        left = QWidget()
        form = QFormLayout(left)
        self.kind = QComboBox()
        rs = session.results
        kinds = [("line", "Along a line")]
        if rs is not None and len(rs.frames()) > 1 and getattr(rs, "kind", "") == "thermal":
            kinds.append(("history", "History at a node"))
        if rs is not None and getattr(rs, "kind", "") in ("modal", "buckling"):
            kinds.append(("spectrum", "Frequencies / load factors"))
        if convergence:
            kinds.append(("convergence", "Mesh convergence"))
        for k, lab in kinds:
            self.kind.addItem(lab, k)
        form.addRow("Plot", self.kind)
        self.quantity = QComboBox()
        if rs is not None:
            for k in rs.available():
                self.quantity.addItem(rs.info(k).label, k)
        form.addRow("Quantity", self.quantity)
        lo, hi = (session.mesh.nodes.min(axis=0), session.mesh.nodes.max(axis=0)) if session.mesh is not None \
            else (np.zeros(3), np.ones(3))
        ax = int(np.argmax(hi - lo))
        c = (lo + hi) / 2
        a, b = c.copy(), c.copy()
        a[ax], b[ax] = lo[ax], hi[ax]
        self.p0 = QLineEdit(", ".join(f"{v:.6g}" for v in a))
        self.p1 = QLineEdit(", ".join(f"{v:.6g}" for v in b))
        form.addRow("Line start", self.p0)
        form.addRow("Line end", self.p1)
        self.npts = QSpinBox()
        self.npts.setRange(2, 5000)
        self.npts.setValue(201)
        form.addRow("Samples", self.npts)
        self.node = QSpinBox()
        self.node.setRange(1, max(1, session.mesh.n_nodes if session.mesh is not None else 1))
        if probe_node is not None:
            self.node.setValue(probe_node + 1)
        form.addRow("Node (history)", self.node)
        self.studies = QListWidget()
        self.studies.setToolTip("Overlay the same quantity from other solved studies (study comparison). "
                                "Each study is sampled on its own mesh.")
        for st in session.project.studies:
            if st.results.completed:
                it = QListWidgetItem(st.name)
                it.setData(Qt.UserRole, st.id)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if st.id == session.project.active_study else Qt.Unchecked)
                self.studies.addItem(it)
        form.addRow("Studies", self.studies)
        btn = QPushButton("Plot")
        btn.clicked.connect(self.replot)
        form.addRow(btn)
        row = QHBoxLayout()
        for text, fn in (("Save PNG…", self.save_png), ("Export CSV…", self.save_csv)):
            b2 = QPushButton(text)
            b2.clicked.connect(fn)
            row.addWidget(b2)
        form.addRow(row)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        self.msg.setObjectName("hint")
        form.addRow(self.msg)
        lay.addWidget(left, 0)
        right = QVBoxLayout()
        self.fig = Figure(figsize=(7, 5), tight_layout=True)
        self.canvas = FigureCanvasQTAgg(self.fig)
        right.addWidget(NavigationToolbar2QT(self.canvas, self))
        right.addWidget(self.canvas, 1)
        lay.addLayout(right, 1)
        self.kind.currentIndexChanged.connect(self.replot)
        self.replot()

    # ------------------------------------------------------------------ plotting
    def replot(self) -> None:
        self.fig.clear()
        axp = self.fig.add_subplot(111)
        self.data = []
        self.msg.setText("")
        try:
            getattr(self, f"_plot_{self.kind.currentData()}")(axp)
        except Exception as exc:  # show the reason instead of an empty chart
            self.msg.setText(f"Cannot plot: {exc}")
        axp.grid(True, alpha=0.3)
        self.canvas.draw_idle()

    def _plot_line(self, axp) -> None:
        s = self.session
        key = self.quantity.currentData()
        p0, p1 = _vec(self.p0.text()), _vec(self.p1.text())
        ids = [self.studies.item(i).data(Qt.UserRole) for i in range(self.studies.count())
               if self.studies.item(i).checkState() == Qt.Checked] or [s.project.active_study]
        unit = ""
        notes = []
        for sid in ids:
            st = s.project.study_by_id(sid)
            mesh, rs = s.load_study_results(sid)
            if rs is None or mesh is None:
                notes.append(f"{st.name}: no results loaded")
                continue
            if key not in rs.available():
                notes.append(f"{st.name}: has no '{key}'")
                continue
            d, v, inside = sample_line(mesh, rs.quantity(key), p0, p1, self.npts.value())
            axp.plot(d, v, label=st.name + ("" if s.project.results_are_current(st) else " (out of date)"))
            self.data.append((st.name, d, v))
            unit = unit_for(rs.info(key).unit_kind, s.units)
            if not inside.all():
                notes.append(f"{st.name}: {int((~inside).sum())} sample(s) outside the solid (gaps in the curve)")
            label = rs.info(key).label
        self.xlabel = f"Distance along line [{s.units.length}]"
        self.ylabel = f"{label} [{unit}]" if self.data else key
        axp.set_xlabel(self.xlabel)
        axp.set_ylabel(self.ylabel)
        axp.set_title(f"{self.ylabel} from ({self.p0.text()}) to ({self.p1.text()})", fontsize=10)
        if len(self.data) > 1:
            axp.legend()
        self.msg.setText("; ".join(notes) + (" Values interpolated linearly from element corner nodes." if self.data
                                              else ""))

    def _plot_history(self, axp) -> None:
        rs = self.session.results
        n = self.node.value() - 1
        t, T = rs.history(n)
        axp.plot(t, T, marker="o", ms=3)
        self.data = [(f"node {n + 1}", t, T)]
        self.xlabel, self.ylabel = "Time [s]", "Temperature [°C]"
        axp.set_xlabel(self.xlabel)
        axp.set_ylabel(self.ylabel)
        axp.set_title(f"Node {n + 1} at {np.round(self.session.mesh.nodes[n], 4).tolist()}", fontsize=10)

    def _plot_spectrum(self, axp) -> None:
        rs = self.session.results
        f = rs.frequencies
        i = np.arange(1, len(f) + 1)
        axp.bar(i, f, color="#2b6cb0")
        buck = getattr(rs, "kind", "") == "buckling"
        self.xlabel, self.ylabel = "Mode", "Load factor [-]" if buck else "Frequency [Hz]"
        axp.set_xlabel(self.xlabel)
        axp.set_ylabel(self.ylabel)
        axp.set_xticks(i)
        self.data = [("value", i.astype(float), f)]
        fr = rs.mass_fractions() if not buck else None
        if fr is not None:
            ax2 = axp.twinx()
            for k, (lab, col) in enumerate((("X", "#c53030"), ("Y", "#2f855a"), ("Z", "#d69e2e"))):
                ax2.plot(i, 100 * np.cumsum(np.nan_to_num(fr[:, k])), marker="o", color=col,
                         label=f"cumulative mass {lab}")
                self.data.append((f"cumulative mass fraction {lab} [%]", i.astype(float),
                                  100 * np.cumsum(np.nan_to_num(fr[:, k]))))
            ax2.set_ylabel("Cumulative effective mass [%]")
            ax2.set_ylim(0, 105)
            ax2.legend(loc="lower right")

    def _plot_convergence(self, axp) -> None:
        c = self.convergence
        n = np.asarray(c["dofs"], float)
        for name, vals in c["quantities"].items():
            axp.plot(n, vals, marker="o", label=name)
            self.data.append((name, n, np.asarray(vals, float)))
        axp.set_xscale("log")
        self.xlabel, self.ylabel = "Degrees of freedom", c.get("ylabel", "value")
        axp.set_xlabel(self.xlabel)
        axp.set_ylabel(self.ylabel)
        axp.legend()

    # ------------------------------------------------------------------ export
    def save_png(self, path: Optional[str] = None) -> Optional[Path]:
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "Save chart", "plot.png", "PNG (*.png)")
        if path:
            self.fig.savefig(path, dpi=150)
            return Path(path)
        return None

    def save_csv(self, path: Optional[str] = None) -> Optional[Path]:
        if not self.data:
            return None
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "Export chart data", "plot.csv", "CSV (*.csv)")
        if not path:
            return None
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:  # Excel-friendly UTF-8
            w = csv.writer(fh)
            w.writerow(["series", self.xlabel, self.ylabel])
            for label, x, y in self.data:
                for a, b in zip(x, y):
                    w.writerow([label, f"{a:.10g}", "" if np.isnan(b) else f"{b:.10g}"])
        return Path(path)
