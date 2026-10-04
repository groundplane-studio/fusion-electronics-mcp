"""Lay out a schematic block: a main part with its passives wired to it.

Input: one block from sch_plan.plan (anchor + members), the netlist, symbol
geometry (symbols.Geo per part) and the rail sets. Output: a Drawing with part
placements, named net wires, junctions, net labels and power symbols, in
millimetres, y up (EAGLE's frame), the anchor at the origin.

How a block is drawn (the style approved on a real board):
- every anchor pin gets a row on its side; a row's net is drawn as a horizontal
  trunk out of the pin;
- along a trunk: parts to a rail hang off it (ground down, supplies up, as
  vertical chains: an LED and its resistor stack), a label stub if the net
  leaves the block, branch lines for extra series parts, and the main line last:
  a series part inline (the trunk continues on its far side) or a multi-pin
  part (a FET) entered at its pin;
- a two-pin part between two adjacent rows (a bootstrap cap) bridges them;
- ground is always a symbol; a rail this block makes (its inductor or pins
  source it) is drawn as a trunk ending in a power symbol, other rails are
  symbols; nets that leave the block end in labels;
- rows are ordered outward so nothing crosses: a row whose drawing reaches over
  another row's line is placed beyond that row; rows that would have to be
  both nearer and further (a tall FET driver) drop into a zone below the part.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from .design import transform
from .symbols import Geo, solve

G = 2.54           # grid
GAP = 2.54         # minimum clear space between drawn things
TEXT_W = 0.78      # character width as a fraction of text size (Fusion's vector font, roughly)
LABEL_SIZE = 1.27  # net label text (the user's standard: reads best in Fusion's schematic view)


# ---------------------------------------------------------------------------
# drawing


@dataclass
class Drawing:
    parts: dict = field(default_factory=dict)          # ref -> (x, y, rot, mirror)
    wires: list = field(default_factory=list)          # (net, [(x, y), ...])
    labels: list = field(default_factory=list)         # (net, x, y, dir) dir +1 flag to the right, -1 left
    supplies: list = field(default_factory=list)       # (net, kind 'gnd'|'bar', x, y, facing (dx, dy))
    boxes: list = field(default_factory=list)          # (x0, y0, x1, y1, owner)

    def moved(self, dx: float, dy: float) -> "Drawing":
        r = lambda v: round(v, 4)
        return Drawing(
            {k: (r(x + dx), r(y + dy), a, m) for k, (x, y, a, m) in self.parts.items()},
            [(n, [(r(x + dx), r(y + dy)) for x, y in pts]) for n, pts in self.wires],
            [(n, r(x + dx), r(y + dy), d) for n, x, y, d in self.labels],
            [(n, k, r(x + dx), r(y + dy), f) for n, k, x, y, f in self.supplies],
            [(r(a + dx), r(b + dy), r(c + dx), r(d + dy), o) for a, b, c, d, o in self.boxes])

    def mirrored(self) -> "Drawing":
        """Mirror about x = 0 (parts get the mirror flag toggled)."""
        return Drawing(
            {k: (-x, y, a, not m) for k, (x, y, a, m) in self.parts.items()},
            [(n, [(-x, y) for x, y in pts]) for n, pts in self.wires],
            [(n, -x, y, -d) for n, x, y, d in self.labels],
            [(n, k, -x, y, (-f[0], f[1])) for n, k, x, y, f in self.supplies],
            [(-c, b, -a, d, o) for a, b, c, d, o in self.boxes])

    def merge(self, o: "Drawing") -> "Drawing":
        self.parts.update(o.parts)
        self.wires += o.wires
        self.labels += o.labels
        self.supplies += o.supplies
        self.boxes += o.boxes
        return self

    def extent(self) -> tuple[float, float, float, float]:
        xs, ys = [], []
        for a, b, c, d, _ in self.boxes:
            xs += [a, c]; ys += [b, d]
        for _, pts in self.wires:
            xs += [p[0] for p in pts]; ys += [p[1] for p in pts]
        if not xs:
            return (0.0, 0.0, 0.0, 0.0)
        return (min(xs), min(ys), max(xs), max(ys))

    def empty(self) -> bool:
        return not (self.parts or self.wires or self.labels or self.supplies)


def text_w(s: str, size: float = 1.778) -> float:
    return len(s) * size * TEXT_W


def label_box(net: str, x: float, y: float, d: int) -> tuple:
    w = text_w(net, LABEL_SIZE) + 2.0
    return (x, y - 1.3, x + w, y + 1.3, "label:" + net) if d > 0 else (x - w, y - 1.3, x, y + 1.3, "label:" + net)


# ---------------------------------------------------------------------------
# block context


@dataclass
class Ctx:
    geo: dict                    # ref -> Geo
    pad_net: dict                # (ref, pad) -> net
    values: dict                 # ref -> value text (for spacing)
    block: set                   # refs in this block (anchor + members)
    anchor: str
    ground: set
    rails: set
    owned: set                   # rails this block makes
    external: set                # nets that leave the block
    supply_geo: dict             # 'gnd' / 'bar' -> Geo
    placed: set = field(default_factory=set)
    drawn: set = field(default_factory=set)      # nets drawn as a trunk
    floating: list = field(default_factory=list)  # nets to draw as a labelled trunk elsewhere
    notes: list = field(default_factory=list)

    def pin_net(self, ref: str, pin: str) -> str | None:
        p = self.geo[ref].pins[pin]
        nets = {self.pad_net.get((ref, pad)) for pad in p.pads} - {None}
        return next(iter(nets)) if len(nets) == 1 else (sorted(nets)[0] if nets else None)

    def wired_pins(self, ref: str) -> list[str]:
        return [p for p in self.geo[ref].pins if self.pin_net(ref, p)]

    def members_on(self, net: str, exclude=()) -> list[tuple[str, str]]:
        out = []
        for ref in sorted(self.block - {self.anchor} - self.placed - set(exclude), key=_natural):
            for p in self.wired_pins(ref):
                if self.pin_net(ref, p) == net:
                    out.append((ref, p))
        return out

    def net_size(self, net: str) -> int:
        return sum(1 for (r, _), n in self.pad_net.items() if n == net)

    def is_rail(self, net: str | None) -> bool:
        return net is not None and (net in self.ground or net in self.rails)


def _natural(s: str):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


# ---------------------------------------------------------------------------
# part and symbol placement helpers


def _part_boxes(ctx: Ctx, ref: str, x: float, y: float, rot: int, mir: bool) -> list:
    geo: Geo = ctx.geo[ref]
    x0, y0, x1, y1 = geo.body()
    pts = [transform(px, py, x, y, rot, mir) for px, py in ((x0, y0), (x1, y1), (x0, y1), (x1, y0))]
    boxes = [(min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts), ref)]
    for prim in geo.prims:
        if prim[0] != "text":
            continue
        s = {">NAME": ref, ">VALUE": ctx.values.get(ref, "")}.get(prim[4], prim[4])
        if not s:
            continue
        tx, ty = transform(prim[1], prim[2], x, y, rot, mir)
        ang = (prim[5] + rot) % 180
        w, h = text_w(s, prim[3]), prim[3]
        if ang == 90:      # reads bottom to top; Fusion keeps it on the anchor's side
            boxes.append((tx - h, ty, tx, ty + w, ref + ":text") if not mir else (tx, ty, tx + h, ty + w, ref + ":text"))
        else:
            left = tx - w if (mir and rot in (0, 180)) or (not mir and rot == 180) else tx
            if "center" in prim[6]:
                left = tx - w / 2
            boxes.append((left, ty - 0.2, left + w, ty + h, ref + ":text"))
    return boxes


def put_part(ctx: Ctx, d: Drawing, ref: str, pin: str, at, facing) -> dict:
    """Place `ref` with `pin` at `at` facing `facing`; returns {pin: ((x, y), outward)} for all pins."""
    x, y, rot, mir = solve(ctx.geo[ref], pin, at, facing)
    d.parts[ref] = (x, y, rot, mir)
    d.boxes += _part_boxes(ctx, ref, x, y, rot, mir)
    ctx.placed.add(ref)
    out = {}
    for name, p in ctx.geo[ref].pins.items():
        px, py = transform(p.x, p.y, x, y, rot, mir)
        ox, oy = transform(p.ox, p.oy, 0, 0, rot, mir)
        out[name] = ((round(px, 4), round(py, 4)), (round(ox), round(oy)))
    return out


def put_supply(ctx: Ctx, d: Drawing, net: str, at, facing) -> None:
    """A power symbol whose pin is at `at`, pointing back along `facing` (the symbol body
    lies in direction `facing` from the point)."""
    kind = "gnd" if net in ctx.ground else "bar"
    geo = ctx.supply_geo[kind]
    pin = next(iter(geo.pins))
    back = (-facing[0], -facing[1])
    x, y, rot, mir = solve(geo, pin, at, back)
    d.supplies.append((net, kind, at[0], at[1], tuple(facing)))
    bx0, by0, bx1, by1 = geo.body()
    pts = [transform(px, py, x, y, rot, mir) for px, py in ((bx0, by0), (bx1, by1), (bx0, by1), (bx1, by0))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    w = text_w("GND" if kind == "gnd" else net, 1.524) / 2 + 0.5
    if facing[1] != 0:      # vertical symbol: text centred above/below
        d.boxes.append((min(xs) - w + 1.9, min(ys) - (2.6 if facing[1] < 0 else 0), max(xs) + w - 1.9,
                        max(ys) + (2.6 if facing[1] > 0 else 0), "sup:" + net))
    else:
        d.boxes.append((min(xs), min(ys) - 0.4, max(xs) + text_w(net, 1.524) + 2.0, max(ys) + 0.4, "sup:" + net))


def two_pin_other(ctx: Ctx, ref: str, pin: str) -> str | None:
    others = [p for p in ctx.wired_pins(ref) if p != pin]
    return others[0] if len(others) == 1 else None


def follow_chain(ctx: Ctx, ref: str, pin: str) -> tuple[list[tuple[str, str, str]], str | None]:
    """From two-pin `ref` entered at `pin`: the series chain through nets that only join two
    block parts, and the net it ends on."""
    seq = []
    cur, entry = ref, pin
    seen = set()
    while True:
        seen.add(cur)
        out = two_pin_other(ctx, cur, entry)
        if out is None:
            return seq, None
        seq.append((cur, entry, out))
        n2 = ctx.pin_net(cur, out)
        if ctx.is_rail(n2) or n2 in ctx.external or ctx.net_size(n2) != 2:
            return seq, n2
        nxt = [(r, p) for r, p in ctx.members_on(n2) if r != cur and r not in seen]
        if len(nxt) != 1 or two_pin_other(ctx, *nxt[0]) is None or nxt[0][0] == ctx.anchor:
            return seq, n2
        cur, entry = nxt[0]


def vchain(ctx: Ctx, seq, end_net: str | None, up: bool) -> Drawing:
    """A vertical stack of two-pin parts hanging from (0, 0), ending in a power symbol."""
    d = Drawing()
    s = 1 if up else -1
    y = 0.0
    net_in = None
    for i, (ref, entry, out) in enumerate(seq):
        top = y + s * G
        n_in = ctx.pin_net(ref, entry)
        d.wires.append((n_in, [(0.0, y), (0.0, top)]))
        pins = put_part(ctx, d, ref, entry, (0.0, top), (0, -s))
        if out is None:          # a one-pin part (test point) ends the chain
            return d
        (ox_, oy_), _ = pins[out]
        y = oy_
        net_in = ctx.pin_net(ref, out)
    if end_net is not None:
        end = (0.0, y + s * G)
        d.wires.append((end_net, [(0.0, y), end]))
        if ctx.is_rail(end_net):
            put_supply(ctx, d, end_net, end, (0, s))
        else:
            d.labels.append((end_net, end[0], end[1], 1))
            d.boxes.append(label_box(end_net, end[0], end[1], 1))
    return d


# ---------------------------------------------------------------------------
# trunks


def trunk(ctx: Ctx, net: str, came_from: str | None = None, bridges=None, end_label: bool = True) -> Drawing:
    """The drawing of `net` entered at (0, 0) from the left; everything lies at x >= 0."""
    ctx.drawn.add(net)
    d = Drawing()
    members = ctx.members_on(net, exclude=[came_from] if came_from else [])
    down, up, series, devices = [], [], [], []
    for ref, pin in members:
        if ref in ctx.placed:
            continue
        pins = ctx.wired_pins(ref)
        if len(pins) >= 3:
            devices.append((ref, pin))
            continue
        if len(pins) == 1:
            up.append(([(ref, pin, None)], None))
            continue
        other = two_pin_other(ctx, ref, pin)
        if other is None:
            continue
        seq, end = follow_chain(ctx, ref, pin)
        n2 = ctx.pin_net(ref, other)
        if bridges is not None and n2 in bridges:
            series.append(("bridge", ref, pin, n2))
        elif end in ctx.ground:
            down.append((seq, end))
        elif ctx.is_rail(end) and not (end in ctx.owned and end not in ctx.drawn):
            up.append((seq, end))
        else:
            series.append(("series", ref, pin, n2))
    cursor = 1.27
    junctions: list[float] = []
    for seq, end in down + up:
        if any(r in ctx.placed for r, _, _ in seq):
            continue
        sub = vchain(ctx, seq, end, up=(end not in ctx.ground))
        x0, _, x1, _ = sub.extent()
        x = max(cursor - x0, cursor)
        d.merge(sub.moved(x, 0))
        junctions.append(x)
        cursor = x + x1 + GAP
    # main line: a device first, else the series part with the most behind it
    lines = [("device", r, p, None) for r, p in devices] + series
    lines.sort(key=lambda it: (it[0] != "device", it[0] == "bridge", -_reach(ctx, it)))
    main = lines[0] if lines and lines[0][0] != "bridge" else None
    # a bridge (two-pin part to the adjacent row) goes on the row that ends with it; a row
    # that carries on past it leaves the bridge to its neighbour
    bridge_items = [it for it in lines if it[0] == "bridge"][:1] if main is None else []
    branches = [it for it in lines if it is not main and it[0] != "bridge"]
    stub_label = None
    if end_label and (net in ctx.external or (net in ctx.owned)) and main is not None:
        stub_label = net
    # columns: stub label, branch drops, then the main line
    label_x = None
    if stub_label:
        label_x = cursor
        sub = Drawing()
        sub.wires.append((net, [(0.0, 0.0), (0.0, -G * 2)]))
        if net in ctx.owned and net not in ctx.external:
            sub.wires[-1] = (net, [(0.0, 0.0), (0.0, G * 2)])
            put_supply(ctx, sub, net, (0.0, G * 2), (0, 1))
        else:
            sub.labels.append((net, 0.0, -G * 2, 1))
            sub.boxes.append(label_box(net, 0.0, -G * 2, 1))
        d.merge(sub.moved(label_x, 0))
        junctions.append(label_x)
        cursor = label_x + sub.extent()[2] + GAP
    branch_x = []
    for _ in branches:
        branch_x.append(cursor)
        junctions.append(cursor)
        cursor += G * 2
    end_x = None
    if main is not None:
        m = line(ctx, main)
        x0, _, _, _ = m.extent()
        xm = max(cursor, cursor - x0)
        d.merge(m.moved(xm, 0))
        end_x = xm
    # branches: rightmost first, each dropped below everything to its right
    for (it, bx) in sorted(zip(branches, branch_x), key=lambda t: -t[1]):
        m = line(ctx, it)
        if m.empty():
            continue
        mx0, my0, mx1, my1 = m.extent()
        lo = 0.0
        for a, b, c, e, _ in d.boxes:
            if c >= bx - 0.1 and a <= bx + mx1 + 0.1:
                lo = min(lo, b)
        for _, pts in d.wires:
            for (px, py) in pts:
                if bx - 0.1 <= px <= bx + mx1 + 0.1:
                    lo = min(lo, py)
        by = lo - GAP - my1
        d.wires.append((net, [(bx, 0.0), (bx, by), (bx + G, by)]))
        d.merge(m.moved(bx + G, by))
    for it in bridge_items:
        _, ref, pin, n2 = it
        x = cursor if end_x is None else max(cursor, d.extent()[2] + GAP)
        pins = put_part(ctx, d, ref, pin, (x + G, 0.0), (-1, 0))
        other = two_pin_other(ctx, ref, pin)
        (ox, oy), _ = pins[other]
        d.wires.append((net, [(x, 0.0), (x + G, 0.0)]))
        bridges[n2] = (ox + G, oy, ref, other)      # caller draws the drop to the other row
        junctions.append(x)
        end_x = end_x if end_x is not None else x
    if main is None and not bridge_items:
        if end_label and net in ctx.external:
            lx = max(cursor, G)
            d.labels.append((net, lx, 0.0, 1))
            d.boxes.append(label_box(net, lx, 0.0, 1))
            end_x = lx
        elif net in ctx.owned:
            lx = max(cursor, G)
            put_supply(ctx, d, net, (lx, 0.0), (0, 1))
            end_x = lx
    xs = sorted(set([0.0] + junctions + ([end_x] if end_x is not None else [])))
    if len(xs) > 1:
        d.wires.append((net, [(x, 0.0) for x in xs]))
    return d


def _reach(ctx: Ctx, it) -> int:
    """How much hangs behind a main-line candidate (bigger goes inline)."""
    kind, ref, pin, n2 = it
    if kind == "device":
        return 100
    return len(ctx.members_on(n2, exclude=[ref])) if n2 else 0


def line(ctx: Ctx, it) -> Drawing:
    """A main/branch line entered at (0, 0) from the left: a series part inline then its far
    net's trunk, or a multi-pin part entered at its pin."""
    kind, ref, pin, n2 = it
    d = Drawing()
    if ref in ctx.placed:
        return d
    if kind == "series":
        pins = put_part(ctx, d, ref, pin, (0.0, 0.0), (-1, 0))
        other = two_pin_other(ctx, ref, pin)
        (ox, oy), _ = pins[other]
        if n2 is None:
            return d
        if ctx.is_rail(n2) and not (n2 in ctx.owned and n2 not in ctx.drawn):
            d.wires.append((n2, [(ox, oy), (ox + G, oy)]))
            put_supply(ctx, d, n2, (ox + G, oy), (0, -1) if n2 in ctx.ground else (0, 1))
            return d
        sub = trunk(ctx, n2, came_from=ref)
        if sub.empty():
            if n2 in ctx.external:
                d.wires.append((n2, [(ox, oy), (ox + G, oy)]))
                d.labels.append((n2, ox + G, oy, 1))
                d.boxes.append(label_box(n2, ox + G, oy, 1))
            return d
        return d.merge(sub.moved(ox, oy))
    # device: enter at `pin`, then each other pin outward
    pins = put_part(ctx, d, ref, pin, (0.0, 0.0), (-1, 0))
    for q, ((px, py), (fx, fy)) in pins.items():
        if q == pin:
            continue
        nq = ctx.pin_net(ref, q)
        if nq is None:
            continue
        stub = (px + fx * G, py + fy * G)
        if ctx.is_rail(nq) and not (nq in ctx.owned and nq not in ctx.drawn):
            if nq in ctx.ground and fy > 0:            # a ground pin pointing up: step sideways first
                d.wires.append((nq, [(px, py), stub, (stub[0] + G, stub[1])]))
                put_supply(ctx, d, nq, (stub[0] + G, stub[1]), (0, -1))
            else:
                d.wires.append((nq, [(px, py), stub]))
                put_supply(ctx, d, nq, stub, (fx, fy) if fx else (0, -1 if nq in ctx.ground else 1))
            continue
        if (fx, fy) == (1, 0) and nq not in ctx.drawn:
            d.merge(trunk(ctx, nq, came_from=ref).moved(px, py))
            continue
        if fx == 0:
            ms = [(r, p) for r, p in ctx.members_on(nq) if r != ref]
            if len(ms) == 1 and two_pin_other(ctx, *ms[0]):
                seq, end = follow_chain(ctx, *ms[0])
                up = fy > 0
                if end is not None and ((end in ctx.ground) != up):
                    sub = vchain(ctx, seq, end, up)
                    d.merge(sub.moved(px, py))
                    continue
        # anything else: a labelled stub here, the net drawn as its own trunk elsewhere
        d.wires.append((nq, [(px, py), stub]))
        d.labels.append((nq, stub[0], stub[1], 1 if fx >= 0 else -1))
        d.boxes.append(label_box(nq, stub[0], stub[1], 1 if fx >= 0 else -1))
        if nq not in ctx.drawn and ctx.members_on(nq):
            ctx.floating.append(nq)
    return d


