"""Bundled design-rule (.edru) and layer-stackup (.estackup) files for common fab processes.

Fusion cannot load rules from a script, so these are files to load by hand: rules in the DRC
dialog (Rules > Load), a stackup in the Layer Stack Manager (Load). Each .edru also carries its
stackup, so loading the rules alone sets both. Values are from the fab's published capabilities
and impedance calculator; check them against the fab before ordering.
"""

from __future__ import annotations

import os
import re
import shutil

from fusion_offline import stackup as SU

DATA = os.path.join(os.path.dirname(__file__), "data")
_KEYS = {"mdWireWire": "wire_to_wire", "mdWirePad": "wire_to_pad", "mdPadVia": "pad_to_via",
         "mdCopperDimension": "copper_to_edge", "msWidth": "min_width", "msDrill": "min_drill"}


def catalog() -> list[dict]:
    out = []
    rdir = os.path.join(DATA, "rules")
    for f in sorted(os.listdir(rdir)):
        if not f.endswith(".edru"):
            continue
        path = os.path.join(rdir, f)
        raw = open(path, "rb").read()
        text = raw.decode("utf-8", "replace")
        st = SU.parse_stackup(raw)
        name = (re.search(r'<designrules name="([^"]*)"', text) or [None, f[:-5]])[1]
        rules = {nice: v.group(1) for k, nice in _KEYS.items()
                 if (v := re.search(r'name="' + k + r'" value="([^"]*)"', text))}
        stack = os.path.join(DATA, "stackups", f[:-5] + ".estackup")
        out.append({
            "name": name,
            "rules_file": path,
            "stackup_file": stack if os.path.exists(stack) else None,
            "copper_layers": len(st.copper) if st else None,
            "board_thickness_mm": round(st.total_mm(), 3) if st else None,
            "dielectrics": [{"layer": l.name, "kind": l.kind, "thickness_mm": l.thickness_mm, "er": l.er}
                            for l in (st.layers if st else []) if l.kind in ("Prepreg", "Core")],
            "rules": rules,
        })
    return out


def copy_to(folder: str) -> list[str]:
    os.makedirs(folder, exist_ok=True)
    done = []
    for sub in ("rules", "stackups"):
        for f in os.listdir(os.path.join(DATA, sub)):
            shutil.copy2(os.path.join(DATA, sub, f), os.path.join(folder, f))
            done.append(os.path.join(folder, f))
    return done
