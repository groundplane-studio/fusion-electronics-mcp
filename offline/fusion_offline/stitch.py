"""Via stitching: choose via positions for a net's pours (e.g. GND) on a grid.

Pure geometry over the exported board (classic EAGLE XML), so it can be
tested without Fusion. A candidate grid point is kept when a via there would
be at least `clearance` away from:
- the board outline (plus the board-edge clearance rule),
- copper of OTHER nets on any layer: traces, vias, pads,
- ALL pads, including the stitched net's own (no via-in-pad),
- holes and restrict circles (layers 41/42/43, i.e. keepouts),
and at least `min_spacing` from other stitching vias and the net's existing
vias. Pads use their real shape: SMDs as rotated rectangles, through-hole
pads as circles (diameter from the drill and the restring rule).
Element transforms follow EAGLE: world = T + M(R(angle) * p), rotate then
mirror X (gerber-verified in Groundplane's fabhub).
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .eagle import parse_rot


def _f(el, k, d=0.0):
    v = el.get(k)
    return float(v) if v not in (None, "") else d


def _mm(v: str | None, default: float) -> float:
    if not v:
        return default
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*([a-z]*)", v)
    if not m:
        return default
    x, unit = float(m.group(1)), m.group(2)
    return x * 0.0254 if unit == "mil" else x


@dataclass
class Obstacle:
    kind: str        # circle | rect | seg
    net: str | None  # None = no net (holes, keepouts, unconnected pads)
    is_pad: bool
    data: tuple
    layers: frozenset | None = None   # export copper layers it occupies; None = all (THT pads, vias, holes)

    def distance(self, x: float, y: float) -> float:
        """Distance from (x, y) to the obstacle's copper/edge (<= 0 inside)."""
        if self.kind == "circle":
            cx, cy, r = self.data
            return math.hypot(x - cx, y - cy) - r
        if self.kind == "rect":
            cx, cy, hw, hh, ang = self.data
            a = math.radians(-ang)
            dx, dy = x - cx, y - cy
            lx, ly = dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)
            ox, oy = abs(lx) - hw, abs(ly) - hh
            return math.hypot(max(ox, 0), max(oy, 0)) + min(max(ox, oy), 0)
        x1, y1, x2, y2, half = self.data
        vx, vy = x2 - x1, y2 - y1
        L2 = vx * vx + vy * vy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - x1) * vx + (y - y1) * vy) / L2))
        return math.hypot(x - (x1 + t * vx), y - (y1 + t * vy)) - half


def _ring(rules: dict, kind: str, drill: float) -> float:
    """Annular ring the design rules give a pad or via, as EAGLE/Fusion compute
    it: rv*drill clamped to [rlMin, rlMax] (outer layers). The copper is the
    larger of this and the library/via diameter, so a 3.0 mm pad on a 2.3 mm
    drill with 25%/0.508 mm rules is really 3.316 mm (seen on the IO
    passthrough: a trace passed our model's 3.0 mm peg and failed Fusion's DRC)."""
    key = "Top" if kind == "Pad" else "Outer"
    rv = _mm(rules.get(f"rv{kind}{key}"), 0.25)
    lo = _mm(rules.get(f"rlMin{kind}{key}"), 0.0)
    hi = _mm(rules.get(f"rlMax{kind}{key}"), 0.508)
    return min(max(rv * drill, lo), hi)


def _xf(px, py, x, y, angle, mirror):
    a = math.radians(angle)
    rx, ry = px * math.cos(a) - py * math.sin(a), px * math.sin(a) + py * math.cos(a)
    if mirror:
        rx = -rx
    return x + rx, y + ry


