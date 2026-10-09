import math
import os
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from fusion_mcp import server as S
from fusion_mcp.session import Session
from fusion_offline import sch_labels as SL

RES = """<symbol name="RES">
<wire x1="-2.54" y1="-0.889" x2="2.54" y2="-0.889" width="0.254" layer="94"/>
<wire x1="2.54" y1="-0.889" x2="2.54" y2="0.889" width="0.254" layer="94"/>
<wire x1="2.54" y1="0.889" x2="-2.54" y2="0.889" width="0.254" layer="94"/>
<wire x1="-2.54" y1="0.889" x2="-2.54" y2="-0.889" width="0.254" layer="94"/>
<text x="-3.81" y="1.4986" size="1.778" layer="95">&gt;NAME</text>
<text x="-3.81" y="-3.302" size="1.778" layer="96">&gt;VALUE</text>
<pin name="1" x="-5.08" y="0" length="short" direction="pas"/>
<pin name="2" x="5.08" y="0" length="short" direction="pas" rot="R180"/>
</symbol>"""
QUAD = """<symbol name="QUAD"><text x="0" y="5" size="1.778" layer="95">&gt;NAME</text>
<pin name="A" x="-5" y="0"/><pin name="B" x="5" y="0"/><pin name="C" x="0" y="5"/><pin name="D" x="0" y="-5"/></symbol>"""


def sch():
    """R1 at R90 (unsmashed: vertical labels); R2 at R0 (horizontal); C1 at R270, smashed with
    vertical labels; U1 a 4-pin part at R90; a net wire passing near R1."""
    parts = "".join(f'<part name="{n}" library="L" deviceset="{d}" device="" value="{v}"/>'
                    for n, d, v in (("R1", "R", "10k"), ("R2", "R", "1k"), ("C1", "R", "100n"), ("U1", "U", "X")))
    c1_attrs = ('<attribute name="NAME" x="31.5" y="12" size="1.778" layer="95" rot="R270"/>'
                '<attribute name="VALUE" x="27" y="12" size="1.778" layer="96" rot="R270"/>')
    return ET.fromstring(f"""<eagle><drawing><schematic><libraries><library name="L">
<symbols>{RES}{QUAD}</symbols><devicesets>
<deviceset name="R" prefix="R"><gates><gate name="G$1" symbol="RES" x="0" y="0"/></gates><devices><device name=""/></devices></deviceset>
<deviceset name="U" prefix="U"><gates><gate name="G$1" symbol="QUAD" x="0" y="0"/></gates><devices><device name=""/></devices></deviceset>
</devicesets></library></libraries><classes/><parts>{parts}</parts><sheets><sheet>
<instances>
<instance part="R1" gate="G$1" x="10" y="10" rot="R90"/>
<instance part="R2" gate="G$1" x="20" y="10"/>
<instance part="C1" gate="G$1" x="30" y="10" smashed="yes" rot="R270">{c1_attrs}</instance>
<instance part="U1" gate="G$1" x="50" y="10" rot="R90"/>
</instances>
<nets><net name="N1" class="0"><segment><wire x1="10" y1="15.08" x2="10" y2="20" width="0.1524" layer="91"/></segment></net></nets>
</sheet></sheets></schematic></drawing></eagle>""")


class PlanTest(unittest.TestCase):
    def test_vertical_two_pin_parts_only(self):
        plan = {p["part"]: p for p in SL.plan_straighten(sch())}
        self.assertEqual(sorted(plan), ["C1", "R1"])            # R2 is horizontal, U1 is not two-pin
        r1 = plan["R1"]
        self.assertTrue(r1["smash"])
        # R1 at R90: body is x 9.111..10.889, y 7.46..12.54; labels go right of it at 0 degrees
        name = next(m for m in r1["moves"] if m["label"] == "NAME")
        self.assertEqual((name["to"], name["angle"]), ([11.389, 10.25], 0.0))
        value = next(m for m in r1["moves"] if m["label"] == "VALUE")
        self.assertEqual(value["to"], [11.389, round(10 - 0.25 - 1.778, 4)])
        self.assertFalse(plan["C1"]["smash"])                   # already smashed

    def test_left_side_and_filters(self):
        plan = SL.plan_straighten(sch(), parts=["R1"], side="left")
        name = next(m for m in plan[0]["moves"] if m["label"] == "NAME")
        self.assertLess(name["to"][0], 9.111)
        self.assertEqual([p["part"] for p in SL.plan_straighten(sch(), prefixes=("C",))], ["C1"])
        with self.assertRaises(ValueError):
            SL.plan_straighten(sch(), side="up")

    def test_commands(self):
        cmds = SL.commands(SL.plan_straighten(sch(), parts=["R1"]))
        self.assertTrue(cmds.startswith("EDIT .s1; SMASH 'R1'; MOVE ("))
        self.assertIn("ROTATE =R0 (11.389 10.25);", cmds)

    def test_match_copies_relative_layout(self):
        # copy C1's layout (R270) onto R1 (R90): relative to the part it is the same layout
        plan, warnings = SL.plan_match(sch(), "C1", ["R1"])
        self.assertEqual(warnings, [])
        moves = {m["label"]: m for m in plan[0]["moves"]}
        # C1: NAME at (31.5, 12) is local (-2, 1.5) at R270; on R1 (10, 10) at R90 that is (8.5, 8)
        self.assertEqual(moves["NAME"]["to"], [8.5, 8.0])
        self.assertEqual(moves["NAME"]["angle"], 90.0)           # label 270 on a part at 270: 0 relative; R1 is at 90
        with self.assertRaises(ValueError):
            SL.plan_match(sch(), "R9", ["R1"])


