"""Declarative forms used by the properties panel and dialogs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QSpinBox,
                               QVBoxLayout, QWidget)


@dataclass
class Field:
    key: str
    label: str
    kind: str = "float"  # float | int | str | multiline | bool | choice | vec3 | text | optfloat | multicheck
    value: Any = None
    unit: str = ""
    options: list = field(default_factory=list)  # choice: [(value, label)], multicheck: [(value, label)]
    minimum: float = -1e30
    maximum: float = 1e30
    decimals: int = 6
    readonly: bool = False
    tooltip: str = ""


class _Float(QLineEdit):
    """Float entry accepting scientific notation; validated on read."""

    def __init__(self, value: float, minimum: float, maximum: float):
        super().__init__(f"{value:.10g}" if value is not None else "")
        self.minimum, self.maximum = minimum, maximum

    def value(self) -> float:
        try:
            v = float(self.text().strip())
        except ValueError as exc:
            raise ValueError(f"'{self.text()}' is not a number") from exc
        if not (self.minimum <= v <= self.maximum):
            raise ValueError(f"{v:g} outside allowed range [{self.minimum:g}, {self.maximum:g}]")
        return v


class FormWidget(QWidget):
    changed = Signal()

    def __init__(self, fields: list[Field], parent=None):
        super().__init__(parent)
        self.fields = fields
        self.editors: dict[str, Any] = {}
        lay = QFormLayout(self)
        lay.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setVerticalSpacing(6)
        for f in fields:
            w = self._editor(f)
            if f.tooltip:
                w.setToolTip(f.tooltip)
            label = f"{f.label}" + (f" [{f.unit}]" if f.unit else "")
            lay.addRow(label, w)
            self.editors[f.key] = w

    def _editor(self, f: Field):
        if f.kind in ("text",) or f.readonly and f.kind not in ("bool",):
            lab = QLabel(str(f.value) if not isinstance(f.value, float) else f"{f.value:.6g}")
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lab.setWordWrap(True)
            return lab
        if f.kind == "float":
            e = _Float(f.value, f.minimum, f.maximum)
            e.textEdited.connect(lambda *_: self.changed.emit())
            return e
        if f.kind == "optfloat":  # empty = None
            e = QLineEdit("" if f.value is None else f"{f.value:.10g}")
            e.setPlaceholderText("free")
            e.textEdited.connect(lambda *_: self.changed.emit())
            return e
        if f.kind == "int":
            e = QSpinBox()
            e.setRange(int(max(f.minimum, -2 ** 31)), int(min(f.maximum, 2 ** 31 - 1)))
            e.setValue(int(f.value))
            e.valueChanged.connect(lambda *_: self.changed.emit())
            return e
        if f.kind == "str":
            e = QLineEdit(str(f.value or ""))
            e.textEdited.connect(lambda *_: self.changed.emit())
            return e
        if f.kind == "multiline":
            e = QPlainTextEdit(str(f.value or ""))
            e.setMinimumHeight(110)
            e.textChanged.connect(lambda *_: self.changed.emit())
            return e
        if f.kind == "bool":
            e = QCheckBox()
            e.setChecked(bool(f.value))
            e.setEnabled(not f.readonly)
            e.toggled.connect(lambda *_: self.changed.emit())
            return e
        if f.kind == "choice":
            e = QComboBox()
            for val, lab in f.options:
                e.addItem(lab, val)
            idx = next((i for i, (v, _) in enumerate(f.options) if v == f.value), 0)
            e.setCurrentIndex(idx)
            e.currentIndexChanged.connect(lambda *_: self.changed.emit())
            return e
        if f.kind == "vec3":
            box = QWidget()
            h = QHBoxLayout(box)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(3)
            vals = list(f.value or [0, 0, 0])
            box.parts = []
            for i, ax in enumerate("xyz"):
                e = _Float(float(vals[i]), -1e30, 1e30)
                e.setPlaceholderText(ax)
                e.setToolTip(ax)
                e.textEdited.connect(lambda *_: self.changed.emit())
                h.addWidget(e)
                box.parts.append(e)
            return box
        if f.kind == "multicheck":
            lw = QListWidget()
            lw.setMinimumHeight(140)
            chosen = set(f.value or [])
            for val, lab in f.options:
                it = QListWidgetItem(lab)
                it.setData(Qt.UserRole, val)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if val in chosen else Qt.Unchecked)
                lw.addItem(it)
            lw.itemChanged.connect(lambda *_: self.changed.emit())
            return lw
        raise ValueError(f.kind)

    def values(self) -> dict[str, Any]:
        """Return validated values; raises ValueError with the field label on bad input."""
        out = {}
        for f in self.fields:
            if f.readonly or f.kind == "text":
                continue
            w = self.editors[f.key]
            try:
                if f.kind == "float":
                    out[f.key] = w.value()
                elif f.kind == "optfloat":
                    t = w.text().strip()
                    out[f.key] = None if t == "" else float(t)
                elif f.kind == "int":
                    out[f.key] = int(w.value())
                elif f.kind == "str":
                    out[f.key] = w.text()
                elif f.kind == "multiline":
                    out[f.key] = w.toPlainText()
                elif f.kind == "bool":
                    out[f.key] = w.isChecked()
                elif f.kind == "choice":
                    out[f.key] = w.currentData()
                elif f.kind == "vec3":
                    out[f.key] = [p.value() for p in w.parts]
                elif f.kind == "multicheck":
                    out[f.key] = [w.item(i).data(Qt.UserRole) for i in range(w.count())
                                  if w.item(i).checkState() == Qt.Checked]
            except ValueError as exc:
                raise ValueError(f"{f.label}: {exc}") from exc
        return out

    def set_value(self, key: str, value: Any) -> None:
        w = self.editors[key]
        f = next(x for x in self.fields if x.key == key)
        if f.kind == "float":
            w.setText(f"{value:.10g}")
        elif f.kind == "choice":
            for i in range(w.count()):
                if w.itemData(i) == value:
                    w.setCurrentIndex(i)
        elif f.kind == "multicheck":
            for i in range(w.count()):
                w.item(i).setCheckState(Qt.Checked if w.item(i).data(Qt.UserRole) in value else Qt.Unchecked)


class ParamDialog(QDialog):
    def __init__(self, title: str, fields: list[Field], parent=None, note: str = "", validator=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        self._validator = validator
        lay = QVBoxLayout(self)
        if note:
            lab = QLabel(note)
            lab.setObjectName("hint")
            lab.setWordWrap(True)
            lay.addWidget(lab)
        self.form = FormWidget(fields)
        lay.addWidget(self.form)
        self.error = QLabel("")
        self.error.setObjectName("error")
        self.error.setWordWrap(True)
        lay.addWidget(self.error)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.result_values: Optional[dict] = None

    def _accept(self) -> None:
        try:
            vals = self.form.values()
            if self._validator:
                msg = self._validator(vals)
                if msg:
                    raise ValueError(msg)
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.result_values = vals
        self.accept()
