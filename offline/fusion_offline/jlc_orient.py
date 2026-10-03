"""JLC placement orientation: derive the rotation/offset JLC needs for a part.

JLC places every part with ITS footprint for the C-number, which is the
EasyEDA footprint. A footprint from anywhere else (KiCad libraries, an
existing EAGLE/Fusion library) can have a different zero orientation or
origin, and the CPL then puts the part turned or shifted (seen on the IO
passthrough: J1, J5, J6 in JLC's placement preview).

The model (from Groundplane's export ULP math, as in bompnp): JLC rotates the
EasyEDA footprint by (element angle + JLC-ROTATION) and centres its origin at
element position + offset rotated into board space. For the copper to line
up, at element angle 0:

    our_pad_i = Rot_R(offset + easy_pad_i)
 => Rot_{-R}(our_pad_i) - easy_pad_i = offset   (the same vector for all i)

So the right R in {0, 90, 180, 270} is the one where that difference collapses
to one vector, and that vector is the offset. Pads are matched by name; when
names do not line up (A/K vs 1/2) a geometric pairing is used and the result
is flagged for a person to confirm polarity. Ported from fabhub rotmatch (same
authors), which was validated against hand-tuned parts of a production board.

EasyEDA footprint data: 1 canvas unit = 10 mil = 0.254 mm; canvas Y grows
down, so Y is negated.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass

_MM = 0.254


def easyeda_pads(result: dict) -> list[tuple[str, float, float]]:
    """(name, x_mm, y_mm) of the footprint pads in an EasyEDA component
    response ('result'), relative to the footprint origin, Y up."""
    data_str = ((result.get("packageDetail") or {}).get("dataStr") or {})
    head = data_str.get("head") or {}
    hx, hy = float(head.get("x", 0)), float(head.get("y", 0))
    pads = []
    for shape in data_str.get("shape") or []:
        if not shape.startswith("PAD~"):
            continue
        f = shape.split("~")
        # PAD~shape~cx~cy~w~h~layer~net~number~holeR~points~rotation~id~...
        try:
            pads.append((str(f[8]), (float(f[2]) - hx) * _MM, -(float(f[3]) - hy) * _MM))
        except (ValueError, IndexError):
            continue
    return pads


def _rot(x: float, y: float, deg: int) -> tuple[float, float]:
    if deg == 0:
        return x, y
    if deg == 90:
        return -y, x
    if deg == 180:
        return -x, -y
    return y, -x


@dataclass
class Derived:
    rotation: int           # JLC-ROTATION
    dx: float               # JLC-X-OFFSET
    dy: float               # JLC-Y-OFFSET
    error: float            # mean residual (mm) after the transform
    matched_pads: int
    mode: str = "named"     # named | geometry
    ambiguous: bool = False
    second_rotation: int | None = None
    agreeing_pads: int = 0  # pads that land within CONSENSUS_MM at this offset
    outliers: list = None   # [(our pad, jlc pad, (dx, dy) mm)] that do not: the footprints differ there

    @property
    def trustworthy(self) -> bool:
        return (self.matched_pads >= 2 and not self.ambiguous and not self.outliers
                and self.error <= 0.35)

    @property
    def is_identity(self) -> bool:
        return self.rotation == 0 and abs(self.dx) < 0.06 and abs(self.dy) < 0.06

    def as_dict(self) -> dict:
        return {"rotation": self.rotation, "dx_mm": self.dx, "dy_mm": self.dy, "residual_mm": self.error,
                "matched_pads": self.matched_pads, "mode": self.mode, "ambiguous": self.ambiguous,
                "second_rotation": self.second_rotation, "trustworthy": self.trustworthy,
                "needs_correction": not self.is_identity, "agreeing_pads": self.agreeing_pads,
                "footprint_differs_at": [{"ours": o, "jlc": j, "delta_mm": [round(d[0], 3), round(d[1], 3)]}
                                         for o, j, d in (self.outliers or [])]}


def _eval(pairs, r):
    diffs = []
    for (gx, gy), (ex, ey) in pairs:
        rgx, rgy = _rot(gx, gy, (-r) % 360)
        diffs.append((rgx - ex, rgy - ey))
    cx = sum(d[0] for d in diffs) / len(diffs)
    cy = sum(d[1] for d in diffs) / len(diffs)
    mean = sum(math.hypot(d[0] - cx, d[1] - cy) for d in diffs) / len(diffs)
    return cx, cy, mean


CONSENSUS_MM = 0.05
PITCH_MM = 0.15


def easyeda_pin_names(result: dict) -> dict[str, str]:
    """{pad number: pin name} from the EasyEDA symbol (its pin numbers are the
    footprint's pad numbers)."""
    out = {}
    for shape in ((result.get("dataStr") or {}).get("shape") or []):
        if not shape.startswith("P~"):
            continue
        sec = shape.split("^^")
        try:
            num = str(sec[0].split("~")[3])
            f = sec[3].split("~") if len(sec) > 3 else []
            out[num] = f[4] if len(f) > 4 and f[4] else num
        except IndexError:
            continue
    return out


def by_function(ours, our_names: dict, easy, easy_names: dict):
    """Relabel both pad lists with pin FUNCTION names when both sides name at least
    two pins by function: pad numbers can mean different pins (an SS54 is pad 1 =
    cathode in KiCad's SMA, pad 1 = anode in JLC's), and matching by number would
    then turn the part around. Returns (ours, easy, used)."""
    def fn(n):
        return n and not n.isdigit() and n.upper() not in ("NC", "~")
    on = {p: our_names.get(p, p) for p, _, _ in ours}
    en = {p: easy_names.get(p, p) for p, _, _ in easy}
    shared = {v for v in on.values() if fn(v)} & {v for v in en.values() if fn(v)}
    if len(shared) < 2 or len(set(on.values())) != len(on) or len(set(en.values())) != len(en):
        return ours, easy, False
    return ([(on[p], x, y) for p, x, y in ours], [(en[p], x, y) for p, x, y in easy], True)


def pin_names_from_schematic(sch_root: ET.Element, ref: str) -> dict[str, str]:
    """{pad: pin name} for a part, from the schematic's device connects."""
    part = next((q for q in sch_root.iter("part") if q.get("name") == ref), None)
    if part is None:
        return {}
    for lib in sch_root.iter("library"):
        if lib.get("name") != part.get("library"):
            continue
        for ds in lib.iter("deviceset"):
            if ds.get("name") != part.get("deviceset"):
                continue
            for dv in ds.iter("device"):
                if (dv.get("name") or "") == (part.get("device") or ""):
                    return {pad: c.get("pin") for c in dv.iter("connect") for pad in (c.get("pad") or "").split()}
    return {}


def _consensus(pairs, r):
    """(dx, dy, agreeing, outlier indices): the offset that the most pairs agree
    on within CONSENSUS_MM (each pair's own offset tried as a candidate), refined
    as the mean over those pairs. A mean over all pairs blurs a footprint that is
    simply different in a few pads (the HCTL RJ45+USB: 14 pads exact, the 8 jack
    signal pads 0.55 mm off) into an offset that fits nothing."""
    offs = []
    for (gx, gy), (ex, ey) in pairs:
        rgx, rgy = _rot(gx, gy, (-r) % 360)
        offs.append((rgx - ex, rgy - ey))
    best = None
    for cx, cy in offs:
        idx = [i for i, (ox, oy) in enumerate(offs) if math.hypot(ox - cx, oy - cy) <= CONSENSUS_MM]
        if best is None or len(idx) > len(best):
            best = idx
    mx = sum(offs[i][0] for i in best) / len(best)
    my = sum(offs[i][1] for i in best) / len(best)
    out = [i for i in range(len(offs)) if i not in best]
    return mx, my, len(best), out, offs


def _geom_pairs(ours, easy, r):
    rot = [_rot(x, y, (-r) % 360) for _, x, y in ours]
    gcx, gcy = sum(p[0] for p in rot) / len(rot), sum(p[1] for p in rot) / len(rot)
    ecx, ecy = sum(x for _, x, _ in easy) / len(easy), sum(y for _, _, y in easy) / len(easy)
    aligned = [(x - gcx + ecx, y - gcy + ecy) for x, y in rot]
    taken: set[int] = set()
    pairs = []
    for (ax, ay), (_, gx, gy) in zip(aligned, ours):
        best_j, best_d = None, None
        for j, (_, ex, ey) in enumerate(easy):
            if j in taken:
                continue
            d = math.hypot(ax - ex, ay - ey)
            if best_d is None or d < best_d:
                best_j, best_d = j, d
        if best_j is None:
            return None
        taken.add(best_j)
        pairs.append(((gx, gy), (easy[best_j][1], easy[best_j][2])))
    return pairs


def derive(ours: list[tuple[str, float, float]], easy: list[tuple[str, float, float]]) -> Derived | None:
    """ours / easy: (pad name, x_mm, y_mm) in footprint coordinates."""
    if not ours or not easy:
        return None
    easy_by_name: dict[str, tuple[float, float]] = {}
    for name, x, y in easy:
        easy_by_name.setdefault(name, (x, y))
    named = [((gx, gy), easy_by_name[n]) for n, gx, gy in ours if n in easy_by_name]
    scored: list[tuple[float, Derived]] = []
    if len(named) >= 2:
        for r in (0, 90, 180, 270):
            dx, dy, mean = _eval(named, r)
            scored.append((mean, Derived(r, round(dx, 4), round(dy, 4), round(mean, 4), len(named), "named")))
    else:
        for r in (0, 90, 180, 270):
            pairs = _geom_pairs(ours, easy, r)
            if pairs and len(pairs) >= 2:
                dx, dy, mean = _eval(pairs, r)
                scored.append((mean, Derived(r, round(dx, 4), round(dy, 4), round(mean, 4), len(pairs), "geometry")))
    if not scored:
        return None
    scored.sort(key=lambda t: t[0])
    best = scored[0][1]
    # names pair up but land far apart: the footprints number pins differently
    # (flipped connectors); geometry finds the alignment, polarity is a human call
    if best.mode == "named" and best.error > 0.5:
        geo = []
        for r in (0, 90, 180, 270):
            pairs = _geom_pairs(ours, easy, r)
            if pairs and len(pairs) >= 2:
                dx, dy, mean = _eval(pairs, r)
                geo.append((mean, Derived(r, round(dx, 4), round(dy, 4), round(mean, 4), len(pairs), "geometry")))
        if geo:
            geo.sort(key=lambda t: t[0])
            if geo[0][0] < best.error:
                scored, best = geo, geo[0][1]
                best.ambiguous = True
                if len(scored) > 1:
                    best.second_rotation = scored[1][1].rotation
    if len(scored) > 1 and not best.ambiguous and scored[1][0] - best.error < 0.2:
        # a runner-up nearly as good: the pattern is symmetric under that turn
        best.ambiguous = True
        best.second_rotation = scored[1][1].rotation
    # offset by consensus, and the pads where the two footprints really differ
    if best.mode == "named":
        named_full = [(n, (gx, gy), easy_by_name[n]) for n, gx, gy in ours if n in easy_by_name]
        pairs, names = [(a, b) for _, a, b in named_full], [(n, n) for n, _, _ in named_full]
    else:
        pairs = _geom_pairs(ours, easy, best.rotation) or []
        lut = {(round(x, 4), round(y, 4)): n for n, x, y in easy}
        names = [(n, lut.get((round(b[0], 4), round(b[1], 4)), "?")) for (n, _, _), (a, b) in zip(ours, pairs)]
    if len(pairs) >= 2:
        mx, my, agree, out, offs = _consensus(pairs, best.rotation)
        # all pads within PITCH_MM of the mean fit: the footprints differ only in pad pitch
        # rounding (KiCad vs EasyEDA TSOT-23-6 rows 2.27 vs 2.4 mm), keep the mean fit
        cx = sum(o[0] for o in offs) / len(offs)
        cy = sum(o[1] for o in offs) / len(offs)
        worst = max(math.hypot(o[0] - cx, o[1] - cy) for o in offs)
        if worst > PITCH_MM and agree >= max(2, len(pairs) // 2):
            best.dx, best.dy = round(mx, 4), round(my, 4)
            best.agreeing_pads = agree
            best.outliers = [(names[i][0], names[i][1], (offs[i][0] - mx, offs[i][1] - my)) for i in out]
            resid = [math.hypot(offs[i][0] - mx, offs[i][1] - my) for i in range(len(offs)) if i not in out]
            best.error = round(sum(resid) / len(resid), 4) if resid else best.error
    return best


def package_pads(root: ET.Element, ref: str) -> tuple[list[tuple[str, float, float]], dict] | None:
    """(pads in the package's own coordinates, element info) for a placed part."""
    board = root.find("./drawing/board")
    el = next((e for e in board.iterfind("./elements/element") if e.get("name") == ref), None)
    if el is None:
        return None
    pk = next((p for lib in board.iterfind("./libraries/library") if lib.get("name") == el.get("library")
               for p in lib.iterfind("./packages/package") if p.get("name") == el.get("package")), None)
    if pk is None:
        return None
    pads = [(s.get("name"), float(s.get("x")), float(s.get("y")))
            for s in list(pk.iterfind("smd")) + list(pk.iterfind("pad"))]
    attrs = {a.get("name"): a.get("value") for a in el.iterfind("attribute")}
    return pads, {"package": el.get("package"), "library": el.get("library"), "rot": el.get("rot"),
                  "attributes": attrs}
