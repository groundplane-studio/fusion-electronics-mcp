"""EAGLE command builders, encoding the rules found in the Phase 1 spike:

- every command string starts with GRID MM;
- names are always single-quoted ('R1' unquoted parses as a 1 degree angle);
- MIRROR, PACKAGE and NAME ignore names in Fusion and must target a
  coordinate; bottom-side placement uses ROTATE =MR<angle>;
- VALUE on a device set without user values opens a modal dialog (SET
  CONFIRM YES does not suppress it), so it is refused before sending.
"""

from __future__ import annotations

import re

from fusion_offline.design import SchematicDesign

GRID = "GRID MM;"
_SAFE = re.compile(r"^[A-Za-z0-9_$#+\-./%:@!&=*() ]+$")


class InvalidInput(ValueError):
    pass


def q(s: str) -> str:
    s = str(s)
    if "'" in s or ";" in s or not s:
        raise InvalidInput(f"name {s!r} is empty or contains a quote/semicolon")
    return f"'{s}'"


def n(v: float) -> str:
    """A number for a command, to 0.1 um. (Not :g: it keeps 6 significant digits, so 235.5175 mm
    went out as 235.518.)"""
    s = f"{round(float(v), 4):.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def pt(x: float, y: float) -> str:
    return f"({n(x)} {n(y)})"


def sheet_prefix(sheet: int | None) -> str:
    return f"EDIT .s{int(sheet)};" if sheet else ""


def orient(angle: float, bottom: bool = False) -> str:
    a = round(float(angle) % 360, 1)
    return f"={'M' if bottom else ''}R{n(a)}"


# -- board -------------------------------------------------------------------
def move_part(ref: str, x: float, y: float) -> str:
    return f"{GRID} MOVE {q(ref)} {pt(x, y)};"


def rotate_part(ref: str, angle: float, bottom: bool) -> str:
    return f"{GRID} ROTATE {orient(angle, bottom)} {q(ref)};"


def add_via(net: str, x: float, y: float, drill: float, diameter: float | None = None) -> str:
    dia = n(diameter) if diameter else "auto"
    return f"{GRID} CHANGE DRILL {n(drill)}; VIA {q(net)} {dia} round {pt(x, y)};"


def add_trace(net: str, layer: int, width: float, points: list[tuple[float, float]]) -> str:
    if len(points) < 2:
        raise InvalidInput("a trace needs at least two points")
    # a point may carry a third value: the arc (degrees, + = counter-clockwise)
    # of the segment that ends at it, written as EAGLE's curve before the point
    path = " ".join((f"{float(p[2]):+g} " if len(p) > 2 and p[2] else "") + pt(p[0], p[1]) for p in points)
    return f"{GRID} LAYER {int(layer)}; CHANGE WIDTH {n(width)}; WIRE {q(net)} {n(width)} {path};"


def define_net_class(number: int, name: str) -> str:
    return f"CLASS {int(number)} {q(name)};"


# -- schematic ---------------------------------------------------------------
def add_part(device: str, library: str, ref: str, x: float, y: float, angle: float = 0,
             sheet: int | None = None) -> str:
    return (f"{GRID} {sheet_prefix(sheet)} ADD {q(device + '@' + library)} {q(ref)} "
            f"R{n(angle % 360)} {pt(x, y)};")


def net_stub(net: str, x: float, y: float, outward: tuple[float, float], length: float = 5.08,
             sheet: int | None = None, label: bool = True) -> str:
    """A short named wire leaving a pin in the direction it faces, with a net
    label at its outer end (LABEL: first point picks the wire, second places
    the text), so pieces of a net that are not drawn as connected are still
    visibly the same net."""
    ex, ey = x + outward[0] * length, y + outward[1] * length
    cmd = f"{GRID} {sheet_prefix(sheet)} NET {q(net)} {pt(x, y)} {pt(ex, ey)};"
    if label:
        mx, my = x + outward[0] * length / 2, y + outward[1] * length / 2
        cmd += f" LABEL {pt(mx, my)} {pt(ex, ey)};"
    return cmd


def new_sheet(number: int, title: str, frame: str | None, frame_ref: str | None) -> str:
    """Create sheet `number` (EDIT .sN on the next number creates it), place
    the frame device at the origin, and set the sheet description (shown by
    title blocks that use >SHEET_HEADLINE)."""
    cmd = f"EDIT .s{int(number)}; {GRID}"
    if frame:
        cmd += f" ADD {q(frame)} {q(frame_ref)} R0 (0 0);"
    if title:
        cmd += f" DESCRIPTION {q(title)};"
    return cmd


def net_wire(net: str, points: list[tuple[float, float]], sheet: int | None = None) -> str:
    return f"{GRID} {sheet_prefix(sheet)} NET {q(net)} " + " ".join(pt(x, y) for x, y in points) + ";"


def rename_net_at(new_name: str, x: float, y: float, sheet: int | None = None) -> str:
    return f"{GRID} {sheet_prefix(sheet)} NAME {q(new_name)} {pt(x, y)};"


def set_variant_at(device_variant: str, x: float, y: float, sheet: int | None = None) -> str:
    return f"{GRID} {sheet_prefix(sheet)} PACKAGE {q(device_variant)} {pt(x, y)};"


def set_value(sch: SchematicDesign, ref: str, value: str) -> str:
    part = sch.parts.get(ref)
    if part is None:
        raise InvalidInput(f"no part {ref!r} in the schematic")
    if not part.user_value:
        raise InvalidInput(
            f"{ref} ({part.deviceset}) has no user-definable value; Fusion would block on a dialog. "
            "Change the value by switching to another device variant (set_part_variant).")
    return f"VALUE {q(ref)} {q(value)};"
