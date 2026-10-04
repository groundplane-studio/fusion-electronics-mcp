"""Cluster placement: put each main part's passives around it by rule, not by optimiser.

Ported from Groundplane's KiCad placement solver (v2, learned from
hand-placed blocks on a test board) to Fusion's board export,
plus chains. Main parts (ICs, connectors) stay where they are; the members of each block
(sch_plan: a main part and the passives that serve it) are placed in the main part's own
frame and moved as a cluster.

Patterns (the user's model), for a two-pad member and the main-part pin it serves:
- P1 pin -> part -> rail: along the pin's escape line, pin end in, rail end out;
- P2 pin -> part -> elsewhere (series): along the escape line;
- P3 a tee to a rail off a net that goes on elsewhere: also along the escape line (v2 rule);
- P4 a part between two pins: along the package edge spanning both (bridge);
- decap (rail to rail): served pin is the anchor's pin on the non-ground rail; stands in the
  column across the escape, supply end on the pin's row, placed first.
Chains: a part with no pin of the main part on its nets (LED after its resistor, a FET's
load) continues outward from the part it shares a net with, facing it. A three-pin member
(a FET driven by a connector pin) is entered at the pin on the main part's net.

Frames: Fusion/EAGLE, y up, rotations counter-clockwise in 90-degree steps, unmirrored
(top side). Boxes are each part's pads plus its silk/documentation outline.
"""

from __future__ import annotations

import copy
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .design import transform
from .eagle import parse_rot
from .stitch import _f

COL_GAP = 1.5          # pin pad edge to the first member's pad edge, along the escape
ROW_GAP = 0.45         # between neighbouring members across a column
STEP = 0.25
MAX_OUT = 8.0
MARGIN = 0.2           # body-to-body clearance


@dataclass
class Part:
    name: str
    x: float
    y: float
    rot: int
    mirror: bool
    pads: dict                      # pad -> (x, y, w, h) in the package frame
    box: tuple                      # package-frame body box (pads + silk/doc outline)

    def to_board(self, px, py, rot=None, x=None, y=None):
        return transform(px, py, self.x if x is None else x, self.y if y is None else y,
                         self.rot if rot is None else rot, self.mirror)

    def board_box(self, x=None, y=None, rot=None):
        pts = [self.to_board(bx, by, rot, x, y) for bx in (self.box[0], self.box[2]) for by in (self.box[1], self.box[3])]
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def read_parts(root: ET.Element) -> tuple[dict, dict]:
    """({ref: Part}, {(ref, pad): net}) from a board export."""
    board = root.find("./drawing/board")
    pkgs = {(lib.get("name"), pk.get("name")): pk for lib in board.iterfind("./libraries/library")
            for pk in lib.iterfind("./packages/package")}
    parts = {}
    for el in board.iterfind("./elements/element"):
        pk = pkgs.get((el.get("library"), el.get("package")))
        if pk is None:
            continue
        ang, mir = parse_rot(el.get("rot"))
        pads, xs, ys = {}, [], []
        for p in list(pk.iterfind("smd")) + list(pk.iterfind("pad")):
            w = _f(p, "dx") or _f(p, "diameter") or 1.0
            h = _f(p, "dy") or _f(p, "diameter") or 1.0
            prot, _ = parse_rot(p.get("rot"))
            if int(prot) % 180 == 90:
                w, h = h, w
            px, py = _f(p, "x"), _f(p, "y")
            pads[p.get("name")] = (px, py, w, h)
            xs += [px - w / 2, px + w / 2]
            ys += [py - h / 2, py + h / 2]
        for w in pk.iterfind("wire"):
            if w.get("layer") in ("21", "51"):
                xs += [_f(w, "x1"), _f(w, "x2")]
                ys += [_f(w, "y1"), _f(w, "y2")]
        if not xs:
            continue
        parts[el.get("name")] = Part(el.get("name"), _f(el, "x"), _f(el, "y"), int(round(ang)) % 360, mir, pads,
                                     (min(xs), min(ys), max(xs), max(ys)))
    pad_net = {}
    for s in board.iterfind("./signals/signal"):
        for c in s.iterfind("contactref"):
            pad_net[(c.get("element"), c.get("pad"))] = s.get("name")
    return parts, pad_net


