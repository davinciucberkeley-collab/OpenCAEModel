"""Optional, recorded and reversible geometry simplification (defeaturing) for analysis.

Candidates are faces that are small compared with the part and unlikely to matter for the
chosen analysis: small *convex* fillets and chamfers, and tiny faces (embossed text, logos,
cosmetic details). Never proposed:

* holes, concave fillets and faces next to re-entrant corners (stress raisers);
* faces carrying loads, supports, interactions or flow conditions;
* faces of thin regions (wall thickness below the threshold);

The change is a ``defeature`` feature in the history: the original stays in the history, the
removed faces and the volume / area change are recorded, and suppressing the feature restores
the original geometry exactly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from opencae.geometry.analysis import GeometryReport


@dataclass
class Candidate:
    face: int
    reason: str


def candidates(rep: GeometryReport, protected: Optional[set[int]] = None, fillet_frac: float = 0.01,
               face_frac: float = 0.01) -> list[Candidate]:
    """Faces that may be removed. ``fillet_frac``: convex fillets with r < fillet_frac x size;
    ``face_frac``: faces smaller than (face_frac x size)^2."""
    protected = set(protected or ())
    L = rep.size
    out = []
    for f in rep.faces:
        if f.index in protected or f.role in ("hole", "fillet_concave"):
            continue
        if f.thickness is not None and rep.min_thickness and f.thickness <= 1.5 * rep.min_thickness and \
                f.area > (face_frac * L) ** 2:
            continue  # thin wall: may matter
        if f.role == "fillet_convex" and f.min_radius < fillet_frac * L:
            out.append(Candidate(f.index, f"convex fillet r = {f.min_radius:.3g} < {fillet_frac:g} x size"))
        elif f.area < (face_frac * L) ** 2 and f.role != "hole":
            out.append(Candidate(f.index, f"small face, area {f.area:.3g} < ({face_frac:g} x size)²"))
    return out


def defeature(session, body_id: str, faces: list[int], reasons: Optional[list[str]] = None):
    """Add a defeaturing feature (removing ``faces`` of the body) and return a comparison:
    {"feature", "volume_before", "volume_after", "area_before", "area_after", "faces_removed"}."""
    from opencae.project.model import Feature
    b = session.bodies[body_id]
    before = {"volume": b.volume, "area": b.area, "faces": len(b.faces)}
    f = Feature("defeature", {"body": body_id, "faces": [int(i) for i in faces],
                              "reasons": list(reasons or [""] * len(faces)), "original": before},
                name=f"Simplify {b.name} ({len(faces)} face(s))")
    session.add_feature(f)
    err = session.regen.errors.get(f.id)
    if err:
        session.project.geometry.features.remove(f)
        session.regenerate()
        raise ValueError(err)
    a = session.bodies[body_id]
    return {"feature": f.id, "volume_before": before["volume"], "volume_after": a.volume,
            "area_before": before["area"], "area_after": a.area, "faces_before": before["faces"],
            "faces_after": len(a.faces), "faces_removed": [int(i) + 1 for i in faces],
            "volume_change": (a.volume - before["volume"]) / before["volume"]}
