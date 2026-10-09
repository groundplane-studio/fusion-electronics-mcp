import unittest
import xml.etree.ElementTree as ET

from fusion_offline import design as D
from fusion_offline import review as RV


def sch(nets: str, parts: str) -> D.SchematicDesign:
    """A GND supply symbol (package-less, pin GND direction sup), a resistor, and the given nets."""
    return D.parse_schematic_design(ET.fromstring(f"""<eagle><drawing><schematic><libraries><library name="L">
<symbols>
<symbol name="GND"><pin name="GND" x="0" y="0" direction="sup"/></symbol>
<symbol name="RES"><pin name="1" x="0" y="0" direction="pas"/><pin name="2" x="5" y="0" direction="pas"/></symbol>
</symbols><devicesets>
<deviceset name="GND"><gates><gate name="1" symbol="GND" x="0" y="0"/></gates><devices><device name=""/></devices></deviceset>
<deviceset name="R"><gates><gate name="G$1" symbol="RES" x="0" y="0"/></gates><devices>
<device name="" package="R0402"><connects><connect gate="G$1" pin="1" pad="1"/><connect gate="G$1" pin="2" pad="2"/></connects>
<technologies><technology name=""/></technologies></device></devices></deviceset>
</devicesets></library></libraries><classes><class number="0" name="default"/></classes>
<parts>{parts}</parts><sheets><sheet><instances>
<instance part="SUPPLY1" gate="1" x="0" y="0"/><instance part="R1" gate="G$1" x="10" y="0"/>
</instances><nets>{nets}</nets></sheet></sheets></schematic></drawing></eagle>"""))


PARTS = ('<part name="SUPPLY1" library="L" deviceset="GND" device="" value="GND"/>'
         '<part name="R1" library="L" deviceset="R" device="" value="10k"/>')


def net(name, *pins):
    refs = "".join(f'<pinref part="{p}" gate="{g}" pin="{n}"/>' for p, g, n in pins)
    return f'<net name="{name}" class="0"><segment>{refs}<wire x1="0" y1="0" x2="1" y2="0" layer="91"/></segment></net>'


class SupplyTest(unittest.TestCase):
    def rules(self, s):
        return {f["rule"]: f for f in RV.review(s)}

    def test_supply_on_its_own_net_is_fine(self):
        s = sch(net("GND", ("SUPPLY1", "1", "GND"), ("R1", "G$1", "1")) + net("A", ("R1", "G$1", "2")), PARTS)
        self.assertNotIn("supply_on_other_net", self.rules(s))

    def test_supply_overwritten_by_another_net_name(self):
        # the PoE board, 2026-10-04: a GND symbol on N$25 (Fusion ERC 102); review used to miss it
        s = sch(net("N$25", ("SUPPLY1", "1", "GND"), ("R1", "G$1", "1")) + net("A", ("R1", "G$1", "2")), PARTS)
        f = self.rules(s)["supply_on_other_net"]
        self.assertEqual(f["severity"], "error")
        self.assertIn("supply symbol SUPPLY1 (GND) sits on net N$25", f["message"])

    def test_value_named_power_symbol(self):
        # power symbols whose value is set to the rail name (FUSION_MCP_POWER_SYMBOL) are fine on that rail
        parts = PARTS.replace('value="GND"', 'value="+5V"')
        s = sch(net("+5V", ("SUPPLY1", "1", "GND"), ("R1", "G$1", "1")) + net("A", ("R1", "G$1", "2")), parts)
        self.assertNotIn("supply_on_other_net", self.rules(s))

    def test_supply_alone(self):
        s = sch(net("GND", ("SUPPLY1", "1", "GND")) + net("A", ("R1", "G$1", "1"), ("R1", "G$1", "2")), PARTS)
        self.assertIn("connects only supply symbol(s) SUPPLY1", self.rules(s)["supply_alone"]["message"])


class OverbarTest(unittest.TestCase):
    def test_readable_and_plain(self):
        self.assertEqual(RV.overbar("PI_LED_!PWR"), {"plain": "PI_LED_PWR", "readable": "PI_LED_~{PWR}", "suspect": None})
        self.assertEqual(RV.overbar("!RESET")["readable"], "~{RESET}")
        self.assertEqual(RV.overbar("GND"), {"plain": "GND", "readable": "GND", "suspect": None})

    def test_suspect_markup(self):
        ob = RV.overbar("PI_!LED!_ACTIVITY")           # bars LED; meant PI_LED_!ACTIVITY
        self.assertEqual(ob["readable"], "PI_~{LED}_ACTIVITY")
        self.assertIn("stops before '_ACTIVITY'", ob["suspect"])
        self.assertIn("empty overbar", RV.overbar("A!!B")["suspect"])
        self.assertIsNone(RV.overbar("!CS!")["suspect"])        # bar over the whole name, closed at the end

    def test_review_flags_it(self):
        s = sch(net("PI_!LED!_ACTIVITY", ("R1", "G$1", "1"), ("R1", "G$1", "2")), PARTS)
        f = {x["rule"]: x for x in RV.review(s)}["overbar_markup"]
        self.assertIn("reads PI_~{LED}_ACTIVITY", f["message"])


if __name__ == "__main__":
    unittest.main()
