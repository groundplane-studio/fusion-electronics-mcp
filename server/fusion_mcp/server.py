"""MCP server: Fusion Electronics read, write, SI, JLC export and library tools.

Reads come from Fusion's EAGLE XML export (parsed here); writes are EAGLE
commands sent through the add-in and verified by re-exporting. No network
access except 127.0.0.1 to the add-in, and easyeda.com only when
check_jlc_orientation is called with fetch=true (see easyeda.py); the
service tools are stubs.
"""

from __future__ import annotations

import contextlib
import xml.etree.ElementTree as ET
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
from . import staleness
from . import dialogs
from .bridge import BridgeOpError, BridgeUnavailable
from .fusion_lock import FusionBusy
from .library import Library, build_script, same_package
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
            stale = staleness.note()
            tail = f" (Note: {stale})" if stale else ""
            try:
                return staleness.attach(fn(*a, **kw), stale)
            except (BridgeUnavailable, FusionBusy) as ex:
                raise ToolError(str(ex) + tail) from None
            except BridgeOpError as ex:
                raise ToolError(f"Fusion refused the operation ({ex.code}): {ex.message}{tail}") from None
            except (WriteFailed, C.InvalidInput, KeyError, ValueError) as ex:
                raise ToolError(str(ex).strip("'\"") + tail) from None
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
    """Which Electronics documents are open in Fusion and which editor is active, and this server's
    version (with a warning when its code is older than what is on disk)."""
    import time as _time
    out = dict(session.context())
    out["server"] = {"version": __version__, "code_loaded": _time.strftime("%Y-%m-%d %H:%M",
                                                                         _time.localtime(staleness.STARTED))}
    return out


@tool(READ)
def list_designs(folder: str | None = None) -> dict:
    """Electronics designs and libraries in the active Fusion project's top folder, or in one
    folder path ('Live tests', 'Parts/Connectors'); folders are not searched recursively (walking
    a big project's folder tree froze Fusion)."""
    return session.bridge.call("list_designs", {"folder": folder}, timeout=120)


@tool(ADDITIVE)
def open_design(name: str, folder: str | None = None, allow_unsaved: bool = False) -> dict:
    """Make a design (schematic + board) current. If any of its documents is already open,
    Fusion just switches to it (already_open=true). Otherwise it is opened from the active
    project, which is refused while an open design has unsaved changes (that once froze
    Fusion): save first, or pass allow_unsaved=true to open it anyway. Switching designs
    affects every Claude session using Fusion."""
    return session.bridge.call("open_design", {"name": name, "folder": folder, "allow_unsaved": allow_unsaved},
                               timeout=180)


