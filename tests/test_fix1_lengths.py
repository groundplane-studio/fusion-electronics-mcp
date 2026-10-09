import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from fusion_mcp import design_store
from fusion_mcp import server as S
from fusion_mcp.session import Session
from fusion_offline import length_groups as LG
from fusion_offline import pairs as PR
from fusion_offline import stackup as SU
from fusion_offline.stitch import board_obstacles

from test_jlc_limits import pinned
from test_length_groups import GROUP, mdi
from test_move_parts import FakeFusion
from test_route_pair import board

STACKS = os.path.join(os.path.dirname(S.__file__), "data", "stackups")
CL = [[1, 0], [19, 0]]


def via_board(layer_setup=None):
    """VP: 10 mm on top, dropping to bottom between x 4 and 6 (two vias); VN: 10 mm on top, no via."""
    ls = f'<param name="layerSetup" value="{layer_setup}"/>' if layer_setup else ""
    w = lambda x1, x2, y, l: f'<wire x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" width="0.2" layer="{l}"/>'
    return ET.fromstring(f"""<eagle><drawing><board><plain/><libraries/><designrules name="t">{ls}</designrules>
<classes/><elements/><signals>
<signal name="VP">{w(0, 4, 1, 1)}<via x="4" y="1" extent="1-16" drill="0.3"/>{w(4, 6, 1, 16)}
<via x="6" y="1" extent="1-16" drill="0.3"/>{w(6, 10, 1, 1)}<via x="8" y="1" extent="1-16" drill="0.3"/></signal>
<signal name="VN">{w(0, 10, 0, 1)}</signal>
</signals></board></drawing></eagle>""")


class ViaBarrelTest(unittest.TestCase):
    def test_depths_without_a_stackup(self):
        d, src = LG.layer_depths(via_board())
        self.assertEqual(d, {1: 0.0, 16: 1.6})
        self.assertIn("assumed 1.6 mm", src)

    def test_depths_from_a_stackup(self):
        with open(os.path.join(STACKS, "JLC04161H-7628 4-layer.estackup"), "rb") as f:
            st = SU.parse_stackup(f.read())
        d, src = LG.layer_depths(via_board("(1+2*15+16)"), st)
        self.assertEqual(list(d), [1, 2, 15, 16])
        self.assertTrue(d[1] < d[2] < d[15] < d[16])
        cu = st.copper
        self.assertAlmostEqual(d[16] - d[1], st.total_mm() - (cu[0].thickness_mm + cu[-1].thickness_mm) / 2, places=3)
        self.assertIn("stackup", src)

    def test_unbalanced_vias_show_as_skew(self):
        p, n = LG.path(via_board(), "VP"), LG.path(via_board(), "VN")
        # two vias join top and bottom (1.6 mm each); the one at x 8 only touches top copper: a stub, 0
        self.assertEqual((p["copper_mm"], p["via_mm"], p["length_mm"]), (10.0, 3.2, 13.2))
        self.assertEqual(n["via_mm"], 0.0)
        res = LG.evaluate(via_board(), {"name": "V", "members": [["VP", "VN"]], "intra_tol_mm": 0.1})
        self.assertEqual(res["members"][0]["skew_mm"], 3.2)
        self.assertEqual(res["members"][0]["add_mm"], {"VN": 3.2})
        self.assertEqual([v["barrel_mm"] for v in LG.via_barrels(via_board(), "VP", {1: 0.0, 16: 1.6})],
                         [1.6, 1.6, 0.0])

    def test_check_length_match_counts_vias(self):
        with mock.patch.object(S, "session", Session(FakeFusion(via_board()))):
            res = S.check_length_match(["VP", "VN"], 0.5)
        rows = {r["net"]: r for r in res["nets"]}
        self.assertEqual((rows["VP"]["length_mm"], rows["VP"]["via_mm"]), (13.2, 3.2))
        self.assertEqual(rows["VN"]["short_by_mm"], 3.2)
        self.assertFalse(res["all_ok"])
        self.assertIn("1.6", res["via_depths_from"])

    def test_route_pair_tunes_for_unbalanced_vias(self):
        # P drops to the bottom and back in its head (two vias), N stays on top
        head = [[0.5, 0.25], {"via": [0.5, 0.25]}, [0.8, 0.6], {"via": [0.8, 0.6]}]
        p = PR.plan_pair(board(), "DP", "DN", CL, 0.2, 0.3, p_head=head, max_skew_mm=0.1)
        self.assertEqual((p["p"]["via_mm"], p["n"]["via_mm"], p["via_barrel_mm"]), (3.2, 0.0, 1.6))
        self.assertGreater(p["skew_before_tuning_mm"], 3.0)
        self.assertLessEqual(abs(p["skew_mm"]), 0.1)                      # N was tuned for the barrels
        self.assertGreater(p["tuning_added_mm"], 3.0)
        p = PR.plan_pair(board(), "DP", "DN", CL, 0.2, 0.3, p_head=head, layer_depths={1: 0.0, 16: 0.8})
        self.assertEqual(p["p"]["via_mm"], 1.6)

    def test_coupled_layer_change_stays_balanced(self):
        p = PR.plan_pair(board(), "DP", "DN", CL, 0.2, 0.3, layer_changes=[{"at": [10, 0], "to": "bottom"}],
                         p_tail=[{"via": [19.6, 0.25]}], n_tail=[{"via": [19.6, -0.25]}])
        self.assertEqual(p["p"]["via_mm"], p["n"]["via_mm"])
        self.assertEqual(p["p"]["via_mm"], 3.2)
        self.assertLessEqual(abs(p["skew_mm"]), 0.1)


