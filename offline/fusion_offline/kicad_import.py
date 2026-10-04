"""KiCad footprint (.kicad_mod) -> library part (library/schema.json format).

Used for footprints that already exist as KiCad files, including the ones
easyeda2kicad produced from EasyEDA/JLC data. Frame conversion: KiCad Y
points down, EAGLE Y points up, so y' = -y and rotations negate; the visual
orientation is unchanged, so EasyEDA-derived footprints stay JLC-native.

- smd pads -> package.smds (roundrect ratio -> roundness %)
- thru_hole pads -> package.pads (rect -> square, else round)
- np_thru_hole pads -> package.holes
- F.SilkS lines -> package.silk
- F.SilkS filled polygons and circles (KiCad's pin-1 triangle and dot) ->
  package.silk_polys / package.silk_circles
Pad names that EAGLE cannot hold (easyeda2kicad writes 1' and 2' for a
switch's second contact pair) are renamed to the next free numbers, and the
renames are returned so the caller's netlist can follow.
"""

from __future__ import annotations

import re


def sexp(text: str):
    toks = re.findall(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+', text)
    stack: list[list] = [[]]
    for t in toks:
        if t == "(":
            stack.append([])
        elif t == ")":
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(t[1:-1] if t.startswith('"') else t)
    return stack[0][0]


def _find(node, key):
    return [c for c in node if isinstance(c, list) and c and c[0] == key]


def _one(node, key):
    r = _find(node, key)
    return r[0] if r else None


def _r(v: float) -> float:
    return round(v + 0.0, 4)


def footprint_to_part(text: str, *, part_id: str, deviceset: str, prefix: str, device: str = "",
                      pin_names: dict[str, str] | None = None, attributes: dict[str, str] | None = None,
                      jlc_native: bool = False, source: str = "", description: str = "",
                      user_value: bool = False, directions: dict[str, str] | None = None) -> tuple[dict, dict]:
    """Returns (part, renames). pin_names maps pad -> symbol pin name (default:
    the pad name); pads sharing a pin name are connected to that one pin."""
    fp = sexp(text)
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(fp[1]).split(":")[-1])
    smds, pads, holes, silk = [], [], [], []
    used: set[str] = set()
    unconnected: set[str] = set()
    renames: dict[str, str] = {}
    raw = [p for p in _find(fp, "pad")]
    numeric = [int(p[1]) for p in raw if str(p[1]).isdigit()]
    nxt = (max(numeric) if numeric else 0) + 1
    for p in raw:
        num, kind, shape = str(p[1]), p[2], p[3]
        at = _one(p, "at")
        x, y = float(at[1]), -float(at[2])
        rot = (-float(at[3])) % 360 if len(at) > 3 else 0.0
        size = _one(p, "size")
        w, h = float(size[1]), float(size[2])
        drill = _one(p, "drill")
        dval = float(drill[-1]) if drill and len(drill) > 1 else None
        if kind == "np_thru_hole" or (kind == "thru_hole" and dval and max(w, h) <= dval + 1e-3):
            # a "plated" pad no bigger than its drill has no copper ring: a
            # locating-peg hole (EasyEDA exports RJ45 pegs this way)
            holes.append({"x": _r(x), "y": _r(y), "drill": dval or w})
            continue
        if not num:
            if kind == "smd":
                continue
            # unnamed plated pads are mounting/locating pegs: keep them as
            # unconnected pads (no symbol pin) so the hole is not lost
            mt = 1
            while f"MT{mt}" in used:
                mt += 1
            num = f"MT{mt}"
            unconnected.add(num)
        if not re.fullmatch(r"[A-Za-z0-9_$+\-.]+", num):
            renames.setdefault(num, str(nxt))
            if renames[num] == str(nxt):
                nxt += 1
            num = renames[num]
        if kind == "smd":
            rr = _one(p, "roundrect_rratio")
            smds.append({"name": num, "x": _r(x), "y": _r(y), "dx": _r(w), "dy": _r(h),
                         "roundness": int(round(float(rr[1]) * 200)) if rr else 0, "rot": rot})
        else:
            if shape in ("oval", "roundrect") and abs(w - h) > 1e-3 and max(w, h) / min(w, h) < 1.6:
                # a near-round oval (Mini-Fit's 2.7 x 3.3) as a LONG pad would be
                # stretched to 2:1 by the board-wide elongation rule and overlap
                # its neighbours: use a round pad on the short side
                pads.append({"name": num, "x": _r(x), "y": _r(y), "drill": dval or 0.8,
                             "diameter": _r(min(w, h)), "shape": "round", "rot": rot,
                             "kicad_size": [_r(w), _r(h)]})
            elif shape in ("oval", "roundrect") and abs(w - h) > 1e-3:
                # Fusion pads cannot take a per-pad elongation: a LONG pad is
                # `diameter` wide and (1 + psElongationLong) x long, set by the
                # design rule (100 % by default). Keep the narrow side and turn the
                # long axis to match the KiCad pad.
                pads.append({"name": num, "x": _r(x), "y": _r(y), "drill": dval or 0.8,
                             "diameter": _r(min(w, h)), "shape": "long",
                             "rot": (rot + (90 if h > w else 0)) % 360,
                             "kicad_size": [_r(w), _r(h)]})
            else:
                pads.append({"name": num, "x": _r(x), "y": _r(y), "drill": dval or 0.8,
                             "diameter": _r(max(w, h)), "shape": "square" if shape == "rect" else "round",
                             "rot": rot})
        used.add(num)
    for ln in _find(fp, "fp_line"):
        layer = _one(ln, "layer")
        if layer and layer[1] == "F.SilkS":
            s, e = _one(ln, "start"), _one(ln, "end")
            silk.append([_r(float(s[1])), _r(-float(s[2])), _r(float(e[1])), _r(-float(e[2]))])
    silk_polys, silk_circles = [], []
    for pl in _find(fp, "fp_poly"):
        layer = _one(pl, "layer")
        if layer and layer[1] == "F.SilkS":
            pts = [[_r(float(q[1])), _r(-float(q[2]))] for q in _find(_one(pl, "pts") or [], "xy")]
            if len(pts) >= 3:
                silk_polys.append(pts)
    for c in _find(fp, "fp_circle"):
        layer = _one(c, "layer")
        if layer and layer[1] == "F.SilkS":
            ctr, end = _one(c, "center"), _one(c, "end")
            fill = _one(c, "fill")
            cx, cy = float(ctr[1]), -float(ctr[2])
            r = ((float(end[1]) - float(ctr[1])) ** 2 + (float(end[2]) - float(ctr[2])) ** 2) ** 0.5
            silk_circles.append({"x": _r(cx), "y": _r(cy), "r": _r(r),
                                 "filled": bool(fill and len(fill) > 1 and fill[1] in ("yes", "solid"))})

    pin_names = pin_names or {}
    directions = directions or {}
    pins, seen = [], {}
    for pad in sorted(used - unconnected, key=lambda s: (len(s), s)):
        pname = pin_names.get(pad, pad)
        if pname in seen:
            seen[pname]["pads"].append(pad)
            continue
        entry = {"name": pname, "pad": pad, "pads": [pad], "direction": directions.get(pname, "pas")}
        seen[pname] = entry
        pins.append(entry)
    for i, p in enumerate(pins):
        p["side"] = "left" if i < (len(pins) + 1) // 2 else "right"
        p["pad"] = " ".join(p.pop("pads"))

    part = {
        "id": part_id, "deviceset": deviceset, "device": device, "description": description,
        "prefix": prefix, "user_value": user_value,
        "package": {"name": name, "smds": smds, "pads": pads, "holes": holes, "silk": silk, "silk_polys": silk_polys, "silk_circles": silk_circles,
                    "jlc_native": jlc_native},
        "symbol": {"name": deviceset, "pins": pins},
        "attributes": attributes or {},
        "metadata": {"source": source or f"kicad:{fp[1]}", "maintainer": "", "verified": False,
                     "notes": f"converted from KiCad footprint {fp[1]}"},
    }
    return part, renames
