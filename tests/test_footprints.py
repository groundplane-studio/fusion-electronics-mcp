import os
import random
import tempfile
import unittest
import xml.etree.ElementTree as ET

from fusion_offline import footprints as FP
from fusion_offline import render as R

# RES_0402 as in Groundplane's library: pads at +-0.5 mm, a 2.0 x 1.0 mm courtyard drawn as
# four tKeepout wires; RECT_CY draws its courtyard as a rectangle instead.
PKGS = """
<package name="RES_0402">
<smd name="1" x="-0.5" y="0" dx="0.6" dy="0.6" layer="1"/>
<smd name="2" x="0.5" y="0" dx="0.6" dy="0.6" layer="1"/>
<wire x1="-1" y1="-0.5" x2="1" y2="-0.5" width="0.05" layer="39"/>
<wire x1="1" y1="0.5" x2="1" y2="-0.5" width="0.05" layer="39"/>
<wire x1="1" y1="0.5" x2="-1" y2="0.5" width="0.05" layer="39"/>
<wire x1="-1" y1="-0.5" x2="-1" y2="0.5" width="0.05" layer="39"/>
<wire x1="-0.9" y1="0.4" x2="0.9" y2="0.4" width="0.1" layer="21"/>
<rectangle x1="-0.2" y1="-0.1" x2="0.2" y2="0.1" layer="21"/>
<text x="0" y="0.7" size="0.5" layer="25">&gt;NAME</text>
</package>
<package name="RECT_CY">
<smd name="1" x="0" y="0" dx="1" dy="1" layer="1"/>
<rectangle x1="-1" y1="-1" x2="1" y2="1" layer="39"/>
</package>
<package name="SOM">
<smd name="1" x="0" y="0" dx="1" dy="1" layer="1"/>
<rectangle x1="-8" y1="-8" x2="8" y2="8" layer="39"/>
</package>
"""


def board(elements: str) -> ET.Element:
    xml = f"""<eagle><drawing><board>
<plain><wire x1="0" y1="0" x2="30" y2="0" width="0" layer="20"/><wire x1="30" y1="0" x2="30" y2="30" width="0" layer="20"/>
<wire x1="30" y1="30" x2="0" y2="30" width="0" layer="20"/><wire x1="0" y1="30" x2="0" y2="0" width="0" layer="20"/></plain>
<libraries><library name="L"><packages>{PKGS}</packages></library></libraries>
<designrules name="t"/><elements>{elements}</elements><signals/></board></drawing></eagle>"""
    return ET.fromstring(xml)


def el(name, x, y, rot="", pkg="RES_0402", extra=""):
    r = f' rot="{rot}"' if rot else ""
    return f'<element name="{name}" library="L" package="{pkg}" value="0" x="{x}" y="{y}"{r}>{extra}</element>'