class NeckOnArcsTest(unittest.TestCase):
    def test_split_keeps_arcs_and_moves_cuts_off_them(self):
        pl = [(0, 0), (2, 0), (3, 1, 90.0), (6, 1)]
        L = PR.length(pl)
        b, m, a = PR._split_at(pl, 4.0, 5.0)                              # both cuts on the last straight
        self.assertEqual(b[-2], (3, 1, 90.0))
        self.assertAlmostEqual(PR.length(b) + PR.length(m) + PR.length(a), L, places=3)
        b, m, a = PR._split_at(pl, 1.0, 2.5)                              # the end cut falls on the arc
        self.assertEqual(m[-1], (3, 1, 90.0))                             # the neck takes the whole arc
        self.assertEqual(a[0], (3, 1))
        self.assertAlmostEqual(PR.length(b) + PR.length(m) + PR.length(a), L, places=3)

    def test_neck_down_after_rounded_tuning_bumps(self):
        obs, _, _ = board_obstacles(pinned())
        need = lambda o: 0.12
        bumped = [(0, 0.25), (2, 0.25), (2.25, 0.5, 90.0), (2.5, 0.75, -90.0), (2.75, 0.5, -90.0),
                  (3, 0.25, 90.0), (20, 0.25)]
        partner = {1: PR.flatten([(0, -0.25), (20, -0.25)], 0.05)}
        out, necks = PR.neck_down_fn("DP", [(1, bumped)], obs, 0.2, need, 0.1, partner=partner, gap=0.3)
        self.assertEqual([n["width_mm"] for n in necks], [0.18])
        self.assertTrue(9 < necks[0]["from"][0] < 10 < necks[0]["to"][0] < 11)
        self.assertAlmostEqual(sum(PR.length(pl) for _, pl, _ in out), PR.length(bumped), places=3)
        self.assertEqual(sum(1 for _, pl, _ in out for q in pl if len(q) > 2), 4)   # the bump's arcs kept

    def test_squeeze_advice_when_neck_down_is_on(self):
        obs, _, _ = board_obstacles(pinned())
        args = (10, 0.25, "DP", 1, obs, 0.2, lambda o: 0.12, 0.1, [(10, -0.25)], 0.3)
        self.assertIn("(neck_down=true)", PR.explain_squeeze(*args)["options"][0])
        on = PR.explain_squeeze(*args, neck_down=True)["options"][0]
        self.assertNotIn("neck_down=true", on)
        self.assertIn("already on", on)


