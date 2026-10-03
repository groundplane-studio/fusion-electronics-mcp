"""Fusion (EAGLE V2) layer stackup and design rules.

The classic EAGLE XML export drops the V2 stackup, but Fusion keeps it in
three places, all with the same <layerstackup> element:
- .estackup files (exported stackup),
- .edru design-rule files (rules + stackup),
- the cloud-format board working copy Fusion writes while a design is open.

A stackup lists layers top to bottom: silk, mask, finish, then alternating
Signal / Prepreg / Core layers, then the bottom finish, mask and silk.
Dielectrics carry thickness and dielectric_constant_1g (Er at 1 GHz).
Signal layers use Fusion's cloud numbering (1, 2, 303, 304 for 4 layers);
`copper` lists them in stack order so callers can map classic export layer
numbers (1, 2, 15, 16) by position.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


def _mm(v: str | None) -> float | None:
    if not v:
        return None
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*([a-z]*)", v)
    if not m:
        return None
    x, unit = float(m.group(1)), m.group(2)
    return {"mm": x, "": x, "mil": x * 0.0254, "mic": x / 1000, "um": x / 1000, "in": x * 25.4}.get(unit, x)


def _float(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


@dataclass
class Layer:
    kind: str                 # Signal, Prepreg, Core, Solder Mask, ...
    name: str
    number: int | None
    thickness_mm: float | None
    er: float | None = None
    df: float | None = None
    material: str = ""


@dataclass
class Stackup:
    name: str
    layers: list[Layer] = field(default_factory=list)
    source: str = ""

    @property
    def copper(self) -> list[Layer]:
        return [l for l in self.layers if l.kind == "Signal"]

    def neighbours(self, copper_index: int) -> tuple[Layer | None, Layer | None]:
        """Dielectric directly above and below the copper layer at this stack position."""
        sig = self.copper[copper_index]
        i = self.layers.index(sig)
        up = next((l for l in reversed(self.layers[:i]) if l.kind in ("Prepreg", "Core")), None)
        down = next((l for l in self.layers[i + 1:] if l.kind in ("Prepreg", "Core")), None)
        if up is not None and self.layers.index(up) < self.layers.index(self.copper[0]):
            up = None
        if down is not None and self.layers.index(down) > self.layers.index(self.copper[-1]):
            down = None
        return up, down

    def total_mm(self) -> float:
        return round(sum(l.thickness_mm or 0 for l in self.layers
                         if l.kind in ("Signal", "Prepreg", "Core")), 4)

    def as_dict(self) -> dict:
        return {"name": self.name, "source": self.source, "board_thickness_mm": self.total_mm(),
                "layers": [{"kind": l.kind, "name": l.name, "number": l.number,
                            "thickness_mm": l.thickness_mm, "er": l.er, "df": l.df, "material": l.material}
                           for l in self.layers if l.kind in ("Signal", "Prepreg", "Core", "Solder Mask")]}


def parse_stackup(xml: bytes | str | ET.Element, source: str = "") -> Stackup | None:
    root = xml if isinstance(xml, ET.Element) else ET.fromstring(xml)
    el = root if root.tag == "layerstackup" else root.find(".//layerstackup")
    if el is None:
        return None
    st = Stackup(el.get("name", ""), source=source)
    for ld in el.iterfind("layerdef"):
        m = ld.find("material")
        m = m if m is not None else ET.Element("material")
        kind = ld.get("type", "")
        num = ld.get("layer")
        st.layers.append(Layer(
            kind=kind, name=ld.get("name", ""),
            number=int(num) if (num and kind == "Signal") else None,
            thickness_mm=_mm(m.get("thickness")),
            er=_float(m.get("dielectric_constant_1g") or m.get("dielectric_constant")),
            df=_float(m.get("dissipation_factor_1g") or m.get("dissipation_factor")),
            material=" ".join(x for x in (m.get("oem"), m.get("oem_material"), m.get("description")) if x)))
    return st


@dataclass
class ClearanceRule:
    name: str
    value_mm: float
    one: str
    other: str
    same_signal: bool
    enabled: bool


def parse_clearance_rules(xml: bytes | str | ET.Element) -> tuple[list[ClearanceRule], dict[str, str]]:
    """V2 copper clearance rules and the class-number -> name map, from an
    .edru or a cloud-format board."""
    root = xml if isinstance(xml, ET.Element) else ET.fromstring(xml)
    classes = {c.get("number", ""): c.get("name", "") for c in root.iter("class")}
    rules = []
    for r in root.iter("rule"):
        if r.get("type") != "Copper Clearance":
            continue
        rules.append(ClearanceRule(r.get("name", ""), _mm(r.get("value")) or 0.0, r.get("onescope", "all"),
                                   r.get("otherscope", "all"), r.get("samesignal") == "yes",
                                   r.get("enabled", "yes") == "yes"))
    return rules, classes


def class_clearance(rules: list[ClearanceRule], class_number: str) -> list[ClearanceRule]:
    """Enabled rules whose scope names this net class (onescope='classes=4')."""
    out = []
    for r in rules:
        if not r.enabled:
            continue
        for scope in (r.one, r.other):
            m = re.match(r"classes=([\d,]+)", scope or "")
            if m and class_number in m.group(1).split(","):
                out.append(r)
                break
    return out
