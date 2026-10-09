"""Design data files, rule-file plausibility clamps, lengths in rule files, CHANGE CLASS
commands, the edge-clearance message, the add-in staleness remedy, open_design folders."""

import os
import re
import tempfile
import types
import unittest
from unittest import mock

from fusion_mcp import design_store as DS
from fusion_mcp import server as S
from fusion_mcp import staleness as ST
from fusion_mcp.commands import InvalidInput
from fusion_mcp.session import Session
from fusion_offline import net_classes as NC
from fusion_offline import rules_edit as RE

from test_rules_edit import Bridge as RulesBridge, jlc_rules
from test_server import load_addin


class DesignStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.p = mock.patch.object(DS, "data_dir", lambda *parts: os.path.join(self.dir, *parts))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_similar_names_and_folders_get_their_own_files(self):
        names = {DS.path("Board v1"), DS.path("Board_v1"), DS.path("Board v1", folder="A"),
                 DS.path("Board v1", project="P")}
        self.assertEqual(len(names), 4)

    def test_reserved_windows_names_work(self):
        for name in ("NUL", "CON", "COM1", "aux"):
            DS.save(name, {"length_groups": {"g": 1}})
            self.assertEqual(DS.load(name), {"length_groups": {"g": 1}})
            self.assertTrue(os.path.basename(DS.path(name)).startswith("d_"))

    def test_update_is_atomic_and_locked(self):
        calls = []
        with mock.patch("fusion_mcp.fusion_lock.LOCK.hold", lambda what="": _Rec(calls)):
            data, where = DS.update("PoE", lambda d: d.setdefault("net_currents", {}).update(VBUS=1.0))
        self.assertEqual(calls, ["enter", "exit"])
        self.assertEqual(DS.load("PoE")["net_currents"], {"VBUS": 1.0})
        self.assertEqual([f for f in os.listdir(os.path.dirname(where)) if f.startswith(".tmp_")], [])

    def test_write_failure_is_a_clear_error(self):
        with mock.patch.object(DS.os, "replace", side_effect=OSError(183, "Cannot create a file")):
            with self.assertRaises(DS.StoreError) as cm:
                DS.save("PoE", {})
        self.assertIn("could not save the data for design 'PoE'", str(cm.exception))

    def test_legacy_file_is_still_read(self):
        os.makedirs(os.path.join(self.dir, "designs"))
        with open(os.path.join(self.dir, "designs", "Old_Board.json"), "w", encoding="utf-8") as f:
            f.write('{"length_groups": {"x": 1}}')
        self.assertEqual(DS.load("Old Board"), {"length_groups": {"x": 1}})


class _Rec:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        self.calls.append("enter")

    def __exit__(self, *a):
        self.calls.append("exit")
        return False


class ClampTest(unittest.TestCase):
    def body(self, text):
        return text[text.index("<designrules"):text.index("</designrules>") + 14]

    def test_same_signal_clearances_never_exceed_different_signal(self):
        clamped = []
        text, changes, _ = RE.edit(jlc_rules(), {0: "default"}, clearances_mm={"wire_pad": 0.08}, clamped=clamped)
        body = self.body(text)
        params = {m[0]: NC._mm(m[1]) for m in re.findall(r'<param name="(md\w+)" value="([^"]*)"', body)}
        floor = min(params[n] for n in RE.DIFFERENT_SIGNAL)
        self.assertEqual(floor, 0.08)
        for n in RE.SAME_SIGNAL:
            self.assertLessEqual(params[n], floor + 1e-9, n)
        for r in re.findall(r'<rule [^>]*samesignal="yes"[^>]*>', body):
            self.assertLessEqual(NC._mm(re.search(r' value="([^"]*)"', r).group(1)), floor + 1e-9, r)
        self.assertTrue(any("mdSmdSmd lowered from 0.1 mm to 0.08 mm" in c for c in clamped), clamped)
        self.assertEqual(changes, ["clearance wire_pad = 0.08 mm"])

    def test_tool_reports_the_clamps(self):
        out = tempfile.mkdtemp()
        with mock.patch.object(S, "session", Session(RulesBridge())):
            res = S.edit_design_rules(clearances_mm={"wire_pad": 0.08}, out_dir=out)
        self.assertTrue(res["clamped"])


class LengthTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(NC._mm(".2mm"), 0.2)
        self.assertEqual(NC._mm("6mil"), 0.1524)
        self.assertEqual(NC._mm("0.01in"), 0.254)
        self.assertEqual(NC._mm("150um"), 0.15)
        self.assertEqual(NC._mm("0.3"), 0.3)
        self.assertIsNone(NC._mm(""))
        self.assertIsNone(NC._mm(None))

    def test_unreadable_is_a_clear_error(self):
        for v in ("5furlong", "abc", "1.2.3mm"):
            with self.assertRaises(ValueError) as cm:
                NC._mm(v)
            self.assertIn("cannot read", str(cm.exception))


