"""Net classes (offline): what a class's rules really are, a rule file that
adds one, and where to pick a net in the schematic to assign it.

Fusion keeps a class in two places (seen on the PoE Magnetics Test Board,
Fusion 2705.1.25, 2026-10-07):
- the board's <classes>: number, name, width, drill and the class-to-class
  clearance (what the classic export shows);
- the V2 design rules: per class a "Minimum Copper Width" and a "Minimum
  Drill Size" rule with onescope="classes=N", and a "Copper Clearance" rule
  with onescope="classes=N" otherscope="classes=N", all ahead (lower
  priority number) of the built-in rules.
The CLASS command creates classes whose V2 rules hit ALL copper and survive
UNDO (2026-10-05: a 0.3 mm width rule flagged every 0.12 mm trace), so it is
never used: set_net_class writes a rule file to load in the DRC dialog, and
assign_net_class picks nets in the schematic with CHANGE CLASS.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

RULE_TYPES = {"width": ("Minimum Copper Width", "Copper Width"),
              "drill": ("Minimum Drill Size", "Drill Size"),
              "clearance": ("Copper Clearance", "Copper Clearance")}


_UNIT_MM = {"": 1.0, "mm": 1.0, "mil": 0.0254, "in": 25.4, "inch": 25.4, "um": 0.001, "mic": 0.001}


def _mm(v: str | None) -> float | None:
    """A length from a rule file ("0.2mm", ".2mm", "6mil", "0.01in", "150um"; a bare number is mm)
    in mm. None for an empty value; ValueError for anything else that is not a length."""
    if v is None or not str(v).strip():
        return None
    m = re.fullmatch(r"\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*([A-Za-z]*)\s*", str(v))
    unit = m.group(2).lower() if m else None
    if not m or unit not in _UNIT_MM:
        raise ValueError(f"cannot read {v!r} as a length (expected a number with mm, mil, in or um)")
    return round(float(m.group(1)) * _UNIT_MM[unit], 6)


def _class_numbers(scope: str | None) -> list[str]:
    m = re.match(r"classes=([\d,]+)$", scope or "")
    return m.group(1).split(",") if m else []


def pad_widths(board_root: ET.Element) -> dict[str, list[tuple[str, float]]]:
    """net -> [(PART.PAD, narrowest width of the pad's copper)]."""
    from .placement_check import pads
    out = {}
    for p in pads(board_root):
        if not p.net:
            continue
        poly, best = p.poly, math.inf
        for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
            L = math.hypot(x2 - x1, y2 - y1)
            if L < 1e-9:
                continue
            nx, ny = -(y2 - y1) / L, (x2 - x1) / L
            d = [x * nx + y * ny for x, y in poly]
            best = min(best, max(d) - min(d))
        out.setdefault(p.net, []).append((f"{p.ref}.{p.name}", round(best, 3)))
    return out


def summarize(board_root: ET.Element, rules_xml: str | None, pads: dict | None = None) -> dict:
    """Classes from the board export plus, when the V2 rules are given, the rules that apply to
    each class and any added rule that is not scoped to a class.
    Each class's width, drill and clearance are its effective values: the class's DRC rule when
    there is one, else the legacy class attribute; `source` says which. Classes made in the Net
    Classes dialog with rules (PoE board, 2026-10-07: poe_ct, pwr_5V, pwr_3V3, shield) have
    legacy width 0 and no legacy clearance; their values live only in the rules."""
    board = board_root.find("./drawing/board")
    counts = {}
    for s in board.iterfind("./signals/signal"):
        counts[s.get("class") or "0"] = counts.get(s.get("class") or "0", 0) + 1
    classes = []
    for c in board.iterfind("./classes/class"):
        num = c.get("number")
        cl = {x.get("class"): _mm(x.get("value")) for x in c.iterfind("clearance")}
        classes.append({"number": int(num), "name": c.get("name"), "nets": counts.get(num, 0),
                        "legacy": {"width": _mm(c.get("width")) or 0.0, "drill": _mm(c.get("drill")) or 0.0,
                                   "clearance": cl.get(num)},
                        "rules": []})
    warnings, notes = [], []
    if rules_xml:
        root = ET.fromstring(rules_xml)
        by_num = {str(c["number"]): c for c in classes}
        for r in sorted(root.iter("rule"), key=lambda r: int(r.get("priority") or 0)):
            kind = next((k for k, (t, _) in RULE_TYPES.items() if t == r.get("type")), None)
            if kind is None or r.get("enabled", "yes") != "yes":
                continue
            scoped = _class_numbers(r.get("onescope")) + _class_numbers(r.get("otherscope"))
            for n in dict.fromkeys(scoped):
                if n in by_num:
                    by_num[n]["rules"].append({"kind": kind, "value_mm": _mm(r.get("value")), "name": r.get("name"),
                                               "scope": f"{r.get('onescope', 'all')} / {r.get('otherscope', '-')}"})
                else:
                    warnings.append(f"rule {r.get('name')!r} names class {n}, which does not exist")
            if not scoped and r.get("builtin_ruleid") is None:
                warnings.append(f"rule {r.get('name')!r} ({r.get('type')} {r.get('value')}) is not scoped to a class "
                                "or a built-in rule: it applies to ALL copper. Classes made with the CLASS command "
                                "leave rules like this; delete it in Rules > Design Rules if it is not meant")
    for c in classes:
        rule = {}
        for r in c["rules"]:                                  # highest priority (lowest number) first
            rule.setdefault(r["kind"], r["value_mm"])
        c["source"] = {}
        for kind in ("width", "drill", "clearance"):
            legacy = c["legacy"][kind]
            if rule.get(kind) is not None:
                c[kind + "_mm"], c["source"][kind] = rule[kind], "design rule"
            elif legacy:
                c[kind + "_mm"], c["source"][kind] = legacy, "class (legacy)"
            else:
                c[kind + "_mm"], c["source"][kind] = None, None
            if c["number"] == 0 or not rules_xml:
                continue
            if legacy and rule.get(kind) is None:
                warnings.append(f"class {c['name']} has {kind} {legacy} mm but no DRC rule for it")
            elif legacy and rule.get(kind) is not None and abs(rule[kind] - legacy) > 1e-4:
                warnings.append(f"class {c['name']}: {kind} {legacy} mm in the class but {rule[kind]} mm in its DRC rule")
        if c["number"] != 0 and rules_xml and not any(c["source"].values()):
            notes.append(f"class {c['name']}: no width, drill or clearance found. If they were set in the Design "
                         "Rules dialog, they reach the file this reads only when the design is saved")
        if pads and c["number"] != 0 and c["source"]["width"] == "design rule" and c["width_mm"]:
            # a class width rule covers all the class's copper, pads too (PoE board: pwr_5V's 1.0 mm
            # rule gave 14 "Copper Width" errors on 5V connector and capacitor pads)
            nets = [sg.get("name") for sg in board.iterfind("./signals/signal") if sg.get("class") == str(c["number"])]
            narrow = sorted(((pad, w) for n in nets for pad, w in pads.get(n, []) if w < c["width_mm"] - 1e-4),
                            key=lambda pw: pw[1])
            if narrow:
                rule = next((r for r in c["rules"] if r["kind"] == "width"), None)
                warnings.append(
                    f"class {c['name']}: its {c['width_mm']} mm width rule"
                    + (f" ({rule['name']}, scope {rule['scope']})" if rule else "")
                    + f" also applies to pads: {len(narrow)} pad(s) on its nets are narrower ("
                    + ", ".join(f"{pd} {w} mm" for pd, w in narrow[:5]) + (", ..." if len(narrow) > 5 else "")
                    + "), and DRC flags each. Limit the rule to wires in Rules > Design Rules, or route the width "
                    "by hand and leave the class width lower")
        set_in_rules = [k for k in ("width", "clearance") if c["source"][k] == "design rule" and not c["legacy"][k]]
        if set_in_rules and c["number"] != 0:
            notes.append(f"class {c['name']}: {' and '.join(set_in_rules)} set in its design rules only (the legacy "
                         "class value is empty); the rule value is the one that counts")
    return {"classes": classes, "warnings": warnings, "notes": notes,
            "note": "a class's clearance is also the gap Fusion uses for differential pairs in that class"}


def effective(board_root: ET.Element, rules_xml: str | None) -> dict[str, dict]:
    """Class name -> {number, width_mm, drill_mm, clearance_mm} as DRC applies them (rule first,
    then legacy). For tools that take a width or clearance from a net's class."""
    return {c["name"]: {k: c[k] for k in ("number", "width_mm", "drill_mm", "clearance_mm")}
            for c in summarize(board_root, rules_xml)["classes"]}


def build_edru(rules_xml: str, classes: list[dict], name: str, width: float, clearance: float,
               drill: float | None = None, number: int | None = None, title: str | None = None) -> tuple[str, dict]:
    """A rule file (.edru) from the board's current V2 rules with class `name` added or updated:
    its width, drill and clearance rules scoped to it (onescope="classes=N"), the way Fusion
    writes them, ahead of the built-in rules. Every existing class is kept.
    classes: [{number, name}] from the board. Returns (file text, the class written)."""
    if not re.fullmatch(r"[A-Za-z0-9_\-+. ]{1,32}", name or ""):
        raise ValueError("class name: 1-32 letters, digits, spaces or _-+.")
    if width <= 0 or clearance <= 0 or (drill is not None and drill <= 0):
        raise ValueError("width, clearance and drill must be positive (mm)")
    root = ET.fromstring(rules_xml)
    if root.tag != "designrules":
        raise ValueError("expected a <designrules> element")
    have = {int(c["number"]): c["name"] for c in classes}
    same = next((n for n, nm in have.items() if nm.casefold() == name.casefold()), None)
    if number is None:
        number = same if same is not None else next(n for n in range(1, 64) if n not in have)
    elif number in have and have[number].casefold() != name.casefold():
        raise ValueError(f"class number {number} is already {have[number]!r}")
    elif same is not None and same != number:
        raise ValueError(f"class {name!r} already exists as number {same}")
    if number == 0:
        raise ValueError("class 0 is the default class; give another number")
    have[number] = name
    set_classes(root, have)
    rules = root.find("rules")
    if rules is None:
        rules = ET.SubElement(root, "rules")
    tag = f"classes={number}"
    values = {"width": width, "drill": drill, "clearance": clearance}
    for kind, (rtype, label) in RULE_TYPES.items():
        mine = [r for r in rules.findall("rule") if r.get("type") == rtype]
        for r in mine:                                        # drop this class's old rule of this kind
            if r.get("onescope") == tag and r.get("builtin_ruleid") is None:
                rules.remove(r)
        if values[kind] is None:
            continue
        mine = [r for r in rules.findall("rule") if r.get("type") == rtype]
        scoped = [r for r in mine if r.get("builtin_ruleid") is None and _class_numbers(r.get("onescope"))]
        pos = len(scoped)                                     # after the other class rules, before built-ins
        for r in mine:
            p = int(r.get("priority", "0"))
            if p >= pos:
                r.set("priority", str(p + 1))
        used = {r.get("name") for r in mine}
        k = 1
        while f"{label} {k}" in used:
            k += 1
        v = f"{values[kind]:g}mm"
        attrs = {"type": rtype, "enabled": "yes", "priority": str(pos), "onescope": tag}
        if kind == "clearance":
            attrs.update(otherscope=tag, value=v, preferredvalue=v, samesignal="no")
        else:
            attrs.update(value=v, preferredvalue=v)
            if kind == "width":
                attrs["check_polywidth"] = "no"
        attrs["name"] = f"{label} {k}"
        new = ET.Element("rule", attrs)
        idx = list(rules).index(mine[0]) if mine else len(rules)
        rules.insert(idx, new)
    return edru_text(root, title), {"number": number, "name": name, "width_mm": width, "clearance_mm": clearance,
                                    "drill_mm": drill}


def set_classes(root: ET.Element, classes: dict[int, str]) -> None:
    """Put every class (number and name, as the bundled .edru files carry them) into a
    <designrules> element, so loading the file keeps them all."""
    for old in root.findall("classes"):
        root.remove(old)
    cl = ET.Element("classes")
    for n in sorted(classes):
        ET.SubElement(cl, "class", {"number": str(n), "name": classes[n]}).text = "\n"
    root.insert(0, cl)


def edru_text(root: ET.Element, title: str | None = None) -> str:
    """A <designrules> element as a rule file to load in Fusion's DRC dialog."""
    if title:
        root.set("name", title)
    body = ET.tostring(root, encoding="unicode")
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE designrules SYSTEM "eagle.dtd">\n'
            '<eagle version="9.7.0">\n' + body + "\n</eagle>\n")


def _seg_dist(px, py, x1, y1, x2, y2) -> float:
    vx, vy = x2 - x1, y2 - y1
    L2 = vx * vx + vy * vy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x1) * vx + (py - y1) * vy) / L2))
    return math.hypot(px - x1 - t * vx, py - y1 - t * vy)