class CourtyardTest(unittest.TestCase):
    def test_0402_at_1mm_pitch_touch_and_closer_overlaps(self):
        # the PoE board case: R0 parts stacked at 1.0 mm pitch only touch
        root = board(el("R18", 10, 10) + el("R19", 10, 11) + el("R20", 10, 11.8))
        cys = FP.courtyards(root)
        self.assertEqual(sorted(cys), ["R18", "R19", "R20"])
        res = {(c["a"], c["b"]): c for c in FP.courtyard_conflicts(cys)}
        self.assertEqual(res[("R18", "R19")]["status"], "touching")
        self.assertEqual(res[("R18", "R19")]["gap_mm"], 0.0)
        ov = res[("R19", "R20")]
        self.assertEqual(ov["status"], "overlap")
        self.assertAlmostEqual(ov["overlap_mm"], 0.2, places=3)
        self.assertAlmostEqual(ov["area_mm2"], 0.4, places=3)
        self.assertNotIn(("R18", "R20"), res)

    def test_rotation_mirroring_and_sides(self):
        # R90 turns the courtyard 1.0 wide in x; side by side at 1.0 mm pitch they touch.
        # A bottom part right on top of a top part is not a conflict.
        root = board(el("R1", 5, 5, "R90") + el("R2", 6, 5, "R270") + el("R3", 5, 5, "MR90"))
        cys = FP.courtyards(root)
        self.assertEqual(cys["R3"].side, "bottom")
        res = FP.courtyard_conflicts(cys)
        self.assertEqual([(c["a"], c["b"], c["status"]) for c in res], [("R1", "R2", "touching")])

    def test_gap_and_near(self):
        root = board(el("R1", 5, 5) + el("R2", 5, 6.3))
        cys = FP.courtyards(root)
        self.assertEqual(FP.courtyard_conflicts(cys), [])
        near = FP.courtyard_conflicts(cys, near_mm=0.5)
        self.assertEqual(near[0]["status"], "near")
        self.assertAlmostEqual(near[0]["gap_mm"], 0.3, places=3)

    def test_rectangle_courtyard_and_45_degrees(self):
        # a 2 x 2 square turned 45 degrees reaches sqrt(2) from its centre
        root = board(el("U1", 5, 5, "R45", "RECT_CY") + el("U2", 7.3, 5, "", "RECT_CY"))
        res = FP.courtyard_conflicts(FP.courtyards(root))
        self.assertEqual(res[0]["status"], "overlap")
        self.assertAlmostEqual(res[0]["overlap_mm"], 2 ** 0.5 + 1 - 2.3, places=3)

    def test_vertex_to_vertex_gap_is_euclidean(self):
        # diagonal neighbours: the separating axis alone would under-report this gap
        root = board(el("U1", 5, 5, "", "RECT_CY") + el("U2", 8, 8, "", "RECT_CY"))
        res = FP.courtyard_conflicts(FP.courtyards(root), near_mm=2.0)
        self.assertAlmostEqual(res[0]["gap_mm"], 2 ** 0.5, places=3)

    def test_filter_by_part(self):
        root = board(el("R1", 5, 5) + el("R2", 5, 5.5) + el("R3", 20, 5) + el("R4", 20, 5.5))
        res = FP.courtyard_conflicts(FP.courtyards(root), refs=["R3"])
        self.assertEqual([(c["a"], c["b"]) for c in res], [("R3", "R4")])

    def test_chain_wires_any_order_and_direction(self):
        sq = [(0, 0, 1, 0, 0), (1, 0, 1, 1, 0), (1, 1, 0, 1, 0), (0, 1, 0, 0, 0)]
        rnd = random.Random(3)
        for _ in range(10):
            ws = [w if rnd.random() < 0.5 else (w[2], w[3], w[0], w[1], -w[4]) for w in sq]
            rnd.shuffle(ws)
            loops, chains = FP.chain_wires(ws)
            self.assertEqual((len(loops), chains), (1, []))
            self.assertAlmostEqual(FP.area(loops[0][:-1]), 1.0, places=6)

    def test_open_outline_uses_hull_and_says_so(self):
        pk = """<package name="OPEN"><wire x1="-1" y1="-1" x2="1" y2="-1" width="0.05" layer="39"/>
<wire x1="1" y1="-1" x2="1" y2="1" width="0.05" layer="39"/></package>"""
        global PKGS
        saved, PKGS = PKGS, PKGS + pk
        try:
            cys = FP.courtyards(board(el("J1", 5, 5, "", "OPEN")))
        finally:
            PKGS = saved
        self.assertFalse(cys["J1"].exact)


