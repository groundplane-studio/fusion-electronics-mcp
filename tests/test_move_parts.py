import json
import os
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from fusion_mcp import server as S
from fusion_mcp.session import Session, WriteFailed
from fusion_offline import placement_check as PC

from test_placement_check import board, el  # noqa: F401  (NOCY package comes with board)


class FakeFusion:
    """Applies MOVE / ROTATE to a board XML the way Fusion does, keeps an undo stack, and can
    shove a bystander aside (Fusion's push mode) to test the batch check."""
    def __init__(self, root: ET.Element, push: str | None = None, design: str = "PoE Magnetics Test Board"):
        self.xml = ET.tostring(root)
        self.design = design
        self.designs = []          # names to switch to on later context calls (another session switching)
        self.undo = []
        self.ran = []
        self.push = push
        self.modes = []
        self.dir = tempfile.mkdtemp()

    def _els(self, root):
        return {e.get("name"): e for e in root.iterfind("./drawing/board/elements/element")}

    def call(self, op, args=None, timeout=60, answers=None, forms=None):
        if op == "activate":
            return {}
        if op == "context":
            if self.designs:
                self.design = self.designs.pop(0)
            return {"active_document": {"name": self.design, "kind": "board"}}
        if op == "violation_mode":
            self.modes.append(args.get("set"))
            return {"before": "push"}
        if op == "export":
            path = os.path.join(self.dir, f"b{len(self.ran)}.brd")
            with open(path, "wb") as f:
                f.write(self.xml)
            return {"path": path}
        if op == "run":
            cmds = args["commands"]
            self.ran.append(cmds)
            if cmds.strip() == "UNDO;":
                self.xml = self.undo.pop()
                return {"raw": "", "dialogs": []}
            self.undo.append(self.xml)
            root = ET.fromstring(self.xml)
            els = self._els(root)
            for name, x, y in re.findall(r"MOVE '([^']+)' \(([-\d.]+) ([-\d.]+)\)", cmds):
                els[name].set("x", x)
                els[name].set("y", y)
            for rot, name in re.findall(r"ROTATE =(M?R[\d.]+) '([^']+)'", cmds):
                els[name].set("rot", rot)
            if self.push:
                els[self.push].set("x", str(float(els[self.push].get("x")) + 1.0))
            self.xml = ET.tostring(root)
            return {"raw": "", "dialogs": []}
        raise AssertionError(op)


def parts_root():
    return board(el("R1", 5, 5) + el("R2", 5, 7) + el("R3", 10, 10) + el("C1", 20, 20))


