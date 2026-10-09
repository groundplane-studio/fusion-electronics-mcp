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

COPPER = {1: {"copper_mm": 0.035, "outer": True, "name": "Top", "source": "x"},
          16: {"copper_mm": 0.035, "outer": True, "name": "Bottom", "source": "x"}}


def board(vias="", wires="", elements="", contacts="", other=""):
    return ET.fromstring(f"""<eagle><drawing><layers><layer number="1" name="Top"/><layer number="16" name="Bottom"/></layers>
<board><plain/><libraries/><designrules name="t"/><classes><class number="0" name="default"/></classes>
<elements>{elements}</elements><signals>
<signal name="VIN">{contacts}<wire x1="0" y1="1" x2="40" y2="1" width="2.0" layer="1"/>
<wire x1="0" y1="3" x2="40" y2="3" width="2.0" layer="16"/>{wires}{vias}</signal>{other}
<signal name="GND"/></signals></board></drawing></eagle>""")


def via(x, y):
    return f'<via x="{x}" y="{y}" extent="1-16" drill="0.3"/>'


def el(name, value):
    return (f'<element name="{name}" library="L" package="X" value="{value}" x="0" y="0">'
            f'<attribute name="MPN" value="{value}"/></element>')


class ViaTransitionTest(unittest.TestCase):
    def test_three_separate_single_vias_fail_a_3a_net(self):
        # finding: the old count (3 vias >= 3 needed) passed though each via carries 3 A
        res = CU.check(board(via(5, 1) + via(15, 1) + via(25, 1)), {"VIN": {"a": 3.0}}, COPPER, 1.0)[0]
        self.assertFalse(res["ok"])
        self.assertEqual(res["via_transitions"], [1, 1, 1])
        self.assertIn("3 of 3 layer change(s) have too few vias for 3.0 A: about 3 needed at each", res["problems"][0])

    def test_vias_side_by_side_are_one_transition(self):
        res = CU.check(board(via(5, 1) + via(5.8, 1) + via(6.6, 1)), {"VIN": {"a": 3.0}}, COPPER, 1.0)[0]
        self.assertEqual(res["via_transitions"], [3])
        self.assertTrue(res["ok"])

    def test_short_segment_joins_vias(self):
        # 3 mm apart (beyond the 2 mm radius) but joined by a 3 mm Top segment
        link = '<wire x1="5" y1="1" x2="8" y2="1" width="2.0" layer="1"/>'
        vias = via(5, 1) + via(8, 1)
        self.assertEqual(CU.check(board(vias, link), {"VIN": {"a": 2.0}}, COPPER)[0]["via_transitions"], [2])
        self.assertEqual(CU.check(board(vias), {"VIN": {"a": 2.0}}, COPPER)[0]["via_transitions"], [1, 1])

    def test_via_current_from_ipc2221(self):
        # 0.3 mm drill, 25 um plating, 10 degC: about 0.84 A; thinner plating carries less
        self.assertAlmostEqual(CU.ipc2221_via_current(0.3, 0.025, 10), 0.84, delta=0.01)
        self.assertAlmostEqual(CU.ipc2221_via_current(0.3, 0.018, 10), 0.68, delta=0.01)
        with self.assertRaises(ValueError):
            CU.ipc2221_via_current(0.3, 0.2, 10)

    def test_inner_layer_cross_section_ratio(self):
        out = CU.ipc2221_width(1, 10, 0.035, True)
        inner = CU.ipc2221_width(1, 10, 0.035, False)
        self.assertAlmostEqual(inner / out, 2 ** (1 / 0.725), places=3)
        self.assertIn("2.6x", CU.__doc__)
        self.assertNotIn("about twice", CU.__doc__)


class InPathTest(unittest.TestCase):
    RATED = {k: {"part": k, "current_a": 1.0, "source": "s"} for k in ("TVS", "CAP", "SENSE", "FUSE", "SWITCH", "PD")}

    def rated_refs(self):
        elements = (el("D1", "TVS") + el("C1", "CAP") + el("R1", "SENSE") + el("F1", "FUSE")
                    + el("U5", "SWITCH") + el("R2", "PD"))
        contacts = "".join(f'<contactref element="{r}" pad="1"/>' for r in ("D1", "C1", "R1", "F1", "U5", "R2"))
        other = ('<signal name="GND"><contactref element="D1" pad="2"/><contactref element="C1" pad="2"/>'
                 '<contactref element="U5" pad="3"/><contactref element="R2" pad="2"/></signal>'
                 '<signal name="VOUT"><contactref element="R1" pad="2"/><contactref element="U5" pad="2"/></signal>')
        root = board(elements=elements, contacts=contacts, other=other.replace('<signal name="GND">', '<signal name="AGND">'))
        return {r["ref"]: r for r in part_ratings.ratings_on(root, "VIN", self.RATED)}

    def test_shunt_parts_are_left_out(self):
        refs = self.rated_refs()
        self.assertEqual(sorted(refs), ["F1", "R1", "U5"])       # TVS, decoupling cap, pull-down left out
        self.assertIn("series part", refs["F1"]["in_path"])
        self.assertEqual(refs["R1"]["in_path"], "bridges VIN to VOUT")
        self.assertEqual(refs["U5"]["in_path"], "bridges VIN to VOUT")

    def test_ground_names(self):
        for n in ("GND", "AGND", "PGND", "GND_ISO", "VSS", "0V"):
            self.assertTrue(part_ratings.is_ground(n), n)
        for n in ("VIN", "VOUT", "+5V", "VBUS"):
            self.assertFalse(part_ratings.is_ground(n), n)


class SizingTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        root = board(elements=el("U5", "LOADSW"), contacts='<contactref element="U5" pad="1"/>',
                     other='<signal name="VOUT"><contactref element="U5" pad="2"/></signal>')
        self.patches = [mock.patch.object(design_store, "path", lambda d: os.path.join(self.dir, d + ".json")),
                        mock.patch.object(part_ratings, "path", lambda: os.path.join(self.dir, "ratings.json")),
                        mock.patch.object(S, "session", Session(FakeFusion(root))),
                        mock.patch.object(S, "_v2_rules", lambda: (None, "none"))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_sized_for_full_current_with_a_warning(self):
        S.set_net_current({"VIN": 3.0})
        S.set_part_rating("LOADSW", 2.0, "https://example.com/loadsw.pdf")
        size = S.size_for_current()["nets"]["VIN"]
        self.assertEqual(size["current_a"], 3.0)
        self.assertEqual(size["width_mm"]["Top"], 1.37)               # 3 A, not 2 A (0.78 mm)
        self.assertTrue(size["warnings"][0].startswith("U5 (LOADSW) is rated 2 A on a 3 A net"))
        self.assertEqual(size["suggested_class"]["name"], "pwr_3A")
        chk = S.check_current()["nets"][0]
        self.assertFalse(chk["ok"])
        self.assertIn("U5 (LOADSW) is rated 2.0 A, below the 3.0 A this net carries", chk["problems"][-1])


class ReferenceTest(unittest.TestCase):
    def test_poe_type4_sized_for_096(self):
        row = next(r for r in S.current_ratings("Type 4")["rows"])
        self.assertEqual(row["current_a"], 0.96)
        self.assertIn("0.866", row["notes"])
        self.assertTrue(any("siemon.com" in u for u in row["source"]["urls"]))


if __name__ == "__main__":
    unittest.main()
