"""Design model built from Fusion's EAGLE XML export (the canonical read source).

Complements eagle.py (the fab-oriented board model that bompnp.py consumes)
with what review, SI and write verification need:

- board: signals with wires / vias / contacts, net classes, design rules,
  copper stack, layer names
- schematic: parts with RESOLVED attributes (library technology attributes
  overlaid by part-level ones; the live API cannot read these), instances,
  pins in sheet coordinates with electrical direction, nets merged across
  sheets (the API returns one net object per sheet)

All coordinates are millimetres (EAGLE XML units).

Transform convention for placed instances: world = T + M(R(angle) * p), i.e.
rotate first, then mirror X. Gerber-verified for board elements in fabhub
(asmview); for schematic instances it is checked against the live API in
tests/test_design.py for unmirrored rotations only.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .eagle import parse_rot, read_board_xml

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _f(el: ET.Element, key: str, default: float = 0.0) -> float:
    v = el.get(key)
    return float(v) if v not in (None, "") else default


def transform(px: float, py: float, x: float, y: float, angle: float, mirror: bool) -> tuple[float, float]:
    a = math.radians(angle)
    rx = px * math.cos(a) - py * math.sin(a)
    ry = px * math.sin(a) + py * math.cos(a)
    if mirror:
        rx = -rx
    return round(x + rx, 4), round(y + ry, 4)


def read_xml(path_or_bytes) -> ET.Element:
    """Parse a .brd/.sch/.lbr (or a Fusion container) into an XML root."""
    if isinstance(path_or_bytes, bytes):
        data = path_or_bytes
    else:
        with open(path_or_bytes, "rb") as fh:
            data = fh.read()
    if data.startswith(b"PK"):
        for ext in (".brd", ".sch", ".lbr"):
            try:
                from .eagle import _read_document_xml
                data = _read_document_xml(data, ext)
                break
            except ValueError:
                continue
    return ET.fromstring(data)


# ---------------------------------------------------------------------------
# board


@dataclass
class Wire:
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: int
    curve: float = 0.0

    @property
    def length(self) -> float:
        chord = math.hypot(self.x2 - self.x1, self.y2 - self.y1)
        c = math.radians(self.curve or 0.0)
        if abs(c) < 1e-9:
            return chord
        return abs(c) * chord / (2 * abs(math.sin(c / 2)))


@dataclass
class Via:
    x: float
    y: float
    drill: float
    extent: str
    diameter: float = 0.0     # 0 = automatic from restring design rules


@dataclass
class Signal:
    name: str
    net_class: str
    contacts: list[tuple[str, str]] = field(default_factory=list)   # (element, pad)
    wires: list[Wire] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    polygons: int = 0

    def length_by_layer(self) -> dict[int, float]:
        out: dict[int, float] = {}
        for w in self.wires:
            out[w.layer] = out.get(w.layer, 0.0) + w.length
        return out

    @property
    def length(self) -> float:
        return sum(w.length for w in self.wires)


@dataclass
class NetClass:
    number: int
    name: str
    width: float
    drill: float
    clearances: dict[str, float] = field(default_factory=dict)


@dataclass
class BoardDesign:
    name: str
    layers: dict[int, str]
    signals: dict[str, Signal]
    classes: dict[int, NetClass]
    rules: dict[str, str]
    element_names: list[str]

    @property
    def copper_layers(self) -> list[int]:
        """Copper layer numbers in stack order, from designrules layerSetup."""
        setup = self.rules.get("layerSetup", "")
        nums = [int(n) for n in re.findall(r"\d+", setup)]
        seen: list[int] = []
        for n in nums:
            if n not in seen:
                seen.append(n)
        return seen or [1, 16]

    def stackup(self) -> dict:
        """Copper stack from designrules. Copper thickness per layer comes
        from mtCopper (16 values, layer 1..16 order). Dielectric thicknesses
        are returned RAW (mtIsolate): how Fusion maps them onto a given
        layerSetup is not verified, and on the test boards they held EAGLE's
        defaults (1.5mm 0.15mm 0.2mm ...), so no per-gap value is derived.
        The dielectric constant is not stored in Fusion designs at all."""
        cu = [_mm(v) for v in self.rules.get("mtCopper", "").split()]
        stack = self.copper_layers
        return {
            "copper_layers": [{"layer": n, "name": self.layers.get(n, str(n)),
                               "copper_mm": cu[n - 1] if 0 < n <= len(cu) else None} for n in stack],
            "layer_setup": self.rules.get("layerSetup"),
            "mtIsolate_raw": self.rules.get("mtIsolate"),
            "dielectric_mm": None,
            "er": None,
            "note": ("Dielectric thicknesses and Er are not reliably available from the design; "
                     "take them from the fab's stackup (e.g. JLC's published stackups) and pass "
                     "them to estimate_impedance."),
        }


def _mm(v: str) -> float:
    m = _NUM.search(v or "")
    if not m:
        return 0.0
    x = float(m.group(0))
    if v.strip().endswith("mil"):
        return x * 0.0254
    if v.strip().endswith("mic"):
        return x / 1000
    return x


def parse_board_design(root: ET.Element) -> BoardDesign:
    board = root.find("./drawing/board")
    if board is None:
        raise ValueError("not a board document")
    layers = {int(l.get("number")): l.get("name", "") for l in root.iterfind("./drawing/layers/layer")}
    rules = {p.get("name"): p.get("value", "") for p in board.iterfind("./designrules/param")}
    classes = {}
    for c in board.iterfind("./classes/class"):
        nc = NetClass(int(c.get("number", 0)), c.get("name", ""), _f(c, "width"), _f(c, "drill"))
        for cl in c.iterfind("clearance"):
            nc.clearances[cl.get("class", "")] = _f(cl, "value")
        classes[nc.number] = nc
    signals = {}
    for s in board.iterfind("./signals/signal"):
        cls_num = int(s.get("class", "0") or 0)
        sig = Signal(s.get("name", ""), classes[cls_num].name if cls_num in classes else str(cls_num))
        for c in s.iterfind("contactref"):
            sig.contacts.append((c.get("element", ""), c.get("pad", "")))
        for w in s.iterfind("wire"):
            sig.wires.append(Wire(_f(w, "x1"), _f(w, "y1"), _f(w, "x2"), _f(w, "y2"),
                                  _f(w, "width"), int(w.get("layer", 0)), _f(w, "curve")))
        for v in s.iterfind("via"):
            sig.vias.append(Via(_f(v, "x"), _f(v, "y"), _f(v, "drill"), v.get("extent", ""), _f(v, "diameter")))
        sig.polygons = len(s.findall("polygon"))
        signals[sig.name] = sig
    names = [e.get("name", "") for e in board.iterfind("./elements/element")]
    return BoardDesign(root.get("name", ""), layers, signals, classes, rules, names)


# ---------------------------------------------------------------------------
# schematic


@dataclass
class PinRef:
    part: str
    gate: str
    pin: str


@dataclass
class SchPin:
    part: str
    gate: str
    pin: str
    pad: str | None
    direction: str
    x: float
    y: float
    sheet: int
    outward: tuple[float, float] = (0.0, 0.0)   # unit vector pointing away from the symbol body


@dataclass
class SchInstance:
    part: str
    gate: str
    x: float
    y: float
    angle: float
    mirror: bool
    sheet: int


@dataclass
class SchPart:
    name: str
    library: str
    deviceset: str
    device: str
    technology: str
    value: str
    package: str | None
    user_value: bool
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class SchSegment:
    sheet: int
    pins: list[tuple[str, str]]       # (part, pin) on this piece of wire
    labels: int
    wires: int
    closed: bool = False              # the wires form a closed loop (a drawn box)
    first_wire: tuple[float, float, float, float] | None = None


@dataclass
class SchNet:
    name: str
    net_class: str
    sheets: list[int] = field(default_factory=list)
    segments: list[SchSegment] = field(default_factory=list)
    pins: list[PinRef] = field(default_factory=list)
    wire_points: list[tuple[int, float, float]] = field(default_factory=list)   # (sheet, x, y)


@dataclass
class SchematicDesign:
    parts: dict[str, SchPart]
    nets: dict[str, SchNet]
    pins: list[SchPin]
    sheets: int
    classes: dict[int, str]
    instances: list["SchInstance"] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)

    def instance(self, part: str, gate: str | None = None) -> SchInstance | None:
        return next((i for i in self.instances if i.part == part and (gate is None or i.gate == gate)), None)

    def pin(self, part: str, pin: str) -> SchPin | None:
        return next((p for p in self.pins if p.part == part and p.pin == pin), None)

    def net_of(self, part: str, pin: str) -> str | None:
        for n in self.nets.values():
            if any(r.part == part and r.pin == pin for r in n.pins):
                return n.name
        return None


def parse_schematic_design(root: ET.Element) -> SchematicDesign:
    sch = root.find("./drawing/schematic")
    if sch is None:
        raise ValueError("not a schematic document")
    libs = {}
    for lib in sch.iterfind("./libraries/library"):
        key = (lib.get("name", ""), lib.get("urn", ""))
        libs[key] = lib
        libs.setdefault((lib.get("name", ""), None), lib)

    def find_lib(name: str, urn: str | None):
        hit = libs.get((name, urn or ""))
        return hit if hit is not None else libs.get((name, None))

    classes = {int(c.get("number", 0)): c.get("name", "") for c in sch.iterfind("./classes/class")}
    parts: dict[str, SchPart] = {}
    gate_syms: dict[tuple[str, str], tuple[ET.Element, ET.Element]] = {}   # (part, gate) -> (symbol, device)
    for p in sch.iterfind("./parts/part"):
        lib = find_lib(p.get("library", ""), p.get("library_urn"))
        ds = dev = None
        if lib is not None:
            ds = next((d for d in lib.iterfind("./devicesets/deviceset") if d.get("name") == p.get("deviceset")), None)
            if ds is not None:
                dev = next((d for d in ds.iterfind("./devices/device") if d.get("name", "") == (p.get("device") or "")), None)
        tech_name = p.get("technology", "")
        attrs: dict[str, str] = {}
        if dev is not None:
            for t in dev.iterfind("./technologies/technology"):
                if t.get("name", "") == tech_name:
                    attrs.update({a.get("name", ""): a.get("value", "") for a in t.iterfind("attribute")})
        attrs.update({a.get("name", ""): a.get("value", "") for a in p.iterfind("attribute")})
        parts[p.get("name", "")] = SchPart(
            name=p.get("name", ""), library=p.get("library", ""), deviceset=p.get("deviceset", ""),
            device=p.get("device", ""), technology=tech_name, value=p.get("value") or attrs.get("VALUE", ""),
            package=dev.get("package") if dev is not None else None,
            user_value=(ds is not None and ds.get("uservalue") == "yes"), attributes=attrs)
        if ds is not None and lib is not None:
            symbols = {s.get("name"): s for s in lib.iterfind("./symbols/symbol")}
            for g in ds.iterfind("./gates/gate"):
                sym = symbols.get(g.get("symbol"))
                if sym is not None:
                    gate_syms[(p.get("name", ""), g.get("name", ""))] = (sym, dev)

    pins: list[SchPin] = []
    instances: list[SchInstance] = []
    nets: dict[str, SchNet] = {}
    sheets = sch.findall("./sheets/sheet")
    for si, sheet in enumerate(sheets, 1):
        for inst in sheet.iterfind("./instances/instance"):
            part, gate = inst.get("part", ""), inst.get("gate", "")
            sd = gate_syms.get((part, gate))
            if not sd:
                continue
            sym, dev = sd
            angle, mirror = parse_rot(inst.get("rot"))
            instances.append(SchInstance(part, gate, _f(inst, "x"), _f(inst, "y"), angle, mirror, si))
            pad_of = {}
            if dev is not None:
                for c in dev.iterfind("./connects/connect"):
                    if c.get("gate") == gate:
                        pad_of[c.get("pin")] = c.get("pad")
            for pn in sym.iterfind("pin"):
                wx, wy = transform(_f(pn, "x"), _f(pn, "y"), _f(inst, "x"), _f(inst, "y"), angle, mirror)
                # A pin at R0 has its body toward +x from the connection point,
                # so "outward" (away from the symbol) is -x, then placed.
                pa, _ = parse_rot(pn.get("rot"))
                ox, oy = transform(-math.cos(math.radians(pa)), -math.sin(math.radians(pa)), 0, 0, angle, mirror)
                pins.append(SchPin(part, gate, pn.get("name", ""), pad_of.get(pn.get("name")),
                                   pn.get("direction", "io"), wx, wy, si, (round(ox, 4), round(oy, 4))))
        for net in sheet.iterfind("./nets/net"):
            cls = int(net.get("class", "0") or 0)
            n = nets.setdefault(net.get("name", ""), SchNet(net.get("name", ""), classes.get(cls, str(cls))))
            n.sheets.append(si)
            for seg in net.iterfind("segment"):
                ws = seg.findall("wire")
                ends: dict[tuple[float, float], int] = {}
                for w in ws:
                    for k in (("x1", "y1"), ("x2", "y2")):
                        pt = (round(_f(w, k[0]), 3), round(_f(w, k[1]), 3))
                        ends[pt] = ends.get(pt, 0) + 1
                closed = len(ws) >= 3 and all(c == 2 for c in ends.values())
                n.segments.append(SchSegment(si, [(r.get("part", ""), r.get("pin", "")) for r in seg.iter("pinref")],
                                             len(seg.findall("label")), len(ws), closed,
                                             (_f(ws[0], "x1"), _f(ws[0], "y1"), _f(ws[0], "x2"), _f(ws[0], "y2")) if ws else None))
            for pr in net.iter("pinref"):
                n.pins.append(PinRef(pr.get("part", ""), pr.get("gate", ""), pr.get("pin", "")))
            for w in net.iter("wire"):
                n.wire_points.append((si, _f(w, "x1"), _f(w, "y1")))
    descs = [(sh.findtext("description") or "").strip() for sh in sheets]
    return SchematicDesign(parts, nets, pins, len(sheets), classes, instances, descs)
