"""Placement plans (offline): where parts should go, as move_parts moves.

place_inline: series parts in one column (or row), each on the row (or
column) of the pad it connects to. For R18-R25 between T1 and J2 on the PoE
board: one column at x, each resistor's T1-side pad exactly on its T1 pad's
row, all turned the same way. That took 12 move/rotate calls by hand.

tidy: snap to the placement grid, line up near-rows, even out pitch, for
parts that do not matter electrically; critical parts are left alone or only
nudged (see critical_parts), and nothing is moved for silkscreen.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict

from . import footprints as FP
from .placement_check import (DEFAULT_GRID, _natural, apply_moves, connections, derived_courtyards, move_effects,
                              pad_gaps, pads, part_class)


def _local_pads(root: ET.Element) -> dict[str, dict[str, tuple[float, float]]]:
    board = root.find("./drawing/board")
    pkgs = FP.packages(board)
    out = {}
    for el in board.iterfind("./elements/element"):
        pk = pkgs.get((el.get("library"), el.get("package")))
        if pk is not None:
            out[el.get("name")] = {p.get("name"): (float(p.get("x")), float(p.get("y")))
                                   for p in list(pk.iterfind("smd")) + list(pk.iterfind("pad"))}
    return out


def pad_offset(local: tuple[float, float], angle: float, bottom: bool) -> tuple[float, float]:
    """A pad's offset from the part origin at this rotation and side (rotate, then mirror x)."""
    a = math.radians(angle)
    x, y = local
    rx, ry = x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)
    return (-rx if bottom else rx), ry


def inline(root: ET.Element, parts: list[str], x: float | None = None, y: float | None = None,
           to: str | None = None, angle: float | None = None, face_anchor: bool = True) -> dict:
    """Plan a column (x given) or a row (y given) of two-pin parts, each on its anchor pad's
    row (or column). The anchor is the pad of `to` that shares a net with one of the part's
    pads, or without `to` the nearest pad of another part on one of its nets.
    angle: the parts' rotation (default: keep each one's). face_anchor: turn a part by 180
    degrees when that puts its connecting pad on the side facing the anchor.
    Returns {moves (for move_parts), anchors, skipped}."""
    if (x is None) == (y is None):
        raise ValueError("give x_mm for a column or y_mm for a row (one of them)")
    column = x is not None
    els = {e.get("name"): e for e in root.iterfind("./drawing/board/elements/element")}
    if to is not None and to not in els:
        raise ValueError(f"no part {to!r} on the board")
    local = _local_pads(root)
    pad_list = pads(root)
    by_net: dict[str, list] = {}
    for p in pad_list:
        if p.net:
            by_net.setdefault(p.net, []).append(p)
    mine = {}
    for p in pad_list:
        mine.setdefault(p.ref, []).append(p)
    moves, anchors, skipped = [], [], []
    for ref in parts:
        if ref not in els:
            raise ValueError(f"no part {ref!r} on the board")
        ps = mine.get(ref, [])
        if len(ps) != 2:
            skipped.append({"part": ref, "why": f"has {len(ps)} pads; place_inline lines up two-pin parts"})
            continue
        e = els[ref]
        ang0, bottom = FP.parse_rot(e.get("rot"))
        cands = []
        for p in ps:
            for q in by_net.get(p.net, []) if p.net else []:
                if q.ref == ref or (to is not None and q.ref != to):
                    continue
                along = abs(q.x - x) if column else abs(q.y - y)     # how far the anchor is from the line
                cands.append((along, _natural(q.name), p, q))
        if not cands:
            skipped.append({"part": ref, "why": f"no pad of {to} shares a net with it" if to else
                            "none of its nets reaches another part"})
            continue
        _, _, p, q = min(cands, key=lambda c: (c[0], c[1]))
        a = (ang0 if angle is None else float(angle)) % 360
        if face_anchor:
            def toward(ang):                     # how far the connecting pad sits from the anchor
                ox, oy = pad_offset(local[ref][p.name], ang, bottom)
                return abs((x + ox) - q.x) if column else abs((y + oy) - q.y)
            if toward((a + 180) % 360) < toward(a) - 1e-6:
                a = (a + 180) % 360
        ox, oy = pad_offset(local[ref][p.name], a, bottom)
        if column:
            tx, ty = x, q.y - oy                  # the connecting pad lands exactly on the anchor's row
        else:
            tx, ty = q.x - ox, y
        moves.append({"ref": ref, "x_mm": round(tx, 4), "y_mm": round(ty, 4), "angle": a})
        anchors.append({"part": ref, "pad": p.name, "on": f"{q.ref}.{q.name}", "net": p.net,
                        "row_mm" if column else "column_mm": round(q.y if column else q.x, 4)})
    return {"moves": moves, "anchors": anchors, "skipped": skipped}


