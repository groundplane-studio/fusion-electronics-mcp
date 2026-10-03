# fusion-electronics-mcp

An MCP server that lets AI agents read, review and edit PCB designs in
Autodesk Fusion Electronics: schematic capture, placement, impedance-controlled
routing, pours and stitching, design checks, and JLCPCB manufacturing outputs.

Every change is verified by reading the design back from Fusion and is undone if
the result does not match. Each tool call is one undo step in Fusion, and
nothing is saved until you ask.

## What it can do

- **Review**: board and schematic summaries, parts, nets, DRC and ERC, a
  schematic review (unconnected power pins, boxes drawn as nets, stray wires,
  nets that need labels), and a picture of the board (`render_board`).
- **Signal integrity**: differential pairs, length matching, and impedance
  estimates (IPC-2141 closed form) against the design's real layer stackup.
- **Schematic capture**: place parts, connect pins (labels added
  automatically), rename nets or single segments, add sheets with your frame.
- **Placement**: move and rotate parts, import placement or netlists from a
  KiCad board, score placements and suggest moves.
- **Routing**: impedance-controlled differential pairs along a path the agent
  chooses (`route_pair`: coupled traces, 45-degree corners, rounded length
  tuning, checked against clearances before anything is written); traces,
  vias, fanouts; copper pours with thermal reliefs; via stitching that keeps
  away from chosen nets; Fusion's autorouter for the rest.
- **Parts and libraries**: build parts from a JSON definition into a Fusion
  library (pads and pin connections verified), attach 3D models, update a
  design from its libraries.
- **JLCPCB**: BOM and CPL (pick and place) files; placement orientation checked
  against the footprint JLC places each part with; a check of your gerber zip
  against the design (every layer, the outline, every drilled hole, paste and
  mask on every SMD pad).

`fusion-electronics-mcp tools` lists all 65 tools.

## Requirements

- Autodesk Fusion (desktop) with Electronics, on Windows or macOS
- Python 3.12 or newer
- An MCP client, for example Claude Code or Claude Desktop

## Install

```bash
pip install "fusion-electronics-mcp[render] @ git+https://github.com/groundplane-studio/fusion-electronics-mcp"
```

The `[render]` extra adds matplotlib for `render_board`; leave it out if you do
not need board pictures.

### Connect it to Fusion

The server talks to Fusion through **Fusion's own MCP server**, so nothing has
to be installed inside Fusion. Turn it on once: in Fusion, open
**Preferences > General > API**, tick **Fusion MCP Server**, then Apply and OK.

If you cannot use Fusion's MCP server, install the bundled add-in instead and
run it from **Utilities > Add-Ins** (tick Run on Startup):

```bash
fusion-electronics-mcp install-addin
```

### Add it to your MCP client

Claude Code:

```bash
claude mcp add fusion-electronics -- fusion-electronics-mcp
```

Claude Desktop (`claude_desktop_config.json`; use the full path to the command
if it is not on your PATH):

```json
{
  "mcpServers": {
    "fusion-electronics": { "command": "fusion-electronics-mcp" }
  }
}
```

### Check the setup

With Fusion running:

```bash
fusion-electronics-mcp doctor
```

## Using it

Open a design in Fusion, then ask your agent things like:

- "Review the schematic and list anything that looks wrong."
- "Check the impedance and length matching of the USB and Ethernet pairs."
- "Route USB_DP/USB_DN as a 90 ohm pair from J2 to J4, matched to 0.1 mm."
- "Add a GND pour on both inner layers and stitch it, keeping vias 0.6 mm from the pairs."
- "Export the JLCPCB BOM and CPL, and check the gerber zip in my Downloads."

Gerbers: Fusion's CAM cannot be driven from outside, so export the gerber zip
from Fusion's CAM Processor yourself; `check_gerbers` then checks it against the
design before you upload it.

## Safety and privacy

- **No telemetry.** The server talks to Fusion on 127.0.0.1 only.
- **One exception, on request**: `check_jlc_orientation` with `fetch=true`
  downloads footprints from easyeda.com (cached forever, at least 15 s between
  requests). Nothing else leaves your machine. The service tools
  (`request_design_review`, `get_assembly_quote`) are stubs and make no network
  calls.
- **Fusion's questions get safe answers.** Fusion asks things with pop-up
  dialogs; a watchdog answers the ones a tool expects and cancels the rest
  (Cancel / No, never Yes). Windows only so far: on macOS a dialog waits for you.
- **Your keyboard stays yours.** If Fusion grabs focus while a tool runs, focus
  goes back to the window you were typing in (`FUSION_MCP_KEEP_FOCUS=0` turns
  this off).
- When you are in the middle of a command in Fusion, edits wait for you
  (Fusion refuses them) instead of cancelling your command.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FUSION_MCP_TRANSPORT` | `auto` | `builtin` (Fusion's MCP server), `addin`, or `auto` (built-in when it answers) |
| `FUSION_MCP_BUILTIN_URL` | `http://127.0.0.1:27182/mcp` | Fusion's MCP server address |
| `FUSION_MCP_LIBRARY` | per-user data folder | component library (part JSON files) |
| `FUSION_MCP_SHEET_FRAME` | none | frame for new schematic sheets, `DEVICE@LIBRARY` |
| `FUSION_MCP_KEEP_FOCUS` | `1` | hand keyboard focus back when Fusion takes it |
| `FUSION_MCP_EASYEDA_CACHE` | per-user data folder | EasyEDA footprint cache |
| `FUSION_MCP_EASYEDA_INTERVAL_S` | `15` | minimum seconds between EasyEDA requests |

The per-user data folder is `%LOCALAPPDATA%\fusion-electronics-mcp` on Windows
and `~/Library/Application Support/fusion-electronics-mcp` on macOS.

## Known limits

- Gerbers are exported from Fusion's CAM dialog by hand (checked by the tool).
- Impedance figures are closed-form estimates, typically within about 10%;
  confirm critical pairs with your fab's calculator.
- The dialog watchdog and focus guard are Windows-only so far.
- Built and tested against Fusion 2705.1.15. Fusion's undocumented command
  interface can change between releases; `docs/architecture.md` lists the
  behaviours the tools depend on.

## Development

```bash
git clone https://github.com/groundplane-studio/fusion-electronics-mcp
cd fusion-electronics-mcp
pip install -e ".[render]"
python -m unittest discover -s tests
```

`docs/architecture.md` explains how the server, Fusion and the add-in fit
together. `tests/live/` holds scripts that drive a running Fusion.

## License

MIT. See `LICENSE`.
