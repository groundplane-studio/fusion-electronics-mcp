import json
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

import test_placement_check as TP
from fusion_mcp import server as S
from fusion_mcp.session import Session
from fusion_offline import placement_plan as PP

from test_move_parts import FakeFusion
from test_placement_check import el

QUAD = """
<package name="QUAD">
<smd name="1" x="-1" y="-1" dx="0.5" dy="0.5" layer="1"/>
<smd name="2" x="1" y="-1" dx="0.5" dy="0.5" layer="1"/>
<smd name="3" x="1" y="1" dx="0.5" dy="0.5" layer="1"/>
<smd name="4" x="-1" y="1" dx="0.5" dy="0.5" layer="1"/>
<wire x1="-1.5" y1="-1.5" x2="1.5" y2="-1.5" width="0.05" layer="39"/>
<wire x1="1.5" y1="-1.5" x2="1.5" y2="1.5" width="0.05" layer="39"/>
<wire x1="1.5" y1="1.5" x2="-1.5" y2="1.5" width="0.05" layer="39"/>
<wire x1="-1.5" y1="1.5" x2="-1.5" y2="-1.5" width="0.05" layer="39"/>
</package>
"""
if "QUAD" not in TP.EXTRA_PKGS:
    TP.EXTRA_PKGS += QUAD


def board(elements, signals=None, plain="", extra_signals=""):
    root = TP.board(elements, signals, plain)
    if extra_signals:
        sigs = root.find("./drawing/board/signals")
        for s in ET.fromstring(f"<x>{extra_signals}</x>"):
            name = s.get("name")
            mine = next((x for x in sigs if x.get("name") == name), None)
            if mine is None:
                sigs.append(s)
            else:
                for child in s:
                    mine.append(child)
    return root


def moves(plan):
    return {m["ref"]: (m["x_mm"], m["y_mm"], m["angle"]) for m in plan["moves"]}


class PairNetsTest(unittest.TestCase):
    def test_partners(self):
        got = PP.pair_nets(["USB_DP", "USB_DN", "TP0_P", "TP0_N", "CLK+", "CLK-", "GND", "VCC", "EN", "TP1_P"])
        self.assertEqual(got, {"USB_DP": "USB", "USB_DN": "USB", "TP0_P": "TP0", "TP0_N": "TP0",
                               "CLK+": "CLK", "CLK-": "CLK"})


class FreePartsTest(unittest.TestCase):
    def test_grid_snap(self):
        plan = PP.tidy(board(el("R1", 10.06, 10)))
        self.assertEqual(moves(plan), {"R1": (10.0, 10.0, 0.0)})
        self.assertEqual(plan["actions"]["R1"], ["onto the 0.125 mm grid"])

    def test_connector_grid_is_coarser(self):
        plan = PP.tidy(board(el("J1", 10.1, 10, "", "HDR1")))
        self.assertEqual(moves(plan)["J1"][:2], (10.0, 10.0))

    def test_near_row_lines_up_with_the_majority(self):
        plan = PP.tidy(board(el("R1", 10, 10) + el("R2", 12, 10) + el("R3", 14, 10.125)))
        self.assertEqual(moves(plan), {"R3": (14, 10, 0.0)})
        self.assertIn("lined up in a row with R1, R2", plan["actions"]["R3"][0])

    def test_two_steps_apart_is_left(self):
        self.assertEqual(PP.tidy(board(el("R1", 10, 10) + el("R2", 12, 10.25)))["moves"], [])

    def test_slightly_uneven_pitch_is_evened(self):
        plan = PP.tidy(board(el("R1", 10, 10) + el("R2", 12, 10) + el("R3", 14.25, 10)))
        self.assertEqual(moves(plan), {"R2": (12.125, 10, 0.0)})            # 2.125 mm pitch, ends kept

    def test_rotation_only_when_asked_and_only_non_polar(self):
        parts = el("R1", 10, 10) + el("R2", 12, 10) + el("R3", 14, 10, "R180") + el("D1", 10, 20) + el("D2", 12, 20, "R180")
        self.assertEqual(PP.tidy(board(parts))["moves"], [])
        self.assertEqual(moves(PP.tidy(board(parts), rotate=True)), {"R3": (14, 10, 0.0)})


