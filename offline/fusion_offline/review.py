"""Offline schematic review rules over the exported schematic model.

These complement Fusion's own ERC (run live through the bridge). Each rule
returns findings {severity, rule, message, refs}; severities are
'error' (almost certainly wrong), 'warning' (review it), 'info'.
"""

from __future__ import annotations

from .design import SchematicDesign

# Pin directions as written in EAGLE XML.
DRIVERS = {"out", "sup", "pwr", "io", "oc", "hiz", "pas"}
NO_CONNECT = {"nc"}


def _natural(s: str):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def _is_physical(part) -> bool:
    """Frames, supply symbols and other package-less parts don't go on the
    board or the BOM."""
    return bool(part.package)


def _has_supply(s: SchematicDesign, seg) -> bool:
    """Supply symbols (GND, +3V3...) are package-less parts with a 'sup' pin;
    they name the net visibly, like a label."""
    for part_name, pin_name in seg.pins:
        part = s.parts.get(part_name)
        pin = s.pin(part_name, pin_name)
        if part and not part.package and pin and pin.direction == "sup":
            return True
    return False


def overbar(name: str) -> dict:
    """Fusion's overbar markup: each '!' turns the bar on or off ('PI_LED_!PWR' bars PWR).
    Returns {plain, readable ('PI_LED_~{PWR}'), suspect: why the markup looks wrong, or None}.
    A bar that stops before the end of the name usually belongs on the signal name at the end
    ('PI_!LED!_ACTIVITY' bars LED; meant 'PI_LED_!ACTIVITY'), and an empty bar ('!!') draws
    nothing."""
    if "!" not in name:
        return {"plain": name, "readable": name, "suspect": None}
    parts = name.split("!")
    plain = "".join(parts)
    readable = "".join(f"~{{{p}}}" if i % 2 and p else p for i, p in enumerate(parts))
    suspect = None
    barred = [p for i, p in enumerate(parts) if i % 2]
    if any(p == "" for i, p in enumerate(parts) if i % 2 and i < len(parts) - 1):
        suspect = "an empty overbar ('!!') draws nothing"
    elif len(parts) % 2 == 1 and parts[-1].strip("_- "):
        suspect = (f"the overbar covers {', '.join(repr(b) for b in barred)} and stops before "
                   f"{parts[-1]!r}: an active-low bar usually runs to the end of the name")
    return {"plain": plain, "readable": readable, "suspect": suspect}


def _visible_groups(segments):
    """Segments on the same sheet that end on a common pin are visibly joined
    through it; group those, so only truly separate pieces are counted."""
    groups: list[list] = []
    for seg in segments:
        keys = {(seg.sheet,) + p for p in seg.pins}
        hit = [g for g in groups if keys & {(x.sheet,) + p for x in g for p in x.pins}]
        merged = [seg] + [x for g in hit for x in g]
        groups = [g for g in groups if g not in hit] + [merged]
    return groups


def unlabeled_pieces(s: SchematicDesign, nets: list[str] | None = None) -> dict[str, list]:
    """Per net: the separately drawn pieces (that connect pins) carrying no
    label or supply symbol. One segment per bare piece is returned (the one
    to label)."""
    out: dict[str, list] = {}
    for n in s.nets.values():
        if nets and n.name not in nets:
            continue
        groups = _visible_groups([g for g in n.segments if g.pins or g.labels])
        if len(groups) < 2:
            continue
        bare = [g for g in groups if not any(seg.labels or _has_supply(s, seg) for seg in g)]
        picks = [next((seg for seg in g if seg.first_wire), None) for g in bare]
        picks = [p for p in picks if p]
        if picks:
            out[n.name] = picks
    return out


