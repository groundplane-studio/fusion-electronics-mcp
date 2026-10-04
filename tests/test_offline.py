import math
import os
import unittest
import xml.etree.ElementTree as ET

from fusion_offline import bompnp, design as D, eagle, review, si

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    return D.read_xml(os.path.join(FIX, name))


class BoardDesignTest(unittest.TestCase):
    def setUp(self):
        self.b = D.parse_board_design(load("mini.brd"))

    def test_signals_and_classes(self):
        self.assertEqual(set(self.b.signals), {"D_P", "D_N"})
        self.assertEqual(self.b.signals["D_P"].net_class, "USB")
        self.assertEqual(self.b.classes[1].width, 0.2)
        self.assertAlmostEqual(self.b.signals["D_N"].length, 10.3)
        self.assertEqual(len(self.b.signals["D_N"].vias), 1)

    def test_stackup_reports_unknowns(self):
        st = self.b.stackup()
        self.assertEqual([l["layer"] for l in st["copper_layers"]], [1, 16])
        self.assertEqual(st["copper_layers"][0]["copper_mm"], 0.035)
        self.assertIsNone(st["er"])
        self.assertIsNone(st["dielectric_mm"])

    def test_arc_length(self):
        w = D.Wire(0, 0, 2, 0, 0.1, 1, curve=180)
        self.assertAlmostEqual(w.length, math_pi(), places=6)


def math_pi():
    import math
    return math.pi


class SchematicDesignTest(unittest.TestCase):
    def setUp(self):
        self.s = D.parse_schematic_design(load("mini.sch"))

    def test_attributes_resolve_from_technology(self):
        self.assertEqual(self.s.parts["R1"].attributes["JLCPCB"], "C25744")
        self.assertEqual(self.s.parts["R2"].attributes["JLCPCB"], "C11702")
        self.assertEqual(self.s.parts["R1"].package, "R0402")
        self.assertTrue(self.s.parts["U1"].user_value)
        self.assertFalse(self.s.parts["R1"].user_value)

    def test_pin_world_coordinates(self):
        # R90 rotates (10.16, 0) to (0, 10.16); the same case matched the live API in the spike
        p2 = self.s.pin("R1", "P$2")
        self.assertEqual((p2.x, p2.y), (0.0, 10.16))
        # MR0: mirror X about the instance origin
        vin = self.s.pin("U1", "VIN")
        self.assertEqual((vin.x, vin.y), (47.62, 0.0))

    def test_outward_vectors(self):
        self.assertEqual(self.s.pin("R2", "P$1").outward, (-1.0, 0.0))    # R0 pin: body to +x
        self.assertEqual(self.s.pin("R2", "P$2").outward, (1.0, 0.0))     # R180 pin
        self.assertEqual(self.s.pin("U1", "VIN").outward, (1.0, 0.0))     # mirrored

    def test_nets_merge_across_sheets(self):
        n = self.s.nets["VOUT"]
        self.assertEqual(n.sheets, [1, 2])
        self.assertEqual({(r.part, r.pin) for r in n.pins}, {("R1", "P$2"), ("R2", "P$1"), ("U1", "VOUT")})
        self.assertEqual(self.s.net_of("U1", "VOUT"), "VOUT")

    def test_instances(self):
        i = self.s.instance("U1")
        self.assertEqual((i.x, i.y, i.angle, i.mirror, i.sheet), (40.0, 0.0, 0.0, True, 1))


class SiTest(unittest.TestCase):
    def test_pair_report(self):
        b = D.parse_board_design(load("mini.brd"))
        self.assertEqual(si.find_diff_pairs(b), [("D_P", "D_N")])
        r = si.pair_report(b, "D_P", "D_N")
        self.assertEqual(r["skew_mm"], 0.3)
        self.assertEqual(r["skew_limit_mm"], 0.5)
        self.assertTrue(r["within_limit"])
        self.assertAlmostEqual(r["gap_mm"], 0.2, places=4)

    def test_impedance_is_labelled_estimate(self):
        r = si.estimate_impedance(0.3, 0.2, 0.035, 4.3)
        self.assertIn("estimate", r["method"])
        self.assertTrue(30 < r["z0_ohm"] < 80)
        with self.assertRaises(ValueError):
            si.estimate_impedance(0.3, 0.2, 0.035, 4.3, geometry="coax")


class ReviewTest(unittest.TestCase):
    def test_rules(self):
        f = review.review(D.parse_schematic_design(load("mini.sch")))
        got = {(x["severity"], x["rule"], x["refs"][0]) for x in f}
        self.assertIn(("error", "unconnected_power_pin", "U1"), got)  # VIN is a power pin
        self.assertIn(("warning", "unconnected_pin", "U1"), got)      # EN, grouped per part
        self.assertEqual(sum(1 for x in f if x["rule"] == "unconnected_pin" and x["refs"] == ["U1"]), 1)
        jlc = next(x for x in f if x["rule"] == "missing_jlc_code")
        self.assertEqual(jlc["refs"], ["U1"])                         # one grouped finding
        self.assertIn(("warning", "empty_value", "U1"), got)
        self.assertFalse(any(": NC" in x["message"] or " NC," in x["message"] for x in f))  # NC pins skipped
        self.assertEqual(f[0]["severity"], "error")                   # sorted by severity