class CriticalTest(unittest.TestCase):
    def test_pair_group_moves_as_one(self):
        # R28/R29 series on USB_P/USB_N and D1 (TVS) from USB_P to GND, all 0.05 mm off the grid in x
        parts = el("R28", 10.05, 10) + el("R29", 10.05, 11) + el("D1", 13.05, 10)
        sig = {"USB_P": [("R28", "1"), ("D1", "1")], "USB_N": [("R29", "1")], "GND": [("D1", "2")]}
        plan = PP.tidy(board(parts, sig))
        self.assertEqual(moves(plan), {"R28": (10.0, 10, 0.0), "R29": (10.0, 11, 0.0), "D1": (13.0, 10, 0.0)})
        self.assertIn("pair USB group moved 0.050 mm as one", plan["actions"]["D1"][0])

    def test_pair_group_stays_put_when_a_member_is_routed(self):
        parts = el("R28", 10.05, 10) + el("R29", 10.05, 11)
        sig = {"USB_P": [("R28", "1")], "USB_N": [("R29", "1")]}
        wire = '<signal name="USB_N"><wire x1="9.55" y1="11" x2="5" y2="11" width="0.2" layer="1"/></signal>'
        plan = PP.tidy(board(parts, sig, extra_signals=wire))
        self.assertEqual(plan["moves"], [])
        whys = {l["part"]: l["why"] for l in plan["left_alone"]}
        self.assertTrue(whys["R29"].startswith("routed"))
        self.assertIn("moves only as one, and R29 cannot move", whys["R28"])

    def test_pair_group_never_spread_or_aligned(self):
        # R28 sits 0.125 off R1/R2's row: a free part would be pulled in, the pair member is not
        parts = el("R1", 6, 10) + el("R2", 8, 10) + el("R28", 10, 10.125) + el("R29", 10, 11.125)
        sig = {"USB_P": [("R28", "1")], "USB_N": [("R29", "1")]}
        self.assertNotIn("R28", moves(PP.tidy(board(parts, sig))))

    def test_crystal_nudged_only_within_the_limit(self):
        self.assertEqual(moves(PP.tidy(board(el("Y1", 10.1, 10, "", "RECT_CY")))), {"Y1": (10.0, 10, 0.0)})
        plan = PP.tidy(board(el("Y1", 10.1, 10, "", "RECT_CY")), nudge=0.05)
        self.assertEqual(plan["moves"], [])
        self.assertIn("critical: crystal", plan["left_alone"][0]["why"])

    def test_keep_leaves_a_critical_part_exactly_as_placed(self):
        # T1 on the PoE board: net class 7, 0.125 mm off the grid, left where the user put it
        root = board(el("Y1", 10.1, 10, "", "RECT_CY") + el("R1", 20.06, 10))
        plan = PP.tidy(root, keep={"Y1"})
        self.assertEqual(set(moves(plan)), {"R1"})
        self.assertIn({"part": "Y1", "why": "kept as placed (keep)"}, plan["left_alone"])

    def test_isolation_bridges(self):
        parts = el("R5", 10.06, 10) + el("C5", 20.06, 10) + el("C6", 30.06, 10)
        sig = {"SHIELD": [("R5", "1")], "GND": [("R5", "2")], "N$9": [("C5", "1")], "N$10": [("C5", "2")],
               "A": [("C6", "1")], "B": [("C6", "2")]}
        pours = ('<signal name="N$9"><polygon layer="1"><vertex x="15" y="5"/><vertex x="19.9" y="5"/>'
                 '<vertex x="19.9" y="15"/><vertex x="15" y="15"/></polygon></signal>'
                 '<signal name="N$10"><polygon layer="1"><vertex x="20.2" y="5"/><vertex x="25" y="5"/>'
                 '<vertex x="25" y="15"/><vertex x="20.2" y="15"/></polygon></signal>')
        root = board(parts, sig, extra_signals=pours)
        why, _ = PP.critical_parts(root)
        self.assertEqual(why["R5"], "isolation bridge (SHIELD to GND)")
        self.assertEqual(why["C5"], "isolation bridge (pads on the N$10 and N$9 pours)")
        self.assertNotIn("C6", why)                          # plain part, free to tidy
        self.assertEqual(set(moves(PP.tidy(root, nudge=0.01))), {"C6"})

    def test_decoupling_cap_and_listed_parts(self):
        parts = el("U1", 10, 10, "", "QUAD") + el("C1", 13, 9) + el("C2", 30, 9) + el("R7", 40.06, 9)
        sig = {"3V3": [("U1", "2"), ("C1", "1"), ("C2", "1")], "GND": [("C1", "2"), ("C2", "2")]}
        why, _ = PP.critical_parts(board(parts, sig), extra={"R7"})
        self.assertEqual(why["C1"], "decoupling U1 on 3V3")
        self.assertNotIn("C2", why)                          # 3V3 cap far from any IC pin
        self.assertEqual(why["R7"], "listed in critical")

    def test_net_class(self):
        root = board(el("R1", 10.06, 10), {"ETH": [("R1", "1")]})
        root.find("./drawing/board/signals/signal").set("class", "7")
        self.assertEqual(PP.critical_parts(root)[0]["R1"], "net ETH has net class 7")


