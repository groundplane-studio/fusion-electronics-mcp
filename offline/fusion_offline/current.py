"""Current-carrying width (offline): IPC-2221 trace widths per layer from the copper weight,
and checks of routed copper and vias against a net's current.

IPC-2221 (the conservative chart most calculators use): I = k * dT^0.44 * A^0.725, I in A,
dT in degC rise, A the cross-section in mil^2; k = 0.048 for outer layers, 0.024 for inner
layers. Width = A / copper thickness. With k halved, inner layers need 2^(1/0.725), about 2.6x
the cross-section of an outer layer for the same current and rise, and on a stackup with 0.5 oz
inner copper (JLC 3313) about 5x the width of 1 oz outer layers.

Vias: the same formula with the inner k (the barrel is enclosed in the board) on the barrel's
plated cross-section, pi/4 * (drill^2 - (drill - 2 * plating)^2). A 0.3 mm drill with 25 um
plating at a 10 degC rise gives about 0.84 A (about 0.95 A if the plating is counted outside the
drill); with thinner plating (about 18 um) about 0.68 A. The 0.8 A per via default of check()
sits just under the 25 um figure; pass a lower via_current_a (see ipc2221_via_current) for
thinner plating.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET

from .design import parse_board_design

K_OUTER, K_INNER = 0.048, 0.024
TRANSITION_RADIUS_MM = 2.0     # vias of a net this close together share one layer change
SHORT_LINK_MM = 5.0            # or joined by one same-layer segment no longer than this


def ipc2221_width(current_a: float, delta_t_c: float, copper_mm: float, outer: bool) -> float:
    """Trace width in mm for current_a at a delta_t_c rise on copper_mm thick copper."""
    if current_a <= 0 or delta_t_c <= 0 or copper_mm <= 0:
        raise ValueError("current, temperature rise and copper thickness must be positive")
    k = K_OUTER if outer else K_INNER
    area_mil2 = (current_a / (k * delta_t_c ** 0.44)) ** (1 / 0.725)
    return area_mil2 / (copper_mm / 0.0254) * 0.0254


def ipc2221_via_current(drill_mm: float = 0.3, plating_mm: float = 0.025, delta_t_c: float = 10.0) -> float:
    """Current in A one via barrel carries at a delta_t_c rise: IPC-2221 with the inner k on the
    plated cross-section inside the drill. 0.3 mm drill, 25 um, 10 degC: about 0.84 A."""
    if drill_mm <= 0 or plating_mm <= 0 or delta_t_c <= 0 or 2 * plating_mm >= drill_mm:
        raise ValueError("drill, plating and temperature rise must be positive, plating under half the drill")
    area_mm2 = math.pi / 4 * (drill_mm ** 2 - (drill_mm - 2 * plating_mm) ** 2)
    return K_INNER * delta_t_c ** 0.44 * (area_mm2 / 0.0254 ** 2) ** 0.725


def via_transitions(vias: list, wires: list) -> list[list]:
    """Group a net's vias into layer changes: vias within TRANSITION_RADIUS_MM of each other, or
    joined end to end by one same-layer segment no longer than SHORT_LINK_MM, count as one
    transition (vias side by side sharing the current). Returns the groups, largest first."""
    n = len(vias)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(i, j):
        parent[find(i)] = find(j)

    def at(v, x, y):
        return math.hypot(v.x - x, v.y - y) <= max(v.drill, v.diameter or 0.0) / 2 + 0.05

    for i in range(n):
        for j in range(i + 1, n):
            if math.hypot(vias[i].x - vias[j].x, vias[i].y - vias[j].y) <= TRANSITION_RADIUS_MM + 1e-9:
                join(i, j)
    for w in wires:
        if w.layer == 19 or w.length > SHORT_LINK_MM:
            continue
        a = [i for i in range(n) if at(vias[i], w.x1, w.y1)]
        b = [i for i in range(n) if at(vias[i], w.x2, w.y2)]
        for i in a:
            for j in b:
                join(i, j)
    groups: dict[int, list] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(vias[i])
    return sorted(groups.values(), key=len, reverse=True)


def copper_by_layer(board_root: ET.Element, stack_copper_mm: list[float] | None = None) -> dict[int, dict]:
    """Export layer number -> {copper_mm, outer, name, source}. stack_copper_mm: the V2 stackup's
    copper thicknesses top to bottom (preferred); else the design's mtCopper; else 35 um."""
    b = parse_board_design(board_root)
    layers = b.copper_layers
    st = b.stackup()["copper_layers"]
    out = {}
    for i, n in enumerate(layers):
        if stack_copper_mm and len(stack_copper_mm) == len(layers):
            cu, src = stack_copper_mm[i], "stackup"
        elif st[i]["copper_mm"]:
            cu, src = st[i]["copper_mm"], "mtCopper (design rules)"
        else:
            cu, src = 0.035, "assumed 35 um (1 oz)"
        out[n] = {"copper_mm": cu, "outer": i in (0, len(layers) - 1), "name": st[i]["name"], "source": src}
    return out


