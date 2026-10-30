import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from fusion_mcp import server as S
from fusion_mcp.session import Session
from fusion_offline import pour_clearance as PCL
from fusion_offline import stackup as ST

from test_move_parts import FakeFusion


def ring(x0, y0, x1, y1, tag):
    v = "".join(f'<vertex x="{x}" y="{y}"/>' for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))
    return f"<{tag}>{v}</{tag}>"


def classic(polygon=""):
    """USB_P (class usb_diff) on Top from (0, 0) to (10, 0), 0.2 mm wide; GND on Top too."""
    return ET.fromstring(f"""<eagle><drawing><layers><layer number="1" name="Top"/><layer number="16" name="Bottom"/></layers>
<board><plain/><libraries/><designrules name="t"><param name="layerSetup" value="(1*16)"/>
<param name="mdWireWire" value="0.1mm"/></designrules>
<classes><class number="0" name="default"/><class number="2" name="usb_diff"/></classes><elements/><signals>
<signal name="USB_P" class="2"><wire x1="0" y1="0" x2="10" y2="0" width="0.2" layer="1"/></signal>
<signal name="USB_N" class="2"><wire x1="0" y1="-0.3" x2="10" y2="-0.3" width="0.2" layer="1"/></signal>
<signal name="GND">{polygon}</signal></signals></board></drawing></eagle>""")


def v2(shapes, layer=1):
    """A V2 working copy holding GND's poured copper on `layer` (304 = Bottom, V2 numbering)."""
    sh = "".join(f'<polygonshape layer="{layer}">{s}</polygonshape>' for s in shapes)
    return ET.fromstring(f'<eagle><drawing><board><signals><signal name="GND"><polygonpour layer="{layer}" width="0.1">'
                         f'{ring(-5, -5, 15, 5, "polygonoutlineobjects")}<polygonfilldetails>{sh}</polygonfilldetails>'
                         '</polygonpour></signal></signals></board></drawing></eagle>')


ABOVE = ring(-1, 0.4, 11, 3, "polygonoutlinesegments")                    # fill edge 0.4 above USB_P's centre


class FillTest(unittest.TestCase):
    def test_pour_above_the_trace(self):
        fill = PCL.fills(v2([ABOVE]))
        res = PCL.check(classic(), ["USB_P"], fill)
        (v,) = res["violations"]
        self.assertEqual((v["net"], v["pour_net"], v["layer"], v["gap_mm"], v["required_mm"]), ("USB_P", "GND", 1, 0.3, 0.6))
        self.assertEqual(v["pour_edge_at"][1], 0.4)
        self.assertAlmostEqual(v["length_mm"], 10.0, delta=0.2)
        self.assertTrue(PCL.check(classic(), ["USB_P"], fill, min_gap_mm=0.25)["ok"])

    def test_two_dielectric_heights(self):
        fill = PCL.fills(v2([ABOVE]))
        v = PCL.check(classic(), ["USB_P"], fill, heights={1: {"h_mm": 0.4}})["violations"][0]
        self.assertEqual(v["required_mm"], 0.8)                                  # 2H beats 3W here

    def test_trace_in_a_hole(self):
        holes = (ring(-2, -2, 12, 2, "polygonoutlinesegments")
                 + f'<polygonholelist>{ring(-1, -0.35, 11, 0.35, "polygonholesegments")}</polygonholelist>')
        v = PCL.check(classic(), ["USB_P"], PCL.fills(v2([holes])))["violations"][0]
        self.assertEqual(v["gap_mm"], 0.25)

    def test_breakout_is_skipped(self):
        fill = PCL.fills(v2([ABOVE]))
        res = PCL.check(classic(), ["USB_P"], fill, pad_reach={"USB_P": [(0, 0, 0.2), (10, 0, 0.2)]})
        self.assertAlmostEqual(res["skipped_breakout_mm"]["USB_P"], 1.6, delta=0.15)   # 0.8 mm at each end
        self.assertAlmostEqual(res["violations"][0]["length_mm"], 8.4, delta=0.2)

    def test_remap_and_heights_by_stack_position(self):
        fill = PCL.remap(PCL.fills(v2([ABOVE], layer=304)), [1, 304], [1, 16])
        self.assertEqual(fill[0]["layer"], 16)
        st = ST.Stackup("t", [ST.Layer("Signal", "Top", 1, 0.035), ST.Layer("Prepreg", "P1", None, 0.092),
                              ST.Layer("Core", "C1", None, 1.2), ST.Layer("Signal", "Bottom", 304, 0.035)])
        h = PCL.reference_heights(st, [1, 16])
        self.assertEqual(h[1], {"h_mm": 0.092, "dielectric": "P1"})
        self.assertEqual(h[16], {"h_mm": 1.2, "dielectric": "C1"})


class OutlineTest(unittest.TestCase):
    def test_estimate_from_a_solid_outline(self):
        poly = '<polygon width="0.2" layer="1" isolate="0.2">' + "".join(
            f'<vertex x="{x}" y="{y}"/>' for x, y in ((-1, -2), (11, -2), (11, 2), (-1, 2))) + "</polygon>"
        v = PCL.check(classic(poly), ["USB_P"], None, clearance_mm=0.1)["violations"][0]
        self.assertEqual((v["gap_mm"], v["estimated"]), (0.2, True))             # the pour's isolate


class Bridge(FakeFusion):
    def __init__(self, root, fill_root):
        super().__init__(root, design="RV1126B SoM RevA")
        self.v2 = os.path.join(tempfile.mkdtemp(), "v2.brd")
        with open(self.v2, "wb") as f:
            f.write(ET.tostring(fill_root))

    def call(self, op, args=None, **kw):
        if op == "design_rules":
            xml = ('<designrules><layerstackup><layerdef type="Signal" name="Top" layer="1"><material thickness="0.035mm"/>'
                   '</layerdef><layerdef type="Prepreg" name="P1"><material thickness="0.1mm"/></layerdef>'
                   '<layerdef type="Signal" name="Bottom" layer="304"><material thickness="0.035mm"/></layerdef>'
                   '</layerstackup></designrules>')
            return {"xml": xml, "path": self.v2, "modified": "2026-10-21T12:00:00"}
        return super().call(op, args, **kw)


class ToolTest(unittest.TestCase):
    def test_finds_the_usb_class_and_uses_the_fill(self):
        with mock.patch.object(S, "session", Session(Bridge(classic(), v2([ABOVE])))):
            res = S.check_pour_clearance()
        self.assertEqual(res["nets"], ["USB_N", "USB_P"])
        self.assertEqual(res["design"], "RV1126B SoM RevA")
        self.assertIn("working copy (2026-10-21T12:00:00)", res["fill"])
        self.assertEqual(res["heights"]["Top"]["h_mm"], 0.1)
        bad = {v["net"]: v for v in res["violations"]}
        self.assertEqual(bad["USB_P"]["gap_mm"], 0.3)
        self.assertEqual(bad["USB_P"]["layer_name"], "Top")
        self.assertNotIn("USB_N", bad)                                           # 0.6 mm away: exactly 3W

    def test_nothing_to_check(self):
        with mock.patch.object(S, "session", Session(Bridge(classic(), v2([ABOVE])))):
            res = S.check_pour_clearance(classes="lvds", pairs="lvds")
            self.assertIsNone(res["ok"])
            with self.assertRaises(Exception):
                S.check_pour_clearance(nets=["NOPE"])


if __name__ == "__main__":
    unittest.main()
