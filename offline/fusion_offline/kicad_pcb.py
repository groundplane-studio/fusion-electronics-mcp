"""Read part placement from a KiCad board (.kicad_pcb) in Fusion's frame.

Frame mapping, verified against a real board built both ways:
- origin = bottom-left of the KiCad Edge.Cuts bounding box (Fusion boards
  are drawn from (0, 0) at bottom-left), and y flips: KiCad y points down;
- top parts keep their angle (both count counter-clockwise as seen from
  the top);
- bottom parts: KiCad flips a footprint top-to-bottom (mirror in y) and stores
  the angle negated; EAGLE and Fusion mirror left-to-right (in x). A KiCad
  bottom part at angle R is therefore Fusion's mirrored part at 180 - R
  (checked pad for pad against pcbnew at R = 0 and R = 90; R + 180, used
  before, is only right at 0 and 180 and swaps the pads at 90 and 270).
"""

from __future__ import annotations

from .kicad_import import _find, _one, sexp


def read_placement(text: str) -> dict:
    root = sexp(text)
    xs, ys = [], []
    for g in _find(root, "gr_rect") + _find(root, "gr_line") + _find(root, "gr_arc"):
        layer = _one(g, "layer")
        if layer and layer[1] == "Edge.Cuts":
            for k in ("start", "end", "mid"):
                v = _one(g, k)
                if v:
                    xs.append(float(v[1]))
                    ys.append(float(v[2]))
    if not xs:
        raise ValueError("the KiCad board has no Edge.Cuts outline")
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    height = y1 - y0
    parts = {}
    for fp in _find(root, "footprint"):
        ref = next((p[2] for p in _find(fp, "property") if len(p) > 2 and p[1] == "Reference"), None)
        if ref is None:
            fpt = next((t[2] for t in _find(fp, "fp_text") if len(t) > 2 and t[1] == "reference"), None)
            ref = fpt
        if not ref:
            continue
        at = _one(fp, "at")
        rot = float(at[3]) if len(at) > 3 else 0.0
        bottom = _one(fp, "layer")[1] == "B.Cu"
        parts[ref] = {"x_mm": round(float(at[1]) - x0, 4), "y_mm": round(height - (float(at[2]) - y0), 4),
                      "angle": round(((180 - rot) if bottom else rot) % 360, 4), "bottom": bottom,
                      "footprint": str(fp[1]), "kicad_angle": rot}
    return {"board_mm": [round(x1 - x0, 4), round(height, 4)], "parts": parts}


def read_netlist(text: str) -> dict:
    """{'parts': {ref: {'footprint', 'value', 'pads': [pad...]}}, 'nets': {net: [(ref, pad)]}}
    from a .kicad_pcb: every pad's net assignment. Unnamed pads (mounting
    holes) and net-less pads are skipped; KiCad's auto names (Net-(...)) are
    kept, so connectivity is exact even where nets are unnamed."""
    root = sexp(text)
    parts, nets = {}, {}
    for fp in _find(root, "footprint"):
        props = {p[1]: p[2] for p in _find(fp, "property") if len(p) > 2}
        ref = props.get("Reference")
        if not ref:
            continue
        import math
        at = _one(fp, "at")
        fx, fy = float(at[1]), float(at[2])
        a = math.radians(float(at[3]) if len(at) > 3 else 0.0)
        pads, pad_xy = [], {}
        for p in _find(fp, "pad"):
            name = str(p[1])
            n = _one(p, "net")
            if not name or n is None or len(n) < 3:
                continue
            pads.append(name)
            nets.setdefault(n[2], []).append((ref, name))
            pa = _one(p, "at")
            px, py = float(pa[1]), float(pa[2])
            pad_xy[name] = (fx + px * math.cos(a) + py * math.sin(a), fy - px * math.sin(a) + py * math.cos(a))
        parts[ref] = {"footprint": str(fp[1]).split(":")[-1], "value": props.get("Value", ""), "pads": pads,
                      "xy": (fx, fy), "pad_xy": pad_xy}
    return {"parts": parts, "nets": {k: sorted(set(v)) for k, v in nets.items()}}


