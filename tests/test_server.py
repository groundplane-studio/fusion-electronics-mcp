import importlib.util
import json
import os
import sys
import types
import unittest

from fusion_mcp import commands as C
from fusion_mcp.library import Library, build_script
from fusion_mcp.session import Session, WriteFailed
from fusion_offline import design as D

ROOT = os.path.dirname(os.path.dirname(__file__))
FIX = os.path.join(ROOT, "tests", "fixtures")


SAMPLE_LIB = os.path.join(ROOT, "library", "parts")   # the sample parts shipped in the repo


def load_addin():
    """Import the add-in with stub adsk modules (only its pure helpers are tested)."""
    adsk = types.ModuleType("adsk")
    core = types.ModuleType("adsk.core")
    core.CustomEventHandler = object
    adsk.core = core
    adsk.electron = types.ModuleType("adsk.electron")
    sys.modules.update({"adsk": adsk, "adsk.core": core, "adsk.electron": adsk.electron})
    path = os.path.join(ROOT, "server", "fusion_mcp", "addin", "FusionElectronicsMCP", "FusionElectronicsMCP.py")
    spec = importlib.util.spec_from_file_location("fem_addin", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CommandsTest(unittest.TestCase):
    def test_names_are_quoted_and_grid_first(self):
        self.assertEqual(C.rotate_part("R1", 90, False), "GRID MM; ROTATE =R90 'R1';")
        self.assertEqual(C.rotate_part("R2", 270, True), "GRID MM; ROTATE =MR270 'R2';")
        self.assertTrue(C.move_part("R1", 1.23456, 2).startswith("GRID MM; MOVE 'R1' (1.2346 2)"))

    def test_rejects_injection(self):
        with self.assertRaises(C.InvalidInput):
            C.move_part("R1'; RUN evil", 0, 0)
        with self.assertRaises(C.InvalidInput):
            C.q("")

    def test_value_guard(self):
        sch = D.parse_schematic_design(D.read_xml(os.path.join(FIX, "mini.sch")))
        with self.assertRaises(C.InvalidInput) as cm:
            C.set_value(sch, "R1", "4.7k")
        self.assertIn("set_part_variant", str(cm.exception))
        self.assertEqual(C.set_value(sch, "U1", "3.3V"), "VALUE 'U1' '3.3V';")

    def test_sheet_prefix(self):
        self.assertIn("EDIT .s2;", C.net_stub("N", 0, 0, (1, 0), sheet=2))


class AddinAllowlistTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addin = load_addin()

    def test_split_respects_quotes(self):
        self.assertEqual(self.addin.split_commands("GRID MM; MOVE 'A;B' (1 1);"), ["GRID MM", "MOVE 'A;B' (1 1)"])

    def test_allowed(self):
        self.addin.validate("GRID MM; MOVE 'R1' (1 1); EDIT 'X.pac'; EDIT .s2; DRC;")

    def test_blocked(self):
        for bad in ("RUN 'x.ulp';", "SCRIPT 'x.scr';", "WRITE;", "EXPORT SCRIPT 'x';", "EDIT 'other.brd';",
                    "CAM;", "OPEN 'x.lbr';"):
            with self.assertRaises(self.addin.BridgeError, msg=bad):
                self.addin.validate(bad)


class LibraryTest(unittest.TestCase):
    def test_parts_have_required_fields(self):
        lib = Library(SAMPLE_LIB)
        self.assertGreaterEqual(len(lib.parts), 3)
        with open(os.path.join(ROOT, "library", "schema.json"), encoding="utf-8") as f:
            schema = json.load(f)
        for p in lib.parts.values():
            for k in schema["required"]:
                self.assertIn(k, p.data, f"{p.id} lacks {k}")
            self.assertFalse(p.data["metadata"]["verified"])     # placeholders only

    def test_search(self):
        hits = Library(SAMPLE_LIB).search("0402 resistor")
        self.assertEqual([h["id"] for h in hits], ["placeholder-res-0402-10k"])

    def test_build_script_passes_allowlist(self):
        addin = load_addin()
        for p in Library(SAMPLE_LIB).parts.values():
            s = build_script(p)
            addin.validate(s)
            self.assertIn(f"EDIT '{p.data['deviceset']}.dev';", s)
            self.assertNotIn("ATTRIBUTE SET", s)
        s = build_script(Library(SAMPLE_LIB).get("placeholder-res-0402-10k"))
        self.assertIn("LAYER 114 JLC_FOOTPRINT;", s)
        self.assertIn("ATTRIBUTE JLCPCB 'C25744';", s)


class FakeBridge:
    """Records commands. Exports return the fixture board; when `mutate` is
    set, any non-UNDO run makes later exports differ (a real change)."""
    def __init__(self, mutate=False, dialogs=None):
        self.ran = []
        self.mutate = mutate
        self.dialogs = dialogs or []
        self.dirty = False
        with open(os.path.join(FIX, "mini.brd"), "rb") as f:
            self.xml = f.read()

    def call(self, op, args=None, timeout=60, answers=None, forms=None):
        if op == "activate":
            return {}
        if op == "run":
            self.ran.append(args["commands"])
            if args["commands"] == "UNDO;":
                self.dirty = False
            elif self.mutate:
                self.dirty = True
            return {"raw": "", "dialogs": list(self.dialogs)}
        if op == "export":
            path = os.path.join(FIX, f"_tmp_{len(self.ran)}.brd")
            with open(path, "wb") as f:
                f.write(self.xml + (b"<!-- changed -->" if self.dirty else b""))
            return {"path": path}
        raise AssertionError(op)


class SessionTest(unittest.TestCase):
    def test_failed_change_is_undone(self):
        fb = FakeBridge(mutate=True)
        with self.assertRaises(WriteFailed) as cm:
            Session(fb).verified_write("board", C.move_part("R1", 1, 1),
                                       lambda a, b: (False, "R1 at (5, 5)"), schematic=False)
        self.assertEqual(fb.ran, ["GRID MM 0.0001; MOVE 'R1' (1 1); GRID LAST;", "UNDO;"])
        self.assertIn("The change was undone", str(cm.exception))
        self.assertNotIn("warning", str(cm.exception))

    def test_no_change_means_no_undo(self):
        # An UNDO after a no-op would revert the PREVIOUS tool call.
        fb = FakeBridge(mutate=False)
        with self.assertRaises(WriteFailed) as cm:
            Session(fb).verified_write("board", C.move_part("R1", 1, 1),
                                       lambda a, b: (False, "R1 at (5, 5)"), schematic=False)
        self.assertEqual(fb.ran, ["GRID MM 0.0001; MOVE 'R1' (1 1); GRID LAST;"])
        self.assertIn("Nothing was changed", str(cm.exception))

    def test_unexpected_dialog_fails_and_is_reported(self):
        d = {"text": "Merge net segment 'A' into given net 'B'?", "buttons": ["Yes", "No"],
             "answer": "No", "expected": False, "kind": "message_box", "title": "Fusion"}
        fb = FakeBridge(mutate=True, dialogs=[d])
        with self.assertRaises(WriteFailed) as cm:
            Session(fb).verified_write("board", C.move_part("R1", 1, 1),
                                       lambda a, b: (True, "looked fine"), schematic=False)
        self.assertIn("Merge net segment", str(cm.exception))
        self.assertIn("answered No", str(cm.exception))
        self.assertEqual(fb.ran[-1], "UNDO;")

    def test_expected_dialog_passes(self):
        d = {"text": "Merge net segment 'A' into given net 'B'?", "buttons": ["Yes", "No"],
             "answer": "Yes", "expected": True, "kind": "message_box", "title": "Fusion"}
        after, detail = Session(FakeBridge(mutate=True, dialogs=[d])).verified_write(
            "board", C.move_part("R1", 1, 1), lambda a, b: (True, "merged"), schematic=False,
            answers=[("^Merge net segment", "Yes")])
        self.assertIn("answered Yes", detail)


EXPECTED_TOOLS = {
    "get_context", "list_designs", "open_design", "open_library", "new_design", "close_design",
    "get_board_summary", "list_parts", "get_part", "list_nets", "get_net", "get_layer_stack", "get_design_rules",
    "run_drc", "run_erc", "review_schematic", "list_diff_pairs", "check_length_match", "check_impedance",
    "estimate_impedance", "export_bom", "export_cpl", "check_gerbers", "check_jlc_orientation", "add_part", "connect_pins", "rename_net", "label_nets",
    "new_sheet", "set_part_variant", "set_part_value", "move_part", "rotate_part", "add_via", "add_trace", "route_pair",
    "set_board_outline", "add_hole", "add_text", "rip_up", "add_pour", "list_pours", "set_pour_thermals", "add_keepout",
    "stitch_vias", "fanout_pad", "clean_vias", "remove_stubs", "autoroute", "routing_status", "render_board",
    "score_placement", "suggest_placement_moves", "import_placement_from_kicad", "import_routing_from_kicad", "route_trace", "route_net", "ground_vias", "route_close", "place_clusters", "lay_bus", "route_remaining", "import_netlist_from_kicad", "undo", "save_design",
    "search_library", "get_library_part", "insert_library_part", "close_library", "update_from_libraries", "attach_3d_model", "push_3d", "check_3d_models", "list_design_rules", "create_library_part",
    "request_design_review", "get_assembly_quote",
}


class ThreeDCheckTest(unittest.TestCase):
    """3D model guards: a part on the wrong side is flagged; a replacement model gets a new name."""

    def test_wrong_side_is_flagged(self):
        from unittest import mock
        from fusion_mcp import server as S
        res = {"board_z_mm": [0.0, 1.6], "parts": [
            {"occurrence": "MINIFIT:J3", "z_mm": [-11.2, 5.1]},      # hangs below: wrong
            {"occurrence": "MINIFIT:J4", "z_mm": [-3.5, 14.4]},      # pins through, body above: right
            {"occurrence": "SW:SW1", "z_mm": [-3.0, 1.6]}]}          # mirrored part below: right
        board = b"""<eagle><drawing><board><elements><element name="SW1" rot="MR180"/></elements></board></drawing></eagle>"""
        with mock.patch.object(S, "_snap", return_value=mock.Mock(board_xml=board)):
            out = S._side_check(res)
        self.assertEqual([w["part"] for w in out["wrong_side"]], ["MINIFIT:J3"])

    def test_replacement_model_gets_a_new_name(self):
        from unittest import mock
        from fusion_mcp import server as S
        devs = {"devices": [{"device": "D", "package": "PKG", "packages3d": ["PKG", "PKG_V2"]}]}
        with mock.patch.object(S.session.bridge, "call", return_value=devs):
            self.assertEqual(S._next_3d_name("PKG"), "PKG_V3")
        with mock.patch.object(S.session.bridge, "call", return_value={"devices": []}):
            self.assertIsNone(S._next_3d_name("PKG"))


class InferStyleTest(unittest.TestCase):
    """Two-pin passives from any source get the library's standard symbol."""

    def test_rules(self):
        from fusion_mcp import std_symbols as SS
        two = lambda a, b: {"pins": [{"name": a, "pad": "1"}, {"name": b, "pad": "2"}]}
        cases = [({"prefix": "R", "symbol": two("1", "2")}, "res"),
                 ({"prefix": "C", "symbol": two("1", "2")}, "cap"),
                 ({"prefix": "C", "description": "Aluminium electrolytic 100uF", "symbol": two("+", "-")}, "cap_pol"),
                 ({"prefix": "D", "description": "Schottky 40 V", "symbol": two("K", "A")}, "schottky"),
                 ({"prefix": "D", "description": "LED 0603 red", "symbol": two("K", "A")}, "led"),
                 ({"prefix": "D", "description": "diode", "symbol": two("1", "2")}, None),     # anode unknown
                 ({"prefix": "FB", "symbol": two("1", "2")}, "ferrite"),
                 ({"prefix": "U", "symbol": two("1", "2")}, None),
                 ({"prefix": "R", "symbol": {"pins": [{"name": n, "pad": n} for n in "123"]}}, None)]
        for part, want in cases:
            self.assertEqual(SS.infer_style(part), want, part)


class TransportTest(unittest.TestCase):
    """auto uses the add-in when it is running; the built-in server refuses what it cannot do."""

    def test_auto_prefers_a_running_addin(self):
        import json, os, socket, tempfile
        from unittest import mock
        from fusion_mcp import bridge as B
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        with tempfile.TemporaryDirectory() as d:
            info = os.path.join(d, "bridge.json")
            json.dump({"port": srv.getsockname()[1], "token": "t", "pid": 1, "protocol": 1}, open(info, "w"))
            with mock.patch.dict(os.environ, {"FUSION_MCP_TRANSPORT": "auto"}),                     mock.patch.object(B._BUILTIN, "available", return_value=True):
                self.assertEqual(B.Bridge(info_path=info).transport(), "addin")
                srv.close()
                self.assertEqual(B.Bridge(info_path=info).transport(), "builtin")

    def test_builtin_refuses_saves(self):
        import os
        from unittest import mock
        from fusion_mcp import bridge as B
        with mock.patch.dict(os.environ, {"FUSION_MCP_TRANSPORT": "builtin"}):
            with self.assertRaises(B.BridgeOpError) as cm:
                B.Bridge(watch_dialogs=False, keep_focus=False).call("save", {})
        self.assertEqual(cm.exception.code, "needs_addin")


class RuleLibraryTest(unittest.TestCase):
    def test_bundled_rule_sets(self):
        from fusion_mcp import rules_lib
        sets = {r["name"]: r for r in rules_lib.catalog()}
        four = next(r for n, r in sets.items() if n.startswith("JLC04161H-3313A"))
        self.assertAlmostEqual(four["board_thickness_mm"], 1.578, places=3)
        self.assertEqual(four["rules"]["copper_to_edge"], "0.3mm")
        self.assertTrue(all(r["stackup_file"] for r in sets.values()))
        self.assertTrue(any(r["copper_layers"] == 2 for r in sets.values()))
        self.assertEqual([d["er"] for d in four["dielectrics"]], [4.1, 4.6, 4.1])     # JLC's 3313 and core
        six = [r for r in sets.values() if r["copper_layers"] == 6]
        self.assertGreaterEqual(len(six), 14)
        self.assertTrue(all(1.3 < r["board_thickness_mm"] < 1.8 for r in six))


class EasyedaSpacingTest(unittest.TestCase):
    def test_requests_are_spaced_across_processes(self):
        import os, tempfile, time
        from unittest import mock
        from fusion_mcp import easyeda as E

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"{}"
        with tempfile.TemporaryDirectory() as d,                 mock.patch.dict(os.environ, {"FUSION_MCP_EASYEDA_CACHE": d}),                 mock.patch.object(E, "MIN_INTERVAL_S", 0.4), mock.patch.object(E.random, "uniform", return_value=0.0),                 mock.patch.object(E.urllib.request, "urlopen", return_value=Resp()):
            E.polite_get("https://easyeda.com/x")
            E._last = 0.0                      # as if another process made that request
            t = time.time()
            E.polite_get("https://easyeda.com/y")
            self.assertGreaterEqual(time.time() - t, 0.35)   # waited on the shared clock file


class CreatePartTest(unittest.TestCase):
    def test_kicad_footprint_to_library_part(self):
        import json, os, tempfile
        from unittest import mock
        from fusion_mcp import server as S, library as L
        fp = """(footprint "R_0603" (layer "F.Cu")
  (pad "1" smd roundrect (at -0.825 0) (size 0.8 0.95) (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "2" smd roundrect (at 0.825 0) (size 0.8 0.95) (layers "F.Cu" "F.Paste" "F.Mask")))"""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "R_0603.kicad_mod")
            open(path, "w").write(fp)
            with mock.patch.object(S, "library", L.Library(os.path.join(d, "parts"))):
                r = S.create_library_part(path, "res-0603-10k", "RES_0603", "R", "10k", jlc_code="c25804",
                                          manufacturer="UNI-ROYAL", mpn="0603WAF1002T5E")
                self.assertEqual((r["pads"], r["symbol_style"]), (2, "res"))
                saved = json.load(open(r["file"]))
                self.assertEqual(saved["attributes"]["JLCPCB"], "C25804")
                with self.assertRaises(FileExistsError):
                    S.create_library_part(path, "res-0603-10k", "RES_0603", "R", "10k")


