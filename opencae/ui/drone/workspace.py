"""Drone mode of the OpenCAE main window: a friendly start page, a parameter sheet on the left and the dashboard of
the project on the right.

Opening a project starts a ``DashboardServer`` and shows its page in a QWebEngineView (or offers the system
browser when QtWebEngine is missing, disabled by safe mode, or its render process died). Runs go through the
server's job manager, so the status-bar progress and the embedded dashboard show the same run.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QBrush, QColor, QDesktopServices, QDoubleValidator, QFont, QKeySequence, QShortcut,
                           QUndoCommand, QUndoStack)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
                               QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QStackedWidget,
                               QStyledItemDelegate, QToolButton, QToolTip, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from opencae.drone import spec as S
from opencae.ui import theme

try:  # optional: without it the dashboard opens in the system browser (looked up here, imported on first use)
    HAVE_WEB = importlib.util.find_spec("PySide6.QtWebEngineWidgets") is not None
except Exception:  # pragma: no cover - depends on the install
    HAVE_WEB = False


def web_available() -> bool:
    """QtWebEngine needs Qt.AA_ShareOpenGLContexts set before the QApplication exists (app/main.py does it);
    without it creating a view crashes the process, so the browser fallback is used instead."""
    if not HAVE_WEB:
        return False
    from PySide6.QtCore import QCoreApplication
    if hasattr(os, "geteuid") and os.geteuid() == 0 and "--no-sandbox" not in os.environ.get(
            "QTWEBENGINE_CHROMIUM_FLAGS", ""):  # Chromium refuses to start as root (containers, CI) without it
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "") +
                                                    " --no-sandbox").strip()
    return QCoreApplication.testAttribute(Qt.AA_ShareOpenGLContexts)


def web_allowed() -> bool:
    """The crash guard (safe mode, OPENCAE_NO_WEBVIEW) decides first; without the module the view is allowed."""
    try:
        from opencae.utilities import crash
    except ImportError:
        return True
    try:
        return bool(crash.web_view_allowed())
    except Exception:  # noqa: BLE001 - a broken guard must not stop the app; be careful and use the browser
        return False


def web_fallback_reason() -> str:
    """Why the dashboard is not shown inside the window (plain words), or "" when it can be."""
    try:
        from opencae.utilities import crash
        if crash.safe_mode():
            return ("OpenCAE is in safe mode because it closed unexpectedly last time, so the dashboard opens in "
                    "your browser. Restart OpenCAE normally to bring the built-in view back.")
        if os.environ.get("OPENCAE_NO_WEBVIEW") == "1":
            return "The built-in web view is switched off (OPENCAE_NO_WEBVIEW=1), so the dashboard opens in your browser."
    except ImportError:
        pass
    if not HAVE_WEB:
        return "The built-in web view is not available in this installation, so the dashboard opens in your browser."
    if not web_available():
        return "The built-in web view is not enabled in this session, so the dashboard opens in your browser."
    if not web_allowed():
        return "The built-in web view is turned off, so the dashboard opens in your browser."
    return ""


EXAMPLES = {"Quad 250 (generic example)": "quad250", "K09-L tailsitter": "k09l"}
STATUSES = ["estimate", "datasheet", "measured", "user"]
SKIP_TOP = {"format", "name", "type", "engine", "notes", "description"}
FOOTER = "Simulation results plan the tests; they are not flight approval."
CAD_SUFFIXES = (".step", ".stp", ".stl", ".obj")
RECENT_KEY = "drone/recent"
MAX_RECENT = 8


def examples_root() -> Path:
    return Path(__file__).resolve().parents[2] / "examples" / "drones"


def projects_home() -> Path:
    return Path(os.environ.get("OPENCAE_HOME") or Path.home() / ".opencae") / "projects"


def copy_example(key: str) -> Path:
    """Copy a bundled example to ~/.opencae/projects/<key> once (the package folder is never written to)."""
    src, dst = examples_root() / key, projects_home() / key
    if not src.exists():
        raise FileNotFoundError(f"example not found: {src}")
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("results", "__pycache__"))
    return dst


# ------------------------------------------------------------------------------------------------ parameter sheet
def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def iter_params(node, prefix="", inherit=None):
    """Yield (dotted key, value, unit, status) for every numeric leaf of a drone.json section. A leaf that is
    still ``null`` (not filled in yet) is yielded with value None. A material's status applies to its numbers."""
    if isinstance(node, dict):
        if "value" in node and not any(isinstance(v, dict) for v in node.values()):
            if _is_number(node["value"]) or (node["value"] is None and prefix):
                yield prefix, node["value"], node.get("unit", ""), node.get("status", inherit or "estimate")
            return
        st = node.get("status") if node.get("status") in STATUSES and "source" in node else inherit
        for k, v in node.items():
            yield from iter_params(v, f"{prefix}.{k}" if prefix else k, st)
    elif _is_number(node):
        yield prefix, node, "", inherit or "estimate"
    elif node is None and prefix:
        yield prefix, None, "", inherit or "estimate"


LABELS = {
    "propulsion.motor.kv": "Motor KV", "propulsion.motor.r_ohm": "Motor resistance",
    "propulsion.motor.i0_a": "Motor no-load current", "propulsion.motor.i_max_a": "Motor max current",
    "propulsion.motor.mass_g": "Motor mass", "propulsion.prop.diameter_in": "Prop diameter",
    "propulsion.prop.pitch_in": "Prop pitch", "propulsion.prop.ct": "Prop thrust coefficient (Ct)",
    "propulsion.prop.cp": "Prop power coefficient (Cp)", "propulsion.battery.cells": "Battery cells (S)",
    "propulsion.battery.capacity_mah": "Battery capacity", "propulsion.battery.c_rating": "Battery C rating",
    "propulsion.battery.r_int_mohm_per_cell": "Battery internal resistance per cell",
    "propulsion.battery.usable_fraction": "Battery usable fraction", "propulsion.esc.i_cont_a": "ESC continuous current",
    "propulsion.esc.efficiency": "ESC efficiency", "aero.cruise_speed_ms": "Cruise speed",
    "aero.alpha_deg": "Angle of attack", "aero.ref_area_m2": "Reference area", "aero.ref_chord_m": "Reference chord",
    "aero.cd0": "Zero-lift drag (Cd0)", "mission.hover_s": "Hover time", "mission.cruise_s": "Cruise time",
    "mission.payload_g": "Mission payload", "mass.payload_g": "Payload mass", "environment.rho": "Air density",
    "environment.g": "Gravity", "analyses.cfd.iterations": "CFD iterations", "analyses.cfd.speed_ms": "CFD air speed",
    "analyses.cfd.alpha_deg": "CFD angle of attack", "analyses.structure.load_factor": "Structure load factor",
    "analyses.flight.disturbance_deg": "Flight gust kick",
}
_MATERIAL_PROPS = {"density_kg_m3": "density", "youngs_modulus_gpa": "Young's modulus", "poisson": "Poisson ratio",
                   "yield_strength_mpa": "yield strength", "max_temp_c": "max temperature"}