class SafetyTest(unittest.TestCase):
    def test_never_introduces_an_overlap(self):
        # R2's courtyard is 0.07 mm clear of R1's; the 0.125 grid would put them 0.125 into each other
        plan = PP.tidy(board(el("R1", 10, 10) + el("R2", 10, 10.93)))
        self.assertEqual(plan["moves"], [])
        self.assertEqual(plan["dropped"][0]["part"], "R2")

    def test_rework_gap_never_shrinks(self):
        root = board(el("R1", 10, 10) + el("R2", 10, 11.06))      # 0.06 apart; the grid makes them touch
        self.assertEqual(moves(PP.tidy(root)), {"R2": (10, 11.0, 0.0)})
        plan = PP.tidy(root, rework_gap=0.5)
        self.assertEqual(plan["moves"], [])
        self.assertIn("courtyard gap to R1 would drop to 0.000", plan["dropped"][0]["why"])

    def test_max_move_and_scope(self):
        root = board(el("R1", 10.06, 10) + el("R2", 20.06, 10))
        self.assertEqual(PP.tidy(root, max_move=0.01)["moves"], [])
        self.assertEqual(set(moves(PP.tidy(root, refs=["R2"]))), {"R2"})
        self.assertEqual(set(moves(PP.tidy(root, region=(5, 5, 15, 15)))), {"R1"})

    def test_routed_parts_left_unless_allowed(self):
        wire = '<signal name="A"><wire x1="9.56" y1="10" x2="5" y2="10" width="0.2" layer="1"/></signal>'
        root = board(el("R1", 10.06, 10), {"A": [("R1", "1")]}, extra_signals=wire)
        self.assertEqual(PP.tidy(root)["moves"], [])
        self.assertEqual(set(moves(PP.tidy(root, skip_routed=False))), {"R1"})


class TidyToolTest(unittest.TestCase):
    def setUp(self):
        S._PREVIEWED_ON.clear()

    def call(self, fake, **kw):
        with mock.patch.object(S, "session", Session(fake)), mock.patch.object(S, "_move_picture", lambda *x: None):
            return json.loads(S.tidy_placement(**kw)[-1])

    def test_dry_run_then_write(self):
        fake = FakeFusion(board(el("R1", 10.06, 10) + el("R2", 12, 10.1)))
        res = self.call(fake)
        self.assertFalse(res["written"])
        self.assertEqual(fake.ran, [])
        self.assertEqual(set(res["actions"]), {"R1", "R2"})
        res = self.call(fake, dry_run=False, design="PoE Magnetics Test Board")
        self.assertTrue(res["written"])
        els = {e.get("name"): e for e in ET.fromstring(fake.xml).iterfind("./drawing/board/elements/element")}
        self.assertEqual([(els[r].get("x"), els[r].get("y")) for r in ("R1", "R2")], [("10", "10"), ("12", "10")])

    def test_nothing_to_tidy(self):
        res = self.call(FakeFusion(board(el("R1", 10, 10))))
        self.assertEqual(res["detail"], "nothing to tidy")


if __name__ == "__main__":
    unittest.main()
