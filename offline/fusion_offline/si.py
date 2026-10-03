"""Signal-integrity checks from the board model: diff pairs, length match,
impedance estimates.

Impedance uses the IPC-2141 closed-form approximations (microstrip,
symmetric stripline, edge-coupled differential). They are fast estimates,
typically within about 10% of a field solver for common geometries, and
are reported as estimates. The dielectric constant is NOT stored in Fusion
designs, so it is always an explicit input (default 4.3 is labelled FR-4
typical, never silently assumed).
"""

from __future__ import annotations

import math
import re
import statistics

from .design import BoardDesign, Signal, Wire

_PAIR_PATTERNS = [
    (re.compile(r"^(.*)_P$"), "{}_N"),
    (re.compile(r"^(.*)_DP$"), "{}_DN"),
    (re.compile(r"^(.*)\+$"), "{}-"),
    (re.compile(r"^(.*[^_])P$"), "{}N"),
]


def find_diff_pairs(board: BoardDesign) -> list[tuple[str, str]]:
    pairs, used = [], set()
    for name in sorted(board.signals):
        if name in used:
            continue
        for pat, neg in _PAIR_PATTERNS:
            m = pat.match(name)
            if m and neg.format(m.group(1)) in board.signals:
                n = neg.format(m.group(1))
                pairs.append((name, n))
                used |= {name, n}
                break
    return pairs


def arc_centre(x1, y1, x2, y2, curve_deg):
    """(cx, cy, r) of an EAGLE arc from (x1, y1) to (x2, y2) sweeping curve_deg
    (positive = counter-clockwise)."""
    c = math.radians(curve_deg)
    L = math.hypot(x2 - x1, y2 - y1)
    r = L / (2 * abs(math.sin(c / 2)))
    nx, ny = -(y2 - y1) / L, (x2 - x1) / L
    k = math.copysign(r * math.cos(abs(c) / 2), c)
    return (x1 + x2) / 2 + nx * k, (y1 + y2) / 2 + ny * k, r


def _seg_point_dist(px, py, w: Wire) -> float:
    if abs(w.curve or 0.0) > 1e-6 and (w.x1, w.y1) != (w.x2, w.y2):
        cx, cy, r = arc_centre(w.x1, w.y1, w.x2, w.y2, w.curve)
        a0 = math.atan2(w.y1 - cy, w.x1 - cx)
        rel = (math.atan2(py - cy, px - cx) - a0) * (1 if w.curve > 0 else -1)
        if rel % (2 * math.pi) <= math.radians(abs(w.curve)) + 1e-9:
            return abs(math.hypot(px - cx, py - cy) - r)
        return min(math.hypot(px - w.x1, py - w.y1), math.hypot(px - w.x2, py - w.y2))
    dx, dy = w.x2 - w.x1, w.y2 - w.y1
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - w.x1) * dx + (py - w.y1) * dy) / L2))
    return math.hypot(px - (w.x1 + t * dx), py - (w.y1 + t * dy))


def pair_gap(p: Signal, n: Signal, step: float = 0.1) -> float | None:
    """The coupled edge-to-edge gap: P is sampled every `step` mm (arcs
    followed) against the nearest N copper on the same layer, and the gap
    value covering the most length wins (0.002 mm bins). A median over
    segments was skewed by short fan-in and chamfer segments."""
    bins: dict[int, int] = {}
    for w in p.wires:
        same = [v for v in n.wires if v.layer == w.layer]
        if not same or w.length < 1e-6:
            continue
        k = max(1, int(w.length / step))
        if abs(w.curve or 0.0) > 1e-6:
            cx, cy, r = arc_centre(w.x1, w.y1, w.x2, w.y2, w.curve)
            a0 = math.atan2(w.y1 - cy, w.x1 - cx)
            pts = [(cx + r * math.cos(a0 + math.radians(w.curve) * i / k),
                    cy + r * math.sin(a0 + math.radians(w.curve) * i / k)) for i in range(k)]
        else:
            pts = [(w.x1 + (w.x2 - w.x1) * i / k, w.y1 + (w.y2 - w.y1) * i / k) for i in range(k)]
        for x, y in pts:
            d = min(_seg_point_dist(x, y, v) for v in same) - (w.width + same[0].width) / 2
            if d > 0:
                key = round(d / 0.002)
                bins[key] = bins.get(key, 0) + 1
    if not bins:
        return None
    return round(max(bins, key=bins.get) * 0.002, 4)


def pair_report(board: BoardDesign, p_name: str, n_name: str, max_skew_mm: float | None = None) -> dict:
    p, n = board.signals[p_name], board.signals[n_name]
    skew = abs(p.length - n.length)
    limit, source = max_skew_mm, "argument"
    if limit is None:
        v = board.rules.get("dpMaxLengthDifference")
        limit, source = (_mm(v) if v else None), "design rule dpMaxLengthDifference"
        if limit == 10.0:
            # 10 mm is EAGLE's default, not an intent; don't call a pair "within limit" against it
            limit, source = None, "none (design rule is the 10 mm EAGLE default; pass max_skew_mm)"
    widths = sorted({round(w.width, 4) for w in p.wires + n.wires})
    return {
        "p": p_name, "n": n_name,
        "length_p_mm": round(p.length, 3), "length_n_mm": round(n.length, 3),
        "skew_mm": round(skew, 3), "skew_limit_mm": limit, "skew_limit_source": source,
        "longer": (p_name if p.length > n.length else n_name) if skew > 1e-6 else None,
        "within_limit": None if limit in (None, 0) else skew <= limit,
        "widths_mm": widths, "gap_mm": pair_gap(p, n),
        "vias": [len(p.vias), len(n.vias)],
        "layers": sorted({w.layer for w in p.wires + n.wires}),
        "net_class": [p.net_class, n.net_class],
    }