def _frame(root):
    xs, ys = [], []
    for g in _find(root, "gr_rect") + _find(root, "gr_line") + _find(root, "gr_arc"):
        layer = _one(g, "layer")
        if layer and layer[1] == "Edge.Cuts":
            for k in ("start", "end", "mid"):
                v = _one(g, k)
                if v:
                    xs.append(float(v[1]))
                    ys.append(float(v[2]))
    if not xs:
        raise ValueError("the KiCad board has no Edge.Cuts outline")
    x0, y0, y1 = min(xs), min(ys), max(ys)
    return lambda x, y: (round(float(x) - x0, 4), round((y1 - y0) - (float(y) - y0), 4))


def read_routing(text: str) -> dict:
    """Tracks, arcs and vias of a KiCad board in Fusion's frame (see read_placement):
    {'segments': [(net, 'top'|'bottom'|layer, width, (x1, y1), (x2, y2))],
     'arcs': [(net, layer, width, start, end, angle_deg)], 'vias': [(net, x, y, drill, size)]}.
    Arc angles are + for counter-clockwise as seen from the top in Fusion's frame."""
    import math
    root = sexp(text)
    T = _frame(root)
    names = {str(n[1]): n[2] for n in _find(root, "net") if len(n) > 2}

    def net_of(item):
        n = _one(item, "net")
        if n is None or len(n) < 2:
            return None
        v = str(n[1])
        return names.get(v, v) or None

    def layer(item):
        lay = _one(item, "layer")[1]
        return {"F.Cu": "top", "B.Cu": "bottom"}.get(lay, lay)

    segs, arcs, vias = [], [], []
    for s in _find(root, "segment"):
        segs.append((net_of(s), layer(s), float(_one(s, "width")[1]),
                     T(*_one(s, "start")[1:3]), T(*_one(s, "end")[1:3])))
    for a in _find(root, "arc"):
        p1, pm, p2 = T(*_one(a, "start")[1:3]), T(*_one(a, "mid")[1:3]), T(*_one(a, "end")[1:3])
        # signed sweep from start through mid to end
        ax, ay = p1[0] - pm[0], p1[1] - pm[1]
        bx, by = p2[0] - pm[0], p2[1] - pm[1]
        d = 2 * (p1[0] * (pm[1] - p2[1]) + pm[0] * (p2[1] - p1[1]) + p2[0] * (p1[1] - pm[1]))
        ux = ((p1[0] ** 2 + p1[1] ** 2) * (pm[1] - p2[1]) + (pm[0] ** 2 + pm[1] ** 2) * (p2[1] - p1[1]) + (p2[0] ** 2 + p2[1] ** 2) * (p1[1] - pm[1])) / d
        uy = ((p1[0] ** 2 + p1[1] ** 2) * (p2[0] - pm[0]) + (pm[0] ** 2 + pm[1] ** 2) * (p1[0] - p2[0]) + (p2[0] ** 2 + p2[1] ** 2) * (pm[0] - p1[0])) / d
        a1 = math.atan2(p1[1] - uy, p1[0] - ux)
        am = math.atan2(pm[1] - uy, pm[0] - ux)
        a2 = math.atan2(p2[1] - uy, p2[0] - ux)
        ccw = (am - a1) % (2 * math.pi) < (a2 - a1) % (2 * math.pi)
        sweep = (a2 - a1) % (2 * math.pi) if ccw else -((a1 - a2) % (2 * math.pi))
        arcs.append((net_of(a), layer(a), float(_one(a, "width")[1]), p1, p2, round(math.degrees(sweep), 3)))
    for v in _find(root, "via"):
        x, y = T(*_one(v, "at")[1:3])
        vias.append((net_of(v), x, y, float(_one(v, "drill")[1]), float(_one(v, "size")[1])))
    return {"segments": segs, "arcs": arcs, "vias": vias}


