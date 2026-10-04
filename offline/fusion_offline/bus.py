"""Bus lanes: N nets laid side by side along one path you choose, like a person lays a bus.

Lane i is the path offset by i * pitch to the left of travel (lane 0 is the path), with
mitred corners (pairs.offset_path), trimmed to the stretch its own net needs: from the first
to the last of its pins projected onto the path, plus a little. Pins then join their lanes with
short taps (router.route with join_existing), so a bus between connectors comes out as ordered
parallel runs with taps at the ends: the structure of the user's hand-laid KiCad buses.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

from .pairs import offset_path
from .stitch import board_obstacles, _inside_poly, _mm


def _project(path, p) -> float:
    """Arc-length position along `path` of the point nearest p."""
    best, s0, at = 1e18, 0.0, 0.0
    for a, b in zip(path, path[1:]):
        vx, vy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(vx, vy)
        t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / (L * L)))
        d = math.hypot(a[0] + t * vx - p[0], a[1] + t * vy - p[1])
        if d < best:
            best, at = d, s0 + t * L
        s0 += L
    return at


def _cut(path, lane, s0, s1):
    """The part of `lane` between path positions s0..s1 (lane vertex k pairs with path vertex k)."""
    out, acc = [], 0.0
    for k, (a, b) in enumerate(zip(path, path[1:])):
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        la, lb = lane[k], lane[k + 1]
        lo, hi = max(s0, acc), min(s1, acc + L)
        if hi > lo + 1e-9 and L > 0:
            t0, t1 = (lo - acc) / L, (hi - acc) / L
            p0 = (round(la[0] + (lb[0] - la[0]) * t0, 4), round(la[1] + (lb[1] - la[1]) * t0, 4))
            p1 = (round(la[0] + (lb[0] - la[0]) * t1, 4), round(la[1] + (lb[1] - la[1]) * t1, 4))
            if not out or math.dist(out[-1], p0) > 1e-6:
                out.append(p0)
            out.append(p1)
        acc += L
    return out


def plan_bus(root: ET.Element, nets: list[str], path, pitch: float, width: float, layer: int,
             pins: dict, margin: float = 1.0) -> dict:
    """pins: {net: [(x, y), ...]} the points each lane must span (its pads). Returns
    {'lanes': [{'net', 'points'}], 'problems': [...]} with a clearance check of every lane
    against other nets' copper on the layer, holes and keepouts."""
    path = [tuple(p) for p in path]
    total = sum(math.dist(a, b) for a, b in zip(path, path[1:]))
    obs, rules, _ = board_obstacles(root)
    clr = _mm(rules.get("mdWireWire"), 0.2)
    lanes, problems = [], []
    power = []
    for sg in root.iterfind("./drawing/board/signals/signal"):
        if re.match(r"(?i)^(a|d|p)?gnd", sg.get("name") or ""):
            continue
        for pg in sg.iterfind("polygon"):
            lay = int(pg.get("layer") or 1)
            if lay in (1, 16, 304):
                power.append((sg.get("name"), lay, [(float(v.get("x")), float(v.get("y"))) for v in pg.iterfind("vertex")]))
    for i, net in enumerate(nets):
        full = offset_path(path, i * pitch)
        ss = [_project(path, p) for p in pins.get(net, [])]
        if not ss:
            problems.append(f"{net}: no pins given")
            continue
        # neighbouring lanes end at least 1 mm apart, so their tap vias never sit side by side
        stag = margin + 1.0 * (i % 2)
        s0, s1 = max(0.0, min(ss) - stag), min(total, max(ss) + stag)
        pts = _cut(path, full, s0, s1)
        if len(pts) < 2:
            problems.append(f"{net}: its pins do not span the path")
            continue
        lanes.append({"net": net, "points": pts})
        # where each pin's tap meets the lane: not inside another net's outer power pour, or the
        # tap (and its via) would cut that pour (seen on a test board: four taps cut a 12 V pour)
        for pin, sp in zip(pins.get(net, []), ss):
            q = _cut(path, full, max(0.0, sp - 1e-3), min(total, sp + 1e-3))
            if not q or math.dist(pin, q[0]) > 12.0:     # a far pin is not tapped here
                continue
            for pnet, play, poly in power:
                if pnet != net and _inside_poly(poly, *q[0]):
                    problems.append(f"{net}: its tap at ({q[0][0]:.2f}, {q[0][1]:.2f}) would cut {pnet}'s pour on "
                                    f"layer {play}; move the path out of the pour")
                    break
        for a, b in zip(pts, pts[1:]):
            n = max(2, int(math.dist(a, b) / 0.1))
            hit = None
            for k in range(n + 1):
                x, y = a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n
                for o in obs:
                    if o.net != net and (o.layers is None or layer in o.layers) and o.distance(x, y) < width / 2 + clr - 0.005:
                        hit = f"{net} lane near ({x:.2f}, {y:.2f}) is too close to {o.net or 'a hole/keepout'}"
                        break
                if hit:
                    break
            if hit:
                problems.append(hit)
                break
    # lanes against each other
    for i in range(len(lanes)):
        for j in range(i + 1, len(lanes)):
            for a, b in zip(lanes[i]["points"], lanes[i]["points"][1:]):
                for c, d in zip(lanes[j]["points"], lanes[j]["points"][1:]):
                    if _seg_seg(a, b, c, d) < width + clr - 0.005:
                        problems.append(f"{lanes[i]['net']} and {lanes[j]['net']} lanes are too close (pitch)")
                        break
    return {"lanes": lanes, "problems": problems}


def _seg_seg(a, b, c, d) -> float:
    def pd(p, q, r):
        vx, vy = r[0] - q[0], r[1] - q[1]
        L2 = vx * vx + vy * vy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - q[0]) * vx + (p[1] - q[1]) * vy) / L2))
        return math.hypot(q[0] + t * vx - p[0], q[1] + t * vy - p[1])
    return min(pd(a, c, d), pd(b, c, d), pd(c, a, b), pd(d, a, b))
