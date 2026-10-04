"""Route many connections on a working copy of the board, with rip-up and reroute.

The free-router step of the user's order (layer pours, big pours, GND vias, close hops,
fan-outs, then this): each connection is routed with router.route (taps into the net's
copper allowed). When one cannot be routed, it is tried again allowed to cross the traces this
run laid, at a cost; every net it crosses is ripped up (whole net, so no tap is left hanging
off missing copper) and queued again. Traces that existed before the run (bus lanes, hand
routes) are never ripped. Rounds are limited per connection.
"""

from __future__ import annotations

import copy
import math
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field

from . import router as RT


@dataclass
class Conn:
    id: str
    net: str
    start: tuple
    start_layers: tuple
    goal: tuple
    goal_layers: tuple
    width: float = 0.25
    label: str = ""


@dataclass
class Result:
    routes: dict = field(default_factory=dict)       # conn id -> Route
    failed: dict = field(default_factory=dict)       # conn id -> reason
    rips: int = 0
    root: ET.Element | None = None


def _sig(root, net):
    return next(s for s in root.iterfind("./drawing/board/signals/signal") if s.get("name") == net)


def _commit(root, c: Conn, r):
    sg = _sig(root, c.net)
    for l, pts, w in r.pieces(c.width):
        for p, q in zip(pts, pts[1:]):
            ET.SubElement(sg, "wire", {"x1": str(p[0]), "y1": str(p[1]), "x2": str(q[0]), "y2": str(q[1]),
                                       "width": str(w), "layer": str(l), "mcp_conn": c.id})
    for vx, vy in r.vias:
        ET.SubElement(sg, "via", {"x": str(vx), "y": str(vy), "extent": "1-16", "drill": "0.3",
                                  "diameter": "0.6", "mcp_conn": c.id})


def _rip(root, ids: set):
    for sg in root.iterfind("./drawing/board/signals/signal"):
        for el in list(sg):
            if el.get("mcp_conn") in ids:
                sg.remove(el)


def _not_joined(root, c: Conn) -> set:
    frag, _ = RT.fragment(root, c.net, c.goal)
    key = lambda p: (round(p[0], 3), round(p[1], 3))
    out = set()
    for _l, a, b in RT._net_traces(root, c.net):
        k = (key(a), key(b))
        if k not in frag and (k[1], k[0]) not in frag:
            out.add(k)
    return out


def route_all(root: ET.Element, conns: list[Conn], vias: bool = True, rounds: int = 3,
              rip_cost: float = 15.0, **kw) -> Result:
    work = copy.deepcopy(root)
    res = Result(root=work)
    by_id = {c.id: c for c in conns}
    tries = Counter()
    queue = list(conns)
    while queue:
        c = queue.pop(0)
        if c.id in res.routes:
            continue
        # tap only copper already joined to the goal: tapping a piece that is not (a bus lane no
        # pin has reached yet) would look routed and leave the connection open
        kw["tap_exclude"] = _not_joined(work, c)
        r = RT.route(work, c.net, c.start, c.start_layers, c.goal, c.goal_layers, width=c.width,
                     vias=vias, join_existing=True, **kw)
        if r.legs and not r.problems:
            _commit(work, c, r)
            res.routes[c.id] = r
            res.failed.pop(c.id, None)
            continue
        tries[c.id] += 1
        if tries[c.id] > rounds:
            res.failed[c.id] = (r.problems or ["no route"])[0]
            continue
        laid = {i for i in res.routes}
        kw["tap_exclude"] = _not_joined(work, c)
        r2 = RT.route(work, c.net, c.start, c.start_layers, c.goal, c.goal_layers, width=c.width, vias=vias,
                      join_existing=True, soft_tags=laid, rip_cost=rip_cost, **kw)
        if not r2.legs or r2.problems:
            res.failed[c.id] = (r2.problems or r.problems or ["no route"])[0]
            continue
        # rip every net it crosses (whole nets: no tap left hanging), then lay it and requeue them
        hit_nets = {by_id[t].net for t in r2.conflicts if t in by_id}
        rip = {i for i in res.routes if by_id[i].net in hit_nets}
        _rip(work, rip)
        for i in rip:
            del res.routes[i]
            queue.append(by_id[i])
        res.rips += len(rip)
        kw["tap_exclude"] = _not_joined(work, c)
        r3 = RT.route(work, c.net, c.start, c.start_layers, c.goal, c.goal_layers, width=c.width,
                      vias=vias, join_existing=True, **kw)
        if r3.legs and not r3.problems:
            _commit(work, c, r3)
            res.routes[c.id] = r3
            res.failed.pop(c.id, None)
        else:
            queue.append(c)
    return res