class ToolListTest(unittest.TestCase):
    def test_no_tool_lost(self):
        # guards against edits that cut a block of tools (it happened once)
        import asyncio
        from fusion_mcp.server import mcp
        names = {t.name for t in asyncio.run(mcp.list_tools())}
        self.assertEqual(sorted(EXPECTED_TOOLS - names), [])


class GridTest(unittest.TestCase):
    def test_fine_grid_and_restore(self):
        g = Session.with_grid("GRID MM; MOVE 'R1' (1 2); GRID MM; LABEL (1 1) (2 2);")
        self.assertEqual(g, "GRID MM 0.0001; MOVE 'R1' (1 2); LABEL (1 1) (2 2); GRID LAST;")
        self.assertEqual(Session.with_grid("UNDO;"), "UNDO;")


class DialogPolicyTest(unittest.TestCase):
    def test_classify(self):
        from fusion_mcp import dialogs as dg
        box = {"texts": ["Merge?"], "buttons": ["Yes", "No"], "radios": [], "checks": [], "edits": [], "ids": [], "others": 0}
        form = dict(box, buttons=["OK", "Cancel"], radios=[{"name": "this Segment"}], edits=[{"name": "New name:"}])
        toast = dict(box, buttons=[], ids=["QTApplication.Nu::QTNotificationMessage.QWidget"])
        W = dg.Window
        self.assertEqual(dg.classify(W(1, "Fusion", "Qt683QWindowIcon"), box), "message_box")
        self.assertEqual(dg.classify(W(1, "Name", "Qt683QWindowIcon"), form), "form")
        self.assertEqual(dg.classify(W(1, "Fusion360", "Qt683QWindowToolSaveBits"), toast), "toast")

    def test_never_presses_dangerous_buttons_by_default(self):
        from fusion_mcp import dialogs
        self.assertNotIn("Yes", [b for b in dialogs.SAFE_ORDER if b not in dialogs.NEVER])
        self.assertEqual(dialogs.SAFE_ORDER[0], "Cancel")