class EnclosingAndSummaryTest(unittest.TestCase):
    def test_module_outline_is_skipped_not_reported(self):
        # U8's keepout drawing covers the parts placed inside it (RV1126B SoM): without the
        # skip every one of them is an "overlap"
        inside = "".join(el(f"C{i}", 10 + 2.5 * i, 15) for i in range(4))
        root = board(el("U8", 15, 15, "", "SOM") + inside + el("R1", 10, 15.8) + el("R9", 26, 5))
        cys = FP.courtyards(root)
        self.assertEqual(FP.enclosing(cys), {"U8": 5})
        self.assertEqual(len([c for c in FP.courtyard_conflicts(cys) if c["a"] == "U8" or c["b"] == "U8"]), 5)
        res = FP.courtyard_conflicts(cys, ignore=FP.enclosing(cys))
        self.assertFalse(any("U8" in (c["a"], c["b"]) for c in res))
        self.assertEqual([(c["a"], c["b"], c["status"]) for c in res], [("C0", "R1", "overlap")])

    def test_two_parts_inside_is_not_enough(self):
        root = board(el("U8", 15, 15, "", "SOM") + el("C1", 12, 15) + el("C2", 18, 15))
        self.assertEqual(FP.enclosing(FP.courtyards(root)), {})

    def test_summary_lists_worst_overlaps_and_counts_the_rest(self):
        conflicts = [{"a": f"R{i}", "b": f"R{i + 1}", "status": "overlap", "overlap_mm": round(0.3 - i / 100, 3)}
                     for i in range(10)]
        conflicts += [{"a": "C1", "b": "C2", "status": "touching", "gap_mm": 0.0}] * 40
        text = FP.summary(conflicts, {"U8": 95}, top=3)
        self.assertEqual(text, "10 overlaps (R0/R1 0.3 mm, R1/R2 0.29 mm, R2/R3 0.28 mm, ...); 40 touching (OK); "
                               "skipped as enclosing outlines: U8 (holds 95 parts)")
        self.assertEqual(FP.summary([]), "0 overlaps; 0 touching (OK)")


class SilkAndPadsTest(unittest.TestCase):
    def test_names_rects_and_pads(self):
        smashed = '<attribute name="NAME" x="3" y="4" size="0.8" layer="25" rot="R90"/>'
        root = board(el("R1", 5, 5, "R90") + el("R2", 10, 5, "MR0", extra=smashed))
        silk = FP.silkscreen(root)
        names = {t["ref"]: t for t in silk if t["kind"] == "text"}
        self.assertEqual((names["R1"]["layer"], names["R1"]["angle"]), ("21", 90.0))
        self.assertAlmostEqual(names["R1"]["x"], 5 - 0.7)       # (0, 0.7) turned 90 degrees
        self.assertEqual((names["R2"]["x"], names["R2"]["y"], names["R2"]["layer"]), (3.0, 4.0, "21"))
        r180 = {t["ref"]: t for t in FP.silkscreen(board(el("R3", 5, 5, "R180"))) if t["kind"] == "text"}["R3"]
        self.assertEqual((r180["angle"], r180["align"]), (0.0, "top-right"))      # shown upright, as Fusion does
        self.assertEqual(FP.readable(180, "bottom-left", "SR180"), (180, "bottom-left"))   # spin keeps it
        rects = [t for t in silk if t["kind"] == "rect"]
        self.assertEqual({r["layer"] for r in rects}, {"21", "22"})   # mirrored part: bottom silk
        pads = {(r, p): (round(x, 3), round(y, 3), s) for r, p, x, y, s in FP.pad_names(root)}
        self.assertEqual(pads[("R1", "1")], (5.0, 4.5, "top"))
        self.assertEqual(pads[("R2", "1")], (10.5, 5.0, "bottom"))


@unittest.skipUnless(R.available(), "matplotlib not installed")
class RenderTest(unittest.TestCase):
    def test_render_reports_courtyard_conflicts(self):
        root = board(el("R18", 10, 10) + el("R19", 10, 11) + el("R20", 10, 11.8))
        out = os.path.join(tempfile.mkdtemp(), "b.png")
        info = R.render(root, out, courtyards=True, silkscreen=True, pad_numbers=True)
        self.assertTrue(os.path.getsize(out) > 0)
        self.assertEqual([c["status"] for c in info["courtyard_conflicts"]], ["overlap", "touching"])
        self.assertNotIn("region", info["courtyard_conflicts"][0])
        plain = R.render(root, out)
        self.assertEqual(plain["courtyard_conflicts"], [])


if __name__ == "__main__":
    unittest.main()
