"""Length groups (offline): path lengths through series parts and checks against a group's
within-pair and between-member tolerances.

On the PoE board each 1000BASE-T pair runs T1 -> 0 ohm resistor (R18-R25) -> J2, so each side
is two nets (TP0_P on T1's side, N$30 on J2's). A path follows a net through a two-pin series
part (R, C, L, FB) into the next net when both nets join exactly two pads, and counts the
part's pad-to-pad distance too. Lengths are routed copper plus each via's barrel (the depth
between the copper layers the via joins on that net); a path with air wires left is marked
unrouted.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict

from .design import parse_board_design

SERIES = ("R", "C", "L", "FB")
DEFAULT_THICKNESS_MM = 1.6


def layer_depths(root: ET.Element, stack=None) -> tuple[dict[int, float], str]:
    """({export copper layer: depth of its mid-plane below the top, mm}, where from). From the V2
    stackup (fusion_offline.stackup.Stackup) when its copper count matches the board and every
    copper and dielectric layer has a thickness; else the copper layers spread evenly over the
    stackup's total thickness, or over 1.6 mm when there is no stackup."""
    copper = parse_board_design(root).copper_layers
    if stack is not None and len(stack.copper) == len(copper) and len(copper) >= 2:
        lay = stack.layers
        i0, i1 = lay.index(stack.copper[0]), lay.index(stack.copper[-1])
        inner = [l for l in lay[i0:i1 + 1] if l.kind in ("Signal", "Prepreg", "Core")]
        if all(l.thickness_mm for l in inner):
            out, z, k = {}, 0.0, 0
            for l in inner:
                if l.kind == "Signal":
                    out[copper[k]] = round(z + l.thickness_mm / 2, 4)
                    k += 1
                z += l.thickness_mm
            return out, f"stackup {stack.name or stack.source}".strip()
    total = stack.total_mm() if stack is not None and stack.total_mm() > 0 else None
    T = total or DEFAULT_THICKNESS_MM
    n = max(len(copper) - 1, 1)
    src = (f"stackup total {T} mm spread evenly (no per-layer thicknesses)" if total else
           f"assumed {T} mm board (no stackup read); pass the stackup for real via lengths")
    return {c: round(T * i / n, 4) for i, c in enumerate(copper)}, src


def via_barrels(root: ET.Element, net: str, depths: dict[int, float], board=None) -> list[dict]:
    """Each via of `net` with the copper layers it joins on that net (wire ends and SMD pads at
    the via) and its barrel length between the outermost two of them (0 when it joins fewer
    than two: a stub)."""
    from . import footprints as FP
    b = board or parse_board_design(root)
    s = b.signals[net]
    mine = set(s.contacts)
    smd = [(x, y, 16 if side == "bottom" else 1) for ref, pad, x, y, side in FP.pad_names(root)
           if side != "both" and (ref, pad) in mine] if s.vias else []
    out = []
    for v in s.vias:
        tol = max(v.drill / 2, 0.05)
        lays = {w.layer for w in s.wires if w.layer in depths and
                min(math.dist((v.x, v.y), (w.x1, w.y1)), math.dist((v.x, v.y), (w.x2, w.y2))) <= tol}
        lays |= {l for x, y, l in smd if math.dist((v.x, v.y), (x, y)) <= tol}
        zs = [depths[l] for l in lays if l in depths]
        out.append({"at": [v.x, v.y], "layers": sorted(lays),
                    "barrel_mm": round(max(zs) - min(zs), 4) if len(zs) >= 2 else 0.0})
    return out


def _prefix(ref: str) -> str:
    m = re.match(r"[A-Za-z]+", ref or "")
    return m.group(0).upper() if m else ""