if __name__ == "__main__":
    unittest.main()


class AutorouteCtlTest(unittest.TestCase):
    def test_top_router_switched_off(self):
        from fusion_mcp import autoroute as AR
        self.assertIn("TopRouterVariant  = 0", AR.ctl_without_top_router("[Default]\n  TopRouterVariant  = 1\n"))
        self.assertIn("TopRouterVariant  = 0", AR.ctl_without_top_router("[Default]\n  Efforts = 2\n"))


class BuiltinTransportTest(unittest.TestCase):
    def test_scripts_compile_and_read_only_ops_never_cancel_commands(self):
        from fusion_mcp import builtin as B
        w = B.build_script("run", {"commands": "GRID MM;", "editor": "board"}, read_only=False)
        r = B.build_script("export", {"kind": "board"}, read_only=True)
        compile(w, "write", "exec")
        compile(r, "read", "exec")
        self.assertIn("def run(_context: str):", w)
        self.assertIn("terminateActiveCommand", w)
        self.assertNotIn("terminateActiveCommand()   #", r)      # a read must not cancel the person's command
        self.assertIn("export", B.READ_ONLY_OPS)
        self.assertNotIn("run", B.READ_ONLY_OPS)


class BuiltinLongArgsTest(unittest.TestCase):
    def test_long_arguments_are_split_over_short_lines(self):
        import json as _json
        from fusion_mcp import builtin as B
        args = {"script": "PAD 1.5 ROUND R0 'P' (0 0);\n" * 400, "editor": "library"}
        scr = B.build_script("run_script", args, read_only=False)
        compile(scr, "s", "exec")
        tail = scr[scr.index("_ARGS ="):]
        self.assertLess(max(len(l) for l in tail.splitlines()), 1100)
        ns = {}
        exec(tail[:tail.index("\n\n\ndef run")], {"json": _json}, ns)
        self.assertEqual(_json.loads(ns["_ARGS"]), args)