# ---------------------------------------------------------------------------
# tidy_placement

GROUND = re.compile(r"^(A|D|P|S)?GND\w*$|^VSS\w*$|^0V$", re.I)
CHASSIS = re.compile(r"SHIELD|CHASSIS|EARTH|^PE$|^FG$|^FGND$", re.I)
NONPOLAR = ("R", "C", "L", "FB")        # a 180-degree turn only swaps equivalent pads


def _prefix(ref: str) -> str:
    m = re.match(r"[A-Za-z]+", ref or "")
    return m.group(0).upper() if m else ""


def pair_nets(net_names) -> dict[str, str]:
    """net -> pair name for nets that come as P/N partners (X_P/X_N, X+/X-, XDP/XDN, XP/XN)."""
    names = {n.upper(): n for n in net_names}
    out = {}
    for up, n in names.items():
        for ps, ns in (("_P", "_N"), ("+", "-"), ("DP", "DN"), ("P", "N")):
            if up.endswith(ps) and len(up) > len(ps):
                partner = names.get(up[: -len(ps)] + ns)
                if partner:
                    base = n[: -len(ps)].rstrip("_")
                    out[n] = out[partner] = base
                    break
    return out


def _pours(board: ET.Element) -> dict[tuple[str, int], list]:
    out = defaultdict(list)
    for s in board.iterfind("./signals/signal"):
        for poly in s.iterfind("polygon"):
            pts = [(float(v.get("x")), float(v.get("y"))) for v in poly.iterfind("vertex")]
            if len(pts) >= 3:
                out[(s.get("name"), int(poly.get("layer")))].append(pts)
    return out


def _in_own_pour(p, pours) -> bool:
    from .stitch import _inside_poly
    layers = [1] if p.sides == {"top"} else [16] if p.sides == {"bottom"} else [1, 16]
    return any(_inside_poly(poly, p.x, p.y) for lay in layers for poly in pours.get((p.net, lay), ()))


def critical_parts(root: ET.Element, pad_list=None, extra=()) -> tuple[dict, dict]:
    """(part -> why it is critical, part -> pair it belongs to) for tidy_placement:
    - two-pin parts on a P/N pair's nets (series R, AC caps, TVS on its lines): one rigid group;
    - parts on a net with a non-default net class;
    - crystals (Y/X/XTAL/OSC, or a kHz/MHz value);
    - decoupling caps: a C from ground to a net that reaches a pin of a 4+ pin part within 3 mm;
    - isolation bridges: two-pin parts from a chassis/shield-type net to another net, or with
      each pad in its own net's pour (R5/C5/C6 between SHIELD and GND on the PoE board);
    - anything in `extra`."""
    board = root.find("./drawing/board")
    pad_list = pad_list if pad_list is not None else pads(root)
    classes = {s.get("name"): s.get("class") or "0" for s in board.iterfind("./signals/signal")}
    values = {e.get("name"): e.get("value") or "" for e in board.iterfind("./elements/element")}
    by_ref, by_net = defaultdict(list), defaultdict(list)
    for p in pad_list:
        by_ref[p.ref].append(p)
        if p.net:
            by_net[p.net].append(p)
    pairs = pair_nets(by_net)
    pours = _pours(board)
    why, group = {}, {}
    for ref, ps in by_ref.items():
        nets = sorted({p.net for p in ps if p.net})
        two = len(ps) == 2
        pre = _prefix(ref)
        classed = [n for n in nets if classes.get(n, "0") != "0"]
        on_pair = [n for n in nets if n in pairs] if two else []
        if ref in extra:
            why[ref] = "listed in critical"
        elif on_pair:
            why[ref] = f"on pair {pairs[on_pair[0]]}"
            group[ref] = pairs[on_pair[0]]
        elif classed:
            why[ref] = f"net {classed[0]} has net class {classes[classed[0]]}"
        elif pre in ("Y", "X", "XTAL", "OSC") or re.search(r"\d\s*[kM]Hz", values.get(ref, ""), re.I):
            why[ref] = "crystal"
        elif two and pre == "C" and len(nets) == 2 and any(GROUND.match(n) for n in nets):
            rail = next(n for n in nets if not GROUND.match(n))
            ic = next((q.ref for q in by_net[rail] if q.ref != ref and len(by_ref[q.ref]) >= 4 and
                       min(math.hypot(q.x - p.x, q.y - p.y) for p in ps) <= 3.0), None)
            if ic:
                why[ref] = f"decoupling {ic} on {rail}"
        if ref not in why and two and len(nets) == 2:
            chassis = [bool(CHASSIS.search(n)) for n in nets]
            if any(chassis) and not all(chassis):
                why[ref] = f"isolation bridge ({nets[chassis.index(True)]} to {nets[chassis.index(False)]})"
            elif all(p.net and _in_own_pour(p, pours) for p in ps):
                why[ref] = f"isolation bridge (pads on the {nets[0]} and {nets[1]} pours)"
    return why, group


