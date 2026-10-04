"""Group a netlist into schematic blocks: each main part (IC, connector) with the
passives that serve it, so a block can be drawn with real wires and only the
nets that leave it get labels.

Rules (deterministic, netlist only):
- anchors are ICs and connectors (U, J, IC, CN, P prefixes by default);
- rails are ground nets and nets with many pins; they never pull parts together,
  they are drawn as power symbols;
- support parts joined by non-rail nets form a sub-circuit (an LED and its
  resistor, a fuse and its bulk caps), and the sub-circuit joins the anchor it
  shares the most non-rail connections with; ties go to the anchor with fewer
  pins (the more specific part: a pull-up belongs to the controller header,
  not to the 50-pin PSU connector);
- support parts on rails only (decoupling, bulk, TVS) join the block that
  sources that rail: the block holding an inductor or anchor pin on it,
  otherwise the anchor with the most pins on it. When the netlist carries board
  positions (parts' "xy"), a part alone on its rails (a decoupling cap) joins the
  nearest anchor with a pin on its supply instead: the IC it decouples.
- rails are also nets named like supplies (3V3_AUX, +5V, V12, VCC_IO), however
  few pins they have.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

ANCHOR_PREFIXES = ("U", "J", "IC", "CN", "P", "X")
GROUND = re.compile(r"^(A|D|P|S)?GND\w*$|^VSS\w*$|^0V$", re.I)
SUPPLY = re.compile(r"^\+?\d+(\.\d+)?V\d*(_\w+)?$|^V\d+(V\d+)?$|^(VCC|VDD|VBAT|VSYS|VBUS)\w*$", re.I)


def prefix(ref: str) -> str:
    return re.match(r"[A-Za-z_]*", ref).group(0).upper()


def rails(nets: dict, min_pins: int = 10) -> set[str]:
    return {n for n, pins in nets.items() if GROUND.match(n) or SUPPLY.match(n) or len(pins) >= min_pins}


def plan(netlist: dict, anchor_prefixes=ANCHOR_PREFIXES, rail_min_pins: int = 10) -> dict:
    """netlist: {'parts': {ref: {...}}, 'nets': {net: [(ref, pad)]}} (kicad_pcb.read_netlist).
    Returns {'blocks': [{'anchor', 'members', 'local_nets', 'external_nets', 'rails'}],
    'rails': [...], 'unassigned': [...]}"""
    parts, nets = netlist["parts"], netlist["nets"]
    rail = rails(nets, rail_min_pins)
    anchors = sorted((r for r in parts if prefix(r) in anchor_prefixes), key=_natural)
    support = [r for r in parts if r not in anchors and parts[r].get("pads")]
    pins = {r: len(set(parts[r].get("pads") or [])) for r in parts}
    nets_of = defaultdict(set)
    for n, pp in nets.items():
        for r, _ in pp:
            nets_of[r].add(n)

    # sub-circuits: support parts joined by non-rail nets
    group = {r: r for r in support}

    def find(r):
        while group[r] != r:
            group[r] = group[group[r]]
            r = group[r]
        return r

    for n, pp in nets.items():
        if n in rail:
            continue
        sup = [r for r, _ in pp if r in group]
        for a, b in zip(sup, sup[1:]):
            group[find(a)] = find(b)
    subs = defaultdict(list)
    for r in support:
        subs[find(r)].append(r)

    owner: dict[str, str] = {}
    rail_only: list[list[str]] = []
    for members in subs.values():
        # each anchor pin the sub-circuit touches counts once (not once per member on that net:
        # five output caps on +5V must not hand a buck's output stage to the connector it feeds)
        score = Counter()
        for n in {n for r in members for n in nets_of[r]} - rail:
            for a, pad in set(nets[n]):
                if a in anchors:
                    score[a] += 1
        if score:
            best = max(score.values())
            a = min((a for a, s in score.items() if s == best), key=lambda a: (pins[a], _natural(a)))
            for r in members:
                owner[r] = a
        else:
            rail_only.append(members)

    def rail_source(n: str) -> str | None:
        for r, _ in nets[n]:
            if prefix(r) == "L" and r in owner:
                return owner[r]
        count = Counter(r for r, _ in nets[n] if r in anchors)
        if not count:
            return None
        best = max(count.values())
        return min((a for a, c in count.items() if c == best), key=lambda a: (-pins[a], _natural(a)))

    unassigned = []
    for members in rail_only:
        nn = sorted({n for r in members for n in nets_of[r]} - {n for n in rail if GROUND.match(n)})
        src = next((s for s in map(rail_source, nn) if s), None)
        xy = parts[members[0]].get("xy") if len(members) == 1 else None
        if xy and not any(prefix(r) == "L" for r in members):
            # the nearest anchor pin on its supply (an output cap: the block that sources the rail)
            best = None
            for n in nn:
                for r, pad in nets[n]:
                    if r in anchors:
                        q = (parts[r].get("pad_xy") or {}).get(pad) or parts[r].get("xy")
                        if q:
                            d = (q[0] - xy[0]) ** 2 + (q[1] - xy[1]) ** 2
                            if best is None or (d, _natural(r)) < best[:2]:
                                best = (d, _natural(r), r)
            sources_here = {rail_source(n) for n in nn} - {None}
            if best and not (sources_here and any(prefix(r) == "L" and r in owner
                                                     for n in nn for r, _ in nets[n])):
                src = best[2]
        for r in members:
            if src:
                owner[r] = src
            else:
                unassigned.append(r)

    sources = {n: rail_source(n) for n in rail if not GROUND.match(n)}
    blocks = []
    for a in anchors:
        members = sorted((r for r, o in owner.items() if o == a), key=_natural)
        inside = {a, *members}
        local, external, used_rails = [], [], set()
        for n in sorted({n for r in inside for n in nets_of[r]}):
            if n in rail:
                used_rails.add(n)
            elif len(nets[n]) == 1:
                # a named net on one pin (a spare GPIO, a signal unused on this board): label it so
                # the sheet shows its name; KiCad's own names for unconnected pins are left off
                if not re.match(r"(?i)^(unconnected-|net-\()", n):
                    external.append(n)
            elif all(r in inside for r, _ in nets[n]):
                local.append(n)
            else:
                external.append(n)
        blocks.append({"anchor": a, "members": members, "local_nets": local,
                       "external_nets": external, "rails": sorted(used_rails),
                       "owned_rails": sorted(n for n, s in sources.items() if s == a)})
    return {"blocks": blocks, "rails": sorted(rail), "ground": sorted(n for n in rail if GROUND.match(n)),
            "unassigned": sorted(unassigned, key=_natural)}


def _natural(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]
