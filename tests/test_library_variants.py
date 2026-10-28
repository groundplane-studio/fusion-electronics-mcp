"""Slotted holes from KiCad footprints, the library text style, package variants added to an
existing device set, and the Create 3D Package crash guard."""

import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from fusion_mcp import library as LIB
from fusion_mcp.library import Part, build_script, deviceset_info, variant_script
from fusion_offline.kicad_import import footprint_to_part

from test_server import load_addin

# a vertical USB-C plug cut down to what matters: two SMD contacts, a plated slot along x, a
# rotated plated slot, and an unplated locating slot
FP = """(footprint "PLUG_TEST" (layer "F.Cu")
  (pad "A1" smd rect (at 2.75 -0.98) (size 0.28 1.06) (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "B1" smd rect (at -2.75 0.98) (size 0.28 1.06) (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "S1" thru_hole oval (at -1.75 -2.8) (size 1.6 0.8) (drill oval 1.2 0.4) (layers "*.Cu" "*.Mask"))
  (pad "S2" thru_hole oval (at 4 0 90) (size 1.2 0.8) (drill oval 0.9 0.5) (layers "*.Cu" "*.Mask"))
  (pad "" np_thru_hole oval (at 3.75 0) (size 1.0 0.58) (drill oval 1.0 0.58) (layers "*.Cu" "*.Mask"))
  (pad "" np_thru_hole circle (at -3.75 0) (size 0.7 0.7) (drill 0.7) (layers "*.Cu" "*.Mask"))
  (fp_line (start -4.75 -2.6) (end 4.75 -2.6) (stroke (width 0.25) (type solid)) (layer "F.SilkS"))
)"""


def plug_part(**kw):
    part, _ = footprint_to_part(FP, part_id="plug-test", deviceset="PLUG_TEST", prefix="J",
                                pin_names={"A1": "GND", "B1": "VBUS", "S1": "SHIELD", "S2": "SHIELD"},
                                attributes={"VALUE": "PLUG", "JLCPCB": "C1", "MF": "M", "MP": "P"}, **kw)
    return Part(part)


class KicadSlotTest(unittest.TestCase):
    def test_plated_and_unplated_slots(self):
        pkg = plug_part().data["package"]
        pads = {p["name"]: p for p in pkg["pads"]}
        s1 = pads["S1"]
        self.assertEqual((s1["shape"], s1["drill"], s1["slot_length"], s1["diameter"], s1["rot"]),
                         ("slot", 0.4, 1.2, 0.8, 0.0))
        self.assertEqual((s1["x"], s1["y"]), (-1.75, 2.8))                     # EAGLE y is up
        s2 = pads["S2"]                                                        # KiCad 90 -> EAGLE 270 -> axis 90
        self.assertEqual((s2["drill"], s2["slot_length"], s2["rot"]), (0.5, 0.9, 90.0))
        self.assertEqual(pkg["slots"], [{"x": 3.75, "y": 0.0, "width": 0.58, "length": 1.0, "rot": 0.0}])
        self.assertEqual(pkg["holes"], [{"x": -3.75, "y": 0.0, "drill": 0.7}])   # round NPTH stays a hole

    def test_round_oval_drill_is_not_a_slot(self):
        part, _ = footprint_to_part('(footprint "X" (pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) '
                                    '(drill oval 0.9) (layers "*.Cu")))', part_id="x", deviceset="X", prefix="J")
        self.assertEqual(part["package"]["pads"][0]["shape"], "round")
        self.assertEqual(part["package"]["slots"], [])


class BuildScriptTest(unittest.TestCase):
    def test_slots_text_style_and_allowlist(self):
        s = build_script(plug_part())
        load_addin().validate(s)
        self.assertIn("PAD 0.8 SLOT R0 'S1' (-1.75 2.8);", s)
        self.assertIn("CHANGE SLOTLENGTH 1.2 (-1.75 2.8);", s)
        self.assertIn("CHANGE SLOTLENGTH 0.9 (4 0);", s)
        self.assertIn("LAYER 46;", s)
        self.assertEqual(s.count("-180 ("), 2)                                  # two rounded ends
        for line in ("CHANGE FONT VECTOR;", "CHANGE RATIO 8;", "CHANGE SIZE 0.8128;", "CHANGE ALIGN BOTTOM LEFT;"):
            self.assertIn(line, s)
        name = next(l for l in s.splitlines() if l.startswith("TEXT '>NAME'"))
        value = next(l for l in s.splitlines() if l.startswith("TEXT '>VALUE'"))
        # above-left of the footprint (leftmost silk x -4.75, top: the S1 slot pad at 2.8 + 1.0)
        self.assertEqual(value, "TEXT '>VALUE' R0 (-4.75 4.05);")
        self.assertEqual(name, "TEXT '>NAME' R0 (-4.75 5.1128);")

    def test_stadium_is_closed(self):
        segs = LIB._stadium({"x": 0, "y": 0, "width": 0.58, "length": 1.0, "rot": 0})
        self.assertEqual(segs, [("(-0.21 0.29)", "(0.21 0.29)"), ("(0.21 0.29)", "-180 (0.21 -0.29)"),
                                ("(0.21 -0.29)", "(-0.21 -0.29)"), ("(-0.21 -0.29)", "-180 (-0.21 0.29)")])


