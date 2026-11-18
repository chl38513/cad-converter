# -*- coding: utf-8 -*-
"""纯标准库的 DXF 几何预览渲染器（生成 SVG，不依赖任何第三方库）。

支持 LINE / CIRCLE / ARC / LWPOLYLINE / POLYLINE / ELLIPSE / SPLINE /
TEXT / MTEXT / POINT / SOLID / 3DFACE / INSERT 等常见图元。
"""
from __future__ import annotations

import html
import math
import re
from typing import Any

ACI = {
    0: "#7a8290", 1: "#d33b32", 2: "#c9930b", 3: "#1e8e3e", 4: "#0e9c9c",
    5: "#2563c9", 6: "#9a3ec4", 7: "#252a32", 8: "#6b7280", 9: "#aab0b8",
    10: "#e05a4f", 11: "#7fd3d0", 12: "#b58a2b", 13: "#8fb96a", 14: "#5c9a72",
    30: "#e08a2b", 40: "#d9853b", 50: "#c8b24a", 60: "#a3b86c", 70: "#7fae7f",
    80: "#5fa08a", 90: "#4fa9a0", 100: "#3f9fae", 110: "#4b93bd", 120: "#5b86c9",
    130: "#6b78cc", 140: "#8269c9", 150: "#9a5fbc", 160: "#b055a8", 170: "#c34d94",
    180: "#cd4a7c", 190: "#d24a63", 200: "#d2504c", 210: "#cf5a3c", 220: "#c76730",
    230: "#bd7727", 240: "#b18920", 250: "#9d9a1b",
    251: "#5c6068", 252: "#70747c", 253: "#888c94", 254: "#a0a4ac", 255: "#b8bcc4",
}


def _color(code: str) -> str:
    try:
        n = int(float(code))
    except Exception:
        return ACI[7]
    if n in ACI:
        return ACI[n]
    return ACI.get(10 + (n % 24) * 10, "#252a32")


def _pairs(path: str) -> list[tuple[str, str]]:
    with open(path, "rb") as f:
        raw = f.read(4 * 1024 * 1024)
    lines = raw.decode("latin-1").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[tuple[str, str]] = []
    for i in range(0, len(lines) - 1, 2):
        out.append((lines[i].strip(), lines[i + 1]))
    return out


def _num(v: str, default: float = 0.0) -> float:
    try:
        return float(v.strip())
    except Exception:
        return default


def _first(tags: list[tuple[str, str]], code: str, default: str = "") -> str:
    for c, v in tags:
        if c == code:
            return v
    return default


def _all(tags: list[tuple[str, str]], code: str) -> list[str]:
    return [v for c, v in tags if c == code]


