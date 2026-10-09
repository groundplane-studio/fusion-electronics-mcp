import re
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from fusion_mcp import server as S
from fusion_mcp.session import Session
from fusion_offline import net_classes as NC
from fusion_offline import stackup as ST

from test_move_parts import FakeFusion

# the shape of the PoE Magnetics Test Board's V2 rules (2026-10-07): class 2 and 7 rules first,
# built-ins after them
RULES = """<designrules name="JLC04161H-3313 4-layer.edru" version="V2_ExtendedLayers">
<rules>
<rulecategory type="Copper Clearance" manualdrc="yes" livedrc="yes"/>
<rule type="Minimum Copper Width" enabled="yes" priority="0" onescope="classes=2" value="0.15mm" preferredvalue="0.15mm" check_polywidth="no" name="Copper Width 1"/>
<rule type="Minimum Copper Width" enabled="yes" priority="1" onescope="classes=7" value="0.12mm" preferredvalue="0.12mm" check_polywidth="no" name="Copper Width 2"/>
<rule type="Minimum Copper Width" builtin_ruleid="14" enabled="yes" priority="2" value="0.1mm" preferredvalue="0.1mm" check_polywidth="no" name="minimum-copper-width-1"/>
<rule type="Minimum Drill Size" enabled="yes" priority="0" onescope="classes=2" value="0.2mm" preferredvalue="0.2mm" name="Drill Size 1"/>
<rule type="Minimum Drill Size" enabled="yes" priority="1" onescope="classes=7" value="0.2mm" preferredvalue="0.2mm" name="Drill Size 2"/>
<rule type="Minimum Drill Size" builtin_ruleid="15" enabled="yes" priority="2" value="0.2mm" preferredvalue="0.2mm" name="minimum-drill-size-1"/>
<rule type="Copper Clearance" enabled="yes" priority="0" onescope="classes=2" otherscope="classes=2" value="0.15mm" preferredvalue="0.15mm" samesignal="no" name="Copper Clearance 1"/>
<rule type="Copper Clearance" enabled="yes" priority="1" onescope="classes=7" otherscope="classes=7" value="0.15mm" preferredvalue="0.15mm" samesignal="no" name="Copper Clearance 2"/>
<rule type="Copper Clearance" builtin_ruleid="8" enabled="yes" priority="2" onescope="is_smd" otherscope="is_smd" value="0.14mm" samesignal="yes" name="copper-clearance-9"/>
<rule type="Copper Clearance" builtin_ruleid="0" enabled="yes" priority="3" onescope="is_wire_polygon" otherscope="is_wire_polygon" value="0.12mm" samesignal="no" name="copper-clearance-1"/>
</rules>
</designrules>"""

CLASSES = """<classes>
<class number="0" name="default" width="0" drill="0"></class>
<class number="2" name="usb_diff_pair" width="0.15" drill="0.2"><clearance class="2" value="0.15"/></class>
<class number="7" name="eth_100" width="0.12" drill="0.2"><clearance class="7" value="0.15"/></class>
</classes>"""


def board_root(signals='<signal name="TP0_P" class="7"/><signal name="TP0_N" class="7"/><signal name="GND"/>'):
    return ET.fromstring(f"<eagle><drawing><board>{CLASSES}<signals>{signals}</signals></board></drawing></eagle>")


def rule_list(text, rtype):
    root = ET.fromstring(text[text.index("<designrules"):text.index("</designrules>") + len("</designrules>")])
    return sorted(((int(r.get("priority")), r.get("onescope"), r.get("value"), r.get("name"))
                   for r in root.iter("rule") if r.get("type") == rtype))


