"""DRC around writes: a dead bridge aborts, schematic writes skip it, retries reuse it, moved errors."""

import json
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from fusion_mcp import server as S
from fusion_mcp.bridge import BridgeOpError, BridgeUnavailable
from fusion_mcp.session import Session, WriteFailed
from fusion_offline import drc as DRC

from test_drc import AIR, DrcFusion, cc
from test_move_parts import parts_root


def is_drc(args):
    return args["commands"].replace("GRID MM 0.0001;", "").replace("GRID LAST;", "").strip() == "DRC;"


class DeadOnDrc(DrcFusion):
    def call(self, op, args=None, timeout=60, answers=None, forms=None):
        if op == "run" and is_drc(args):
            raise BridgeUnavailable("No answer from Fusion within 80s")
        return super().call(op, args, timeout, answers, forms)


class DrcErrorsFail(DrcFusion):
    def call(self, op, args=None, timeout=60, answers=None, forms=None):
        if op == "errors":
            raise BridgeOpError("error", "could not read the DRC errors")
        return super().call(op, args, timeout, answers, forms)


class TravellingError(DrcFusion):
    """R1 carries a clearance error with no signature, wherever it is."""
    def call(self, op, args=None, timeout=60, answers=None, forms=None):
        if op == "errors":
            r1 = next(e for e in ET.fromstring(self.xml).iter("element") if e.get("name") == "R1")
            errs = [AIR, cc(float(r1.get("x")), 5)]
            return {"kind": "board", "count": len(errs), "errors": errs}
        return super().call(op, args, timeout, answers, forms)


class EditorFake(DrcFusion):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.activated = []

    def call(self, op, args=None, timeout=60, answers=None, forms=None):
        if op == "activate":
            self.activated.append(args["kind"])
            return {}
        if op == "context":
            return {"active_document": {"name": self.design}, "active_editor": "schematic"}
        return super().call(op, args, timeout, answers, forms)


def move(session, x, dry=False):
    with mock.patch.object(S, "session", session), mock.patch.object(S, "_move_picture", lambda *a: None):
        return json.loads(S._move_parts([{"ref": "R1", "x_mm": x}], dry)[-1])


class BeforeDrcTest(unittest.TestCase):
    def setUp(self):
        S._PREVIEWED_ON.clear()

    def test_unreachable_fusion_during_drc_aborts_the_write(self):
        fake = DeadOnDrc(parts_root())
        with self.assertRaises(BridgeUnavailable) as cm:
            move(Session(fake), 8)
        self.assertIn("nothing was written", str(cm.exception))
        self.assertFalse(any("MOVE" in c for c in fake.ran))

    def test_a_drc_problem_of_its_own_is_noted_and_the_write_goes_ahead(self):
        fake = DrcErrorsFail(parts_root())
        res = move(Session(fake), 8)
        self.assertIn("DRC not run", res["detail"])
        self.assertTrue(any("MOVE" in c for c in fake.ran))


class WhenDrcRunsTest(unittest.TestCase):
    def setUp(self):
        S._PREVIEWED_ON.clear()

    def test_schematic_writes_run_no_drc(self):
        fake = DrcFusion(parts_root())
        Session(fake).verified_write("schematic", "NAME 'X' (1 1);", lambda b, a: (True, "ok"))
        self.assertEqual(fake.drc_runs, 0)

    def test_failed_and_undone_write_keeps_the_cached_drc(self):
        fake = DrcFusion(parts_root())
        session = Session(fake)
        with self.assertRaises(WriteFailed):
            session.verified_write("board", "MOVE 'R1' (6 5);", lambda b, a: (False, "not what was asked"))
        self.assertEqual(fake.drc_runs, 1)               # before only; the write was undone
        session.verified_write("board", "MOVE 'R1' (7 5);", lambda b, a: (True, "ok"))
        self.assertEqual(fake.drc_runs, 2)               # the retry reused the "before" DRC

    def test_drc_brings_the_schematic_editor_back(self):
        fake = EditorFake(parts_root())
        Session(fake).drc_errors()
        self.assertEqual(fake.activated, ["board", "schematic"])
        fake.activated.clear()
        Session(fake).drc_errors(restore_editor=False)
        self.assertEqual(fake.activated, ["board"])


class MovedErrorTest(unittest.TestCase):
    def setUp(self):
        S._PREVIEWED_ON.clear()

    def test_existing_error_moving_with_its_part_is_not_new(self):
        fake = TravellingError(parts_root())
        fake.undo_on_new_drc = True
        res = move(Session(fake), 6)
        self.assertIn("1 existing moved with the change", res["detail"])
        self.assertIn("no new errors", res["detail"])

    def test_diff_pairs_unsigned_errors_only_when_asked(self):
        before, after = [cc(1, 2)], [cc(3, 4), {"code": "Width", "description": "Width"}]
        self.assertEqual(DRC.diff(before, after)["new_count"], 2)
        d = DRC.diff(before, after, pair_moved=True)
        self.assertEqual((d["new_count"], d["fixed_count"], d["moved_count"], d["new_copper"]), (1, 0, 1, 1))
        signed = DRC.diff([cc(1, 2, "a")], [cc(3, 4, "b")], pair_moved=True)
        self.assertEqual((signed["new_count"], signed["moved_count"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
