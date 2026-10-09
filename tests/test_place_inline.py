import json
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from fusion_mcp import server as S
from fusion_mcp.session import Session
from fusion_offline import placement_plan as PP

from test_move_parts import FakeFusion
from test_placement_check import board, el


def poe_like():
    """T1's pads on rows 10, 11 and 13 at x 6; R18-R20 scattered and turned; J2 at the far end.
    R19 joins T1 with its pad 2, the others with pad 1 (as hand-placed parts often do)."""
    parts = (el("T1", 6, 10, "", "XFMR") + el("R18", 14, 3, "R90") + el("R19", 12, 20, "R270")
             + el("R20", 9, 15) + el("J2", 20, 11, "", "HDR1") + el("C9", 30, 30))
    sig = {"A": [("R18", "1"), ("T1", "1")], "B": [("R19", "2"), ("T1", "2")], "C": [("R20", "1"), ("T1", "3")],
           "D": [("R18", "2"), ("J2", "1")]}
    return board(parts, sig)


class InlinePlanTest(unittest.TestCase):
    def test_column_on_the_anchor_rows_facing_the_anchor(self):
        plan = PP.inline(poe_like(), ["R18", "R19", "R20"], x=10, to="T1", angle=0)
        moves = {m["ref"]: (m["x_mm"], m["y_mm"], m["angle"]) for m in plan["moves"]}
        # R19 connects with pad 2 (at +0.5 when at 0 degrees): turned to 180 so it faces T1
        self.assertEqual(moves, {"R18": (10, 10, 0), "R19": (10, 11, 180), "R20": (10, 13, 0)})
        self.assertEqual([(a["part"], a["pad"], a["on"], a["row_mm"]) for a in plan["anchors"]],
                         [("R18", "1", "T1.1", 10.0), ("R19", "2", "T1.2", 11.0), ("R20", "1", "T1.3", 13.0)])

    def test_face_anchor_off_keeps_the_given_angle(self):
        plan = PP.inline(poe_like(), ["R19"], x=10, to="T1", angle=0, face_anchor=False)
        self.assertEqual(plan["moves"][0]["angle"], 0)

    def test_connecting_pad_lands_exactly_on_the_row_when_turned(self):
        # at 90 degrees pad 1 (-0.5, 0) sits 0.5 below the origin: the origin goes 0.5 above the row
        plan = PP.inline(poe_like(), ["R18"], x=10, to="T1", angle=90, face_anchor=False)
        m = plan["moves"][0]
        self.assertEqual((m["x_mm"], m["y_mm"]), (10, 10.5))

    def test_row_mode(self):
        # y given: each part on its anchor pad's column (T1's pads are all at x 6)
        plan = PP.inline(poe_like(), ["R18", "R20"], y=5, to="T1", angle=90)
        self.assertEqual({m["ref"]: m["x_mm"] for m in plan["moves"]}, {"R18": 6, "R20": 6})

    def test_without_to_the_nearest_connected_pad_is_the_anchor(self):
        # R18 joins T1 (x 6) and J2 (x 20): a column at x 17 is nearer J2
        plan = PP.inline(poe_like(), ["R18"], x=17, angle=0)
        self.assertEqual(plan["anchors"][0]["on"], "J2.1")
        self.assertEqual(plan["moves"][0]["y_mm"], 11)

    def test_skips_and_errors(self):
        plan = PP.inline(poe_like(), ["C9", "T1"], x=10, to="T1")
        self.assertEqual({s["part"] for s in plan["skipped"]}, {"C9", "T1"})
        self.assertEqual(plan["moves"], [])
        with self.assertRaises(ValueError):
            PP.inline(poe_like(), ["R18"], x=10, y=5)
        with self.assertRaises(ValueError):
            PP.inline(poe_like(), ["R18"], x=10, to="U99")


class PlaceInlineToolTest(unittest.TestCase):
    def setUp(self):
        S._PREVIEWED_ON.clear()

    def call(self, fake, **kw):
        with mock.patch.object(S, "session", Session(fake)), mock.patch.object(S, "_move_picture", lambda *x: None):
            return json.loads(S.place_inline(**kw)[-1])

    def test_dry_run_by_default_then_one_write(self):
        fake = FakeFusion(poe_like())
        res = self.call(fake, parts=["R18", "R19", "R20"], x_mm=10, to="T1", angle=0)
        self.assertFalse(res["written"])
        self.assertEqual(fake.ran, [])
        self.assertEqual(len(res["anchors"]), 3)
        res = self.call(fake, parts=["R18", "R19", "R20"], x_mm=10, to="T1", angle=0, dry_run=False,
                        design="PoE Magnetics Test Board")
        self.assertTrue(res["written"])
        self.assertEqual(len([c for c in fake.ran if c.strip() != "UNDO;"]), 1)
        els = {e.get("name"): e for e in ET.fromstring(fake.xml).iterfind("./drawing/board/elements/element")}
        self.assertEqual([(els[r].get("x"), els[r].get("y"), els[r].get("rot") or "R0") for r in ("R18", "R19", "R20")],
                         [("10", "10", "R0"), ("10", "11", "R180"), ("10", "13", "R0")])

    def test_nothing_to_place(self):
        res = self.call(FakeFusion(poe_like()), parts=["C9"], x_mm=10, to="T1")
        self.assertEqual(res["detail"], "nothing to place")


if __name__ == "__main__":
    unittest.main()
