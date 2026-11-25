"""K09-L Design Monitor: an application to create design variants, monitor every variant against the same
requirements, gates, metrics and analyses, run the solvers per variant, and export an HTML dashboard.

Data: opencae.engineering.study.Study (one engine; variants = model revisions). The engine is used from one
thread at a time: while a solver job runs, the window shows cached snapshots and blocks edits.
"""
from __future__ import annotations

import html
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem,
                               QTabWidget, QTextBrowser, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from opencae.engineering import approval as AP
from opencae.engineering import catalog as C
from opencae.engineering import study as ST
from opencae.engineering.graph import CURRENT
from opencae.engineering.quantities import Q

STATUS_COL = {AP.PASS: ("#2f7d4a", "#dff1e5"), AP.COND: ("#8a5d00", "#fbf0d4"), AP.FAIL: ("#b3302b", "#f9e1df"),
              AP.NOT_EV: ("#5d6a75", "#e9edf0"), "current": ("#2f7d4a", "#dff1e5"), "stale": ("#8a5d00", "#fbf0d4"),
              "not run": ("#5d6a75", "#e9edf0"), "failed": ("#b3302b", "#f9e1df")}
ICON = {AP.PASS: "✓", AP.COND: "◐", AP.FAIL: "✕", AP.NOT_EV: "○"}
SHORT = {AP.PASS: "Pass", AP.COND: "Cond.", AP.FAIL: "Fail", AP.NOT_EV: "Not eval."}
EV = {"user_input": "input", "supplier_spec": "supplier", "analytical": "analytical", "simulation": "simulation",
      "reduced_order": "reduced-order", "test_measurement": "measured", "provisional_estimate": "estimate",
      "missing": "missing"}


class _Worker(QObject):
    progress = Signal(str, int, int)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            res = self.fn(lambda s, i, n: self.progress.emit(s, i, n))
        except Exception:
            self.failed.emit(traceback.format_exc())
            return
        self.done.emit(res)


# ---------------------------------------------------------------------------------------------- dialogs
class VariantEditor(QDialog):
    """Edit a variant: catalog selections and design parameters (range-checked; evidence class kept unless
    re-classified)."""

    CLASSES = [("keep", "keep evidence class"), ("user_input", "user input"), ("supplier_spec", "supplier data"),
               ("provisional_estimate", "estimate")]

    def __init__(self, study: ST.Study, rid: str, parent=None):
        super().__init__(parent)
        self.study, self.rid = study, rid
        v = study.variant(rid)
        self.setWindowTitle(f"Edit variant - {v['name']}")
        self.resize(760, 760)
        lay = QVBoxLayout(self)
        top = QFormLayout()
        self.name = QLineEdit(v["name"])
        self.desc = QLineEdit(v.get("description", ""))
        top.addRow("Name", self.name)
        top.addRow("Description", self.desc)
        lay.addLayout(top)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        bl = QVBoxLayout(body)
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)
        m = study.engine.model
        cfg = v["config"]
        g = QGroupBox("Components and configuration (catalog)")
        f = QFormLayout(g)
        self.combos = {}
        for kind in C.KINDS:
            cb = QComboBox()
            for o in m.catalog[kind]:
                cb.addItem(o.name + ("" if o.compatible else "  (not offered)"), o.id)
                i = cb.count() - 1
                if not o.compatible:
                    cb.model().item(i).setEnabled(False)
                    cb.setItemData(i, o.incompatible_reason, Qt.ToolTipRole)
                elif o.notes:
                    cb.setItemData(i, o.notes, Qt.ToolTipRole)
            cb.setCurrentIndex(max(cb.findData(cfg.get(kind)), 0))
            self.combos[kind] = cb
            f.addRow(kind, cb)
        bl.addWidget(g)
        self.spins, self.cls, self.missing = {}, {}, {}
        params = {k: Q.from_dict(d) for k, d in v["params"].items()}
        groups: dict[str, QFormLayout] = {}
        for key, label, group in ST.EDITABLE:
            q = params.get(key) or m.params_default.get(key)
            if q is None:
                continue
            if group not in groups:
                gb = QGroupBox(group)
                groups[group] = QFormLayout(gb)
                bl.addWidget(gb)
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            sp = QDoubleSpinBox()
            lo, hi = ST.PARAM_RANGES.get(key, (0.0, 1e6))
            sp.setRange(lo, hi)
            sp.setDecimals(4 if hi <= 2 else 2)
            sp.setSingleStep((hi - lo) / 100 if hi < 1e5 else 1.0)
            sp.setValue(float(q.value) if q.value is not None else lo)
            sp.setSuffix(f" {q.unit}" if q.unit and q.unit != "-" else "")
            sp.setToolTip(f"{q.ref}\nEvidence: {EV.get(q.source, q.source)}" + (f"\n{q.note}" if q.note else ""))
            rl.addWidget(sp, 1)
            if q.value is None or key == "shipping":
                mc = QCheckBox("missing")
                mc.setChecked(q.value is None)
                mc.toggled.connect(sp.setDisabled)
                sp.setDisabled(q.value is None)
                self.missing[key] = mc
                rl.addWidget(mc)
            cc = QComboBox()
            for cid, ct in self.CLASSES:
                cc.addItem(ct, cid)
            cc.setToolTip(f"current evidence class: {EV.get(q.source, q.source)}")
            rl.addWidget(cc)
            ev = QLabel(EV.get(q.source, q.source))
            ev.setMinimumWidth(80)
            ev.setStyleSheet("color:#8a5d00" if q.source in ("provisional_estimate", "missing") else "color:#4b5966")
            rl.addWidget(ev)
            groups[group].addRow(label, row)
            self.spins[key], self.cls[key] = (sp, q), cc
        note = QLabel("Geometry parameters act only when their option is selected (feet_b: outrigger, shortening; "
                      "inboard_b: motor sets inboard). Edited values keep their evidence class unless you re-classify "
                      "them. A supplier or test value needs a reference.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#5d6a75")
        bl.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.result_changes = None

    def _ok(self):
        opts = {k: cb.currentData() for k, cb in self.combos.items()
                if cb.currentData() != self.study.variant(self.rid)["config"].get(k)}
        params = {}
        for key, (sp, q) in self.spins.items():
            cls = self.cls[key].currentData()
            val = None if (key in self.missing and self.missing[key].isChecked()) else round(sp.value(), 6)
            same = (q.value is None and val is None) or (q.value is not None and val is not None
                                                         and abs(float(q.value) - val) < 10 ** -sp.decimals())
            if same and cls == "keep":
                continue
            if cls == "supplier_spec":
                ref, ok = QInputDialog.getText(self, "Reference", f"Source of the supplier value for {key}:")
                if not ok or not ref.strip():
                    return
                params[key] = (val, cls, ref.strip())
            elif cls != "keep":
                params[key] = (val, cls, "edited in the Design Monitor")
            else:
                params[key] = val
        self.result_changes = {"name": self.name.text().strip() or self.study.variant(self.rid)["name"],
                               "description": self.desc.text().strip(), "options": opts, "params": params}
        self.accept()