class ChangeClassTest(unittest.TestCase):
    def test_coordinates_keep_0_1_um(self):
        cmd = NC.change_class_commands("pwr", {"VBUS": (1, 235.5175, 12.25)})
        self.assertEqual(cmd, "EDIT .s1; CHANGE CLASS pwr (235.5175 12.25);")

    def test_names_quoted_or_refused(self):
        self.assertIn("CHANGE CLASS 'eth 100'", NC.change_class_commands("eth 100", {"A": (1, 1, 1)}))
        with self.assertRaises(InvalidInput):
            NC.change_class_commands("it's", {"A": (1, 1, 1)})


class EdgeMessageTest(unittest.TestCase):
    def test_matches_the_jlc_data(self):
        import json
        with open(os.path.join(os.path.dirname(S.__file__), "data", "jlc_capabilities.json"), encoding="utf-8") as f:
            edge = json.load(f)
        text = json.dumps(edge)
        self.assertIn('"copper_to_routed_edge": {"all": 0.2', text)

        class Default(RulesBridge):
            def call(self, op, args=None, timeout=60, **kw):
                res = super().call(op, args, timeout, **kw)
                if op == "design_rules":
                    res = dict(res, xml=res["xml"].replace('name="mdCopperDimension" value="',
                                                           'name="mdCopperDimension" value="40mil" x="'))
                return res
        with mock.patch.object(S, "session", Session(Default())):
            res = S.get_design_rules()
        msg = next(w for w in res["warnings"] if "edge clearance" in w)
        self.assertIn("0.2 mm copper to a routed edge", msg)
        self.assertNotIn("needs only 0.3", msg)


class AddinStalenessTest(unittest.TestCase):
    def setUp(self):
        ST._cache.update(at=-1e9, note=None)

    def tearDown(self):
        ST._cache.update(at=-1e9, note=None)

    def test_addin_change_says_how_to_reload_the_addin(self):
        addin = os.path.join(ST.ADDIN_DIR, "FusionElectronicsMCP.py")

        def newest(server_only=False):
            return (ST.STARTED, "x.py") if server_only else (ST.STARTED + 60, addin)
        with mock.patch.object(ST, "newest", newest):
            msg = ST.note(clock=lambda: 100.0)
        self.assertIn("Scripts and Add-Ins", msg)
        self.assertIn("install-addin --force", msg)
        self.assertNotIn("Connectors", msg)


class Doc:
    def __init__(self, name, kind, folder=None, active=False):
        self.name, self.kind, self.isModified, self.isActive = name, kind, False, active
        self.activated = 0
        if folder is not None:
            node = types.SimpleNamespace(isRoot=True, parentFolder=None, name="root")
            for part in [p for p in folder.split("/") if p]:
                node = types.SimpleNamespace(isRoot=False, parentFolder=node, name=part)
            self.dataFile = types.SimpleNamespace(parentFolder=node)

    def activate(self):
        self.activated += 1


class OpenDesignFolderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addin = load_addin()

    def setUp(self):
        a = self.addin
        self.saved = {k: getattr(a, k) for k in ("_docs", "_kind_of", "op_context", "_settle", "_project_designs", "_app")}
        self.opened = []
        a._kind_of = lambda d: d.kind
        a.op_context = lambda args: {}
        a._settle = lambda n=10: None
        a._project_designs = lambda folder=None, project=None: (
            types.SimpleNamespace(name="Proj"), [(folder or "", types.SimpleNamespace(name="PoE", fileExtension="fprj"))])
        a._app = types.SimpleNamespace(documents=types.SimpleNamespace(open=lambda f: self.opened.append(f.name)))

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(self.addin, k, v)

    def test_same_name_in_another_folder_is_opened_not_activated(self):
        other = Doc("PoE", "design", folder="Old")
        self.addin._docs = lambda: [other]
        res = self.addin.op_open_design({"name": "PoE", "folder": "Live tests"})
        self.assertNotIn("already_open", res)
        self.assertEqual((other.activated, self.opened), (0, ["PoE"]))

    def test_matching_folder_is_activated(self):
        mine = Doc("PoE", "design", folder="Live tests")
        self.addin._docs = lambda: [Doc("PoE", "design", folder="Old"), mine]
        res = self.addin.op_open_design({"name": "PoE", "folder": "/Live tests/"})
        self.assertTrue(res["already_open"])
        self.assertEqual(mine.activated, 1)

    def test_ambiguous_name_without_folder_is_refused(self):
        self.addin._docs = lambda: [Doc("PoE", "design", folder="Old"), Doc("PoE", "design", folder="New")]
        with self.assertRaises(self.addin.BridgeError) as cm:
            self.addin.op_open_design({"name": "PoE"})
        self.assertEqual(cm.exception.code, "ambiguous")
        self.assertIn("pass folder=", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