class MoveToolsTest(unittest.TestCase):
    def setUp(self):
        S._PREVIEWED_ON.clear()

    def run_tool(self, fake, fn, *a, **kw):
        with mock.patch.object(S, "session", Session(fake)), mock.patch.object(S, "_move_picture", lambda *x: None):
            out = fn(*a, **kw)
        return json.loads(out[-1])

    def test_batch_is_one_write_and_one_undo_step(self):
        fake = FakeFusion(parts_root())
        res = self.run_tool(fake, S._move_parts, [{"ref": "R1", "x_mm": 8, "y_mm": 5},
                                                  {"ref": "R2", "angle": 90},
                                                  {"ref": "R3", "x_mm": 12, "y_mm": 12, "angle": 180, "bottom": True}],
                            False)
        self.assertTrue(res["written"])
        writes = [c for c in fake.ran if c.strip() != "UNDO;"]
        self.assertEqual(len(writes), 1)                        # one run = one undo step in the add-in
        self.assertIn("MOVE 'R1' (8 5);", writes[0])
        self.assertIn("ROTATE =R90 'R2';", writes[0])
        self.assertNotIn("MOVE 'R2'", writes[0])                # only what changes is sent
        self.assertIn("ROTATE =MR180 'R3';", writes[0])
        self.assertEqual(fake.modes, ["ignore", "push"])         # Fusion's push mode restored after
        els = fake._els(ET.fromstring(fake.xml))
        self.assertEqual((els["R3"].get("x"), els["R3"].get("rot")), ("12", "MR180"))

    def test_bystander_pushed_aside_undoes_the_whole_batch(self):
        fake = FakeFusion(parts_root(), push="C1")
        before = fake.xml
        with mock.patch.object(S, "session", Session(fake)):
            with self.assertRaises(Exception) as cm:
                S._move_parts([{"ref": "R1", "x_mm": 8, "y_mm": 5}, {"ref": "R2", "x_mm": 8, "y_mm": 7}], False)
        self.assertIn("Fusion also moved C1", str(cm.exception))
        self.assertEqual(fake.ran[-1], "UNDO;")
        self.assertEqual(fake.xml, before)

    def test_dry_run_writes_nothing_and_reports_what_changes(self):
        # R2 moved 0.6 mm above R1: its courtyard overlaps R1's and its pads come within the rules
        fake = FakeFusion(parts_root())
        res = self.run_tool(fake, S._move_parts, [{"ref": "R2", "x_mm": 5, "y_mm": 5.6}], True)
        self.assertFalse(res["written"])
        self.assertEqual(fake.ran, [])
        self.assertEqual([(i["kind"], i["a"], i["b"]) for i in res["introduced"] if i["kind"] != "silkscreen"],
                         [("courtyard", "R1", "R2"), ("pads", "R2.1", "R1.1"), ("pads", "R2.2", "R1.2")])
        self.assertEqual(res["moves"][0]["from"], [5.0, 7.0])

    def test_dry_run_reports_cleared_problems(self):
        fake = FakeFusion(board(el("R1", 5, 5) + el("R2", 5, 5.6)))
        res = self.run_tool(fake, S._move_parts, [{"ref": "R2", "y_mm": 7}], True)
        self.assertEqual([(i["kind"], i["a"], i["b"]) for i in res["cleared"] if i["kind"] != "silkscreen"],
                         [("courtyard", "R1", "R2"), ("pads", "R2.1", "R1.1"), ("pads", "R2.2", "R1.2")])
        self.assertEqual(res["introduced"], [])

    def test_nothing_to_do_and_bad_input(self):
        fake = FakeFusion(parts_root())
        res = self.run_tool(fake, S._move_parts, [{"ref": "R1", "x_mm": 5, "y_mm": 5}], False)
        self.assertFalse(res["written"])
        self.assertEqual(fake.ran, [])
        with mock.patch.object(S, "session", Session(fake)):
            with self.assertRaises(ValueError):
                S._move_parts([{"ref": "R99", "x_mm": 1}], False)
            with self.assertRaises(ValueError):
                S._move_parts([{"ref": "R1", "x_mm": 1}, {"ref": "R1", "y_mm": 1}], False)

    def test_single_part_tools_share_the_path(self):
        fake = FakeFusion(parts_root())
        res = self.run_tool(fake, S.move_part, "R1", 6, 5, dry_run=True)
        self.assertFalse(res["written"])
        res = self.run_tool(fake, S.rotate_part, "R1", 90)
        self.assertTrue(res["written"])
        self.assertIn("ROTATE =R90 'R1';", fake.ran[-1])