class DrawingRulesTest(unittest.TestCase):
    SCH = """<eagle><drawing><schematic><libraries/><parts/><sheets>
<sheet><nets>
<net name="BOX"><segment>
<wire x1="0" y1="0" x2="10" y2="0" width="0.15" layer="91"/><wire x1="10" y1="0" x2="10" y2="5" width="0.15" layer="91"/>
<wire x1="10" y1="5" x2="0" y2="5" width="0.15" layer="91"/><wire x1="0" y1="5" x2="0" y2="0" width="0.15" layer="91"/>
</segment></net>
<net name="STRAY"><segment><wire x1="20" y1="0" x2="25" y2="0" width="0.15" layer="91"/></segment></net>
<net name="SPLIT">
<segment><pinref part="A" gate="G" pin="1"/><wire x1="30" y1="0" x2="35" y2="0" width="0.15" layer="91"/></segment>
<segment><pinref part="B" gate="G" pin="1"/><wire x1="40" y1="0" x2="45" y2="0" width="0.15" layer="91"/><label x="45" y="0"/></segment>
</net>
<net name="JOINED">
<segment><pinref part="C" gate="G" pin="1"/><wire x1="50" y1="0" x2="52" y2="0" width="0.15" layer="91"/></segment>
<segment><pinref part="C" gate="G" pin="1"/><wire x1="50" y1="0" x2="50" y2="3" width="0.15" layer="91"/></segment>
</net>
</nets></sheet></sheets></schematic></drawing></eagle>"""

    def test_rules(self):
        import xml.etree.ElementTree as ET
        s = D.parse_schematic_design(ET.fromstring(self.SCH))
        rules = {(x["rule"], x["refs"][0]) for x in review.review(s)}
        self.assertIn(("box_drawn_as_net", "BOX"), rules)
        self.assertIn(("stray_wire", "STRAY"), rules)
        self.assertIn(("unlabeled_net_segment", "SPLIT"), rules)
        self.assertNotIn(("unlabeled_net_segment", "JOINED"), rules)   # joined through a shared pin
        self.assertEqual(list(review.unlabeled_pieces(s)), ["SPLIT"])

    def test_default_skew_limit_is_not_trusted(self):
        b = D.parse_board_design(load("mini.brd"))
        b.rules["dpMaxLengthDifference"] = "10mm"
        r = si.pair_report(b, "D_P", "D_N")
        self.assertIsNone(r["within_limit"])
        self.assertEqual(r["longer"], "D_N")


class StackupTest(unittest.TestCase):
    XML = """<eagle><designrules><classes><class number="0" name="default"/><class number="4" name="diff"/></classes>
<rules>
<rule type="Copper Clearance" enabled="yes" onescope="classes=4" otherscope="all" value="8mil" samesignal="no" name="cc1"/>
<rule type="Copper Clearance" enabled="no" onescope="classes=4" otherscope="all" value="1mm" samesignal="no" name="off"/>
</rules>
<layerstackup name="2L">
<layerdef type="Solder Mask" name="Top SolderMask"><material thickness="0.0254mm" dielectric_constant="3.6"/></layerdef>
<layerdef type="Signal" layer="1" name="Top"><material thickness="0.035mm"/></layerdef>
<layerdef type="Core" name="Core"><material thickness="0.2mm" dielectric_constant_1g="4.2"/></layerdef>
<layerdef type="Signal" layer="16" name="Bottom"><material thickness="0.035mm"/></layerdef>
</layerstackup></designrules></eagle>"""

    def test_parse(self):
        from fusion_offline import stackup as ST
        st = ST.parse_stackup(self.XML)
        self.assertEqual([c.name for c in st.copper], ["Top", "Bottom"])
        up, down = st.neighbours(0)
        self.assertIsNone(up)
        self.assertEqual((down.thickness_mm, down.er), (0.2, 4.2))
        rules, cls = ST.parse_clearance_rules(self.XML)
        self.assertEqual(cls["4"], "diff")
        self.assertEqual([r.name for r in ST.class_clearance(rules, "4")], ["cc1"])   # disabled rule ignored
        self.assertAlmostEqual(ST.class_clearance(rules, "4")[0].value_mm, 0.2032)

    def test_pair_impedance_on_stack(self):
        from fusion_offline import stackup as ST
        b = D.parse_board_design(load("mini.brd"))
        r = si.pair_impedance(b, ST.parse_stackup(self.XML), "D_P", "D_N")
        self.assertEqual(r["layer"], "Top")
        self.assertIn("Core", r["reference"])
        self.assertTrue(50 < r["zdiff_ohm"] < 150)


class StubTest(unittest.TestCase):
    def test_find_stubs(self):
        from fusion_offline import stitch as ST
        stubs = ST.find_stubs(load("mini.brd"))
        by = {s["net"]: s for s in stubs}
        self.assertEqual(set(by), {"D_P", "D_N"})          # fixture wires end in free space
        self.assertEqual(by["D_N"]["dangling"], (1.0, 2.4))  # its other end sits on a via


class KicadPlacementTest(unittest.TestCase):
    PCB = """(kicad_pcb (version 20240108)
  (gr_rect (start 100 50) (end 120 80) (layer "Edge.Cuts"))
  (footprint "R_0402" (layer "F.Cu") (at 105 55 90) (property "Reference" "R1"))
  (footprint "SW" (layer "B.Cu") (at 110 75) (property "Reference" "SW1"))
  (footprint "C" (layer "B.Cu") (at 112 60 270) (property "Reference" "C1")))"""

    def test_frame_and_bottom_rule(self):
        from fusion_offline import kicad_pcb as KP
        r = KP.read_placement(self.PCB)
        self.assertEqual(r["board_mm"], [20.0, 30.0])
        self.assertEqual((r["parts"]["R1"]["x_mm"], r["parts"]["R1"]["y_mm"], r["parts"]["R1"]["angle"]), (5.0, 25.0, 90.0))
        sw = r["parts"]["SW1"]
        self.assertEqual((sw["x_mm"], sw["y_mm"], sw["angle"], sw["bottom"]), (10.0, 5.0, 180.0, True))
        # bottom 270 -> mirrored 180 - 270 = 270 (pcbnew: a bottom part at 90 is Fusion MR90)
        self.assertEqual(r["parts"]["C1"]["angle"], 270.0)


