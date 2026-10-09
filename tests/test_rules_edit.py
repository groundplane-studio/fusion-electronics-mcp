import os
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from fusion_mcp import server as S
from fusion_mcp.session import Session
from fusion_offline import rules_edit as RE

ROOT = os.path.dirname(os.path.dirname(__file__))
EDRU = os.path.join(ROOT, "server", "fusion_mcp", "data", "rules", "JLC 2-layer 1.6mm.edru")


def jlc_rules() -> str:
    text = open(EDRU, encoding="utf-8").read()
    return text[text.index("<designrules"):text.index("</designrules>") + len("</designrules>")]


def board():
    return ET.fromstring('<eagle><drawing><board><classes><class number="0" name="default"/>'
                         '<class number="7" name="eth_100"/></classes><signals/></board></drawing></eagle>')


class SettingsTest(unittest.TestCase):
    def test_bundled_jlc_rules(self):
        st = RE.settings(jlc_rules())
        self.assertEqual(sorted(st["teardrops"]), ["pad", "smd", "via", "wire_polygon"])
        self.assertFalse(any(t["auto_generate"] for t in st["teardrops"].values()))
        self.assertEqual(st["teardrops"]["via"]["lengthratio"], 0.5)
        self.assertEqual(st["pair"], {"max_length_difference_mm": 10.0, "gap_factor": 2.5})
        self.assertEqual(st["clearances_mm"]["wire_wire"], 0.12)
        self.assertEqual(len(st["warnings"]), 2)
        self.assertIn("10 mm EAGLE default", st["warnings"][0])
        self.assertIn("none auto-generates", st["warnings"][1])


class EditTest(unittest.TestCase):
    def test_teardrops_pair_and_clearances(self):
        text, changes, unchanged = RE.edit(jlc_rules(), {0: "default", 7: "eth_100"},
                                teardrops={"all": {"auto_generate": True, "curved_sides": True}, "via": {"lengthratio": 0.6}},
                                pair_max_length_difference_mm=0.127, pair_gap_factor=3, clearances_mm={"wire_wire": 0.15})
        body = text[text.index("<designrules"):text.index("</designrules>") + 14]
        st = RE.settings(body)
        self.assertTrue(all(t["auto_generate"] and t["curved_sides"] for t in st["teardrops"].values()))
        self.assertEqual(st["teardrops"]["via"]["lengthratio"], 0.6)
        self.assertEqual(st["pair"], {"max_length_difference_mm": 0.127, "gap_factor": 3.0})
        self.assertEqual(st["clearances_mm"]["wire_wire"], 0.15)
        self.assertEqual(st["warnings"], [])
        # classic params kept in step, every class kept, a loadable file
        self.assertIn('name="dpMaxLengthDifference" value="0.127mm"', body)
        self.assertIn('name="mdWireWire" value="0.15mm"', body)
        self.assertEqual(re.findall(r'<class number="(\d+)" name="([^"]+)"', body), [("0", "default"), ("7", "eth_100")])
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="utf-8"?>'))
        self.assertIn("pair max length difference = 0.127 mm", changes)

    def test_values_already_set_are_reported_not_rewritten(self):
        # PoE board, 2026-10-08: the pair skew was already 0.1 mm; asking for it again changes nothing
        text, changes, unchanged = RE.edit(jlc_rules(), {0: "default"}, pair_max_length_difference_mm=10,
                                           clearances_mm={"wire_wire": 0.12})
        self.assertIsNone(text)
        self.assertEqual(changes, [])
        self.assertEqual(unchanged, ["pair max length difference is already 10 mm", "clearance wire_wire is already 0.12 mm"])
        text, changes, unchanged = RE.edit(jlc_rules(), {0: "default"}, pair_max_length_difference_mm=10,
                                           pair_gap_factor=3)
        self.assertIsNotNone(text)
        self.assertEqual(changes, ["pair gap factor = 3"])

    def test_refusals(self):
        for kw in ({"teardrops": {"vias": {"auto_generate": True}}}, {"teardrops": {"via": {"avoid": True}}},
                   {"teardrops": {"via": {"lengthratio": 5}}}, {"clearances_mm": {"trace_trace": 0.1}},
                   {"pair_gap_factor": 0}, {}):
            with self.assertRaises(ValueError, msg=kw):
                RE.edit(jlc_rules(), {0: "default"}, **kw)


class Bridge:
    def __init__(self, modified=False):
        self.modified, self.dir = modified, tempfile.mkdtemp()

    def call(self, op, args=None, timeout=60, **kw):
        if op == "context":
            return {"active_document": {"name": "PoE Magnetics Test Board", "modified": self.modified}}
        if op == "activate":
            return {}
        if op == "export":
            path = os.path.join(self.dir, "b.brd")
            with open(path, "wb") as f:
                f.write(ET.tostring(board()))
            return {"path": path}
        if op == "design_rules":
            return {"xml": jlc_rules(), "modified": "2026-10-08T00:00:00"}
        raise AssertionError(op)


class ToolTest(unittest.TestCase):
    def test_get_design_rules_reports_teardrops_and_pair(self):
        with mock.patch.object(S, "session", Session(Bridge())):
            res = S.get_design_rules()
        self.assertEqual(res["pair"]["max_length_difference_mm"], 10.0)
        self.assertIn("via", res["teardrops"])
        self.assertTrue(any("10 mm EAGLE default" in w for w in res["warnings"]))

    def test_edit_writes_a_file_and_refuses_unsaved(self):
        out = tempfile.mkdtemp()
        with mock.patch.object(S, "session", Session(Bridge())):
            res = S.edit_design_rules(pair_max_length_difference_mm=0.127, out_dir=out)
        self.assertTrue(os.path.exists(res["file"]))
        self.assertIn("0.127mm", open(res["file"], encoding="utf-8").read())
        with mock.patch.object(S, "session", Session(Bridge())):
            same = S.edit_design_rules(pair_max_length_difference_mm=10, out_dir=out)
        self.assertIsNone(same["file"])
        self.assertIn("no file written", same["note"])
        with mock.patch.object(S, "session", Session(Bridge(modified=True))):
            with self.assertRaises(Exception) as cm:
                S.edit_design_rules(pair_max_length_difference_mm=0.127, out_dir=out)
        self.assertIn("unsaved changes", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
