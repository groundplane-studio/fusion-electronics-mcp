"""Audit fixes: rule files only in allowed folders, AUTO SAVE / LOAD only in the temp folder."""
import os
import sys
import tempfile
import types
import unittest

from fusion_mcp import server as S


class RuleFileFolderTest(unittest.TestCase):
    def test_refuses_folder_outside_the_allowed_ones(self):
        with self.assertRaises(ValueError):
            S._save_rule_file("x", "<eagle/>", os.path.join(os.path.abspath(os.sep), "Windows", "x"))

    def test_writes_to_the_rules_folder(self):
        p = S._save_rule_file("fix5 test", "<eagle/>", None)
        try:
            self.assertTrue(os.path.exists(p))
        finally:
            os.remove(p)


class AutoFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake = types.ModuleType("adsk")
        fake.core = types.ModuleType("adsk.core")
        sys.modules.setdefault("adsk", fake)
        sys.modules.setdefault("adsk.core", fake.core)
        sys.modules.setdefault("adsk.electron", types.ModuleType("adsk.electron"))
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, os.path.join(here, "server", "fusion_mcp", "addin", "FusionElectronicsMCP"))
        import FusionElectronicsMCP as A
        cls.A = A

    def test_temp_folder_allowed(self):
        p = os.path.join(tempfile.gettempdir(), "fusion-mcp-auto-x", "job.ctl").replace("\\", "/")
        self.assertTrue(self.A._auto_file_ok(f"AUTO LOAD '{p}';"))

    def test_other_paths_refused(self):
        self.assertFalse(self.A._auto_file_ok("AUTO SAVE 'C:/Users/someone/Desktop/x.ctl';"))
        self.assertFalse(self.A._auto_file_ok("AUTO LOAD;"))

    def test_plain_auto_allowed(self):
        self.assertTrue(self.A._auto_file_ok("AUTO 'GND' 'VCC';"))
        self.assertTrue(self.A._auto_file_ok("AUTO;"))


if __name__ == "__main__":
    unittest.main()
