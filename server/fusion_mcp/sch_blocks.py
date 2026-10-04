"""Draw a schematic as blocks (a main part with its passives wired to it) from a
netlist, in the open Fusion design.

Steps, each verified:
1. stage: ADD every part once, off the sheet, and read the real symbol
   geometry back from the schematic export (any library works);
2. lay out offline (fusion_offline.sch_plan + sch_layout), pack the blocks onto
   framed sheets, and write an SVG preview;
3. replace the staged parts: ADD each at its final place and orientation,
   ADD the power symbols and set their values (before any wire touches them:
   Fusion asks about a power symbol whose value differs from its net's name);
4. draw the named wires, junctions and cross-reference labels;
5. read the schematic back and check every pin against the netlist.
"""

from __future__ import annotations

import os
import re

from fusion_offline import design as D, sch_layout as L, sch_plan as SP, sch_render as R, symbols as S

from . import commands as C

STAGE_X, STAGE_Y = 600.0, -150.0


def safe_net(name: str) -> str:
    return name if not name.startswith("Net-(") else re.sub(r"[^A-Za-z0-9_+-]", "_", name)[:40]


def library_geometry(root) -> dict:
    """{(DEVICESET+DEVICE, library name): Geo} from an export's embedded libraries."""
    out = {}
    for lib in root.iter("library"):
        for k, g in S.from_library_xml(lib).items():
            out[(k, lib.get("name", ""))] = g
    return out


def split(spec: str) -> tuple[str, str]:
    dev, _, lib = spec.partition("@")
    if not lib:
        raise ValueError(f"{spec!r}: expected DEVICE@LIBRARY")
    return dev, lib


def _orient(rot: int, mir: bool) -> str:
    return f"{'M' if mir else ''}R{int(rot) % 360}"


def normalise(d: L.Drawing, geo: dict) -> None:
    """Mirrored two-pin parts become an unmirrored rotation with the same pin points
    (text stays readable); multi-pin parts keep their mirror."""
    for ref, (x, y, rot, mir) in list(d.parts.items()):
        g = geo[ref]
        if not mir or len(g.pins) > 2:
            continue
        want = {n: D.transform(p.x, p.y, x, y, rot, mir) for n, p in g.pins.items()}
        for r2 in S.ROTS:
            # same pins with rotation r2 and no mirror: origin offset from the first pin
            first = next(iter(g.pins.values()))
            fx, fy = D.transform(first.x, first.y, 0, 0, r2, False)
            ox, oy = want[first.name][0] - fx, want[first.name][1] - fy
            got = {n: D.transform(p.x, p.y, ox, oy, r2, False) for n, p in g.pins.items()}
            if all(abs(got[n][0] - want[n][0]) < 0.01 and abs(got[n][1] - want[n][1]) < 0.01 for n in g.pins):
                d.parts[ref] = (round(ox, 4), round(oy, 4), r2, False)
                break


