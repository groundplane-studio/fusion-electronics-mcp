"""Only one copy of the add-in runs in a Fusion process: a second copy refuses to start and does
not delete the running copy's bridge.json when it is stopped."""
import os
import tempfile
import types
import unittest

from test_server import load_addin


class SingleCopyTest(unittest.TestCase):
    def setUp(self):
        self.a = load_addin()
        self.msgs = []
        ui = types.SimpleNamespace(messageBox=lambda *args: self.msgs.append(args[0]))
        app = types.SimpleNamespace(userInterface=ui)
        self.a.adsk.core.Application = types.SimpleNamespace(get=lambda: app)
        self.tmp = tempfile.mkdtemp()
        self.a.INFO_DIR = self.tmp
        self.a.INFO_PATH = os.path.join(self.tmp, "bridge.json")

    def test_second_copy_refuses_to_start(self):
        setattr(self.a.adsk.core, self.a._OWNER_ATTR, os.path.join("C:", "elsewhere", "FusionElectronicsMCP.py"))
        with open(self.a.INFO_PATH, "w") as f:
            f.write('{"token": "theirs"}')
        self.a.run(None)
        self.assertIn("already running", self.msgs[0])
        self.assertIsNone(self.a._server)
        self.a.stop(None)                                   # stopping this copy leaves theirs alone
        self.assertTrue(os.path.exists(self.a.INFO_PATH))
        self.assertTrue(getattr(self.a.adsk.core, self.a._OWNER_ATTR))

    def test_no_other_copy(self):
        self.assertIsNone(self.a._other_copy())


if __name__ == "__main__":
    unittest.main()
