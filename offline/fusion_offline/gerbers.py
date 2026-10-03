"""Check a CAM output (gerbers + drills, zip or folder) against the design.

The RS-274X / Excellon parsers, layer classification and drill matching are
ported from Groundplane's fabhub gerbercheck (same authors, MIT here). The
check itself is new: instead of comparing two gerber sets, it compares the
CAM output with the board export the gerbers should represent:
- the layer set is complete (copper count = stackup, mask, silk, paste where
  the board has SMD pads, outline, drill);
- the outline's size is the board's;
- every drilled hole of the design (pads, vias, holes) is in the drill files,
  diameter and position, and nothing extra is;
- every SMD pad has paste and a mask opening over it (flash, stroke or
  region: Fusion strokes rounded pads); flash counts are reported too.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .eagle import parse_rot


@dataclass
class GerberStats:
    path: str = ""
    unit: str = "mm"
    flashes: int = 0
    draws: int = 0
    apertures: int = 0
    bbox: tuple[float, float, float, float] | None = None   # minx, miny, maxx, maxy

    @property
    def size(self) -> tuple[float, float] | None:
        if not self.bbox:
            return None
        return (round(self.bbox[2] - self.bbox[0], 3), round(self.bbox[3] - self.bbox[1], 3))


_FS_RE = re.compile(r"%FS(?P<zeros>[LT]?)A?X(?P<xi>\d)(?P<xd>\d)Y(?P<yi>\d)(?P<yd>\d)\*%")
_COORD_RE = re.compile(r"(?:X(?P<x>-?\d+))?(?:Y(?P<y>-?\d+))?(?:I-?\d+)?(?:J-?\d+)?"
                       r"D(?P<op>0?[123])\*")


def parse_gerber(path: str) -> GerberStats:
    st = GerberStats(path=path)
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()

    m = _FS_RE.search(text)
    int_digits, dec_digits, trailing_omit = 3, 4, False
    if m:
        int_digits, dec_digits = int(m.group("xi")), int(m.group("xd"))
        trailing_omit = m.group("zeros") == "T"
    st.unit = "in" if "%MOIN*%" in text else "mm"
    scale = 10.0 ** dec_digits
    st.apertures = len(re.findall(r"%ADD\d+", text))

    def coord(raw: str) -> float:
        if trailing_omit:
            neg = raw.startswith("-")
            digits = raw.lstrip("-").ljust(int_digits + dec_digits, "0")
            v = int(digits)
            return (-v if neg else v) / scale
        return int(raw) / scale

    x = y = 0.0
    minx = miny = math.inf
    maxx = maxy = -math.inf

    def commit(px: float, py: float) -> None:
        nonlocal minx, miny, maxx, maxy
        minx, miny = min(minx, px), min(miny, py)
        maxx, maxy = max(maxx, px), max(maxy, py)

    for cm in _COORD_RE.finditer(text):
        px, py = x, y
        if cm.group("x") is not None:
            x = coord(cm.group("x"))
        if cm.group("y") is not None:
            y = coord(cm.group("y"))
        op = cm.group("op").lstrip("0")
        if op == "1":
            st.draws += 1
            commit(px, py)   # a stroke covers its start point too
            commit(x, y)
        elif op == "3":
            st.flashes += 1
            commit(x, y)
    if minx is not math.inf:
        st.bbox = (minx, miny, maxx, maxy)
    if st.unit == "in" and st.bbox:
        st.bbox = tuple(v * 25.4 for v in st.bbox)  # normalize to mm
    return st


# ---------------------------------------------------------------------------
# Excellon parsing


@dataclass
class DrillFile:
    path: str = ""
    holes: list[tuple[float, float, float]] = field(default_factory=list)   # (dia, x, y)

    @property
    def histogram(self) -> dict[float, int]:
        h: dict[float, int] = {}
        for d, _, _ in self.holes:
            h[d] = h.get(d, 0) + 1
        return dict(sorted(h.items()))


_TOOL_DEF_RE = re.compile(r"^T(\d+)C([\d.]+)")
_TOOL_SEL_RE = re.compile(r"^T(\d+)\s*$")
_XY_RE = re.compile(r"^X(-?[\d.]+)Y(-?[\d.]+)")
_FMT_RE = re.compile(r"^(?:METRIC|INCH)\s*,\s*[LT]Z\s*,\s*(0+)\.(0+)")


def parse_excellon(path: str) -> DrillFile:
    df = DrillFile(path=path)
    tools: dict[str, float] = {}
    cur = 0.0
    unit_scale = 1.0
    dec_digits = 0    # for decimal-point-less coordinate styles (e.g. EAGLE METRIC,TZ,000.000)
    in_header = True
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    for line in lines:
        line = line.strip()
        if line.startswith(("INCH", "M72")):
            unit_scale = 25.4
        elif line.startswith(("METRIC", "M71")):
            unit_scale = 1.0
        m = _FMT_RE.match(line)
        if m:
            dec_digits = len(m.group(2))
        if line == "%" or line.startswith("M95"):
            in_header = False
            continue
        m = _TOOL_DEF_RE.match(line)
        if m:
            tools[m.group(1)] = float(m.group(2)) * unit_scale
            continue
        m = _TOOL_SEL_RE.match(line)
        if m and not in_header:
            cur = tools.get(m.group(1), 0.0)
            continue
        m = _XY_RE.match(line)
        if m:
            def val(raw: str) -> float:
                if "." not in raw and dec_digits:
                    return int(raw) / (10 ** dec_digits)
                return float(raw)
            df.holes.append((round(cur, 4),
                             val(m.group(1)) * unit_scale, val(m.group(2)) * unit_scale))
    return df



def match_drills(golden: DrillFile, made: DrillFile, tol: float = 0.02) -> dict:
    """Align both hole clouds on their min corner (probing a Y flip) and pair holes
    of equal diameter within `tol` mm. Returns counts + leftovers."""
    def norm(holes, flip_y):
        xs = [x for _, x, _ in holes]
        ys = [y for _, _, y in holes]
        x0 = min(xs)
        if flip_y:
            y1 = max(ys)
            return [(d, x - x0, y1 - y) for d, x, y in holes]
        y0 = min(ys)
        return [(d, x - x0, y - y0) for d, x, y in holes]

    if not golden.holes or not made.holes:
        return {"matched": 0, "golden_total": len(golden.holes),
                "made_total": len(made.holes), "unmatched_golden": golden.holes,
                "unmatched_made": made.holes, "flipped_y": False}

    best = None
    for flip in (False, True):
        g = norm(golden.holes, False)
        k = norm(made.holes, flip)
        pool = list(k)
        matched = 0
        left_g = []
        for d, x, y in g:
            hit = next((i for i, (d2, x2, y2) in enumerate(pool)
                        if abs(d2 - d) <= 0.01 and abs(x2 - x) <= tol and abs(y2 - y) <= tol),
                       None)
            if hit is None:
                left_g.append((d, x, y))
            else:
                pool.pop(hit)
                matched += 1
        result = {"matched": matched, "golden_total": len(g), "made_total": len(made.holes),
                  "unmatched_golden": left_g, "unmatched_made": pool, "flipped_y": flip}
        if best is None or result["matched"] > best["matched"]:
            best = result
    return best



def _is_bottom(n: str, tok: str) -> bool:
    """True when a lower-cased fab filename names the bottom side. Handles the
    full word ('bottom'), Fusion's abbreviation ('_bot'), the kicad 'b_<layer>'
    prefix, and Protel bottom extensions (.gbs/.gbp/.gbo)."""
    return ("bottom" in n or "_bot" in n or "b_" + tok in n
            or n.endswith(("gbs", "gbp", "gbo")))


def classify(filename: str) -> tuple[str, str] | None:
    """(kind, side) for a single fab file, across the naming conventions in use
    here: Fusion's descriptive CAM ('copper_top_l1.gbr', 'soldermask_bottom.gbr'),
    Fusion's numbered/abbreviated CAM ('copper_l1.gbr', 'Soldermask_Bot.gbr',
    'Legend_Top.gbr', 'Paste_Bot.gbr', 'Profile_NP.gbr'), and kicad-cli
    ('board-F_Cu.gtl', 'board-B_Mask.gbs', 'board.drl').

    Copper named by layer number alone carries no side, so each such file tags
    ('copper','inner') here; classify_files() resolves the true top/bottom across
    the whole set. Explicit inner copper tags ('copper','inner') too, and the set
    comparator pairs those by sorted order. None = not a fab layer (.gbrjob etc)."""
    n = filename.lower()
    if n.endswith((".xln", ".drl")) or "drill" in n:
        return ("drill", "")
    if "profile" in n or "edge_cuts" in n or n.endswith(".gm1"):
        return ("profile", "")
    for kind, hints in (("silk", ("silk", "legend")), ("mask", ("mask",)),
                        ("paste", ("paste",))):
        if any(h in n for h in hints):
            return (kind, "bottom" if _is_bottom(n, kind) else "top")
    if "copper" in n or "_cu" in n or n.endswith((".gtl", ".gbl", ".g1", ".g2", ".g3", ".g4")):
        if "top" in n or "f_cu" in n or n.endswith(".gtl"):
            return ("copper", "top")
        if "bottom" in n or "b_cu" in n or "_bot" in n or n.endswith(".gbl"):
            return ("copper", "bottom")
        return ("copper", "inner")
    return None


def classify_files(paths: list[str]) -> list[tuple[str, tuple[str, str] | None]]:
    """classify() every path, then resolve numbered copper across the whole set.

    A CAM job that names copper by layer number only ('copper_l1.gbr' ...
    'copper_l8.gbr') gives classify() no side to read, so it tags each one
    ('copper','inner'). When a set has such copper and NOT ONE file names a copper
    side, the lowest layer number becomes top, the highest bottom, the rest inner.
    Sets that already name a side (copper_top_l1, *_Cu, .gtl/.gbl) are returned
    exactly as classify() tagged them, so the descriptive and kicad conventions
    are untouched."""
    tagged = [(p, classify(os.path.basename(p))) for p in paths]
    coppers = [k for _, k in tagged if k and k[0] == "copper"]
    if len(coppers) >= 2 and not any(side in ("top", "bottom") for _, side in coppers):
        nums: dict[str, int] = {}
        for p, k in tagged:
            if k and k[0] == "copper":
                m = re.search(r"_l(\d+)", os.path.basename(p).lower())
                if m:
                    nums[p] = int(m.group(1))
        if len(nums) == len(coppers) and len(set(nums.values())) == len(nums):
            lo, hi = min(nums.values()), max(nums.values())
            side_of = {p: ("top" if v == lo else "bottom" if v == hi else "inner")
                       for p, v in nums.items()}
            tagged = [(p, ("copper", side_of[p]) if p in side_of else k)
                      for p, k in tagged]
    return tagged




# ---------------------------------------------------------------------------
# check against the design


def _f(el, k, d=0.0):
    v = el.get(k)
    return float(v) if v not in (None, "") else d


def _xf(px, py, x, y, angle, mirror):
    a = math.radians(angle)
    rx, ry = px * math.cos(a) - py * math.sin(a), px * math.sin(a) + py * math.cos(a)
    if mirror:
        rx = -rx
    return x + rx, y + ry


def design_holes(root: ET.Element) -> tuple[DrillFile, dict]:
    """Every drilled hole of the board export (pad drills, via drills, package
    and board holes), plus pad counts per side for the report."""
    board = root.find("./drawing/board")
    df = DrillFile(path="design")
    counts = {"smd_top": 0, "smd_bottom": 0, "tht_pads": 0, "vias": 0, "holes": 0}
    pkgs = {(lib.get("name"), pk.get("name")): pk for lib in board.iterfind("./libraries/library")
            for pk in lib.iterfind("./packages/package")}
    for el in board.iterfind("./elements/element"):
        pk = pkgs.get((el.get("library"), el.get("package")))
        if pk is None:
            continue
        ang, mir = parse_rot(el.get("rot"))
        ex, ey = _f(el, "x"), _f(el, "y")
        for p in pk.iterfind("pad"):
            x, y = _xf(_f(p, "x"), _f(p, "y"), ex, ey, ang, mir)
            df.holes.append((round(_f(p, "drill"), 4), x, y))
            counts["tht_pads"] += 1
        for h in pk.iterfind("hole"):
            x, y = _xf(_f(h, "x"), _f(h, "y"), ex, ey, ang, mir)
            df.holes.append((round(_f(h, "drill"), 4), x, y))
            counts["holes"] += 1
        for s in pk.iterfind("smd"):
            top = (s.get("layer") == "1") != mir
            counts["smd_top" if top else "smd_bottom"] += 1
    for s in board.iterfind("./signals/signal"):
        for v in s.iterfind("via"):
            df.holes.append((round(_f(v, "drill"), 4), _f(v, "x"), _f(v, "y")))
            counts["vias"] += 1
    for h in board.iterfind("./plain/hole"):
        df.holes.append((round(_f(h, "drill"), 4), _f(h, "x"), _f(h, "y")))
        counts["holes"] += 1
    return df, counts


def _files(path: str) -> list[str]:
    if os.path.isdir(path):
        return [os.path.join(dp, f) for dp, _, fs in os.walk(path) for f in fs]
    if zipfile.is_zipfile(path):
        tmp = tempfile.mkdtemp(prefix="fusion-mcp-gerbers-")
        with zipfile.ZipFile(path) as z:
            z.extractall(tmp)
        return [os.path.join(dp, f) for dp, _, fs in os.walk(tmp) for f in fs]
    raise ValueError(f"{path} is neither a folder nor a zip file")


def check(path: str, root: ET.Element, copper_layers: int, tol_mm: float = 0.05) -> dict:
    tagged = classify_files(_files(path))
    layers: dict[tuple[str, str], list[str]] = {}
    for p, k in tagged:
        if k:
            layers.setdefault(k, []).append(p)
    ignored = sorted(os.path.basename(p) for p, k in tagged if not k)
    problems: list[str] = []
    info: dict = {}
    board = root.find("./drawing/board")
    xs, ys = [], []
    for w in board.iterfind("./plain/wire"):
        if w.get("layer") == "20":
            xs += [_f(w, "x1"), _f(w, "x2")]
            ys += [_f(w, "y1"), _f(w, "y2")]
    outline = (round(max(xs) - min(xs), 3), round(max(ys) - min(ys), 3)) if xs else None
    holes, counts = design_holes(root)
    has_bottom_silk = any(t.get("layer") == "22" for t in board.iterfind("./plain/text"))

    copper = {s: layers.get(("copper", s), []) for s in ("top", "inner", "bottom")}
    n_cu = sum(len(v) for v in copper.values())
    info["copper_files"] = {s: sorted(os.path.basename(p) for p in v) for s, v in copper.items()}
    if n_cu != copper_layers:
        problems.append(f"{n_cu} copper layers in the CAM output, the stackup has {copper_layers}")
    for s in ("top", "bottom"):
        if len(copper[s]) != 1:
            problems.append(f"expected one {s} copper file, found {len(copper[s])}")
        nm = len(layers.get(("mask", s), []))
        if nm != 1:
            problems.append(f"expected one {s} solder mask file, found {nm}")
    if not layers.get(("silk", "top")):
        problems.append("no top silkscreen file")
    if has_bottom_silk and not layers.get(("silk", "bottom")):
        problems.append("the board has bottom silkscreen text but there is no bottom silkscreen file")
    if counts["smd_top"] and not layers.get(("paste", "top")):
        problems.append(f"{counts['smd_top']} top SMD pads but no top paste file")
    if counts["smd_bottom"] and not layers.get(("paste", "bottom")):
        problems.append(f"{counts['smd_bottom']} bottom SMD pads but no bottom paste file")

    prof = layers.get(("profile", ""), [])
    if not prof:
        problems.append("no board outline (profile) file")
    else:
        st = parse_gerber(prof[0])
        info["outline_mm"], info["design_outline_mm"] = st.size, outline
        # the outline is stroked, so its bbox can exceed the board by the line width
        if st.size and outline and (abs(st.size[0] - outline[0]) > 0.3 or abs(st.size[1] - outline[1]) > 0.3):
            problems.append(f"outline is {st.size} mm, the board is {outline} mm")

    flashes = {}
    for s in ("top", "bottom"):
        for p in copper[s] + layers.get(("mask", s), []) + layers.get(("paste", s), []):
            st = parse_gerber(p)
            flashes[os.path.basename(p)] = st.flashes
            if st.size and outline and (st.size[0] > outline[0] + 0.3 or st.size[1] > outline[1] + 0.3):
                problems.append(f"{os.path.basename(p)} is {st.size} mm, larger than the board {outline} mm")
    info["flashes"] = flashes
    # every SMD pad needs paste and a mask opening over it (flash, stroke or region)
    for side, top in (("top", True), ("bottom", False)):
        pads = smd_pads(root, top)
        if not pads:
            continue
        for kind in ("paste", "mask"):
            files = layers.get((kind, side), [])
            if not files:
                continue
            miss = uncovered_pads(files[0], pads)
            info[f"{kind}_{side}_pads_covered"] = f"{len(pads) - len(miss)} of {len(pads)}"
            if miss:
                problems.append(f"{len(miss)} {side} SMD pads have no {kind} over them: {miss[:8]}")

    drills = layers.get(("drill", ""), [])
    made = DrillFile(path="cam")
    for p in drills:
        made.holes += parse_excellon(p).holes
    info["drill_files"] = sorted(os.path.basename(p) for p in drills)
    if not drills:
        problems.append("no drill file")
    else:
        hd = {round(d, 2): n for d, n in holes.histogram.items()}
        hm = {round(d, 2): n for d, n in made.histogram.items()}
        info["drill_histogram_design"], info["drill_histogram_cam"] = hd, hm
        if hd != hm:
            problems.append(f"drill sizes/counts differ: design {hd}, CAM {hm}")
        m = match_drills(holes, made, tol=tol_mm)
        info["drills_matched"] = f"{m['matched']} of {m['golden_total']} design holes ({m['made_total']} in CAM)"
        if m["unmatched_golden"]:
            problems.append(f"{len(m['unmatched_golden'])} design holes missing or moved in the drill files, e.g. "
                            f"{[(d, round(x, 2), round(y, 2)) for d, x, y in m['unmatched_golden'][:4]]} "
                            "(relative to the hole pattern's corner)")
        if m["unmatched_made"]:
            problems.append(f"{len(m['unmatched_made'])} holes in the drill files that the design does not have")
    info["design_counts"] = counts
    return {"ok": not problems, "problems": problems, "ignored_files": ignored, **info}


# ---------------------------------------------------------------------------
# per-pad coverage (paste, mask)


def gerber_ops(path: str) -> tuple[list, list, list]:
    """(flashes [(x, y)], draws [(x1, y1, x2, y2)], regions [[(x, y), ...]]) in mm.
    Fusion draws rounded pads as strokes and some shapes as regions, so pad
    coverage cannot be judged from flash counts alone."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    m = _FS_RE.search(text)
    dec = int(m.group("xd")) if m else 4
    k = (25.4 if "%MOIN*%" in text else 1.0) / 10.0 ** dec
    flashes, draws, regions = [], [], []
    x = y = 0.0
    in_region, poly = False, []
    for tok in re.finditer(r"G36\*|G37\*|(?:X(-?\d+))?(?:Y(-?\d+))?(?:I-?\d+)?(?:J-?\d+)?D0?([123])\*", text):
        s = tok.group(0)
        if s == "G36*":
            in_region, poly = True, []
            continue
        if s == "G37*":
            in_region = False
            if poly:
                regions.append(poly)
            continue
        px, py = x, y
        if tok.group(1) is not None:
            x = int(tok.group(1)) * k
        if tok.group(2) is not None:
            y = int(tok.group(2)) * k
        op = tok.group(3)
        if in_region:
            if op == "2":
                if poly:
                    regions.append(poly)
                poly = [(x, y)]
            else:
                poly.append((x, y))
        elif op == "3":
            flashes.append((x, y))
        elif op == "1":
            draws.append((px, py, x, y))
    return flashes, draws, regions