class PlacementTest(unittest.TestCase):
    def test_mst_and_crossings(self):
        from fusion_offline import placement as PL
        e = PL._mst([(0, 0), (3, 0), (0, 4)])
        self.assertAlmostEqual(sum(((a[0]-b[0])**2+(a[1]-b[1])**2)**0.5 for a, b in e), 7.0)
        self.assertTrue(PL._cross(((0, 0), (2, 2)), ((0, 2), (2, 0))))
        self.assertFalse(PL._cross(((0, 0), (1, 0)), ((0, 1), (1, 1))))

    def test_score_on_fixture(self):
        from fusion_offline import placement as PL
        m = PL.load(load("mini.brd"))
        s = PL.score(m)
        self.assertEqual(s["crossings"], 0)
        self.assertGreaterEqual(s["ratsnest_mm"], 0)


class BomTest(unittest.TestCase):
    def test_jlc_bom_and_cpl(self):
        with open(os.path.join(FIX, "mini.brd"), "rb") as f:
            res = bompnp.generate(eagle.parse_board(f.read()))
        bom = bompnp.bom_csv(res)
        self.assertIn("C25744", bom)
        self.assertIn("R1, R2", bom)
        cpl = bompnp.pnp_csv(res)
        self.assertIn("R2,", cpl)
        self.assertIn("Bottom", cpl)


if __name__ == "__main__":
    unittest.main()


class PairGeometryTest(unittest.TestCase):
    def test_offset_keeps_gap_through_45_bends(self):
        from fusion_offline import pairs as PR
        c = [(0, 10), (0, 5), (3, 2), (10, 2)]
        a, b = PR.offset_path(c, 0.25), PR.offset_path(c, -0.25)
        for (p, q), (r, s) in zip(zip(a, a[1:]), zip(b, b[1:])):
            self.assertAlmostEqual(PR._seg_dist(p, q, r, s), 0.5, places=6)

    def test_fan_in_is_octilinear_and_arrives_along_trunk(self):
        from fusion_offline import pairs as PR
        pts = PR.fan_in((19.65, 58.11), (20.41, 56.4), (0.0, -1.0))
        self.assertEqual(len(pts), 3)
        (x0, y0), (x1, y1), (x2, y2) = pts
        self.assertAlmostEqual(abs(x1 - x0), abs(y1 - y0), places=6)   # 45-degree leg
        self.assertAlmostEqual(x1, x2, places=6)                       # then straight down

    def test_bumps_add_requested_length(self):
        from fusion_offline import pairs as PR
        pl = [(0.0, 0.0), (20.0, 0.0)]
        for want in (1.0, 2.6, 0.3):                                   # incl. one below a full-radius bump
            new, added = PR.add_bumps(pl, want, [(0.0, -0.5), (20.0, -0.5)])
            self.assertAlmostEqual(PR.length(new) - 20.0, added, places=3)
            self.assertAlmostEqual(added, want, places=3)
            flat = PR.flatten(new, 0.01)
            self.assertTrue(all(y >= -1e-6 for _, y in flat))         # bulges away from the partner
            self.assertAlmostEqual(sum(math.dist(a, b) for a, b in zip(flat, flat[1:])), 20.0 + want, places=2)

    def test_arc_distance(self):
        from fusion_offline import design as DS, si as SI
        w = DS.Wire(1.0, 0.0, 0.0, 1.0, 0.2, 1, 90.0)                 # quarter circle about the origin
        self.assertAlmostEqual(SI._seg_point_dist(math.sqrt(0.5), math.sqrt(0.5), w), 0.0, places=6)
        self.assertAlmostEqual(SI._seg_point_dist(0.0, 0.0, w), 1.0, places=6)
        self.assertAlmostEqual(SI._seg_point_dist(-1.0, 0.0, w), math.sqrt(2), places=6)


