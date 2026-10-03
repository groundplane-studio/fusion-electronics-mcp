"""Run the read/validation tools over real designs in the active project.

    python tests/live/validate_designs.py ["Design name" ...]

Reports go to %LOCALAPPDATA%/fusion-electronics-mcp/validation/ (OUTSIDE the
repo: they contain design data and must never be committed). Only counts are
printed. Designs are opened, checked, and closed WITHOUT saving (DRC/ERC can
mark a design modified).
"""

import json
import os
import sys
import time
import traceback
from collections import Counter

from mcp.server.mcpserver.exceptions import ToolError

from fusion_mcp import server as S

OUT = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "fusion-electronics-mcp", "validation")
os.makedirs(OUT, exist_ok=True)

designs = S.list_designs()["designs"]
wanted = sys.argv[1:] or [d["name"] for d in designs if d["name"] != "Basic Board"]

for d in [x for x in designs if x["name"] in wanted]:
    name = d["name"]
    report, timing = {"design": name}, {}

    def run(key, fn):
        t = time.perf_counter()
        try:
            report[key] = fn()
        except ToolError as ex:
            report[key] = {"error": str(ex)}
        except Exception:
            report[key] = {"error": traceback.format_exc()[-800:]}
        timing[key] = round(time.perf_counter() - t, 1)

    run("open", lambda: S.open_design(name, d["folder"]))
    run("summary", S.get_board_summary)
    run("layer_stack", S.get_layer_stack)
    run("review", lambda: S.review_schematic(include_erc=True))
    run("drc", S.run_drc)
    run("diff_pairs", S.list_diff_pairs)
    run("bom", S.export_bom)
    run("cpl", S.export_cpl)
    run("close", lambda: S.close_design(name, discard_changes=True))
    report["timing_s"] = timing
    safe = "".join(c if c.isalnum() else "_" for c in name)
    with open(os.path.join(OUT, f"{safe}.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, ensure_ascii=False, default=str)

    errs = [k for k, v in report.items() if isinstance(v, dict) and "error" in v]
    summ = report.get("summary") or {}
    rev = report.get("review") or {}
    findings = Counter((x["severity"], x["rule"]) for x in rev.get("findings", []))
    pairs = report.get("diff_pairs") if isinstance(report.get("diff_pairs"), list) else []
    drc = report.get("drc") or {}
    print(f"\n== {name}: {summ.get('parts')} parts, {summ.get('nets')} nets, "
          f"{len(summ.get('copper_layers') or [])} copper layers; tool errors: {errs or 'none'}")
    print("   review:", dict(findings))
    print("   ERC:", (rev.get("erc") or {}).get("count"), " DRC:", drc.get("count"),
          dict(Counter(e.get("description") for e in drc.get("errors", []))))
    print("   diff pairs:", len(pairs), "worst skew mm:", max((p["skew_mm"] for p in pairs), default=None))
    print("   BOM lines:", (report.get("bom") or {}).get("lines"), " timing:", timing)
print("\nreports in", OUT)
