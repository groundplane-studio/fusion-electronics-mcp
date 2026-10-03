"""MCP server: Fusion Electronics read, write, SI, JLC export and library tools.

Reads come from Fusion's EAGLE XML export (parsed here); writes are EAGLE
commands sent through the add-in and verified by re-exporting. No network
access except 127.0.0.1 to the add-in, and easyeda.com only when
check_jlc_orientation is called with fetch=true (see easyeda.py); the
service tools are stubs.
"""

from __future__ import annotations

import contextlib
import functools
import math
import re
import os
import time
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from fusion_offline import bompnp, design as D, review as review_rules, si

from . import PROJECT_URL, __version__

from . import commands as C
from . import dialogs
from .bridge import BridgeOpError, BridgeUnavailable
from .library import Library, build_script
from .session import Session, Snapshot, WriteFailed

mcp = MCPServer(
    "fusion-electronics",
    version=__version__,
    website_url=PROJECT_URL,
    instructions=(
        "Tools for Autodesk Fusion Electronics designs open in Fusion (needs Fusion running with its "
        "MCP server turned on, Preferences > General > API, or the FusionElectronicsMCP add-in). "
        "Coordinates are millimetres. Reads reflect the design as "
        "exported at call time. Every write is verified by reading the design back and is undone "
        "if the result does not match. Nothing is saved until save_design is called."),
)
session = Session()
library = Library()

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
ADDITIVE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
CHANGE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)
CALC = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def tool(annotations: ToolAnnotations):
    """Register a tool and turn bridge/validation failures into clear ToolErrors."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapped(*a, **kw):
            try:
                return fn(*a, **kw)
            except BridgeUnavailable as ex:
                raise ToolError(str(ex)) from None
            except BridgeOpError as ex:
                raise ToolError(f"Fusion refused the operation ({ex.code}): {ex.message}") from None
            except (WriteFailed, C.InvalidInput, KeyError, ValueError) as ex:
                raise ToolError(str(ex).strip("'\"")) from None
        return mcp.tool(annotations=annotations)(wrapped)
    return deco


def _snap(board=True, schematic=True) -> Snapshot:
    return session.snapshot(board, schematic)


def _layer_name(board, num: int) -> str:
    return board.layers.get(num, str(num))


# ---------------------------------------------------------------------------
# read


@tool(READ)
def get_context() -> dict:
    """Which Electronics documents are open in Fusion and which editor is active."""
    return session.context()


@tool(READ)
def list_designs() -> dict:
    """Electronics designs in the active Fusion project (name, folder, version)."""
    return session.bridge.call("list_designs", timeout=120)


@tool(ADDITIVE)
def open_design(name: str, folder: str | None = None) -> dict:
    """Open a design (schematic + board) from the active project and make it current."""
    return session.bridge.call("open_design", {"name": name, "folder": folder}, timeout=180)


@tool(ADDITIVE)
def open_library(name: str, folder: str | None = None) -> dict:
    """Open a Fusion library (.flbr) from the active project in the library editor."""
    return session.bridge.call("open_design", {"name": name, "folder": folder, "kind": "library"}, timeout=180)


@tool(ADDITIVE)
def new_design(name: str | None = None, folder: str | None = None) -> dict:
    """Create a new electronics design with a schematic and a board. With `name`, it is saved right
    away into the active project (optionally inside `folder`, created if missing)."""
    return session.bridge.call("new_design", {"name": name, "folder": folder}, timeout=240)


@tool(CHANGE)
def close_design(name: str, discard_changes: bool = False) -> dict:
    """Close a design. Refuses if it has unsaved changes unless discard_changes is true."""
    return session.bridge.call("close_design", {"name": name, "discard_changes": discard_changes}, timeout=120)


@tool(READ)
def get_board_summary() -> dict:
    """Board size, copper layers, part and net counts, net classes and design-rule highlights."""
    s = _snap(schematic=False)
    fab, b = s.fab(), s.board()
    return {
        "size_mm": fab.size_mm, "outline_bbox_mm": fab.outline,
        "copper_layers": [{"number": n, "name": _layer_name(b, n)} for n in b.copper_layers],
        "parts": len(fab.elements), "nets": len(b.signals),
        "routed_nets": sum(1 for x in b.signals.values() if x.wires),
        "vias": sum(len(x.vias) for x in b.signals.values()),
        "net_classes": [{"number": c.number, "name": c.name, "width_mm": c.width, "drill_mm": c.drill}
                        for c in b.classes.values()],
        "rules": {k: b.rules.get(k) for k in ("mdWireWire", "msWidth", "msDrill", "rvViaOuter", "layerSetup")},
    }


@tool(READ)
def list_parts(filter: str = "", limit: int = 500) -> list[dict]:
    """Parts on the board with placement and JLC attributes. `filter` matches refdes, value or package."""
    fab = _snap(schematic=False).fab()
    f = filter.lower()
    rows = []
    for e in sorted(fab.elements, key=lambda e: bompnp._natural_key(e.name)):
        if f and f not in f"{e.name} {e.value} {e.package}".lower():
            continue
        rows.append({"ref": e.name, "value": e.value, "package": e.package, "x_mm": e.x, "y_mm": e.y,
                     "angle": e.angle, "side": "bottom" if e.mirror else "top",
                     "jlcpcb": e.attr("JLCPCB") or None, "mpn": e.attr("MP") or None})
    return rows[:limit]


@tool(READ)
def get_part(ref: str) -> dict:
    """Everything about one part: board placement, schematic device, attributes, and each pin's net."""
    s = _snap()
    fab = s.fab()
    el = next((e for e in fab.elements if e.name == ref), None)
    out: dict[str, Any] = {"ref": ref}
    if el:
        out["board"] = {"x_mm": el.x, "y_mm": el.y, "angle": el.angle, "side": "bottom" if el.mirror else "top",
                        "package": el.package, "library": el.library, "attributes": el.attrs}
    if s.sch_xml:
        sch = s.schematic()
        p = sch.parts.get(ref)
        if p:
            out["schematic"] = {"deviceset": p.deviceset, "device": p.device, "library": p.library,
                                "value": p.value, "user_value": p.user_value, "attributes": p.attributes,
                                "pins": [{"pin": x.pin, "pad": x.pad, "direction": x.direction,
                                          "net": sch.net_of(x.part, x.pin), "sheet": x.sheet,
                                          "x_mm": x.x, "y_mm": x.y} for x in sch.pins if x.part == ref]}
    if len(out) == 1:
        raise KeyError(f"no part {ref!r} on the board or schematic")
    return out


@tool(READ)
def list_nets(filter: str = "", limit: int = 1000) -> list[dict]:
    """Nets (merged across schematic sheets) with pin counts and routed length on the board."""
    s = _snap()
    b = s.board()
    sch = s.schematic() if s.sch_xml else None
    names = set(b.signals) | (set(sch.nets) if sch else set())
    rows = []
    for name in sorted(names):
        if filter and filter.lower() not in name.lower():
            continue
        sig = b.signals.get(name)
        rows.append({"name": name, "class": sig.net_class if sig else (sch.nets[name].net_class if sch else None),
                     "schematic_pins": len(sch.nets[name].pins) if sch and name in sch.nets else 0,
                     "board_contacts": len(sig.contacts) if sig else 0,
                     "routed_mm": round(sig.length, 3) if sig else 0.0, "vias": len(sig.vias) if sig else 0})
    return rows[:limit]


@tool(READ)
def get_net(name: str) -> dict:
    """One net: its schematic pins (with direction) and its board routing by layer."""
    s = _snap()
    b = s.board()
    sch = s.schematic() if s.sch_xml else None
    out: dict[str, Any] = {"name": name}
    if sch and name in sch.nets:
        n = sch.nets[name]
        out["schematic"] = {"class": n.net_class, "sheets": sorted(set(n.sheets)),
                            "pins": [{"part": r.part, "pin": r.pin,
                                      "direction": (sch.pin(r.part, r.pin).direction if sch.pin(r.part, r.pin) else None)}
                                     for r in n.pins]}
    if name in b.signals:
        g = b.signals[name]
        out["board"] = {"class": g.net_class, "contacts": [f"{e}.{p}" for e, p in g.contacts],
                        "length_mm": round(g.length, 3),
                        "length_by_layer_mm": {_layer_name(b, k): round(v, 3) for k, v in g.length_by_layer().items()},
                        "widths_mm": sorted({round(w.width, 4) for w in g.wires}),
                        "vias": [{"x_mm": v.x, "y_mm": v.y, "drill_mm": v.drill, "extent": v.extent} for v in g.vias],
                        "polygons": g.polygons}
    if len(out) == 1:
        raise KeyError(f"no net {name!r}")
    return out


@tool(READ)
def get_layer_stack(stackup_file: str | None = None) -> dict:
    """The layer stackup: copper and dielectric thicknesses, dielectric constants (Er) and materials.
    Read from the open design's Fusion design rules, or from `stackup_file` (.estackup or .edru)."""
    st, note = _stackup(stackup_file)
    base = _snap(schematic=False).board().stackup()
    if st is None:
        base["note"] = note
        return base
    out = st.as_dict()
    out["layer_setup"] = base.get("layer_setup")
    return out


def _stackup(stackup_file: str | None = None):
    """(Stackup or None, note). Source order: explicit file, then the open design."""
    from fusion_offline import stackup as ST
    if stackup_file:
        with open(stackup_file, "rb") as f:
            st = ST.parse_stackup(f.read(), source=f"file {stackup_file}")
        return st, ("" if st else f"no <layerstackup> in {stackup_file}")
    try:
        dr = session.bridge.call("design_rules", timeout=60)
    except (BridgeOpError, BridgeUnavailable) as ex:
        return None, f"design stackup unavailable ({ex}); pass stackup_file (.estackup or .edru)"
    st = ST.parse_stackup(dr["xml"], source=f"open design (Fusion working copy, {dr['modified']})")
    return st, ("" if st else "the design has no layer stackup; pass stackup_file")


def _design_rules_xml(stackup_file: str | None):
    if stackup_file and stackup_file.lower().endswith(".edru"):
        with open(stackup_file, "rb") as f:
            return f.read()
    try:
        return session.bridge.call("design_rules", timeout=60)["xml"]
    except (BridgeOpError, BridgeUnavailable):
        return None


@tool(READ)
def run_drc() -> dict:
    """Run Fusion's DRC on the board and return the violations."""
    session.run("DRC;", "board")
    return session.errors("board")


@tool(READ)
def run_erc() -> dict:
    """Run Fusion's ERC on the schematic and return the findings."""
    session.run("ERC;", "schematic")
    return session.errors("schematic")


@tool(READ)
def review_schematic(include_erc: bool = True) -> dict:
    """Schematic review: offline rules (unconnected power pins, single-pin nets, output conflicts,
    undriven nets, missing JLC codes, empty values) plus Fusion's ERC."""
    s = _snap(board=False)
    findings = review_rules.review(s.schematic())
    out: dict[str, Any] = {"findings": findings,
                           "counts": {k: sum(1 for f in findings if f["severity"] == k) for k in ("error", "warning", "info")}}
    if include_erc:
        out["erc"] = run_erc()
    return out


# ---------------------------------------------------------------------------
# signal integrity


