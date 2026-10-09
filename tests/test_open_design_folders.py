"""open_design by name alone: the add-in remembers the folder each design was found in."""
import os
import tempfile
import types
import unittest

from test_fix4_store_rules import Doc, load_addin


class RememberFolderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addin = load_addin()

    def setUp(self):
        a = self.addin
        self.saved = {k: getattr(a, k) for k in ("_docs", "_kind_of", "op_context", "_settle", "_project_designs",
                                                 "_app", "FOLDERS_PATH", "INFO_DIR")}
        self.tmp = tempfile.mkdtemp()
        a.INFO_DIR = self.tmp
        a.FOLDERS_PATH = os.path.join(self.tmp, "design_folders.json")
        self.opened, self.listed = [], []
        a._kind_of = lambda d: d.kind
        a.op_context = lambda args: {}
        a._settle = lambda n=10: None
        a._docs = lambda: []
        self.where = {"Rock": "Boards/Rock"}

        def listing(folder=None, project=None):
            self.listed.append(folder)
            if folder is None:
                raise a.BridgeError("timeout", "listing project 'groundplane' took too long")
            files = [(folder, types.SimpleNamespace(name=n, fileExtension="fprj"))
                     for n, f in self.where.items() if f == folder]
            return types.SimpleNamespace(name="groundplane"), files
        a._project_designs = listing
        a._app = types.SimpleNamespace(data=types.SimpleNamespace(activeProject=types.SimpleNamespace(name="groundplane")),
                                       documents=types.SimpleNamespace(open=lambda f: self.opened.append(f.name)))

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(self.addin, k, v)

    def test_name_only_uses_the_folder_seen_while_open(self):
        doc = Doc("Rock", "design", folder="Boards/Rock")
        doc.dataFile.parentProject = types.SimpleNamespace(name="groundplane")
        self.addin._docs = lambda: [doc]
        self.assertTrue(self.addin.op_open_design({"name": "Rock"})["already_open"])
        self.addin._docs = lambda: []                    # closed again
        self.addin.op_open_design({"name": "Rock"})
        self.assertEqual(self.opened, ["Rock"])
        self.assertEqual(self.listed, ["Boards/Rock"])   # never listed the slow top folder

    def test_name_only_remembers_after_opening_by_folder(self):
        self.addin.op_open_design({"name": "Rock", "folder": "Boards/Rock"})
        self.addin.op_open_design({"name": "Rock"})
        self.assertEqual(self.opened, ["Rock", "Rock"])
        self.assertEqual(self.listed, ["Boards/Rock", "Boards/Rock"])

    def test_timeout_names_known_folders(self):
        self.addin.op_open_design({"name": "Rock", "folder": "Boards/Rock"})
        with self.assertRaises(self.addin.BridgeError) as cm:
            self.addin.op_open_design({"name": "Other"})
        self.assertEqual(cm.exception.code, "timeout")
        self.assertIn("'Boards/Rock'", str(cm.exception))



class OtherProjectTest(RememberFolderTest):
    def test_design_in_another_project_says_which(self):
        doc = Doc("Rock", "design", folder="Boards/Rock")
        doc.dataFile.parentProject = types.SimpleNamespace(name="Electronics MCP")
        self.addin._docs = lambda: [doc]
        self.addin.op_open_design({"name": "Rock"})
        self.addin._docs = lambda: []
        with self.assertRaises(self.addin.BridgeError) as cm:
            self.addin.op_open_design({"name": "Rock"})
        self.assertIn("'Electronics MCP'", str(cm.exception))
        self.assertEqual(self.listed, [])                # no slow listing at all


if __name__ == "__main__":
    unittest.main()
