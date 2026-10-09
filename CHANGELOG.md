# Changelog

## Unreleased (0.3.0)

From laying out the PoE Magnetics Test Board (RJ45, magnetics, CM4) in
October 2026. Every write stays verified and one undo step; new write tools
take `design=` and refuse when Fusion's active design is another one.

### Placement
- `render_board(courtyards, silkscreen, pad_numbers)`: courtyards with
  overlaps filled red and touching pairs orange (touching is not a
  violation); module outlines that hold several parts are skipped.
- `check_placement`: courtyard overlaps, pad gaps against the rules,
  silkscreen on a neighbour's pads, series parts off their pad's row, and
  tidiness (off grid, almost aligned, mixed rotation, uneven pitch).
- `move_parts`: a batch of moves and rotations in one undo step, undone as a
  whole if any part lands elsewhere or a neighbour is pushed; `dry_run`
  pictures and a report of what the moves introduce or clear. `move_part` and
  `rotate_part` take `dry_run` and now return that report.
- `place_inline`: series parts in one column (or row) on the rows of the pads
  they connect to.
- `tidy_placement`: grid, rows and even pitch for parts that do not matter
  electrically; pair parts move only as one group, net-class parts,
  decoupling caps, crystals and isolation bridges are only nudged; nothing
  moves for silkscreen; rework gaps never shrink.

### Net classes and design rules
- `list_net_classes`, `set_net_class` (writes a rule file to load; never the
  CLASS command), `assign_net_class` (picks nets on schematic wires).
  Values come from each class's design rules first; warns about rules on all
  copper and width rules wider than the class's pads.
- `get_design_rules` reports teardrops, the pair rule and built-in
  clearances; `edit_design_rules` writes a rule file with changes.
- `jlc_limits`: JLCPCB's published limits, cited and dated.

### Routing and DRC
- `route_pair`: coupled via-pair layer changes, vias in heads, tuning on any
  layer, arcs in the centreline, meander style and settings, JLC same-net
  spacing, clearances DRC enforces (class clearances, margin), squeezes
  explained and `neck_down`, coupled detours to lengthen both traces
  (`add_length_mm`) and `group=` targets.
- DRC before and after every write: the reply lists only new errors
  (`FUSION_MCP_DRC_AFTER_WRITES`, `FUSION_MCP_UNDO_ON_NEW_DRC`); `check_drc`
  summarises and diffs.
- Length groups (`set_length_group`, `check_length_groups`) with paths
  through series parts; `length_tolerances`, a cited table of typical
  tolerances.
- Current: `set_net_current`, `size_for_current` (IPC-2221 per layer from the
  real copper), `check_current`, `current_ratings` and `set_part_rating`
  (sizing capped by the weakest part); `route_trace` / `route_net` size from
  a net's current when no width is given.

### Schematic
- `straighten_labels` and `match_labels` for part labels.
- `review_schematic`: supply symbols on other nets, overbar markup;
  `list_nets` / `get_net` show overbar names readably.