class FakeSchematic:
    """Applies EDIT .sN, SMASH 'part', MOVE (a) (b) and ROTATE =Rn (p) to the schematic XML.
    wrong_pick: MOVE grabs the nearest instance instead of the label (to test the check)."""
    def __init__(self, root, wrong_pick=False):
        self.xml, self.undo, self.ran, self.wrong = ET.tostring(root), [], [], wrong_pick
        self.dir = tempfile.mkdtemp()

    def call(self, op, args=None, timeout=60, **kw):
        if op in ("activate",):
            return {}
        if op == "context":
            return {"active_document": {"name": "PoE Magnetics Test Board", "kind": "schematic"}}
        if op == "export":
            path = os.path.join(self.dir, f"s{len(self.ran)}.sch")
            with open(path, "wb") as f:
                f.write(self.xml)
            return {"path": path}
        if op == "run":
            cmds = args["commands"]
            if cmds.strip() == "UNDO;":
                self.xml = self.undo.pop()
                self.ran.append("UNDO;")
                return {"raw": "", "dialogs": []}
            self.ran.append(cmds)
            self.undo.append(self.xml)
            root = ET.fromstring(self.xml)
            labels = {(i["part"], n): l for i in SL.instances(root) for n, l in i["labels"].items()}
            insts = {i.get("part"): i for i in root.iter("instance")}
            for c in [c.strip() for c in cmds.split(";") if c.strip()]:
                if c.startswith("SMASH"):
                    part = re.search(r"'([^']+)'", c).group(1)
                    inst = insts[part]
                    inst.set("smashed", "yes")
                    for n in ("NAME", "VALUE"):
                        if inst.find(f"attribute[@name='{n}']") is None and (part, n) in labels:
                            lab = labels[(part, n)]
                            ET.SubElement(inst, "attribute", {"name": n, "x": f"{lab['x']:g}", "y": f"{lab['y']:g}",
                                                              "size": "1.778", "rot": f"R{lab['angle']:g}"})
                elif c.startswith("MOVE"):
                    (ax, ay), (bx, by) = [tuple(map(float, q)) for q in re.findall(r"\(([-\d.]+) ([-\d.]+)\)", c)]
                    attrs = [a for a in root.iter("attribute")
                             if math.dist((float(a.get("x")), float(a.get("y"))), (ax, ay)) < 1e-3]
                    if self.wrong or not attrs:
                        inst = min(insts.values(), key=lambda i: math.dist((float(i.get("x")), float(i.get("y"))), (ax, ay)))
                        inst.set("x", f"{float(inst.get('x')) + bx - ax:g}")
                        inst.set("y", f"{float(inst.get('y')) + by - ay:g}")
                    else:
                        attrs[0].set("x", f"{bx:g}")
                        attrs[0].set("y", f"{by:g}")
                elif c.startswith("ROTATE"):
                    rot = re.search(r"=(M?R[\d.]+)", c).group(1)
                    px, py = map(float, re.search(r"\(([-\d.]+) ([-\d.]+)\)", c).groups())
                    for a in root.iter("attribute"):
                        if math.dist((float(a.get("x")), float(a.get("y"))), (px, py)) < 1e-3:
                            a.set("rot", rot)
            self.xml = ET.tostring(root)
            return {"raw": "", "dialogs": []}
        raise AssertionError(op)


class ToolTest(unittest.TestCase):
    def test_dry_run_then_write(self):
        fake = FakeSchematic(sch())
        with mock.patch.object(S, "session", Session(fake)):
            res = S.straighten_labels()
            self.assertFalse(res["written"])
            self.assertEqual(fake.ran, [])
            res = S.straighten_labels(dry_run=False, design="PoE Magnetics Test Board")
        self.assertTrue(res["written"], res)
        self.assertIn("4 label(s) on 2 part(s) placed", res["detail"])
        insts = {i["part"]: i for i in SL.instances(ET.fromstring(fake.xml))}
        for part in ("R1", "C1"):
            for lab in insts[part]["labels"].values():
                self.assertEqual((lab["smashed"], lab["angle"]), (True, 0.0))
        self.assertEqual(SL.plan_straighten(ET.fromstring(fake.xml)), [])     # nothing left to do

    def test_a_wrong_pick_is_undone(self):
        fake = FakeSchematic(sch(), wrong_pick=True)
        before = fake.xml
        with mock.patch.object(S, "session", Session(fake)):
            with self.assertRaises(Exception) as cm:
                S.straighten_labels(parts=["R1"], dry_run=False)
        self.assertIn("moved or turned", str(cm.exception))
        self.assertEqual(fake.xml, before)

    def test_match_labels_tool(self):
        fake = FakeSchematic(sch())
        with mock.patch.object(S, "session", Session(fake)):
            res = S.match_labels("C1", ["R1"])
        self.assertTrue(res["written"], res)
        r1 = {i["part"]: i for i in SL.instances(ET.fromstring(fake.xml))}["R1"]
        self.assertEqual((r1["labels"]["NAME"]["x"], r1["labels"]["NAME"]["y"]), (8.5, 8.0))


if __name__ == "__main__":
    unittest.main()
