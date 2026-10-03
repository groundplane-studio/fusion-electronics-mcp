"""Placement scoring and move suggestions (offline, from the board XML).

Used to iterate placement against routing without touching Fusion:
- ratsnest length: per net, the minimum spanning tree over its pad centres
  (the usual pre-route wiring estimate), summed;
- crossings: pairs of ratsnest edges from different nets that intersect,
  a good predictor of vias and routing congestion;
- suggestions: for each movable part, candidate positions near the centre
  of gravity of the pads it connects to, clamped inside the board and kept
  clear of same-side parts' footprint boxes; ranked by the drop in score.
Score = ratsnest_mm + crossing_weight * crossings (crossing_weight in mm).
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .eagle import parse_rot


def _f(el, k, d=0.0):
    v = el.get(k)
    return float(v) if v not in (None, "") else d


def _xf(px, py, x, y, angle, mirror):
    a = math.radians(angle)
    rx, ry = px * math.cos(a) - py * math.sin(a), px * math.sin(a) + py * math.cos(a)
    if mirror:
        rx = -rx
    return x + rx, y + ry


@dataclass
class Part:
    name: str
    x: float
    y: float
    angle: float
    mirror: bool
    local_pads: dict = field(default_factory=dict)     # pad -> (lx, ly)
    local_box: tuple = (0.0, 0.0, 0.0, 0.0)            # around pads and silk, in the part frame

    def pad_xy(self, pad, x=None, y=None):
        lx, ly = self.local_pads[pad]
        return _xf(lx, ly, self.x if x is None else x, self.y if y is None else y, self.angle, self.mirror)

    def box(self, x=None, y=None):
        x0, y0, x1, y1 = self.local_box
        pts = [_xf(a, b, self.x if x is None else x, self.y if y is None else y, self.angle, self.mirror)
               for a, b in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass
class Model:
    parts: dict
    nets: dict            # net -> [(part, pad)]
    outline: tuple        # x0, y0, x1, y1
    keepouts: list = field(default_factory=list)   # (x, y, r): holes and restrict circles


def load(root: ET.Element) -> Model:
    board = root.find("./drawing/board")
    pkgs = {(lib.get("name"), pk.get("name")): pk for lib in board.iterfind("./libraries/library")
            for pk in lib.iterfind("./packages/package")}
    parts = {}
    for el in board.iterfind("./elements/element"):
        pk = pkgs.get((el.get("library"), el.get("package")))
        ang, mir = parse_rot(el.get("rot"))
        p = Part(el.get("name"), _f(el, "x"), _f(el, "y"), ang, mir)
        xs, ys = [], []
        if pk is not None:
            for s in list(pk.iterfind("smd")) + list(pk.iterfind("pad")):
                p.local_pads[s.get("name")] = (_f(s, "x"), _f(s, "y"))
                half = max(_f(s, "dx"), _f(s, "dy"), _f(s, "diameter"), _f(s, "drill")) / 2
                xs += [_f(s, "x") - half, _f(s, "x") + half]
                ys += [_f(s, "y") - half, _f(s, "y") + half]
            for w in pk.iterfind("wire"):
                if w.get("layer") in ("21", "39", "51"):
                    xs += [_f(w, "x1"), _f(w, "x2")]
                    ys += [_f(w, "y1"), _f(w, "y2")]
        if xs:
            p.local_box = (min(xs), min(ys), max(xs), max(ys))
        parts[p.name] = p
    nets = {}
    for s in board.iterfind("./signals/signal"):
        refs = [(c.get("element"), c.get("pad")) for c in s.iterfind("contactref")
                if c.get("element") in parts and c.get("pad") in parts[c.get("element")].local_pads]
        if len(refs) > 1:
            nets[s.get("name")] = refs
    xs, ys = [], []
    for w in board.iterfind("./plain/wire"):
        if w.get("layer") == "20":
            xs += [_f(w, "x1"), _f(w, "x2")]
            ys += [_f(w, "y1"), _f(w, "y2")]
    keep = [(_f(h, "x"), _f(h, "y"), _f(h, "drill") / 2) for h in board.iterfind("./plain/hole")]
    keep += [(_f(c, "x"), _f(c, "y"), _f(c, "radius")) for c in board.iterfind("./plain/circle")
             if c.get("layer") in ("39", "40", "41", "42") and _f(c, "width") == 0]
    return Model(parts, nets, (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 0, 0), keep)


def _mst(points):
    """Prim's MST over points; returns list of edges ((x1, y1), (x2, y2))."""
    if len(points) < 2:
        return []
    inside, edges = {0}, []
    best = {i: (math.hypot(points[i][0] - points[0][0], points[i][1] - points[0][1]), 0)
            for i in range(1, len(points))}
    while best:
        j = min(best, key=lambda k: best[k][0])
        d, i = best.pop(j)
        edges.append((points[i], points[j]))
        inside.add(j)
        for k in list(best):
            dk = math.hypot(points[k][0] - points[j][0], points[k][1] - points[j][1])
            if dk < best[k][0]:
                best[k] = (dk, j)
    return edges