def path(root: ET.Element, net: str, follow: bool = True, depths: dict[int, float] | None = None,
         skip=()) -> dict:
    """{nets, parts, length_mm, copper_mm, via_mm, parts_mm, unrouted} for the path starting at
    `net`. With follow, it goes through two-pin series parts both ways while each net joins
    exactly two pads. length_mm = copper + via barrels + series parts. depths: layer_depths
    (default: from the board alone). skip: nets on the path whose copper and vias are left out
    (a route about to be replaced)."""
    b = parse_board_design(root)
    if depths is None:
        depths = layer_depths(root)[0]
    if net not in b.signals:
        raise ValueError(f"no net {net!r} on the board")
    pads = defaultdict(list)                                   # part -> [(pad, net)]
    for s in b.signals.values():
        for el, pad in s.contacts:
            pads[el].append((pad, s.name))

    def through(n, came_from):
        """The series part on net n (not came_from) and the net on its other side, if any."""
        s = b.signals[n]
        if len(s.contacts) != 2:
            return None
        for el, pad in s.contacts:
            if el == came_from or _prefix(el) not in SERIES or len(pads[el]) != 2:
                continue
            other = next(nn for p_, nn in pads[el] if p_ != pad)
            if other and other != n and other in b.signals and len(b.signals[other].contacts) == 2:
                return el, other
        return None
    nets, parts = [net], []
    if follow:
        for direction in (0, 1):
            cur, came = net, None
            while True:
                step = through(cur, came)
                if step is None or step[1] in nets:
                    break
                el, nxt = step
                if direction == 0:
                    nets.append(nxt)
                    parts.append(el)
                else:
                    nets.insert(0, nxt)
                    parts.insert(0, el)
                cur, came = nxt, el
    copper, air, vias, unrouted = 0.0, 0.0, 0.0, False
    for n in nets:
        if n in skip:
            continue
        vias += sum(v["barrel_mm"] for v in via_barrels(root, n, depths, b))
        for w in b.signals[n].wires:
            if w.layer == 19:
                unrouted = True
                air += w.length
            else:
                copper += w.length
    parts_mm = 0.0
    for el in parts:
        (p1, n1), (p2, n2) = pads[el]
        a = _pad_xy(root, el, p1)
        c = _pad_xy(root, el, p2)
        if a and c:
            parts_mm += math.dist(a, c)
    return {"nets": nets, "parts": parts, "copper_mm": round(copper, 3), "via_mm": round(vias, 3),
            "parts_mm": round(parts_mm, 3), "length_mm": round(copper + vias + parts_mm, 3), "unrouted": unrouted, "airwire_mm": round(air, 3),
            "routed_fraction": round(copper / (copper + air), 3) if copper + air > 0 else 0.0}


def _pad_xy(root, el_name, pad_name):
    from . import footprints as FP
    for ref, pad, x, y, _ in FP.pad_names(root):
        if ref == el_name and pad == pad_name:
            return x, y
    return None


def evaluate(root: ET.Element, group: dict, depths: dict[int, float] | None = None) -> dict:
    """Check one group: {name, members: [[P, N] | net], intra_tol_mm, inter_tol_mm, target:
    "longest" | mm, measure: "pair_average" | "max", follow_series: bool}. depths: layer_depths
    for the via barrels (default: from the board alone)."""
    if depths is None:
        depths = layer_depths(root)[0]
    follow = group.get("follow_series", True)
    measure = group.get("measure", "pair_average")
    intra, inter = group.get("intra_tol_mm"), group.get("inter_tol_mm")
    rows = []
    for m in group["members"]:
        if isinstance(m, (list, tuple)):
            p, n = path(root, m[0], follow, depths), path(root, m[1], follow, depths)
            skew = round(p["length_mm"] - n["length_mm"], 3)
            L = max(p["length_mm"], n["length_mm"]) if measure == "max" else (p["length_mm"] + n["length_mm"]) / 2
            rows.append({"member": f"{m[0]}/{m[1]}", "nets": [m[0], m[1]], "p": p, "n": n, "length_mm": round(L, 3),
                         "skew_mm": skew, "routed_fraction": min(p["routed_fraction"], n["routed_fraction"]),
                         "intra_ok": None if intra is None else abs(skew) <= intra + 1e-9,
                         "unrouted": p["unrouted"] or n["unrouted"]})
        else:
            s = path(root, m, follow, depths)
            rows.append({"member": m, "nets": [m], "path": s, "length_mm": s["length_mm"], "unrouted": s["unrouted"],
                         "routed_fraction": s["routed_fraction"]})
    target = group.get("target", "longest")
    ref = max(r["length_mm"] for r in rows) if target == "longest" else float(target)
    for r in rows:
        r["delta_mm"] = round(r["length_mm"] - ref, 3)
        r["inter_ok"] = None if inter is None else abs(r["delta_mm"]) <= inter + 1e-9
        add = {}
        if r["delta_mm"] < 0 and r["inter_ok"] is False:
            need = -r["delta_mm"]
            if "p" in r:                                       # both sides to the target, which also ends the skew
                lp, ln = r["p"]["length_mm"], r["n"]["length_mm"]
                to = max(ref, lp, ln)
                add = {r["nets"][0]: round(to - lp, 3), r["nets"][1]: round(to - ln, 3)}
            else:
                add = {r["nets"][0]: round(need, 3)}
        elif "p" in r and r["intra_ok"] is False:
            add = {r["nets"][1] if r["skew_mm"] > 0 else r["nets"][0]: round(abs(r["skew_mm"]), 3)}
        r["add_mm"] = add
    fails = [r["member"] for r in rows if r.get("intra_ok") is False or r["inter_ok"] is False]
    return {"group": group["name"], "target_mm": round(ref, 3), "ok": not fails and not any(r["unrouted"] for r in rows),
            "failing": fails, "unrouted": [r["member"] for r in rows if r["unrouted"]],
            "partial": {r["member"]: r["routed_fraction"] for r in rows if r["unrouted"]},
            "note": ("lengths so far are routed copper (with via barrels and series parts); members still partly routed are "
                     "listed in partial with the routed share of their path, and cannot pass yet")
            if any(r["unrouted"] for r in rows) else None,
            "spread_mm": round(max(r["length_mm"] for r in rows) - min(r["length_mm"] for r in rows), 3),
            "members": rows}
