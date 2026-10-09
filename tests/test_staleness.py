import unittest
from unittest import mock

from fusion_mcp import server as S
from fusion_mcp import staleness as ST


class NoteTest(unittest.TestCase):
    def setUp(self):
        ST._cache.update(at=-1e9, note=None)

    def tearDown(self):
        ST._cache.update(at=-1e9, note=None)

    def test_fresh_code_has_no_note(self):
        with mock.patch.object(ST, "newest", lambda: (ST.STARTED, "x.py")):
            self.assertIsNone(ST.note(clock=lambda: 100.0))

    def test_newer_file_on_disk(self):
        with mock.patch.object(ST, "newest", lambda: (ST.STARTED + 60, "/x/server.py")):
            msg = ST.note(clock=lambda: 100.0)
        self.assertIn("running older code than is on disk (server.py changed", msg)
        self.assertIn("toggle fusion-electronics off and on in Connectors", msg)

    def test_checked_at_most_every_ten_seconds(self):
        with mock.patch.object(ST, "newest", lambda: (ST.STARTED, "x.py")):
            self.assertIsNone(ST.note(clock=lambda: 100.0))
        with mock.patch.object(ST, "newest", lambda: (ST.STARTED + 60, "x.py")):
            self.assertIsNone(ST.note(clock=lambda: 105.0))          # cached
            self.assertIsNotNone(ST.note(clock=lambda: 111.0))       # checked again

    def test_attach(self):
        self.assertEqual(ST.attach({"a": 1}, "old"), {"a": 1, "server_outdated": "old"})
        self.assertEqual(ST.attach(["x"], "old"), ["x", "Note: old"])
        self.assertEqual(ST.attach("x", "old"), "x\nNote: old")
        self.assertEqual(ST.attach({"a": 1}, None), {"a": 1})


class ToolWrapperTest(unittest.TestCase):
    def test_replies_and_errors_carry_the_note(self):
        with mock.patch.object(ST, "note", lambda: "running older code"):
            res = S.length_tolerances("DDR4")
            self.assertEqual(res["server_outdated"], "running older code")
        with mock.patch.object(ST, "note", lambda: None):
            self.assertNotIn("server_outdated", S.length_tolerances("DDR4"))

    def test_error_message_carries_the_note(self):
        with mock.patch.object(ST, "note", lambda: "running older code"):
            with self.assertRaises(Exception) as cm:
                S.set_part_rating("X1", 1.0, "")
        self.assertIn("(Note: running older code)", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