# one or two plain sentences per parameter (from docs/DRONE_PROJECTS.md)
HELP = {
    "propulsion.motor.kv": "Motor constant in rpm per volt. Printed on the motor or in its datasheet.",
    "propulsion.motor.r_ohm": "Winding resistance of the motor in ohm (datasheet, or measured).",
    "propulsion.motor.i0_a": "Current the motor draws with no load, in amps.",
    "propulsion.motor.i_max_a": "Highest current the motor is rated for, in amps.",
    "propulsion.motor.mass_g": "Mass of ONE motor in grams. Used for motor parts that have no mass entry.",
    "propulsion.prop.diameter_in": "Propeller diameter in inches (a 5 inch prop is 5.0).",
    "propulsion.prop.pitch_in": "Propeller pitch in inches (how far it advances per turn).",
    "propulsion.prop.ct": "Static thrust coefficient: thrust = Ct x air density x (rev/s)^2 x diameter^4. "
                          "From the maker's test data or your own thrust stand.",
    "propulsion.prop.cp": "Static power coefficient: power = Cp x air density x (rev/s)^3 x diameter^5.",
    "propulsion.battery.cells": "Number of cells in series (3S = 3). Each cell is 3.7 V nominal.",
    "propulsion.battery.capacity_mah": "Battery capacity in milliamp-hours.",
    "propulsion.battery.c_rating": "Continuous discharge rating. C x capacity is the allowed current.",
    "propulsion.battery.r_int_mohm_per_cell": "Internal resistance of one cell in milliohm.",
    "propulsion.battery.usable_fraction": "How much of the capacity you will use, 0 to 1 (0.8 keeps a reserve).",
    "propulsion.esc.i_cont_a": "Continuous current rating of the speed controller, in amps.",
    "propulsion.esc.efficiency": "Speed controller efficiency, 0 to 1 (about 0.95 is typical).",
    "aero.cruise_speed_ms": "Speed in metres per second used for the air-flow (CFD) analysis.",
    "aero.alpha_deg": "Nose-up angle of the aircraft in that flow, in degrees.",
    "aero.ref_area_m2": "Optional reference area for the force coefficients, in square metres.",
    "aero.ref_chord_m": "Optional reference chord for the moment coefficients, in metres.",
    "aero.cd0": "Optional zero-lift drag coefficient.",
    "mission.hover_s": "Seconds you plan to hover.", "mission.cruise_s": "Seconds you plan to cruise.",
    "mission.payload_g": "Grams of payload carried in the mission.",
    "mass.payload_g": "Grams carried, counted in the total mass (0 if none).",
    "environment.rho": "Air density in kg/m3 (1.225 at sea level).",
    "environment.g": "Gravity in m/s2 (9.80665).",
    "analyses.cfd.iterations": "Solver iterations of the air-flow run. More can help it settle but takes longer.",
    "analyses.cfd.speed_ms": "Optional air speed for the CFD run; overrides the cruise speed.",
    "analyses.cfd.alpha_deg": "Optional angle of attack for the CFD run; overrides the aero value.",
    "analyses.structure.load_factor": "Multiple of the maximum thrust applied to the arms (2 = twice max thrust).",
    "analyses.flight.disturbance_deg": "The tilt the simulated aircraft is knocked by, then must recover from.",
}
_MATERIAL_HELP = {
    "density_kg_m3": "Density in kg/m3. Gives a part its mass when you have not typed one (closed CAD solid needed).",
    "youngs_modulus_gpa": "Stiffness in GPa. Feeds the structure analysis.",
    "poisson": "Poisson ratio, between 0 and 0.5 (about 0.3 for metals and carbon plate).",
    "yield_strength_mpa": "Stress in MPa at which the material starts to give. Feeds the safety factor.",
    "max_temp_c": "Highest temperature the material tolerates, in degrees C.",
}

_UNIT_SUFFIX = (("_mohm_per_cell", "mohm"), ("_kg_m3", "kg/m3"), ("_gpa", "GPa"), ("_mpa", "MPa"), ("_mah", "mAh"),
                ("_ohm", "ohm"), ("_deg", "deg"), ("_ms", "m/s"), ("_m2", "m2"), ("_in", "in"), ("_c", "C"),
                ("_a", "A"), ("_g", "g"), ("_s", "s"), ("_m", "m"))
_UNIT_KEYS = {"propulsion.motor.kv": "rpm/V", "environment.rho": "kg/m3", "environment.g": "m/s2"}
_GROUP_ICONS = {"mass": "gravity", "propulsion": "force", "aero": "deform", "materials": "material",
                "mission": "animate", "environment": "pressure", "analyses": "solve"}


def unit_for(key: str, unit: str = "") -> str:
    """The unit of a parameter: the file's own, else a guess from the key name (empty when unknown)."""
    if unit:
        return unit
    if key in _UNIT_KEYS:
        return _UNIT_KEYS[key]
    last = key.split(".")[-1]
    for suf, u in _UNIT_SUFFIX:
        if last.endswith(suf):
            return u
    return ""


def _words(text: str) -> str:
    t = text.replace("_", " ").strip()
    return t[:1].upper() + t[1:]


def friendly_label(key: str) -> str:
    """A readable name for a dotted drone.json key (the dotted key stays available as the tooltip)."""
    if key in LABELS:
        return LABELS[key]
    parts = key.split(".")
    if parts[0] == "materials" and len(parts) == 3:
        return f"{_words(parts[1])} – {_MATERIAL_PROPS.get(parts[2], parts[2].replace('_', ' '))}"
    if key.startswith("mass.parts.") and len(parts) == 3:
        return f"{_words(parts[2])} mass"
    return _words(" ".join(parts[1:]) if len(parts) > 1 else key)


def help_for(key: str) -> str:
    if key in HELP:
        return HELP[key]
    parts = key.split(".")
    if parts[0] == "materials" and len(parts) == 3 and parts[2] in _MATERIAL_HELP:
        return f"Material '{parts[1]}': " + _MATERIAL_HELP[parts[2]]
    if key.startswith("mass.parts."):
        return "Mass in grams. A role name is the mass of EACH part with that role; a part name is that one part."
    return f"{friendly_label(key)} ({key}). See docs/DRONE_PROJECTS.md for what it means."


def is_integer_key(key: str) -> bool:
    return S.is_integer_key(key)


def check_value(key: str, text):
    """(number or None, message) for a text typed into the sheet or wizard; the range rules live in spec."""
    t = str(text).strip()
    if t == "":
        return None, ""
    try:
        num = float(t)
        if not math.isfinite(num):
            raise ValueError
    except ValueError:
        return None, f"'{t}' is not a number ({key})"
    return (None, msg) if (msg := S.check_value(key, num, friendly_label(key), t)) else (num, "")


def _fmt(v) -> str:
    if v is None:
        return ""
    return f"{v:g}" if isinstance(v, float) else str(v)


_CHANGED_BG = QColor("#3d3720")
_ERROR_FG = QColor(theme.ERR)
_DIM_FG = QColor(theme.FG_DIM)


class _Edit(QUndoCommand):
    def __init__(self, sheet, key, old, new, kind):
        super().__init__(f"{'Edit' if kind == 'value' else 'Evidence of'} {friendly_label(key)}")
        self.sheet, self.key, self.old, self.new, self.kind = sheet, key, old, new, kind

    def redo(self):
        self.sheet._apply(self.key, self.new, self.kind)

    def undo(self):
        self.sheet._apply(self.key, self.old, self.kind)