class SummaryTest(unittest.TestCase):
    def test_classes_rules_and_counts(self):
        out = NC.summarize(board_root(), RULES)
        eth = next(c for c in out["classes"] if c["name"] == "eth_100")
        self.assertEqual((eth["number"], eth["width_mm"], eth["drill_mm"], eth["clearance_mm"], eth["nets"]),
                         (7, 0.12, 0.2, 0.15, 2))
        self.assertEqual(sorted(r["kind"] for r in eth["rules"]), ["clearance", "drill", "width"])
        self.assertEqual(out["warnings"], [])

    def test_class_made_in_the_dialog_takes_its_values_from_the_rules(self):
        # PoE board v23 (2026-10-07): poe_ct #1 set up in the Net Classes dialog: legacy width 0, no
        # legacy clearance; 0.3 mm width / 0.2 mm clearance only as rules scoped to it
        brd = ET.fromstring(f"<eagle><drawing><board>{CLASSES.replace('</classes>', '<class number=\"1\" name=\"poe_ct\" width=\"0\" drill=\"0\"></class></classes>')}"
                            '<signals><signal name="N$5" class="1"/></signals></board></drawing></eagle>')
        rules = RULES.replace("</rules>", '<rule type="Minimum Copper Width" enabled="yes" priority="0" onescope="classes=1" '
                                          'value="0.3mm" name="Copper Width 9"/><rule type="Copper Clearance" enabled="yes" '
                                          'priority="0" onescope="classes=1" otherscope="classes=1" value="0.2mm" '
                                          'name="Copper Clearance 9"/></rules>')
        out = NC.summarize(brd, rules)
        poe = next(c for c in out["classes"] if c["name"] == "poe_ct")
        self.assertEqual((poe["width_mm"], poe["clearance_mm"], poe["drill_mm"]), (0.3, 0.2, None))
        self.assertEqual(poe["source"], {"width": "design rule", "drill": None, "clearance": "design rule"})
        self.assertEqual(poe["legacy"], {"width": 0.0, "drill": 0.0, "clearance": None})
        self.assertEqual(out["warnings"], [])
        self.assertIn("class poe_ct: width and clearance set in its design rules only", out["notes"][0])
        self.assertEqual(NC.effective(brd, rules)["poe_ct"], {"number": 1, "width_mm": 0.3, "drill_mm": None,
                                                              "clearance_mm": 0.2})
        # without the rules only the legacy values are known
        self.assertEqual(NC.effective(brd, None)["poe_ct"]["width_mm"], None)
        self.assertEqual(NC.effective(brd, None)["eth_100"]["width_mm"], 0.12)

    def test_stray_unscoped_rule_is_flagged(self):
        # what CLASS 8 poe_ct left on 2026-10-05: a width rule on all copper
        stray = RULES.replace("</rules>", '<rule type="Minimum Copper Width" enabled="yes" priority="0" value="0.3mm" '
                                          'name="Copper Width 3"/></rules>')
        out = NC.summarize(board_root(), stray)
        self.assertTrue(any("'Copper Width 3'" in w and "applies to ALL copper" in w for w in out["warnings"]))

    def test_mismatch_between_class_and_rule(self):
        out = NC.summarize(board_root(), RULES.replace('value="0.12mm" preferredvalue="0.12mm"',
                                                       'value="0.2mm" preferredvalue="0.2mm"'))
        self.assertIn("class eth_100: width 0.12 mm in the class but 0.2 mm in its DRC rule", out["warnings"])

    def test_without_rules(self):
        out = NC.summarize(board_root(), None)
        self.assertEqual(len(out["classes"]), 3)


