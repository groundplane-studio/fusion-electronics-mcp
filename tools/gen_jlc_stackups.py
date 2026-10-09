"""Build Fusion stackup (.estackup) and rule (.edru) files from JLCPCB's published impedance
stackups (https://jlcpcb.com/impedance).

    python tools/gen_jlc_stackups.py fetch      # download the page and parse it to jlc_stackups.json
    python tools/gen_jlc_stackups.py build      # write server/fusion_mcp/data/{stackups,rules}

Thicknesses and dielectric constants are JLC's own: prepreg 7628 4.4, 3313 4.1, 1080 3.91,
2116 4.16, core 4.6, solder mask 3.8 (as stated on the page). Fusion has one dielectric layer
between two copper layers, so stacked prepreg plies are combined: their thicknesses add and the
Er is the series (capacitance-weighted) value, total / sum(t_i / Er_i). A stackup using a
material whose Er JLC does not state is skipped, never guessed. The rules part of each .edru is
the 4-layer rule set in data/rules (Groundplane's JLC rules) with RULES applied: values raised
to JLC's published minimums.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "server", "fusion_mcp", "data")
SRC = os.path.join(DATA, "jlc_stackups.json")
URL = "https://jlcpcb.com/impedance"
ER = {"7628": 4.4, "3313": 4.1, "1080": 3.91, "2116": 4.16}
CORE_ER, MASK_ER = 4.6, 3.8
RULES_TEMPLATE = os.path.join(DATA, "rules", "JLC04161H-3313A 4-layer.edru")
TWO_LAYER = os.path.join(DATA, "rules", "JLC 2-layer 1.6mm.edru")
# rule values raised to JLC's published minimums (jlcpcb.com/capabilities/pcb-capabilities, 2026-10-04);
# applied to every .edru, the 2-layer set included
RULES = {
    "msDrill": "0.3mm",          # JLC allows 0.15-0.2 mm but charges more below 0.3 mm; go smaller by hand
    "mdDrill": "0.2mm",          # via hole to via hole 0.2 mm (pad holes want 0.45 mm)
    # mdSmd* are SAME-signal clearances (different nets use mdPadPad etc.): each must stay at or
    # under the smallest different-signal value (0.1 mm), or Fusion's DRC plausibility check stops it
    "mdSmdSmd": "0.1mm", "mdSmdPad": "0.1mm",
    # PTH annular ring: 2-layer minimum 0.18 mm, and 0.18 + 0.1 pad-to-track meets PTH-to-track 0.28 mm
    "rlMinPadTop": "0.18mm", "rlMinPadInner": "0.18mm", "rlMinPadBottom": "0.18mm",
    # via ring 0.1 mm + 0.1 mm wire-to-via meets via hole to track 0.2 mm (0.3 mm drill -> 0.5 mm via)
    "rlMinViaOuter": "0.1mm", "rlMinViaInner": "0.1mm",
    # same-signal via spacing may not exceed the different-signal one (mdViaVia 0.12 mm), or
    # Fusion's DRC stops on a plausibility warning before it runs
    "mdViaViaSameLayer": "0.12mm",
}


def apply_rules(text: str) -> str:
    for name, value in RULES.items():
        text, n = re.subn(rf'(<param name="{name}" value=")[^"]*(")', rf"\g<1>{value}\g<2>", text)
        if n != 1:
            raise ValueError(f"rule {name} found {n} times")
    return text


def parse(html: str) -> dict:
    end = html.find("window.__NUXT__")
    t = re.sub(r"<[^>]+>", "|", html[: end if end > 0 else len(html)])
    t = re.sub(r"(\|\s*)+", "|", t)
    heads = list(re.finditer(r"\|(\d+)\) (JLC0[46]161H-[0-9A-Z]+) Stackup\|", t))
    six = t.find("6-Layer Impedance Control Stackup")
    out = {}
    for k, h in enumerate(heads):
        name = h.group(2)
        if name in out:
            continue
        stop = heads[k + 1].start() if k + 1 < len(heads) else h.end() + 4000
        if name.startswith("JLC04") and six > h.end():
            stop = min(stop, six)                     # the last 4-layer table ends at the 6-layer section
        layers, glass = [], None
        for tok in (x.strip() for x in t[h.end():stop].split("|")):
            if re.fullmatch(r"\d{4}\*\d", tok):
                glass = tok.split("*")[0]
                continue
            m = re.fullmatch(r"(\d+\.\d+)mm", tok)
            if not m:
                continue
            mm = float(m.group(1))
            if mm <= 0.071:
                layers.append({"kind": "copper", "mm": mm})
            elif glass:
                layers.append({"kind": "prepreg", "glass": glass, "mm": mm})
                glass = None
            else:
                layers.append({"kind": "core", "mm": mm})
        out[name] = layers
    return out


def fetch() -> None:
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 (fusion-electronics-mcp stackup refresh)"})
    html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    data = {"source": URL, "retrieved": datetime.date.today().isoformat(),
            "dielectric_constants": {**ER, "core": CORE_ER, "solder_mask": MASK_ER}, "stackups": parse(html)}
    with open(SRC, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    print(f"{len(data['stackups'])} stackups -> {SRC}")


def _layer(kind: str, extra: str, inner: str) -> str:
    return f' <layerdef type="{kind}" id="{{{uuid.uuid4()}}}"{extra}>\n {inner}\n</layerdef>\n'


def keep_ids(text: str, path: str) -> str:
    """Reuse the layer ids already in `path` (same layers, same order), so rebuilding the files
    only changes what really changed instead of every id."""
    if not os.path.exists(path):
        return text
    with open(path, encoding="utf-8") as f:
        old = re.findall(r'<layerdef [^>]*id="([^"]*)"', f.read())
    new = re.findall(r'<layerdef [^>]*id="([^"]*)"', text)
    if len(old) != len(new):
        return text
    it = iter(old)
    return re.sub(r'(<layerdef [^>]*id=")[^"]*(")', lambda m: m.group(1) + next(it) + m.group(2), text)


def stackup_xml(name: str, layers: list[dict], retrieved: str) -> str | None:
    cu = [i for i, layer in enumerate(layers) if layer["kind"] == "copper"]
    if len(cu) < 2 or cu[0] != 0 or cu[-1] != len(layers) - 1:
        raise ValueError(f"{name}: copper is not on the outside")
    n = len(cu)
    half = (n - 2) // 2
    # Fusion numbers inner copper from the top (2, 3, ...) and from the bottom (..., 302, 303)
    numbers = [1] + list(range(2, 2 + half)) + list(range(304 - half, 304)) + [304]
    mask = (f'<material name="JLC solder mask" thickness="0.0305mm" dielectric_constant="{MASK_ER}" '
            'dissipation_factor="0.025" glass_transition_temperature="125degC" color="#FF000000" '
            'description="JLC solder mask (1.2 mil over substrate, Er 3.8)" source="User"/>')
    silk = ('<material type="ASP" name="ASP:White" material="Epoxy Ink" process="Automatic Screen Printing" '
            'color="#FFFFFFFF" description="Automatic Screen Printing - White"/>')
    finish = ('<material type="HASL-LeadFree" material="Lead-Free" process="HASL Lead-Free" thickness="0.02mm" '
              'color="#FFF2F2F2" description="Surface Finish"/>')
    body = _layer("Silk Screen", ' name="Top SilkScreen"', silk)
    body += _layer("Solder Mask", ' name="Top SolderMask" locally_modified_material="yes"', mask)
    body += _layer("Surface Finish", ' name="Top Surface Finish"', finish)
    for k in range(n):
        lo = layers[cu[k]]
        outer = k in (0, n - 1)
        cname = "Top" if k == 0 else "Bottom" if k == n - 1 else f"L{k + 1}"
        weight, mname = ("1oz", "1oz outer") if outer else ("1/2oz", "0.5oz inner")
        body += _layer("Signal", f' layer="{numbers[k]}" name="{cname}" comment="No comment" locally_modified_material="yes"',
                       f'<material name="{mname}" thickness="{lo["mm"]}mm" weight="{weight}" process="Electro Deposited" '
                       f'description="JLC {"outer" if outer else "inner"} copper" source="User"/>')
        if k == n - 1:
            break
        plies = layers[cu[k] + 1:cu[k + 1]]
        if not plies:
            raise ValueError(f"{name}: no dielectric between copper {k + 1} and {k + 2}")
        kinds = {p["kind"] for p in plies}
        # some 6-layer stackups put prepreg + a bare core (no copper) + prepreg between two copper
        # layers: electrically one dielectric, combined like stacked prepreg plies
        kind = "Core" if kinds == {"core"} else "Prepreg"
        ers = [ER.get(p["glass"]) if p["kind"] == "prepreg" else CORE_ER for p in plies]
        if None in ers:
            return None                                # an Er JLC does not state: skip, never guess
        t = round(sum(p["mm"] for p in plies), 4)
        er = round(t / sum(p["mm"] / e for p, e in zip(plies, ers)), 3)
        desc = " + ".join((f'{p["glass"]} {p["mm"]}' if p["kind"] == "prepreg" else f'core {p["mm"]}') + " mm"
                          for p in plies)
        oem = "+".join(p.get("glass", "Core") for p in plies)
        body += _layer(kind, f' layer="{2 * k + 1}" name="Dielectric-{2 * k + 1}" comment="" locally_modified_material="yes"',
                       f'<material name="JLC {oem} {t}" oem="JLC" oem_material="{oem}" thickness="{t}mm" '
                       f'dielectric_constant_1g="{er}" description="{name}: {desc} (JLC, {retrieved})" source="User"/>')
    body += _layer("Surface Finish", ' name="Bottom Surface Finish"', finish)
    body += _layer("Solder Mask", ' name="Bottom SolderMask" locally_modified_material="yes"', mask)
    body += _layer("Silk Screen", ' name="Bottom SilkScreen"', silk)
    return (f'<layerstackup name="{name}" type="Standard Stackup" version="V2" display_unit="mic" '
            f'roughness_model="1" allow_instack_material_edit="yes">\n{body} <viadef name="Thru 1:64" '
            f'startlayer="1" stoplayer="304" complement="no"/>\n </layerstackup>')


def build() -> None:
    with open(SRC, encoding="utf-8") as f:
        data = json.load(f)
    with open(RULES_TEMPLATE, encoding="utf-8") as f:
        rules = f.read()
    made, skipped = [], []
    head = '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE eagle SYSTEM "eagle.dtd">\n<eagle version="9.7.0">\n'
    for name, layers in data["stackups"].items():
        xml = stackup_xml(name, layers, data["retrieved"])
        if xml is None:
            skipped.append(name)
            continue
        n = sum(1 for layer in layers if layer["kind"] == "copper")
        base = f"{name} {n}-layer"
        path = os.path.join(DATA, "stackups", base + ".estackup")
        xml = keep_ids(xml, path)                 # the rule file carries the same stackup and ids
        with open(path, "w", encoding="utf-8") as f:
            f.write(head + xml + "\n</eagle>\n")
        r = re.sub(r"<layerstackup .*?</layerstackup>", lambda _: xml, rules, count=1, flags=re.S)
        r = re.sub(r'<designrules name="[^"]*"', f'<designrules name="{name} (Groundplane rules)"', r, count=1)
        with open(os.path.join(DATA, "rules", base + ".edru"), "w", encoding="utf-8") as f:
            f.write(apply_rules(r))
        made.append(base)
    with open(TWO_LAYER, encoding="utf-8") as f:
        two = f.read()
    with open(TWO_LAYER, "w", encoding="utf-8") as f:
        f.write(apply_rules(two))
    print(f"built {len(made)}; skipped (an Er JLC does not state): {skipped}")


if __name__ == "__main__":
    {"fetch": fetch, "build": build}[sys.argv[1] if len(sys.argv) > 1 else "build"]()
