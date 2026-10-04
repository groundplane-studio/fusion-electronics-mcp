"""Single-net router: one connection at a time, laid like a person would.

A* on a grid (default 0.127 mm) over the board's real obstacles (stitch.board_obstacles:
pads by shape, other nets' traces and vias, holes, keepouts, the outline with the
edge-clearance rule). Moves are the 8 directions; a 45-degree turn costs a little,
90-degree corners are not allowed (two 45s instead), reversing is not allowed, and a
via costs a lot, so routes come out straight, octilinear and on one layer where they
can. The grid is anchored on the start pad, and the last leg is re-aimed so the trace
ends exactly on the target pad centre. Every result is checked against the exact
geometry before it is returned.

Layers are the classic export's copper numbers: 1 = top, 16 = bottom.
"""

from __future__ import annotations

import heapq
import math
import re
from array import array
import xml.etree.ElementTree as ET

import numpy as np
from dataclasses import dataclass, field

from .stitch import Obstacle, _mm, board_obstacles

DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]


@dataclass
class Route:
    net: str
    legs: list = field(default_factory=list)      # [(layer, [(x, y), ...])]
    vias: list = field(default_factory=list)      # [(x, y)]
    length_mm: float = 0.0
    problems: list = field(default_factory=list)
    joined: str = ""
    conflicts: set = field(default_factory=set)   # route_all connections this route crosses
    widths: list = field(default_factory=list)    # per leg: None = the route's width, else a neck-down width

    def pieces(self, width: float):
        """(layer, points, width) per leg: the neck-downs at pads carry their own width."""
        ws = self.widths or [None] * len(self.legs)
        return [(l, pts, w or width) for (l, pts), w in zip(self.legs, ws)]

    def as_dict(self) -> dict:
        ws = self.widths or [None] * len(self.legs)
        return {"net": self.net, "legs": [{"layer": l, "points": [list(p) for p in pts], **({"width": w} if w else {})}
                                          for (l, pts), w in zip(self.legs, ws)],
                "vias": [list(v) for v in self.vias], "length_mm": round(self.length_mm, 3), "ends_on": self.joined,
                "problems": self.problems}


def _layer_ok(o: Obstacle, layer: int) -> bool:
    return o.layers is None or layer in o.layers


def _dist_field(o: Obstacle, XX, YY):
    """Distance from grid points (arrays) to an obstacle's copper (<= 0 inside), vectorised."""
    d = o.data
    if o.kind == "circle":
        return np.hypot(XX - d[0], YY - d[1]) - d[2]
    if o.kind == "rect":
        cx, cy, hw, hh, ang = d
        a = math.radians(-ang)
        dx, dy = XX - cx, YY - cy
        lx = dx * math.cos(a) - dy * math.sin(a)
        ly = dx * math.sin(a) + dy * math.cos(a)
        ox, oy = np.abs(lx) - hw, np.abs(ly) - hh
        return np.hypot(np.maximum(ox, 0), np.maximum(oy, 0)) + np.minimum(np.maximum(ox, oy), 0)
    x1, y1, x2, y2, half = d
    vx, vy = x2 - x1, y2 - y1
    L2 = vx * vx + vy * vy
    t = np.zeros_like(XX) if L2 == 0 else np.clip(((XX - x1) * vx + (YY - y1) * vy) / L2, 0.0, 1.0)
    return np.hypot(XX - (x1 + t * vx), YY - (y1 + t * vy)) - half


def _in_poly(poly, XX, YY):
    """Even-odd point-in-polygon for grid arrays."""
    inside = np.zeros(XX.shape, dtype=bool)
    for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1]):
        if ay == by:
            continue
        cond = (ay > YY) != (by > YY)
        xint = ax + (YY - ay) * (bx - ax) / (by - ay)
        inside ^= cond & (XX < xint)
    return inside


