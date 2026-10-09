"""Design rules (offline): read teardrop, pair and clearance settings from the board's V2
rules, and write a modified rule file (.edru) to load in Fusion's DRC dialog (the API cannot
write rules).

V2 rules seen in the bundled JLC files and the PoE board: four Teardrop rules (onescope
is_via / is_pad / is_smd / is_wire_polygon; lengthratio, widthratio, curved_sides,
auto_generate), one "Matched Lengths" rule (tolerance, gapfactor: the pair skew and gap factor
Fusion checks pairs with) and built-in Copper Clearance rules per object pair. The classic
<param>s dpMaxLengthDifference / dpGapFactor / md* carry the same values and are kept in step.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .net_classes import _mm, edru_text, set_classes

TEARDROP = {"via": "is_via", "pad": "is_pad", "smd": "is_smd", "wire_polygon": "is_wire_polygon"}
CLEARANCE = {"wire_wire": ("is_wire_polygon", "is_wire_polygon", "mdWireWire"),
             "wire_pad": ("is_wire_polygon", "is_pad_smd", "mdWirePad"),
             "wire_via": ("is_wire_polygon", "is_via", "mdWireVia"),
             "pad_pad": ("is_pad_smd", "is_pad_smd", "mdPadPad"),
             "pad_via": ("is_pad_smd", "is_via", "mdPadVia"),
             "via_via": ("is_via", "is_via", "mdViaVia")}
EAGLE_PAIR_DEFAULT = 10.0           # mm: dpMaxLengthDifference / Matched Lengths tolerance out of the box


def _yes(v) -> bool:
    return str(v).lower() == "yes"


def _clearance_rule(rules: ET.Element, one: str, other: str):
    for r in rules.iter("rule"):
        if (r.get("type") == "Copper Clearance" and r.get("builtin_ruleid") is not None
                and {r.get("onescope"), r.get("otherscope")} == {one, other} and r.get("samesignal", "no") != "yes"):
            return r
    return None


def settings(rules_xml: str) -> dict:
    """Teardrops per object type, the pair rule, built-in clearances, and warnings about defaults."""
    root = ET.fromstring(rules_xml)
    tear = {}
    for name, scope in TEARDROP.items():
        r = next((r for r in root.iter("rule") if r.get("type") == "Teardrop" and r.get("onescope") == scope), None)
        if r is not None:
            tear[name] = {"enabled": _yes(r.get("enabled")), "auto_generate": _yes(r.get("auto_generate")),
                          "lengthratio": float(r.get("lengthratio") or 0), "widthratio": float(r.get("widthratio") or 0),
                          "curved_sides": _yes(r.get("curved_sides"))}
    ml = next((r for r in root.iter("rule") if r.get("type") == "Matched Lengths"), None)
    pair = {"max_length_difference_mm": _mm(ml.get("tolerance")) if ml is not None else None,
            "gap_factor": float(ml.get("gapfactor")) if ml is not None and ml.get("gapfactor") else None}
    clear = {}
    for name, (one, other, _) in CLEARANCE.items():
        r = _clearance_rule(root, one, other)
        if r is not None:
            clear[name] = _mm(r.get("value"))
    warnings = []
    if pair["max_length_difference_mm"] is not None and abs(pair["max_length_difference_mm"] - EAGLE_PAIR_DEFAULT) < 1e-6:
        warnings.append("the pair rule's length difference is the 10 mm EAGLE default: DRC lets a pair's P and N "
                        "differ by up to 10 mm. Set your interface's skew (length_tolerances) with edit_design_rules")
    if tear and not any(t["auto_generate"] for t in tear.values() if t["enabled"]):
        warnings.append("teardrop rules are defined but none auto-generates teardrops")
    return {"teardrops": tear, "pair": pair, "clearances_mm": clear, "warnings": warnings}


def edit(rules_xml: str, classes: dict[int, str], teardrops: dict | None = None,
         pair_max_length_difference_mm: float | None = None, pair_gap_factor: float | None = None,
         clearances_mm: dict | None = None, title: str | None = None) -> tuple[str | None, list[str], list[str]]:
    """(the rules with the changes applied as .edru text, what changed, what already had the value
    asked for). The text is None when nothing differs.
    teardrops: {"via" | "pad" | "smd" | "wire_polygon" | "all": {enabled, auto_generate, lengthratio,
    widthratio, curved_sides}} (only the keys given change); clearances_mm: {"wire_wire": 0.12, ...}."""
    root = ET.fromstring(rules_xml)
    if root.tag != "designrules":
        raise ValueError("expected a <designrules> element")
    changes, unchanged, asked = [], [], False
    params = {p.get("name"): p for p in root.iter("param")}

    def setp(name, value):
        if name in params:
            params[name].set("value", value)
    for key, spec in (teardrops or {}).items():
        names = list(TEARDROP) if key == "all" else [key]
        if any(n not in TEARDROP for n in names):
            raise ValueError(f"teardrop kinds: {', '.join(TEARDROP)} or all")
        for n in names:
            r = next((r for r in root.iter("rule") if r.get("type") == "Teardrop" and r.get("onescope") == TEARDROP[n]), None)
            if r is None:
                raise ValueError(f"these rules have no teardrop rule for {n}")
            for k, v in spec.items():
                if k not in ("enabled", "auto_generate", "lengthratio", "widthratio", "curved_sides"):
                    raise ValueError(f"teardrop setting {k!r}: enabled, auto_generate, lengthratio, widthratio, "
                                     "curved_sides (Fusion's 'avoid violations' option is not known in the file yet)")
                asked = True
                if k in ("lengthratio", "widthratio"):
                    if not 0 < float(v) <= 2:
                        raise ValueError(f"teardrop {k} {v}: a ratio between 0 and 2")
                    new = f"{float(v):g}"
                    same = abs(float(r.get(k) or 0) - float(v)) < 1e-9
                else:
                    new = "yes" if v else "no"
                    same = (r.get(k) or "no").lower() == new
                if same:
                    unchanged.append(f"teardrop {n}: {k} is already {v}")
                    continue
                r.set(k, new)
                changes.append(f"teardrop {n}: {k} = {v}")
    ml = next((r for r in root.iter("rule") if r.get("type") == "Matched Lengths"), None)
    if pair_max_length_difference_mm is not None:
        if ml is None or pair_max_length_difference_mm <= 0:
            raise ValueError("no Matched Lengths rule to set, or a length difference that is not positive")
        asked = True
        if abs(_mm(ml.get("tolerance")) - pair_max_length_difference_mm) < 1e-6:
            unchanged.append(f"pair max length difference is already {pair_max_length_difference_mm:g} mm")
        else:
            ml.set("tolerance", f"{pair_max_length_difference_mm:g}mm")
            setp("dpMaxLengthDifference", f"{pair_max_length_difference_mm:g}mm")
            changes.append(f"pair max length difference = {pair_max_length_difference_mm:g} mm")
    if pair_gap_factor is not None:
        if ml is None or pair_gap_factor <= 0:
            raise ValueError("no Matched Lengths rule to set, or a gap factor that is not positive")
        asked = True
        if abs(float(ml.get("gapfactor") or 0) - pair_gap_factor) < 1e-9:
            unchanged.append(f"pair gap factor is already {pair_gap_factor:g}")
        else:
            ml.set("gapfactor", f"{pair_gap_factor:g}")
            setp("dpGapFactor", f"{pair_gap_factor:g}")
            changes.append(f"pair gap factor = {pair_gap_factor:g}")
    for name, mm in (clearances_mm or {}).items():
        if name not in CLEARANCE:
            raise ValueError(f"clearance {name!r}: one of {', '.join(CLEARANCE)}")
        one, other, param = CLEARANCE[name]
        r = _clearance_rule(root, one, other)
        if r is None:
            raise ValueError(f"these rules have no built-in {name} clearance rule")
        if mm <= 0:
            raise ValueError(f"clearance {name} must be positive")
        asked = True
        if abs(_mm(r.get("value")) - mm) < 1e-6:
            unchanged.append(f"clearance {name} is already {mm:g} mm")
            continue
        r.set("value", f"{mm:g}mm")
        r.set("preferredvalue", f"{mm:g}mm")
        setp(param, f"{mm:g}mm")
        changes.append(f"clearance {name} = {mm:g} mm")
    if not asked:
        raise ValueError("nothing to change")
    if not changes:
        return None, [], unchanged
    set_classes(root, classes)
    return edru_text(root, title), changes, unchanged
