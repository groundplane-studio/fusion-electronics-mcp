"""Placement checks from the board XML (offline): courtyards, pad gaps,
silkscreen on pads, series parts lined up with their pads, and tidiness.

- courtyards: footprints.courtyards / courtyard_conflicts (touching is not a
  violation); parts whose package has no courtyard are listed, or given a
  derived one (pads, silkscreen and body outline plus a margin) on request.
- pad gaps: copper of pads on DIFFERENT parts and different nets against the
  design rules (mdSmdSmd, mdSmdPad, mdPadPad), with true pad shapes
  (SMD rectangles and stadiums, through-hole pads sized by the restring rule).
- silkscreen on pads: package and board silkscreen (21/22: lines, arcs,
  circles, rects, polygons, part names) over pad copper on the same side.
  Text extents use an estimated character width, so text hits are approximate.
- alignment: a two-pin part whose pad joins exactly one other pad
  (point-to-point) should sit on that pad's row (or column); the offset
  across the part's axis is reported.
- tidiness: part origins off the placement grid, rows and columns that are
  almost (not exactly) aligned, mixed rotations among the same package in a
  row, and uneven pitch along a row.
Every shape is a convex polygon in board coordinates (footprints.gap).
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass

from . import footprints as FP
from .pairs import flatten
from .stitch import _mm, _ring

PASSIVE_PREFIXES = ("R", "C", "L", "D", "FB", "LED", "F")
DEFAULT_GRID = {"passive": 0.125, "other": 0.25}     # agreed 2026-10-05
CHAR_W = 0.8                                          # EAGLE vector font: width per character / size (estimate)


def _f(el, k, d=0.0):
    v = el.get(k)
    return float(v) if v not in (None, "") else d


def _circle(cx, cy, r, n=16):
    """Polygon around a circle (circumscribed, so gaps are never over-stated)."""
    R = r / math.cos(math.pi / n)
    return [(cx + R * math.cos(2 * math.pi * (i + 0.5) / n), cy + R * math.sin(2 * math.pi * (i + 0.5) / n))
            for i in range(n)]


def _stadium(x1, y1, x2, y2, r):
    return FP.convex_hull(_circle(x1, y1, max(r, 0.005), 8) + _circle(x2, y2, max(r, 0.005), 8))


def _box(cx, cy, hw, hh, ang):
    a = math.radians(ang)
    c, s = math.cos(a), math.sin(a)
    return [(cx + dx * c - dy * s, cy + dx * s + dy * c) for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]


def _bbox(poly):
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


@dataclass
class Pad:
    ref: str
    name: str
    net: str | None
    smd: bool
    sides: frozenset           # {"top"}, {"bottom"} or both
    x: float
    y: float
    poly: list
    bbox: tuple


def pads(root: ET.Element) -> list[Pad]:
    """Every pad's copper as a convex polygon in board coordinates."""
    board = root.find("./drawing/board")
    rules = {p.get("name"): p.get("value") for p in board.iterfind("./designrules/param")}
    pad_net = {(c.get("element"), c.get("pad")): s.get("name")
               for s in board.iterfind("./signals/signal") for c in s.iterfind("contactref")}
    pkgs = FP.packages(board)
    out = []
    for el in board.iterfind("./elements/element"):
        pk = pkgs.get((el.get("library"), el.get("package")))
        if pk is None:
            continue
        xf, _, mir = FP.transform(el)
        ref = el.get("name")
        for s in pk.iterfind("smd"):
            lx, ly, dx, dy = _f(s, "x"), _f(s, "y"), _f(s, "dx"), _f(s, "dy")
            prot = FP.parse_rot(s.get("rot"))[0]
            if _f(s, "roundness") >= 100 and abs(dx - dy) > 1e-6:
                a = math.radians(prot + (0 if dx >= dy else 90))
                h = (max(dx, dy) - min(dx, dy)) / 2
                local = _stadium(lx - h * math.cos(a), ly - h * math.sin(a), lx + h * math.cos(a),
                                 ly + h * math.sin(a), min(dx, dy) / 2)
            elif _f(s, "roundness") >= 100:
                local = _circle(lx, ly, dx / 2)
            else:
                local = _box(lx, ly, dx / 2, dy / 2, prot)
            poly = FP.convex_hull([xf(x, y) for x, y in local])
            side = "bottom" if FP.board_layer(s.get("layer"), mir) == "16" else "top"
            x, y = xf(lx, ly)
            out.append(Pad(ref, s.get("name"), pad_net.get((ref, s.get("name"))), True, frozenset({side}),
                           x, y, poly, _bbox(poly)))
        for p in pk.iterfind("pad"):
            lx, ly, drill = _f(p, "x"), _f(p, "y"), _f(p, "drill")
            dia = max(_f(p, "diameter"), drill + 2 * _ring(rules, "Pad", drill))
            prot = FP.parse_rot(p.get("rot"))[0]
            shape = (p.get("shape") or "round").lower()
            if shape == "square":
                local = _box(lx, ly, dia / 2, dia / 2, prot)
            elif shape == "octagon":
                local = _circle(lx, ly, dia / 2 * math.cos(math.pi / 8), 8)
            elif shape in ("long", "offset"):
                a = math.radians(prot)
                ox, oy = math.cos(a) * dia / 2, math.sin(a) * dia / 2
                cx, cy = (lx + ox, ly + oy) if shape == "offset" else (lx, ly)
                local = _stadium(cx - ox, cy - oy, cx + ox, cy + oy, dia / 2)
            else:
                local = _circle(lx, ly, dia / 2)
            poly = FP.convex_hull([xf(x, y) for x, y in local])
            x, y = xf(lx, ly)
            out.append(Pad(ref, p.get("name"), pad_net.get((ref, p.get("name"))), False,
                           frozenset({"top", "bottom"}), x, y, poly, _bbox(poly)))
    return out


