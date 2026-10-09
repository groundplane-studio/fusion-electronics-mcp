"""DRC results (offline): group them by type and tell new errors from old ones.

Fusion's DRC on a board in progress lists every airwire and every known
problem (234 lines on the PoE board), so after a write only the difference
matters. Errors are matched by Fusion's signature when it gives one, else by
type, layer and position (to 0.01 mm). An error without a signature that sits on
a part a write moved moves with it, so diffs for a write pair such errors up by
type and layer (pair_moved) instead of counting a moved error as new.
"""

from __future__ import annotations

import re

AIRWIRE = re.compile(r"air.?wire", re.I)
COPPER = re.compile(r"clearance|overlap|width|short|drill|restring|stop ?mask", re.I)


def kind(err: dict) -> str:
    """The error's type: its description up to the first " - " ("Copper Clearance - Net Class:
    7 eth_100" -> "Copper Clearance"), else its code."""
    d = str(err.get("description") or "").strip()
    return d.split(" - ")[0].strip() if d else str(err.get("code") or "unknown")


def key(err: dict) -> tuple:
    if err.get("signature"):
        return ("sig", str(err["signature"]))
    return (kind(err), str(err.get("description") or ""), str(err.get("layer") or ""),
            round(float(err.get("x_mm") or 0), 2), round(float(err.get("y_mm") or 0), 2))


def is_airwire(err: dict) -> bool:
    return bool(AIRWIRE.search(f"{err.get('code', '')} {err.get('description', '')}"))


def summarize(errors: list[dict], top: int = 5) -> dict:
    """{total, airwires, by_type: {type: count}} with airwires counted, not listed."""
    by = {}
    for e in errors:
        if not is_airwire(e):
            by[kind(e)] = by.get(kind(e), 0) + 1
    return {"total": len(errors), "airwires": sum(1 for e in errors if is_airwire(e)),
            "by_type": dict(sorted(by.items(), key=lambda kv: -kv[1]))}


def _same_kind(err: dict) -> tuple:
    return (kind(err), str(err.get("description") or ""), str(err.get("layer") or ""))


def diff(before: list[dict], after: list[dict], top: int = 5, pair_moved: bool = False) -> dict:
    """New and fixed errors between two DRC runs, grouped by type, with up to `top` locations
    per type; airwires as counts. pair_moved: an unsigned error that is gone and an unsigned one
    of the same type, description and layer that appeared count as one error that moved (with
    the part or trace a write moved), not as fixed + new; they are counted under "moved"."""
    b = {key(e): e for e in before}
    a = {key(e): e for e in after}
    new = [e for k, e in a.items() if k not in b and not is_airwire(e)]
    fixed = [e for k, e in b.items() if k not in a and not is_airwire(e)]
    moved = 0
    if pair_moved:
        def count(errs):
            n: dict[tuple, int] = {}
            for e in errs:
                if not e.get("signature"):
                    n[_same_kind(e)] = n.get(_same_kind(e), 0) + 1
            return n
        cn, cf = count(new), count(fixed)
        pairs = {k: min(cn[k], cf.get(k, 0)) for k in cn}
        moved = sum(pairs.values())

        def drop(errs):
            left, out = dict(pairs), []
            for e in errs:
                k = _same_kind(e)
                if not e.get("signature") and left.get(k):
                    left[k] -= 1
                else:
                    out.append(e)
            return out
        new, fixed = drop(new), drop(fixed)
    groups = {}
    for e in new:
        g = groups.setdefault(kind(e), {"count": 0, "at": [], "description": e.get("description")})
        g["count"] += 1
        if len(g["at"]) < top and e.get("x_mm") is not None:
            g["at"].append([e.get("x_mm"), e.get("y_mm")] + ([e["layer"]] if e.get("layer") else []))
    gone = {}
    for e in fixed:
        gone[kind(e)] = gone.get(kind(e), 0) + 1
    return {"new": groups, "new_count": len(new), "fixed": gone, "fixed_count": len(fixed),
            "unchanged_count": sum(1 for k, e in a.items() if k in b and not is_airwire(e)),
            "airwires": [sum(1 for e in before if is_airwire(e)), sum(1 for e in after if is_airwire(e))],
            "moved_count": moved, "new_copper": sum(1 for e in new if COPPER.search(kind(e)))}


def line(d: dict) -> str:
    """One line for a write's reply."""
    parts = []
    if d["new_count"]:
        parts.append(f"{d['new_count']} new (" + ", ".join(
            f"{t} x{g['count']}" + (f" at {g['at'][0][:2]}" if g["at"] else "") for t, g in d["new"].items()) + ")")
    else:
        parts.append("no new errors")
    if d["fixed_count"]:
        parts.append(f"{d['fixed_count']} fixed")
    if d.get("moved_count"):
        parts.append(f"{d['moved_count']} existing moved with the change")
    a0, a1 = d["airwires"]
    if a0 != a1:
        parts.append(f"airwires {a0} -> {a1}")
    return "DRC: " + "; ".join(parts)