def _overlap(a, b, m=MARGIN):
    return a[0] < b[2] + m and b[0] < a[2] + m and a[1] < b[3] + m and b[1] < a[3] + m


ROTS = (0, 90, 180, 270)


def _dir(rot, vx, vy):
    x, y = transform(vx, vy, 0, 0, rot, False)
    return (round(x, 6), round(y, 6))


def orient(part: Part, near: str, far: str | None, toward: tuple[float, float]) -> int:
    """Rotation that points the part from its far pad to its near pad along `toward`."""
    if far is None:
        return 0
    n, f = part.pads[near], part.pads[far]
    v = (n[0] - f[0], n[1] - f[1])
    best, score = 0, -9
    for r in ROTS:
        d = _dir(r, *v)
        L = math.hypot(*d) or 1
        s = (d[0] * toward[0] + d[1] * toward[1]) / L
        if s > score + 1e-9:
            best, score = r, s
    return best


def place_pad_at(part: Part, pad: str, rot: int, target: tuple[float, float]) -> tuple[float, float]:
    """Origin that puts `pad` of `part` (rotated `rot`) on `target`."""
    px, py = transform(part.pads[pad][0], part.pads[pad][1], 0, 0, rot, part.mirror)
    return round(target[0] - px, 4), round(target[1] - py, 4)


@dataclass
class Plan:
    moves: dict = field(default_factory=dict)        # ref -> (x, y, rot)
    roles: dict = field(default_factory=dict)        # ref -> "P1 on J9.5" ...
    misses: list = field(default_factory=list)