class BlockSchematic:
    def __init__(self, netlist: dict, part_map: dict, pad_map: dict | None = None, skip=None):
        self.parts = {r: p for r, p in netlist["parts"].items() if p["pads"] and r not in set(skip or [])}
        self.netlist = {"parts": self.parts,
                        "nets": {safe_net(n): [x for x in v if x[0] in self.parts] for n, v in netlist["nets"].items()}}
        self.part_map = part_map
        self.pad_map = pad_map or {}
        self.spec = {}
        unmapped = []
        for r, p in self.parts.items():
            s = part_map.get(r) or part_map.get(p["footprint"])
            if not s:
                unmapped.append(r)
            else:
                self.spec[r] = split(s)
        if unmapped:
            raise ValueError(f"no part_map entry for {sorted(unmapped)} (map their refdes or footprint)")
        self.plan = SP.plan(self.netlist)

    # -- step 1 ------------------------------------------------------------
    def stage_script(self, existing: set, supplies: list[tuple[str, str]]) -> tuple[str, list[str]]:
        cmds, staged = [C.GRID, "EDIT .s1;"], []
        i = 0
        for r in sorted(self.parts):
            if r in existing:
                continue
            dev, lib = self.spec[r]
            cmds.append(f"ADD {C.q(dev + '@' + lib)} {C.q(r)} R0 {C.pt(STAGE_X + 40 * (i % 10), STAGE_Y - 40 * (i // 10))};")
            staged.append(r)
            i += 1
        for k, (dev, lib) in enumerate(supplies):
            name = f"MCPSTAGE{k + 1}"
            cmds.append(f"ADD {C.q(dev + '@' + lib)} {C.q(name)} R0 {C.pt(STAGE_X - 40, STAGE_Y - 40 * k)};")
            staged.append(name)
        return " ".join(cmds), staged

    def pad_nets(self, geo: dict) -> tuple[dict, list[str]]:
        """(ref, library pad) -> net, translating KiCad pad names: pad_map (by refdes or
        footprint), else identical names, else a lone pad."""
        out, problems = {}, []
        for n, pp in self.netlist["nets"].items():
            for r, pad in pp:
                g = geo[r]
                m = self.pad_map.get(r) or self.pad_map.get(self.parts[r]["footprint"]) or {}
                lp = m.get(pad, pad)
                if lp not in g.pad_pin:
                    if len(g.pad_pin) == 1 and len(set(self.parts[r]["pads"])) == 1:
                        lp = next(iter(g.pad_pin))
                    else:
                        problems.append(f"{r} pad {pad}: the library device has pads {sorted(g.pad_pin)}; add a pad_map entry")
                        continue
                out[(r, lp)] = n
        return out, problems

    # -- step 2 ------------------------------------------------------------
    def layout(self, geo: dict, supply_geo: dict, area: L.SheetArea | None = None):
        pad_net, problems = self.pad_nets(geo)
        if problems:
            raise ValueError("; ".join(problems[:12]))
        values = {r: p["value"] for r, p in self.parts.items()}
        ground, rails = set(self.plan["ground"]), set(self.plan["rails"])
        blocks, notes = [], []
        for b in self.plan["blocks"]:
            refs = {b["anchor"], *b["members"]}
            ctx = L.Ctx(geo, pad_net, values, refs, b["anchor"], ground, rails, set(b["owned_rails"]),
                        set(b["external_nets"]), supply_geo)
            d = L.layout_block(ctx)
            normalise(d, geo)
            blocks.append((b["anchor"], d))
            notes += [f"{b['anchor']}: {x}" for x in ctx.notes]
        if self.plan["unassigned"]:
            notes.append(f"parts in no block (not drawn): {self.plan['unassigned']}")
        spots = L.pack(blocks, area)
        sheets: dict[int, L.Drawing] = {}
        for (name, d), (sh, dx, dy) in zip(blocks, spots):
            sheets.setdefault(sh, L.Drawing()).merge(d.moved(dx, dy))
        checks = {sh: L.check(d, geo, pad_net, set(d.parts), supply_geo) for sh, d in sheets.items()}
        self.pad_net, self.values, self.geo = pad_net, values, geo
        return sheets, checks, notes, [(n, sh) for (n, _), (sh, _, _) in zip(blocks, spots)]

    def preview(self, sheets: dict, geo: dict, supply_geo: dict, path: str) -> str:
        pages = []
        for sh, d in sorted(sheets.items()):
            dd = L.Drawing().merge(d)
            dd.boxes.append((0.0, 0.0, 431.8, 279.4, "frame:"))
            pages.append(f"<h3 style='font-family:sans-serif'>Sheet {sh}</h3>" +
                         R.render_svg(dd, geo, self.values, supply_geo, scale=3.0, pad=0))
        with open(path, "w", encoding="utf-8") as f:
            f.write("<!doctype html><meta charset=utf-8><body style='background:#eee;margin:10px'>" + "".join(pages))
        return path

    # -- steps 3 and 4 -----------------------------------------------------
    def place_script(self, sheets: dict, existing_sheets: int, frame: str | None, power: dict,
                     first_supply: int) -> tuple[list[tuple[int, str]], dict]:
        """Per-sheet ADD scripts (parts, power symbols + values). power: {'gnd': (dev, lib),
        'bar': (dev, lib), 'rails': {net: (dev, lib)}}. Returns scripts and supply names."""
        out, names = [], {}
        k = first_supply
        for sh, d in sorted(sheets.items()):
            cmds, values = [C.GRID], [C.GRID, f"EDIT .s{sh};"]
            if sh > existing_sheets:
                cmds.append(C.new_sheet(sh, "", frame, f"FRAME{sh}"))
            else:
                cmds.append(f"EDIT .s{sh};")
            for ref, (x, y, rot, mir) in sorted(d.parts.items()):
                dev, lib = self.spec[ref]
                cmds.append(f"ADD {C.q(dev + '@' + lib)} {C.q(ref)} {_orient(rot, mir)} {C.pt(x, y)};")
            for net, kind, x, y, facing in d.supplies:
                dev, lib = power["rails"].get(net) or power[kind]
                g = power["geo"][kind]
                pin = next(iter(g.pins))
                ox, oy, rot, mir = S.solve(g, pin, (x, y), (-facing[0], -facing[1]))
                name = f"SUPPLY{k}"
                k += 1
                cmds.append(f"ADD {C.q(dev + '@' + lib)} {C.q(name)} {_orient(rot, mir)} {C.pt(ox, oy)};")
                default_value = "GND" if kind == "gnd" else dev
                if net not in power["rails"] and net != default_value:
                    values.append(f"VALUE {C.q(name)} {C.q(net)};")
                names[name] = net
            out.append((sh, " ".join(cmds)))
            # VALUE in the same script as the ADDs opens Fusion's Value dialog (seen on
            # 2705.1.15; cancelling it aborts the script); on its own it does not
            if len(values) > 2:
                out.append((sh, " ".join(values)))
        return out, names

    def wire_scripts(self, sheets: dict, geo: dict | None = None) -> list[tuple[int, str]]:
        geo = geo if geo is not None else getattr(self, "geo", None)
        out = []
        for sh, d in sorted(sheets.items()):
            cmds = [C.GRID, f"EDIT .s{sh};"]
            count: dict = {}
            wires = _snap_to_pins(d, geo) if geo else d.wires
            for net, pts in split_at_ends(wires):
                if len(pts) < 2 or all(abs(p[0] - pts[0][0]) < 1e-6 and abs(p[1] - pts[0][1]) < 1e-6 for p in pts):
                    continue
                # one NET per segment: a NET stops where it meets an existing wire (EAGLE),
                # so a polyline through a junction drawn earlier would be cut short there
                for a, b in zip(pts, pts[1:]):
                    if abs(a[0] - b[0]) > 1e-6 or abs(a[1] - b[1]) > 1e-6:
                        cmds.append(f"NET {C.q(net)} {C.pt(*a)} {C.pt(*b)};")
                for i, p in enumerate(pts):
                    key = (round(p[0], 3), round(p[1], 3))
                    count[key] = count.get(key, 0) + (1 if i in (0, len(pts) - 1) else 2)
            for (x, y), c in sorted(count.items()):
                if c >= 3:
                    cmds.append(f"JUNCTION {C.pt(x, y)};")
            if d.labels:
                cmds.append(f"CHANGE XREF ON; CHANGE SIZE {L.LABEL_SIZE};")
                for net, x, y, dr in d.labels:
                    pick = _pick_point(d, net, x, y)
                    if pick is None:
                        continue
                    cmds.append(f"LABEL {'R0' if dr > 0 else 'R180'} {C.pt(*pick)} {C.pt(x, y)};")
                cmds.append("CHANGE XREF OFF;")
            out.append((sh, " ".join(cmds)))
        return out

    # -- step 5 ------------------------------------------------------------
    def verify(self, sch) -> dict:
        wrong, missing = [], []
        for (r, pad), want in self.pad_net.items():
            pin = next((p for p in sch.pins if p.part == r and p.pad and pad in p.pad.split()), None)
            if pin is None:
                missing.append(f"{r} pad {pad}: not in the schematic")
                continue
            got = sch.net_of(r, pin.pin)
            if got != want:
                wrong.append(f"{r}.{pin.pin}: {got or 'unconnected'} (want {want})")
        return {"ok": not wrong and not missing, "wrong": wrong[:40], "missing": missing[:40],
                "pins_checked": len(self.pad_net)}


