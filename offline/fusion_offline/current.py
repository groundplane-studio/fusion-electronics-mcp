"""Current-carrying width (offline): IPC-2221 trace widths per layer from the copper weight,
and checks of routed copper and vias against a net's current.

IPC-2221 (the conservative chart most calculators use): I = k * dT^0.44 * A^0.725, I in A,
dT in degC rise, A the cross-section in mil^2; k = 0.048 for outer layers, 0.024 for inner
layers. Width = A / copper thickness. Inner layers need about twice the cross-section, and on
a stackup with 0.5 oz inner copper (JLC 3313) about 5x the width of 1 oz outer layers.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET

from .design import parse_board_design

K_OUTER, K_INNER = 0.048, 0.024


def ipc2221_width(current_a: float, delta_t_c: float, copper_mm: float, outer: bool) -> float:
    """Trace width in mm for current_a at a delta_t_c rise on copper_mm thick copper."""
    if current_a <= 0 or delta_t_c <= 0 or copper_mm <= 0:
        raise ValueError("current, temperature rise and copper thickness must be positive")
    k = K_OUTER if outer else K_INNER
    area_mil2 = (current_a / (k * delta_t_c ** 0.44)) ** (1 / 0.725)
    return area_mil2 / (copper_mm / 0.0254) * 0.0254


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
          via_current_a: float = 1.0) -> list[dict]:
    """For each net with a current: the width it needs per layer, its narrowest routed segment per
    layer, undersized segments (where), and vias against current / via_current_a. Layers where
    the net has a pour are not judged by trace width."""
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
        if s.vias and len(s.vias) < vias_needed:
            problems.append(f"{len(s.vias)} via(s) for {amps} A: about {vias_needed} needed at {via_current_a} A each "
                            "(put them side by side where it changes layer)")
        elif not s.vias and len(layers_used) > 1 and not poured:
            problems.append("routed on more than one layer but has no via of its own")
        out.append({"net": net, "current_a": amps, "delta_t_c": dt, "required_width_mm": need,
                    "narrowest_mm": {copper[k]["name"]: v["width_mm"] for k, v in narrowest.items()},
                    "vias": len(s.vias), "poured_layers": sorted(poured), "ok": not problems, "problems": problems,
                    "unrouted": any(w.layer == 19 for w in s.wires)})
    return out
