"""Current ratings of parts, kept from datasheet lookups (part number -> amps, source, date) in
one file in the per-user data folder, shared by all designs. On the PoE board the magnetics
(T1, CND-tek G2415S, 802.3af only) are rated 350 mA although PoE allows up to 960 mA: the
part, not the standard, sets the limit."""

from __future__ import annotations

import datetime
import json
import os

from . import data_dir

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
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, p)
    return p


def set_rating(part: str, current_a: float, source: str, note: str | None = None) -> dict:
    if not part.strip() or current_a <= 0 or not source.strip():
        raise ValueError("give the part number, a current above 0 A and where the rating comes from (datasheet link)")
    data = load()
    data[part.strip().upper()] = {"part": part.strip(), "current_a": float(current_a), "source": source.strip(),
                                  "note": note, "date": datetime.date.today().isoformat()}
    save(data)
    return data[part.strip().upper()]


def ratings_on(board_root, net: str, data: dict | None = None) -> list[dict]:
    """Rated parts touching a net: matched on the element's value or its part-number attributes."""
    data = load() if data is None else data
    if not data:
        return []
    els = {e.get("name"): e for e in board_root.iter("element")}
    refs = sorted({c.get("element") for s in board_root.iter("signal") if s.get("name") == net
                   for c in s.iter("contactref")})
    out = []
    for ref in refs:
        e = els.get(ref)
        if e is None:
            continue
        keys = [e.get("value") or ""] + [a.get("value") or "" for a in e.iter("attribute") if (a.get("name") or "").upper() in ATTRS]
        hit = next((data[k.strip().upper()] for k in keys if k.strip() and k.strip().upper() in data), None)
        if hit:
            out.append({"ref": ref, **hit})
    return sorted(out, key=lambda r: r["current_a"])
