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
        self.assertEqual(r["parts"]["C1"]["angle"], 90.0)        # bottom 270 -> mirrored 90


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
