"""Review fixes for tidy_placement: routed pads, rigid pair groups, place_inline rows, the final
re-check, and locked parts."""

import unittest
from unittest import mock

from fusion_offline import placement_plan as PP

from test_placement_check import el
from test_tidy_placement import board, moves


class RoutedInsidePadTest(unittest.TestCase):
    def test_trace_ending_off_centre_inside_the_pad(self):
        # R1.1 is centred at (9.56, 10), 0.6 x 0.6; the trace ends 0.2 mm off centre, still on copper
        wire = '<signal name="A"><wire x1="9.76" y1="10.1" x2="5" y2="10" width="0.2" layer="1"/></signal>'
        root = board(el("R1", 10.06, 10), {"A": [("R1", "1")]}, extra_signals=wire)
        self.assertEqual(PP.routed_parts(root, PP.pads(root)), {"R1"})
        plan = PP.tidy(root)
        self.assertEqual(plan["moves"], [])
        self.assertTrue(plan["left_alone"][0]["why"].startswith("routed"))

    def test_via_inside_the_pad(self):
        via = '<signal name="A"><via x="9.66" y="9.9" extent="1-16" drill="0.3"/></signal>'
        root = board(el("R1", 10.06, 10), {"A": [("R1", "1")]}, extra_signals=via)
        self.assertEqual(PP.routed_parts(root, PP.pads(root)), {"R1"})

    def test_trace_clear_of_the_pad_or_on_the_other_side(self):
        clear = '<signal name="A"><wire x1="8.9" y1="10" x2="5" y2="10" width="0.2" layer="1"/></signal>'
        root = board(el("R1", 10.06, 10), {"A": [("R1", "1")]}, extra_signals=clear)
        self.assertEqual(PP.routed_parts(root, PP.pads(root)), set())
        bottom = '<signal name="A"><wire x1="9.56" y1="10" x2="5" y2="10" width="0.2" layer="16"/></signal>'
        root = board(el("R1", 10.06, 10), {"A": [("R1", "1")]}, extra_signals=bottom)
        self.assertEqual(PP.routed_parts(root, PP.pads(root)), set())    # top pad, bottom trace

    def test_rotated_pad(self):
        # R90: R1.1 sits at (10, 9.5) and is 0.6 x 0.6; an end at (10.25, 9.3) is on it
        wire = '<signal name="A"><wire x1="10.25" y1="9.3" x2="5" y2="9" width="0.1" layer="1"/></signal>'
        root = board(el("R1", 10, 10, "R90"), {"A": [("R1", "1")]}, extra_signals=wire)
        self.assertEqual(PP.routed_parts(root, PP.pads(root)), {"R1"})


class PairGroupDropTest(unittest.TestCase):
    def test_dropping_one_member_drops_the_group(self):
        # R28/R29 on USB_P/USB_N 0.05 mm off the grid; R3 (kept) is 0.01 mm clear of R29, so R29's
        # 0.05 mm shift left would overlap it. The group must stay together: neither moves.
        parts = el("R28", 10.05, 10) + el("R29", 10.05, 11) + el("R3", 8.04, 11)
        sig = {"USB_P": [("R28", "1")], "USB_N": [("R29", "1")]}
        plan = PP.tidy(board(parts, sig), keep={"R3"})
        self.assertEqual(plan["moves"], [])
        dropped = {d["part"]: d["why"] for d in plan["dropped"]}
        self.assertEqual(set(dropped), {"R28", "R29"})
        self.assertIn("pair USB group moves only as one", dropped["R28"])


class PlaceInlineKeptTest(unittest.TestCase):
    def test_grid_snap_keeps_a_part_on_its_anchor_row(self):
        # place_inline put R1.1 exactly on U1.2's row (y 9.03), off the 0.125 grid
        parts = el("U1", 10, 10.03, "", "QUAD") + el("R1", 13, 9.03)
        root = board(parts, {"A": [("U1", "2"), ("R1", "1")]})
        plan = PP.tidy(root, keep={"U1"})
        self.assertNotIn("R1", moves(plan))

    def test_other_axis_still_snaps(self):
        parts = el("U1", 10, 10.03, "", "QUAD") + el("R1", 13.06, 9.03)
        root = board(parts, {"A": [("U1", "2"), ("R1", "1")]})
        self.assertEqual(moves(PP.tidy(root, keep={"U1"})), {"R1": (13.0, 9.03, 0.0)})

    def test_row_alignment_does_not_pull_it_off_either(self):
        # R2 and R3 sit on y 9.0; R1 is 0.03 above them on U1.2's row and must stay there
        parts = el("U1", 10, 10.03, "", "QUAD") + el("R1", 13, 9.03) + el("R2", 15.5, 9) + el("R3", 18, 9)
        root = board(parts, {"A": [("U1", "2"), ("R1", "1")]})
        self.assertNotIn("R1", moves(PP.tidy(root, keep={"U1"})))


class FinalRecheckTest(unittest.TestCase):
    def test_never_returns_unchecked_moves(self):
        # each check blames only the first moved part, so ten movers outlast the eight passes
        parts = "".join(el(f"R{i}", 2 + 3 * i + 0.06, 10) for i in range(1, 11))
        root = board(parts)
        real = PP.move_effects

        def effects(before, after, refs):
            res = real(before, after, refs)
            first = sorted(refs, key=PP._natural)[0]
            res["introduced"] = [{"kind": "courtyard", "a": f"{first}.1", "b": "X.1"}]
            return res
        with mock.patch.object(PP, "move_effects", effects):
            plan = PP.tidy(root)
        self.assertEqual(plan["moves"], [])
        self.assertEqual(len(plan["dropped"]), 10)


class LockedTest(unittest.TestCase):
    def test_locked_part_left_alone(self):
        locked = '<element name="R1" library="L" package="RES_0402" value="0" x="10.06" y="10" locked="yes"/>'
        plan = PP.tidy(board(locked + el("R2", 20.06, 10)))
        self.assertEqual(set(moves(plan)), {"R2"})
        self.assertIn({"part": "R1", "why": "locked in Fusion: unlock it to let tidy move it"}, plan["left_alone"])

    def test_locked_crystal_not_nudged(self):
        locked = '<element name="Y1" library="L" package="RECT_CY" value="0" x="10.1" y="10" locked="yes"/>'
        self.assertEqual(PP.tidy(board(locked))["moves"], [])


if __name__ == "__main__":
    unittest.main()