class GerberCheckTest(unittest.TestCase):
    def _cam(self, root, tmp, drop_hole=False, extra_hole=False):
        from fusion_offline import gerbers as G
        holes, _ = G.design_holes(root)
        hs = holes.holes[1:] if drop_hole else list(holes.holes)
        if extra_hole:
            hs.append((0.3, 1.0, 1.0))
        tools = sorted({d for d, _, _ in hs})
        lines = ["M48", "METRIC"] + [f"T{i + 1}C{d:.3f}" for i, d in enumerate(tools)] + ["%"]
        for i, d in enumerate(tools):
            lines.append(f"T{i + 1}")
            lines += [f"X{x:.3f}Y{y:.3f}" for dd, x, y in hs if dd == d]
        lines.append("M30")
        open(os.path.join(tmp, "drill_1_16.xln"), "w").write("\n".join(lines))
        b = root.find("./drawing/board")
        xs = [float(w.get(k)) for w in b.iterfind("./plain/wire") if w.get("layer") == "20" for k in ("x1", "x2")]
        ys = [float(w.get(k)) for w in b.iterfind("./plain/wire") if w.get("layer") == "20" for k in ("y1", "y2")]
        def g(x0, y0, x1, y1):
            c = lambda v: int(round(v * 1e6))
            return ("%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.1*%\nD10*\n"
                    f"X{c(x0)}Y{c(y0)}D02*\nX{c(x1)}Y{c(y0)}D01*\nX{c(x1)}Y{c(y1)}D01*\nM02*\n")
        box = g(min(xs), min(ys), max(xs), max(ys))

        def with_pads(top):
            c = lambda v: int(round(v * 1e6))
            fl = "".join(f"X{c((b[0] + b[2]) / 2)}Y{c((b[1] + b[3]) / 2)}D03*\n" for _, b in G.smd_pads(root, top))
            return box.replace("M02*", fl + "M02*")
        for name in ("profile.gbr", "copper_top_l1.gbr", "copper_bottom_l2.gbr", "silkscreen_top.gbr"):
            open(os.path.join(tmp, name), "w").write(box)
        for name, top in (("soldermask_top.gbr", True), ("solderpaste_top.gbr", True),
                          ("soldermask_bottom.gbr", False), ("solderpaste_bottom.gbr", False)):
            open(os.path.join(tmp, name), "w").write(with_pads(top))

    def test_design_vs_cam(self):
        import tempfile
        from fusion_offline import gerbers as G
        root = ET.parse(os.path.join(FIX, "mini.brd")).getroot()
        holes, _ = G.design_holes(root)
        with tempfile.TemporaryDirectory() as tmp:
            self._cam(root, tmp)
            r = G.check(tmp, root, copper_layers=2)
            self.assertTrue(r["ok"], r["problems"])
        if holes.holes:
            with tempfile.TemporaryDirectory() as tmp:
                self._cam(root, tmp, drop_hole=True)
                r = G.check(tmp, root, copper_layers=2)
                self.assertFalse(r["ok"])
        with tempfile.TemporaryDirectory() as tmp:
            self._cam(root, tmp, extra_hole=True)
            r = G.check(tmp, root, copper_layers=4)
            self.assertFalse(r["ok"])
            self.assertTrue(any("copper layers" in p for p in r["problems"]))
            self.assertTrue(any("does not have" in p or "differ" in p for p in r["problems"]))
        with tempfile.TemporaryDirectory() as tmp:          # paste missing on the pads
            self._cam(root, tmp)
            open(os.path.join(tmp, "solderpaste_bottom.gbr"), "w").write("%FSLAX46Y46*%\n%MOMM*%\nM02*\n")
            r = G.check(tmp, root, copper_layers=2)
            self.assertTrue(any("no paste over them" in p for p in r["problems"]), r["problems"])


class JlcOrientTest(unittest.TestCase):
    def test_derive_rotation_and_offset(self):
        from fusion_offline import jlc_orient as J
        # JLC's footprint: 4 pins centred on the origin along x; ours: origin at pin 1, turned 180
        easy = [(str(i + 1), -3.0 + 2.0 * i, 0.0) for i in range(4)]
        ours = [(str(i + 1), -2.0 * i, 0.0) for i in range(4)]
        d = J.derive(ours, easy)
        self.assertEqual(d.rotation, 180)
        self.assertAlmostEqual(d.dx, 3.0, places=3)        # Rot_-180(ours) - easy
        self.assertTrue(d.trustworthy)
        self.assertTrue(J.derive(easy, easy).is_identity)


class JlcConsensusTest(unittest.TestCase):
    def test_partly_different_footprint_keeps_exact_offset(self):
        from fusion_offline import jlc_orient as J
        easy = [("1", -3.0, 0.0), ("2", 3.0, 0.0), ("3", -3.0, 4.0), ("4", 3.0, 4.0), ("S1", -1.0, 2.0), ("S2", 1.0, 2.0)]
        ours = [(n, x + (0.55 if n.startswith("S") else 0.0), y) for n, x, y in easy]   # signal row shifted
        d = J.derive(ours, easy)
        self.assertEqual(d.rotation, 0)
        self.assertAlmostEqual(d.dx, 0.0, places=3)
        self.assertEqual(sorted(o for o, _, _ in d.outliers), ["S1", "S2"])
        self.assertFalse(d.trustworthy)


class JlcFunctionMatchTest(unittest.TestCase):
    def test_diode_numbered_differently_is_not_turned_around(self):
        from fusion_offline import jlc_orient as J
        ours = [("1", -2.0, 0.0), ("2", 2.0, 0.0)]                 # KiCad SMA: pad 1 = cathode, left
        easy = [("1", 2.5, 0.0), ("2", -2.5, 0.0)]                 # JLC SMA:   pad 1 = anode, right
        o, e, used = J.by_function(ours, {"1": "K", "2": "A"}, easy, {"1": "A", "2": "K"})
        self.assertTrue(used)
        d = J.derive(o, e)
        self.assertEqual(d.rotation, 0)                            # cathode stays on the left
        self.assertAlmostEqual(d.dx, 0.0, places=3)

    def test_pitch_rounding_keeps_mean_fit(self):
        from fusion_offline import jlc_orient as J
        ours = [(str(i + 1), -1.137, 0.95 - 0.95 * i) for i in range(3)] + [(str(i + 4), 1.137, -0.95 + 0.95 * i) for i in range(3)]
        easy = [(str(i + 1), -1.2, 0.95 - 0.95 * i) for i in range(3)] + [(str(i + 4), 1.2, -0.95 + 0.95 * i) for i in range(3)]
        d = J.derive(ours, easy)
        self.assertEqual(d.rotation, 0)
        self.assertAlmostEqual(d.dx, 0.0, places=3)
        self.assertFalse(d.outliers)


