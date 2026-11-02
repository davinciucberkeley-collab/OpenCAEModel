"""Engineering icons drawn with QPainter (original artwork, no external assets)."""
from __future__ import annotations

import math
from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

S = 64
BLUE, ORANGE, GREEN, RED, GREY, WHITE = "#5aa6f0", "#f0a03c", "#5cc98a", "#ef5b55", "#9aa4b4", "#e8ecf2"
PURPLE, CYAN, YELLOW = "#b48cf2", "#4fd1d9", "#f2d14c"


def _pen(c, w=3.0):
    p = QPen(QColor(c))
    p.setWidthF(w)
    p.setJoinStyle(Qt.RoundJoin)
    p.setCapStyle(Qt.RoundCap)
    return p


def _poly(*pts):
    return QPolygonF([QPointF(x, y) for x, y in pts])


def _iso_box(p: QPainter, x, y, w, h, d, c):
    col = QColor(c)
    top = _poly((x, y + d), (x + d, y), (x + w + d, y), (x + w, y + d))
    front = _poly((x, y + d), (x + w, y + d), (x + w, y + d + h), (x, y + d + h))
    side = _poly((x + w, y + d), (x + w + d, y), (x + w + d, y + h), (x + w, y + d + h))
    p.setPen(_pen("#1b1f26", 2))
    p.setBrush(col.lighter(130))
    p.drawPolygon(top)
    p.setBrush(col)
    p.drawPolygon(front)
    p.setBrush(col.darker(140))
    p.drawPolygon(side)


def _arrow(p: QPainter, x1, y1, x2, y2, c, w=4.0, head=11):
    p.setPen(_pen(c, w))
    p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
    a = math.atan2(y2 - y1, x2 - x1)
    pts = [(x2, y2), (x2 - head * math.cos(a - 0.45), y2 - head * math.sin(a - 0.45)),
           (x2 - head * math.cos(a + 0.45), y2 - head * math.sin(a + 0.45))]
    p.setBrush(QColor(c))
    p.setPen(_pen(c, 1))
    p.drawPolygon(_poly(*pts))


def _hatch(p: QPainter, x, y, w):
    p.setPen(_pen(GREY, 3))
    p.drawLine(QPointF(x, y), QPointF(x + w, y))
    p.setPen(_pen(GREY, 2))
    for i in range(0, int(w) + 1, 8):
        p.drawLine(QPointF(x + i, y), QPointF(x + i - 7, y + 8))


