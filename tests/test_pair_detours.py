import os
import tempfile
import unittest
from unittest import mock

from fusion_mcp import design_store
from fusion_mcp import server as S
from fusion_mcp.session import Session
from fusion_offline import pairs as PR

from test_move_parts import FakeFusion
from test_route_pair import board

CL = [[1, 0], [19, 0]]


def plan(root=None, **kw):
    return PR.plan_pair(root if root is not None else board(), "DP", "DN", kw.pop("centreline", CL), 0.2, 0.3, **kw)


class DetourTest(unittest.TestCase):
    """TP0-TP3 on the PoE board were matched between pairs by hand with coupled 45-degree detours
    (TP0 +0.709 mm with d = 0.856, TP2 +0.274 with d = 0.33)."""

    def test_both_traces_get_the_same_length(self):
        base = plan()
        p = plan(add_length=0.709)
        self.assertTrue(p["ok"], p["why_not_ok"])
        self.assertAlmostEqual(p["p"]["length_mm"] - base["p"]["length_mm"], 0.709, places=2)
        self.assertAlmostEqual(p["n"]["length_mm"] - base["n"]["length_mm"], 0.709, places=2)
        self.assertAlmostEqual(p["skew_mm"], 0.0, places=3)
        self.assertEqual(p["detours"][0]["count"], 1)
        self.assertAlmostEqual(p["detours"][0]["depth_mm"], 0.709 / (2 * (2 ** 0.5) - 2), places=2)   # about 0.856

    def test_split_into_several_when_too_deep(self):
        p = plan(add_length=2.0, detour_max_depth=0.5)
        self.assertEqual(p["detours"][0]["count"], 5)
        self.assertLessEqual(p["detours"][0]["depth_mm"], 0.5)
        self.assertAlmostEqual(p["p"]["length_mm"] - plan()["p"]["length_mm"], 2.0, places=2)

    def test_rounded_detours(self):
        p = plan(add_length=0.6, detour_style="rounded")
        self.assertAlmostEqual(p["p"]["length_mm"] - plan()["p"]["length_mm"], 0.6, places=2)
        self.assertTrue(any(len(q) > 2 for t in p["p"]["traces"] for q in t["points"]))      # arcs
        with self.assertRaises(ValueError):
            plan(add_length=0.6, detour_style="rounded", detour_radius=0.2)              # inner trace would fold

    def test_side_away_from_a_neighbour(self):
        # X runs just above the pair in the middle: the detour goes down
        root = board(extra_signals='<signal name="X"><wire x1="7" y1="1.1" x2="13" y2="1.1" width="0.2" layer="1"/></signal>')
        p = plan(root, add_length=0.5)
        self.assertEqual(p["detour_side"], "right")
        self.assertTrue(p["ok"], p["conflicts"])
        with self.assertRaises(ValueError):
            plan(root, add_length=0.5, detour_side="up")

    def test_no_room(self):
        with self.assertRaises(ValueError) as cm:
            plan(centreline=[[1, 0], [4, 0]], add_length=3.0)
        self.assertIn("straight runs hold at most 0.000 mm", str(cm.exception))


class DetourCapacityTest(unittest.TestCase):
    """PoE board TD2 (2026-10-08): +14.7 mm said '18 detour(s) 0.00 mm deep need 24.72 mm', and with
    detour_max_depth_mm=3 it used 0.97 mm deep detours."""
    TD2 = [[46.9, 22.5], [48.5, 22.5], [58.25, 32.25], [59.0, 32.25]]

    def test_capacity_error_names_the_limit(self):
        with self.assertRaises(ValueError) as cm:
            PR.add_detours(self.TD2, 14.7, 1.0, 0.12, 0.15, max_depth=1.0)
        msg = str(cm.exception)
        self.assertIn("hold at most 2.", msg)                        # three 1 mm detours on the 13.79 mm run
        self.assertIn("13.79 mm", msg)
        self.assertNotIn("0.00 mm deep", msg)

    def test_allowed_depth_is_used(self):
        new, info = PR.add_detours(self.TD2, 2.4, 1.0, 0.12, 0.15, max_depth=3.0)
        self.assertEqual(info[0]["count"], 1)
        self.assertAlmostEqual(info[0]["depth_mm"], 2.4 / (2 * (2 ** 0.5) - 2), places=3)      # 2.897, not 0.97
        self.assertAlmostEqual(PR.length(new) - PR.length(self.TD2), 2.4, places=3)

    def test_spread_over_several_runs(self):
        c = [[0, 0], [10, 0], [10, 10]]
        new, info = PR.add_detours(c, 3.0, 1.0, 0.2, 0.3, max_depth=1.0)
        self.assertEqual(len(info), 2)                                # one 10 mm run holds about 1.66 mm
        self.assertAlmostEqual(sum(i["added_mm"] for i in info), 3.0, places=3)
        self.assertAlmostEqual(PR.length(new) - PR.length(c), 3.0, places=3)


class GroupTargetTest(unittest.TestCase):
    def test_route_pair_lengthens_to_the_group_target(self):
        # XP/XN (another member) are 22 mm routed; DP/DN would be about 20 mm: 2 mm is added
        xs = ('<signal name="XP"><wire x1="0" y1="5" x2="22" y2="5" width="0.2" layer="1"/></signal>'
              '<signal name="XN"><wire x1="0" y1="6" x2="22" y2="6" width="0.2" layer="1"/></signal>')
        root = board(extra_signals=xs)
        d = tempfile.mkdtemp()
        with mock.patch.object(design_store, "path", lambda n: os.path.join(d, n + ".json")), \
                mock.patch.object(S, "session", Session(FakeFusion(root))), \
                mock.patch.object(S, "_v2_rules", lambda: (None, "none")):
            S.set_length_group("G", [["DP", "DN"], ["XP", "XN"]], 0.1, 0.1)
            res = S.route_pair("DP", "DN", CL, 0.2, 0.3, group="G", dry_run=True)
        g = res["plan"]["group"]
        self.assertEqual((g["member"], g["target_mm"]), ("DP/DN", 22.0))
        self.assertGreater(g["add_mm"], 1.5)
        avg = (res["plan"]["p"]["length_mm"] + res["plan"]["n"]["length_mm"]) / 2
        self.assertAlmostEqual(avg, 22.0, places=2)
        self.assertTrue(res["plan"]["ok"], res["plan"]["why_not_ok"])


if __name__ == "__main__":
    unittest.main()
