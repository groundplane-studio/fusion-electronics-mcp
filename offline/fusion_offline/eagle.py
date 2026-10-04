# Ported from Groundplane's fabhub eagle reader, unchanged except imports.
"""EAGLE XML reader for Fusion 360 Electronics boards and libraries.

Fusion's own containers are zips wrapping plain EAGLE 9.7 XML:
  .flbr / .fbrd  ->  <Doc Asset>/electron.BlobParts/ExtFile.<guid>.lbr|.brd
  .f3z           ->  contains a .fbrd entry (plus .fsch/.f3d we ignore)
so load_board()/load_library() accept any of: raw XML, a Fusion container, or an
f3z archive, and always hand back the parsed XML tree wrapped in our model.

Only what the fab pipeline needs is modeled: elements with their attributes,
packages with pad geometry and marker texts, the board outline, and copper usage.
Coordinates in EAGLE XML are already millimeters.
"""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


JLC_MARKER_LAYER_NAME = "JLC_FOOTPRINT"   # resolved to its number per-document, like the ULP
JLC_MARKER_TEXT = "JLC"


# ---------------------------------------------------------------------------
# container handling


def _read_document_xml(data: bytes, inner_ext: str) -> bytes:
    """Return raw EAGLE XML from `data`, unwrapping Fusion zip containers as needed."""
    if not data.startswith(b"PK"):
        return data
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        # Fusion container: the embedded EAGLE doc under electron.BlobParts/
        blobs = [n for n in names if "electron.BlobParts/" in n and n.lower().endswith(inner_ext)]
        if blobs:
            return z.read(blobs[0])
        # f3z: contains a .fbrd (itself a container) - recurse
        wrapped = [n for n in names if n.lower().endswith(".f" + inner_ext.lstrip("."))]
        if wrapped:
            return _read_document_xml(z.read(wrapped[0]), inner_ext)
        # plain zip of a raw .brd/.lbr (e.g. someone zipped an export)
        raw = [n for n in names if n.lower().endswith(inner_ext)]
        if raw:
            return _read_document_xml(z.read(raw[0]), inner_ext)
    raise ValueError(f"no {inner_ext} document found inside archive")


def read_board_xml(path_or_bytes) -> bytes:
    data = path_or_bytes if isinstance(path_or_bytes, bytes) else open(path_or_bytes, "rb").read()
    return _read_document_xml(data, ".brd")


def read_library_xml(path_or_bytes) -> bytes:
    data = path_or_bytes if isinstance(path_or_bytes, bytes) else open(path_or_bytes, "rb").read()
    return _read_document_xml(data, ".lbr")


# ---------------------------------------------------------------------------
# model


@dataclass
class Smd:
    name: str
    x: float
    y: float
    dx: float
    dy: float
    layer: int
    rot: float = 0.0   # pad's own rotation within the package (EAGLE rot="R90")


@dataclass
class Pad:
    name: str
    x: float
    y: float
    drill: float


@dataclass
class PkgWire:
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: int


@dataclass
class Package:
    library: str
    name: str
    smds: list[Smd] = field(default_factory=list)
    pads: list[Pad] = field(default_factory=list)
    texts: list[tuple[int, str]] = field(default_factory=list)   # (layer, content)
    wires: list[PkgWire] = field(default_factory=list)           # silk/docu outlines
    description: str = ""

    @property
    def solder_pad_count(self) -> int:
        """Countable solder joints: one per SMD pad + one per through-hole pad."""
        return len(self.smds) + len(self.pads)

    def has_marker(self, layer_number: int, substr: str = JLC_MARKER_TEXT) -> bool:
        return any(ly == layer_number and substr in (txt or "") for ly, txt in self.texts)


_ROT_RE = re.compile(r"^(?P<flags>[SMsm]*)R(?P<angle>-?\d+(?:\.\d+)?)$")


def parse_rot(rot: str | None) -> tuple[float, bool]:
    """EAGLE rot attribute ('R90', 'MR180', 'SR33.5', absent) -> (angle_deg, mirrored)."""
    if not rot:
        return 0.0, False
    m = _ROT_RE.match(rot.strip())
    if not m:
        raise ValueError(f"unparseable rot attribute: {rot!r}")
    flags = m.group("flags").upper()
    return float(m.group("angle")), "M" in flags


@dataclass
class Element:
    name: str
    value: str
    library: str
    package: str
    x: float
    y: float
    angle: float
    mirror: bool
    attrs: dict[str, str] = field(default_factory=dict)

    def attr(self, name: str) -> str:
        return (self.attrs.get(name) or "").strip()


