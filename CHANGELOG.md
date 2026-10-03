# Changelog

## 0.2.0 (unreleased)

First public release.

### Connection
- Talks to Fusion through Fusion's built-in MCP server (Preferences > General
  > API > Fusion MCP Server): nothing to install inside Fusion. The bundled
  add-in remains as a fallback (`fusion-electronics-mcp install-addin`).
- `fusion-electronics-mcp doctor` checks the setup; `tools` lists the tools.
- Every write is verified by reading the design back and undone on mismatch;
  one undo step per tool call; Fusion's dialogs are answered safely (Windows);
  keyboard focus is handed back if Fusion takes it.

### Tools (65)
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