class Grid:
    """The routing grid for one net and one connection: blocked cells per layer (other nets'
    copper + clearance, holes, keepouts, the board edge), via-blocked cells, and per-cell extra
    costs (connector pin fields; other nets' pours). Built with numpy over a window around the
    connection; the search reads flat Python sequences (index = i * ny + j)."""

    def __init__(self, root: ET.Element, net: str, width: float, clearance: float | None, step: float,
                 origin: tuple[float, float], layers=(1, 16), via_d: float = 0.6, window=None, cache=None,
                 soft_cost: float = 4.0, pour_cost: float = 30.0, plane_cost: float = 0.25,
                 soft_tags: set | None = None, rip_cost: float = 15.0, necks=(), neck_width: float | None = None,
                 hug: float = 0.0):
        obs, rules, outline = cache if cache else board_obstacles(root)
        self.rules, self.outline, self.net, self.step = rules, outline, net, step
        self.clear = clearance if clearance is not None else _mm(rules.get("mdWireWire"), 0.2)
        self.edge = _mm(rules.get("mdCopperDimension"), 0.3)
        # a via keeps the design's pad-via / wire-via rules where they are larger (seen: a via
        # 0.123 mm from a pad against a 0.13 mm pad-via rule)
        self.via_clear_pad = max(self.clear, _mm(rules.get("mdPadVia"), 0.0))
        self.via_clear_wire = max(self.clear, _mm(rules.get("mdWireVia"), 0.0))
        self.half = width / 2
        # neck-down: within each neck circle (cx, cy, r) around a pad the trace may narrow to neck_width
        self.necks = [n for n in necks if neck_width and neck_width < width]
        self.half_n = (neck_width / 2) if self.necks else self.half
        self.via_r = via_d / 2
        xs = [v for s in outline for v in (s[0], s[2])] or [0, 100]
        ys = [v for s in outline for v in (s[1], s[3])] or [0, 100]
        if window:                         # only the area around the connection (much faster)
            wx0, wy0, wx1, wy1 = window
            xs = [max(min(xs), wx0), min(max(xs), wx1)]
            ys = [max(min(ys), wy0), min(max(ys), wy1)]
        ox, oy = origin
        self.x0 = ox - math.floor((ox - min(xs)) / step) * step
        self.y0 = oy - math.floor((oy - min(ys)) / step) * step
        self.nx = int((max(xs) - self.x0) / step) + 1
        self.ny = int((max(ys) - self.y0) / step) + 1
        bx0, by0, bx1, by1 = min(xs), min(ys), max(xs), max(ys)
        self.outside = lambda x, y: x < bx0 - 1e-6 or x > bx1 + 1e-6 or y < by0 - 1e-6 or y > by1 + 1e-6
        self.layers = layers
        near = lambda o: _obs_near(o, bx0 - 3, by0 - 3, bx1 + 3, by1 + 3)
        others = [o for o in obs if o.net != net and near(o)]
        # traces route_all laid may be crossed at a cost (they get ripped up and re-routed)
        st = soft_tags or set()
        self.ripable = [o for o in others if o.tag and o.tag in st]
        self.other = [o for o in others if not (o.tag and o.tag in st)]
        self.own = [o for o in obs if o.net == net and near(o)]
        self.X = self.x0 + np.arange(self.nx) * step
        self.Y = self.y0 + np.arange(self.ny) * step
        XX, YY = np.meshgrid(self.X, self.Y, indexing="ij")
        # board edge: copper keeps the edge-clearance rule from the outline
        if outline:
            poly = [(s[0], s[1]) for s in outline]
            inside = _in_poly(poly, XX, YY) if len(poly) >= 3 else np.ones(XX.shape, bool)
            edist = np.full(XX.shape, np.inf)
            for s in outline:
                edist = np.minimum(edist, _dist_field(Obstacle("seg", None, False, (s[0], s[1], s[2], s[3], 0.0)), XX, YY))
        else:
            inside, edist = np.ones(XX.shape, bool), np.full(XX.shape, np.inf)
        blocked = {}
        for l in layers:
            b = ~inside | (edist < self.edge + self.half)
            for o in self.other:
                if _layer_ok(o, l):
                    self._mark(b, o, self.half + self.clear)
            if self.necks:
                bn = ~inside | (edist < self.edge + self.half_n)
                for o in self.other:
                    if _layer_ok(o, l):
                        self._mark(bn, o, self.half_n + self.clear)
                b = np.where(self._neck_mask(XX, YY), bn, b)
            blocked[l] = b
        vb = ~inside | (edist < self.edge + self.via_r)
        for o in self.other:
            self._mark(vb, o, self.via_r + G_via_clear(self, o))
        for o in self.own:                       # no via in the net's own pads
            if o.is_pad:
                self._mark(vb, o, self.via_r + G_via_clear(self, o))
        for l in layers:
            vb |= blocked[l]
        # extra cost per cell entered: through-hole pin fields, other nets' pours on that layer
        cost = {l: np.zeros(XX.shape) for l in layers}
        for _ref, (cx0, cy0, cx1, cy1) in _tht_bodies(root):
            m = (XX >= cx0) & (XX <= cx1) & (YY >= cy0) & (YY <= cy1)
            for l in layers:
                cost[l][m] += soft_cost
        for o in self.ripable:
            for l in layers:
                if _layer_ok(o, l):
                    m = np.zeros(XX.shape, bool)
                    self._mark(m, o, self.half + self.clear)
                    cost[l][m] += rip_cost
        self._hug = {}
        if hug > 0:
            # run beside the traces already there (one clearance off), as a bundle: every cell
            # costs `hug` more except that band, so the search stays admissible. Search only:
            # the smoothing pass straightens without it (no lane-hopping jogs)
            for l in layers:
                band = np.zeros(XX.shape, bool)
                for o in self.other + self.ripable:
                    if o.kind == "seg" and not o.is_pad and _layer_ok(o, l):
                        self._band(band, o, self.half + self.clear, step * 1.01)
                self._hug[l] = np.where(band, 0.0, hug)
        for pnet, player, poly in _pours(root):
            if pnet == net:
                continue
            ground = re.match(r"(?i)^(a|d|p)?gnd", pnet)
            inside_p = _in_poly(poly, XX, YY)
            if not ground and player in (1, 16):
                vb |= inside_p                 # no via punched through another net's outer power pour
                                               # (inner planes are made for vias to pass)
            if player in cost:
                cost[player][inside_p] += plane_cost if ground else pour_cost
        self.blocked = blocked
        self.via_blocked = vb
        self._cost = cost

    def _band(self, grid, o: Obstacle, gap: float, width: float):
        """Mark cells between gap and gap + width from a trace (the lane beside it)."""
        x1, y1, x2, y2, h = o.data
        m = gap + width + h
        i0, j0 = self.ij(min(x1, x2) - m, min(y1, y2) - m)
        i1, j1 = self.ij(max(x1, x2) + m, max(y1, y2) + m)
        i0, j0, i1, j1 = max(i0, 0), max(j0, 0), min(i1, self.nx - 1), min(j1, self.ny - 1)
        if i1 < i0 or j1 < j0:
            return
        XX, YY = np.meshgrid(self.X[i0:i1 + 1], self.Y[j0:j1 + 1], indexing="ij")
        d = _dist_field(o, XX, YY)
        grid[i0:i1 + 1, j0:j1 + 1] |= (d >= gap - 1e-6) & (d <= gap + width)

    def _neck_mask(self, XX, YY):
        m = np.zeros(np.shape(XX), bool)
        for cx, cy, r in self.necks:
            m |= np.hypot(XX - cx, YY - cy) <= r
        return m

    def half_at(self, xs, ys):
        """Trace half-width at sample points (narrower inside the neck circles)."""
        if not self.necks:
            return self.half
        return np.where(self._neck_mask(xs, ys), self.half_n, self.half)

    def freeze(self):
        """Flat read-only views for the search."""
        self.B = {l: self.blocked[l].ravel().tobytes() for l in self.layers}
        self.VB = self.via_blocked.ravel().tobytes()
        self.C = {l: (self._cost[l] + self._hug[l] if l in self._hug else self._cost[l]).ravel().tolist()
                  for l in self.layers}

    def xy(self, i, j):
        return (round(self.x0 + i * self.step, 4), round(self.y0 + j * self.step, 4))

    def ij(self, x, y):
        return (int(round((x - self.x0) / self.step)), int(round((y - self.y0) / self.step)))

    def _mark(self, grid, o: Obstacle, margin: float):
        d = o.data
        if o.kind == "circle":
            bx0, by0, bx1, by1 = d[0] - d[2], d[1] - d[2], d[0] + d[2], d[1] + d[2]
        elif o.kind == "rect":
            r = math.hypot(d[2], d[3])
            bx0, by0, bx1, by1 = d[0] - r, d[1] - r, d[0] + r, d[1] + r
        else:
            bx0, bx1 = min(d[0], d[2]) - d[4], max(d[0], d[2]) + d[4]
            by0, by1 = min(d[1], d[3]) - d[4], max(d[1], d[3]) + d[4]
        i0, j0 = self.ij(bx0 - margin, by0 - margin)
        i1, j1 = self.ij(bx1 + margin, by1 + margin)
        i0, j0, i1, j1 = max(i0, 0), max(j0, 0), min(i1, self.nx - 1), min(j1, self.ny - 1)
        if i1 < i0 or j1 < j0:
            return
        XX, YY = np.meshgrid(self.X[i0:i1 + 1], self.Y[j0:j1 + 1], indexing="ij")
        grid[i0:i1 + 1, j0:j1 + 1] |= _dist_field(o, XX, YY) < margin - 1e-6

    def free(self, i, j, layer):
        return 0 <= i < self.nx and 0 <= j < self.ny and not self.blocked[layer][i, j]