class _Spin(QDoubleSpinBox):
    """A spin box that shows 2400 and 0.07, not 2400.000000."""

    def textFromValue(self, v):
        return _fmt(round(v, 9))

    def valueFromText(self, text):
        try:
            return float(text.replace(self.suffix(), "").strip())
        except ValueError:
            return 0.0

    def validate(self, text, pos):
        from PySide6.QtGui import QValidator
        t = text.replace(self.suffix(), "").strip()
        if t in ("", "-", "+", "."):
            return QValidator.Intermediate, text, pos
        try:
            float(t)
            return QValidator.Acceptable, text, pos
        except ValueError:
            return QValidator.Invalid, text, pos


class _ValueDelegate(QStyledItemDelegate):
    """Editing a value opens a unit-aware spin box. Negative numbers can be typed; they are then flagged in red
    with a message instead of being silently clamped."""

    def createEditor(self, parent, option, index):
        key = index.sibling(index.row(), 0).data(Qt.UserRole)
        if not key:
            return super().createEditor(parent, option, index)
        sp = _Spin(parent)
        sp.setRange(-1e12, 1e12)
        sp.setDecimals(0 if is_integer_key(key) else 6)
        sp.setKeyboardTracking(False)
        sp.setProperty("key", key)
        u = index.sibling(index.row(), 2).data(Qt.DisplayRole)
        if u:
            sp.setSuffix(f" {u}")
        sp.setToolTip(help_for(key))
        return sp

    def setEditorData(self, editor, index):
        if isinstance(editor, _Spin):
            t = str(index.data(Qt.EditRole) or "").strip()
            editor.setProperty("empty0", t == "")
            try:
                editor.setValue(float(t))
            except ValueError:
                editor.setValue(0.0)
            editor.selectAll()
        else:
            super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        if isinstance(editor, _Spin):
            if editor.property("empty0") and editor.value() == 0.0:
                return  # opened and closed on a not-yet-filled value: leave it empty
            model.setData(index, _fmt(round(editor.value(), 9)), Qt.EditRole)
        else:
            super().setModelData(editor, model, index)


class ParameterSheet(QTreeWidget):
    """Numeric inputs of drone.json grouped by top-level section: value, unit, evidence status, help and reset.

    Edits go through a QUndoStack. A row that differs from the file is highlighted; a bad value is red with a
    message. ``edited`` fires after every edit, undo, redo, load or revert."""

    edited = Signal()
    COL_NAME, COL_VALUE, COL_UNIT, COL_STATUS, COL_ACT = range(5)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(5)
        self.setHeaderLabels(["Parameter", "Value", "Unit", "Status", ""])
        self.setAlternatingRowColors(True)
        self.setIconSize(self.iconSize().expandedTo(self.iconSize()))
        self.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed |
                             QAbstractItemView.SelectedClicked)
        self.setItemDelegateForColumn(self.COL_VALUE, _ValueDelegate(self))
        self.header().setMinimumSectionSize(40)
        self.header().setSectionResizeMode(0, QHeaderView.Interactive)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        for c in (1, 2, 3, 4):
            self.header().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self._orig: dict = {}
        self._status_seen: dict = {}
        self._items: dict = {}
        self._combos: dict = {}
        self._template: dict = {}
        self._groups: dict = {}
        self._text: dict = {}      # last text seen per key (for undo)
        self._errors: dict = {}
        self._spec: dict = {}
        self._busy = False
        self.undo_stack = QUndoStack(self)
        self.itemChanged.connect(self._on_item_changed)
        self.undo_stack.indexChanged.connect(self._index_changed)

    def _index_changed(self, *_):
        try:
            self.edited.emit()
        except RuntimeError:  # the widget is being deleted
            pass

    # ------------------------------------------------------------------ loading
    def load(self, spec: dict) -> None:
        self._busy = True
        self.clear()
        self._orig, self._items, self._combos, self._groups = {}, {}, {}, {}
        self._status_seen = {}
        self._text, self._errors, self._spec = {}, {}, spec
        self._template = self._template_values(spec)
        from opencae.ui.icons import icon
        for key, value, unit, status in iter_params({k: v for k, v in spec.items() if k not in SKIP_TOP}):
            top = key.split(".")[0]
            if top not in self._groups:
                g = QTreeWidgetItem(self, [_words(top)])
                g.setFirstColumnSpanned(True)
                f = g.font(0)
                f.setBold(True)
                g.setFont(0, f)
                g.setIcon(0, icon(_GROUP_ICONS.get(top, "settings")))
                g.setForeground(0, QBrush(QColor(theme.ACCENT)))
                self._groups[top] = g
            u = unit_for(key, unit)
            label = friendly_label(key) + ("  (needs a value)" if value is None else "")
            it = QTreeWidgetItem(self._groups[top], [label, _fmt(value), u, "", ""])
            it.setToolTip(0, key)
            it.setToolTip(1, "Not filled in yet: type a number from a datasheet or a measurement."
                          if value is None else help_for(key))
            it.setFlags(it.flags() | Qt.ItemIsEditable)
            it.setData(0, Qt.UserRole, key)
            self._items[key] = it
            self._orig[key] = (value, status)
            self._text[key] = _fmt(value)
            cb = QComboBox()
            cb.addItems(STATUSES)
            cb.setCurrentText(status if status in STATUSES else "estimate")
            cb.setToolTip("Where this number comes from: estimate, datasheet, measured or user.")
            cb.currentTextChanged.connect(lambda t, k=key: self._on_status(k, t))
            self.setItemWidget(it, self.COL_STATUS, cb)
            self._combos[key] = cb
            self.setItemWidget(it, self.COL_ACT, self._row_buttons(key))
        for g in self._groups.values():
            g.setExpanded(True)
        self.undo_stack.clear()
        self._busy = False
        for key in self._items:
            self._refresh_row(key)
        self.resizeColumnToContents(0)  # fits the longest label; never narrower than 220 px
        self.setColumnWidth(0, max(self.columnWidth(0), 220))
        self.edited.emit()

    @staticmethod
    def _template_values(spec: dict) -> dict:
        """Numbers a fresh template of this drone type would hold (most are empty: those reset to the file)."""
        try:
            from opencae.drone import templates as TPL
            g = spec.get("geometry") or {}
            roles = spec.get("roles") or {}
            t = TPL.template(spec.get("type", "multirotor"), spec.get("name", "drone"), g.get("file", "a.stl"),
                             g.get("units", "mm"), roles, list(roles))
            return {k: v for k, v, _u, _s in iter_params({a: b for a, b in t.items() if a not in SKIP_TOP})
                    if v is not None}
        except Exception:  # noqa: BLE001 - the template is a convenience only
            return {}

    def _row_buttons(self, key: str) -> QWidget:
        w = QWidget()
        w.setAutoFillBackground(False)
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 0, 2, 0)
        h.setSpacing(2)
        q = QToolButton()
        q.setText("?")
        q.setToolTip(help_for(key))
        q.setAutoRaise(True)
        q.clicked.connect(lambda checked=False, k=key, b=q: QToolTip.showText(b.mapToGlobal(b.rect().bottomLeft()),
                                                                              help_for(k), b))
        r = QToolButton()
        r.setText("↺")
        r.setAutoRaise(True)
        r.setToolTip("Reset to template" if key in self._template else "Reset to the saved value")
        r.clicked.connect(lambda checked=False, k=key: self.reset_row(k))
        h.addWidget(q)
        h.addWidget(r)
        return w

    # ------------------------------------------------------------------ editing
    def _apply(self, key: str, value: str, kind: str) -> None:
        """Set a row from an undo command (no new command is made)."""
        self._busy = True
        try:
            if kind == "value":
                self._items[key].setText(self.COL_VALUE, value)
                self._text[key] = value
            else:
                self._combos[key].setCurrentText(value)
                self._status_seen[key] = value
        finally:
            self._busy = False
        self._refresh_row(key)
        self.edited.emit()

    def _on_item_changed(self, item, col):
        if self._busy or col != self.COL_VALUE:
            return
        key = item.data(0, Qt.UserRole)
        if key not in self._items:
            return
        old, new = self._text.get(key, ""), item.text(col)
        if old == new:
            return
        self._text[key] = new
        self.undo_stack.push(_Edit(self, key, old, new, "value"))

    def _on_status(self, key: str, new: str):
        if self._busy:
            return
        old = self._status_seen.get(key) or self._orig[key][1]
        if old == new:
            return
        self._status_seen[key] = new
        self.undo_stack.push(_Edit(self, key, old, new, "status"))

    def reset_row(self, key: str) -> None:
        """Back to the template value when the template has one, else to the value saved in the file."""
        if key not in self._items:
            return
        v = self._template.get(key, self._orig[key][0])
        text = _fmt(v)
        if text != self._items[key].text(self.COL_VALUE):
            self.undo_stack.push(_Edit(self, key, self._items[key].text(self.COL_VALUE), text, "value"))

    def revert(self) -> None:
        """Drop every unsaved edit."""
        if self._spec:
            self.load(self._spec)

    def _refresh_row(self, key: str) -> None:
        it = self._items[key]
        ov, ost = self._orig[key]
        text = it.text(self.COL_VALUE).strip()
        num, msg = check_value(key, text)
        status = self._combos[key].currentText()
        value_changed = text != _fmt(ov).strip()
        changed = value_changed or status != ost
        if msg:
            self._errors[key] = msg
        else:
            self._errors.pop(key, None)
        for c in range(4):
            it.setBackground(c, QBrush(_CHANGED_BG) if changed else QBrush())
        it.setForeground(self.COL_VALUE, QBrush(_ERROR_FG if msg else (_DIM_FG if (ov is None and not text) else
                                                                         QColor(theme.FG))))
        it.setToolTip(self.COL_VALUE, msg or ("Changed: not saved yet. Ctrl+S saves." if changed else
                                              ("Not filled in yet." if ov is None else help_for(key))))
        it.setToolTip(0, key)

    # ------------------------------------------------------------------ queries
    def changed_keys(self) -> list:
        out = []
        for key, it in self._items.items():
            ov, ost = self._orig[key]
            if it.text(self.COL_VALUE).strip() != _fmt(ov).strip() or self._combos[key].currentText() != ost:
                out.append(key)
        return out

    def is_dirty(self) -> bool:
        return bool(self.changed_keys())

    def first_error(self) -> str:
        return next(iter(self._errors.values()), "")

    def errors(self) -> dict:
        return dict(self._errors)

    def edits(self) -> tuple[dict, dict]:
        """(value overrides, status changes) against the loaded file. Raises ValueError on a bad entry."""
        vals, stats = {}, {}
        for key, it in self._items.items():
            ov, os_ = self._orig[key]
            text = it.text(1).strip()
            num, msg = check_value(key, text)
            if msg:
                raise ValueError(msg)
            if text == "":
                if ov is not None:
                    raise ValueError(f"{friendly_label(key)} is empty; enter a number ({key})")
            elif ov is None or num != float(ov):
                vals[key] = int(num) if (isinstance(ov, int) or (ov is None and is_integer_key(key))) \
                    and num == int(num) else num
            st = self._combos[key].currentText()
            if st != os_ and (text != "" or ov is not None):
                stats[key] = st
        return vals, stats

    # ------------------------------------------------------------------ filter
    def set_filter(self, text: str) -> int:
        """Show only rows whose label, key or help contains every word of ``text``. Returns the rows shown."""
        words = [w for w in text.lower().split() if w]
        shown = 0
        for top, g in self._groups.items():
            n = 0
            for i in range(g.childCount()):
                ch = g.child(i)
                key = ch.data(0, Qt.UserRole)
                hay = f"{ch.text(0)} {key} {help_for(key)} {ch.text(2)}".lower()
                ok = all(w in hay for w in words)
                ch.setHidden(not ok)
                n += ok
            g.setHidden(n == 0)
            if words and n:
                g.setExpanded(True)
            shown += n
        return shown


