import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from fusion_mcp import design_store
from fusion_mcp import server as S
from fusion_mcp.session import Session
from fusion_offline import length_groups as LG

from test_move_parts import FakeFusion


def mdi(tp1_extra=0.0, n31_end=10.3, airwire=False):
    """Two pairs like the PoE board's MDI: T1 pin -> 0402 series R -> J2 pin on each side.
    TP0_P: 4.5 + R18 (1.0) + 4.5 = 10.0 mm; TP0_N: 4.5 + 1.0 + (n31_end - 5.5).
    TP1 is the same shape 3 mm higher, with tp1_extra mm more on both of its J2-side nets."""
    els, sigs = [], []

    def side(net_t, net_j, y, r, j_end):
        els.extend([f'<element name="T{r}" library="L" package="PIN" value="" x="0" y="{y}"/>',
                    f'<element name="{r}" library="L" package="R2P" value="0" x="5" y="{y}"/>',
                    f'<element name="J{r}" library="L" package="PIN" value="" x="{j_end}" y="{y}"/>'])
        sigs.append(f'<signal name="{net_t}"><contactref element="T{r}" pad="1"/><contactref element="{r}" pad="1"/>'
                    f'<wire x1="0" y1="{y}" x2="4.5" y2="{y}" width="0.12" layer="1"/></signal>')
        lay = "19" if airwire and net_j == "N$31" else "1"
        sigs.append(f'<signal name="{net_j}"><contactref element="{r}" pad="2"/><contactref element="J{r}" pad="1"/>'
                    f'<wire x1="5.5" y1="{y}" x2="{j_end}" y2="{y}" width="0.12" layer="{lay}"/></signal>')
    side("TP0_P", "N$30", 1, "R18", 10)
    side("TP0_N", "N$31", 0, "R19", n31_end)
    side("TP1_P", "N$32", 4, "R20", 10 + tp1_extra)
    side("TP1_N", "N$33", 3, "R21", 10 + tp1_extra)
    return ET.fromstring(f"""<eagle><drawing><board><plain/><libraries><library name="L"><packages>
<package name="PIN"><smd name="1" x="0" y="0" dx="0.3" dy="0.3" layer="1"/></package>
<package name="R2P"><smd name="1" x="-0.5" y="0" dx="0.5" dy="0.5" layer="1"/><smd name="2" x="0.5" y="0" dx="0.5" dy="0.5" layer="1"/></package>
</packages></library></libraries><designrules name="t"/><classes/><elements>{''.join(els)}</elements>
<signals>{''.join(sigs)}</signals></board></drawing></eagle>""")


GROUP = {"name": "MDI", "members": [["TP0_P", "TP0_N"], ["TP1_P", "TP1_N"]], "intra_tol_mm": 0.1, "inter_tol_mm": 0.5}


class PathTest(unittest.TestCase):
    def test_through_the_series_resistor(self):
        p = LG.path(mdi(), "TP0_P")
        self.assertEqual((p["nets"], p["parts"]), (["TP0_P", "N$30"], ["R18"]))
        self.assertEqual((p["copper_mm"], p["parts_mm"], p["length_mm"], p["unrouted"]), (9.0, 1.0, 10.0, False))
        self.assertEqual(set(LG.path(mdi(), "N$30")["nets"]), {"TP0_P", "N$30"})  # the same path from either end
        self.assertEqual(LG.path(mdi(), "TP0_P", follow=False)["length_mm"], 4.5)

    def test_unknown_net(self):
        with self.assertRaises(ValueError):
            LG.path(mdi(), "NOPE")


class EvaluateTest(unittest.TestCase):
    def test_all_within_tolerance(self):
        res = LG.evaluate(mdi(n31_end=10.05, tp1_extra=0.3), GROUP)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["target_mm"], 10.3)

    def test_intra_pair_skew(self):
        res = LG.evaluate(mdi(), GROUP)                  # TP0_N is 0.3 longer than TP0_P
        tp0 = res["members"][0]
        self.assertEqual((tp0["skew_mm"], tp0["intra_ok"]), (-0.3, False))
        self.assertEqual(tp0["add_mm"], {"TP0_P": 0.3})
        self.assertEqual(res["failing"], ["TP0_P/TP0_N"])

    def test_between_pairs(self):
        res = LG.evaluate(mdi(n31_end=10, tp1_extra=0.8), GROUP)   # TP1 is 0.8 longer
        tp0 = res["members"][0]
        self.assertEqual((tp0["delta_mm"], tp0["inter_ok"]), (-0.8, False))
        self.assertEqual(tp0["add_mm"], {"TP0_P": 0.8, "TP0_N": 0.8})
        self.assertEqual(res["spread_mm"], 0.8)
        fixed = LG.evaluate(mdi(n31_end=10, tp1_extra=0.8), {**GROUP, "target": 11.0})
        self.assertEqual(fixed["target_mm"], 11.0)

    def test_measure_max_and_single_nets(self):
        res = LG.evaluate(mdi(), {**GROUP, "measure": "max"})
        self.assertEqual(res["members"][0]["length_mm"], 10.3)
        res = LG.evaluate(mdi(), {"name": "x", "members": ["TP0_P", "TP1_P"], "inter_tol_mm": 0.1})
        self.assertTrue(res["ok"])

    def test_unrouted_is_never_ok(self):
        res = LG.evaluate(mdi(airwire=True), GROUP)
        self.assertFalse(res["ok"])
        self.assertEqual(res["unrouted"], ["TP0_P/TP0_N"])
        # N$31 (4.8 mm) is an air wire: TP0_N's path is 4.5 of 9.3 mm routed; the pair counts its less routed side
        self.assertEqual(res["partial"], {"TP0_P/TP0_N": round(4.5 / 9.3, 3)})
        self.assertIn("partly routed", res["note"])
        self.assertIsNone(LG.evaluate(mdi(), GROUP)["note"])


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.patches = [mock.patch.object(design_store, "path", lambda d: os.path.join(self.dir, d + ".json")),
                        mock.patch.object(S, "session", Session(FakeFusion(mdi())))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_save_check_list_delete(self):
        res = S.set_length_group("MDI", [["TP0_P", "TP0_N"], ["TP1_P", "TP1_N"]], 0.1, 0.5)
        self.assertEqual(res["design"], "PoE Magnetics Test Board")
        self.assertTrue(os.path.exists(res["saved_to"]))
        self.assertEqual(list(S.list_length_groups()["groups"]), ["MDI"])
        chk = S.check_length_groups()
        self.assertFalse(chk["ok"])
        self.assertEqual(chk["groups"][0]["failing"], ["TP0_P/TP0_N"])
        self.assertEqual(S.delete_length_group("MDI")["left"], [])
        self.assertIn("no length groups saved", S.check_length_groups()["note"])

    def test_refusals(self):
        with self.assertRaises(Exception):
            S.set_length_group("x", [["TP0_P", "NOPE"]])
        with self.assertRaises(Exception):
            S.set_length_group("x", [["TP0_P"]])
        with self.assertRaises(Exception):
            S.delete_length_group("missing")


if __name__ == "__main__":
    unittest.main()