def required(current_a: float, delta_t_c: float, copper: dict[int, dict]) -> dict[int, float]:
    return {n: round(ipc2221_width(current_a, delta_t_c, c["copper_mm"], c["outer"]), 3) for n, c in copper.items()}


def check(board_root: ET.Element, currents: dict[str, dict], copper: dict[int, dict],
          via_current_a: float = 0.8) -> list[dict]:
    """For each net with a current: the width it needs per layer, its narrowest routed segment per
    layer, undersized segments (where), and its vias. Vias are grouped into layer changes
    (via_transitions) and each change must have ceil(current / via_current_a) vias, since the
    whole current passes through each one (conservative where the net branches). via_current_a
    defaults to 1 A: IPC-2221 on a 0.3 mm, 25 um barrel at 10 degC, rounded up from about 0.9 A
    (see the module notes and ipc2221_via_current). Layers where the net has a pour are not
    judged by trace width."""
    b = parse_board_design(board_root)
    out = []
    for net, spec in sorted(currents.items()):
        s = b.signals.get(net)
        if s is None:
            out.append({"net": net, "ok": False, "problems": ["not on the board"]})
            continue
        amps, dt = float(spec["a"]), float(spec.get("delta_t_c", 10.0))
        need = required(amps, dt, copper)
        poured = {int(pg.get("layer")) for sg in board_root.iter("signal") if sg.get("name") == net
                  for pg in sg.iter("polygon")}
        if not s.vias and not poured and not any(w.layer != 19 for w in s.wires):
            out.append({"net": net, "current_a": amps, "delta_t_c": dt, "required_width_mm": need, "ok": None,
                        "status": "not routed: nothing checked", "problems": [], "unrouted": True})
            continue
        problems, narrowest = [], {}
        for w in s.wires:
            if w.layer == 19 or w.layer not in need:
                continue
            if w.layer not in narrowest or w.width < narrowest[w.layer]["width_mm"]:
                narrowest[w.layer] = {"width_mm": round(w.width, 3), "at": [round(w.x1, 3), round(w.y1, 3)]}
        for lay, nw in sorted(narrowest.items()):
            if lay in poured:
                continue
            if nw["width_mm"] < need[lay] - 1e-4:
                n_bad = sum(1 for w in s.wires if w.layer == lay and w.width < need[lay] - 1e-4)
                problems.append(f"{n_bad} segment(s) on {copper[lay]['name']} narrower than {need[lay]} mm "
                                f"(narrowest {nw['width_mm']} mm at {nw['at']})")
        layers_used = {w.layer for w in s.wires if w.layer != 19}
        vias_needed = math.ceil(amps / via_current_a - 1e-9)
        transitions = via_transitions(s.vias, s.wires) if s.vias else []
        short = [g for g in transitions if len(g) < vias_needed]
        if short:
            where = "; ".join(f"{len(g)} at {[round(g[0].x, 3), round(g[0].y, 3)]}" for g in short)
            problems.append(f"{len(short)} of {len(transitions)} layer change(s) have too few vias for {amps} A: "
                            f"about {vias_needed} needed at each, {via_current_a} A per via ({where}); "
                            "put them side by side where it changes layer")
        elif not s.vias and len(layers_used) > 1 and not poured:
            problems.append("routed on more than one layer but has no via of its own")
        out.append({"net": net, "current_a": amps, "delta_t_c": dt, "required_width_mm": need,
                    "narrowest_mm": {copper[k]["name"]: v["width_mm"] for k, v in narrowest.items()},
                    "vias": len(s.vias), "via_transitions": [len(g) for g in transitions],
                    "vias_needed_per_transition": vias_needed, "poured_layers": sorted(poured),
                    "ok": not problems, "problems": problems,
                    "unrouted": any(w.layer == 19 for w in s.wires)})
    return out