### Reliability
- One server process at a time talks to Fusion (a lock shared by all
  sessions' servers; `FUSION_MCP_LOCK_WAIT_S`).
- Replies say when the running server is older than the code on disk.
- Command numbers are exact to 0.1 um (they kept only 6 significant digits).
- Lengths from `list_nets`, `get_net` and `check_length_match` no longer
  count air wires.

### Parts and copper
- `insert_library_part` reuses a package the library already has when its pads are the same
  (two ICs on the same JLC SOT-23-6, for example), instead of refusing the second part. A
  different footprint under an existing package name is still refused.
- New `delete_copper` tool: deletes chosen vias, trace segments and pours of one net and leaves
  the rest alone. To redraw a pour that has stopped filling properly, delete it and `add_pour` it
  again.
- JLC rule files: every same-signal clearance (the SMD-SMD and SMD-pad rules, same-layer via
  spacing) is at or under the smallest different-signal one (0.1 mm), so Fusion's plausibility
  prompt no longer blocks DRC. `edit_design_rules` lowers them the same way and says so.
  Rebuilding the rule files keeps their layer ids, so a rebuild only changes real values.

### Fixes before release
- Lengths count via barrels (the depth between the layers each via joins), so a pair where one
  side changes layer reports its real skew. `route_pair(group=)` honours the group's `measure`
  and `follow_series` and no longer counts old copper of a re-routed pair; between-pair tuning
  lands on the target instead of overshooting by half the skew.
- Neck-down works after rounded tuning (arcs no longer stop it).
- `set_length_group(preset=)` picks exact names first, so DDR4 and HDMI can be chosen.
- `size_for_current` sizes for the net's full current and warns about parts rated below it;
  only parts in the current path count (not TVS diodes or decoupling caps). Vias are checked
  per layer change, at 0.8 A per via by default (IPC-2221 on a 0.3 mm barrel, 25 um plating).
  PoE Type 4 is sized for 0.96 A.
- `tidy_placement` leaves parts alone when a trace ends anywhere on their pads, keeps P/N pair
  groups together, keeps `place_inline` alignment, skips locked parts, and re-checks after its
  last pass.
- A write is not sent when Fusion did not answer the DRC before it. DRC after writes runs only
  for board writes and puts the schematic editor back. The shared lock stays held until a
  request that timed out has really finished, and multi-step tools hold it throughout.
- Per-design data (length groups, net currents) is saved atomically under the shared lock, in
  a file named by a hash of the design, so similar names no longer share data.
- Fusion's built-in server: a write is never re-run because its own output contained "not
  initialized"; the whole chain of stdout wrappers is repaired.
- `open_design` matches the folder as well as the name of an already-open design.
- `open_design` by name alone tries the folder the design was last found or seen open in before
  the project's top folder (listing a big project's top folder can time out), and when it does
  time out it names the folders it knows (add-in 0.14.3).
- Only one copy of the add-in runs in a Fusion process (say the installed copy and one added
  from a source folder): a second copy refuses to start and says where the running one is, and
  stopping a copy no longer deletes the running copy's connection file (add-in 0.14.4).
- Hardening: rule files are written only to the rules folder, the temp folder, Downloads or
  Documents; the add-in allows AUTO SAVE / LOAD only for files in the temp folder ;
  previews go to the per-user data folder; the autorouter's temp folder is removed after a run;
  part ratings are saved atomically under the shared lock.


## 0.2.0 (2026-10-04)

First public release.

### Connection
- Talks to Fusion through the bundled add-in (`fusion-electronics-mcp
  install-addin`; 127.0.0.1 only, token-gated). Fusion's built-in MCP server
  is the fallback when the add-in is not running; saving and pushing to the
  3D PCB refuse to run on it, since it drops library saves and cancels
  Fusion's dialogs.
- `fusion-electronics-mcp doctor` checks the setup; `tools` lists the tools.
- Every write is verified by reading the design back and undone on mismatch;
  one undo step per tool call; Fusion's dialogs are answered safely (Windows);
  keyboard focus is handed back if Fusion takes it.

### Schematics drawn as blocks
- `import_netlist_from_kicad` draws a reviewable schematic by default
  (`style="blocks"`): each IC or connector with its passives wired to it
  (series parts inline, caps and pull-ups hanging off the net, LED and FET
  drivers stacked, bootstrap caps bridging their pins), labels only on nets
  that leave a block, ground and rails as power symbols, blocks packed onto
  framed sheets. Parts are added once to read their real symbols, the layout
  is checked offline (every pin, no shorts, no piece of a net joined only by
  name), an HTML preview is written, and every pin is checked against the
  netlist after drawing. `preview_only=true` stops before drawing.
- `pad_map` translates KiCad pad names to library pad names (one-pad parts map
  themselves).