def _snap_to_pins(d: L.Drawing, geo: dict, tol: float = 0.002) -> list:
    """Wire points within `tol` of a pin, moved exactly onto it. Moving a block rounds the part
    and its wires separately, which can leave a wire end 0.0001 mm off a pin: Fusion does not
    connect that (seen on a FET gate)."""
    from fusion_offline.design import transform
    pins = []
    for ref, (x, y, rot, mir) in d.parts.items():
        if ref in geo:
            pins += [transform(p.x, p.y, x, y, rot, mir) for p in geo[ref].pins.values()]
    out = []
    for net, pts in d.wires:
        new = []
        for p in pts:
            q = next((q for q in pins if abs(q[0] - p[0]) <= tol and abs(q[1] - p[1]) <= tol), None)
            new.append(q if q else p)
        out.append((net, new))
    return out


def split_at_ends(wires: list) -> list:
    """Cut every segment where another wire of the same net ends on it, so each joint is a
    wire end (Fusion's ERC flags a wire that ends part-way along another as not visibly
    connected, code 115)."""
    ends: dict = {}
    for net, pts in wires:
        for p in (pts[0], pts[-1]):
            ends.setdefault(net, set()).add((round(p[0], 4), round(p[1], 4)))
    out = []
    for net, pts in wires:
        for a, b in zip(pts, pts[1:]):
            cut = [e for e in ends.get(net, ()) if e != (round(a[0], 4), round(a[1], 4)) and
                   e != (round(b[0], 4), round(b[1], 4)) and L._on_segment(e[0], e[1], a, b, tol=0.005)]
            cut.sort(key=lambda e: (e[0] - a[0]) ** 2 + (e[1] - a[1]) ** 2)
            chain = [a, *cut, b]
            out += [(net, [p, q]) for p, q in zip(chain, chain[1:])]
    return out


def _pick_point(d: L.Drawing, net: str, x: float, y: float):
    """A point on the wire that ends at (x, y), 0.5 mm in from the end (LABEL picks the wire there)."""
    for n, pts in d.wires:
        if n != net:
            continue
        for a, b in zip(pts, pts[1:]):
            for p, q in ((a, b), (b, a)):
                if abs(p[0] - x) < 0.01 and abs(p[1] - y) < 0.01:
                    L_ = ((q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2) ** 0.5
                    if L_ < 0.01:
                        continue
                    t = min(0.5, L_ / 2) / L_
                    return (round(p[0] + (q[0] - p[0]) * t, 4), round(p[1] + (q[1] - p[1]) * t, 4))
    return None
