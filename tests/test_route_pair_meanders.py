import math
import unittest

from fusion_offline import pairs as PR

from test_route_pair import board

LINE = [(0.0, 0.0), (20.0, 0.0)]
BELOW = [(0.0, -0.5), (20.0, -0.5)]


def legs_ok(pl, width, gap):
    return PR.same_net_spacing("X", [(1, pl)], width, gap) == []


class BumpShapeTest(unittest.TestCase):
    def test_45_degree_bumps(self):
        for want in (0.4, 1.3):
            new, added = PR.add_bumps(LINE, want, BELOW, style="45")
            self.assertAlmostEqual(added, want, places=3)
            self.assertAlmostEqual(PR.length(new) - 20.0, want, places=3)
            for a, b in zip(new, new[1:]):
                dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
                # flat or 45 degrees, to the 0.1 um rounding (plan_pair's clean-up makes it exact)
                self.assertTrue(dy < 1e-6 or abs(dx - dy) < 2.1e-4, (a, b))
            self.assertTrue(all(len(q) == 2 for q in new))                  # no arcs

    def test_legs_keep_the_same_net_spacing(self):
        # a 0.05 mm flat and gap put tall legs 0.33 mm apart, under 0.12 + 0.25 (JLC same-net spacing)
        w = 0.12
        tight, _ = PR.add_bumps(LINE, 1.2, BELOW, radius=0.14, top=0.05, space=0.05)
        self.assertFalse(legs_ok(tight, w, 0.25))
        for style in ("rounded", "45"):
            ok, added = PR.add_bumps(LINE, 1.2, BELOW, radius=0.14, top=0.05, space=0.05, style=style, leg_min=w + 0.25)
            self.assertAlmostEqual(added, 1.2, places=3)
            self.assertTrue(legs_ok(ok, w, 0.25), style)
        # a tiny correction shrinks the radius: leg_min still keeps the two legs apart
        small, _ = PR.add_bumps(LINE, 0.1, BELOW, radius=0.14, top=0.12, space=0.12, leg_min=w + 0.25)
        bump = [q for q in small if abs(q[1]) > 1e-6 or len(q) > 2]
        self.assertGreaterEqual(max(q[0] for q in bump) - min(q[0] for q in bump), w + 0.25 - 1e-3)

    def test_poe_board_meander_settings(self):
        # r 0.14, flat 0.12, gap 0.12 at w 0.12: legs 0.40 c-c, 0.28 edge to edge (2026-10-04)
        new, added = PR.add_bumps(LINE, 1.2, BELOW, radius=0.14, top=0.12, space=0.12, max_h=0.37, leg_min=0.12 + 0.25)
        self.assertAlmostEqual(added, 1.2, places=3)
        self.assertTrue(legs_ok(new, 0.12, 0.25))

    def test_near_puts_the_bumps_at_that_end(self):
        new, _ = PR.add_bumps(LINE, 0.6, BELOW, near=(20, 0))
        xs = [q[0] for q in new if abs(q[1]) > 1e-6]
        self.assertGreater(min(xs), 15)
        new, _ = PR.add_bumps(LINE, 0.6, BELOW, near=(0, 0))
        self.assertLess(max(q[0] for q in new if abs(q[1]) > 1e-6), 5)

    def test_same_net_check_catches_a_hairpin(self):
        hairpin = [(0, 0), (5, 0), (5, 0.3), (0, 0.3)]
        res = PR.same_net_spacing("X", [(1, hairpin)], 0.12, 0.25)
        self.assertEqual(len(res), 1)
        self.assertIn("within 0.180 mm of itself", res[0]["note"])


class ArcCentrelineTest(unittest.TestCase):
    def test_pair_follows_an_arc_concentrically(self):
        # east, a quarter turn left about (9, 2), then north
        root = board(p_end=(10.75, 11), n_end=(11.25, 11))
        # the inner trace is shorter round the bend; tuning would add bumps, so it is off here
        p = PR.plan_pair(root, "DP", "DN", [[1, 0], [9, 0], [11, 2, 90], [11, 10]], 0.2, 0.3, tune=False)
        self.assertEqual(p["conflicts"], [])
        self.assertAlmostEqual(p["skew_mm"], -math.pi / 2 * 0.5, places=3)  # 90 degrees x (2.25 - 1.75)
        for side, radius in (("p", 1.75), ("n", 2.25)):            # inner trace R - 0.25, outer R + 0.25
            pts = p[side]["traces"][0]["points"]
            k = next(i for i, q in enumerate(pts) if len(q) > 2)
            self.assertEqual(pts[k][2], 90)
            for q in (pts[k - 1], pts[k]):
                self.assertAlmostEqual(math.dist(q[:2], (9, 2)), radius, places=3)

    def test_layer_change_must_be_on_a_straight_part(self):
        root = board(p_end=(10.75, 11), n_end=(11.25, 11))
        with self.assertRaises(ValueError):
            PR.plan_pair(root, "DP", "DN", [[1, 0], [9, 0], [11, 2, 90], [11, 10]], 0.2, 0.3,
                         layer_changes=[{"at": [9 + 2 * math.sin(math.radians(45)), 2 - 2 * math.cos(math.radians(45))]}])


class MismatchPlacementTest(unittest.TestCase):
    def test_bumps_go_next_to_the_end_that_causes_the_skew(self):
        root = board(p_end=(20.5, 0.25))                          # DP's end pad is staggered by 0.5 mm
        for at, side in (("mismatch", "end"), ("longest", "middle")):
            p = PR.plan_pair(root, "DP", "DN", [[1, 0], [19.5, 0]], 0.2, 0.3, tune_at=at, max_skew_mm=0.05)
            self.assertTrue(p["skew_ok"], p["why_not_ok"])
            xs = [q[0] for t in p["n"]["traces"] for q in t["points"] if abs(q[1] + 0.25) > 1e-3 and 1 < q[0] < 19.5]
            if side == "end":
                self.assertGreater(min(xs), 14)
            else:
                self.assertTrue(6 < sum(xs) / len(xs) < 14)

    def test_bad_settings(self):
        with self.assertRaises(ValueError):
            PR.plan_pair(board(), "DP", "DN", [[1, 0], [19, 0]], 0.2, 0.3, tune_style="square")
        with self.assertRaises(ValueError):
            PR.plan_pair(board(), "DP", "DN", [[1, 0], [19, 0]], 0.2, 0.3, tune_at="start")


if __name__ == "__main__":
    unittest.main()
