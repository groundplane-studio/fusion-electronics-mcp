import json
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from fusion_mcp import jlc
from fusion_mcp import server as S
from fusion_mcp.session import Session
from fusion_offline import pairs as PR

from test_move_parts import FakeFusion
from test_route_pair import RULES

CL = [[1, 0], [19, 0]]


def pinned(above=True, below=True):
    """DP/DN straight along y +-0.25; pins of other nets at x 10 whose edges sit 0.11 mm from the
    traces (clearance 0.12): a pair squeezing between two pins, like the RJ45 pin field."""
    pins = []
    if above:
        pins.append(("PA", 10, 0.61, "X"))
    if below:
        pins.append(("PB", 10, -0.61, "Y"))
    els = "".join(f'<element name="{n}" library="L" package="SMD1" value="" x="{x}" y="{y}"/>'
                  for n, x, y in [("P1", 0, 0.25), ("P2", 20, 0.25), ("N1", 0, -0.25), ("N2", 20, -0.25)]
                  + [(n, x, y) for n, x, y, _ in pins])
    sig = "".join(f'<signal name="{net}"><contactref element="{n}" pad="1"/></signal>' for n, _, _, net in pins)
    return ET.fromstring(f"""<eagle><drawing><board><plain/>
<libraries><library name="L"><packages><package name="SMD1"><smd name="1" x="0" y="0" dx="0.3" dy="0.3" layer="1"/></package></packages></library></libraries>
<designrules name="t">{RULES}</designrules><classes/><elements>{els}</elements><signals>
<signal name="DP"><contactref element="P1" pad="1"/><contactref element="P2" pad="1"/></signal>
<signal name="DN"><contactref element="N1" pad="1"/><contactref element="N2" pad="1"/></signal>{sig}
</signals></board></drawing></eagle>""")


def j2_pins():
    """PoE board TP2_1 at J2 (2026-10-08), moved to the origin: N runs at y -0.2 between round THT pads
    (1.53 mm) of another net (X, below) and of P itself (above, where P turns up into its pin)."""
    els = [("P1", "SMD2", 0, 0.2), ("N1", "SMD2", 0, -0.2), ("P2", "THT1", 10.14, 0.855), ("N2", "THT1", 11.92, -0.165),
           ("X1", "THT1", 10.14, -1.185)]
    e = "".join(f'<element name="{n}" library="L" package="{pk}" value="" x="{x}" y="{y}"/>' for n, pk, x, y in els)
    return ET.fromstring(f"""<eagle><drawing><board><plain/>
<libraries><library name="L"><packages><package name="SMD2"><smd name="1" x="0" y="0" dx="0.2" dy="0.2" layer="1"/></package>
<package name="THT1"><pad name="1" x="0" y="0" drill="0.9" diameter="1.53"/></package></packages></library></libraries>
<designrules name="t">{RULES}</designrules><classes/><elements>{e}</elements><signals>
<signal name="DP"><contactref element="P1" pad="1"/><contactref element="P2" pad="1"/></signal>
<signal name="DN"><contactref element="N1" pad="1"/><contactref element="N2" pad="1"/></signal>
<signal name="X"><contactref element="X1" pad="1"/></signal>
</signals></board></drawing></eagle>""")


class TableTest(unittest.TestCase):
    def test_values_and_source(self):
        t = jlc.table()
        self.assertEqual((t["source"], t["retrieved"]), ("https://jlcpcb.com/capabilities/pcb-capabilities", "2026-10-07"))
        self.assertEqual(jlc.limit("min_trace_width", 2), 0.10)
        self.assertEqual(jlc.limit("min_trace_width", 4), 0.09)
        self.assertEqual(jlc.limit("same_net_track_spacing"), 0.25)
        self.assertEqual(jlc.limit("pth_to_track", which="recommended"), 0.35)
        for name, row in t["limits"].items():
            self.assertTrue(row.get("text"), name)                     # every value says where it came from


class SqueezeTest(unittest.TestCase):
    def test_pair_between_two_pins_is_explained(self):
        p = PR.plan_pair(pinned(), "DP", "DN", CL, 0.2, 0.3)
        self.assertFalse(p["ok"])
        sq = next(c["squeeze"] for c in p["conflicts"] if c["net"] == "DP" and "squeeze" in c)
        self.assertEqual((sq["between"], sq["with_partner"], sq["max_width_mm"]), (["X", "Y"], True, 0.18))
        self.assertEqual(sq["options"][:2], ["neck down to 0.180 mm over the squeeze (neck_down=true)",
                                             "lower the clearance to 0.110 mm (e.g. the class clearance)"])

    def test_one_sided_conflict_is_not_a_squeeze(self):
        p = PR.plan_pair(pinned(below=False), "DP", "DN", CL, 0.2, 0.3)
        self.assertFalse(p["ok"])
        self.assertFalse(any("squeeze" in c for c in p["conflicts"]))

    def test_no_width_fits(self):
        p = PR.plan_pair(pinned(), "DP", "DN", CL, 0.2, 0.3, min_width=0.2)
        sq = next(c["squeeze"] for c in p["conflicts"] if "squeeze" in c)
        self.assertIn("no width fits here", sq["options"][0])

    def test_neck_down_only_over_the_squeeze(self):
        p = PR.plan_pair(pinned(), "DP", "DN", CL, 0.2, 0.3, neck_down=True, min_width=0.1)
        self.assertTrue(p["ok"], p["conflicts"])
        self.assertEqual([(n["net"], n["width_mm"]) for n in p["necks"]], [("DP", 0.18), ("DN", 0.18)])
        for n in p["necks"]:
            self.assertTrue(0.3 < n["length_mm"] < 1.0, n)               # the pin (0.3 mm) plus a little each side
            self.assertTrue(9 < n["from"][0] < 10 < n["to"][0] < 11)
        widths = [t["width"] for t in p["p"]["traces"]]
        self.assertEqual(sorted(set(widths)), [0.18, 0.2])
        self.assertIn("neck-down", p["warnings"][-1])