# ------------------------------------------------------------------------------------------------ start page
def project_info(path) -> dict:
    """{name, type, verdict, exists} of a project folder (never raises)."""
    p = Path(path)
    info = {"path": str(p), "name": p.name, "type": "", "verdict": "", "exists": (p / "drone.json").exists()}
    try:
        spec = json.loads((p / "drone.json").read_text(encoding="utf-8"))
        info["name"] = str(spec.get("name") or p.name)
        info["type"] = str(spec.get("type") or "")
    except (OSError, ValueError):
        pass
    try:
        from opencae.ui.drone.ux import latest_results
        res = latest_results(p)
        if res:
            info["verdict"] = str(res.get("verdict") or "")
    except Exception:  # noqa: BLE001
        pass
    return info


class _Card(QPushButton):
    def __init__(self, title: str, text: str, primary: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumSize(200, 112)
        self.setToolTip(text)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        t = QLabel(title)
        t.setStyleSheet(f"font-size:15px;font-weight:700;background:transparent;"
                        f"color:{theme.ACCENT if primary else theme.FG}")
        d = QLabel(text)
        d.setWordWrap(True)
        d.setStyleSheet(f"color:{theme.FG_DIM};background:transparent")
        for w in (t, d):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay.addWidget(t)
        lay.addWidget(d, 1)
        self.title_label = t


class StartPage(QWidget):
    """What Drone mode shows with no project open: four big cards, the recent projects, how it works."""

    new_requested = Signal()
    open_requested = Signal()
    example_requested = Signal(str)
    recent_open = Signal(str)
    recent_remove = Signal(str)

    CARD_STYLE = (f"QPushButton#card{{background:{theme.BG_2};border:1px solid {theme.BORDER};border-radius:10px;"
                  f"text-align:left;}}QPushButton#card:hover{{border-color:{theme.ACCENT};background:{theme.BG_3};}}")

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)
        host = QWidget()
        scroll.setWidget(host)
        row = QHBoxLayout(host)
        row.addStretch(1)
        body = QWidget()
        body.setMaximumWidth(1000)
        body.setMinimumWidth(560)
        row.addWidget(body, 100)
        row.addStretch(1)
        v = QVBoxLayout(body)
        v.setContentsMargins(24, 28, 24, 24)
        v.setSpacing(14)
        h1 = QLabel("Drone mode")
        h1.setStyleSheet("font-size:26px;font-weight:700")
        sub = QLabel("Turn a CAD file into mass, thrust, strength, air-flow and hover checks. "
                     "Start with one of these, or drop a CAD file or project folder anywhere on this page.")
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{theme.FG_DIM}")
        v.addWidget(h1)
        v.addWidget(sub)

        grid = QHBoxLayout()
        grid.setSpacing(12)
        self.cards: dict = {}
        for key, title, text, sig, primary in (
                ("new", "New drone from CAD", "Pick a STEP, STL or OBJ file. A short wizard checks the parts and "
                 "asks for a few key numbers.", self.new_requested.emit, True),
                ("open", "Open project", "Open a drone project folder you made before (it holds a drone.json).",
                 self.open_requested.emit, False),
                ("quad", "Quad 250 example", "A 5-inch FPV quadcopter with typical numbers. Quick to run.",
                 lambda: self.example_requested.emit("quad250"), False),
                ("k09l", "K09-L example", "The K09-L tailsitter project. A bigger airframe, slower CFD.",
                 lambda: self.example_requested.emit("k09l"), False)):
            c = _Card(title, text, primary)
            c.setStyleSheet(self.CARD_STYLE + (f"QPushButton#card{{border-color:{theme.ACCENT};}}" if primary else ""))
            c.clicked.connect(lambda checked=False, f=sig: f())
            self.cards[key] = c
            grid.addWidget(c)
        v.addLayout(grid)

        rh = QLabel("Recent projects")
        rh.setStyleSheet("font-size:15px;font-weight:700;margin-top:8px")
        v.addWidget(rh)
        self.recent_list = QListWidget()
        self.recent_list.setObjectName("recent")
        self.recent_list.setMinimumHeight(150)
        self.recent_list.setToolTip("Click a project to open it")
        self.recent_list.itemClicked.connect(self._clicked)
        v.addWidget(self.recent_list, 1)
        self.recent_empty = QLabel("Projects you open will show up here.")
        self.recent_empty.setStyleSheet(f"color:{theme.FG_DIM}")
        v.addWidget(self.recent_empty)

        hh = QLabel("How it works")
        hh.setStyleSheet("font-size:15px;font-weight:700;margin-top:8px")
        v.addWidget(hh)
        steps = QHBoxLayout()
        self.steps = []
        for i, (t, d) in enumerate((("Import CAD", "Bring in your airframe as STEP, STL or OBJ."),
                                    ("Fill parameters", "Motor, prop, battery and material numbers. Unknowns can wait."),
                                    ("Run", "Press F5. Results and the verdict appear in the dashboard."))):
            f = QFrame()
            f.setStyleSheet(f"QFrame{{background:{theme.BG_2};border:1px solid {theme.BORDER};border-radius:8px;}}"
                            f"QLabel{{background:transparent;border:none}}")
            fl = QVBoxLayout(f)
            n = QLabel(f"{i + 1}  {t}")
            n.setStyleSheet(f"font-weight:700;color:{theme.ACCENT_2}")
            dl = QLabel(d)
            dl.setWordWrap(True)
            dl.setStyleSheet(f"color:{theme.FG_DIM}")
            fl.addWidget(n)
            fl.addWidget(dl)
            steps.addWidget(f, 1)
            self.steps.append(f)
            if i < 2:
                a = QLabel("›")
                a.setStyleSheet(f"font-size:22px;color:{theme.FG_DIM}")
                steps.addWidget(a)
        v.addLayout(steps)
        v.addStretch(1)

    def set_recent(self, infos: list) -> None:
        self.recent_list.clear()
        for info in infos:
            it = QListWidgetItem()
            it.setData(Qt.UserRole, info["path"])
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(8, 4, 8, 4)
            name = QLabel(info["name"])
            name.setStyleSheet("font-weight:700;background:transparent")
            bits = [b for b in (info["type"].replace("_", " "), info["verdict"] or "no results yet") if b]
            if not info["exists"]:
                bits = ["folder not found"]
            meta = QLabel("  ·  ".join(bits) + f"   {info['path']}")
            meta.setStyleSheet(f"color:{theme.FG_DIM};background:transparent")
            for lab in (name, meta):
                lab.setAttribute(Qt.WA_TransparentForMouseEvents)
            rm = QToolButton()
            rm.setText("Remove")
            rm.setToolTip("Remove from this list (the project itself stays on disk)")
            rm.clicked.connect(lambda checked=False, p=info["path"]: self.recent_remove.emit(p))
            h.addWidget(name)
            h.addWidget(meta, 1)
            h.addWidget(rm)
            it.setSizeHint(w.sizeHint())
            self.recent_list.addItem(it)
            self.recent_list.setItemWidget(it, w)
        self.recent_empty.setVisible(not infos)
        self.recent_list.setVisible(bool(infos))

    def _clicked(self, item):
        path = item.data(Qt.UserRole)
        if path:
            self.recent_open.emit(path)