@tool(READ)
def list_diff_pairs(max_skew_mm: float | None = None) -> list[dict]:
    """Differential pairs (by _P/_N style names) with per-side length, skew, width, gap and vias.
    The skew limit defaults to the design's dpMaxLengthDifference rule."""
    b = _snap(schematic=False).board()
    return [si.pair_report(b, p, n, max_skew_mm) for p, n in si.find_diff_pairs(b)]


@tool(READ)
def check_length_match(nets: list[str], tolerance_mm: float) -> dict:
    """Compare routed lengths of a group of nets (e.g. a bus or lanes) against the longest."""
    b = _snap(schematic=False).board()
    missing = [n for n in nets if n not in b.signals]
    if missing:
        raise KeyError(f"not on the board: {', '.join(missing)}")
    lens = {n: round(b.signals[n].length, 3) for n in nets}
    ref = max(lens.values())
    rows = [{"net": n, "length_mm": L, "short_by_mm": round(ref - L, 3), "ok": ref - L <= tolerance_mm}
            for n, L in lens.items()]
    return {"reference_mm": ref, "tolerance_mm": tolerance_mm, "all_ok": all(r["ok"] for r in rows), "nets": rows}


@tool(READ)
def check_impedance(target_diff_ohm: float = 100.0, tolerance_pct: float = 10.0,
                    stackup_file: str | None = None) -> dict:
    """Estimate every differential pair's impedance on its routed layer using the real stackup, and
    compare with the target. Also flags pairs whose P-N gap is tighter than their net class's
    copper clearance rule (a common cause of mass DRC errors). Estimates are IPC-2141 closed form,
    typically within ~10% of a field solver; use a solver or the fab's calculator for sign-off."""
    from fusion_offline import stackup as ST
    st, note = _stackup(stackup_file)
    if st is None:
        raise ValueError(note)
    b = _snap(schematic=False).board()
    rules_xml = _design_rules_xml(stackup_file)
    rules, _ = ST.parse_clearance_rules(rules_xml) if rules_xml else ([], {})
    num_of = {c.name: str(c.number) for c in b.classes.values()}
    rows = []
    for p, n in si.find_diff_pairs(b):
        r = si.pair_impedance(b, st, p, n)
        if r.get("zdiff_ohm"):
            r["deviation_pct"] = round((r["zdiff_ohm"] - target_diff_ohm) / target_diff_ohm * 100, 1)
            r["ok"] = abs(r["deviation_pct"]) <= tolerance_pct
        cls = b.signals[p].net_class
        conflicts = [x for x in ST.class_clearance(rules, num_of.get(cls, "")) if not x.same_signal]
        gap = r.get("gap_mm")
        if gap and conflicts:
            worst = max(conflicts, key=lambda x: x.value_mm)
            if worst.value_mm > gap + 1e-4:
                r["clearance_conflict"] = (f"class {cls} rule '{worst.name}' requires {worst.value_mm} mm to other "
                                           f"signals, but the pair gap is {gap} mm, so DRC flags P against N")
        rows.append(r)
    return {"stackup": st.source, "target_diff_ohm": target_diff_ohm, "tolerance_pct": tolerance_pct,
            "pairs": rows, "out_of_tolerance": [f"{r['p']}/{r['n']}" for r in rows if r.get("ok") is False],
            "clearance_conflicts": [f"{r['p']}/{r['n']}" for r in rows if r.get("clearance_conflict")]}


@tool(CALC)
def estimate_impedance(width_mm: float, dielectric_mm: float, er: float, copper_mm: float = 0.035,
                       geometry: str = "microstrip", gap_mm: float | None = None) -> dict:
    """Closed-form (IPC-2141) impedance estimate for a hypothetical trace. geometry: microstrip
    (dielectric_mm = height to the reference plane) or stripline (plane-to-plane). get_layer_stack
    gives the real thicknesses and Er; check_impedance does this for every routed pair."""
    return si.estimate_impedance(width_mm, dielectric_mm, copper_mm, er, geometry, gap_mm)


# ---------------------------------------------------------------------------
# JLC exports


def _jlc_orientation(snap, refs=None, fetch: bool = False) -> dict:
    """ref -> {'code', 'derived' | 'error'} from JLC's (EasyEDA) footprint for each
    placed part's JLCPCB code. Cache only unless fetch is true."""
    from fusion_offline import jlc_orient as J
    from . import easyeda as E
    root = D.read_xml(snap.board_xml)
    out = {}
    for el in snap.fab().elements:
        if refs and el.name not in refs:
            continue
        code = (el.attrs.get("JLCPCB") or el.attrs.get("LCSC") or "").strip()
        if not code:
            out[el.name] = {"code": None, "error": "no JLCPCB attribute"}
            continue
        try:
            result = E.fetch(code) if fetch else E.cached(code)
        except (E.RateLimited, ValueError, OSError) as ex:
            out[el.name] = {"code": code, "error": str(ex)}
            continue
        if result is None:
            out[el.name] = {"code": code, "error": "not in the EasyEDA cache (check_jlc_orientation with fetch=true)"}
            continue
        pp = J.package_pads(root, el.name)
        d = J.derive(pp[0] if pp else [], J.easyeda_pads(result))
        out[el.name] = {"code": code, "derived": d.as_dict() if d else None,
                        **({} if d else {"error": "pads could not be matched"})}
    return out


def _jlc(apply_orientation: bool = False):
    snap = _snap(schematic=False)
    overrides, review = None, {}
    from_library = {e.name: {k: e.attrs.get(k) for k in ("JLC-ROTATION", "JLC-X-OFFSET", "JLC-Y-OFFSET")}
                    for e in snap.fab().elements
                    if any(e.attrs.get(k) for k in ("JLC-ROTATION", "JLC-X-OFFSET", "JLC-Y-OFFSET"))}
    if apply_orientation:
        overrides = {}
        for ref, r in _jlc_orientation(snap).items():
            d = r.get("derived")
            if not d or ref in from_library:      # the library's attributes win (bompnp)
                continue
            if d["trustworthy"]:
                if d["needs_correction"]:
                    overrides[ref] = (d["rotation"], d["dx_mm"], d["dy_mm"])
            else:
                review[ref] = d
    res = bompnp.generate(snap.fab(), overrides=overrides)
    res.orientation = {"applied": overrides or {}, "needs_review": review, "from_library": from_library}
    return res


@tool(READ)
def export_bom() -> dict:
    """JLCPCB-format BOM CSV (plus the excluded-parts CSV), generated from the open board."""
    res = _jlc()
    return {"bom_csv": bompnp.bom_csv(res), "excluded_csv": bompnp.bom_excluded_csv(res),
            "lines": len(res.bom), "excluded": len(res.bom_excluded)}


@tool(READ)
def check_gerbers(path: str) -> dict:
    """Check a CAM output (zip or folder of gerbers + Excellon drills) against the open design:
    complete layer set (copper count = stackup, mask, silk incl. bottom when the board has bottom
    text, paste where there are SMD pads, outline, drill), outline size, every drilled hole of the
    design (pads, vias, holes) present with the right diameter and position and nothing extra, and
    pad flash counts per layer. Run it on the zip before uploading to the fab."""
    from fusion_offline import gerbers as G
    snap = _snap(schematic=False)
    return G.check(path, D.read_xml(snap.board_xml), copper_layers=len(snap.board().copper_layers))


@tool(READ)
def export_cpl(jlc_orientation: bool = True) -> dict:
    """JLCPCB-format pick-and-place (CPL) CSV. JLC places each part with its own (EasyEDA)
    footprint, whose zero orientation and origin can differ from ours (KiCad-sourced connectors and
    ICs typically). With jlc_orientation, parts whose footprint data is in the local EasyEDA cache get
    the derived rotation/offset applied (library JLC-ROTATION / JLC-X-OFFSET / JLC-Y-OFFSET attributes
    still win); ambiguous derivations are listed for review instead. Never uses the network: fill the
    cache with check_jlc_orientation(fetch=true)."""
    res = _jlc(apply_orientation=jlc_orientation)
    return {"cpl_csv": bompnp.pnp_csv(res), "excluded_csv": bompnp.pnp_excluded_csv(res),
            "orientation_applied": {k: {"rotation": v[0], "dx_mm": v[1], "dy_mm": v[2]}
                                    for k, v in res.orientation["applied"].items()},
            "orientation_needs_review": res.orientation["needs_review"],
            "orientation_from_library": res.orientation["from_library"]}


@tool(READ)
def check_jlc_orientation(refs: list[str] | None = None, fetch: bool = False) -> dict:
    """Compare each placed part's footprint with the one JLC places it with (EasyEDA's, by the
    part's JLCPCB code) and derive the CPL rotation/offset that lines them up: pads matched by name
    (geometry when names differ, flagged ambiguous so a person confirms polarity). fetch=true
    downloads missing footprints from easyeda.com (the only internet access this server makes:
    cached forever, >= 15 s between requests); otherwise only the local cache is used."""
    snap = _snap(schematic=False)
    res = _jlc_orientation(snap, refs, fetch)
    fix = {k: v["derived"] for k, v in res.items() if v.get("derived") and v["derived"]["needs_correction"]}
    return {"parts": res, "corrections": {k: v for k, v in fix.items() if v["trustworthy"]},
            "needs_review": {k: v for k, v in res.items() if v.get("derived") and not v["derived"]["trustworthy"]},
            "missing": {k: v["error"] for k, v in res.items() if v.get("error")}}


# ---------------------------------------------------------------------------
# schematic writes


# Fusion's confirmations when a net wire or name joins two nets (seen on 2705.1.15):
#   "Merge net segment 'SPIKE_A' into given net 'JOIN_B'?" [Yes/No]   (NET wire)
#   "Connect MCP_R2 and MCP_RENAMED?" [Yes/No]                          (NAME onto an existing net)
MERGE_PROMPT = r"^(Merge net segment .+ into given net .+|Connect \S+ and \S+)\?$"


def _pin_map(sch) -> dict[tuple[str, str], str | None]:
    return {(p.part, p.pin): sch.net_of(p.part, p.pin) for p in sch.pins}


def _resolve_pin(sch, spec: str):
    part, _, pin = spec.partition(".")
    cand = [p for p in sch.pins if p.part == part and (p.pin == pin or p.pad == pin)]
    if not cand:
        raise KeyError(f"no pin {spec!r} (use PART.PIN or PART.PAD)")
    if len(cand) > 1:
        cand = [p for p in cand if p.pin == pin] or cand
    return cand[0]


@tool(ADDITIVE)
def add_part(device: str, library_name: str, ref: str, x_mm: float, y_mm: float,
             angle: float = 0, sheet: int = 1) -> dict:
    """Place a device (device set + variant name, e.g. 'RES_0603_10K_1%_1/10W') from a Fusion
    library into the schematic. The part is forward-annotated onto the board. `sheet` may be an
    existing sheet or the next new one (sheets + 1)."""
    sch = _snap(board=False).schematic()
    if ref in sch.parts:
        raise ValueError(f"{ref} already exists; pick an unused reference designator")
    if not 1 <= sheet <= sch.sheets + 1:
        raise ValueError(f"sheet {sheet} does not exist (the schematic has {sch.sheets}; use 1..{sch.sheets + 1})")
    cmd = C.add_part(device, library_name, ref, x_mm, y_mm, angle, sheet)

    def verify(before, after):
        sch = after.schematic()
        p = sch.parts.get(ref)
        if p is None:
            return False, f"{ref} did not appear (check the device and library names; the library must be saved and closed)"
        on_board = after.board_xml is None or any(e.name == ref for e in after.fab().elements)
        return on_board, f"{ref} placed as {p.deviceset}{p.device}" + ("" if on_board else " but not on the board")

    after, detail = session.verified_write("schematic", cmd, verify)
    return {"ok": True, "detail": detail, "part": get_part(ref)}