class BuildEdruTest(unittest.TestCase):
    classes = [{"number": 0, "name": "default"}, {"number": 2, "name": "usb_diff_pair"}, {"number": 7, "name": "eth_100"}]

    def test_new_class_goes_after_the_others_before_builtins(self):
        text, cls = NC.build_edru(RULES, self.classes, "poe_ct", 0.3, 0.2, 0.3)
        self.assertEqual(cls["number"], 1)                          # first free number
        self.assertEqual(rule_list(text, "Minimum Copper Width"),
                         [(0, "classes=2", "0.15mm", "Copper Width 1"), (1, "classes=7", "0.12mm", "Copper Width 2"),
                          (2, "classes=1", "0.3mm", "Copper Width 3"), (3, None, "0.1mm", "minimum-copper-width-1")])
        self.assertEqual([p for p, *_ in rule_list(text, "Copper Clearance")], [0, 1, 2, 3, 4])
        # every class kept, the new one included; nothing applies to all copper
        names = re.findall(r'<class number="(\d+)" name="([^"]+)"', text)
        self.assertEqual(names, [("0", "default"), ("1", "poe_ct"), ("2", "usb_diff_pair"), ("7", "eth_100")])
        out = NC.summarize(board_root(), text[text.index("<designrules"):text.index("</designrules>") + 14])
        self.assertFalse(any("ALL copper" in w for w in out["warnings"]))
        # the clearance rule reads back for the class (as check_impedance reads it)
        rules, _ = ST.parse_clearance_rules(text.encode())
        self.assertEqual([r.value_mm for r in ST.class_clearance(rules, "1")], [0.2])

    def test_updating_a_class_replaces_its_rules(self):
        text, cls = NC.build_edru(RULES, self.classes, "eth_100", 0.12, 0.13)
        self.assertEqual(cls["number"], 7)
        cc = [r for r in rule_list(text, "Copper Clearance") if r[1] == "classes=7"]
        self.assertEqual([r[2] for r in cc], ["0.13mm"])
        self.assertFalse(any(r[1] == "classes=7" for r in rule_list(text, "Minimum Drill Size")))   # no drill given

    def test_refusals(self):
        with self.assertRaises(ValueError):
            NC.build_edru(RULES, self.classes, "poe_ct", 0.3, 0.2, number=7)      # 7 is eth_100
        with self.assertRaises(ValueError):
            NC.build_edru(RULES, self.classes, "eth_100", 0.12, 0.15, number=3)   # eth_100 is 7
        with self.assertRaises(ValueError):
            NC.build_edru(RULES, self.classes, "bad'name", 0.1, 0.1)
        with self.assertRaises(ValueError):
            NC.build_edru(RULES, self.classes, "x", 0, 0.1)


def sch_root(classes='<class number="0" name="default"/><class number="7" name="eth_100"/><class number="6" name="50 ohm"/>'):
    """Two sheets. On sheet 1 net B's wire crosses the middle of A's first (longest) wire."""
    return ET.fromstring(f"""<eagle><drawing><schematic><classes>{classes}</classes><sheets>
<sheet><nets>
<net name="A" class="0"><segment><wire x1="0" y1="0" x2="20" y2="0" layer="91"/><wire x1="20" y1="0" x2="20" y2="4" layer="91"/></segment></net>
<net name="B" class="0"><segment><wire x1="10" y1="-5" x2="10" y2="5" layer="91"/></segment></net>
<net name="C" class="0"><segment><wire x1="0" y1="20" x2="0.02" y2="20" layer="91"/></segment></net>
</nets></sheet>
<sheet><nets>
<net name="D" class="0"><segment><wire x1="0" y1="0" x2="10" y2="0" layer="91"/></segment></net>
</nets></sheet>
</sheets></schematic></drawing></eagle>""")


class PickTest(unittest.TestCase):
    def test_avoids_other_nets_and_reports_what_it_cannot_pick(self):
        pts, fails = NC.pick_points(sch_root(), ["A", "B", "D", "C", "Z"])
        self.assertEqual(pts["A"], (1, 20, 2))                     # A's long wire is crossed by B at its middle
        self.assertEqual(pts["D"], (2, 5, 0))
        # B's only wire has A running through its middle; C's is too short to pick; Z does not exist
        self.assertEqual(set(fails), {"B", "C", "Z"})
        self.assertIn("another net within 0.05 mm", fails["B"])

    def test_commands_per_sheet_and_quoting(self):
        cmds = NC.change_class_commands("eth_100", {"A": (1, 20, 2), "D": (2, 5, 0), "B": (1, 10, 3)})
        self.assertEqual(cmds, "EDIT .s1; CHANGE CLASS eth_100 (20 2) (10 3); EDIT .s2; CHANGE CLASS eth_100 (5 0);")
        self.assertIn("CHANGE CLASS '50 ohm' (5 0)", NC.change_class_commands("50 ohm", {"D": (2, 5, 0)}))