class BlockPlanTest(unittest.TestCase):
    """sch_plan: supply-named rails, decaps to the IC they sit at, spare pins labelled."""

    def test_rules(self):
        from fusion_offline import sch_plan as SP
        p2 = lambda xy: {"footprint": "x", "value": "", "pads": ["1", "2"], "xy": xy}
        parts = {"U1": {"footprint": "x", "value": "", "pads": ["1", "2", "3"], "xy": (0, 0), "pad_xy": {"1": (-1, 0)}},
                 "U2": {"footprint": "x", "value": "", "pads": ["1", "2"], "xy": (10, 0), "pad_xy": {"1": (9, 0)}},
                 "C1": p2((-1.5, 1)), "C2": p2((9.5, 1)), "J1": {"footprint": "x", "value": "", "pads": ["1", "2"], "xy": (20, 0)}}
        nets = {"3V3_AUX": [("U1", "1"), ("U2", "1"), ("C1", "1"), ("C2", "1")],
                "GND": [("U1", "2"), ("U2", "2"), ("C1", "2"), ("C2", "2"), ("J1", "2")],
                "GPIO_SPARE": [("J1", "1")], "unconnected-(U1-Pad3)": [("U1", "3")]}
        plan = SP.plan({"parts": parts, "nets": nets})
        self.assertIn("3V3_AUX", plan["rails"])                   # 4 pins, but named like a supply
        b = {x["anchor"]: x for x in plan["blocks"]}
        self.assertEqual(b["U1"]["members"], ["C1"])               # each decap with the IC it sits at
        self.assertEqual(b["U2"]["members"], ["C2"])
        self.assertIn("GPIO_SPARE", b["J1"]["external_nets"])      # a spare pin keeps its name
        self.assertNotIn("unconnected-(U1-Pad3)", b["U1"]["external_nets"])


class SchematicBlocksTest(unittest.TestCase):
    """Block-style schematic: grouping and a layout with every pin wired correctly."""

    @staticmethod
    def _box(name, pins):
        from fusion_offline import symbols as S
        return S.from_part_json({"deviceset": name, "symbol": {"name": name, "pins": [
            {"name": n, "pad": n, "side": side} for n, side in pins]}})

    def setUp(self):
        two = [("1", "left"), ("2", "right")]
        self.geo = {"U1": self._box("REG", [("VIN", "left"), ("EN", "left"), ("FB", "left"),
                                            ("BST", "right"), ("SW", "right"), ("GND", "right")]),
                    "J1": self._box("HDR", [("1", "left"), ("2", "left"), ("3", "left")])}
        for r in ("C1", "C2", "C3", "L1", "R1", "R2", "D1"):
            self.geo[r] = self._box("P2", two)
        self.geo["TP1"] = self._box("TP", [("1", "left")])
        nets = {
            "VIN": [("U1", "VIN"), ("U1", "EN"), ("C1", "1"), ("J1", "1")],
            "GND": [("U1", "GND"), ("C1", "2"), ("C2", "2"), ("D1", "2"), ("J1", "3")] + [(f"X{i}", "1") for i in range(6)],
            "BST": [("U1", "BST"), ("C3", "1")],
            "SW": [("U1", "SW"), ("C3", "2"), ("L1", "1")],
            "VOUT": [("L1", "2"), ("C2", "1"), ("R1", "1"), ("U1", "FB"), ("TP1", "1")] + [(f"Y{i}", "1") for i in range(6)],
            "LED_A": [("R1", "2"), ("D1", "1")],
            "SIG": [("J1", "2"), ("R2", "2")],
            "VOUT2": [],
        }
        nets["VOUT"].append(("R2", "1"))
        parts = {r: {"footprint": "x", "value": r, "pads": sorted({p for n, pp in nets.items() for rr, p in pp if rr == r})}
                 for r in self.geo}
        for i in range(6):
            parts[f"X{i}"] = {"footprint": "x", "value": "", "pads": ["1"]}
            parts[f"Y{i}"] = {"footprint": "x", "value": "", "pads": ["1"]}
        self.nl = {"parts": parts, "nets": {k: v for k, v in nets.items() if v}}

    def test_grouping(self):
        from fusion_offline import sch_plan as SP
        plan = SP.plan({"parts": {r: p for r, p in self.nl["parts"].items() if r in self.geo},
                        "nets": self.nl["nets"]})
        blocks = {b["anchor"]: b for b in plan["blocks"]}
        self.assertEqual(set(blocks["U1"]["members"]), {"C1", "C2", "C3", "D1", "L1", "R1", "TP1"})
        self.assertEqual(blocks["J1"]["members"], ["R2"])           # pull-up goes with the header
        self.assertEqual(blocks["U1"]["owned_rails"], ["VOUT"])     # the inductor makes VOUT

    def test_layout_wires_every_pin(self):
        from fusion_offline import sch_layout as L, sch_plan as SP
        parts = {r: p for r, p in self.nl["parts"].items() if r in self.geo}
        nl = {"parts": parts, "nets": {n: [x for x in v if x[0] in parts] for n, v in self.nl["nets"].items()}}
        plan = SP.plan(nl)
        rails_all = SP.rails(self.nl["nets"])
        pad_net = {(r, p): n for n, pp in nl["nets"].items() for r, p in pp}
        sup = {"gnd": self._box("GNDSYM", [("GND", "left")]), "bar": self._box("BAR", [("VDD", "left")])}
        for b in plan["blocks"]:
            refs = {b["anchor"], *b["members"]}
            owned = {"VOUT"} if b["anchor"] == "U1" else set()
            ctx = L.Ctx(self.geo, pad_net, {}, refs, b["anchor"], {"GND"}, rails_all, owned,
                        set(b["external_nets"]), sup)
            d = L.layout_block(ctx)
            self.assertEqual(set(d.parts), refs, b["anchor"])
            c = L.check(d, self.geo, pad_net, refs, sup)
            mine = {k: v for k, v in c.items() if k in ("wrong", "missing", "shorts", "pin_on_wire",
                                                        "wire_touch", "unlinked") and v}
            self.assertEqual(mine, {}, b["anchor"])