def review(s: SchematicDesign, jlc_attr: str = "JLCPCB") -> list[dict]:
    out: list[dict] = []
    add = lambda sev, rule, msg, refs: out.append({"severity": sev, "rule": rule, "message": msg, "refs": refs})

    connected = {(r.part, r.pin) for n in s.nets.values() for r in n.pins}
    loose: dict[str, list] = {}
    for p in s.pins:
        part = s.parts.get(p.part)
        if part is None or not _is_physical(part) or p.direction in NO_CONNECT:
            continue
        if p.pin.upper().split("@")[0] in ("NC", "N/C", "DNC") or p.pin.upper().startswith("NC_"):
            continue
        if (p.part, p.pin) not in connected:
            loose.setdefault(p.part, []).append(p)
    for ref, ps in sorted(loose.items()):
        power = [p for p in ps if p.direction in ("pwr", "sup")]
        if power:
            add("error", "unconnected_power_pin",
                f"{ref}: power pin(s) not connected: " + ", ".join(p.pin for p in power), [ref])
        other = [p for p in ps if p.direction not in ("pwr", "sup")]
        if other:
            names = ", ".join(p.pin for p in other[:12]) + (f" (+{len(other) - 12} more)" if len(other) > 12 else "")
            add("warning", "unconnected_pin",
                f"{ref}: {len(other)} pin(s) not connected: {names}. Mark intentional no-connects", [ref])

    for n in s.nets.values():
        phys = [r for r in n.pins if (s.parts.get(r.part) and _is_physical(s.parts[r.part]))]
        if len(n.pins) == 1:
            add("warning", "single_pin_net", f"net {n.name} connects only {n.pins[0].part}.{n.pins[0].pin}",
                [n.pins[0].part])
        dirs = {}
        for r in phys:
            pin = s.pin(r.part, r.pin)
            if pin:
                dirs.setdefault(pin.direction, []).append(f"{r.part}.{r.pin}")
        if len(dirs.get("out", [])) > 1:
            add("error", "output_conflict", f"net {n.name} has {len(dirs['out'])} output pins: "
                + ", ".join(dirs["out"]), [x.split(".")[0] for x in dirs["out"]])
        if dirs and set(dirs) == {"in"}:
            add("warning", "undriven_net", f"net {n.name} has only input pins ("
                + ", ".join(dirs["in"]) + ")", [x.split(".")[0] for x in dirs["in"]])

    for n in s.nets.values():
        # A net drawn as several separate pieces is only readable if each
        # piece carries a name label (or a supply symbol, which is a pin).
        loose = [g for g in n.segments if not g.pins and not g.labels]
        boxes = [g for g in loose if g.closed]
        stray = [g for g in loose if not g.closed]
        on = lambda gs: ", ".join(map(str, sorted({g.sheet for g in gs})))
        if boxes:
            add("info", "box_drawn_as_net",
                f"net {n.name}: {len(boxes)} closed outline(s) drawn with the NET tool (sheet {on(boxes)}); "
                "draw section boxes with LINE on a drawing layer so they are not nets", [n.name])
        if stray:
            add("info", "stray_wire",
                f"net {n.name} has {len(stray)} wire piece(s) that connect no pin and carry no label "
                f"(sheet {on(stray)}); likely leftovers to delete", [n.name])
        groups = _visible_groups([g for g in n.segments if g.pins or g.labels])
        if len(groups) > 1:
            bare = [g for g in groups if not any(seg.labels or _has_supply(s, seg) for seg in g)]
            if bare:
                sheets = sorted({seg.sheet for g in bare for seg in g})
                add("warning", "unlabeled_net_segment",
                    f"net {n.name} is drawn as {len(groups)} separate pieces; {len(bare)} have no net label "
                    f"(sheet {', '.join(map(str, sheets))}), so the connection is not visible", [n.name])

    for n in s.nets.values():
        # supply symbols name their net: a supply on a differently named net was overwritten
        # (Fusion ERC 102 "SUPPLY pin GND overwritten with N$25"; review missed it, 2026-10-04)
        sup = []
        for r in n.pins:
            part = s.parts.get(r.part)
            pin = s.pin(r.part, r.pin)
            if part and not part.package and pin and pin.direction == "sup":
                sup.append((r.part, r.pin, part.value or ""))
        for ref, pin_name, value in sup:
            names = {pin_name.split("@")[0].upper(), value.strip().upper()} - {""}
            if n.name.upper() not in names:
                add("error", "supply_on_other_net",
                    f"supply symbol {ref} ({' / '.join(sorted({pin_name.split('@')[0], value} - {''}))}) sits on net "
                    f"{n.name}: the supply name was overwritten. Join it to its rail or rename the net",
                    [ref, n.name])
        if sup and len(n.pins) == len(sup):
            add("warning", "supply_alone",
                f"net {n.name} connects only supply symbol(s) {', '.join(r for r, _, _ in sup)}: the wire "
                "probably misses the pin or the net it was meant for", [r for r, _, _ in sup])
        ob = overbar(n.name)
        if ob["suspect"]:
            add("warning", "overbar_markup", f"net {n.name} (reads {ob['readable']}): {ob['suspect']}", [n.name])

    no_code, no_value = [], []
    for part in s.parts.values():
        if not _is_physical(part):
            continue
        val = (part.value or "").strip()
        if not val.upper().startswith("NC/") and not part.attributes.get(jlc_attr, "").strip():
            no_code.append(part.name)
        if part.user_value and not val:
            no_value.append(part.name)
    if no_code:
        no_code.sort(key=_natural)
        add("info", "missing_jlc_code", f"{len(no_code)} part(s) have no {jlc_attr} code: "
            + ", ".join(no_code[:20]) + (" ..." if len(no_code) > 20 else ""), no_code)
    for ref in no_value:
        add("warning", "empty_value", f"{ref} needs a value but has none", [ref])
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda f: (order[f["severity"]], f["rule"], f["refs"]))
