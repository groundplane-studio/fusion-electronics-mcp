"""Schematic part labels (offline): where each part's NAME and VALUE texts are, and plans to
make them read horizontally or copy them from another part.

Fusion's labels turn with the part, so a resistor at R90 gets vertical labels, and copy and
paste resets them to the symbol's default. A label's place is either its smashed attribute
(<instance smashed="yes"><attribute name="NAME" x y rot .../>) or, unsmashed, the symbol's
>NAME / >VALUE text carried by the instance's position and rotation.
Transforms follow EAGLE: rotate, then mirror x.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

from .eagle import parse_rot

LABELS = ("NAME", "VALUE")


def _f(el, k, d=0.0):
    v = el.get(k)
    return float(v) if v not in (None, "") else d


def _xf(px, py, x, y, angle, mirror):
    a = math.radians(angle)
    rx, ry = px * math.cos(a) - py * math.sin(a), px * math.sin(a) + py * math.cos(a)
    return x + (-rx if mirror else rx), y + ry


def _inv(wx, wy, x, y, angle, mirror):
    dx, dy = wx - x, wy - y
    if mirror:
        dx = -dx
    a = math.radians(-angle)
    return dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)


def _prefix(ref: str) -> str:
    m = re.match(r"[A-Za-z]+", ref or "")
    return m.group(0).upper() if m else ""


def instances(root: ET.Element) -> list[dict]:
    """Every gate instance: part, gate, sheet (1-based), x, y, angle, mirror, smashed, pins (count),
    body (x0, y0, x1, y1 in schematic coordinates) and labels {NAME/VALUE: {x, y, angle, size, align,
    smashed}}."""
    sch = root.find("./drawing/schematic")
    libs = {}
    for lib in sch.iterfind("./libraries/library"):
        libs[lib.get("name")] = ({s.get("name"): s for s in lib.iterfind("./symbols/symbol")},
                                 {d.get("name"): d for d in lib.iterfind("./devicesets/deviceset")})
    parts = {p.get("name"): p for p in sch.iterfind("./parts/part")}
    out = []
    for si, sheet in enumerate(sch.iterfind("./sheets/sheet"), 1):
        for inst in sheet.iterfind("./instances/instance"):
            part = parts.get(inst.get("part"))
            if part is None:
                continue
            syms, dsets = libs.get(part.get("library"), ({}, {}))
            ds = dsets.get(part.get("deviceset"))
            gate = ds.find(f"./gates/gate[@name='{inst.get('gate')}']") if ds is not None else None
            sym = syms.get(gate.get("symbol")) if gate is not None else None
            ang, mir = parse_rot(inst.get("rot"))
            x, y = _f(inst, "x"), _f(inst, "y")
            pins = list(sym.iterfind("pin")) if sym is not None else []
            pts = []
            for w in (sym.iterfind("wire") if sym is not None else []):
                if w.get("layer") == "94":
                    pts += [(_f(w, "x1"), _f(w, "y1")), (_f(w, "x2"), _f(w, "y2"))]
            for r in (sym.iterfind("rectangle") if sym is not None else []):
                if r.get("layer") == "94":
                    pts += [(_f(r, "x1"), _f(r, "y1")), (_f(r, "x2"), _f(r, "y2"))]
            for c in (sym.iterfind("circle") if sym is not None else []):
                if c.get("layer") == "94":
                    rr = _f(c, "radius")
                    pts += [(_f(c, "x") - rr, _f(c, "y") - rr), (_f(c, "x") + rr, _f(c, "y") + rr)]
            if not pts:
                pts = [(_f(p, "x"), _f(p, "y")) for p in pins] or [(0.0, 0.0)]
            world = [_xf(px, py, x, y, ang, mir) for px, py in pts]
            body = (min(p[0] for p in world), min(p[1] for p in world), max(p[0] for p in world), max(p[1] for p in world))
            labels = {}
            smashed = {a.get("name"): a for a in inst.iterfind("attribute") if a.get("name") in LABELS}
            for name in LABELS:
                a = smashed.get(name)
                if a is not None and a.get("x") is not None:
                    ra, _ = parse_rot(a.get("rot"))
                    labels[name] = {"x": _f(a, "x"), "y": _f(a, "y"), "angle": ra % 360, "size": _f(a, "size", 1.778),
                                    "align": a.get("align") or "bottom-left", "smashed": True}
                    continue
                t = next((t for t in (sym.iterfind("text") if sym is not None else [])
                          if (t.text or "").strip().upper() == ">" + name), None)
                if t is None:
                    continue
                tx, ty = _xf(_f(t, "x"), _f(t, "y"), x, y, ang, mir)
                ra, _ = parse_rot(t.get("rot"))
                labels[name] = {"x": round(tx, 4), "y": round(ty, 4), "angle": (ra + ang) % 360,
                                "size": _f(t, "size", 1.778), "align": t.get("align") or "bottom-left", "smashed": False}
            out.append({"part": inst.get("part"), "gate": inst.get("gate"), "sheet": si, "x": x, "y": y,
                        "angle": ang % 360, "mirror": mir, "smashed": inst.get("smashed") == "yes",
                        "pins": len(pins), "symbol": sym.get("name") if sym is not None else None,
                        "body": body, "labels": labels})
    return out


def _horizontal(angle: float) -> bool:
    return abs((angle % 180)) < 1e-3 or abs((angle % 180) - 180) < 1e-3


def plan_straighten(root: ET.Element, parts=None, prefixes=("R", "C", "L", "D"), sheet=None,
                    side: str = "right", gap: float = 0.5) -> list[dict]:
    """For two-pin parts turned 90 or 270 degrees (mirrored too), NAME and VALUE labels that read
    vertically: move each beside the body (NAME above the body's centre line, VALUE below) at
    0 degrees. Labels already horizontal are left alone."""
    if side not in ("right", "left"):
        raise ValueError("side is 'right' or 'left'")
    want_pre = {p.upper() for p in prefixes} if prefixes else None
    out = []
    for inst in instances(root):
        if parts and inst["part"] not in parts:
            continue
        if not parts and want_pre is not None and _prefix(inst["part"]) not in want_pre:
            continue
        if sheet and inst["sheet"] != sheet:
            continue
        if inst["pins"] != 2 or _horizontal(inst["angle"]):
            continue
        x0, y0, x1, y1 = inst["body"]
        cy = (y0 + y1) / 2
        moves = []
        for name in LABELS:
            lab = inst["labels"].get(name)
            if lab is None or _horizontal(lab["angle"]):
                continue
            size = lab["size"]
            width = 0.8 * size * max(len(inst["part"]) if name == "NAME" else 4, 1)
            tx = x1 + gap if side == "right" else x0 - gap - width
            ty = cy + 0.25 if name == "NAME" else cy - 0.25 - size
            moves.append({"label": name, "from": [lab["x"], lab["y"]], "to": [round(tx, 4), round(ty, 4)],
                          "angle": 0.0})
        if moves:
            out.append({"part": inst["part"], "gate": inst["gate"], "sheet": inst["sheet"],
                        "smash": not all(inst["labels"][m["label"]]["smashed"] for m in moves), "moves": moves})
    return out


def plan_match(root: ET.Element, source: str, targets: list[str]) -> tuple[list[dict], list[str]]:
    """Copy the source part's label positions and angles, relative to the part, onto the targets
    (after copy and paste). Returns (plan, warnings)."""
    insts = instances(root)
    src = next((i for i in insts if i["part"] == source), None)
    if src is None:
        raise ValueError(f"no part {source!r} in the schematic")
    rel = {}
    for name, lab in src["labels"].items():
        lx, ly = _inv(lab["x"], lab["y"], src["x"], src["y"], src["angle"], src["mirror"])
        rel[name] = (lx, ly, (lab["angle"] - src["angle"]) % 360)
    out, warnings = [], []
    for t in targets:
        inst = next((i for i in insts if i["part"] == t), None)
        if inst is None:
            raise ValueError(f"no part {t!r} in the schematic")
        if inst["symbol"] != src["symbol"]:
            warnings.append(f"{t} uses symbol {inst['symbol']}, {source} uses {src['symbol']}: positions copied anyway")
        moves = []
        for name, (lx, ly, ra) in rel.items():
            lab = inst["labels"].get(name)
            if lab is None:
                continue
            wx, wy = _xf(lx, ly, inst["x"], inst["y"], inst["angle"], inst["mirror"])
            angle = (ra + inst["angle"]) % 360
            if math.dist((wx, wy), (lab["x"], lab["y"])) < 1e-3 and abs((angle - lab["angle"] + 180) % 360 - 180) < 1e-3:
                continue
            moves.append({"label": name, "from": [lab["x"], lab["y"]], "to": [round(wx, 4), round(wy, 4)],
                          "angle": round(angle, 3)})
        if moves:
            out.append({"part": t, "gate": inst["gate"], "sheet": inst["sheet"],
                        "smash": not all(inst["labels"][m["label"]]["smashed"] for m in moves), "moves": moves})
    return out, warnings


def commands(plan: list[dict]) -> str:
    """EDIT .s<sheet>; SMASH 'part'; then per label MOVE (from) (to); ROTATE =R<a> (to);"""
    def n(v):                                               # to 0.1 um (":g" keeps only 6 digits)
        s = f"{round(float(v), 4):.4f}".rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s
    out, cur = [], None
    for p in sorted(plan, key=lambda q: q["sheet"]):
        if p["sheet"] != cur:
            out.append(f"EDIT .s{p['sheet']};")
            cur = p["sheet"]
        if p["smash"]:
            out.append(f"SMASH '{p['part']}';")
        for m in p["moves"]:
            if math.dist(m["from"], m["to"]) > 1e-4:
                out.append(f"MOVE ({n(m['from'][0])} {n(m['from'][1])}) ({n(m['to'][0])} {n(m['to'][1])});")
            out.append(f"ROTATE =R{n(m['angle'])} ({n(m['to'][0])} {n(m['to'][1])});")
    return " ".join(out)
