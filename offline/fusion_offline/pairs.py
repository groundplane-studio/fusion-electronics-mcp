"""Differential pair routing geometry (offline, from the board XML).

The agent picks a centreline for a pair (waypoints, 45-degree bends); this
module turns it into two coupled traces and checks them before anything is
written to Fusion:

- trunk: the centreline offset by +-(width + gap)/2 with mitred corners, so
  the edge-to-edge gap stays `gap` through every bend;
- fan-in: each trace joins its pad with at most one 45-degree or orthogonal
  dogleg that arrives along the trunk direction. Which offset side is P is
  decided at the start pads; if the end pads come out the other way round
  (`crossed`), one trace must change layer to get past the other: pass a
  tail for it with a via (see plan_pair);
- tuning: the shorter trace gets 45-degree bumps on its longest trunk run,
  bulging away from its partner, until the skew is within tolerance;
- checks: every planned segment and via against other nets' copper, holes
  and keepouts (layer-aware), and P against N on the same layer.

Paths are lists of steps: ("pt", x, y) continues the trace, ("via", x, y)
ends the trace on this layer at a via and continues on the other layer.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET

from .stitch import Obstacle, board_obstacles, pad_centres, _mm

OCT = [(math.cos(math.radians(a)), math.sin(math.radians(a))) for a in range(0, 360, 45)]


def _unit(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(dx, dy)
    return (dx / L, dy / L) if L else (0.0, 0.0)


def offset_path(pts, d: float) -> list[tuple[float, float]]:
    """Offset a polyline by d (positive = left of travel) with mitred joints."""
    out = []
    n = len(pts)
    for i in range(n):
        if i in (0, n - 1):
            ux, uy = _unit(pts[0], pts[1]) if i == 0 else _unit(pts[-2], pts[-1])
            out.append((pts[i][0] - uy * d, pts[i][1] + ux * d))
            continue
        u1, u2 = _unit(pts[i - 1], pts[i]), _unit(pts[i], pts[i + 1])
        n1, n2 = (-u1[1], u1[0]), (-u2[1], u2[0])
        bx, by = n1[0] + n2[0], n1[1] + n2[1]
        bl = math.hypot(bx, by)
        if bl < 1e-9:
            raise ValueError(f"the centreline doubles back at waypoint {i}")
        bx, by = bx / bl, by / bl
        m = d / (bx * n1[0] + by * n1[1])
        out.append((pts[i][0] + bx * m, pts[i][1] + by * m))
    return out


def _curve(q) -> float:
    return q[2] if len(q) > 2 else 0.0


def _seg_len(a, b) -> float:
    chord = math.hypot(b[0] - a[0], b[1] - a[1])
    c = math.radians(_curve(b))
    return chord if abs(c) < 1e-9 else abs(c) * chord / (2 * abs(math.sin(c / 2)))


def length(pl) -> float:
    """Polyline length; a point (x, y, curve) ends an arc of `curve` degrees."""
    return sum(_seg_len(a, b) for a, b in zip(pl, pl[1:]))


def _arc_points(a, b, n: int):
    """n + 1 points along the segment a -> b (an arc if b carries a curve)."""
    c = _curve(b)
    if abs(c) < 1e-9:
        return [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n + 1)]
    from .si import arc_centre
    cx, cy, r = arc_centre(a[0], a[1], b[0], b[1], c)
    a0 = math.atan2(a[1] - cy, a[0] - cx)
    sw = math.radians(c)
    return [(cx + r * math.cos(a0 + sw * k / n), cy + r * math.sin(a0 + sw * k / n)) for k in range(n + 1)]


def flatten(pl, step: float = 0.05):
    """Dense (x, y) polyline following arcs, for checks and drawing."""
    out = [tuple(pl[0][:2])]
    for a, b in zip(pl, pl[1:]):
        out += _arc_points(a, b, max(1, int(_seg_len(a, b) / step)))[1:]
    return out


def _dedupe(pl, tol=1e-4):
    out = [pl[0]]
    for q in pl[1:]:
        if math.hypot(q[0] - out[-1][0], q[1] - out[-1][1]) > tol or _curve(q):
            out.append(q)
    return out


def _dense(pl, step=0.25):
    return flatten(pl, step)


def fan_in(pad, end, d) -> list[tuple[float, float]]:
    """Points from `pad` to trunk point `end`, arriving along direction d
    (the trunk's direction at that end, pointing into the trunk). One
    octilinear leg from the pad, then a straight run along d; falls back to
    a straight line when no such dogleg exists."""
    best = None
    for e in OCT:
        # pad + a*e = end - t*d  ->  a*e + t*d = end - pad
        det = e[0] * d[1] - e[1] * d[0]
        if abs(det) < 1e-9:
            continue
        rx, ry = end[0] - pad[0], end[1] - pad[1]
        a = (rx * d[1] - ry * d[0]) / det
        t = (e[0] * ry - e[1] * rx) / det
        if a >= -1e-6 and t >= -1e-6 and (best is None or a + t < best[0]):
            best = (a + t, (pad[0] + a * e[0], pad[1] + a * e[1]))
    if best is None:
        return [tuple(pad), tuple(end)]
    return _dedupe([tuple(pad), best[1], tuple(end)])


def add_bumps(pl, extra: float, partner, radius: float = 0.25, max_h: float = 0.6, top: float = 0.1,
              space: float = 0.1, min_seg: float = 2.0):
    """Rounded trombone bumps (the KiCad tuner's shape: every corner a
    90-degree arc of `radius`) on the longest straight run of `pl`, adding
    about `extra` mm and bulging away from the partner polyline. A bump of
    height h adds 2h + (2 pi - 8) r; the last bump is lowered (and, below
    h = 2r, its radius shrunk) to land on the exact length. Returns
    (polyline with (x, y, curve) arc points, added)."""
    if extra <= 1e-4:
        return list(pl), 0.0
    r = radius
    per = 2 * max_h + (2 * math.pi - 8) * r
    pitch = 4 * r + top + space
    new = list(pl)
    straight = [k for k in range(len(new) - 1) if not _curve(new[k + 1])]
    if not straight:
        return new, 0.0
    i = max(straight, key=lambda k: _seg_len(new[k], new[k + 1]))
    a, b = new[i], new[i + 1]
    L = _seg_len(a, b)
    room = int((L - space) // pitch)
    if L < min_seg or room <= 0:
        return new, 0.0
    ux, uy = _unit(a, b)
    nx, ny = -uy, ux
    mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    away = min(partner, key=lambda q: math.dist(q[:2], (mx, my)))
    if (away[0] - mx) * nx + (away[1] - my) * ny > 0:
        nx, ny = -nx, -ny
    turn = 90.0 if (nx, ny) == (-uy, ux) else -90.0      # first corner turns toward n
    k = min(room, math.ceil(extra / per - 1e-9))
    each = extra / k
    if each > per:
        each = per                                       # run too short: fill it, the caller tunes the rest elsewhere
    # identical bumps: lower them to share the length; below h = 2r shrink the radius
    rr = r
    h = (each - (2 * math.pi - 8) * r) / 2
    if h < 2 * r + 0.02:                                 # no sliver of a straight leg between the arcs
        rr = each / (2 * math.pi - 4)
        h = 2 * rr
    s0 = (L - k * pitch + space) / 2
    pts, added = [], 0.0
    for j in range(k):
        w = 4 * rr + top
        t0 = s0 + j * pitch + (4 * r - 4 * rr) / 2
        P = lambda along, out: (a[0] + ux * along + nx * out, a[1] + uy * along + ny * out)
        q0 = P(t0, 0)
        pts += [q0, (*P(t0 + rr, rr), turn)]
        if h - 2 * rr > 1e-6:
            pts.append(P(t0 + rr, h - rr))
        pts += [(*P(t0 + 2 * rr, h), -turn), P(t0 + 2 * rr + top, h), (*P(t0 + 3 * rr + top, h - rr), -turn)]
        if h - 2 * rr > 1e-6:
            pts.append(P(t0 + 3 * rr + top, rr))
        pts.append((*P(t0 + w, 0), turn))
        added += 2 * h + (2 * math.pi - 8) * rr
    new = new[:i + 1] + pts + new[i + 1:]
    return [tuple(round(v, 4) for v in q) for q in new], added


def _snap_last_leg(pts, tol_deg: float = 2.0):
    """Make the segment into the trunk exactly octilinear by sliding the
    point before it along its own axis. Fusion's 45-degree wire bend turns a
    leg that is a hair off 45 degrees into a micro-jog (a hard corner)."""
    if len(pts) < 3:
        return pts
    a, q, t = pts[-3], pts[-2], pts[-1]
    dx, dy = t[0] - q[0], t[1] - q[1]
    if abs(dx) < 1e-9 or abs(dy) < 1e-9:
        return pts
    if abs(math.degrees(math.atan2(abs(dy), abs(dx))) - 45) > tol_deg:
        return pts
    if abs(q[0] - a[0]) < 1e-6:            # q reached vertically: slide in y
        q = (q[0], t[1] - math.copysign(abs(dx), dy))
    elif abs(q[1] - a[1]) < 1e-6:          # reached horizontally: slide in x
        q = (t[0] - math.copysign(abs(dy), dx), q[1])
    else:
        return pts
    return pts[:-2] + [q, t]


def clean_steps(steps, snap: float = 0.003, tiny: float = 0.01):
    """Make every straight segment exactly octilinear when it is within
    `snap` mm of it (moving the rest of the path by the correction), and
    merge sub-`tiny` slivers into the next leg. Both pad ends stay exactly
    on the pad origin (Fusion leaves an air wire otherwise), so a residual
    offset of a few microns can remain in the last leg, inside the pad.
    Fusion's 45-degree wire bend otherwise splits a segment that is a hair
    off into a micro-jog."""
    merged = [steps[0]]
    for i, st in enumerate(steps[1:], 1):
        last = i == len(steps) - 1
        if (not last and st[0] == "pt" and merged[-1][0] == "pt" and not (len(st) > 3 and st[3])
                and not (len(steps[i + 1]) > 3 and steps[i + 1][3]) and math.dist(merged[-1][1:3], st[1:3]) < tiny):
            continue                                       # drop a sliver point; the next leg absorbs it
        merged.append(st)
    out = [list(st) for st in merged]
    for i in range(1, len(out)):
        a, b = out[i - 1], out[i]
        if len(b) > 3 and b[3]:
            continue                                       # arcs keep their geometry
        dx, dy = b[1] - a[1], b[2] - a[2]
        fix = None
        if 1e-6 < abs(dx) < snap and abs(dy) > snap:
            fix = (-dx, 0.0)
        elif 1e-6 < abs(dy) < snap and abs(dx) > snap:
            fix = (0.0, -dy)
        elif abs(dx) > snap and 1e-6 < abs(abs(dy) - abs(dx)) < snap:
            fix = (0.0, math.copysign(abs(dx), dy) - dy)
        if fix and i < len(out) - 1:
            for st in out[i:-1]:                           # the end pad stays exact: Fusion only
                st[1] += fix[0]                            # connects a wire end at the pad origin
                st[2] += fix[1]
    # absorb the residual before the end pad: slide the last corner along its own straight run
    if len(out) >= 3 and all(st[0] == "pt" and not (len(st) > 3 and st[3]) for st in out[-3:]):
        a, q, t = out[-3], out[-2], out[-1]
        dx, dy = t[1] - q[1], t[2] - q[2]
        if abs(q[1] - a[1]) < 1e-6:                        # q reached vertically: move it in y
            if 1e-6 < abs(abs(dy) - abs(dx)) < snap and abs(dx) > snap:
                q[2] = t[2] - math.copysign(abs(dx), dy)
            elif 1e-6 < abs(dy) < snap and abs(dx) > snap:
                q[2] = t[2]
        elif abs(q[2] - a[2]) < 1e-6:                      # reached horizontally: move it in x
            if 1e-6 < abs(abs(dx) - abs(dy)) < snap and abs(dy) > snap:
                q[1] = t[1] - math.copysign(abs(dy), dx)
            elif 1e-6 < abs(dx) < snap and abs(dy) > snap:
                q[1] = t[1]
    return [tuple(st) for st in out]


def chamfer_steps(steps, size: float = 0.5):
    """Replace every corner of 60 degrees or more between straight segments
    with two 45-degree bends (a chamfer of up to `size` mm along each leg,
    at most 45% of the shorter leg). Vias, arcs and the end points stay."""
    if size <= 0:
        return list(steps)
    out = [steps[0]]
    for i in range(1, len(steps) - 1):
        cur = steps[i]
        prev, nxt = out[-1], steps[i + 1]
        if cur[0] != "pt" or _curve(cur[1:]) or _curve(nxt[1:]):
            out.append(cur)
            continue
        A, B, Cc = prev[1:3], cur[1:3], nxt[1:3]
        la, lb = math.dist(A, B), math.dist(B, Cc)
        if la < 1e-6 or lb < 1e-6:
            out.append(cur)
            continue
        u1, u2 = _unit(A, B), _unit(B, Cc)
        turn = math.degrees(math.acos(max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))))
        if turn < 60 or turn > 120:
            out.append(cur)
            continue
        c = min(size, 0.45 * la, 0.45 * lb)
        out.append(("pt", B[0] - u1[0] * c, B[1] - u1[1] * c))
        out.append(("pt", B[0] + u2[0] * c, B[1] + u2[1] * c))
    out.append(steps[-1])
    return out


def steps_to_geometry(steps, start_layer: int, other_layer: int):
    """[("pt"|"via", x, y[, curve])] -> ([(layer, [pts])], [(x, y)] vias)."""
    traces, vias = [], []
    layer, cur = start_layer, []
    for kind, x, y, *c in steps:
        cur.append((x, y, *c))
        if kind == "via":
            vias.append((x, y))
            if len(cur) > 1:
                traces.append((layer, _dedupe(cur)))
            layer = other_layer if layer == start_layer else start_layer
            cur = [(x, y)]
    if len(cur) > 1:
        traces.append((layer, _dedupe(cur)))
    return traces, vias


def _seg_dist(p1, p2, p3, p4) -> float:
    """Minimum distance between segments p1p2 and p3p4."""
    def pd(p, a, b):
        vx, vy = b[0] - a[0], b[1] - a[1]
        L2 = vx * vx + vy * vy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / L2))
        return math.hypot(p[0] - a[0] - t * vx, p[1] - a[1] - t * vy)

    def o(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    if o(p1, p2, p3) * o(p1, p2, p4) < 0 and o(p3, p4, p1) * o(p3, p4, p2) < 0:
        return 0.0
    return min(pd(p1, p3, p4), pd(p2, p3, p4), pd(p3, p1, p2), pd(p4, p1, p2))


def _check(net, traces, vias, width, via_d, obstacles, clearance, step=0.05):
    out, seen = [], set()

    def hit(o, x, y, extra):
        lim = clearance if (o.net or o.is_pad) else 0.0
        d = o.distance(x, y) - extra
        if d < lim - 1e-4:
            key = (o.net, o.kind, o.data[:2])
            if key not in seen:
                seen.add(key)
                out.append({"net": net, "at": [round(x, 3), round(y, 3)],
                            "with": o.net or ("hole/keepout" if not o.is_pad else "unconnected pad"),
                            "obstacle_at": [round(o.data[0], 3), round(o.data[1], 3)],
                            "short_by_mm": round(lim - d, 3)})
    for layer, pl in traces:
        for x, y in flatten(pl, step):
                for o in obstacles:
                    if o.net != net and (o.layers is None or layer in o.layers):
                        hit(o, x, y, width / 2)
    for x, y in vias:
        for o in obstacles:
            if o.net != net:
                hit(o, x, y, via_d / 2)
    return out


def plan_pair(root: ET.Element, p_net: str, n_net: str, centreline, width: float, gap: float,
              clearance: float | None = None, layer: int = 1, other_layer: int = 16,
              p_tail=None, n_tail=None, p_head=None, n_head=None, via_drill: float = 0.3, via_diameter: float = 0.6,
              max_skew_mm: float = 0.1, tune: bool = True, chamfer_mm: float = 0.5) -> dict:
    """Plan a coupled route for one pair. centreline: [[x, y], ...] from the
    start pads' end to the end pads' end (the trunk; fan-ins are added).
    p_head / n_head: optional explicit path [[x, y], ...] from that trace's
    start pad to its trunk start, for pins the automatic fan-in cannot reach
    (e.g. through a gap in a pin row).
    p_tail / n_tail: optional explicit path from that trace's trunk end to
    its end pad, as [[x, y], ...] with {"via": [x, y]} entries to change
    layer; use one to get past the partner when the pads are crossed.
    Layers are export numbers (1 top, 16 bottom)."""
    obs, rules, outline = board_obstacles(root)
    if clearance is None:
        clearance = max(_mm(rules.get(k), 0.0) for k in ("mdWireWire", "mdWirePad", "mdWireVia"))
    pads = pad_centres(root)
    for net in (p_net, n_net):
        if len(pads.get(net, [])) != 2:
            raise ValueError(f"{net} has {len(pads.get(net, []))} pads; route_pair joins exactly two")
    c = [tuple(map(float, q)) for q in centreline]
    if len(c) < 2:
        raise ValueError("the centreline needs at least two points")

    def ends(net):
        a, b = [(x, y) for x, y, _ in pads[net]]
        return (a, b) if math.dist(a, c[0]) + math.dist(b, c[-1]) <= math.dist(b, c[0]) + math.dist(a, c[-1]) else (b, a)
    (ps, pe), (ns, ne) = ends(p_net), ends(n_net)
    half = (width + gap) / 2
    left, right = offset_path(c, half), offset_path(c, -half)
    p_left = math.dist(left[0], ps) + math.dist(right[0], ns) <= math.dist(right[0], ps) + math.dist(left[0], ns)
    pt, nt = (left, right) if p_left else (right, left)
    crossed = math.dist(pt[-1], pe) + math.dist(nt[-1], ne) > math.dist(pt[-1], ne) + math.dist(nt[-1], pe) + 1e-6
    d0, d1 = _unit(c[0], c[1]), _unit(c[-2], c[-1])

    def head(pad, t, given):
        if given:
            pts = [tuple(pad)] + [tuple(map(float, q)) for q in given] + [t[0]]
            return _dedupe(_snap_last_leg(pts))
        return fan_in(pad, t[0], d0)

    def tail(t, pad, given):
        if given:
            pts = [("pt", *t[-1])]
            for q in given:
                pts.append(("via", *map(float, q["via"])) if isinstance(q, dict) else ("pt", *map(float, q)))
            return pts + [("pt", *pad)]
        back = fan_in(pad, t[-1], (-d1[0], -d1[1]))
        return [("pt", x, y) for x, y in reversed(back)]

    p_steps = chamfer_steps([("pt", *q) for q in head(ps, pt, p_head) + pt[1:-1]] + tail(pt, pe, p_tail), chamfer_mm)
    n_steps = chamfer_steps([("pt", *q) for q in head(ns, nt, n_head) + nt[1:-1]] + tail(nt, ne, n_tail), chamfer_mm)

    def split(steps):
        """(points before the first via, remaining steps): bumps go on the first part."""
        k = next((i for i, st in enumerate(steps) if st[0] == "via"), len(steps))
        return [st[1:] for st in steps[:k]], steps[k:]

    def steps_len(steps):
        tr, _ = steps_to_geometry(steps, layer, other_layer)
        return sum(length(pl) for _, pl in tr)
    skew_before = None
    added = 0.0
    if tune:
        for _ in range(8):
            lp, ln = steps_len(p_steps), steps_len(n_steps)
            if skew_before is None:
                skew_before = round(lp - ln, 4)
            if abs(lp - ln) <= max_skew_mm:
                break
            short, other = (p_steps, n_steps) if lp < ln else (n_steps, p_steps)
            pts, rest = split(short)
            new, a = add_bumps(pts, abs(lp - ln), _dense(split(other)[0]))
            if a <= 1e-4:
                break
            new_steps = [("pt", *q) for q in new] + rest
            if lp < ln:
                p_steps = new_steps
            else:
                n_steps = new_steps
            added += a
    p_steps, n_steps = clean_steps(p_steps), clean_steps(n_steps)
    p_tr, p_v = steps_to_geometry(p_steps, layer, other_layer)
    n_tr, n_v = steps_to_geometry(n_steps, layer, other_layer)
    conflicts = _check(p_net, p_tr, p_v, width, via_diameter, obs, clearance)
    conflicts += _check(n_net, n_tr, n_v, width, via_diameter, obs, clearance)
    # P against N: coupled runs sit exactly at `gap`; anything closer is a short
    p_flat = [(l, flatten(pl, 0.1)) for l, pl in p_tr]
    n_flat = [(l, flatten(pl, 0.1)) for l, pl in n_tr]
    pair_min = min((_seg_dist(a, b, e, f) - width for lp_, pl in p_flat for ln_, nl in n_flat if lp_ == ln_
                    for a, b in zip(pl, pl[1:]) for e, f in zip(nl, nl[1:])), default=None)
    for v in p_v:
        for ln_, nl in n_flat:
            for e, f in zip(nl, nl[1:]):
                dd = _seg_dist(v, v, e, f) - via_diameter / 2 - width / 2
                if dd < clearance - 1e-4:
                    conflicts.append({"net": p_net, "at": list(v), "with": n_net, "short_by_mm": round(clearance - dd, 3)})
    for v in n_v:
        for lp_, pl in p_flat:
            for e, f in zip(pl, pl[1:]):
                dd = _seg_dist(v, v, e, f) - via_diameter / 2 - width / 2
                if dd < clearance - 1e-4:
                    conflicts.append({"net": n_net, "at": list(v), "with": p_net, "short_by_mm": round(clearance - dd, 3)})
    if pair_min is not None and pair_min < min(gap, clearance) - 1e-3:
        conflicts.append({"net": p_net, "with": n_net, "short_by_mm": round(min(gap, clearance) - pair_min, 3),
                          "note": "P and N closer than the pair gap on the same layer (crossed pads need a tail with a via)"})
    lp = sum(length(pl) for _, pl in p_tr)
    ln = sum(length(pl) for _, pl in n_tr)
    return {"p": {"net": p_net, "traces": [{"layer": l, "points": [[round(v, 4) for v in q] for q in pl]} for l, pl in p_tr],
                  "vias": [[round(x, 4), round(y, 4)] for x, y in p_v], "length_mm": round(lp, 3)},
            "n": {"net": n_net, "traces": [{"layer": l, "points": [[round(v, 4) for v in q] for q in pl]} for l, pl in n_tr],
                  "vias": [[round(x, 4), round(y, 4)] for x, y in n_v], "length_mm": round(ln, 3)},
            "skew_mm": round(lp - ln, 4), "skew_before_tuning_mm": skew_before, "tuning_added_mm": round(added, 4),
            "crossed": crossed, "p_side": "left" if p_left else "right", "clearance_mm": clearance,
            "conflicts": conflicts, "ok": not conflicts and abs(lp - ln) <= max_skew_mm + 1e-6}