def _draw(name: str, p: QPainter) -> None:
    if name == "new":
        p.setPen(_pen(WHITE, 3)); p.setBrush(QColor("#323944"))
        p.drawPolygon(_poly((14, 6), (40, 6), (52, 18), (52, 58), (14, 58)))
        p.drawLine(40, 6, 40, 18); p.drawLine(40, 18, 52, 18)
        p.setPen(_pen(GREEN, 4)); p.drawLine(26, 38, 40, 38); p.drawLine(33, 31, 33, 45)
    elif name == "open":
        p.setPen(_pen(ORANGE, 3)); p.setBrush(QColor(ORANGE).darker(160))
        p.drawPolygon(_poly((6, 16), (24, 16), (29, 22), (56, 22), (56, 52), (6, 52)))
        p.setBrush(QColor(ORANGE).darker(110)); p.drawPolygon(_poly((12, 30), (60, 30), (54, 52), (6, 52)))
    elif name == "save":
        p.setPen(_pen(BLUE, 3)); p.setBrush(QColor(BLUE).darker(170))
        p.drawRoundedRect(QRectF(8, 8, 48, 48), 5, 5)
        p.setBrush(QColor(WHITE)); p.drawRect(QRectF(18, 8, 26, 16))
        p.setBrush(QColor("#1b1f26")); p.drawRect(QRectF(16, 34, 32, 22))
    elif name == "undo" or name == "redo":
        if name == "redo":
            p.translate(S, 0); p.scale(-1, 1)
        path = QPainterPath(QPointF(16, 28)); path.cubicTo(28, 12, 54, 16, 50, 44)
        p.setPen(_pen(WHITE, 4)); p.setBrush(Qt.NoBrush); p.drawPath(path)
        p.setBrush(QColor(WHITE)); p.drawPolygon(_poly((8, 32), (22, 16), (26, 34)))
    elif name == "box":
        _iso_box(p, 10, 14, 30, 30, 14, BLUE)
    elif name == "plate":
        _iso_box(p, 6, 22, 40, 8, 18, BLUE)
    elif name == "beam":
        _iso_box(p, 4, 22, 46, 12, 10, BLUE)
    elif name == "cantilever":
        _iso_box(p, 14, 20, 38, 10, 8, BLUE)
        p.setPen(_pen(GREY, 3)); p.drawLine(10, 10, 10, 52)
        for yy in range(12, 52, 8):
            p.drawLine(10, yy, 4, yy + 6)
        _arrow(p, 56, 8, 56, 26, RED, 3, 8)
    elif name in ("cylinder", "cone"):
        col = QColor(BLUE)
        p.setPen(_pen("#1b1f26", 2)); p.setBrush(col)
        if name == "cylinder":
            p.drawRect(QRectF(14, 16, 36, 34)); p.setBrush(col.darker(130)); p.drawEllipse(QRectF(14, 42, 36, 14))
            p.setBrush(col); p.setPen(Qt.NoPen); p.drawRect(QRectF(15, 16, 34, 33)); p.setPen(_pen("#1b1f26", 2))
            p.setBrush(col.lighter(130)); p.drawEllipse(QRectF(14, 9, 36, 14))
        else:
            p.drawPolygon(_poly((32, 6), (52, 48), (12, 48))); p.setBrush(col.darker(130)); p.drawEllipse(QRectF(12, 41, 40, 14))
    elif name == "sphere":
        from PySide6.QtGui import QRadialGradient
        g = QRadialGradient(QPointF(24, 22), 34); g.setColorAt(0, QColor(BLUE).lighter(160)); g.setColorAt(1, QColor(BLUE).darker(160))
        p.setPen(_pen("#1b1f26", 2)); p.setBrush(QBrush(g)); p.drawEllipse(QRectF(8, 8, 48, 48))
    elif name == "import":
        _iso_box(p, 22, 22, 22, 22, 10, GREY); _arrow(p, 4, 12, 26, 30, GREEN, 4)
    elif name == "export":
        _iso_box(p, 6, 22, 22, 22, 10, GREY); _arrow(p, 32, 30, 58, 10, ORANGE, 4)
    elif name in ("union", "subtract", "intersect"):
        a, b = QRectF(8, 14, 32, 32), QRectF(24, 14, 32, 32)
        pa, pb = QPainterPath(), QPainterPath(); pa.addEllipse(a); pb.addEllipse(b)
        res = {"union": pa.united(pb), "subtract": pa.subtracted(pb), "intersect": pa.intersected(pb)}[name]
        p.setPen(_pen(GREY, 2)); p.setBrush(Qt.NoBrush); p.drawPath(pa); p.drawPath(pb)
        p.setPen(_pen(WHITE, 2)); p.setBrush(QColor(BLUE)); p.drawPath(res)
    elif name == "fillet":
        p.setPen(_pen(GREY, 3)); p.drawLine(10, 54, 10, 30); p.drawLine(34, 10, 56, 10)
        path = QPainterPath(QPointF(10, 30)); path.quadTo(10, 10, 34, 10)
        p.setPen(_pen(ORANGE, 5)); p.setBrush(Qt.NoBrush); p.drawPath(path)
    elif name == "chamfer":
        p.setPen(_pen(GREY, 3)); p.drawLine(10, 54, 10, 30); p.drawLine(34, 10, 56, 10)
        p.setPen(_pen(ORANGE, 5)); p.drawLine(10, 30, 34, 10)
    elif name == "extrude":
        p.setPen(_pen(GREY, 2)); p.setBrush(QColor(BLUE).darker(150)); p.drawPolygon(_poly((8, 44), (36, 44), (48, 34), (20, 34)))
        _arrow(p, 28, 38, 28, 8, GREEN, 4)
    elif name == "transform":
        _arrow(p, 32, 32, 58, 32, RED, 3, 8); _arrow(p, 32, 32, 32, 6, GREEN, 3, 8); _arrow(p, 32, 32, 12, 52, BLUE, 3, 8)
    elif name == "check":
        p.setPen(_pen(GREEN, 6)); p.drawPolyline(_poly((12, 32), (26, 46), (52, 16)))
    elif name == "inspect":
        p.setPen(_pen(WHITE, 4)); p.setBrush(Qt.NoBrush); p.drawEllipse(QRectF(10, 10, 30, 30)); p.drawLine(36, 36, 54, 54)
    elif name == "mesh":
        p.setPen(_pen(CYAN, 2)); p.setBrush(QColor(CYAN).darker(260))
        pts = [(8, 52), (32, 52), (56, 52), (20, 32), (44, 32), (32, 12)]
        for a, b, c in ((0, 1, 3), (1, 4, 3), (1, 2, 4), (3, 4, 5)):
            p.drawPolygon(_poly(pts[a], pts[b], pts[c]))
    elif name == "mesh_settings":
        _draw("mesh", p); p.setPen(_pen(ORANGE, 3)); p.setBrush(QColor("#1b1f26")); p.drawEllipse(QRectF(36, 36, 22, 22))
        p.drawLine(47, 40, 47, 54); p.drawLine(40, 47, 54, 47)
    elif name == "refine":
        p.setPen(_pen(CYAN, 2)); p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(8, 8, 48, 48)); p.drawLine(32, 8, 32, 56); p.drawLine(8, 32, 56, 32)
        p.drawLine(20, 8, 20, 32); p.drawLine(8, 20, 32, 20)
        p.drawLine(14, 8, 14, 20); p.drawLine(8, 14, 20, 14)
    elif name == "quality":
        cols = [GREEN, YELLOW, ORANGE, RED]
        for i, h in enumerate((40, 30, 18, 8)):
            p.setPen(Qt.NoPen); p.setBrush(QColor(cols[i])); p.drawRect(QRectF(8 + i * 12, 56 - h, 10, h))
    elif name == "material":
        p.setPen(_pen("#1b1f26", 2))
        for i, c in enumerate((GREY, ORANGE, BLUE)):
            p.setBrush(QColor(c)); p.drawRoundedRect(QRectF(8 + i * 8, 36 - i * 12, 40, 14), 3, 3)
    elif name == "fixed":
        p.setPen(_pen(BLUE, 3)); p.setBrush(QColor(BLUE).darker(140)); p.drawPolygon(_poly((32, 10), (50, 40), (14, 40)))
        _hatch(p, 8, 44, 48)
    elif name == "displacement":
        p.setPen(_pen(BLUE, 3)); p.setBrush(QColor(BLUE).darker(140)); p.drawPolygon(_poly((20, 14), (36, 40), (4, 40)))
        _hatch(p, 2, 44, 36); _arrow(p, 36, 24, 60, 24, YELLOW, 4, 9)
    elif name == "symmetry":
        p.setPen(_pen(PURPLE, 3)); p.drawLine(32, 4, 32, 60)
        p.setPen(_pen(PURPLE, 2)); p.drawText(QRectF(8, 18, 20, 28), Qt.AlignCenter, "S")
        _iso_box(p, 36, 22, 16, 18, 6, GREY)
    elif name == "force":
        _arrow(p, 12, 52, 50, 14, RED, 5, 14)
    elif name == "pressure":
        for x in (14, 32, 50):
            _arrow(p, x, 6, x, 38, ORANGE, 3, 9)
        p.setPen(_pen(GREY, 4)); p.drawLine(6, 46, 58, 46)
    elif name == "gravity":
        p.setPen(_pen(WHITE, 2)); p.setFont(p.font()); _arrow(p, 32, 6, 32, 50, GREEN, 5, 13)
        p.setPen(_pen(GREEN, 2)); p.drawText(QRectF(36, 4, 24, 22), Qt.AlignCenter, "g")
    elif name == "solve":
        p.setPen(_pen(GREEN, 2)); p.setBrush(QColor(GREEN)); p.drawPolygon(_poly((16, 8), (56, 32), (16, 56)))
    elif name == "stop":
        p.setPen(_pen(RED, 2)); p.setBrush(QColor(RED)); p.drawRoundedRect(QRectF(12, 12, 40, 40), 5, 5)
    elif name == "validate":
        p.setPen(_pen(WHITE, 3)); p.setBrush(QColor("#323944")); p.drawRoundedRect(QRectF(8, 6, 40, 52), 4, 4)
        p.setPen(_pen(GREEN, 4)); p.drawPolyline(_poly((16, 22), (22, 28), (32, 16)))
        p.drawPolyline(_poly((16, 42), (22, 48), (32, 36))); p.setPen(_pen(GREY, 3))
        p.drawLine(36, 24, 44, 24); p.drawLine(36, 44, 44, 44)
    elif name == "contour":
        cols = ["#3b4cc0", "#6f92f3", "#aac7fd", "#f7b89c", "#e7745b", "#b40426"]
        for i, c in enumerate(cols):
            p.setPen(Qt.NoPen); p.setBrush(QColor(c)); p.drawRect(QRectF(8 + i * 8, 10, 8, 44))
    elif name == "deform":
        p.setPen(_pen(GREY, 2)); p.setBrush(Qt.NoBrush); p.drawRect(QRectF(6, 24, 50, 12))
        path = QPainterPath(QPointF(6, 24)); path.quadTo(36, 26, 56, 42)
        path2 = QPainterPath(QPointF(6, 36)); path2.quadTo(36, 38, 54, 54)
        p.setPen(_pen(ORANGE, 3)); p.drawPath(path); p.drawPath(path2); p.drawLine(56, 42, 54, 54)
    elif name == "animate":
        _draw("deform", p); p.setPen(_pen(GREEN, 2)); p.setBrush(QColor(GREEN)); p.drawPolygon(_poly((40, 4), (56, 13), (40, 22)))
    elif name == "probe":
        p.setPen(_pen(YELLOW, 3)); p.drawLine(10, 54, 34, 30); p.setBrush(QColor(YELLOW)); p.drawEllipse(QRectF(30, 10, 22, 22))
    elif name == "report":
        p.setPen(_pen(WHITE, 3)); p.setBrush(QColor("#323944")); p.drawRect(QRectF(12, 6, 40, 52))
        for i, w in enumerate((26, 20, 26, 14)):
            p.setPen(_pen(GREY if i % 2 else BLUE, 3)); p.drawLine(18, 16 + i * 10, 18 + w, 16 + i * 10)
    elif name == "screenshot":
        p.setPen(_pen(WHITE, 3)); p.setBrush(QColor("#323944")); p.drawRoundedRect(QRectF(6, 18, 52, 36), 5, 5)
        p.drawRect(QRectF(20, 10, 18, 8)); p.setBrush(QColor(BLUE)); p.drawEllipse(QRectF(22, 24, 22, 22))
    elif name.startswith("view_"):
        _iso_box(p, 14, 18, 26, 26, 12, GREY)
        hi = {"view_front": _poly((14, 30), (40, 30), (40, 56), (14, 56)),
              "view_back": _poly((14, 30), (40, 30), (40, 56), (14, 56)),
              "view_top": _poly((14, 30), (26, 18), (52, 18), (40, 30)),
              "view_bottom": _poly((14, 56), (26, 44), (52, 44), (40, 56)),
              "view_right": _poly((40, 30), (52, 18), (52, 44), (40, 56)),
              "view_left": _poly((14, 30), (26, 18), (26, 44), (14, 56)),
              "view_iso": None}.get(name)
        if hi is not None:
            p.setPen(_pen(ORANGE, 2)); c = QColor(ORANGE); c.setAlpha(170); p.setBrush(c); p.drawPolygon(hi)
        else:
            _iso_box(p, 14, 18, 26, 26, 12, ORANGE)
    elif name == "fit":
        p.setPen(_pen(WHITE, 4))
        for (x, y, dx, dy) in ((8, 8, 1, 1), (56, 8, -1, 1), (8, 56, 1, -1), (56, 56, -1, -1)):
            p.drawLine(x, y, x + 14 * dx, y); p.drawLine(x, y, x, y + 14 * dy)
        _iso_box(p, 20, 22, 16, 16, 8, BLUE)
    elif name == "perspective":
        p.setPen(_pen(WHITE, 3)); p.setBrush(Qt.NoBrush); p.drawPolygon(_poly((10, 10), (54, 18), (54, 46), (10, 54)))
    elif name == "ortho":
        p.setPen(_pen(WHITE, 3)); p.setBrush(Qt.NoBrush); p.drawRect(QRectF(10, 14, 44, 36))
    elif name in ("shaded", "shaded_edges", "wireframe", "mesh_view"):
        col = QColor(BLUE)
        pts = ((10, 50), (32, 8), (54, 50))
        if name != "wireframe":
            p.setPen(Qt.NoPen); p.setBrush(col); p.drawPolygon(_poly(*pts))
        if name in ("shaded_edges", "wireframe"):
            p.setPen(_pen(WHITE, 3)); p.setBrush(Qt.NoBrush); p.drawPolygon(_poly(*pts))
        if name in ("wireframe", "mesh_view"):
            p.setPen(_pen(WHITE, 2)); p.drawLine(21, 29, 43, 29); p.drawLine(21, 29, 32, 50); p.drawLine(43, 29, 32, 50)
            if name == "mesh_view":
                p.drawPolygon(_poly(*pts))
    elif name == "background":
        from PySide6.QtGui import QLinearGradient
        g = QLinearGradient(0, 8, 0, 56); g.setColorAt(0, QColor("#6a7891")); g.setColorAt(1, QColor("#16191e"))
        p.setPen(_pen(WHITE, 2)); p.setBrush(QBrush(g)); p.drawRoundedRect(QRectF(8, 8, 48, 48), 5, 5)
    elif name.startswith("select_"):
        kind = name.split("_", 1)[1]
        _iso_box(p, 12, 18, 28, 28, 12, GREY)
        p.setPen(_pen(ORANGE, 4)); c = QColor(ORANGE)
        if kind == "body":
            p.setBrush(Qt.NoBrush); p.drawRect(QRectF(6, 10, 50, 46))
        elif kind == "face":
            c.setAlpha(200); p.setBrush(c); p.drawPolygon(_poly((12, 30), (40, 30), (40, 58), (12, 58)))
        elif kind == "edge":
            p.drawLine(40, 30, 52, 18)
        elif kind == "vertex":
            p.setBrush(c); p.drawEllipse(QRectF(35, 25, 10, 10))
        elif kind == "node":
            p.setBrush(QColor(CYAN)); p.setPen(_pen(CYAN, 2)); p.drawEllipse(QRectF(22, 36, 9, 9))
        elif kind == "element":
            p.setPen(_pen(CYAN, 3)); p.setBrush(QColor(CYAN).darker(180)); p.drawPolygon(_poly((14, 54), (30, 34), (38, 54)))
    elif name == "delete":
        p.setPen(_pen(RED, 6)); p.drawLine(16, 16, 48, 48); p.drawLine(48, 16, 16, 48)
    elif name == "settings":
        p.setPen(_pen(GREY, 6)); p.setBrush(Qt.NoBrush)
        for i in range(8):
            a = i * math.pi / 4
            p.drawLine(QPointF(32 + 16 * math.cos(a), 32 + 16 * math.sin(a)), QPointF(32 + 24 * math.cos(a), 32 + 24 * math.sin(a)))
        p.drawEllipse(QRectF(16, 16, 32, 32))
    elif name == "help":
        p.setPen(_pen(BLUE, 3)); p.setBrush(QColor(BLUE).darker(170)); p.drawEllipse(QRectF(8, 8, 48, 48))
        f = p.font(); f.setPixelSize(32); f.setBold(True); p.setFont(f); p.setPen(_pen(WHITE, 2))
        p.drawText(QRectF(8, 8, 48, 48), Qt.AlignCenter, "?")
    elif name == "app":
        _iso_box(p, 8, 16, 34, 30, 12, BLUE)
        p.setPen(_pen(CYAN, 2)); p.setBrush(Qt.NoBrush)
        p.drawLine(8, 28, 42, 62 - 16); p.drawLine(25, 28, 25, 58); p.drawLine(8, 44, 42, 44)
        _arrow(p, 56, 4, 56, 24, RED, 4, 9)
    else:  # unknown -> visible placeholder so missing icons are obvious during development
        p.setPen(_pen(GREY, 2)); p.setBrush(Qt.NoBrush); p.drawRect(QRectF(10, 10, 44, 44))


@lru_cache(maxsize=None)
def icon(name: str) -> QIcon:
    pm = QPixmap(S, S)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    try:
        _draw(name, p)
    finally:
        p.end()
    return QIcon(pm)