def _mm(v: str) -> float:
    m = re.search(r"-?\d+(?:\.\d+)?", v or "")
    if not m:
        return 0.0
    x = float(m.group(0))
    return x * 0.0254 if v.strip().endswith("mil") else x


def microstrip_z0(w: float, h: float, t: float, er: float) -> float:
    """IPC-2141 surface microstrip, valid roughly for 0.1 < w/h < 2.0."""
    return 87.0 / math.sqrt(er + 1.41) * math.log(5.98 * h / (0.8 * w + t))


def stripline_z0(w: float, b: float, t: float, er: float) -> float:
    """IPC-2141 symmetric stripline; b = plane-to-plane dielectric thickness."""
    return 60.0 / math.sqrt(er) * math.log(4 * b / (0.67 * math.pi * (0.8 * w + t)))


def diff_microstrip(w: float, s: float, h: float, t: float, er: float) -> float:
    return 2 * microstrip_z0(w, h, t, er) * (1 - 0.48 * math.exp(-0.96 * s / h))


def diff_stripline(w: float, s: float, b: float, t: float, er: float) -> float:
    return 2 * stripline_z0(w, b, t, er) * (1 - 0.347 * math.exp(-2.9 * s / b))


def estimate_impedance(width_mm: float, dielectric_mm: float, copper_mm: float, er: float,
                       geometry: str = "microstrip", gap_mm: float | None = None) -> dict:
    """geometry: 'microstrip' (outer layer over a plane) or 'stripline'
    (inner layer between two planes, dielectric_mm = plane-to-plane)."""
    if geometry == "microstrip":
        z0 = microstrip_z0(width_mm, dielectric_mm, copper_mm, er)
        zd = diff_microstrip(width_mm, gap_mm, dielectric_mm, copper_mm, er) if gap_mm else None
    elif geometry == "stripline":
        z0 = stripline_z0(width_mm, dielectric_mm, copper_mm, er)
        zd = diff_stripline(width_mm, gap_mm, dielectric_mm, copper_mm, er) if gap_mm else None
    else:
        raise ValueError("geometry must be 'microstrip' or 'stripline'")
    return {"z0_ohm": round(z0, 1), "zdiff_ohm": round(zd, 1) if zd else None,
            "method": f"IPC-2141 {geometry} closed form (estimate, typically within ~10%)",
            "inputs": {"width_mm": width_mm, "dielectric_mm": dielectric_mm, "copper_mm": copper_mm,
                       "er": er, "gap_mm": gap_mm}}


def dominant_layer(sig: Signal) -> int | None:
    by = sig.length_by_layer()
    return max(by, key=by.get) if by else None


def pair_impedance(board: BoardDesign, stack, p_name: str, n_name: str) -> dict:
    """Estimate a routed pair's impedance on the layer carrying most of its
    length, using the real stackup. Board export layers (1, 2, 15, 16) map to
    stackup copper layers by stack position. Outer layers are treated as
    microstrip over the adjacent dielectric; inner layers as symmetric
    stripline between their two neighbouring dielectrics (an approximation
    when the stack is not symmetric about the layer)."""
    p, n = board.signals[p_name], board.signals[n_name]
    layer = dominant_layer(p)
    copper = board.copper_layers
    if layer is None or layer not in copper:
        return {"p": p_name, "n": n_name, "error": "not routed on a copper layer in the stack"}
    idx = copper.index(layer)
    if idx >= len(stack.copper):
        return {"p": p_name, "n": n_name, "error": "board copper layers and stackup do not match"}
    cu = stack.copper[idx]
    up, down = stack.neighbours(idx)
    widths = [w.width for w in p.wires + n.wires if w.layer == layer and w.length > 0.2]
    width = statistics.mode([round(w, 4) for w in widths]) if widths else None
    gap = pair_gap(p, n)
    t = cu.thickness_mm or 0.035
    if not width:
        return {"p": p_name, "n": n_name, "error": "no routed segments to measure"}
    if idx in (0, len(stack.copper) - 1):
        d = down if idx == 0 else up
        if d is None or not d.thickness_mm or not d.er:
            return {"p": p_name, "n": n_name, "error": f"no thickness/Er for the dielectric under {cu.name}"}
        est = estimate_impedance(width, d.thickness_mm, t, d.er, "microstrip", gap)
        ref = f"{cu.name} over {d.name} ({d.thickness_mm} mm, Er {d.er})"
    else:
        if not (up and down and up.thickness_mm and down.thickness_mm and up.er and down.er):
            return {"p": p_name, "n": n_name, "error": f"missing dielectric data around {cu.name}"}
        b = up.thickness_mm + down.thickness_mm + t
        er = (up.er * up.thickness_mm + down.er * down.thickness_mm) / (up.thickness_mm + down.thickness_mm)
        est = estimate_impedance(width, b, t, round(er, 3), "stripline", gap)
        ref = f"{cu.name} between {up.name} and {down.name} (b {round(b, 3)} mm, Er {round(er, 2)})"
    out = {"p": p_name, "n": n_name, "layer": cu.name, "reference": ref, "width_mm": width,
           "gap_mm": gap, "length_mm": round(p.length, 2), "zdiff_ohm": est["zdiff_ohm"],
           "z0_ohm": est["z0_ohm"], "method": est["method"]}
    if p.length < 6.0:
        out["note"] = ("short pair (likely a breakout or connector fan-out); a local impedance "
                       "excursion over a few mm matters far less than on the main run")
    return out
