import unittest
import xml.etree.ElementTree as ET

from fusion_offline import placement_check as PC

from test_footprints import PKGS, el

EXTRA_PKGS = """
<package name="NOCY">
<smd name="1" x="-0.5" y="0" dx="0.6" dy="0.6" layer="1"/>
<smd name="2" x="0.5" y="0" dx="0.6" dy="0.6" layer="1"/>
<wire x1="-0.8" y1="0.5" x2="0.8" y2="0.5" width="0.1" layer="51"/>
</package>
<package name="HDR1">
<pad name="1" x="0" y="0" drill="1.0" diameter="1.7"/>
</package>
<package name="XFMR">
<smd name="1" x="0" y="0" dx="1.5" dy="0.6" layer="1"/>
<smd name="2" x="0" y="1" dx="1.5" dy="0.6" layer="1"/>
<smd name="3" x="0" y="3" dx="1.5" dy="0.6" layer="1"/>
</package>
"""


def board(elements: str, signals: dict | None = None, plain: str = "", rules: str = "") -> ET.Element:
    sig = "".join(f'<signal name="{n}">' + "".join(f'<contactref element="{r}" pad="{p}"/>' for r, p in refs)
                  + "</signal>" for n, refs in (signals or {}).items())
    xml = f"""<eagle><drawing><board>
<plain><wire x1="0" y1="0" x2="40" y2="0" width="0" layer="20"/><wire x1="40" y1="0" x2="40" y2="40" width="0" layer="20"/>
<wire x1="40" y1="40" x2="0" y2="40" width="0" layer="20"/><wire x1="0" y1="40" x2="0" y2="0" width="0" layer="20"/>{plain}</plain>
<libraries><library name="L"><packages>{PKGS}{EXTRA_PKGS}</packages></library></libraries>
<designrules name="t">{rules}</designrules><elements>{elements}</elements><signals>{sig}</signals></board></drawing></eagle>"""
    return ET.fromstring(xml)


RULES = '<param name="mdSmdSmd" value="0.15mm"/><param name="mdSmdPad" value="0.2mm"/><param name="mdPadPad" value="0.25mm"/>'


class PadGapTest(unittest.TestCase):
    def test_gap_under_rule_between_nets(self):
        # R1.2 copper ends at x 5.8, R2.1 starts at 5.9: 0.1 mm against a 0.15 mm SMD-SMD rule
        root = board(el("R1", 5, 5) + el("R2", 6.7, 5), {"A": [("R1", "2")], "B": [("R2", "1")]}, rules=RULES)
        res = PC.check(root)
        v = res["pad_gaps"]["violations"]
        self.assertEqual(len(v), 1)
        self.assertEqual((v[0]["a"], v[0]["b"], v[0]["rule"]), ("R1.2", "R2.1", "mdSmdSmd"))
        self.assertAlmostEqual(v[0]["gap_mm"], 0.1, places=3)
        self.assertAlmostEqual(v[0]["short_by_mm"], 0.05, places=3)

    def test_same_net_and_far_pads_pass(self):
        same = board(el("R1", 5, 5) + el("R2", 6.7, 5), {"A": [("R1", "2"), ("R2", "1")]}, rules=RULES)
        self.assertEqual(PC.check(same)["pad_gaps"]["count"], 0)
        far = board(el("R1", 5, 5) + el("R2", 6.8, 5), {"A": [("R1", "2")], "B": [("R2", "1")]}, rules=RULES)
        self.assertEqual(PC.check(far)["pad_gaps"]["count"], 0)

    def test_tht_pad_uses_pad_rule_and_restring(self):
        # 1.7 mm pad on 1.0 drill (ring 0.25 x 1.0 = 0.25 -> 1.5, so the library 1.7 wins);
        # its edge is at 10.85; the 0402 pad starts at 11.0 -> 0.15 < 0.2 SMD-pad
        root = board(el("J1", 10, 10, "", "HDR1") + el("R1", 11.8, 10), {"A": [("J1", "1")], "B": [("R1", "1")]},
                     rules=RULES)
        v = PC.check(root)["pad_gaps"]["violations"]
        self.assertEqual(v[0]["rule"], "mdSmdPad")
        self.assertAlmostEqual(v[0]["gap_mm"], 0.15, delta=0.01)   # the circle is a 16-gon, never larger than true