class EvaluateAddTest(unittest.TestCase):
    def test_pair_average_add_lands_on_the_target(self):
        # TP0: P 10.0, N 10.3 (average 10.15); TP1: 11.0 both. Each side goes to 11, no overshoot
        res = LG.evaluate(mdi(tp1_extra=1.0), GROUP)
        self.assertEqual(res["members"][0]["add_mm"], {"TP0_P": 1.0, "TP0_N": 0.7})
        res = LG.evaluate(mdi(tp1_extra=1.0), {**GROUP, "measure": "max"})
        self.assertEqual(res["members"][0]["add_mm"], {"TP0_P": 1.0, "TP0_N": 0.7})


class RoutePairGroupTest(unittest.TestCase):
    def run_route(self, root, group, members, *a, **kw):
        d = tempfile.mkdtemp()
        with mock.patch.object(design_store, "path", lambda n: os.path.join(d, n + ".json")), \
                mock.patch.object(S, "session", Session(FakeFusion(root))), \
                mock.patch.object(S, "_v2_rules", lambda: (None, "none")):
            S.set_length_group("G", members, 0.1, 0.1, **group)
            return S.route_pair(*a, group="G", dry_run=True, **kw)["plan"]

    def test_measure_max(self):
        # P carries two vias (3.2 mm) untuned; with measure max its longer side is what meets the target
        xs = ('<signal name="XP"><wire x1="0" y1="5" x2="25" y2="5" width="0.2" layer="1"/></signal>'
              '<signal name="XN"><wire x1="0" y1="6" x2="25" y2="6" width="0.2" layer="1"/></signal>')
        head = [[0.5, 0.25], {"via": [0.5, 0.25]}, [0.8, 0.6], {"via": [0.8, 0.6]}]
        plan = self.run_route(board(extra_signals=xs), {"measure": "max"}, [["DP", "DN"], ["XP", "XN"]],
                              "DP", "DN", CL, 0.2, 0.3, p_head_mm=head, tune=False)
        self.assertEqual(plan["group"]["measure"], "max")
        self.assertAlmostEqual(max(plan["p"]["length_mm"], plan["n"]["length_mm"]), 25.0, delta=0.02)

    def test_old_copper_and_follow_series(self):
        # TP0_P/TP0_N already have copper (4.5 mm each) and series parts beyond; the group does not
        # follow series parts, so only this route counts toward TP0
        plan = self.run_route(mdi(), {"follow_series": False}, [["TP0_P", "TP0_N"], ["TP1_P", "TP1_N"]],
                              "TP0_P", "TP0_N", [[0.3, 0.5], [4.2, 0.5]], 0.2, 0.8)
        g = plan["group"]
        self.assertEqual((g["target_mm"], g["path_without_route_mm"]), (4.5, 0.0))
        self.assertAlmostEqual(g["member_with_route_mm"], g["route_mm"], places=3)


class TolerancePresetTest(unittest.TestCase):
    def names(self, q):
        return [r["interface"] for r in S._tolerance_rows(q)]

    def test_exact_then_word_then_substring(self):
        self.assertEqual(self.names("DDR4"), ["DDR4"])
        self.assertEqual(self.names("ddr4"), ["DDR4"])
        self.assertTrue(self.names("LPDDR4")[0].startswith("LPDDR4"))
        self.assertEqual(len(self.names("LPDDR4")), 1)
        self.assertEqual(self.names("HDMI"), ["HDMI 1.4/2.0 TMDS"])
        self.assertEqual(len(self.names("HDMI 2.1")), 1)
        self.assertIn("DDR5", self.names("DDR5")[0])
        self.assertEqual(len(self.names("MDI")), 2)                       # still ambiguous
        self.assertEqual(self.names("nothing like it"), [])

    def test_preset_ddr4(self):
        d = tempfile.mkdtemp()
        with mock.patch.object(design_store, "path", lambda n: os.path.join(d, n + ".json")), \
                mock.patch.object(S, "session", Session(FakeFusion(mdi()))):
            res = S.set_length_group("D", [["TP0_P", "TP0_N"]], preset="DDR4")
        self.assertEqual(res["preset"]["interface"], "DDR4")


if __name__ == "__main__":
    unittest.main()