# ---------------------------------------------------------------------------
# rows on one side of the anchor


@dataclass
class Row:
    pins: list                   # [(pin name, (x, y))] joined pins, top first
    net: str | None
    shape: Drawing = field(default_factory=Drawing)
    attach_min: float = 0.0      # x where the shape may start (pin end or the join wire)
    zone: bool = False
    x: float = 0.0
    joins: list = field(default_factory=list)   # x positions on this row's trunk where bridges land
    bridge_to: tuple | None = None               # (row index, x offset, y offset in shape coords)

    @property
    def y(self) -> float:
        return self.pins[0][1][1]

    @property
    def pin_x(self) -> float:
        return self.pins[0][1][0]


def side_rows(ctx: Ctx, pins: list) -> list[Row]:
    """pins: [(name, (x, y))] on one side (right-hand local frame), any order."""
    pins = sorted(pins, key=lambda p: -p[1][1])
    rows: list[Row] = []
    for name, pt in pins:
        net = ctx.pin_net(ctx.anchor, name)
        if rows and net is not None and rows[-1].net == net and abs(rows[-1].pins[-1][1][1] - pt[1]) <= G + 0.01:
            rows[-1].pins.append((name, pt))
        else:
            rows.append(Row([(name, pt)], net))
    return rows


def build_side(ctx: Ctx, rows: list[Row], body_bottom: float, top_y: float, bottom_y: float) -> Drawing:
    """Lay out one side's rows (right-hand frame: pins point +x). Returns the side's drawing."""
    seen_nets = set()
    bridges_by_row: dict[int, dict] = {}
    for i, r in enumerate(rows):
        n = r.net
        joined = len(r.pins) > 1
        r.attach_min = r.pin_x + G          # always a wire stub: a symbol or label never sits on the pin
        if n is None:
            continue
        first_time = n not in seen_nets
        seen_nets.add(n)
        d = Drawing()
        if n in ctx.ground or (ctx.is_rail(n) and not (n in ctx.owned and n not in ctx.drawn and first_time)):
            last = r.pins[-1][1][1]
            if n in ctx.ground and i == len(rows) - 1:
                if abs(last - r.y) > 0.01:
                    d.wires.append((n, [(0.0, 0.0), (0.0, last - r.y)]))
                put_supply(ctx, d, n, (0.0, last - r.y), (0, -1))
            elif not ctx.is_rail(n) or (n not in ctx.ground and i == 0):
                put_supply(ctx, d, n, (0.0, 0.0), (0, 1))
            else:
                put_supply(ctx, d, n, (0.0, 0.0), (1, 0))
            r.shape = d
            continue
        if not first_time or n in ctx.drawn:
            # the net's trunk is elsewhere: just a label here
            d.labels.append((n, 0.0, 0.0, 1))
            d.boxes.append(label_box(n, 0.0, 0.0, 1))
            r.shape = d
            continue
        # adjacent rows this row may bridge to (a two-pin part between them)
        nb = {}
        for j in (i - 1, i + 1):
            if 0 <= j < len(rows) and rows[j].net and not ctx.is_rail(rows[j].net) and len(rows[j].pins) == 1:
                nb[rows[j].net] = None
        sub = trunk(ctx, n, bridges=nb)
        for net2, hit in nb.items():
            if hit:
                j = next(k for k in (i - 1, i + 1) if 0 <= k < len(rows) and rows[k].net == net2)
                r.bridge_to = (j, hit[0], hit[1])
                sub.wires.append((net2, [(hit[0] - G, hit[1]), (hit[0], hit[1])]))
        r.shape = sub

    # ordering: r must lie beyond s when r's drawing reaches over s's line
    def yr(r: Row):
        x0, y0, x1, y1 = r.shape.extent()
        last = r.pins[-1][1][1] - r.y
        return r.y + min(y0, last), r.y + max(y1, 0.0)

    n = len(rows)

    def covers(a: Row, b: Row) -> bool:
        if a.shape.empty():
            return False
        if a.zone:
            return b.y < a.y - 0.01
        y0, y1 = yr(a)
        return any(y0 + 0.01 < p[1][1] < y1 - 0.01 for p in b.pins) or any(
            abs(p[1][1] - a.y) > 0.01 and y0 - 0.01 <= p[1][1] <= y1 + 0.01 and p not in a.pins for p in b.pins)

    for _ in range(n + 1):
        edges = defaultdict(set)      # s -> r : s nearer than r
        for a in range(n):
            for b in range(n):
                if a == b or rows[b].shape.empty() and not rows[b].pins:
                    continue
                if rows[a].bridge_to and rows[a].bridge_to[0] == b:
                    edges[a].add(b)
                    continue
                if rows[b].bridge_to and rows[b].bridge_to[0] == a:
                    continue
                if covers(rows[a], rows[b]):
                    edges[b].add(a)
        order, cyc = _topo(n, edges)
        if not cyc:
            break
        # move the tallest row stuck in a cycle into the zone below the part
        cand = [i for i in cyc if not rows[i].zone]
        if not cand:
            ctx.notes.append("rows on one side could not be ordered without crossings")
            order = list(range(n))
            break
        big = max(cand, key=lambda i: (yr(rows[i])[1] - yr(rows[i])[0], -i))
        rows[big].zone = True
    # x positions in order
    widths = {}
    for i in order:
        r = rows[i]
        x0, _, x1, _ = r.shape.extent()
        start = r.attach_min
        for s in range(n):
            if i in edges.get(s, ()):
                rs = rows[s]
                sx1 = widths.get(s, rs.attach_min)
                start = max(start, sx1 + GAP - (0.0 if r.zone else min(x0, 0.0)))
        r.x = start if r.zone else max(start, start - x0)
        if r.zone:
            widths[i] = r.x + max(x1, 0.0)
        else:
            widths[i] = r.x + x1
        if r.bridge_to:
            j, bx, by = r.bridge_to
            rows[j].joins.append(r.x + bx)
    # bridge partners must start beyond their join point
    for i in order:
        r = rows[i]
        if r.joins:
            need = max(r.joins) + G
            if r.x < need:
                x0 = r.shape.extent()[0]
                r.x = max(r.x, need - min(x0, 0.0))
    out = Drawing()
    # zone below everything on this side
    inband_lo = min([body_bottom] + [yr(r)[0] for r in rows if not r.zone and not r.shape.empty()])
    zone_top = inband_lo - GAP * 2
    for r in rows:
        if r.net is None:
            continue
        if len(r.pins) > 1:
            jx = r.pin_x + G
            for _, (px, py) in r.pins:
                out.wires.append((r.net, [(px, py), (jx, py)]))
            out.wires.append((r.net, [(jx, r.pins[0][1][1]), (jx, r.pins[-1][1][1])]))
        if r.shape.empty():
            continue
        if r.zone:
            _, _, _, sy1 = r.shape.extent()
            ay = zone_top - max(sy1, 0.0)
            pts = [(r.pin_x, r.y), (r.x, r.y), (r.x, ay)]
            out.wires.append((r.net, pts))
            out.merge(r.shape.moved(r.x, ay))
        else:
            xs = sorted(set([r.pin_x if len(r.pins) == 1 else r.pin_x + G, r.x] + r.joins))
            if xs[-1] - xs[0] > 0.01:
                out.wires.append((r.net, [(x, r.y) for x in xs]))
            out.merge(r.shape.moved(r.x, r.y))
            if r.bridge_to:
                j, bx, by = r.bridge_to
                ty = rows[j].y
                out.wires.append((rows[j].net, [(r.x + bx, r.y + by), (r.x + bx, ty)]))
    return out