class Round3Test(unittest.TestCase):
    """PoE board TP2_1 (2026-10-08): tuning bumps on P near a 45-degree bend hit N, and neck_down then
    narrowed N to dodge them, far from the J2 pin squeeze."""

    def test_bumps_move_away_from_partner_copper_and_give_up_rather_than_hit_it(self):
        line, below = [(0.0, 0.0), (20.0, 0.0)], [(0.0, -0.5), (20.0, -0.5)]
        # the partner's next segment rises through the middle of the run, on the bump side
        bend = [(10.0, y / 10) for y in range(0, 15)]
        clear = (bend, 0.2 + 0.2)                                     # width + gap, centre to centre
        new, got = PR.add_bumps(line, 0.6, below, keep_off=clear)
        self.assertAlmostEqual(got, 0.6, places=3)
        bump = [q for q in new if abs(q[1]) > 1e-6]
        self.assertTrue(all(abs(q[0] - 10.0) >= 0.4 - 1e-3 for q in PR.flatten(new, 0.05) if q[1] > 1e-6), bump)
        # partner copper all along the bump side: no place fits, nothing is added
        wall = ([(x / 10, 0.5) for x in range(0, 201)], 0.4)
        self.assertEqual(PR.add_bumps(line, 0.6, below, keep_off=wall)[1], 0.0)

    def test_bumps_keep_clear_of_the_partner_near_a_bend(self):
        from test_route_pair import board
        root = board(p_end=(12, 1.905), n_end=(13.5, 1.405))         # N ends 1.5 mm further: P is tuned
        p = PR.plan_pair(root, "DP", "DN", [[1, 0], [2.4, 0], [4.055, 1.655], [11, 1.655]], 0.2, 0.3,
                         max_skew_mm=0.05, neck_down=True)
        pn = [c for c in p["conflicts"] if c.get("with") in ("DP", "DN")]
        self.assertEqual(pn, [])
        self.assertEqual(p["necks"], [])
        self.assertTrue(p["skew_ok"], p["why_not_ok"])

    def test_neck_covers_a_round_pad_to_where_the_clearance_holds(self):
        # the neck stopped at x 10.316, 0.176 mm past the pad centre, with N still 0.024 mm too close to it
        p = PR.plan_pair(j2_pins(), "DP", "DN", [[1, 0], [8.8, 0]], 0.2, 0.2, clearance=0.12, margin=0.01,
                         pair_clearance=0.15, neck_down=True)
        self.assertTrue(p["ok"], p["conflicts"])
        (nk,) = p["necks"]
        self.assertEqual((nk["net"], nk["width_mm"]), ("DN", 0.12))
        self.assertLess(nk["from"][0], 10.14 - 0.3)
        self.assertGreater(nk["to"][0], 10.14 + 0.3)                  # about as far each side of the pad centre
        self.assertLess(nk["to"][0], 11.0)                            # and not on to N's own pin

    def test_no_neck_for_copper_on_one_side(self):
        p = PR.plan_pair(pinned(below=False), "DP", "DN", CL, 0.2, 0.3, neck_down=True, min_width=0.1)
        self.assertEqual(p["necks"], [])                              # one-sided: route another way
        self.assertFalse(p["ok"])


class RoutePairWidthTest(unittest.TestCase):
    def test_each_piece_is_written_at_its_width(self):
        fake = FakeFusion(pinned())
        sent = {}

        def fake_write(self, editor, commands, verify, **kw):
            sent["cmds"] = commands
            return None, "ok"
        with mock.patch.object(S, "session", Session(fake)), mock.patch.object(S, "_v2_rules", lambda: (None, "none")), \
                mock.patch.object(Session, "verified_write", fake_write),                 mock.patch.object(S, "_write_layer", lambda name: {"top": 1, "bottom": 16}[name]):
            res = S.route_pair("DP", "DN", CL, 0.2, 0.3, neck_down=True, design="PoE Magnetics Test Board")
        self.assertTrue(res["written"])
        self.assertIn("WIRE 'DP' 0.16", sent["cmds"])           # 2 x (0.21 - 0.12 - the 0.01 mm margin)
        self.assertIn("WIRE 'DP' 0.2", sent["cmds"])


if __name__ == "__main__":
    unittest.main()