def solve(root: ET.Element, blocks: list[dict], rails: set, fixed: set | None = None,
          keep: set | None = None) -> Plan:
    """Place every block's members around its anchor. blocks: sch_plan blocks (anchor + members).
    fixed: parts that never move (default: every anchor). keep: members to leave where they are."""
    parts, pad_net = read_parts(root)
    fixed = set(fixed or ()) | {b["anchor"] for b in blocks}
    keep = set(keep or ())
    movable = {m for b in blocks for m in b["members"] if m in parts and m not in fixed and m not in keep}
    plan = Plan()
    taken = [parts[r].board_box() for r in parts if r not in movable]
    taken += _keepouts(root)
    outline = _outline(root)
    nets_of = lambda r: {p: pad_net.get((r, p)) for p in parts[r].pads}

    def fits(box):
        if outline and (box[0] < outline[0] + 0.3 or box[1] < outline[1] + 0.3 or
                        box[2] > outline[2] - 0.3 or box[3] > outline[3] - 0.3):
            return False
        return not any(_overlap(box, t) for t in taken)

    for b in blocks:
        a = parts.get(b["anchor"])
        if a is None:
            continue
        members = [m for m in b["members"] if m in movable]
        anchor_pins = {}                                   # net -> [pad] of the anchor
        for p in a.pads:
            n = pad_net.get((a.name, p))
            if n:
                anchor_pins.setdefault(n, []).append(p)
        acx, acy = (a.box[0] + a.box[2]) / 2, (a.box[1] + a.box[3]) / 2

        # a connector at a board edge: every pin escapes away from that edge, into the board
        edge_dir = None
        if outline and re.match(r"^(J|P|CN|X)\d", a.name):
            bb = a.board_box()
            gaps = {(0, -1): outline[3] - bb[3], (0, 1): bb[1] - outline[1], (-1, 0): outline[2] - bb[2], (1, 0): bb[0] - outline[0]}
            d0 = min(gaps, key=gaps.get)
            if gaps[d0] < 6.0:
                edge_dir = d0

        def escape(pad):
            """Board-frame unit escape direction of an anchor pad: the package side it sits on, or
            for a connector at the board edge, away from that edge (into the board)."""
            if edge_dir:
                return edge_dir
            px, py, w, h = a.pads[pad]
            hw, hh = max((a.box[2] - a.box[0]) / 2, 1e-6), max((a.box[3] - a.box[1]) / 2, 1e-6)
            dx, dy = (px - acx) / hw, (py - acy) / hh
            loc = ((1 if dx > 0 else -1), 0) if abs(dx) >= abs(dy) else (0, (1 if dy > 0 else -1))
            d = _dir(a.rot, *loc)
            d = (round(d[0]), round(d[1]))
            if outline:
                bx, by = a.to_board(px, py)
                room = {(1, 0): outline[2] - bx, (-1, 0): bx - outline[0], (0, 1): outline[3] - by, (0, -1): by - outline[1]}[d]
                if room < 4.0:
                    d = (-d[0], -d[1])
            return d

        def pin_edge(pad, d):
            """(where the column starts, pad centre): the pad edge along the escape, or for a
            connector escaping into the board, its body edge (its housing is in the way)."""
            px, py, w, h = a.pads[pad]
            cx, cy = a.to_board(px, py)
            wd, hd = (w, h) if a.rot % 180 == 0 else (h, w)
            ex, ey = cx + d[0] * wd / 2, cy + d[1] * hd / 2
            if edge_dir:
                bb = a.board_box()
                if d[0]:
                    ex = bb[2] if d[0] > 0 else bb[0]
                else:
                    ey = bb[3] if d[1] > 0 else bb[1]
            return (ex, ey), (cx, cy)

        # classify
        decaps, bridges, cols, chains = [], [], {}, []
        for m in members:
            nets = nets_of(m)
            wired = [p for p, n in nets.items() if n]
            if len(parts[m].pads) == 2 and len(wired) == 2:
                (p1, n1), (p2, n2) = list(nets.items())
                hit = [(p, n) for p, n in ((p1, n1), (p2, n2)) if n in anchor_pins and n not in rails]
                if not hit:
                    hit_r = [(p, n) for p, n in ((p1, n1), (p2, n2)) if n in anchor_pins and not re.match(r"(?i)^(a|d|p)?gnd", n)]
                    if n1 in rails and n2 in rails and hit_r:
                        decaps.append((m, hit_r[0][0], anchor_pins[hit_r[0][1]][0]))
                    else:
                        chains.append(m)
                    continue
                near, n_a = hit[0]
                far = p2 if near == p1 else p1
                n_f = nets[far]
                pin = anchor_pins[n_a][0]
                d = escape(pin)
                if n_f in anchor_pins and n_f not in rails and anchor_pins[n_f][0] != pin:
                    bridges.append((m, near, far, pin, anchor_pins[n_f][0]))
                    continue
                pat = "P1" if n_f in rails else "P2"
                cols.setdefault(d, []).append((m, near, far, pin, pat))
            else:
                hit = [(p, n) for p, n in nets.items() if n in anchor_pins and n not in rails]
                if hit and len(parts[m].pads) >= 3:
                    p, n = hit[0]
                    cols.setdefault(escape(anchor_pins[n][0]), []).append((m, p, None, anchor_pins[n][0], "IN"))
                else:
                    chains.append(m)

        def put(m, x, y, rot, role):
            box = parts[m].board_box(x, y, rot)
            if not fits(box):
                return False
            taken.append(box)
            plan.moves[m] = (x, y, rot)
            plan.roles[m] = role
            parts[m].x, parts[m].y, parts[m].rot = x, y, rot
            return True

        # decaps first: across the column, supply pad on the pin's row
        for m, near, pin in decaps:
            d = escape(pin)
            far = next(p for p in parts[m].pads if p != near)
            perp = (-d[1], d[0])
            edge, c = pin_edge(pin, d)
            ok = False
            for k in [i * STEP for i in range(int(MAX_OUT / STEP))]:
                for side in (1, -1):
                    rot = orient(parts[m], near, far, (-perp[0] * side, -perp[1] * side))
                    t = (edge[0] + d[0] * (COL_GAP + k), edge[1] + d[1] * (COL_GAP + k))
                    x, y = place_pad_at(parts[m], near, rot, t)
                    if put(m, x, y, rot, f"decap on {a.name}.{pin}"):
                        ok = True
                        break
                if ok:
                    break
            if not ok:
                plan.misses.append(f"{m}: no slot as decap on {a.name}.{pin}")

        # bridges: along the edge, spanning both pins
        for m, near, far, pin_a, pin_b in bridges:
            d = escape(pin_a)
            ea, ca = pin_edge(pin_a, d)
            _, cb = pin_edge(pin_b, d)
            toward = (ca[0] - cb[0], ca[1] - cb[1])
            L = math.hypot(*toward) or 1
            rot = orient(parts[m], near, far, (toward[0] / L, toward[1] / L))
            ok = False
            for k in [i * STEP for i in range(int(MAX_OUT / STEP))]:
                mid = ((ca[0] + cb[0]) / 2 + d[0] * (COL_GAP + k + (ea[0] - ca[0]) * d[0]),
                       (ca[1] + cb[1]) / 2 + d[1] * (COL_GAP + k + (ea[1] - ca[1]) * d[1]))
                pc = [(parts[m].pads[p][0], parts[m].pads[p][1]) for p in (near, far)]
                cx, cy = (pc[0][0] + pc[1][0]) / 2, (pc[0][1] + pc[1][1]) / 2
                ox, oy = transform(cx, cy, 0, 0, rot, False)
                if put(m, round(mid[0] - ox, 4), round(mid[1] - oy, 4), rot, f"P4 {a.name}.{pin_a}-{pin_b}"):
                    ok = True
                    break
            if not ok:
                plan.misses.append(f"{m}: no slot as a bridge on {a.name}")

        # columns: along each pin's escape, rows spread across the side
        for d, items in cols.items():
            perp = (-d[1], d[0])
            items.sort(key=lambda it: pin_edge(it[3], d)[1][0] * perp[0] + pin_edge(it[3], d)[1][1] * perp[1])
            last_lat = None
            for m, near, far, pin, pat in items:
                edge, c = pin_edge(pin, d)
                lat0 = c[0] * perp[0] + c[1] * perp[1]
                rot = orient(parts[m], near, far, (-d[0], -d[1])) if far else _face(parts[m], near, (-d[0], -d[1]))
                ok = False
                # nearest first: one step further out costs less than one step sideways
                cands = sorted(((k, off) for k in [i * STEP for i in range(int(MAX_OUT / STEP))]
                                for off in [0.0] + [v for i in range(1, 40) for v in (i * STEP, -i * STEP)]),
                               key=lambda c: c[0] + 2 * abs(c[1]))
                for k, off in cands:
                    t = (edge[0] + d[0] * (COL_GAP + k) + perp[0] * off, edge[1] + d[1] * (COL_GAP + k) + perp[1] * off)
                    x, y = place_pad_at(parts[m], near, rot, t)
                    if put(m, x, y, rot, f"{pat} on {a.name}.{pin}"):
                        ok = True
                        break
                if not ok:
                    plan.misses.append(f"{m}: no slot ({pat} on {a.name}.{pin})")

        # chains: continue outward from the placed part they share a net with
        pending = list(chains)
        for _ in range(len(pending) + 2):
            if not pending:
                break
            for m in list(pending):
                nets = nets_of(m)
                link = None
                for r in list(plan.moves) + [a.name]:
                    if r == m:
                        continue
                    for p, n in nets.items():
                        if n and n not in rails:
                            q = next((qq for qq in parts[r].pads if pad_net.get((r, qq)) == n), None)
                            if q:
                                link = (r, q, p)
                                break
                    if link:
                        break
                if not link:
                    continue
                r, q, p = link
                pr = parts[r]
                qx, qy = pr.to_board(pr.pads[q][0], pr.pads[q][1])
                # outward: from the linked part's centre through its linked pad
                bx = (pr.board_box()[0] + pr.board_box()[2]) / 2
                by = (pr.board_box()[1] + pr.board_box()[3]) / 2
                v = (qx - bx, qy - by)
                if abs(v[0]) >= abs(v[1]):
                    out = (1 if v[0] > 0 else -1, 0)
                else:
                    out = (0, 1 if v[1] > 0 else -1)
                others = [pp for pp in parts[m].pads if pp != p]
                far = others[0] if len(others) == 1 else None
                rot = orient(parts[m], p, far, (-out[0], -out[1])) if far else _face(parts[m], p, (-out[0], -out[1]))
                ok = False
                perp = (-out[1], out[0])
                for k in [0.6 + i * STEP for i in range(int(MAX_OUT / STEP))]:
                    for off in (0.0, STEP, -STEP, 2 * STEP, -2 * STEP, 4 * STEP, -4 * STEP):
                        t = (qx + out[0] * k + perp[0] * off, qy + out[1] * k + perp[1] * off)
                        x, y = place_pad_at(parts[m], p, rot, t)
                        if put(m, x, y, rot, f"chain after {r}.{q}"):
                            ok = True
                            break
                    if ok:
                        break
                if not ok:
                    plan.misses.append(f"{m}: no slot after {r}")
                pending.remove(m)
        for m in pending:
            plan.misses.append(f"{m}: not linked to anything placed (left where it is)")
    return plan