def _pours(root):
    out = []
    for sg in root.iterfind("./drawing/board/signals/signal"):
        for pg in sg.iterfind("polygon"):
            lay = int(pg.get("layer") or 1)
            lay = 16 if lay in (16, 304) else lay
            out.append((sg.get("name"), lay, [(float(v.get("x")), float(v.get("y"))) for v in pg.iterfind("vertex")]))
    return out


def _tht_bodies(root):
    from .stitch import part_bodies
    board = root.find("./drawing/board")
    pkgs = {(lib.get("name"), pk.get("name")): pk for lib in board.iterfind("./libraries/library")
            for pk in lib.iterfind("./packages/package")}
    tht = {el.get("name") for el in board.iterfind("./elements/element")
           if (pk := pkgs.get((el.get("library"), el.get("package")))) is not None and len(pk.findall("pad")) >= 4}
    return [(r, b) for r, b in part_bodies(root, margin=0.0) if r in tht]


def G_via_clear(G, o: Obstacle) -> float:
    return G.via_clear_pad if o.is_pad else G.via_clear_wire


def _obs_near(o: Obstacle, x0, y0, x1, y1) -> bool:
    d = o.data
    if o.kind == "circle":
        return x0 - d[2] <= d[0] <= x1 + d[2] and y0 - d[2] <= d[1] <= y1 + d[2]
    if o.kind == "rect":
        r = math.hypot(d[2], d[3])
        return x0 - r <= d[0] <= x1 + r and y0 - r <= d[1] <= y1 + r
    return not (max(d[0], d[2]) + d[4] < x0 or min(d[0], d[2]) - d[4] > x1 or
                max(d[1], d[3]) + d[4] < y0 or min(d[1], d[3]) - d[4] > y1)


