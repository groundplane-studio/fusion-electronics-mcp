"""Standard two-pin schematic symbols: 7.62 mm pin to pin (like a capacitor),
the left pin on the origin, short pins, the drawing between x = 2.54 and 5.08,
>NAME at (0, 5.08) and >VALUE at (0, 2.54).

standardise() turns an existing two-pin symbol into that shape without
changing how it looks: the drawing between the pins is squeezed (x only) into
the body space and the pins are MOVEd, never deleted, so every device's
CONNECTs survive. The script it returns runs in Fusion's library editor; the
expected result is returned too, for checking against the library export.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET

from . import commands as C

LENGTH = 7.62
PIN = 2.54
NAME_AT = (0.0, 5.08)
VALUE_AT = (0.0, 2.54)
_LEN = {"point": 0.0, "short": 2.54, "middle": 5.08, "long": 7.62}


def _f(el, k, d=0.0):
    v = el.get(k)
    return float(v) if v not in (None, "") else d


def _arc_mid(x1, y1, x2, y2, curve):
    """Midpoint of an EAGLE arc from (x1, y1) to (x2, y2) sweeping `curve` degrees (+ = CCW)."""
    if not curve:
        return ((x1 + x2) / 2, (y1 + y2) / 2)
    a = math.radians(curve)
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    dx, dy = x2 - x1, y2 - y1
    chord = math.hypot(dx, dy)
    r = chord / (2 * math.sin(abs(a) / 2))
    h = math.sqrt(max(r * r - (chord / 2) ** 2, 0.0))
    # centre lies to the left of the chord for a CCW sweep < 180, to the right for CW
    nx, ny = -dy / chord, dx / chord
    s = 1 if (curve > 0) == (abs(curve) < 180) else -1
    cx, cy = mx + s * nx * h, my + s * ny * h
    a1 = math.atan2(y1 - cy, x1 - cx)
    am = a1 + a / 2
    return (cx + r * math.cos(am), cy + r * math.sin(am))


def is_two_pin_horizontal(sym: ET.Element) -> bool:
    pins = sym.findall("pin")
    return len(pins) == 2 and all(abs(_f(p, "y")) < 1e-6 for p in pins) and \
        {(p.get("rot") or "R0") for p in pins} == {"R0", "R180"}


def standardise(sym: ET.Element) -> tuple[str, dict] | None:
    """(script, expected) for one symbol, or None when it is already standard or not a
    horizontal two-pin symbol."""
    if not is_two_pin_horizontal(sym):
        return None
    pins = sorted(sym.findall("pin"), key=lambda p: _f(p, "x"))
    left, right = pins
    if (left.get("rot") or "R0") != "R0":          # the left pin must point left
        return None
    ll, lr = _LEN.get(left.get("length", "long"), 7.62), _LEN.get(right.get("length", "long"), 7.62)
    if abs(_f(left, "x")) < 1e-6 and abs(_f(right, "x") - LENGTH) < 1e-6:
        return None
    # the drawing's extent (everything but >NAME/>VALUE)
    xs = []
    for el in sym:
        if el.tag == "wire":
            xs += [_f(el, "x1"), _f(el, "x2")]
        elif el.tag == "rectangle":
            xs += [_f(el, "x1"), _f(el, "x2")]
        elif el.tag == "circle":
            xs += [_f(el, "x") - _f(el, "radius"), _f(el, "x") + _f(el, "radius")]
        elif el.tag == "polygon":
            xs += [_f(v, "x") for v in el.iterfind("vertex")]
    if not xs:
        xs = [_f(left, "x") + ll, _f(right, "x") - lr]
    a0, a1 = min(xs), max(xs)
    w = a1 - a0
    # narrow drawings sit between short pins like a capacitor; wider ones keep their
    # size (up to 5.08 mm, scaled down beyond) between zero-length pins with drawn leads
    point = w > PIN + 0.01
    k = min(1.0, (LENGTH - 2 * 1.27) / w) if point else 1.0
    X = lambda x: round(LENGTH / 2 + (x - (a0 + a1) / 2) * k, 4)
    new_len = 0.0 if point else PIN
    leads = []
    lo, hi = X(a0), X(a1)
    if lo - new_len > 0.01:
        leads.append((new_len, lo))
    if LENGTH - new_len - hi > 0.01:
        leads.append((hi, LENGTH - new_len))
    name = sym.get("name")
    cmds = [C.GRID, f"EDIT {C.q(name + '.sym')};"]
    adds, expect = [], {"pins": {left.get("name"): (0.0, 0.0), right.get("name"): (LENGTH, 0.0)},
                        "wires": [], "rects": [], "circles": [], "texts": []}
    for el in list(sym):
        t = el.tag
        if t == "wire":
            x1, y1, x2, y2, c = _f(el, "x1"), _f(el, "y1"), _f(el, "x2"), _f(el, "y2"), _f(el, "curve")
            mx, my = _arc_mid(x1, y1, x2, y2, c)
            cmds.append(f"DELETE {C.pt(mx, my)};")
            w = el.get("width", "0.254")
            curve = f" {c:+g}" if c else ""
            adds.append(f"LAYER {el.get('layer', '94')}; WIRE {w} {C.pt(X(x1), y1)}{curve} {C.pt(X(x2), y2)};")
            expect["wires"].append((X(x1), y1, X(x2), y2))
            expect.setdefault("curves", []).append(c)
        elif t == "rectangle":
            x1, y1, x2, y2 = _f(el, "x1"), _f(el, "y1"), _f(el, "x2"), _f(el, "y2")
            cmds.append(f"DELETE {C.pt((x1 + x2) / 2, (y1 + y2) / 2)};")
            adds.append(f"LAYER {el.get('layer', '94')}; RECT {C.pt(X(x1), y1)} {C.pt(X(x2), y2)};")
            expect["rects"].append((X(x1), y1, X(x2), y2))
        elif t == "circle":
            x, y, r = _f(el, "x"), _f(el, "y"), _f(el, "radius")
            cmds.append(f"DELETE {C.pt(x + r, y)};")
            adds.append(f"LAYER {el.get('layer', '94')}; CIRCLE {el.get('width', '0.254')} {C.pt(X(x), y)} {C.pt(X(x) + r, y)};")
            expect["circles"].append((X(x), y, r))
        elif t == "polygon":
            vs = [(_f(v, "x"), _f(v, "y")) for v in el.iterfind("vertex")]
            a, b = vs[0], vs[1]
            cmds.append(f"DELETE {C.pt((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)};")
            pts = " ".join(C.pt(X(x), y) for x, y in vs + [vs[0]])
            adds.append(f"LAYER {el.get('layer', '94')}; POLYGON {el.get('width', '0.254')} {pts};")
        elif t == "text":
            s = el.text or ""
            x, y = _f(el, "x"), _f(el, "y")
            if s in (">NAME", ">VALUE"):
                to = NAME_AT if s == ">NAME" else VALUE_AT
                if abs(x - to[0]) > 1e-6 or abs(y - to[1]) > 1e-6:
                    expect["texts"].append((s, to))
                    cmds.append(f"DELETE {C.pt(x, y)};")
                    adds.append(f"CHANGE SIZE {el.get('size', '1.524')}; LAYER {el.get('layer', '95')}; "
                                f"TEXT {C.q(s)} R0 {C.pt(*to)};")
                else:
                    expect["texts"].append((s, to))
            else:
                cmds.append(f"DELETE {C.pt(x, y)};")
                adds.append(f"CHANGE SIZE {el.get('size', '1.524')}; LAYER {el.get('layer', '94')}; "
                            f"TEXT {C.q(s)} R0 {C.pt(X(x), y)};")
                expect["texts"].append((s, (X(x), y)))
    # pins: park both away from everything, then put them in place (no collisions)
    for p, park in ((left, (-50.0, 0.0)), (right, (-60.0, 0.0))):
        cmds.append(f"MOVE {C.pt(_f(p, 'x'), _f(p, 'y'))} {C.pt(*park)};")
    # CHANGE LENGTH keeps the pin's inner end and moves its connection point (seen on
    # 2705.1.15), so change the length while parked, then move into place
    for p, park, to, ln, d in ((left, (-50.0, 0.0), (0.0, 0.0), ll, 1), (right, (-60.0, 0.0), (LENGTH, 0.0), lr, -1)):
        at = park
        if abs(ln - new_len) > 1e-6:
            cmds.append(f"CHANGE LENGTH {'POINT' if point else 'SHORT'} {C.pt(*park)};")
            at = (park[0] + d * (ln - new_len), park[1])
        cmds.append(f"MOVE {C.pt(*at)} {C.pt(*to)};")
    for x1, x2 in leads:
        adds.append(f"LAYER 94; WIRE 0.1524 {C.pt(x1, 0)} {C.pt(x2, 0)};")
        expect["wires"].append((x1, 0.0, x2, 0.0))
        expect.setdefault("curves", []).append(0)
    expect["xml"] = _expected_xml(sym, X, left, right, "point" if point else "short", leads)
    # WIRE follows the bend style: diagonals become steps unless it is 'straight' (2)
    return " ".join(cmds + ["SET WIRE_BEND 2;"] + [a for a in adds if a] + ["SET WIRE_BEND 1;"]), expect


def _expected_xml(sym: ET.Element, X, left, right, pin_len: str = "short", leads=()) -> ET.Element:
    import copy
    new = copy.deepcopy(sym)
    for el in new:
        if el.tag in ("wire", "rectangle"):
            for k in ("x1", "x2"):
                el.set(k, str(X(_f(el, k))))
        elif el.tag == "circle":
            el.set("x", str(X(_f(el, "x"))))
        elif el.tag == "polygon":
            for v in el.iterfind("vertex"):
                v.set("x", str(X(_f(v, "x"))))
        elif el.tag == "text":
            if el.text in (">NAME", ">VALUE"):
                to = NAME_AT if el.text == ">NAME" else VALUE_AT
                el.set("x", str(to[0])); el.set("y", str(to[1]))
            else:
                el.set("x", str(X(_f(el, "x"))))
        elif el.tag == "pin":
            el.set("x", "0" if el.get("name") == left.get("name") else str(LENGTH))
            el.set("length", pin_len)
    for x1, x2 in leads:
        ET.SubElement(new, "wire", {"x1": str(x1), "y1": "0", "x2": str(x2), "y2": "0", "width": "0.1524", "layer": "94"})
    return new


def _merged(segs) -> list:
    """Straight segments with collinear touching pieces joined (Fusion merges them when it
    draws, e.g. a new lead onto the straight end of a zigzag)."""
    segs = [((round(a, 3), round(b, 3)), (round(c, 3), round(d, 3))) for a, b, c, d in segs]
    changed = True
    while changed:
        changed = False
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                (p, q), (r, t) = segs[i], segs[j]
                for a, b in ((p, q), (q, p)):
                    for c, d in ((r, t), (t, r)):
                        if a == c:   # shared end: collinear and pointing away from each other?
                            v1, v2 = (b[0] - a[0], b[1] - a[1]), (d[0] - c[0], d[1] - c[1])
                            if abs(v1[0] * v2[1] - v1[1] * v2[0]) < 1e-6 and v1[0] * v2[0] + v1[1] * v2[1] < 0:
                                segs[i] = (b, d)
                                del segs[j]
                                changed = True
                                break
                    if changed:
                        break
                if changed:
                    break
            if changed:
                break
    return sorted(tuple(round(v, 2) for v in sorted([p, q]) for v in v) for p, q in segs)


def check(sym: ET.Element, expect: dict) -> list[str]:
    """Differences between a symbol from the library export and the expected result."""
    bad = []
    pins = {p.get("name"): (_f(p, "x"), _f(p, "y")) for p in sym.findall("pin")}
    for n, (x, y) in expect["pins"].items():
        if n not in pins or abs(pins[n][0] - x) > 0.01 or abs(pins[n][1] - y) > 0.01:
            bad.append(f"pin {n} at {pins.get(n)} not {(x, y)}")
    got_w = _merged([(_f(w, "x1"), _f(w, "y1"), _f(w, "x2"), _f(w, "y2")) for w in sym.findall("wire")
                     if not _f(w, "curve")])
    want_w = _merged([w for w, el in zip(expect["wires"], expect.get("curves", [0] * len(expect["wires"]))) if not el])
    if got_w != want_w:
        bad.append(f"wires differ: {len(got_w)} vs {len(want_w)} expected")
    n_arcs = sum(1 for w in sym.findall("wire") if _f(w, "curve"))
    if n_arcs != sum(1 for c in expect.get("curves", []) if c):
        bad.append(f"arcs differ: {n_arcs}")
    if len(sym.findall("rectangle")) != len(expect["rects"]):
        bad.append("rectangles differ")
    texts = {(t.text or ""): (_f(t, "x"), _f(t, "y")) for t in sym.findall("text")}
    for s, (x, y) in expect["texts"]:
        if s not in texts or abs(texts[s][0] - x) > 0.01 or abs(texts[s][1] - y) > 0.01:
            bad.append(f"text {s} at {texts.get(s)} not {(x, y)}")
    return bad


# ---------------------------------------------------------------------------
# standard artwork for library parts (data/std_symbols.json: GPLIB's two-pin
# symbols after standardise(), keyed by style)

ANODE_NAMES = {"A", "ANODE", "+", "POS", "P", "PLUS"}


def styles() -> dict:
    import json
    import os
    with open(os.path.join(os.path.dirname(__file__), "data", "std_symbols.json"), encoding="utf-8") as f:
        return json.load(f)


def infer_style(part: dict) -> str | None:
    """The standard symbol a two-pin passive gets, from its prefix and description, so parts
    imported from EasyEDA or KiCad draw like the rest of the library instead of keeping the
    source's symbol. None for anything else (ICs, connectors) or a polar style whose anode pin
    cannot be told apart."""
    pins = part.get("symbol", {}).get("pins") or []
    if len(pins) != 2:
        return None
    pre = (part.get("prefix") or "").upper()
    text = " ".join(str(part.get(k) or "") for k in ("description", "deviceset", "id")).lower()
    has_anode = any(p["name"].upper() in ANODE_NAMES for p in pins)
    style = None
    if pre in ("R",):
        style = "res"
    elif pre in ("C",):
        style = "cap_pol" if (any(w in text for w in ("electrolytic", "tantalum", "polar", "elec")) and has_anode) else "cap"
    elif pre in ("FB",) or "ferrite" in text:
        style = "ferrite"
    elif pre in ("L",):
        style = "inductor"
    elif pre in ("F",) or "fuse" in text or "ptc" in text:
        style = "fuse"
    elif pre in ("Y", "X") and any(w in text for w in ("crystal", "xtal", "resonator")):
        style = "crystal"
    elif pre in ("D", "LED"):
        if pre == "LED" or "led" in text.split() or text.startswith("led"):
            style = "led"
        elif "schottky" in text:
            style = "schottky"
        elif "zener" in text:
            style = "zener"
        elif "tvs" in text or "esd" in text:
            style = "tvs_bidir" if "bidir" in text else "tvs"
        else:
            style = "diode"
    if style and styles()[style]["polar"] and not has_anode:
        return None
    return style


def pin_order(style: str, pins: list[dict]) -> list[dict]:
    """[left, right] pins for a styled two-pin part: the anode / + pin on the left for
    polar styles (the artwork's anode side), else the part's own order."""
    st = styles()[style]
    if len(pins) != 2:
        raise ValueError(f"style {style!r} is for two-pin parts; this one has {len(pins)} pins")
    if st["polar"]:
        a = [p for p in pins if p["name"].upper() in ANODE_NAMES]
        if len(a) != 1:
            raise ValueError(f"style {style!r} is polar: name one pin A/ANODE/+/POS (got {[p['name'] for p in pins]})")
        return [a[0], next(p for p in pins if p is not a[0])]
    left = [p for p in pins if p.get("side", "left") == "left"]
    return (left + [p for p in pins if p not in left])[:2]


def art_commands(style: str) -> list[str]:
    """EAGLE commands that draw a style's artwork and texts (symbol editor)."""
    out = []
    for it in styles()[style]["items"]:
        t, layer = it["tag"], it.get("layer", "94")
        if t == "wire":
            c = float(it.get("curve", 0) or 0)
            curve = f" {c:+g}" if c else ""
            out.append(f"LAYER {layer}; WIRE {it.get('width', '0.254')} ({it['x1']} {it['y1']}){curve} ({it['x2']} {it['y2']});")
        elif t == "rectangle":
            out.append(f"LAYER {layer}; RECT ({it['x1']} {it['y1']}) ({it['x2']} {it['y2']});")
        elif t == "circle":
            x, y, r = float(it["x"]), float(it["y"]), float(it["radius"])
            out.append(f"LAYER {layer}; CIRCLE {it.get('width', '0.254')} ({x:g} {y:g}) ({x + r:g} {y:g});")
        elif t == "polygon":
            vs = it["vertices"]
            pts = " ".join(f"({v['x']} {v['y']})" for v in vs + [vs[0]])
            out.append(f"LAYER {layer}; POLYGON {it.get('width', '0.254')} {pts};")
        elif t == "text":
            out.append(f"CHANGE SIZE {it.get('size', '1.524')}; LAYER {layer}; TEXT {C.q(it['text'])} R0 ({it['x']} {it['y']});")
    return out


def art_symbol(style: str, left: str, right: str) -> ET.Element:
    """The styled symbol as EAGLE XML (for offline geometry and previews)."""
    st = styles()[style]
    sym = ET.Element("symbol", {"name": style})
    for it in st["items"]:
        el = ET.SubElement(sym, it["tag"], {k: v for k, v in it.items() if k not in ("tag", "vertices", "text")})
        if it["tag"] == "polygon":
            for v in it["vertices"]:
                ET.SubElement(el, "vertex", v)
        if it["tag"] == "text":
            el.text = it["text"]
    ET.SubElement(sym, "pin", {"name": left, "x": "0", "y": "0", "visible": "off", "length": st["pin_length"],
                               "direction": "pas"})
    ET.SubElement(sym, "pin", {"name": right, "x": str(LENGTH), "y": "0", "visible": "off",
                               "length": st["pin_length"], "direction": "pas", "rot": "R180"})
    return sym


def restyle_script(sym_name: str, pins: list[dict], style: str) -> str:
    """Redraw a two-pin symbol made by library.build_script (a box, pins at x = -7.62 and
    7.62) in a standard style: delete the box and texts, move the pins (never delete them:
    CONNECTs stay), draw the artwork."""
    left, right = pin_order(style, pins)
    sides = {p["name"]: p.get("side", "left") for p in pins}
    hh = 2.54 / 2 + 1.27
    st = styles()[style]
    cmds = [C.GRID, f"EDIT {C.q(sym_name + '.sym')};",
            # the box sides are picked away from y = 0, where the pins' inner ends sit (a pick
            # there takes the pin, not the wire; seen on 2705.1.15)
            f"DELETE (0 {-hh:g});", f"DELETE (5.08 {hh * 0.75:g});", f"DELETE (5.08 {-hh * 0.75:g});",
            f"DELETE (0 {hh:g});", f"DELETE (-5.08 {hh * 0.75:g});", f"DELETE (-5.08 {-hh * 0.75:g});",
            f"DELETE (-5.08 {hh + 0.5:g});", f"DELETE (-5.08 {-hh - 2.3:g});"]
    now = {p["name"]: (-7.62 if sides[p["name"]] == "left" else 7.62) for p in pins}
    for p, park in ((left, -50.0), (right, -60.0)):
        cmds.append(f"MOVE ({now[p['name']]:g} 0) ({park:g} 0);")
    new_len = _LEN[st["pin_length"]]
    for p, park, to, want_side, d in ((left, -50.0, 0.0, "left", 1), (right, -60.0, LENGTH, "right", -1)):
        if sides[p["name"]] != want_side:
            cmds.append(f"ROTATE R180 ({park:g} 0);")
        at = park
        if abs(new_len - PIN) > 1e-6:           # parked, already facing its final way
            cmds.append(f"CHANGE LENGTH {st['pin_length'].upper()} ({park:g} 0);")
            at = park + d * (PIN - new_len)
        cmds.append(f"MOVE ({at:g} 0) ({to:g} 0);")
    return " ".join(cmds + ["SET WIRE_BEND 2;"] + art_commands(style) + ["SET WIRE_BEND 1;"])