def _face(part: Part, pad: str, toward):
    """Rotation that puts `pad` on the side of the part facing `toward` (multi-pin parts)."""
    cx, cy = (part.box[0] + part.box[2]) / 2, (part.box[1] + part.box[3]) / 2
    px, py = part.pads[pad][0] - cx, part.pads[pad][1] - cy
    best, score = 0, -9
    for r in ROTS:
        d = _dir(r, px, py)
        L = math.hypot(*d) or 1
        s = (d[0] * toward[0] + d[1] * toward[1]) / L
        if s > score + 1e-9:
            best, score = r, s
    return best


def _keepouts(root):
    out = []
    b = root.find("./drawing/board")
    for c in b.iterfind("./plain/circle"):
        if c.get("layer") in ("41", "42", "43"):
            r = _f(c, "radius")
            out.append((_f(c, "x") - r, _f(c, "y") - r, _f(c, "x") + r, _f(c, "y") + r))
    for h in b.iterfind("./plain/hole"):
        r = _f(h, "drill") / 2 + 0.5
        out.append((_f(h, "x") - r, _f(h, "y") - r, _f(h, "x") + r, _f(h, "y") + r))
    return out


def _outline(root):
    xs, ys = [], []
    for w in root.iterfind("./drawing/board/plain/wire"):
        if w.get("layer") == "20":
            xs += [_f(w, "x1"), _f(w, "x2")]
            ys += [_f(w, "y1"), _f(w, "y2")]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def applied(root: ET.Element, plan: Plan) -> ET.Element:
    """A copy of the board with the plan's moves applied (for previews)."""
    r2 = copy.deepcopy(root)
    for el in r2.iterfind("./drawing/board/elements/element"):
        mv = plan.moves.get(el.get("name"))
        if mv:
            el.set("x", str(mv[0]))
            el.set("y", str(mv[1]))
            mir = (el.get("rot") or "").startswith("M")
            el.set("rot", ("M" if mir else "") + f"R{mv[2]}")
    # traces of moved parts no longer line up: drop the copper of nets touching them
    moved = set(plan.moves)
    for s in r2.iterfind("./drawing/board/signals/signal"):
        if any(c.get("element") in moved for c in s.iterfind("contactref")):
            for w in list(s.iterfind("wire")):
                if w.get("layer") != "19":
                    s.remove(w)
            for v in list(s.iterfind("via")):
                s.remove(v)
    return r2
