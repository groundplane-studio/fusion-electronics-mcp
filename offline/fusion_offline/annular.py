"""Annular rings (offline): every via and through-hole pad's copper against its drill.

The ring here is the full difference, copper diameter - drill (as the user states it): a 0.35 mm
via on a 0.20 mm drill has 0.15 mm. The radial ring, half that, is reported alongside. The
minimum grows with the drill in proportion to the user's sizes (2026-10-21): 0.35/0.20,
0.43/0.25, 0.55/0.30, 0.70/0.35 mm. Between two sizes the required diameter is interpolated,
below the smallest drill it scales with that size's ratio, above the largest it keeps that
size's ring (0.35 mm: scaling a 2.7 mm mounting hole by 2x would ask for a 5.4 mm pad), and it
is never under drill + min_ring.

Copper per layer is computed as Fusion does: restring rv * drill clamped to [rlMin, rlMax]
(stitch._ring, per layer: rvPadTop / rvPadInner / rvPadBottom, rvViaOuter / rvViaInner), and
the library pad or via diameter where the design's useDiameter bits say it is used. The bits
(1 pad top, 2 pad inner, 4 pad bottom, 8 via outer, 16 via inner) are read from the order of
Fusion's Restring tab; the RV1126B SoM's 29 (all but pad inner) is the EAGLE default, where
pads on inner layers take their size from the restring only.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict

from . import footprints as FP
from . import placement_check as PC
from .design import parse_board_design
from .stitch import _mm

DEFAULT_SIZES = {0.20: 0.35, 0.25: 0.43, 0.30: 0.55, 0.35: 0.70}      # drill: diameter (mm)
MIN_RING_MM = 0.15
USE_DIAMETER_DEFAULT = 29
_BITS = {("Pad", "Top"): 1, ("Pad", "Inner"): 2, ("Pad", "Bottom"): 4, ("Via", "Outer"): 8, ("Via", "Inner"): 16}


def required_diameter(drill: float, min_ring: float = MIN_RING_MM, sizes: dict | None = None) -> float:
    """Smallest copper diameter for this drill: drill + min_ring, or more where the sizes scale up."""
    pts = sorted((float(d), float(c)) for d, c in (DEFAULT_SIZES if sizes is None else sizes).items())
    need = drill + min_ring
    if pts:
        if drill <= pts[0][0]:
            scaled = drill * pts[0][1] / pts[0][0]
        elif drill >= pts[-1][0]:
            scaled = drill + pts[-1][1] - pts[-1][0]
        else:
            (d0, c0), (d1, c1) = next((a, b) for a, b in zip(pts, pts[1:]) if a[0] <= drill <= b[0])
            scaled = c0 + (c1 - c0) * (drill - d0) / (d1 - d0)
        need = max(need, scaled)
    return round(need, 4)


def _restring(rules: dict, kind: str, layer: str, drill: float) -> float:
    rv = _mm(rules.get(f"rv{kind}{layer}"), 0.25)
    lo = _mm(rules.get(f"rlMin{kind}{layer}"), 0.0)
    hi = _mm(rules.get(f"rlMax{kind}{layer}"), 0.508)
    return min(max(rv * drill, lo), hi)


def copper(rules: dict, kind: str, drill: float, diameter: float, inner: bool = True) -> dict[str, float]:
    """Copper diameter per layer group ({"top", "bottom", "inner"} for pads, {"outer", "inner"} for vias)."""
    use = int(float(rules.get("useDiameter") or USE_DIAMETER_DEFAULT))
    groups = (("Top", "Bottom", "Inner") if kind == "Pad" else ("Outer", "Inner"))
    out = {}
    for g in groups:
        if g == "Inner" and not inner:
            continue
        d = drill + 2 * _restring(rules, kind, g, drill)
        if use & _BITS[(kind, g)]:
            d = max(d, diameter)
        out[g.lower()] = round(d, 4)
    return out


def check(root: ET.Element, min_ring: float = MIN_RING_MM, sizes: dict | None = None, examples: int = 5) -> dict:
    """Vias and through-hole pads whose copper is under the required diameter on any layer,
    grouped by size, and a table of every size on the board."""
    bd = parse_board_design(root)
    rules = bd.rules
    inner = len(bd.copper_layers) > 2
    board = root.find("./drawing/board")
    smd = [p for p in PC.pads(root) if p.smd]          # to say when a via sits in an SMD pad

    def in_pad(x, y):
        for p in smd:
            b = p.bbox
            if b[0] <= x <= b[2] and b[1] <= y <= b[3] and FP.contains(p.poly, [(x, y)], tol=0.0):
                return f"{p.ref}.{p.name}"
        return None

    groups = defaultdict(lambda: {"count": 0, "nets": set(), "at": [], "in_pad": 0})
    for net, sig in bd.signals.items():
        for v in sig.vias:
            cu = copper(rules, "Via", v.drill, v.diameter, inner)
            g = groups[("via", v.drill, v.diameter, tuple(sorted(cu.items())))]
            g["count"] += 1
            g["nets"].add(net)
            ip = in_pad(v.x, v.y)
            g["in_pad"] += ip is not None
            if len(g["at"]) < examples:
                g["at"].append({"x": round(v.x, 4), "y": round(v.y, 4), "net": net, **({"in_pad": ip} if ip else {})})
    pad_net = {(c.get("element"), c.get("pad")): s.get("name")
               for s in board.iterfind("./signals/signal") for c in s.iterfind("contactref")}
    pkgs = FP.packages(board)
    for el in board.iterfind("./elements/element"):
        pk = pkgs.get((el.get("library"), el.get("package")))
        if pk is None:
            continue
        xf, _, _ = FP.transform(el)
        for p in pk.iterfind("pad"):
            drill, dia = float(p.get("drill") or 0), float(p.get("diameter") or 0)
            if drill <= 0:
                continue
            cu = copper(rules, "Pad", drill, dia, inner)
            g = groups[("pad", drill, dia, tuple(sorted(cu.items())))]
            g["count"] += 1
            net = pad_net.get((el.get("name"), p.get("name")))
            if net:
                g["nets"].add(net)
            if len(g["at"]) < examples:
                x, y = xf(float(p.get("x") or 0), float(p.get("y") or 0))
                g["at"].append({"x": round(x, 4), "y": round(y, 4), "pad": f"{el.get('name')}.{p.get('name')}",
                                **({"net": net} if net else {})})
    sizes_seen, failures = [], []
    for (kind, drill, dia, cu), g in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
        cu = dict(cu)
        need = required_diameter(drill, min_ring, sizes)
        worst_layer = min(cu, key=cu.get)
        worst = cu[worst_layer]
        row = {"kind": kind, "drill_mm": drill, "diameter_mm": dia or None, "copper_mm": cu, "count": g["count"],
               "ring_mm": round(worst - drill, 4), "radial_ring_mm": round((worst - drill) / 2, 4),
               "required_diameter_mm": need, "ok": worst >= need - 1e-6}
        if dia == 0:
            row["note"] = "diameter 0: sized by the restring rules"
        if g["in_pad"]:
            row["in_smd_pad"] = g["in_pad"]
        sizes_seen.append(row)
        if not row["ok"]:
            short = {k: round(need - v, 4) for k, v in cu.items() if v < need - 1e-6}
            failures.append({**row, "short_by_mm": short, "nets": sorted(g["nets"])[:10],
                             "more_nets": max(0, len(g["nets"]) - 10), "at": g["at"]})
    n_bad = sum(f["count"] for f in failures)
    return {"ok": not failures, "failing": n_bad,
            "checked": sum(r["count"] for r in sizes_seen),
            "rule": (f"copper diameter - drill >= {min_ring:g} mm on every layer, scaled up with the drill "
                     f"({', '.join(f'{c:g}/{d:g}' for d, c in sorted((DEFAULT_SIZES if sizes is None else sizes).items()))} mm)"),
            "failures": failures, "sizes": sizes_seen}
