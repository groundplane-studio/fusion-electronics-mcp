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


def _seg_tangents(a, b):
    """(start, end) unit tangents of the segment a -> b; an arc (b carries its sweep in
    degrees, + = counter-clockwise) turns by the sweep from start to end."""
    ux, uy = _unit(a, b)
    c = math.radians(_curve(b))
    if abs(c) < 1e-9:
        return (ux, uy), (ux, uy)
    rot = lambda t: (ux * math.cos(t) - uy * math.sin(t), ux * math.sin(t) + uy * math.cos(t))
    return rot(-c / 2), rot(c / 2)


def offset_path(pts, d: float) -> list[tuple]:
    """Offset a polyline by d (positive = left of travel) with mitred joints. Arcs (points
    (x, y, curve)) stay arcs with the same sweep: their ends move along the normals, so an arc
    joined tangentially stays concentric with radius R -+ d (4f)."""
    out = []
    n = len(pts)
    for i in range(n):
        if i == 0:
            ux, uy = _seg_tangents(pts[0], pts[1])[0]
            out.append((pts[0][0] - uy * d, pts[0][1] + ux * d))
            continue
        u1 = _seg_tangents(pts[i - 1], pts[i])[1]
        if i == n - 1:
            q = (pts[i][0] - u1[1] * d, pts[i][1] + u1[0] * d)
        else:
            u2 = _seg_tangents(pts[i], pts[i + 1])[0]
            n1, n2 = (-u1[1], u1[0]), (-u2[1], u2[0])
            bx, by = n1[0] + n2[0], n1[1] + n2[1]
            bl = math.hypot(bx, by)
            if bl < 1e-9:
                raise ValueError(f"the centreline doubles back at waypoint {i}")
            bx, by = bx / bl, by / bl
            m = d / (bx * n1[0] + by * n1[1])
            q = (pts[i][0] + bx * m, pts[i][1] + by * m)
        out.append((*q, _curve(pts[i])) if _curve(pts[i]) else q)
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
              space: float = 0.1, min_seg: float = 2.0, style: str = "rounded", near=None, leg_min: float = 0.0,
              keep_off=None):
    """Length-tuning bumps on one straight run of `pl`, adding about `extra` mm and bulging away
    from the partner polyline. Returns (polyline with (x, y, curve) arc points, added).
    style "rounded" (default): the KiCad tuner's shape, every corner a 90-degree arc of
    `radius`; a bump of height h adds 2h + (2 pi - 8) r, and below h = 2r the radius shrinks.
    style "45": trapezoids with 45-degree legs; a bump of height h adds 2h (sqrt 2 - 1).
    top: the flat on top; space: the straight between bumps; max_h: the tallest bump.
    near: an (x, y) to tune next to (the end that causes the skew); without it the longest run
    is used and the bumps centred on it.
    leg_min: the least centre-to-centre distance between neighbouring legs of the same trace
    (width + same-net spacing): the flat and the spaces grow so legs never come closer.
    keep_off: (points, min_distance): every point of the bumps must stay at least min_distance
    from these points (the partner trace, centre to centre). Runs and positions are tried in
    turn (preferred, centred, either end, other runs) and the first that keeps clear is used;
    near a bend the partner's next segment can sit on the side the bumps bulge to."""
    if extra <= 1e-4:
        return list(pl), 0.0
    r = radius
    diag = style == "45"
    rounded_per = lambda h, rr: 2 * h + (2 * math.pi - 8) * rr
    per = 2 * max_h * (math.sqrt(2) - 1) if diag else rounded_per(max_h, r)
    new = list(pl)
    cand = [k for k in range(len(new) - 1) if not _curve(new[k + 1]) and _seg_len(new[k], new[k + 1]) >= min_seg]
    if not cand:
        return new, 0.0
    if near is not None:
        cand.sort(key=lambda k: _seg_dist(near, near, new[k][:2], new[k + 1][:2]))
    else:
        cand.sort(key=lambda k: -_seg_len(new[k], new[k + 1]))
    for i in cand:
        a, b = new[i], new[i + 1]
        L = _seg_len(a, b)
        k = max(1, math.ceil(extra / per - 1e-9))
        while k >= 1:
            each = min(extra / k, per)
            if diag:
                h = each / (2 * (math.sqrt(2) - 1))
                rr = 0.0
                top_e = max(top, leg_min * math.sqrt(2))
                space_e = max(space, leg_min * math.sqrt(2))
                width_b = 2 * h + top_e
            else:
                rr = r
                h = (each - (2 * math.pi - 8) * r) / 2
                if h < 2 * r + 0.02:                         # no sliver of a straight leg between the arcs
                    rr = each / (2 * math.pi - 4)
                    h = 2 * rr
                top_e = max(top, leg_min - 2 * rr)
                space_e = max(space, leg_min - 2 * rr)
                width_b = 4 * rr + top_e
            pitch = width_b + space_e
            if k * pitch + space_e <= L:
                break
            k -= 1                                          # fewer bumps fit: each is capped at `per`
        if k < 1:
            continue
        ux, uy = _unit(a, b)
        nx, ny = -uy, ux
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        away = min(partner, key=lambda q: math.dist(q[:2], (mx, my)))
        if (away[0] - mx) * nx + (away[1] - my) * ny > 0:
            nx, ny = -nx, -ny
        turn = 90.0 if (nx, ny) == (-uy, ux) else -90.0  # first corner turns toward n
        span = k * pitch - space_e
        starts = [(L - span) / 2, space_e, L - span - space_e]
        if near is not None:
            starts.insert(0, space_e if math.dist(near, a[:2]) <= math.dist(near, b[:2]) else L - span - space_e)
        P = lambda along, out: (a[0] + ux * along + nx * out, a[1] + uy * along + ny * out)
        for s0 in dict.fromkeys(round(s, 6) for s in starts):
            pts, added = [], 0.0
            for j in range(k):
                t0 = s0 + j * pitch
                if diag:
                    pts += [P(t0, 0), P(t0 + h, h), P(t0 + h + top_e, h), P(t0 + 2 * h + top_e, 0)]
                    added += 2 * h * (math.sqrt(2) - 1)
                    continue
                pts += [P(t0, 0), (*P(t0 + rr, rr), turn)]
                if h - 2 * rr > 1e-6:
                    pts.append(P(t0 + rr, h - rr))
                pts += [(*P(t0 + 2 * rr, h), -turn), P(t0 + 2 * rr + top_e, h), (*P(t0 + 3 * rr + top_e, h - rr), -turn)]
                if h - 2 * rr > 1e-6:
                    pts.append(P(t0 + 3 * rr + top_e, rr))
                pts.append((*P(t0 + width_b, 0), turn))
                added += rounded_per(h, rr)
            if keep_off:
                pts_off, dmin = keep_off
                body = flatten([P(s0, 0)] + pts[1:], 0.05)
                if any(math.dist(q, o) < dmin - 1e-4 for q in body for o in pts_off
                       if abs(q[0] - o[0]) < dmin and abs(q[1] - o[1]) < dmin):
                    continue                                 # too close to the partner here: try elsewhere
            out = new[:i + 1] + pts + new[i + 1:]
            return [tuple(round(v, 4) for v in q) for q in out], added
    return new, 0.0