class StandardSymbolsTest(unittest.TestCase):
    """Two-pin symbols to the 7.62 mm standard without changing their pins' names."""

    def test_resistor_keeps_artwork_size_and_moves_pins(self):
        import xml.etree.ElementTree as ET
        from fusion_mcp import std_symbols as SS
        sym = ET.fromstring(
            '<symbol name="RES"><pin name="P$1" x="10.16" y="0" length="short" rot="R180"/>'
            '<pin name="P$2" x="0" y="0" length="short"/>'
            '<wire x1="2.54" y1="0" x2="3.81" y2="1" width="0.15" layer="94"/>'
            '<wire x1="3.81" y1="1" x2="7.62" y2="0" width="0.15" layer="94"/>'
            '<text x="0" y="5.08" size="1.524" layer="95">&gt;NAME</text></symbol>')
        script, exp = SS.standardise(sym)
        self.assertEqual(exp["pins"], {"P$2": (0.0, 0.0), "P$1": (7.62, 0.0)})
        self.assertIn("CHANGE LENGTH POINT", script)           # 5.08 mm of artwork: point pins + leads
        self.assertNotIn("DELETE (0 0)", script)                # pins are moved, never deleted
        xml = exp["xml"]
        xs = [float(w.get(k)) for w in xml.iter("wire") for k in ("x1", "x2")]
        self.assertAlmostEqual(min(xs), 0.0); self.assertAlmostEqual(max(xs), 7.62)
        self.assertEqual(SS.check(xml, exp), [])

    def test_already_standard_is_left_alone(self):
        import xml.etree.ElementTree as ET
        from fusion_mcp import std_symbols as SS
        cap = ET.fromstring('<symbol name="CAP"><pin name="1" x="0" y="0" length="short"/>'
                            '<pin name="2" x="7.62" y="0" length="short" rot="R180"/></symbol>')
        self.assertIsNone(SS.standardise(cap))

    def test_styled_library_part_puts_anode_left(self):
        from fusion_offline import symbols as S
        g = S.from_part_json({"deviceset": "D", "symbol": {"name": "D", "style": "schottky", "pins": [
            {"name": "K", "pad": "1", "side": "left"}, {"name": "A", "pad": "2", "side": "right"}]}})
        self.assertEqual((g.pins["A"].x, g.pins["K"].x), (0.0, 7.62))
        self.assertEqual(g.pad_pin, {"1": "K", "2": "A"})


class RouterTest(unittest.TestCase):
    """route(): octilinear, around other nets with clearance, ends exactly on the pads."""

    BOARD = """<eagle><drawing><board>
<plain><wire x1="0" y1="0" x2="40" y2="0" width="0" layer="20"/><wire x1="40" y1="0" x2="40" y2="20" width="0" layer="20"/>
<wire x1="40" y1="20" x2="0" y2="20" width="0" layer="20"/><wire x1="0" y1="20" x2="0" y2="0" width="0" layer="20"/></plain>
<libraries><library name="L"><packages><package name="P"><smd name="1" x="0" y="0" dx="1" dy="1" layer="1"/></package></packages></library></libraries>
<designrules name="r"><param name="mdWireWire" value="0.2mm"/><param name="mdCopperDimension" value="0.3mm"/></designrules>
<elements><element name="A" library="L" package="P" x="5.03" y="10.07"/><element name="B" library="L" package="P" x="35.11" y="10.02"/></elements>
<signals><signal name="S"><contactref element="A" pad="1"/><contactref element="B" pad="1"/></signal>
<signal name="X"><wire x1="20" y1="2" x2="20" y2="18" width="0.5" layer="1"/></signal></signals>
</board></drawing></eagle>"""

    def test_routes_around_and_lands_on_pads(self):
        import math
        import xml.etree.ElementTree as ET
        from fusion_offline import router as R
        root = ET.fromstring(self.BOARD)
        r = R.route(root, "S", (5.03, 10.07), (1,), (35.11, 10.02), (1,), width=0.25, vias=False, step=0.254)
        self.assertEqual(r.problems, [])
        (layer, pts), = r.legs
        self.assertEqual(pts[0], (5.03, 10.07))
        self.assertEqual(pts[-1], (35.11, 10.02))
        for a, b in zip(pts[1:-2], pts[2:-1]):           # interior runs are 0/45/90 degrees
            ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 45
            self.assertTrue(ang < 0.01 or ang > 44.99, (a, b))
        self.assertTrue(any(abs(p[1] - 10) > 7.5 for p in pts))   # went round the X trace's end

    def test_uses_a_via_when_blocked(self):
        import xml.etree.ElementTree as ET
        from fusion_offline import router as R
        wall = self.BOARD.replace('y1="2" x2="20" y2="18"', 'y1="-1" x2="20" y2="21"')
        r = R.route(ET.fromstring(wall), "S", (5.03, 10.07), (1,), (35.11, 10.02), (1,), width=0.25, step=0.254)
        self.assertEqual(r.problems, [])
        self.assertEqual(len(r.vias), 2)

    def test_wide_trace_necks_down_at_a_fine_pitch_pad(self):
        import xml.etree.ElementTree as ET
        from fusion_offline import router as R
        # a SOT-23-style pin between two other-net pins 0.95 mm away: a 1.2 mm trace cannot leave it
        board = self.BOARD.replace('<package name="P">', '<package name="Q"><smd name="1" x="0" y="0" dx="1.3" dy="0.6" layer="1"/></package><package name="P">')
        board = board.replace('<element name="A" library="L" package="P" x="5.03" y="10.07"/>',
                              '<element name="A" library="L" package="Q" x="5" y="10"/>'
                              '<element name="N1" library="L" package="Q" x="5" y="10.95"/>'
                              '<element name="N2" library="L" package="Q" x="5" y="9.05"/>')
        board = board.replace('<signal name="X"><wire x1="20" y1="2" x2="20" y2="18" width="0.5" layer="1"/></signal>',
                              '<signal name="Y"><contactref element="N1" pad="1"/><contactref element="N2" pad="1"/></signal>')
        r = R.route(ET.fromstring(board), "S", (5, 10), (1,), (35.11, 10.02), (1,), width=1.2, vias=False, step=0.127)
        self.assertEqual(r.problems, [])
        ws = [w for _, _, w in r.pieces(1.2)]
        self.assertEqual(ws[0], 0.6)                 # narrow at the pin
        self.assertIn(1.2, ws)                       # full width once clear of it
        self.assertEqual(r.legs[0][1][0], (5, 10))

    def test_oblong_pad_is_a_stadium(self):
        import xml.etree.ElementTree as ET
        from fusion_offline import stitch as ST
        board = self.BOARD.replace('<smd name="1" x="0" y="0" dx="1" dy="1" layer="1"/>',
                                   '<smd name="1" x="0" y="0" dx="1" dy="2" layer="1" roundness="100"/>')
        obs, _, _ = ST.board_obstacles(ET.fromstring(board))
        a = next(o for o in obs if o.is_pad and abs(o.data[0] - 5.03) < 1e-6)
        self.assertEqual(a.kind, "seg")
        self.assertAlmostEqual(a.distance(5.03, 10.07 + 1.0), 0.0, places=6)   # end cap
        self.assertAlmostEqual(a.distance(5.03 + 0.5, 10.07), 0.0, places=6)   # side
        self.assertGreater(a.distance(5.03 + 0.5, 10.07 + 1.0), 0.1)          # no square corner