@tool(ADDITIVE)
def connect_pins(net: str, pins: list[str], allow_merge: bool = False, labels: bool = True) -> dict:
    """Connect schematic pins into a named net. Pins are PART.PIN or PART.PAD. Each pin gets a short
    named wire stub with a net label (labels=false to omit), so no wire crosses other parts and
    every piece of the net is visibly named. Refuses to join two existing nets unless allow_merge
    is true."""
    snap = _snap(board=False)
    sch = snap.schematic()
    targets = [_resolve_pin(sch, p) for p in pins]
    for t in targets:
        cur = sch.net_of(t.part, t.pin)
        if cur and cur != net and not allow_merge:
            raise ValueError(f"{t.part}.{t.pin} is already on net {cur!r}; pass allow_merge=true to join it to {net!r}")
    cmd = " ".join(C.net_stub(net, t.x, t.y, t.outward, sheet=t.sheet, label=labels)
                   for t in targets if sch.net_of(t.part, t.pin) != net)
    if not cmd:
        return {"ok": True, "detail": "already connected", "net": net}
    before_map = _pin_map(sch)
    wanted = {(t.part, t.pin) for t in targets}

    def verify(before, after):
        m = _pin_map(after.schematic())
        missing = [f"{a}.{b}" for a, b in wanted if m.get((a, b)) != net]
        changed = [f"{k[0]}.{k[1]}: {before_map.get(k)} -> {v}" for k, v in m.items()
                   if k not in wanted and before_map.get(k) != v and not (allow_merge and v == net)]
        if missing:
            return False, "not connected: " + ", ".join(missing)
        if changed:
            return False, "other pins changed net: " + "; ".join(changed[:10])
        return True, f"{len(wanted)} pins on {net}"

    answers = [(MERGE_PROMPT, "Yes")] if allow_merge else None
    after, detail = session.verified_write("schematic", cmd, verify, answers=answers)
    return {"ok": True, "detail": detail, "net": get_net(net)}


@tool(CHANGE)
def rename_net(old_name: str, new_name: str, allow_merge: bool = False,
               only_segment_with_pin: str | None = None) -> dict:
    """Rename a schematic net (all sheets where it has wires). Renaming onto a name that already
    exists merges the two nets, which is refused unless allow_merge is true.
    only_segment_with_pin: 'REF.PIN' renames just the wire segment on that pin (e.g. a labelled stub),
    moving that pin to new_name and leaving the rest of the net as it is; how to swap two pins'
    nets: rename one stub to a temporary name, the other stub across, then the temporary one."""
    sch = _snap(board=False).schematic()
    n = sch.nets.get(old_name)
    if n is None:
        raise KeyError(f"no net {old_name!r}")
    if new_name in sch.nets and not allow_merge:
        raise ValueError(f"net {new_name!r} already exists; renaming would merge it with {old_name!r}. "
                         "Pass allow_merge=true if that is intended")
    if only_segment_with_pin:
        ref, _, pin = only_segment_with_pin.partition(".")
        seg = next((sg for sg in n.segments if (ref, pin) in sg.pins), None)
        if seg is None or seg.first_wire is None:
            raise ValueError(f"{only_segment_with_pin} is not on a wired segment of {old_name!r}")
        x1, y1, x2, y2 = seg.first_wire
        cmd = [C.rename_net_at(new_name, (x1 + x2) / 2, (y1 + y2) / 2, seg.sheet)]
        others = [p for sg in n.segments if sg is not seg for p in sg.pins]

        def verify(before, after):
            nets = after.schematic().nets
            moved = new_name in nets and any((p.part, p.pin) == (ref, pin) for p in nets[new_name].pins)
            kept = not others or (old_name in nets and all(
                any((q.part, q.pin) == p for q in nets[old_name].pins) for p in others))
            return moved and kept, (f"{only_segment_with_pin} moved {old_name} -> {new_name}" if moved and kept
                                    else f"{only_segment_with_pin} on {new_name}: {moved}; rest of {old_name} kept: {kept}")
        form = dialogs.FORM_RENAME_THIS_SEGMENT
    else:
        if not n.wire_points:
            raise ValueError(f"net {old_name!r} has no wires to target")
        seen, cmd = set(), []
        for sheet, x, y in n.wire_points:
            if sheet not in seen:
                seen.add(sheet)
                cmd.append(C.rename_net_at(new_name, x, y, sheet))

        def verify(before, after):
            nets = after.schematic().nets
            ok = new_name in nets and old_name not in nets
            return ok, f"renamed {old_name} -> {new_name}" if ok else f"nets now: {old_name in nets=} {new_name in nets=}"
        # NAME on a net with several segments opens a "Name" form; answer it with
        # "every Segment on this Sheet" (one NAME per sheet covers the whole net).
        form = dialogs.FORM_RENAME_ALL_SEGMENTS

    after, detail = session.verified_write("schematic", " ".join(cmd), verify, board=False,
                                           answers=[(MERGE_PROMPT, "Yes")] if allow_merge else None,
                                           forms=[form])
    return {"ok": True, "detail": detail}


@tool(CHANGE)
def set_part_variant(ref: str, device_variant: str) -> dict:
    """Switch a part to another device variant of its device set (how variant-based passive libraries change value;
    the variant's JLC code and attributes follow). Example variant: '_680R_1%_1/10W'."""
    sch = _snap(board=False).schematic()
    inst = sch.instance(ref)
    if inst is None:
        raise KeyError(f"no placed instance of {ref!r}")
    cmd = C.set_variant_at(device_variant, inst.x, inst.y, inst.sheet)

    def verify(before, after):
        p = after.schematic().parts.get(ref)
        ok = p is not None and p.device == device_variant
        return ok, (f"{ref} is now {p.deviceset}{p.device} ({p.value}, JLCPCB {p.attributes.get('JLCPCB')})"
                    if ok else f"{ref} is still {p.device if p else '?'} (is {device_variant!r} a variant of its device set?)")

    after, detail = session.verified_write("schematic", cmd, verify)
    return {"ok": True, "detail": detail}


@tool(CHANGE)
def set_part_value(ref: str, value: str) -> dict:
    """Set a part's value. Only for device sets with user-definable values; for fixed-value library
    parts (e.g. passives with one variant per value) use set_part_variant instead."""
    sch = _snap(board=False).schematic()
    cmd = C.set_value(sch, ref, value)

    def verify(before, after):
        p = after.schematic().parts.get(ref)
        return (p is not None and p.value == value), f"{ref} value is {p.value if p else '?'}"

    after, detail = session.verified_write("schematic", cmd, verify)
    return {"ok": True, "detail": detail}


@tool(ADDITIVE)
def label_nets(nets: list[str] | None = None) -> dict:
    """Add a net label to every piece of a net that is drawn separately from the rest without a
    label (the convention: same net, not visibly connected, must be named). Limit with `nets`."""
    sch = _snap(board=False).schematic()
    todo = review_rules.unlabeled_pieces(sch, nets)
    if not todo:
        return {"ok": True, "detail": "every separately drawn net piece is already labelled", "labelled": {}}
    cmds = []
    for name, segs in todo.items():
        for g in segs:
            x1, y1, x2, y2 = g.first_wire
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            cmds.append(f"EDIT .s{g.sheet}; GRID MM; LABEL {C.pt(mx, my)} {C.pt(x2, y2)};")
    want = {k: len(v) for k, v in todo.items()}

    def verify(before, after):
        left = review_rules.unlabeled_pieces(after.schematic(), list(want))
        return (not left), ("labelled " + ", ".join(f"{k} x{v}" for k, v in want.items())
                            if not left else f"still unlabelled: {sorted(left)}")

    after, detail = session.verified_write("schematic", " ".join(cmds), verify, board=False)
    return {"ok": True, "detail": detail, "labelled": want}


def _frame_setting() -> str | None:
    """Sheet frame for new_sheet: env FUSION_MCP_SHEET_FRAME as
    'DEVICE@LIBRARY' (e.g. 'FRAME_B_L@MY_FRAMES'), empty for none."""
    return (os.environ.get("FUSION_MCP_SHEET_FRAME") or "").strip() or None


@tool(ADDITIVE)
def new_sheet(title: str = "", frame: str | None = None, sheet: int | None = None) -> dict:
    """Add a schematic sheet with your standard frame and set its headline (sheet description).
    `frame` is 'DEVICE@LIBRARY'; default from FUSION_MCP_SHEET_FRAME, or none. Fusion does not add a
    frame to new sheets by itself. Pass `sheet` to set up an EXISTING sheet instead (e.g. sheet 1
    of a new design). Returns the sheet number and the frame's drawing area."""
    snap = _snap(board=False)
    sch = snap.schematic()
    frame = frame if frame is not None else _frame_setting()
    if sheet is not None and not 1 <= sheet <= sch.sheets:
        raise ValueError(f"sheet {sheet} does not exist (1..{sch.sheets})")
    number = sheet or sch.sheets + 1
    ref = None
    if frame:
        n = 1
        while f"FRAME{n}" in sch.parts:
            n += 1
        ref = f"FRAME{n}"
    cmd = C.new_sheet(number, title, frame, ref)

    def verify(before, after):
        s2 = after.schematic()
        if s2.sheets != max(number, sch.sheets):
            return False, f"sheet count is {s2.sheets}, expected {max(number, sch.sheets)}"
        if frame and ref not in s2.parts:
            return False, f"frame {frame} was not placed (is the library available in this design?)"
        if title and s2.descriptions[number - 1] != title:
            return False, f"headline is {s2.descriptions[number - 1]!r}"
        return True, f"sheet {number} {'set up' if sheet else 'created'}" + (f" with {frame}" if frame else "")

    after, detail = session.verified_write("schematic", cmd, verify, board=False)
    area = None
    if frame:
        root = D.read_xml(after.sch_xml)
        fr = next((f for f in root.iter("frame")), None)   # first frame symbol in the libraries
        if fr is not None:
            area = {"x1": float(fr.get("x1")), "y1": float(fr.get("y1")),
                    "x2": float(fr.get("x2")), "y2": float(fr.get("y2"))}
    return {"ok": True, "detail": detail, "sheet": number, "frame_ref": ref, "frame_area_mm": area}


# ---------------------------------------------------------------------------
# board writes


class _IgnoreViolators:
    """Run a placement edit in Fusion's 'Ignore Violators' mode and restore the
    user's mode after. In the default 'push' mode Fusion shoves a moved part
    away from others, even on the other side of the board."""
    def __enter__(self):
        self.prev = None
        try:
            self.prev = session.bridge.call("violation_mode", {"set": "ignore"})["before"]
        except (BridgeOpError, BridgeUnavailable):
            pass
        return self

    def __exit__(self, *exc):
        if self.prev and self.prev != "ignore":
            with contextlib.suppress(BridgeOpError, BridgeUnavailable):
                session.bridge.call("violation_mode", {"set": self.prev})
        return False