def board_obstacles(root: ET.Element) -> tuple[list[Obstacle], dict, list[tuple[float, float, float, float]]]:
    """(obstacles, rules, outline segments) from a board XML root."""
    board = root.find("./drawing/board")
    rules = {p.get("name"): p.get("value") for p in board.iterfind("./designrules/param")}
    obs: list[Obstacle] = []
    # pad -> net from the signals' contactrefs
    pad_net = {}
    for s in board.iterfind("./signals/signal"):
        for c in s.iterfind("contactref"):
            pad_net[(c.get("element"), c.get("pad"))] = s.get("name")
        for w in s.iterfind("wire"):
            if w.get("layer") in ("19",):
                continue
            # arcs are kept as their chord, widened by the sagitta (conservative)
            c = abs(math.radians(_f(w, "curve")))
            chord = math.hypot(_f(w, "x2") - _f(w, "x1"), _f(w, "y2") - _f(w, "y1"))
            sag = 0.0 if c < 1e-9 else chord / (2 * math.sin(c / 2)) * (1 - math.cos(c / 2))
            obs.append(Obstacle("seg", s.get("name"), False,
                                (_f(w, "x1"), _f(w, "y1"), _f(w, "x2"), _f(w, "y2"), _f(w, "width") / 2 + sag),
                                frozenset({int(w.get("layer"))})))
        for v in s.iterfind("via"):
            d = max(_f(v, "diameter"), _f(v, "drill") + 2 * _ring(rules, "Via", _f(v, "drill")))
            obs.append(Obstacle("circle", s.get("name"), False, (_f(v, "x"), _f(v, "y"), d / 2)))
    pkgs = {}
    for lib in board.iterfind("./libraries/library"):
        for pk in lib.iterfind("./packages/package"):
            pkgs[(lib.get("name"), pk.get("name"))] = pk
    for el in board.iterfind("./elements/element"):
        pk = pkgs.get((el.get("library"), el.get("package")))
        if pk is None:
            continue
        ang, mir = parse_rot(el.get("rot"))
        ex, ey = _f(el, "x"), _f(el, "y")
        for smd in pk.iterfind("smd"):
            cx, cy = _xf(_f(smd, "x"), _f(smd, "y"), ex, ey, ang, mir)
            prot, _ = parse_rot(smd.get("rot"))
            a = (ang + prot) * (-1 if mir else 1)
            side = 16 if (smd.get("layer") == "1") == mir else 1
            obs.append(Obstacle("rect", pad_net.get((el.get("name"), smd.get("name"))), True,
                                (cx, cy, _f(smd, "dx") / 2, _f(smd, "dy") / 2, a), frozenset({side})))
        for pad in pk.iterfind("pad"):
            cx, cy = _xf(_f(pad, "x"), _f(pad, "y"), ex, ey, ang, mir)
            drill = _f(pad, "drill")
            dia = max(_f(pad, "diameter"), drill + 2 * _ring(rules, "Pad", drill))
            net_ = pad_net.get((el.get("name"), pad.get("name")))
            shape = (pad.get("shape") or "round").lower()
            if shape in ("long", "offset"):
                # LONG: elongated to 2x the diameter along the pad's x (psElongationLong 100%);
                # OFFSET: the same length, shifted to one side. Kept as a rectangle (conservative).
                prot, _ = parse_rot(pad.get("rot"))
                a = (ang + prot) * (-1 if mir else 1)
                if shape == "offset":
                    ra = math.radians(a)
                    cx, cy = cx + math.cos(ra) * dia / 2, cy + math.sin(ra) * dia / 2
                obs.append(Obstacle("rect", net_, True, (cx, cy, dia, dia / 2, a)))
            else:
                obs.append(Obstacle("circle", net_, True, (cx, cy, dia / 2)))
        for h in pk.iterfind("hole"):
            cx, cy = _xf(_f(h, "x"), _f(h, "y"), ex, ey, ang, mir)
            obs.append(Obstacle("circle", None, True, (cx, cy, _f(h, "drill") / 2)))
    outline = []
    for w in board.iterfind("./plain/wire"):
        if w.get("layer") == "20":
            outline.append((_f(w, "x1"), _f(w, "y1"), _f(w, "x2"), _f(w, "y2")))
    for h in board.iterfind("./plain/hole"):
        obs.append(Obstacle("circle", None, True, (_f(h, "x"), _f(h, "y"), _f(h, "drill") / 2)))
    for c in board.iterfind("./plain/circle"):
        if c.get("layer") in ("41", "42", "43") and _f(c, "width") == 0:
            obs.append(Obstacle("circle", None, True, (_f(c, "x"), _f(c, "y"), _f(c, "radius"))))
    return obs, rules, outline


def _inside(x, y, segs) -> bool:
    """Even-odd point-in-polygon over the outline's straight segments."""
    inside = False
    for x1, y1, x2, y2 in segs:
        if (y1 > y) != (y2 > y):
            xc = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if xc > x:
                inside = not inside
    return inside


