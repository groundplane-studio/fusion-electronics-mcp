"""Component library: JSON parts (library/parts/*.json, schema in
library/schema.json) and their Fusion build scripts.

Scripts use the same command dialect as Groundplane's fabhub/scrgen.py, which
the Phase 1 spike ran successfully through Electron.runScript: EDIT .pac/.sym/
.dev, SMD, PIN, CONNECT, TECHNOLOGY '', ATTRIBUTE, and the layer-114 JLC
marker for JLC-native footprints. The EAGLE `ATTRIBUTE SET` grammar is broken
in Fusion and is never emitted.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .commands import n, q

from . import data_dir

# the component library lives in the per-user data folder (FUSION_MCP_LIBRARY overrides);
# the parts come from the separate library repository
DEFAULT_DIR = data_dir("library", "parts")
MARKER_LAYER = 114


@dataclass
class Part:
    data: dict

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def device_name(self) -> str:
        """The ADD name: deviceset + device (EAGLE concatenates them)."""
        return self.data["deviceset"] + (self.data.get("device") or "")

    def summary(self) -> dict:
        d = self.data
        return {"id": d["id"], "deviceset": d["deviceset"], "device": d.get("device", ""),
                "description": d.get("description", ""), "category": d.get("category", ""),
                "package": d["package"]["name"],
                "pads": len(d["package"].get("smds", [])) + len(d["package"].get("pads", [])),
                "attributes": d.get("attributes", {}), "metadata": d.get("metadata", {})}


class Library:
    def __init__(self, directory: str | None = None):
        self.directory = os.path.abspath(directory or os.environ.get("FUSION_MCP_LIBRARY") or DEFAULT_DIR)
        self.parts: dict[str, Part] = {}
        if os.path.isdir(self.directory):
            for fn in sorted(os.listdir(self.directory)):
                if fn.endswith(".json"):
                    with open(os.path.join(self.directory, fn), encoding="utf-8") as f:
                        p = Part(json.load(f))
                    self.parts[p.id] = p

    def add(self, data: dict, overwrite: bool = False) -> str:
        """Save a part definition as <id>.json in the library folder; returns the path."""
        os.makedirs(self.directory, exist_ok=True)
        path = os.path.join(self.directory, f"{data['id']}.json")
        if os.path.exists(path) and not overwrite:
            raise FileExistsError(f"part {data['id']!r} already exists ({path}); pass overwrite=true to replace it")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self.parts[data["id"]] = Part(data)
        return path

    def search(self, query: str, limit: int = 20) -> list[dict]:
        terms = [t for t in query.lower().split() if t]
        hits = []
        for p in self.parts.values():
            hay = json.dumps(p.summary()).lower()
            if all(t in hay for t in terms):
                hits.append(p.summary())
        return hits[:limit]

    def get(self, part_id: str) -> Part:
        if part_id not in self.parts:
            raise KeyError(f"no library part {part_id!r}")
        return self.parts[part_id]


def same_package(el, pkg: dict, tol: float = 0.02) -> bool:
    """An exported library <package> element has the same pads as the part's package (names,
    positions and sizes), so a second device can use it instead of drawing it again."""
    def key(name, x, y, a, b):
        return (str(name).upper(), round(float(x) / tol), round(float(y) / tol), round(float(a) / tol), round(float(b) / tol))
    got = sorted([key(s.get("name"), s.get("x"), s.get("y"), s.get("dx"), s.get("dy")) for s in el.findall("smd")]
                 + [key(p.get("name"), p.get("x"), p.get("y"), p.get("drill"), p.get("diameter") or 0) for p in el.findall("pad")])
    want = sorted([key(s["name"], s["x"], s["y"], s["dx"], s["dy"]) for s in pkg.get("smds", [])]
                  + [key(p["name"], p["x"], p["y"], p["drill"], p.get("diameter", 0)) for p in pkg.get("pads", [])])
    return got == want


def build_script(part: Part, reuse_package: bool = False) -> str:
    """reuse_package: the library already has this package (same pads), so only the symbol and
    device are drawn and the device uses the existing package."""
    d = part.data
    pkg, sym = d["package"], d["symbol"]
    L = ["GRID MM;"]
    if not reuse_package:
        L += _package_commands(pkg)
    return _symbol_and_device(L, d, sym, pkg)


def _package_commands(pkg: dict) -> list:
    L = [f"EDIT {q(pkg['name'] + '.pac')};", "LAYER 1;"]
    for s in pkg.get("smds", []):
        L.append(f"SMD {n(s['dx'])} {n(s['dy'])} -{int(s.get('roundness', 0))} R{n(s.get('rot', 0))} "
                 f"{q(s['name'])} ({n(s['x'])} {n(s['y'])});")
    for p in pkg.get("pads", []):
        L.append(f"CHANGE DRILL {n(p['drill'])};")
        shape = {"square": "SQUARE", "long": "LONG", "octagon": "OCTAGON"}.get(p.get("shape"), "ROUND")
        L.append(f"PAD {n(p['diameter'])} {shape} R{n(p.get('rot', 0))} "
                 f"{q(p['name'])} ({n(p['x'])} {n(p['y'])});")
    for h in pkg.get("holes", []):
        L.append(f"HOLE {n(h['drill'])} ({n(h['x'])} {n(h['y'])});")
    if pkg.get("silk"):
        L.append("LAYER 21;")
        for x1, y1, x2, y2 in pkg["silk"]:
            L.append(f"WIRE 0.12 ({n(x1)} {n(y1)}) ({n(x2)} {n(y2)});")
    if pkg.get("silk_polys") or pkg.get("silk_circles"):
        L.append("LAYER 21;")
        # filled silk (KiCad's pin-1 triangle) becomes a filled dot at its centre: Fusion's
        # wire bend style squares off non-45-degree edges of both WIREs and POLYGONs
        # (a pin-1 triangle came out as a T on 2705.1.15); a CIRCLE cannot be bent
        for pts in pkg.get("silk_polys", []):
            cx = sum(x for x, _ in pts) / len(pts)
            cy = sum(y for _, y in pts) / len(pts)
            L.append(f"CIRCLE 0 ({n(cx)} {n(cy)}) ({n(cx + 0.2)} {n(cy)});")
        for c in pkg.get("silk_circles", []):
            w = 0 if c.get("filled") else 0.12      # CIRCLE width 0 draws a filled disc
            L.append(f"CIRCLE {n(w)} ({n(c['x'])} {n(c['y'])}) ({n(c['x'] + c['r'])} {n(c['y'])});")
    ys = [s["y"] for s in pkg.get("smds", []) + pkg.get("pads", [])] or [0]
    L += ["CHANGE SIZE 0.8;", "LAYER 25;", f"TEXT '>NAME' R0 (-1 {n(max(ys) + 1.2)});",
          "LAYER 27;", f"TEXT '>VALUE' R0 (-1 {n(min(ys) - 1.8)})"+";"]
    if pkg.get("jlc_native"):
        L += [f"LAYER {MARKER_LAYER} JLC_FOOTPRINT;", "CHANGE SIZE 0.6;", "TEXT 'JLC' R0 (0 0);"]
    return L


def _symbol_and_device(L: list, d: dict, sym: dict, pkg: dict) -> str:
    from . import std_symbols as SS
    # two-pin passives always get the library's standard symbol (std_symbols), whatever the
    # source drew; "style": false in the part keeps the part's own symbol
    style = sym.get("style")
    if style is None:
        style = SS.infer_style(d)
    if style:
        # a standard two-pin symbol: 7.62 mm, the anode / + pin on the left
        sym = {**sym, "style": style}
        lp, rp = SS.pin_order(sym["style"], sym["pins"])
        plen = SS.styles()[sym["style"]]["pin_length"]
        L += [f"EDIT {q(sym['name'] + '.sym')};",
              f"PIN {q(lp['name'])} {lp.get('direction', 'pas')} {plen} R0 (0 0);",
              f"PIN {q(rp['name'])} {rp.get('direction', 'pas')} {plen} R180 (7.62 0);",
              "CHANGE VISIBLE OFF (0 0);", "CHANGE VISIBLE OFF (7.62 0);"] + SS.art_commands(sym["style"])
        return _device_part(L, d, sym, pkg, q)
    left = [p for p in sym["pins"] if p.get("side", "left") == "left"]
    right = [p for p in sym["pins"] if p.get("side") == "right"]
    rows = max(len(left), len(right), 1)
    hh = rows * 2.54 / 2 + 1.27
    L += [f"EDIT {q(sym['name'] + '.sym')};", "LAYER 94;",
          f"WIRE 0.254 (-5.08 {n(-hh)}) (5.08 {n(-hh)}) (5.08 {n(hh)}) (-5.08 {n(hh)}) (-5.08 {n(-hh)});"]
    for side, pins, x, rot in (("left", left, -7.62, "R0"), ("right", right, 7.62, "R180")):
        for i, p in enumerate(pins):
            y = hh - 2.54 * (i + 1)
            L.append(f"PIN {q(p['name'])} {p.get('direction', 'pas')} short {rot} ({n(x)} {n(y)});")
    L += ["CHANGE SIZE 1.778;", "LAYER 95;", f"TEXT '>NAME' R0 (-5.08 {n(hh + 0.5)});",
          "LAYER 96;", f"TEXT '>VALUE' R0 (-5.08 {n(-hh - 2.3)});"]
    return _device_part(L, d, sym, pkg, q)


def _device_part(L: list, d: dict, sym: dict, pkg: dict, q) -> str:
    L += [f"EDIT {q(d['deviceset'] + '.dev')};", f"PREFIX {q(d['prefix'])};",
          f"VALUE {'ON' if d.get('user_value') else 'OFF'};",
          f"ADD {q(sym['name'])} 'G$1' NEXT 0 (0 0);",
          f"PACKAGE {q(pkg['name'])} '{d.get('device', '')}';"]
    # ONE CONNECT per pin, all its pads in one space-separated string. Verified
    # on 2705.1.15: a second CONNECT for the same pin REPLACES the first, so
    # per-pad lines silently leave only the last pad connected.
    L += [f"CONNECT {q('G$1.' + p['name'])} {q(' '.join(str(p['pad']).split()))};" for p in sym["pins"]]
    L.append("TECHNOLOGY '';")
    clean = lambda v: str(v).replace("'", "").replace(";", ",")
    for k, v in d.get("attributes", {}).items():
        L.append(f"ATTRIBUTE {k} {q(clean(v)) if v else chr(39) * 2};")
    meta = d.get("metadata", {})
    for k in ("source", "maintainer", "link"):
        if meta.get(k):
            L.append(f"ATTRIBUTE LIB_{k.upper()} {q(clean(meta[k]))};")
    return "\n".join(L) + "\n"
