import json
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from fusion_mcp import server as S
from fusion_mcp.session import Session
from fusion_offline import drc as DRC

from test_move_parts import FakeFusion, parts_root

AIR = {"code": "Air-Wire", "description": "Air-Wire", "x_mm": 1, "y_mm": 1}


def cc(x, y, sig=None):
    e = {"code": "CopperClearance", "description": "Copper Clearance - Net Class: 7 eth_100", "layer": "Top",
         "x_mm": x, "y_mm": y}
    if sig:
        e["signature"] = sig
    return e


class OfflineTest(unittest.TestCase):
    def test_summary_groups_and_counts_airwires(self):
        s = DRC.summarize([AIR, AIR, cc(1, 2), cc(3, 4), {"code": "Overlap", "description": "Overlap"}])
        self.assertEqual(s, {"total": 5, "airwires": 2, "by_type": {"Copper Clearance": 2, "Overlap": 1}})

    def test_diff_by_signature_or_position(self):
        before = [AIR, AIR, cc(1, 2, "s1"), cc(3, 4)]
        after = [AIR, cc(1.5, 2.5, "s1"), cc(5, 6), {"code": "Width", "description": "Width"}]
        d = DRC.diff(before, after)
        self.assertEqual(d["new_count"], 2)                       # (5, 6) and the width error; s1 is the same error
        self.assertEqual(d["new"]["Copper Clearance"]["at"], [[5, 6, "Top"]])
        self.assertEqual((d["fixed_count"], d["unchanged_count"], d["airwires"]), (1, 1, [2, 1]))
        self.assertEqual(d["new_copper"], 2)
        self.assertEqual(DRC.line(d), "DRC: 2 new (Copper Clearance x1 at [5, 6], Width x1); 1 fixed; airwires 2 -> 1")
        self.assertEqual(DRC.line(DRC.diff(before, before)), "DRC: no new errors")


class DrcFusion(FakeFusion):
    """A FakeFusion with DRC: R1 at x 8 has a clearance error; every DRC run is counted."""
    drc_after_writes = True
    undo_on_new_drc = False

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.drc_runs = 0

    def call(self, op, args=None, timeout=60, answers=None, forms=None):
        if op == "run" and args["commands"].replace("GRID MM 0.0001;", "").replace("GRID LAST;", "").strip() == "DRC;":
            self.drc_runs += 1
            return {"raw": "", "dialogs": [{"text": "DRC", "expected": False}]}   # its window is not a failure
        if op == "errors":
            r1 = next(e for e in ET.fromstring(self.xml).iter("element") if e.get("name") == "R1")
            errs = [AIR] + ([cc(8, 5)] if r1.get("x") == "8" else [])
            return {"kind": "board", "count": len(errs), "errors": errs}
        return super().call(op, args, timeout, answers, forms)


class WriteDrcTest(unittest.TestCase):
    def setUp(self):
        S._PREVIEWED_ON.clear()

    def move(self, fake, x):
        with mock.patch.object(S, "session", self.session), mock.patch.object(S, "_move_picture", lambda *a: None):
            return json.loads(S._move_parts([{"ref": "R1", "x_mm": x}], False)[-1])

    def test_new_errors_reported_and_the_baseline_reused(self):
        fake = DrcFusion(parts_root())
        self.session = Session(fake)
        res = self.move(fake, 8)
        self.assertIn("DRC: 1 new (Copper Clearance x1 at [8, 5])", res["detail"])
        self.assertEqual(fake.drc_runs, 2)                         # before and after
        res = self.move(fake, 9)                                   # board unchanged since: no DRC before
        self.assertIn("DRC: no new errors; 1 fixed", res["detail"])
        self.assertEqual(fake.drc_runs, 3)

    def test_undo_on_new_copper_errors(self):
        fake = DrcFusion(parts_root())
        fake.undo_on_new_drc = True
        self.session = Session(fake)
        before = fake.xml
        with self.assertRaises(Exception) as cm:
            self.move(fake, 8)
        self.assertIn("added 1 copper DRC error", str(cm.exception))
        self.assertEqual(fake.xml, before)

    def test_off_by_default_for_bridges_without_the_setting(self):
        fake = FakeFusion(parts_root())
        self.session = Session(fake)
        res = self.move(fake, 8)
        self.assertNotIn("DRC", res["detail"])


class CheckDrcTest(unittest.TestCase):
    def test_first_and_second_check(self):
        S._DRC_LAST.clear()
        fake = DrcFusion(parts_root())
        with mock.patch.object(S, "session", Session(fake)):
            first = S.check_drc()
            self.assertIsNone(first["since_last_check"])
            self.assertEqual((first["airwires"], first["by_type"]), (1, {}))
            fake.xml = fake.xml.replace(b'name="R1" library="L" package="RES_0402" value="0" x="5"',
                                        b'name="R1" library="L" package="RES_0402" value="0" x="8"')
            second = S.check_drc()
        self.assertEqual(second["since_last_check"]["new_count"], 1)
        self.assertEqual(second["design"], "PoE Magnetics Test Board")


if __name__ == "__main__":
    unittest.main()