class SilkTest(unittest.TestCase):
    def test_board_line_over_a_pad(self):
        line = '<wire x1="4" y1="5" x2="4.8" y2="5" width="0.15" layer="21"/>'
        root = board(el("R1", 5, 5), plain=line)
        res = PC.check(root)["silk_on_pads"]
        self.assertEqual([(h["silk"], h["pad"], h["kind"]) for h in res["board_silk"]], [("board line", "R1.1", "board")])
        self.assertEqual(res["counts"], {"other_parts_pads": 0, "board_silk": 1, "part_names": 0, "own_pads_library": 0})

    def test_package_silk_clear_of_pads_and_bottom_silk_ignores_top_pads(self):
        line = '<wire x1="4" y1="5" x2="4.8" y2="5" width="0.15" layer="22"/>'
        root = board(el("R1", 5, 5), plain=line)
        # RES_0402's silk line (y 0.35..0.45) and centre rect (touching the pad edges) are clear;
        # its >NAME text at y 0.7 is clear too
        self.assertEqual(sum(PC.check(root)["silk_on_pads"]["counts"].values()), 0)

    def test_neighbour_outline_over_pads_is_listed(self):
        # R2 sits 0.3 mm above R1: R1's silk line (5.35..5.45) lands on R2's pads (4.996..5.6)
        root = board(el("R1", 5, 5) + el("R2", 5, 5.3))
        res = PC.check(root)["silk_on_pads"]
        mine = [e for e in res["other_parts_pads"] if (e["silk_of"], e["on_pads_of"]) == ("R1", "R2")]
        self.assertEqual(len(mine), 1)                          # one entry for the pair, both pads named
        self.assertEqual(mine[0]["pads"], ["1", "2"])

    def test_name_text_over_pad_is_marked_approximate(self):
        smashed = '<attribute name="NAME" x="4.5" y="4.9" size="0.5" layer="25"/>'
        root = board(el("R1", 5, 5, extra=smashed))
        # "R1" at size 0.5 is about 0.8 mm wide from x 4.5: over pad 1 (4.2..4.8) and pad 2 (5.2..5.8)
        res = PC.check(root)["silk_on_pads"]
        self.assertEqual(res["counts"]["part_names"], 2)
        self.assertEqual(res["other_parts_pads"], [])
        hits = PC.silk_on_pads(PC.silk_shapes(root), PC.pads(root))
        self.assertEqual((hits[0]["silk"], hits[0]["kind"], hits[0]["note"]), ("R1 name R1", "name", "text extent estimated"))


class AlmostAlignedTest(unittest.TestCase):
    def test_one_stray_part_reports_the_whole_row(self):
        # R1..R3 share y 10; R4 is 0.125 mm low: one group of four, not three pairs
        parts = el("R1", 10, 10) + el("R2", 11, 10) + el("R3", 12, 10) + el("R4", 13, 9.875)
        near = PC.check(board(parts))["tidiness"]["almost_aligned"]
        self.assertEqual(near, [{"axis": "row", "parts": ["R1", "R2", "R3", "R4"], "spread_mm": 0.125}])

    def test_two_grid_steps_apart_is_deliberate(self):
        near = PC.check(board(el("R1", 10, 10) + el("R2", 11, 10.25)))["tidiness"]["almost_aligned"]
        self.assertEqual(near, [])


class AlignmentTest(unittest.TestCase):
    def test_series_part_offset_from_its_pin_row(self):
        root = board(el("R18", 10, 10) + el("J1", 5, 10.2, "", "HDR1") + el("R19", 10, 12) + el("U1", 15, 12, "", "RECT_CY"),
                     {"N1": [("R18", "1"), ("J1", "1")], "N2": [("R19", "2"), ("U1", "1")]})
        al = PC.check(root, alignment=True)["alignment"]
        self.assertEqual(al["aligned_count"], 1)                 # R19.2 sits on U1's row
        self.assertEqual([(a["part"], a["pad"], a["to"], a["along"], a["offset_mm"]) for a in al["off"]],
                         [("R18", "1", "J1.1", "row", 0.2)])


class TidinessTest(unittest.TestCase):
    def test_grid_rows_rotation_and_pitch(self):
        parts = (el("R1", 10, 10) + el("R2", 11, 10) + el("R3", 12.25, 10, "R180")     # row: pitches 1.0, 1.25
                 + el("R4", 10.06, 20) + el("R5", 12, 20.125)                            # R4 off grid; almost a row
                 + el("J1", 30, 30.3, "", "HDR1"))                                       # 30.3 is off the 0.25 grid
        t = PC.check(board(parts))["tidiness"]
        self.assertEqual({o["part"] for o in t["off_grid"]}, {"R4", "J1"})
        self.assertEqual({o["part"]: o["nearest"] for o in t["off_grid"]}["J1"], [30.0, 30.25])
        self.assertIn({"axis": "row", "parts": ["R4", "R5"], "spread_mm": 0.125}, t["almost_aligned"])
        self.assertEqual(t["mixed_rotation"][0]["angles"], {"R1": 0.0, "R2": 0.0, "R3": 180.0})
        self.assertEqual(t["uneven_spacing"][0]["pitches_mm"], [1.0, 1.25])

    def test_grid_override(self):
        t = PC.check(board(el("R4", 10.06, 20)), grid={"passive": 0.02})["tidiness"]
        self.assertEqual(t["off_grid_count"], 0)