def render(path: str, width: int = 1000, height: int = 620,
           max_entities: int = 8000) -> dict[str, Any]:
    pairs = _pairs(path)

    layer_color: dict[str, str] = {}
    section = ""
    in_layer = False
    cur_layer: list[tuple[str, str]] = []
    entities: list[tuple[str, list[tuple[str, str]]]] = []
    cur_type = ""
    cur_tags: list[tuple[str, str]] = []
    in_entities = False

    def flush_layer() -> None:
        if cur_layer:
            name = _first(cur_layer, "2").strip()
            if name:
                layer_color[name.upper()] = _color(_first(cur_layer, "62", "7"))
        cur_layer.clear()

    def flush_entity() -> None:
        nonlocal cur_type, cur_tags
        if cur_type:
            entities.append((cur_type, cur_tags))
        cur_type, cur_tags = "", []

    for code, value in pairs:
        if code == "0":
            t = value.strip().upper()
            if t == "SECTION":
                flush_entity()
                section = ""
                continue
            if t == "ENDSEC":
                flush_entity()
                flush_layer()
                in_layer = False
                section = ""
                in_entities = False
                continue
            if t == "TABLE":
                flush_layer()
                in_layer = False
                continue
            if t == "ENDTAB":
                flush_layer()
                in_layer = False
                continue
            if t == "LAYER" and section == "TABLES":
                flush_layer()
                in_layer = True
                cur_layer = []
                continue
            if in_entities:
                flush_entity()
                cur_type = t
                cur_tags = []
                continue
            if in_layer:
                continue
            continue
        if code == "2" and section == "" and value.strip().upper() in (
                "HEADER", "TABLES", "BLOCKS", "ENTITIES", "OBJECTS", "CLASSES"):
            section = value.strip().upper()
            in_entities = section == "ENTITIES"
            continue
        if in_entities and cur_type:
            cur_tags.append((code, value))
        elif in_layer:
            cur_layer.append((code, value))
    flush_entity()
    flush_layer()

    # 收集几何与包围盒
    shapes: list[str] = []
    pts: list[tuple[float, float]] = []
    used_layers: set[str] = set()
    polyline_vertices: list[tuple[float, float]] = []
    polyline_active = False
    polyline_meta: dict[str, str] = {"color": ACI[7], "layer": "0"}
    polyline_closed = False
    count = 0

    def add_pt(x: float, y: float) -> None:
        if math.isfinite(x) and math.isfinite(y):
            pts.append((x, y))

    def poly_points(tags: list[tuple[str, str]]) -> list[tuple[float, float]]:
        xs = _all(tags, "10")
        ys = _all(tags, "20")
        return [(_num(xs[i]), _num(ys[i])) for i in range(min(len(xs), len(ys)))]

    for etype, tags in entities:
        if count >= max_entities:
            break
        if polyline_active:
            if etype == "VERTEX":
                polyline_vertices.append((_num(_first(tags, "10")), _num(_first(tags, "20"))))
                continue
            if etype == "SEQEND":
                if len(polyline_vertices) > 1:
                    for p in polyline_vertices:
                        add_pt(*p)
                    shapes.append(("polyline", polyline_vertices, polyline_meta, polyline_closed))
                    count += 1
                polyline_active = False
                polyline_vertices = []
                continue

        layer = _first(tags, "8", "0").strip().upper()
        used_layers.add(layer)
        color = _color(_first(tags, "62")) if _first(tags, "62") else layer_color.get(layer, ACI[7])
        meta = {"color": color, "layer": layer}

        if etype == "LINE":
            a = (_num(_first(tags, "10")), _num(_first(tags, "20")))
            b = (_num(_first(tags, "11")), _num(_first(tags, "21")))
            add_pt(*a); add_pt(*b)
            shapes.append(("line", (a, b), meta)); count += 1
        elif etype in ("CIRCLE", "ARC"):
            c = (_num(_first(tags, "10")), _num(_first(tags, "20")))
            r = _num(_first(tags, "40"))
            if r > 0:
                add_pt(c[0] - r, c[1] - r); add_pt(c[0] + r, c[1] + r)
                if etype == "CIRCLE":
                    shapes.append(("circle", (c, r), meta)); count += 1
                else:
                    shapes.append(("arc", (c, r, _num(_first(tags, "50")), _num(_first(tags, "51"))), meta))
                    count += 1
        elif etype == "LWPOLYLINE":
            pp = poly_points(tags)
            if len(pp) > 1:
                closed = bool(int(_num(_first(tags, "70"))) & 1)
                for p in pp:
                    add_pt(*p)
                shapes.append(("polyline", pp, meta, closed)); count += 1
        elif etype == "POLYLINE":
            polyline_active = True
            polyline_vertices = []
            polyline_meta = meta
            polyline_closed = bool(int(_num(_first(tags, "70"))) & 1)
        elif etype in ("SOLID", "3DFACE", "TRACE"):
            pp = [(_num(_all(tags, "10")[i]), _num(_all(tags, "20")[i]))
                  for i in range(min(len(_all(tags, "10")), len(_all(tags, "20"))))]
            if len(pp) >= 3:
                for p in pp:
                    add_pt(*p)
                shapes.append(("polyline", pp, meta, True)); count += 1
        elif etype == "ELLIPSE":
            c = (_num(_first(tags, "10")), _num(_first(tags, "20")))
            major = (_num(_first(tags, "11")), _num(_first(tags, "21")))
            ratio = _num(_first(tags, "40"), 0.5)
            t0 = _num(_first(tags, "41"), 0.0)
            t1 = _num(_first(tags, "42"), math.tau)
            if t1 <= t0:
                t1 += math.tau
            ml = math.hypot(*major)
            if ml > 0 and ratio > 0:
                ang = math.atan2(major[1], major[0])
                ca, sa = math.cos(ang), math.sin(ang)
                pp = []
                for i in range(65):
                    t = t0 + (t1 - t0) * i / 64
                    ex, ey = ml * math.cos(t), ml * ratio * math.sin(t)
                    pp.append((c[0] + ex * ca - ey * sa, c[1] + ex * sa + ey * ca))
                for p in pp:
                    add_pt(*p)
                shapes.append(("polyline", pp, meta, False)); count += 1
        elif etype == "SPLINE":
            pp = poly_points(tags)
            if len(pp) > 1:
                for p in pp:
                    add_pt(*p)
                shapes.append(("polyline", pp, meta, False)); count += 1
        elif etype == "POINT":
            p = (_num(_first(tags, "10")), _num(_first(tags, "20")))
            add_pt(*p)
            shapes.append(("point", p, meta)); count += 1
        elif etype in ("TEXT", "MTEXT", "ATTRIB"):
            p = (_num(_first(tags, "10")), _num(_first(tags, "20")))
            h = _num(_first(tags, "40"), 5)
            txt = _first(tags, "1") or _first(tags, "3")
            txt = txt.replace("\\P", " ").replace("\\X", " ")
            txt = re.sub(r"\\[A-Za-z][^;]*;", "", txt).replace("{", "").replace("}", "")
            txt = txt.strip()
            if txt:
                add_pt(p[0], p[1]); add_pt(p[0] + h * max(1, len(txt)) * 0.6, p[1] + h)
                shapes.append(("text", (p, h, txt, _num(_first(tags, "50"))), meta))
                count += 1
        elif etype == "INSERT":
            p = (_num(_first(tags, "10")), _num(_first(tags, "20")))
            add_pt(p[0] - 2, p[1] - 2); add_pt(p[0] + 2, p[1] + 2)
            shapes.append(("insert", p, meta)); count += 1

    if not pts:
        return {"svg": _empty_svg(width, height, "未识别到可预览的图元"),
                "count": 0, "layers": [], "bounds": [], "truncated": False}

    min_x = min(p[0] for p in pts); max_x = max(p[0] for p in pts)
    min_y = min(p[1] for p in pts); max_y = max(p[1] for p in pts)
    dx = max(max_x - min_x, 1e-6); dy = max(max_y - min_y, 1e-6)
    pad = 28
    scale = min((width - 2 * pad) / dx, (height - 2 * pad) / dy)
    scale = max(scale, 1e-9)
    off_x = (width - dx * scale) / 2
    off_y = (height - dy * scale) / 2

    def X(x: float) -> float:
        return off_x + (x - min_x) * scale

    def Y(y: float) -> float:
        return off_y + (max_y - y) * scale

    def stroke(color: str) -> str:
        return f'stroke="{color}" fill="none" stroke-width="1.5" vector-effect="non-scaling-stroke"'

    body: list[str] = []
    for item in shapes:
        kind, data, meta = item[0], item[1], item[2]
        color = meta["color"]
        if kind == "line":
            (a, b) = data
            body.append(f'<line x1="{X(a[0]):.2f}" y1="{Y(a[1]):.2f}" '
                        f'x2="{X(b[0]):.2f}" y2="{Y(b[1]):.2f}" {stroke(color)}/>')
        elif kind == "circle":
            (c, r) = data
            body.append(f'<circle cx="{X(c[0]):.2f}" cy="{Y(c[1]):.2f}" '
                        f'r="{r * scale:.2f}" {stroke(color)}/>')
        elif kind == "arc":
            (c, r, a0, a1) = data
            if a1 <= a0:
                a1 += 360
            st = math.radians(a0); en = math.radians(a1)
            p0 = (c[0] + r * math.cos(st), c[1] + r * math.sin(st))
            p1 = (c[0] + r * math.cos(en), c[1] + r * math.sin(en))
            large = 1 if (a1 - a0) > 180 else 0
            body.append(f'<path d="M {X(p0[0]):.2f} {Y(p0[1]):.2f} '
                        f'A {r * scale:.2f} {r * scale:.2f} 0 {large} 0 '
                        f'{X(p1[0]):.2f} {Y(p1[1]):.2f}" {stroke(color)}/>')
        elif kind == "polyline":
            pp, closed = data, item[3]
            d = "M " + " L ".join(f"{X(p[0]):.2f} {Y(p[1]):.2f}" for p in pp)
            if closed:
                d += " Z"
            body.append(f'<path d="{d}" {stroke(color)}/>')
        elif kind == "point":
            x, y = X(data[0]), Y(data[1])
            body.append(f'<path d="M {x-4:.2f} {y:.2f} L {x+4:.2f} {y:.2f} '
                        f'M {x:.2f} {y-4:.2f} L {x:.2f} {y+4:.2f}" {stroke(color)}/>')
        elif kind == "text":
            (p, h, txt, rot) = data
            fs = max(7, min(44, h * scale))
            body.append(f'<text x="{X(p[0]):.2f}" y="{Y(p[1]):.2f}" fill="{color}" '
                        f'font-size="{fs:.1f}" font-family="Segoe UI, Microsoft YaHei, sans-serif" '
                        f'transform="rotate({-rot:.1f} {X(p[0]):.2f} {Y(p[1]):.2f})">'
                        f'{html.escape(txt[:120])}</text>')
        elif kind == "insert":
            x, y = X(data[0]), Y(data[1])
            body.append(f'<rect x="{x-3:.2f}" y="{y-3:.2f}" width="6" height="6" '
                        f'fill="none" stroke="{color}" stroke-width="1"/>')

    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
           f'width="100%" height="100%" preserveAspectRatio="xMidYMid meet">'
           f'<rect width="{width}" height="{height}" fill="#fbfcfe"/>'
           f'<g id="cad-geometry">{"".join(body)}</g></svg>')
    return {
        "svg": svg, "count": count, "layers": sorted(used_layers),
        "bounds": [round(min_x, 2), round(min_y, 2), round(max_x, 2), round(max_y, 2)],
        "truncated": len(entities) > max_entities,
    }


def _empty_svg(width: int, height: int, message: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="100%" height="100%"><rect width="{width}" height="{height}" fill="#fbfcfe"/>'
            f'<text x="{width/2}" y="{height/2}" text-anchor="middle" fill="#8a919c" '
            f'font-size="15" font-family="Segoe UI, Microsoft YaHei, sans-serif">'
            f'{html.escape(message)}</text></svg>')