class _Grid:
    """Bucket items by bounding box for neighbour queries."""
    def __init__(self, cell: float = 2.0):
        self.cell, self.cells = cell, defaultdict(list)

    def _keys(self, b, pad=0.0):
        c = self.cell
        for i in range(math.floor((b[0] - pad) / c), math.floor((b[2] + pad) / c) + 1):
            for j in range(math.floor((b[1] - pad) / c), math.floor((b[3] + pad) / c) + 1):
                yield i, j

    def add(self, item, b):
        for k in self._keys(b):
            self.cells[k].append(item)

    def near(self, b, pad):
        seen = set()
        for k in self._keys(b, pad):
            for it in self.cells.get(k, ()):
                if id(it) not in seen:
                    seen.add(id(it))
                    yield it


def pad_gaps(pad_list: list[Pad], rules: dict, refs=None, region=None) -> list[dict]:
    """Pads of different parts and different nets closer than the design rule."""
    r_ss = _mm(rules.get("mdSmdSmd"), 0.2032)
    r_sp = _mm(rules.get("mdSmdPad"), 0.2032)
    r_pp = _mm(rules.get("mdPadPad"), 0.2032)
    worst = max(r_ss, r_sp, r_pp)
    g = _Grid()
    for p in pad_list:
        g.add(p, p.bbox)
    want = set(refs) if refs else None
    out, seen = [], set()
    for a in pad_list:
        if want and a.ref not in want:
            continue
        if region and not _in_region(a.bbox, region):
            continue
        for b in g.near(a.bbox, worst):
            if b.ref == a.ref or (a.net and a.net == b.net) or not (a.sides & b.sides):
                continue
            key = tuple(sorted(((a.ref, a.name), (b.ref, b.name))))
            if key in seen:
                continue
            seen.add(key)
            rule, rname = ((r_ss, "mdSmdSmd") if a.smd and b.smd else
                           (r_pp, "mdPadPad") if not (a.smd or b.smd) else (r_sp, "mdSmdPad"))
            if FP.separation(a.poly, b.poly) >= rule - 1e-4:
                continue
            d = FP.gap(a.poly, b.poly)
            if d < rule - 1e-4:
                out.append({"a": f"{a.ref}.{a.name}", "b": f"{b.ref}.{b.name}", "nets": [a.net, b.net],
                            "gap_mm": round(d, 3), "rule_mm": round(rule, 3), "rule": rname,
                            "short_by_mm": round(rule - d, 3)})
    out.sort(key=lambda d: -d["short_by_mm"])
    return out


