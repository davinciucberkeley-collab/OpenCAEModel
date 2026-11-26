"""Dialogs of Assembly mode: joint (with live preview), motor (driver), study settings, manual mass values,
grouping, and the structural load case.

All dialogs are non-modal: the 3-D view stays usable (preview, picking an edge or a face) while one is open. Each
takes the workspace, validates while the user types, shows the reason in red next to a disabled OK button, and only
touches the model in ``accept``.
"""
from __future__ import annotations

import copy
import math
from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QRadioButton,
                               QStackedWidget, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from opencae.assembly import (GROUND, Assembly, Driver, Joint, MassProps, Profile, get_mbd)
from opencae.ui import theme

PRESCRIBED_TEXT = "Prescribed motion — the joint follows this exactly"
DYNAMIC_TEXT = "Dynamic — the motion is computed"
AXIS_PRESETS = {"+X": (1, 0, 0), "−X": (-1, 0, 0), "+Y": (0, 1, 0), "−Y": (0, -1, 0),
                "+Z": (0, 0, 1), "−Z": (0, 0, -1), "-X": (-1, 0, 0), "-Y": (0, -1, 0), "-Z": (0, 0, -1)}
RPM = 2 * math.pi / 60.0


def dspin(lo=-1e9, hi=1e9, decimals=4, step=1.0, suffix="", value=0.0) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    if suffix:
        s.setSuffix(" " + suffix)
    s.setValue(value)
    s.setKeyboardTracking(False)
    return s


def error_label() -> QLabel:
    lab = QLabel("")
    lab.setObjectName("error")
    lab.setWordWrap(True)
    return lab


def hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("hint")
    lab.setWordWrap(True)
    return lab


class _Dialog(QDialog):
    """Common frame: non-modal, error label, OK / Cancel buttons (``ok_button`` is enabled only when valid)."""

    def __init__(self, ws, title: str, ok_text: str = "OK") -> None:
        super().__init__(ws)
        self.ws = ws
        self.setWindowTitle(title)
        self.setModal(False)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.root = QVBoxLayout(self)
        self.error_label = error_label()
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = self.buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setText(ok_text)
        self.ok_button.setObjectName("primary")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

    def finish_layout(self) -> None:
        self.root.addWidget(self.error_label)
        self.root.addWidget(self.buttons)

    def set_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.ok_button.setEnabled(not text)


# ======================================================================================== geometry helpers
def circle_from_polyline(pts) -> Optional[tuple[np.ndarray, np.ndarray, float]]:
    """(centre, unit normal, radius) of a (nearly) circular polyline, else None. Plane by SVD, circle by Kasa
    least squares in the plane; the residual must be < 0.5 % of the radius and the arc must cover > 45 deg."""
    P = np.asarray(pts, float)
    if P.ndim != 2 or len(P) < 5:
        return None
    c0 = P.mean(axis=0)
    _, s, vt = np.linalg.svd(P - c0)
    normal = vt[2]
    if s[1] < 1e-9 or s[2] > 5e-3 * s[1]:
        return None  # a line, or not planar
    u, v = vt[0], vt[1]
    xy = np.column_stack([(P - c0) @ u, (P - c0) @ v])
    A = np.column_stack([2 * xy[:, 0], 2 * xy[:, 1], np.ones(len(xy))])
    b = (xy ** 2).sum(axis=1)
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    cx, cy = sol[0], sol[1]
    r2 = sol[2] + cx * cx + cy * cy
    if r2 <= 0:
        return None
    r = math.sqrt(r2)
    resid = np.abs(np.hypot(xy[:, 0] - cx, xy[:, 1] - cy) - r).max()
    if resid > 5e-3 * r:
        return None
    ang = np.unwrap(np.arctan2(xy[:, 1] - cy, xy[:, 0] - cx))
    if abs(ang[-1] - ang[0]) < math.radians(45) and abs(ang.max() - ang.min()) < math.radians(45):
        return None
    centre = c0 + cx * u + cy * v
    k = int(np.argmax(np.abs(normal)))
    if normal[k] < 0:
        normal = -normal
    return centre, normal / np.linalg.norm(normal), float(r)