def _seg_hits_box(x1, y1, x2, y2, box) -> bool:
    bx0, by0, bx1, by1 = box
    for i in range(11):
        px, py = x1 + (x2 - x1) * i / 10, y1 + (y2 - y1) * i / 10
        if bx0 <= px <= bx1 and by0 <= py <= by1:
            return True
    return False


def smd_pads(root: ET.Element, top: bool = True) -> list[tuple[str, tuple[float, float, float, float]]]:
    """('REF.PAD', inner box) for every SMD pad on one side: the central 60% of
    the pad, where paste and a mask opening must be."""
    board = root.find("./drawing/board")
    pkgs = {(lib.get("name"), pk.get("name")): pk for lib in board.iterfind("./libraries/library")
            for pk in lib.iterfind("./packages/package")}
    out = []
    for el in board.iterfind("./elements/element"):
        pk = pkgs.get((el.get("library"), el.get("package")))
        if pk is None:
            continue
        ang, mir = parse_rot(el.get("rot"))
        for s in pk.iterfind("smd"):
            if ((s.get("layer") == "1") != mir) != top:
                continue
            if s.get("cream") == "no":
                continue
            cx, cy = _xf(_f(s, "x"), _f(s, "y"), _f(el, "x"), _f(el, "y"), ang, mir)
            prot, _ = parse_rot(s.get("rot"))
            a = math.radians(ang + prot)
            hw, hh = _f(s, "dx") * 0.3, _f(s, "dy") * 0.3
            ex = abs(hw * math.cos(a)) + abs(hh * math.sin(a))
            ey = abs(hw * math.sin(a)) + abs(hh * math.cos(a))
            out.append((f"{el.get('name')}.{s.get('name')}", (cx - ex, cy - ey, cx + ex, cy + ey)))
    return out


def uncovered_pads(path: str, pads) -> list[str]:
    flashes, draws, regions = gerber_ops(path)
    miss = []
    for name, box in pads:
        bx0, by0, bx1, by1 = box
        if any(bx0 <= x <= bx1 and by0 <= y <= by1 for x, y in flashes):
            continue
        if any(_seg_hits_box(*d, box) for d in draws):
            continue
        if any(any(bx0 <= x <= bx1 and by0 <= y <= by1 for x, y in r) or
               (min(p[0] for p in r) <= bx0 and max(p[0] for p in r) >= bx1 and
                min(p[1] for p in r) <= by0 and max(p[1] for p in r) >= by1) for r in regions):
            continue
        miss.append(name)
    return miss
