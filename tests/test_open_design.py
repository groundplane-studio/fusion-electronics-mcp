import types
import unittest

from fusion_mcp.bridge import Bridge, BridgeOpError

from test_server import load_addin


class Doc:
    def __init__(self, name, kind, modified=False, active=False):
        self.name, self.kind, self.isModified, self.isActive = name, kind, modified, active
        self.activated = 0

    def activate(self):
        self.activated += 1


class OpenDesignTest(unittest.TestCase):
    """Fusion froze on 2026-10-07 during open_design while another design had unsaved changes."""

    @classmethod
    def setUpClass(cls):
        cls.addin = load_addin()

    def setUp(self):
        a = self.addin
        self.saved = {k: getattr(a, k) for k in ("_docs", "_kind_of", "op_context", "_settle", "_project_designs", "_app")}
        self.opened = []
        self.listed = 0

        def designs(folder=None, project=None):
            self.listed += 1
            return types.SimpleNamespace(name="Proj"), [("", types.SimpleNamespace(name="PoE", fileExtension="fprj"))]
        a._kind_of = lambda d: d.kind
        a.op_context = lambda args: {}
        a._settle = lambda n=10: None
        a._project_designs = designs
        a._app = types.SimpleNamespace(documents=types.SimpleNamespace(open=lambda f: self.opened.append(f.name)))

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(self.addin, k, v)

    def test_open_board_tab_counts_as_open(self):
        # only the board and schematic tabs are open, no design overview: switch, never re-open
        board = Doc("PoE", "board")
        self.addin._docs = lambda: [Doc("SoM", "board", modified=True, active=True), board, Doc("PoE", "schematic")]
        res = self.addin.op_open_design({"name": "PoE"})
        self.assertTrue(res["already_open"])
        self.assertEqual(board.activated, 1)
        self.assertEqual((self.opened, self.listed), ([], 0))      # no project walk, no documents.open

    def test_design_overview_preferred_and_active_left_alone(self):
        design = Doc("PoE", "design", active=True)
        self.addin._docs = lambda: [Doc("PoE", "board"), design]
        self.addin.op_open_design({"name": "PoE"})
        self.assertEqual(design.activated, 0)                      # already the active one

    def test_unsaved_design_blocks_opening_from_the_project(self):
        self.addin._docs = lambda: [Doc("SoM", "board", modified=True, active=True), Doc("SoM", "design")]
        with self.assertRaises(self.addin.BridgeError) as cm:
            self.addin.op_open_design({"name": "PoE"})
        self.assertEqual(cm.exception.code, "unsaved")
        self.assertIn("'SoM' has unsaved changes", str(cm.exception))
        self.assertEqual((self.opened, self.listed), ([], 0))
        self.addin.op_open_design({"name": "PoE", "allow_unsaved": True})
        self.assertEqual(self.opened, ["PoE"])

    def test_nothing_unsaved_opens_from_the_project(self):
        self.addin._docs = lambda: [Doc("SoM", "board", active=True)]
        self.addin.op_open_design({"name": "PoE"})
        self.assertEqual(self.opened, ["PoE"])


class TimeoutMessageTest(unittest.TestCase):
    def test_not_started_explains_fusion_is_busy(self):
        res = {"ok": False, "error": {"code": "timeout", "message": "timed out after 60s before Fusion started it (not run)"}}
        with self.assertRaises(BridgeOpError) as cm:
            Bridge._finish(types.SimpleNamespace(last_dialogs=[]), res, "context", False, 0)
        self.assertIn("Fusion never started this call", cm.exception.message)
        self.assertIn("press Esc", cm.exception.message)

    def test_other_errors_unchanged(self):
        res = {"ok": False, "error": {"code": "not_found", "message": "no design 'X'"}}
        with self.assertRaises(BridgeOpError) as cm:
            Bridge._finish(types.SimpleNamespace(last_dialogs=[]), res, "open_design", False, 0)
        self.assertEqual(cm.exception.message, "no design 'X'")



class AddinVersionTest(unittest.TestCase):
    def test_manifest_matches_the_code(self):
        # install-addin and doctor read the manifest; the running add-in reports ADDIN_VERSION.
        # 2026-10-07: the code said 0.14.0 and the manifest 0.13.0, so Fusion and install-addin showed 13.
        import os
        import re
        from fusion_mcp.cli import BUNDLED_ADDIN, manifest_version
        with open(os.path.join(BUNDLED_ADDIN, "FusionElectronicsMCP.py"), encoding="utf-8") as f:
            code = re.search(r'^ADDIN_VERSION = "([^"]+)"', f.read(), re.M).group(1)
        self.assertEqual(manifest_version(BUNDLED_ADDIN), code)


if __name__ == "__main__":
    unittest.main()
