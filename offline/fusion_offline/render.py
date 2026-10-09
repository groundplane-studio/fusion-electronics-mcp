"""Render a board (EAGLE XML export) to PNG for review and route planning.

Draws what the export holds, not what a tool intended: outline, pads (by
net), holes, keepouts, part names, package silkscreen outlines, traces by
layer, vias, pour outlines, and optionally planned pair routes (route_pair
plans) on top. Optional layers: courtyards (39/40, with overlaps filled red
and touching pairs outlined orange), the rest of the silkscreen (rects,
polygons, part names as placed) and pad numbers. Needs
matplotlib, an optional dependency: pip install "fusion-electronics-mcp[render]".
"""

from __future__ import annotations

import math
import textwrap
import re
import xml.etree.ElementTree as ET

from . import footprints as FP
from .eagle import parse_rot
from .pairs import flatten
from .stitch import board_obstacles

LAYER_STYLE = {1: ("#c0392b", "-"), 16: ("#2e5fb8", "--"), 2: ("#c98a00", "-"), 15: ("#2e9e5b", "-")}


def available() -> bool:
    try:
        import matplotlib  # noqa: F401
        return True
    except ImportError:
        return False


def render(root: ET.Element, out_path: str, highlight: str | None = None, traces: bool = True,
           region: tuple[float, float, float, float] | None = None, plans: list[dict] | None = None,
           title: str | None = None, courtyards: bool = False, silkscreen: bool = False,
           pad_numbers: bool = False, courtyard_ignore: list[str] | None = None) -> dict:
    """Write a PNG of the board to out_path. highlight: regex of nets to
    colour and label at their pads. region: (x0, y0, x1, y1) mm to zoom.
    Returns a short legend of what was drawn (with courtyard conflicts when
    courtyards are drawn)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle, Polygon

    board = root.find("./drawing/board")
    obs, _, outline = board_obstacles(root)
    hl = re.compile(highlight) if highlight else None
    nets = sorted({o.net for o in obs if o.net and hl and hl.search(o.net)})
    cmap = plt.get_cmap("tab20")
    col = {n: cmap(i % 20) for i, n in enumerate(nets)}
    xs = [v for s in outline for v in (s[0], s[2])] or [0, 1]
    ys = [v for s in outline for v in (s[1], s[3])] or [0, 1]
    x0, y0, x1, y1 = region or (min(xs), min(ys), max(xs), max(ys))
    w, h = max(x1 - x0, 1), max(y1 - y0, 1)
    scale = 14 / max(w, h)
    fig, ax = plt.subplots(figsize=(max(w * scale, 4), max(h * scale, 4) + 0.6), dpi=110)
    pt_per_mm = fig.get_size_inches()[0] * 72 * 0.85 / (w + 2)   # line widths in points for true copper width
    for a, b, c, d in outline:
        ax.plot([a, c], [b, d], "k-", lw=1.5)
    for o in obs:
        if not o.is_pad and o.kind == "circle" and o.net is None:
            continue
        if not o.is_pad and o.kind in ("seg", "circle"):
            continue  # traces and vias are drawn from the signals below
        c = col.get(o.net, (0.75, 0.75, 0.75)) if o.net else (0.5, 0.5, 0.5)
        if o.kind == "seg":                     # oblong pad
            x1_, y1_, x2_, y2_, hw = o.data
            ax.plot([x1_, x2_], [y1_, y2_], "-", color=c, lw=2 * hw * pt_per_mm, solid_capstyle="round")
        elif o.kind == "circle":
            ax.add_patch(Circle(o.data[:2], o.data[2], fc=c, ec="k", lw=0.3))
        else:
            cx, cy, hw, hh, a = o.data
            r = math.radians(a)
            ax.add_patch(Polygon([(cx + dx * math.cos(r) - dy * math.sin(r), cy + dx * math.sin(r) + dy * math.cos(r))
                                  for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))], fc=c, ec="k", lw=0.3))
    for hole in board.iterfind("./plain/hole"):
        ax.add_patch(Circle((float(hole.get("x")), float(hole.get("y"))), float(hole.get("drill")) / 2,
                            fc="w", ec="r", lw=1))
    for c in board.iterfind("./plain/circle"):
        if c.get("layer") in ("41", "42", "43"):
            ax.add_patch(Circle((float(c.get("x")), float(c.get("y"))), float(c.get("radius")),
                                fc="none", ec="r", ls="--", lw=0.6))
    # silkscreen: package outlines (tPlace/bPlace) and board text, in true size
    pkgs = {(lib.get("name"), pk.get("name")): pk for lib in board.iterfind("./libraries/library")
            for pk in lib.iterfind("./packages/package")}
    silk = {"21": "#7a5c00", "22": "#5c5c9e"}
    for el in board.iterfind("./elements/element"):
        pk = pkgs.get((el.get("library"), el.get("package")))
        if pk is None:
            continue
        ang, mir = parse_rot(el.get("rot"))
        ex, ey = float(el.get("x")), float(el.get("y"))
        a = math.radians(ang)
        for sw in pk.iterfind("wire"):
            lay = sw.get("layer")
            if lay not in ("21", "22"):
                continue
            lay = {"21": "22", "22": "21"}[lay] if mir else lay
            pts = []
            for kx, ky in (("x1", "y1"), ("x2", "y2")):
                px, py = float(sw.get(kx)), float(sw.get(ky))
                rx, ry = px * math.cos(a) - py * math.sin(a), px * math.sin(a) + py * math.cos(a)
                pts.append((ex + (-rx if mir else rx), ey + ry))
            ax.plot([pts[0][0], pts[1][0]], [pts[0][1], pts[1][1]], "-", color=silk[lay], lw=0.6, alpha=0.8)
        for sc in pk.iterfind("circle"):
            lay = sc.get("layer")
            if lay not in ("21", "22"):
                continue
            lay = {"21": "22", "22": "21"}[lay] if mir else lay
            px, py = float(sc.get("x")), float(sc.get("y"))
            rx, ry = px * math.cos(a) - py * math.sin(a), px * math.sin(a) + py * math.cos(a)
            w0 = float(sc.get("width") or 0)
            ax.add_patch(Circle((ex + (-rx if mir else rx), ey + ry), float(sc.get("radius")),
                                fc=silk[lay] if w0 == 0 else "none", ec=silk[lay], lw=0.6))
    for t in board.iterfind("./plain/text"):
        if t.get("layer") not in ("21", "22"):
            continue
        tr, tm = parse_rot(t.get("rot"))
        al = (t.get("align") or "bottom-left").split("-")
        va = {"bottom": "bottom", "center": "center", "top": "top"}[al[0]]
        ha = {"left": "left", "center": "center", "right": "right"}[al[-1]] if len(al) > 1 else "center"
        ax.text(float(t.get("x")), float(t.get("y")), t.text or "", fontsize=float(t.get("size") or 1) * pt_per_mm * 0.95,
                rotation=tr, ha=ha, va=va, color=silk[t.get("layer")], family="monospace", weight="bold",
                clip_on=True)
    if silkscreen:
        for it in FP.silkscreen(root):
            c = silk[it["layer"]]
            if it["kind"] in ("rect", "polygon"):
                ax.add_patch(Polygon(it["points"], fc=c, ec=c, lw=0.4, alpha=0.7))
            else:
                al = it["align"].split("-")
                va = {"bottom": "bottom", "center": "center", "top": "top"}.get(al[0], "bottom")
                ha = {"left": "left", "center": "center", "right": "right"}.get(al[-1], "center") if len(al) > 1 else "center"
                ax.text(it["x"], it["y"], it["text"], fontsize=it["size"] * pt_per_mm * 0.95, rotation=it["angle"],
                        rotation_mode="anchor", ha=ha, va=va, color=c, family="monospace", clip_on=True)
    conflicts, skipped = [], {}
    if courtyards:
        cys = FP.courtyards(root)
        skipped = FP.enclosing(cys)
        ignored = set(skipped) | set(courtyard_ignore or ())
        conflicts = FP.courtyard_conflicts(cys, ignore=ignored)
        hot = {}
        for cf in conflicts:
            if cf["status"] not in ("overlap", "touching"):
                continue
            for r in (cf["a"], cf["b"]):
                if hot.get(r) != "overlap":
                    hot[r] = cf["status"]
        for key, cy in cys.items():
            base = "#008b8b" if cy.side == "top" else "#8b5a8b"
            edge = {"overlap": "red", "touching": "darkorange"}.get(hot.get(cy.ref), base)
            off = cy.ref in ignored
            for poly in cy.polygons:
                ax.add_patch(Polygon(poly, fc="none", ec="grey" if off else edge,
                                     ls=":" if off else "-." if cy.side == "bottom" else "-",
                                     lw=1.2 if cy.ref in hot else 0.6))
        for cf in conflicts:
            for poly in cf.get("region", []):
                ax.add_patch(Polygon(poly, fc="red", ec="red", alpha=0.45, lw=0.5))
    if pad_numbers:
        for ref, pad, px, py, side in FP.pad_names(root):
            if x0 - 1 <= px <= x1 + 1 and y0 - 1 <= py <= y1 + 1:
                ax.text(px, py, pad, fontsize=max(3.0, min(7.0, 0.5 * pt_per_mm)), ha="center", va="center",
                        color="black" if side != "bottom" else "#333366", clip_on=True)
    for el in board.iterfind("./elements/element"):
        _, mir = parse_rot(el.get("rot"))
        ex, ey = float(el.get("x")), float(el.get("y"))
        if x0 <= ex <= x1 and y0 <= ey <= y1:
            ax.text(ex, ey, el.get("name") + (" (b)" if mir else ""), fontsize=9, ha="center", va="center",
                    color="navy", weight="bold")
    n_wires = n_vias = 0
    for s in board.iterfind("./signals/signal"):
        name = s.get("name")
        for poly in s.iterfind("polygon"):
            pts = [(float(v.get("x")), float(v.get("y"))) for v in poly.iterfind("vertex")]
            if pts:
                ax.add_patch(Polygon(pts, fc="none", ec=LAYER_STYLE.get(int(poly.get("layer")), ("grey", "-"))[0],
                                     ls=":", lw=0.8))
        if traces:
            for wr in s.iterfind("wire"):
                lay = int(wr.get("layer"))
                if lay == 19:
                    continue
                colour, ls = LAYER_STYLE.get(lay, ("grey", "-"))
                seg = flatten([(float(wr.get("x1")), float(wr.get("y1"))),
                               (float(wr.get("x2")), float(wr.get("y2")), float(wr.get("curve") or 0))], 0.02)
                ax.plot(*zip(*seg), ls,
                        color=col.get(name, colour), lw=max(float(wr.get("width")) * pt_per_mm, 0.5),
                        solid_capstyle="round")
                n_wires += 1
            for v in s.iterfind("via"):
                d = float(v.get("diameter") or 0) or float(v.get("drill")) * 2
                ax.add_patch(Circle((float(v.get("x")), float(v.get("y"))), d / 2, fc="gold", ec="k", lw=0.4))
                n_vias += 1
    if hl:
        for o in obs:
            if o.is_pad and o.net in col and x0 <= o.data[0] <= x1 and y0 <= o.data[1] <= y1:
                ax.text(o.data[0], o.data[1] + 0.9, o.net, fontsize=5.5, ha="center", color=col[o.net])
    for plan in plans or []:
        for side, colour in (("p", "crimson"), ("n", "royalblue")):
            for t in plan[side]["traces"]:
                px, py = zip(*flatten(t["points"], 0.02))
                ax.plot(px, py, "-" if t["layer"] == 1 else "--", color=colour, lw=1.0)
            for vx, vy in plan[side]["vias"]:
                ax.add_patch(Circle((vx, vy), 0.3, fc="none", ec=colour, lw=0.8))
    ax.set_xlim(x0 - 1, x1 + 1)
    ax.set_ylim(y0 - 1, y1 + 1)
    ax.set_aspect("equal")
    step = 5 if max(w, h) > 20 else 1
    ax.set_xticks([t for t in range(int(x0) - int(x0) % step, int(x1) + step, step)])
    ax.set_yticks([t for t in range(int(y0) - int(y0) % step, int(y1) + step, step)])
    ax.grid(True, lw=0.3, alpha=0.5)
    legend = "top: red solid, bottom: blue dashed, pours dotted, vias gold"
    if courtyards:
        legend += "; courtyards teal (bottom purple), overlap red, touching orange, skipped grey dotted"
    ax.set_title("\n".join(textwrap.wrap(title or legend, max(30, int(fig.get_size_inches()[0] * 13)))), fontsize=9)
    plt.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return {"path": out_path, "traces": n_wires, "vias": n_vias, "highlighted_nets": nets,
            "region_mm": [x0, y0, x1, y1],
            "courtyard_conflicts": [{k: v for k, v in c.items() if k != "region"} for c in conflicts],
            "courtyards_skipped": skipped}