class FakeSchematic(FakeFusion):
    """Exports the schematic for kind=schematic and the board otherwise; CHANGE CLASS picks the
    net with a wire through the point on the current sheet and sets its class everywhere."""
    def __init__(self, sch: ET.Element, brd: ET.Element, wrong: str | None = None):
        super().__init__(brd)
        self.sch = ET.tostring(sch)
        self.wrong = wrong              # also change this net (to test the "other nets" check)

    def call(self, op, args=None, timeout=60, answers=None, forms=None):
        if op == "export" and args["kind"] == "schematic":
            import os
            path = os.path.join(self.dir, f"s{len(self.ran)}.sch")
            with open(path, "wb") as f:
                f.write(self.sch)
            return {"path": path}
        if op == "run" and args["commands"].strip() != "UNDO;":
            self.ran.append(args["commands"])
            self.undo.append((self.xml, self.sch))
            sch, brd = ET.fromstring(self.sch), ET.fromstring(self.xml)
            sheets = sch.findall("./drawing/schematic/sheets/sheet")
            classes = {c.get("name"): c.get("number") for c in sch.iter("class")}
            sheet = 1
            for cmd in [c.strip() for c in args["commands"].split(";")]:
                if cmd.startswith("EDIT .s"):
                    sheet = int(cmd[7:])
                elif cmd.startswith("CHANGE CLASS"):
                    m = re.match(r"CHANGE CLASS ('[^']+'|\S+) (.*)", cmd)
                    num = classes[m.group(1).strip("'")]
                    for x, y in re.findall(r"\(([-\d.]+) ([-\d.]+)\)", m.group(2)):
                        hit = next(n.get("name") for n in sheets[sheet - 1].iterfind("./nets/net") for w in n.iter("wire")
                                   if NC._seg_dist(float(x), float(y), *(float(w.get(k)) for k in ("x1", "y1", "x2", "y2"))) < 1e-3)
                        for name in [hit] + ([self.wrong] if self.wrong else []):
                            for n in sch.iter("net"):
                                if n.get("name") == name:
                                    n.set("class", num)
                            for s in brd.iter("signal"):
                                if s.get("name") == name:
                                    s.set("class", num)
            self.sch, self.xml = ET.tostring(sch), ET.tostring(brd)
            return {"raw": "", "dialogs": []}
        if op == "run":
            self.ran.append("UNDO;")
            self.xml, self.sch = self.undo.pop()
            return {"raw": "", "dialogs": []}
        return super().call(op, args, timeout, answers, forms)


def brd_for_sch():
    return ET.fromstring('<eagle><drawing><board><classes><class number="0" name="default"/><class number="7" name="eth_100"/>'
                         '</classes><signals><signal name="A"/><signal name="B"/><signal name="D"/></signals></board></drawing></eagle>')


class PadWidthTest(unittest.TestCase):
    """PoE board 2026-10-08: pwr_5V's 1.0 mm class width rule gave 14 Copper Width errors on 5V pads."""

    def board(self):
        return ET.fromstring("""<eagle><drawing><board><plain/><libraries><library name="L"><packages>
<package name="PIN"><smd name="1" x="0" y="0" dx="0.35" dy="1.2" layer="1"/></package>
<package name="BIG"><smd name="1" x="0" y="0" dx="1.5" dy="1.5" layer="1"/></package>
</packages></library></libraries><designrules name="t"/>
<classes><class number="0" name="default"/><class number="3" name="pwr_5V" width="0" drill="0"/></classes>
<elements><element name="U1" library="L" package="PIN" value="" x="5" y="5"/>
<element name="C1" library="L" package="BIG" value="" x="10" y="5"/></elements>
<signals><signal name="5V" class="3"><contactref element="U1" pad="1"/><contactref element="C1" pad="1"/></signal></signals>
</board></drawing></eagle>""")

    def rules(self, width):
        return RULES.replace("</rules>", f'<rule type="Minimum Copper Width" enabled="yes" priority="0" onescope="classes=3" '
                                         f'value="{width}mm" name="Copper Width 3"/></rules>')

    def test_rule_wider_than_pads_is_flagged(self):
        root = self.board()
        out = NC.summarize(root, self.rules(1.0), NC.pad_widths(root))
        w = next(x for x in out["warnings"] if x.startswith("class pwr_5V"))
        self.assertIn("1.0 mm width rule (Copper Width 3, scope classes=3 / -) also applies to pads", w)
        self.assertIn("1 pad(s) on its nets are narrower (U1.1 0.35 mm)", w)

    def test_rule_within_the_pads_is_fine(self):
        root = self.board()
        out = NC.summarize(root, self.rules(0.3), NC.pad_widths(root))
        self.assertFalse(any(x.startswith("class pwr_5V") for x in out["warnings"]))


