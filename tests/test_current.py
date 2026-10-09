import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from fusion_mcp import design_store, part_ratings
from fusion_mcp import server as S
from fusion_mcp.session import Session
from fusion_offline import current as CU

from test_move_parts import FakeFusion


def vin_board(width=0.2, vias=0, pour=False, parts=False, vin_class=None):
    v = "".join(f'<via x="{5 + i}" y="1" extent="1-16" drill="0.3"/>' for i in range(vias))
    poly = '<polygon width="0.2" layer="1"><vertex x="0" y="0"/><vertex x="10" y="0"/><vertex x="10" y="5"/></polygon>' if pour else ""
    bottom = '<wire x1="5" y1="1" x2="9" y2="1" width="1.6" layer="16"/>' if vias else ""
    return ET.fromstring(f"""<eagle><drawing><layers><layer number="1" name="Top"/><layer number="16" name="Bottom"/></layers>
<board><plain/><libraries/><designrules name="t"/><classes><class number="0" name="default"/>
<class number="3" name="pwr_5V" width="1.0" drill="0"/></classes><elements>{T1 if parts else ""}</elements><signals>
<signal name="VIN"{f' class="{vin_class}"' if vin_class else ""}>{'<contactref element="T1" pad="1"/>' if parts else ""}<wire x1="0" y1="1" x2="5" y2="1" width="{width}" layer="1"/>{bottom}{v}{poly}</signal>
<signal name="GND"/></signals></board></drawing></eagle>""")


T1 = ('<element name="T1" library="L" package="X" value="G2415S" x="0" y="0">'
      '<attribute name="MPN" value="G2415S"/></element>')


class WidthTest(unittest.TestCase):
    def test_ipc2221_against_the_reference_table(self):
        # the spec's IPC-2221 table: outer 1 oz, 10 degC rise
        for amps, mm in ((0.5, 0.12), (1, 0.30), (1.5, 0.53), (2, 0.78), (3, 1.37)):
            self.assertAlmostEqual(CU.ipc2221_width(amps, 10, 0.035, True), mm, delta=0.006)
        self.assertAlmostEqual(CU.ipc2221_width(1, 20, 0.035, True), 0.20, delta=0.005)
        # inner 0.5 oz (17.5 um): about 5x the outer width
        self.assertAlmostEqual(CU.ipc2221_width(1, 10, 0.0175, False), 1.563, places=2)
        with self.assertRaises(ValueError):
            CU.ipc2221_width(0, 10, 0.035, True)

    def test_copper_sources(self):
        root = vin_board()
        assumed = CU.copper_by_layer(root)
        self.assertEqual({c["source"] for c in assumed.values()}, {"assumed 35 um (1 oz)"})
        self.assertTrue(all(c["outer"] for c in assumed.values()))
        stack = CU.copper_by_layer(root, [0.035, 0.035])
        self.assertEqual(stack[1]["source"], "stackup")