# ---------------------------------------------------------------------------------------------- main window
class MonitorWindow(QMainWindow):
    def __init__(self, study: Optional[ST.Study] = None, study_path: Optional[Path] = None):
        super().__init__()
        self.setWindowTitle("OpenCAE Studio - K09-L Design Monitor")
        self.resize(1600, 960)
        self.study = study or (ST.Study.open(study_path) if study_path else ST.Study.open_default())
        self.busy = False
        self._thread: Optional[QThread] = None
        self._cancel = False
        self._viewer = None
        self._viewer_rid = None
        self._platforms = []
        self._build()
        self.refresh(full=True)

    # ------------------------------------------------------------------ layout
    def _build(self):
        tb = QToolBar("Study")
        tb.setMovable(False)
        self.addToolBar(tb)
        self.acts = {}

        def act(key, text, fn, tip="", shortcut=None):
            a = QAction(text, self)
            a.triggered.connect(lambda checked=False, f=fn: f())  # never pass 'checked' as an argument
            a.setToolTip(tip or text)
            if shortcut:
                a.setShortcut(shortcut)
            tb.addAction(a)
            self.acts[key] = a
            return a

        act("new", "New variant", self.new_variant, "Create a variant from an existing one", QKeySequence.New)
        act("edit", "Edit variant…", self.edit_variant, "Change components, geometry and parameters", "Ctrl+E")
        act("delete", "Delete", self.delete_variant)
        act("baseline", "Set as baseline", self.set_baseline, "Compare every variant against this one")
        tb.addSeparator()
        act("run", "Run analyses", self.run_analyses, "Run the solvers (FEA, assembly, hover, transition) that are "
                                                      "not current for the selected variant")
        act("run_all", "Run all stale", self.run_all, "Run the missing analyses of every variant")
        act("robust", "Run robustness", self.run_robustness, "11 off-nominal forward transitions (6-DOF model)")
        act("cancel", "Cancel", self.cancel, "Stop after the analysis that is running")
        tb.addSeparator()
        act("platform", "Open in platform", self.open_platform, "Open the selected variant in the six-page platform")
        act("open", "Open study…", self.open_study)
        act("save", "Save study", self.save_study, shortcut=QKeySequence.Save)
        act("save_as", "Save study as…", self.save_as)
        tb.addSeparator()
        act("measure", "Import measurements…", self.import_measurements, "Test measurements (CSV) for the selected "
            "variant: requirements whose inputs were all measured are judged on the measurement")
        act("website", "Build website", self.build_website, "Build and open the design-review website of the selected "
                                                            "variant (contours, failure parameters, audit)", "Ctrl+B")
        act("report", "Export dashboard report…", self.export_report, "Self-contained HTML dashboard of all variants",
            "Ctrl+R")

        split = QSplitter(Qt.Horizontal)
        self.setCentralWidget(split)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(6, 6, 6, 6)
        hdr = QLabel("Variants")
        hdr.setStyleSheet("font-weight:600;font-size:14px")
        ll.addWidget(hdr)
        self.vlist = QListWidget()
        self.vlist.currentItemChanged.connect(self._picked)
        self.vlist.itemDoubleClicked.connect(lambda *_: self.edit_variant())
        ll.addWidget(self.vlist, 1)
        self.vinfo = QLabel()
        self.vinfo.setWordWrap(True)
        self.vinfo.setStyleSheet("color:#4b5966")
        ll.addWidget(self.vinfo)
        split.addWidget(left)

        self.tabs = QTabWidget()
        split.addWidget(self.tabs)
        split.setSizes([300, 1300])
        # monitor tab
        mon = QWidget()
        ml = QVBoxLayout(mon)
        bar = QHBoxLayout()
        self.only_diff = QCheckBox("Only rows that differ between variants")
        self.only_diff.toggled.connect(lambda: self.refresh(full=False))
        self.only_bad = QCheckBox("Only rows that need attention")
        self.only_bad.toggled.connect(lambda: self.refresh(full=False))
        bar.addWidget(self.only_diff)
        bar.addWidget(self.only_bad)
        bar.addStretch(1)
        self.summary = QLabel()
        bar.addWidget(self.summary)
        ml.addLayout(bar)
        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().sectionClicked.connect(self._col_clicked)
        ml.addWidget(self.table, 1)
        self.tabs.addTab(mon, "Monitor")
        # variant tab
        var = QSplitter(Qt.Horizontal)
        info = QWidget()
        il = QVBoxLayout(info)
        self.def_view = QTextBrowser()
        self.def_view.setOpenExternalLinks(False)
        il.addWidget(self.def_view, 2)
        self.gate_tree = QTreeWidget()
        self.gate_tree.setHeaderLabels(["Gate / criterion", "Status", "Why"])
        self.gate_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        il.addWidget(self.gate_tree, 3)
        var.addWidget(info)
        self.view_box = QWidget()
        vl = QVBoxLayout(self.view_box)
        vl.setContentsMargins(0, 0, 0, 0)
        self.view_label = QLabel("3D view: changed parts in orange")
        vl.addWidget(self.view_label)
        var.addWidget(self.view_box)
        var.setSizes([700, 700])
        self.tabs.addTab(var, "Variant")
        # log
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 9))
        self.tabs.addTab(self.log, "Activity log")
        self.tabs.currentChanged.connect(self._tab)
        # status
        self.prog = QProgressBar()
        self.prog.setMaximumWidth(300)
        self.prog.setVisible(False)
        self.statusBar().addPermanentWidget(self.prog)
        self._log(f"Study opened: {self.study.path or '(new)'} - {len(self.study.ids())} variants")

    # ------------------------------------------------------------------ helpers
    def _log(self, text: str):
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")
        self.statusBar().showMessage(text, 8000)

    def rid(self) -> Optional[str]:
        it = self.vlist.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _set_busy(self, busy: bool, text: str = ""):
        self.busy = busy
        for k in ("new", "edit", "delete", "baseline", "run", "run_all", "robust", "platform", "open", "save",
                  "save_as", "report"):
            self.acts[k].setEnabled(not busy)
        self.acts["cancel"].setEnabled(busy)
        self.prog.setVisible(busy)
        if text:
            self._log(text)

    def _snap(self, rid) -> Optional[dict]:
        return self.study.cached(rid) if self.busy else self.study.snapshot(rid)

    # ------------------------------------------------------------------ refresh
    def refresh(self, full: bool = True):
        ids = self.study.ids()
        snaps = {r: self._snap(r) for r in ids}
        if not self.busy and self.study.current in ids:
            self.study.select(self.study.current)
        if full:
            cur = self.rid() or self.study.current
            self.vlist.blockSignals(True)
            self.vlist.clear()
            for r in ids:
                v, s = self.study.variant(r), snaps[r]
                if s:
                    c = s["counts"]
                    nh = sum(1 for h in s["heavy"].values() if h["status"] == CURRENT)
                    sub = f"✕{c[AP.FAIL]}  ◐{c[AP.COND]}  ✓{c[AP.PASS]}  ○{c[AP.NOT_EV]}   ⚙{nh}/5"
                else:
                    sub = "(not measured yet)"
                it = QListWidgetItem(f"{v['name']}{'   [baseline]' if r == self.study.baseline else ''}\n{sub}")
                it.setData(Qt.UserRole, r)
                it.setToolTip(v.get("description", ""))
                if s and s["counts"][AP.FAIL]:
                    it.setForeground(QBrush(QColor("#b3302b")))
                self.vlist.addItem(it)
                if r == cur:
                    self.vlist.setCurrentItem(it)
            self.vlist.blockSignals(False)
            if self.vlist.currentItem() is None and self.vlist.count():
                self.vlist.setCurrentRow(self.vlist.count() - 1)
        self._fill_table(ids, snaps)
        self._fill_variant()

    def _fill_table(self, ids, snaps):
        names = [self.study.variant(r)["name"] for r in ids]
        rows = []  # (group, label, [cell (text, status, tooltip)], key)
        reqdefs = self.study.engine.model.requirements
        metric_req = {r.metric: r for r in reqdefs}
        for rq in reqdefs:
            cells = []
            for r in ids:
                s = snaps[r]
                x = next((t for t in s["requirements"] if t["id"] == rq.id), None) if s else None
                if x is None:
                    cells.append(("…", None, ""))
                    continue
                p = x.get("predicted")
                pv = "" if p is None else (f"  {p:.3g}" if isinstance(p, float) else f"  {p}")
                cells.append((f"{ICON[x['status']]} {SHORT[x['status']]}{pv}", x["status"],
                              f"{x.get('why', '')}\nNext: {x.get('action') or '-'}"))
            rows.append(("Requirements", f"{rq.id}  {rq.text}  ({rq.op} {rq.threshold.value:g})", cells))
        for key, label, group, sc, unit, nd, better in ST.METRICS:
            cells = []
            rq = metric_req.get(key)
            for r in ids:
                s = snaps[r]
                q = (s or {}).get("metrics", {}).get(key) if s else None
                if not q or q.get("value") is None:
                    cells.append(("—", None, ""))
                    continue
                v = q["value"] * sc
                st = None
                if rq is not None and rq.threshold.value is not None:
                    thr = rq.threshold.value * sc
                    st = AP.PASS if ((v >= thr) if rq.op == ">=" else (v <= thr)) else AP.FAIL
                    if st == AP.PASS:
                        st = None  # only a miss is coloured; the requirement row carries the status
                cells.append((f"{v:,.{nd}f} {unit}".strip() + f"   [{EV.get(q['source'], q['source'])}]", st,
                              f"{q.get('ref', '')}\n{q.get('note', '')}".strip()))
            rows.append((group, label, cells))
        for nid, label in ST.HEAVY:
            cells = []
            for r in ids:
                s = snaps[r]
                if not s:
                    cells.append(("…", None, ""))
                    continue
                h = s["heavy"][nid]
                sm = h["summary"]
                det = ""
                if sm:
                    if nid.startswith("fea"):
                        det = f"  SF {sm['safety_factor']:.1f}"
                    elif nid == "assembly_check":
                        det = f"  {sm['interferences']} interf."
                    elif nid == "flight_hover":
                        det = f"  err {sm['max_position_error_mm']:.1f} mm"
                    elif sm.get("height_loss_m") is not None:
                        det = f"  loss {sm['height_loss_m']:.2f} m"
                cells.append((h["status"] + det, h["status"], h.get("reason") or (h.get("time") or "")))
            rows.append(("Analyses", label, cells))
        if snaps.get(ids[0]):
            for i, g in enumerate(snaps[ids[0]]["gates"]):
                cells = []
                for r in ids:
                    s = snaps[r]
                    if not s:
                        cells.append(("…", None, ""))
                        continue
                    gg = s["gates"][i]
                    bad = [c["text"] for c in gg["criteria"] if c["status"] != AP.PASS]
                    cells.append((f"{ICON[gg['status']]} {SHORT[gg['status']]}", gg["status"], "\n".join(bad[:12])))
                rows.append(("Gates", f"{g['id']}  {g['name']}", cells))
        # filters
        if self.only_diff.isChecked():
            rows = [r for r in rows if len({c[0] for c in r[2]}) > 1]
        if self.only_bad.isChecked():
            rows = [r for r in rows if any(c[1] in (AP.FAIL, AP.NOT_EV, "stale", "not run", "failed") for c in r[2])]
        t = self.table
        t.clear()
        t.setColumnCount(1 + len(ids))
        t.setHorizontalHeaderLabels(["Monitored parameter"] + [
            n + ("  (baseline)" if r == self.study.baseline else "") for n, r in zip(names, ids)])
        n_rows = len(rows) + len({r[0] for r in rows})
        t.setRowCount(n_rows)
        bold = QFont()
        bold.setBold(True)
        row, last = 0, None
        for group, label, cells in rows:
            if group != last:
                it = QTableWidgetItem(group.upper())
                it.setFont(bold)
                it.setBackground(QBrush(QColor("#e6ebef")))
                t.setItem(row, 0, it)
                for j in range(len(ids)):
                    x = QTableWidgetItem("")
                    x.setBackground(QBrush(QColor("#e6ebef")))
                    t.setItem(row, j + 1, x)
                row += 1
                last = group
            t.setItem(row, 0, QTableWidgetItem(label))
            for j, (text, st, tip) in enumerate(cells):
                it = QTableWidgetItem(text)
                it.setToolTip(tip)
                if st in STATUS_COL:
                    fg, bg = STATUS_COL[st]
                    it.setForeground(QBrush(QColor(fg)))
                    it.setBackground(QBrush(QColor(bg)))
                if ids[j] == self.rid():
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                t.setItem(row, j + 1, it)
            row += 1
        t.setRowCount(row)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for j in range(len(ids)):
            t.horizontalHeader().setSectionResizeMode(j + 1, QHeaderView.ResizeToContents)
        tot = sum(1 for r in ids if snaps[r] and snaps[r]["counts"][AP.FAIL] == 0)
        self.summary.setText(f"{len(ids)} variants · {tot} without failing requirements")

    def _fill_variant(self):
        rid = self.rid()
        if not rid:
            return
        s = self._snap(rid)
        v = self.study.variant(rid)
        if not s:
            self.def_view.setHtml("<p>Measuring…</p>")
            return
        diff = self.study.diff(rid) if rid != self.study.baseline else []
        bname = self.study.variant(self.study.baseline)["name"]
        e = html.escape
        dr = "".join(f"<tr><td><code>{e(k)}</code></td><td>{e(a)}</td><td><b>{e(b)}</b></td></tr>" for k, a, b in diff) \
            or "<tr><td colspan=3><i>no differences (this is the baseline)</i></td></tr>"
        hv = "".join(f"<tr><td>{e(h['label'])}</td><td style='color:{STATUS_COL.get(h['status'], ('#333',))[0]}'>"
                     f"<b>{e(h['status'])}</b></td><td>{e(h.get('reason') or h.get('time') or '')}</td></tr>"
                     for h in s["heavy"].values())
        parts = "".join(f"<li>{e(p)}</li>" for p in s["definition"]["changed_parts"]) or "<li><i>none</i></li>"
        c = s["counts"]
        self.vinfo.setText(f"<b>{e(v['name'])}</b><br>{e(v.get('description', ''))}<br>"
                           f"<span style='color:#b3302b'>✕ {c[AP.FAIL]} fail</span> · ◐ {c[AP.COND]} conditional · "
                           f"✓ {c[AP.PASS]} pass · ○ {c[AP.NOT_EV]} not evaluated")
        rob = s.get("robustness")
        rb = ""
        if rob:
            ok = sum(1 for x in rob if not x["ground_contact"] and x["altitude_loss"] <= AP.TRANSITION_MAX_ALT_LOSS)
            rb = (f"<h3>Transition robustness</h3><p>{ok} of {len(rob)} cases within "
                  f"{AP.TRANSITION_MAX_ALT_LOSS:.0f} m</p><table>" + "".join(
                      f"<tr><td>{e(x['case'])}</td><td>{'ground contact' if x['ground_contact'] else str(x['altitude_loss']) + ' m'}"
                      f"</td></tr>" for x in rob) + "</table>")
        self.def_view.setHtml(f"""<h2>{e(v['name'])}</h2><p>{e(v.get('description', ''))}</p>
<h3>Differences from the baseline ({e(bname)})</h3><table cellpadding=3 border=0>{dr}</table>
<h3>Analyses</h3><table cellpadding=3>{hv}</table><h3>Parts changed from the imported CAD</h3><ul>{parts}</ul>{rb}""")
        self.gate_tree.clear()
        for g in s["gates"]:
            top = QTreeWidgetItem([f"{g['id']}  {g['name']}", SHORT[g["status"]], ""])
            top.setForeground(1, QBrush(QColor(STATUS_COL[g["status"]][0])))
            for cr in g["criteria"]:
                ch = QTreeWidgetItem([cr["text"], SHORT.get(cr["status"], cr["status"]), (cr.get("why") or "")[:200]])
                ch.setForeground(1, QBrush(QColor(STATUS_COL.get(cr["status"], ("#333",))[0])))
                ch.setToolTip(2, cr.get("why") or "")
                top.addChild(ch)
            self.gate_tree.addTopLevelItem(top)
        if self.tabs.currentIndex() == 1:
            self._update_view()

    def _tab(self, i):
        if i == 1:
            self._update_view()

    def _update_view(self):
        rid = self.rid()
        if self.busy or not rid or rid == self._viewer_rid:
            return
        try:
            if self._viewer is None:
                from opencae.ui.platform.window import Viewer
                self._viewer = Viewer(self.view_box)
                self.view_box.layout().addWidget(self._viewer, 1)
            import numpy as np

            from opencae.ui.platform import scene as SC
            self.study.select(rid)
            m = self.study.engine.model
            pd = SC.aircraft_polydata(m)
            mod = m.modified_bodies()
            self._viewer.show_aircraft(pd, reset=self._viewer_rid is None)
            if mod:
                self._viewer.recolour(set(mod))
            self._viewer_rid = rid
            n = len(mod)
            self.view_label.setText(f"3D view of {self.study.variant(rid)['name']}: "
                                    + (f"{n} bodies changed from the imported CAD (orange)" if n else "imported CAD"))
            _ = np
        except Exception as exc:  # no OpenGL (remote desktop, tests): the rest of the monitor still works
            self.view_label.setText(f"3D view unavailable here ({type(exc).__name__}).")

    # ------------------------------------------------------------------ interaction
    def _picked(self, cur, prev):
        if cur is None:
            return
        rid = cur.data(Qt.UserRole)
        if not self.busy:
            self.study.select(rid)
        self._viewer_rid = None if self._viewer_rid != rid else rid
        self._fill_table(self.study.ids(), {r: self._snap(r) for r in self.study.ids()})
        self._fill_variant()

    def _col_clicked(self, col):
        if col >= 1:
            self.vlist.setCurrentRow(col - 1)

    def _guard(self) -> bool:
        if self.busy:
            QMessageBox.information(self, "Busy", "An analysis is running; wait for it or press Cancel.")
            return False
        return True

    def new_variant(self):
        if not self._guard():
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("New variant")
        f = QFormLayout(dlg)
        name = QLineEdit(f"Variant {len(self.study.ids()) + 1}")
        base = QComboBox()
        for r in self.study.ids():
            base.addItem(self.study.variant(r)["name"], r)
        base.setCurrentIndex(max(base.findData(self.rid()), 0))
        desc = QLineEdit()
        f.addRow("Name", name)
        f.addRow("Start from", base)
        f.addRow("Description", desc)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        rev = self.study.add_variant(name.text().strip() or "Variant", base.currentData(), desc.text().strip())
        self._log(f"New variant '{rev['name']}' from '{self.study.variant(base.currentData())['name']}'")
        self.refresh(full=True)
        self.vlist.setCurrentRow(self.vlist.count() - 1)
        self.edit_variant()

    def edit_variant(self):
        rid = self.rid()
        if not rid or not self._guard():
            return
        before = self.study.snapshot(rid)
        dlg = VariantEditor(self.study, rid, self)
        if dlg.exec() != QDialog.Accepted or not dlg.result_changes:
            return
        ch = dlg.result_changes
        try:
            evs = self.study.edit(rid, ch["options"], ch["params"])
        except ValueError as exc:
            QMessageBox.warning(self, "Not applied", str(exc))
            return
        self.study.rename(rid, ch["name"], ch["description"])
        after = self.study.snapshot(rid)
        self._log(f"Edited '{ch['name']}': " + (", ".join(ev.get("change", "") for ev in evs) or "name / description"))
        for a, b in zip(before["requirements"], after["requirements"]):
            if a["status"] != b["status"]:
                self._log(f"   {a['id']}: {SHORT[a['status']]} -> {SHORT[b['status']]}  ({b.get('why', '')})")
        stale = [h["label"] for h in after["heavy"].values() if h["status"] != CURRENT]
        if stale:
            self._log("   analyses to run for this variant: " + ", ".join(stale))
        self._viewer_rid = None
        self.refresh(full=True)
        self._autosave()

    def delete_variant(self):
        rid = self.rid()
        if not rid or not self._guard():
            return
        name = self.study.variant(rid)["name"]
        if QMessageBox.question(self, "Delete variant", f"Delete '{name}' and its definition? Solver runs stay in "
                                                         "the history.") != QMessageBox.Yes:
            return
        try:
            self.study.delete_variant(rid)
        except ValueError as exc:
            QMessageBox.information(self, "Not deleted", str(exc))
            return
        self._log(f"Deleted variant '{name}'")
        self.refresh(full=True)
        self._autosave()

    def set_baseline(self):
        rid = self.rid()
        if rid and self._guard():
            self.study.baseline = rid
            self._log(f"Baseline: {self.study.variant(rid)['name']}")
            self.refresh(full=True)

    # ------------------------------------------------------------------ jobs
    def _start(self, title: str, fn: Callable, on_done: Callable):
        self._cancel = False
        self._set_busy(True, title)
        self.prog.setRange(0, 0)
        self._thread = QThread(self)
        self._worker = _Worker(fn)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._progress)
        # bound methods of this window: Qt queues the calls into the GUI thread (a plain function would run in
        # the worker thread)
        self._on_done = on_done
        self._worker.done.connect(self._job_done)
        self._worker.failed.connect(self._job_failed)
        from opencae.utilities import threads
        threads.track(self._thread, cancel=lambda: setattr(self, "_cancel", True))
        self._report_start(title)
        self._thread.start()

    # -- the Progress Monitor (best effort: reporting never stops a run)
    _rjob = None

    def _report_start(self, title: str) -> None:
        try:
            from opencae.ui.jobs import registry as R
            reg = R.default()
            key = str(self.study.path or ST.USER_STUDY)
            name = Path(key).stem or "K09-L study"
            reg.register_project(key, name)
            self._rjob = reg.create_job(key, "drone", title, name, "k09l", "gui", stages=[],
                                        cancel=self.cancel, project_name=name)
        except Exception:  # noqa: BLE001
            self._rjob = None

    def _report(self, fn: str, *a, **kw) -> None:
        if self._rjob is None:
            return
        try:
            from opencae.ui.jobs import registry as R
            getattr(R.default(), fn)(self._rjob, *a, **kw)
        except Exception:  # noqa: BLE001
            pass

    def _end_thread(self):
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
            self._thread = None
        self._set_busy(False)
        self._viewer_rid = None

    @Slot(object)
    def _job_done(self, res):
        self._report("finish", not self._cancel, cancelled=bool(self._cancel))
        self._end_thread()
        cb, self._on_done = self._on_done, None
        self.refresh(full=True)
        self._autosave()
        if cb:
            cb(res)

    @Slot(str)
    def _job_failed(self, tb: str):
        self._report("finish", False, error=tb.strip().splitlines()[-1], cancelled=bool(self._cancel))
        self._end_thread()
        self._on_done = None
        self._log("FAILED: " + tb.strip().splitlines()[-1])
        self.log.appendPlainText(tb)
        self.refresh(full=True)
        QMessageBox.warning(self, "Job failed", tb.strip().splitlines()[-1])

    @Slot(str, int, int)
    def _progress(self, what: str, i: int, n: int):
        if n and what != "done":
            self._report("set_progress", i / n)  # i of n analyses are finished: countable
            self._report("log", f"running {what} ({i + 1}/{n})")
        if n:
            self.prog.setRange(0, n)
            self.prog.setValue(i)
        if what != "done":
            self.statusBar().showMessage(f"{what} ({i + 1}/{n})")
            self._log(f"   running {what} ({i + 1}/{n})")

    def cancel(self):
        self._cancel = True
        self._log("Cancel requested: the job stops after the step that is running")

    def run_analyses(self, rid=None, then=None):
        rid = rid or self.rid()
        if not rid or not self._guard():
            return
        name = self.study.variant(rid)["name"]

        def job(prog):
            return self.study.run_analyses(rid, progress=prog, cancelled=lambda: self._cancel)

        def done(res):
            self._log(f"Analyses of '{name}': done {', '.join(res['done']) or 'nothing (all current)'}"
                      + (f"; FAILED {res['errors']}" if res["errors"] else ""))
            if then:
                then()

        self._start(f"Running the analyses of '{name}'", job, done)

    def run_all(self):
        if not self._guard():
            return
        todo = [r for r in self.study.ids() if self.study.stale_analyses(r)]
        if not todo:
            self._log("Every variant's analyses are current")
            return

        def chain(rest):
            if rest and not self._cancel:
                self.run_analyses(rest[0], then=lambda: chain(rest[1:]))

        chain(todo)

    def run_robustness(self):
        rid = self.rid()
        if not rid or not self._guard():
            return
        name = self.study.variant(rid)["name"]

        def job(prog):
            return self.study.run_robustness(rid, progress=prog, cancelled=lambda: self._cancel)

        def done(rows):
            ok = sum(1 for x in rows if not x["ground_contact"] and x["altitude_loss"] <= AP.TRANSITION_MAX_ALT_LOSS)
            self._log(f"Robustness of '{name}': {ok} of {len(rows)} transitions within "
                      f"{AP.TRANSITION_MAX_ALT_LOSS:.0f} m")

        self._start(f"Running 11 off-nominal transitions for '{name}'", job, done)

    # ------------------------------------------------------------------ files
    def _autosave(self):
        try:
            p = self.study.save()
            self.statusBar().showMessage(f"Saved {p}", 4000)
        except Exception as exc:
            self._log(f"Autosave failed: {exc}")

    def save_study(self):
        if self._guard():
            self._log(f"Study saved: {self.study.save()}")

    def save_as(self):
        if not self._guard():
            return
        p, _ = QFileDialog.getSaveFileName(self, "Save study as", str(self.study.path or ST.USER_STUDY), "JSON (*.json)")
        if p:
            self._log(f"Study saved: {self.study.save(Path(p))}")

    def open_study(self, path=None):
        if not self._guard():
            return
        path = path or QFileDialog.getOpenFileName(self, "Open study", str(ST.USER_STUDY.parent), "JSON (*.json)")[0]
        if not path:
            return
        self.study = ST.Study.open(Path(path))
        self._viewer_rid = None
        self._log(f"Study opened: {path}")
        self.refresh(full=True)

    def export_report(self, path=None):
        if not self._guard():
            return
        path_given = path is not None
        if path is None:
            default = (Path(self.study.path).with_name("k09l_dashboard.html") if self.study.path
                       else ST.USER_STUDY.with_name("k09l_dashboard.html"))
            path, _ = QFileDialog.getSaveFileName(self, "Export dashboard report", str(default), "HTML (*.html)")
            if not path:
                return
        # VTK renders stay in the GUI thread (offscreen rendering from a worker thread is not safe on every
        # platform); the work is short, so it runs here with a progress bar
        from PySide6.QtWidgets import QApplication
        cur = self.study.current
        self._set_busy(True, "Building the dashboard report")
        self.busy = False  # the engine is used from this thread only

        def prog(what, i, n):
            self.prog.setRange(0, max(n, 1))
            self.prog.setValue(i)
            self.statusBar().showMessage(f"Dashboard: {what}")
            QApplication.processEvents()

        try:
            p = ST.write_dashboard(self.study, Path(path), renders=True, progress=prog)
        except Exception:
            self._set_busy(False)
            tb = traceback.format_exc()
            self._log("Dashboard report FAILED: " + tb.strip().splitlines()[-1])
            QMessageBox.warning(self, "Dashboard report", tb.strip().splitlines()[-1])
            return None
        self._set_busy(False)
        if cur in self.study.ids():
            self.study.select(cur)
        self._log(f"Dashboard report written: {p}")
        if path_given is False:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
        return p

    def import_measurements(self, path=None):
        rid = self.rid()
        if not rid or not self._guard():
            return None
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Import test measurements", str(Path.home()), "CSV (*.csv)")
            if not path:
                return None
        try:
            res = self.study.import_measurements(rid, Path(path))
        except Exception as exc:  # noqa: BLE001 - shown to the user, nothing changed
            self._log(f"Measurement import FAILED: {exc}")
            QMessageBox.warning(self, "Import measurements", str(exc))
            return None
        name = self.study.variant(rid)["name"]
        self._log(f"Measurements for '{name}': {res['imported']} imported, {res['stored']} stored; requirements "
                  f"judged on measurements: {', '.join(res['closed']) or 'none yet'}")
        self.refresh(full=True)
        self._autosave()
        return res

    def build_website(self, out=None, open_browser=True):
        """The design-review website of the selected variant (static, opens from disk; see opencae/web)."""
        rid = self.rid()
        if not rid or not self._guard():
            return None
        from PySide6.QtWidgets import QApplication

        from opencae.web import build as WB
        out = Path(out) if out else (Path(self.study.path).with_name("k09l_website") if self.study.path
                                     else WB.DEFAULT_OUT)
        cur = self.study.current
        self._set_busy(True, "Building the website")
        self.busy = False  # thumbnails render with VTK in this (GUI) thread

        def prog(what, i, n):
            self.prog.setRange(0, max(n, 1))
            self.prog.setValue(i)
            self.statusBar().showMessage(f"Website: {what}")
            QApplication.processEvents()

        try:
            p = WB.build_site(self.study, out, self.study.variant(rid)["name"], progress=prog)
        except Exception:
            self._set_busy(False)
            tb = traceback.format_exc()
            self._log("Website FAILED: " + tb.strip().splitlines()[-1])
            QMessageBox.warning(self, "Website", tb.strip().splitlines()[-1])
            return None
        self._set_busy(False)
        if cur in self.study.ids():
            self.study.select(cur)
        self._log(f"Website written: {p}")
        if open_browser:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
        return p

    def open_platform(self):
        rid = self.rid()
        if not rid or not self._guard():
            return
        from opencae.engineering.graph import Engine
        from opencae.ui.platform.window import PlatformWindow
        v = self.study.variant(rid)
        e = Engine()
        e.model.config = dict(v["config"])
        e.model.params.update({k: Q.from_dict(d) for k, d in v["params"].items()})
        e.history = list(self.study.engine.history)  # the variant's solver runs show as current there too
        e.recompute()
        for nid in e.heavy:
            e.heavy_status(nid)
        w = PlatformWindow(engine=e)
        w.setWindowTitle(f"OpenCAE Studio - K09-L platform - {v['name']} (copy; edit variants in the monitor)")
        w.show()
        self._platforms.append(w)
        self._log(f"Opened '{v['name']}' in the platform (a copy: changes there do not change the variant)")

    def closeEvent(self, ev):
        if self.busy:
            QMessageBox.information(self, "Busy", "An analysis is running; cancel it and wait for the step to finish.")
            ev.ignore()
            return
        self._autosave()
        super().closeEvent(ev)