class LaggingRulesTest(unittest.TestCase):
    def test_class_with_no_values_says_why(self):
        # PoE board 2026-10-07: four classes made in the dialog, their rules not in the working copy yet
        brd = ET.fromstring(f"<eagle><drawing><board>{CLASSES.replace('</classes>', '<class number=\"8\" name=\"shield\" width=\"0\" drill=\"0\"></class></classes>')}"
                            "<signals/></board></drawing></eagle>")
        out = NC.summarize(brd, RULES)
        self.assertIn("class shield: no width, drill or clearance found", out["notes"][0])
        self.assertIn("saved", out["notes"][0])


class SetNetClassToolTest(unittest.TestCase):
    class Bridge:
        def __init__(self, modified):
            self.modified, self.ops = modified, []
            import tempfile
            self.dir = tempfile.mkdtemp()

        def call(self, op, args=None, timeout=60, **kw):
            self.ops.append(op)
            if op == "context":
                return {"active_document": {"name": "PoE Magnetics Test Board", "kind": "board", "modified": self.modified}}
            if op == "activate":
                return {}
            if op == "export":
                import os
                path = os.path.join(self.dir, "b.brd")
                with open(path, "wb") as f:
                    f.write(ET.tostring(board_root()))
                return {"path": path}
            if op == "design_rules":
                return {"xml": RULES, "modified": "2026-10-07T21:09:01"}
            raise AssertionError(op)

    def test_refuses_with_unsaved_changes(self):
        b = self.Bridge(modified=True)
        with mock.patch.object(S, "session", Session(b)):
            with self.assertRaises(Exception) as cm:
                S.set_net_class("poe_ct", 0.3, 0.2)
        self.assertIn("unsaved changes", str(cm.exception))
        self.assertEqual(b.ops, ["context"])                       # nothing read, nothing written

    def test_writes_the_file_when_saved(self):
        import os
        import tempfile
        b = self.Bridge(modified=False)
        out_dir = tempfile.mkdtemp()
        with mock.patch.object(S, "session", Session(b)):
            res = S.set_net_class("poe_ct", 0.3, 0.2, 0.3, out_dir=out_dir)
        self.assertTrue(os.path.exists(res["file"]))
        self.assertEqual(res["class"]["number"], 1)
        text = open(res["file"], encoding="utf-8").read()
        self.assertIn('onescope="classes=1"', text)
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="utf-8"?>'))


class AssignToolTest(unittest.TestCase):
    def call(self, fake, *a, **kw):
        with mock.patch.object(S, "session", Session(fake)):
            return S.assign_net_class(*a, **kw)

    def test_assigns_in_one_write_and_verifies(self):
        fake = FakeSchematic(sch_root(), brd_for_sch())
        res = self.call(fake, "ETH_100", ["A", "D"], design="PoE Magnetics Test Board")
        self.assertEqual(len(fake.ran), 1)
        self.assertIn("EDIT .s1; CHANGE CLASS eth_100 (20 2); EDIT .s2; CHANGE CLASS eth_100 (5 0);", fake.ran[0])
        self.assertIn("2 net(s) now in eth_100 (class 7)", res["detail"])
        classes = {s.get("name"): s.get("class") for s in ET.fromstring(fake.xml).iter("signal")}
        self.assertEqual(classes, {"A": "7", "B": None, "D": "7"})

    def test_another_net_changing_is_undone(self):
        fake = FakeSchematic(sch_root(), brd_for_sch(), wrong="B")
        with self.assertRaises(Exception) as cm:                 # WriteFailed, as the tool's error
            self.call(fake, "eth_100", ["A"])
        self.assertIn("other nets changed class too: B", str(cm.exception))
        self.assertEqual(fake.ran[-1], "UNDO;")

    def test_refusals_write_nothing(self):
        fake = FakeSchematic(sch_root(), brd_for_sch())
        with self.assertRaises(Exception) as cm:
            self.call(fake, "poe_ct", ["A"])
        self.assertIn("no net class 'poe_ct'", str(cm.exception))
        with self.assertRaises(Exception) as cm:
            self.call(fake, "eth_100", ["A", "C"])
        self.assertIn("nothing was written; cannot pick: C:", str(cm.exception))
        self.assertEqual(fake.ran, [])


if __name__ == "__main__":
    unittest.main()