def _topo(n: int, edges) -> tuple[list[int], list[int]]:
    indeg = [0] * n
    for s, rs in edges.items():
        for r in rs:
            indeg[r] += 1
    q = [i for i in range(n) if indeg[i] == 0]
    order = []
    while q:
        q.sort()
        i = q.pop(0)
        order.append(i)
        for r in sorted(edges.get(i, ())):
            indeg[r] -= 1
            if indeg[r] == 0:
                q.append(r)
    cyc = [i for i in range(n) if i not in order]
    return order + cyc, cyc


# ---------------------------------------------------------------------------
# a whole block


def layout_block(ctx: Ctx) -> Drawing:
    a = ctx.anchor
    geo: Geo = ctx.geo[a]
    d = Drawing()
    d.parts[a] = (0.0, 0.0, 0, False)
    d.boxes += _part_boxes(ctx, a, 0.0, 0.0, 0, False)
    ctx.placed.add(a)
    bx0, by0, bx1, by1 = geo.body()
    sides = defaultdict(list)
    for name, p in geo.pins.items():
        sides[(round(p.ox), round(p.oy))].append((name, (p.x, p.y)))
    right = side_rows(ctx, sides.get((1, 0), []))
    left = side_rows(ctx, [(nm, (-x, y)) for nm, (x, y) in sides.get((-1, 0), [])])
    # owned rails: the side that reaches them first draws the trunk (right side first:
    # outputs usually sit there)
    d.merge(build_side(ctx, right, by0, by1, by0))
    d.merge(build_side(ctx, left, by0, by1, by0).mirrored())
    for (fx, fy), plist in sides.items():
        if fx != 0:
            continue
        for name, (px, py) in plist:
            n = ctx.pin_net(a, name)
            if n is None:
                continue
            stub = (px + fx * G, py + fy * G)
            d.wires.append((n, [(px, py), stub]))
            if ctx.is_rail(n):
                put_supply(ctx, d, n, stub, (0, fy) if (n in ctx.ground) == (fy < 0) else (1, 0))
            else:
                d.labels.append((n, stub[0], stub[1], 1))
                d.boxes.append(label_box(n, stub[0], stub[1], 1))
                if n not in ctx.drawn and ctx.members_on(n):
                    ctx.floating.append(n)
    # leftovers: nets reached only through labels, and parts on rails only
    lo = d.extent()[1] - GAP * 2
    x = d.extent()[0]
    for n in list(dict.fromkeys(ctx.floating)):
        if n in ctx.drawn or not ctx.members_on(n):
            continue
        sub = Drawing()
        sub.labels.append((n, 0.0, 0.0, -1))
        sub.boxes.append(label_box(n, 0.0, 0.0, -1))
        sub.merge(trunk(ctx, n, end_label=False))
        sx0, sy0, sx1, sy1 = sub.extent()
        d.merge(sub.moved(x - sx0, lo - sy1))
        x += sx1 - sx0 + GAP * 2
    for ref in sorted(ctx.block - ctx.placed, key=_natural):
        pins = ctx.wired_pins(ref)
        sub = Drawing()
        if len(pins) == 2 and all(ctx.is_rail(ctx.pin_net(ref, p)) for p in pins):
            hi = next((p for p in pins if ctx.pin_net(ref, p) not in ctx.ground), pins[0])
            lo_p = next(p for p in pins if p != hi)
            pp = put_part(ctx, sub, ref, hi, (0.0, 0.0), (0, 1))
            put_supply(ctx, sub, ctx.pin_net(ref, hi), (0.0, G), (0, 1))
            sub.wires.append((ctx.pin_net(ref, hi), [(0.0, 0.0), (0.0, G)]))
            (lx, ly), _ = pp[lo_p]
            sub.wires.append((ctx.pin_net(ref, lo_p), [(lx, ly), (lx, ly - G)]))
            put_supply(ctx, sub, ctx.pin_net(ref, lo_p), (lx, ly - G), (0, -1))
        else:
            ctx.notes.append(f"{ref}: not reached from {a}; drawn with labels on each pin")
            pp = put_part(ctx, sub, ref, pins[0], (0.0, 0.0), (-1, 0)) if pins else {}
            for q, ((px, py), (fx, fy)) in pp.items():
                nq = ctx.pin_net(ref, q)
                if not nq:
                    continue
                stub = (px + fx * G, py + fy * G)
                sub.wires.append((nq, [(px, py), stub]))
                if ctx.is_rail(nq):
                    put_supply(ctx, sub, nq, stub, (fx, fy) if fx else (0, fy))
                else:
                    sub.labels.append((nq, stub[0], stub[1], 1 if fx >= 0 else -1))
                    sub.boxes.append(label_box(nq, stub[0], stub[1], 1 if fx >= 0 else -1))
        sx0, sy0, sx1, sy1 = sub.extent()
        d.merge(sub.moved(x - sx0, lo - sy1))
        x += sx1 - sx0 + GAP * 2
    _heal(ctx, d)
    return d