# ======================================================================================== joint dialog
class JointDialog(_Dialog):
    """Add or edit a joint. The 3-D view shows the axis (and the turning direction) live; Reverse flips it."""

    def __init__(self, ws, joint: Optional[Joint] = None, parent_id: Optional[str] = None,
                 child_id: Optional[str] = None) -> None:
        super().__init__(ws, "Edit joint" if joint is not None else "Add joint",
                         "Apply" if joint is not None else "Add joint")
        self.asm: Assembly = ws.assembly
        self.editing = joint
        self.ul = ws.length_unit
        self._reversed = bool(joint.reversed) if joint is not None else False
        self._loading = True
        self._edge_mode = False
        root = self.root
        root.addWidget(hint("A joint says how a body (the child) may move relative to another body (the parent) "
                            "or to the ground."))

        form = QFormLayout()
        self.name = QLineEdit(joint.name if joint else "")
        form.addRow("Name", self.name)
        self.kind = QComboBox()
        for k, txt in (("revolute", "Revolute — turns about an axis (a hinge)"),
                       ("prismatic", "Prismatic — slides along an axis"),
                       ("fixed", "Fixed — welded, no relative motion")):
            self.kind.addItem(txt, k)
        self.kind.setCurrentIndex(self.kind.findData(joint.kind if joint else "revolute"))
        form.addRow("Type", self.kind)
        self.parent_combo = QComboBox()
        self.parent_combo.addItem("Ground (the world)", GROUND)
        self.child_combo = QComboBox()
        for b in self.asm.bodies:
            self.parent_combo.addItem(b.name, b.id)
            self.child_combo.addItem(b.name, b.id)
        form.addRow("Parent body", self.parent_combo)
        form.addRow("Child body (moves)", self.child_combo)
        root.addLayout(form)

        og = QGroupBox(f"Where is the joint? (origin, {self.ul})")
        gl = QHBoxLayout(og)
        self.origin = [dspin(decimals=4, step=1.0, suffix=self.ul) for _ in range(3)]
        for ax, sp in zip("XYZ", self.origin):
            gl.addWidget(QLabel(ax))
            gl.addWidget(sp)
        root.addWidget(og)

        self.axis_group = QGroupBox("Which way does it turn or slide? (axis)")
        al = QVBoxLayout(self.axis_group)
        presets = QHBoxLayout()
        self.preset_buttons = {}
        for label in ("+X", "−X", "+Y", "−Y", "+Z", "−Z"):
            b = QToolButton()
            b.setText(label)
            b.setFixedWidth(40)
            b.setStyleSheet(f"QToolButton {{ border:1px solid {theme.BORDER}; background:{theme.BG_3}; padding:3px; }}"
                            f"QToolButton:hover {{ border-color:{theme.ACCENT}; }}")
            b.setToolTip(f"Axis along {label}")
            b.clicked.connect(lambda _=False, lab=label: self.set_axis_preset(lab))
            presets.addWidget(b)
            self.preset_buttons[label] = b
        presets.addStretch(1)
        al.addLayout(presets)
        custom = QHBoxLayout()
        self.axis = [dspin(-1e6, 1e6, 4, 0.1) for _ in range(3)]
        for ax, sp in zip("XYZ", self.axis):
            custom.addWidget(QLabel(ax))
            custom.addWidget(sp)
        al.addLayout(custom)
        row = QHBoxLayout()
        self.reverse_btn = QPushButton("Reverse axis")
        self.reverse_btn.setToolTip("Flip the direction of positive motion")
        self.reverse_btn.clicked.connect(self.reverse)
        row.addWidget(self.reverse_btn)
        self.edge_btn = QPushButton("Use a circular edge…")
        self.edge_btn.setToolTip("Click a circular edge (a hole or a cylinder rim) in the 3-D view: its centre and "
                                 "axis fill in the fields above")
        self.edge_btn.setCheckable(True)
        self.edge_btn.toggled.connect(self._toggle_edge_mode)
        row.addWidget(self.edge_btn)
        row.addStretch(1)
        al.addLayout(row)
        self.axis_text = QLabel("")
        self.axis_text.setObjectName("hint")
        self.axis_text.setWordWrap(True)
        al.addWidget(self.axis_text)
        root.addWidget(self.axis_group)

        self.limit_check = QCheckBox("Limit the travel")
        self.limit_lo = dspin(-1e6, 1e6, 3, 1.0)
        self.limit_hi = dspin(-1e6, 1e6, 3, 1.0)
        lim = QHBoxLayout()
        lim.addWidget(self.limit_check)
        lim.addWidget(QLabel("from"))
        lim.addWidget(self.limit_lo)
        lim.addWidget(QLabel("to"))
        lim.addWidget(self.limit_hi)
        self.limit_box = QWidget()
        self.limit_box.setLayout(lim)
        lim.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.limit_box)
        self.finish_layout()

        # ---- initial values
        if joint is not None:
            self.parent_combo.setCurrentIndex(max(0, self.parent_combo.findData(joint.parent)))
            self.child_combo.setCurrentIndex(max(0, self.child_combo.findData(joint.child)))
            for sp, v in zip(self.origin, joint.origin):
                sp.setValue(float(v))
            for sp, v in zip(self.axis, joint.axis):
                sp.setValue(float(v))
            if joint.limits is not None:
                self.limit_check.setChecked(True)
                self._set_limits_display(joint.limits, joint.kind)
        else:
            if parent_id is not None:
                self.parent_combo.setCurrentIndex(max(0, self.parent_combo.findData(parent_id)))
            if child_id is not None:
                self.child_combo.setCurrentIndex(max(0, self.child_combo.findData(child_id)))
            elif self.asm.bodies:
                free = [b for b in self.asm.bodies if not b.grounded]
                self.child_combo.setCurrentIndex(self.child_combo.findData((free or self.asm.bodies)[0].id))
            self.axis[2].setValue(1.0)
            c = ws.body_com(self.child_combo.currentData())
            if c is not None:
                for sp, v in zip(self.origin, c):
                    sp.setValue(float(v))
        self._loading = False
        for w in (self.name,):
            w.textEdited.connect(self._on_name)
        self._auto_name = joint is None
        for sp in self.origin + self.axis + [self.limit_lo, self.limit_hi]:
            sp.valueChanged.connect(self._changed)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.parent_combo.currentIndexChanged.connect(self._changed)
        self.child_combo.currentIndexChanged.connect(self._changed)
        self.limit_check.toggled.connect(self._changed)
        self._kind_changed()

    # ---- state ---------------------------------------------------------
    def _on_name(self, _t: str) -> None:
        self._auto_name = False

    def kind_value(self) -> str:
        return self.kind.currentData()

    def _limit_unit(self) -> str:
        return "deg" if self.kind_value() == "revolute" else self.ul

    def _set_limits_display(self, lim, kind: str) -> None:
        f = math.degrees if kind == "revolute" else (lambda v: v)
        self.limit_lo.setValue(f(lim[0]))
        self.limit_hi.setValue(f(lim[1]))

    def _kind_changed(self) -> None:
        k = self.kind_value()
        self.axis_group.setVisible(k != "fixed")
        self.limit_box.setVisible(k != "fixed")
        for sp in (self.limit_lo, self.limit_hi):
            sp.setSuffix(" " + self._limit_unit())
        self.edge_btn.setEnabled(k == "revolute")
        self._changed()

    def set_axis_preset(self, label: str) -> None:
        v = AXIS_PRESETS[label]
        self._reversed = False
        for sp, x in zip(self.axis, v):
            sp.blockSignals(True)
            sp.setValue(float(x))
            sp.blockSignals(False)
        self._changed()

    def reverse(self) -> None:
        self._reversed = not self._reversed
        self._changed()

    def set_bodies(self, parent_id: str, child_id: str) -> None:
        self.parent_combo.setCurrentIndex(self.parent_combo.findData(parent_id))
        self.child_combo.setCurrentIndex(self.child_combo.findData(child_id))
        self._changed()

    def effective_axis(self) -> np.ndarray:
        a = np.array([sp.value() for sp in self.axis], float)
        n = np.linalg.norm(a)
        if n < 1e-12:
            return np.zeros(3)
        a = a / n
        return -a if self._reversed else a

    def joint(self) -> Joint:
        k = self.kind_value()
        j = Joint(name=self.name.text().strip() or "Joint", kind=k, parent=self.parent_combo.currentData(),
                  child=self.child_combo.currentData(), origin=[sp.value() for sp in self.origin],
                  axis=[sp.value() for sp in self.axis] if k != "fixed" else [0.0, 0.0, 1.0],
                  reversed=self._reversed and k != "fixed")
        if self.limit_check.isChecked() and k != "fixed":
            f = math.radians if k == "revolute" else (lambda v: v)
            j.limits = [f(self.limit_lo.value()), f(self.limit_hi.value())]
        if self.editing is not None:
            j.id = self.editing.id
            j.initial_q, j.initial_qd, j.damping = self.editing.initial_q, self.editing.initial_qd, \
                self.editing.damping
            if (self.editing.kind == k and np.allclose(self.editing.axis, j.axis)
                    and self.editing.reversed == j.reversed and self.editing.x_axis is not None):
                j.x_axis = list(self.editing.x_axis)
        return j

    def _relevant(self, problems: list[str], j: Joint) -> list[str]:
        names = {f"'{j.name}'"}
        for bid in (j.parent, j.child):
            b = self.asm.body(bid)
            if b is not None:
                names.add(f"'{b.name}'")
        return [p for p in problems if any(n in p for n in names)]

    def _changed(self, *_a) -> None:
        if self._loading:
            return
        k = self.kind_value()
        if self._auto_name:
            cb = self.child_combo.currentText()
            self.name.setText(f"{cb} {'hinge' if k == 'revolute' else 'slider' if k == 'prismatic' else 'mount'}")
        j = self.joint()
        # validation in the model's own words, on a copy of the assembly
        trial = copy.deepcopy(self.asm)
        if self.editing is not None:
            trial.joints = [x for x in trial.joints if x.id != self.editing.id]
        trial.joints.append(copy.deepcopy(j))
        msgs = self._relevant(trial.validate(), j)
        if not self.asm.bodies:
            msgs = ["The assembly has no bodies yet."]
        if k != "fixed" and np.linalg.norm(np.array([sp.value() for sp in self.axis])) < 1e-12:
            msgs.append("The axis is the zero vector: choose +X, +Y, +Z, … or type a direction.")
        if self.limit_check.isChecked() and k != "fixed" and not self.limit_lo.value() < self.limit_hi.value():
            msgs.append("Travel limits: the lower value must be smaller than the upper value.")
        self.set_error("\n".join(dict.fromkeys(msgs)))
        if k != "fixed":
            z = self.effective_axis()
            self.axis_text.setText(f"Positive motion: {'right-hand rotation about' if k == 'revolute' else 'sliding along'}"
                                   f" the arrow ({z[0]:+.3f}, {z[1]:+.3f}, {z[2]:+.3f})"
                                   + ("  — axis reversed" if self._reversed else ""))
        else:
            self.axis_text.setText("")
        self.ws.preview_joint(j)

    # ---- circular edge --------------------------------------------------
    def _toggle_edge_mode(self, on: bool) -> None:
        self._edge_mode = on
        if on:
            self.axis_text.setText("Click a circular edge in the 3-D view…")
            self.ws.begin_pick("edge", self.apply_edge_pick)
        else:
            self.ws.end_pick()
            self._changed()

    def apply_edge_pick(self, pick) -> bool:
        circ = self.ws.circle_of_edge(pick)
        if circ is None:
            self.axis_text.setText("That edge is not circular: pick the rim of a hole or a cylinder, or type the "
                                   "origin and the axis.")
            return False
        centre, normal, _r = circ
        self._loading = True
        for sp, v in zip(self.origin, centre):
            sp.setValue(float(v))
        for sp, v in zip(self.axis, normal):
            sp.setValue(float(v))
        self._reversed = False
        self._loading = False
        self.edge_btn.blockSignals(True)
        self.edge_btn.setChecked(False)
        self.edge_btn.blockSignals(False)
        self.ws.end_pick()
        self._changed()
        return True

    # ---- end -----------------------------------------------------------
    def accept(self) -> None:
        if not self.ok_button.isEnabled():
            return
        self.ws.end_pick()
        self.ws.preview_joint(None)
        self.ws.commit_joint(self.joint(), self.editing)
        super().accept()

    def reject(self) -> None:
        self.ws.end_pick()
        self.ws.preview_joint(None)
        super().reject()

    def done(self, r: int) -> None:  # window closed by the title-bar button too
        self.ws.preview_joint(None)
        super().done(r)


