"""New drone project: pick a CAD file, a folder, a name and a type; creates the project through the pipeline."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
                               QWizard, QWizardPage)

from opencae.ui import theme


TYPES = ["multirotor", "fixed_wing", "tailsitter", "vtol"]
TYPE_INFO = {
    "multirotor": "Multirotor: quadcopters and other drones that fly on rotors alone. Checks hover, thrust and "
                  "strength of the arms.",
    "fixed_wing": "Fixed wing: a plane with a wing and a tail that flies forward. Adds the air-flow (lift and "
                  "drag) analysis.",
    "tailsitter": "Tailsitter: takes off and lands on its tail, then tips over to fly like a plane.",
    "vtol": "VTOL: lifts off with rotors and flies forward on a wing (separate lift and cruise motors).",
}
MATERIALS = {  # name: (key, density kg/m3, Young's modulus GPa, Poisson, yield MPa); typical values, not datasheets
    "Carbon fibre plate": ("carbon_plate", 1600, 50, 0.3, 600),
    "Fibreglass plate": ("fibreglass_plate", 1850, 24, 0.28, 300),
    "Aluminium 6061-T6": ("aluminium_6061", 2700, 68.9, 0.33, 275),
    "PETG (3D printed)": ("petg", 1270, 2.1, 0.4, 50),
    "Nylon PA12 (3D printed)": ("nylon_pa12", 1010, 1.7, 0.4, 45),
}
NO_IDEA = "I don't know yet"
STRUCTURAL = ("frame", "arm", "wing", "fin")
# key, label, unit, example
PARAMS = [("propulsion.motor.kv", "Motor KV", "rpm/V", "2400"),
          ("propulsion.prop.diameter_in", "Prop diameter", "in", "5.1"),
          ("propulsion.prop.pitch_in", "Prop pitch", "in", "4.3"),
          ("propulsion.battery.cells", "Battery cells (S)", "", "4"),
          ("propulsion.battery.capacity_mah", "Battery capacity", "mAh", "1500")]
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


def _glob_safe(name: str) -> str:
    """An exact-match glob for a part name (the characters that mean something in a glob are escaped)."""
    return "".join(f"[{c}]" if c in "[]?*" else c for c in name)


class _CadPage(QWizardPage):
    def __init__(self, w):
        super().__init__()
        self.w = w
        self.setTitle("1. Your CAD file")
        self.setSubTitle("Choose the airframe file, give the project a name and pick the kind of aircraft.")
        form = QFormLayout(self)
        form.addRow("CAD file", w._with_button(w.cad, "Browse…", w._pick_cad))
        form.addRow("Project folder", w._with_button(w.folder, "Browse…", w._pick_folder))
        form.addRow("Name", w.name)
        form.addRow("Type", w.type)
        form.addRow("", w.type_info)
        form.addRow("", w.message)

    def validatePage(self) -> bool:
        return not self.w.check_cad()


class _RolesPage(QWizardPage):
    def __init__(self, w):
        super().__init__()
        self.w = w
        self.setTitle("2. Check the parts")
        self.setSubTitle("Each part of the CAD file was given a role from its name. Change a role if it is wrong.")
        lay = QVBoxLayout(self)
        lay.addWidget(w.roles_table, 1)
        lay.addWidget(w.roles_note)

    def initializePage(self) -> None:
        self.w.load_parts()


class _ParamsPage(QWizardPage):
    def __init__(self, w):
        super().__init__()
        self.w = w
        self.setTitle("3. Key numbers")
        self.setSubTitle("Fill in what you know. Tick \"I don't know yet\" to leave a value empty; it can be "
                         "filled in later in the parameter sheet.")
        form = QFormLayout(self)
        for key, label, unit, example in PARAMS:
            chk, edit = w.param_widgets[key]
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(edit, 1)
            if unit:
                h.addWidget(QLabel(unit))
            h.addWidget(chk)
            form.addRow(label, row)
        form.addRow("Frame material", w.material)
        form.addRow("", w.param_message)
        hint = QLabel("Numbers typed here are saved as estimates. Raise them to 'datasheet' or 'measured' in the "
                      "parameter sheet once you have the source.")
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        form.addRow("", hint)


class NewDroneDialog(QWizard):
    """Three pages: CAD file / name / type, part roles, key parameters. Finish creates the project."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New drone project")
        self.setWizardStyle(QWizard.ClassicStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        self.setButtonText(QWizard.FinishButton, "Create project")
        self.resize(680, 520)
        self.project_dir: Optional[Path] = None
        self.parts: dict = {}      # part name -> guessed role
        self.cad = QLineEdit()
        self.cad.setPlaceholderText("airframe.step / .stl / .obj")
        self.cad.setToolTip("The airframe as a STEP, STL or OBJ file")
        self.folder = QLineEdit()
        self.folder.setPlaceholderText(str(default_projects_dir() / "my_drone"))
        self.folder.setToolTip("Where the project is stored (a new folder is fine)")
        self.name = QLineEdit()
        self.name.setToolTip("A short name for the drone")
        self.type = QComboBox()
        self.type.addItems(TYPES)
        self.type.setToolTip("The kind of aircraft; it decides the default requirements")
        self.type_info = QLabel("")
        self.type_info.setWordWrap(True)
        self.type_info.setObjectName("hint")
        self.type.currentTextChanged.connect(lambda t: self.type_info.setText(TYPE_INFO.get(t, "")))
        self.type_info.setText(TYPE_INFO[self.type.currentText()])
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setStyleSheet(f"color:{theme.ERR}")
        self.roles_table = QTableWidget(0, 3)
        self.roles_table.setHorizontalHeaderLabels(["Part", "Guessed from the name", "Role"])
        self.roles_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.roles_table.verticalHeader().setVisible(False)
        self.roles_table.setToolTip("Pick the role of each part. The role decides its material and mass.")
        self.roles_note = QLabel("")
        self.roles_note.setWordWrap(True)
        self.roles_note.setObjectName("hint")
        self.param_widgets: dict = {}
        for key, label, unit, example in PARAMS:
            chk = QCheckBox(NO_IDEA)
            chk.setChecked(True)
            edit = QLineEdit()
            edit.setPlaceholderText(f"e.g. {example}")
            edit.setEnabled(False)
            chk.toggled.connect(lambda on, e=edit: e.setEnabled(not on))
            self.param_widgets[key] = (chk, edit)
        self.material = QComboBox()
        self.material.addItems([NO_IDEA] + list(MATERIALS))
        self.material.setToolTip("Typical values for the frame material; replace them with your supplier's numbers")
        self.param_message = QLabel("")
        self.param_message.setWordWrap(True)
        self.param_message.setStyleSheet(f"color:{theme.ERR}")
        self.addPage(_CadPage(self))
        self.addPage(_RolesPage(self))
        self.addPage(_ParamsPage(self))

    @staticmethod
    def _with_button(edit: QLineEdit, text: str, fn) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        b = QPushButton(text)
        b.setToolTip("Choose with a file dialog")
        b.clicked.connect(lambda checked=False: fn())
        h.addWidget(b)
        return w

    # ------------------------------------------------------------------ page 1
    def _pick_cad(self):
        f, _ = QFileDialog.getOpenFileName(self, "CAD file", "", CAD_FILTER)
        if f:
            self.set_cad(f)

    def set_cad(self, f: str) -> None:
        """Fill the CAD file and, when empty, the name and folder."""
        self.cad.setText(str(f))
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

    def check_cad(self) -> str:
        """Problem with page 1 in plain words ('' when fine); also shown in red."""
        cad, folder = Path(self.cad.text().strip()), self.folder.text().strip()
        msg = ""
        if not self.cad.text().strip() or not cad.exists():
            msg = "Choose an existing CAD file (STEP, STL or OBJ)."
        elif cad.suffix.lower() not in (".step", ".stp", ".stl", ".obj"):
            msg = "The CAD file must be STEP, STL or OBJ."
        elif not folder:
            msg = "Choose a project folder."
        self.message.setText(msg)
        return msg

    # ------------------------------------------------------------------ page 2
    def load_parts(self) -> None:
        """Read the part names from the CAD file and fill the role table with the guessed roles."""
        self.parts = {}
        self.roles_table.setRowCount(0)
        cad = Path(self.cad.text().strip())
        try:
            from opencae.drone import geometry as G
            names = [n for n, _p, _t in G.read_raw(cad)]
            self.parts = G.assign_roles(names, G.guess_roles(names))
        except Exception as e:  # noqa: BLE001 - shown to the person
            self.roles_note.setText(f"The parts could not be read ({e}). The project is still created; set the "
                                    "roles later in drone.json.")
            return
        from opencae.drone.types import ROLES
        for name, role in self.parts.items():
            r = self.roles_table.rowCount()
            self.roles_table.insertRow(r)
            for c, text in ((0, name), (1, role)):
                it = QTableWidgetItem(text)
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.roles_table.setItem(r, c, it)
            cb = QComboBox()
            cb.setEditable(True)
            cb.addItems(list(ROLES))
            cb.setCurrentText(role)
            cb.currentTextChanged.connect(lambda *_: self._update_roles_note())
            self.roles_table.setCellWidget(r, 2, cb)
        self._update_roles_note()

    def parts_roles(self) -> dict:
        """part name -> the role chosen in the table."""
        out = {}
        for r in range(self.roles_table.rowCount()):
            out[self.roles_table.item(r, 0).text()] = self.roles_table.cellWidget(r, 2).currentText().strip() or "other"
        return out

    def set_role(self, part: str, role: str) -> bool:
        for r in range(self.roles_table.rowCount()):
            if self.roles_table.item(r, 0).text() == part:
                self.roles_table.cellWidget(r, 2).setCurrentText(role)
                return True
        return False

    def _update_roles_note(self) -> None:
        roles = self.parts_roles()
        counts: dict = {}
        for role in roles.values():
            counts[role] = counts.get(role, 0) + 1
        text = f"{len(roles)} part(s): " + ", ".join(f"{n} {r}" for r, n in counts.items()) if roles else \
            "No parts were found in the CAD file."
        if "other" in counts:
            text += ". Parts marked 'other' are not given a role; the analyses ignore their mass until you set one."
        self.roles_note.setText(text)

    # ------------------------------------------------------------------ page 3
    def set_param(self, key: str, value) -> None:
        """Fill one of the key numbers (e.g. 'propulsion.motor.kv'); None goes back to 'I don't know yet'."""
        chk, edit = self.param_widgets[key]
        if value is None:
            chk.setChecked(True)
            edit.clear()
        else:
            chk.setChecked(False)
            edit.setText(str(value))

    def set_material(self, name: str) -> None:
        self.material.setCurrentText(name)

    def params(self) -> dict:
        """{dotted key: number} of the values that were filled in. Raises ValueError with a plain message."""
        from opencae.ui.drone.workspace import check_value
        out = {}
        for key, (chk, edit) in self.param_widgets.items():
            if chk.isChecked() or not edit.text().strip():
                continue
            num, msg = check_value(key, edit.text())
            if msg:
                raise ValueError(msg)
            out[key] = int(num) if num == int(num) and key in ("propulsion.battery.cells",) else num
        return out

    # ------------------------------------------------------------------ create
    def create(self) -> Optional[Path]:
        """Create the project, apply the role edits and the key numbers. On a problem: red message, None."""
        if self.check_cad():
            return None
        cad, folder = Path(self.cad.text().strip()), self.folder.text().strip()
        try:
            params = self.params()
        except ValueError as e:
            self.param_message.setText(str(e))
            self.message.setText(str(e))
            return None
        self.param_message.setText("")
        try:
            from opencae.drone import pipeline
            out = Path(pipeline.new_project(Path(folder), cad, self.name.text().strip() or Path(folder).name,
                                            self.type.currentText()))
        except Exception as e:  # shown to the person, never hidden
            self.message.setText(f"Could not create the project: {e}")
            return None
        try:
            self._apply_choices(out, params)
        except Exception as e:  # noqa: BLE001 - the project exists; the choices can be made in the sheet
            self.message.setText(f"The project was created, but the roles or numbers could not be applied: {e}")
            self.project_dir = out
            return out
        self.message.setText("")
        self.project_dir = out
        return out

    def _apply_choices(self, project: Path, params: dict) -> None:
        from opencae.drone import spec as S
        spec = S.load(project)
        roles = self.parts_roles()
        if roles and roles != self.parts:  # a role was edited: write every part by exact name so the edit wins
            new: dict = {}
            for part, role in roles.items():
                if role != "other":
                    new.setdefault(role, []).append(_glob_safe(part))
            spec["roles"] = new
            parts = (spec.get("mass") or {}).get("parts")
            if isinstance(parts, dict):
                for r in [r for r, v in parts.items() if v is None and r not in new]:
                    del parts[r]
                for r in new:
                    if r != "motor":
                        parts.setdefault(r, None)
        if params:
            spec = S.apply_overrides(spec, params)
            for key, val in params.items():
                node = spec
                *path, last = key.split(".")
                for p in path:
                    node = node[p]
                from opencae.ui.drone.workspace import unit_for
                node[last] = {"value": val, "unit": unit_for(key), "status": "estimate",
                              "source": "typed in the New drone wizard; replace with the datasheet value"}
        mat = self.material.currentText()
        if mat in MATERIALS and isinstance(spec.get("materials"), dict):
            key, rho, e, nu, sy = MATERIALS[mat]
            mats = spec["materials"]
            base = mats.pop("my_material", {})
            mats[key] = {"density_kg_m3": rho, "youngs_modulus_gpa": e, "poisson": nu, "yield_strength_mpa": sy,
                         "status": "estimate",
                         "source": "typical value for " + mat + ", not a datasheet; replace with your supplier's"}
            rm = spec.setdefault("role_materials", {})
            for role in (spec.get("roles") or {}):
                if role in STRUCTURAL:
                    rm[role] = key
        S.save(spec, project / "drone.json")

    def accept(self) -> None:
        if self.create() is not None:
            super().accept()