@dataclass
class Board:
    layers: dict[int, str] = field(default_factory=dict)          # number -> name
    packages: dict[tuple[str, str], Package] = field(default_factory=dict)
    elements: list[Element] = field(default_factory=list)
    outline: tuple[float, float, float, float] | None = None       # minx, miny, maxx, maxy
    outline_wires: list[tuple[float, float, float, float]] = field(default_factory=list)
    layer_setup: str | None = None    # designrules layerSetup, e.g. "(1+2*15+16)"
    fusion_cloud: bool = False        # Fusion cloud/cache dialect (what the ULP uploads)

    def layer_number(self, name: str) -> int | None:
        for num, lname in self.layers.items():
            if lname == name:
                return num
        return None

    @property
    def jlc_marker_layer(self) -> int | None:
        return self.layer_number(JLC_MARKER_LAYER_NAME)

    def package_of(self, el: Element) -> Package | None:
        return (self.packages.get((el.library, el.package))
                or next((p for (lib, name), p in self.packages.items() if name == el.package), None))

    def element_has_marker(self, el: Element) -> bool:
        ly = self.jlc_marker_layer
        if ly is None:
            return False
        pkg = self.package_of(el)
        return bool(pkg and pkg.has_marker(ly))

    @property
    def copper_layers(self) -> list[int]:
        """Copper layers actually carrying signal wires (1..16), sorted."""
        return sorted(self._copper_used)

    @property
    def copper_count(self) -> int:
        """Number of copper layers in the stack. The designrules layerSetup is
        authoritative when present ("(1+2*15+16)" exported, "(1+2*63+64)" in the
        Fusion cloud dialect, both 4 distinct numbers); content scanning is the
        fallback. Never below 2 for a routed board."""
        if self.layer_setup:
            nums = set(re.findall(r"\d+", self.layer_setup))
            if nums:
                return len(nums)
        return max(len(self._copper_used), 2 if self._copper_used else 0)

    _copper_used: set = field(default_factory=set)

    @property
    def size_mm(self) -> tuple[float, float] | None:
        if not self.outline:
            return None
        x0, y0, x1, y1 = self.outline
        return round(x1 - x0, 3), round(y1 - y0, 3)


# ---------------------------------------------------------------------------
# parsing


def _parse_package(lib_name: str, node: ET.Element) -> Package:
    pkg = Package(library=lib_name, name=node.get("name", ""))
    for child in node:
        tag = child.tag
        if tag == "smd":
            pkg.smds.append(Smd(name=child.get("name", ""),
                                x=float(child.get("x", 0)), y=float(child.get("y", 0)),
                                dx=float(child.get("dx", 0)), dy=float(child.get("dy", 0)),
                                layer=int(child.get("layer", 1)),
                                rot=parse_rot(child.get("rot"))[0]))
        elif tag == "pad":
            pkg.pads.append(Pad(name=child.get("name", ""),
                                x=float(child.get("x", 0)), y=float(child.get("y", 0)),
                                drill=float(child.get("drill", 0))))
        elif tag == "text":
            pkg.texts.append((int(child.get("layer", 0)), child.text or ""))
        elif tag == "wire":
            ly = int(child.get("layer", 0))
            if ly in (21, 22, 51, 52):   # silk + docu outlines, both sides
                pkg.wires.append(PkgWire(
                    x1=float(child.get("x1", 0)), y1=float(child.get("y1", 0)),
                    x2=float(child.get("x2", 0)), y2=float(child.get("y2", 0)),
                    width=float(child.get("width", 0.1)), layer=ly))
        elif tag == "description":
            pkg.description = (child.text or "").strip()
    return pkg


def parse_board(xml_bytes: bytes) -> Board:
    root = ET.fromstring(xml_bytes)
    board_node = root.find(".//board")
    if board_node is None:
        raise ValueError("not an EAGLE board document (no <board> node)")
    b = Board()
    b._copper_used = set()

    for ly in root.iterfind(".//layers/layer"):
        b.layers[int(ly.get("number"))] = ly.get("name", "")
        if ly.get("color") is None and ly.get("blackcolor") is not None:
            b.fusion_cloud = True

    for p in board_node.iterfind("designrules/param"):
        if p.get("name") == "layerSetup":
            b.layer_setup = p.get("value")

    for lib in board_node.iterfind("libraries/library"):
        lib_name = lib.get("name", "")
        for pk in lib.iterfind("packages/package"):
            pkg = _parse_package(lib_name, pk)
            b.packages[(lib_name, pkg.name)] = pkg

    for el in board_node.iterfind("elements/element"):
        angle, mirror = parse_rot(el.get("rot"))
        attrs = {a.get("name", ""): a.get("value", "") for a in el.iterfind("attribute")}
        b.elements.append(Element(
            name=el.get("name", ""), value=el.get("value", ""),
            library=el.get("library", ""), package=el.get("package", ""),
            x=float(el.get("x", 0)), y=float(el.get("y", 0)),
            angle=angle, mirror=mirror, attrs=attrs))

    xs: list[float] = []
    ys: list[float] = []
    for w in board_node.iterfind("plain/wire"):
        if int(w.get("layer", 0)) == 20:
            seg = (float(w.get("x1")), float(w.get("y1")),
                   float(w.get("x2")), float(w.get("y2")))
            b.outline_wires.append(seg)
            xs += [seg[0], seg[2]]
            ys += [seg[1], seg[3]]
    if xs:
        b.outline = (min(xs), min(ys), max(xs), max(ys))

    # Two copper numbering schemes exist for the same design: the EAGLE-compatible
    # export uses legacy 1..16 (top=1, bottom=16), while the Fusion cloud/cache
    # .brd (what the ULP uploads via B.name) numbers stack positions L3+ as 303,
    # 304, ... (L1/L2 stay 1/2). Canonicalize 3xx to its stack position so the
    # copper-layer COUNT (what quoting needs) is right for both variants.
    fusion_scheme = False
    for tag in ("wire", "polygon"):
        for w in board_node.iterfind(f"signals/signal/{tag}"):
            ly = int(w.get("layer", 0))
            if 301 <= ly <= 332:
                b._copper_used.add(ly - 300)
                fusion_scheme = True
            elif 1 <= ly <= 16:
                b._copper_used.add(ly)
    if board_node.find("signals/signal/via") is not None:
        # a via implies top+bottom exist; 16 is only the bottom in the legacy scheme
        b._copper_used.add(1)
        if not fusion_scheme:
            b._copper_used.add(16)

    return b


def load_board(path) -> Board:
    return parse_board(read_board_xml(path))
