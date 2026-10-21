"""Pour keep-away (offline): how close same-layer pour copper runs to controlled-impedance traces.

Traces designed as plain (non-coplanar) microstrip or stripline lose impedance when pour copper
on their own layer comes close: the side copper adds capacitance. Keeping it at least 3 trace
widths and 2 dielectric heights (to the nearest reference plane) away leaves the impedance
within a few percent of the non-coplanar value, so the required gap is max(w_factor * W,
h_factor * H) with W the segment's width and H the thinner dielectric next to the layer.

Poured copper: Fusion's V2 working copy keeps each pour's fill (<polygonpour> /
<polygonfilldetails> / <polygonshape>: the outer contour in <polygonoutlinesegments> and the
holes in <polygonholelist>). Those contours are the copper's edge: on the RV1126B SoM every
foreign via in a GND fill sits exactly mdWireVia (0.09 mm) from its hole. The classic EAGLE
export has only the outlines (<polygon>); from those the gap is estimated: a trace inside a
solid outline gets max(isolate, clearance), as Fusion would pour it, else its distance to the
outline's copper edge.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections import defaultdict

from .design import parse_board_design
from .pairs import flatten

CELL = 0.5


def _pts(e):
    return [(float(v.get("x")), float(v.get("y"))) for v in e.iterfind("vertex")]


def fills(root: ET.Element) -> list[dict]:
    """Poured copper from a V2 board file: [{net, layer, rings: [[(x, y), ...], ...]}] (outer
    contours and holes alike: each ring is a copper edge). Empty when the file has no fill."""
    out = []
    for sig in root.iter("signal"):
        for pp in sig.iterfind("polygonpour"):
            for sh in pp.iter("polygonshape"):
                rings = []
                c = sh.find("polygonoutlinesegments")
                if c is None:
                    c = sh.find("polygonoutlineobjects")
                if c is not None:
                    rings.append(_pts(c))
                rings += [_pts(h) for h in sh.iter("polygonholesegments")]
                rings = [r for r in rings if len(r) >= 3]
                if rings:
                    out.append({"net": sig.get("name"), "layer": int(sh.get("layer") or pp.get("layer")),
                                "rings": rings})
    return out


def remap(fill: list[dict], v2_layers: list[int], board_layers: list[int]) -> list[dict]:
    """Fill layers renumbered from the V2 file's copper numbering (the stackup's: 1, 2, 3, 4, 301,
    ..., 304 on the RV1126B SoM) to the classic export's (1, 2, 3, 4, 13, ..., 16), by stack
    position. Fills on layers not in the stack are dropped."""
    to = dict(zip(v2_layers, board_layers)) if len(v2_layers) == len(board_layers) else {}
    return [{**f, "layer": to[f["layer"]]} for f in fill if f["layer"] in to]


def outlines(root: ET.Element) -> list[dict]:
    """Pour outlines from the classic export: [{net, layer, width, isolate, outline}] (solid and
    hatched pours; cutouts are not copper)."""
    out = []
    for sig in root.iterfind("./drawing/board/signals/signal"):
        for pg in sig.iterfind("polygon"):
            if (pg.get("pour") or "solid") == "cutout":
                continue
            pts = _pts(pg)
            if len(pts) >= 3:
                out.append({"net": sig.get("name"), "layer": int(pg.get("layer")), "width": float(pg.get("width") or 0),
                            "isolate": float(pg.get("isolate") or 0), "outline": pts})
    return out


def reference_heights(stack, copper_layers: list[int]) -> dict[int, dict]:
    """{board layer: {h_mm, dielectric}}: the thinner dielectric next to each copper layer (the
    nearest reference plane), matching board layers to the stackup by position."""
    out = {}
    if stack is None or len(stack.copper) != len(copper_layers):
        return out
    for i, n in enumerate(copper_layers):
        up, down = stack.neighbours(i)
        cand = [d for d in (up, down) if d is not None and d.thickness_mm]
        if cand:
            d = min(cand, key=lambda d: d.thickness_mm)
            out[n] = {"h_mm": d.thickness_mm, "dielectric": d.name}
    return out


def _seg_dist(p, a, b):
    vx, vy = b[0] - a[0], b[1] - a[1]
    L = vx * vx + vy * vy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / L))
    return math.hypot(p[0] - a[0] - t * vx, p[1] - a[1] - t * vy)


def _inside(ring, x, y):
    c = False
    for (ax, ay), (bx, by) in zip(ring, ring[1:] + ring[:1]):
        if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
            c = not c
    return c


class _Edges:
    """Copper edges per (layer, net) bucketed on a grid, for nearest-edge queries."""
    def __init__(self):
        self.cells = defaultdict(list)

    def add(self, layer, net, a, b, owner):
        for i in range(math.floor(min(a[0], b[0]) / CELL), math.floor(max(a[0], b[0]) / CELL) + 1):
            for j in range(math.floor(min(a[1], b[1]) / CELL), math.floor(max(a[1], b[1]) / CELL) + 1):
                self.cells[(layer, i, j)].append((net, a, b, owner))

    def near(self, layer, x, y, reach):
        seen = set()
        for i in range(math.floor((x - reach) / CELL), math.floor((x + reach) / CELL) + 1):
            for j in range(math.floor((y - reach) / CELL), math.floor((y + reach) / CELL) + 1):
                for e in self.cells.get((layer, i, j), ()):
                    if id(e) not in seen:
                        seen.add(id(e))
                        yield e


def check(root: ET.Element, nets: list[str], fill: list[dict] | None = None, heights: dict | None = None,
          w_factor: float = 3.0, h_factor: float = 2.0, min_gap_mm: float | None = None,
          clearance_mm: float = 0.0, step: float = 0.05, pad_reach: dict | None = None) -> dict:
    """Same-layer pour copper closer to these nets' traces than the required gap.
    fill: fills() of the V2 working copy, or None to estimate from the export's outlines.
    min_gap_mm: one fixed gap instead of max(w_factor * W, h_factor * H).
    pad_reach: {net: [(x, y, r), ...]} the net's own pads and vias (centre, copper radius):
    trace within the required gap of them is the breakout, where the pour has to come close to
    the pad anyway, so it is counted as skipped, not judged."""
    bd = parse_board_design(root)
    heights = heights or {}
    edges = _Edges()
    owners = []
    if fill is not None:
        for f in fill:
            k = len(owners)
            owners.append({"net": f["net"], "layer": f["layer"], "rings": f["rings"], "estimated": False})
            for r in f["rings"]:
                for a, b in zip(r, r[1:] + r[:1]):
                    edges.add(f["layer"], f["net"], a, b, k)
    else:
        for o in outlines(root):
            k = len(owners)
            owners.append({"net": o["net"], "layer": o["layer"], "rings": [o["outline"]], "estimated": True,
                           "half_width": o["width"] / 2, "pour_gap": max(o["isolate"], clearance_mm)})
            r = o["outline"]
            for a, b in zip(r, r[1:] + r[:1]):
                edges.add(o["layer"], o["net"], a, b, k)
    hits = {}
    skipped = defaultdict(float)
    checked = defaultdict(float)
    for net in nets:
        sig = bd.signals.get(net)
        if sig is None:
            continue
        own = (pad_reach or {}).get(net, [])
        for w in sig.wires:
            h = heights.get(w.layer, {}).get("h_mm")
            need = min_gap_mm if min_gap_mm is not None else max(w_factor * w.width, h_factor * h if h else 0.0)
            pts = flatten([(w.x1, w.y1), (w.x2, w.y2, w.curve)] if w.curve else [(w.x1, w.y1), (w.x2, w.y2)], step)
            for x, y in pts:
                checked[(net, w.layer)] += step
                if any(math.hypot(x - px, y - py) - r < need for px, py, r in own):
                    skipped[net] += step
                    continue
                best = {}
                for pnet, a, b, k in edges.near(w.layer, x, y, need + w.width):
                    if pnet == net:
                        continue
                    d = _seg_dist((x, y), a, b)
                    if d < best.get(k, (math.inf,))[0]:
                        best[k] = (d, a, b)
                for k, (d, a, b) in best.items():
                    o = owners[k]
                    if o["estimated"]:
                        inside = _inside(o["rings"][0], x, y)
                        gap = o["pour_gap"] if inside else d - w.width / 2 - o["half_width"]
                    else:
                        gap = d - w.width / 2
                        # a trace in pour copper (not in a hole) overlaps it
                        if gap < need and _inside(o["rings"][0], x, y) and not any(_inside(r, x, y) for r in o["rings"][1:]):
                            gap = -gap
                    if gap < need - 1e-4:
                        key = (net, w.layer, o["net"])
                        cur = hits.get(key)
                        if cur is None:
                            cur = hits[key] = {"net": net, "layer": w.layer, "pour_net": o["net"], "gap_mm": math.inf,
                                               "required_mm": round(need, 4), "trace_width_mm": w.width,
                                               "h_mm": h, "length_mm": 0.0, "estimated": o["estimated"]}
                        cur["length_mm"] += step
                        cur["required_mm"] = round(max(cur["required_mm"], need), 4)
                        if gap < cur["gap_mm"]:
                            cur["gap_mm"] = gap
                            cur["at"] = [round(x, 3), round(y, 3)]
                            if not o["estimated"]:
                                t = _closest(a, b, (x, y))
                                cur["pour_edge_at"] = [round(t[0], 3), round(t[1], 3)]
    out = []
    for v in sorted(hits.values(), key=lambda v: v["gap_mm"]):
        v["gap_mm"] = round(v["gap_mm"], 4)
        v["length_mm"] = round(v["length_mm"], 2)
        if v["gap_mm"] < 0:
            v["note"] = "the trace runs through pour copper: a short, or the fill is older than the trace"
        out.append(v)
    return {"ok": not out, "violations": out,
            "checked_mm": {f"{n}@{l}": round(v, 2) for (n, l), v in sorted(checked.items())},
            "skipped_breakout_mm": {n: round(v, 2) for n, v in sorted(skipped.items())}}


def _closest(a, b, p):
    vx, vy = b[0] - a[0], b[1] - a[1]
    L = vx * vx + vy * vy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / L))
    return a[0] + t * vx, a[1] + t * vy