### EasyEDA etiquette across processes
- Every request to EasyEDA goes through one limiter shared by all processes
  on the machine (a clock file in the cache folder plus a lock): at least
  15 s apart with a little jitter, and a 15 minute back-off after a 403 or
  429, so two sessions or a library worker never burst.

### Design rules and stackups
- Rule sets and stackups for every JLCPCB impedance stackup (16 four-layer,
  14 six-layer) and a 2-layer 1.6 mm board, each as a .edru (rules plus
  stackup) and a .estackup. Built by `tools/gen_jlc_stackups.py` from JLC's
  published tables (thicknesses and dielectric constants as JLC states them;
  stacked plies combined in series; a stackup with a material JLC gives no
  dielectric constant for is left out). `list_design_rules` shows them and
  can copy them where Fusion's file dialog reaches. 4- and 6-layer files load
  in Fusion's DRC dialog and Layer Stack Manager (checked). The 2-layer core's Er
  (4.5) is the 2-layer value on JLC's capabilities page.
- Rule values checked against JLC's published capabilities (2026-10-04) and
  raised where they fell short: SMD pad to pad 0.15 mm, hole to hole 0.2 mm,
  PTH annular ring at least 0.18 mm, via ring at least 0.1 mm (via hole to
  track 0.2 mm). Minimum drill is 0.3 mm: JLC drills 0.15 to 0.2 mm but charges
  more for it, so lower it only when a design needs it.

### Opening designs never walks the project
- `open_design` / `open_library` bring an already open document forward
  without listing anything, and otherwise look only in the active project's
  top folder or the one folder you name, stopping after a few seconds.
  Walking a big project's folder tree froze Fusion.

### Your own parts library
- `create_library_part` turns a KiCad footprint (KiCad's own, or JLCPCB's
  exported with easyeda2kicad) into a part in the server's component library,
  with a generated symbol and the JLCPCB number, ready for
  `insert_library_part`. The README walks through setting up a library.