def _in_region(b, region):
    x0, y0, x1, y1 = region
    return not (b[2] < x0 or b[0] > x1 or b[3] < y0 or b[1] > y1)


def _text_poly(x, y, text, size, angle, align):
    w, h = max(len(text), 1) * size * CHAR_W, size
    al = align.split("-")
    va, ha = al[0], (al[-1] if len(al) > 1 else "center")
    x0 = {"left": 0.0, "center": -w / 2, "right": -w}.get(ha, 0.0)
    y0 = {"bottom": 0.0, "center": -h / 2, "top": -h}.get(va, 0.0)
    a = math.radians(angle)
    c, s = math.cos(a), math.sin(a)
    return [(x + dx * c - dy * s, y + dx * s + dy * c) for dx, dy in ((x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h))]


def silk_shapes(root: ET.Element) -> list[dict]:
    """Silkscreen as convex polygons: {owner (ref or None for board drawing), layer '21'|'22',
    what, poly, approx}. Lines and arcs become one stadium per short segment."""
    board = root.find("./drawing/board")
    pkgs = FP.packages(board)
    out = []

    def wire(owner, lay, pts, width, what):
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            out.append({"owner": owner, "layer": lay, "what": what, "poly": _stadium(x1, y1, x2, y2, width / 2),
                        "approx": False})

    def drawing(container, owner, xf, mir):
        for w in container.iterfind("wire"):
            lay = FP.board_layer(w.get("layer"), mir)
            if lay in ("21", "22"):
                pts = flatten([(_f(w, "x1"), _f(w, "y1")), (_f(w, "x2"), _f(w, "y2"), _f(w, "curve"))], 0.1)
                wire(owner, lay, [xf(*p) for p in pts], _f(w, "width"), "line")
        for c in container.iterfind("circle"):
            lay = FP.board_layer(c.get("layer"), mir)
            if lay not in ("21", "22"):
                continue
            cx, cy, r, wd = _f(c, "x"), _f(c, "y"), _f(c, "radius"), _f(c, "width")
            if wd == 0:
                out.append({"owner": owner, "layer": lay, "what": "circle",
                            "poly": FP.convex_hull([xf(*p) for p in _circle(cx, cy, r)]), "approx": False})
            else:
                ring = [(cx + r * math.cos(2 * math.pi * i / 24), cy + r * math.sin(2 * math.pi * i / 24)) for i in range(25)]
                wire(owner, lay, [xf(*p) for p in ring], wd, "circle")
    for el in board.iterfind("./elements/element"):
        pk = pkgs.get((el.get("library"), el.get("package")))
        if pk is None:
            continue
        xf, _, mir = FP.transform(el)
        drawing(pk, el.get("name"), xf, mir)
    drawing(board.find("./plain"), None, lambda x, y: (x, y), False)
    for it in FP.silkscreen(root):
        if it["kind"] == "text":
            poly = _text_poly(it["x"], it["y"], it["text"], it["size"], it["angle"], it["align"])
            out.append({"owner": it["ref"], "layer": it["layer"], "what": f"name {it['text']}", "poly": poly,
                        "approx": True})
        else:
            out.append({"owner": it["ref"], "layer": it["layer"], "what": it["kind"],
                        "poly": FP.convex_hull(it["points"]), "approx": False})
    for t in board.iterfind("./plain/text"):
        if t.get("layer") in ("21", "22"):
            ang, al = FP.readable(FP.parse_rot(t.get("rot"))[0], t.get("align") or "bottom-left", t.get("rot"))
            out.append({"owner": None, "layer": t.get("layer"), "what": f"text {t.text or ''}",
                        "poly": _text_poly(_f(t, "x"), _f(t, "y"), t.text or "", _f(t, "size", 1.0), ang, al),
                        "approx": True})
    for s in out:
        s["bbox"] = _bbox(s["poly"])
    return out


