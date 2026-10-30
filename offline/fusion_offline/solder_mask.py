"""Solder mask openings (offline): pads whose mask openings overlap, or leave a mask dam
(web) between them thinner than the fab can make.

Each pad's opening is its copper grown by the stop frame, as Fusion makes it: mvStopFrame x the
pad's smaller dimension (an SMD's dx/dy, a pad's diameter) clamped to [mlMinStopFrame,
mlMaxStopFrame]. Pads with stop="no" have none. Vias are tented unless their drill reaches
mlViaStopLimit. The dam between two openings is the copper gap minus both frames; at or below 0
the openings merge. Pads on the same part count (BGA balls, QFN pins), since that is where dams
go missing. JLC's minimum dam is 0.10 mm on green mask, 0.13 mm on black or white, 0.20 mm on
2 oz copper (jlc_capabilities); fabs often remove a dam they cannot make, leaving one opening
over both pads, which can let solder bridge pads of different nets.

Pad copper is a polygon circumscribing round shapes (16 sides), so a gap between round pads
can read a few um small.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict

from . import footprints as FP
from . import placement_check as PC
from .design import parse_board_design
from .stitch import _mm

MIN_DAM_MM = 0.10


def frame(rules: dict, size: float) -> float:
    """Stop-mask expansion for a pad whose smaller dimension (or diameter) is `size`."""
    mv = float(rules.get("mvStopFrame") or 1)
    return min(max(mv * size, _mm(rules.get("mlMinStopFrame"), 0.1016)), _mm(rules.get("mlMaxStopFrame"), 0.1016))


def check(root: ET.Element, min_dam_mm: float = MIN_DAM_MM, refs=None, examples: int = 5,
          stop_frame_mm: float | None = None) -> dict:
    """Pad pairs of two different nets whose mask openings merge or leave a dam under min_dam_mm,
    grouped by part pair. Pairs on one net, or with an unconnected pad, cannot bridge two nets:
    they are only counted. stop_frame_mm: one expansion for every pad instead of the rules' (to
    see what a smaller frame would fix)."""
    bd = parse_board_design(root)
    rules = bd.rules
    items = [p for p in PC.pads(root) if p.stop]
    limit = _mm(rules.get("mlViaStopLimit"), 999.0)
    for net, sig in bd.signals.items():
        for v in sig.vias:
            if v.drill >= limit - 1e-9:
                d = v.diameter or v.drill * 1.5
                poly = PC._circle(v.x, v.y, d / 2)
                items.append(PC.Pad("via", f"({v.x:g} {v.y:g})", net, False, frozenset({"top", "bottom"}),
                                    v.x, v.y, poly, PC._bbox(poly), d, True))
    fr = {id(p): frame(rules, p.size) if stop_frame_mm is None else stop_frame_mm for p in items}
    reach = 2 * max(fr.values(), default=0.0) + min_dam_mm
    g = PC._Grid()
    for p in items:
        g.add(p, p.bbox)
    want = set(refs) if refs else None
    groups = defaultdict(lambda: {"count": 0, "merged": 0, "dam_mm": float("inf"), "copper_gap_mm": None, "pairs": []})
    same_net = defaultdict(int)
    unconnected = 0
    seen = set()
    for a in items:
        if want and a.ref not in want:
            continue
        for b in g.near(a.bbox, reach):
            if b is a or not (a.sides & b.sides):
                continue
            key = (id(a), id(b)) if id(a) < id(b) else (id(b), id(a))
            if key in seen:
                continue
            seen.add(key)
            need = fr[id(a)] + fr[id(b)] + min_dam_mm
            if FP.separation(a.poly, b.poly) >= need - 1e-6:
                continue
            gap = FP.gap(a.poly, b.poly)
            dam = gap - fr[id(a)] - fr[id(b)]
            if dam >= min_dam_mm - 1e-6:
                continue
            if not (a.net and b.net):
                unconnected += 1
                continue
            if a.net == b.net:
                same_net[a.net] += 1
                continue
            (pa, pb) = sorted((a, b), key=lambda p: (p.ref, PC._natural(p.name)))
            k = (pa.ref, pb.ref)
            gr = groups[k]
            gr["count"] += 1
            gr["merged"] += dam <= 1e-6
            if dam < gr["dam_mm"]:
                gr["dam_mm"], gr["copper_gap_mm"] = dam, gap
            if len(gr["pairs"]) < examples:
                gr["pairs"].append({"a": f"{pa.ref}.{pa.name}", "b": f"{pb.ref}.{pb.name}", "nets": [pa.net, pb.net],
                                    "copper_gap_mm": round(gap, 4), "dam_mm": round(dam, 4),
                                    "at": [round((pa.x + pb.x) / 2, 3), round((pa.y + pb.y) / 2, 3)]})
    out = []
    for (ra, rb), gr in sorted(groups.items(), key=lambda kv: kv[1]["dam_mm"]):
        out.append({"parts": ra if ra == rb else f"{ra} / {rb}", "pairs": gr["count"], "merged": gr["merged"],
                    "worst_dam_mm": round(gr["dam_mm"], 4), "copper_gap_mm": round(gr["copper_gap_mm"], 4),
                    "examples": gr["pairs"]})
    frames = sorted({round(v, 4) for v in fr.values()})
    return {"ok": not out, "min_dam_mm": min_dam_mm, "stop_frames_mm": frames,
            "pairs": sum(o["pairs"] for o in out), "merged": sum(o["merged"] for o in out),
            "problems": out, "same_net_pairs": dict(sorted(same_net.items())), "unconnected_pairs": unconnected}
