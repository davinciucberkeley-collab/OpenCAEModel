"""Tools > Settings: solver configuration with a real 'Test Solver' run."""
from __future__ import annotations

import html
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from opencae.solver.launcher import discover, solver_dll_dirs
from opencae.utilities.settings import get_setting, set_settings


class _TestWorker(QObject):
    done = Signal(object)

    def __init__(self, backend):
        super().__init__()
        self.backend = backend

    def run(self):
        try:
            rep = self.backend.selftest()
        except Exception as exc:  # never leave the dialog hanging
            from opencae.solver.calculix.selftest import SelfTestReport, Step
            rep = SelfTestReport(exe=str(self.backend.executable()))
            rep.steps.append(Step("Self-test", False, f"{type(exc).__name__}: {exc}"))
        self.done.emit(rep)


def run_selftest_async(parent: QObject, backend, callback) -> QThread:
    """Run backend.selftest() in a worker thread; call callback(report) in the GUI thread."""
    th = QThread(parent)
    w = _TestWorker(backend)
    w.moveToThread(th)
    th.started.connect(w.run)
    w.done.connect(callback)
    w.done.connect(th.quit)
    th.finished.connect(w.deleteLater)
    th._worker = w  # keep alive
    th.start()
    return th


class SolverSettingsDialog(QDialog):
    def __init__(self, parent=None, threads: int = 2):
        super().__init__(parent)
        self.setWindowTitle("Settings — solvers (CalculiX, SU2)")
        self.resize(820, 620)
        lay = QVBoxLayout(self)
        intro = QLabel("The solver runs as a separate process with its own DLL search path, so a CalculiX installed "
                       "in another conda environment (e.g. <b>opencae</b>) works without changing the GUI "
                       "environment. A solver is shown as ready only after <b>Test Solver</b> has run a real "
                       "analysis successfully.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        form = QFormLayout()
        self.found = QComboBox()
        self._installs = discover(get_setting("ccx_path"), get_setting("ccx_extra_dll_dirs") or [])
        for inst in self._installs:
            self.found.addItem(f"{inst.label}   [{inst.source}]", inst.exe)
        if not self._installs:
            self.found.addItem("(no CalculiX executable found)", "")
        self.found.activated.connect(lambda i: self.path.setText(self.found.itemData(i) or ""))
        form.addRow("Detected installations", self.found)
        row = QHBoxLayout()
        self.path = QLineEdit(get_setting("ccx_path") or (self._installs[0].exe if self._installs else ""))
        self.path.setPlaceholderText("empty = automatic detection")
        b = QPushButton("Browse…")
        b.clicked.connect(self._browse)
        row.addWidget(self.path, 1)
        row.addWidget(b)
        w = QWidget()
        w.setLayout(row)
        form.addRow("CalculiX executable", w)
        self.extra = QPlainTextEdit("\n".join(get_setting("ccx_extra_dll_dirs") or []))
        self.extra.setPlaceholderText("Optional: additional folders containing the solver's DLLs, one per line")
        self.extra.setMaximumHeight(70)
        form.addRow("Extra DLL folders", self.extra)
        self.threads = QSpinBox()
        self.threads.setRange(1, 256)
        self.threads.setValue(int(get_setting("ccx_threads", threads)))
        form.addRow("Default solver threads", self.threads)
        lay.addLayout(form)
        trow = QHBoxLayout()
        self.test_btn = QPushButton("Test Solver")
        self.test_btn.setObjectName("primary")
        self.test_btn.clicked.connect(self._test)
        self.state = QLabel("Not tested")
        trow.addWidget(self.test_btn)
        trow.addWidget(self.state, 1)
        lay.addLayout(trow)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        f = QFont("Consolas")
        f.setStyleHint(QFont.Monospace)
        self.out.setFont(f)
        lay.addWidget(self.out, 1)
        # ---- SU2 (flow studies)
        su2box = QLabel("<b>SU2 (CFD, optional)</b> — used only by flow studies. Not bundled: download the "
                        "Windows ZIP from github.com/su2code/SU2/releases, unzip, and select bin\\SU2_CFD.exe. "
                        "No PATH or SU2_RUN changes are needed.")
        su2box.setWordWrap(True)
        lay.addWidget(su2box)
        srow = QHBoxLayout()
        from opencae.cfd.su2 import find_su2
        found = find_su2(get_setting("su2_path") or None)
        self.su2_path = QLineEdit(get_setting("su2_path") or (found.exe if found else ""))
        self.su2_path.setPlaceholderText("empty = automatic detection (SU2_RUN, PATH, conda environments)")
        sb = QPushButton("Browse…")
        sb.clicked.connect(self._browse_su2)
        self.su2_btn = QPushButton("Test SU2")
        self.su2_btn.clicked.connect(self._test_su2)
        self.su2_state = QLabel("Not tested" if found or get_setting("su2_path") else "SU2 not found")
        srow.addWidget(self.su2_path, 1)
        srow.addWidget(sb)
        srow.addWidget(self.su2_btn)
        srow.addWidget(self.su2_state)
        lay.addLayout(srow)
        self.su2_report = None
        self._su2_thread: Optional[QThread] = None
        # ---- OpenFOAM (flow studies, optional)
        fbox = QLabel("<b>OpenFOAM (CFD, optional)</b> — ESI OpenFOAM v2006 or later, on Linux or inside WSL on "
                      "Windows. Enter the conda environment folder (e.g. /home/me/miniforge3/envs/openfoam) or an "
                      "etc/bashrc file; empty = automatic search. Ubuntu's own 'openfoam' package (v1912) does not "
                      "work. Nothing is installed by OpenCAE.")
        fbox.setWordWrap(True)
        lay.addWidget(fbox)
        frow = QHBoxLayout()
        self.foam_env = QLineEdit(get_setting("openfoam_env") or "")
        self.foam_env.setPlaceholderText("empty = automatic (openfoam.com packages, conda environments, WSL)")
        self.foam_distro = QLineEdit(get_setting("openfoam_wsl_distro") or "")
        self.foam_distro.setPlaceholderText("WSL distribution (Windows; empty = default)")
        self.foam_np = QSpinBox()
        self.foam_np.setRange(1, 256)
        self.foam_np.setValue(int(get_setting("openfoam_processes") or 1))
        self.foam_np.setToolTip("MPI processes for simpleFoam (1 = serial). Use > 1 only if your installation has "
                                "a working MPI.")
        self.foam_btn = QPushButton("Test OpenFOAM")
        self.foam_btn.clicked.connect(self._test_foam)
        self.foam_state = QLabel("Not tested")
        frow.addWidget(self.foam_env, 2)
        frow.addWidget(self.foam_distro, 1)
        frow.addWidget(QLabel("processes"))
        frow.addWidget(self.foam_np)
        frow.addWidget(self.foam_btn)
        frow.addWidget(self.foam_state)
        lay.addLayout(frow)
        self.foam_report = None
        self._foam_thread: Optional[QThread] = None
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.report = None
        self._thread: Optional[QThread] = None

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "CalculiX executable", self.path.text() or str(Path.home()),
                                           "Executables (*.exe);;All files (*)")
        if p:
            self.path.setText(p)

    def _extra_dirs(self) -> list[str]:
        return [ln.strip() for ln in self.extra.toPlainText().splitlines() if ln.strip()]

    def make_backend(self):
        from opencae.solver.base import CalculixBackend
        return CalculixBackend(self.path.text().strip() or None, self._extra_dirs())

    def _test(self):
        be = self.make_backend()
        self.test_btn.setEnabled(False)
        self.state.setText("Running…")
        self.out.setPlainText(f"Testing {be.executable() or '(no executable)'} …")
        from PySide6.QtWidgets import QApplication
        self._thread = run_selftest_async(QApplication.instance(), be, self._tested)

    def _tested(self, rep):
        self.report = rep
        self.test_btn.setEnabled(True)
        self.state.setText("<b style='color:#4cbb7f'>PASSED — solver ready</b>" if rep.ok else
                           "<b style='color:#e5534b'>FAILED — see details</b>")
        self.out.setPlainText(rep.summary())

    def _browse_su2(self):
        p, _ = QFileDialog.getOpenFileName(self, "SU2_CFD executable", self.su2_path.text() or str(Path.home()),
                                           "Executables (*.exe);;All files (*)")
        if p:
            self.su2_path.setText(p)

    def make_su2_backend(self):
        from opencae.cfd.su2 import SU2Backend
        return SU2Backend(self.su2_path.text().strip() or None)

    def _test_su2(self):
        be = self.make_su2_backend()
        self.su2_btn.setEnabled(False)
        self.su2_state.setText("Running…")
        self.out.setPlainText(f"Testing SU2 {be.executable() or '(no executable)'} …")
        from PySide6.QtWidgets import QApplication
        self._su2_thread = run_selftest_async(QApplication.instance(), be, self._su2_tested)

    def _su2_tested(self, rep):
        self.su2_report = rep
        self.su2_btn.setEnabled(True)
        self.su2_state.setText("<b style='color:#4cbb7f'>PASSED</b>" if rep.ok else
                               "<b style='color:#e5534b'>FAILED</b>")
        self.out.setPlainText(rep.summary())

    def make_foam_backend(self):
        from opencae.cfd.openfoam.backend import OpenFOAMBackend
        return OpenFOAMBackend(self.foam_env.text().strip(), self.foam_distro.text().strip())

    def _test_foam(self):
        self.foam_btn.setEnabled(False)
        self.foam_state.setText("Searching…")
        be = self.make_foam_backend()
        self.out.setPlainText(be.status_text() + "\nRunning a channel-flow test with snappyHexMesh and simpleFoam "
                                                 "(about a minute)…")
        if not be.install:
            from opencae.cfd.openfoam.install import INSTALL_HELP
            self.foam_btn.setEnabled(True)
            self.foam_state.setText("<b style='color:#e5534b'>NOT FOUND</b>")
            self.out.setPlainText(INSTALL_HELP)
            return
        from PySide6.QtWidgets import QApplication
        self._foam_thread = run_selftest_async(QApplication.instance(), be, self._foam_tested)

    def _foam_tested(self, rep):
        self.foam_report = rep
        self.foam_btn.setEnabled(True)
        self.foam_state.setText("<b style='color:#4cbb7f'>PASSED</b>" if rep.ok else
                                "<b style='color:#e5534b'>FAILED</b>")
        self.out.setPlainText(rep.summary())

    def done(self, r):
        for th in (self._thread, self._su2_thread, self._foam_thread):
            if th is not None and th.isRunning():
                th.quit()
                th.wait(330000)
        super().done(r)

    def _save(self):
        set_settings(ccx_path=self.path.text().strip() or None, ccx_extra_dll_dirs=self._extra_dirs() or None,
                     ccx_threads=int(self.threads.value()), su2_path=self.su2_path.text().strip() or None,
                     openfoam_env=self.foam_env.text().strip() or None,
                     openfoam_wsl_distro=self.foam_distro.text().strip() or None,
                     openfoam_processes=int(self.foam_np.value()) if self.foam_np.value() > 1 else None)
        self.accept()


def report_html(rep) -> str:
    return "<pre>" + html.escape(rep.summary()) + "</pre>"