class CourtyardFallbackTest(unittest.TestCase):
    def test_missing_courtyards_listed_or_derived(self):
        root = board(el("R1", 5, 5) + el("C1", 5, 6.3, "", "NOCY"))
        res = PC.check(root)
        self.assertEqual(res["courtyards"]["no_courtyard"], ["C1"])
        self.assertEqual(res["courtyards"]["overlap_count"], 0)
        self.assertIn("1 parts have no courtyard", res["summary"])
        # derived: C1's pads reach 0.3 below its origin, plus a 0.7 margin -> down to 6.3 - 1.0 = 5.3;
        # R1's courtyard reaches up to 5.5, so they overlap by 0.2
        res = PC.check(root, derive_missing=True, margin=0.7)
        ov = res["courtyards"]["overlaps"]
        self.assertEqual((ov[0]["a"], ov[0]["b"], ov[0]["derived"]), ("C1", "R1", ["C1"]))
        self.assertAlmostEqual(ov[0]["overlap_mm"], 0.2, places=3)
        self.assertEqual(res["courtyards"]["derived"], ["C1"])


class LiveRunFeedbackTest(unittest.TestCase):
    """Cases from the live run on the PoE Magnetics Test Board (2026-10-07)."""

    def test_marker_inside_module_outline_is_inside_not_overlap(self):
        # J4/J5 sit inside the CM4 outline U1, which holds only 2 parts (below the enclosing rule)
        root = board(el("U1", 15, 15, "", "SOM") + el("J4", 12, 12, "", "RECT_CY") + el("J5", 18, 18, "", "RECT_CY"))
        res = PC.check(root)["courtyards"]
        self.assertEqual(res["overlap_count"], 0)
        self.assertEqual({(c["a"], c["b"], c["container"]) for c in res["inside"]}, {("J4", "U1", "U1"), ("J5", "U1", "U1")})

    def test_same_size_parts_on_top_of_each_other_still_overlap(self):
        root = board(el("R1", 5, 5) + el("R2", 5.2, 5))
        self.assertEqual(PC.check(root)["courtyards"]["overlap_count"], 1)

    def test_stacked_parts_are_judged_as_a_column(self):
        # C1 directly under R1, same x: aligned, not "2 mm off along row"
        root = board(el("R1", 10, 10) + el("C1", 10, 8), {"N1": [("R1", "1"), ("C1", "1")]})
        al = PC.series_alignment(PC.pads(root))
        self.assertEqual([(a["part"], a["along"], a["offset_mm"], a["aligned"]) for a in al][0], ("C1", "column", 0.0, True))

    def test_far_partners_are_ignored(self):
        root = board(el("LED1", 10, 10) + el("J1", 25, 10, "", "HDR1"), {"N1": [("LED1", "1"), ("J1", "1")]})
        self.assertEqual(PC.series_alignment(PC.pads(root)), [])
        self.assertEqual(len(PC.series_alignment(PC.pads(root), reach_mm=20)), 1)

    def test_better_aligned_end_wins_and_other_end_is_reported(self):
        # R18: pad 1 on T1's row (3.5 mm away), pad 2 to J2 1 mm off its row at the other end
        root = board(el("T1", 6, 10, "", "XFMR") + el("R18", 10, 10) + el("J2", 14, 11, "", "HDR1"),
                     {"A": [("R18", "1"), ("T1", "1")], "B": [("R18", "2"), ("J2", "1")]})
        a = PC.series_alignment(PC.pads(root))[0]
        self.assertEqual((a["to"], a["aligned"], a["other_end"]["to"], a["other_end"]["offset_mm"]),
                         ("T1.1", True, "J2.1", 1.0))

    def test_column_following_pin_pitch_is_not_uneven(self):
        # R18..R20 at T1's pad rows (pitch 1 then 2): follows T1, not uneven
        parts = el("T1", 6, 10, "", "XFMR") + el("R18", 10, 10) + el("R19", 10, 11) + el("R20", 10, 13)
        sig = {"A": [("R18", "1"), ("T1", "1")], "B": [("R19", "1"), ("T1", "2")], "C": [("R20", "1"), ("T1", "3")]}
        t = PC.check(board(parts, sig))["tidiness"]
        self.assertEqual(t["uneven_spacing"], [])
        self.assertEqual(t["follows_pin_pitch"], [{"axis": "column", "parts": ["R18", "R19", "R20"],
                                                   "follows_pins_of": ["T1"]}])
        t = PC.check(board(parts))["tidiness"]                     # same spacing, no connections: uneven
        self.assertEqual(t["uneven_spacing"][0]["pitches_mm"], [1.0, 2.0])

    def test_derived_courtyard_sources(self):
        root = board(el("C1", 5, 5, "", "NOCY"))
        top = {src: max(y for _, y in PC.derived_courtyards(root, {}, 0.25, src)["C1"].polygons[0])
               for src in ("pads", "body")}
        self.assertAlmostEqual(top["pads"], 5 + 0.3 + 0.25)        # pad edge
        self.assertAlmostEqual(top["body"], 5 + 0.5 + 0.25)        # tDocu line
        with self.assertRaises(ValueError):
            PC.check(root, derive_from="silk")


class ToolRegisteredTest(unittest.TestCase):
    def test_check_placement_is_a_tool(self):
        import asyncio
        from fusion_mcp.server import mcp
        self.assertIn("check_placement", {t.name for t in asyncio.run(mcp.list_tools())})


if __name__ == "__main__":
    unittest.main()