def _element(snap, ref):
    return next((e for e in snap.fab().elements if e.name == ref), None)


@tool(CHANGE)
def move_part(ref: str, x_mm: float, y_mm: float) -> dict:
    """Move a board part so its origin is at (x, y) mm."""
    def verify(before, after):
        e = _element(after, ref)
        if e is None:
            return False, f"no part {ref} on the board"
        ok = math.isclose(e.x, x_mm, abs_tol=1e-3) and math.isclose(e.y, y_mm, abs_tol=1e-3)
        if ok:
            return True, f"{ref} at ({e.x}, {e.y})"
        near = sorted((math.hypot(o.x - x_mm, o.y - y_mm), o) for o in after.fab().elements if o.name != ref)
        near = [f"{o.name} ({'bottom' if o.mirror else 'top'}) at ({o.x}, {o.y})" for d, o in near if d < 4.0]
        return False, (f"{ref} landed at ({e.x}, {e.y}) instead of ({x_mm}, {y_mm}); Fusion moved it aside, "
                       "probably avoiding an overlap" + (f" with {', '.join(near)}" if near else ""))
    with _IgnoreViolators():
        after, detail = session.verified_write("board", C.move_part(ref, x_mm, y_mm), verify, schematic=False)
    return {"ok": True, "detail": detail}


@tool(CHANGE)
def rotate_part(ref: str, angle: float, bottom: bool = False) -> dict:
    """Set a board part's absolute rotation (degrees) and side (bottom=true places it on the bottom)."""
    def verify(before, after):
        e = _element(after, ref)
        if e is None:
            return False, f"no part {ref} on the board"
        ok = math.isclose(e.angle % 360, angle % 360, abs_tol=0.05) and e.mirror == bottom
        return ok, f"{ref} angle {e.angle} {'bottom' if e.mirror else 'top'}"
    with _IgnoreViolators():
        after, detail = session.verified_write("board", C.rotate_part(ref, angle, bottom), verify, schematic=False)
    return {"ok": True, "detail": detail}


@tool(ADDITIVE)
def add_via(net: str, x_mm: float, y_mm: float, drill_mm: float = 0.3) -> dict:
    """Add a through via on a net at (x, y) mm. The pad diameter follows the design's restring rules."""
    def verify(before, after):
        b0, b1 = before.board(), after.board()
        n0 = len(b0.signals[net].vias) if net in b0.signals else 0
        s = b1.signals.get(net)
        hit = s and any(math.isclose(v.x, x_mm, abs_tol=1e-3) and math.isclose(v.y, y_mm, abs_tol=1e-3) for v in s.vias)
        return bool(hit and len(s.vias) == n0 + 1), f"{net} vias {n0} -> {len(s.vias) if s else 0}"
    after, detail = session.verified_write("board", C.add_via(net, x_mm, y_mm, drill_mm), verify, schematic=False)
    return {"ok": True, "detail": detail}


@tool(ADDITIVE)
def add_trace(net: str, layer: str | int, width_mm: float, points_mm: list[list[float]]) -> dict:
    """Route a trace through the given points [[x, y], ...] (mm). layer: 'top', 'bottom', 'inner1'..
    or a layer number."""
    pts = [(float(p[0]), float(p[1])) for p in points_mm]
    wl = _write_layer(layer)

    def verify(before, after):
        missing, lay = _uncovered(after, net, wl, pts)
        return (not missing), (f"{net} routed on layer {lay}" if not missing
                               else f"{net}: path not covered near {missing[:3]}")
    after, detail = session.verified_write("board", C.add_trace(net, wl, width_mm, pts), verify, schematic=False)
    return {"ok": True, "detail": detail}


def _uncovered(snap, net, wl, pts):
    """Points along `pts` not covered by `net` copper on write layer wl.
    Fusion merges a wire that continues an existing one, so segment counts
    are meaningless; the path must be covered by same-net copper."""
    b1 = snap.board()
    s1 = b1.signals.get(net)
    lay = _board_layer(b1, {1: 1}.get(wl, 16 if wl == _write_layer("bottom") else wl))
    wires = [w for w in (s1.wires if s1 else []) if w.layer == lay]
    from fusion_offline.pairs import flatten
    missing = []
    for px, py in flatten(pts, 0.1):
        if not any(si._seg_point_dist(px, py, w) <= max(w.width / 2, 1e-3) + 1e-3 for w in wires):
            missing.append((round(px, 3), round(py, 3)))
            if len(missing) > 2:
                break
    return missing, lay


@tool(ADDITIVE)
def route_pair(p_net: str, n_net: str, centreline_mm: list[list[float]], width_mm: float, gap_mm: float,
               layer: str = "top", p_tail_mm: list | None = None, n_tail_mm: list | None = None,
               p_head_mm: list | None = None, n_head_mm: list | None = None,
               max_skew_mm: float = 0.1, tune: bool = True, via_drill_mm: float = 0.3,
               via_diameter_mm: float = 0.6, chamfer_mm: float = 0.5, dry_run: bool = False) -> dict:
    """Route a differential pair as two coupled traces along a centreline you choose.

    centreline_mm: [[x, y], ...] for the middle of the pair, from near the start pads to near the
    end pads; use 45-degree bends. Each trace is the centreline offset by (width + gap) / 2 with
    mitred corners, so the gap holds through bends, plus a short 45-degree fan-in to its pad.
    Which side is P is set by the start pads. If the end pads are the other way round the result
    says crossed=true: either approach the end pads from the other direction (no via), or pass a
    tail for one trace, [[x, y], {"via": [x, y]}, [x, y]], which changes layer at the via.
    p_head_mm / n_head_mm: explicit path from a trace's start pad to the trunk, for pins the
    automatic 45-degree fan-in cannot reach cleanly (e.g. through a gap in a pin row).
    Every 90-degree corner (typically where a trace leaves a pin) becomes two 45-degree bends,
    chamfer_mm along each leg (0 keeps hard corners).
    The shorter trace gets rounded bumps until the skew is within max_skew_mm.
    Nothing is written if the plan has conflicts (copper of other nets, holes, keepouts, the
    partner trace) or dry_run=true; the plan is returned either way."""
    from fusion_offline import pairs as PR
    snap = _snap(schematic=False)
    root = D.read_xml(snap.board_xml)
    top, bottom = 1, 16
    start, other = (bottom, top) if str(layer).lower() == "bottom" else (top, bottom)
    plan = PR.plan_pair(root, p_net, n_net, centreline_mm, width_mm, gap_mm, layer=start, other_layer=other,
                        p_tail=p_tail_mm, n_tail=n_tail_mm, p_head=p_head_mm, n_head=n_head_mm, via_drill=via_drill_mm, via_diameter=via_diameter_mm,
                        max_skew_mm=max_skew_mm, tune=tune, chamfer_mm=chamfer_mm)
    if dry_run or not plan["ok"]:
        return {"written": False, "plan": plan}
    wmap = {top: _write_layer("top"), bottom: _write_layer("bottom")}
    cmds, checks = [], []
    for side in ("p", "n"):
        net = plan[side]["net"]
        for t in plan[side]["traces"]:
            pts = [tuple(q) for q in t["points"]]
            cmds.append(C.add_trace(net, wmap[t["layer"]], width_mm, pts))
            checks.append((net, wmap[t["layer"]], pts))
        for vx, vy in plan[side]["vias"]:
            cmds.append(C.add_via(net, vx, vy, via_drill_mm, via_diameter_mm))
    vias = [(plan[s]["net"], v) for s in ("p", "n") for v in plan[s]["vias"]]

    def verify(before, after):
        bad = []
        for net, wl, pts in checks:
            missing, _ = _uncovered(after, net, wl, pts)
            if missing:
                bad.append(f"{net} not covered near {missing[:2]}")
        b = after.board()
        for net in (p_net, n_net):
            s = b.signals.get(net)
            if s and any(w.layer == 19 for w in s.wires):
                bad.append(f"{net} still has unrouted connections (a trace end missed its pad origin?)")
        for net, (vx, vy) in vias:
            s = b.signals.get(net)
            if not (s and any(math.isclose(v.x, vx, abs_tol=1e-3) and math.isclose(v.y, vy, abs_tol=1e-3) for v in s.vias)):
                bad.append(f"{net} via missing at ({vx}, {vy})")
        return (not bad), ("; ".join(bad) if bad else
                           f"{p_net}/{n_net} routed: {plan['p']['length_mm']} / {plan['n']['length_mm']} mm, "
                           f"skew {plan['skew_mm']} mm, {len(vias)} vias")
    after, detail = session.verified_write("board", " ".join(cmds), verify, schematic=False)
    return {"written": True, "detail": detail, "plan": plan}


@tool(CHANGE)
def set_board_outline(width_mm: float, height_mm: float, x0_mm: float = 0.0, y0_mm: float = 0.0,
                      replace: bool = False) -> dict:
    """Draw a rectangular board outline (Dimension layer 20). New Fusion designs come with a default
    outline; pass replace=true to remove the existing outline first."""
    snap = _snap(schematic=False)
    old = snap.fab().outline_wires
    if old and not replace:
        raise ValueError(f"the board already has an outline ({len(old)} segments, bbox {snap.fab().outline}); "
                         "pass replace=true to redraw it")
    x1, y1 = x0_mm + width_mm, y0_mm + height_mm
    pts = [(x0_mm, y0_mm), (x1, y0_mm), (x1, y1), (x0_mm, y1), (x0_mm, y0_mm)]
    dels = " ".join(f"DELETE {C.pt((a + c) / 2, (b + d) / 2)};" for a, b, c, d in old)
    cmd = f"GRID MM; {dels} LAYER 20; WIRE 0 " + " ".join(C.pt(x, y) for x, y in pts) + ";"

    def verify(before, after):
        f = after.fab()
        o = f.outline
        ok = (o is not None and len(f.outline_wires) == 4
              and all(math.isclose(a, b, abs_tol=1e-3) for a, b in zip(o, (x0_mm, y0_mm, x1, y1))))
        return ok, f"outline {o}, {len(f.outline_wires)} segments"
    after, detail = session.verified_write("board", cmd, verify, schematic=False)
    return {"ok": True, "detail": detail, "size_mm": [width_mm, height_mm]}


_SILK = {"top_silk": 21, "bottom_silk": 22, "top_doc": 51, "bottom_doc": 52}
_ALIGNS = {"center", "bottom-left", "bottom-center", "bottom-right", "center-left", "center-right",
           "top-left", "top-center", "top-right"}