class RouteAllTest(unittest.TestCase):
    """route_all: several connections, a blocked one gets through by ripping a laid route."""

    def test_does_not_tap_copper_the_goal_is_not_joined_to(self):
        import xml.etree.ElementTree as ET
        from fusion_offline import route_all as RA, router as R
        # a lane of net S that no pin reaches yet, lying between A and B: tapping it is not a connection
        board = RouterTest.BOARD.replace('<signal name="X"><wire x1="20" y1="2" x2="20" y2="18" width="0.5" layer="1"/></signal>', '')
        board = board.replace('<contactref element="B" pad="1"/></signal>',
                              '<contactref element="B" pad="1"/><wire x1="12" y1="10" x2="28" y2="10" width="0.25" layer="1"/></signal>')
        res = RA.route_all(ET.fromstring(board), [RA.Conn("s", "S", (5.03, 10.07), (1,), (35.11, 10.02), (1,))], vias=False)
        self.assertEqual(res.failed, {})
        frag, _ = R.fragment(res.root, "S", (35.11, 10.02))
        ends = {p for ab in frag for p in ab}
        self.assertIn((5.03, 10.07), ends)                       # A really reaches B's copper

    def test_rip_up_and_reroute(self):
        import xml.etree.ElementTree as ET
        from fusion_offline import route_all as RA
        board = RouterTest.BOARD.replace('<signal name="X"><wire x1="20" y1="2" x2="20" y2="18" width="0.5" layer="1"/></signal>',
                                         '<signal name="X"><contactref element="C" pad="1"/><contactref element="D" pad="1"/></signal>')
        board = board.replace('<element name="B" library="L" package="P" x="35.11" y="10.02"/>',
                              '<element name="B" library="L" package="P" x="35.11" y="10.02"/>'
                              '<element name="C" library="L" package="P" x="20" y="1.5"/><element name="D" library="L" package="P" x="20" y="18.5"/>')
        root = ET.fromstring(board)
        conns = [RA.Conn("x", "X", (20, 1.5), (1,), (20, 18.5), (1,)), RA.Conn("s", "S", (5.03, 10.07), (1,), (35.11, 10.02), (1,))]
        res = RA.route_all(root, conns, vias=True)
        self.assertEqual(res.failed, {})
        self.assertEqual(set(res.routes), {"x", "s"})
        # the two nets cross: one of them changes layer to get past the other
        self.assertTrue(any(r.vias for r in res.routes.values()))


