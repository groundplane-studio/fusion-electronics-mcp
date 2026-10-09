import json
import math
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from fusion_mcp import server as S
from fusion_mcp.session import Session
from fusion_offline import pairs as PR

from test_move_parts import FakeFusion

RULES = ('<param name="mdWireWire" value="0.12mm"/><param name="mdWirePad" value="0.1mm"/>'
         '<param name="mdWireVia" value="0.1mm"/><param name="mdViaVia" value="0.15mm"/>')


def board(p_end=(20, 0.25), n_end=(20, -0.25), extra_signals="", classes="", sig_class=""):
    """DP from (0, 0.25) to p_end, DN from (0, -0.25) to n_end: SMD pads on top."""
    els = "".join(f'<element name="{n}" library="L" package="SMD1" value="" x="{x}" y="{y}"/>'
                  for n, (x, y) in (("P1", (0, 0.25)), ("P2", p_end), ("N1", (0, -0.25)), ("N2", n_end)))
    cls = f' class="{sig_class}"' if sig_class else ""
    return ET.fromstring(f"""<eagle><drawing><board><plain/>
<libraries><library name="L"><packages><package name="SMD1"><smd name="1" x="0" y="0" dx="0.3" dy="0.3" layer="1"/></package></packages></library></libraries>
<designrules name="t">{RULES}</designrules><classes>{classes}</classes><elements>{els}</elements><signals>
<signal name="DP"{cls}><contactref element="P1" pad="1"/><contactref element="P2" pad="1"/></signal>
<signal name="DN"{cls}><contactref element="N1" pad="1"/><contactref element="N2" pad="1"/></signal>
{extra_signals}</signals></board></drawing></eagle>""")


CL = [[1, 0], [19, 0]]


def plan(root=None, **kw):
    args = dict(width=0.2, gap=0.3)
    args.update(kw)
    return PR.plan_pair(root if root is not None else board(), "DP", "DN", kw.pop("centreline", CL),
                        **{k: v for k, v in args.items() if k != "centreline"})


class BasicTest(unittest.TestCase):
    def test_straight_pair(self):
        p = plan()
        self.assertTrue(p["ok"], p)
        self.assertIsNone(p["why_not_ok"])
        self.assertEqual((p["p"]["vias"], p["trunk_layer"]), ([], 1))
        self.assertAlmostEqual(p["skew_mm"], 0.0, places=4)


class LayerChangeTest(unittest.TestCase):
    """TP1 on the PoE board (2026-10-04) had to cross under TP2; done by hand with tail vias."""

    def test_via_pair_keeps_the_pair_coupled(self):
        p = plan(layer_changes=[{"at": [7, 0], "to": "bottom"}, {"at": [13, 0], "to": "top"}])
        self.assertTrue(p["ok"], p["why_not_ok"])
        # vias across the pair: via 0.6 + via clearance 0.15 (mdViaVia) -> 0.75 apart, centred on the line
        self.assertEqual(p["rules"]["via_pair_spacing_mm"], 0.75)
        pv, nv = p["p"]["vias"], p["n"]["vias"]
        self.assertEqual(len(pv), 2)
        for a, b in zip(pv, nv):
            self.assertAlmostEqual(a[0], b[0], places=4)                  # side by side, across the pair
            self.assertAlmostEqual(abs(a[1] - b[1]), 0.75, places=4)
        self.assertEqual([t["layer"] for t in p["p"]["traces"]], [1, 16, 1])
        # on the bottom both traces run straight at the same pitch as on top (+-0.25)
        bot_p = next(t for t in p["p"]["traces"] if t["layer"] == 16)["points"]
        bot_n = next(t for t in p["n"]["traces"] if t["layer"] == 16)["points"]
        mid = lambda pts: pts[1:-1]                                     # between the two vias
        self.assertEqual([round(q[1], 4) for q in mid(bot_p)], [0.25, 0.25])
        self.assertEqual([round(q[1], 4) for q in mid(bot_n)], [-0.25, -0.25])
        # the fan-out to each via is 45 degrees
        seg = next(t for t in p["p"]["traces"] if t["layer"] == 1)["points"][-2:]
        dx, dy = seg[1][0] - seg[0][0], seg[1][1] - seg[0][1]
        self.assertAlmostEqual(abs(dx), abs(dy), places=3)

    def test_spacing_follows_the_via_clearance_and_margin(self):
        p = plan(layer_changes=[{"at": [7, 0]}, {"at": [13, 0]}], margin=0.02)
        self.assertEqual(p["rules"]["via_pair_spacing_mm"], 0.77)

    def test_via_pair_keeps_the_pair_class_clearance(self):
        # PoE board TD0 (2026-10-08): eth_100 (class clearance 0.15) got 0.6 + 0.12 + 0.01 = 0.73 mm,
        # a 0.13 mm via gap DRC flags. With mdViaVia below the class clearance it must be 0.6 + 0.15 + 0.01.
        root = board()
        root.find(".//designrules/param[@name='mdViaVia']").set("value", "0.12mm")
        p = plan(root, layer_changes=[{"at": [7, 0]}, {"at": [13, 0]}], pair_clearance=0.15, margin=0.01)
        self.assertEqual(p["rules"]["via_pair_spacing_mm"], 0.76)
        a, b = p["p"]["vias"][0], p["n"]["vias"][0]
        self.assertGreaterEqual(math.dist(a, b) - 0.6, 0.15)
        self.assertTrue(p["ok"], p["conflicts"])

    def test_wrong_to_and_bad_points(self):
        with self.assertRaises(ValueError):
            plan(layer_changes=[{"at": [7, 0], "to": "top"}])          # already on top: it goes to bottom
        with self.assertRaises(ValueError):
            plan(layer_changes=[{"at": [7, 3]}])                        # not on the centreline
        with self.assertRaises(ValueError):
            plan(layer_changes=[{"at": [1.1, 0]}])                      # no room to fan out