### One symbol style for every two-pin passive
- Parts built into a library (`insert_library_part`) draw resistors,
  capacitors, inductors, ferrites, fuses, crystals, diodes, LEDs and TVS with
  the server's standard symbols, whatever symbol the part came with from
  EasyEDA or KiCad (style inferred from the prefix and description; `"style":
  false` keeps the part's own).

### 3D models that land the right way up
- `attach_3d_model` refuses a model whose body would sit below the board
  (nothing is saved): vendor STEPs are often Y-up, and KiCad's 3D rotation
  signs are the opposite of Fusion's. More of the model below the board than
  above counts as upside down (a flipped through-hole part still pokes its
  pins up). A replacement model's file gets a versioned name.
- `update_from_libraries(refresh_parts=...)` re-pulls parts Fusion's own update
  leaves on an old 3D model (it reports nothing to do), answering Fusion's
  "update device set?" question, and reports every part's 3D model afterwards.
- `push_3d` brings the board into its 3D PCB (creating it the first time and
  answering the Push dialog); `check_3d_models` lists any part whose model is
  on the wrong side, wherever Fusion nests it.
- A question from Fusion that a tool did not expect now fails the call instead
  of quietly getting the safe answer and carrying on.

### Placement and routing, the way a person does it
- `place_clusters`: passives placed around the part they serve by rule (the
  user's patterns; ported from Groundplane's KiCad solver, plus chains and
  connector pins at a board edge escaping into the board). Preview first.
- The routing order is a set of tools: `add_pour` (priority `rank`),
  `ground_vias` (a via beside every ground pad, never under a part, 0.3 mm
  drill), `route_close` (every short hop at once), `lay_bus` (ordered parallel
  lanes along a path, staggered ends so tap vias never collide),
  `route_remaining` (the rest, with rip-up and reroute), plus `route_trace` and
  `route_net` for single connections and nets.
- New router (numpy grid, A* with 45-degree moves): about 10x faster than the
  first version on a two-layer power backplane benchmark, pays to cut other nets' power
  pours (cuts dropped from 109 mm to 2 mm) and to run through connector pin
  fields, taps into a net's existing copper, smooths the grid's jogs, and every
  write is checked for connections and for top/bottom traces meeting without a
  via (Fusion's airwire count can miss that).
- Neck-down: a power trace wider than a fine-pitch pad narrows to the pad's
  width close to it and keeps its full width elsewhere.
- Oblong pads (round-ended SMDs, long through-hole pads) are modelled by
  their real shape, so traces can pass their ends at the real clearance.
- Optional `hug` cost in the router: traces prefer the lane one clearance
  beside existing traces, so long runs form bundles.
- Tested on a second board, a 4-layer carrier with 0.65 mm pitch QFN and
  VSSOP parts and 0.15 mm rules: 84 of 84 connections in about 13 s, no
  clearance errors, 10% shorter than the board's Freerouting routing.
- `import_placement_from_kicad` places each part by where its pads must land
  (`fit_pads`, on by default), so a Fusion footprint whose origin or pin-1
  orientation differs from KiCad's still lands pad for pad; parts whose pads
  still disagree by more than 0.1 mm are reported.
- Fixed: a KiCad bottom-side part at angle R is Fusion's mirrored part at
  180 - R, not R + 180 (the two agree only at 0 and 180; at 90 and 270 the
  old rule swapped the pads). Checked against pcbnew pad positions.
- Block schematics: nets named like supplies (3V3_AUX, +5V, V12) are drawn as
  rails however few pins they have; a decoupling cap joins the block of the
  IC pin it sits next to on the board; output caps follow the regulator that
  makes the rail; a sub-circuit counts each main-part pin it touches once, so
  a buck's output stage stays with the buck, not the connector it feeds;
  spare pins on named nets get a label; a final pass wires any pin left
  unwired and labels any net piece joined only by name.
- Power pours are close to a wall for other nets' traces, and no via is put
  through another net's outer power pour (inner planes still take vias).
- `lay_bus` reports a lane whose taps would land in another net's power pour.
- `remove_stubs` finds dangling ends from the copper itself (Fusion's DRC
  misses a bus lane's tail past its last tap), cuts a tail back to its last
  tap, and undoes any change that adds an unrouted connection.
- Fixed: `route_remaining` could tap copper of the net that was not yet joined
  to the pin it was routing to (a bus lane no pin had reached), leaving the
  connection open while reporting it routed. Taps are now limited to copper
  joined to the goal, and joins part-way along a trace count as joined.
- Block schematics: wire ends are snapped onto pins after a block is moved
  (rounding could leave one 0.0001 mm off, which Fusion does not connect).
- `stitch_vias` keeps off parts and out of other nets' pours.
- `import_routing_from_kicad` copies a KiCad board's tracks and vias.
- Writes containing editor settings (SET) are one undo step again.

### Tools (77)
- Review: board/schematic summaries, parts, nets, DRC, ERC, schematic review
  rules, `render_board`.
- Signal integrity: differential pairs, length matching, impedance estimates
  from the design's stackup.
- Schematic capture: place parts, connect pins with labels, rename nets or a
  single segment, label split nets, new sheets with a frame.
- Placement: move/rotate, KiCad placement and netlist import, placement
  scoring and suggestions.
- Routing: `route_pair` (coupled, exact 45-degree geometry, rounded length
  tuning, pre-checked clearances), traces, vias, fanouts, pours with thermal
  reliefs, via stitching with per-net keep-away, autorouter (works around
  Fusion's hanging TopRouter variant), stub and via cleanup, rip-up.
- Libraries: build parts from JSON (pads and connections verified), 3D models,
  update a design from its libraries; KiCad footprint import keeps pin-1 marks.
- JLCPCB: BOM, CPL with orientation corrections (library attributes or
  derived from JLC's footprints), `check_jlc_orientation`, `check_gerbers`.