class DesignGuardTest(unittest.TestCase):
    """2026-10-07: a dry run on the PoE board, then another session switched Fusion to the
    RV1126B SoM, whose parts have the same names, before the write."""

    def setUp(self):
        S._PREVIEWED_ON.clear()

    def call(self, fake, *a, **kw):
        with mock.patch.object(S, "session", Session(fake)), mock.patch.object(S, "_move_picture", lambda *x: None):
            return json.loads(S._move_parts(*a, **kw)[-1])

    def test_wrong_design_is_refused_before_anything_is_sent(self):
        fake = FakeFusion(parts_root(), design="RV1126B SoM")
        with self.assertRaises(WriteFailed) as cm:
            self.call(fake, [{"ref": "R1", "x_mm": 8}], False, design="PoE Magnetics Test Board")
        self.assertIn("active design is 'RV1126B SoM'", str(cm.exception))
        self.assertEqual(fake.ran, [])

    def test_switch_just_before_the_write_is_caught(self):
        # first context check passes; Fusion is switched before the check right before writing
        fake = FakeFusion(parts_root())
        fake.designs = ["PoE Magnetics Test Board", "RV1126B SoM"]
        with self.assertRaises(WriteFailed):
            self.call(fake, [{"ref": "R1", "x_mm": 8}], False, design="PoE Magnetics Test Board")
        self.assertEqual(fake.ran, [])

    def test_dry_run_then_switch_refuses_a_write_without_design(self):
        fake = FakeFusion(parts_root())
        dry = self.call(fake, [{"ref": "R1", "x_mm": 8}], True)
        self.assertEqual(dry["design"], "PoE Magnetics Test Board")
        fake.design = "RV1126B SoM"
        with self.assertRaises(WriteFailed) as cm:
            self.call(fake, [{"ref": "R1", "x_mm": 8}], False)
        self.assertIn("last previewed on 'PoE Magnetics Test Board'", str(cm.exception))
        self.assertEqual(fake.ran, [])
        res = self.call(fake, [{"ref": "R1", "x_mm": 8}], False, design="RV1126B SoM")   # named on purpose
        self.assertTrue(res["written"])

    def test_same_design_dry_run_then_write(self):
        fake = FakeFusion(parts_root())
        self.call(fake, [{"ref": "R1", "x_mm": 8}], True)
        res = self.call(fake, [{"ref": "R1", "x_mm": 8}], False)
        self.assertEqual((res["written"], res["design"]), (True, "PoE Magnetics Test Board"))


class FullerReportTest(unittest.TestCase):
    def setUp(self):
        S._PREVIEWED_ON.clear()

    def test_silkscreen_and_derived_courtyards_are_in_the_diff(self):
        # R1's silk line lies on R2's pads at 0.3 mm pitch; C1 (no library courtyard) sits on R3
        root = board(el("R1", 5, 5) + el("R2", 5, 5.3) + el("R3", 15, 5) + el("C1", 15, 5.6, "", "NOCY"))
        moved = PC.apply_moves(root, PC.resolve_moves(root, [{"ref": "R2", "y_mm": 8}, {"ref": "C1", "y_mm": 9}]))
        eff = PC.move_effects(root, moved, ["R2", "C1"])
        kinds = {(i["kind"], i.get("derived", [None])[0] if i["kind"] == "courtyard" else None) for i in eff["cleared"]}
        self.assertIn(("silkscreen", None), kinds)
        self.assertIn(("courtyard", "C1"), kinds)
        self.assertEqual(eff["introduced"], [])


class ApplyMovesTest(unittest.TestCase):
    def test_left_out_values_keep_their_current_state(self):
        root = board(el("R1", 5, 5, "MR90"))
        t = PC.resolve_moves(root, [{"ref": "R1", "x_mm": 6}])[0]
        self.assertEqual((t["x"], t["y"], t["angle"], t["bottom"], t["moves"], t["turns"]), (6.0, 5.0, 90.0, True, True, False))
        moved = PC.apply_moves(root, [t])
        e = moved.find("./drawing/board/elements/element")
        self.assertEqual((e.get("x"), e.get("rot")), ("6", "MR90"))
        self.assertEqual(root.find("./drawing/board/elements/element").get("x"), "5")   # original untouched


class ToolRegisteredTest(unittest.TestCase):
    def test_move_parts_is_a_tool(self):
        import asyncio
        names = {t.name for t in asyncio.run(S.mcp.list_tools())}
        self.assertTrue({"move_parts", "move_part", "rotate_part"} <= names)


if __name__ == "__main__":
    unittest.main()
