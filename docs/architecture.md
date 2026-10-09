# Architecture and Fusion behaviour

How the server talks to Fusion, and the Fusion Electronics behaviours every
tool is built around. Verified on Fusion 2705.1.15 (Windows, embedded Python
3.14) unless noted.

## Pieces

```
MCP client  <-stdio->  MCP server (this package, outside Fusion)
                          |  TCP 127.0.0.1, random port + token (bridge.json)
                          v
                       Fusion add-in (FusionElectronicsMCP)  -- fixed operations only
                          |  CustomEvent -> Fusion's main thread
                          v
                       adsk.electron API, exportManager, Electron.run / Electron.runScript
```

- **The add-in is thin and fixed.** It exposes named operations (export, run
  commands, save, open/close documents, a few API reads). It never executes
  code sent to it, and `run` refuses EAGLE verbs outside an allow list.
- **Reads** come from Fusion's official EAGLE XML export (`exportManager`),
  parsed offline (`fusion_offline`). One export is a complete, consistent
  snapshot of the board or schematic in about a second.
- **Writes** are EAGLE commands sent through the undocumented text commands
  `Electron.run <commands>` and `Electron.runScript <file.scr>`. Their return
  value is always empty, even for unknown commands, so **every write tool
  verifies by exporting again** and undoes the change if the result is wrong.
- **One tool call = one undo step**: the add-in wraps a call in
  `beginDesignChange` / `endDesignChange`.
- **Offline package** (`fusion_offline`, stdlib only): parsers, JLC BOM/CPL,
  SI estimates, the schematic review rules, routing geometry (pairs, stitching,
  placement scoring), gerber checks, JLC orientation. Testable without Fusion.

## Network

127.0.0.1 only, except `check_jlc_orientation(fetch=true)`, which downloads
footprints from easyeda.com (see `server/fusion_mcp/easyeda.py`: cached
forever, at least 15 s between requests, back-off after a 403, an honest user
agent). The service tools (`request_design_review`, `get_assembly_quote`) are
stubs that make no network calls.

## Dialogs

Fusion reports errors and asks questions with modal dialogs and notification
toasts, which block automation. The server runs a watchdog during every call
(Windows UI Automation through PowerShell, `dialogs.py`):

- message boxes get a safe answer (Cancel / No / Close / OK, never Yes) unless
  the calling tool registered an expected (pattern, button) pair;
- forms (e.g. the "Name" form when renaming a multi-segment net) are answered
  by registered actions, otherwise cancelled;
- toasts are read and returned as `Fusion said: ...` (they are Fusion's only
  error channel for commands).

While a call runs, keyboard focus is handed back to the window the person was
using if Fusion grabs it (`FUSION_MCP_KEEP_FOCUS=0` turns this off).
Mac: the dialog watchdog and focus guard are Windows-only so far; tools work,
but a dialog on a Mac needs a person to close it.

## Fusion behaviours the tools depend on

Commands and coordinates
- Quote names: `ROTATE R90 R1` reads `R1` as a 1-degree angle.
- MIRROR, PACKAGE, NAME (and pick-based DELETE / CHANGE) only work at a
  coordinate; EAGLE picks the nearest visible object, so pick-based edits show
  only the target layer first and restore the visible layers after.
- Every write is wrapped in `GRID MM 0.0001; ... GRID LAST;`: MOVE snaps relative
  to the part's off-grid position, and a bare `GRID MM` changes the person's grid.
- `WIRE` uses the current bend style even between explicit points: a segment
  that is not exactly 0/45/90 degrees is split into a micro-jog (polygons too).
  Routing geometry is therefore made exactly octilinear before it is written.
- A wire end connects to a pad only at the pad origin; a few microns off leaves
  an air wire.
- Multi-pad `CONNECT` must be one line per pin (`CONNECT 'G$1.A' '1 3'`); a
  second CONNECT for the same pin replaces the first.