@tool(ADDITIVE)
def open_library(name: str, folder: str | None = None, allow_unsaved: bool = False) -> dict:
    """Open a Fusion library (.flbr) from the active project in the library editor (or switch to
    it if open). allow_unsaved as in open_design."""
    return session.bridge.call("open_design", {"name": name, "folder": folder, "kind": "library",
                                               "allow_unsaved": allow_unsaved}, timeout=180)


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
    """Board size, copper layers, part and net counts, net classes (width, drill and clearance as
    DRC applies them: a class's design rule first, else its legacy value) and design-rule
    highlights."""
    from fusion_offline import net_classes as NC
    s = _snap(schematic=False)
    fab, b = s.fab(), s.board()
    xml, _ = _v2_rules()
    eff = NC.effective(D.read_xml(s.board_xml), xml)
    return {
        "size_mm": fab.size_mm, "outline_bbox_mm": fab.outline,
        "copper_layers": [{"number": n, "name": _layer_name(b, n)} for n in b.copper_layers],
        "parts": len(fab.elements), "nets": len(b.signals),
        "routed_nets": sum(1 for x in b.signals.values() if x.wires),
        "vias": sum(len(x.vias) for x in b.signals.values()),
        "net_classes": [{"name": name, **v} for name, v in eff.items()],
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
    """Nets (merged across schematic sheets) with pin counts and routed length on the board (copper
    only). Names with overbar markup ('!') also get a readable form, e.g. PI_LED_~{PWR}."""
    from fusion_offline import review as RV
    s = _snap()
    b = s.board()
    sch = s.schematic() if s.sch_xml else None
    names = set(b.signals) | (set(sch.nets) if sch else set())
    rows = []
    for name in sorted(names):
        if filter and filter.lower() not in name.lower():
            continue
        sig = b.signals.get(name)
        row = {"name": name, "class": sig.net_class if sig else (sch.nets[name].net_class if sch else None),
               "schematic_pins": len(sch.nets[name].pins) if sch and name in sch.nets else 0,
               "board_contacts": len(sig.contacts) if sig else 0,
               "routed_mm": round(sum(w.length for w in sig.wires if w.layer != 19), 3) if sig else 0.0,
               "vias": len(sig.vias) if sig else 0}
        if "!" in name:
            ob = RV.overbar(name)
            row["readable"] = ob["readable"]
            if ob["suspect"]:
                row["markup_warning"] = ob["suspect"]
        rows.append(row)
    return rows[:limit]


@tool(READ)
def get_net(name: str) -> dict:
    """One net: its schematic pins (with direction) and its board routing by layer (length_mm is
    routed copper; unrouted_mm the air wires). Overbar markup gets a readable form."""
    from fusion_offline import review as RV
    s = _snap()
    b = s.board()
    sch = s.schematic() if s.sch_xml else None
    out: dict[str, Any] = {"name": name}
    if "!" in name:
        out["overbar"] = RV.overbar(name)
    if sch and name in sch.nets:
        n = sch.nets[name]
        out["schematic"] = {"class": n.net_class, "sheets": sorted(set(n.sheets)),
                            "pins": [{"part": r.part, "pin": r.pin,
                                      "direction": (sch.pin(r.part, r.pin).direction if sch.pin(r.part, r.pin) else None)}
                                     for r in n.pins]}
    if name in b.signals:
        g = b.signals[name]
        out["board"] = {"class": g.net_class, "contacts": [f"{e}.{p}" for e, p in g.contacts],
                        "length_mm": round(sum(w.length for w in g.wires if w.layer != 19), 3),
                        "unrouted_mm": round(sum(w.length for w in g.wires if w.layer == 19), 3),
                        "length_by_layer_mm": {_layer_name(b, k): round(v, 3) for k, v in g.length_by_layer().items()},
                        "widths_mm": sorted({round(w.width, 4) for w in g.wires}),
                        "vias": [{"x_mm": v.x, "y_mm": v.y, "drill_mm": v.drill, "extent": v.extent} for v in g.vias],
                        "polygons": g.polygons}
    if "schematic" not in out and "board" not in out:
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
    """Run Fusion's DRC on the board and return every violation (check_drc summarises them and
    shows what changed instead)."""
    session.run("DRC;", "board", check_dialogs=False)
    return session.errors("board")


_DRC_LAST: dict[str, list] = {}     # design -> errors of its last check_drc (this server)


@tool(READ)
def check_drc(compare: bool = True, top: int = 5) -> dict:
    """Run Fusion's DRC and summarise it: errors by type, airwires as a count (no 200-line
    dump). With compare=true (default) also what changed since the last check_drc on this
    design: new errors by type with up to `top` locations each, and how many were fixed or are
    unchanged. Writes report their own new errors (DRC before and after each verified write)."""
    from fusion_offline import drc as DRC
    active = session.active_design()
    errs = session.drc_errors()
    out = {"design": active, **DRC.summarize(errs)}
    prev = _DRC_LAST.get(active or "")
    if compare and prev is not None:
        out["since_last_check"] = DRC.diff(prev, errs, top)
    elif compare:
        out["since_last_check"] = None
        out["note"] = "first check of this design in this session; the next check_drc shows what changed"
    _DRC_LAST[active or ""] = errs
    return out


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
    lens = {n: round(sum(w.length for w in b.signals[n].wires if w.layer != 19), 3) for n in nets}  # copper, not air wires
    unrouted = [n for n in nets if any(w.layer == 19 for w in b.signals[n].wires)]
    ref = max(lens.values())
    rows = [{"net": n, "length_mm": L, "short_by_mm": round(ref - L, 3), "ok": ref - L <= tolerance_mm}
            for n, L in lens.items()]
    return {"reference_mm": ref, "tolerance_mm": tolerance_mm, "all_ok": all(r["ok"] for r in rows) and not unrouted,
            "unrouted": unrouted, "nets": rows}


def _tolerance_rows(query: str | None = None) -> list[dict]:
    import json as _json
    with open(os.path.join(os.path.dirname(__file__), "data", "length_tolerances.json"), encoding="utf-8") as f:
        rows = _json.load(f)["rows"]
    if not query:
        return rows
    q = query.casefold()
    return [r for r in rows if q in r["interface"].casefold()]


@tool(READ)
def length_tolerances(interface: str | None = None) -> dict:
    """Typical length-matching tolerances per interface (Ethernet MDI, RGMII, SGMII, USB 2/3,
    PCIe, SATA, HDMI, MIPI CSI-2, DDR4, LPDDR4, ...): within a pair and between pairs/lanes, the
    impedance, notes, and the vendor document, table and URL each value was read from. A field is
    null where no source could be confirmed. Typical starting points only: the SoC/PHY design
    guide for your part wins. interface filters by name (e.g. "1000BASE-T", "DDR4")."""
    import json as _json
    with open(os.path.join(os.path.dirname(__file__), "data", "length_tolerances.json"), encoding="utf-8") as f:
        table = _json.load(f)
    rows = _tolerance_rows(interface)
    return {"note": table["note"], "checked": table["checked"], "rows": rows}


def _groups_of(design: str | None) -> tuple[str, dict]:
    from . import design_store
    name = design or session.active_design() or "unnamed"
    return name, design_store.load(name).get("length_groups", {})


@tool(ADDITIVE)
def set_length_group(name: str, members: list, intra_tol_mm: float | None = None,
                     inter_tol_mm: float | None = None, target: str | float = "longest",
                     measure: str = "pair_average", follow_series: bool = True,
                     preset: str | None = None, design: str | None = None) -> dict:
    """Create or replace a length-matching group, saved for this design (a file on this machine,
    not in the Fusion design). members: pairs ["TP0_P", "TP0_N"] and/or single nets. Each member is
    measured as a path: through two-pin series parts (0 ohm, series R, AC caps) into the next net
    when follow_series (TP0_P -> R18 -> N$30), with the part's pad-to-pad length. intra_tol_mm: P
    vs N skew allowed in each pair; inter_tol_mm: each member against the target ("longest" or a
    length in mm); measure: a pair's length is its "pair_average" or "max". E.g. 1000BASE-T MDI:
    TP0-TP3, intra 0.1, inter 0.5 (see the typical tolerances table). Check with
    check_length_groups. preset: an interface from length_tolerances (e.g. "1000BASE-T") fills
    in whichever tolerance you leave out and the table has a value for (a blank one stays unset:
    give it yourself). design: defaults to Fusion's active design."""
    from . import design_store
    used = None
    if preset:
        rows = _tolerance_rows(preset)
        if len(rows) != 1:
            raise ValueError(f"preset {preset!r} matches {len(rows)} rows of length_tolerances; be more specific")
        used = rows[0]
        if intra_tol_mm is None and used.get("within_pair"):
            intra_tol_mm = used["within_pair"]["mm"]
        if inter_tol_mm is None and used.get("between_pairs"):
            inter_tol_mm = used["between_pairs"]["mm"]
    if measure not in ("pair_average", "max"):
        raise ValueError("measure is 'pair_average' or 'max'")
    if target != "longest":
        target = float(target)
    clean = []
    for m in members:
        if isinstance(m, (list, tuple)):
            if len(m) != 2:
                raise ValueError(f"a pair member is [P, N]: {m!r}")
            clean.append([str(m[0]), str(m[1])])
        else:
            clean.append(str(m))
    if not clean:
        raise ValueError("no members")
    root = D.read_xml(_snap(schematic=False).board_xml)
    sigs = {s.get("name") for s in root.iterfind("./drawing/board/signals/signal")}
    missing = [n for m in clean for n in (m if isinstance(m, list) else [m]) if n not in sigs]
    if missing:
        raise ValueError(f"not on the board: {', '.join(missing)}")
    dname, _ = _groups_of(design)
    data = design_store.load(dname)
    data.setdefault("length_groups", {})[name] = {
        "name": name, "members": clean, "intra_tol_mm": intra_tol_mm, "inter_tol_mm": inter_tol_mm,
        "target": target, "measure": measure, "follow_series": follow_series}
    where = design_store.save(dname, data)
    out = {"design": dname, "group": data["length_groups"][name], "saved_to": where}
    if used:
        out["preset"] = {"interface": used["interface"], "source": used["source"], "notes": used["notes"]}
        if inter_tol_mm is None or intra_tol_mm is None:
            out["preset"]["missing"] = [k for k, v in (("intra_tol_mm", intra_tol_mm), ("inter_tol_mm", inter_tol_mm))
                                        if v is None]
    return out


@tool(READ)
def list_length_groups(design: str | None = None) -> dict:
    """The length groups saved for a design (default: the active one)."""
    dname, groups = _groups_of(design)
    return {"design": dname, "groups": groups}


@tool(CHANGE)
def delete_length_group(name: str, design: str | None = None) -> dict:
    """Remove a saved length group."""
    from . import design_store
    dname, groups = _groups_of(design)
    if name not in groups:
        raise ValueError(f"no length group {name!r} for {dname!r}; groups: {', '.join(groups) or 'none'}")
    data = design_store.load(dname)
    data["length_groups"].pop(name)
    design_store.save(dname, data)
    return {"design": dname, "deleted": name, "left": sorted(data["length_groups"])}


def _copper(root) -> dict:
    """Copper per export layer: the V2 stackup's thicknesses when readable, else the design's."""
    from fusion_offline import current as CU, stackup as ST
    xml, _ = _v2_rules()
    st = ST.parse_stackup(xml) if xml else None
    cu = [l.thickness_mm for l in st.copper] if st and st.copper and all(l.thickness_mm for l in st.copper) else None
    return CU.copper_by_layer(root, cu)


def _currents(design: str | None = None) -> tuple[str, dict]:
    from . import design_store
    name = design or session.active_design() or "unnamed"
    return name, design_store.load(name).get("net_currents", {})


@tool(ADDITIVE)
def set_net_current(currents: dict[str, float], delta_t_c: float = 10.0, design: str | None = None) -> dict:
    """Record the current each net carries (A), and the temperature rise allowed (degC), for this
    design (a file on this machine, next to its length groups). size_for_current and
    check_current use them, and route_trace / route_net size the trace from them when no width
    is given. A current of 0 removes the net."""
    from . import design_store
    dname, _ = _currents(design)
    data = design_store.load(dname)
    store = data.setdefault("net_currents", {})
    root = D.read_xml(_snap(schematic=False).board_xml)
    sigs = {sg.get("name") for sg in root.iterfind("./drawing/board/signals/signal")}
    missing = [n for n in currents if n not in sigs]
    if missing:
        raise ValueError(f"not on the board: {', '.join(missing)}")
    for net, amps in currents.items():
        if float(amps) <= 0:
            store.pop(net, None)
        else:
            store[net] = {"a": float(amps), "delta_t_c": float(delta_t_c)}
    design_store.save(dname, data)
    return {"design": dname, "net_currents": store}


@tool(READ)
def current_ratings(interface: str | None = None) -> dict:
    """Reference currents interfaces may carry (PoE Types 1-4 per pair set, USB 2.0/3.x default,
    USB Type-C 1.5 A / 3 A), each with the document it was read from. The parts on a path can
    allow less: see set_part_rating. interface filters by name (e.g. "PoE")."""
    import json as _json
    with open(os.path.join(os.path.dirname(__file__), "data", "current_ratings.json"), encoding="utf-8") as f:
        table = _json.load(f)
    if interface:
        table["rows"] = [r for r in table["rows"] if interface.casefold() in r["interface"].casefold()]
    return table


@tool(ADDITIVE)
def set_part_rating(part_number: str, current_a: float, source: str, note: str | None = None) -> dict:
    """Record a part's current rating from its datasheet (part number or value as on the board's
    parts, the rating in A, and the datasheet link or document), kept for all designs.
    size_for_current and check_current then cap a net's current at the weakest rated part on it:
    e.g. PoE magnetics rated 350 mA on a PoE input that the standard allows 960 mA."""
    from . import part_ratings
    return {"saved": part_ratings.set_rating(part_number, current_a, source, note), "file": part_ratings.path()}


@tool(READ)
def list_part_ratings() -> dict:
    """The part current ratings recorded with set_part_rating."""
    from . import part_ratings
    return {"ratings": part_ratings.load(), "file": part_ratings.path()}


@tool(READ)
def size_for_current(nets: list[str] | None = None, current_a: float | None = None,
                     delta_t_c: float | None = None) -> dict:
    """Trace width each net needs for its current on every copper layer (IPC-2221, conservative:
    I = k dT^0.44 A^0.725, k 0.048 outer / 0.024 inner), from each layer's real copper thickness
    (the board's stackup; 0.5 oz inner layers need several times the outer width). Uses the
    currents saved with set_net_current, or current_a for the nets given. Suggests a current-tier
    net class (e.g. pwr_1A with the outer width) to create with set_net_class and assign with
    assign_net_class, so DRC flags narrower traces."""
    from fusion_offline import current as CU
    root = D.read_xml(_snap(schematic=False).board_xml)
    copper = _copper(root)
    _, saved = _currents()
    if current_a is not None:
        if not nets:
            raise ValueError("give the nets for current_a")
        want = {n: {"a": current_a, "delta_t_c": delta_t_c or 10.0} for n in nets}
    else:
        want = {n: v for n, v in saved.items() if not nets or n in nets}
        if delta_t_c:
            want = {n: {**v, "delta_t_c": delta_t_c} for n, v in want.items()}
    if not want:
        return {"nets": {}, "note": "no currents saved for these nets: set_net_current, or pass current_a"}
    from . import part_ratings
    from fusion_offline import net_classes as NC
    rated = part_ratings.load()
    fab_min = _fab_min_width(root)
    xml, _ = _v2_rules()
    by_num = {str(v["number"]): {"name": k, **v} for k, v in NC.effective(root, xml).items()}
    sig_class = {sg.get("name"): sg.get("class") or "0" for sg in root.iterfind("./drawing/board/signals/signal")}
    rows = {}
    for net, v in want.items():
        amps = v["a"]
        parts = part_ratings.ratings_on(root, net, rated)
        limit = None
        if parts and parts[0]["current_a"] < amps:
            limit = parts[0]
            amps = limit["current_a"]
        need = CU.required(amps, v["delta_t_c"], copper)
        widths, fab_note = _practical(need, copper, fab_min)
        outer = max(widths[copper[n]["name"]] for n in need if copper[n]["outer"])
        inner = {copper[n]["name"]: widths[copper[n]["name"]] for n in need if not copper[n]["outer"]}
        rows[net] = {"current_a": amps, "delta_t_c": v["delta_t_c"], "width_mm": widths,
                     "ipc2221_mm": {copper[n]["name"]: w for n, w in need.items()}}
        if fab_note:
            rows[net]["width_note"] = fab_note
        cls_num = sig_class.get(net, "0")
        cls = by_num.get(cls_num)
        if cls and cls_num != "0":
            cw = cls["width_mm"] or 0.0
            rows[net]["existing_class"] = {"name": cls["name"], "width_mm": cw,
                                           "advice": _class_advice(cls["name"], cw, outer, inner)}
            rows[net]["suggested_class"] = {"name": cls["name"], "width_mm": max(outer, cw), "existing": True}
        else:
            fits = sorted((c for c in by_num.values() if c["number"] != 0 and (c["width_mm"] or 0) >= outer - 1e-6),
                          key=lambda c: c["width_mm"])
            rows[net]["suggested_class"] = ({"name": fits[0]["name"], "width_mm": fits[0]["width_mm"], "existing": True}
                                            if fits else {"name": f"pwr_{amps:g}A", "width_mm": outer, "existing": False})
        if parts:
            rows[net]["rated_parts"] = [{"ref": r["ref"], "part": r["part"], "current_a": r["current_a"],
                                         "source": r["source"]} for r in parts]
        if limit:
            rows[net]["limited_by"] = (f"{limit['ref']} ({limit['part']}) is rated {limit['current_a']} A, below the "
                                       f"{v['a']} A set for {net}: sized for the part. If more current must flow, "
                                       "that part has to change too.")
    return {"nets": rows, "copper": {c["name"]: {"copper_mm": c["copper_mm"], "outer": c["outer"],
                                                 "from": c["source"]} for c in copper.values()},
            "method": "IPC-2221 (conservative); IPC-2152 gives narrower widths for the same rise",
            "note": ("a class width rule covers the class's pads too: narrower pads (connector pins, "
                     "small capacitors) then fail DRC. Make the class rule a wire-only rule, or route at "
                     "these widths and keep the class width at what the pads allow")}


@tool(READ)
def check_current(nets: list[str] | None = None, via_current_a: float = 1.0) -> dict:
    """Check routed copper against the saved net currents (set_net_current): the narrowest segment
    per layer against the IPC-2221 width for that layer's copper, and the via count against the
    current (via_current_a per via, about 1 A for a 0.3 mm via). Layers where the net has a pour
    are not judged by trace width."""
    from fusion_offline import current as CU
    dname, saved = _currents()
    want = {n: v for n, v in saved.items() if not nets or n in nets}
    if not want:
        return {"design": dname, "nets": [], "note": "no currents saved: set_net_current"}
    from . import part_ratings
    root = D.read_xml(_snap(schematic=False).board_xml)
    res = CU.check(root, want, _copper(root), via_current_a)
    rated = part_ratings.load()
    for r in res:
        low = [p for p in part_ratings.ratings_on(root, r["net"], rated) if p["current_a"] < r.get("current_a", 0)]
        if low:
            r["ok"] = False
            r["problems"].append(", ".join(f"{p['ref']} ({p['part']}) is rated {p['current_a']} A" for p in low)
                                 + f", below the {r['current_a']} A this net carries")
    judged = [r for r in res if r["ok"] is not None]
    return {"design": dname, "ok": all(r["ok"] for r in judged) if judged else None,
            "not_routed": [r["net"] for r in res if r["ok"] is None], "nets": res}


def _class_advice(name: str, cw: float, outer: float, inner: dict) -> str:
    """Judge a class width against the outer layers, where class-width traces run, with inner
    layers as a separate note; never advise narrowing the class."""
    inner_need = max(inner.values()) if inner else 0.0
    if cw >= outer - 1e-6:
        text = f"{name} {cw:g} mm is enough on the outer layers (needs {outer:g} mm)"
    else:
        text = (f"{name} {cw:g} mm < {outer:g} mm needed on the outer layers: widen the class to {outer:g} mm "
                "(set_net_class), pour the net, or run a parallel trace")
    if inner_need > max(cw, outer) + 1e-6:
        text += (f"; inner layers need {inner_need:g} mm, so use a pour or plane there" if inner_need > 3 * max(cw, outer)
                 else f"; inner layers need {inner_need:g} mm: keep this net on the outer layers or route at least "
                      f"{inner_need:g} mm there")
    return text


def _practical(need: dict, copper: dict, fab_min: float) -> tuple[dict, str | None]:
    """IPC widths made routable: never below the fab's minimum trace width, rounded up to 0.01 mm.
    Returns ({layer name: mm}, a note when the fab minimum, not the current, set a width)."""
    out, floored = {}, []
    for n, w in need.items():
        out[copper[n]["name"]] = math.ceil(max(w, fab_min) * 100 - 1e-6) / 100
        if w < fab_min:
            floored.append(copper[n]["name"])
    note = (f"{', '.join(floored)}: width set by the fab minimum ({fab_min:g} mm), not the current"
            if floored else None)
    return out, note


def _width_for(net: str | None, given: float | None, root=None) -> tuple[float, str | None]:
    """The width to route a net with: the one given, else the saved current's width on the outer
    layers, else 0.25 mm."""
    if given is not None:
        return given, None
    if net:
        _, saved = _currents()
        if net in saved:
            from fusion_offline import current as CU
            root = root if root is not None else D.read_xml(_snap(schematic=False).board_xml)
            copper = _copper(root)
            need = CU.required(saved[net]["a"], saved[net]["delta_t_c"], copper)
            widths, fab_note = _practical(need, copper, _fab_min_width(root))
            w = max(widths[copper[n]["name"]] for n in need if copper[n]["outer"])
            return w, (f"width {w:g} mm from {net}'s {saved[net]['a']} A (IPC-2221, outer layers)"
                       + (f"; {fab_note}" if fab_note else ""))
    return 0.25, None


@tool(READ)
def check_length_groups(groups: list[str] | None = None) -> dict:
    """Check the saved length groups of the active design against the routed board: each member's
    path length (through series parts), its P/N skew against intra_tol_mm, its difference from
    the group target against inter_tol_mm, pass/fail, and add_mm: how much to add to which net
    to pass. Members with air wires left are listed as unrouted."""
    from fusion_offline import length_groups as LG
    dname, saved = _groups_of(None)
    if not saved:
        return {"design": dname, "groups": [], "note": "no length groups saved for this design: set_length_group"}
    want = groups or sorted(saved)
    unknown = [g for g in want if g not in saved]
    if unknown:
        raise ValueError(f"no length group {', '.join(unknown)}; groups: {', '.join(sorted(saved))}")
    root = D.read_xml(_snap(schematic=False).board_xml)
    res = [LG.evaluate(root, saved[g]) for g in want]
    return {"design": dname, "ok": all(r["ok"] for r in res), "groups": res}


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
    sch_root = D.read_xml(snap.sch_xml) if snap.sch_xml else ET.Element("eagle")
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
        ours, easy, used = J.by_function(pp[0] if pp else [], J.pin_names_from_schematic(sch_root, el.name),
                                         J.easyeda_pads(result), J.easyeda_pin_names(result))
        d = J.derive(ours, easy)
        out[el.name] = {"code": code, "matched_by": "pin function" if used else "pad number",
                        "derived": d.as_dict() if d else None,
                        **({} if d else {"error": "pads could not be matched"})}
    return out


def _jlc(apply_orientation: bool = False):
    snap = _snap(schematic=apply_orientation)      # pin names come from the schematic
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
    cached forever, >= 15 s between requests); otherwise only the local cache is used. Pads are
    matched by pin FUNCTION (K/A, FB/EN...) when both footprints name their pins, so a part whose
    libraries number pads differently is not turned around."""
    snap = _snap()
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


def _sch_wires(root) -> list[tuple]:
    return sorted((si, n.get("name"), w.get("x1"), w.get("y1"), w.get("x2"), w.get("y2"))
                  for si, sh in enumerate(root.iterfind("./drawing/schematic/sheets/sheet"), 1)
                  for n in sh.iterfind("./nets/net") for w in n.iter("wire"))


def _apply_labels(plan: list[dict], dry_run: bool, design: str | None, extra: dict) -> dict:
    """Write a label plan (fusion_offline.sch_labels) as ONE undo step and check it: every moved
    label smashed, at its place and angle; no part moved or turned; no other label and no net
    wire changed (the edits pick by coordinate, so anything else changing means a wrong pick)."""
    from fusion_offline import sch_labels as SL
    active = session.require_design(design)
    if not plan:
        return {"written": False, "design": active, "detail": "no labels to change", **extra}
    out = {"design": active, "parts": [{"part": p["part"], "sheet": p["sheet"], "smash": p["smash"],
                                        "labels": {m["label"]: {"to": m["to"], "angle": m["angle"]} for m in p["moves"]}}
                                       for p in plan], **extra}
    if dry_run:
        return {"written": False, **out}
    planned = {(p["part"], p["gate"]): p for p in plan}

    def verify(before, after):
        r0, r1 = D.read_xml(before.sch_xml), D.read_xml(after.sch_xml)
        i0 = {(i["part"], i["gate"]): i for i in SL.instances(r0)}
        i1 = {(i["part"], i["gate"]): i for i in SL.instances(r1)}
        bad = []
        for key, a in i1.items():
            b = i0.get(key)
            if b is None:
                continue
            if (abs(a["x"] - b["x"]) > 1e-3 or abs(a["y"] - b["y"]) > 1e-3 or a["mirror"] != b["mirror"]
                    or abs((a["angle"] - b["angle"] + 180) % 360 - 180) > 0.05):
                bad.append(f"{key[0]} itself moved or turned (a pick hit the part, not its label)")
            p = planned.get(key)
            for name, lab in a["labels"].items():
                m = next((m for m in p["moves"] if m["label"] == name), None) if p else None
                if m is None:
                    old = b["labels"].get(name)
                    if old and (abs(lab["x"] - old["x"]) > 1e-3 or abs(lab["y"] - old["y"]) > 1e-3):
                        bad.append(f"{key[0]} {name} moved but was not meant to")
                    continue
                if not lab["smashed"]:
                    bad.append(f"{key[0]} was not smashed")
                elif (abs(lab["x"] - m["to"][0]) > 1e-3 or abs(lab["y"] - m["to"][1]) > 1e-3
                      or abs((lab["angle"] - m["angle"] + 180) % 360 - 180) > 0.05):
                    bad.append(f"{key[0]} {name} at ({lab['x']}, {lab['y']}) {lab['angle']} instead of "
                               f"({m['to'][0]}, {m['to'][1]}) {m['angle']}")
        if _sch_wires(r0) != _sch_wires(r1):
            bad.append("a net wire changed (a pick hit a wire)")
        n = sum(len(p["moves"]) for p in plan)
        return not bad, ("; ".join(bad[:6]) if bad else f"{n} label(s) on {len(plan)} part(s) placed")
    after, detail = session.verified_write("schematic", SL.commands(plan), verify, board=False,
                                           design=design or active)
    return {"written": True, "detail": detail, **out}


@tool(CHANGE)
def straighten_labels(parts: list[str] | None = None, prefixes: list[str] | None = None, sheet: int | None = None,
                      side: str = "right", dry_run: bool = True, design: str | None = None) -> dict:
    """Make the ref (>NAME) and value (>VALUE) labels of two-pin passives read horizontally whatever
    way the symbol is turned: for each part at R90 / R270 (mirrored too) whose labels read
    vertically, SMASH it and put each such label beside the body (NAME above the centre line,
    VALUE below) at 0 degrees, on `side` ("right" or "left"). Labels already horizontal are left
    alone. prefixes: which parts (default R, C, L, D) unless `parts` names them; sheet: one sheet
    only. ONE undo step; checked afterwards (labels where planned, no part or net wire moved),
    else undone. dry_run=true (default) lists what would change."""
    from fusion_offline import sch_labels as SL
    root = D.read_xml(_snap(board=False).sch_xml)
    plan = SL.plan_straighten(root, parts, tuple(prefixes) if prefixes else ("R", "C", "L", "D"), sheet, side)
    return _apply_labels(plan, dry_run, design, {})


@tool(CHANGE)
def match_labels(source: str, targets: list[str], dry_run: bool = False, design: str | None = None) -> dict:
    """Copy a part's label layout onto other parts: the NAME and VALUE positions and angles relative
    to the part, so pasted copies (which Fusion resets to the symbol's default) look like the
    original. Targets are smashed as needed. ONE undo step, checked as straighten_labels."""
    from fusion_offline import sch_labels as SL
    root = D.read_xml(_snap(board=False).sch_xml)
    plan, warnings = SL.plan_match(root, source, targets)
    return _apply_labels(plan, dry_run, design, {"warnings": warnings} if warnings else {})


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


@tool(CHANGE)
def move_part(ref: str, x_mm: float, y_mm: float, dry_run: bool = False, design: str | None = None) -> list:
    """Move a board part so its origin is at (x, y) mm. dry_run=true writes nothing and returns
    a picture of the part at its new place with courtyards, plus the problems the move would
    introduce or clear. design: the design you mean (as get_context names it); the write is
    refused if Fusion's active design is another one. To move several parts as one undo step,
    use move_parts."""
    return _move_parts([{"ref": ref, "x_mm": x_mm, "y_mm": y_mm}], dry_run, design)


@tool(CHANGE)
def rotate_part(ref: str, angle: float, bottom: bool = False, dry_run: bool = False,
                design: str | None = None) -> list:
    """Set a board part's absolute rotation (degrees) and side (bottom=true places it on the
    bottom). dry_run and design work as in move_part."""
    return _move_parts([{"ref": ref, "angle": angle, "bottom": bottom}], dry_run, design)


@tool(CHANGE)
def move_parts(moves: list[dict], dry_run: bool = False, design: str | None = None) -> list:
    """Move and/or rotate several board parts as ONE undo step.
    moves: [{"ref": "R18", "x_mm": 75.5, "y_mm": 38.75, "angle": 180, "bottom": false}, ...];
    leave out anything that should stay as it is (e.g. only "angle").
    The write is checked as a whole: every listed part must end exactly where asked and no other
    part may move (Fusion pushes parts aside in its default mode, so the moves run with
    violations ignored); otherwise the whole batch is undone.
    dry_run=true writes nothing: it returns a picture of the parts at their new places with
    courtyards drawn, and what the moves would introduce or clear: courtyard overlaps (derived
    ones marked, for parts without a library courtyard), pad gaps under the rules, and silkscreen
    on a neighbour's pads. The same report comes back after a real write. Traces stay put in
    the preview; in Fusion, trace ends on a moved part's pads follow it.
    design: the design you mean (as get_context or the dry run's reply names it). The write is
    refused if Fusion's active design is another one, checked again immediately before writing.
    Without it, a write is still refused when these parts were last previewed on a different
    design than the one now active (Fusion was switched in between)."""
    return _move_parts(moves, dry_run, design)


@tool(CHANGE)
def place_inline(parts: list[str], x_mm: float | None = None, y_mm: float | None = None, to: str | None = None,
                 angle: float | None = None, face_anchor: bool = True, dry_run: bool = True,
                 design: str | None = None) -> list:
    """Line up two-pin parts (series resistors, AC caps) on the pads they connect to, as ONE
    undo step: x_mm puts them in one column at that x, each on the ROW of its anchor pad;
    y_mm puts them in one row, each on its anchor pad's COLUMN. The anchor is the pad of `to`
    (e.g. "T1") that shares a net with the part, or without `to` the connected pad of another
    part nearest the line. The part's connecting pad lands exactly on the anchor's row/column.
    angle: rotation for all of them (default: keep each one's); face_anchor turns a part by
    180 degrees when that puts its connecting pad on the side facing the anchor.
    dry_run=true (default) writes nothing and returns the preview and report of move_parts
    (what would be introduced or cleared); run again with dry_run=false (and design=) to move.
    Parts that are not two-pin or share no net with `to` are skipped and listed."""
    import json
    from fusion_offline import placement_plan as PP
    root = D.read_xml(_snap(schematic=False).board_xml)
    plan = PP.inline(root, parts, x=x_mm, y=y_mm, to=to, angle=angle, face_anchor=face_anchor)
    if not plan["moves"]:
        return [json.dumps({"written": False, "detail": "nothing to place", "skipped": plan["skipped"]})]
    out = _move_parts(plan["moves"], dry_run, design)
    report = json.loads(out[-1])
    report.update(anchors=plan["anchors"], skipped=plan["skipped"])
    return out[:-1] + [json.dumps(report)]


@tool(CHANGE)
def tidy_placement(parts: list[str] | None = None, region_mm: list[float] | None = None,
                   grid_mm: dict[str, float] | None = None, critical: list[str] | None = None,
                   nudge_mm: float = 0.25, rework_gap_mm: float = 0.0, max_move_mm: float = 0.5,
                   align: bool = True, spread: bool = True, rotate: bool = False, skip_routed: bool = True,
                   keep: list[str] | None = None, dry_run: bool = True, design: str | None = None) -> list:
    """Tidy placement as ONE undo step: snap parts to the grid (grid_mm, default
    {"passive": 0.125, "other": 0.25}), line up rows/columns that are within 0.2 mm of lining up,
    and even out a slightly uneven pitch along a row of one package (rows that follow a
    neighbour's pin pitch are left alone). rotate=true also turns non-polar two-pin parts (R, C,
    L, FB) by 180 degrees to match their row (off by default: it swaps pads and drags traces).
    Critical parts are left alone or only nudged onto the grid by at most nudge_mm: two-pin parts
    on a P/N pair's nets (series R, AC caps, TVS) as ONE rigid group, parts on nets with a net
    class, decoupling caps next to IC pins, crystals, isolation bridges (a chassis/shield net to
    another net, or pads on two different pours), and anything in `critical`.
    Never: moves a part with traces ending on its pads (skip_routed=false to allow), moves
    anything more than max_move_mm, introduces a courtyard overlap or a pad gap under the rules,
    or brings courtyards closer than rework_gap_mm (or closer than they already were). Silkscreen
    is never a reason to move. parts / region_mm [x0, y0, x1, y1] limit what may move; keep lists
    parts to leave exactly as placed (no nudge either), e.g. a deliberately placed T1.
    dry_run=true (default) writes nothing: the move_parts preview and report plus what each part
    gets and why, which parts were left alone and why, and moves dropped by the rules above.
    Run again with dry_run=false (and design=) to apply it."""
    import json
    from fusion_offline import placement_plan as PP
    root = D.read_xml(_snap(schematic=False).board_xml)
    plan = PP.tidy(root, refs=parts, region=tuple(region_mm) if region_mm else None, grid=grid_mm,
                   critical=critical or (), nudge=nudge_mm, rework_gap=rework_gap_mm, max_move=max_move_mm,
                   align=align, spread=spread, rotate=rotate, skip_routed=skip_routed, keep=set(keep or ()))
    extra = {"actions": plan["actions"], "left_alone": plan["left_alone"], "dropped": plan["dropped"],
             "critical_count": plan["critical_count"]}
    if not plan["moves"]:
        return [json.dumps({"written": False, "detail": "nothing to tidy", **extra})]
    out = _move_parts(plan["moves"], dry_run, design)
    report = json.loads(out[-1])
    report.update(extra)
    return out[:-1] + [json.dumps(report)]


_PREVIEWED_ON: dict[str, str] = {}      # part -> design its last dry run was made on (this server)


def _move_parts(moves: list[dict], dry_run: bool, design: str | None = None) -> list:
    import json
    from fusion_offline import placement_check as PC
    if not moves:
        raise ValueError("no moves given")
    active = session.require_design(design)
    snap = _snap(schematic=False)
    root = D.read_xml(snap.board_xml)
    plan = PC.resolve_moves(root, moves)
    todo = [t for t in plan if t["moves"] or t["turns"]]
    refs = [t["ref"] for t in plan]
    listing = [{"ref": t["ref"], "to": [round(t["x"], 4), round(t["y"], 4)], "angle": t["angle"],
                "side": "bottom" if t["bottom"] else "top",
                "from": [t["was"]["x"], t["was"]["y"]], "from_angle": t["was"]["angle"],
                "from_side": "bottom" if t["was"]["bottom"] else "top"} for t in todo]
    if not todo:
        return [json.dumps({"written": False, "design": active, "detail": "every part is already where asked",
                            "moves": []})]
    if dry_run:
        if active:
            _PREVIEWED_ON.update({r: active for r in refs})
        proposed = PC.apply_moves(root, plan)
        effects = PC.move_effects(root, proposed, refs)
        out = {"written": False, "design": active, "moves": listing, **effects}
        result = [json.dumps(out)]
        picture = _move_picture(proposed, plan, f"proposed: {len(todo)} part(s) moved (nothing written)")
        return ([picture] if picture else []) + result
    elsewhere = sorted({_PREVIEWED_ON[r] for r in refs if _PREVIEWED_ON.get(r) not in (None, active)})
    if elsewhere and not design:
        raise WriteFailed(f"these parts were last previewed on {', '.join(map(repr, elsewhere))} but Fusion's "
                          f"active design is now {active!r} (it was switched in between): nothing was written. "
                          "Run the dry run again on the design you mean, or pass design= to confirm it.")
    cmds = []
    for t in todo:
        if t["moves"]:
            cmds.append(C.move_part(t["ref"], t["x"], t["y"]))
        if t["turns"]:
            cmds.append(C.rotate_part(t["ref"], t["angle"], t["bottom"]))

    def verify(before, after):
        here = {e.name: e for e in after.fab().elements}
        was = {e.name: e for e in before.fab().elements}
        bad = []
        for t in todo:
            e = here.get(t["ref"])
            if e is None:
                bad.append(f"{t['ref']} is gone")
            elif not (math.isclose(e.x, t["x"], abs_tol=1e-3) and math.isclose(e.y, t["y"], abs_tol=1e-3)
                      and math.isclose(e.angle % 360, t["angle"], abs_tol=0.05) and e.mirror == t["bottom"]):
                bad.append(f"{t['ref']} at ({e.x}, {e.y}) {e.angle} {'bottom' if e.mirror else 'top'} instead of "
                           f"({t['x']:g}, {t['y']:g}) {t['angle']:g} {'bottom' if t['bottom'] else 'top'}")
        listed = {t["ref"] for t in todo}
        pushed = [n for n, e in here.items() if n not in listed and n in was and
                  (abs(e.x - was[n].x) > 1e-3 or abs(e.y - was[n].y) > 1e-3 or e.mirror != was[n].mirror
                   or abs((e.angle - was[n].angle) % 360) > 0.05)]
        if pushed:
            bad.append(f"Fusion also moved {', '.join(sorted(pushed)[:8])} (not in the list)")
        return not bad, ("; ".join(bad) if bad else f"moved {len(todo)} part(s)")
    with _IgnoreViolators():
        after, detail = session.verified_write("board", " ".join(cmds), verify, schematic=False,
                                               timeout=max(60.0, 2.0 * len(todo)), design=design or active)
    for r in refs:
        _PREVIEWED_ON.pop(r, None)
    effects = PC.move_effects(root, D.read_xml(after.board_xml), refs)
    return [json.dumps({"written": True, "design": active, "detail": detail, "moves": listing, **effects})]


def _move_picture(root, plan, title):
    """The preview picture around the moved parts (old and new places), or None without matplotlib."""
    from fusion_offline import render as R
    if not R.available():
        return None
    xs = [v for t in plan for v in (t["x"], t["was"]["x"])]
    ys = [v for t in plan for v in (t["y"], t["was"]["y"])]
    region = (min(xs) - 4, min(ys) - 4, max(xs) + 4, max(ys) + 4)
    out = os.path.join(tempfile_dir(), "fusion-electronics-mcp-move-preview.png")
    R.render(root, out, region=region, courtyards=True, title=title)
    return Image(path=out)


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


def _fab_min_width(root) -> float:
    """JLC's minimum trace width for this board's copper layer count."""
    from . import jlc
    return jlc.limit("min_trace_width", len(D.parse_board_design(root).copper_layers))


@tool(READ)
def jlc_limits() -> dict:
    """JLCPCB's published manufacturing limits that the tools check against (trace and space,
    same-net spacing, vias, annular rings, hole spacing, edge clearance, silkscreen, solder mask),
    with the page they were read from and the date. JLC changes them: check the page before
    relying on one."""
    from . import jlc
    return jlc.table()


def _class_clearances(root) -> dict[str, float]:
    """net -> its class clearance as DRC applies it (the class's design rule, else the class)."""
    from fusion_offline import net_classes as NC
    xml, _ = _v2_rules()
    by_num = {str(v["number"]): v["clearance_mm"] for v in NC.effective(root, xml).values() if v["clearance_mm"]}
    return {sg.get("name"): by_num[sg.get("class")] for sg in root.iterfind("./drawing/board/signals/signal")
            if sg.get("class") in by_num}


@tool(ADDITIVE)
def route_pair(p_net: str, n_net: str, centreline_mm: list[list[float]], width_mm: float, gap_mm: float,
               layer: str = "top", p_tail_mm: list | None = None, n_tail_mm: list | None = None,
               p_head_mm: list | None = None, n_head_mm: list | None = None,
               max_skew_mm: float = 0.1, tune: bool = True, via_drill_mm: float = 0.3,
               via_diameter_mm: float = 0.6, chamfer_mm: float = 0.5, dry_run: bool = False,
               layer_changes: list[dict] | None = None, margin_mm: float = 0.01,
               tune_style: str = "rounded", tune_radius_mm: float = 0.25, tune_flat_mm: float = 0.1,
               tune_gap_mm: float = 0.1, tune_max_height_mm: float = 0.6, tune_at: str = "longest",
               same_net_gap_mm: float = 0.25, neck_down: bool = False, add_length_mm: float = 0.0,
               group: str | None = None, detour_style: str = "45", detour_max_depth_mm: float = 1.0,
               detour_side: str = "auto", design: str | None = None) -> dict:
    """Route a differential pair as two coupled traces along a centreline you choose.

    centreline_mm: [[x, y], ...] for the middle of the pair, from near the start pads to near the
    end pads; use 45-degree bends. A point [x, y, curve] ends an arc of `curve` degrees (+ =
    counter-clockwise): both traces follow it concentrically, for rounded detours. Each trace is the centreline offset by (width + gap) / 2 with
    mitred corners, so the gap holds through bends, plus a short 45-degree fan-in to its pad.
    Which side is P is set by the start pads. If the end pads are the other way round the result
    says crossed=true: either approach the end pads from the other direction (no via), or pass a
    tail for one trace, [[x, y], {"via": [x, y]}, [x, y]], which changes layer at the via.
    p_head_mm / n_head_mm: explicit path from a trace's start pad to the trunk, for pins the
    automatic 45-degree fan-in cannot reach cleanly (e.g. through a gap in a pin row); they take
    {"via": [x, y]} entries too (both heads the same number, so the trunk is on one layer).
    layer_changes: [{"at": [x, y], "to": "bottom"}, ...] points ON the centreline where the PAIR
    changes layer together: a via pair across the pair (spaced via diameter + via clearance +
    margin, or the pair pitch if wider), each trace fanning out to its via at 45 degrees and back,
    so the pair stays coupled at the same width and gap on the new layer. Needs a little straight
    centreline either side of the point.
    Every 90-degree corner (typically where a trace leaves a pin) becomes two 45-degree bends,
    chamfer_mm along each leg (0 keeps hard corners).
    The shorter trace gets bumps on a straight run on any layer (tails after vias included)
    until the skew is within max_skew_mm; why_not_ok says why when it cannot. tune_style
    "rounded" (default) or "45"; tune_radius_mm, tune_flat_mm (the flat top), tune_gap_mm (between
    bumps) and tune_max_height_mm set the shape (used on the PoE board: 0.14 / 0.12 / 0.12);
    tune_at "mismatch" puts them on the run nearest the end whose pads cause the skew (TI
    SPRAAR7 2.3) instead of the longest run. Neighbouring legs of one trace keep at least
    same_net_gap_mm edge to edge (JLC 0.25 mm; the flat and gaps widen to keep it), and the whole
    route is checked for it.
    A conflict where a trace squeezes between two pieces of copper (e.g. between RJ45 pins) says
    so: the room there, the width that would fit and the clearance that would. neck_down=true
    narrows the trace only over each squeeze to the widest width that fits, never below JLC's
    minimum trace width for the board's layer count, and lists the necks (impedance rises there:
    check it with estimate_impedance).
    add_length_mm lengthens BOTH traces equally (matching between pairs) with coupled detours in
    the centreline: trapezoids with 45-degree legs (0.828 x depth each) or detour_style "rounded",
    at most detour_max_depth_mm deep (several if needed), on detour_side "left", "right" or "auto"
    (the side with fewer conflicts). group="MDI" (a saved length group, see set_length_group) works
    out add_length_mm itself: the group's target minus the member's whole path (its other nets,
    series parts and this route).
    Clearances are the ones DRC enforces: the board's wire rules, each other net's class
    clearance and the pair's own class clearance (from the class's design rule, else the
    class), plus margin_mm so 45-degree rounding cannot dip below them. A pair gap below its
    class clearance is a conflict; one equal to it is a warning.
    Nothing is written if the plan has conflicts (copper of other nets, holes, keepouts, the
    partner trace) or dry_run=true; the plan is returned either way. design: as in move_parts."""
    from fusion_offline import pairs as PR
    active = session.require_design(design)
    snap = _snap(schematic=False)
    root = D.read_xml(snap.board_xml)
    top, bottom = 1, 16
    start, other = (bottom, top) if str(layer).lower() == "bottom" else (top, bottom)
    net_cl = _class_clearances(root)
    pair_cl = max((net_cl.get(n, 0.0) for n in (p_net, n_net)), default=0.0) or None
    def plan_with(add):
        return PR.plan_pair(root, p_net, n_net, centreline_mm, width_mm, gap_mm, layer=start, other_layer=other,
                            p_tail=p_tail_mm, n_tail=n_tail_mm, p_head=p_head_mm, n_head=n_head_mm,
                            via_drill=via_drill_mm, via_diameter=via_diameter_mm, max_skew_mm=max_skew_mm, tune=tune,
                            chamfer_mm=chamfer_mm, layer_changes=layer_changes, net_clearance=net_cl,
                            pair_clearance=pair_cl, margin=margin_mm, tune_style=tune_style, tune_radius=tune_radius_mm,
                            tune_flat=tune_flat_mm, tune_gap=tune_gap_mm, tune_max_height=tune_max_height_mm,
                            tune_at=tune_at, same_net_gap=same_net_gap_mm, min_width=_fab_min_width(root),
                            neck_down=neck_down, add_length=add, detour_style=detour_style,
                            detour_max_depth=detour_max_depth_mm, detour_side=detour_side)
    plan = plan_with(add_length_mm)
    if group:
        from fusion_offline import length_groups as LG
        _, saved = _groups_of(design)
        if group not in saved:
            raise ValueError(f"no length group {group!r}; groups: {', '.join(sorted(saved)) or 'none'}")
        g = saved[group]
        member = next((m for m in g["members"] if isinstance(m, list) and
                       {p_net, n_net} & set(LG.path(root, m[0], g.get("follow_series", True))["nets"]
                                            + LG.path(root, m[1], g.get("follow_series", True))["nets"])), None)
        if member is None:
            raise ValueError(f"{p_net}/{n_net} is not on the path of any pair in group {group!r}")
        ev = LG.evaluate(root, g)
        others = [r["length_mm"] for r in ev["members"] if r["nets"] != member]
        target = ev["target_mm"] if g.get("target", "longest") != "longest" else max(others, default=0.0)
        now = (LG.path(root, member[0])["length_mm"] + LG.path(root, member[1])["length_mm"]) / 2
        planned = (plan["p"]["length_mm"] + plan["n"]["length_mm"]) / 2
        need = round(target - now - planned, 4)
        info = {"name": group, "member": "/".join(member), "target_mm": target, "path_without_route_mm": round(now, 3),
                "route_mm": round(planned, 3), "add_mm": max(need, 0.0)}
        if need > 1e-3:
            plan = plan_with(need)
        elif need < -(g.get("inter_tol_mm") or 0.0):
            info["note"] = (f"this route already makes the member {-need:.3f} mm longer than the group target: "
                            "shorten its path, or the others need lengthening")
        plan["group"] = info
    if dry_run or not plan["ok"]:
        return {"written": False, "design": active, "plan": plan}
    wmap = {top: _write_layer("top"), bottom: _write_layer("bottom")}
    cmds, checks = [], []
    for side in ("p", "n"):
        net = plan[side]["net"]
        for t in plan[side]["traces"]:
            pts = [tuple(q) for q in t["points"]]
            cmds.append(C.add_trace(net, wmap[t["layer"]], t.get("width", width_mm), pts))
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
    after, detail = session.verified_write("board", " ".join(cmds), verify, schematic=False, design=design or active)
    return {"written": True, "design": active, "detail": detail, "plan": plan}


@tool(ADDITIVE)
def route_trace(from_pad: str, to_pad: str, width_mm: float | None = None, layer: str = "any",
                allow_vias: bool = True, clearance_mm: float | None = None, via_drill_mm: float = 0.3,
                via_diameter_mm: float = 0.6, grid_mm: float = 0.127, dry_run: bool = True) -> dict:
    """Route one connection between two pads (PART.PAD, e.g. 'J1.A19' to 'J9.5') the way a person
    would: straight runs, 45-degree corners (no 90s), around other nets' copper with the design's
    clearance (or clearance_mm), keepouts and the board edge, ending exactly on the pad centres.
    layer: 'top' / 'bottom' to prefer one layer, 'any' to let it choose; vias only where needed
    (allow_vias=false forbids them). dry_run=true (default) returns the plan and a picture without
    writing; run again with dry_run=false to draw it (checked against the board afterwards).
    width_mm: when left out, the width the net's saved current needs (set_net_current; IPC-2221 on
    the outer layers), else 0.25 mm."""
    from fusion_offline import router as RT
    from fusion_offline.render import render
    snap = _snap(schematic=False)
    root = D.read_xml(snap.board_xml)
    a_ref, _, a_pad = from_pad.partition(".")
    b_ref, _, b_pad = to_pad.partition(".")
    ax, ay, al, an = RT.pad_target(root, a_ref, a_pad)
    bx, by, bl, bn = RT.pad_target(root, b_ref, b_pad)
    if an != bn or an is None:
        raise ValueError(f"{from_pad} is on {an!r} and {to_pad} on {bn!r}: not the same net")
    width_mm, width_note = _width_for(an, width_mm, root)
    pref = {"top": 1, "bottom": 16}.get(str(layer).lower())
    if pref and not allow_vias:
        al, bl = tuple(l for l in al if l == pref) or al, tuple(l for l in bl if l == pref) or bl
    r = RT.route(root, an, (ax, ay), al, (bx, by), bl, width=width_mm, clearance=clearance_mm,
                 prefer_layer=pref, vias=allow_vias, via_drill=via_drill_mm, via_d=via_diameter_mm, step=grid_mm)
    plan = {"p": {"net": an, "traces": [{"layer": l, "points": [list(q) for q in pts]} for l, pts in r.legs],
                  "vias": [list(v) for v in r.vias]}, "n": {"net": "", "traces": [], "vias": []}}
    out = {"route": r.as_dict(), "written": False, "width_mm": width_mm}
    if width_note:
        out["width_from"] = width_note
    xs = [q[0] for _, pts in r.legs for q in pts] + [ax, bx]
    ys = [q[1] for _, pts in r.legs for q in pts] + [ay, by]
    with contextlib.suppress(Exception):
        pic = render(root, os.path.join(tempfile_dir(), "fusion-electronics-mcp-route.png"), highlight=f"^{re.escape(an)}$",
                     region=(min(xs) - 4, min(ys) - 4, max(xs) + 4, max(ys) + 4), plans=[plan],
                     title=f"{an}: {from_pad} -> {to_pad} (plan)")
        out["picture"] = pic["path"]
    if dry_run or r.problems or not r.legs:
        return out
    wmap = {1: _write_layer("top"), 16: _write_layer("bottom")}
    cmds = ["GRID MM; SET WIRE_BEND 2;"]
    for l, pts, w in r.pieces(width_mm):
        cmds.append(C.add_trace(an, wmap[l], w, pts))
    for vx, vy in r.vias:
        cmds.append(C.add_via(an, vx, vy, via_drill_mm, via_diameter_mm))
    cmds.append("SET WIRE_BEND 1;")

    def verify(before, after):
        bad = []
        for l, pts in r.legs:
            missing, _ = _uncovered(after, an, wmap[l], pts)
            if missing:
                bad.append(f"not covered near {missing[:2]}")
        s = after.board().signals.get(an)
        for vx, vy in r.vias:
            if not (s and any(math.isclose(v.x, vx, abs_tol=1e-3) and math.isclose(v.y, vy, abs_tol=1e-3) for v in s.vias)):
                bad.append(f"via missing at ({vx}, {vy})")
        return (not bad), ("; ".join(bad) if bad else f"{an} {from_pad} -> {to_pad}: {r.length_mm:.1f} mm, {len(r.vias)} vias")
    after, detail = session.verified_write("board", " ".join(cmds), verify, schematic=False)
    out.update(written=True, detail=detail, routing=_routing_state(after))
    return out


@tool(ADDITIVE)
def route_net(net: str, width_mm: float | None = None, layer: str = "any", allow_vias: bool = True,
              clearance_mm: float | None = None, max_steps: int = 20, dry_run: bool = True) -> dict:
    """Route a whole net with route_trace's router, one airwire at a time, shortest first: each
    connection starts at a pad still unconnected and ends on the nearer of its partner pad or any
    copper the net already has (a tap), so the net grows as a tidy tree. Each step is written and
    checked before the next is planned; stops when the net has no airwires (pours count as copper).
    dry_run=true (default) plans only the first connection and returns its picture.
    width_mm: when left out, the width the net's saved current needs (set_net_current), else
    0.25 mm."""
    from fusion_offline import router as RT
    from fusion_offline import stitch as ST
    width_mm, width_note = _width_for(net, width_mm)
    pref = {"top": 1, "bottom": 16}.get(str(layer).lower())
    steps = []
    for _ in range(max_steps):
        snap = _snap(schematic=False)
        root = D.read_xml(snap.board_xml)
        sig = next((x for x in root.iterfind(".//signals/signal") if x.get("name") == net), None)
        if sig is None:
            raise ValueError(f"no net {net!r} on the board")
        air = [(float(w.get("x1")), float(w.get("y1")), float(w.get("x2")), float(w.get("y2")))
               for w in sig.iterfind("wire") if w.get("layer") == "19"]
        if not air:
            break
        air.sort(key=lambda a: math.hypot(a[2] - a[0], a[3] - a[1]))
        pads = {}
        for c in sig.iterfind("contactref"):
            with contextlib.suppress(KeyError):
                x, y, ls, _ = RT.pad_target(root, c.get("element"), c.get("pad"))
                pads[(round(x, 3), round(y, 3))] = (f"{c.get('element')}.{c.get('pad')}", ls)
        # a pad is "wired" when one of the net's traces ends on it: start from the other end
        ends_at = {(round(float(w.get(k1)), 3), round(float(w.get(k2)), 3)) for w in sig.iterfind("wire")
                   if w.get("layer") != "19" for k1, k2 in (("x1", "y1"), ("x2", "y2"))}
        plan = None
        for x1, y1, x2, y2 in air:
            k1, k2 = (round(x1, 3), round(y1, 3)), (round(x2, 3), round(y2, 3))
            ends = [pads.get(k1) if k1 not in ends_at else None, pads.get(k2) if k2 not in ends_at else None]
            exclude = set()
            if not ends[0] and not ends[1]:
                # both ends already have copper (two pieces of the net): start from the end of one
                # piece and keep the router from tapping that same piece
                exclude, s_layers = RT.fragment(root, net, (x1, y1))
                ends[0] = (pads.get(k1, (f"({x1:.2f}, {y1:.2f})", None))[0], s_layers)
            (sx, sy, s_end), (gx, gy, g_end) = ((x1, y1, ends[0]), (x2, y2, pads.get(k2))) if ends[0] else ((x2, y2, ends[1]), (x1, y1, pads.get(k1)))
            sl = s_end[1]
            # an airwire that ends on a trace: finish on that trace's layer
            gl = g_end[1] if g_end else RT.fragment(root, net, (gx, gy))[1]
            if pref and not allow_vias:
                sl = tuple(l for l in sl if l == pref) or sl
                gl = tuple(l for l in gl if l == pref) or gl
            r = RT.route(root, net, (sx, sy), sl, (gx, gy), gl, width=width_mm, clearance=clearance_mm,
                         prefer_layer=pref, vias=allow_vias, join_existing=True, tap_exclude=exclude)
            if r.legs and not r.problems:
                plan = (s_end[0], g_end[0] if g_end else f"({gx:.2f}, {gy:.2f})", r)
                break
            steps.append({"from": s_end[0], "problems": r.problems})
        if plan is None:
            break
        a, b, r = plan
        if dry_run:
            from fusion_offline.render import render
            pl = {"p": {"net": net, "traces": [{"layer": l, "points": [list(q) for q in pts]} for l, pts in r.legs],
                        "vias": [list(v) for v in r.vias]}, "n": {"net": "", "traces": [], "vias": []}}
            xs = [q[0] for _, pts in r.legs for q in pts]
            ys = [q[1] for _, pts in r.legs for q in pts]
            pic = render(root, os.path.join(tempfile_dir(), "fusion-electronics-mcp-route.png"), highlight=f"^{re.escape(net)}$",
                         region=(min(xs) - 4, min(ys) - 4, max(xs) + 4, max(ys) + 4), plans=[pl], title=f"{net}: next {a} -> {b}")
            return {"written": False, "next": {"from": a, "to": b, **r.as_dict()}, "airwires": len(air),
                    "picture": pic["path"], "skipped": steps, "width_mm": width_mm,
                    **({"width_from": width_note} if width_note else {})}
        wmap = {1: _write_layer("top"), 16: _write_layer("bottom")}
        cmds = ["GRID MM; SET WIRE_BEND 2;"] + [C.add_trace(net, wmap[l], w, pts) for l, pts, w in r.pieces(width_mm)] +                [C.add_via(net, vx, vy, 0.3, 0.6) for vx, vy in r.vias] + ["SET WIRE_BEND 1;"]
        n_air = len(air)

        def verify(before, after, n_air=n_air):
            s1 = after.board().signals.get(net)
            left = sum(1 for w in s1.wires if w.layer == 19) if s1 else 0
            joints = _layer_joints(after, {net})
            if joints:
                return False, f"{net}: top and bottom traces meet without a via at {joints[:3]}"
            return left < n_air, f"{net}: airwires {n_air} -> {left}"
        after, detail = session.verified_write("board", " ".join(cmds), verify, schematic=False)
        steps.append({"from": a, "to": b, "length_mm": round(r.length_mm, 2), "vias": len(r.vias),
                      "ends_on": r.joined, "detail": detail})
    final = _routing_state(_snap(schematic=False))
    return {"written": not dry_run, "net": net, "steps": steps, "width_mm": width_mm,
            **({"width_from": width_note} if width_note else {}),
            "net_done": net not in final["unrouted_nets"], "routing": final}


@tool(ADDITIVE)
def route_close(max_len_mm: float = 8.0, width_mm: float = 0.25, widths: dict[str, float] | None = None,
                allow_vias: bool = False, dry_run: bool = False) -> dict:
    """Route every short connection at once: each airwire up to max_len_mm (local hops: passives
    to their pins, LED + resistor, bootstrap caps), shortest first, with route_trace's router and
    no vias by default. widths: {net regex: mm} for power nets (e.g. {"^3V3|^12V|^SW$": 0.5}).
    Planned one after another on a working copy (each sees the ones before), written as one undo
    step and checked. Connections it cannot make cleanly are listed and left for later."""
    import copy
    from fusion_offline import router as RT
    snap = _snap(schematic=False)
    root = copy.deepcopy(D.read_xml(snap.board_xml))
    sigs = {x.get("name"): x for x in root.iterfind(".//signals/signal")}
    pads = {}
    for name, sig in sigs.items():
        for c in sig.iterfind("contactref"):
            with contextlib.suppress(KeyError):
                x, y, ls, _ = RT.pad_target(root, c.get("element"), c.get("pad"))
                pads[(name, round(x, 3), round(y, 3))] = (f"{c.get('element')}.{c.get('pad')}", ls)
    air = []
    pour_nets = {name for name, sig in sigs.items() if sig.find("polygon") is not None}
    for name, sig in sigs.items():
        if name in pour_nets:                 # their pours and vias connect them, not traces
            continue
        for w in sig.iterfind("wire"):
            if w.get("layer") == "19":
                a = (float(w.get("x1")), float(w.get("y1")))
                b = (float(w.get("x2")), float(w.get("y2")))
                if math.dist(a, b) <= max_len_mm:
                    air.append((math.dist(a, b), name, a, b))
    air.sort()
    width_of = lambda n: next((w for pat, w in (widths or {}).items() if re.search(pat, n)), width_mm)
    done, skipped = [], []
    wired = set()                             # (net, x, y) of pads a trace (planned or existing) ends on
    for name, sig in sigs.items():
        for w in sig.iterfind("wire"):
            if w.get("layer") != "19":
                for k in (("x1", "y1"), ("x2", "y2")):
                    wired.add((name, round(float(w.get(k[0])), 3), round(float(w.get(k[1])), 3)))
    for L, net, a, b in air:
        ka, kb = (net, round(a[0], 3), round(a[1], 3)), (net, round(b[0], 3), round(b[1], 3))
        pa, pb = pads.get(ka), pads.get(kb)
        if not pa and not pb:
            skipped.append({"net": net, "why": "neither end is a pad"})
            continue
        # start from an end that has no copper yet: a start already on a trace just "taps" itself
        if pa and ka not in wired:
            (s_pt, s_pad), (g_pt, g_pad) = (a, pa), (b, pb)
        elif pb and kb not in wired:
            (s_pt, s_pad), (g_pt, g_pad) = (b, pb), (a, pa)
        else:
            skipped.append({"net": net, "why": "both ends already have copper; left for the router"})
            continue
        w = width_of(net)
        r = RT.route(root, net, s_pt, s_pad[1], g_pt, g_pad[1] if g_pad else (1, 16), width=w,
                     vias=allow_vias, join_existing=True)
        if r.problems or not r.legs:
            skipped.append({"net": net, "from": s_pad[0], "to": g_pad[0] if g_pad else str(g_pt), "why": r.problems[:1]})
            continue
        for l, pts, lw in r.pieces(w):        # into the working copy: later routes see it
            wired.add((net, round(pts[0][0], 3), round(pts[0][1], 3)))
            wired.add((net, round(pts[-1][0], 3), round(pts[-1][1], 3)))
            for p, q in zip(pts, pts[1:]):
                ET.SubElement(sigs[net], "wire", {"x1": str(p[0]), "y1": str(p[1]), "x2": str(q[0]), "y2": str(q[1]),
                                                  "width": str(lw), "layer": str(l)})
        for vx, vy in r.vias:
            ET.SubElement(sigs[net], "via", {"x": str(vx), "y": str(vy), "extent": "1-16", "drill": "0.3", "diameter": "0.6"})
        done.append((net, s_pad[0], g_pad[0] if g_pad else str(g_pt), w, r))
    out = {"routed": [{"net": n, "from": a, "to": b, "width_mm": w, "length_mm": round(r.length_mm, 2), "vias": len(r.vias)}
                      for n, a, b, w, r in done], "skipped": skipped}
    if dry_run or not done:
        return {"written": False, **out}
    wmap = {1: _write_layer("top"), 16: _write_layer("bottom")}
    cmds = ["GRID MM; SET WIRE_BEND 2;"]
    for n, a, b, w, r in done:
        cmds += [C.add_trace(n, wmap[l], lw, pts).replace("GRID MM; ", "") for l, pts, lw in r.pieces(w)]
        cmds += [C.add_via(n, vx, vy, 0.3, 0.6).replace("GRID MM; ", "") for vx, vy in r.vias]
    cmds.append("SET WIRE_BEND 1;")
    before_air = _routing_state(snap)["unrouted_connections"]

    def verify(before, after):
        left = _routing_state(after)["unrouted_connections"]
        joints = _layer_joints(after, {n for n, *_ in done})
        if joints:
            return False, f"top and bottom traces meet without a via at {joints[:3]}"
        return left <= before_air - len(done), f"airwires {before_air} -> {left} ({len(done)} routed)"
    after, detail = session.verified_write("board", " ".join(cmds), verify, schematic=False, timeout=600)
    return {"written": True, "detail": detail, **out, "routing": _routing_state(after)}


@tool(ADDITIVE)
def route_remaining(width_mm: float = 0.25, widths: dict[str, float] | None = None, nets: list[str] | None = None,
                    allow_vias: bool = True, include_pour_nets: bool = False, dry_run: bool = True) -> dict:
    """The free-router step (after pours, GND vias, close hops, fan-outs and bus lanes): route every
    connection still unrouted, shortest first, with rip-up and reroute when one is blocked (only
    traces laid in this run are ever ripped; existing routing stays). Routes are 45-degree, keep
    clear of connector pin fields, and cost extra to run through other nets' power pours (a via is
    usually cheaper). widths: {net regex: mm} for power nets. Nets with pours are left to their
    pours unless include_pour_nets. dry_run=true (default) plans offline and returns a picture;
    dry_run=false writes it all as one undo step, checked (airwires drop, no layer change without a via)."""
    from fusion_offline import route_all as RA, router as RT
    from fusion_offline.render import render
    snap = _snap(schematic=False)
    root = D.read_xml(snap.board_xml)
    width_of = lambda n: next((w for pat, w in (widths or {}).items() if re.search(pat, n)), width_mm)
    pads = {}
    for sg in root.iterfind(".//signals/signal"):
        for c in sg.iterfind("contactref"):
            with contextlib.suppress(KeyError):
                x, y, ls, _ = RT.pad_target(root, c.get("element"), c.get("pad"))
                pads[(sg.get("name"), round(x, 3), round(y, 3))] = (f"{c.get('element')}.{c.get('pad')}", ls)
    conns = []
    for sg in root.iterfind(".//signals/signal"):
        n = sg.get("name")
        if (nets and n not in nets) or (not include_pour_nets and sg.find("polygon") is not None):
            continue
        for k, w in enumerate(sg.iterfind("wire")):
            if w.get("layer") != "19":
                continue
            a = (float(w.get("x1")), float(w.get("y1")))
            b = (float(w.get("x2")), float(w.get("y2")))
            pa, pb = pads.get((n, round(a[0], 3), round(a[1], 3))), pads.get((n, round(b[0], 3), round(b[1], 3)))
            la = pa[1] if pa else RT.fragment(root, n, a)[1]
            lb = pb[1] if pb else RT.fragment(root, n, b)[1]
            (s_, sl, sp), (g_, gl, gp) = ((a, la, pa), (b, lb, pb)) if (pa or not pb) else ((b, lb, pb), (a, la, pa))
            conns.append(RA.Conn(f"{n}#{k}", n, s_, sl, g_, gl, width_of(n),
                                 f"{sp[0] if sp else s_} -> {gp[0] if gp else g_}"))
    conns.sort(key=lambda c: math.dist(c.start, c.goal))
    res = RA.route_all(root, conns, vias=allow_vias)
    by_id = {c.id: c for c in conns}
    out = {"planned": len(res.routes), "connections": len(conns), "rips": res.rips,
           "failed": {by_id[k].label: v for k, v in res.failed.items()},
           "length_mm": round(sum(r.length_mm for r in res.routes.values()), 1),
           "vias": sum(len(r.vias) for r in res.routes.values()), "written": False}
    with contextlib.suppress(Exception):
        work = res.root
        for el in work.iter():
            el.attrib.pop("mcp_conn", None)
        out["picture"] = render(work, os.path.join(tempfile_dir(), "fusion-electronics-mcp-route-remaining.png"),
                                title="route_remaining (plan)")["path"]
    if dry_run or not res.routes:
        return out
    wmap = {1: _write_layer("top"), 16: _write_layer("bottom")}
    cmds = ["GRID MM; SET WIRE_BEND 2;"]
    for cid, r in res.routes.items():
        c = by_id[cid]
        cmds += [C.add_trace(c.net, wmap[l], w, pts).replace("GRID MM; ", "") for l, pts, w in r.pieces(c.width)]
        cmds += [C.add_via(c.net, vx, vy, 0.3, 0.6).replace("GRID MM; ", "") for vx, vy in r.vias]
    cmds.append("SET WIRE_BEND 1;")
    before_air = _routing_state(snap)["unrouted_connections"]

    def verify(before, after):
        joints = _layer_joints(after, {by_id[i].net for i in res.routes})
        if joints:
            return False, f"top and bottom traces meet without a via at {joints[:3]}"
        left = _routing_state(after)["unrouted_connections"]
        return left <= before_air - len(res.routes), f"airwires {before_air} -> {left} ({len(res.routes)} routed)"
    after, detail = session.verified_write("board", " ".join(cmds), verify, schematic=False, timeout=900)
    out.update(written=True, detail=detail, routing=_routing_state(after))
    return out


def _board_netlist(root) -> dict:
    """{'parts': {ref: {'pads': [...], 'footprint', 'value'}}, 'nets': {net: [(ref, pad)]}} from a board export."""
    parts, nets = {}, {}
    for el in root.iterfind("./drawing/board/elements/element"):
        parts[el.get("name")] = {"pads": [], "footprint": el.get("package", ""), "value": el.get("value", "")}
    for sg in root.iterfind("./drawing/board/signals/signal"):
        for c in sg.iterfind("contactref"):
            nets.setdefault(sg.get("name"), []).append((c.get("element"), c.get("pad")))
            if c.get("element") in parts:
                parts[c.get("element")]["pads"].append(c.get("pad"))
    return {"parts": {r: p for r, p in parts.items() if p["pads"]}, "nets": nets}


@tool(CHANGE)
def place_clusters(fixed: list[str] | None = None, keep: list[str] | None = None, dry_run: bool = True) -> dict:
    """Place passives around the part they serve, by rule (the user's patterns: pin -> part -> rail
    and series parts along the pin's escape, tees too, decaps standing across the column first,
    bridges along the package edge, chains such as LED + resistor following the part they hang
    off; connector pins at a board edge escape into the board). Main parts (ICs, connectors) and
    `fixed` never move; `keep` lists members to leave where they are (hand-made power stages,
    deliberate rows). dry_run=true (default) returns the moves and a before/after picture; then
    run with dry_run=false to move them (one undo step). Traces on moved parts' nets are not moved:
    rip them up first (not pour nets) and re-route with route_close."""
    from fusion_offline import cluster_place as CP, sch_plan as SP
    from fusion_offline.render import render
    snap = _snap(schematic=False)
    root = D.read_xml(snap.board_xml)
    nl = _board_netlist(root)
    plan = SP.plan(nl)
    P = CP.solve(root, plan["blocks"], set(plan["rails"]), fixed=set(fixed or ()), keep=set(keep or ()))
    moves = {r: {"x": m[0], "y": m[1], "rot": m[2], "role": P.roles.get(r)} for r, m in sorted(P.moves.items())}
    out = {"moves": moves, "not_placed": P.misses}
    with contextlib.suppress(Exception):
        out["picture"] = render(CP.applied(root, P), os.path.join(tempfile_dir(), "fusion-electronics-mcp-placement.png"),
                                traces=False, title="place_clusters (plan)")["path"]
    if dry_run or not moves:
        return {"written": False, **out}
    cmds = ["GRID MM;"]
    for r, m in moves.items():
        cmds.append(C.move_part(r, m["x"], m["y"]).replace("GRID MM; ", ""))
        cmds.append(C.rotate_part(r, m["rot"], False).replace("GRID MM; ", ""))

    def verify(before, after):
        here = {e.name: e for e in after.fab().elements}
        bad = [r for r, m in moves.items() if r not in here or not (math.isclose(here[r].x, m["x"], abs_tol=1e-3)
               and math.isclose(here[r].y, m["y"], abs_tol=1e-3) and math.isclose(here[r].angle % 360, m["rot"] % 360, abs_tol=0.05))]
        return not bad, (f"moved {len(moves)} parts" if not bad else f"not where planned: {bad[:8]}")
    after, detail = session.verified_write("board", " ".join(cmds), verify, schematic=False, timeout=300)
    return {"written": True, "detail": detail, **out}


@tool(ADDITIVE)
def lay_bus(nets: list[str], path_mm: list[list[float]], pitch_mm: float = 0.6, width_mm: float = 0.25,
            layer: str = "bottom", dry_run: bool = True) -> dict:
    """Lay a bus: one lane per net, side by side along a path you choose ([[x, y], ...], 45-degree
    corners), lane 0 on the path and lane i offset i * pitch to the LEFT of travel, each lane
    trimmed to the stretch its own pins span. Order `nets` so the taps at the ends cross as little
    as possible. Then join each pin to its lane with route_net (taps; vias where a tap must cross
    other lanes). Checked against other nets' copper, holes, keepouts and the lanes' own pitch;
    nothing is written if a lane conflicts. dry_run=true (default) returns the plan and a picture."""
    from fusion_offline import bus as BU, router as RT
    from fusion_offline.render import render
    snap = _snap(schematic=False)
    root = D.read_xml(snap.board_xml)
    lay = 16 if str(layer).lower() == "bottom" else 1
    pins = {}
    for sg in root.iterfind(".//signals/signal"):
        if sg.get("name") in nets:
            for c in sg.iterfind("contactref"):
                with contextlib.suppress(KeyError):
                    x, y, _, _ = RT.pad_target(root, c.get("element"), c.get("pad"))
                    pins.setdefault(sg.get("name"), []).append((x, y))
    plan = BU.plan_bus(root, nets, path_mm, pitch_mm, width_mm, lay, pins)
    pl = {"p": {"net": "", "traces": [{"layer": lay, "points": [list(q) for q in ln["points"]]} for ln in plan["lanes"]],
                "vias": []}, "n": {"net": "", "traces": [], "vias": []}}
    out = {"lanes": [{"net": ln["net"], "from": ln["points"][0], "to": ln["points"][-1]} for ln in plan["lanes"]],
           "problems": plan["problems"], "written": False}
    with contextlib.suppress(Exception):
        xs = [p[0] for ln in plan["lanes"] for p in ln["points"]]
        ys = [p[1] for ln in plan["lanes"] for p in ln["points"]]
        out["picture"] = render(root, os.path.join(tempfile_dir(), "fusion-electronics-mcp-bus.png"),
                                region=(min(xs) - 6, min(ys) - 6, max(xs) + 6, max(ys) + 6), plans=[pl],
                                title="lay_bus (plan)")["path"]
    if dry_run or plan["problems"]:
        return out
    wl = _write_layer(layer)
    cmds = ["GRID MM; SET WIRE_BEND 2;"] + [C.add_trace(ln["net"], wl, width_mm, ln["points"]).replace("GRID MM; ", "")
                                            for ln in plan["lanes"]] + ["SET WIRE_BEND 1;"]

    def verify(before, after):
        bad = [ln["net"] for ln in plan["lanes"] if _uncovered(after, ln["net"], wl, ln["points"])[0]]
        return not bad, (f"{len(plan['lanes'])} lanes laid" if not bad else f"lanes not as drawn: {bad}")
    after, detail = session.verified_write("board", " ".join(cmds), verify, schematic=False)
    out.update(written=True, detail=detail)
    return out


def _layer_joints(snap, nets=None) -> list[str]:
    """Places where a top trace and a bottom trace of a net meet with no via or through-hole pad
    there. Fusion's airwire count can treat these as connected, so the routing
    tools check for them explicitly."""
    from fusion_offline import router as RT
    root = D.read_xml(snap.board_xml)
    k = lambda x, y: (round(float(x), 3), round(float(y), 3))
    out = []
    for sig in root.iterfind(".//signals/signal"):
        n = sig.get("name")
        if nets is not None and n not in nets:
            continue
        ends = {}
        for w in sig.iterfind("wire"):
            if w.get("layer") in ("1", "16"):
                for p in (k(w.get("x1"), w.get("y1")), k(w.get("x2"), w.get("y2"))):
                    ends.setdefault(p, set()).add(w.get("layer"))
        if not any(len(v) > 1 for v in ends.values()):
            continue
        ok = {k(v.get("x"), v.get("y")) for v in sig.iterfind("via")}
        for c in sig.iterfind("contactref"):
            with contextlib.suppress(KeyError):
                x, y, ls, _ = RT.pad_target(root, c.get("element"), c.get("pad"))
                if len(ls) > 1:
                    ok.add(k(x, y))
        out += [f"{n} at {p}" for p, ls in ends.items() if len(ls) > 1 and p not in ok]
    return out


def tempfile_dir() -> str:
    import tempfile
    return tempfile.gettempdir()


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
    kept, BUT a ripped-up net's pours stay unfilled afterwards (RATSNEST does not refill them on
    2705.1.15): avoid ripping up nets that have pours, or re-add their pours after."""
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
             thermal_width_mm: float = 0.3, rank: int = 1) -> dict:
    """Add a copper pour (polygon) on a net, e.g. a GND plane. Without points it follows the board
    outline (inset_mm = 0): the copper-to-edge distance then comes from the design rule for board
    edge clearance, as EAGLE intends. Note the outline wire is width_mm wide and centred on the
    vertices, so an inset moves copper only inset - width/2 from the edge. isolate_mm is the
    clearance to other copper. thermal_width_mm is the width of the thermal-relief spokes joining
    pads to the pour (Fusion's default is a thin 0.1524 mm; the gap comes from the design rule
    slThermalIsolate). rank sets priority where pours of different nets overlap on a layer: rank 1
    wins and is cut out of higher ranks (e.g. output islands rank 1 inside a rank 3 GND plane).
    Respects keepouts (add_keepout); filled immediately."""
    if not 1 <= int(rank) <= 6:
        raise ValueError("rank is 1 (highest priority) to 6")
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
    # straight bends: a diagonal edge would otherwise be drawn as a 45-degree jog
    cmd = (f"GRID MM; CHANGE POUR SOLID; CHANGE ISOLATE {C.n(isolate_mm)}; CHANGE ORPHANS OFF; "
           f"CHANGE THERMALS ON; CHANGE THERMALWIDTH {C.n(thermal_width_mm)}; CHANGE RANK {int(rank)}; "
           f"LAYER {wl}; SET WIRE_BEND 2; POLYGON {C.q(net)} {C.n(width_mm)} {_poly_cmd(pts)}; "
           f"SET WIRE_BEND 1; RATSNEST;")

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
                max_vias: int = 400, keep_away_mm: dict[str, float] | None = None, under_parts: str | None = None,
                dry_run: bool = False) -> dict:
    """Via stitching for a net's pours: vias on a grid wherever they clear other nets' copper, every
    pad (no via-in-pad), holes, keepouts and the board edge, using the design's clearance and
    drill rules. keep_away_mm: {net regex: mm} keeps vias further from some nets' copper, e.g.
    {"^ETH|^USB_D": 0.6} to keep ground as far from impedance pairs as their pours are.
    under_parts: refdes regex of parts vias may go under, e.g. "J[0-9]+" for big through-hole
    connectors: under the body (an overhang to the board edge) but not in the pin field (the
    pads' box + 2 mm); other parts stay via-free.
    One call places them all (one undo step). dry_run=true only plans."""
    from fusion_offline import stitch as ST
    snap = _snap(schematic=False)
    plan = ST.plan_stitching(D.read_xml(snap.board_xml), net, pitch_mm, drill_mm, keep_away=keep_away_mm,
                             under_parts=under_parts)
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


@tool(ADDITIVE)
def ground_vias(net: str = "GND", max_dist_mm: float = 1.5, trace_width_mm: float = 0.3,
                skip: list[str] | None = None, dry_run: bool = False) -> dict:
    """A via next to every SMD pad on a plane net (GND by default): a short trace from the pad to
    the nearest clear via spot (never via-in-pad), tying the pad to the plane on the other layer.
    Planned one pad at a time so the vias keep clear of each other; all written as one undo step.
    skip: pads to leave out ('U1.4'). Through-hole pads are skipped (they reach both layers)."""
    import copy
    from fusion_offline import stitch as ST
    from fusion_offline import router as RT
    snap = _snap(schematic=False)
    root = copy.deepcopy(D.read_xml(snap.board_xml))
    sig = next((x for x in root.iterfind(".//signals/signal") if x.get("name") == net), None)
    if sig is None:
        raise ValueError(f"no net {net!r}")
    skip = set(skip or [])
    wired = {(round(float(w.get(a)), 3), round(float(w.get(b)), 3)) for w in sig.iterfind("wire")
             if w.get("layer") != "19" for a, b in (("x1", "y1"), ("x2", "y2"))}
    plans, missed = [], []
    for c in sig.findall("contactref"):
        name = f"{c.get('element')}.{c.get('pad')}"
        if name in skip:
            continue
        with contextlib.suppress(KeyError):
            x, y, layers, _ = RT.pad_target(root, c.get("element"), c.get("pad"))
            if len(layers) > 1 or (round(x, 3), round(y, 3)) in wired:   # through-hole, or already has its via
                continue
            plan = ST.plan_fanout(root, c.get("element"), c.get("pad"), net, trace_width_mm, max_dist_mm, step=0.1)
            if plan is None:
                missed.append(name)
                continue
            plans.append((name, layers[0], plan))
            v = ET.SubElement(sig, "via", {"x": str(plan["via"][0]), "y": str(plan["via"][1]),
                                           "extent": "1-16", "drill": str(plan["drill"]), "diameter": str(plan["diameter"])})
            ET.SubElement(sig, "wire", {"x1": str(plan["from"][0]), "y1": str(plan["from"][1]), "x2": str(plan["via"][0]),
                                        "y2": str(plan["via"][1]), "width": str(trace_width_mm), "layer": str(layers[0])})
    out = {"planned": len(plans), "no_spot": missed,
           "vias": [{"pad": n, "via": p["via"], "trace_mm": p["distance"]} for n, _, p in plans]}
    if dry_run or not plans:
        return {"written": False, **out}
    wmap = {1: _write_layer("top"), 16: _write_layer("bottom")}
    cmds = ["GRID MM;"]
    for n, l, p in plans:
        cmds.append(C.add_trace(net, wmap[l], trace_width_mm, [p["from"], p["via"]]).replace("GRID MM; ", ""))
        cmds.append(C.add_via(net, p["via"][0], p["via"][1], p["drill"], p["diameter"]).replace("GRID MM; ", ""))
    n0 = len(snap.board().signals[net].vias)

    def verify(before, after):
        got = len(after.board().signals[net].vias) - n0
        return got == len(plans), f"{got}/{len(plans)} {net} vias placed by their pads"
    after, detail = session.verified_write("board", " ".join(cmds), verify, schematic=False, timeout=600)
    return {"written": True, "detail": detail, **out, "routing": _routing_state(after)}


def _seg_near(sb: dict, x: float, y: float, tol: float) -> bool:
    from fusion_offline import stitch as ST
    return ST.Obstacle("seg", None, False, (sb["x1"], sb["y1"], sb["x2"], sb["y2"], 0.0)).distance(x, y) <= tol


@tool(CHANGE)
def remove_stubs(max_rounds: int = 5, geometric: bool = True) -> dict:
    """Fix trace stubs. A stub whose loose end lies on a same-net pad (Fusion re-anchors trace
    ends off-centre when a part rotates) is SNAPPED to the pad centre; a truly dangling end is cut
    back to the last place something joins the segment (a tap, a via), or the segment is deleted
    when nothing joins it before its other end. Fusion's DRC does not report every stub (a bus
    lane's tail past its last tap passes it), so with geometric=true (default) the board's copper
    is also checked directly. Every change is verified, and the run stops if the number of
    unrouted connections goes up, so a real connection is never removed."""
    from fusion_offline import stitch as ST
    fixed = []
    start_air = _routing_state(_snap(schematic=False))["unrouted_connections"]
    for _ in range(max_rounds):
        session.run("DRC;", "board")
        drc = [(e.get("x_mm"), e.get("y_mm")) for e in session.errors("board")["errors"]
               if e.get("description") == "Wire Stub" and e.get("x_mm") is not None]
        snap = _snap(schematic=False)
        cands = [sb for sb in ST.stub_candidates(D.read_xml(snap.board_xml))
                 if any(_seg_near(sb, x, y, 0.05) for x, y in drc) or (geometric and not sb["on_pad"])]
        if not cands:
            break
        for sb in cands:
            if not sb["on_pad"] and sb.get("keep_to"):
                # something joins the segment part-way: keep it up to there
                wl = _write_layer("bottom") if sb["layer"] == 16 else sb["layer"]
                dx, dy = sb["dangling"]
                ox, oy = (sb["x1"], sb["y1"]) if (sb["x2"], sb["y2"]) == (dx, dy) else (sb["x2"], sb["y2"])
                _delete_segment(sb, wl)
                add_trace(sb["net"], wl, sb["width"], [[ox, oy], list(sb["keep_to"])])
                fixed.append({"net": sb["net"], "action": "cut back to its last tap",
                              "removed_mm": round(math.dist(sb["keep_to"], (dx, dy)), 3)})
                if _routing_state(_snap(schematic=False))["unrouted_connections"] > start_air:
                    session.undo("board")
                    session.undo("board")
                    fixed[-1]["action"] = "undone: it was carrying a connection"
                    return {"ok": False, "fixed": fixed}
                continue
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
            if _routing_state(_snap(schematic=False))["unrouted_connections"] > start_air:
                session.undo("board")
                fixed[-1]["action"] = "undone: it was carrying a connection"
                return {"ok": False, "fixed": fixed}
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
def delete_copper(net: str, vias: list[list[float]] | None = None,
                  segments: list[list[float]] | None = None,
                  pours: list[list[float]] | None = None) -> dict:
    """Delete specific copper of one net without touching the rest: vias at [x, y], trace
    segments given by their end points [x1, y1, x2, y2] (mm, either order), and pours (polygons)
    given by any point [x, y] on their outline. Pours of the net that are not listed stay
    filled, which rip_up cannot do for a pour net (e.g. clearing GND vias under a part that is
    about to rotate, or moving part of a 12 V trunk). To redraw a pour that no longer fills
    properly, delete it here and add_pour it again. Each deletion is verified: exactly the
    listed copper goes, parts stay put."""
    snap = _snap(schematic=False)
    for px, py in pours or []:
        root = D.read_xml(snap.board_xml)
        sig_el = next((s for s in root.iter("signal") if s.get("name") == net), None)
        hit, best = None, 0.15
        for pg in (sig_el.findall("polygon") if sig_el is not None else []):
            pts = [(float(v.get("x")), float(v.get("y"))) for v in pg.findall("vertex")]
            for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
                vx, vy = x2 - x1, y2 - y1
                L = vx * vx + vy * vy
                t = 0 if L == 0 else max(0.0, min(1.0, ((px - x1) * vx + (py - y1) * vy) / L))
                # the export holds the outline inset by half its width, so click on that line
                # (a plain DELETE on a polygon edge only removes a corner, Shift deletes the polygon)
                d = math.hypot(px - x1 - t * vx, py - y1 - t * vy)
                if d < best:
                    hit, best, click = int(pg.get("layer")), d, (x1 + t * vx, y1 + t * vy)
        if hit is None:
            raise ValueError(f"no {net} pour has an outline through ({px}, {py})")
        wl = _write_layer("bottom") if hit in (16, 304) else hit
        pre, post = _only_layers([wl])
        count = lambda sn: sn.board().signals[net].polygons
        copper = lambda sn: (len(sn.board().signals[net].wires), len(sn.board().signals[net].vias))

        def verify_pour(before, after):
            same_parts = [(e.name, e.x, e.y) for e in before.fab().elements] == \
                         [(e.name, e.x, e.y) for e in after.fab().elements]
            ok = count(after) == count(before) - 1 and copper(after) == copper(before) and same_parts
            return ok, f"{net} pours {count(before)} -> {count(after)}"
        try:
            session.verified_write("board", f"{pre} DELETE (S {C.pt(*click)[1:]}; {post}", verify_pour, schematic=False)
        finally:
            _ensure_view()
        snap = _snap(schematic=False)
    sig = snap.board().signals.get(net)
    if sig is None:
        raise KeyError(f"no net {net!r}")
    near = lambda a, b: abs(a - b) < 0.01
    done = {"vias": [], "segments": [], "pours": [list(p) for p in pours or []]}
    for s in segments or []:
        x1, y1, x2, y2 = s
        w = next((w for w in sig.wires if w.layer != 19 and (
            (near(w.x1, x1) and near(w.y1, y1) and near(w.x2, x2) and near(w.y2, y2)) or
            (near(w.x1, x2) and near(w.y1, y2) and near(w.x2, x1) and near(w.y2, y1)))), None)
        if w is None:
            raise ValueError(f"no {net} segment from ({x1}, {y1}) to ({x2}, {y2})")
        wl = _write_layer("bottom") if w.layer == 16 else w.layer
        # click near the end fewer other wires and vias meet, so the pick cannot land on a neighbour
        meets = lambda x, y: sum(1 for o in sig.wires if o is not w and o.layer != 19 and (
            (near(o.x1, x) and near(o.y1, y)) or (near(o.x2, x) and near(o.y2, y)))) +             sum(1 for v in sig.vias if near(v.x, x) and near(v.y, y))
        loose = (w.x1, w.y1) if meets(w.x1, w.y1) < meets(w.x2, w.y2) else (w.x2, w.y2)
        _delete_segment({"net": net, "x1": w.x1, "y1": w.y1, "x2": w.x2, "y2": w.y2,
                         "dangling": loose}, wl)
        sig = _snap(schematic=False).board().signals[net]
        done["segments"].append([w.x1, w.y1, w.x2, w.y2])
    if vias:
        have = {(round(v.x, 3), round(v.y, 3)) for v in sig.vias}
        want = {(round(x, 3), round(y, 3)) for x, y in vias}
        if want - have:
            raise ValueError(f"no {net} via at {sorted(want - have)}")
        pre, post = _only_layers([18])
        cmd = pre + " " + " ".join(f"DELETE {C.pt(x, y)};" for x, y in want) + " " + post
        key = lambda sn: {(round(v.x, 3), round(v.y, 3)) for v in sn.board().signals[net].vias}

        def verify(before, after):
            gone = key(before) - key(after)
            same_parts = [(e.name, e.x, e.y) for e in before.fab().elements] == \
                         [(e.name, e.x, e.y) for e in after.fab().elements]
            return gone == want and same_parts, f"removed {len(gone)} of {len(want)} {net} vias"
        try:
            session.verified_write("board", cmd, verify, schematic=False)
        finally:
            _ensure_view()
        done["vias"] = sorted(want)
    return {"ok": True, "net": net, "deleted": done}


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
                    rotation_deg: list[float] | None = None, folder: str = "3D Packages",
                    name: str | None = None, allow_below_board: bool = False) -> dict:
    """Give a package in the OPEN library a 3D model from a STEP file. The model is placed in the
    footprint's frame (KiCad library models need no offset; pass offset_mm [x, y, z] and
    rotation_deg [rx, ry, rz] when a model's origin differs), saved as a 3D package document in
    the project's `folder`, and linked to the package. Checks that the model sits over the pads.

    The model must stand on the board: more of it above the board than below (pins may go
    through), else nothing is saved and the call fails (allow_below_board=true for a part that
    really hangs below, e.g. a through-board connector): fix rotation_deg (KiCad's 3D rotation
    signs are the opposite of Fusion's; vendor STEPs are often Y-up and need +90 about X).
    If the package already has a 3D model, the new model's file gets a versioned name
    (<package>_V2, ...) so the files in `folder` stay distinguishable. Boards that already use the
    package keep their old model until refreshed: save the library (save_design), then on each
    board run update_from_libraries(refresh_parts=[one part per device]) and push_3d."""
    if name is None:
        name = _next_3d_name(package)
    res = session.bridge.call("create_package3d", {"package": package, "step_path": step_path,
                                                    "offset_mm": offset_mm, "rotation_deg": rotation_deg,
                                                    "folder": folder, "doc_name": name,
                                                    "require_up": not allow_below_board},
                              timeout=300, answers=[(r"If you don't save", "Save")])
    mb, pb = res.get("model_bbox_mm"), res.get("pad_bbox_mm")
    if mb and not allow_below_board and (mb[5] <= 0.2 or -mb[2] > mb[5]):   # an older add-in saves unchecked
        raise WriteFailed(f"the model spans z {mb[2]} to {mb[5]} mm: mostly below the board, upside down; "
                          f"fix rotation_deg (it was saved by an older add-in: replace it). {res}")
    if mb and pb:
        mcx, mcy = (mb[0] + mb[3]) / 2, (mb[1] + mb[4]) / 2
        pcx, pcy = (pb[0] + pb[3]) / 2, (pb[1] + pb[4]) / 2     # both boxes: min xyz, max xyz
        overlap = not (mb[3] < pb[0] or mb[0] > pb[3] or mb[4] < pb[1] or mb[1] > pb[4])
        res["model_vs_pads_center_mm"] = [round(mcx - pcx, 3), round(mcy - pcy, 3)]
        if not overlap:
            res["warning"] = "the model does not overlap the pads; check offset/rotation before using it"
    linked = [n for d in res.get("devices", []) for n in d["packages3d"]]
    if not linked or (name and name not in linked and res["package"] not in linked):
        raise WriteFailed(f"the 3D package was created but is not linked to {package}: {res}")
    return {"ok": True, **res, "next": "save_design; then on each board update_from_libraries(refresh_parts=[...]) and push_3d"}


def _next_3d_name(package: str) -> str | None:
    """None (use the package name) for a package without a 3D model yet, else the next free
    <package>_V<n>."""
    try:
        devs = session.bridge.call("lib_device3d", {"match": package}, timeout=60).get("devices", [])
    except Exception:
        return None
    names = {n for d in devs if d.get("package", "").upper() == package.upper() for n in d.get("packages3d", [])}
    if not names:
        return None
    vers = [int(m.group(1)) for n in names if (m := re.match(re.escape(package) + r"_V(\d+)$", n, re.I))]
    return f"{package}_V{max(vers + [1]) + 1}"

@tool(READ)
def list_design_rules(copy_to: str | None = None) -> dict:
    """Design-rule (.edru) and stackup (.estackup) files shipped with this server for common fab
    processes (every JLCPCB 4- and 6-layer impedance stackup, and a 2-layer 1.6 mm board; built
    from JLC's published tables by tools/gen_jlc_stackups.py): copper layers, board thickness,
    dielectrics (thickness, Er) and the key clearances of each. Fusion cannot load rules from a
    script: load a .edru in the DRC dialog (Rules > Load; it carries its stackup too) or a
    .estackup in the Layer Stack Manager. copy_to copies the files into a folder you can reach
    from Fusion's file dialog (e.g. Downloads)."""
    from . import rules_lib
    out = {"rule_sets": rules_lib.catalog(),
           "how_to_load": "Fusion: DRC (Rules) > Load > pick the .edru; it sets the rules and the layer stackup. "
                          "Save the design afterwards: the working copy refreshes only after a save."}
    if copy_to:
        out["copied"] = rules_lib.copy_to(copy_to)
    return out


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
        warn.append("board edge clearance is the 40 mil Fusion default; JLC needs only 0.3 mm. Load a rule set "
                    "before pouring or routing: list_design_rules shows the bundled JLC .edru files")
    if vals.get("msDrill") == "0.35mm":
        warn.append("minimum drill is the 0.35 mm default; check it against your fab")
    from fusion_offline import rules_edit as RE
    more = RE.settings(x)
    return {"params": vals, "teardrops": more["teardrops"], "pair": more["pair"],
            "clearances_mm": more["clearances_mm"], "warnings": warn + more["warnings"]}


def _v2_rules() -> tuple[str | None, str]:
    """The board's V2 design rules XML and a note on how fresh it is (Fusion's working copy)."""
    try:
        res = session.bridge.call("design_rules", timeout=60)
    except BridgeOpError as ex:
        return None, f"design rules not readable ({ex.message})"
    return res["xml"], f"rules as Fusion last wrote them ({res.get('modified')}); unsaved rule edits may not show yet"


def _rules_base(allow_unsaved: bool):
    """(board root, V2 rules XML, how fresh, design name) to build a rule file from. Refuses while
    the design has unsaved changes: the rules are read as last saved, and loading a rule file
    replaces all rules, so rules made since the save would be lost."""
    doc = session.context().get("active_document") or {}
    if doc.get("modified") and not allow_unsaved:
        raise ValueError(f"{doc.get('name')!r} has unsaved changes. The rule file is built from the rules as last "
                         "saved, and loading it replaces all rules, so rules made since the save would be lost. "
                         "Save the design first, or pass allow_unsaved=true if no rules changed since the save.")
    root = D.read_xml(_snap(schematic=False).board_xml)
    xml, note = _v2_rules()
    if xml is None:
        raise ValueError(f"cannot build the rule file: {note}")
    return root, xml, note, doc.get("name") or "board"


def _save_rule_file(name: str, text: str, out_dir: str | None) -> str:
    from . import data_dir
    folder = out_dir or data_dir("rules")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, re.sub(r"[^\w.+-]+", "_", name) + ".edru")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


@tool(ADDITIVE)
def edit_design_rules(teardrops: dict | None = None, pair_max_length_difference_mm: float | None = None,
                      pair_gap_factor: float | None = None, clearances_mm: dict | None = None,
                      out_dir: str | None = None, allow_unsaved: bool = False) -> dict:
    """Change design rules WITHOUT touching the design (the API cannot write rules): writes a rule
    file (.edru) from the board's current rules with the changes, every class kept, to load in
    Fusion's DRC dialog (Rules > Load).
    teardrops: {"via" | "pad" | "smd" | "wire_polygon" | "all": {"auto_generate": true,
    "lengthratio": 0.5, "widthratio": 0.7, "curved_sides": true, "enabled": true}} (only the keys
    given change). pair_max_length_difference_mm: the P/N skew DRC allows in a pair (the EAGLE
    default 10 mm checks nothing useful; see length_tolerances); pair_gap_factor: Fusion's pair gap
    factor. clearances_mm: built-in clearances {"wire_wire", "wire_pad", "wire_via", "pad_pad",
    "pad_via", "via_via": mm}. Refuses while the design has unsaved changes (allow_unsaved to override).
    get_design_rules shows the current values."""
    from fusion_offline import rules_edit as RE
    root, xml, note, board_name = _rules_base(allow_unsaved)
    classes = {int(c.get("number")): c.get("name") for c in root.iterfind("./drawing/board/classes/class")}
    text, changes, unchanged = RE.edit(xml, classes, teardrops, pair_max_length_difference_mm, pair_gap_factor,
                                       clearances_mm, title=f"{board_name} (edited rules)")
    if not changes:
        return {"file": None, "changes": [], "unchanged": unchanged, "based_on": note,
                "note": "every value asked for is already in the rules: no file written"}
    path = _save_rule_file(f"{board_name} rules", text, out_dir)
    return {"file": path, "changes": changes, "unchanged": unchanged, "based_on": note,
            "next": ["In Fusion open the DRC / Design Rules dialog, Rules > Load, and pick this file.",
                     "Check with get_design_rules."]}


@tool(READ)
def list_net_classes() -> dict:
    """Net classes of the open board: number, name, width, drill, clearance, how many nets use
    each, and the DRC rules that apply to each class. Warns about added rules that are not
    scoped to a class (they apply to ALL copper; the CLASS command leaves such rules) and about
    classes whose values and DRC rules disagree, and about class width rules wider than pads on
    the class's nets (a class width rule also covers pads, so DRC flags them). A class's clearance
    is also the gap Fusion uses for differential pairs in that class."""
    from fusion_offline import net_classes as NC
    root = D.read_xml(_snap(schematic=False).board_xml)
    xml, note = _v2_rules()
    out = NC.summarize(root, xml, NC.pad_widths(root))
    out["rules_source"] = note
    return out


@tool(ADDITIVE)
def set_net_class(name: str, width_mm: float, clearance_mm: float, drill_mm: float | None = None,
                  number: int | None = None, out_dir: str | None = None, allow_unsaved: bool = False) -> dict:
    """Create or update a net class WITHOUT touching the design: writes a rule file (.edru) made
    from the board's current rules, with every existing class kept and this class's width,
    drill and clearance rules scoped to it the way Fusion writes them. Load it in Fusion:
    DRC (Design Rules) > Rules > Load, pick the file. Then check with list_net_classes and put
    nets in the class with assign_net_class. The class's clearance is also Fusion's diff-pair
    gap for that class: keep it at or below the pair gap you route with.
    (The EAGLE CLASS command is never used: on Fusion 2705 it made rules that hit ALL copper and
    survived UNDO.) Without drill_mm, the class gets no drill rule.
    The file is built from the rules as Fusion last SAVED them, and loading a rule file replaces
    all rules: with unsaved changes, rules made since the last save would be lost. So it refuses
    while the design has unsaved changes; save first (allow_unsaved=true to build it anyway)."""
    from fusion_offline import net_classes as NC
    root, xml, note, board_name = _rules_base(allow_unsaved)
    classes = [{"number": int(c.get("number")), "name": c.get("name")}
               for c in root.iterfind("./drawing/board/classes/class")]
    text, cls = NC.build_edru(xml, classes, name, width_mm, clearance_mm, drill_mm, number,
                              title=f"{board_name} + class {name}")
    path = _save_rule_file(f"{board_name} class {name}", text, out_dir)
    return {"file": path, "class": cls, "based_on": note,
            "next": ["In Fusion open the DRC / Design Rules dialog, Rules > Load, and pick this file.",
                     "Run list_net_classes: the class should show its width, drill and clearance, with matching "
                     "rules and no warnings.",
                     "Assign nets with assign_net_class."],
            "by_hand_instead": (f"Design Rules > Net Classes > New: name {name}, width {width_mm} mm, clearance "
                                f"{clearance_mm} mm" + (f", drill {drill_mm} mm" if drill_mm else "") + ".")}


def _net_class_map(root) -> dict[str, str]:
    """net -> class number, from a board or schematic export."""
    return {n.get("name"): n.get("class") or "0" for n in root.iter() if n.tag in ("signal", "net")
            and n.get("name") is not None}


@tool(CHANGE)
def assign_net_class(class_name: str, nets: list[str], design: str | None = None) -> dict:
    """Put nets in an existing net class, as ONE undo step. Each net is picked in the schematic at
    the middle of one of its own wires with no other net within 0.05 mm (CHANGE CLASS does not take
    net names), sheet by sheet. Verified afterwards in the board and schematic: every listed net
    must have the class and no other net's class may change, else it is undone. Nets with no
    pickable wire are refused before anything is written. Create the class first (set_net_class).
    design: as in move_parts."""
    from fusion_offline import net_classes as NC
    if not nets:
        raise ValueError("no nets given")
    active = session.require_design(design)
    snap = _snap()
    if snap.sch_xml is None:
        raise ValueError("no schematic is linked to this board")
    sch = D.read_xml(snap.sch_xml)
    classes = {c.get("name"): c.get("number") for c in sch.iter("class") if c.get("name") is not None}
    exact = next((n for n in classes if n.casefold() == class_name.casefold()), None)
    if exact is None:
        raise ValueError(f"no net class {class_name!r}; classes: {', '.join(sorted(classes))}. Create it with set_net_class")
    num = classes[exact]
    nets = list(dict.fromkeys(nets))
    before = _net_class_map(sch)
    todo = [n for n in nets if before.get(n) != num]
    if not todo:
        return {"ok": True, "detail": f"all {len(nets)} nets are already in {exact}", "design": active}
    points, failures = NC.pick_points(sch, todo)
    if failures:
        raise ValueError("nothing was written; cannot pick: " + "; ".join(f"{n}: {w}" for n, w in failures.items()))

    def verify(b, a):
        bad = []
        for which, x0, x1 in (("schematic", b.sch_xml, a.sch_xml), ("board", b.board_xml, a.board_xml)):
            if x0 is None or x1 is None:
                continue
            m0, m1 = _net_class_map(D.read_xml(x0)), _net_class_map(D.read_xml(x1))
            wrong = [n for n in todo if n in m1 and m1[n] != num]
            if wrong:
                bad.append(f"{which}: not in {exact}: {', '.join(wrong[:8])}")
            other = [n for n in m1 if n not in todo and m0.get(n, m1[n]) != m1[n]]
            if other:
                bad.append(f"{which}: other nets changed class too: {', '.join(sorted(other)[:8])}")
        return not bad, ("; ".join(bad) if bad else f"{len(todo)} net(s) now in {exact} (class {num})")
    after, detail = session.verified_write("schematic", NC.change_class_commands(exact, points), verify,
                                           design=design or active)
    return {"ok": True, "detail": detail, "design": active, "picked": {n: list(p) for n, p in points.items()},
            "already": [n for n in nets if n not in todo]}


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
                 plan: dict | None = None, out_path: str | None = None, courtyards: bool = False,
                 silkscreen: bool = False, pad_numbers: bool = False,
                 courtyard_ignore: list[str] | None = None) -> list:
    """Picture of the board as Fusion has it now (from a fresh export): outline, pads, holes,
    keepouts, part names, package silkscreen outlines, traces (top red solid, bottom blue dashed),
    vias, pour outlines.
    highlight_nets: regex of nets to colour and label at their pads. region_mm: [x0, y0, x1, y1]
    to zoom. plan: a route_pair plan (from dry_run) drawn on top before writing it.
    courtyards: draw each part's courtyard (tKeepout/bKeepout, layers 39/40; top teal solid,
    bottom purple dash-dot). Overlapping courtyards are outlined red with the overlap filled red;
    courtyards whose edges just touch (within 0.01 mm, not a violation) are outlined orange. The
    conflicts are summarised in the reply (overlaps listed, touching pairs counted). Use this
    before claiming parts overlap. A courtyard that wholly holds 3 or more other parts (a module
    or shield-can outline on the keepout layer) is skipped and drawn grey dotted;
    courtyard_ignore skips more parts by name.
    silkscreen: also draw silkscreen rects, polygons and part names where they are placed.
    pad_numbers: label every pad with its pad name.
    Returns the PNG and where it was saved. Needs matplotlib (optional install)."""
    from fusion_offline import render as R
    if not R.available():
        raise ValueError('render_board needs matplotlib: pip install "fusion-electronics-mcp[render]"')
    import tempfile
    root = D.read_xml(_snap(schematic=False).board_xml)
    out = out_path or os.path.join(tempfile.gettempdir(), "fusion-electronics-mcp-board.png")
    info = R.render(root, out, highlight_nets, traces, tuple(region_mm) if region_mm else None,
                    [plan] if plan else None, courtyards=courtyards, silkscreen=silkscreen,
                    pad_numbers=pad_numbers, courtyard_ignore=courtyard_ignore)
    text = (f"saved {out}; {info['traces']} trace segments, {info['vias']} vias; "
            f"highlighted {', '.join(info['highlighted_nets']) or 'none'}")
    if courtyards:
        from fusion_offline import footprints as FP
        text += "; courtyards: " + FP.summary(info["courtyard_conflicts"], info["courtyards_skipped"])
    return [Image(path=out), text]


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


@tool(READ)
def check_placement(parts: list[str] | None = None, region_mm: list[float] | None = None,
                    check_alignment: bool = False, grid_mm: dict[str, float] | None = None,
                    derive_missing_courtyards: bool = False, courtyard_margin_mm: float = 0.25,
                    derived_courtyard_from: str = "body", alignment_reach_mm: float = 5.0,
                    ignore: list[str] | None = None, top: int = 20) -> dict:
    """Check placement before routing (reads the board, changes nothing):
    - courtyards (layers 39/40): overlaps with depth; edges that just touch are counted, not
      flagged. A courtyard that wholly holds 3+ other parts (module or shield-can outline) is
      skipped, and a small part wholly inside a courtyard 4x its size or more (a marker in a
      module outline) is reported as "inside", not as an overlap. Parts whose library has no
      courtyard are listed; derive_missing_courtyards=true checks them with a box plus
      courtyard_margin_mm around derived_courtyard_from: "body" (pads and the tDocu outline,
      default), "pads" or "outline" (also silkscreen, which overstates).
    - pad gaps: pads of different parts and nets closer than the design rules (SMD-SMD,
      SMD-pad, pad-pad), using true pad shapes.
    - silkscreen on pads: silkscreen lines, shapes and part names over pad copper on the same
      side (text size is estimated).
    - tidiness: part origins off the grid (grid_mm, default {"passive": 0.125, "other": 0.25}),
      rows/columns within 0.2 mm of lining up, mixed rotations and uneven pitch among the same
      package in a row. A row whose parts each line up with pins of one neighbour follows its
      pin pitch and is listed separately, not as uneven.
    - check_alignment=true: two-pin parts linked point-to-point to a pad within
      alignment_reach_mm, judged against their better-aligned end (reported, with the other
      end), and how far they sit off that pad's row (side by side) or column (stacked).
    parts / region_mm [x0, y0, x1, y1] limit the check; ignore skips parts from the courtyard
    check. Lists are cut to `top` entries, with full counts."""
    from fusion_offline import placement_check as PC
    root = D.read_xml(_snap(schematic=False).board_xml)
    return PC.check(root, refs=parts, region=tuple(region_mm) if region_mm else None, alignment=check_alignment,
                    grid=grid_mm, derive_missing=derive_missing_courtyards, margin=courtyard_margin_mm,
                    ignore=ignore, top=top, derive_from=derived_courtyard_from, reach_mm=alignment_reach_mm)


@tool(ADDITIVE)
def import_netlist_from_kicad(pcb_path: str, part_map: dict[str, str], sheet: int = 1,
                              skip: list[str] | None = None, dry_run: bool = False, style: str = "blocks",
                              pad_map: dict[str, dict[str, str]] | None = None,
                              ground_symbol: str | None = None, power_symbol: str | None = None,
                              frame: str | None = None, preview_only: bool = False) -> dict:
    """Build the schematic of a KiCad board in this design. part_map maps a KiCad refdes OR footprint
    name to 'DEVICE@LIBRARY' (device = device set + variant), e.g. {"R1": "RES_0402_1K_1%@MY_PASSIVES",
    "RJ45-TH_RJSAE538402": "CONN_RJ45_2X1_HC-RJ45-059A@MCP Library"}. Mounting holes (no pads) are
    skipped; add them on the board with add_hole.

    style="blocks" (default) draws a reviewable schematic: each IC/connector with its passives wired
    to it (series parts inline, caps and pull-ups hanging off the net, LED/FET drivers stacked), labels
    only on nets that leave a block, ground and rails as power symbols, blocks packed onto framed
    sheets. It needs ground_symbol and power_symbol ('DEVICE@LIBRARY'; a power symbol whose net name
    follows its value, e.g. GPLIB's bars) or FUSION_MCP_GROUND_SYMBOL / FUSION_MCP_POWER_SYMBOL, and
    frame or FUSION_MCP_SHEET_FRAME for new sheets. pad_map translates KiCad pad names to library pad
    names where they differ, by refdes or footprint ({"D_SMB": {"1": "C", "2": "A"}}; one-pad parts
    map themselves). preview_only=true lays it out and writes an HTML preview without drawing it
    (parts are added once to read their symbols, then removed). Every pin is checked afterwards.
    style="grid" places parts in rows with a labelled stub on every pin (the old behaviour).
    dry_run=true reports the plan without changing anything."""
    from fusion_offline import kicad_pcb as KP
    with open(pcb_path, encoding="utf-8") as f:
        nl = KP.read_netlist(f.read())
    if style == "blocks":
        return _import_blocks(nl, part_map, skip, dry_run, pad_map, ground_symbol, power_symbol, frame, preview_only)
    if style != "grid":
        raise ValueError("style is 'blocks' or 'grid'")
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


def _import_blocks(nl, part_map, skip, dry_run, pad_map, ground_symbol, power_symbol, frame, preview_only) -> dict:
    from . import data_dir, sch_blocks as SB
    bs = SB.BlockSchematic(nl, part_map, pad_map, skip)
    plan = {"blocks": [{"anchor": b["anchor"], "members": b["members"], "labelled_nets": b["external_nets"]}
                       for b in bs.plan["blocks"]], "rails": bs.plan["rails"], "unassigned": bs.plan["unassigned"]}
    if dry_run:
        return {"ok": True, "applied": False, "parts": len(bs.parts), **plan}
    gnd = ground_symbol or os.environ.get("FUSION_MCP_GROUND_SYMBOL")
    pwr = power_symbol or os.environ.get("FUSION_MCP_POWER_SYMBOL")
    frame = frame or os.environ.get("FUSION_MCP_SHEET_FRAME")
    if not gnd or not pwr:
        raise ValueError("blocks style needs ground_symbol and power_symbol ('DEVICE@LIBRARY'), "
                         "or FUSION_MCP_GROUND_SYMBOL / FUSION_MCP_POWER_SYMBOL")
    session.activate("schematic")
    sch = _snap(board=False).schematic()
    clash = sorted(set(bs.parts) & set(sch.parts))
    if clash:
        raise ValueError(f"already in the schematic: {clash[:20]}; the blocks style draws a fresh schematic "
                         "(delete them first, or use style='grid')")
    supplies = [SB.split(gnd), SB.split(pwr)]
    script, staged = bs.stage_script(set(sch.parts), supplies)
    session.run_script(script, "schematic")
    try:
        libgeo = SB.library_geometry(D.read_xml(session.export("schematic")))
    finally:
        session.run_script(C.GRID + " EDIT .s1; " + " ".join(f"DELETE {C.q(r)};" for r in staged), "schematic")
    missing = sorted({f"{d}@{l}" for d, l in bs.spec.values() if (d, l) not in libgeo})
    if missing:
        raise ValueError(f"could not read these devices back (check names): {missing}")
    geo = {r: libgeo[k] for r, k in bs.spec.items()}
    supply_geo = {"gnd": libgeo[SB.split(gnd)], "bar": libgeo[SB.split(pwr)]}
    sheets, checks, notes, where = bs.layout(geo, supply_geo)
    os.makedirs(data_dir("previews"), exist_ok=True)
    preview = bs.preview(sheets, geo, supply_geo, data_dir("previews", "schematic-blocks.html"))
    bad = {sh: {k: v for k, v in c.items() if v and k in ("wrong", "missing", "shorts", "pin_on_wire", "wire_touch", "unlinked")}
           for sh, c in checks.items()}
    bad = {sh: v for sh, v in bad.items() if v}
    report = {"parts": len(bs.parts), "sheets": len(sheets), "blocks_on_sheets": where, "preview": preview,
              "layout_problems": bad, "crossings": sum(c["crossings"] for c in checks.values()),
              "overlaps": [o for c in checks.values() for o in c["overlaps"]][:20], "notes": notes, **plan}
    if preview_only or bad:
        return {"ok": not bad, "applied": False, **report}
    sch = _snap(board=False).schematic()
    first = 1 + max([int(m.group(1)) for p in sch.parts if (m := re.match(r"SUPPLY(\d+)$", p))] or [0])
    power = {"gnd": SB.split(gnd), "bar": SB.split(pwr), "rails": {}, "geo": supply_geo}
    place, names = bs.place_script(sheets, sch.sheets, frame, power, first)
    has_frame = frame and any(p.deviceset + p.device == SB.split(frame)[0] for p in sch.parts.values())
    interrupted = []

    def run(cmd):
        # a dialog the watchdog had to cancel stops an EAGLE script part-way: report it
        res = session.bridge.call("run_script", {"script": cmd, "editor": "schematic"}, timeout=240,
                                  forms=[dialogs.FORM_SUPPLY_VALUE],
                                  # VALUE on one power symbol: change only this one, not every
                                  # symbol with the old value
                                  answers=[(r"change all supply components with value", "No")])
        interrupted.extend(f"{d.get('title')}: {d.get('text')}"[:120] for d in res.get("dialogs") or []
                           if not d.get("expected"))

    for i, (sh, cmd) in enumerate(place):
        if i == 0 and sh == 1 and frame and not has_frame:
            cmd += f" ADD {C.q(frame)} 'FRAME1' R0 (0 0);"
        run(cmd)
    for sh, cmd in bs.wire_scripts(sheets):
        run(cmd)
    result = bs.verify(_snap(board=False).schematic())
    after = _snap(board=False).schematic()
    placed_supplies = sum(1 for p in after.parts if p in names)
    ok = result["ok"] and not interrupted and placed_supplies == len(names)
    return {"ok": ok, "applied": True, "check": result, "power_symbols": f"{placed_supplies}/{len(names)}",
            "interrupted_by_dialogs": interrupted, **report}


@tool(ADDITIVE)
def import_routing_from_kicad(pcb_path: str, nets: list[str] | None = None, vias: bool = True,
                              dry_run: bool = False) -> dict:
    """Copy a KiCad board's tracks, arcs and vias into this board (same frame as
    import_placement_from_kicad: origin at the board's bottom-left, y up), as one undo step.
    Run import_placement_from_kicad first so pads line up. nets limits it to those nets; vias=false
    skips vias. Every segment is checked against the board read back from Fusion. Pours are not
    copied (use add_pour). Import BEFORE adding pours: with pours on the board Fusion refills them
    after every via, and 490 vias kept it busy for over 40 minutes (2705.1.15). dry_run=true only
    counts what would be drawn."""
    from fusion_offline import kicad_pcb as KP
    from .sch_blocks import safe_net
    with open(pcb_path, encoding="utf-8") as f:
        r = KP.read_routing(f.read())
    want = set(nets) if nets else None
    keep = lambda n: n and (want is None or n in want)
    segs = [(safe_net(n), *rest) for n, *rest in r["segments"] if keep(n)]
    arcs = [(safe_net(n), *rest) for n, *rest in r["arcs"] if keep(n)]
    vs = [(safe_net(n), *rest) for n, *rest in r["vias"] if keep(n)] if vias else []
    snap = _snap(schematic=False)
    have = set(snap.board().signals)
    unknown = sorted({x[0] for x in segs + arcs + vs} - have)
    plan = {"segments": len(segs), "arcs": len(arcs), "vias": len(vs), "nets": len({x[0] for x in segs + arcs + vs}),
            "unknown_nets": unknown}
    if dry_run or unknown:
        if unknown and not dry_run:
            raise ValueError(f"nets not on this board: {unknown[:12]}")
        return {"ok": not unknown, "applied": False, **plan}
    layers = {lay: _write_layer(lay) for lay in {x[1] for x in segs + arcs}}
    cmds = ["GRID MM;", "SET WIRE_BEND 2;"]          # straight: arbitrary-angle segments stay as drawn
    for net, lay, w, a, b in segs:
        cmds.append(C.add_trace(net, layers[lay], w, [a, b]).replace("GRID MM; ", ""))
    for net, lay, w, a, b, ang in arcs:
        cmds.append(C.add_trace(net, layers[lay], w, [a, (b[0], b[1], ang)]).replace("GRID MM; ", ""))
    for net, x, y, drill, size in vs:
        cmds.append(C.add_via(net, x, y, drill, size))
    cmds.append("SET WIRE_BEND 1;")
    n_vias = {n: len(sg.vias) for n, sg in snap.board().signals.items()}

    def verify(before, after):
        b1 = after.board()
        short = []
        exp = {}
        for net, *_ in vs:
            exp[net] = exp.get(net, 0) + 1
        for net, k in exp.items():
            got = len(b1.signals[net].vias) - n_vias.get(net, 0)
            if got < k:
                short.append(f"{net}: {got}/{k} vias")
        miss = []
        for net, lay, w, a, b in segs:
            m, _ = _uncovered(after, net, layers[lay], [a, b])
            if m:
                miss.append(f"{net} {lay} {a}->{b}")
        ok = not short and not miss
        return ok, (f"{len(segs)} segments, {len(arcs)} arcs, {len(vs)} vias drawn" if ok
                    else f"not as drawn: {(short + miss)[:8]}")
    after, detail = session.verified_write("board", " ".join(cmds), verify, schematic=False, timeout=900)
    return {"ok": True, "applied": True, "detail": detail, **plan, "routing": _routing_state(after)}


@tool(CHANGE)
def import_placement_from_kicad(pcb_path: str, dry_run: bool = False, skip: list[str] | None = None,
                                fit_pads: bool = True) -> dict:
    """Place this board's parts where a KiCad board (.kicad_pcb) has them: position, rotation and
    side, matched by reference designator. KiCad's frame is converted (origin at the board's
    bottom-left, y up; bottom parts at angle R become mirrored 180 - R). All moves run in one
    verified command (one undo step) in Ignore Violators mode, so parts may sit over each other on
    opposite sides. dry_run=true only reports the moves. Routing is not imported.

    fit_pads (default): each part is placed so its pads land on the KiCad board's pads (matched by
    pad name, least squares), not by footprint origin and angle: a Fusion footprint whose origin
    or pin-1 orientation differs (JLC's SOT-23-6 is turned 180 degrees from KiCad's, a header's
    origin is pin 1 in one and the centre in the other) still lands right. Parts whose pads sit
    more than 0.1 mm from KiCad's after the fit are listed under footprint_differs."""
    from fusion_offline import kicad_pcb as KP
    with open(pcb_path, encoding="utf-8") as f:
        text = f.read()
    ref = KP.read_placement(text)
    snap = _snap(schematic=False)
    here = {e.name: e for e in snap.fab().elements}
    skip = set(skip or [])
    fitted, differs = {}, []
    if fit_pads:
        kpads = KP.read_pads(text)
        board = D.read_xml(snap.board_xml).find("./drawing/board")
        pkgs = {(lib.get("name"), pk.get("name")): pk for lib in board.iterfind("./libraries/library")
                for pk in lib.iterfind("./packages/package")}
        for el in board.iterfind("./elements/element"):
            n = el.get("name")
            pk = pkgs.get((el.get("library"), el.get("package")))
            if n in skip or n not in kpads or pk is None:
                continue
            fit = KP.fit_pose(KP.package_pads(pk), kpads[n]["pads"], kpads[n]["bottom"])
            if fit:
                fitted[n] = fit
                if fit["worst_mm"] > 0.1:
                    differs.append({"ref": n, "worst_pad_offset_mm": fit["worst_mm"]})
    moves, same = [], []
    for name, p in sorted(ref["parts"].items()):
        if name in skip or name not in here:
            continue
        if name in fitted:
            f_ = fitted[name]
            p = {**p, "x_mm": f_["x_mm"], "y_mm": f_["y_mm"], "angle": f_["angle"], "bottom": f_["mirror"]}
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
              "only_in_fusion": sorted(set(here) - set(ref["parts"])),
              "placed_by_pads": len(fitted), "footprint_differs": differs}
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


@tool(ADDITIVE)
def create_library_part(footprint_path: str, part_id: str, deviceset: str, prefix: str, value: str,
                        jlc_code: str | None = None, pin_names: dict[str, str] | None = None,
                        directions: dict[str, str] | None = None, description: str = "",
                        jlc_native: bool = False, manufacturer: str = "", mpn: str = "",
                        overwrite: bool = False) -> dict:
    """Create a part in this server's component library from a KiCad footprint (.kicad_mod): from
    KiCad's own libraries, or JLCPCB's footprint for a part exported with `easyeda2kicad --full
    --lcsc_id=C...` (set jlc_native=true for those: no rotation correction needed at JLC). The
    symbol is generated: two-pin passives get the standard symbols (resistor, capacitor, diode,
    LED, ...), anything else a box with its pins (pin_names maps pad -> pin name, pads sharing a
    name join one pin; directions maps pin name -> in/out/io/pwr/pas/oc). jlc_code (C-number)
    fills JLCPCB, and MF/MP from cached EasyEDA data when not given. Then open your Fusion library
    and run insert_library_part, save_design, attach_3d_model (with the part's STEP file)."""
    from fusion_offline.kicad_import import footprint_to_part
    from . import easyeda
    with open(footprint_path, encoding="utf-8") as f:
        text = f.read()
    attrs = {"VALUE": value}
    if jlc_code:
        attrs["JLCPCB"] = jlc_code.strip().upper()
        if not (manufacturer and mpn):
            r = easyeda.cached(attrs["JLCPCB"]) or {}
            c = ((r.get("dataStr") or {}).get("head") or {}).get("c_para") or {}
            manufacturer = manufacturer or (c.get("Manufacturer") or "").split("(")[0]
            mpn = mpn or c.get("Manufacturer Part") or ""
    if manufacturer:
        attrs["MF"] = manufacturer
    if mpn:
        attrs["MP"] = mpn
    part, renames = footprint_to_part(text, part_id=part_id, deviceset=deviceset, prefix=prefix,
                                      pin_names=pin_names, directions=directions, attributes=attrs,
                                      jlc_native=jlc_native, description=description,
                                      source=("easyeda " + attrs["JLCPCB"]) if jlc_native and jlc_code
                                      else f"kicad footprint {os.path.basename(footprint_path)}")
    path = library.add(part, overwrite=overwrite)
    from . import std_symbols as SS
    pk = part["package"]
    return {"ok": True, "part_id": part_id, "file": path, "package": pk["name"],
            "pads": len(pk.get("smds", [])) + len(pk.get("pads", [])), "holes": len(pk.get("holes", [])),
            "pins": len(part["symbol"]["pins"]), "symbol_style": part["symbol"].get("style") or SS.infer_style(part),
            "merged_pads": renames, "attributes": attrs,
            "next": "open your Fusion library, then insert_library_part, save_design, attach_3d_model"}


@tool(READ)
def get_library_part(part_id: str) -> dict:
    """Full definition of one library part, including metadata (source, maintainer, link)."""
    return library.get(part_id).data


@tool(ADDITIVE)
def insert_library_part(part_id: str) -> dict:
    """Build a library part into the Fusion library open in the library editor. Afterwards call
    save_design, then close_library, before placing it with add_part (placing from a library that
    is still open can crash Fusion).

    Two-pin passives (resistors, capacitors, inductors, ferrites, fuses, crystals, diodes, LEDs,
    TVS) are drawn with this server's standard symbols, not the symbol the part came with from
    EasyEDA or KiCad, so every schematic reads the same; set "style": false in the part to keep
    its own symbol, or name a style ("res", "cap", "cap_pol", "inductor", "ferrite", "fuse",
    "crystal", "diode", "schottky", "zener", "led", "tvs", "tvs_bidir")."""
    part = library.get(part_id)
    session.activate("library")
    from fusion_offline import design as D0
    existing = D0.read_xml(session.export("library"))
    up0 = lambda v: (v or "").upper()
    if any(up0(e.get("name")) == up0(part.data["deviceset"]) for e in existing.iter("deviceset")):
        raise ValueError(f"{part.data['deviceset']} is already in this library; re-running the build "
                         "script would duplicate it")
    # a package another device already drew (e.g. two ICs on the same JLC SOT-23-6) is reused
    # when its pads are the same; a different footprint under the same name is refused
    have = next((e for e in existing.iter("package") if up0(e.get("name")) == up0(part.data["package"]["name"])), None)
    if have is not None and not same_package(have, part.data["package"]):
        raise ValueError(f"package {part.data['package']['name']} is already in this library with different "
                         "pads; rename this part's package")
    session.run_script(build_script(part, reuse_package=have is not None), "library")
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
def update_from_libraries(refresh_parts: list[str] | None = None) -> dict:
    """Update the open design from all its libraries (Fusion's 'Update all'): brings in library
    changes such as attributes (e.g. JLC-ROTATION / JLC-X-OFFSET / JLC-Y-OFFSET) and 3D packages.
    Save the library and close it first.

    'Update all' can leave parts on an old 3D model and report nothing to do (seen when a
    package's model was replaced). refresh_parts (reference designators, one per device is enough, e.g. ["J3",
    "J9"]) re-pulls those parts' devices from their library with REPLACE, accepting Fusion's
    "a different version of device set ... update?" question (every part of that device follows).
    Returns the attribute changes and every part's 3D model afterwards, listing parts with none."""
    def attrs(sn):
        root = D.read_xml(sn.board_xml)
        return {e.get("name"): {a.get("name"): a.get("value") for a in e.iterfind("attribute")}
                for e in root.iterfind(".//board/elements/element")}
    session.activate("board")
    before = attrs(_snap(schematic=False))
    session.bridge.call("update_libraries", {}, timeout=300)
    refreshed, unexpected = [], []
    if refresh_parts:
        snap = _snap(schematic=True)
        broot, sroot = D.read_xml(snap.board_xml), D.read_xml(snap.sch_xml)
        els = {e.get("name"): e for e in broot.iterfind(".//board/elements/element")}
        parts = {p.get("name"): p for p in sroot.iterfind(".//parts/part")}
        for ref in refresh_parts:
            e, p = els.get(ref), parts.get(ref)
            if e is None or p is None:
                raise ValueError(f"no part {ref!r} on the board and schematic")
            spec = f"{p.get('deviceset')}{p.get('device') or ''}@{p.get('library')}"
            mirrored = (e.get("rot") or "").startswith("M")
            pre, post = _only_layers([24 if mirrored else 23])     # pick the part by its origin
            res = session.bridge.call("run", {"commands": f"GRID MM; {pre} REPLACE {C.q(spec)} "
                                                          f"{C.pt(float(e.get('x')), float(e.get('y')))}; {post}",
                                              "editor": "board"}, timeout=300,
                                      answers=[(r"already present in this file and needs to be updated", "Yes")])
            _ensure_view()
            refreshed.append({"part": ref, "device": spec, "messages": res.get("messages")})
            unexpected += [f"{d.get('title')}: {d.get('text')}"[:160] for d in res.get("dialogs") or []
                           if not d.get("expected") and d.get("title") != "REPLACE"]
    after = attrs(_snap(schematic=False))
    changed = {ref: {k: v for k, v in a.items() if before.get(ref, {}).get(k) != v}
               for ref, a in after.items() if a != before.get(ref)}
    out = {"ok": not unexpected, "parts_changed": len(changed), "changes": changed, "refreshed": refreshed,
           "unexpected_dialogs": unexpected}
    try:
        els3d = session.bridge.call("elements3d", {}, timeout=60)["elements"]
        out["models"] = {e["name"]: e["package3d"] for e in els3d}
        out["parts_without_3d_model"] = sorted(e["name"] for e in els3d if not e["package3d"])
    except Exception as ex:                      # add-in older than 0.13
        out["models"] = f"not readable: {ex}"[:200]
    out["next"] = "push_3d to bring the change into the 3D PCB"
    return out


@tool(CHANGE)
def push_3d() -> dict:
    """Bring the board's changes into its 3D PCB (creating the 3D PCB the first time, answering
    Fusion's Push dialog), then check every part's model is on its own side of the board: a
    top-side part mostly below the board (a model with the wrong up axis) is listed under
    wrong_side. The first push needs the add-in transport (the built-in server ends a script by
    cancelling any command still open)."""
    session.activate("board")
    res = session.bridge.call("push_3d", {}, timeout=900, forms=[dialogs.FORM_PUSH_3D])
    return {"ok": True, **_side_check(res)}


@tool(READ)
def check_3d_models() -> dict:
    """Check the open 3D PCB: every part's height range against the board's, listing parts whose
    model sits on the wrong side (a top-side part hanging below the board, or the reverse)."""
    return _side_check(session.bridge.call("pcb3d_bodies", {}, timeout=120))


def _side_check(res: dict) -> dict:
    board = res.get("board_z_mm")
    mirrored = set()
    with contextlib.suppress(Exception):
        root = D.read_xml(_snap(schematic=False).board_xml)
        mirrored = {e.get("name") for e in root.iterfind(".//board/elements/element")
                    if (e.get("rot") or "").startswith("M")}
    wrong = []
    if board:
        bot, top = board
        for p in res.get("parts", []):
            ref = p["occurrence"].rsplit(":", 1)[-1]
            z0, z1 = p["z_mm"]
            above, below = max(0.0, z1 - top), max(0.0, bot - z0)
            if (ref in mirrored and above > below) or (ref not in mirrored and below > above):
                wrong.append({"part": p["occurrence"], "z_mm": p["z_mm"], "board_z_mm": board})
    return {**res, "wrong_side": wrong}

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
