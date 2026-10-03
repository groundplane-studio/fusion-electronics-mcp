"""Live end-to-end check of the MCP tools against an open, DISPOSABLE design.

    python tests/live/e2e_basic_board.py "Basic Board"

Requires Fusion with the FusionElectronicsMCP add-in running and the named
design open. Makes changes and does NOT save them; close the design without
saving afterwards. Each step prints PASS/FAIL with the tool's own detail.
"""

import json
import sys
import traceback

from mcp.server.mcpserver.exceptions import ToolError

import os
# a library with variant-based passives (device RES_0603_10K_1%_1/10W); set to yours
PASSIVES = os.environ.get("FUSION_MCP_TEST_PASSIVES", "!GPLIB_PASSIVE")

from fusion_mcp import server as S

DESIGN = sys.argv[1] if len(sys.argv) > 1 else "Basic Board"
results = []


def step(name, fn, expect=None):
    try:
        r = fn()
        ok = True if expect is None else bool(expect(r))
        results.append((name, ok))
        print(("PASS " if ok else "FAIL ") + name + ": " + json.dumps(r, default=str)[:300])
        return r
    except ToolError as ex:
        results.append((name, False))
        print(f"FAIL {name}: ToolError: {ex}")
    except Exception:
        results.append((name, False))
        print(f"FAIL {name}: " + traceback.format_exc()[-600:])


def expect_error(name, fn, needle):
    try:
        fn()
        results.append((name, False))
        print(f"FAIL {name}: expected an error")
    except ToolError as ex:
        ok = needle in str(ex)
        results.append((name, ok))
        print(("PASS " if ok else "FAIL ") + f"{name}: refused: {str(ex)[:200]}")


S.session.bridge.call("activate", {"kind": "board", "name": DESIGN})
step("context", S.get_context, lambda r: r["active_document"]["name"] == DESIGN)
summary = step("board summary", S.get_board_summary, lambda r: r["parts"] >= 3)
parts = step("list parts", S.list_parts, lambda r: any(p["ref"] == "R1" for p in r))
r1 = next(p for p in parts if p["ref"] == "R1")
step("get part R1", lambda: S.get_part("R1"), lambda r: "board" in r and "schematic" in r)
step("layer stack", S.get_layer_stack, lambda r: r["copper_layers"])

step("move R1", lambda: S.move_part("R1", 6.5, 7.25))
step("rotate R1 bottom 90", lambda: S.rotate_part("R1", 90, bottom=True))
step("rotate R1 back", lambda: S.rotate_part("R1", r1["angle"], bottom=r1["side"] == "bottom"))
step("move R1 back", lambda: S.move_part("R1", r1["x_mm"], r1["y_mm"]))
expect_error("move unknown part", lambda: S.move_part("NOPE99", 1, 1), "Unknown element: NOPE99")

step("add part R20 (schematic)", lambda: S.add_part("RES_0603_10K_1%_1/10W", PASSIVES, "R20", 30.48, -30.48),
     lambda r: r["part"]["board"] is not None)
step("connect R20.1 + R1.P$2 as MCP_NET", lambda: S.connect_pins("MCP_NET", ["R20.1", "R1.P$2"], allow_merge=True),
     lambda r: {"R20.P$1", "R1.P$2"} <= {f"{p['part']}.{p['pin']}" for p in r["net"]["schematic"]["pins"]})
step("rename MCP_NET -> MCP_RENAMED", lambda: S.rename_net("MCP_NET", "MCP_RENAMED"))
step("variant R20 -> 680R", lambda: S.set_part_variant("R20", "_680R_1%_1/10W"))
expect_error("value on fixed-value part", lambda: S.set_part_value("R20", "1k"), "set_part_variant")
step("get net", lambda: S.get_net("MCP_RENAMED"), lambda r: "board" in r)
step("via on MCP_RENAMED", lambda: S.add_via("MCP_RENAMED", 3.0, 3.0))
step("trace on MCP_RENAMED", lambda: S.add_trace("MCP_RENAMED", 1, 0.25, [[3.0, 3.0], [6.0, 3.0]]))
step("run drc", S.run_drc, lambda r: "count" in r)
step("review schematic", S.review_schematic, lambda r: "findings" in r and "erc" in r)
step("diff pairs", S.list_diff_pairs)
step("export bom", S.export_bom, lambda r: "JLCPCB" in r["bom_csv"])
step("export cpl", S.export_cpl, lambda r: "Designator" in r["cpl_csv"])
step("service stub", S.get_assembly_quote, lambda r: r["status"] == "not_connected")

bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (f"; failed: {bad}" if bad else ""))
sys.exit(1 if bad else 0)