def read_pads(text: str) -> dict:
    """{ref: {'bottom': bool, 'pads': {pad name: (x, y)}}}: every named pad's centre in Fusion's
    frame (see read_placement). Pads sharing a name (a QFN's exposed pad split up, thermal-via
    pads) give their centroid. KiCad stores a pad's position in the footprint's frame; the
    board position is the footprint's plus that offset turned by the footprint's angle
    (counter-clockwise as seen from the top, in KiCad's y-down coordinates)."""
    import math
    root = sexp(text)
    T = _frame(root)
    out = {}
    for fp in _find(root, "footprint"):
        ref = next((p[2] for p in _find(fp, "property") if len(p) > 2 and p[1] == "Reference"), None)
        if not ref:
            continue
        at = _one(fp, "at")
        fx, fy = float(at[1]), float(at[2])
        a = math.radians(float(at[3]) if len(at) > 3 else 0.0)
        acc = {}
        for p in _find(fp, "pad"):
            name = str(p[1])
            if not name:
                continue
            pa = _one(p, "at")
            px, py = float(pa[1]), float(pa[2])
            x = fx + px * math.cos(a) + py * math.sin(a)
            y = fy - px * math.sin(a) + py * math.cos(a)
            acc.setdefault(name, []).append(T(x, y))
        out[ref] = {"bottom": _one(fp, "layer")[1] == "B.Cu",
                    "pads": {n: (round(sum(q[0] for q in v) / len(v), 4), round(sum(q[1] for q in v) / len(v), 4))
                             for n, v in acc.items()}}
    return out


def package_pads(pkg) -> dict:
    """{pad name: (x, y)} of an EAGLE <package> element (SMDs and through-hole pads; pads
    sharing a name give their centroid)."""
    acc = {}
    for p in list(pkg.iterfind("smd")) + list(pkg.iterfind("pad")):
        acc.setdefault(p.get("name"), []).append((float(p.get("x")), float(p.get("y"))))
    return {n: (sum(q[0] for q in v) / len(v), sum(q[1] for q in v) / len(v)) for n, v in acc.items()}


def fit_pose(local: dict, target: dict, mirror: bool) -> dict | None:
    """Where a part must go so its package pads (`local`, package frame) land on `target`
    (board positions by pad name): EAGLE places a pad at T + M(R(angle) p), M flipping x when
    mirrored. Least squares over the shared pad names (2-D Procrustes), so a footprint whose
    origin or pin-1 orientation differs from the reference board's still lands pad-for-pad.
    None when fewer than two pads are shared. The residual says whether the footprints agree."""
    import math
    names = [n for n in local if n in target]
    if len(names) < 2:
        return None
    sgn = -1.0 if mirror else 1.0
    P = [local[n] for n in names]
    Q = [(sgn * target[n][0], target[n][1]) for n in names]       # M applied to the targets
    pc = (sum(p[0] for p in P) / len(P), sum(p[1] for p in P) / len(P))
    qc = (sum(q[0] for q in Q) / len(Q), sum(q[1] for q in Q) / len(Q))
    s = c = 0.0
    for (px, py), (qx, qy) in zip(P, Q):
        px, py, qx, qy = px - pc[0], py - pc[1], qx - qc[0], qy - qc[1]
        c += px * qx + py * qy
        s += px * qy - py * qx
    a = math.atan2(s, c)
    deg = math.degrees(a) % 360
    snap = round(deg / 90) * 90 % 360
    if abs((deg - snap + 180) % 360 - 180) < 0.05:
        deg, a = float(snap), math.radians(snap)
    ca, sa = math.cos(a), math.sin(a)
    tx = qc[0] - (ca * pc[0] - sa * pc[1])
    ty = qc[1] - (sa * pc[0] + ca * pc[1])
    errs = []
    for n, (px, py) in zip(names, P):
        wx = sgn * (tx + ca * px - sa * py)
        wy = ty + sa * px + ca * py
        errs.append(math.dist((wx, wy), target[n]))
    return {"x_mm": round(sgn * tx, 4), "y_mm": round(ty, 4), "angle": round(deg, 2), "mirror": mirror,
            "pads": len(names), "worst_mm": round(max(errs), 4)}
