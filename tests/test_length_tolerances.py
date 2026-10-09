import os
import re
import tempfile
import unittest
from unittest import mock

from fusion_mcp import design_store
from fusion_mcp import server as S
from fusion_mcp.session import Session

from test_length_groups import mdi
from test_move_parts import FakeFusion


class TableTest(unittest.TestCase):
    def setUp(self):
        self.rows = S.length_tolerances()["rows"]

    def test_every_value_has_a_source(self):
        for r in self.rows:
            self.assertTrue(r["notes"], r["interface"])
            if r["within_pair"] or r["between_pairs"]:
                self.assertTrue(r["source"] and r["source"]["doc"] and r["source"]["url"], r["interface"])
                self.assertTrue(r["source"]["url"].startswith("https://"))

    def test_mil_values_convert_to_mm(self):
        for r in self.rows:
            for k in ("within_pair", "between_pairs"):
                v = r[k]
                m = re.match(r"(\d+(?:\.\d+)?) mil", v["value"]) if v else None
                if m:
                    self.assertAlmostEqual(v["mm"], float(m.group(1)) * 0.0254, places=3, msg=r["interface"])

    def test_lookup(self):
        self.assertEqual(len(S.length_tolerances("1000BASE-T")["rows"]), 1)
        ddr4 = S.length_tolerances("DDR4")["rows"][0]
        self.assertIn("SPRAD06C", ddr4["source"]["doc"])
        self.assertIsNone(S.length_tolerances("DDR3")["rows"][0]["within_pair"])     # unconfirmed stays blank


class PresetTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.patches = [mock.patch.object(design_store, "path", lambda d: os.path.join(self.dir, d + ".json")),
                        mock.patch.object(S, "session", Session(FakeFusion(mdi())))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_preset_fills_only_what_the_table_has(self):
        res = S.set_length_group("MDI", [["TP0_P", "TP0_N"], ["TP1_P", "TP1_N"]], preset="1000BASE-T")
        self.assertEqual(res["group"]["intra_tol_mm"], 0.508)
        self.assertIsNone(res["group"]["inter_tol_mm"])                 # no confirmed between-pair figure
        self.assertEqual(res["preset"]["missing"], ["inter_tol_mm"])
        res = S.set_length_group("MDI", [["TP0_P", "TP0_N"], ["TP1_P", "TP1_N"]], inter_tol_mm=0.5, preset="1000BASE-T")
        self.assertEqual((res["group"]["intra_tol_mm"], res["group"]["inter_tol_mm"]), (0.508, 0.5))
        self.assertNotIn("missing", res["preset"])

    def test_ambiguous_preset(self):
        with self.assertRaises(Exception):
            S.set_length_group("x", ["TP0_P"], preset="MDI")             # 1000BASE-T and 100BASE-TX both match


if __name__ == "__main__":
    unittest.main()