def _heal(ctx: Ctx, d: Drawing) -> None:
    """Last pass: a pin the rules left unwired gets a stub and a label (or power symbol), and a
    piece of a net joined to the rest only by name gets a label, so the drawing is always
    complete (a series chain ending on a second pin of the main part, a pull-up hanging off a
    net whose label sits elsewhere)."""
    ends = {(round(px, 2), round(py, 2)) for _, pts in d.wires for px, py in pts}
    for ref, (x, y, rot, mir) in list(d.parts.items()):
        for name, p in ctx.geo[ref].pins.items():
            n = ctx.pin_net(ref, name)
            px, py = transform(p.x, p.y, x, y, rot, mir)
            if n is None or (round(px, 2), round(py, 2)) in ends:
                continue
            fx, fy = transform(p.ox, p.oy, 0, 0, rot, mir)
            fx, fy = round(fx), round(fy)
            stub = (round(px + fx * G, 4), round(py + fy * G, 4))
            d.wires.append((n, [(px, py), stub]))
            if ctx.is_rail(n):
                put_supply(ctx, d, n, stub, (fx, fy) if fx else (0, fy))
            else:
                dirn = 1 if fx >= 0 else -1
                d.labels.append((n, stub[0], stub[1], dirn))
                d.boxes.append(label_box(n, stub[0], stub[1], dirn))
            ends.add((round(px, 2), round(py, 2)))
    for net, (lx, ly) in _unlinked_points(d):
        d.labels.append((net, lx, ly, 1))
        d.boxes.append(label_box(net, lx, ly, 1))


