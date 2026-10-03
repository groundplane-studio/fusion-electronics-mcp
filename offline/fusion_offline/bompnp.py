# Ported from Groundplane fabhub/bompnp.py (som_builder repo), unchanged except imports.
"""JLCPCB BOM + PNP generation - server-side port of jlcpcb_export_with_MF_MP_EQUIV.ulp.

This is the single source of truth for what the ULP used to compute inside Fusion:
exclusion rules, BOM grouping, ohm-symbol repair, and the JLC offset/rotation model
(component-space offsets rotated into board space by the final angle, X inverted on
the bottom side, everything suppressed when the package carries the JLC_FOOTPRINT
layer marker). EQUIV_OK is intentionally dropped (2026-08-28): substitution
decisions move to the cost tooling.

Byte-format notes kept from the ULP: BOM CSVs carry a UTF-8 BOM for Excel, the PNP
does not; coordinates are %.2f; rotations are %.2f with trailing zeros trimmed.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

from .eagle import Board, Element

# ASCII parens: the ULP source has fullwidth ones, but Fusion's printf transliterates
# them on output (same as it does the ohm symbol), so shipped CSVs use ASCII.
BOM_HEADER = "QTY,Comment,Designator,Footprint,JLCPCB Part #(optional),MF,MP"
BOM_EXCL_HEADER = "Designator,Value,Footprint,JLCPCB Part #,MF,MP,Reason"
PNP_HEADER = "Designator,Mid X,Mid Y,Layer,Rotation"
PNP_EXCL_HEADER = "Designator,Value,Layer,Reason,X (mm),Y (mm),Rotation"


# ---------------------------------------------------------------------------
# exclusion rules (same patterns, same precedence as the ULP)


def exclusion_reason(el: Element) -> str | None:
    name, value, pkg = el.name, el.value, el.package
    if "_NC" in name:
        return "_NC in name"
    if "NC/" in value:
        return "NC/ in value"
    if "MOUNTHOLES" in name:
        return "MOUNTHOLES in name"
    if "MOUNTHOLE" in name:
        return "MOUNTHOLE in name"
    if "MOUNTHOLES" in value:
        return "MOUNTHOLES in value"
    if "MOUNTHOLE" in value:
        return "MOUNTHOLE in value"
    if "MOUNTHOLES" in pkg:
        return "MOUNTHOLES in footprint"
    if "MOUNTHOLE" in pkg:
        return "MOUNTHOLE in footprint"
    if "TEST_POINT" in pkg:
        return "TEST_POINT in footprint"
    if "TESTPOINT" in pkg:
        return "TESTPOINT in footprint"
    if "TP" in name:
        return "TP in name"
    return None


# ---------------------------------------------------------------------------
# ohm-symbol repair (port of printResOhmValue)

_OHM_FORMS = {"\u03a9", "\u2126", "O"}  # greek omega, ohm sign, transliterated ASCII


def normalize_ohms(value: str) -> str:
    """Rewrite ohm markers (Ω, Ω-sign, or Fusion's transliterated ASCII 'O') to Ω,
    only where they follow a digit or a digit+multiplier (k/K/M/m), like the ULP."""
    out: list[str] = []
    for i, c in enumerate(value):
        if c in _OHM_FORMS:
            prev = value[i - 1] if i > 0 else ""
            prev2 = value[i - 2] if i > 1 else ""
            is_ohm = prev.isdigit() or (prev in "kKMm" and prev2.isdigit())
            if is_ohm:
                out.append("\u03a9")
                continue
        out.append(c)
    return "".join(out)


# ---------------------------------------------------------------------------
# JLC offset model (port of getEffectiveOffsetX/Y + the PNP export block)


def _effective_offset(x_off: float, y_off: float, angle_deg: float) -> tuple[float, float]:
    a = angle_deg % 360.0
    q = int((a + 45) // 90) % 4
    if q == 0:
        return x_off, y_off
    if q == 1:
        return -y_off, x_off
    if q == 2:
        return -x_off, -y_off
    return y_off, -x_off


def _parse_attr_real(s: str) -> float:
    s = (s or "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


_GRID = 320000          # EAGLE internal resolution: 1/320000 mm (3.125 nm)
_INV_GRID = 1.0 / _GRID   # the inexact precomputed inverse u2mm multiplies by


def _u2mm_like(v: float) -> float:
    """Reproduce the exact double the ULP sees for a coordinate. EAGLE stores
    positions as integers on a 3.125nm grid and u2mm() MULTIPLIES by a precomputed
    double(1/320000); that inexact multiply is why 53.535 prints as 53.54 while
    44.095 prints as 44.09. Reconstructing the grid integer from the XML decimal
    and repeating the multiply matched the golden export 8/8 on every case where
    naive parsing misrounds."""
    return round(v * _GRID) * _INV_GRID


def _fmt2(x: float) -> str:
    """%.2f as EAGLE's printf does it: correctly rounded from the BINARY double,
    ties away from zero. Python's f-string rounds ties to even and misrounded 115
    golden cells on the shop's 0.125mm placement grid."""
    from decimal import Decimal, ROUND_HALF_UP
    return str(Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _fmt_rotation(angle: float) -> str:
    a = angle % 360.0
    s = _fmt2(a).rstrip("0").rstrip(".")
    return s or "0"


@dataclass
class PnpRow:
    designator: str
    x: float
    y: float
    layer: str            # Top | Bottom
    rotation: str
    used_offsets: bool
    marker_suppressed: bool


def pnp_placement(board: Board, el: Element,
                  overrides: dict[str, tuple[float, float, float]] | None = None,
                  forced: dict[str, tuple[float, float, float]] | None = None
                  ) -> PnpRow:
    """One element's placement with the full JLC offset/marker model applied.

    Two override tiers, both designator -> (rotation, dx, dy):
    `forced` = human-saved placements from the tool's parts library; the tool
    is the system of record for corrections, so these beat everything, marker
    and library attributes included. `overrides` = automatic derivations from
    EasyEDA pad matching; those apply ONLY when the library gives no answer
    itself (no marker, no JLC-* attributes)."""
    marker = board.element_has_marker(el)
    has_offset_attrs = bool(el.attr("JLC-X-OFFSET") or el.attr("JLC-Y-OFFSET")
                            or el.attr("JLC-ROTATION"))
    ov = (forced or {}).get(el.name)
    if ov is None:
        ov = ((overrides or {}).get(el.name)
              if not marker and not has_offset_attrs else None)
    if ov is not None:
        rot_o, dx_o, dy_o = ov
        x_off, y_off = dx_o, dy_o
        angle = el.angle + rot_o
        has_offset_attrs = True   # count it in the stats like attribute offsets
        marker = False            # a forced fix replaces the marker's answer
    elif marker:
        x_off = y_off = 0.0
        angle = el.angle
    else:
        x_off = _parse_attr_real(el.attr("JLC-X-OFFSET"))
        y_off = _parse_attr_real(el.attr("JLC-Y-OFFSET"))
        angle = el.angle + _parse_attr_real(el.attr("JLC-ROTATION"))
    eff_x, eff_y = _effective_offset(x_off, y_off, angle)
    if el.mirror:
        eff_x = -eff_x
    return PnpRow(designator=el.name,
                  x=_u2mm_like(el.x) + eff_x, y=_u2mm_like(el.y) + eff_y,
                  layer="Bottom" if el.mirror else "Top",
                  rotation=_fmt_rotation(angle),
                  used_offsets=has_offset_attrs and not marker,
                  marker_suppressed=has_offset_attrs and marker)


# ---------------------------------------------------------------------------
# BOM grouping


@dataclass
class BomRow:
    qty: int
    value: str
    designators: list[str]
    footprint: str
    jlcpcb: str
    mf: str
    mp: str


@dataclass
class ExportResult:
    bom: list[BomRow] = field(default_factory=list)
    bom_excluded: list[tuple[Element, str]] = field(default_factory=list)
    pnp: list[PnpRow] = field(default_factory=list)
    pnp_excluded: list[tuple[Element, str, PnpRow]] = field(default_factory=list)

    @property
    def stats(self) -> dict:
        return {
            "bom_unique": len(self.bom),
            "bom_total": sum(r.qty for r in self.bom),
            "excluded": len(self.bom_excluded),
            "pnp_top": sum(1 for r in self.pnp if r.layer == "Top"),
            "pnp_bottom": sum(1 for r in self.pnp if r.layer == "Bottom"),
            "offsets_applied": sum(1 for r in self.pnp if r.used_offsets),
            "offsets_marker_suppressed": sum(1 for r in self.pnp if r.marker_suppressed),
        }


def _natural_key(name: str) -> tuple:
    """EAGLE iterates B.elements in natural designator order (C9 before C10);
    match it so exports are byte-comparable with the ULP's."""
    import re
    return tuple(int(part) if part.isdigit() else part
                 for part in re.findall(r"\d+|\D+", name))


def consolidate_by_code(res: "ExportResult") -> None:
    """Merge BOM rows sharing a C-code (JLC warns on duplicate part numbers;
    decided 2026-08-29: consolidate on EXPORT, keep separate in the tool UI).
    Substitutions make same-code rows common. Lines without a code untouched."""
    by_code: dict[str, BomRow] = {}
    out: list[BomRow] = []
    for r in res.bom:
        if not r.jlcpcb:
            out.append(r)
            continue
        tgt = by_code.get(r.jlcpcb)
        if tgt is None:
            by_code[r.jlcpcb] = r
            out.append(r)
        else:
            tgt.qty += r.qty
            tgt.designators.extend(r.designators)
            tgt.mf = tgt.mf or r.mf
            tgt.mp = tgt.mp or r.mp
    for r in by_code.values():
        r.designators.sort(key=_natural_key)
    res.bom = out


def generate(board: Board,
             overrides: dict[str, tuple[float, float, float]] | None = None,
             forced: dict[str, tuple[float, float, float]] | None = None
             ) -> ExportResult:
    res = ExportResult()
    groups: dict[tuple[str, str], BomRow] = {}   # (value, package) -> row, first-seen order

    for el in sorted(board.elements, key=lambda e: _natural_key(e.name)):
        reason = exclusion_reason(el)
        if reason:
            res.bom_excluded.append((el, reason))
            res.pnp_excluded.append((el, reason,
                                     pnp_placement(board, el, overrides, forced)))
            continue

        key = (el.value, el.package)
        row = groups.get(key)
        if row is None:
            row = BomRow(qty=0, value=normalize_ohms(el.value), designators=[],
                         footprint=el.package, jlcpcb="", mf="", mp="")
            groups[key] = row
            res.bom.append(row)
        row.qty += 1
        row.designators.append(el.name)
        # MF/MP stay raw (the ULP ships trailing whitespace as-is, 'Diodes
        # Incorporated ', and the golden diff holds us to that) but the C-code
        # is an IDENTIFIER: a padded 'C22373907 ' from a sloppy library attr
        # misses every cache/queue/price lookup downstream, so strip it
        row.jlcpcb = row.jlcpcb or el.attrs.get("JLCPCB", "").strip()
        row.mf = row.mf or el.attrs.get("MF", "")
        row.mp = row.mp or el.attrs.get("MP", "")

        res.pnp.append(pnp_placement(board, el, overrides, forced))

    return res


# ---------------------------------------------------------------------------
# CSV writers (formats preserved from the ULP, minus EQUIV_OK)


def _csv_field(s: str) -> str:
    """The ULP wrote fields raw; keep that, but rescue any field containing a comma
    or quote so the CSV stays parseable (deviation from the ULP, safe superset)."""
    if "," in s or '"' in s:
        return '"' + s.replace('"', '""') + '"'
    return s


def bom_csv(res: ExportResult) -> str:
    out = io.StringIO()
    out.write("\ufeff")
    out.write(BOM_HEADER + "\r\n")
    for r in res.bom:
        desig = ", ".join(r.designators)
        if r.qty > 1:
            desig = '"' + desig + '"'
        out.write(f"{r.qty},{_csv_field(r.value)},{desig},{_csv_field(r.footprint)},"
                  f"{_csv_field(r.jlcpcb)},{_csv_field(r.mf)},{_csv_field(r.mp)}\r\n")
    return out.getvalue()


def bom_excluded_csv(res: ExportResult) -> str:
    out = io.StringIO()
    out.write("\ufeff")
    out.write(BOM_EXCL_HEADER + "\r\n")
    for el, reason in res.bom_excluded:
        out.write(f"{el.name},{_csv_field(normalize_ohms(el.value))},{_csv_field(el.package)},"
                  f"{_csv_field(el.attrs.get('JLCPCB', ''))},{_csv_field(el.attrs.get('MF', ''))},"
                  f"{_csv_field(el.attrs.get('MP', ''))},{reason}\r\n")
    return out.getvalue()


def pnp_csv(res: ExportResult) -> str:
    out = io.StringIO()
    out.write(PNP_HEADER + "\r\n")
    for r in res.pnp:
        out.write(f"{r.designator},{_fmt2(r.x)},{_fmt2(r.y)},{r.layer},{r.rotation}\r\n")
    return out.getvalue()


def pnp_excluded_csv(res: ExportResult) -> str:
    out = io.StringIO()
    out.write("\ufeff")
    out.write(PNP_EXCL_HEADER + "\r\n")
    for el, reason, row in res.pnp_excluded:
        out.write(f"{el.name},{_csv_field(normalize_ohms(el.value))},{row.layer},{reason},"
                  f"{_fmt2(row.x)},{_fmt2(row.y)},{row.rotation}\r\n")
    return out.getvalue()
