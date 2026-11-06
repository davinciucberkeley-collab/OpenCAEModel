"""FDM print settings of one body (stored per study and body as a plain dict)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

INFILL_PATTERNS = ("rectilinear", "grid", "triangles", "cubic", "gyroid", "honeycomb", "concentric")
LEVELS = {1: "Level 1: homogeneous isotropic (screening)",
          2: "Level 2: orthotropic in the print frame",
          3: "Level 3: shell + infill regions"}


@dataclass
class PrintSettings:
    """Manufacturing parameters of a printed body. Lengths in mm, temperatures in degC.

    ``build_axis`` is the print (Z) direction in model coordinates; ``raster_axis`` an in-plane
    reference direction (material axis 1). Both are required for orthotropic analysis; nothing is
    assumed when they are missing (the analysis then warns and uses global Z)."""

    level: int = 1
    build_axis: Optional[list] = None
    raster_axis: Optional[list] = None
    layer_height: float = 0.2
    nozzle_diameter: float = 0.4
    line_width: float = 0.45
    perimeters: int = 2
    top_bottom_layers: int = 4
    infill_percent: float = 100.0
    infill_pattern: str = "grid"
    print_temperature: Optional[float] = None
    bed_temperature: Optional[float] = None
    post_processing: str = ""
    notes: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def wall_thickness(self) -> float:
        """Perimeter shell thickness [mm] = perimeters x line width."""
        return self.perimeters * self.line_width

    @property
    def top_bottom_thickness(self) -> float:
        return self.top_bottom_layers * self.layer_height

    def validate(self) -> list[str]:
        e = []
        if self.level not in LEVELS:
            e.append("level must be 1, 2 or 3")
        if not (0.0 < self.infill_percent <= 100.0):
            e.append("infill must be in (0, 100] %")
        if self.infill_pattern not in INFILL_PATTERNS:
            e.append(f"unknown infill pattern '{self.infill_pattern}'")
        for k in ("layer_height", "nozzle_diameter", "line_width"):
            if getattr(self, k) <= 0:
                e.append(f"{k} must be > 0")
        if self.layer_height > 0.8 * self.nozzle_diameter:
            e.append("layer height above 80 % of the nozzle diameter is outside normal FDM practice")
        if self.perimeters < 0 or self.top_bottom_layers < 0:
            e.append("perimeters and top/bottom layers must be >= 0")
        if self.level >= 2 and self.build_axis is None:
            e.append("levels 2 and 3 need the build (Z) direction of the print")
        return e

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PrintSettings":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (d or {}).items() if k in known})