def _cross(a, b):
    (p1, p2), (p3, p4) = a, b
    if p1 in (p3, p4) or p2 in (p3, p4):
        return False

    def o(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return (o(p1, p2, p3) * o(p1, p2, p4) < 0) and (o(p3, p4, p1) * o(p3, p4, p2) < 0)


def score(m: Model, exclude=("GND",), crossing_weight: float = 2.0, override: dict | None = None) -> dict:
    """override: {part: (x, y)} to evaluate a hypothetical move."""
    override = override or {}
    edges, per_net = [], {}
    for net, refs in m.nets.items():
        if net in exclude:
            continue
        pts = []
        for part, pad in refs:
            p = m.parts[part]
            ox, oy = override.get(part, (p.x, p.y))
            pts.append(p.pad_xy(pad, ox, oy))
        e = _mst(pts)
        per_net[net] = round(sum(math.hypot(a[0] - b[0], a[1] - b[1]) for a, b in e), 3)
        edges += [(net, ed) for ed in e]
    crossings = 0
    for i in range(len(edges)):
        for j in range(i + 1, len(edges)):
            if edges[i][0] != edges[j][0] and _cross(edges[i][1], edges[j][1]):
                crossings += 1
    total = round(sum(per_net.values()), 3)
    return {"ratsnest_mm": total, "crossings": crossings,
            "score": round(total + crossing_weight * crossings, 3),
            "worst_nets": sorted(per_net.items(), key=lambda kv: -kv[1])[:8]}


def _overlaps(m: Model, name, x, y, margin=0.2):
    a = m.parts[name].box(x, y)
    for cx, cy, r in m.keepouts:
        nx, ny = min(max(cx, a[0]), a[2]), min(max(cy, a[1]), a[3])
        if math.hypot(cx - nx, cy - ny) < r + margin:
            return "keepout"
    for o in m.parts.values():
        if o.name == name or o.mirror != m.parts[name].mirror:
            continue
        b = o.box()
        if a[0] < b[2] + margin and a[2] > b[0] - margin and a[1] < b[3] + margin and a[3] > b[1] - margin:
            return o.name
    return None


def suggest(m: Model, exclude=("GND",), fixed=(), crossing_weight: float = 2.0, top: int = 8,
            step: float = 0.5, radius: float = 6.0) -> dict:
    base = score(m, exclude, crossing_weight)
    out = []
    x0, y0, x1, y1 = m.outline
    for name, p in m.parts.items():
        if name in fixed or not p.local_pads:
            continue
        mine = [(net, refs) for net, refs in m.nets.items() if net not in exclude and any(r[0] == name for r in refs)]
        others = [m.parts[r[0]].pad_xy(r[1]) for _, refs in mine for r in refs if r[0] != name]
        if not others:
            continue
        cx, cy = sum(o[0] for o in others) / len(others), sum(o[1] for o in others) / len(others)
        best = None
        n = int(radius / step)
        for i in range(-n, n + 1):
            for j in range(-n, n + 1):
                x, y = round(p.x + i * step, 3), round(p.y + j * step, 3)
                if math.hypot(x - cx, y - cy) > math.hypot(p.x - cx, p.y - cy) + step:
                    continue
                bx = p.box(x, y)
                if bx[0] < x0 or bx[1] < y0 or bx[2] > x1 or bx[3] > y1 or _overlaps(m, name, x, y):
                    continue
                s = score(m, exclude, crossing_weight, {name: (x, y)})
                if best is None or s["score"] < best[0]["score"]:
                    best = (s, x, y)
        if best and best[0]["score"] < base["score"] - 1e-3:
            out.append({"ref": name, "from": [p.x, p.y], "to": [best[1], best[2]],
                        "score_gain": round(base["score"] - best[0]["score"], 3),
                        "ratsnest_after": best[0]["ratsnest_mm"], "crossings_after": best[0]["crossings"]})
    out.sort(key=lambda d: -d["score_gain"])
    return {"current": base, "suggestions": out[:top]}