# ------------------------------------------------------------------------------------------------ workspace
class DroneWorkspace(QWidget):
    """The "Drone" mode of the main window. Emits ``progress(running, fraction, text)`` for the status bar and
    ``project_changed(path)`` ("" when closed)."""

    progress = Signal(bool, float, str)
    project_changed = Signal(str)
    message = Signal(str)

    def __init__(self, parent=None, start_server: bool = True):
        super().__init__(parent)
        self.project_dir: Optional[Path] = None
        self.server = None
        self.url = ""
        self.roles_text = ""
        self._start_server = start_server
        self._was_running = False
        self._nlog = 0
        self._settings: Optional[QSettings] = None
        self.web = None
        self._web_dead = False
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._poll)
        self._make_shortcuts()
        self.setAcceptDrops(True)
        self._enable(False)
        self.refresh_start_page()

    # ------------------------------------------------------------------ settings and recents
    @property
    def settings(self) -> QSettings:
        if self._settings is None:
            self._settings = QSettings("OpenCAE", "OpenCAE Studio")
        return self._settings

    @settings.setter
    def settings(self, value: QSettings) -> None:
        self._settings = value
        self.refresh_start_page()

    def recent_projects(self) -> list:
        """Up to 8 remembered project paths, newest first: [{'path', 'opened'}]."""
        try:
            raw = self.settings.value(RECENT_KEY, "[]")
            items = json.loads(raw) if isinstance(raw, str) else []
        except (ValueError, TypeError):
            items = []
        return [i for i in items if isinstance(i, dict) and i.get("path")][:MAX_RECENT]

    def _save_recent(self, items: list) -> None:
        self.settings.setValue(RECENT_KEY, json.dumps(items[:MAX_RECENT]))
        self.settings.sync()

    def remember_recent(self, path) -> None:
        p = str(Path(path))
        items = [i for i in self.recent_projects() if i["path"] != p]
        items.insert(0, {"path": p, "opened": time.time()})
        self._save_recent(items)
        self.refresh_start_page()

    def forget_recent(self, path) -> None:
        self._save_recent([i for i in self.recent_projects() if i["path"] != str(path)])
        self.refresh_start_page()

    def refresh_start_page(self) -> None:
        self.start.set_recent([project_info(i["path"]) for i in self.recent_projects()])

    # ------------------------------------------------------------------ layout
    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Horizontal)
        root.addWidget(split)

        left = QWidget()
        v = QVBoxLayout(left)
        v.setContentsMargins(6, 6, 6, 6)
        self.title = QLabel("No drone project open")
        f = self.title.font()
        f.setBold(True)
        self.title.setFont(f)
        v.addWidget(self.title)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search parameters (Ctrl+F): motor, battery, density…")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip("Type to show only the matching parameters")
        self.search.textChanged.connect(self._on_search)
        v.addWidget(self.search)
        self.sheet = ParameterSheet()
        self.sheet.edited.connect(self._on_sheet_changed)
        v.addWidget(self.sheet, 1)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        v.addWidget(self.msg)
        bar = QHBoxLayout()
        self.change_label = QLabel("")
        self.change_label.setStyleSheet(f"color:{theme.WARN}")
        bar.addWidget(self.change_label, 1)
        self.revert_btn = QPushButton("Revert")
        self.revert_btn.setToolTip("Drop the changes that are not saved yet")
        self.revert_btn.clicked.connect(lambda checked=False: self.revert_parameters())
        self.save_btn = QPushButton("Save")
        self.save_btn.setToolTip("Write the changed values to drone.json (Ctrl+S)")
        self.save_btn.clicked.connect(lambda checked=False: self.save_parameters())
        self.save_run_btn = QPushButton("Save && run")
        self.save_run_btn.setObjectName("primary")
        self.save_run_btn.setToolTip("Save, then run every analysis (F5)")
        self.save_run_btn.clicked.connect(lambda checked=False: self.run(None))
        for b in (self.revert_btn, self.save_btn, self.save_run_btn):
            bar.addWidget(b)
        v.addLayout(bar)
        left.setMinimumWidth(480)
        split.addWidget(left)

        right = QSplitter(Qt.Vertical)
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(0)
        tv.addWidget(self._action_bar())
        self.pages = QStackedWidget()
        self.start = StartPage()
        self.start.new_requested.connect(self.new_project)
        self.start.open_requested.connect(self.open_dialog)
        self.start.example_requested.connect(self.open_example)
        self.start.recent_open.connect(self.open_recent)
        self.start.recent_remove.connect(self.forget_recent)
        self.pages.addWidget(self.start)                    # 0: no project
        self._dash_page: QWidget
        reason = web_fallback_reason()
        if not reason:
            try:
                from PySide6.QtWebEngineWidgets import QWebEngineView
            except Exception as exc:  # noqa: BLE001 - a broken install falls back to the browser
                reason = f"The built-in web view could not be loaded ({exc}), so the dashboard opens in your browser."
        if not reason:
            self.web = QWebEngineView()
            try:
                self.web.renderProcessTerminated.connect(self._on_render_gone)
            except Exception:  # noqa: BLE001 - older bindings
                pass
            self._dash_page = self.web
        else:
            self._dash_page = self._fallback(reason)
        self.pages.addWidget(self._dash_page)               # 1: dashboard
        tv.addWidget(self.pages, 1)
        right.addWidget(top)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 9))
        self.log.setMaximumBlockCount(1000)
        right.addWidget(self.log)
        right.setStretchFactor(0, 5)
        right.setStretchFactor(1, 1)
        right.setSizes([700, 110])
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([540, 860])

    def _btn(self, text: str, tip: str, slot, primary: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setToolTip(tip)
        b.clicked.connect(lambda checked=False, s=slot: s())
        if primary:
            b.setObjectName("primary")
        return b

    def _action_bar(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(6, 4, 6, 4)
        self.run_btn = self._btn("Run all", "Save and run every analysis (F5)", lambda: self.run(None), True)
        self.run_opts_btn = self._btn("Run…", "Choose analyses and the air-flow quality (Shift+F5)",
                                      lambda: self.run_options())
        self.stop_btn = self._btn("Stop", "Stop after the current step (Esc)", lambda: self.request_stop())
        self.report_btn = self._btn("Export report", "Save an HTML report of the latest results",
                                    lambda: self.export_report())
        self.folder_btn = self._btn("Open project folder", "Show the project folder in your file manager",
                                    lambda: self.open_folder())
        self.copy_btn = self._btn("Copy summary", "Copy the verdict and results as text", lambda: self.copy_summary())
        self.help_btn = self._btn("Shortcuts", "List the keyboard shortcuts (F1)", lambda: self.show_shortcuts())
        for b in (self.run_btn, self.run_opts_btn, self.stop_btn):
            h.addWidget(b)
        h.addSpacing(12)
        for b in (self.report_btn, self.folder_btn, self.copy_btn):
            h.addWidget(b)
        h.addStretch(1)
        h.addWidget(self.help_btn)
        return w

    def _fallback(self, reason: str) -> QWidget:
        fb = QWidget()
        fl = QVBoxLayout(fb)
        fl.addStretch(1)
        head = QLabel("The dashboard opens in your browser")
        head.setAlignment(Qt.AlignCenter)
        head.setStyleSheet("font-size:18px;font-weight:700")
        fl.addWidget(head)
        lab = QLabel(reason)
        lab.setWordWrap(True)
        lab.setAlignment(Qt.AlignCenter)
        lab.setStyleSheet(f"color:{theme.FG_DIM}")
        lab.setMaximumWidth(620)
        fl.addWidget(lab, 0, Qt.AlignCenter)
        btn = QPushButton("Open dashboard in browser")
        btn.setObjectName("primary")
        btn.setToolTip("Open this project's dashboard in your normal web browser")
        btn.clicked.connect(lambda checked=False: self.open_in_browser())
        fl.addWidget(btn, 0, Qt.AlignCenter)
        try:
            from opencae.utilities import crash
            if crash.safe_mode():
                leave = QPushButton("Leave safe mode")
                leave.setToolTip("Use the built-in dashboard view again the next time OpenCAE starts "
                                 "(if this session ends normally)")
                leave.clicked.connect(lambda checked=False: (crash.clear_safe_mode(), leave.setEnabled(False)))
                fl.addWidget(leave, 0, Qt.AlignCenter)
        except Exception:  # noqa: BLE001
            pass
        fl.addStretch(1)
        fb.browser_btn = btn
        fb.reason_label = lab
        self.browser_btn = btn
        self.fallback_label = lab
        return fb

    def _on_render_gone(self, status=None, code=None):
        """The web view's render process died: swap in the fallback panel (never crash)."""
        try:
            import logging
            logging.getLogger("opencae.ui").error("dashboard web view render process terminated (status=%s, code=%s)",
                                                  status, code)
        except Exception:  # noqa: BLE001
            pass
        self._log(f"The built-in dashboard view stopped (status {status}, code {code}); using the browser instead.")
        QTimer.singleShot(0, lambda: self._swap_to_fallback(
            "The built-in dashboard view stopped working, so the dashboard opens in your browser. "
            "Nothing is lost; your project and results are safe."))

    def _swap_to_fallback(self, reason: str) -> None:
        if self.web is None:
            return
        web, self.web, self._web_dead = self.web, None, True
        was_dash = self.pages.currentWidget() is web
        self.pages.removeWidget(web)
        try:
            web.setUrl(QUrl("about:blank"))
        except Exception:  # noqa: BLE001
            pass
        web.deleteLater()
        self._dash_page = self._fallback(reason)
        self.pages.addWidget(self._dash_page)
        if was_dash:
            self.pages.setCurrentWidget(self._dash_page)
        if self.url:
            QDesktopServices.openUrl(QUrl(self.url))

    def _make_shortcuts(self):
        ctx = Qt.WidgetWithChildrenShortcut

        def sc(seq, fn):
            s = QShortcut(QKeySequence(seq), self)
            s.setContext(ctx)
            s.activated.connect(fn)
            return s

        self.shortcuts = {
            "F5": sc("F5", lambda: self.run(None)),
            "Shift+F5": sc("Shift+F5", lambda: self.run_options()),
            "Esc": sc("Esc", lambda: self.request_stop()),
            "Ctrl+N": sc("Ctrl+N", lambda: self.new_project()),
            "Ctrl+O": sc("Ctrl+O", lambda: self.open_dialog()),
            "Ctrl+F": sc("Ctrl+F", lambda: self.focus_search()),
            "Ctrl+S": sc("Ctrl+S", lambda: self.save_parameters()),
            "F1": sc("F1", lambda: self.show_shortcuts()),
        }
        undo = self.sheet.undo_stack.createUndoAction(self, "Undo")
        redo = self.sheet.undo_stack.createRedoAction(self, "Redo")
        undo.setShortcut(QKeySequence("Ctrl+Z"))
        redo.setShortcut(QKeySequence("Ctrl+Y"))
        for a in (undo, redo):
            a.setShortcutContext(ctx)
            self.addAction(a)
        self.undo_action, self.redo_action = undo, redo

    def _enable(self, on: bool):
        for w in (self.save_btn, self.save_run_btn, self.run_btn, self.run_opts_btn, self.stop_btn, self.folder_btn,
                  self.search):
            w.setEnabled(on)
        self.report_btn.setEnabled(on)
        self.copy_btn.setEnabled(on)
        if not on:
            self.revert_btn.setEnabled(False)

    def _log(self, text: str):
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")
        self.message.emit(text)

    def _error(self, text: str):
        self.msg.setStyleSheet(f"color:{theme.ERR}")
        self.msg.setText(text)

    def _ok(self, text: str):
        self.msg.setStyleSheet(f"color:{theme.OK}")
        self.msg.setText(text)

    # ------------------------------------------------------------------ parameter sheet glue
    def focus_search(self):
        if self.project_dir is not None:
            self.search.setFocus()
            self.search.selectAll()

    def _on_search(self, text: str):
        n = self.sheet.set_filter(text)
        if text.strip() and n == 0:
            self.msg.setStyleSheet(f"color:{theme.WARN}")
            self.msg.setText(f"No parameter matches '{text.strip()}'.")
        elif not self.sheet.first_error():
            self.msg.setText("")

    def _on_sheet_changed(self):
        keys = self.sheet.changed_keys()
        err = self.sheet.first_error()
        self.revert_btn.setEnabled(bool(keys) and self.project_dir is not None)
        self.change_label.setText(f"{len(keys)} unsaved change{'s' if len(keys) != 1 else ''}" if keys else "")
        if err:
            self._error(err)
        elif self.msg.styleSheet().find(theme.ERR) >= 0:
            self.msg.setText("")

    def revert_parameters(self):
        self.sheet.revert()
        self._ok("Changes dropped; the sheet shows what is in drone.json.")

    def reload(self) -> None:
        """Re-read drone.json into the sheet (e.g. after the console changed a value). Unsaved edits are dropped."""
        if self.project_dir is None:
            return
        self._reload_sheet()
        self._on_search(self.search.text()) if self.search.text() else None
        self.message.emit("Parameters reloaded from drone.json")

    # ------------------------------------------------------------------ project
    def open_project(self, project_dir, show_roles: bool = False) -> bool:
        project_dir = Path(project_dir)
        if project_dir.is_file():  # a drone.json path
            project_dir = project_dir.parent
        if not (project_dir / "drone.json").exists():
            self._error(f"No drone.json in {project_dir}")
            self.message.emit(f"Not a drone project: {project_dir}")
            return False
        self.close_project()
        self.project_dir = project_dir
        self.title.setText(f"Drone project: {project_dir.name}")
        self.search.clear()
        self._reload_sheet()
        self._enable(True)
        self._log(f"Project opened: {project_dir}")
        try:
            self.remember_recent(project_dir)
        except Exception as e:  # noqa: BLE001 - the recent list is a convenience
            self._log(f"Could not update the recent projects list: {e}")
        if self._start_server:
            try:
                from opencae.web.server import DashboardServer
                self.server = DashboardServer(project_dir, port=0)
                self.url = self.server.start()
                self._log(f"Dashboard: {self.url}")
                if self.web is not None:
                    self.web.setUrl(QUrl(self.url))
                self.pages.setCurrentWidget(self._dash_page)
                if self.web is None and QApplication.platformName() != "offscreen":
                    QDesktopServices.openUrl(QUrl(self.url))  # no built-in view: use the system browser
                self.timer.start()
            except Exception as e:
                self.server = None
                self._error(f"Dashboard server not started: {e}")
                self._log(f"Could not start the dashboard server: {e}")
        self.project_changed.emit(str(project_dir))
        if show_roles:
            self.show_roles()
        return True

    def open_recent(self, path: str) -> bool:
        if not (Path(path) / "drone.json").exists():
            self._error(f"The project folder is gone: {path}. Remove it from the list or restore the folder.")
            return False
        return self.open_project(Path(path))

    def close_project(self):
        self._stop_server()
        self.project_dir = None
        self.url = ""
        self.sheet.clear()
        self.sheet.undo_stack.clear()
        self.title.setText("No drone project open")
        self.msg.setText("")
        self.change_label.setText("")
        self._enable(False)
        self.pages.setCurrentWidget(self.start)
        if self.web is not None:
            self.web.setUrl(QUrl("about:blank"))
        self.progress.emit(False, 0.0, "")
        self.project_changed.emit("")

    def _reload_sheet(self):
        try:
            from opencae.drone import spec as S
            self.sheet.load(S.load(self.project_dir))
        except Exception as e:
            self._error(f"Could not read drone.json: {e}")

    def new_project(self, cad: str = ""):
        from opencae.ui.drone.new_dialog import NewDroneDialog
        dlg = NewDroneDialog(self)
        if cad:
            dlg.set_cad(cad)
        if dlg.exec() == QDialog.Accepted and dlg.project_dir:
            self.open_project(dlg.project_dir, show_roles=True)

    def open_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "Open drone project", str(projects_home()))
        if d:
            self.open_project(Path(d))

    def open_example(self, key: str) -> bool:
        try:
            return self.open_project(copy_example(key))
        except Exception as e:
            self._error(f"Could not open the example: {e}")
            return False

    def show_roles(self):
        """List the parts per role that were guessed from the part names, so the person can check them."""
        from opencae.ui.drone.new_dialog import roles_summary
        self.roles_text = roles_summary(self.project_dir)
        self._log("Roles guessed from the part names:\n" + self.roles_text)
        self._ok("Check the roles guessed from the part names (change them in drone.json):\n" + self.roles_text)

    # ------------------------------------------------------------------ drag and drop
    @staticmethod
    def _drop_paths(mime) -> list:
        return [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()] if mime.hasUrls() else []

    @staticmethod
    def _droppable(p: Path) -> bool:
        return (p.is_dir() and (p / "drone.json").exists()) or p.name == "drone.json" or \
            (p.is_file() and p.suffix.lower() in CAD_SUFFIXES)

    def dragEnterEvent(self, ev):
        if any(self._droppable(p) for p in self._drop_paths(ev.mimeData())):
            ev.acceptProposedAction()
        else:
            ev.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, ev):
        paths = [p for p in self._drop_paths(ev.mimeData()) if self._droppable(p)]
        if paths:
            ev.acceptProposedAction()
            self.handle_drop(paths[0])

    def handle_drop(self, path) -> None:
        """A dropped project folder / drone.json opens; a CAD file starts the New drone wizard with it filled in."""
        p = Path(path)
        if p.is_dir() or p.name == "drone.json":
            self.open_project(p)
        elif p.suffix.lower() in CAD_SUFFIXES:
            self.new_project(cad=str(p))
        else:
            self._error(f"Drop a CAD file (STEP, STL, OBJ) or a drone project folder. Not usable: {p.name}")

    # ------------------------------------------------------------------ parameters
    def save_parameters(self) -> bool:
        """Write edited values and evidence statuses to drone.json. On bad input: red message, file unchanged."""
        from opencae.drone import spec as S
        if self.project_dir is None:
            return False
        try:
            vals, stats = self.sheet.edits()
            cur = S.load(self.project_dir)
            new = S.apply_overrides(cur, vals) if vals else cur
            for k, st in stats.items():
                S.set_status(new, k, st)
            problems = S.validate(new)
            if problems:
                raise ValueError("; ".join(problems))
            if vals or stats:
                S.save(new, self.project_dir / "drone.json")
        except (KeyError, ValueError, TypeError) as e:
            self._error("Not saved: " + (str(e.args[0]) if e.args else str(e)))
            return False
        self._ok("Saved." if (vals or stats) else "Nothing changed.")
        self._reload_sheet()
        if self.search.text():
            self.sheet.set_filter(self.search.text())
        if vals or stats:
            self._log(f"Parameters saved ({len(vals)} value(s), {len(stats)} status(es) changed)")
        return True

    # ------------------------------------------------------------------ runs
    def run(self, only: Optional[list] = None, overrides: Optional[dict] = None, force: bool = False,
            keep_cases: bool = False) -> bool:
        """Save the parameters, then start a run on the dashboard server's job manager. ``overrides`` apply to
        this run only (nothing is written to drone.json)."""
        if not self.server:
            self._log("No dashboard server: cannot start a run.")
            return False
        if not self.save_parameters():
            return False
        body: dict = {}
        if only:
            body["only"] = list(only)
        if overrides:
            body["overrides"] = dict(overrides)
        if force:
            body["force"] = True
        if keep_cases:
            body["keep_cases"] = True
        try:
            self.server.run(body)
            what = ", ".join(only) if only else "all analyses"
            extra = [t for t, on in (("run-only overrides " + json.dumps(overrides) if overrides else "", overrides),
                                     ("forced", force), ("keeping solver files", keep_cases)) if on]
            self._log(f"Run started: {what}" + (f" ({'; '.join(extra)})" if extra else ""))
        except Exception as e:
            self._error(f"Could not start the run: {e}")
            self._log(f"Could not start the run: {e}")
            return False
        self._poll()
        return True

    def build_run_options(self):
        from opencae.drone import spec as S
        from opencae.ui.drone.ux import RunOptionsDialog
        try:
            spec = S.load(self.project_dir) if self.project_dir else {}
        except Exception:  # noqa: BLE001
            spec = {}
        return RunOptionsDialog(spec, self)

    def run_options(self) -> bool:
        """Show the run-options dialog and start the run it describes. False when cancelled or not started."""
        if self.project_dir is None:
            return False
        dlg = self.build_run_options()
        if dlg.exec() != QDialog.Accepted:
            return False
        return self.run_with(dlg.options())

    def run_with(self, opts: dict) -> bool:
        """Start a run from ``RunOptionsDialog.options()``. With "remember" the CFD settings go into drone.json."""
        over = dict(opts.get("overrides") or {})
        if opts.get("remember") and over:
            from opencae.drone import spec as S
            if not self.save_parameters():
                return False
            try:
                S.save(S.apply_overrides(S.load(self.project_dir), over), self.project_dir / "drone.json")
            except (KeyError, ValueError, TypeError) as e:
                self._error("Could not remember the air-flow settings: " + (str(e.args[0]) if e.args else str(e)))
                return False
            self._reload_sheet()
            over = {}
        return self.run(opts.get("only"), over or None, bool(opts.get("force")), bool(opts.get("keep_cases")))

    def is_running(self) -> bool:
        return bool(self.server and self.server.jobs.running)

    def request_stop(self) -> bool:
        """Esc / the Stop button: ask first, then stop. False when nothing runs or the person said no."""
        if not self.is_running():
            return False
        ans = QMessageBox.question(self, "Stop the run?",
                                   "Stop the running analysis? The step in progress finishes first.",
                                   QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ans != QMessageBox.Yes:
            return False
        self.stop()
        return True

    def stop(self):
        if self.server:
            self.server.stop_job()
            self._log("Stop requested; the running step finishes first.")

    def _poll(self):
        if not self.server:
            return
        p = self.server.jobs.snapshot()
        running = bool(p.get("running"))
        frac = float(p.get("frac") or 0)
        self.progress.emit(running, frac, str(p.get("step", "")))
        log = p.get("log") or []
        if len(log) < self._nlog:
            self._nlog = 0
        for line in log[self._nlog:]:
            self.log.appendPlainText(str(line))
        self._nlog = len(log)
        if self._was_running and not running:
            err = p.get("error")
            self._log(f"Run stopped: {err}" if err else "Run finished.")
            if not self.sheet.is_dirty():
                self._reload_sheet()
            self.refresh_start_page()
            if self.web is not None:
                self.web.reload()
        self._was_running = running

    # ------------------------------------------------------------------ results actions
    def export_report(self, path=None):
        """Save an HTML report of the latest results. ``path`` None asks with a file dialog. Returns the path."""
        if self.project_dir is None:
            return None
        from opencae.ui.drone import ux
        if ux.latest_results(self.project_dir) is None:
            self._error("There are no results yet. Run the analyses first, then export the report.")
            return None
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "Export report", str(self.project_dir / "report.html"),
                                                  "HTML report (*.html)")
            if not path:
                return None
        try:
            out = ux.export_report(self.project_dir, path)
        except OSError as e:
            self._error(f"Could not write the report: {e}")
            return None
        self._ok(f"Report saved: {out}")
        self._log(f"Report exported: {out}")
        return out

    def open_folder(self):
        if self.project_dir is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.project_dir)))

    def summary(self) -> str:
        from opencae.ui.drone import ux
        if self.project_dir is None:
            return ""
        return ux.summary_text(ux.latest_results(self.project_dir), self.project_dir.name)

    def copy_summary(self) -> str:
        text = self.summary()
        if text:
            QApplication.clipboard().setText(text)
            self._ok("Summary copied to the clipboard.")
        return text

    def show_shortcuts(self):
        from opencae.ui.drone.ux import ShortcutsDialog
        dlg = getattr(self, "_shortcuts_dlg", None)
        if dlg is None:  # non-modal: it never blocks the app (or a test) and is reused
            dlg = self._shortcuts_dlg = ShortcutsDialog(self)
        dlg.show()
        dlg.raise_()

    def open_in_browser(self):
        if self.url:
            QDesktopServices.openUrl(QUrl(self.url))

    # ------------------------------------------------------------------ closing
    def _stop_server(self):
        self.timer.stop()
        self._nlog = 0
        self._was_running = False
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:
                pass
            self.server = None

    def shutdown(self):
        """Stop the server and drop the web view (called when the app closes)."""
        self._stop_server()
        if self.web is not None:
            self.web.setUrl(QUrl("about:blank"))

    def closeEvent(self, ev):
        self.shutdown()
        super().closeEvent(ev)