@tool(ADDITIVE)
def add_text(text: str, x_mm: float, y_mm: float, layer: str | int = "top_silk", size_mm: float = 1.0,
             ratio_pct: int = 16, angle: float = 0.0, align: str = "center") -> dict:
    """Add board text, e.g. silkscreen pin labels. layer: top_silk, bottom_silk, top_doc,
    bottom_doc or a layer number. Vector font; size_mm is the character height and ratio_pct the
    stroke as % of it (JLC needs >= 1.0 mm text and >= 0.153 mm strokes: the defaults). Bottom
    layers are mirrored so they read correctly from the bottom. align: center, bottom-left, ...
    Keep text off pads: fabs clip silk over exposed copper."""
    lay = _SILK.get(layer, layer) if isinstance(layer, str) else int(layer)
    if not isinstance(lay, int):
        raise ValueError(f"unknown layer {layer!r}; use {sorted(_SILK)} or a number")
    if align not in _ALIGNS:
        raise ValueError(f"align must be one of {sorted(_ALIGNS)}")
    if "'" in text or ";" in text or not text.strip():
        raise ValueError("text must be non-empty and contain no quote or semicolon")
    mirror = "M" if lay in (22, 52) else ""
    cmd = (f"GRID MM; LAYER {lay}; CHANGE FONT VECTOR; CHANGE SIZE {C.n(size_mm)}; CHANGE RATIO {int(ratio_pct)}; "
           f"CHANGE ALIGN {align}; TEXT '{text}' {mirror}R{C.n(angle % 360)} {C.pt(x_mm, y_mm)};")

    def texts(sn):
        root = D.read_xml(sn.board_xml)
        return [t for t in root.iterfind(".//board/plain/text")]

    def verify(before, after):
        hit = [t for t in texts(after) if (t.text or "") == text and t.get("layer") == str(lay)
               and math.isclose(float(t.get("x")), x_mm, abs_tol=1e-3)
               and math.isclose(float(t.get("y")), y_mm, abs_tol=1e-3)]
        if not hit:
            return False, f"no text {text!r} on layer {lay} at ({x_mm}, {y_mm})"
        t = hit[0]
        return True, f"text {text!r} on layer {lay}, size {t.get('size')}, ratio {t.get('ratio')}, rot {t.get('rot') or 'R0'}"

    after, detail = session.verified_write("board", cmd, verify, schematic=False)
    return {"ok": True, "detail": detail}


@tool(ADDITIVE)
def add_hole(x_mm: float, y_mm: float, drill_mm: float) -> dict:
    """Add a non-plated hole on the board (e.g. a mounting hole)."""
    def holes(sn):
        root = D.read_xml(sn.board_xml)
        return [(float(h.get("x")), float(h.get("y")), float(h.get("drill"))) for h in root.iterfind(".//board/plain/hole")]

    def verify(before, after):
        hit = any(math.isclose(x, x_mm, abs_tol=1e-3) and math.isclose(y, y_mm, abs_tol=1e-3) and
                  math.isclose(d, drill_mm, abs_tol=1e-3) for x, y, d in holes(after))
        return hit and len(holes(after)) == len(holes(before)) + 1, f"{len(holes(after))} holes"
    after, detail = session.verified_write("board", f"GRID MM; HOLE {C.n(drill_mm)} {C.pt(x_mm, y_mm)};",
                                           verify, schematic=False)
    return {"ok": True, "detail": detail}


# Layers Fusion refuses in DISPLAY ('Unavailable layer: 23'); a refused layer
# aborts the rest of the DISPLAY list, so they are never named. They stay visible.
_NO_DISPLAY = {23, 24}


def _visible_layers() -> list[int]:
    return [l["number"] for l in session.bridge.call("layers", timeout=60)["layers"] if l["visible"]]


def _only_layers(layers: list[int]) -> tuple[str, str]:
    """(prefix, suffix) commands that show only `layers` for a pick-based edit
    and then restore exactly the layers the user had visible. EAGLE picks the
    nearest visible object; with pads visible a DELETE meant for a via picked
    the part instead (Fusion refused: 'Can't backannotate')."""
    vis = _visible_layers()
    _only_layers.last_view = vis
    pre = "DISPLAY NONE " + " ".join(str(l) for l in layers if l not in _NO_DISPLAY) + ";"
    post = "DISPLAY NONE " + " ".join(str(l) for l in vis if l not in _NO_DISPLAY) + ";"
    return pre, post


def _ensure_view() -> None:
    """Re-apply the view saved by the last _only_layers if it did not come back."""
    want = getattr(_only_layers, "last_view", None)
    if want is not None and _visible_layers() != want:
        session.run("DISPLAY NONE " + " ".join(str(l) for l in want if l not in _NO_DISPLAY) + ";", "board")


def _write_layer(layer) -> int:
    """Layer number to use in EAGLE commands. Fusion numbers copper by stack
    position in its own scheme (2-layer: Top 1, Bottom 304; 4-layer: 1, 2,
    303, 304), and layer 16 is an unused inner 'Route16', NOT the bottom
    (verified on 2705.1.15: bottom traces sent to 16 landed on Route16).
    Accepts 'top', 'bottom', 'inner1'.., a stack number, or the classic
    export numbering (1, 2, 15, 16 for 4-layer)."""
    from fusion_offline import stackup as ST
    stack = None
    try:
        st = ST.parse_stackup(session.bridge.call("design_rules", timeout=60)["xml"])
        stack = [c.number for c in st.copper] if st else None
    except (BridgeOpError, BridgeUnavailable):
        pass
    stack = stack or [1, 304]
    if isinstance(layer, str):
        key = layer.strip().lower()
        if key == "top":
            return stack[0]
        if key == "bottom":
            return stack[-1]
        if key.startswith("inner") and key[5:].isdigit() and 0 < int(key[5:]) < len(stack) - 1:
            return stack[int(key[5:])]
        if key.isdigit():
            layer = int(key)
        else:
            raise ValueError(f"unknown layer {layer!r}; use top, bottom, inner1.. or a layer number")
    if layer in stack:
        return layer
    classic = _snap(schematic=False).board().copper_layers
    if layer in classic and len(classic) == len(stack):
        return stack[classic.index(layer)]
    if layer == 16:
        return stack[-1]
    raise ValueError(f"layer {layer} is not a copper layer of this board (stack {stack})")


def _board_layer(b, layer: int) -> int:
    """Map a requested copper layer (1 = top, 16 = bottom, or a stack layer
    number) to the layer number used in the board export."""
    stack = b.copper_layers
    if layer in stack:
        return layer
    if layer == 16 and stack:
        return stack[-1]
    return layer


# Fusion's confirmation for ripping up all routing, and the UNROUTE panel the
# command opens (seen on 2705.1.15).
RIPUP_PROMPT = r"^All segments will be converted to unrouted signals"
FORM_UNROUTE_DONE = {"title": r"^UNROUTE$", "requires": [], "actions": [{"do": "press", "target": "Done"}],
                     "label": "closed the UNROUTE panel"}


@tool(CHANGE)
def rip_up(nets: list[str] | None = None) -> dict:
    """Remove routed traces and vias (they become unrouted connections again). Polygons/pours are
    kept. Without `nets`, rips up the whole board."""
    names = " ".join(C.q(n) for n in (nets or []))
    cmd = f"RIPUP {names};" if names else "RIPUP;"

    def verify(before, after):
        b = after.board()
        left = {n: (len(s.wires), len(s.vias)) for n, s in b.signals.items()
                if (not nets or n in nets) and (s.wires or s.vias)}
        left = {n: v for n, v in left.items() if any(w.layer != 19 for w in b.signals[n].wires) or v[1]}
        return (not left), ("ripped up" if not left else f"still routed: {dict(list(left.items())[:6])}")

    after, detail = session.verified_write("board", cmd, verify, schematic=False,
                                           answers=[(RIPUP_PROMPT, "Yes")], forms=[FORM_UNROUTE_DONE])
    return {"ok": True, "detail": detail}


def _poly_cmd(pts) -> str:
    pts = list(pts) + [pts[0]]
    return " ".join(C.pt(x, y) for x, y in pts)


@tool(ADDITIVE)
def add_pour(net: str, layer: str | int = "top", points_mm: list[list[float]] | None = None,
             inset_mm: float = 0.0, isolate_mm: float = 0.25, width_mm: float = 0.25,
             thermal_width_mm: float = 0.3) -> dict:
    """Add a copper pour (polygon) on a net, e.g. a GND plane. Without points it follows the board
    outline (inset_mm = 0): the copper-to-edge distance then comes from the design rule for board
    edge clearance, as EAGLE intends. Note the outline wire is width_mm wide and centred on the
    vertices, so an inset moves copper only inset - width/2 from the edge. isolate_mm is the
    clearance to other copper. thermal_width_mm is the width of the thermal-relief spokes joining
    pads to the pour (Fusion's default is a thin 0.1524 mm; the gap comes from the design rule
    slThermalIsolate). Respects keepouts (add_keepout); filled immediately."""
    wl = _write_layer(layer)
    if points_mm:
        pts = [(float(p[0]), float(p[1])) for p in points_mm]
    else:
        o = _snap(schematic=False).fab().outline
        if not o:
            raise ValueError("the board has no outline; draw one first or pass points_mm")
        x0, y0, x1, y1 = o
        pts = [(x0 + inset_mm, y0 + inset_mm), (x1 - inset_mm, y0 + inset_mm),
               (x1 - inset_mm, y1 - inset_mm), (x0 + inset_mm, y1 - inset_mm)]
    cmd = (f"GRID MM; CHANGE POUR SOLID; CHANGE ISOLATE {C.n(isolate_mm)}; CHANGE ORPHANS OFF; "
           f"CHANGE THERMALS ON; CHANGE THERMALWIDTH {C.n(thermal_width_mm)}; LAYER {wl}; "
           f"POLYGON {C.q(net)} {C.n(width_mm)} {_poly_cmd(pts)}; RATSNEST;")

    def count(sn):
        root = D.read_xml(sn.board_xml)
        sig = next((x for x in root.iterfind(".//signals/signal") if x.get("name") == net), None)
        return 0 if sig is None else len(sig.findall("polygon"))

    def verify(before, after):
        n0, n1 = count(before), count(after)
        return n1 == n0 + 1, f"{net} pours {n0} -> {n1} on layer {wl}"

    after, detail = session.verified_write("board", cmd, verify, schematic=False)
    return {"ok": True, "detail": detail, "outline": pts}


@tool(READ)
def list_pours() -> dict:
    """Copper pours with their live settings: net, layer, thermal relief width, isolate, rank."""
    res = session.bridge.call("pours", timeout=60)
    for p in res["pours"]:
        if p["thermals"] and p["thermal_width_mm"] < 0.2:
            p["warning"] = f"thermal spokes are only {p['thermal_width_mm']} mm (set_pour_thermals)"
    return res


@tool(CHANGE)
def set_pour_thermals(net: str, width_mm: float, layer: str | int | None = None) -> dict:
    """Set the thermal-relief spoke width of a net's existing pours (all its pours, or one layer).
    Each pour is picked on its outline with the other copper layers hidden, then the view is
    restored."""
    pours = [p for p in session.bridge.call("pours", timeout=60)["pours"] if p["net"] == net]
    if layer is not None:
        wl = _write_layer(layer)
        pours = [p for p in pours if p["layer"] == wl]
    if not pours:
        raise KeyError(f"no pours on {net}" + (f" layer {layer}" if layer is not None else ""))
    copper = sorted({p["layer"] for p in session.bridge.call("pours", timeout=60)["pours"]} | {1, _write_layer("bottom")})
    cmds = []
    for p in pours:
        if len(p["outline"]) < 2:
            raise ValueError(f"cannot read the outline of the {net} pour on layer {p['layer']}")
        (xa, ya), (xb, yb) = p["outline"][0], p["outline"][1]
        pre, post = _only_layers([p["layer"]])
        cmds.append(f"{pre} CHANGE THERMALWIDTH {C.n(width_mm)} {C.pt((xa + xb) / 2, (ya + yb) / 2)}; {post}")
    try:
        session.run(" ".join(cmds), "board")
    finally:
        _ensure_view()
    now = [p for p in session.bridge.call("pours", timeout=60)["pours"] if p["net"] == net
           and (layer is None or p["layer"] == _write_layer(layer))]
    bad = [p["layer"] for p in now if abs(p["thermal_width_mm"] - width_mm) > 1e-3]
    if bad:
        raise WriteFailed(f"thermal width not applied on layer(s) {bad}")
    return {"ok": True, "pours": [{"layer": p["layer"], "thermal_width_mm": p["thermal_width_mm"]} for p in now]}