def plan_stitching(root: ET.Element, net: str, pitch: float = 2.0, drill: float | None = None,
                   clearance: float | None = None, min_spacing: float | None = None,
                   keep_away: dict[str, float] | None = None) -> dict:
    """keep_away: {net regex: mm} larger clearances to some nets' copper, e.g. keep
    stitching vias as far from impedance-controlled pairs as the pours are."""
    import re
    away = [(re.compile(k), float(v)) for k, v in (keep_away or {}).items()]
    obs, rules, outline = board_obstacles(root)
    if not outline:
        raise ValueError("board has no outline")
    drill = drill or _mm(rules.get("msDrill"), 0.3)
    ring = max(0.25 * drill, _mm(rules.get("rlMinViaOuter"), 0.2032))
    r = (drill + 2 * ring) / 2
    clr = clearance if clearance is not None else max(_mm(rules.get("mdWireVia"), 0.2), _mm(rules.get("mdPadVia"), 0.2))
    edge = _mm(rules.get("mdCopperDimension"), 0.3)
    spacing = min_spacing if min_spacing is not None else pitch * 0.9
    xs = [v for s in outline for v in (s[0], s[2])]
    ys = [v for s in outline for v in (s[1], s[3])]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    edge_segs = [Obstacle("seg", None, False, (a, b, c, d, 0.0)) for a, b, c, d in outline]
    existing = [(o.data[0], o.data[1]) for o in obs if o.kind == "circle" and o.net == net and not o.is_pad]
    chosen: list[tuple[float, float]] = []
    reasons = {"edge": 0, "copper": 0, "pad": 0, "keepout_or_hole": 0, "spacing": 0}
    nx, ny = int((x1 - x0) / pitch), int((y1 - y0) / pitch)
    ox, oy = x0 + ((x1 - x0) - nx * pitch) / 2, y0 + ((y1 - y0) - ny * pitch) / 2
    for i in range(nx + 1):
        for j in range(ny + 1):
            x, y = round(ox + i * pitch, 4), round(oy + j * pitch, 4)
            if not _inside(x, y, outline) or min(e.distance(x, y) for e in edge_segs) < edge + r:
                reasons["edge"] += 1
                continue
            bad = None
            for o in obs:
                d = o.distance(x, y)
                if o.net is None:
                    if d < r + (clr if o.is_pad else 0.0):
                        bad = "keepout_or_hole" if not o.is_pad or o.kind == "circle" else "pad"
                        break
                elif o.is_pad:
                    if d < r + clr:
                        bad = "pad"
                        break
                elif o.net != net and d < r + max([clr] + [v for rx, v in away if rx.search(o.net)]):
                    bad = "copper"
                    break
            if bad:
                reasons[bad] += 1
                continue
            if any(math.hypot(x - a, y - b) < spacing for a, b in chosen + existing):
                reasons["spacing"] += 1
                continue
            chosen.append((x, y))
    return {"net": net, "vias": chosen, "drill_mm": drill, "diameter_mm": round(2 * r, 4),
            "clearance_mm": clr, "edge_clearance_mm": edge, "pitch_mm": pitch, "rejected": reasons}


def pad_center(root: ET.Element, ref: str, pad: str) -> tuple[float, float] | None:
    board = root.find("./drawing/board")
    pkgs = {(lib.get("name"), pk.get("name")): pk for lib in board.iterfind("./libraries/library")
            for pk in lib.iterfind("./packages/package")}
    for el in board.iterfind("./elements/element"):
        if el.get("name") != ref:
            continue
        pk = pkgs.get((el.get("library"), el.get("package")))
        ang, mir = parse_rot(el.get("rot"))
        for p in list(pk.iterfind("smd")) + list(pk.iterfind("pad")):
            if p.get("name") == pad:
                return _xf(_f(p, "x"), _f(p, "y"), _f(el, "x"), _f(el, "y"), ang, mir)
    return None


def plan_fanout(root: ET.Element, ref: str, pad: str, net: str, trace_width: float = 0.25,
                max_dist: float = 3.0, step: float = 0.1, drill: float | None = None) -> dict | None:
    """Nearest via position for a pad's fanout: the via clears other nets'
    copper, every pad (including this one: no via-in-pad), holes, keepouts and
    the edge; and the straight trace from the pad centre to it clears other
    nets' copper. Returns {via: (x, y), from: (x, y), distance} or None."""
    obs, rules, outline = board_obstacles(root)
    c = pad_center(root, ref, pad)
    if c is None:
        raise KeyError(f"no pad {ref}.{pad}")
    drill = drill or _mm(rules.get("msDrill"), 0.3)
    ring = max(0.25 * drill, _mm(rules.get("rlMinViaOuter"), 0.2032))
    r = (drill + 2 * ring) / 2
    clr = max(_mm(rules.get("mdWireVia"), 0.2), _mm(rules.get("mdPadVia"), 0.2))
    wclr = max(_mm(rules.get("mdWireWire"), 0.15), _mm(rules.get("mdWirePad"), 0.15))
    edge = _mm(rules.get("mdCopperDimension"), 0.3)
    edges = [Obstacle("seg", None, False, (a, b, cc, d, 0.0)) for a, b, cc, d in outline]
    others = [o for o in obs if o.net != net and o.net is not None]
    blockers = [o for o in obs if o.net is None or o.is_pad]
    best = None
    n = int(max_dist / step)
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            x, y = c[0] + i * step, c[1] + j * step
            dist = math.hypot(x - c[0], y - c[1])
            if dist > max_dist or (best and dist >= best["distance"]):
                continue
            if not _inside(x, y, outline) or min(e.distance(x, y) for e in edges) < edge + r:
                continue
            if any(o.distance(x, y) < r + (clr if (o.is_pad or o.net) else 0.0) for o in blockers + others):
                continue
            ok = True
            for k in range(1, 20):
                px, py = c[0] + (x - c[0]) * k / 20, c[1] + (y - c[1]) * k / 20
                if any(o.distance(px, py) < trace_width / 2 + wclr for o in others):
                    ok = False
                    break
            if ok:
                best = {"via": (round(x, 4), round(y, 4)), "from": (round(c[0], 4), round(c[1], 4)),
                        "distance": round(dist, 3), "drill": drill, "diameter": round(2 * r, 4)}
    return best