def silk_on_pads(silk: list[dict], pad_list: list[Pad], refs=None, region=None, tol: float = 0.005) -> list[dict]:
    """Silkscreen overlapping pad copper on the same side, one entry per (silk owner, what, pad).
    kind: other (a part's drawing over another part's pads: parts too close), board (board
    silkscreen), name (a part name over pads; printed only if the CAM job includes tNames/bNames)
    or own (a package's drawing over its own pads: a library matter, fabs clip it)."""
    g = _Grid()
    for p in pad_list:
        g.add(p, p.bbox)
    want = set(refs) if refs else None
    hits = {}
    for s in silk:
        side = "top" if s["layer"] == "21" else "bottom"
        if region and not _in_region(s["bbox"], region):
            continue
        for p in g.near(s["bbox"], 0.0):
            if side not in p.sides or (want and p.ref not in want and s["owner"] not in want):
                continue
            sep = FP.separation(s["poly"], p.poly)
            if sep < -tol:
                key = (s["owner"], s["what"], p.ref, p.name)
                if key not in hits or -sep > hits[key]["overlap_mm"]:
                    kind = ("name" if s["what"].startswith("name") else "board" if s["owner"] is None
                            else "own" if s["owner"] == p.ref else "other")
                    hits[key] = {"silk": f"{s['owner'] or 'board'} {s['what']}", "pad": f"{p.ref}.{p.name}",
                                 "kind": kind, "side": side, "overlap_mm": round(-sep, 3)}
                    if s["approx"]:
                        hits[key]["note"] = "text extent estimated"
    return sorted(hits.values(), key=lambda d: -d["overlap_mm"])


def connections(pad_list: list[Pad], reach_mm: float = 5.0) -> dict[str, list[dict]]:
    """ref -> point-to-point links from a two-pin part's pads to another part's pad within
    reach_mm. The axis comes from how the two pads sit: side by side is a row (offset in y),
    one above the other is a column (offset in x)."""
    by_ref, by_net = defaultdict(list), defaultdict(list)
    for p in pad_list:
        by_ref[p.ref].append(p)
        if p.net:
            by_net[p.net].append(p)
    out = defaultdict(list)
    for ref, ps in by_ref.items():
        if len(ps) != 2:
            continue
        for p in ps:
            members = by_net.get(p.net, []) if p.net else []
            if len(members) != 2:
                continue
            q = next((m for m in members if m.ref != ref), None)
            if q is None:
                continue
            dx, dy = q.x - p.x, q.y - p.y
            if math.hypot(dx, dy) > reach_mm:
                continue
            axis = "row" if abs(dx) >= abs(dy) else "column"
            out[ref].append({"pad": p.name, "to": f"{q.ref}.{q.name}", "to_part": q.ref, "net": p.net,
                             "along": axis, "offset_mm": round(dy if axis == "row" else dx, 3),
                             "distance_mm": round(math.hypot(dx, dy), 3)})
    return out


def series_alignment(pad_list: list[Pad], refs=None, tol: float = 0.01, reach_mm: float = 5.0) -> list[dict]:
    """For two-pin parts linked point-to-point to a nearby pad (within reach_mm): the
    better-aligned of its links (R18-R25 are judged against T1's rows, not J2 at the far end),
    how far the part sits off that pad's row or column, and the other end if there is one."""
    out = []
    for ref, links in sorted(connections(pad_list, reach_mm).items(), key=lambda kv: _natural(kv[0])):
        if refs and ref not in refs:
            continue
        links = sorted(links, key=lambda c: (abs(c["offset_mm"]), c["distance_mm"]))
        best = {k: v for k, v in links[0].items() if k != "to_part"}
        best.update(part=ref, aligned=abs(best["offset_mm"]) <= tol)
        if len(links) > 1:
            o = links[1]
            best["other_end"] = {"to": o["to"], "along": o["along"], "offset_mm": o["offset_mm"]}
        out.append(best)
    return out