def same_net_spacing(net, traces, width: float, gap: float, step: float = 0.05) -> list[dict]:
    """Places where one trace comes back closer than `gap` edge to edge to itself on the same
    layer (meander legs, tight detours): JLC asks for 0.25 mm same-net spacing. Points close
    along the path (corners) do not count."""
    out = []
    lim = width + gap
    for layer, pl in traces:
        pts = flatten(pl, step)
        if len(pts) < 3:
            continue
        s = [0.0]
        for p, q in zip(pts, pts[1:]):
            s.append(s[-1] + math.dist(p, q))
        cells = {}
        for i, (x, y) in enumerate(pts):
            cells.setdefault((math.floor(x / lim), math.floor(y / lim)), []).append(i)
        worst = None
        for i, (x, y) in enumerate(pts):
            cx, cy = math.floor(x / lim), math.floor(y / lim)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j in cells.get((cx + dx, cy + dy), ()):
                        if j <= i or s[j] - s[i] <= 1.6 * lim:
                            continue
                        d = math.dist(pts[i], pts[j])
                        if d < lim - 1e-3 and (worst is None or d < worst[0]):
                            worst = (d, pts[i])
        if worst:
            out.append({"net": net, "at": [round(worst[1][0], 3), round(worst[1][1], 3)], "with": net,
                        "short_by_mm": round(lim - worst[0], 3),
                        "note": f"the trace comes back within {worst[0] - width:.3f} mm of itself edge to edge "
                                f"on layer {layer} (same-net spacing {gap} mm, JLC)"})
    return out


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
    """Planned copper against other nets' copper, holes and keepouts. clearance: mm, or a
    function of the obstacle (per-net class clearances)."""
    out, seen = [], set()

    def hit(o, x, y, extra, layer=None):
        lim = (clearance(o) if callable(clearance) else clearance) if (o.net or o.is_pad) else 0.0
        d = o.distance(x, y) - extra
        if d < lim - 1e-4:
            key = (o.net, o.kind, o.data[:2])
            if key not in seen:
                seen.add(key)
                out.append({"net": net, "at": [round(x, 3), round(y, 3)], "layer": layer,
                            "with": o.net or ("hole/keepout" if not o.is_pad else "unconnected pad"),
                            "obstacle_at": [round(o.data[0], 3), round(o.data[1], 3)],
                            "short_by_mm": round(lim - d, 3)})
    for layer, pl in traces:
        for x, y in flatten(pl, step):
                for o in obstacles:
                    if o.net != net and (o.layers is None or layer in o.layers):
                        hit(o, x, y, width / 2, layer)
    for x, y in vias:
        for o in obstacles:
            if o.net != net:
                hit(o, x, y, via_d / 2)
    return out


def _down(v: float) -> float:
    """Round down to 1 um (a hair of tolerance so 0.10999999 stays 0.11)."""
    return math.floor(v * 1000 + 1e-6) / 1000


def explain_squeeze(x: float, y: float, net: str, layer: int, obstacles, width: float, need, min_width: float,
                    partner=(), gap: float = 0.0, neck_down: bool = False):
    """Is a conflict at (x, y) a squeeze: copper on both sides (two pins) leaving no room to move
    the trace (and its partner, when the partner runs between it and the far side)? Then the
    room there and the ways out, for the trace where it is: the widest width that fits (if the
    fab minimum does) or the clearance that would. neck_down: whether the plan already had
    neck-down on (then the advice does not suggest turning it on). None if it is not a squeeze."""
    cand = sorted((o for o in obstacles if o.net != net and (o.layers is None or layer in o.layers)
                   and (o.net or o.is_pad)), key=lambda o: o.distance(x, y))
    if len(cand) < 2:
        return None
    a = cand[0]
    ua = _unit((x, y), a.data[:2])
    b = next((o for o in cand[1:8] if (lambda u: u[0] * ua[0] + u[1] * ua[1])(_unit((x, y), o.data[:2])) < -0.2), None)
    if b is None:
        return None
    room = a.distance(x, y) + b.distance(x, y)               # edge to edge through the point
    between = any(_seg_dist((x, y), b.data[:2], q, q) < width for q in partner)
    needed = width + need(a) + need(b) + ((width + gap) if between else 0.0)
    if room >= needed - 1e-4:
        return None                                          # there is room: the path is just off-centre
    max_w = 2 * (a.distance(x, y) - need(a))                 # at this centreline, on the near side
    max_c = a.distance(x, y) - width / 2
    names = [o.net or "an unconnected pad" for o in (a, b)]
    options = []
    if max_w >= min_width - 1e-6 and neck_down:
        options.append(f"a {_down(max_w):.3f} mm neck would fit here, but neck_down (already on) did not narrow "
                       "this spot: move the route so the trace runs through the squeeze")
    elif max_w >= min_width - 1e-6:
        options.append(f"neck down to {_down(max_w):.3f} mm over the squeeze (neck_down=true)")
    else:
        options.append(f"no width fits here: even the fab minimum {min_width} mm is too wide")
    if max_c > 0:
        options.append(f"lower the clearance to {_down(max_c):.3f} mm (e.g. the class clearance)")
    options.append("route another way")
    return {"between": names, "room_mm": round(room, 3), "needs_mm": round(needed, 3),
            "with_partner": between, "max_width_mm": round(max(max_w, 0.0), 3), "options": options}