- In the library editor, `MOVE` through `Electron.run` silently did nothing;
  the same commands through `Electron.runScript` work.

Layers and rules
- Copper layer numbers: 2-layer Top 1 / Bottom 304; 4-layer 1, 2, 303, 304.
  Layer 16 is an unused inner "Route16", not the bottom. The classic export
  renumbers to 1, 2, 15, 16.
- Pad copper = max(library diameter, drill + 2 x clamp(rv x drill, rlMin, rlMax))
  from the design rules; via copper likewise.
- Design rules (V2) live in the design; the working copy refreshes only after a
  save. The API cannot write them: people load `.edru` files in the DRC dialog.
- Net classes (2705.1.25): a class lives in the board's `<classes>` (number,
  name, width, drill, class-to-class clearance) and in the V2 rules as a
  "Minimum Copper Width", "Minimum Drill Size" and "Copper Clearance" rule with
  `onescope="classes=N"` (clearance also `otherscope="classes=N"`), ahead of
  the built-in rules. The legacy width fills in only after a save. The
  `CLASS` command made rules that hit all copper and survived UNDO, so
  classes are made by loading a generated `.edru` (or the Net Classes dialog);
  nets are put in a class with `CHANGE CLASS name (x y)` on a schematic wire
  (`CHANGE CLASS name net` fails). A class width rule also covers the class's
  pads.
- Loading an `.edru` replaces all rules; the working copy the server reads
  them from is the last saved state, so rule files are built only from a
  saved design.
- `SET POLYGON_RATSNEST ON` is needed for pours to fill; `CHANGE THERMALWIDTH`
  sets relief spokes (Fusion's default 0.1524 mm is thin).
- Never wrap AUTO / DRC / ERC / RATSNEST / SET in a design change: wrapping AUTO
  broke undo for the whole session.

Sessions and safety
- Each Claude session runs its own server; an OS file lock in the per-user data
  folder lets one at a time talk to Fusion, held for a write's whole export,
  write, read-back and undo.
- Writes take `design=` and check Fusion's active design just before writing:
  another session (or the person) can switch designs between calls, and part
  names like R1 exist on most boards.
- Command numbers are written to 0.1 um (`:g` kept 6 significant digits, so
  coordinates over 100 mm were rounded).

Autorouter (AUTO)
- Opens a "Routing Variants" dialog and starts variants at once. End Job applies
  the variant loaded in the board, not the highlighted row: invoke the chosen row.
- The TopRouter variant can hang at the share already routed; the tool runs the
  job with it off (control file `TopRouterVariant = 0`) and restores the design's
  settings afterwards.
- It asks whether to run when a layer with objects (an inner plane) is not
  enabled for routing; answering Yes keeps signals off the plane.

Libraries
- Fusion upper-cases library object names.
- Placing a part from a library saved moments earlier in the same session
  crashed Fusion: save, close the library, then place.
- `Electron::UpdateDesignFromAllLibraries` brings library changes (attributes,
  3D packages) into a design without dialogs.
- 3D packages: `Electron.Create3DPackage <footprint xml>`, import the STEP, save
  the Package3D document through the API before Finish (`Package3DStop`).

Manufacturing
- Fusion's CAM (`Electron.mfgexport`) reports success but writes nothing when
  driven headlessly: people export gerbers from the CAM dialog, and
  `check_gerbers` validates the zip against the design.
- Fusion strokes rounded SMD pads in paste/mask instead of flashing them, so
  paste is checked pad by pad, not by flash count.
- JLC places parts with its EasyEDA footprint; the CPL needs `JLC-ROTATION` /
  `JLC-X-OFFSET` / `JLC-Y-OFFSET` where the library footprint differs. Library
  attributes win over automatic derivation.

## Tests

- `tests/test_offline.py`, `tests/test_server.py`: unit tests, no Fusion needed.
- `tests/live/`: scripts that drive a running Fusion (end-to-end board build,
  dialog catalogue, validation over the designs in the active project). Their
  outputs stay outside the repository.