def _seg_dist(x, y, s):
    x1, y1, x2, y2 = s
    vx, vy = x2 - x1, y2 - y1
    L2 = vx * vx + vy * vy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - x1) * vx + (y - y1) * vy) / L2))
    return math.hypot(x - (x1 + t * vx), y - (y1 + t * vy))


SQ2 = math.sqrt(2)


def route(root: ET.Element, net: str, start: tuple[float, float], start_layers: tuple,
          goal: tuple[float, float], goal_layers: tuple, width: float = 0.25, clearance: float | None = None,
          prefer_layer: int | None = None, vias: bool = True, via_drill: float = 0.3, via_d: float = 0.6,
          step: float = 0.127, turn_cost: float = 0.6, via_cost: float = 8.0, layer_cost: float = 0.15,
          max_nodes: int = 2_000_000, join_existing: bool = False, margin_mm: float | None = None,
          soft_cost: float = 4.0, tap_exclude: set | None = None, pour_cost: float = 30.0,
          plane_cost: float = 0.25, cache=None, h_weight: float = 1.0, soft_tags: set | None = None,
          rip_cost: float = 15.0, neck_width: float | None = None, neck_len: float = 1.5, hug: float = 0.0) -> Route:
    """Route `net` from pad centre `start` (on any of start_layers) to pad centre `goal`, or
    (join_existing) to whichever is nearer: the goal pad or any trace the net already has, which
    it taps where it meets it (Fusion connects a trace ending part-way along another, verified
    on 2705.1.15). Costs per grid step: 1 (or sqrt 2) of length, a 45-degree turn, an extra cost
    inside through-hole pin fields (soft_cost), inside another net's power pour (pour_cost) or
    ground plane (plane_cost) on that layer; a via costs via_cost.

    Neck-down: a trace wider than the pad it starts or ends on (a power trace on a fine-pitch
    pin) narrows to the pad's short side (or neck_width) within neck_len of the pad, as a person
    does it; the rest of the run keeps its width."""
    span = math.dist(start, goal)
    if span > 40 and step < 0.25:          # long runs: a coarser grid keeps the search small
        step = 0.254
    m = margin_mm if margin_mm is not None else max(6.0, 0.6 * span)
    window = (min(start[0], goal[0]) - m, min(start[1], goal[1]) - m, max(start[0], goal[0]) + m, max(start[1], goal[1]) + m)
    obs_all = cache if cache else board_obstacles(root)
    necks, nw = [], neck_width
    for p in (start, goal):
        short = _pad_short_side(obs_all[0], net, p)
        if short and short < width:
            necks.append((p[0], p[1], neck_len + short / 2))
            nw = min(nw or short, short)
    G = Grid(root, net, width, clearance, step, start, via_d=via_d, window=window, cache=obs_all,
             soft_cost=soft_cost, pour_cost=pour_cost, plane_cost=plane_cost, soft_tags=soft_tags, rip_cost=rip_cost,
             necks=necks, neck_width=nw, hug=hug)
    si, sj = G.ij(*start)
    gi, gj = G.ij(*goal)
    r = Route(net)
    ny, nx = G.ny, G.nx
    for (pi, pj) in ((si, sj), (gi, gj)):  # the start/goal pad cells are free even if a neighbour's clearance reaches them
        if 0 <= pi < nx and 0 <= pj < ny:
            for l in G.layers:
                G.blocked[l][pi, pj] = False
    ex = tap_exclude or set()
    key = lambda p: (round(p[0], 3), round(p[1], 3))
    taps = [t for t in (_net_traces(root, net) if join_existing else [])
            if not (G.outside(*t[1]) and G.outside(*t[2])) and (key(t[1]), key(t[2])) not in ex and (key(t[2]), key(t[1])) not in ex]
    # heuristic: octile distance (in steps) to the nearest target (goal or sampled taps), precomputed
    tgt = [(gi, gj)]
    for l, a, b in taps:
        n_ = max(1, int(math.dist(a, b) / (2 * step)))
        tgt += [G.ij(a[0] + (b[0] - a[0]) * k / n_, a[1] + (b[1] - a[1]) * k / n_) for k in range(n_ + 1)]
    II, JJ = np.meshgrid(np.arange(nx), np.arange(ny), indexing="ij")
    H = np.full((nx, ny), np.inf)
    for ti, tj in set(tgt):
        dx, dy = np.abs(II - ti), np.abs(JJ - tj)
        H = np.minimum(H, np.maximum(dx, dy) + (SQ2 - 1) * np.minimum(dx, dy))
    H = (H * h_weight).ravel().tolist()      # weighted A*: > 1 searches far less for a slightly longer path
    # cells where the route may end on one of the net's own traces (same layer)
    tol = step * 0.55
    tapmask = {}
    if taps:
        XX, YY = np.meshgrid(G.X, G.Y, indexing="ij")
        for l in G.layers:
            mm = np.zeros((nx, ny), bool)
            for tl, a, b in taps:
                if tl == l:
                    mm |= _dist_field(Obstacle("seg", None, False, (a[0], a[1], b[0], b[1], 0.0)), XX, YY) <= tol
            tapmask[l] = mm.ravel().tobytes()
    G.freeze()
    B, VB, C = G.B, G.VB, G.C
    lidx = {l: k for k, l in enumerate(G.layers)}
    L = list(G.layers)
    gidx = gi * ny + gj
    goal_set = set(goal_layers)
    steps = [(di, dj, di * ny + dj, SQ2 if di and dj else 1.0) for di, dj in DIRS]
    pref_mult = {l: (1 + (layer_cost if prefer_layer and l != prefer_layer else 0)) for l in L}
    args = (nx, ny, B, VB, C, H, L, lidx, start_layers, si * ny + sj, gidx, goal_set, tapmask, steps,
            pref_mult, turn_cost, via_cost, vias, max_nodes)
    # fast pass: one state per cell and layer (the direction lives in the parent); a tight spot it
    # cannot solve gets the exact search with the direction in the state
    found = _search(*args, full=False)
    if found[0] is None:
        found = _search(*args, full=True)
    path, end_tap_cell, n, limited = found
    if path is None:
        r.problems.append("no route found" + (" (search limit)" if limited else ""))
        return r
    cells = [(idx // ny, idx % ny, L[li]) for idx, li in path]
    legs, cur, cur_l = [], [], cells[0][2]
    for (i, j, l) in cells:
        if l != cur_l:
            legs.append((cur_l, cur))
            r.vias.append(G.xy(i, j))
            cur, cur_l = [], l
        p = G.xy(i, j)
        if not cur or cur[-1] != p:
            cur.append(p)
    legs.append((cur_l, cur))
    legs = [(l, _corners(pts)) for l, pts in legs]
    end_tap = None
    if end_tap_cell is not None:
        x, y = G.xy(*divmod(end_tap_cell, ny))
        l_end = legs[-1][0]
        end_tap = [(a, b) for tl, a, b in taps if tl == l_end and _seg_dist(x, y, (a[0], a[1], b[0], b[1])) <= tol + 1e-6] or None
    l_last, pts = legs[-1]
    legs[-1] = (l_last, _land(pts, end_tap) if end_tap else _aim(pts, goal))
    r.joined = "existing trace" if end_tap else "pad"
    legs[0] = (legs[0][0], [start] + legs[0][1][1:]) if legs[0][1] else legs[0]
    legs = [(l, _smooth(G, l, p)) for l, p in legs]
    legs = [(l, p) for l, p in legs if len(p) >= 2]
    r.legs, r.widths = _split_necks(G, legs)
    r.length_mm = sum(math.dist(a, b) for _, p in r.legs for a, b in zip(p, p[1:]))
    r.problems += check_route(G, r)
    # which route_all connections this route crosses (they must be ripped up for it)
    for o in G.ripable:
        for l, pts in r.legs:
            if not _layer_ok(o, l):
                continue
            for a, b in zip(pts, pts[1:]):
                n_ = max(2, int(math.dist(a, b) / 0.05))
                t = np.linspace(0.0, 1.0, n_ + 1)
                xs, ys = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
                if (_dist_field(o, xs, ys) < G.half_at(xs, ys) + G.clear - 0.005).any():
                    r.conflicts.add(o.tag)
                    break
        for x, y in r.vias:
            if o.distance(x, y) < G.via_r + G_via_clear(G, o) - 0.005:
                r.conflicts.add(o.tag)
    return r


def _octi(a, b, tol=1e-4) -> bool:
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    return dx < tol or dy < tol or abs(dx - dy) < tol


def _seg_clear(G, l, a, b) -> bool:
    n = max(2, int(math.dist(a, b) / 0.05))
    t = np.linspace(0.0, 1.0, n + 1)
    xs, ys = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
    h = G.half_at(xs, ys)
    for o in G.other + G.ripable:
        if _layer_ok(o, l) and (_dist_field(o, xs, ys) < h + G.clear - 0.005).any():
            return False
    return True


def _pad_short_side(obs, net, p):
    """Short side of the net's own pad under point p (None when p is not on a pad)."""
    for o in obs:
        if o.is_pad and o.net == net and o.distance(*p) <= 1e-6:
            if o.kind == "rect":
                return 2 * min(o.data[2], o.data[3])
            if o.kind == "seg":
                return 2 * o.data[4]
            return 2 * o.data[2]
    return None


def _split_necks(G, legs):
    """Cut the legs where they cross a neck circle: the inside pieces get the neck width."""
    if not G.necks:
        return legs, []
    inside = lambda q: any(math.hypot(q[0] - cx, q[1] - cy) <= r + 1e-6 for cx, cy, r in G.necks)
    out, widths = [], []
    for l, pts in legs:
        cur = [pts[0]]
        cur_in = inside(pts[0])
        for a, b in zip(pts, pts[1:]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            ts = []
            for cx, cy, r in G.necks:
                fx, fy = a[0] - cx, a[1] - cy
                A, B, Cc = dx * dx + dy * dy, 2 * (fx * dx + fy * dy), fx * fx + fy * fy - r * r
                disc = B * B - 4 * A * Cc
                if A > 0 and disc > 0:
                    for t in ((-B - math.sqrt(disc)) / (2 * A), (-B + math.sqrt(disc)) / (2 * A)):
                        if 1e-6 < t < 1 - 1e-6:
                            ts.append(t)
            for t in sorted(ts):
                c = (round(a[0] + dx * t, 4), round(a[1] + dy * t, 4))
                now_in = inside((a[0] + dx * min(1.0, t + 1e-4), a[1] + dy * min(1.0, t + 1e-4)))
                if now_in != cur_in:
                    cur.append(c)
                    out.append((l, cur))
                    widths.append(2 * G.half_n if cur_in else None)
                    cur, cur_in = [c], now_in
            cur.append(b)
        if len(cur) >= 2:
            out.append((l, cur))
            widths.append(2 * G.half_n if cur_in else None)
    return out, widths


def _seg_cost(G, l, a, b) -> float:
    """Extra cost (pin fields, pours, ripable traces) along a segment, from the grid."""
    n = max(1, int(math.dist(a, b) / G.step))
    c = G._cost[l]
    tot = 0.0
    for k in range(n):
        x = a[0] + (b[0] - a[0]) * (k + 0.5) / n
        y = a[1] + (b[1] - a[1]) * (k + 0.5) / n
        i, j = G.ij(x, y)
        if 0 <= i < G.nx and 0 <= j < G.ny:
            tot += c[i, j]
    return tot * math.dist(a, b) / (n * G.step)


def _corner_options(a, b):
    """Corner points joining a to b with one straight and one 45-degree segment (both orders)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    m = min(abs(dx), abs(dy))
    sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
    return [(round(a[0] + sx * m, 4), round(a[1] + sy * m, 4)), (round(b[0] - sx * m, 4), round(b[1] - sy * m, 4))]


def _smooth(G, l, pts):
    """Take out the grid's jogs and staircases: replace runs of corners by one octilinear segment,
    or by two (straight + 45 degrees), where that is clear and costs no more (pin fields, pours)."""
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    i = 0
    n = len(pts)
    while i < n - 1:
        done = False
        for j in range(n - 1, i + 1, -1):
            old = sum(_seg_cost(G, l, p, q) for p, q in zip(pts[i:j], pts[i + 1:j + 1]))
            a, b = pts[i], pts[j]
            slack = 0.5                         # a pad exit's first step may sit in its own pin field
            if _octi(a, b) and _seg_clear(G, l, a, b) and _seg_cost(G, l, a, b) <= old + slack:
                out.append(b)
                i, done = j, True
                break
            for c in _corner_options(a, b):
                if (math.dist(a, c) > 1e-6 and math.dist(c, b) > 1e-6 and _octi(a, c) and _octi(c, b)
                        and _seg_clear(G, l, a, c) and _seg_clear(G, l, c, b)
                        and _seg_cost(G, l, a, c) + _seg_cost(G, l, c, b) <= old + slack):
                    out += [c, b]
                    i, done = j, True
                    break
            if done:
                break
        if not done:
            out.append(pts[i + 1])
            i += 1
    return _corners(out)


def _search(nx, ny, B, VB, C, H, L, lidx, start_layers, sidx, gidx, goal_set, tapmask, steps,
            pref_mult, turn_cost, via_cost, vias, max_nodes, full):
    """A* over cells x layers (x 8 directions when full). Returns ([(cell, layer index)], the cell
    where it tapped an existing trace or None, expansions, hit the limit)."""
    S = 8 if full else 1
    nst = nx * ny * 2 * S
    INF = 1e18
    best = array("d", [INF]) * nst
    came = array("q", [-1]) * nst
    dirs = None if full else array("b", [-1]) * nst
    open_ = []
    push, pop = heapq.heappush, heapq.heappop
    for l in start_layers:
        if l in lidx:
            if full:
                for d in range(8):
                    st = ((sidx * 2 + lidx[l]) << 3) | d
                    best[st] = 0.0
                    push(open_, (H[sidx], 0.0, st))
            else:
                st = sidx * 2 + lidx[l]
                best[st] = 0.0
                push(open_, (H[sidx], 0.0, st))
    end, end_tap_cell = None, None
    n = 0
    nlay = len(L)
    while open_:
        f, g, st = pop(open_)
        if g > best[st] + 1e-9:
            continue
        if full:
            d = st & 7
            rest = st >> 3
        else:
            d = dirs[st]
            rest = st
        li = rest & 1
        idx = rest >> 1
        l = L[li]
        if idx == gidx and l in goal_set:
            end = st
            break
        if g > 0 and tapmask and tapmask[l][idx]:
            end, end_tap_cell = st, idx
            break
        n += 1
        if n > max_nodes:
            break
        i, j = divmod(idx, ny)
        Bl, Cl = B[l], C[l]
        pm = pref_mult[l]
        for dd in ((-1, 0, 1) if d >= 0 else (0, 1, 2, 3, 4, 5, 6, 7)):
            nd = (d + dd) & 7 if d >= 0 else dd
            di, dj, didx, base = steps[nd]
            ni, nj = i + di, j + dj
            if ni < 0 or nj < 0 or ni >= nx or nj >= ny:
                continue
            nidx = idx + didx
            if Bl[nidx]:
                continue
            if di and dj and (Bl[idx + di * ny] or Bl[idx + dj]):
                continue                           # no squeezing diagonally between two blocked cells
            ng = g + base * pm + (turn_cost if (d >= 0 and dd) else 0.0) + Cl[nidx] * base
            ns = (((nidx * 2 + li) << 3) | nd) if full else nidx * 2 + li
            if ng < best[ns] - 1e-9:
                best[ns] = ng
                came[ns] = st
                if not full:
                    dirs[ns] = nd
                push(open_, (ng + H[nidx], ng, ns))
        if vias and not VB[idx]:
            for nli in range(nlay):
                if nli != li and not B[L[nli]][idx]:
                    ns = (((idx * 2 + nli) << 3) | (d & 7)) if full else idx * 2 + nli
                    ng = g + via_cost
                    if ng < best[ns] - 1e-9:
                        best[ns] = ng
                        came[ns] = st
                        if not full:
                            dirs[ns] = d
                        push(open_, (ng + H[idx], ng, ns))
    if end is None:
        return None, None, n, n > max_nodes
    path = [end]
    while came[path[-1]] >= 0:
        path.append(came[path[-1]])
    path.reverse()
    if full:
        return [((st >> 3) >> 1, (st >> 3) & 1) for st in path], end_tap_cell, n, False
    return [(st >> 1, st & 1) for st in path], end_tap_cell, n, False


def _net_traces(root: ET.Element, net: str) -> list:
    out = []
    for sig in root.iterfind("./drawing/board/signals/signal"):
        if sig.get("name") != net:
            continue
        for w in sig.iterfind("wire"):
            if w.get("layer") in ("1", "16") and not float(w.get("curve") or 0):
                out.append((int(w.get("layer")), (float(w.get("x1")), float(w.get("y1"))),
                            (float(w.get("x2")), float(w.get("y2")))))
    return out


def _land(pts, segs):
    """End the last run exactly on one of the traces `segs`: where the run's line crosses one
    (preferred: no jog), else at the nearest point of the first."""
    if isinstance(segs, tuple):
        segs = [segs]
    for seg in segs:
        hit = _cross(pts, seg)
        if hit:
            return hit
    a, b = segs[0]
    p = pts[-1]
    vx, vy = b[0] - a[0], b[1] - a[1]
    L2 = vx * vx + vy * vy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / L2))
    c = (round(a[0] + t * vx, 4), round(a[1] + t * vy, 4))
    return pts if math.dist(c, p) < 1e-6 else pts + [c]


def _cross(pts, seg):
    a, b = seg
    p = pts[-1]
    if len(pts) >= 2:
        q = pts[-2]
        d = (p[0] - q[0], p[1] - q[1])
        e = (b[0] - a[0], b[1] - a[1])
        den = d[0] * e[1] - d[1] * e[0]
        if abs(den) > 1e-9:
            t = ((a[0] - q[0]) * e[1] - (a[1] - q[1]) * e[0]) / den
            u = ((a[0] - q[0]) * d[1] - (a[1] - q[1]) * d[0]) / den
            if t > 0 and -1e-9 <= u <= 1 + 1e-9:
                return pts[:-1] + [(round(q[0] + d[0] * t, 4), round(q[1] + d[1] * t, 4))]
    return None


def _corners(pts):
    if len(pts) <= 2:
        return pts
    out = [pts[0]]
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        d1 = (round(b[0] - a[0], 3), round(b[1] - a[1], 3))
        d2 = (round(c[0] - b[0], 3), round(c[1] - b[1], 3))
        if abs(d1[0] * d2[1] - d1[1] * d2[0]) > 1e-6:
            out.append(b)
    out.append(pts[-1])
    return out


def _aim(pts, goal):
    """Move the last run sideways so it ends exactly on `goal` (keeps its direction)."""
    if len(pts) < 2:
        return pts + [goal]
    a, b = pts[-2], pts[-1]
    if math.dist(b, goal) < 1e-6:
        return pts
    d = (b[0] - a[0], b[1] - a[1])
    L = math.hypot(*d)
    if L < 1e-9:
        return pts[:-1] + [goal]
    d = (d[0] / L, d[1] / L)
    if len(pts) >= 3:
        p = pts[-3]
        e = (a[0] - p[0], a[1] - p[1])
        # intersection of the previous run (through p, direction e) and the line through goal with direction d
        den = e[0] * d[1] - e[1] * d[0]
        if abs(den) > 1e-9:
            t = ((goal[0] - p[0]) * d[1] - (goal[1] - p[1]) * d[0]) / den
            na = (round(p[0] + e[0] * t, 4), round(p[1] + e[1] * t, 4))
            if (na[0] - p[0]) * e[0] + (na[1] - p[1]) * e[1] > 0 and (goal[0] - na[0]) * d[0] + (goal[1] - na[1]) * d[1] > 0:
                return pts[:-2] + [na, goal]
    return pts + [goal]


def check_route(G: Grid, r: Route) -> list[str]:
    """Exact clearance check of a route against the other nets' copper and the edge (vectorised
    over sample points every 0.05 mm)."""
    for l, pts in r.legs:
        for a, b in zip(pts, pts[1:]):
            n = max(2, int(math.dist(a, b) / 0.05))
            t = np.linspace(0.0, 1.0, n + 1)
            xs, ys = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            for o in G.other:
                if not _layer_ok(o, l):
                    continue
                dd = _dist_field(o, xs, ys) - G.half_at(xs, ys)
                k = int(np.argmin(dd))
                if dd[k] < G.clear - 0.005:
                    return [f"layer {l} near ({xs[k]:.2f}, {ys[k]:.2f}) is {dd[k]:.3f} mm from "
                            f"{o.net or 'a hole/keepout'}"]
    for x, y in r.vias:
        for o in G.other:
            if o.distance(x, y) < G.via_r + G_via_clear(G, o) - 0.005:
                return [f"via at ({x}, {y}) too close to {o.net or 'a hole/keepout'}"]
    return []


def pad_target(root: ET.Element, ref: str, pad: str) -> tuple[float, float, tuple, str | None]:
    """(x, y, copper layers, net) of a part's pad: through-hole pads are on both layers,
    an SMD on its side (a mirrored part's SMDs are on the bottom)."""
    from .eagle import parse_rot
    from .stitch import _f, _xf
    board = root.find("./drawing/board")
    pkgs = {(lib.get("name"), pk.get("name")): pk for lib in board.iterfind("./libraries/library")
            for pk in lib.iterfind("./packages/package")}
    net = None
    for s in board.iterfind("./signals/signal"):
        if any(c.get("element") == ref and c.get("pad") == pad for c in s.iterfind("contactref")):
            net = s.get("name")
    for el in board.iterfind("./elements/element"):
        if el.get("name") != ref:
            continue
        pk = pkgs.get((el.get("library"), el.get("package")))
        ang, mir = parse_rot(el.get("rot"))
        for p in pk.iterfind("pad"):
            if p.get("name") == pad:
                x, y = _xf(_f(p, "x"), _f(p, "y"), _f(el, "x"), _f(el, "y"), ang, mir)
                return round(x, 4), round(y, 4), (1, 16), net
        for p in pk.iterfind("smd"):
            if p.get("name") == pad:
                x, y = _xf(_f(p, "x"), _f(p, "y"), _f(el, "x"), _f(el, "y"), ang, mir)
                side = 16 if (p.get("layer") == "1") == mir else 1
                return round(x, 4), round(y, 4), (side,), net
        raise KeyError(f"{ref} has no pad {pad!r}")
    raise KeyError(f"no part {ref!r} on the board")


def fragment(root: ET.Element, net: str, start: tuple[float, float]) -> tuple[set, tuple]:
    """The piece of `net`'s copper that touches `start`: its trace segments (as rounded end pairs)
    and the copper layers present at `start`. Traces join at shared ends and through vias."""
    key = lambda x, y: (round(float(x), 3), round(float(y), 3))
    sig = next((s for s in root.iterfind("./drawing/board/signals/signal") if s.get("name") == net), None)
    if sig is None:
        return set(), (1, 16)
    segs = [(key(w.get("x1"), w.get("y1")), key(w.get("x2"), w.get("y2")), int(w.get("layer")))
            for w in sig.iterfind("wire") if w.get("layer") in ("1", "16")]
    vias = {key(v.get("x"), v.get("y")) for v in sig.iterfind("via")}
    def on(p, a, b):                       # p on segment a-b (a tap ending part-way along it)
        return _seg_dist(p[0], p[1], (a[0], a[1], b[0], b[1])) <= 0.002

    s0 = key(*start)
    seen_pts = {(s0, l) for l in (1, 16)} if s0 in vias else set()
    layers_at = {l for a, b, l in segs if s0 in (a, b) or on(s0, a, b)} | ({1, 16} if s0 in vias else set())
    seen_pts |= {(s0, l) for l in layers_at}
    out, changed = set(), True
    while changed:
        changed = False
        for a, b, l in segs:
            if (a, b) in out:
                continue
            if (a, l) in seen_pts or (b, l) in seen_pts or any(ll == l and on(p, a, b) for p, ll in seen_pts):
                out.add((a, b))
                hits = [a, b] + [q for a2, b2, l2 in segs if l2 == l for q in (a2, b2) if on(q, a, b)]
                for p in hits:
                    for ll in ((1, 16) if p in vias else (l,)):
                        if (p, ll) not in seen_pts:
                            seen_pts.add((p, ll))
                            changed = True
    return out, tuple(sorted(layers_at)) or (1, 16)

