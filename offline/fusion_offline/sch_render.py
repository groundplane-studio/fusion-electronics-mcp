"""SVG preview of a schematic layout (sch_layout.Drawing) with the real library
symbols, so a layout can be reviewed before anything is written to Fusion."""

from __future__ import annotations

import html
import math
from collections import Counter

from .design import transform

SYM, WIRE, LBL, PWR, TXT, PIN = "#a3262a", "#2f8a3c", "#2456a6", "#6b4fa0", "#333", "#7a2a2a"


def _readable(angle: float) -> float:
    a = angle % 360
    return a - 180 if 90 < a <= 270 else a


def render_svg(d, geo: dict, values: dict, supply_geo: dict, scale: float = 4.0, title: str = "",
               pad: float = 6.0) -> str:
    x0, y0, x1, y1 = d.extent()
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad + (6 if title else 0)
    W, H = (x1 - x0) * scale, (y1 - y0) * scale
    X = lambda x: round((x - x0) * scale, 2)
    Y = lambda y: round((y1 - y) * scale, 2)
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" viewBox="0 0 {W:.0f} {H:.0f}" '
         f'font-family="IBM Plex Mono, Consolas, monospace"><rect width="100%" height="100%" fill="#fff"/>']
    if title:
        o.append(f'<text x="{X(x0 + pad)}" y="{Y(y1 - 4)}" font-size="{3.2 * scale}" font-weight="700" fill="{TXT}">'
                 f'{html.escape(title)}</text>')

    def draw_geo(g, x, y, rot, mir, texts: dict, color=SYM):
        for p in g.prims:
            k = p[0]
            if k == "wire":
                a = transform(p[1], p[2], x, y, rot, mir)
                b = transform(p[3], p[4], x, y, rot, mir)
                if p[6]:
                    r = math.hypot(b[0] - a[0], b[1] - a[1]) / (2 * math.sin(math.radians(abs(p[6]) / 2)))
                    sweep = 0 if (p[6] > 0) != mir else 1
                    o.append(f'<path d="M{X(a[0])},{Y(a[1])} A{r * scale:.2f},{r * scale:.2f} 0 0 {sweep} '
                             f'{X(b[0])},{Y(b[1])}" fill="none" stroke="{color}" stroke-width="{max(p[5], 0.15) * scale:.2f}"/>')
                else:
                    o.append(f'<line x1="{X(a[0])}" y1="{Y(a[1])}" x2="{X(b[0])}" y2="{Y(b[1])}" stroke="{color}" '
                             f'stroke-width="{max(p[5], 0.15) * scale:.2f}" stroke-linecap="round"/>')
            elif k == "circle":
                c = transform(p[1], p[2], x, y, rot, mir)
                fill = color if p[4] == 0 else "none"
                o.append(f'<circle cx="{X(c[0])}" cy="{Y(c[1])}" r="{p[3] * scale:.2f}" fill="{fill}" stroke="{color}" '
                         f'stroke-width="{max(p[4], 0.15) * scale:.2f}"/>')
            elif k == "rect":
                a = transform(p[1], p[2], x, y, rot, mir)
                b = transform(p[3], p[4], x, y, rot, mir)
                o.append(f'<rect x="{min(X(a[0]), X(b[0]))}" y="{min(Y(a[1]), Y(b[1]))}" width="{abs(X(b[0]) - X(a[0]))}" '
                         f'height="{abs(Y(b[1]) - Y(a[1]))}" fill="{color}"/>')
            elif k == "poly":
                pts = " ".join(f"{X(q[0])},{Y(q[1])}" for q in (transform(v[0], v[1], x, y, rot, mir) for v in p[1]))
                o.append(f'<polygon points="{pts}" fill="{color}" stroke="{color}" stroke-width="{0.2 * scale}"/>')
            elif k == "text":
                s = texts.get(p[4], p[4] if not p[4].startswith(">") else "")
                if not s:
                    continue
                t = transform(p[1], p[2], x, y, rot, mir)
                ang = _readable(p[5] + rot)
                anchor = "middle" if "center" in p[6] else ("end" if ((mir and (p[5] + rot) % 180 == 0) or
                                                                     90 < (p[5] + rot) % 360 <= 270) else "start")
                col = TXT if p[4] == ">VALUE" else color
                o.append(f'<text x="{X(t[0])}" y="{Y(t[1])}" font-size="{p[3] * scale:.1f}" fill="{col}" '
                         f'text-anchor="{anchor}" transform="rotate({-ang} {X(t[0])} {Y(t[1])})">{html.escape(s)}</text>')
        for pn in g.pins.values():
            a = transform(pn.x, pn.y, x, y, rot, mir)
            b = transform(pn.x - pn.ox * pn.length, pn.y - pn.oy * pn.length, x, y, rot, mir)
            o.append(f'<line x1="{X(a[0])}" y1="{Y(a[1])}" x2="{X(b[0])}" y2="{Y(b[1])}" stroke="{PIN}" '
                     f'stroke-width="{0.25 * scale}"/>')
            if pn.visible in ("both", "pin") and len(g.pins) > 2 and not g.supply:
                ix, iy = transform(pn.x - pn.ox * (pn.length + 0.8), pn.y - pn.oy * (pn.length + 0.8), x, y, rot, mir)
                ox, oy = transform(pn.ox, pn.oy, 0, 0, rot, mir)
                anchor = "end" if ox > 0.5 else "start"
                o.append(f'<text x="{X(ix)}" y="{Y(iy) + 0.55 * scale}" font-size="{1.5 * scale:.1f}" fill="{TXT}" '
                         f'text-anchor="{anchor}">{html.escape(pn.name)}</text>')

    for ref, (x, y, rot, mir) in d.parts.items():
        draw_geo(geo[ref], x, y, rot, mir, {">NAME": ref, ">VALUE": values.get(ref, "")})
    for net, kind, x, y, facing in d.supplies:
        g = supply_geo[kind]
        pin = next(iter(g.pins.values()))
        from .symbols import solve
        sx, sy, rot, mir = solve(g, pin.name, (x, y), (-facing[0], -facing[1]))
        draw_geo(g, sx, sy, rot, mir, {">VALUE": "GND" if kind == "gnd" else net}, PWR)
    count = Counter()
    for net, pts in d.wires:
        o.append(f'<polyline points="{" ".join(f"{X(p[0])},{Y(p[1])}" for p in pts)}" fill="none" stroke="{WIRE}" '
                 f'stroke-width="{0.3 * scale}" stroke-linejoin="round"/>')
        for i, p in enumerate(pts):
            count[(round(p[0], 2), round(p[1], 2))] += 1 if i in (0, len(pts) - 1) else 2
    for (px, py), c in count.items():
        if c >= 3:
            o.append(f'<circle cx="{X(px)}" cy="{Y(py)}" r="{0.6 * scale}" fill="{WIRE}"/>')
    for net, x, y, dr in d.labels:
        w = len(net) * 1.27 * 0.78 + 2.0
        s = scale
        if dr > 0:
            path = f"M{X(x)},{Y(y)} l{1.2 * s},{-1.2 * s} h{(w - 1.2) * s} v{2.4 * s} h{-(w - 1.2) * s} z"
            tx = X(x) + 1.6 * s
            anchor = "start"
        else:
            path = f"M{X(x)},{Y(y)} l{-1.2 * s},{-1.2 * s} h{-(w - 1.2) * s} v{2.4 * s} h{(w - 1.2) * s} z"
            tx = X(x) - 1.6 * s
            anchor = "end"
        o.append(f'<path d="{path}" fill="#eef3fb" stroke="{LBL}" stroke-width="{0.2 * s}"/>')
        o.append(f'<text x="{tx}" y="{Y(y) + 0.6 * s}" font-size="{1.27 * s:.1f}" fill="{LBL}" font-weight="600" '
                 f'text-anchor="{anchor}">{html.escape(net)}</text>')
    o.append("</svg>")
    return "".join(o)