class HeadViaTest(unittest.TestCase):
    def test_heads_take_vias_and_must_match(self):
        with self.assertRaises(ValueError):
            plan(p_head=[[0.5, 0.25], {"via": [0.6, 0.25]}])
        with self.assertRaises(ValueError) as cm:
            plan(p_head=[{"x": 1}], n_head=[{"x": 1}])
        self.assertIn('{"via": [x, y]}', str(cm.exception))
        # both heads drop to the bottom right after the pads: the trunk is on the bottom
        p = plan(p_head=[[0.4, 0.25], {"via": [0.4, 0.25]}], n_head=[[0.4, -0.25], {"via": [0.4, -0.25]}],
                 p_tail=[{"via": [19.6, 0.25]}], n_tail=[{"via": [19.6, -0.25]}])
        self.assertEqual(p["trunk_layer"], 16)
        self.assertEqual(len(p["p"]["vias"]), 2)


class TuningTest(unittest.TestCase):
    def test_tail_after_a_via_is_tuned(self):
        # the pair spends most of its length on the bottom; DP's end pad is 0.5 mm further away
        root = board(p_end=(20.5, 0.25))
        p = plan(root, centreline=[[1, 0], [19.5, 0]],
                 layer_changes=[{"at": [2.5, 0]}, {"at": [18, 0]}], max_skew_mm=0.05)
        self.assertTrue(p["skew_ok"], p["why_not_ok"])
        self.assertGreater(p["tuning_added_mm"], 0.3)
        bottom_n = next(t for t in p["n"]["traces"] if t["layer"] == 16)["points"]
        self.assertTrue(any(len(q) > 2 for q in bottom_n))              # rounded bumps on the bottom run

    def test_why_not_ok_when_only_skew_fails(self):
        root = board(p_end=(20.5, 0.25))
        p = plan(root, centreline=[[1, 0], [19.5, 0]], tune=False)
        self.assertFalse(p["ok"])
        self.assertEqual(p["conflicts"], [])
        self.assertIn("over max_skew_mm 0.1: tuning is off", p["why_not_ok"])
        short = board(p_end=(3.5, 0.25), n_end=(3, -0.25))
        p = plan(short, centreline=[[1, 0], [2.5, 0]])
        self.assertIn("no straight run long enough", p["why_not_ok"])


class ClearanceTest(unittest.TestCase):
    NEIGHBOUR = '<signal name="X"><wire x1="5" y1="0.67" x2="15" y2="0.67" width="0.2" layer="1"/></signal>'

    def test_neighbour_net_class_clearance_is_enforced(self):
        # X's copper is 0.67 - 0.1 - 0.35 = 0.22 mm from DP: fine against 0.12, not against X's class 0.25
        root = board(extra_signals=self.NEIGHBOUR)
        self.assertTrue(plan(root)["ok"])
        p = plan(root, net_clearance={"X": 0.25})
        self.assertFalse(p["ok"])
        self.assertEqual({c["with"] for c in p["conflicts"]}, {"X"})

    def test_margin(self):
        root = board(extra_signals=self.NEIGHBOUR)
        self.assertTrue(plan(root, net_clearance={"X": 0.21})["ok"])
        self.assertFalse(plan(root, net_clearance={"X": 0.21}, margin=0.02)["ok"])

    def test_gap_against_the_pair_class_clearance(self):
        self.assertIn("equals its class clearance", plan(pair_clearance=0.3)["warnings"][0])
        p = plan(pair_clearance=0.35)
        self.assertFalse(p["ok"])
        self.assertIn("below its net class clearance", p["conflicts"][-1]["note"])


class PartnerCopperTest(unittest.TestCase):
    def test_existing_partner_copper_is_named_as_such(self):
        # PoE board TP2_1 (2026-10-08): conflicts with TP2_1_N copper left on the board read like tuning bumps
        left = '<signal name="DN"><wire x1="10" y1="-0.5" x2="10" y2="0.6" width="0.2" layer="1"/></signal>'
        p = plan(board(extra_signals=left))
        mine = [c for c in p["conflicts"] if c.get("with") == "DN" and "obstacle_at" in c]
        self.assertTrue(mine)
        self.assertIn("existing DN copper on the board (not part of this plan)", mine[0]["note"])
        self.assertIn("rip up DN first", mine[0]["note"])


class RoutePairToolTest(unittest.TestCase):
    def test_class_clearance_comes_from_the_board(self):
        # DP/DN in class 7 with a 0.35 clearance (legacy value; no rules readable): gap 0.3 is a conflict
        root = board(classes='<class number="0" name="default"/><class number="7" name="eth_100" width="0.2" drill="0">'
                             '<clearance class="7" value="0.35"/></class>', sig_class="7")
        fake = FakeFusion(root)
        with mock.patch.object(S, "session", Session(fake)), mock.patch.object(S, "_v2_rules", lambda: (None, "none")):
            res = S.route_pair("DP", "DN", CL, 0.2, 0.3, dry_run=True, design="PoE Magnetics Test Board")
        self.assertFalse(res["written"])
        self.assertEqual(res["plan"]["rules"]["pair_class_clearance_mm"], 0.35)
        self.assertFalse(res["plan"]["ok"])
        self.assertEqual(fake.ran, [])


if __name__ == "__main__":
    unittest.main()
