"""Footprint geometry in board coordinates: courtyards, silkscreen, pad names.

Courtyards are the package's tKeepout / bKeepout drawing (layers 39 / 40):
closed wire loops, rects, polygons and filled circles. A part on the bottom
(mirrored) has its 39 drawing on 40. Each shape is reduced to a convex
polygon (its hull; `exact` says whether that changed the shape) so two
courtyards can be compared with the separating axis test:
- gap: the shortest distance between them when they are apart,
- overlap: the penetration depth (how far one must move to clear the other).
Edges that coincide (an 0402's 2.0 x 1.0 mm courtyard at 1.0 mm pitch) are
"touching", which is not a violation.
Element transforms follow EAGLE: rotate, then mirror X (see stitch._xf).
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .eagle import parse_rot
from .pairs import flatten

TOUCH_TOL = 0.01      # mm: closer than this either way counts as touching
COURTYARD = {"39": "top", "40": "bottom"}


def _f(el, k, d=0.0):
    v = el.get(k)
    return float(v) if v not in (None, "") else d


@dataclass
class Courtyard:
    ref: str
    side: str                                   # top | bottom
    polygons: list = field(default_factory=list)   # convex, counter-clockwise [(x, y), ...]
    exact: bool = True                          # False when a shape was replaced by its hull


def packages(board: ET.Element) -> dict:
    return {(lib.get("name"), pk.get("name")): pk for lib in board.iterfind("./libraries/library")
            for pk in lib.iterfind("./packages/package")}


def transform(el: ET.Element):
    """Local package (x, y) -> board (x, y) for an element, plus (angle, mirrored)."""
    ang, mir = parse_rot(el.get("rot"))
    ex, ey = _f(el, "x"), _f(el, "y")
    a = math.radians(ang)
    c, s = math.cos(a), math.sin(a)

    def xf(px, py):
        rx, ry = px * c - py * s, px * s + py * c
        return ex + (-rx if mir else rx), ey + ry
    return xf, ang, mir


def board_layer(layer: str, mirrored: bool) -> str:
    """The layer a package drawing ends up on: t* <-> b* for a mirrored part."""
    swap = {"21": "22", "22": "21", "25": "26", "26": "25", "27": "28", "28": "27", "39": "40", "40": "39",
            "1": "16", "16": "1"}
    return swap.get(layer, layer) if mirrored else layer


def readable(angle: float, align: str, rot: str | None) -> tuple[float, str]:
    """How Fusion shows a text: one turned past 90 (up to 270) degrees is drawn
    turned back by 180 with its alignment swapped, so it reads upright, unless
    its rot carries the spin flag (S)."""
    a = angle % 360
    if "S" in (rot or "").upper().lstrip("M").split("R")[0] or not (90 < a <= 270):
        return a, align
    flip = {"left": "right", "right": "left", "top": "bottom", "bottom": "top", "center": "center"}
    return (a - 180) % 360, "-".join(flip[p] for p in align.split("-"))


def _rect_corners(r: ET.Element):
    x1, y1, x2, y2 = _f(r, "x1"), _f(r, "y1"), _f(r, "x2"), _f(r, "y2")
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    a = math.radians(parse_rot(r.get("rot"))[0])
    out = []
    for px, py in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
        dx, dy = px - cx, py - cy
        out.append((cx + dx * math.cos(a) - dy * math.sin(a), cy + dx * math.sin(a) + dy * math.cos(a)))
    return out


def _circle_pts(cx, cy, r, n=24):
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def chain_wires(wires, tol=1e-3):
    """[(x1, y1, x2, y2, curve)] -> (closed loops, open chains), each a flattened point list."""
    segs = list(wires)
    loops, chains = [], []
    key = lambda p: (round(p[0] / tol), round(p[1] / tol))
    while segs:
        x1, y1, x2, y2, cu = segs.pop()
        path = [(x1, y1), (x2, y2, cu)]
        grew = True
        while grew and key(path[0]) != key(path[-1]):
            grew = False
            for i, (a, b, c, d, cv) in enumerate(segs):
                if key((a, b)) == key(path[-1]):
                    path.append((c, d, cv))
                elif key((c, d)) == key(path[-1]):
                    path.append((a, b, -cv))
                elif key((c, d)) == key(path[0]):
                    path.insert(0, (a, b))
                    path[1] = (path[1][0], path[1][1], cv)
                elif key((a, b)) == key(path[0]):
                    path.insert(0, (c, d))
                    path[1] = (path[1][0], path[1][1], -cv)
                else:
                    continue
                segs.pop(i)
                grew = True
                break
        pts = [(p[0], p[1]) for p in flatten(path, 0.05)]
        (loops if len(pts) > 3 and key(pts[0]) == key(pts[-1]) else chains).append(pts)
    return loops, chains


def convex_hull(pts):
    """Counter-clockwise hull (monotone chain), no repeated end point."""
    p = sorted(set((round(x, 6), round(y, 6)) for x, y in pts))
    if len(p) < 3:
        return p

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, hi = [], []
    for q in p:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], q) <= 1e-12:
            lo.pop()
        lo.append(q)
    for q in reversed(p):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], q) <= 1e-12:
            hi.pop()
        hi.append(q)
    return lo[:-1] + hi[:-1]


def area(poly) -> float:
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(poly, poly[1:] + poly[:1]))) / 2


def courtyards(root: ET.Element) -> dict[str, Courtyard]:
    """ref -> Courtyard for every element whose package draws one (layers 39/40)."""
    board = root.find("./drawing/board")
    pkgs = packages(board)
    out = {}
    for el in board.iterfind("./elements/element"):
        pk = pkgs.get((el.get("library"), el.get("package")))
        if pk is None:
            continue
        xf, _, mir = transform(el)
        by_side: dict[str, list] = {}
        exact = True
        wires: dict[str, list] = {}
        for w in pk.iterfind("wire"):
            lay = w.get("layer")
            if lay in COURTYARD:
                wires.setdefault(lay, []).append((_f(w, "x1"), _f(w, "y1"), _f(w, "x2"), _f(w, "y2"), _f(w, "curve")))
        shapes = []                                  # (layer, local points, is_closed_shape)
        for lay, ws in wires.items():
            loops, chains = chain_wires(ws)
            shapes += [(lay, lp[:-1], True) for lp in loops]
            if chains:                               # an open outline: its hull stands in for it
                shapes.append((lay, [p for ch in chains for p in ch], False))
        for r in pk.iterfind("rectangle"):
            if r.get("layer") in COURTYARD:
                shapes.append((r.get("layer"), _rect_corners(r), True))
        for pg in pk.iterfind("polygon"):
            if pg.get("layer") in COURTYARD:
                shapes.append((pg.get("layer"), [(_f(v, "x"), _f(v, "y")) for v in pg.iterfind("vertex")], True))
        for c in pk.iterfind("circle"):
            if c.get("layer") in COURTYARD:
                r = _f(c, "radius") + (_f(c, "width") / 2 if _f(c, "width") else 0)
                shapes.append((c.get("layer"), _circle_pts(_f(c, "x"), _f(c, "y"), r), True))
        for lay, pts, closed in shapes:
            world = [xf(x, y) for x, y in pts]
            hull = convex_hull(world)
            if len(hull) < 3:
                continue
            if not closed or abs(area(hull) - area(world)) > 0.01 * max(area(hull), 1e-9):
                exact = False
            by_side.setdefault(COURTYARD[board_layer(lay, mir)], []).append(hull)
        for side, polys in by_side.items():
            key = el.get("name") if len(by_side) == 1 else f"{el.get('name')}@{side}"
            out[key] = Courtyard(el.get("name"), side, polys, exact)
    return out


def _proj(poly, ax, ay):
    d = [x * ax + y * ay for x, y in poly]
    return min(d), max(d)


def separation(a, b) -> float:
    """Separating-axis distance between two convex polygons: > 0 apart (a lower
    bound on the gap), <= 0 overlapping by that depth (exact for convex shapes)."""
    best = -math.inf
    for poly in (a, b):
        for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
            nx, ny = y2 - y1, x1 - x2
            L = math.hypot(nx, ny)
            if L < 1e-12:
                continue
            nx, ny = nx / L, ny / L
            amin, amax = _proj(a, nx, ny)
            bmin, bmax = _proj(b, nx, ny)
            best = max(best, bmin - amax, amin - bmax)
    return best


def _seg_dist(p, q, r, s) -> float:
    def pd(pt, a, b):
        vx, vy = b[0] - a[0], b[1] - a[1]
        L2 = vx * vx + vy * vy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((pt[0] - a[0]) * vx + (pt[1] - a[1]) * vy) / L2))
        return math.hypot(pt[0] - a[0] - t * vx, pt[1] - a[1] - t * vy)
    return min(pd(p, r, s), pd(q, r, s), pd(r, p, q), pd(s, p, q))


def gap(a, b) -> float:
    """Signed distance between convex polygons: the true gap when apart, minus the
    penetration depth when overlapping."""
    sep = separation(a, b)
    if sep <= 0:
        return sep
    return min(_seg_dist(p, q, r, s) for p, q in zip(a, a[1:] + a[:1]) for r, s in zip(b, b[1:] + b[:1]))


def clip(subject, clipper):
    """Sutherland-Hodgman: subject polygon clipped by a convex CCW clipper."""
    out = list(subject)
    for (ax, ay), (bx, by) in zip(clipper, clipper[1:] + clipper[:1]):
        inp, out = out, []
        if not inp:
            break
        side = lambda p: (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)
        for i, cur in enumerate(inp):
            prev = inp[i - 1]
            sc, sp = side(cur), side(prev)
            if sc >= 0:
                if sp < 0:
                    t = sp / (sp - sc)
                    out.append((prev[0] + t * (cur[0] - prev[0]), prev[1] + t * (cur[1] - prev[1])))
                out.append(cur)
            elif sp >= 0:
                t = sp / (sp - sc)
                out.append((prev[0] + t * (cur[0] - prev[0]), prev[1] + t * (cur[1] - prev[1])))
    return out


def contains(outer, inner, tol: float = TOUCH_TOL) -> bool:
    """True when convex CCW polygon `outer` holds every vertex of `inner`."""
    for (ax, ay), (bx, by) in zip(outer, outer[1:] + outer[:1]):
        L = math.hypot(bx - ax, by - ay)
        if L < 1e-12:
            continue
        if any((bx - ax) * (y - ay) - (by - ay) * (x - ax) < -tol * L for x, y in inner):
            return False
    return True


def enclosing(cys: dict[str, Courtyard], min_parts: int = 3) -> dict[str, int]:
    """ref -> how many other same-side parts its courtyard wholly contains, for
    courtyards holding at least min_parts. These are not real courtyards but
    outlines drawn on the keepout layer: a module or SoM footprint, a shield can
    (U8 on the RV1126B SoM gave about 95 false overlaps). Checks skip them."""
    out = {}
    for ka, A in cys.items():
        n = 0
        for kb, B in cys.items():
            if kb == ka or B.side != A.side or B.ref == A.ref:
                continue
            if all(any(contains(pa, pb) for pa in A.polygons) for pb in B.polygons):
                n += 1
        if n >= min_parts:
            out[A.ref] = n
    return out


def _holder(A: Courtyard, B: Courtyard, ratio: float = 4.0):
    """The larger of two courtyards when the smaller lies wholly inside it and is at most
    1/ratio of its area, else None."""
    big, small = (A, B) if sum(map(area, A.polygons)) >= sum(map(area, B.polygons)) else (B, A)
    if sum(map(area, big.polygons)) < ratio * sum(map(area, small.polygons)):
        return None
    if all(any(contains(pb, ps) for pb in big.polygons) for ps in small.polygons):
        return big
    return None


def courtyard_conflicts(cys: dict[str, Courtyard], refs=None, region=None, near_mm: float = 0.0,
                        tol: float = TOUCH_TOL, ignore=None) -> list[dict]:
    """Pairs of same-side courtyards that overlap, touch, or (near_mm > 0) are
    closer than near_mm. refs: only pairs involving one of these parts.
    region: (x0, y0, x1, y1) only pairs with a courtyard reaching into it.
    ignore: parts left out entirely (e.g. those `enclosing` finds).
    Each item: {a, b, side, status: overlap|touching|near, gap_mm or overlap_mm, area_mm2, region}."""
    skip = set(ignore or ())
    keys = sorted(k for k in cys if cys[k].ref not in skip)
    want = set(refs) if refs else None

    def in_region(cy):
        if not region:
            return True
        x0, y0, x1, y1 = region
        return any(x0 <= x <= x1 and y0 <= y <= y1 for p in cy.polygons for x, y in p) or \
            any(min(x for x, _ in p) <= x0 <= max(x for x, _ in p) and min(y for _, y in p) <= y0 <= max(y for _, y in p)
                for p in cy.polygons)
    out = []
    for i, ka in enumerate(keys):
        A = cys[ka]
        for kb in keys[i + 1:]:
            B = cys[kb]
            if A.side != B.side or A.ref == B.ref:
                continue
            if want and A.ref not in want and B.ref not in want:
                continue
            if not (in_region(A) or in_region(B)):
                continue
            worst, inter = math.inf, []
            for pa in A.polygons:
                for pb in B.polygons:
                    if separation(pa, pb) > max(near_mm, tol) + 1e-9:
                        continue
                    g = gap(pa, pb)
                    worst = min(worst, g)
                    if g < -tol:
                        c = clip(pa, pb)
                        if len(c) >= 3:
                            inter.append(c)
            if worst == math.inf:
                continue
            item = {"a": A.ref, "b": B.ref, "side": A.side}
            holder = _holder(A, B)
            if worst < -tol and holder:
                item.update(status="inside", container=holder.ref,
                            note="wholly inside a courtyard at least 4x its size (a module or outline); "
                                 "check it belongs there")
            elif worst < -tol:
                item.update(status="overlap", overlap_mm=round(-worst, 3),
                            area_mm2=round(sum(area(c) for c in inter), 4), region=inter)
            elif worst <= tol:
                item.update(status="touching", gap_mm=round(max(worst, 0.0), 3))
            elif worst < near_mm:
                item.update(status="near", gap_mm=round(worst, 3))
            else:
                continue
            if not (A.exact and B.exact):
                item["note"] = "a courtyard is not convex; its hull was used, so this may over-report"
            out.append(item)
    order = {"overlap": 0, "inside": 1, "touching": 2, "near": 3}
    out.sort(key=lambda d: (order[d["status"]], -d.get("overlap_mm", 0), d.get("gap_mm", 0)))
    return out


def summary(conflicts: list[dict], skipped: dict[str, int] | None = None, top: int = 8) -> str:
    """One short line: overlaps (the worst `top` listed), touching and near pairs as
    counts, and the enclosing courtyards that were skipped."""
    ov = [c for c in conflicts if c["status"] == "overlap"]
    n_touch = sum(c["status"] == "touching" for c in conflicts)
    n_near = sum(c["status"] == "near" for c in conflicts)
    n_inside = sum(c["status"] == "inside" for c in conflicts)
    head = f"{len(ov)} overlap" + ("" if len(ov) == 1 else "s")
    if ov:
        shown = ", ".join(f"{c['a']}/{c['b']} {c['overlap_mm']} mm" for c in ov[:top])
        head += f" ({shown}{', ...' if len(ov) > top else ''})"
    parts = [head, f"{n_touch} touching (OK)"]
    if n_inside:
        parts.append(f"{n_inside} inside a larger outline")
    if n_near:
        parts.append(f"{n_near} near")
    if skipped:
        parts.append("skipped as enclosing outlines: "
                     + ", ".join(f"{r} (holds {n} parts)" for r, n in sorted(skipped.items())))
    return "; ".join(parts)


def pad_names(root: ET.Element) -> list[tuple[str, str, float, float, str]]:
    """(ref, pad, x, y, side: top|bottom|both) for every SMD and through-hole pad."""
    board = root.find("./drawing/board")
    pkgs = packages(board)
    out = []
    for el in board.iterfind("./elements/element"):
        pk = pkgs.get((el.get("library"), el.get("package")))
        if pk is None:
            continue
        xf, _, mir = transform(el)
        for s in pk.iterfind("smd"):
            x, y = xf(_f(s, "x"), _f(s, "y"))
            out.append((el.get("name"), s.get("name"), x, y, "bottom" if board_layer(s.get("layer"), mir) == "16" else "top"))
        for p in pk.iterfind("pad"):
            x, y = xf(_f(p, "x"), _f(p, "y"))
            out.append((el.get("name"), p.get("name"), x, y, "both"))
    return out


def silkscreen(root: ET.Element) -> list[dict]:
    """Silkscreen drawing in board coordinates beyond package wires and circles
    (which the renderer already draws): package rects and polygons on 21/22,
    and the part names (>NAME on tNames 25/26, smashed or not), resolved.
    Items: {kind: rect|polygon|text, layer: '21'|'22', points | x, y, text, size, angle, align}."""
    board = root.find("./drawing/board")
    pkgs = packages(board)
    silk_of = {"21": "21", "22": "22", "25": "21", "26": "22"}
    out = []
    for el in board.iterfind("./elements/element"):
        pk = pkgs.get((el.get("library"), el.get("package")))
        if pk is None:
            continue
        xf, ang, mir = transform(el)
        for r in pk.iterfind("rectangle"):
            lay = board_layer(r.get("layer"), mir)
            if lay in ("21", "22"):
                out.append({"kind": "rect", "layer": lay, "ref": el.get("name"),
                            "points": [xf(x, y) for x, y in _rect_corners(r)]})
        for pg in pk.iterfind("polygon"):
            lay = board_layer(pg.get("layer"), mir)
            if lay in ("21", "22"):
                out.append({"kind": "polygon", "layer": lay, "ref": el.get("name"),
                            "points": [xf(_f(v, "x"), _f(v, "y")) for v in pg.iterfind("vertex")]})
        smashed = {a.get("name"): a for a in el.iterfind("attribute") if a.get("x") is not None}
        values = {"NAME": el.get("name"), "VALUE": el.get("value") or ""}
        if "NAME" in smashed:
            a = smashed["NAME"]
            lay = a.get("layer")
            if lay in silk_of and (a.get("display") or "value") != "off":
                ra, al = readable(parse_rot(a.get("rot"))[0], a.get("align") or "bottom-left", a.get("rot"))
                out.append({"kind": "text", "layer": silk_of[lay], "ref": el.get("name"), "x": _f(a, "x"),
                            "y": _f(a, "y"), "text": values["NAME"], "size": _f(a, "size", 1.0), "angle": ra,
                            "align": al})
        else:
            for t in pk.iterfind("text"):
                lay = board_layer(t.get("layer"), mir)
                if lay not in silk_of or (t.text or "").strip().upper() != ">NAME":
                    continue
                x, y = xf(_f(t, "x"), _f(t, "y"))
                ra, al = readable(parse_rot(t.get("rot"))[0] + ang, t.get("align") or "bottom-left", t.get("rot"))
                out.append({"kind": "text", "layer": silk_of[lay], "ref": el.get("name"), "x": x, "y": y,
                            "text": values["NAME"], "size": _f(t, "size", 1.0), "angle": ra, "align": al})
    return out
