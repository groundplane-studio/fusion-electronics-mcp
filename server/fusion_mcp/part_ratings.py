"""Current ratings of parts, kept from datasheet lookups (part number -> amps, source, date) in
one file in the per-user data folder, shared by all designs. On the PoE board the magnetics
(T1, CND-tek G2415S, 802.3af only) are rated 350 mA although PoE allows up to 960 mA: the
part, not the standard, sets the limit."""

from __future__ import annotations

import datetime
import json
import os
import re

from . import data_dir

# Reference prefixes of parts that carry a net's current whatever their other pins do: connectors,
# fuses, inductors / ferrites, transformers / magnetics, switches and relays.
SERIES_PREFIXES = ("J", "P", "CN", "CON", "X", "USB", "F", "FB", "FU", "PTC", "L", "T", "TR",
                   "SW", "S", "K", "RLY")
GROUND = re.compile(r"GND|GROUND|EARTH|^VSS|^0V$|^PE$", re.I)

ATTRS = ("MPN", "MFR_PN", "MANUFACTURER_PART_NUMBER", "PART_NUMBER", "PARTNO", "LCSC", "JLCPCB", "VALUE")


def path() -> str:
    return data_dir("part_ratings.json")


def load() -> dict:
    try:
        with open(path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(data: dict) -> str:
    import tempfile
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=os.path.dirname(p))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return p


def set_rating(part: str, current_a: float, source: str, note: str | None = None) -> dict:
    if not part.strip() or current_a <= 0 or not source.strip():
        raise ValueError("give the part number, a current above 0 A and where the rating comes from (datasheet link)")
    from . import design_store
    with design_store.locked():               # one load-change-save at a time across sessions
        data = load()
        data[part.strip().upper()] = {"part": part.strip(), "current_a": float(current_a), "source": source.strip(),
                                      "note": note, "date": datetime.date.today().isoformat()}
        save(data)
    return data[part.strip().upper()]


def is_ground(net: str | None) -> bool:
    """A ground net by name: GND (AGND, PGND, GND_ISO...), GROUND, EARTH, VSS, 0V, PE."""
    return bool(net) and bool(GROUND.search(net))


def in_path(ref: str, part_nets: set[str], net: str) -> str | None:
    """Why a part touching a net carries that net's current, or None when it does not (a shunt).
    Heuristic on the reference prefix and where the part's other pads go:
    - connectors, fuses, inductors, ferrites, transformers, switches, relays (SERIES_PREFIXES):
      always in the path;
    - capacitors (C): never, they pass no DC (decoupling, AC coupling);
    - any other part whose only other nets are ground (TVS, ESD diode, pull-down): not counted,
      it sits across the rail to ground;
    - any other part bridging this net to another non-ground net: in the path (current sense
      resistor, series diode, load switch or regulator IC, which may also have a ground pin).
      A pull-up to a signal net is also counted: the heuristic cannot tell it from a series
      part, so record ratings only for parts that matter.
    Parts with every pad on this net, or with no other connected pad, are not counted."""
    m = re.match(r"[A-Za-z]+", ref or "")
    prefix = m.group(0).upper() if m else ""
    if prefix in SERIES_PREFIXES:
        return "series part (connector, fuse, inductor, ferrite, magnetics or switch)"
    if prefix == "C":
        return None
    others = {n for n in part_nets if n != net}
    bridged = {n for n in others if not is_ground(n)}
    if not bridged:
        return None
    return f"bridges {net} to {', '.join(sorted(bridged))}"


def ratings_on(board_root, net: str, data: dict | None = None) -> list[dict]:
    """Rated parts that carry a net's current (see in_path: shunt parts such as decoupling caps,
    TVS diodes and pull-downs to ground are left out), matched on the element's value or its
    part-number attributes. Each row has the rating plus "in_path", why the part counts."""
    data = load() if data is None else data
    if not data:
        return []
    els = {e.get("name"): e for e in board_root.iter("element")}
    nets_of: dict[str, list[str]] = {}
    for sg in board_root.iter("signal"):
        for c in sg.iter("contactref"):
            nets_of.setdefault(c.get("element"), []).append(sg.get("name"))
    refs = sorted(r for r, ns in nets_of.items() if net in ns)
    out = []
    for ref in refs:
        e = els.get(ref)
        if e is None:
            continue
        keys = [e.get("value") or ""] + [a.get("value") or "" for a in e.iter("attribute") if (a.get("name") or "").upper() in ATTRS]
        hit = next((data[k.strip().upper()] for k in keys if k.strip() and k.strip().upper() in data), None)
        if not hit:
            continue
        why = in_path(ref, set(nets_of[ref]), net)
        if why:
            out.append({"ref": ref, **hit, "in_path": why})
    return sorted(out, key=lambda r: r["current_a"])