class PadFitTest(unittest.TestCase):
    """import_placement_from_kicad(fit_pads): place a footprint by where its pads must land."""

    PCB = """(kicad_pcb (gr_rect (start 0 0) (end 20 10) (layer "Edge.Cuts"))
  (footprint "X:SOT" (layer "F.Cu") (at 5 4 90) (property "Reference" "U1")
    (pad "1" smd rect (at -1 1 90) (size 0.6 1) (layers "F.Cu")) (pad "2" smd rect (at 1 1 90) (size 0.6 1) (layers "F.Cu"))
    (pad "3" smd rect (at 0 -1 90) (size 0.6 1) (layers "F.Cu")))
  (footprint "X:SOT" (layer "B.Cu") (at 14 6 30) (property "Reference" "U2")
    (pad "1" smd rect (at -1 -1 30) (size 0.6 1) (layers "B.Cu")) (pad "2" smd rect (at 1 -1 30) (size 0.6 1) (layers "B.Cu"))
    (pad "3" smd rect (at 0 1 30) (size 0.6 1) (layers "B.Cu"))))"""

    def test_pads_in_fusion_frame(self):
        from fusion_offline import kicad_pcb as KP
        p = KP.read_pads(self.PCB)
        # U1 at (5, 4) turned 90 CCW: local (-1, 1) (y down) -> board (5 + 1, 4 + 1) in KiCad -> Fusion y = 10 - 5
        self.assertEqual(p["U1"]["pads"]["1"], (6.0, 5.0))
        self.assertTrue(p["U2"]["bottom"])

    def test_fit_finds_a_turned_footprint_and_a_mirrored_one(self):
        import math
        from fusion_offline import kicad_pcb as KP
        from fusion_offline.stitch import _xf
        pads = KP.read_pads(self.PCB)
        # a Fusion footprint of the same part (KiCad's, y up: 1 (-1, -1), 2 (1, -1), 3 (0, 1)),
        # turned 180 degrees and with its origin moved
        local = {"1": (1.5, 1.0), "2": (-0.5, 1.0), "3": (0.5, -1.0)}
        for ref in ("U1", "U2"):
            f = KP.fit_pose(local, pads[ref]["pads"], pads[ref]["bottom"])
            self.assertLess(f["worst_mm"], 1e-6)
            self.assertEqual(f["mirror"], ref == "U2")
            for n, (px, py) in local.items():
                x, y = _xf(px, py, f["x_mm"], f["y_mm"], f["angle"], f["mirror"])
                self.assertLess(math.dist((x, y), pads[ref]["pads"][n]), 1e-3)


class SmoothTest(unittest.TestCase):
    def test_staircase_becomes_one_run(self):
        import xml.etree.ElementTree as ET
        from fusion_offline import router as R
        root = ET.fromstring(RouterTest.BOARD.replace('y1="2" x2="20" y2="18"', 'y1="2" x2="20" y2="3"'))
        G = R.Grid(root, "S", 0.25, None, 0.127, (5.03, 10.07))
        stair = [(5.0, 10.0), (6.0, 10.0), (6.5, 10.5), (7.5, 10.5), (8.0, 11.0), (12.0, 11.0)]
        out = R._smooth(G, 1, stair)
        self.assertLess(len(out), len(stair))
        self.assertEqual((out[0], out[-1]), (stair[0], stair[-1]))
        self.assertTrue(all(R._octi(a, b) for a, b in zip(out, out[1:])))


class BusTest(unittest.TestCase):
    def test_lanes_offset_and_trimmed(self):
        import xml.etree.ElementTree as ET
        from fusion_offline import bus as BU
        root = ET.fromstring(RouterTest.BOARD.replace('y1="2" x2="20" y2="18"', 'y1="19" x2="20.5" y2="19"'))
        plan = BU.plan_bus(root, ["A", "B"], [(30, 6), (8, 6)], 0.8, 0.25, 16,
                           {"A": [(28, 9), (10, 9)], "B": [(20, 9), (12, 9)]})
        self.assertEqual(plan["problems"], [])
        a, b = plan["lanes"]
        self.assertEqual({p[1] for p in a["points"]}, {6.0})
        self.assertEqual({round(p[1], 3) for p in b["points"]}, {5.2})          # 0.8 to the left of travel
        self.assertLess(max(p[0] for p in b["points"]), max(p[0] for p in a["points"]))   # trimmed to its pins


class ClusterPlaceTest(unittest.TestCase):
    """place_clusters' solver: a pull-up lands on its pin's escape line, pin end toward the pin."""

    BOARD = """<eagle><drawing><board>
<plain><wire x1="0" y1="0" x2="40" y2="0" width="0" layer="20"/><wire x1="40" y1="0" x2="40" y2="30" width="0" layer="20"/>
<wire x1="40" y1="30" x2="0" y2="30" width="0" layer="20"/><wire x1="0" y1="30" x2="0" y2="0" width="0" layer="20"/></plain>
<libraries><library name="L"><packages>
<package name="IC"><smd name="1" x="-2" y="1" dx="1" dy="0.5" layer="1"/><smd name="2" x="-2" y="-1" dx="1" dy="0.5" layer="1"/>
<smd name="3" x="2" y="0" dx="1" dy="0.5" layer="1"/></package>
<package name="R"><smd name="1" x="-0.75" y="0" dx="0.8" dy="0.9" layer="1"/><smd name="2" x="0.75" y="0" dx="0.8" dy="0.9" layer="1"/></package>
</packages></library></libraries>
<elements><element name="U1" library="L" package="IC" x="20" y="15"/><element name="R1" library="L" package="R" x="5" y="5"/></elements>
<signals><signal name="SIG"><contactref element="U1" pad="1"/><contactref element="R1" pad="2"/></signal>
<signal name="VCC"><contactref element="U1" pad="3"/><contactref element="R1" pad="1"/></signal></signals>
</board></drawing></eagle>"""

    def test_pullup_on_escape_line(self):
        import xml.etree.ElementTree as ET
        from fusion_offline import cluster_place as CP
        root = ET.fromstring(self.BOARD)
        P = CP.solve(root, [{"anchor": "U1", "members": ["R1"]}], rails={"VCC"})
        self.assertIn("R1", P.moves)
        parts, _ = CP.read_parts(CP.applied(root, P))
        r1 = parts["R1"]
        near = r1.to_board(*r1.pads["2"][:2])      # the SIG pad
        far = r1.to_board(*r1.pads["1"][:2])
        self.assertAlmostEqual(near[1], 16.0, places=3)        # on U1.1's row
        self.assertLess(near[0], 17.5)                          # left of the IC (pin 1 escapes left)
        self.assertLess(far[0], near[0])                        # rail end further out
