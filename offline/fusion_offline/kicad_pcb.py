"""Read part placement from a KiCad board (.kicad_pcb) in Fusion's frame.

Frame mapping, verified against a real board built both ways:
- origin = bottom-left of the KiCad Edge.Cuts bounding box (Fusion boards
  are drawn from (0, 0) at bottom-left), and y flips: KiCad y points down;
- top parts keep their angle (both count counter-clockwise as seen from
  the top);
- bottom parts: KiCad flips a footprint top-to-bottom (mirror in y), EAGLE
  and Fusion mirror left-to-right (in x). A KiCad bottom part at angle R is
  therefore Fusion's mirrored part at R + 180.
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
                      "angle": round(((rot + 180) if bottom else rot) % 360, 4), "bottom": bottom,
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
        pads = []
        for p in _find(fp, "pad"):
            name = str(p[1])
            n = _one(p, "net")
            if not name or n is None or len(n) < 3:
                continue
            pads.append(name)
            nets.setdefault(n[2], []).append((ref, name))
        parts[ref] = {"footprint": str(fp[1]).split(":")[-1], "value": props.get("Value", ""), "pads": pads}
    return {"parts": parts, "nets": {k: sorted(set(v)) for k, v in nets.items()}}