def part_class(ref: str, n_pads: int) -> str:
    m = re.match(r"[A-Za-z]+", ref or "")
    return "passive" if m and m.group(0).upper() in PASSIVE_PREFIXES and n_pads <= 2 else "other"


def tidiness(root: ET.Element, pad_list: list[Pad], grid: dict | None = None, refs=None, region=None,
             near_mm: float = 0.2, reach_mm: float = 3.0, pin_tol: float = 0.05) -> dict:
    """Off-grid origins, almost-aligned rows/columns, mixed rotations and uneven pitch.
    A row or column whose parts each line up with a pin of the same neighbouring part
    (series resistors on T1's pad rows) follows that part's pin pitch and is not uneven."""
    follows = defaultdict(set)
    for ref, links in connections(pad_list).items():
        follows[ref] = {c["to_part"] for c in links if abs(c["offset_mm"]) <= pin_tol}
    grid = {**DEFAULT_GRID, **(grid or {})}
    board = root.find("./drawing/board")
    n_pads = defaultdict(int)
    for p in pad_list:
        n_pads[p.ref] += 1
    parts = []
    for el in board.iterfind("./elements/element"):
        ref = el.get("name")
        if not n_pads.get(ref):
            continue                                   # frames, logos, holes without pads
        x, y = _f(el, "x"), _f(el, "y")
        if refs and ref not in refs:
            continue
        if region and not _in_region((x, y, x, y), region):
            continue
        ang, mir = FP.parse_rot(el.get("rot"))
        parts.append({"ref": ref, "x": x, "y": y, "angle": ang % 360, "side": "bottom" if mir else "top",
                      "package": el.get("package"), "cls": part_class(ref, n_pads[ref])})
    off_grid = []
    for p in parts:
        g = grid[p["cls"]]
        gx, gy = round(p["x"] / g) * g, round(p["y"] / g) * g
        if abs(gx - p["x"]) > 1e-3 or abs(gy - p["y"]) > 1e-3:
            off_grid.append({"part": p["ref"], "at": [p["x"], p["y"]], "nearest": [round(gx, 4), round(gy, 4)],
                             "grid_mm": g, "move_mm": round(math.hypot(gx - p["x"], gy - p["y"]), 4)})
    near = []                                          # groups of parts that almost share a row/column
    for axis, k, o in (("row", "y", "x"), ("column", "x", "y")):
        parent = list(range(len(parts)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        loose = set()
        for i, a in enumerate(parts):
            for j in range(i + 1, len(parts)):
                b = parts[j]
                if a["side"] != b["side"] or a["cls"] != b["cls"] or abs(a[o] - b[o]) > reach_mm:
                    continue
                d = abs(a[k] - b[k])
                if d <= near_mm + 1e-9:
                    parent[find(i)] = find(j)
                    if d > 1e-3:
                        loose.add(i)
        groups = defaultdict(list)
        for i in range(len(parts)):
            groups[find(i)].append(i)
        loose_roots = {find(i) for i in loose}
        for root_i, members in groups.items():
            if len(members) < 2 or root_i not in loose_roots:
                continue
            vals = [parts[i][k] for i in members]
            near.append({"axis": axis, "parts": sorted((parts[i]["ref"] for i in members), key=_natural),
                         "spread_mm": round(max(vals) - min(vals), 4)})
    lines = defaultdict(list)                          # exact rows/columns of one package
    for p in parts:
        lines[("row", p["side"], p["package"], round(p["y"], 3))].append(p)
        lines[("column", p["side"], p["package"], round(p["x"], 3))].append(p)
    mixed, uneven, pin_pitch = [], [], []
    for (axis, side, pkg, at), ps in lines.items():
        if len(ps) < 2:
            continue
        o = "x" if axis == "row" else "y"
        ps = sorted(ps, key=lambda q: q[o])
        runs, cur = [], [ps[0]]                         # split where the gap is far more than a part
        for q in ps[1:]:
            if q[o] - cur[-1][o] > reach_mm:
                runs.append(cur)
                cur = []
            cur.append(q)
        runs.append(cur)
        for run in runs:
            if len(run) < 2:
                continue
            angles = sorted({q["angle"] for q in run})
            if len(angles) > 1:
                mixed.append({"axis": axis, "at_mm": at, "package": pkg, "parts": [q["ref"] for q in run],
                              "angles": {q["ref"]: q["angle"] for q in run}})
            if len(run) >= 3:
                pitches = [round(b[o] - a[o], 4) for a, b in zip(run, run[1:])]
                if max(pitches) - min(pitches) > 0.01:
                    common = set.intersection(*(follows[q["ref"]] for q in run))
                    if common:
                        pin_pitch.append({"axis": axis, "parts": [q["ref"] for q in run],
                                          "follows_pins_of": sorted(common, key=_natural)})
                    else:
                        uneven.append({"axis": axis, "at_mm": at, "package": pkg,
                                       "parts": [q["ref"] for q in run], "pitches_mm": pitches})
    return {"grid_mm": grid, "off_grid": off_grid, "almost_aligned": sorted(near, key=lambda d: d["spread_mm"]),
            "mixed_rotation": mixed, "uneven_spacing": uneven, "follows_pin_pitch": pin_pitch}


def derived_courtyards(root: ET.Element, have: dict, margin: float = 0.25, source: str = "body") -> dict:
    """Courtyards for parts without one: the box around pads, silkscreen (21) and the
    body outline (tDocu 51) in the part's own frame, plus margin. Marked derived."""
    board = root.find("./drawing/board")
    pkgs = FP.packages(board)
    out = {}
    known = {c.ref for c in have.values()}
    for el in board.iterfind("./elements/element"):
        ref = el.get("name")
        pk = pkgs.get((el.get("library"), el.get("package")))
        if ref in known or pk is None:
            continue
        xs, ys = [], []
        for s in pk.iterfind("smd"):
            for x, y in _box(_f(s, "x"), _f(s, "y"), _f(s, "dx") / 2, _f(s, "dy") / 2, FP.parse_rot(s.get("rot"))[0]):
                xs.append(x)
                ys.append(y)
        for p in pk.iterfind("pad"):
            r = max(_f(p, "diameter"), _f(p, "drill") * 1.5) / 2 * (2 if (p.get("shape") or "") in ("long", "offset") else 1)
            xs += [_f(p, "x") - r, _f(p, "x") + r]
            ys += [_f(p, "y") - r, _f(p, "y") + r]
        layers = {"pads": (), "body": ("51",), "outline": ("51", "21")}[source]
        for w in pk.iterfind("wire"):
            if w.get("layer") in layers:
                xs += [_f(w, "x1"), _f(w, "x2")]
                ys += [_f(w, "y1"), _f(w, "y2")]
        if not xs:
            continue
        xf, _, mir = FP.transform(el)
        x0, y0, x1, y1 = min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin
        poly = FP.convex_hull([xf(x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))])
        out[ref] = FP.Courtyard(ref, "bottom" if mir else "top", [poly], True)
        out[ref].derived = True
    return out


def check(root: ET.Element, refs=None, region=None, alignment: bool = False, grid: dict | None = None,
          derive_missing: bool = False, margin: float = 0.25, ignore=None, top: int = 20,
          derive_from: str = "body", reach_mm: float = 5.0) -> dict:
    if derive_from not in ("pads", "body", "outline"):
        raise ValueError("derive_from is 'pads', 'body' (pads and tDocu outline) or 'outline' (also silkscreen)")
    board = root.find("./drawing/board")
    rules = {p.get("name"): p.get("value") for p in board.iterfind("./designrules/param")}
    cys = FP.courtyards(root)
    skipped = FP.enclosing(cys)
    have = {c.ref for c in cys.values()}
    pad_list = pads(root)
    all_refs = {p.ref for p in pad_list}
    missing = sorted(all_refs - have, key=_natural)
    derived = {}
    if derive_missing:
        derived = derived_courtyards(root, cys, margin, derive_from)
        cys = {**cys, **derived}
    conflicts = FP.courtyard_conflicts(cys, refs=refs, region=region, ignore=set(skipped) | set(ignore or ()))
    for c in conflicts:
        c.pop("region", None)
        d = [r for r in (c["a"], c["b"]) if r in derived]
        if d:
            c["derived"] = d
    gaps = pad_gaps(pad_list, rules, refs, region)
    silk = silk_on_pads(silk_shapes(root), pad_list, refs, region)
    by_kind = defaultdict(list)
    for h in silk:
        by_kind[h["kind"]].append(h)
    neighbours = {}                                    # one entry per (silk of part A, pads of part B)
    for h in by_kind["other"]:
        a, b = h["silk"].split()[0], h["pad"].split(".")[0]
        e = neighbours.setdefault((a, b), {"silk_of": a, "on_pads_of": b, "pads": [], "side": h["side"],
                                           "overlap_mm": 0.0})
        e["pads"].append(h["pad"].split(".", 1)[1])
        e["overlap_mm"] = max(e["overlap_mm"], h["overlap_mm"])
    neighbours = sorted(neighbours.values(), key=lambda e: -e["overlap_mm"])
    for e in neighbours:
        e["pads"] = sorted(set(e["pads"]), key=_natural)
    out = {
        "courtyards": {
            "overlaps": [c for c in conflicts if c["status"] == "overlap"][:top],
            "overlap_count": sum(c["status"] == "overlap" for c in conflicts),
            "inside": [c for c in conflicts if c["status"] == "inside"][:top],
            "touching_count": sum(c["status"] == "touching" for c in conflicts),
            "skipped_enclosing": skipped,
            "no_courtyard": missing if not derive_missing else [],
            "derived": sorted(derived, key=_natural),
        },
        "pad_gaps": {"violations": gaps[:top], "count": len(gaps)},
        "silk_on_pads": {
            "other_parts_pads": neighbours[:top], "board_silk": by_kind["board"][:top],
            "counts": {"other_parts_pads": len(neighbours), "board_silk": len(by_kind["board"]),
                       "part_names": len(by_kind["name"]), "own_pads_library": len(by_kind["own"])},
            "note": ("other_parts_pads counts pairs of parts (one part's silkscreen on the other's pads); "
                     "part names print only if the CAM job includes tNames/bNames; a package's own "
                     "silkscreen over its pads is a library matter (fabs clip it)")},
        "tidiness": tidiness(root, pad_list, grid, refs, region),
    }
    t = out["tidiness"]
    for k in ("off_grid", "almost_aligned", "mixed_rotation", "uneven_spacing"):
        t[k + "_count"] = len(t[k])
        t[k] = t[k][:top]
    if alignment:
        al = series_alignment(pad_list, refs, reach_mm=reach_mm)
        out["alignment"] = {"off": [a for a in al if not a["aligned"]][:top],
                            "off_count": sum(not a["aligned"] for a in al),
                            "aligned_count": sum(a["aligned"] for a in al)}
    bits = [f"courtyards: {FP.summary(conflicts, skipped, top=5)}"]
    if missing and not derive_missing:
        bits.append(f"{len(missing)} parts have no courtyard (derive_missing_courtyards=true to check them)")
    bits.append(f"pad gaps under the rules: {len(gaps)}")
    bits.append(f"silkscreen on a neighbour's pads: {len(neighbours)} part pairs, board silkscreen on pads: "
                f"{len(by_kind['board'])} (names on pads {len(by_kind['name'])}, library silk on own pads "
                f"{len(by_kind['own'])})")
    bits.append(f"off grid: {t['off_grid_count']}, almost aligned: {t['almost_aligned_count']}, "
                f"mixed rotation: {t['mixed_rotation_count']}, uneven spacing: {t['uneven_spacing_count']}")
    if alignment:
        bits.append(f"series parts off their pad's line: {out['alignment']['off_count']}")
    out["summary"] = "; ".join(bits)
    return out


def _natural(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def resolve_moves(root: ET.Element, moves: list[dict]) -> list[dict]:
    """[{ref, x_mm?, y_mm?, angle?, bottom?}] -> full targets {ref, x, y, angle, bottom, was}
    (anything left out keeps its current value). Unknown or repeated parts are refused."""
    els = {e.get("name"): e for e in root.iterfind("./drawing/board/elements/element")}
    out, seen = [], set()
    for m in moves:
        ref = str(m.get("ref") or "")
        if ref not in els:
            raise ValueError(f"no part {ref!r} on the board")
        if ref in seen:
            raise ValueError(f"{ref} is listed twice")
        seen.add(ref)
        e = els[ref]
        ang0, mir0 = FP.parse_rot(e.get("rot"))
        x0, y0 = _f(e, "x"), _f(e, "y")
        pick = lambda k, d: d if m.get(k) is None else m[k]
        t = {"ref": ref, "x": float(pick("x_mm", x0)), "y": float(pick("y_mm", y0)),
             "angle": float(pick("angle", ang0)) % 360, "bottom": bool(pick("bottom", mir0)),
             "was": {"x": x0, "y": y0, "angle": ang0 % 360, "bottom": mir0}}
        t["moves"] = abs(t["x"] - x0) > 1e-4 or abs(t["y"] - y0) > 1e-4
        t["turns"] = abs((t["angle"] - ang0 + 180) % 360 - 180) > 1e-3 or t["bottom"] != mir0
        out.append(t)
    return out


def apply_moves(root: ET.Element, targets: list[dict]) -> ET.Element:
    """A copy of the board with the parts at their targets (for previews). Traces stay where
    they are: Fusion drags the ends attached to a moved part's pads, the preview does not."""
    import copy
    r2 = copy.deepcopy(root)
    by_ref = {t["ref"]: t for t in targets}
    for el in r2.iterfind("./drawing/board/elements/element"):
        t = by_ref.get(el.get("name"))
        if t:
            el.set("x", f"{t['x']:g}")
            el.set("y", f"{t['y']:g}")
            el.set("rot", ("M" if t["bottom"] else "") + f"R{t['angle']:g}")
    return r2


def move_effects(before: ET.Element, after: ET.Element, refs: list[str]) -> dict:
    """Problems that involve the moved parts, before and after: what the moves introduce and
    what they clear. Covers courtyard overlaps (derived courtyards, marked, for parts whose
    library has none), pad gaps under the rules, and silkscreen on a neighbour's pads or board
    silkscreen on pads."""
    def problems(root):
        cys = FP.courtyards(root)
        skipped = FP.enclosing(cys)
        derived = derived_courtyards(root, cys)
        found = {}
        for c in FP.courtyard_conflicts({**cys, **derived}, refs=refs, ignore=skipped):
            if c["status"] in ("overlap", "inside"):
                c.pop("region", None)
                d = [r for r in (c["a"], c["b"]) if r in derived]
                if d:
                    c["derived"] = d
                found[("courtyard", c["a"], c["b"])] = c
        rules = {p.get("name"): p.get("value") for p in root.iterfind("./drawing/board/designrules/param")}
        pad_list = pads(root)
        for g in pad_gaps(pad_list, rules, refs):
            found[("pads", g["a"], g["b"])] = g
        for h in silk_on_pads(silk_shapes(root), pad_list, refs):
            if h["kind"] in ("other", "board"):
                h["silk_on"] = "neighbour's pad" if h.pop("kind") == "other" else "pad (board silkscreen)"
                found[("silkscreen", h["silk"], h["pad"])] = h
        return found
    b, a = problems(before), problems(after)
    new = [{"kind": k[0], **v} for k, v in a.items() if k not in b]
    cleared = [{"kind": k[0], **v} for k, v in b.items() if k not in a]
    still = [{"kind": k[0], **v} for k, v in a.items() if k in b]
    return {"introduced": new, "cleared": cleared, "unchanged": still}
