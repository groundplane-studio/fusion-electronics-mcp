"""Catalog which EAGLE commands raise Fusion dialogs (live, disposable design).

    python tests/live/dialog_catalog.py

Runs each probe command with the dialog watchdog on, records any dialog text
and the button it pressed, and UNDOes the probe if the design changed. Writes
docs/dialog-catalog.json. Never saves.
"""

import json
import os
import sys

import os
# a library with variant-based passives (device RES_0603_10K_1%_1/10W); set to yours
PASSIVES = os.environ.get("FUSION_MCP_TEST_PASSIVES", "!GPLIB_PASSIVE")

from fusion_mcp.bridge import Bridge, BridgeOpError, describe
from fusion_mcp.session import Session

b = Bridge()
s = Session(b)

snap = s.snapshot()
sch = snap.schematic()
pins = {(p.part, p.pin): p for p in sch.pins}
nets = sorted(sch.nets)
inst = {i.part: i for i in sch.instances}
r1, r2 = inst["R1"], inst["R2"]
p_r1 = pins[("R1", "P$2")]
p_r2 = pins[("R2", "P$1")]
print("nets:", nets)

PROBES = [
    ("schematic", "value_fixed_part", "VALUE 'R2' '1k';"),
    ("schematic", "unknown_command", "FROBNICATE 'R1';"),
    ("schematic", "add_unknown_device", "GRID MM; ADD 'NOPE_DEVICE@NOPE_LIB' 'R50' R0 (60 60);"),
    ("schematic", "add_duplicate_name", f"GRID MM; ADD 'RES_0603_10K_1%_1/10W@{PASSIVES}' 'R1' R0 (60 60);"),
    ("schematic", "name_part_to_existing", f"GRID MM; NAME 'R1' ({r2.x} {r2.y});"),
    ("schematic", "name_nothing_at_point", "GRID MM; NAME 'XX' (90 90);"),
    ("schematic", "edit_missing_sheet", "EDIT .s9;"),
    ("schematic", "net_from_empty_point", "GRID MM; NET 'N_FREE' (80 80) (85 80);"),
    ("schematic", "net_join_two_named_nets", f"GRID MM; NET 'JOIN_A' ({p_r1.x} {p_r1.y}) ({p_r1.x + p_r1.outward[0]*2.54} {p_r1.y + p_r1.outward[1]*2.54}); "
                                              f"NET 'JOIN_B' ({p_r2.x} {p_r2.y}) ({p_r2.x + p_r2.outward[0]*2.54} {p_r2.y + p_r2.outward[1]*2.54}); "
                                              f"NET ({p_r1.x + p_r1.outward[0]*2.54} {p_r1.y + p_r1.outward[1]*2.54}) ({p_r2.x + p_r2.outward[0]*2.54} {p_r2.y + p_r2.outward[1]*2.54});"),
    ("schematic", "variant_missing", f"GRID MM; PACKAGE '_NOPE_VARIANT' ({r1.x} {r1.y});"),
    ("schematic", "delete_part", f"GRID MM; DELETE ({r1.x} {r1.y});"),
    ("board", "move_unknown_part", "GRID MM; MOVE 'NOPE99' (1 1);"),
    ("board", "via_auto_diameter", "GRID MM; CHANGE DRILL 0.3; VIA 'VIA_TEST' auto round (2 2);"),
    ("board", "signal_on_linked_board", "SIGNAL 'SIG_T' 'R1' '1' 'R2' '1';"),
    ("board", "delete_element_on_board", "DELETE 'R1';"),
    ("board", "add_on_linked_board", f"GRID MM; ADD 'RES_0603_10K_1%_1/10W@{PASSIVES}' 'R51' R0 (5 5);"),
    ("board", "wire_unknown_layer", "GRID MM; LAYER 77; WIRE 'X' 0.2 (1 1) (2 2);"),
]

out = []
for editor, name, cmd in PROBES:
    before = s.snapshot()
    rec = {"probe": name, "editor": editor, "commands": cmd}
    try:
        s.activate(editor)
        r = b.call("run", {"commands": cmd, "editor": editor}, timeout=40)
        rec["ms"] = r.get("ms")
        rec["dialogs"] = r.get("dialogs", [])
    except BridgeOpError as ex:
        rec["refused"] = str(ex)
        rec["dialogs"] = b.last_dialogs
    after = s.snapshot()
    rec["changed"] = (after.board_xml != before.board_xml) or (after.sch_xml != before.sch_xml)
    if rec["changed"]:
        s.undo(editor)
        again = s.snapshot()
        rec["undone_clean"] = again.board_xml == before.board_xml and again.sch_xml == before.sch_xml
    for d in rec["dialogs"]:
        d.pop("hwnd", None)
    print(f"{name:28s} dialogs={len(rec['dialogs'])} changed={rec['changed']} "
          + ("; ".join(describe(d) for d in rec["dialogs"]) or rec.get("refused", "")))
    out.append(rec)

path = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "dialog-catalog.json")
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w", encoding="utf-8") as f:
    json.dump({"fusion": b.call("ping")["fusion_version"], "probes": out}, f, indent=2, ensure_ascii=False)
print("wrote", os.path.abspath(path))