def pad_centres(root: ET.Element) -> dict[str, list[tuple[float, float, "Obstacle"]]]:
    """net -> [(cx, cy, pad shape)] for every connected pad."""
    obs, _, _ = board_obstacles(root)
    out: dict[str, list] = {}
    for o in obs:
        if o.is_pad and o.net:
            out.setdefault(o.net, []).append((o.data[0], o.data[1], o))
    return out


def stub_candidates(root: ET.Element, tol: float = 1e-3) -> list[dict]:
    """Segments with an end that is not at a same-net wire end/interior, via
    or pad CENTRE (Fusion's wire-stub rule). Each carries `on_pad`: the
    centre of a same-net pad whose copper the loose end lies on (an
    off-centre end, electrically connected: fix by snapping), or None (a
    true dangling end: delete)."""
    board = root.find("./drawing/board")
    pads = pad_centres(root)
    out = []
    for s in board.iterfind("./signals/signal"):
        net = s.get("name")
        wires = [w for w in s.iterfind("wire") if w.get("layer") != "19"]
        vias = [(_f(v, "x"), _f(v, "y")) for v in s.iterfind("via")]
        segs = [(_f(w, "x1"), _f(w, "y1"), _f(w, "x2"), _f(w, "y2"), w.get("layer"), _f(w, "width")) for w in wires]
        for i, (x1, y1, x2, y2, layer, width) in enumerate(segs):
            for (px, py) in ((x1, y1), (x2, y2)):
                ok = any(math.hypot(px - a, py - b) <= tol for a, b in vias) or                     any(math.hypot(px - cx, py - cy) <= tol for cx, cy, _ in pads.get(net, []))
                if not ok:
                    ok = any(j != i and l2 == layer and
                             Obstacle("seg", net, False, (a1, b1, a2, b2, 0.0)).distance(px, py) <= tol
                             for j, (a1, b1, a2, b2, l2, _) in enumerate(segs))
                if not ok:
                    on = next(((cx, cy) for cx, cy, o in pads.get(net, []) if o.distance(px, py) <= tol), None)
                    out.append({"net": net, "layer": int(layer), "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                                "width": width, "dangling": (px, py), "on_pad": on,
                                "length": round(math.hypot(x2 - x1, y2 - y1), 4)})
                    break
    return out


def find_stubs(root: ET.Element, tol: float = 1e-3) -> list[dict]:
    """Copper wire segments with an end that connects to nothing: not on a
    same-net pad's copper (anywhere on the pad, as Fusion counts it, not only
    its centre), a same-net via, or another same-net wire on that layer.
    Pours are ignored (a stub inside a pour is still a stub to DRC)."""
    board = root.find("./drawing/board")
    obs, _, _ = board_obstacles(root)
    pads_by_net: dict[str, list[Obstacle]] = {}
    for o in obs:
        if o.is_pad and o.net:
            pads_by_net.setdefault(o.net, []).append(o)
    stubs = []
    for s in board.iterfind("./signals/signal"):
        net = s.get("name")
        wires = [w for w in s.iterfind("wire") if w.get("layer") != "19"]
        vias = [(_f(v, "x"), _f(v, "y")) for v in s.iterfind("via")]
        segs = [(_f(w, "x1"), _f(w, "y1"), _f(w, "x2"), _f(w, "y2"), w.get("layer"), _f(w, "width")) for w in wires]
        for i, (x1, y1, x2, y2, layer, width) in enumerate(segs):
            for (px, py) in ((x1, y1), (x2, y2)):
                connected = any(math.hypot(px - a, py - b) <= tol for a, b in vias) or                     any(p.distance(px, py) <= tol for p in pads_by_net.get(net, []))
                if not connected:
                    for j, (a1, b1, a2, b2, l2, _) in enumerate(segs):
                        if j == i or l2 != layer:
                            continue
                        if Obstacle("seg", net, False, (a1, b1, a2, b2, 0.0)).distance(px, py) <= tol:
                            connected = True
                            break
                if not connected:
                    stubs.append({"net": net, "layer": int(layer), "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                                  "dangling": (px, py), "length": round(math.hypot(x2 - x1, y2 - y1), 4)})
                    break
    return stubs