class CheckTest(unittest.TestCase):
    COPPER = {1: {"copper_mm": 0.035, "outer": True, "name": "Top", "source": "x"},
              16: {"copper_mm": 0.035, "outer": True, "name": "Bottom", "source": "x"}}

    def test_narrow_trace_is_flagged(self):
        res = CU.check(vin_board(0.2), {"VIN": {"a": 1.0}}, self.COPPER)[0]
        self.assertFalse(res["ok"])
        self.assertIn("narrower than 0.3 mm (narrowest 0.2 mm", res["problems"][0])
        self.assertTrue(CU.check(vin_board(0.35), {"VIN": {"a": 1.0}}, self.COPPER)[0]["ok"])

    def test_pour_layer_is_not_judged_by_width(self):
        self.assertTrue(CU.check(vin_board(0.2, pour=True), {"VIN": {"a": 1.0}}, self.COPPER)[0]["ok"])

    def test_vias_against_current(self):
        res = CU.check(vin_board(1.6, vias=1), {"VIN": {"a": 2.0}}, self.COPPER, 1.0)[0]
        self.assertIn("1 of 1 layer change(s) have too few vias for 2.0 A: about 2 needed at each", res["problems"][0])
        self.assertTrue(CU.check(vin_board(1.6, vias=2), {"VIN": {"a": 2.0}}, self.COPPER, 1.0)[0]["ok"])


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.patches = [mock.patch.object(design_store, "path", lambda d: os.path.join(self.dir, d + ".json")),
                        mock.patch.object(part_ratings, "path", lambda: os.path.join(self.dir, "ratings.json")),
                        mock.patch.object(S, "session", Session(FakeFusion(vin_board(0.2, parts=True)))),
                        mock.patch.object(S, "_v2_rules", lambda: (None, "none"))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_store_size_check_and_route_width(self):
        S.set_net_current({"VIN": 1.0})
        size = S.size_for_current()["nets"]["VIN"]
        self.assertEqual(size["width_mm"], {"Top": 0.3, "Bottom": 0.3})
        # pwr_5V (1.0 mm) already exists and is wide enough for 1 A: suggested instead of a new name
        self.assertEqual(size["suggested_class"], {"name": "pwr_5V", "width_mm": 1.0, "existing": True})
        chk = S.check_current()
        self.assertFalse(chk["ok"])
        w, note = S._width_for("VIN", None)
        self.assertEqual(w, 0.3)
        self.assertIn("from VIN's 1.0 A", note)
        self.assertEqual(S._width_for("VIN", 0.5), (0.5, None))         # a given width wins
        self.assertEqual(S._width_for("GND", None), (0.25, None))       # no current saved
        S.set_net_current({"VIN": 0})
        self.assertIn("no currents saved", S.check_current()["note"])

    def test_weak_part_is_a_warning_not_a_smaller_width(self):
        # PoE: the standard allows far more, but the magnetics are rated 350 mA; the copper is
        # still sized for the net's 0.96 A
        S.set_net_current({"VIN": 0.96})
        S.set_part_rating("G2415S", 0.35, "https://example.com/g2415s.pdf", "802.3af only")
        size = S.size_for_current()["nets"]["VIN"]
        self.assertEqual(size["current_a"], 0.96)
        self.assertNotIn("limited_by", size)
        self.assertIn("T1 (G2415S) is rated 0.35 A on a 0.96 A net", size["warnings"][0])
        self.assertEqual(size["rated_parts"][0]["ref"], "T1")
        chk = S.check_current()["nets"][0]
        self.assertIn("T1 (G2415S) is rated 0.35 A, below the 0.96 A this net carries", chk["problems"][-1])
        self.assertIn("G2415S", S.list_part_ratings()["ratings"])
        with self.assertRaises(Exception):
            S.set_part_rating("X1", 1.0, "")                               # a rating needs its source

    def test_reference_currents(self):
        poe = S.current_ratings("PoE")["rows"]
        self.assertEqual([r["current_a"] for r in poe], [0.35, 0.6, 0.6, 0.96])
        self.assertTrue(all(r["source"]["url"].startswith("https://") for r in poe))
        usb = {r["interface"]: r["current_a"] for r in S.current_ratings("USB")["rows"]}
        self.assertEqual(usb["USB 2.0 default (VBUS)"], 0.5)
        self.assertIsNone(usb["USB Power Delivery (3 A, 5 A with an e-marked cable)"])

    def test_fab_minimum_and_rounding(self):
        # 0.1 A needs 0.013 mm by IPC-2221: the fab minimum sets it
        size = S.size_for_current(nets=["GND"], current_a=0.1)["nets"]["GND"]
        self.assertEqual(size["width_mm"], {"Top": 0.1, "Bottom": 0.1})
        self.assertIn("width set by the fab minimum (0.1 mm), not the current", size["width_note"])
        self.assertAlmostEqual(size["ipc2221_mm"]["Top"], 0.013, places=3)
        # 3 A: 1.367 rounds up to 1.37
        self.assertEqual(S.size_for_current(nets=["GND"], current_a=3.0)["nets"]["GND"]["width_mm"]["Top"], 1.37)

    def test_existing_class_too_narrow(self):
        # 5V in pwr_5V (1.0 mm) carrying 3 A needs 1.37 mm on Top
        with mock.patch.object(S, "session", Session(FakeFusion(vin_board(0.2, parts=True, vin_class="3")))):
            size = S.size_for_current(nets=["VIN"], current_a=3.0)["nets"]["VIN"]
        self.assertEqual(size["existing_class"]["name"], "pwr_5V")
        self.assertIn("pwr_5V 1 mm < 1.37 mm needed on the outer layers: widen the class to 1.37 mm",
                      size["existing_class"]["advice"])
        self.assertEqual(size["suggested_class"], {"name": "pwr_5V", "width_mm": 1.37, "existing": True})

    def test_class_advice_judges_outer_layers_and_notes_inner(self):
        # PoE board 2026-10-08: the advice compared against an inner layer, then advised the outer width
        poe = S._class_advice("poe_ct", 0.3, 0.1, {"Route2": 0.36, "Route15": 0.36})
        self.assertEqual(poe, "poe_ct 0.3 mm is enough on the outer layers (needs 0.1 mm); inner layers need 0.36 mm: "
                              "keep this net on the outer layers or route at least 0.36 mm there")
        five = S._class_advice("pwr_5V", 1.0, 1.37, {"Route2": 6.92})
        self.assertTrue(five.startswith("pwr_5V 1 mm < 1.37 mm needed on the outer layers: widen the class to 1.37 mm"))
        self.assertTrue(five.endswith("inner layers need 6.92 mm, so use a pour or plane there"))
        self.assertNotIn("inner", S._class_advice("pwr_3V3", 0.3, 0.1, {}))
        for text in (poe, five):                                     # never a width below the class
            self.assertNotIn("widen the class to 0.1", text)

    def test_unrouted_nets_are_not_judged(self):
        S.set_net_current({"GND": 1.0})                                  # GND has no copper at all
        chk = S.check_current()
        self.assertIsNone(chk["ok"])
        self.assertEqual(chk["not_routed"], ["GND"])
        self.assertEqual(chk["nets"][0]["status"], "not routed: nothing checked")
        S.set_net_current({"VIN": 1.0})                                  # routed and too narrow: judged
        self.assertFalse(S.check_current()["ok"])

    def test_one_off_sizing_and_refusals(self):
        res = S.size_for_current(nets=["GND"], current_a=3.0)
        self.assertAlmostEqual(res["nets"]["GND"]["width_mm"]["Top"], 1.367, places=2)
        with self.assertRaises(Exception):
            S.size_for_current(current_a=1.0)
        with self.assertRaises(Exception):
            S.set_net_current({"NOPE": 1.0})


if __name__ == "__main__":
    unittest.main()