# ---------------------------------------------------------------------------
# checks


def check(d: Drawing, ctx_geo: dict, pad_net: dict, refs: set, supply_geo: dict) -> dict:
    """Connectivity of the drawing against the netlist, plus overlaps and stray contacts.
    Pins connect only at wire ends/vertices (EAGLE); same-named wires are one net."""
    ends: dict[tuple, set] = defaultdict(set)
    segs = []
    for net, pts in d.wires:
        for p in pts:
            ends[(round(p[0], 2), round(p[1], 2))].add(net)
        for p, q in zip(pts, pts[1:]):
            segs.append((net, p, q))
    wrong, missing, shorts, on_wire = [], [], [], []
    for ref, (x, y, rot, mir) in d.parts.items():
        geo = ctx_geo[ref]
        for name, p in geo.pins.items():
            px, py = transform(p.x, p.y, x, y, rot, mir)
            key = (round(px, 2), round(py, 2))
            nets = ends.get(key, set())
            want = {pad_net.get((ref, pad)) for pad in p.pads} - {None}
            if len(nets) > 1:
                shorts.append(f"{ref}.{name} touches {sorted(nets)}")
            if want and not nets:
                missing.append(f"{ref}.{name} ({sorted(want)[0]}) not connected")
            elif want and nets and not (nets & want):
                wrong.append(f"{ref}.{name} on {sorted(nets)} but should be {sorted(want)}")
            elif not want and nets:
                wrong.append(f"{ref}.{name} is unconnected in the netlist but touches {sorted(nets)}")
            for net, a, b in segs:
                if net in want:
                    continue
                if _on_segment(px, py, a, b) and key not in {(round(a[0], 2), round(a[1], 2)), (round(b[0], 2), round(b[1], 2))}:
                    on_wire.append(f"{ref}.{name} sits on a {net} wire")
    # wire ends touching other nets' wires (EAGLE would join them)
    touch = []
    for net, a, b in segs:
        for (k, nets) in ends.items():
            if net in nets:
                continue
            if _on_segment(k[0], k[1], a, b):
                touch.append(f"{sorted(nets)} end at {k} lands on {net}")
    cross = 0
    for i, (n1, a, b) in enumerate(segs):
        for n2, c, e in segs[i + 1:]:
            if n1 != n2 and _cross(a, b, c, e):
                cross += 1
    overl = []
    body = [bx for bx in d.boxes if ":" not in bx[4]]
    for i, A in enumerate(body):
        for B in body[i + 1:]:
            if A[0] < B[2] - 0.05 and B[0] < A[2] - 0.05 and A[1] < B[3] - 0.05 and B[1] < A[3] - 0.05:
                overl.append(f"{A[4]} overlaps {B[4]}")
    unlinked = _unlinked(d)
    return {"wrong": wrong, "missing": missing, "shorts": shorts, "pin_on_wire": on_wire,
            "wire_touch": touch, "unlinked": unlinked, "crossings": cross, "overlaps": overl}