def _split_at(pl, s0: float, s1: float):
    """A polyline cut into (before, middle, after) at path lengths s0 < s1 (arcs measured along
    the arc), or None when nothing is left between. Arcs are not cut: a cut that falls on one
    moves out to its end, so the middle takes the whole arc."""
    cum = [0.0]
    for a, b in zip(pl, pl[1:]):
        cum.append(cum[-1] + _seg_len(a, b))
    s0, s1 = max(0.0, s0), min(cum[-1], s1)
    seg = lambda s: min(max(k for k in range(len(cum) - 1) if cum[k] <= s + 1e-12), len(pl) - 2)
    i0, i1 = seg(s0), seg(s1)
    if _curve(pl[i0 + 1]):
        s0 = cum[i0]
    if _curve(pl[i1 + 1]):
        s1 = cum[i1 + 1]
    if s1 - s0 < 1e-3:
        return None

    def at(i, s):
        """The point at path length s on segment i: a vertex (its arc kept) or a cut on a straight."""
        L = cum[i + 1] - cum[i]
        t = 0.0 if L < 1e-12 else (s - cum[i]) / L
        if t <= 1e-9:
            return tuple(pl[i][:2])
        if t >= 1 - 1e-9:
            return tuple(pl[i + 1])
        a, b = pl[i], pl[i + 1]
        return round(a[0] + (b[0] - a[0]) * t, 4), round(a[1] + (b[1] - a[1]) * t, 4)
    p0, p1 = at(i0, s0), at(i1, s1)
    before = [tuple(q) for q in pl[:i0 + 1]] + [p0]
    middle = [p0[:2]] + [tuple(q) for q in pl[i0 + 1:i1 + 1]] + [p1]
    after = [p1[:2]] + [tuple(q) for q in pl[i1 + 1:]]
    clean = lambda pts: [q for k, q in enumerate(pts) if k == 0 or math.dist(q[:2], pts[k - 1][:2]) > 1e-6]
    return clean(before), clean(middle), clean(after)


def neck_down_fn(net, traces, obstacles, width: float, need, min_width: float, step: float = 0.05,
              extend: float = 0.1, partner=None, gap: float = 0.0):
    """Narrow each trace only where it squeezes between other nets' copper on both sides (pins):
    the section where the full width breaks the clearance and the trace cannot move over, plus
    `extend` each side, at the widest width that fits (not below min_width). The partner trace
    is not an obstacle here: crowding by it is a routing problem, never a neck. Returns (traces as [(layer, pts, width)], necks: [{layer, length_mm,
    width_mm}]); traces that need no neck keep `width`."""
    out, necks = [], []
    for layer, pl in traces:
        pts = flatten(pl, step)
        cum = [0.0]
        for a, b in zip(pts, pts[1:]):
            cum.append(cum[-1] + math.dist(a, b))
        bad = {}                                              # sample index -> widest width that fits there
        tight = {}                                            # sample index -> 2 x the least room, where the full width conflicts
        near = [o for o in obstacles if o.net != net and (o.layers is None or layer in o.layers) and (o.net or o.is_pad)]
        for k, (x, y) in enumerate(pts):
            rooms = [(o.distance(x, y) - need(o), _unit((x, y), o.data[:2])) for o in near]
            least = min((r for r, _ in rooms), default=width)
            if least < width / 2 - 1e-4:
                tight[k] = 2 * least
            mate = None                                       # direction to the partner, if it runs alongside
            if partner and partner.get(layer):
                q = min(partner[layer], key=lambda q: (q[0] - x) ** 2 + (q[1] - y) ** 2)
                if math.dist(q, (x, y)) < 2 * (width + gap):
                    mate = _unit((x, y), q)
            for room, u in rooms:
                if room >= width / 2 - 1e-4:
                    continue
                # a squeeze only when copper on the far side leaves no room to move the trace (and its
                # partner, when the partner is in between) over; one-sided crowding is a routing
                # conflict, never a reason to neck (PoE board TP2_1, 2026-10-08)
                far = []
                for r2, u2 in rooms:
                    if u2[0] * u[0] + u2[1] * u[1] < -0.2:
                        between = mate is not None and mate[0] * u2[0] + mate[1] * u2[1] > 0.5
                        far.append(r2 - ((width + gap) if between else 0.0))
                if far and room + min(far) < width - 1e-4:
                    bad[k] = min(bad.get(k, width), 2 * room)
        if not bad:
            out.append((layer, pl, width))
            continue
        groups, cur = [], [min(bad)]                             # each squeeze on its own
        for k in sorted(bad)[1:]:
            if cum[k] - cum[cur[-1]] > 2 * extend + step:
                groups.append(cur)
                cur = []
            cur.append(k)
        groups.append(cur)
        # a squeeze runs on as long as the full width still conflicts next to it: beside a round
        # pad the both-sides test ends before the clearance does (PoE board TP2_1 at J2 pin 6, 2026-10-08)
        for g in groups:
            while g[0] - 1 in tight and g[0] - 1 not in bad:
                g.insert(0, g[0] - 1)
            while g[-1] + 1 in tight and g[-1] + 1 not in bad:
                g.append(g[-1] + 1)
        rest, offset, pieces, ok = pl, 0.0, [], True
        for g in groups:
            w = _down(min(bad.get(k, tight.get(k, width)) for k in g))
            cut = _split_at(rest, cum[g[0]] - extend - offset, cum[g[-1]] + extend - offset) if w >= min_width - 1e-6 else None
            if cut is None:
                ok = False
                break
            before, middle, after = cut
            pieces += [(before, width), (middle, w)]
            necks.append({"net": net, "layer": layer, "width_mm": w, "length_mm": round(length(middle), 3),
                          "from": list(middle[0]), "to": list(middle[-1])})
            offset += length(before) + length(middle)
            rest = after
        if not ok:
            out.append((layer, pl, width))                       # cannot neck: the conflict stays
            continue
        pieces.append((rest, width))
        out += [(layer, pts_, wd) for pts_, wd in pieces if len(pts_) >= 2]
    return out, necks