class SplitSettingsTest(unittest.TestCase):
    def test_settings_run_outside_the_grouped_write(self):
        from fusion_mcp.session import split_settings
        pre, body, post = split_settings("GRID MM; SET WIRE_BEND 2; WIRE 'A' 0.25 (0 0) (1 1); SET WIRE_BEND 1; RATSNEST;")
        self.assertEqual(pre, "SET WIRE_BEND 2;")
        self.assertEqual(post, "SET WIRE_BEND 1;")
        self.assertNotIn("SET", body)


def _fusion_writer_class():
    """A fresh copy of the stdout wrapper Autodesk's MCP script runner installs
    before every script (same shape: write/flush, and __getattr__ forwarding to
    self._original). Each script run defines a new class object."""
    class _NsSanitizedWriter:
        def __init__(self, original):
            self._original = original

        def write(self, s):
            return self._original.write(s)

        def flush(self):
            return self._original.flush()

        def __getattr__(self, name):
            return getattr(self._original, name)
    return _NsSanitizedWriter


class FusionStdoutRecursionTest(unittest.TestCase):
    def _repair(self):
        from fusion_mcp import builtin as B
        ns: dict = {}
        exec(B.STREAM_REPAIR.split("import sys as _femcp_sys")[0], ns)
        return ns["_femcp_repair_streams"]

    def _leaked(self, runs):
        import io
        real = io.StringIO()
        out = real
        for _ in range(runs):                      # one wrapper per run that was never unwrapped
            out = _fusion_writer_class()(out)
        return real, types.SimpleNamespace(stdout=out, stderr=out, __stdout__=real, __stderr__=real)

    def test_leaked_wrappers_recurse_until_repaired(self):
        real, fake_sys = self._leaked(5000)
        with self.assertRaises(RecursionError):    # what Fusion reported: line 25, in __getattr__
            fake_sys.stdout.encoding
        self._repair()(fake_sys)
        self.assertIs(vars(fake_sys.stdout)["_original"], real)
        fake_sys.stdout.write("@@FEMCP@@{}")
        self.assertEqual(fake_sys.stdout.getvalue(), "@@FEMCP@@{}")   # forwarded, no recursion
        self.assertEqual(fake_sys.stdout.encoding, real.encoding)

    def test_wrapper_without_original_raises_or_is_repaired(self):
        cls = _fusion_writer_class()
        orphan = object.__new__(cls)               # e.g. copied/unpickled: __init__ never ran
        with self.assertRaises(RecursionError):    # __getattr__('_original') calls itself forever
            orphan.encoding
        import io
        real = io.StringIO()
        fake_sys = types.SimpleNamespace(stdout=orphan, stderr=io.StringIO(), __stdout__=real, __stderr__=None)
        self._repair()(fake_sys)
        orphan.write("ok")
        self.assertEqual(real.getvalue(), "ok")

    def test_every_script_repairs_streams_before_anything_else(self):
        from fusion_mcp import builtin as B
        for op, ro in (("export", True), ("run", False)):
            s = B.build_script(op, {}, read_only=ro)
            self.assertTrue(s.startswith(B.STREAM_REPAIR))
            compile(s, op, "exec")