def _unlinked(d: Drawing) -> list[str]:
    return [m for _, _, m in _unlinked_pieces(d)]


def _unlinked_points(d: Drawing) -> list:
    return [(n, at) for n, at, _ in _unlinked_pieces(d)]


def _unlinked_pieces(d: Drawing) -> list:
    """Pieces of a net that are only joined by name: a net drawn in several pieces needs a
    label or power symbol on every piece (Fusion's ERC code 115)."""
    parent: dict = {}

    def find(a):
        while parent.setdefault(a, a) != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    pts_of = defaultdict(list)
    for net, pts in d.wires:
        keys = [(net, round(p[0], 2), round(p[1], 2)) for p in pts]
        for k in keys:
            find(k)
        for a, b in zip(keys, keys[1:]):
            parent[find(a)] = find(b)
        pts_of[net] += keys
    # an end on another wire's interior joins it too
    for net, pts in d.wires:
        for a, b in zip(pts, pts[1:]):
            for k in pts_of[net]:
                if _on_segment(k[1], k[2], a, b, tol=0.005):
                    parent[find(k)] = find((net, round(a[0], 2), round(a[1], 2)))
    marked = set()
    for net, kind, x, y, _ in d.supplies:
        k = (net, round(x, 2), round(y, 2))
        if k in parent:
            marked.add(find(k))
    for net, x, y, _ in d.labels:
        k = (net, round(x, 2), round(y, 2))
        if k in parent:
            marked.add(find(k))
    out = []
    for net, keys in pts_of.items():
        comps = {find(k) for k in keys}
        if len(comps) > 1:
            for c in comps - marked:
                # where a label would go: the piece's free end furthest right (a wire end that
                # no other point of the piece shares), else any point of it
                piece = [k for k in keys if find(k) == c]
                count = defaultdict(int)
                for k in piece:
                    count[k] += 1
                free = [k for k in piece if count[k] == 1] or piece
                at = max(free, key=lambda k: (k[1], -k[2]))
                out.append((net, (at[1], at[2]), f"a piece of {net} at {c[1:]} has no label or power symbol"))
    return out