@tool(ADDITIVE)
def add_keepout(x_mm: float, y_mm: float, radius_mm: float,
                layers: list[str] = ["top", "bottom", "vias"]) -> dict:
    """Circular keepout (e.g. around a mounting hole): no copper on 'top' / 'bottom' and no 'vias'
    inside it. Pours and the autorouter respect it. Uses EAGLE restrict layers 41/42/43."""
    rl = {"top": 41, "bottom": 42, "vias": 43}
    bad = [l for l in layers if l not in rl]
    if bad:
        raise ValueError(f"unknown keepout layers {bad}; use top, bottom, vias")
    cmd = "GRID MM; " + " ".join(f"LAYER {rl[l]}; CIRCLE 0 {C.pt(x_mm, y_mm)} {C.pt(x_mm + radius_mm, y_mm)};"
                                  for l in layers)

    def circles(sn):
        root = D.read_xml(sn.board_xml)
        return sum(1 for c in root.iterfind(".//board/plain/circle") if c.get("layer") in {str(rl[l]) for l in layers}
                   and math.isclose(float(c.get("x")), x_mm, abs_tol=1e-3)
                   and math.isclose(float(c.get("y")), y_mm, abs_tol=1e-3))

    def verify(before, after):
        return circles(after) == circles(before) + len(layers), f"{circles(after)} keepout circles at ({x_mm}, {y_mm})"

    after, detail = session.verified_write("board", cmd, verify, schematic=False)
    return {"ok": True, "detail": detail}


@tool(ADDITIVE)
def stitch_vias(net: str = "GND", pitch_mm: float = 2.0, drill_mm: float | None = None,
                max_vias: int = 400, keep_away_mm: dict[str, float] | None = None, dry_run: bool = False) -> dict:
    """Via stitching for a net's pours: vias on a grid wherever they clear other nets' copper, every
    pad (no via-in-pad), holes, keepouts and the board edge, using the design's clearance and
    drill rules. keep_away_mm: {net regex: mm} keeps vias further from some nets' copper, e.g.
    {"^ETH|^USB_D": 0.6} to keep ground as far from impedance pairs as their pours are.
    One call places them all (one undo step). dry_run=true only plans."""
    from fusion_offline import stitch as ST
    snap = _snap(schematic=False)
    plan = ST.plan_stitching(D.read_xml(snap.board_xml), net, pitch_mm, drill_mm, keep_away=keep_away_mm)
    vias = plan["vias"][:max_vias]
    if dry_run or not vias:
        return {"ok": True, "planned": len(vias), **{k: v for k, v in plan.items() if k != "vias"},
                "positions": vias[:50]}
    dia = plan["diameter_mm"]
    cmd = f"GRID MM; CHANGE DRILL {C.n(plan['drill_mm'])}; " + " ".join(
        f"VIA {C.q(net)} {C.n(dia)} round {C.pt(x, y)};" for x, y in vias)
    want = {(round(x, 3), round(y, 3)) for x, y in vias}

    def verify(before, after):
        got = {(round(v.x, 3), round(v.y, 3)) for v in after.board().signals.get(net).vias} if             after.board().signals.get(net) else set()
        missing = want - got
        return (not missing), (f"{len(want)} stitching vias on {net}" if not missing
                               else f"{len(missing)} of {len(want)} vias missing, e.g. {sorted(missing)[:3]}")

    # every via makes Fusion refill the pours: ~0.5 s each on a 4-pour board (254 took ~2 min)
    after, detail = session.verified_write("board", cmd, verify, schematic=False, timeout=60 + 1.0 * len(vias))
    return {"ok": True, "detail": detail, "placed": len(vias), "diameter_mm": dia, "drill_mm": plan["drill_mm"],
            "rejected": plan["rejected"]}


@tool(ADDITIVE)
def fanout_pad(ref: str, pad: str, layer: str = "top", trace_width_mm: float = 0.25,
               max_dist_mm: float = 3.0) -> dict:
    """Fanout via for one pad: a short trace from the pad to the nearest valid via spot (clear of
    other nets, every pad, holes, keepouts and the edge; never via-in-pad). Typical uses: connect a
    GND pad that routing cut off from its pour, or power-pin fanout before autorouting."""
    from fusion_offline import stitch as ST
    snap = _snap(schematic=False)
    root = D.read_xml(snap.board_xml)
    net = next((n for n, s in snap.board().signals.items() if (ref, pad) in s.contacts), None)
    if net is None:
        raise KeyError(f"{ref}.{pad} is not connected to a net")
    plan = ST.plan_fanout(root, ref, pad, net, trace_width_mm, max_dist_mm)
    if plan is None:
        raise ValueError(f"no via spot within {max_dist_mm} mm of {ref}.{pad} with a clear path; "
                         "move parts or allow a longer fanout")
    wl = _write_layer(layer)
    (x0, y0), (x1, y1) = plan["from"], plan["via"]
    cmd = (f"GRID MM; LAYER {wl}; CHANGE WIDTH {C.n(trace_width_mm)}; WIRE {C.q(net)} {C.n(trace_width_mm)} "
           f"{C.pt(x0, y0)} {C.pt(x1, y1)}; CHANGE DRILL {C.n(plan['drill'])}; "
           f"VIA {C.q(net)} {C.n(plan['diameter'])} round {C.pt(x1, y1)};")

    def verify(before, after):
        s = after.board().signals.get(net)
        hit = s and any(math.isclose(v.x, x1, abs_tol=1e-3) and math.isclose(v.y, y1, abs_tol=1e-3) for v in s.vias)
        return bool(hit), f"{ref}.{pad} ({net}) fanned out {plan['distance']} mm to a via at ({x1}, {y1})"

    after, detail = session.verified_write("board", cmd, verify, schematic=False)
    return {"ok": True, "detail": detail, "net": net, "via": plan["via"]}


def _seg_near(sb: dict, x: float, y: float, tol: float) -> bool:
    from fusion_offline import stitch as ST
    return ST.Obstacle("seg", None, False, (sb["x1"], sb["y1"], sb["x2"], sb["y2"], 0.0)).distance(x, y) <= tol


@tool(CHANGE)
def remove_stubs(max_rounds: int = 5) -> dict:
    """Fix the trace stubs Fusion's DRC reports. A stub whose loose end lies on a same-net pad
    (Fusion re-anchors trace ends off-centre when a part rotates) is SNAPPED to the pad centre;
    a truly dangling segment is deleted. Only DRC-reported stubs are touched, and every change is
    verified, so a real connection is never removed."""
    from fusion_offline import stitch as ST
    fixed = []
    for _ in range(max_rounds):
        session.run("DRC;", "board")
        drc = [(e.get("x_mm"), e.get("y_mm")) for e in session.errors("board")["errors"]
               if e.get("description") == "Wire Stub" and e.get("x_mm") is not None]
        if not drc:
            break
        snap = _snap(schematic=False)
        cands = [sb for sb in ST.stub_candidates(D.read_xml(snap.board_xml))
                 if any(_seg_near(sb, x, y, 0.05) for x, y in drc)]
        if not cands:
            break
        for sb in cands:
            wl = _write_layer("bottom") if sb["layer"] == 16 else sb["layer"]
            dx, dy = sb["dangling"]
            ox, oy = (sb["x1"], sb["y1"]) if (sb["x2"], sb["y2"]) == (dx, dy) else (sb["x2"], sb["y2"])
            if sb["on_pad"]:
                # add the corrected segment first (pad centre to the far end), then remove the old one
                cx, cy = sb["on_pad"]
                add_trace(sb["net"], wl, sb["width"], [[cx, cy], [ox, oy]])
                # Fusion merges collinear wires: the old segment may already
                # have been extended to the pad centre
                still = any(round(w.x1, 3) == round(sb["x1"], 3) and round(w.y1, 3) == round(sb["y1"], 3) and
                            round(w.x2, 3) == round(sb["x2"], 3) and round(w.y2, 3) == round(sb["y2"], 3)
                            for w in _snap(schematic=False).board().signals[sb["net"]].wires)
                if not still:
                    fixed.append({"net": sb["net"], "action": "snapped to pad centre (merged)",
                                  "length_mm": sb["length"]})
                    continue
            _delete_segment(sb, wl)
            fixed.append({"net": sb["net"], "action": "snapped to pad centre" if sb["on_pad"] else "deleted",
                          "length_mm": sb["length"]})
    _ensure_view()
    session.run("DRC;", "board")
    left = sum(1 for e in session.errors("board")["errors"] if e.get("description") == "Wire Stub")
    return {"ok": True, "fixed": fixed, "remaining_stubs": left}


def _delete_segment(sb: dict, wl: int) -> None:
    """Delete exactly one wire segment (verified), clicking near its loose end
    with only its copper layer visible."""
    pre, post = _only_layers([wl])
    key = lambda sn: sorted((n, round(w.x1, 3), round(w.y1, 3), round(w.x2, 3), round(w.y2, 3), w.layer)
                            for n, s in sn.board().signals.items() for w in s.wires if w.layer != 19)
    target = (sb["net"], round(sb["x1"], 3), round(sb["y1"], 3), round(sb["x2"], 3), round(sb["y2"], 3))
    dx, dy = sb["dangling"]
    ox, oy = (sb["x1"], sb["y1"]) if (sb["x2"], sb["y2"]) == (dx, dy) else (sb["x2"], sb["y2"])

    def verify(before, after):
        b0, b1 = key(before), key(after)
        gone = [w for w in b0 if w not in b1]
        same = [(e.name, e.x, e.y) for e in before.fab().elements] == [(e.name, e.x, e.y) for e in after.fab().elements]
        ok = same and len(gone) == 1 and gone[0][:5] == target and len(b1) == len(b0) - 1
        return ok, (f"removed segment on {target[0]}" if ok else f"picked the wrong object: {gone[:2]}")

    for t in (0.9, 0.97, 0.75, 0.5):
        mx, my = ox + (dx - ox) * t, oy + (dy - oy) * t
        try:
            session.verified_write("board", f"{pre} DELETE {C.pt(mx, my)}; {post}", verify, schematic=False)
            return
        except WriteFailed:
            continue
    raise WriteFailed(f"could not isolate the segment on {sb['net']} at {sb['dangling']}")


