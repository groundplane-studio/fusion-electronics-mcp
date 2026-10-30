import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from fusion_mcp import server as S
from fusion_mcp.session import Session
from fusion_offline import annular as AR

from test_move_parts import FakeFusion

RULES = ('<param name="rvPadTop" value="0.1"/><param name="rvPadBottom" value="0.25"/><param name="rvPadInner" value="0.25"/>'
         '<param name="rlMinPadTop" value="0mil"/><param name="rlMaxPadTop" value="20mil"/>'
         '<param name="rvViaOuter" value="0.25"/><param name="rlMinViaOuter" value="0mil"/>'
         '<param name="useDiameter" value="29"/>')


def board(layers="(1*16)"):
    """RV1126B SoM via sizes (2026-10-21): 0.15/0.29 in a BGA ball pad is the one too small."""
    vias = [("GND", 27.5, 26.0, 0.15, 0.29), ("GND", 5, 5, 0.15, 0.30), ("SIG", 6, 5, 0.2, 0.35), ("SIG", 7, 5, 0.25, 0.43)]
    sig = {}
    for net, x, y, d, dia in vias:
        sig.setdefault(net, []).append(f'<via x="{x}" y="{y}" extent="1-16" drill="{d}" diameter="{dia}"/>')
    sigs = "".join(f'<signal name="{n}">{"".join(v)}</signal>' for n, v in sig.items())
    return ET.fromstring(f"""<eagle><drawing><board><plain/>
<libraries><library name="L"><packages>
<package name="BALL"><smd name="1" x="0" y="0" dx="0.3" dy="0.3" layer="1" roundness="100"/></package>
<package name="TH"><pad name="1" x="0" y="0" drill="1.0" diameter="1.2"/></package>
</packages></library></libraries>
<designrules name="t"><param name="layerSetup" value="{layers}"/>{RULES}</designrules><classes/>
<elements><element name="U1" library="L" package="BALL" value="" x="27.5" y="26.0"/>
<element name="J1" library="L" package="TH" value="" x="20" y="20"/></elements>
<signals>{sigs}<signal name="SHLD"><contactref element="J1" pad="1"/></signal></signals></board></drawing></eagle>""")


class RequiredTest(unittest.TestCase):
    def test_the_users_sizes_and_between(self):
        self.assertEqual([AR.required_diameter(d) for d in (0.20, 0.25, 0.30, 0.35)], [0.35, 0.43, 0.55, 0.70])
        self.assertEqual(AR.required_diameter(0.225), 0.39)                  # halfway between 0.35 and 0.43
        self.assertEqual(AR.required_diameter(0.15), 0.30)                   # drill + 0.15 beats 1.75 x 0.15
        self.assertEqual(AR.required_diameter(2.7), 3.05)                    # above 0.35: the 0.35 mm ring, not 2x
        self.assertEqual(AR.required_diameter(0.2, 0.15, {}), 0.35)          # no sizes: drill + min ring
        self.assertEqual(AR.required_diameter(0.2, 0.25, {}), 0.45)


class CopperTest(unittest.TestCase):
    def test_restring_and_library_diameter(self):
        rules = {"rvViaOuter": "0.25", "rlMinViaOuter": "8mil", "useDiameter": "29"}
        self.assertEqual(AR.copper(rules, "Via", 0.3, 0.0, inner=False), {"outer": 0.7064})     # 0.3 + 2 x 8 mil
        self.assertEqual(AR.copper(rules, "Via", 0.3, 0.8, inner=False), {"outer": 0.8})
        pad = AR.copper({"rvPadTop": "0.25", "rvPadInner": "0.25", "rvPadBottom": "0.25", "useDiameter": "29"},
                        "Pad", 1.0, 1.8)
        self.assertEqual(pad, {"top": 1.8, "bottom": 1.8, "inner": 1.5})   # inner pads: restring only

    def test_use_diameter_bits(self):
        rules = {"rvViaOuter": "0.25", "useDiameter": "0"}
        self.assertEqual(AR.copper(rules, "Via", 0.2, 0.5, inner=False), {"outer": 0.3})        # diameter ignored


class CheckTest(unittest.TestCase):
    def test_flags_the_small_via_and_the_thin_pad(self):
        res = AR.check(board())
        self.assertFalse(res["ok"])
        self.assertEqual(res["checked"], 5)
        bad = {(f["kind"], f["drill_mm"]): f for f in res["failures"]}
        self.assertEqual(sorted(bad), [("pad", 1.0), ("via", 0.15)])
        v = bad[("via", 0.15)]
        self.assertEqual((v["diameter_mm"], v["ring_mm"], v["radial_ring_mm"], v["required_diameter_mm"]), (0.29, 0.14, 0.07, 0.3))
        self.assertEqual(v["short_by_mm"], {"outer": 0.01})
        self.assertEqual(v["at"][0]["in_pad"], "U1.1")
        p = bad[("pad", 1.0)]
        self.assertEqual(p["copper_mm"], {"top": 1.2, "bottom": 1.5})      # 2 layers: no inner ring
        self.assertEqual(p["short_by_mm"], {"top": 0.15})
        self.assertEqual(p["at"][0]["pad"], "J1.1")
        ok = {(s["kind"], s["drill_mm"], s["diameter_mm"]) for s in res["sizes"] if s["ok"]}
        self.assertEqual(ok, {("via", 0.15, 0.3), ("via", 0.2, 0.35), ("via", 0.25, 0.43)})

    def test_inner_layers_use_the_pad_restring(self):
        res = AR.check(board("(1*2*15*16)"))
        p = next(f for f in res["failures"] if f["kind"] == "pad")
        self.assertEqual(p["copper_mm"]["inner"], 1.5)

    def test_tool(self):
        with mock.patch.object(S, "session", Session(FakeFusion(board(), design="RV1126B SoM RevA"))):
            res = S.check_annular_ring()
            self.assertEqual((res["design"], res["failing"]), ("RV1126B SoM RevA", 2))
            loose = S.check_annular_ring(min_ring_mm=0.1, sizes=[[0.15, 0.25]])
            self.assertEqual([f["kind"] for f in loose["failures"]], [])
            with self.assertRaises(Exception):
                S.check_annular_ring(sizes=[[0.3, 0.2]])


if __name__ == "__main__":
    unittest.main()