class BuiltinClientRecoveryTest(unittest.TestCase):
    def _client(self, post):
        from fusion_mcp import builtin as B
        c = B.BuiltinClient("http://127.0.0.1:1/mcp")
        c._initialize = lambda timeout=5.0: setattr(c, "session", "s%d" % c._id)
        c._post = post
        return c

    @staticmethod
    def _ok(payload):
        text = json.dumps({"success": True, "message": "@@FEMCP@@" + json.dumps(payload)})
        return None, {"result": {"content": [{"text": text}]}}

    def test_calls_never_overlap(self):
        import threading
        import time
        live, peak = [0], [0]

        def post(body, timeout):
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            time.sleep(0.05)
            live[0] -= 1
            return self._ok({"ok": True, "result": 1})
        c = self._client(post)
        ts = [threading.Thread(target=c.call, args=("context", {}, 5)) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(peak[0], 1)

    def test_lost_session_is_reinitialised_and_retried(self):
        calls = []

        def post(body, timeout):
            calls.append(body)
            if len(calls) == 1:
                return None, {"jsonrpc": "2.0", "error": {"code": -32600,
                              "message": "Session not initialized. Call 'initialize' first."}}
            return self._ok({"ok": True, "result": "fine"})
        c = self._client(post)
        self.assertEqual(c.call("context", {}, 5), {"ok": True, "result": "fine"})
        self.assertEqual(len(calls), 2)

    def test_failure_resets_session_for_next_call(self):
        from fusion_mcp import builtin as B
        state = {"fail": True}

        def post(body, timeout):
            if state["fail"]:
                raise TimeoutError("timed out")
            return self._ok({"ok": True, "result": 2})
        c = self._client(post)
        with self.assertRaises(OSError):
            c.call("context", {}, 5)
        self.assertIsNone(c.session)
        state["fail"] = False
        self.assertEqual(c.call("context", {}, 5)["result"], 2)

    def test_recursion_error_is_reported_cleanly(self):
        from fusion_mcp import builtin as B
        err = ('Traceback (most recent call last):\n  File "<string>", line 25, in __getattr__\n'
               'RecursionError: Stack overflow (used 993 kB)')
        c = self._client(lambda body, timeout: (None, {"result": {"content": [
            {"text": json.dumps({"success": False, "error": err})}]}}))
        with self.assertRaises(B.BuiltinError) as cm:
            c.call("context", {}, 5)
        self.assertEqual(cm.exception.code, "fusion_stdout_corrupt")
