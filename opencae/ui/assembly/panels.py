"""Assembly tree and the property cards (inspectors) of Assembly mode.

The tree is Assembly > Bodies (> Components) / Joints / Couplings / Drivers / Applied loads / Studies. Selecting an
item shows its card on the right: a body card with mass, centre of mass, inertia, local frame and where the numbers
come from; a joint card; a motor card; and so on. Cards are plain read-outs with the few buttons that act on the item.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from opencae.assembly import GROUND, get_mbd
from opencae.ui import theme

MINUS = "−"


def fnum(v: float, sig: int = 4) -> str:
    """Compact number: ``-0.0`` -> ``0``, ``3.0`` -> ``3``, no exponent for ordinary sizes."""
    if abs(v) < 1e-12:
        return "0"
    return f"{v:.{sig}g}"


def fvec(v, sig: int = 4) -> str:
    return "(" + ", ".join(fnum(float(x), sig) for x in v) + ")"


# ------------------------------------------------------------------------------------------------ icons
_ICONS: dict = {}


def kind_icon(kind: str, flag: str = "") -> QIcon:
    key = (kind, flag)
    if key in _ICONS:
        return _ICONS[key]
    S = 18
    pm = QPixmap(S, S)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    blue, amber, green, grey = QColor(theme.ACCENT), QColor(theme.ACCENT_2), QColor(theme.OK), QColor("#9aa3b2")
    if kind in ("assembly", "bodies"):
        p.setPen(QPen(grey, 1.4))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(3, 6, 12, 8))
        p.drawRect(QRectF(5, 3, 8, 5))
    elif kind == "body":
        if flag == "grounded":  # a block on a hatched base: fixed to the world
            p.setPen(QPen(blue, 1.4))
            p.setBrush(QBrush(QColor(blue.red(), blue.green(), blue.blue(), 90)))
            p.drawRect(QRectF(4, 3, 10, 8))
            p.setPen(QPen(grey, 1.4))
            p.drawLine(QPointF(2, 14), QPointF(16, 14))
            for x in (3, 6.5, 10, 13.5):
                p.drawLine(QPointF(x, 14), QPointF(x - 2, 17))
        else:
            p.setPen(QPen(blue, 1.4))
            p.setBrush(QBrush(QColor(blue.red(), blue.green(), blue.blue(), 90)))
            p.drawRect(QRectF(3, 4, 12, 10))
    elif kind == "component":
        p.setPen(QPen(grey, 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(4, 5, 10, 8))
        p.drawLine(QPointF(4, 5), QPointF(7, 3))
        p.drawLine(QPointF(14, 5), QPointF(17, 3))
    elif kind == "joints":
        p.setPen(QPen(amber, 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(4, 4, 10, 10))
    elif kind == "joint":
        if flag == "revolute":
            p.setPen(QPen(amber, 1.6))
            p.setBrush(Qt.NoBrush)
            p.drawArc(QRectF(3, 3, 12, 12), 40 * 16, 270 * 16)
            p.setBrush(QBrush(amber))
            p.drawPolygon(QPolygonF([QPointF(15.5, 4.5), QPointF(16.5, 9.5), QPointF(11.5, 8.5)]))
        elif flag == "prismatic":
            p.setPen(QPen(green, 1.6))
            p.drawLine(QPointF(3, 9), QPointF(15, 9))
            p.setBrush(QBrush(green))
            p.drawPolygon(QPolygonF([QPointF(1.5, 9), QPointF(5, 6), QPointF(5, 12)]))
            p.drawPolygon(QPolygonF([QPointF(16.5, 9), QPointF(13, 6), QPointF(13, 12)]))
        else:  # fixed
            p.setPen(QPen(grey, 1.4))
            p.setBrush(QBrush(grey))
            p.drawEllipse(QRectF(5.5, 5.5, 7, 7))
            p.drawLine(QPointF(3, 3), QPointF(6, 6))
            p.drawLine(QPointF(15, 15), QPointF(12, 12))
    elif kind in ("couplings", "coupling"):
        p.setPen(QPen(amber, 1.4))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(2, 5, 8, 8))
        p.drawEllipse(QRectF(8, 5, 8, 8))
    elif kind in ("drivers", "driver"):
        p.setPen(QPen(blue, 1.4))
        p.setBrush(QBrush(QColor(blue.red(), blue.green(), blue.blue(), 120)))
        p.drawEllipse(QRectF(3, 3, 12, 12))
        p.setBrush(QBrush(blue))
        p.drawEllipse(QRectF(7, 7, 4, 4))
    elif kind in ("loads", "load"):
        p.setPen(QPen(QColor(theme.ERR), 1.8))
        p.drawLine(QPointF(9, 2), QPointF(9, 14))
        p.setBrush(QBrush(QColor(theme.ERR)))
        p.drawPolygon(QPolygonF([QPointF(9, 17), QPointF(5.5, 11.5), QPointF(12.5, 11.5)]))
    elif kind in ("studies", "study"):
        p.setPen(QPen(green if flag == "result" else grey, 1.6))
        p.setBrush(Qt.NoBrush)
        p.drawPolyline(QPolygonF([QPointF(2, 13), QPointF(6, 8), QPointF(10, 11), QPointF(16, 4)]))
        p.drawLine(QPointF(2, 16), QPointF(16, 16))
    p.end()
    ic = QIcon(pm)
    _ICONS[key] = ic
    return ic


# ------------------------------------------------------------------------------------------------ tree
class AssemblyTree(QTreeWidget):
    """Assembly structure. ``selected(kind, id)`` is emitted when the USER selects an item; ``select_item`` can
    change the selection silently (``emit=False``)."""

    selected = Signal(str, str)
    context_requested = Signal(str, str, object)
    GROUPS = (("bodies", "Bodies"), ("joints", "Joints"), ("couplings", "Couplings"), ("drivers", "Drivers (motors)"),
              ("loads", "Applied loads"), ("studies", "Studies (motion)"))

    def __init__(self) -> None:
        super().__init__()
        self.setHeaderHidden(True)
        self.setIndentation(14)
        self.setUniformRowHeights(True)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.itemSelectionChanged.connect(self._on_changed)
        self._quiet = False
        self._items: dict[tuple[str, str], QTreeWidgetItem] = {}
        self._grounded: dict[str, bool] = {}

    # ---- building --------------------------------------------------------
    def rebuild(self, ws) -> None:
        keep = self.current()
        self._quiet = True
        expanded = {k for k, it in self._items.items() if it.isExpanded()}
        first = not self._items
        self.clear()
        self._items.clear()
        self._grounded.clear()
        asm = ws.assembly
        root = self._add(None, "assembly", "", f"Assembly — {ws.project.name}")
        groups = {k: self._add(root, k, "", name) for k, name in self.GROUPS}
        for b in asm.bodies:
            self._grounded[b.id] = b.grounded
            txt = b.name + ("  (grounded)" if b.grounded else "")
            bi = self._add(groups["bodies"], "body", b.id, txt, "grounded" if b.grounded else "")
            for cid in b.component_ids:
                c = asm.component(cid)
                if c is not None:
                    self._add(bi, "component", c.id, c.name or c.id)
        for j in asm.joints:
            par = "ground" if j.parent == GROUND else (asm.body(j.parent).name if asm.body(j.parent) else "?")
            ch = asm.body(j.child).name if asm.body(j.child) else "?"
            self._add(groups["joints"], "joint", j.id, f"{j.name} — {j.kind} ({par} → {ch})", j.kind)
        for c in asm.couplings:
            self._add(groups["couplings"], "coupling", c.id, f"{c.name} ({ws.coupling_formula(c)})")
        study = ws.study
        if study is not None:
            S = get_mbd(study)
            for d in S.drivers:
                self._add(groups["drivers"], "driver", d.id, f"{d.name} — {'prescribed' if d.prescribed else 'dynamic'} "
                          f"{ws.mode_word(d)}")
            for ld in S.loads:
                self._add(groups["loads"], "load", ld.id, ld.name or "Applied load")
        for st in ws.mbd_studies():
            done = bool(st.results.completed)
            self._add(groups["studies"], "study", st.id,
                      st.name + ("  — current" if study is not None and st.id == study.id else "")
                      + ("  ✓" if done else ""), "result" if done else "")
        for k in groups:
            groups[k].setText(0, f"{dict(self.GROUPS)[k]} ({groups[k].childCount()})")
        root.setExpanded(True)
        for k, g in groups.items():
            g.setExpanded(first or (k, "") in expanded or k in ("bodies", "joints", "drivers", "couplings"))
        for bi in range(groups["bodies"].childCount()):
            groups["bodies"].child(bi).setExpanded(False)
        self._quiet = False
        if keep and keep in self._items:
            self.select_item(*keep, emit=False)

    def _add(self, parent, kind: str, ident: str, text: str, flag: str = "") -> QTreeWidgetItem:
        it = QTreeWidgetItem([text])
        it.setData(0, Qt.UserRole, (kind, ident))
        it.setIcon(0, kind_icon(kind, flag))
        if parent is None:
            self.addTopLevelItem(it)
        else:
            parent.addChild(it)
        self._items[(kind, ident)] = it
        if ident == "" and kind not in ("assembly",):
            self._items[(kind, "")] = it
        return it

    # ---- queries ---------------------------------------------------------
    def count(self, kind: str) -> int:
        return sum(1 for (k, i), it in self._items.items() if k == kind and i != "")

    def text_of(self, kind: str, ident: str) -> str:
        it = self._items.get((kind, ident))
        return it.text(0) if it is not None else ""

    def is_grounded(self, body_id: str) -> bool:
        return bool(self._grounded.get(body_id))

    def current(self) -> Optional[tuple[str, str]]:
        its = self.selectedItems()
        if not its:
            return None
        return tuple(its[0].data(0, Qt.UserRole))

    # ---- selection ---------------------------------------------------------
    def select_item(self, kind: str, ident: str, emit: bool = False) -> bool:
        it = self._items.get((kind, ident))
        if it is None:
            return False
        prev, self._quiet = self._quiet, not emit
        try:
            self.clearSelection()
            self.setCurrentItem(it)
            it.setSelected(True)
            p = it.parent()
            while p is not None:
                p.setExpanded(True)
                p = p.parent()
            self.scrollToItem(it)
        finally:
            self._quiet = prev
        return True

    def clear_selection_quiet(self) -> None:
        prev, self._quiet = self._quiet, True
        self.clearSelection()
        self._quiet = prev

    def _on_changed(self) -> None:
        if self._quiet:
            return
        cur = self.current()
        if cur is not None:
            self.selected.emit(*cur)

    def _menu(self, pos) -> None:
        it = self.itemAt(pos)
        if it is None:
            return
        kind, ident = it.data(0, Qt.UserRole)
        self.context_requested.emit(kind, ident, self.viewport().mapToGlobal(pos))


# ------------------------------------------------------------------------------------------------ cards
class PropertiesPanel(QScrollArea):
    """The card of the selected item. ``value(name)`` returns the text of a row (used by tests and tooltips)."""

    def __init__(self, ws) -> None:
        super().__init__()
        self.ws = ws
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._values: dict[str, QWidget] = {}
        self._holder: Optional[QWidget] = None
        self.show_item("assembly", "")

    # ---- row helpers ---------------------------------------------------------
    def value(self, name: str) -> str:
        w = self._values.get(name)
        if w is None:
            return ""
        if isinstance(w, QCheckBox):
            return "yes" if w.isChecked() else "no"
        if isinstance(w, QLabel):
            return w.text()
        return ""

    def _new(self) -> QVBoxLayout:
        self._values = {}
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)
        self._holder = holder
        self.setWidget(holder)
        return lay

    def _title(self, lay, text: str, sub: str = "") -> None:
        t = QLabel(f"<b style='font-size:14px'>{_esc(text)}</b>")
        t.setWordWrap(True)
        lay.addWidget(t)
        if sub:
            s = QLabel(sub)
            s.setObjectName("hint")
            s.setWordWrap(True)
            lay.addWidget(s)

    def _grid(self, lay) -> QGridLayout:
        g = QGridLayout()
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(4)
        g.setColumnStretch(1, 1)
        lay.addLayout(g)
        return g

    def _row(self, grid: QGridLayout, label: str, text: str, name: str, tip: str = "") -> QLabel:
        r = grid.rowCount()
        k = QLabel(label)
        k.setObjectName("hint")
        k.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        v = QLabel(text)
        v.setObjectName(f"v_{name}")
        v.setWordWrap(True)
        v.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if tip:
            v.setToolTip(tip)
        grid.addWidget(k, r, 0, Qt.AlignTop)
        grid.addWidget(v, r, 1)
        self._values[name] = v
        return v

    def _buttons(self, lay, specs) -> None:
        row = QHBoxLayout()
        for text, slot, tip in specs:
            b = QPushButton(text)
            if tip:
                b.setToolTip(tip)
            b.clicked.connect(lambda _=False, s=slot: s())
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

    def _note(self, lay, text: str, kind: str = "hint") -> QLabel:
        lab = QLabel(text)
        lab.setObjectName(kind)
        lab.setWordWrap(True)
        lay.addWidget(lab)
        return lab

    # ---- dispatch -----------------------------------------------------------
    def refresh(self) -> None:
        sel = self.ws.selected
        self.show_item(*sel) if sel else self.show_item("assembly", "")

    def show_item(self, kind: str, ident: str) -> None:
        fn = getattr(self, f"_card_{kind}", None)
        lay = self._new()
        if fn is None or not fn(lay, ident):
            self._card_assembly(lay, "")
        lay.addStretch(1)

    # ---- cards ---------------------------------------------------------------
    def _card_assembly(self, lay, _ident) -> bool:
        ws = self.ws
        asm = ws.assembly
        self._title(lay, f"Assembly — {ws.project.name}")
        g = self._grid(lay)
        self._row(g, "Bodies", str(len(asm.bodies)), "n_bodies")
        self._row(g, "Joints", str(len(asm.joints)), "n_joints")
        self._row(g, "Couplings", str(len(asm.couplings)), "n_couplings")
        if ws.study is not None:
            self._row(g, "Current study", ws.study.name, "study")
        probs = ws.model_problems()
        if probs:
            self._note(lay, "Model check — things to fix before running:", "warning")
            for p in probs:
                self._note(lay, "• " + p, "error")
        else:
            lab = self._note(lay, "Model check: no problems found.")
            lab.setStyleSheet(f"color:{theme.OK};")
        self._buttons(lay, [("Add joint…", ws.add_joint, "Connect two bodies"),
                            ("Add motor…", ws.add_driver, "Command a joint")])
        self._note(lay, "Click a part in the 3-D view or an item in the tree to see its properties.")
        return True

    def _group_card(self, lay, title: str, text: str, buttons) -> bool:
        self._title(lay, title)
        self._note(lay, text)
        if buttons:
            self._buttons(lay, buttons)
        return True

    def _card_bodies(self, lay, _i) -> bool:
        return self._group_card(lay, "Bodies", "A body is a rigid piece: one or more components that move together. "
                                "Grounded bodies are fixed to the world.", [])

    def _card_joints(self, lay, _i) -> bool:
        return self._group_card(lay, "Joints", "Joints say how bodies may move relative to each other.",
                                [("Add joint…", self.ws.add_joint, "")])

    def _card_couplings(self, lay, _i) -> bool:
        return self._group_card(lay, "Couplings", "A coupling ties one joint to another with a fixed ratio "
                                "(an ideal transmission).", [])

    def _card_drivers(self, lay, _i) -> bool:
        return self._group_card(lay, "Drivers (motors)", "A motor commands one joint: a prescribed motion, or a "
                                "torque / force that the dynamics respond to.",
                                [("Add motor…", self.ws.add_driver, "")])

    def _card_loads(self, lay, _i) -> bool:
        return self._group_card(lay, "Applied loads", "Explicit forces on bodies, for example a fingertip load. "
                                "Nothing is inferred: contact is not modelled.", [])

    def _card_studies(self, lay, _i) -> bool:
        return self._group_card(lay, "Studies (motion)", "Each study has its own motors, loads, gravity and times.",
                                [])

    def _card_body(self, lay, ident) -> bool:
        ws = self.ws
        asm = ws.assembly
        b = asm.body(ident)
        if b is None:
            return False
        u = ws.length_unit
        self._title(lay, b.name, "Rigid body")
        cb = QCheckBox("Ground (fixed to the world)")
        cb.setObjectName("v_grounded")
        cb.setChecked(b.grounded)
        cb.setToolTip("A grounded body does not move. Every moving body must be connected to the ground "
                      "through joints.")
        cb.toggled.connect(lambda on, bid=b.id: self._ground_toggled(bid, on))
        self._values["grounded"] = cb
        lay.addWidget(cb)
        g = self._grid(lay)
        comps = [asm.component(c) for c in b.component_ids]
        self._row(g, "Components", ", ".join(c.name for c in comps if c) or "none", "components")
        mp = b.effective_mass()
        if mp is None:
            self._row(g, "Mass", "not available yet", "mass")
            self._note(lay, "No mass: assign a material with a density to the component(s) in the Studio, or use "
                       "“Set manual values…”.", "warning")
        else:
            self._row(g, "Mass", f"{mp.mass_kg:.4g} kg", "mass")
            self._row(g, "Centre of mass", f"{fvec(mp.com)} {u}", "com", "in the assembly frame")
            I = mp.inertia_matrix()
            ig = QGridLayout()
            ig.setHorizontalSpacing(12)
            for r in range(3):
                for c in range(3):
                    lab = QLabel(f"{I[r, c]:.4g}")
                    lab.setAlignment(Qt.AlignRight)
                    lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
                    ig.addWidget(lab, r, c)
            k = QLabel("Inertia")
            k.setObjectName("hint")
            r0 = g.rowCount()
            g.addWidget(k, r0, 0, Qt.AlignTop)
            g.addLayout(ig, r0, 1)
            self._row(g, "", "kg·m² about the centre of mass, in the assembly axes", "inertia_unit")
            self._row(g, "Local frame", "Body frame: origin at the centre of mass, axes parallel to the assembly "
                      "axes in the reference pose (all joint coordinates zero).", "frame")
            self._row(g, "Source", ws.mass_source_text(b), "source")
        btns = [("Set manual values…", lambda bid=b.id: ws.set_manual_mass(bid),
                 "Enter mass, centre of mass and inertia by hand (a purchased motor, for example)"),
                ("Group with…", lambda bid=b.id: ws.group_with(bid), "Merge other bodies into this one")]
        if b.manual is not None:
            btns.append(("Use CAD values", lambda bid=b.id: ws.clear_manual_mass(ws.assembly.body(bid)),
                         "Remove the manual override"))
        self._buttons(lay, btns)
        return True

    def _ground_toggled(self, bid: str, on: bool) -> None:
        ok, why = self.ws.set_grounded(bid, on)
        if not ok:
            cb = self._values.get("grounded")
            if cb is not None:
                cb.blockSignals(True)
                cb.setChecked(not on)
                cb.blockSignals(False)
            self.ws.warn("Cannot change grounding", why)

    def _card_component(self, lay, ident) -> bool:
        ws = self.ws
        asm = ws.assembly
        c = asm.component(ident)
        if c is None:
            return False
        self._title(lay, c.name or c.id, "Component (a placed part)")
        g = self._grid(lay)
        rb = asm.body_of_component(c.id)
        self._row(g, "Belongs to body", rb.name if rb else "none", "body")
        P = c.placement_matrix()
        self._row(g, "Placement", f"offset {fvec(P[:3, 3])} {ws.length_unit}"
                  + ("" if np.allclose(P[:3, :3], np.eye(3)) else ", rotated"), "placement")
        self._row(g, "Geometry", ws.project.geometry.body_names.get(c.body_id, c.body_id), "geometry")
        if c.source:
            self._row(g, "Source", c.source, "source")
        self._note(lay, "The animation only moves the picture of the part; the CAD and its placement are never "
                   "changed.")
        return True

    def _card_joint(self, lay, ident) -> bool:
        ws = self.ws
        asm = ws.assembly
        j = asm.joint(ident)
        if j is None:
            return False
        u = ws.length_unit
        self._title(lay, j.name, f"{j.kind.capitalize()} joint")
        g = self._grid(lay)
        par = "Ground" if j.parent == GROUND else (asm.body(j.parent).name if asm.body(j.parent) else "?")
        self._row(g, "Parent → child", f"{par} → {asm.body(j.child).name if asm.body(j.child) else '?'}",
                  "bodies")
        self._row(g, "Origin", f"{fvec(j.origin)} {u}", "origin")
        if j.kind != "fixed":
            z = j.z_axis()
            self._row(g, "Axis", f"{fvec(z, 3)}" + ("  (reversed)" if j.reversed else ""), "axis",
                      "positive motion: right-hand rotation about / sliding along this direction")
            if j.limits is not None:
                f = 180 / math.pi if j.kind == "revolute" else 1.0
                un = "deg" if j.kind == "revolute" else u
                self._row(g, "Travel limits", f"{j.limits[0] * f:.4g} to {j.limits[1] * f:.4g} {un}", "limits")
            else:
                self._row(g, "Travel limits", "none", "limits")
            f = 180 / math.pi if j.kind == "revolute" else 1.0
            un = ("deg", "deg/s") if j.kind == "revolute" else (u, f"{u}/s")
            self._row(g, "Starts at", f"{j.initial_q * f:.4g} {un[0]}, speed {j.initial_qd * f:.4g} {un[1]}",
                      "initial")
        drv = ws.driver_of(j.id)
        fol = next((c for c in asm.couplings if c.follower == j.id), None)
        if drv is not None:
            self._row(g, "Motor", f"{drv.name} — "
                      f"{'prescribed (follows exactly)' if drv.prescribed else 'dynamic (motion computed)'}", "driver")
        elif fol is not None:
            self._row(g, "Motion", f"moved by the transmission '{fol.name}' ({ws.coupling_formula(fol)})", "driver")
        elif j.kind != "fixed":
            self._row(g, "Motor", "none (moves freely)", "driver")
        btns = [("Edit…", lambda: ws.edit_joint(j.id), "")]
        if j.kind != "fixed" and drv is None and fol is None:
            btns.append(("Add motor…", lambda: ws.add_driver(j.id), ""))
        btns.append(("Delete", lambda: ws.delete_joint(j.id), "Remove this joint (and its motors)"))
        self._buttons(lay, btns)
        return True

    def _card_coupling(self, lay, ident) -> bool:
        ws = self.ws
        c = ws.assembly.coupling(ident) if hasattr(ws.assembly, "coupling") else \
            next((x for x in ws.assembly.couplings if x.id == ident), None)
        if c is None:
            return False
        self._title(lay, c.name, "Coupling (ideal transmission)")
        g = self._grid(lay)
        lead, fol = ws.assembly.joint(c.leader), ws.assembly.joint(c.follower)
        self._row(g, "Leader joint", lead.name if lead else "?", "leader")
        self._row(g, "Follower joint", fol.name if fol else "?", "follower")
        self._row(g, "Ratio and sign", f"{ws.ratio_text(c.ratio)}  (follower = ratio × leader)", "ratio")
        self._row(g, "Transmission", ws.coupling_formula(c, capital=True), "transmission")
        self._row(g, "Idealisation", c.note or "Ideal transmission: massless, rigid, no backlash or friction.",
                  "note")
        self._note(lay, "The follower has no motor of its own: the constraint moves it. The effort reported for "
                   "the leader includes what the transmission carries.")
        return True

    def _card_driver(self, lay, ident) -> bool:
        ws = self.ws
        S = get_mbd(ws.study) if ws.study else None
        d = next((x for x in (S.drivers if S else []) if x.id == ident), None)
        if d is None:
            return False
        j = ws.assembly.joint(d.joint)
        self._title(lay, d.name, "Prescribed motion" if d.prescribed else "Dynamic (torque / force)")
        g = self._grid(lay)
        self._row(g, "Joint", j.name if j else "?", "joint")
        self._row(g, "Kind", ("Prescribed motion — the joint follows this exactly" if d.prescribed else
                              "Dynamic — the motion is computed"), "kind")
        self._row(g, "Command", ws.mode_word(d, long=True), "mode")
        self._row(g, "Profile", ws.profile_text(d), "profile")
        self._buttons(lay, [("Edit…", lambda: ws.edit_driver(d.id), ""),
                            ("Delete", lambda: ws.delete_driver(d.id), "")])
        return True

    def _card_load(self, lay, ident) -> bool:
        ws = self.ws
        S = get_mbd(ws.study) if ws.study else None
        ld = next((x for x in (S.loads if S else []) if x.id == ident), None)
        if ld is None:
            return False
        rb = ws.assembly.body(ld.body)
        self._title(lay, ld.name or "Applied load", "Explicit external load")
        g = self._grid(lay)
        self._row(g, "On body", rb.name if rb else "?", "body")
        self._row(g, "Point", f"{fvec(ld.point)} {ws.length_unit}", "point")
        self._row(g, "Force", f"{fvec(ld.force_N)} N", "force")
        self._row(g, "Moment", f"{fvec(ld.moment_Nm)} N·m", "moment")
        self._row(g, "Direction frame", "turns with the body" if ld.frame == "body" else "fixed in the assembly", "frame")
        if ld.note:
            self._row(g, "Note", ld.note, "note")
        return True

    def _card_study(self, lay, ident) -> bool:
        ws = self.ws
        st = ws.project.study_by_id(ident)
        if st is None:
            return False
        S = get_mbd(st)
        self._title(lay, st.name, "Motion study")
        g = self._grid(lay)
        gv = np.asarray(S.gravity, float)
        self._row(g, "Gravity", "off" if np.allclose(gv, 0) else f"{fvec(gv)} m/s²", "gravity")
        self._row(g, "Duration", f"{S.duration_s:g} s", "duration")
        self._row(g, "Time step", f"{S.dt_s:g} s (output every {S.output_dt_s:g} s)", "dt")
        self._row(g, "Motors", ", ".join(d.name for d in S.drivers) or "none", "drivers")
        self._row(g, "Results", "ready" if st.results.completed else "not run yet", "results")
        if st.notes:
            self._note(lay, st.notes[:600] + ("…" if len(st.notes) > 600 else ""))
        btns = [("Make current", lambda: ws.set_study(st.id), ""),
                ("Settings…", lambda: ws.edit_study_settings(st.id), "")]
        self._buttons(lay, btns)
        return True


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