@tool(CHANGE)
def clean_vias(net: str = "GND") -> dict:
    """Delete a net's vias that now violate clearance to a pad (any net) or to another net's copper,
    e.g. stitching vias left under a part that moved. Run stitch_vias again afterwards to refill."""
    from fusion_offline import stitch as ST
    snap = _snap(schematic=False)
    root = D.read_xml(snap.board_xml)
    obs, rules, _ = ST.board_obstacles(root)
    clr = max(ST._mm(rules.get("mdWireVia"), 0.2), ST._mm(rules.get("mdPadVia"), 0.2))
    sig = snap.board().signals.get(net)
    if sig is None:
        raise KeyError(f"no net {net!r}")
    bad = []
    for v in sig.vias:
        r = (v.diameter or (v.drill + 2 * max(0.25 * v.drill, ST._mm(rules.get("rlMinViaOuter"), 0.2032)))) / 2
        for o in obs:
            if o.kind == "circle" and not o.is_pad and o.net == net and                     math.isclose(o.data[0], v.x, abs_tol=1e-3) and math.isclose(o.data[1], v.y, abs_tol=1e-3):
                continue
            if (o.is_pad or (o.net and o.net != net)) and o.distance(v.x, v.y) < r + clr - 1e-4:
                bad.append((v.x, v.y))
                break
    if not bad:
        return {"ok": True, "removed": []}
    pre, post = _only_layers([18])
    cmd = pre + " " + " ".join(f"DELETE {C.pt(x, y)};" for x, y in bad) + " " + post
    key = lambda sn: sorted((round(v.x, 3), round(v.y, 3)) for v in sn.board().signals[net].vias)

    def verify(before, after):
        gone = set(key(before)) - set(key(after))
        want = {(round(x, 3), round(y, 3)) for x, y in bad}
        same_parts = [(e.name, e.x, e.y) for e in before.fab().elements] == [(e.name, e.x, e.y) for e in after.fab().elements]
        return gone == want and same_parts, f"removed {len(gone)} of {len(want)} conflicting {net} vias"

    try:
        after, detail = session.verified_write("board", cmd, verify, schematic=False)
    finally:
        _ensure_view()
    return {"ok": True, "detail": detail, "removed": bad}


@tool(ADDITIVE)
def attach_3d_model(package: str, step_path: str, offset_mm: list[float] | None = None,
                    rotation_deg: list[float] | None = None, folder: str = "3D Packages") -> dict:
    """Give a package in the OPEN library a 3D model from a STEP file. The model is placed in the
    footprint's frame (KiCad library models need no offset; pass offset_mm [x, y, z] and
    rotation_deg [rx, ry, rz] when a model's origin differs), saved as a 3D package document in
    the project's `folder`, and linked to the package. Checks that the model sits over the pads.
    Save the library afterwards (save_design)."""
    res = session.bridge.call("create_package3d", {"package": package, "step_path": step_path,
                                                    "offset_mm": offset_mm, "rotation_deg": rotation_deg,
                                                    "folder": folder}, timeout=300,
                              answers=[(r"If you don't save", "Save")])
    mb, pb = res.get("model_bbox_mm"), res.get("pad_bbox_mm")
    if mb and pb:
        mcx, mcy = (mb[0] + mb[3]) / 2, (mb[1] + mb[4]) / 2
        pcx, pcy = (pb[0] + pb[3]) / 2, (pb[1] + pb[4]) / 2     # both boxes: min xyz, max xyz
        overlap = not (mb[3] < pb[0] or mb[0] > pb[3] or mb[4] < pb[1] or mb[1] > pb[4])
        res["model_vs_pads_center_mm"] = [round(mcx - pcx, 3), round(mcy - pcy, 3)]
        if not overlap:
            res["warning"] = "the model does not overlap the pads; check offset/rotation before using it"
    if not any(res["package"] in d["packages3d"] for d in res.get("devices", [])):
        raise WriteFailed(f"the 3D package was created but is not linked to {package}: {res}")
    return {"ok": True, **res}


@tool(READ)
def get_design_rules() -> dict:
    """Key design rules of the open board (clearances, minimum width and drill, edge clearance, via
    restring), read from Fusion's own V2 rules. Warns when they are still Fusion's new-design
    defaults, which are too loose/tight for most fabs (e.g. 40 mil edge clearance)."""
    import re as _re
    x = session.bridge.call("design_rules", timeout=60)["xml"]
    keys = ("mdWireWire", "mdWirePad", "mdWireVia", "mdPadPad", "mdPadVia", "mdViaVia", "mdCopperDimension",
            "msWidth", "msDrill", "rvViaOuter", "rlMinViaOuter", "layerSetup")
    vals = {k: (_re.search(r'name="' + k + r'" value="([^"]*)"', x) or [None, None])[1] for k in keys}
    warn = []
    if vals.get("mdCopperDimension") in ("40mil", "1.016mm"):
        warn.append("board edge clearance is the 40 mil Fusion default; JLC needs only 0.3 mm. Set the rules "
                    "(DRC dialog > load .edru) before pouring or routing")
    if vals.get("msDrill") == "0.35mm":
        warn.append("minimum drill is the 0.35 mm default; check it against your fab")
    return {"params": vals, "warnings": warn}


def _routing_state(sn) -> dict:
    b = sn.board()
    unrouted = {n: sum(1 for w in s.wires if w.layer == 19) for n, s in b.signals.items()}
    copper = lambda s: [w for w in s.wires if w.layer != 19]
    return {"unrouted_connections": sum(unrouted.values()),
            "unrouted_nets": sorted(n for n, c in unrouted.items() if c),
            "segments": sum(len(copper(s)) for s in b.signals.values()),
            "vias": sum(len(s.vias) for s in b.signals.values()),
            "routed_length_mm": round(sum(sum(w.length for w in copper(s)) for s in b.signals.values()), 1)}


@tool(ADDITIVE)
def autoroute(nets: list[str] | None = None, engine: str = "fusion", timeout_s: float = 600.0,
              route_past_planes: bool = True, top_router: bool = False) -> dict:
    """Route unrouted connections with an autorouter, keeping existing traces (route critical
    nets, power and pours first). engine='fusion' runs Fusion's own autorouter and applies its
    best variant (most complete, then fewest vias). Without `nets`, routes everything left.
    route_past_planes: when a copper layer holding a pour (an inner GND plane) is not enabled for
    routing, Fusion asks whether to run anyway; true answers Yes (signals stay off the plane and the
    pour refills around new vias), false stops.
    top_router: Fusion's TopRouter variant can hang at the share already routed and block the job;
    off by default for the job (the design's autorouter settings are restored afterwards).
    Returns routing metrics before and after."""
    if engine != "fusion":
        raise ValueError("engine must be 'fusion' (freerouting support is planned)")
    from . import autoroute as AR
    before = session.snapshot(schematic=False)
    session.activate("board")
    cmd = "AUTO " + " ".join(C.q(n) for n in nets) + ";" if nets else "AUTO;"
    import tempfile
    tmp = tempfile.mkdtemp(prefix="fusion-mcp-auto-")
    saved = os.path.join(tmp, "design.ctl").replace("\\", "/")
    restore = False
    if not top_router:
        session.bridge.call("run", {"commands": f"AUTO SAVE {C.q(saved)};", "editor": "board"}, timeout=60)
        if os.path.exists(saved):
            with open(saved, encoding="utf-8", errors="replace") as f:
                text = f.read()
            job = os.path.join(tmp, "job.ctl").replace("\\", "/")
            with open(job, "w", encoding="utf-8") as f:
                f.write(AR.ctl_without_top_router(text))
            session.bridge.call("run", {"commands": f"AUTO LOAD {C.q(job)};", "editor": "board"}, timeout=60)
            restore = True
    try:
        run = AR.run(session.bridge, cmd, timeout_s,
                     answers=[(AR.PLANE_LAYERS_PROMPT, "Yes")] if route_past_planes else None)
    finally:
        if restore:
            with contextlib.suppress(BridgeOpError, BridgeUnavailable):
                session.bridge.call("run", {"commands": f"AUTO LOAD {C.q(saved)};", "editor": "board"}, timeout=60)
    m0 = _routing_state(before)
    after = session.snapshot(schematic=False)
    m1 = _routing_state(after)
    for _ in range(10):                     # Fusion may still be applying the chosen variant
        if m1 != m0 or run["applied"] is None:
            break
        time.sleep(3)
        after = session.snapshot(schematic=False)
        m1 = _routing_state(after)
    if m1 == m0:
        raise WriteFailed("the autorouter ran but the board did not change"
                          + (" (timed out)" if run["timed_out"] else "")
                          + (f"; variants seen: {[r['label'] for r in run['variants']]}" if run["variants"] else ""))
    if run["applied"] is None:
        run["applied"] = {"note": "Fusion finished and applied the job itself",
                          "best_seen": AR.best(run["variants"]) if run["variants"] else None}
    return {"ok": True, "engine": "fusion", "applied_variant": run["applied"], "variants": len(run["variants"]),
            "stalled_variants_skipped": run.get("stalled", []),
            "seconds": run["seconds"], "before": m0, "after": m1, "other_dialogs": run["other_dialogs"]}


@tool(READ)
def render_board(highlight_nets: str | None = None, region_mm: list[float] | None = None, traces: bool = True,
                 plan: dict | None = None, out_path: str | None = None) -> list:
    """Picture of the board as Fusion has it now (from a fresh export): outline, pads, holes,
    keepouts, part names, traces (top red solid, bottom blue dashed), vias, pour outlines.
    highlight_nets: regex of nets to colour and label at their pads. region_mm: [x0, y0, x1, y1]
    to zoom. plan: a route_pair plan (from dry_run) drawn on top before writing it. Returns the
    PNG and where it was saved. Needs matplotlib (optional install)."""
    from fusion_offline import render as R
    if not R.available():
        raise ValueError('render_board needs matplotlib: pip install "fusion-electronics-mcp[render]"')
    import tempfile
    root = D.read_xml(_snap(schematic=False).board_xml)
    out = out_path or os.path.join(tempfile.gettempdir(), "fusion-electronics-mcp-board.png")
    info = R.render(root, out, highlight_nets, traces, tuple(region_mm) if region_mm else None,
                    [plan] if plan else None)
    return [Image(path=out), f"saved {out}; {info['traces']} trace segments, {info['vias']} vias; "
                             f"highlighted {', '.join(info['highlighted_nets']) or 'none'}"]


@tool(READ)
def routing_status() -> dict:
    """Unrouted connections, unrouted nets, segment and via counts, routed length."""
    return _routing_state(_snap(schematic=False))


@tool(READ)
def score_placement(exclude_nets: list[str] = ["GND"], crossing_weight_mm: float = 2.0) -> dict:
    """Placement quality without routing: ratsnest length (MST per net over pad centres), number of
    crossing air wires between nets, and the worst nets. Lower is better. Pour nets (GND) are
    excluded by default. Use it before and after placement changes."""
    from fusion_offline import placement as PL
    return PL.score(PL.load(D.read_xml(_snap(schematic=False).board_xml)), tuple(exclude_nets), crossing_weight_mm)


@tool(READ)
def suggest_placement_moves(exclude_nets: list[str] = ["GND"], fixed: list[str] | None = None,
                            top: int = 8, radius_mm: float = 6.0) -> dict:
    """Ranked single-part moves that lower the placement score (shorter ratsnest, fewer crossings),
    searched within radius_mm of each part, inside the board and clear of same-side parts. `fixed`
    lists parts that must not move (connectors, mechanical parts). Nothing is changed: apply the
    ones that make sense with move_part, then score again."""
    from fusion_offline import placement as PL
    m = PL.load(D.read_xml(_snap(schematic=False).board_xml))
    return PL.suggest(m, tuple(exclude_nets), tuple(fixed or ()), top=top, radius=radius_mm)


