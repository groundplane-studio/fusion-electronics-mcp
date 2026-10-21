import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from fusion_mcp import server as S
from fusion_mcp.session import Session
from fusion_offline import solder_mask as SM

from test_move_parts import FakeFusion

RULES = ('<param name="mvStopFrame" value="1"/><param name="mlMinStopFrame" value="0mil"/>'
         '<param name="mlMaxStopFrame" value="4mil"/><param name="mlViaStopLimit" value="999mm"/>')


def board():
    """0.3 mm square pads with the RV1126B SoM's 4 mil stop frame (0.1016 mm a side):
    U1 pins 0.15 mm apart (openings merge, like its BGA), R1 / R2 0.28 apart (a 0.077 mm dam),
    U2 pins on one net, U3 with an unconnected pin, U4 with no mask on one pin."""
    pkg = ('<package name="P2"><smd name="1" x="0" y="0" dx="0.3" dy="0.3" layer="1"/>'
           '<smd name="2" x="0.45" y="0" dx="0.3" dy="0.3" layer="1"/></package>'
           '<package name="P2NS"><smd name="1" x="0" y="0" dx="0.3" dy="0.3" layer="1"/>'
           '<smd name="2" x="0.45" y="0" dx="0.3" dy="0.3" layer="1" stop="no"/></package>'
           '<package name="P1"><smd name="1" x="0" y="0" dx="0.3" dy="0.3" layer="1"/></package>')
    els = [("U1", "P2", 0, 0), ("R1", "P1", 2, 0), ("R2", "P1", 2.58, 0), ("U2", "P2", 0, 3), ("U3", "P2", 0, 6),
           ("U4", "P2NS", 0, 9)]
    e = "".join(f'<element name="{n}" library="L" package="{p}" value="" x="{x}" y="{y}"/>' for n, p, x, y in els)
    nets = {"A": [("U1", "1"), ("R1", "1"), ("U3", "1"), ("U4", "1")], "B": [("U1", "2"), ("R2", "1"), ("U4", "2")],
            "C": [("U2", "1"), ("U2", "2")]}
    sig = "".join(f'<signal name="{n}">' + "".join(f'<contactref element="{a}" pad="{b}"/>' for a, b in c) + "</signal>"
                  for n, c in nets.items())
    return ET.fromstring(f"""<eagle><drawing><board><plain/><libraries><library name="L"><packages>{pkg}</packages></library>
</libraries><designrules name="t"><param name="layerSetup" value="(1*16)"/>{RULES}</designrules><classes/>
<elements>{e}</elements><signals>{sig}</signals></board></drawing></eagle>""")


class FrameTest(unittest.TestCase):
    def test_stop_frame_from_the_rules(self):
        rules = {"mvStopFrame": "1", "mlMinStopFrame": "0mil", "mlMaxStopFrame": "4mil"}
        self.assertAlmostEqual(SM.frame(rules, 0.3), 0.1016)
        self.assertAlmostEqual(SM.frame(rules, 0.05), 0.05)                     # 100% of a tiny pad
        self.assertAlmostEqual(SM.frame({}, 0.3), 0.1016)                       # EAGLE default 4 mil


class CheckTest(unittest.TestCase):
    def test_merged_and_thin(self):
        res = SM.check(board())
        self.assertFalse(res["ok"])
        by = {p["parts"]: p for p in res["problems"]}
        self.assertEqual(sorted(by), ["R1 / R2", "U1"])
        self.assertEqual((by["U1"]["merged"], by["U1"]["worst_dam_mm"]), (1, -0.0532))
        self.assertEqual(by["U1"]["examples"][0]["nets"], ["A", "B"])
        self.assertEqual((by["R1 / R2"]["merged"], by["R1 / R2"]["worst_dam_mm"]), (0, 0.0768))
        self.assertEqual(res["same_net_pairs"], {"C": 1})
        self.assertEqual(res["unconnected_pairs"], 1)                           # U3.2 has no net
        self.assertEqual(res["stop_frames_mm"], [0.1016])

    def test_a_smaller_frame(self):
        res = SM.check(board(), stop_frame_mm=0.05)
        by = {p["parts"]: p for p in res["problems"]}
        self.assertEqual(sorted(by), ["U1"])                                    # 0.28 - 0.1 leaves 0.18
        self.assertEqual((by["U1"]["merged"], by["U1"]["worst_dam_mm"]), (0, 0.05))
        self.assertTrue(SM.check(board(), min_dam_mm=0.0, stop_frame_mm=0.05)["ok"])

    def test_tool(self):
        with mock.patch.object(S, "session", Session(FakeFusion(board(), design="RV1126B SoM RevA"))):
            res = S.check_solder_mask()
            self.assertEqual((res["design"], res["merged_pairs"]), ("RV1126B SoM RevA", 1))
            self.assertEqual([p["parts"] for p in res["merged"]], ["U1"])
            self.assertEqual([p["parts"] for p in res["thin_dams"]], ["R1 / R2"])
            self.assertEqual([p["parts"] for p in S.check_solder_mask(parts=["R1"])["thin_dams"]], ["R1 / R2"])


if __name__ == "__main__":
    unittest.main()
