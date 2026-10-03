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
    "score_placement", "suggest_placement_moves", "import_placement_from_kicad", "import_netlist_from_kicad", "undo", "save_design",
    "search_library", "get_library_part", "insert_library_part", "close_library", "update_from_libraries", "attach_3d_model",
    "request_design_review", "get_assembly_quote",
}


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