# ======================================================================================== profile preview
class ProfilePreview(QWidget):
    """Small plot of a command profile (value in display units against time)."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(260, 110)
        self.points: list[tuple[float, float]] = []
        self.unit = ""
        self.t_end = 1.0

    def set_points(self, pts, unit: str, t_end: float) -> None:
        self.points = [(float(a), float(b)) for a, b in pts]
        self.unit, self.t_end = unit, max(float(t_end), 1e-9)
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#171a20"))
        m = 6
        left, bottom = 46, 18
        plot = QRectF(left, m, w - left - m, h - m - bottom)
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawRect(plot)
        if len(self.points) < 1:
            return
        ts = [a for a, _ in self.points]
        vs = [b for _, b in self.points]
        t1 = max(self.t_end, max(ts))
        lo, hi = min(vs + [0.0]), max(vs + [0.0])
        if hi - lo < 1e-12:
            hi, lo = hi + 1, lo - 1
        pad = 0.08 * (hi - lo)
        lo, hi = lo - pad, hi + pad

        def X(t):
            return plot.left() + plot.width() * t / t1

        def Y(v):
            return plot.bottom() - plot.height() * (v - lo) / (hi - lo)
        if lo < 0 < hi:
            p.setPen(QPen(QColor("#4a5260"), 1, Qt.DashLine))
            p.drawLine(QPointF(plot.left(), Y(0)), QPointF(plot.right(), Y(0)))
        p.setPen(QPen(QColor(theme.ACCENT), 2))
        for a, b in zip(self.points[:-1], self.points[1:]):
            p.drawLine(QPointF(X(a[0]), Y(a[1])), QPointF(X(b[0]), Y(b[1])))
        p.setPen(QColor(theme.FG_DIM))
        f = QFont(p.font())
        f.setPointSizeF(7.5)
        p.setFont(f)
        p.drawText(QRectF(0, plot.top() - 4, left - 4, 14), Qt.AlignRight | Qt.AlignVCenter, f"{hi:.3g}")
        p.drawText(QRectF(0, plot.bottom() - 10, left - 4, 14), Qt.AlignRight | Qt.AlignVCenter, f"{lo:.3g}")
        p.drawText(QRectF(plot.left(), plot.bottom() + 2, plot.width(), 14), Qt.AlignLeft, "0 s")
        p.drawText(QRectF(plot.left(), plot.bottom() + 2, plot.width(), 14), Qt.AlignRight,
                   f"{t1:.3g} s   ({self.unit})")


# ======================================================================================== driver dialog
class DriverDialog(_Dialog):
    """Add or edit the motor of a joint: prescribed motion (follows exactly) or dynamic torque / force."""

    def __init__(self, ws, study, driver: Optional[Driver] = None, joint_id: Optional[str] = None) -> None:
        super().__init__(ws, "Edit motor" if driver is not None else "Add motor",
                         "Apply" if driver is not None else "Add motor")
        self.study = study
        self.asm: Assembly = ws.assembly
        self.editing = driver
        self.ul = ws.length_unit
        self._loading = True
        S = get_mbd(study)
        self.duration = S.duration_s
        root = self.root
        root.addWidget(hint("A motor commands one joint. Choose whether the joint must follow the command exactly "
                            "(prescribed) or whether the command is a torque / force and the motion is computed."))
        form = QFormLayout()
        self.name = QLineEdit(driver.name if driver else "")
        form.addRow("Name", self.name)
        self.joint_combo = QComboBox()
        for j in self.asm.joints:
            if j.kind != "fixed":
                self.joint_combo.addItem(f"{j.name} ({j.kind})", j.id)
        form.addRow("Joint", self.joint_combo)
        root.addLayout(form)

        self.prescribed_radio = QRadioButton(PRESCRIBED_TEXT)
        self.dynamic_radio = QRadioButton(DYNAMIC_TEXT)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.prescribed_radio)
        self.mode_group.addButton(self.dynamic_radio)
        mg = QGroupBox("Kind of command")
        ml = QVBoxLayout(mg)
        ml.addWidget(self.prescribed_radio)
        self.quantity = QComboBox()
        ql = QHBoxLayout()
        ql.addSpacing(22)
        ql.addWidget(QLabel("Command"))
        ql.addWidget(self.quantity, 1)
        self.speed_unit = QComboBox()
        self.speed_unit.addItems(["deg/s", "rpm"])
        ql.addWidget(self.speed_unit)
        ml.addLayout(ql)
        ml.addWidget(self.dynamic_radio)
        self.kind_hint = hint("")
        ml.addWidget(self.kind_hint)
        root.addWidget(mg)

        pg = QGroupBox("Command over time")
        pl = QVBoxLayout(pg)
        top = QHBoxLayout()
        top.addWidget(QLabel("Profile"))
        self.profile_kind = QComboBox()
        for k, t in (("constant", "Constant"), ("ramp", "Ramp (linear change, then hold)"),
                     ("table", "Table (time, value)")):
            self.profile_kind.addItem(t, k)
        top.addWidget(self.profile_kind, 1)
        self.unit_label = QLabel("")
        self.unit_label.setObjectName("hint")
        top.addWidget(self.unit_label)
        pl.addLayout(top)
        self.pages = QStackedWidget()
        # constant
        c = QWidget()
        cl = QFormLayout(c)
        self.const_value = dspin(decimals=5, step=1.0)
        cl.addRow("Value", self.const_value)
        self.pages.addWidget(c)
        # ramp
        r = QWidget()
        rl = QFormLayout(r)
        self.ramp_t0 = dspin(0, 1e6, 4, 0.1, "s")
        self.ramp_t1 = dspin(0, 1e6, 4, 0.1, "s", 1.0)
        self.ramp_v0 = dspin(decimals=5, step=1.0)
        self.ramp_v1 = dspin(decimals=5, step=1.0)
        rl.addRow("Starts at", self.ramp_t0)
        rl.addRow("Ends at", self.ramp_t1)
        rl.addRow("Value at start", self.ramp_v0)
        rl.addRow("Value at end", self.ramp_v1)
        self.pages.addWidget(r)
        # table
        t = QWidget()
        tl = QVBoxLayout(t)
        tl.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 2)
        self.table.setMaximumHeight(130)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        tl.addWidget(self.table)
        tb = QHBoxLayout()
        add = QPushButton("Add row")
        rem = QPushButton("Remove row")
        add.clicked.connect(self._add_row)
        rem.clicked.connect(self._remove_row)
        tb.addWidget(add)
        tb.addWidget(rem)
        tb.addStretch(1)
        tl.addLayout(tb)
        self.pages.addWidget(t)
        pl.addWidget(self.pages)
        self.preview = ProfilePreview()
        pl.addWidget(self.preview)
        root.addWidget(pg)
        self.finish_layout()

        # ---- initial state
        jid = driver.joint if driver else (joint_id or self._first_free_joint())
        if jid is not None:
            self.joint_combo.setCurrentIndex(max(0, self.joint_combo.findData(jid)))
        if driver is not None:
            self.prescribed_radio.setChecked(driver.prescribed)
            self.dynamic_radio.setChecked(not driver.prescribed)
        else:
            self.prescribed_radio.setChecked(True)
        self._fill_quantity(driver.mode if driver else None)
        self._auto_name = driver is None
        self._set_speed_unit_default(driver)
        self._load_profile(driver.profile if driver else None)
        self._loading = False
        self.name.textEdited.connect(lambda _t: setattr(self, "_auto_name", False))
        self.joint_combo.currentIndexChanged.connect(self._joint_changed)
        self.prescribed_radio.toggled.connect(self._mode_changed)
        self.quantity.currentIndexChanged.connect(self._quantity_changed)
        self.speed_unit.currentIndexChanged.connect(self._speed_unit_changed)
        self.profile_kind.currentIndexChanged.connect(self._profile_kind_changed)
        for sp in (self.const_value, self.ramp_t0, self.ramp_t1, self.ramp_v0, self.ramp_v1):
            sp.valueChanged.connect(self._changed)
        self.table.itemChanged.connect(self._changed)
        self._prev_unit = self.speed_unit.currentText()
        self._changed()

    # ---- helpers ------------------------------------------------------------
    def _first_free_joint(self) -> Optional[str]:
        used = {d.joint for d in get_mbd(self.study).drivers}
        fol = {c.follower for c in self.asm.couplings}
        for j in self.asm.joints:
            if j.kind != "fixed" and j.id not in used and j.id not in fol:
                return j.id
        return self.joint_combo.itemData(0) if self.joint_combo.count() else None

    def joint_value(self) -> Optional[Joint]:
        return self.asm.joint(self.joint_combo.currentData()) if self.joint_combo.count() else None

    def set_joint(self, jid: str) -> None:
        self.joint_combo.setCurrentIndex(self.joint_combo.findData(jid))

    def mode_value(self) -> str:
        if self.dynamic_radio.isChecked():
            return "effort"
        return self.quantity.currentData() or "position"

    def _fill_quantity(self, mode: Optional[str]) -> None:
        j = self.joint_value()
        prism = j is not None and j.kind == "prismatic"
        self.quantity.blockSignals(True)
        self.quantity.clear()
        if self.dynamic_radio.isChecked():
            self.quantity.addItem("Force (N)" if prism else "Torque (N·m)", "effort")
            self.quantity.setEnabled(False)
        else:
            self.quantity.addItem(f"Position ({self.ul})" if prism else "Angle (deg)", "position")
            self.quantity.addItem(f"Speed ({self.ul}/s)" if prism else "Angular speed", "velocity")
            self.quantity.setEnabled(True)
            if mode in ("position", "velocity"):
                self.quantity.setCurrentIndex(self.quantity.findData(mode))
        self.quantity.blockSignals(False)
        self.speed_unit.setVisible(not prism and self.mode_value() == "velocity")
        self.kind_hint.setText("The joint follows the command exactly; the effort needed is reported afterwards "
                               "(no motor model)." if self.prescribed_radio.isChecked() else
                               "The command is applied as a torque / force; the resulting motion depends on the "
                               "inertia, gravity and loads.")

    def _set_speed_unit_default(self, driver: Optional[Driver]) -> None:
        unit = "deg/s"
        if driver is not None and driver.mode == "velocity":
            _ts, vs = driver.profile.breakpoints()
            if len(vs) and float(np.max(np.abs(vs))) * 180 / math.pi >= 360.0:
                unit = "rpm"
        self.speed_unit.blockSignals(True)
        self.speed_unit.setCurrentText(unit)
        self.speed_unit.blockSignals(False)

    def unit_text(self) -> str:
        return self.unit_label.text()

    def _unit_and_factor(self) -> tuple[str, float]:
        """(display unit, display value -> SI/project value factor)."""
        j = self.joint_value()
        prism = j is not None and j.kind == "prismatic"
        mode = self.mode_value()
        if mode == "effort":
            return ("N" if prism else "N·m"), 1.0
        if prism:
            return (self.ul if mode == "position" else f"{self.ul}/s"), 1.0
        if mode == "position":
            return "deg", math.pi / 180.0
        return (("rpm", RPM) if self.speed_unit.currentText() == "rpm" else ("deg/s", math.pi / 180.0))

    # ---- profile in / out -----------------------------------------------------
    def _load_profile(self, p: Optional[Profile]) -> None:
        prev, self._loading = self._loading, True
        try:
            self._load_profile_inner(p)
        finally:
            self._loading = prev

    def _load_profile_inner(self, p: Optional[Profile]) -> None:
        unit, f = self._unit_and_factor()
        self.unit_label.setText(f"values in {unit}")
        if p is None:
            mode = self.mode_value()
            default_end = {"position": 20.0, "velocity": 60.0, "effort": 0.002}.get(mode, 1.0)
            j = self.joint_value()
            if j is not None and j.kind == "prismatic":
                default_end = {"position": 10.0, "velocity": 10.0, "effort": 1.0}[mode]
            p = Profile("ramp", t0=0.0, t1=self.duration, v0=0.0, v1=default_end * f)
        self.profile_kind.setCurrentIndex(self.profile_kind.findData(p.kind))
        self.pages.setCurrentIndex({"constant": 0, "ramp": 1, "table": 2}[p.kind])
        self.const_value.setValue((p.value or 0.0) / f)
        self.ramp_t0.setValue(p.t0)
        self.ramp_t1.setValue(p.t1)
        self.ramp_v0.setValue(p.v0 / f)
        self.ramp_v1.setValue(p.v1 / f)
        tab = p.table if p.table else [[0.0, 0.0], [self.duration / 2, (p.v1 or 1.0)], [self.duration, (p.v1 or 1.0)]]
        self.set_table([[t, v / f] for t, v in tab])

    def set_table(self, rows) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        unit, _f = self._unit_and_factor()
        self.table.setHorizontalHeaderLabels(["Time (s)", f"Value ({unit})"])
        for r, (t, v) in enumerate(rows):
            self.table.setItem(r, 0, QTableWidgetItem(f"{float(t):.6g}"))
            self.table.setItem(r, 1, QTableWidgetItem(f"{float(v):.6g}"))
        self.table.blockSignals(False)
        self._changed()

    def _add_row(self) -> None:
        r = self.table.rowCount()
        last_t = float(self.table.item(r - 1, 0).text()) if r else 0.0
        self.table.blockSignals(True)
        self.table.setRowCount(r + 1)
        self.table.setItem(r, 0, QTableWidgetItem(f"{last_t + max(self.duration / 4, 0.1):.6g}"))
        self.table.setItem(r, 1, QTableWidgetItem(self.table.item(r - 1, 1).text() if r else "0"))
        self.table.blockSignals(False)
        self._changed()

    def _remove_row(self) -> None:
        r = self.table.currentRow()
        if r < 0:
            r = self.table.rowCount() - 1
        if r >= 0:
            self.table.removeRow(r)
            self._changed()

    def _table_rows(self) -> list[list[float]]:
        rows = []
        for r in range(self.table.rowCount()):
            try:
                rows.append([float(self.table.item(r, 0).text()), float(self.table.item(r, 1).text())])
            except (AttributeError, ValueError):
                raise ValueError(f"table row {r + 1}: type numbers for the time and the value")
        return rows

    def profile(self) -> Profile:
        _unit, f = self._unit_and_factor()
        k = self.profile_kind.currentData()
        if k == "constant":
            return Profile("constant", value=self.const_value.value() * f)
        if k == "ramp":
            return Profile("ramp", t0=self.ramp_t0.value(), t1=self.ramp_t1.value(), v0=self.ramp_v0.value() * f,
                           v1=self.ramp_v1.value() * f)
        return Profile("table", table=[[t, v * f] for t, v in self._table_rows()])

    def driver(self) -> Driver:
        j = self.joint_value()
        d = Driver(name=self.name.text().strip() or "Motor", joint=j.id if j else "", mode=self.mode_value(),
                   profile=self.profile())
        if self.editing is not None:
            d.id = self.editing.id
        return d

    # ---- reactions to edits ---------------------------------------------------
    def _joint_changed(self) -> None:
        self._fill_quantity(self.mode_value() if self.mode_value() != "effort" else None)
        self._changed()

    def _mode_changed(self) -> None:
        if self._loading:
            return
        old_unit = self._unit_and_factor()
        self._fill_quantity(None)
        self._rescale(old_unit)
        self._changed()

    def _quantity_changed(self) -> None:
        if self._loading:
            return
        self.speed_unit.setVisible(self.joint_value() is not None and self.joint_value().kind != "prismatic"
                                   and self.mode_value() == "velocity")
        self._changed()

    def _speed_unit_changed(self) -> None:
        """Keep the physical values: convert the numbers shown from the old speed unit to the new one."""
        if self._loading:
            return
        old = self._prev_unit
        new = self.speed_unit.currentText()
        self._prev_unit = new
        if old == new:
            return
        k = (RPM / (math.pi / 180.0)) if old == "rpm" else ((math.pi / 180.0) / RPM)  # old display -> new display
        self._loading = True
        for sp in (self.const_value, self.ramp_v0, self.ramp_v1):
            sp.setValue(sp.value() * k)
        rows = []
        for r in range(self.table.rowCount()):
            try:
                rows.append([float(self.table.item(r, 0).text()), float(self.table.item(r, 1).text()) * k])
            except (AttributeError, ValueError):
                pass
        self._loading = False
        self.set_table(rows)

    def _rescale(self, _old) -> None:
        self._load_profile(None)

    def _profile_kind_changed(self) -> None:
        self.pages.setCurrentIndex({"constant": 0, "ramp": 1, "table": 2}[self.profile_kind.currentData()])
        self._changed()

    def _changed(self, *_a) -> None:
        if self._loading:
            return
        unit, _f = self._unit_and_factor()
        self.unit_label.setText(f"values in {unit}")
        self.table.setHorizontalHeaderLabels(["Time (s)", f"Value ({unit})"])
        j = self.joint_value()
        if self._auto_name and j is not None:
            self.name.setText(f"Motor ({j.name})")
        msgs: list[str] = []
        if j is None:
            msgs.append("There is no joint that can take a motor: add a revolute or prismatic joint first.")
        else:
            for d in get_mbd(self.study).drivers:
                if d.joint == j.id and (self.editing is None or d.id != self.editing.id):
                    msgs.append(f"Joint '{j.name}' already has a motor ('{d.name}'). A joint takes only one "
                                "motor: edit or remove that one.")
            for c in self.asm.couplings:
                if c.follower == j.id:
                    lead = self.asm.joint(c.leader)
                    msgs.append(f"Joint '{j.name}' is moved by the transmission '{c.name}' from "
                                f"'{lead.name if lead else c.leader}': drive that joint instead.")
        prof = None
        try:
            prof = self.profile()
            msgs += [f"Profile: {p}" for p in prof.problems()]
        except ValueError as exc:
            msgs.append(str(exc))
        self.set_error("\n".join(msgs))
        if prof is not None and not prof.problems():
            self._update_preview(prof)

    def _update_preview(self, prof: Profile) -> None:
        unit, f = self._unit_and_factor()
        t_end = max(self.duration, 1e-6)
        ts, vs = prof.breakpoints()
        grid = sorted(set([0.0, t_end] + [float(t) for t in ts if 0.0 <= t <= t_end]))
        pts = [(t, prof.value_at(t) / f) for t in grid]
        self.preview.set_points(pts, unit, t_end)

    def accept(self) -> None:
        if not self.ok_button.isEnabled():
            return
        self.ws.commit_driver(self.study, self.driver(), self.editing)
        super().accept()


# ======================================================================================== study settings
class StudySettingsDialog(_Dialog):
    """Gravity, duration, time step, output step and the initial state of every joint."""

    GRAVITY = {"off": [0.0, 0.0, 0.0], "-z": [0.0, 0.0, -9.80665]}

    def __init__(self, ws, study) -> None:
        super().__init__(ws, f"Study settings — {study.name}", "Apply")
        self.study = study
        self.asm: Assembly = ws.assembly
        self._loading = True
        S = get_mbd(study)
        self.S = S
        form = QFormLayout()
        self.gravity_preset = QComboBox()
        self.gravity_preset.addItem("Off (no gravity)", "off")
        self.gravity_preset.addItem("Earth gravity, −Z  (9.80665 m/s²)", "-z")
        self.gravity_preset.addItem("Custom…", "custom")
        form.addRow("Gravity", self.gravity_preset)
        gl = QHBoxLayout()
        self.gravity = [dspin(-1e4, 1e4, 5, 0.1, "m/s²") for _ in range(3)]
        for ax, sp in zip("XYZ", self.gravity):
            gl.addWidget(QLabel(ax))
            gl.addWidget(sp)
        self.gravity_box = QWidget()
        self.gravity_box.setLayout(gl)
        gl.setContentsMargins(0, 0, 0, 0)
        form.addRow("", self.gravity_box)
        self.duration = dspin(1e-6, 1e5, 6, 0.1, "s", S.duration_s)
        self.dt = dspin(0.0, 1.0, 7, 1e-4, "s", S.dt_s)
        self.output_dt = dspin(0.0, 100.0, 6, 1e-3, "s", S.output_dt_s)
        form.addRow("Duration", self.duration)
        form.addRow("Time step (solver)", self.dt)
        form.addRow("Output step (samples stored)", self.output_dt)
        self.integrator = QComboBox()
        self.integrator.addItem("RK4 (accurate, default)", "RK4")
        self.integrator.addItem("Implicit (stable for stiff models)", "implicitfast")
        self.integrator.setCurrentIndex(max(0, self.integrator.findData(S.integrator)))
        form.addRow("Integrator", self.integrator)
        self.root.addLayout(form)
        self.estimate = hint("")
        self.root.addWidget(self.estimate)

        ic = QGroupBox("Starting state of each joint (applies to every study of this assembly)")
        il = QVBoxLayout(ic)
        self.ic_table = QTableWidget(0, 3)
        self.ic_table.setHorizontalHeaderLabels(["Joint", "Start angle / position", "Start speed"])
        self.ic_table.verticalHeader().setVisible(False)
        self.ic_table.horizontalHeader().setStretchLastSection(True)
        self.ic_table.setMaximumHeight(150)
        self._ic_joints = [j for j in self.asm.joints if j.kind != "fixed"]
        self.ic_table.setRowCount(len(self._ic_joints))
        for r, j in enumerate(self._ic_joints):
            prism = j.kind == "prismatic"
            u = (ws.length_unit, f"{ws.length_unit}/s") if prism else ("deg", "deg/s")
            f = 1.0 if prism else 180.0 / math.pi
            it = QTableWidgetItem(f"{j.name} ({j.kind})")
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.ic_table.setItem(r, 0, it)
            self.ic_table.setItem(r, 1, QTableWidgetItem(f"{j.initial_q * f:.6g}"))
            self.ic_table.setItem(r, 2, QTableWidgetItem(f"{j.initial_qd * f:.6g}"))
            self.ic_table.item(r, 1).setToolTip(u[0])
            self.ic_table.item(r, 2).setToolTip(u[1])
        il.addWidget(self.ic_table)
        il.addWidget(hint("Angles in deg, speeds in deg/s (prismatic joints: length unit and length unit/s). A "
                          "joint with a prescribed motor follows its command from the first step."))
        self.root.addWidget(ic)
        self.finish_layout()

        g = list(S.gravity)
        self.gravity_preset.setCurrentIndex(self.gravity_preset.findData(
            "off" if np.allclose(g, 0) else "-z" if np.allclose(g, self.GRAVITY["-z"]) else "custom"))
        for sp, v in zip(self.gravity, g):
            sp.setValue(v)
        self._loading = False
        self.gravity_preset.currentIndexChanged.connect(self._preset)
        for w in (self.duration, self.dt, self.output_dt, *self.gravity):
            w.valueChanged.connect(self._changed)
        self.ic_table.itemChanged.connect(self._changed)
        self._preset()

    def _preset(self) -> None:
        k = self.gravity_preset.currentData()
        self.gravity_box.setVisible(k == "custom")
        if k in self.GRAVITY and not self._loading:
            for sp, v in zip(self.gravity, self.GRAVITY[k]):
                sp.setValue(v)
        self._changed()

    def gravity_vector(self) -> list[float]:
        k = self.gravity_preset.currentData()
        return list(self.GRAVITY[k]) if k in self.GRAVITY else [sp.value() for sp in self.gravity]

    def _ic_values(self) -> list[tuple[float, float]]:
        out = []
        for r, j in enumerate(self._ic_joints):
            f = 1.0 if j.kind == "prismatic" else math.pi / 180.0
            try:
                q = float(self.ic_table.item(r, 1).text()) * f
                qd = float(self.ic_table.item(r, 2).text()) * f
            except (AttributeError, ValueError):
                raise ValueError(f"starting state of '{j.name}': type numbers")
            out.append((q, qd))
        return out

    def _changed(self, *_a) -> None:
        if self._loading:
            return
        msgs = []
        dt, out, dur = self.dt.value(), self.output_dt.value(), self.duration.value()
        if dt <= 0:
            msgs.append("The time step must be larger than zero.")
        elif out < dt * (1 - 1e-9):
            msgs.append("The output step cannot be smaller than the time step.")
        elif dur < out * (1 - 1e-9):
            msgs.append("The duration must be at least one output step.")
        else:
            n = dur / dt
            if n > 5e6:
                msgs.append(f"{n:,.0f} solver steps is too many: increase the time step.")
            ratio = out / dt
            if abs(ratio - round(ratio)) > 1e-6:
                msgs.append("The output step must be a whole multiple of the time step.")
            if not msgs:
                self.estimate.setText(f"{n:,.0f} solver steps, {int(round(dur / out)) + 1} stored samples.")
        try:
            self._ic_values()
        except ValueError as exc:
            msgs.append(str(exc))
        self.set_error("\n".join(msgs))

    def accept(self) -> None:
        if not self.ok_button.isEnabled():
            return
        S = get_mbd(self.study)
        S.gravity = self.gravity_vector()
        S.duration_s, S.dt_s, S.output_dt_s = self.duration.value(), self.dt.value(), self.output_dt.value()
        S.integrator = self.integrator.currentData()
        self.ws.commit_study_settings(self.study, S, dict(zip([j.id for j in self._ic_joints], self._ic_values())))
        super().accept()


# ======================================================================================== manual mass values
class ManualMassDialog(_Dialog):
    """Mass, centre of mass and inertia typed by hand (a purchased motor, a part without CAD density)."""

    def __init__(self, ws, body) -> None:
        super().__init__(ws, f"Set manual values — {body.name}", "Use these values")
        self.body = body
        self.ul = ws.length_unit
        cur = body.effective_mass() or MassProps(mass_kg=0.01, com=[0, 0, 0], source="manual",
                                                 inertia_kgm2=[[1e-6, 0, 0], [0, 1e-6, 0], [0, 0, 1e-6]])
        self.root.addWidget(hint("Use this for parts without usable CAD (for example a motor): they replace the "
                                 "CAD-derived mass of this body. Mass in kg, inertia in kg·m² about the "
                                 "centre of mass, in the assembly axes."))
        form = QFormLayout()
        self.mass = dspin(0.0, 1e6, 6, 0.001, "kg", cur.mass_kg)
        form.addRow("Mass", self.mass)
        self.com = [dspin(decimals=4, suffix=self.ul, value=float(v)) for v in cur.com]
        cl = QHBoxLayout()
        for ax, sp in zip("XYZ", self.com):
            cl.addWidget(QLabel(ax))
            cl.addWidget(sp)
        form.addRow(f"Centre of mass ({self.ul})", cl)
        I = cur.inertia_matrix()
        self.I = {}
        il = QGridLayout()
        for k, (name, (a, b)) in enumerate((("Ixx", (0, 0)), ("Iyy", (1, 1)), ("Izz", (2, 2)), ("Ixy", (0, 1)),
                                            ("Ixz", (0, 2)), ("Iyz", (1, 2)))):
            sp = dspin(-1e6, 1e6, 9, 1e-6, value=float(I[a, b]))
            sp.setDecimals(9)
            self.I[name] = sp
            il.addWidget(QLabel(name), k // 3, 2 * (k % 3))
            il.addWidget(sp, k // 3, 2 * (k % 3) + 1)
        form.addRow("Inertia (kg·m²)", il)
        self.note = QLineEdit(cur.note if cur.source != "cad" else "")
        self.note.setPlaceholderText("where do these numbers come from? (datasheet, weighing, …)")
        form.addRow("Source note", self.note)
        self.root.addLayout(form)
        self.finish_layout()
        for sp in [self.mass, *self.com, *self.I.values()]:
            sp.valueChanged.connect(self._changed)
        self._changed()

    def matrix(self) -> np.ndarray:
        v = {k: s.value() for k, s in self.I.items()}
        return np.array([[v["Ixx"], v["Ixy"], v["Ixz"]], [v["Ixy"], v["Iyy"], v["Iyz"]],
                         [v["Ixz"], v["Iyz"], v["Izz"]]])

    def _changed(self, *_a) -> None:
        msgs = []
        if not self.mass.value() > 0:
            msgs.append("The mass must be larger than zero.")
        I = self.matrix()
        ev = np.linalg.eigvalsh(I)
        if ev.min() <= 0:
            msgs.append("The inertia matrix must be positive definite (all three principal moments > 0).")
        elif ev[2] > (ev[0] + ev[1]) * (1 + 1e-9):
            msgs.append("These principal moments are not physically possible (the largest cannot exceed the sum "
                        "of the other two).")
        self.set_error("\n".join(msgs))

    def accept(self) -> None:
        if not self.ok_button.isEnabled():
            return
        mp = MassProps(mass_kg=self.mass.value(), com=[s.value() for s in self.com],
                       inertia_kgm2=self.matrix().tolist(), source="manual", density_kg_m3=None,
                       note=self.note.text().strip() or "entered manually")
        self.ws.commit_manual_mass(self.body, mp)
        super().accept()


# ======================================================================================== group
class GroupDialog(_Dialog):
    """Merge other bodies into this one (they become one rigid body)."""

    def __init__(self, ws, body) -> None:
        super().__init__(ws, f"Group with… — {body.name}", "Merge into one body")
        self.body = body
        self.root.addWidget(hint(f"Tick the bodies that move together with '{body.name}'. They become one rigid "
                                 "body (their components stay separate in the tree)."))
        self.list = QListWidget()
        for b in ws.assembly.bodies:
            if b.id == body.id:
                continue
            it = QListWidgetItem(b.name + ("  (grounded)" if b.grounded else ""))
            it.setData(Qt.UserRole, b.id)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            self.list.addItem(it)
        self.root.addWidget(self.list)
        self.finish_layout()
        self.list.itemChanged.connect(self._changed)
        self._changed()

    def chosen(self) -> list[str]:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]

    def _changed(self, *_a) -> None:
        self.set_error("" if self.chosen() else "Tick at least one body.")

    def accept(self) -> None:
        if not self.ok_button.isEnabled():
            return
        ok, why = self.ws.group_bodies(self.body.id, self.chosen())
        if not ok:
            self.set_error(why)
            return
        super().accept()


# ======================================================================================== structural load case
class LoadCaseDialog(_Dialog):
    """Hand one component's motion load over to a structural static-hold study."""

    def __init__(self, ws) -> None:
        super().__init__(ws, "Create structural load case", "Create study")
        self.setMinimumWidth(720)
        self._loading = True
        self._pick_role: Optional[str] = None
        self._record = None
        res = ws.result
        self.root.addWidget(hint("Take the loads of this motion at one moment and make a normal structural study "
                                 "from them: the root of the component is fixed, the force acts on the load face. "
                                 "Then mesh and solve it with the usual buttons."))
        form = QFormLayout()
        self.source_label = QLabel(ws.study.name)
        form.addRow("Source study", self.source_label)
        self.time = dspin(float(res.t[0]), float(res.t[-1]), 4, float(res.t[1] - res.t[0]) if len(res.t) > 1 else 0.1,
                          "s", ws.current_time)
        self.time.setDecimals(5)
        form.addRow("Time", self.time)
        self.body_combo = QComboBox()
        for b in ws.loadcase_bodies():
            self.body_combo.addItem(b.name, b.id)
        form.addRow("Component", self.body_combo)
        self.frame_label = QLabel("The component's own CAD frame; the moment is taken about the root joint point.")
        self.frame_label.setWordWrap(True)
        form.addRow("Load frame", self.frame_label)
        rl = QHBoxLayout()
        self.root_face = QComboBox()
        self.root_pick = QPushButton("Pick in 3-D view")
        self.root_pick.setCheckable(True)
        rl.addWidget(self.root_face, 1)
        rl.addWidget(self.root_pick)
        form.addRow("Fixed (root) face", rl)
        ll = QHBoxLayout()
        self.load_face = QComboBox()
        self.load_pick = QPushButton("Pick in 3-D view")
        self.load_pick.setCheckable(True)
        ll.addWidget(self.load_face, 1)
        ll.addWidget(self.load_pick)
        form.addRow("Load (application) face", ll)
        self.root.addLayout(form)
        self.values = QPlainTextEdit()
        self.values.setReadOnly(True)
        self.values.setMinimumHeight(240)
        mono = QFont("monospace")
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSizeF(9.0)
        self.values.setFont(mono)
        self.root.addWidget(self.values)
        self.note_label = QLabel("")
        self.note_label.setWordWrap(True)
        self.note_label.setObjectName("hint")
        self.root.addWidget(self.note_label)
        self.finish_layout()
        self.body_combo.currentIndexChanged.connect(self._body_changed)
        self.time.valueChanged.connect(self._changed)
        self.root_face.currentIndexChanged.connect(self._changed)
        self.load_face.currentIndexChanged.connect(self._changed)
        self.root_pick.toggled.connect(lambda on: self._pick("root", on))
        self.load_pick.toggled.connect(lambda on: self._pick("load", on))
        sel = ws.selected
        if sel[0] == "body":
            self.body_combo.setCurrentIndex(max(0, self.body_combo.findData(sel[1])))
        self._loading = False
        self._body_changed()

    def body_id(self) -> Optional[str]:
        return self.body_combo.currentData()

    def _body_changed(self, *_a) -> None:
        bid = self.body_id()
        faces = self.ws.end_faces(bid) if bid else []
        for combo in (self.root_face, self.load_face):
            combo.blockSignals(True)
            combo.clear()
        gid = self.ws.geometry_id_of_body(bid) if bid else None
        for f in faces:
            label = f"Face {f['index'] + 1}: {f['label']} ({f['area']:.4g} {self.ws.length_unit}²)"
            self.root_face.addItem(label, f["index"])
            self.load_face.addItem(label, f["index"])
        if faces:
            near = [f for f in faces if f.get("role") == "root"]
            far = [f for f in faces if f.get("role") == "tip"]
            if near:
                self.root_face.setCurrentIndex(self.root_face.findData(near[0]["index"]))
            if far:
                self.load_face.setCurrentIndex(self.load_face.findData(far[0]["index"]))
        for combo in (self.root_face, self.load_face):
            combo.blockSignals(False)
        self._gid = gid
        self._changed()

    def _pick(self, role: str, on: bool) -> None:
        other = self.load_pick if role == "root" else self.root_pick
        if on:
            other.blockSignals(True)
            other.setChecked(False)
            other.blockSignals(False)
            self._pick_role = role
            self.ws.begin_pick("face", self.apply_face_pick)
        else:
            self.ws.end_pick()
            self._pick_role = None

    def apply_face_pick(self, pick) -> bool:
        if pick is None or pick.kind != "face" or pick.body != getattr(self, "_gid", None):
            self.set_error("Pick a face of the selected component (its own geometry).")
            return False
        combo = self.root_face if self._pick_role == "root" else self.load_face
        k = combo.findData(pick.index)
        if k < 0:
            combo.addItem(f"Face {pick.index + 1}: picked", pick.index)
            k = combo.findData(pick.index)
        combo.setCurrentIndex(k)
        for b in (self.root_pick, self.load_pick):
            b.blockSignals(True)
            b.setChecked(False)
            b.blockSignals(False)
        self.ws.end_pick()
        self._pick_role = None
        return True

    def values_text(self) -> str:
        return self.values.toPlainText()

    def _changed(self, *_a) -> None:
        if self._loading:
            return
        self._record = None
        self.note_label.setText("Static hold: the structural study has no inertia, so it is valid while the "
                                "component is not accelerating.")
        if self.body_id() is None or self.root_face.currentData() is None or self.load_face.currentData() is None:
            self.values.setPlainText("")
            self.set_error("Choose a component, a root face and a load face.")
            return
        try:
            text, record = self.ws.preview_load_case(self.body_id(), float(self.time.value()),
                                                     self.root_face.currentData(), self.load_face.currentData())
        except Exception as exc:  # noqa: BLE001 - the message is the explanation
            self.values.setPlainText("")
            self.set_error(str(exc))
            return
        self._record = record
        self.values.setPlainText(text)
        self.set_error("")

    def accept(self) -> None:
        if not self.ok_button.isEnabled():
            return
        try:
            self.ws.create_load_case(self.body_id(), float(self.time.value()), self.root_face.currentData(),
                                     self.load_face.currentData())
        except Exception as exc:  # noqa: BLE001
            self.set_error(str(exc))
            return
        self.ws.end_pick()
        super().accept()

    def reject(self) -> None:
        self.ws.end_pick()
        super().reject()
