"""New drone project: pick a CAD file, a folder, a name and a type; creates the project through the pipeline."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QVBoxLayout, QWidget)

from opencae.ui import theme

TYPES = ["multirotor", "fixed_wing", "tailsitter", "vtol"]
CAD_FILTER = "CAD / mesh files (*.step *.stp *.stl *.obj);;All files (*)"


def default_projects_dir() -> Path:
    import os
    return Path(os.environ.get("OPENCAE_HOME") or Path.home() / ".opencae") / "projects"


def roles_summary(project: Path) -> str:
    """One line per role: number of parts and their names, from the CAD file's part names."""
    from opencae.cli import guess_roles_of
    roles = guess_roles_of(Path(project))
    if not roles:
        return "No parts were found in the CAD file."
    return "\n".join(f"{r}: {len(n)} part(s) - {', '.join(n[:6])}{' ...' if len(n) > 6 else ''}"
                     for r, n in roles.items())


class NewDroneDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New drone project")
        self.resize(560, 220)
        self.project_dir: Optional[Path] = None
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.cad = QLineEdit()
        self.cad.setPlaceholderText("airframe.step / .stl / .obj")
        self.folder = QLineEdit()
        self.folder.setPlaceholderText(str(default_projects_dir() / "my_drone"))
        self.name = QLineEdit()
        self.type = QComboBox()
        self.type.addItems(TYPES)
        form.addRow("CAD file", self._with_button(self.cad, "Browse…", self._pick_cad))
        form.addRow("Project folder", self._with_button(self.folder, "Browse…", self._pick_folder))
        form.addRow("Name", self.name)
        form.addRow("Type", self.type)
        lay.addLayout(form)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setStyleSheet(f"color:{theme.ERR}")
        lay.addWidget(self.message)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Create project")
        self.buttons.button(QDialogButtonBox.Ok).setObjectName("primary")
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

    @staticmethod
    def _with_button(edit: QLineEdit, text: str, fn) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        b = QPushButton(text)
        b.clicked.connect(lambda checked=False: fn())
        h.addWidget(b)
        return w

    def _pick_cad(self):
        f, _ = QFileDialog.getOpenFileName(self, "CAD file", "", CAD_FILTER)
        if f:
            self.cad.setText(f)
            if not self.name.text():
                self.name.setText(Path(f).stem)
            if not self.folder.text():
                self.folder.setText(str(default_projects_dir() / Path(f).stem))

    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Project folder", str(default_projects_dir().parent))
        if d:
            self.folder.setText(str(Path(d) / (self.name.text() or "drone")) if any(Path(d).iterdir()) else d)

    def set_values(self, cad: str, folder: str, name: str, type_: str) -> None:
        self.cad.setText(str(cad))
        self.folder.setText(str(folder))
        self.name.setText(name)
        self.type.setCurrentText(type_)

    def create(self) -> Optional[Path]:
        """Create the project; on a problem show a red message and return None."""
        cad, folder = Path(self.cad.text().strip()), self.folder.text().strip()
        if not self.cad.text().strip() or not cad.exists():
            self.message.setText("Choose an existing CAD file (STEP, STL or OBJ).")
            return None
        if cad.suffix.lower() not in (".step", ".stp", ".stl", ".obj"):
            self.message.setText("The CAD file must be STEP, STL or OBJ.")
            return None
        if not folder:
            self.message.setText("Choose a project folder.")
            return None
        try:
            from opencae.drone import pipeline
            self.project_dir = Path(pipeline.new_project(Path(folder), cad, self.name.text().strip() or Path(folder).name,
                                                         self.type.currentText()))
        except Exception as e:  # shown to the person, never hidden
            self.message.setText(f"Could not create the project: {e}")
            return None
        self.message.setText("")
        return self.project_dir

    def _accept(self):
        if self.create() is not None:
            self.accept()