@tool(ADDITIVE)
def import_netlist_from_kicad(pcb_path: str, part_map: dict[str, str], sheet: int = 1,
                              skip: list[str] | None = None, dry_run: bool = False) -> dict:
    """Build the schematic connectivity of a KiCad board in this design: place each part and connect
    every net (named stubs with labels), using pad numbers. part_map maps a KiCad refdes OR footprint
    name to 'DEVICE@LIBRARY' (device = device set + variant), e.g. {"R1": "RES_0402_1K_1%@MY_PASSIVES",
    "RJ45-TH_RJSAE538402": "CONN_RJ45_2X1_HC-RJ45-059A@MCP Library"}. Parts already in the schematic
    are reused. Mounting holes (no pads) are skipped; add them on the board with add_hole.
    dry_run=true reports the plan without changing anything."""
    from fusion_offline import kicad_pcb as KP
    with open(pcb_path, encoding="utf-8") as f:
        nl = KP.read_netlist(f.read())
    skip = set(skip or [])
    parts = {r: p for r, p in nl["parts"].items() if p["pads"] and r not in skip}
    unmapped = sorted(r for r, p in parts.items() if r not in part_map and p["footprint"] not in part_map)
    nets = {n: [x for x in v if x[0] in parts] for n, v in nl["nets"].items()}
    nets = {n: v for n, v in nets.items() if len(v) > 1}
    plan = {"parts": len(parts), "nets": len(nets), "unmapped_parts": unmapped,
            "skipped_no_pads": sorted(r for r, p in nl["parts"].items() if not p["pads"])}
    if dry_run or unmapped:
        if unmapped and not dry_run:
            raise ValueError(f"no part_map entry for {unmapped} (map their refdes or footprint)")
        return {"ok": True, "applied": False, **plan}
    sch = _snap(board=False).schematic()
    # rows inside the frame, tallest parts first; heights from pin counts (2.54 mm per pin pair)
    order = sorted(parts, key=lambda r: -len(parts[r]["pads"]))
    x, y, row_h, placed = 25.4, 254.0, 0.0, []
    for r in order:
        n_pins = len(set(parts[r]["pads"]))
        h = max(12.7, (n_pins + 1) // 2 * 2.54 + 10.16)
        w = 40.64
        if x + w > 320.0:
            x, y, row_h = 25.4, y - row_h - 12.7, 0.0
        if r not in sch.parts:
            dev, _, lib = (part_map.get(r) or part_map[parts[r]["footprint"]]).partition("@")
            add_part(dev, lib, r, round(x / 2.54) * 2.54, round((y - h / 2) / 2.54) * 2.54, 0, sheet)
            placed.append(r)
        x += w + 12.7
        row_h = max(row_h, h)
    done, failed = [], {}
    for net, refs in sorted(nets.items()):
        name = net if not net.startswith("Net-(") else re.sub(r"[^A-Za-z0-9_+-]", "_", net)[:40]
        try:
            connect_pins(name, [f"{r}.{p}" for r, p in refs])
            done.append(name)
        except ToolError as ex:
            failed[name] = str(ex)[:300]
    return {"ok": not failed, "applied": True, **plan, "placed": placed, "nets_connected": len(done),
            "failed_nets": failed}


@tool(CHANGE)
def import_placement_from_kicad(pcb_path: str, dry_run: bool = False, skip: list[str] | None = None) -> dict:
    """Place this board's parts where a KiCad board (.kicad_pcb) has them: position, rotation and
    side, matched by reference designator. KiCad's frame is converted (origin at the board's
    bottom-left, y up; bottom parts at angle R become mirrored R + 180). All moves run in one
    verified command (one undo step) in Ignore Violators mode, so parts may sit over each other on
    opposite sides. dry_run=true only reports the moves. Routing is not imported."""
    from fusion_offline import kicad_pcb as KP
    with open(pcb_path, encoding="utf-8") as f:
        ref = KP.read_placement(f.read())
    snap = _snap(schematic=False)
    here = {e.name: e for e in snap.fab().elements}
    skip = set(skip or [])
    moves, same = [], []
    for name, p in sorted(ref["parts"].items()):
        if name in skip or name not in here:
            continue
        e = here[name]
        if (math.isclose(e.x, p["x_mm"], abs_tol=1e-3) and math.isclose(e.y, p["y_mm"], abs_tol=1e-3)
                and math.isclose(e.angle % 360, p["angle"] % 360, abs_tol=0.05) and e.mirror == p["bottom"]):
            same.append(name)
        else:
            moves.append((name, p))
    report = {"kicad_board_mm": ref["board_mm"], "fusion_board_mm": snap.fab().size_mm,
              "to_move": [{"ref": n, "x_mm": p["x_mm"], "y_mm": p["y_mm"], "angle": p["angle"],
                           "side": "bottom" if p["bottom"] else "top"} for n, p in moves],
              "already_placed": same,
              "only_in_kicad": sorted(set(ref["parts"]) - set(here) - skip),
              "only_in_fusion": sorted(set(here) - set(ref["parts"]))}
    if dry_run or not moves:
        return {"ok": True, "applied": False, **report}
    cmd = " ".join(C.move_part(n, p["x_mm"], p["y_mm"]) + " " + C.rotate_part(n, p["angle"], p["bottom"])
                   for n, p in moves)

    def verify(before, after):
        now = {e.name: e for e in after.fab().elements}
        bad = [n for n, p in moves if not (
            math.isclose(now[n].x, p["x_mm"], abs_tol=1e-3) and math.isclose(now[n].y, p["y_mm"], abs_tol=1e-3)
            and math.isclose(now[n].angle % 360, p["angle"] % 360, abs_tol=0.05) and now[n].mirror == p["bottom"])]
        return (not bad), (f"placed {len(moves)} parts" if not bad else
                           "not placed as requested: " + ", ".join(f"{n} at ({now[n].x}, {now[n].y}, {now[n].angle})"
                                                                   for n in bad[:6]))

    with _IgnoreViolators():
        after, detail = session.verified_write("board", cmd, verify, schematic=False)
    return {"ok": True, "applied": True, "detail": detail, **report}


@tool(CHANGE)
def undo(editor: str = "board") -> dict:
    """Undo the last change in the board or schematic editor."""
    session.undo(editor)
    return {"ok": True}


@tool(CHANGE)
def save_design(description: str = "saved by fusion-electronics-mcp") -> dict:
    """Save the active document as a new version in Fusion's cloud."""
    return session.bridge.call("save", {"description": description, "wait_s": 120}, timeout=200)


# ---------------------------------------------------------------------------
# library


@tool(READ)
def search_library(query: str) -> list[dict]:
    """Search this server's component library (library/parts)."""
    return library.search(query)


@tool(READ)
def get_library_part(part_id: str) -> dict:
    """Full definition of one library part, including metadata (source, maintainer, link)."""
    return library.get(part_id).data


@tool(ADDITIVE)
def insert_library_part(part_id: str) -> dict:
    """Build a library part into the Fusion library open in the library editor. Afterwards call
    save_design, then close_library, before placing it with add_part (placing from a library that
    is still open can crash Fusion)."""
    part = library.get(part_id)
    session.activate("library")
    from fusion_offline import design as D0
    existing = D0.read_xml(session.export("library"))
    names = {(e.get("name") or "").upper() for e in existing.iter() if e.tag in ("deviceset", "package")}
    if part.data["deviceset"].upper() in names or part.data["package"]["name"].upper() in names:
        raise ValueError(f"{part.data['deviceset']} (or its package) is already in this library; "
                         "re-running the build script would duplicate pads")
    session.run_script(build_script(part), "library")
    from fusion_offline import design as D
    root = D.read_xml(session.export("library"))
    up = lambda v: (v or "").upper()
    ds = next((d for d in root.iter("deviceset") if up(d.get("name")) == up(part.data["deviceset"])), None)
    if ds is None:
        raise WriteFailed(f"device set {part.data['deviceset']} was not created")
    # Fusion upper-cases library object names (TSSOP-16_4.4x5mm -> TSSOP-16_4.4X5MM)
    pk = next((p for p in root.iter("package") if up(p.get("name")) == up(part.data["package"]["name"])), None)
    if pk is None:
        raise WriteFailed(f"package {part.data['package']['name']} was not created")
    want = part.data["package"]
    got_smd, got_pad, got_hole = len(pk.findall("smd")), len(pk.findall("pad")), len(pk.findall("hole"))
    exp = (len(want.get("smds", [])), len(want.get("pads", [])), len(want.get("holes", [])))
    if (got_smd, got_pad, got_hole) != exp:
        raise WriteFailed(f"package has smd/pad/hole {got_smd}/{got_pad}/{got_hole}, expected {exp[0]}/{exp[1]}/{exp[2]}")
    pads = pk.findall("smd") + pk.findall("pad")
    want_conn = {p["name"]: sorted(str(p["pad"]).split()) for p in part.data["symbol"]["pins"]}
    got_conn = {c.get("pin"): sorted((c.get("pad") or "").split()) for c in ds.iter("connect")}
    if want_conn != got_conn:
        raise WriteFailed(f"pin-pad connections are {got_conn}, expected {want_conn}")
    return {"ok": True, "device": part.device_name, "pads": len(pads), "holes": got_hole,
            "next": "save_design, then close_library, then add_part"}


@tool(CHANGE)
def update_from_libraries() -> dict:
    """Update the open design from all its libraries (Fusion's 'Update all'): brings in library
    changes such as attributes (e.g. JLC-ROTATION / JLC-X-OFFSET / JLC-Y-OFFSET) and 3D packages.
    Save the library and close it first. Reports which parts' attributes changed."""
    def attrs(sn):
        root = D.read_xml(sn.board_xml)
        return {e.get("name"): {a.get("name"): a.get("value") for a in e.iterfind("attribute")}
                for e in root.iterfind(".//board/elements/element")}
    session.activate("board")
    before = attrs(_snap(schematic=False))
    session.bridge.call("update_libraries", {}, timeout=300)
    after = attrs(_snap(schematic=False))
    changed = {ref: {k: v for k, v in a.items() if before.get(ref, {}).get(k) != v}
               for ref, a in after.items() if a != before.get(ref)}
    return {"ok": True, "parts_changed": len(changed), "changes": changed}


@tool(CHANGE)
def close_library(name: str) -> dict:
    """Close an open (saved) library document so its parts can be placed."""
    return session.bridge.call("close_library", {"name": name})


# ---------------------------------------------------------------------------
# services (explicit, opt-in, not connected yet)


_NOT_CONNECTED = ("Not yet connected. This service tool makes no network calls in this version; "
                  "nothing was sent anywhere.")


@tool(READ)
def request_design_review(notes: str = "") -> dict:
    """Request a human design review from Groundplane (not connected yet: makes no network calls)."""
    return {"status": "not_connected", "message": _NOT_CONNECTED}


@tool(READ)
def get_assembly_quote(quantity: int = 5) -> dict:
    """Get a PCB assembly quote (not connected yet: makes no network calls)."""
    return {"status": "not_connected", "message": _NOT_CONNECTED}


def main() -> None:
    from .cli import main as cli_main
    raise SystemExit(cli_main())
