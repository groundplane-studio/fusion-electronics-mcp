"""Schematic symbol geometry from EAGLE library XML (a .lbr, a Fusion library
export, or the libraries embedded in a schematic export).

A Geo is one device's single-gate symbol at rotation 0, in millimetres:
pins (connection point + outward direction), pad -> pin, the drawing
primitives (for previews) and the body box (for spacing). Multi-gate devices
are not supported yet (Geo.gates > 1 tells the caller).
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .design import transform
from .eagle import parse_rot

PIN_LEN = {"point": 0.0, "short": 2.54, "middle": 5.08, "long": 7.62}


@dataclass
class Pin:
    name: str
    x: float
    y: float
    ox: float          # outward unit vector (away from the body)
    oy: float
    length: float
    pads: list[str] = field(default_factory=list)
    visible: str = "both"


@dataclass
class Geo:
    key: str                                   # DEVICESET+DEVICE
    pins: dict[str, Pin]
    pad_pin: dict[str, str]
    prims: list[tuple] = field(default_factory=list)   # ('wire', x1, y1, x2, y2, w) / ('circle', x, y, r, w) / ('rect', x1, y1, x2, y2) / ('poly', [(x, y)]) / ('text', x, y, size, s, angle, align)
    gates: int = 1
    supply: bool = False

    def body(self) -> tuple[float, float, float, float]:
        """Box around the drawing and the pin lines (not the texts)."""
        xs, ys = [], []
        for p in self.prims:
            if p[0] == "wire":
                xs += [p[1], p[3]]; ys += [p[2], p[4]]
            elif p[0] == "circle":
                xs += [p[1] - p[3], p[1] + p[3]]; ys += [p[2] - p[3], p[2] + p[3]]
            elif p[0] == "rect":
                xs += [p[1], p[3]]; ys += [p[2], p[4]]
            elif p[0] == "poly":
                xs += [q[0] for q in p[1]]; ys += [q[1] for q in p[1]]
        for pn in self.pins.values():
            xs += [pn.x, pn.x - pn.ox * pn.length]; ys += [pn.y, pn.y - pn.oy * pn.length]
        if not xs:
            return (0.0, 0.0, 0.0, 0.0)
        return (min(xs), min(ys), max(xs), max(ys))


def _f(el, k, d=0.0):
    v = el.get(k)
    return float(v) if v not in (None, "") else d


def symbol_prims(sym: ET.Element) -> list[tuple]:
    out: list[tuple] = []
    for w in sym.iterfind("wire"):
        out.append(("wire", _f(w, "x1"), _f(w, "y1"), _f(w, "x2"), _f(w, "y2"), _f(w, "width", 0.254),
                    _f(w, "curve")))
    for c in sym.iterfind("circle"):
        out.append(("circle", _f(c, "x"), _f(c, "y"), _f(c, "radius"), _f(c, "width", 0.254)))
    for r in sym.iterfind("rectangle"):
        out.append(("rect", _f(r, "x1"), _f(r, "y1"), _f(r, "x2"), _f(r, "y2")))
    for p in sym.iterfind("polygon"):
        out.append(("poly", [(_f(v, "x"), _f(v, "y")) for v in p.iterfind("vertex")]))
    for t in sym.iterfind("text"):
        a, _ = parse_rot(t.get("rot"))
        out.append(("text", _f(t, "x"), _f(t, "y"), _f(t, "size", 1.778), (t.text or ""), a,
                    t.get("align", "bottom-left")))
    return out


def symbol_pins(sym: ET.Element) -> dict[str, Pin]:
    pins = {}
    for pn in sym.iterfind("pin"):
        a, _ = parse_rot(pn.get("rot"))
        # at R0 the pin line runs +x from the connection point into the body
        ox, oy = -math.cos(math.radians(a)), -math.sin(math.radians(a))
        pins[pn.get("name", "")] = Pin(pn.get("name", ""), _f(pn, "x"), _f(pn, "y"), round(ox, 6), round(oy, 6),
                                       PIN_LEN.get(pn.get("length", "long"), 7.62),
                                       visible=pn.get("visible", "both"))
    return pins


def from_library_xml(root: ET.Element, library: str | None = None) -> dict[str, Geo]:
    """{DEVICESET+DEVICE: Geo} for every device in one or more <library> elements under root.
    With `library`, only that library (by name)."""
    out: dict[str, Geo] = {}
    libs = [root] if root.tag == "library" else list(root.iter("library"))
    for lib in libs:
        if library is not None and lib.get("name") not in (library, None):
            continue
        symbols = {s.get("name"): s for s in lib.iter("symbol")}
        for ds in lib.iter("deviceset"):
            gates = ds.findall("./gates/gate")
            if not gates:
                continue
            g = gates[0]
            sym = symbols.get(g.get("symbol"))
            if sym is None:
                continue
            for dv in ds.iterfind("./devices/device"):
                pins = symbol_pins(sym)
                for p in pins.values():
                    p.x, p.y = round(p.x + _f(g, "x"), 4), round(p.y + _f(g, "y"), 4)
                pad_pin = {}
                for c in dv.iterfind("./connects/connect"):
                    if c.get("gate") != g.get("name"):
                        continue
                    pads = (c.get("pad") or "").split()
                    if c.get("pin") in pins:
                        pins[c.get("pin")].pads = pads
                    for pad in pads:
                        pad_pin[pad] = c.get("pin")
                supply = not dv.get("package") and all(p.get("direction") == "sup" for p in sym.iterfind("pin"))
                key = (ds.get("name") or "") + (dv.get("name") or "")
                out[key] = Geo(key, pins, pad_pin, symbol_prims(sym), len(gates), supply)
    return out


def from_part_json(d: dict) -> Geo:
    """The symbol fusion_mcp.library.build_script draws for a part JSON (a box,
    pins left and right, 2.54 mm pitch). Keep in step with build_script."""
    sym = d["symbol"]
    if sym.get("style"):
        from fusion_mcp import std_symbols as SS      # the artwork lives with the library builder
        lp, rp = SS.pin_order(sym["style"], sym["pins"])
        el = SS.art_symbol(sym["style"], lp["name"], rp["name"])
        pins = symbol_pins(el)
        pad_pin = {}
        for p in sym["pins"]:
            pins[p["name"]].pads = str(p["pad"]).split()
            for pad in pins[p["name"]].pads:
                pad_pin[pad] = p["name"]
        return Geo(d["deviceset"] + d.get("device", ""), pins, pad_pin, symbol_prims(el))
    left = [p for p in sym["pins"] if p.get("side", "left") == "left"]
    right = [p for p in sym["pins"] if p.get("side") == "right"]
    rows = max(len(left), len(right), 1)
    hh = rows * 2.54 / 2 + 1.27
    prims: list[tuple] = [("wire", -5.08, -hh, 5.08, -hh, 0.254, 0), ("wire", 5.08, -hh, 5.08, hh, 0.254, 0),
                          ("wire", 5.08, hh, -5.08, hh, 0.254, 0), ("wire", -5.08, hh, -5.08, -hh, 0.254, 0),
                          ("text", -5.08, hh + 0.5, 1.778, ">NAME", 0, "bottom-left"),
                          ("text", -5.08, -hh - 2.3, 1.778, ">VALUE", 0, "bottom-left")]
    pins, pad_pin = {}, {}
    for side, plist, x, ox in (("left", left, -7.62, -1.0), ("right", right, 7.62, 1.0)):
        for i, p in enumerate(plist):
            y = round(hh - 2.54 * (i + 1), 4)
            pads = str(p["pad"]).split()
            pins[p["name"]] = Pin(p["name"], x, y, ox, 0.0, 2.54, pads)
            for pad in pads:
                pad_pin[pad] = p["name"]
    key = d["deviceset"] + d.get("device", "")
    return Geo(key, pins, pad_pin, prims)


# ---------------------------------------------------------------------------
# placement maths


ROTS = (0, 90, 180, 270)


def place_point(px: float, py: float, x: float, y: float, rot: float, mirror: bool) -> tuple[float, float]:
    return transform(px, py, x, y, rot, mirror)


def place_dir(ox: float, oy: float, rot: float, mirror: bool) -> tuple[float, float]:
    dx, dy = transform(ox, oy, 0, 0, rot, mirror)
    return round(dx), round(dy)


def solve(geo: Geo, pin: str, at: tuple[float, float], facing: tuple[int, int],
          prefer_mirror: bool = False) -> tuple[float, float, int, bool]:
    """Origin and rotation that put `pin`'s connection point at `at` with its outward
    direction `facing`. Unmirrored first unless prefer_mirror."""
    p = geo.pins[pin]
    order = [(r, m) for m in ((True, False) if prefer_mirror else (False, True)) for r in ROTS]
    for rot, mir in order:
        if place_dir(p.ox, p.oy, rot, mir) == tuple(facing):
            px, py = transform(p.x, p.y, 0, 0, rot, mir)
            return round(at[0] - px, 4), round(at[1] - py, 4), rot, mir
    raise ValueError(f"{geo.key}.{pin} cannot face {facing}")