LIB_XML = """<eagle><drawing><library>
<symbols><symbol name="USBC"><pin name="GND"/><pin name="VBUS"/><pin name="SHIELD"/></symbol></symbols>
<devicesets><deviceset name="CONN_USBC" prefix="J"><gates><gate name="G$1" symbol="USBC"/></gates><devices>
<device name="_OLD" package="OLD"><connects><connect gate="G$1" pin="GND" pad="1"/></connects>
<technologies><technology name=""><attribute name="VALUE" value="OLD"/></technology>
<technology name="_DNP"><attribute name="VALUE" value="NC/OLD"/></technology></technologies></device>
</devices></deviceset></devicesets></library></drawing></eagle>"""


class VariantTest(unittest.TestCase):
    def test_deviceset_info(self):
        info = deviceset_info(ET.fromstring(LIB_XML), "conn_usbc")
        self.assertEqual(info["gates"], [{"name": "G$1", "symbol": "USBC", "pins": ["GND", "VBUS", "SHIELD"]}])
        self.assertEqual(info["variants"][0]["technologies"], {"": {"VALUE": "OLD"}, "_DNP": {"VALUE": "NC/OLD"}})
        self.assertIsNone(deviceset_info(ET.fromstring(LIB_XML), "NOPE"))

    def test_plan_matches_pins_by_name_and_adds_dnp(self):
        from fusion_mcp.server import plan_variant
        info = deviceset_info(ET.fromstring(LIB_XML), "CONN_USBC")
        connects, techs = plan_variant(plug_part(), info, "_NEW", None, None, True)
        self.assertEqual(connects, {"G$1.GND": "A1", "G$1.VBUS": "B1", "G$1.SHIELD": "S1 S2"})
        self.assertEqual(techs["_DNP"]["VALUE"], "NC/PLUG")
        self.assertEqual(techs[""], {"VALUE": "PLUG", "JLCPCB": "C1", "MF": "M", "MP": "P"})

    def test_plan_refuses_bad_maps(self):
        from fusion_mcp.server import plan_variant
        info = deviceset_info(ET.fromstring(LIB_XML), "CONN_USBC")
        with self.assertRaisesRegex(ValueError, "unconnected symbol pins"):
            plan_variant(plug_part(), info, "_NEW", {"GND": "A1"}, None, False)
        with self.assertRaisesRegex(ValueError, "not in package"):
            plan_variant(plug_part(), info, "_NEW", {"GND": "A1", "VBUS": "B1", "SHIELD": "S9"}, None, False)
        with self.assertRaisesRegex(ValueError, "already has a variant"):
            plan_variant(plug_part(), info, "_old", None, None, False)

    def test_variant_script(self):
        s = variant_script(plug_part(), "CONN_USBC", "_NEW", {"G$1.GND": "A1", "G$1.SHIELD": "S1 S2"},
                           {"": {"VALUE": "PLUG"}, "_DNP": {"VALUE": "NC/PLUG"}})
        load_addin().validate(s)
        self.assertIn("EDIT 'PLUG_TEST.pac';", s)
        self.assertNotIn(".sym'", s)                                           # no symbol drawn
        self.assertIn("PACKAGE 'PLUG_TEST' '_NEW';", s)
        self.assertIn("CONNECT 'G$1.SHIELD' 'S1 S2';", s)
        self.assertIn("TECHNOLOGY '';\nATTRIBUTE VALUE 'PLUG';\nATTRIBUTE PACKAGE DELETE;", s)
        self.assertIn("TECHNOLOGY '_DNP';\nATTRIBUTE VALUE 'NC/PLUG';\nATTRIBUTE PACKAGE DELETE;", s)
        reuse = variant_script(plug_part(), "CONN_USBC", "_NEW", {}, {"": {}}, reuse_package=True)
        self.assertNotIn(".pac'", reuse)


class CrashGuardTest(unittest.TestCase):
    def test_known_crashing_build_is_refused(self):
        from fusion_mcp import server as S
        with mock.patch.object(S.session.bridge, "call", return_value={"fusion_version": "2706.1.14"}):
            with self.assertRaisesRegex(ValueError, "accept_crash_risk"):
                S._check_3d_build(False)
            S._check_3d_build(True)
        with mock.patch.object(S.session.bridge, "call", return_value={"fusion_version": "2705.1.15"}):
            S._check_3d_build(False)


if __name__ == "__main__":
    unittest.main()