def _fillet(pts, r: float):
    """Round each 45-degree corner of a straight polyline with an arc of radius r: the corner
    point becomes a tangent point and an arc end (x, y, curve). Ends stay."""
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, b, c = out[-1][:2], pts[i], pts[i + 1]
        u1, u2 = _unit(a, b), _unit(b, c)
        cross = u1[0] * u2[1] - u1[1] * u2[0]
        ang = math.degrees(math.atan2(cross, u1[0] * u2[0] + u1[1] * u2[1]))
        if abs(ang) < 1e-6:
            out.append(b)
            continue
        tl = r * math.tan(math.radians(abs(ang)) / 2)
        out.append((b[0] - u1[0] * tl, b[1] - u1[1] * tl))
        out.append((b[0] + u2[0] * tl, b[1] + u2[1] * tl, ang))
    out.append(pts[-1])
    return out


def add_detours(c, add: float, side: float, width: float, gap: float, max_depth: float = 1.0,
                style: str = "45", radius: float | None = None, avoid=(), flat: float | None = None):
    """Coupled length detours in a pair's centreline: trapezoids with 45-degree legs (or the same
    with rounded corners) bulging to `side` (+1 left of travel, -1 right), spread over the
    straight runs longest first, each run taking as many detours (at most max_depth deep) as it
    holds. Each detour adds the same length to both traces, as the four bends cancel the inner
    and outer offsets (a 45-degree trapezoid of depth d adds 2 d (sqrt 2 - 1) = 0.828 d).
    Returns (centreline, [{at, count, depth_mm, added_mm} per run]). When the runs cannot hold
    `add`, the error says how much they can and which run limits it."""
    if add <= 1e-4:
        return list(c), []
    half = (width + gap) / 2
    pitch = width + gap
    flat = flat if flat is not None else 2 * pitch
    if style == "rounded":
        radius = radius if radius is not None else max(0.2, half + width)
        if radius < half + width / 2 + 1e-6:
            raise ValueError(f"detour radius {radius} mm is too small for this pair: the inner trace needs at "
                             f"least {half + width / 2:.3f} mm")
    elif style != "45":
        raise ValueError("detour style is '45' or 'rounded'")
    margin = 2 * pitch + 0.5

    def shape(a, u, nrm, L, k, d):
        """Points of k detours of depth d centred on the run a -> a + u L."""
        span = k * (2 * d + flat) + (k - 1) * flat
        s0 = (L - span) / 2
        P = lambda t, o: (a[0] + u[0] * t + nrm[0] * o, a[1] + u[1] * t + nrm[1] * o)
        pts = [tuple(a)]
        for j in range(k):
            t = s0 + j * (2 * d + 2 * flat)
            pts += [P(t, 0), P(t + d, d), P(t + d + flat, d), P(t + 2 * d + flat, 0)]
        pts.append(P(L, 0))
        return _fillet(pts, radius) if style == "rounded" else pts

    def gained(a, u, nrm, L, k, d):
        return length(shape(a, u, nrm, L, k, d)) - L

    runs = []
    for i in range(len(c) - 1):
        a, b = c[i], c[i + 1]
        if _curve(b):
            continue
        L = math.dist(a[:2], b[:2])
        if L < 1e-9 or any(_seg_dist(p, p, a[:2], b[:2]) < 1e-3 for p in avoid):
            continue
        k = int((L - 2 * margin + flat) // (2 * max_depth + 2 * flat))     # detours this run holds
        u = _unit(a[:2], b[:2])
        nrm = (-u[1] * side, u[0] * side)
        cap = gained(a[:2], u, nrm, L, k, max_depth) if k > 0 else 0.0
        runs.append({"i": i, "a": a[:2], "b": b[:2], "L": L, "k": k, "u": u, "nrm": nrm, "cap": cap})
    if not runs:
        raise ValueError("no straight part of the centreline to put a length detour on")
    total = sum(r["cap"] for r in runs)
    if total < add - 1e-4:
        top = max(runs, key=lambda r: r["L"])
        raise ValueError(
            f"the centreline's straight runs hold at most {total:.3f} mm of detours {max_depth:g} mm deep, "
            f"{add - total:.3f} mm short of {add:.3f} mm (the run lengths limit it: the longest, "
            f"{top['L']:.2f} mm from {[round(v, 3) for v in top['a']]} to {[round(v, 3) for v in top['b']]}, "
            f"holds {top['k']} detour(s)). Allow deeper detours (detour_max_depth_mm), lengthen the straight runs, "
            "or add the rest on another part of the route")
    left, plan = add, []
    for r in sorted(runs, key=lambda r: -r["cap"]):
        if left <= 1e-6 or r["cap"] <= 1e-6:
            continue
        want = min(left, r["cap"])
        per_max = r["cap"] / r["k"]
        k = max(1, math.ceil(want / per_max - 1e-9))
        if style == "45":
            d = want / (k * 2 * (math.sqrt(2) - 1))
        else:
            d = min(max_depth, want / (0.828 * k))
            for _ in range(6):                              # rounded corners take a little length back
                got = gained(r["a"], r["u"], r["nrm"], r["L"], k, d)
                if abs(got - want) < 1e-5:
                    break
                slope = (gained(r["a"], r["u"], r["nrm"], r["L"], k, d + 1e-3) - got) / 1e-3
                d = min(max_depth, max(1e-3, d + (want - got) / slope))
        got = gained(r["a"], r["u"], r["nrm"], r["L"], k, d)
        plan.append((r, k, d, got))
        left -= got
    new = list(c)
    info = []
    for r, k, d, got in sorted(plan, key=lambda t: -t[0]["i"]):  # from the end, so indices stay valid
        pts = [tuple(round(v, 4) for v in q) for q in shape(r["a"], r["u"], r["nrm"], r["L"], k, d)]
        new = new[:r["i"] + 1] + pts[1:-1] + new[r["i"] + 1:]
        mid = (r["a"][0] + r["u"][0] * r["L"] / 2 + r["nrm"][0] * d / 2,
               r["a"][1] + r["u"][1] * r["L"] / 2 + r["nrm"][1] * d / 2)
        info.append({"at": [round(mid[0], 3), round(mid[1], 3)], "count": k, "depth_mm": round(d, 4),
                     "added_mm": round(got, 4)})
    return new, sorted(info, key=lambda x: -x["added_mm"])


def _layer_of(name, layer: int, other: int) -> int:
    n = str(name).lower()
    if n in ("top", "1"):
        return 1
    if n in ("bottom", "16"):
        return 16
    raise ValueError(f"layer change 'to': top or bottom, not {name!r}")


def _insert_layer_changes(c, changes, half: float, spacing: float):
    """Centreline with each layer change point (and the two points where the pair starts and
    ends its fan-out into the via pair) inserted as vertices. Returns (centreline, {vertex index
    of each change: its spacing})."""
    fan = max(0.0, spacing / 2 - half)
    pts = list(c)
    marks = []
    for ch in changes:
        at = tuple(map(float, ch["at"]))
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            L = math.dist(a[:2], b[:2])
            if L < 1e-9 or _curve(b):
                continue
            u = _unit(a, b)
            t = (at[0] - a[0]) * u[0] + (at[1] - a[1]) * u[1]
            off = abs((at[0] - a[0]) * u[1] - (at[1] - a[1]) * u[0])
            if off <= 1e-3 and -1e-6 <= t <= L + 1e-6:
                if t - fan < 0.05 or t + fan > L - 0.05:
                    raise ValueError(f"layer change at {list(at)}: needs {fan + 0.05:.3f} mm of straight centreline on "
                                     f"each side for the pair to fan out to the via pair ({spacing:.3f} mm apart)")
                p = (a[0] + u[0] * t, a[1] + u[1] * t)
                new = ([(p[0] - u[0] * fan, p[1] - u[1] * fan), p, (p[0] + u[0] * fan, p[1] + u[1] * fan)]
                       if fan > 1e-6 else [p])
                pts[i + 1:i + 1] = new
                marks.append(p)
                break
        else:
            raise ValueError(f"layer change at {list(at)} is not on a straight part of the centreline")
    idx = {}
    for p in marks:
        idx[next(j for j, q in enumerate(pts) if q == p)] = spacing
    if 0 in idx or len(pts) - 1 in idx:
        raise ValueError("a layer change cannot be at the end of the centreline")
    return pts, idx


def _runs(steps):
    """(a, b) index ranges of the trace on one layer: from the start (or a via) to the next via
    (or the end); the vias themselves are the shared end points."""
    cuts = [i for i, st in enumerate(steps) if st[0] == "via"]
    bounds = [0] + cuts + [len(steps) - 1]
    return [(a, b) for a, b in zip(bounds, bounds[1:]) if b > a]


def _longest_straight(pts) -> float:
    return max((_seg_len(a, b) for a, b in zip(pts, pts[1:]) if not _curve(b)), default=0.0)


def plan_pair(root: ET.Element, p_net: str, n_net: str, centreline, width: float, gap: float,
              clearance: float | None = None, layer: int = 1, other_layer: int = 16,
              p_tail=None, n_tail=None, p_head=None, n_head=None, via_drill: float = 0.3, via_diameter: float = 0.6,
              max_skew_mm: float = 0.1, tune: bool = True, chamfer_mm: float = 0.5,
              layer_changes=None, net_clearance: dict | None = None, pair_clearance: float | None = None,
              via_clearance: float | None = None, margin: float = 0.0, tune_style: str = "rounded",
              tune_radius: float = 0.25, tune_flat: float = 0.1, tune_gap: float = 0.1, tune_max_height: float = 0.6,
              tune_at: str = "longest", same_net_gap: float = 0.25, min_width: float = 0.09,
              neck_down: bool = False, add_length: float = 0.0, detour_style: str = "45",
              detour_max_depth: float = 1.0, detour_radius: float | None = None, detour_side: str = "auto",
              layer_depths: dict | None = None) -> dict:
    """Plan a coupled route for one pair. centreline: [[x, y], ...] from the
    start pads' end to the end pads' end (the trunk; fan-ins are added).
    p_head / n_head: optional explicit path from that trace's start pad to its
    trunk start, as [[x, y], ...] with {"via": [x, y]} entries to change layer
    (both heads need the same number of vias so the trunk stays on one layer).
    p_tail / n_tail: optional explicit path from that trace's trunk end to
    its end pad, the same way; use one to get past the partner when the pads
    are crossed.
    layer_changes: [{"at": [x, y], "to": "bottom"}, ...] points on the
    centreline where the PAIR changes layer: a via pair across the pair,
    spaced via_diameter + via clearance (+ margin) or the pair pitch if wider,
    each trace fanning out to its via at 45 degrees and back, so the pair stays
    coupled with the same width and gap on the new layer.
    Clearances: each check uses the largest of `clearance` (default: the
    board's wire rules), the obstacle net's class clearance (net_clearance)
    and the pair's class clearance (pair_clearance), plus `margin`.
    Tuning adds bumps to the shorter trace on a straight run on ANY layer
    (tails after vias included): tune_style "rounded" (default) or "45",
    tune_radius / tune_flat / tune_gap / tune_max_height set the shape, and
    tune_at "longest" (the longest run) or "mismatch" (the run nearest the end
    whose pads cause the skew). Neighbouring legs of one trace keep at least
    same_net_gap edge to edge (JLC: 0.25 mm), checked on the whole route.
    Centreline points may carry an arc: [x, y, curve] (degrees, + = counter-
    clockwise) ends an arc; both traces follow it concentrically.
    A conflict where a trace squeezes between two pieces of copper (a pin
    field) says so, with the room there and the ways out; neck_down=true
    narrows the trace only over each squeeze, to the widest width that fits
    and not below min_width (the fab minimum), and lists the necks.
    add_length: lengthen BOTH traces by this much (between-pair matching) with
    coupled detours in the centreline (add_detours): detour_style "45" or
    "rounded", at most detour_max_depth deep each, on detour_side "left",
    "right" or "auto" (both tried, the one with fewer conflicts kept).
    Lengths (and the skew tuned) include each via's barrel between layer and
    other_layer; layer_depths: {export layer: depth mm} (length_groups.layer_depths,
    default from the board alone: 1.6 mm assumed without a stackup).
    Layers are export numbers (1 top, 16 bottom)."""
    if add_length and add_length > 1e-4:
        call = {k: v for k, v in locals().items()}
        cl = [tuple(map(float, q[:3])) if len(q) > 2 and q[2] else tuple(map(float, q[:2])) for q in centreline]
        avoid = [tuple(map(float, ch["at"][:2])) for ch in (layer_changes or [])]
        sides = {"left": [1.0], "right": [-1.0], "auto": [1.0, -1.0]}.get(detour_side)
        if sides is None:
            raise ValueError("detour_side is 'left', 'right' or 'auto'")
        best, err = None, None
        for sd in sides:
            try:
                c2, info = add_detours(cl, add_length, sd, width, gap, detour_max_depth, detour_style, detour_radius,
                                       avoid)
            except ValueError as ex:
                err = ex
                continue
            p = plan_pair(**{**call, "centreline": c2, "add_length": 0.0})
            p["detours"] = info
            p["detour_side"] = "left" if sd > 0 else "right"
            if best is None or (p["ok"], -len(p["conflicts"])) > (best["ok"], -len(best["conflicts"])):
                best = p
        if best is None:
            raise err
        return best
    if tune_style not in ("rounded", "45"):
        raise ValueError("tune_style is 'rounded' or '45'")
    if tune_at not in ("longest", "mismatch"):
        raise ValueError("tune_at is 'longest' or 'mismatch'")
    if layer_depths is None:
        from .length_groups import layer_depths as _depths
        layer_depths, depths_from = _depths(root)
    else:
        depths_from = "given"
    if layer not in layer_depths or other_layer not in layer_depths:
        raise ValueError(f"layer {layer} or {other_layer} is not a copper layer of the board's stack")
    barrel = abs(layer_depths[layer] - layer_depths[other_layer])
    obs, rules, outline = board_obstacles(root)
    if clearance is None:
        clearance = max(_mm(rules.get(k), 0.0) for k in ("mdWireWire", "mdWirePad", "mdWireVia"))
    net_clearance = net_clearance or {}
    pc = pair_clearance or 0.0

    def need(o):                                           # clearance DRC enforces to this obstacle
        return max(clearance, net_clearance.get(o.net, 0.0) if o.net else 0.0, pc) + margin
    # P and N are in the same class: DRC holds their vias apart by the class-to-class clearance too
    # (eth_100's 0.15 mm on the PoE board; 0.73 mm spacing from the 0.12 mm board rule failed it)
    via_clear = max(via_clearance if via_clearance is not None else max(_mm(rules.get("mdViaVia"), 0.0), clearance),
                    pc) + margin
    pads = pad_centres(root)
    for net in (p_net, n_net):
        if len(pads.get(net, [])) != 2:
            raise ValueError(f"{net} has {len(pads.get(net, []))} pads; route_pair joins exactly two")
    c = [tuple(map(float, q[:3])) if len(q) > 2 and q[2] else tuple(map(float, q[:2])) for q in centreline]
    if len(c) < 2:
        raise ValueError("the centreline needs at least two points")
    half = (width + gap) / 2
    spacing = max(2 * half, via_diameter + via_clear)
    changes = list(layer_changes or [])
    if changes:
        c, change_at = _insert_layer_changes(c, changes, half, spacing)
    else:
        change_at = {}

    def ends(net):
        a, b = [(x, y) for x, y, _ in pads[net]]
        c0, c1 = c[0][:2], c[-1][:2]
        return (a, b) if math.dist(a, c0) + math.dist(b, c1) <= math.dist(b, c0) + math.dist(a, c1) else (b, a)
    (ps, pe), (ns, ne) = ends(p_net), ends(n_net)
    left, right = offset_path(c, half), offset_path(c, -half)
    p_left = math.dist(left[0], ps) + math.dist(right[0], ns) <= math.dist(right[0], ps) + math.dist(left[0], ns)
    pt, nt = (left, right) if p_left else (right, left)
    pe2, ne2 = pt[-1][:2], nt[-1][:2]
    crossed = math.dist(pe2, pe) + math.dist(ne2, ne) > math.dist(pe2, ne) + math.dist(ne2, pe) + 1e-6
    d0, d1 = _seg_tangents(c[0], c[1])[0], _seg_tangents(c[-2], c[-1])[1]

    def given_steps(given):
        return [("via", *map(float, q["via"])) if isinstance(q, dict) else ("pt", *map(float, q)) for q in given]

    def head(pad, t, given):
        if given:
            steps = [("pt", *pad)] + given_steps(given) + [("pt", *t[0])]
            k = max((i for i, st in enumerate(steps) if st[0] == "via"), default=0)
            tail_pts = _dedupe(_snap_last_leg([st[1:] for st in steps[k:]]))
            return steps[:k] + [(steps[k][0], *tail_pts[0])] + [("pt", *q) for q in tail_pts[1:]]
        return [("pt", *q) for q in fan_in(pad, t[0], d0)]

    def trunk(t, sign):
        """The trace's trunk between its first and last point, vias at the layer changes."""
        out = []
        for j in range(1, len(t) - 1):
            if j in change_at:
                u = _unit(c[j - 1], c[j + 1])
                n = (-u[1], u[0])
                s = change_at[j] / 2
                out.append(("via", c[j][0] + sign * n[0] * s, c[j][1] + sign * n[1] * s))
            else:
                out.append(("pt", *t[j]))
        return out

    def tail(t, pad, given):
        if given:
            return [("pt", *t[-1])] + given_steps(given) + [("pt", *pad)]
        back = fan_in(pad, t[-1][:2], (-d1[0], -d1[1]))
        return [("pt", *t[-1])] + [("pt", x, y) for x, y in list(reversed(back))[1:]]

    for name, h in (("p_head_mm", p_head), ("n_head_mm", n_head)):
        for q in h or []:
            if isinstance(q, dict) and "via" not in q:
                raise ValueError(f"{name}: a dict entry must be {{\"via\": [x, y]}}")
    hv = [sum(isinstance(q, dict) for q in (h or [])) for h in (p_head, n_head)]
    if hv[0] % 2 != hv[1] % 2:
        raise ValueError(f"p_head_mm has {hv[0]} via(s) and n_head_mm {hv[1]}: both heads must change layer the same "
                         "way, or the trunk would be on two layers")
    trunk_layer = layer if hv[0] % 2 == 0 else other_layer
    lay = trunk_layer
    for ch in changes:                                     # check each change's "to" against the toggling
        lay = other_layer if lay == layer else layer
        if "to" in ch and _layer_of(ch["to"], layer, other_layer) != lay:
            raise ValueError(f"layer change at {ch['at']}: the pair is already on "
                             f"{'top' if lay != 1 else 'bottom'} there, so it goes to {'top' if lay == 1 else 'bottom'}")
    p_sign = 1.0 if p_left else -1.0
    parts = {"p": (head(ps, pt, p_head), trunk(pt, p_sign), tail(pt, pe, p_tail)),
             "n": (head(ns, nt, n_head), trunk(nt, -p_sign), tail(nt, ne, n_tail))}
    p_steps = chamfer_steps(sum(parts["p"], []), chamfer_mm)
    n_steps = chamfer_steps(sum(parts["n"], []), chamfer_mm)

    def steps_len(steps):                                 # traces plus via barrels
        tr, vs = steps_to_geometry(steps, layer, other_layer)
        return sum(length(pl) for _, pl in tr) + barrel * len(vs)

    def run_layer(steps, a):                              # the layer after every via up to the run's start
        return layer if sum(1 for st in steps[:a + 1] if st[0] == "via") % 2 == 0 else other_layer

    near = None
    if tune_at == "mismatch":                              # tune next to the end whose pads cause the skew
        plen = steps_len
        head_skew = abs(plen(parts["p"][0]) - plen(parts["n"][0]))
        tail_skew = abs(plen(parts["p"][2]) - plen(parts["n"][2]))
        a_, b_ = (ps, ns) if head_skew >= tail_skew else (pe, ne)
        near = ((a_[0] + b_[0]) / 2, (a_[1] + b_[1]) / 2)
    bump = dict(radius=tune_radius, top=tune_flat, space=tune_gap, max_h=tune_max_height, style=tune_style,
                leg_min=width + same_net_gap)
    skew_before = None
    added = 0.0
    tune_note = None
    if tune:
        for _ in range(12):
            lp, ln = steps_len(p_steps), steps_len(n_steps)
            if skew_before is None:
                skew_before = round(lp - ln, 4)
            if abs(lp - ln) <= max_skew_mm:
                break
            short, other = (p_steps, n_steps) if lp < ln else (n_steps, p_steps)
            other_tr, _ = steps_to_geometry(other, layer, other_layer)
            done = False
            if near is None:
                order = lambda ab: -_longest_straight([st[1:] for st in short[ab[0]:ab[1] + 1]])
            else:
                order = lambda ab: min(math.dist(near, st[1:3]) for st in short[ab[0]:ab[1] + 1])
            for a, b in sorted(_runs(short), key=order):
                lay_ = run_layer(short, a)
                partner = [q for l_, pl in other_tr if l_ == lay_ for q in _dense(pl)] or \
                          [q for _, pl in other_tr for q in _dense(pl)]
                seg = [st[1:] for st in short[a:b + 1]]
                fine = [q for l_, pl in other_tr if l_ == lay_ for q in flatten(pl, 0.05)]
                new, got = add_bumps(seg, abs(lp - ln), partner, near=near,
                                     keep_off=(fine, width + min(gap, clearance)) if fine else None, **bump)
                if got > 1e-4:
                    rebuilt = short[:a] + [short[a]] + [("pt", *q) for q in new[1:-1]] + [short[b]] + short[b + 1:]
                    if lp < ln:
                        p_steps = rebuilt
                    else:
                        n_steps = rebuilt
                    added += got
                    done = True
                    break
            if not done:
                tune_note = ("tuning found no straight run long enough (2 mm or more) on either layer of the shorter "
                             "trace to add the remaining length")
                break
    p_steps, n_steps = clean_steps(p_steps), clean_steps(n_steps)
    p_tr, p_v = steps_to_geometry(p_steps, layer, other_layer)
    n_tr, n_v = steps_to_geometry(n_steps, layer, other_layer)
    necks = []
    if neck_down:
        flat_of = lambda tr: {l_: [q for ll, pl in tr if ll == l_ for q in flatten(pl, 0.05)] for l_, _ in tr}
        p_tw, nk = neck_down_fn(p_net, p_tr, obs, width, need, min_width, partner=flat_of(n_tr), gap=gap)
        necks += nk
        n_tw, nk = neck_down_fn(n_net, n_tr, obs, width, need, min_width, partner=flat_of(p_tr), gap=gap)
        necks += nk
    else:
        p_tw = [(l_, pl, width) for l_, pl in p_tr]
        n_tw = [(l_, pl, width) for l_, pl in n_tr]
    conflicts = []
    for net, tw, vs in ((p_net, p_tw, p_v), (n_net, n_tw, n_v)):
        for wd in sorted({w_ for _, _, w_ in tw}):
            conflicts += _check(net, [(l_, pl) for l_, pl, w_ in tw if w_ == wd], vs if wd == width else [],
                                wd, via_diameter, obs, need)
        if width not in {w_ for _, _, w_ in tw}:
            conflicts += _check(net, [], vs, width, via_diameter, obs, need)
    for cf in conflicts:
        if cf["with"] in (p_net, n_net) and "obstacle_at" in cf:
            # the partner's copper ALREADY on the board (a route left from before, a rip-up that missed
            # some), not the planned partner trace: say so, it reads like a tuning clash otherwise
            cf["note"] = (f"existing {cf['with']} copper on the board (not part of this plan) at {cf['obstacle_at']}: "
                          f"rip up {cf['with']} first, or route around it")
        if cf.get("layer") is not None and cf["with"] not in (p_net, n_net):
            mine_tr, other = (p_tr, n_tr) if cf["net"] == p_net else (n_tr, p_tr)
            partner = [q for l_, pl in other if l_ == cf["layer"] for q in flatten(pl, 0.1)]
            # judge it where the trace passes closest to the obstacle, not where it first enters
            ox, oy = cf.get("obstacle_at") or cf["at"]
            near = [q for l_, pl in mine_tr if l_ == cf["layer"] for q in flatten(pl, 0.02)
                    if math.dist(q, cf["at"]) < 1.0]
            x, y = min(near, key=lambda q: math.dist(q, (ox, oy))) if near else cf["at"]
            sq = explain_squeeze(x, y, cf["net"], cf["layer"], obs, width, need, min_width, partner, gap, neck_down)
            if sq:
                cf["squeeze"] = sq
    if same_net_gap:
        conflicts += same_net_spacing(p_net, p_tr, width, same_net_gap)
        conflicts += same_net_spacing(n_net, n_tr, width, same_net_gap)
    # P against N: coupled runs sit exactly at `gap`; anything closer is a short
    p_flat = [(l, flatten(pl, 0.1)) for l, pl in p_tr]
    n_flat = [(l, flatten(pl, 0.1)) for l, pl in n_tr]
    pair_min = min((_seg_dist(a, b, e, f) - width for lp_, pl in p_flat for ln_, nl in n_flat if lp_ == ln_
                    for a, b in zip(pl, pl[1:]) for e, f in zip(nl, nl[1:])), default=None)
    for vs, net, flats, onet in ((p_v, p_net, n_flat, n_net), (n_v, n_net, p_flat, p_net)):
        for v in vs:
            for _, nl in flats:
                for e, f in zip(nl, nl[1:]):
                    dd = _seg_dist(v, v, e, f) - via_diameter / 2 - width / 2
                    lim = max(clearance, pc) + margin              # the partner is in the pair's class
                    if dd < lim - 1e-4:
                        conflicts.append({"net": net, "at": list(v), "with": onet, "short_by_mm": round(lim - dd, 3)})
    for v in p_v:
        for w in n_v:
            dd = math.dist(v, w) - via_diameter
            if dd < via_clear - margin - 1e-4:
                conflicts.append({"net": p_net, "at": list(v), "with": n_net, "short_by_mm": round(via_clear - margin - dd, 3),
                                  "note": "the pair's vias are closer than the via clearance"})
    if pair_min is not None and pair_min < min(gap, clearance) - 1e-3:
        conflicts.append({"net": p_net, "with": n_net, "short_by_mm": round(min(gap, clearance) - pair_min, 3),
                          "note": "P and N closer than the pair gap on the same layer (crossed pads need a tail with a via)"})
    warnings = []
    if pc and gap < pc - 1e-6:
        conflicts.append({"net": p_net, "with": n_net, "short_by_mm": round(pc - gap, 3),
                          "note": f"the pair gap {gap} mm is below its net class clearance {pc} mm: DRC flags every "
                                  "coupled segment. Widen the gap or lower the class clearance"})
    elif pc and gap < pc + max(margin, 0.01) - 1e-6:
        warnings.append(f"the pair gap {gap} mm equals its class clearance {pc} mm: 45-degree segments can round "
                        f"just below it and fail DRC; use a gap of at least {pc + max(margin, 0.01):g} mm")
    p_via, n_via = barrel * len(p_v), barrel * len(n_v)
    lp = sum(length(pl) for _, pl in p_tr) + p_via
    ln = sum(length(pl) for _, pl in n_tr) + n_via
    if necks:
        warnings.append(f"{len(necks)} neck-down section(s): impedance rises there; check it with "
                        "estimate_impedance at the neck width")
    skew_ok = abs(lp - ln) <= max_skew_mm + 1e-6
    why = []
    if conflicts:
        why.append(f"{len(conflicts)} conflict(s)")
    if not skew_ok:
        why.append(f"skew {abs(lp - ln):.3f} mm is over max_skew_mm {max_skew_mm}" +
                   (": tuning is off (tune=false)" if not tune else f": {tune_note}" if tune_note else ""))
    tr_out = lambda tw: [{"layer": l, "points": [[round(v, 4) for v in q] for q in pl], "width": round(w_, 4)}
                         for l, pl, w_ in tw]
    return {"p": {"net": p_net, "traces": tr_out(p_tw), "vias": [[round(x, 4), round(y, 4)] for x, y in p_v],
                  "length_mm": round(lp, 3), "via_mm": round(p_via, 3)},
            "n": {"net": n_net, "traces": tr_out(n_tw), "vias": [[round(x, 4), round(y, 4)] for x, y in n_v],
                  "length_mm": round(ln, 3), "via_mm": round(n_via, 3)},
            "via_barrel_mm": round(barrel, 4), "via_depths_from": depths_from,
            "necks": necks,
            "skew_mm": round(lp - ln, 4), "skew_before_tuning_mm": skew_before, "tuning_added_mm": round(added, 4),
            "skew_ok": skew_ok, "crossed": crossed, "p_side": "left" if p_left else "right",
            "trunk_layer": trunk_layer,
            "rules": {"clearance_mm": round(clearance + margin, 4), "pair_class_clearance_mm": pc or None,
                      "same_net_gap_mm": same_net_gap or None,
                      "via_pair_spacing_mm": round(spacing, 4) if changes else None, "margin_mm": margin},
            "clearance_mm": clearance, "conflicts": conflicts, "warnings": warnings,
            "ok": not conflicts and skew_ok, "why_not_ok": "; ".join(why) or None}