def pick_points(sch_root: ET.Element, nets: list[str], clear_mm: float = 0.05) -> tuple[dict, dict]:
    """For each net, one point to pick it in the schematic: (sheet, x, y), the midpoint of
    one of its own wires (longest first) with no other net's wire within clear_mm.
    Returns (points, failures: net -> why)."""
    sheets = sch_root.findall("./drawing/schematic/sheets/sheet")
    wires = []                                               # (sheet, net, x1, y1, x2, y2)
    for i, sh in enumerate(sheets, 1):
        for n in sh.iterfind("./nets/net"):
            for w in n.iter("wire"):
                wires.append((i, n.get("name"), float(w.get("x1")), float(w.get("y1")), float(w.get("x2")),
                              float(w.get("y2"))))
    known = {w[1] for w in wires} | {n.get("name") for sh in sheets for n in sh.iterfind("./nets/net")}
    points, failures = {}, {}
    for net in nets:
        if net not in known:
            failures[net] = "no net of that name in the schematic"
            continue
        own = sorted((w for w in wires if w[1] == net), key=lambda w: -math.hypot(w[4] - w[2], w[5] - w[3]))
        if not own:
            failures[net] = "it has no wire in the schematic to pick (only pins or labels); assign it by hand"
            continue
        for sheet, _, x1, y1, x2, y2 in own:
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            if math.hypot(x2 - x1, y2 - y1) < 2 * clear_mm:
                continue
            if all(_seg_dist(mx, my, *w[2:]) > clear_mm for w in wires if w[0] == sheet and w[1] != net):
                points[net] = (sheet, round(mx, 4), round(my, 4))
                break
        else:
            failures[net] = f"every wire of it has another net within {clear_mm} mm of its middle"
    return points, failures


def change_class_commands(class_name: str, points: dict) -> str:
    """EDIT .s<sheet>; CHANGE CLASS <name> (x y) ...; per sheet (verified by hand on 2705.1.25
    with an unquoted name; names with other characters are quoted, and a name with a quote or
    semicolon is refused). Coordinates to 0.1 um (commands.n, not :g)."""
    from fusion_mcp import commands as C          # the command rules live with the server
    nm = class_name if re.fullmatch(r"\w+", class_name or "") else C.q(class_name)
    by_sheet = {}
    for net, (sheet, x, y) in sorted(points.items()):
        by_sheet.setdefault(sheet, []).append(C.pt(x, y))
    return " ".join(f"EDIT .s{s}; CHANGE CLASS {nm} {' '.join(pts)};" for s, pts in sorted(by_sheet.items()))