def _on_segment(px, py, a, b, tol=0.02) -> bool:
    (x1, y1), (x2, y2) = a, b
    if min(x1, x2) - tol <= px <= max(x1, x2) + tol and min(y1, y2) - tol <= py <= max(y1, y2) + tol:
        return abs((x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)) <= tol * max(1.0, math.hypot(x2 - x1, y2 - y1))
    return False


def _cross(a, b, c, e) -> bool:
    """Proper crossing of an axis-aligned pair (one horizontal, one vertical)."""
    if a[1] == b[1] and c[0] == e[0]:
        h, v = (a, b), (c, e)
    elif a[0] == b[0] and c[1] == e[1]:
        h, v = (c, e), (a, b)
    else:
        return False
    x = v[0][0]
    y = h[0][1]
    return (min(h[0][0], h[1][0]) + 0.01 < x < max(h[0][0], h[1][0]) - 0.01 and
            min(v[0][1], v[1][1]) + 0.01 < y < max(v[0][1], v[1][1]) - 0.01)


# ---------------------------------------------------------------------------
# sheets


@dataclass
class SheetArea:
    x0: float = 12.0
    y0: float = 12.0
    x1: float = 419.0
    y1: float = 263.0
    keepouts: list = field(default_factory=lambda: [(320.0, 0.0, 431.8, 40.0)])   # title block (B frame)