def routed_parts(root: ET.Element, pad_list) -> set[str]:
    """Parts with a trace or via ending on one of their pads."""
    ends = set()
    for s in root.iterfind("./drawing/board/signals/signal"):
        for w in s.iterfind("wire"):
            if w.get("layer") != "19":
                ends.add((round(float(w.get("x1")), 3), round(float(w.get("y1")), 3)))
                ends.add((round(float(w.get("x2")), 3), round(float(w.get("y2")), 3)))
        for v in s.iterfind("via"):
            ends.add((round(float(v.get("x")), 3), round(float(v.get("y")), 3)))
    return {p.ref for p in pad_list if (round(p.x, 3), round(p.y, 3)) in ends}


def _courtyard_gaps(root: ET.Element, refs, near: float) -> dict:
    cys = FP.courtyards(root)
    skip = FP.enclosing(cys)
    allc = {**cys, **derived_courtyards(root, cys)}
    out = {}
    for c in FP.courtyard_conflicts(allc, refs=refs, near_mm=near, ignore=skip):
        if c["status"] == "inside":
            continue
        out[tuple(sorted((c["a"], c["b"])))] = -c["overlap_mm"] if c["status"] == "overlap" else c["gap_mm"]
    return out


def tidy(root: ET.Element, refs=None, region=None, grid: dict | None = None, critical=(), nudge: float = 0.25,
         rework_gap: float = 0.0, max_move: float = 0.5, align: bool = True, spread: bool = True,
         rotate: bool = False, skip_routed: bool = True, near: float = 0.2, reach: float = 3.0,
         keep=()) -> dict:
    """Plan tidy moves. Returns {moves (for move_parts), actions: part -> [what], left_alone:
    [{part, why}], dropped: [{part, why}]}. Nothing is moved for silkscreen. keep: parts left
    exactly as placed (no nudge either); they still anchor the lines their neighbours join."""
    from .placement_check import resolve_moves
    grid = {**DEFAULT_GRID, **(grid or {})}
    pad_list = pads(root)
    n_pads = defaultdict(int)
    for p in pad_list:
        n_pads[p.ref] += 1
    pos = {}
    for el in root.iterfind("./drawing/board/elements/element"):
        r = el.get("name")
        if n_pads.get(r):
            ang, bot = FP.parse_rot(el.get("rot"))
            pos[r] = {"x": float(el.get("x")), "y": float(el.get("y")), "angle": ang % 360, "bottom": bot,
                      "cls": part_class(r, n_pads[r]), "pkg": el.get("package")}

    def in_scope(r):
        if refs and r not in refs:
            return False
        if region:
            x0, y0, x1, y1 = region
            return x0 <= pos[r]["x"] <= x1 and y0 <= pos[r]["y"] <= y1
        return True
    scope = [r for r in sorted(pos, key=_natural) if in_scope(r)]
    why, group = critical_parts(root, pad_list, set(critical or ()))
    routed = routed_parts(root, pad_list) if skip_routed else set()
    t = {r: dict(pos[r]) for r in pos}                     # targets, start where they are
    actions, left = defaultdict(list), {}
    snap = lambda v, g: round(round(v / g) * g, 4)
    free = set()
    for r in scope:
        if r in keep:
            left[r] = "kept as placed (keep)"
        elif r in routed:
            left[r] = "routed: traces end on its pads, moving it would drag them (skip_routed=false to allow)"
        elif r not in why:
            free.add(r)

    # 1. grid
    for r in sorted(free, key=_natural):
        g = grid[pos[r]["cls"]]
        x, y = snap(t[r]["x"], g), snap(t[r]["y"], g)
        if abs(x - t[r]["x"]) > 1e-6 or abs(y - t[r]["y"]) > 1e-6:
            t[r]["x"], t[r]["y"] = x, y
            actions[r].append(f"onto the {g} mm grid")
    for r in scope:                                         # critical single parts: a small nudge at most
        if r in free or r in left or r in group:
            continue
        g = grid[pos[r]["cls"]]
        x, y = snap(t[r]["x"], g), snap(t[r]["y"], g)
        d = math.hypot(x - t[r]["x"], y - t[r]["y"])
        if 1e-6 < d <= nudge:
            t[r]["x"], t[r]["y"] = x, y
            actions[r].append(f"nudged {d:.3f} mm onto the {g} mm grid (critical: {why[r]})")
        else:
            left[r] = f"critical: {why[r]}" + (f"; the grid is {d:.3f} mm away (nudge limit {nudge})" if d > 1e-6 else "")
    groups = defaultdict(list)
    for r in scope:
        if r in group and r not in left:
            groups[group[r]].append(r)
    for name, members in sorted(groups.items()):
        everyone = sorted((r for r in pos if group.get(r) == name), key=_natural)
        stuck = [r for r in everyone if r not in members]
        if stuck:
            for r in members:
                left[r] = f"pair {name} group moves only as one, and {', '.join(stuck)} cannot move (routed, kept or out of scope)"
            continue
        a = members[0]
        g = grid[pos[a]["cls"]]
        dx, dy = snap(t[a]["x"], g) - t[a]["x"], snap(t[a]["y"], g) - t[a]["y"]
        d = math.hypot(dx, dy)
        if 1e-6 < d <= nudge:
            for r in members:
                t[r]["x"], t[r]["y"] = round(t[r]["x"] + dx, 4), round(t[r]["y"] + dy, 4)
                actions[r].append(f"pair {name} group moved {d:.3f} mm as one onto the grid")
        else:
            for r in members:
                left[r] = f"pair {name} group (kept together)" + (f"; the grid is {d:.3f} mm away" if d > 1e-6 else "")

    # 2. almost-aligned rows and columns
    if align:
        cand = sorted(pos, key=_natural)                     # parts that cannot move still anchor a line
        for axis, k, o in (("row", "y", "x"), ("column", "x", "y")):
            parent = {r: r for r in cand}

            def find(r):
                while parent[r] != r:
                    parent[r] = parent[parent[r]]
                    r = parent[r]
                return r
            for i, a in enumerate(cand):
                for b in cand[i + 1:]:
                    if (t[a]["bottom"] != t[b]["bottom"] or pos[a]["cls"] != pos[b]["cls"]
                            or abs(t[a][o] - t[b][o]) > reach or abs(t[a][k] - t[b][k]) > near + 1e-9):
                        continue
                    parent[find(a)] = find(b)
            clusters = defaultdict(list)
            for r in cand:
                clusters[find(r)].append(r)
            for members in clusters.values():
                vals = [round(t[m][k], 4) for m in members]
                movers = [m for m in members if m in free]
                if len(members) < 2 or max(vals) - min(vals) <= 1e-4 or not movers:
                    continue
                fixed = {round(t[m][k], 4) for m in members if m not in free}
                if len(fixed) > 1:
                    continue                                 # two fixed parts disagree: leave it
                if fixed:
                    tv = fixed.pop()
                else:
                    counts = defaultdict(int)
                    for v in vals:
                        counts[v] += 1
                    best = max(counts.values())
                    # the line most of them are on; on a tie, the one that moves them least from
                    # where they started
                    tops = [v for v, c in counts.items() if c == best]
                    tv = min(sorted(tops), key=lambda v: sum(abs(pos[m][k] - v) for m in movers))
                others = sorted((m for m in members if abs(t[m][k] - tv) <= 1e-4), key=_natural)
                for m in movers:
                    if abs(t[m][k] - tv) > 1e-4:
                        t[m][k] = tv
                        actions[m].append(f"lined up in a {axis} with {', '.join(others[:4]) or 'its neighbours'}")

    # 3. even pitch along exact rows/columns of one package, only small irregularities
    if spread:
        follows = {r: {c["to_part"] for c in links if abs(c["offset_mm"]) <= 0.05}
                   for r, links in connections(pad_list).items()}
        lines = defaultdict(list)
        for r in free:
            lines[("row", t[r]["bottom"], pos[r]["pkg"], round(t[r]["y"], 4))].append(r)
            lines[("column", t[r]["bottom"], pos[r]["pkg"], round(t[r]["x"], 4))].append(r)
        for (axis, _, pkg, _), members in lines.items():
            o = "x" if axis == "row" else "y"
            members = sorted(members, key=lambda r: t[r][o])
            runs, cur = [], [members[0]]
            for r in members[1:]:
                if t[r][o] - t[cur[-1]][o] > reach:
                    runs.append(cur)
                    cur = []
                cur.append(r)
            runs.append(cur)
            for run in runs:
                if len(run) < 3 or set.intersection(*(follows.get(r, set()) for r in run)):
                    continue
                pitches = [t[b][o] - t[a][o] for a, b in zip(run, run[1:])]
                mean = sum(pitches) / len(pitches)
                if max(pitches) - min(pitches) <= 0.01 or max(pitches) - min(pitches) > 0.5 * mean:
                    continue
                g = grid[pos[run[0]]["cls"]]
                pitch = max(g, snap(mean, g))
                for i, r in enumerate(run):
                    v = round(t[run[0]][o] + i * pitch, 4)
                    if abs(v - t[r][o]) > 1e-4:
                        t[r][o] = v
                        actions[r].append(f"even {pitch:g} mm pitch along the {axis} ({run[0]}..{run[-1]})")

    # 4. rotations in a row of one package (off by default: a turn swaps pads and drags traces)
    if rotate:
        rows = defaultdict(list)
        for r in free:
            if _prefix(r) in NONPOLAR and n_pads[r] == 2:
                rows[(t[r]["bottom"], pos[r]["pkg"], round(t[r]["y"], 4))].append(r)
        for members in rows.values():
            angles = defaultdict(list)
            for r in members:
                angles[t[r]["angle"]].append(r)
            if len(angles) < 2:
                continue
            target = max(sorted(angles), key=lambda a: len(angles[a]))
            for a, rs in angles.items():
                if a != target and abs((a - target) % 360 - 180) < 1e-3:
                    for r in rs:
                        t[r]["angle"] = target
                        actions[r].append(f"turned to {target:g} degrees like its row")

    # constraints: max move, nothing introduced, rework gaps never shrunk
    dropped = []

    def moved_list():
        return [r for r in scope if (abs(t[r]["x"] - pos[r]["x"]) > 1e-6 or abs(t[r]["y"] - pos[r]["y"]) > 1e-6
                                     or abs(t[r]["angle"] - pos[r]["angle"]) > 1e-6)]

    def drop(r, reason):
        t[r] = dict(pos[r])
        actions.pop(r, None)
        dropped.append({"part": r, "why": reason})
    for r in moved_list():
        d = math.hypot(t[r]["x"] - pos[r]["x"], t[r]["y"] - pos[r]["y"])
        if d > max_move + 1e-9:
            drop(r, f"would move {d:.3f} mm (max_move {max_move})")
    gap_near = max(rework_gap, 0.0) + 0.05
    for _ in range(8):
        moved = moved_list()
        if not moved:
            break
        moves = [{"ref": r, "x_mm": t[r]["x"], "y_mm": t[r]["y"], "angle": t[r]["angle"]} for r in moved]
        after = apply_moves(root, resolve_moves(root, moves))
        bad = {}
        for item in move_effects(root, after, moved)["introduced"]:
            if item["kind"] == "silkscreen":
                continue                                      # low severity: never a reason either way
            for side in ("a", "b"):
                r = item[side].split(".")[0]
                if r in moved:
                    bad.setdefault(r, f"would introduce a {item['kind']} problem with "
                                      f"{item['b' if side == 'a' else 'a'].split('.')[0]}")
        g0, g1 = _courtyard_gaps(root, moved, gap_near), _courtyard_gaps(after, moved, gap_near)
        for pair, gap in g1.items():
            need = min(g0.get(pair, math.inf), rework_gap if rework_gap > 0 else 0.0)
            if gap < need - 1e-3:
                for r in pair:
                    if r in moved:
                        bad.setdefault(r, f"courtyard gap to {pair[1] if r == pair[0] else pair[0]} would drop "
                                          f"to {gap:.3f} mm (keeps at least {need if need != math.inf else 0:.3f})")
        if not bad:
            break
        for r, reason in bad.items():
            drop(r, reason)
    moved = moved_list()
    return {"moves": [{"ref": r, "x_mm": t[r]["x"], "y_mm": t[r]["y"], "angle": t[r]["angle"]} for r in moved],
            "actions": {r: actions[r] for r in moved},
            "left_alone": [{"part": r, "why": w} for r, w in sorted(left.items(), key=lambda kv: _natural(kv[0]))],
            "dropped": dropped,
            "critical_count": sum(1 for r in scope if r in why)}