def pack(blocks: list[tuple[str, Drawing]], area: SheetArea | None = None, gap: float = 10.16) -> list[tuple[int, float, float]]:
    """Shelf-pack blocks onto sheets in order: rows top to bottom, left to right.
    Returns (sheet, dx, dy) per block: move the block's drawing by (dx, dy)."""
    a = area or SheetArea()
    out = []
    sheet, cx, top, row_h = 1, a.x0, a.y1, 0.0

    def right_limit(y_lo, y_hi):
        lim = a.x1
        for kx0, ky0, kx1, ky1 in a.keepouts:
            if y_lo < ky1 + gap / 2 and y_hi > ky0:
                lim = min(lim, kx0 - gap / 2)
        return lim

    for name, d in blocks:
        x0, y0, x1, y1 = d.extent()
        w, h = x1 - x0, y1 - y0
        for _ in range(3):
            if cx + w <= right_limit(top - h, top) or cx == a.x0:
                break
            cx, top, row_h = a.x0, top - row_h - gap, 0.0
        if top - h < a.y0 and top != a.y1:
            sheet, cx, top, row_h = sheet + 1, a.x0, a.y1, 0.0
        if cx + w > right_limit(top - h, top) and cx == a.x0 and top - h < a.y0 + 40:
            pass  # wider than the free space next to the title block: let it overlap rather than fail
        dx = round((cx - x0) / 2.54) * 2.54
        dy = round((top - y1) / 2.54) * 2.54
        out.append((sheet, dx, dy))
        cx += w + gap
        row_h = max(row_h, h)
    return out
