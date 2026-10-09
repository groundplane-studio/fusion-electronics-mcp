"""Fusion Electronics MCP bridge add-in.

A thin, fixed set of primitives served over 127.0.0.1 for the external MCP
server (see docs/protocol.md). All design logic lives in the server; this
add-in only touches Fusion, always on the main thread.

    server --TCP 127.0.0.1 (token)--> socket thread
        --app.fireCustomEvent(job id)--> main-thread handler --> Fusion
        <--threading.Event-- result JSON

There is no arbitrary code execution. EAGLE commands pass an allowlist of
verbs that edit the open design only: no RUN (ULPs), SCRIPT, WRITE, EXPORT,
CAM, OPEN or other file/system commands.

Connection info (port + per-session token) is written to
%APPDATA%/fusion-electronics-mcp/bridge.json and removed on stop.
Stdlib only; runs on Fusion's embedded Python.
"""

import contextlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import tempfile
import threading
import time
import traceback
import uuid

import adsk.core  # type: ignore
import adsk.electron  # type: ignore

ADDIN_VERSION = "0.14.2"
PROTOCOL = 1
EVENT_ID = "fusion_electronics_mcp_bridge"
DEFAULT_TIMEOUT = 60.0
INFO_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "fusion-electronics-mcp")
INFO_PATH = os.path.join(INFO_DIR, "bridge.json")
WORK_DIR = os.path.join(tempfile.gettempdir(), "fusion-electronics-mcp")
U_PER_MM = 320000.0

# EAGLE command verbs allowed through `run` / `run_script`.
ALLOWED_VERBS = {
    "ADD", "ATTRIBUTE", "AUTO", "CHANGE", "CIRCLE", "CLASS", "CONNECT", "DELETE", "DESCRIPTION",
    "DISPLAY", "DRC", "EDIT", "ERC", "GATESWAP", "GRID", "HOLE", "INVOKE", "JUNCTION", "LABEL", "LAYER", "MIRROR",
    "MOVE", "NAME", "NET", "PACKAGE", "PAD", "PIN", "PINSWAP", "POLYGON", "PREFIX", "RATSNEST",
    "RECT", "REDO", "REPLACE", "RIPUP", "ROTATE", "ROUTE", "SET", "SIGNAL", "SMASH", "SMD",
    "SPLIT", "TECHNOLOGY", "TEXT", "UNDO", "VALUE", "VIA", "WIRE",
}
# EDIT is only allowed for library objects (.pac/.sym/.dev) and schematic
# sheets (.s<n>), never for files.
_EDIT_OK = re.compile(r"^EDIT\s+('?[^';]+\.(pac|sym|dev)'?|\.s\d+)\s*$", re.I)

_app = None
_ui = None
_event = None
_handler = None
_server = None
_token = ""
_stop = threading.Event()
_jobs = {}
_jobs_lock = threading.Lock()


class BridgeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# command validation


def split_commands(text):
    """Split an EAGLE command string on ';' outside single quotes."""
    out, buf, q = [], [], False
    for ch in text:
        if ch == "'":
            q = not q
        if ch == ";" and not q:
            s = "".join(buf).strip()
            if s:
                out.append(s)
            buf = []
        else:
            buf.append(ch)
    s = "".join(buf).strip()
    if s:
        out.append(s)
    return out


_AUTO_FILE = re.compile(r"^\s*AUTO\s+(SAVE|LOAD)\b\s*(.*?)\s*;?\s*$", re.I)


def _auto_file_ok(cmd):
    """AUTO SAVE / LOAD read and write autorouter files: only under the system temp folder."""
    m = _AUTO_FILE.match(cmd)
    if not m:
        return True
    path = m.group(2).strip().strip("'\"")
    if not path:
        return False
    tmp = os.path.realpath(tempfile.gettempdir())
    real = os.path.realpath(path)
    return real.startswith(tmp + os.sep)


def validate(text):
    cmds = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cmds += split_commands(line)
    for c in cmds:
        verb = c.split(None, 1)[0].upper()
        if verb not in ALLOWED_VERBS:
            raise BridgeError("command_not_allowed", f"EAGLE command {verb!r} is not allowed by the bridge")
        if verb == "EDIT" and not _EDIT_OK.match(c):
            raise BridgeError("command_not_allowed", "EDIT is only allowed for library .pac/.sym/.dev objects")
        if verb == "AUTO" and not _auto_file_ok(c):
            raise BridgeError("command_not_allowed", "AUTO SAVE / LOAD is only allowed for files in the temp folder")
    return cmds


# ---------------------------------------------------------------------------
# helpers (main thread only)


def _kind_of(doc):
    t = doc.objectType if doc else ""
    for k in ("Board", "Schematic", "Library", "EcadDesign"):
        if t.endswith("::" + k + "Document"):
            return {"Board": "board", "Schematic": "schematic", "Library": "library", "EcadDesign": "design"}[k]
    return "other"


def _docs():
    return [_app.documents.item(i) for i in range(_app.documents.count)]


def _doc_info(d):
    info = {"name": d.name, "kind": _kind_of(d), "modified": d.isModified, "saved": d.isSaved,
            "active": d.isActive}
    with contextlib.suppress(Exception):
        if d.dataFile:
            info["version"] = d.dataFile.versionNumber
    return info


def _product_kind():
    p = _app.activeProduct
    t = p.objectType if p else ""
    return {"adsk::electron::Board": "board", "adsk::electron::Schematic": "schematic",
            "adsk::electron::Library": "library", "adsk::electron::EcadDesign": "design"}.get(t, "other")


def _settle(n=10):
    for _ in range(n):
        adsk.doEvents()
        time.sleep(0.05)


def op_ping(args):
    return {"addin_version": ADDIN_VERSION, "protocol": PROTOCOL, "fusion_version": _app.version,
            "pid": os.getpid()}


def op_context(args):
    doc = _app.activeDocument
    ws = None
    with contextlib.suppress(Exception):
        ws = _ui.activeWorkspace.id
    return {"active_document": _doc_info(doc) if doc else None, "active_editor": _product_kind(),
            "workspace": ws, "documents": [_doc_info(d) for d in _docs() if _kind_of(d) != "other"]}


def op_activate(args):
    kind = args.get("kind")
    if kind not in ("board", "schematic", "library"):
        raise BridgeError("bad_args", "kind must be board, schematic or library")
    if _product_kind() == kind and not args.get("name"):
        return op_context({})
    name = args.get("name")
    cand = [d for d in _docs() if _kind_of(d) == kind and (name is None or d.name == name)]
    if name is None and _app.activeDocument is not None:
        # prefer the document belonging to the active design, else any of that kind
        same = [d for d in cand if d.name == _app.activeDocument.name]
        cand = same or cand
    if not cand:
        raise BridgeError("not_open", f"no open {kind} document named {name!r}")
    cand[0].activate()
    _settle(20)
    if _product_kind() != kind:
        raise BridgeError("activate_failed", f"activated {cand[0].name!r} but the editor is {_product_kind()}")
    return op_context({})


def _active_product(kind):
    if _product_kind() != kind:
        raise BridgeError("wrong_editor", f"the active editor is {_product_kind()}, need {kind} (call activate)")
    p = _app.activeProduct
    return {"board": adsk.electron.Board, "schematic": adsk.electron.Schematic,
            "library": adsk.electron.Library}[kind].cast(p)


def _linked(kind):
    """The board or schematic product, reachable from whichever Electronics
    editor is active (board, schematic, or the design overview)."""
    cur = _product_kind()
    p = _app.activeProduct
    if cur == kind:
        return _active_product(kind)
    if cur == "design":
        d = adsk.electron.EcadDesign.cast(p)
        return d.board if kind == "board" else d.schematic
    if cur == "schematic" and kind == "board":
        return _active_product("schematic").linkedBoard
    if cur == "board" and kind == "schematic":
        return _active_product("board").linkedSchematic
    # The active document is not an Electronics editor (e.g. a 3D view): use
    # the open board/schematic of the same design; never guess between designs
    # (this once exported the wrong design's board).
    cand = [d for d in _docs() if _kind_of(d) == kind]
    act = _app.activeDocument.name if _app.activeDocument is not None else None
    same = [d for d in cand if d.name == act]
    if same or len(cand) == 1:
        d = (same or cand)[0]
        return d.board if kind == "board" else d.schematic
    if not cand:
        raise BridgeError("not_open", f"no {kind} is open")
    raise BridgeError("ambiguous", f"the active document is not an Electronics editor and {len(cand)} {kind}s "
                      f"are open ({', '.join(sorted({d.name for d in cand}))}); activate the one to use")


def op_export(args):
    kind = args.get("kind")
    if kind == "board":
        prod, fn, ext = _linked("board"), "createEagleBrdExportOptions", ".brd"
    elif kind == "schematic":
        prod, fn, ext = _linked("schematic"), "createEagleSchExportOptions", ".sch"
    elif kind == "library":
        prod, fn, ext = _active_product("library"), "createEagleLbrExportOptions", ".lbr"
    else:
        raise BridgeError("bad_args", "kind must be board, schematic or library")
    if prod is None:
        raise BridgeError("not_linked", f"no {kind} is linked to the active document")
    os.makedirs(WORK_DIR, exist_ok=True)
    path = os.path.join(WORK_DIR, f"export-{uuid.uuid4().hex}{ext}")
    t0 = time.perf_counter()
    em = prod.exportManager
    if not em.execute(getattr(em, fn)(path)) or not os.path.exists(path):
        raise BridgeError("export_failed", f"Fusion did not write the {kind} export")
    return {"path": path, "bytes": os.path.getsize(path), "ms": round((time.perf_counter() - t0) * 1000, 1),
            "name": prod.name}


def _grouped(editor_kind, fn):
    """Run fn() inside one design change so a tool call is ONE undo step.

    Verified on Fusion 2705.1.15: without this, each EAGLE command in a call is its own
    undo step, so a single UNDO could not revert a multi-command write. On an
    exception the change is cancelled (cancelDesignChange rolls back cleanly).
    Exports cannot run inside an open design change ("resource deadlock"), so
    the server verifies after this returns and UNDOes once on mismatch.
    """
    prod = _active_product(editor_kind) if editor_kind in ("board", "schematic", "library") else None
    if prod is None:
        return fn()
    prod.beginDesignChange("fusion-electronics-mcp")
    try:
        out = fn()
    except Exception:
        with contextlib.suppress(Exception):
            prod.cancelDesignChange()
        raise
    prod.endDesignChange()
    return out


def _is_history(cmds):
    return all(c.split(None, 1)[0].upper() in ("UNDO", "REDO") for c in cmds)


# Commands that manage their own undo history or edit nothing. Wrapping AUTO
# in begin/endDesignChange raised the undo.cpp IsRecording() assertion on
# 2705.1.15, so these are never wrapped.
NO_GROUP = {"AUTO", "DRC", "ERC", "RATSNEST", "SET", "UNDO", "REDO"}


def _groupable(cmds):
    return not any(c.split(None, 1)[0].upper() in NO_GROUP for c in cmds)


def op_run(args):
    cmds = validate(args.get("commands") or "")
    expect = args.get("editor")
    if expect and _product_kind() != expect:
        raise BridgeError("wrong_editor", f"the active editor is {_product_kind()}, need {expect}")
    if not cmds:
        raise BridgeError("bad_args", "no commands")
    if not _is_history(cmds) and any(c.split(None, 1)[0].upper() in ("UNDO", "REDO") for c in cmds):
        raise BridgeError("bad_args", "UNDO/REDO must be sent on their own")
    line = "Electron.run " + " ".join(c + ";" for c in cmds)
    t0 = time.perf_counter()
    if _groupable(cmds):
        raw = _grouped(_product_kind(), lambda: _app.executeTextCommand(line))
    else:
        raw = _app.executeTextCommand(line)
    return {"raw": raw, "ms": round((time.perf_counter() - t0) * 1000, 1), "editor": _product_kind()}


def op_run_script(args):
    text = args.get("script") or ""
    cmds = validate(text)
    expect = args.get("editor")
    if expect and _product_kind() != expect:
        raise BridgeError("wrong_editor", f"the active editor is {_product_kind()}, need {expect}")
    if any(c.split(None, 1)[0].upper() in ("UNDO", "REDO") for c in cmds):
        raise BridgeError("bad_args", "scripts may not contain UNDO/REDO")
    os.makedirs(WORK_DIR, exist_ok=True)
    path = os.path.join(WORK_DIR, f"script-{uuid.uuid4().hex}.scr")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    t0 = time.perf_counter()
    try:
        raw = _grouped(_product_kind(), lambda: _app.executeTextCommand(f'Electron.runScript "{path}"'))
    finally:
        with contextlib.suppress(Exception):
            os.remove(path)
    return {"raw": raw, "ms": round((time.perf_counter() - t0) * 1000, 1), "editor": _product_kind()}


def op_design_rules(args):
    """The V2 <designrules> element (net classes, clearance rules, layer
    stackup with dielectric constants) of the open board. The classic EAGLE
    export drops it, but Fusion's cloud-format working copy of the open
    board keeps it: %TEMP%/Neutron/ElectronFileOutput/<pid>/brd-*/<name>.brd.
    The working copy may lag unsaved in-session edits; stackups rarely change
    mid-session, and the path and timestamp are returned so callers can tell."""
    board = _linked("board")
    base = os.path.join(tempfile.gettempdir(), "Neutron", "ElectronFileOutput", str(os.getpid()))
    cands = []
    if os.path.isdir(base):
        for d in os.listdir(base):
            if d.startswith("brd-"):
                f = os.path.join(base, d, board.name + ".brd")
                if os.path.exists(f):
                    cands.append(f)
    if not cands:
        raise BridgeError("not_found", f"no Fusion working copy found for board {board.name!r}")
    path = max(cands, key=os.path.getmtime)
    with open(path, "rb") as f:
        data = f.read()
    a = data.find(b"<designrules")
    b = data.find(b"</designrules>", a)
    if a < 0 or b < 0:
        raise BridgeError("not_found", "the working copy has no <designrules> element")
    return {"board": board.name, "path": path, "modified": time.strftime("%Y-%m-%dT%H:%M:%S",
            time.localtime(os.path.getmtime(path))), "xml": data[a:b + len(b"</designrules>")].decode("utf-8", "replace")}


def op_pours(args):
    """Live pour settings from the API (the cloud working copy lags behind
    edits, so it is not used here)."""
    board = _linked("board")
    out = []
    for i in range(board.signals.count):
        sig = board.signals.item(i)
        for j in range(sig.polyPours.count):
            p = sig.polyPours.item(j)
            pts = []
            with contextlib.suppress(Exception):
                for k in range(p.wires.count):
                    w = p.wires.item(k)
                    pts.append([round(w.x1 / U_PER_MM, 4), round(w.y1 / U_PER_MM, 4)])
            out.append({"net": sig.name, "layer": p.layer, "thermals": p.thermals,
                        "thermal_width_mm": round(p.thermalWidth / U_PER_MM, 4),
                        "isolate_mm": round(p.isolate / U_PER_MM, 4), "width_mm": round(p.width / U_PER_MM, 4),
                        "rank": p.rank, "orphans": p.orphans, "hatched": p.hatched, "outline": pts})
    return {"pours": out}


def op_layers(args):
    """Board layers with visibility, so pick-based edits can show only the
    layer they target and restore the user's exact view afterwards."""
    board = _linked("board")
    return {"layers": [{"number": l.number, "name": l.name, "visible": l.visible, "used": l.used}
                       for l in (board.layers.item(i) for i in range(board.layers.count))]}


_MODES = {"ignore": ("Ignore Violators", "IgnoreViolatorsCommand"),
          "walkaround": ("Walkaround Violators", "WalkaroundViolatorsCommand"),
          "push": ("Push Violators", "PushViolatorsCommand")}


def op_violation_mode(args):
    """Get (and optionally set) the board's violation mode: how Fusion treats
    objects in the way of an edit. In 'push' mode a MOVE is shoved away from
    other parts, even across board sides (a bottom switch under a top LED)."""
    lc = adsk.core.ListControlDefinition.cast(_ui.commandDefinitions.itemById("ViolationModeCommand").controlDefinition)

    def current():
        for i in range(lc.listItems.count):
            if lc.listItems.item(i).isSelected:
                name = lc.listItems.item(i).name
                return next((k for k, v in _MODES.items() if v[0] == name), name)
        return None
    before = current()
    want = args.get("set")
    if want:
        if want not in _MODES:
            raise BridgeError("bad_args", "mode must be ignore, walkaround or push")
        _ui.commandDefinitions.itemById(_MODES[want][1]).execute()
        _settle(8)
    return {"before": before, "mode": current()}


def op_errors(args):
    kind = args.get("kind") or _product_kind()
    if kind not in ("board", "schematic"):
        raise BridgeError("bad_args", "kind must be board or schematic")
    prod = _linked(kind)
    out = []
    for i in range(prod.errors.count):
        e = prod.errors.item(i)
        row = {}
        for k in ("code", "description", "layer", "state", "sheet", "signature"):
            with contextlib.suppress(Exception):
                row[k] = getattr(e, k)
        with contextlib.suppress(Exception):
            row["x_mm"] = round(e.x / U_PER_MM, 4)
            row["y_mm"] = round(e.y / U_PER_MM, 4)
        out.append(row)
    return {"kind": kind, "count": len(out), "errors": out}


def op_save(args):
    doc = _app.activeDocument
    if doc is None:
        raise BridgeError("no_document", "no active document")
    desc = str(args.get("description") or "saved by fusion-electronics-mcp")[:200]
    before = None
    with contextlib.suppress(Exception):
        before = doc.dataFile.versionNumber
    ok = doc.save(desc)
    after = before
    for _ in range(int(float(args.get("wait_s", 30)) / 0.25)):
        adsk.doEvents()
        time.sleep(0.25)
        with contextlib.suppress(Exception):
            after = doc.dataFile.versionNumber
        if before is None or (after and after > before):
            break
    return {"ok": bool(ok), "document": doc.name, "version_before": before, "version_after": after,
            "modified": doc.isModified}


def op_close(args):
    """Close an open library document (needed before ADDing parts from it)."""
    name = args.get("name")
    cand = [d for d in _docs() if _kind_of(d) == "library" and d.name == name]
    if not cand:
        raise BridgeError("not_open", f"no open library named {name!r}")
    if cand[0].isModified and not args.get("discard"):
        raise BridgeError("unsaved", f"library {name!r} has unsaved changes; save first")
    cand[0].close(False)
    _settle(10)
    return op_context({})


def op_new_design(args):
    """New electronics design with a schematic and a board, via Fusion's own
    commands (the API can only create mechanical documents). Verified on
    2705.1.15: no dialogs; the design opens as 'Untitled'. Optionally saved
    straight away under `name` in the active project's `folder`."""
    for cid in ("NewElectronDesignDocumentCommand", "NewElectronSchDocumentCommand",
                "NewElectronPcbDocumentCommand"):
        cd = _ui.commandDefinitions.itemById(cid)
        if cd is None:
            raise BridgeError("unsupported", f"Fusion command {cid} not found in this build")
        cd.execute()
        _settle(40)
    name = args.get("name")
    if name:
        design = next((d for d in _docs() if _kind_of(d) == "design" and d.isActive is not None
                       and d.name.startswith("Untitled")), None)
        folder = _app.data.activeProject.rootFolder
        for part in [p for p in (args.get("folder") or "").split("/") if p]:
            sub = None
            for i in range(folder.dataFolders.count):
                if folder.dataFolders.item(i).name == part:
                    sub = folder.dataFolders.item(i)
            folder = sub or folder.dataFolders.add(part)
        if design is not None:
            design.saveAs(name, folder, "created by fusion-electronics-mcp", "")
            _settle(40)
    return op_context({})


def _project_designs(folder=None, project=None, budget_s=8.0):
    """Designs and libraries in the active project's top folder, or in one named folder path
    ('Live tests', 'Parts/Connectors'). Never a recursive walk: walking a big project's folder
    tree held the main thread until Fusion froze (2705.1.15, three times), so a search stops
    after budget_s and says so."""
    proj = _app.data.activeProject
    if project and project != proj.name:
        raise BridgeError("unsupported", "only the active project can be listed: walking another project's "
                          "folders froze Fusion (2705.1.15); open that project in Fusion first")
    t0 = time.time()
    node = proj.rootFolder
    path = ""
    for part in [x for x in (folder or "").split("/") if x]:
        node = next((node.dataFolders.item(i) for i in range(node.dataFolders.count)
                     if node.dataFolders.item(i).name == part), None)
        if node is None:
            raise BridgeError("not_found", f"no folder {folder!r} in project {proj.name!r}")
        path = f"{path}/{part}" if path else part
        if time.time() - t0 > budget_s:
            raise BridgeError("timeout", f"listing project {proj.name!r} took too long; open the design in Fusion by hand")
    out = []
    for i in range(node.dataFiles.count):
        f = node.dataFiles.item(i)
        if f.fileExtension in ("fprj", "flbr"):
            out.append((path, f))
        if time.time() - t0 > budget_s:
            raise BridgeError("timeout", f"listing project {proj.name!r} took too long; open the design in Fusion by hand")
    return proj, out


def op_list_designs(args):
    proj, found = _project_designs(folder=args.get("folder"), project=args.get("project"))
    return {"project": proj.name,
            "designs": [{"name": f.name, "folder": path, "version": f.versionNumber}
                        for path, f in found if f.fileExtension == "fprj"],
            "libraries": [{"name": f.name, "folder": path, "version": f.versionNumber}
                          for path, f in found if f.fileExtension == "flbr"]}


def op_find_documents(args):
    """Project names in the active hub (one call). Listing the folders of a project that is
    not the active one froze Fusion twice on 2705.1.15 (2026-10-03: the cloud fetch never
    completes while a script holds the main thread), so this never walks projects; open
    libraries from other projects in Fusion by hand."""
    hub = _app.data.activeHub
    return {"hub": hub.name, "projects": [hub.dataProjects.item(i).name for i in range(hub.dataProjects.count)]}


def _norm_folder(path):
    return "/".join(p for p in str(path or "").replace("\\", "/").split("/") if p)


def _doc_folder(d):
    """A document's folder in its project as 'A/B' ('' for the top folder), or None when Fusion
    does not say (an unsaved document, or one without a data file)."""
    try:
        node = d.dataFile.parentFolder
        parts = []
        while node is not None and not getattr(node, "isRoot", False):
            parts.append(node.name)
            node = node.parentFolder
        return "/".join(reversed(parts))
    except Exception:
        return None


def op_open_design(args):
    """Bring a design (or library) forward, opening it from the project only when none of its
    documents is open. Fusion froze on 2026-10-07 during open_design while another design had
    unsaved changes; re-opening a file that is already open (only its board or schematic tab,
    without the design overview) can raise a modal prompt that blocks Fusion's main thread.
    So: any open document of the design counts as open and is activated, and opening from the
    project is refused while an open Electronics document has unsaved changes, unless
    allow_unsaved is set."""
    name = args.get("name")
    folder = args.get("folder")
    ext = "flbr" if args.get("kind") == "library" else "fprj"
    kinds = ("library",) if ext == "flbr" else ("design", "board", "schematic")
    mine = sorted((d for d in _docs() if d.name == name and _kind_of(d) in kinds),
                  key=lambda d: kinds.index(_kind_of(d)))
    if mine:
        # a same-named design can be open from another folder: match the folder when one is given,
        # and refuse to guess when the name alone is ambiguous
        where = {id(d): _doc_folder(d) for d in mine}
        known = sorted({f for f in where.values() if f is not None})
        if folder is not None:
            want = _norm_folder(folder)
            hits = [d for d in mine if where[id(d)] == want]
            if not hits and any(f is None for f in where.values()):
                raise BridgeError("ambiguous", f"{name!r} is open but Fusion does not say which folder it is in, so "
                                  f"it cannot be matched to folder {folder!r}. Switch to it in Fusion by hand, or "
                                  "close it first.")
            mine = hits
        elif len(known) > 1:
            raise BridgeError("ambiguous", f"{name!r} is open from more than one folder ("
                              + ", ".join(repr(f or "top folder") for f in known)
                              + "); pass folder= to say which one")
    if mine:                                   # already open: bring it forward, never open it again
        if not mine[0].isActive:
            mine[0].activate()
            _settle(20)
        res = op_context({})
        res["already_open"] = True
        return res
    unsaved = sorted({d.name for d in _docs() if _kind_of(d) != "other" and d.isModified})
    if unsaved and not args.get("allow_unsaved"):
        raise BridgeError("unsaved", f"{', '.join(repr(u) for u in unsaved)} has unsaved changes. Opening another "
                          "design from the project while one has unsaved changes froze Fusion once; save it "
                          "first (save_design), open the design in Fusion by hand, or pass allow_unsaved=true.")
    proj, found = _project_designs(folder=folder, project=args.get("project"))
    cand = [(p, f) for p, f in found if f.name == name and f.fileExtension == ext]
    if not cand:
        raise BridgeError("not_found", f"no {'library' if ext == 'flbr' else 'design'} {name!r} in "
                          f"{folder or 'the top folder'} of the active project {proj.name!r}; pass folder= "
                          "(it is not searched recursively), or open it in Fusion by hand")
    _app.documents.open(cand[0][1])
    _settle(40)
    return op_context({})


def op_close_design(args):
    """Close a design's board, schematic and design documents. Closing only
    the design document does NOT discard edits to its board/schematic
    (seen on 2705.1.15), so all three are closed explicitly."""
    name = args.get("name")
    docs = [d for d in _docs() if d.name == name and _kind_of(d) in ("board", "schematic", "design")]
    if not docs:
        raise BridgeError("not_open", f"design {name!r} is not open")
    if any(d.isModified for d in docs) and not args.get("discard_changes"):
        raise BridgeError("unsaved", f"{name!r} has unsaved changes; save first or pass discard_changes")
    for kind in ("board", "schematic", "design"):
        for d in [x for x in docs if _kind_of(x) == kind]:
            d.close(False)
            _settle(5)
    return op_context({})


def _data_folder(path):
    """DataFolder in the active project for a 'A/B' path, created if missing."""
    folder = _app.data.activeProject.rootFolder
    for part in [x for x in (path or "").split("/") if x]:
        nxt = None
        for i in range(folder.dataFolders.count):
            if folder.dataFolders.item(i).name == part:
                nxt = folder.dataFolders.item(i)
        folder = nxt or folder.dataFolders.add(part)
    return folder


def _bbox_mm(b):
    return [round(b.minPoint.x * 10, 3), round(b.minPoint.y * 10, 3), round(b.minPoint.z * 10, 3),
            round(b.maxPoint.x * 10, 3), round(b.maxPoint.y * 10, 3), round(b.maxPoint.z * 10, 3)]


def op_create_package3d(args):
    """Give a library package a 3D model from a STEP file, the way Fusion's UI
    does it (verified on 2705.1.15): Electron.Create3DPackage <footprint xml>
    opens a Package3D document with the footprint as sketches; the STEP is
    imported into it and placed; the document is saved through the API (so
    Finish does not stop on 'Unsaved changes' and a Save form); then Finish
    (Package3DStop) links it to the package in the open library."""
    import math
    import xml.etree.ElementTree as ET
    import adsk.fusion  # type: ignore
    pkg_name = args["package"]
    step = args["step_path"]
    if not os.path.exists(step):
        raise BridgeError("not_found", f"STEP file not found: {step}")
    lib = _active_product("library")
    os.makedirs(WORK_DIR, exist_ok=True)
    lbr = os.path.join(WORK_DIR, f"lib-{uuid.uuid4().hex}.lbr")
    em = lib.exportManager
    em.execute(em.createEagleLbrExportOptions(lbr))
    root = ET.parse(lbr).getroot()
    pk = next((x for x in root.iter("package") if x.get("name") == pkg_name), None)
    if pk is None:
        raise BridgeError("not_found", f"package {pkg_name!r} is not in the open library")
    fx = os.path.join(WORK_DIR, f"fp-{uuid.uuid4().hex}.xml")
    ET.ElementTree(pk).write(fx, encoding="utf-8", xml_declaration=True)
    lib_doc = _app.activeDocument
    _app.executeTextCommand(f"Electron.Create3DPackage {fx}")
    _settle(30)
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if design is None:
        raise BridgeError("package3d_failed", "Fusion did not open a 3D package document")
    doc3d = _app.activeDocument
    rc = design.rootComponent
    im = _app.importManager
    n_before = rc.occurrences.count
    if not im.importToTarget(im.createSTEPImportOptions(step), rc) or rc.occurrences.count <= n_before:
        raise BridgeError("import_failed", f"STEP import failed: {step}")
    occ = rc.occurrences.item(rc.occurrences.count - 1)
    m = adsk.core.Matrix3D.create()
    origin = adsk.core.Point3D.create(0, 0, 0)
    for axis, deg in zip(((1, 0, 0), (0, 1, 0), (0, 0, 1)), args.get("rotation_deg") or (0, 0, 0)):
        if deg:
            r = adsk.core.Matrix3D.create()
            r.setToRotation(math.radians(deg), adsk.core.Vector3D.create(*axis), origin)
            m.transformBy(r)
    off = args.get("offset_mm") or (0, 0, 0)
    t = adsk.core.Matrix3D.create()
    t.translation = adsk.core.Vector3D.create(off[0] / 10, off[1] / 10, off[2] / 10)
    m.transformBy(t)
    occ.transform = m
    with contextlib.suppress(Exception):
        if design.designType == adsk.fusion.DesignTypes.ParametricDesignType:
            design.snapshots.add()
    model_bb = _bbox_mm(occ.boundingBox)
    pad_bb = None
    with contextlib.suppress(Exception):
        pad_bb = _bbox_mm(rc.sketches.itemByName("Pad").boundingBox)
    # more of the model below the board than above it = upside down (a flipped through-hole part
    # still pokes its pins up through the board, so "top above zero" is not enough)
    if args.get("require_up", True) and model_bb and (model_bb[5] <= 0.2 or -model_bb[2] > model_bb[5]):
        # the body would hang below the board (a model in another up-axis convention, or a rotation
        # with the wrong sign): never save it; once a design has used a 3D package it is very hard
        # to replace (seen with vendor connector models)
        with contextlib.suppress(Exception):
            doc3d.close(False)
        with contextlib.suppress(Exception):
            lib_doc.activate()
        raise BridgeError("model_below_board",
                          f"the model spans z {model_bb[2]} to {model_bb[5]} mm: mostly below the board, upside down; fix rotation_deg "
                          f"(KiCad's 3D rotation signs are the opposite of Fusion's) and try again. Model box {model_bb}")
    folder = _data_folder(args.get("folder") or "3D Packages")
    # a new name when replacing a model: a design matches 3D packages by name, so a corrected
    # model saved under the old name never reaches boards that already use that name
    doc3d.saveAs(args.get("doc_name") or pkg_name, folder, "3D package created by fusion-electronics-mcp", "")
    for _ in range(120):
        adsk.doEvents()
        time.sleep(0.25)
        with contextlib.suppress(Exception):
            if doc3d.isSaved and not doc3d.isModified:
                break
    _ui.commandDefinitions.itemById("Package3DStop").execute()
    _settle(40)
    with contextlib.suppress(Exception):
        lib_doc.activate()
        _settle(10)
    lib = _active_product("library")
    linked = []
    for i in range(lib.deviceSets.count):
        ds = lib.deviceSets.item(i)
        for j in range(ds.devices.count):
            dv = ds.devices.item(j)
            with contextlib.suppress(Exception):
                if dv.package and dv.package.name == pkg_name:
                    linked.append({"device": ds.name + dv.name,
                                   "packages3d": [dv.packages3d.item(k).name for k in range(dv.packages3d.count)]})
    for f in (lbr, fx):
        with contextlib.suppress(Exception):
            os.remove(f)
    return {"package": pkg_name, "model_bbox_mm": model_bb, "pad_bbox_mm": pad_bb,
            "saved_as": f"{args.get('folder') or '3D Packages'}/{args.get('doc_name') or pkg_name}", "devices": linked}


def op_elements3d(args):
    """Read-only: the 3D package each board element uses (None = no 3D model), optionally
    only `names`. locally_modified 3D packages are skipped by Fusion's library update."""
    board = _linked("board")
    names = set(args.get("names") or [])
    out = []
    els = board.elements
    for i in range(els.count):
        e = els.item(i)
        if names and e.name not in names:
            continue
        p3 = None
        with contextlib.suppress(Exception):
            p3 = e.package3d
        out.append({"name": e.name, "package3d": None if p3 is None else _safe(lambda: p3.name),
                    "id": None if p3 is None else _safe(lambda: p3.id),
                    "locally_modified": None if p3 is None else _safe(lambda: p3.locallyModified)})
    return {"elements": out}


def op_lib_device3d(args):
    """Read-only: in the open library, the 3D packages each device links (devices whose package
    name contains `match`, or all)."""
    lib = _active_product("library")
    match = (args.get("match") or "").upper()
    out = []
    for i in range(lib.deviceSets.count):
        ds = lib.deviceSets.item(i)
        for j in range(ds.devices.count):
            d = ds.devices.item(j)
            with contextlib.suppress(Exception):
                if d.package and (not match or match in d.package.name.upper()):
                    out.append({"device": ds.name + d.name, "package": d.package.name,
                                "packages3d": [d.packages3d.item(k).name for k in range(d.packages3d.count)]})
    return {"devices": out}


def _pcb3d_design():
    import adsk.fusion  # type: ignore
    for i in range(_app.documents.count):
        doc = _app.documents.item(i)
        with contextlib.suppress(Exception):
            for k in range(doc.products.count):
                des = adsk.fusion.Design.cast(doc.products.item(k))
                if des and des.rootComponent.allOccurrences.count:
                    return doc, des
    return None, None


def op_pcb3d_bodies(args):
    """Read-only: in the open 3D PCB, each board part's height range (mm, z) next to the board's,
    so a model hanging on the wrong side shows up. Parts are found by reference designator at
    any depth (Fusion nests them under 'Packages' in a new 3D PCB, at the top in older ones)."""
    doc, des = _pcb3d_design()
    if des is None:
        raise BridgeError("not_found", "no 3D PCB document is open")
    refs = set()
    with contextlib.suppress(Exception):
        els = _linked("board").elements
        refs = {els.item(i).name for i in range(els.count)}
    root = des.rootComponent
    board = None
    occs = root.allOccurrences
    for i in range(occs.count):                       # a 'Board' occurrence (new 3D PCBs)
        o = occs.item(i)
        if o.name.split(":")[0].lower() == "board":
            bb = o.boundingBox
            board = (round(bb.minPoint.z * 10, 3), round(bb.maxPoint.z * 10, 3))
            break
    if board is None:                                 # else the largest body at the top level
        best = None
        for i in range(root.bRepBodies.count):
            bb = root.bRepBodies.item(i).boundingBox
            area = (bb.maxPoint.x - bb.minPoint.x) * (bb.maxPoint.y - bb.minPoint.y)
            if best is None or area > best[0]:
                best = (area, round(bb.minPoint.z * 10, 3), round(bb.maxPoint.z * 10, 3))
        board = best[1:] if best else None
    parts = []
    for i in range(occs.count):
        o = occs.item(i)
        ref = o.name.rsplit(":", 1)[-1]
        name = o.name.split(":")[0]
        hit = ref in refs or name in refs or any(name.endswith(" " + r) or name.endswith("_" + r) for r in refs)
        if not hit:
            continue
        with contextlib.suppress(Exception):
            bb = o.boundingBox
            parts.append({"occurrence": o.name, "z_mm": [round(bb.minPoint.z * 10, 3), round(bb.maxPoint.z * 10, 3)]})
    return {"document": doc.name, "board_z_mm": None if board is None else list(board), "parts": parts}


def op_push_3d(args):
    """Bring the 2D board's changes into its open 3D PCB without the preferences dialog: activate
    the 3D PCB document and run 'Pull from 2D PCB' (PullPCB3DCmd)."""
    doc, des = _pcb3d_design()
    if doc is None:
        # no 3D PCB yet: Fusion's own Push to 3D PCB creates it (from the board editor)
        if _product_kind() != "board":
            with contextlib.suppress(Exception):
                _linked("board")
        cd = _app.userInterface.commandDefinitions.itemById("Electron::Pcb3DViewAdvanced")
        if cd is None:
            raise BridgeError("unsupported", "this Fusion build has no Push to 3D PCB command")
        cd.execute()
        # Fusion builds the new 3D PCB after the dialog closes: wait for it (up to ~3 minutes)
        deadline = time.time() + float(args.get("wait_s", 180))
        while time.time() < deadline:
            _settle(20)                                  # about a second of event processing
            doc, des = _pcb3d_design()
            if doc is not None:
                break
        if doc is None:
            raise BridgeError("not_found", "Push to 3D PCB did not create a 3D PCB in time (a dialog may be waiting in Fusion)")
        _settle(200)
        return op_pcb3d_bodies({})
    doc.activate()
    _settle(10)
    cd = _app.userInterface.commandDefinitions.itemById(args.get("command") or "PullPCB3DCmd")
    if cd is None:
        raise BridgeError("unsupported", "this Fusion build has no PullPCB3DCmd")
    cd.execute()
    _settle(int(args.get("settle", 400)))             # ~20 s for the 3D PCB to rebuild
    return op_pcb3d_bodies({})


def _safe(fn):
    try:
        v = fn()
        return v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
    except Exception as ex:
        return f"<{type(ex).__name__}>"


def op_update_libraries(args):
    """Update the active design from all its libraries (Fusion's 'Update all',
    command Electron::UpdateDesignFromAllLibraries; no dialogs seen on
    2705.1.15). Brings library changes such as attributes and 3D packages in."""
    if _product_kind() not in ("board", "schematic", "design"):
        raise BridgeError("wrong_editor", f"the active editor is {_product_kind()}; activate the board or schematic")
    cd = _app.userInterface.commandDefinitions.itemById("Electron::UpdateDesignFromAllLibraries")
    if cd is None:
        raise BridgeError("unsupported", "this Fusion build has no Electron::UpdateDesignFromAllLibraries command")
    cd.execute()
    _settle(int(args.get("settle", 60)))
    return op_context({})


OPS = {"ping": op_ping, "context": op_context, "activate": op_activate, "export": op_export,
       "run": op_run, "run_script": op_run_script, "errors": op_errors, "save": op_save,
       "close_library": op_close, "list_designs": op_list_designs, "open_design": op_open_design,
       "find_documents": op_find_documents,
       "close_design": op_close_design, "design_rules": op_design_rules, "new_design": op_new_design,
       "pours": op_pours, "create_package3d": op_create_package3d,
       "layers": op_layers, "violation_mode": op_violation_mode, "update_libraries": op_update_libraries,
       "elements3d": op_elements3d, "lib_device3d": op_lib_device3d, "pcb3d_bodies": op_pcb3d_bodies,
       "push_3d": op_push_3d}


# ---------------------------------------------------------------------------
# transport


class _Handler(adsk.core.CustomEventHandler):
    def notify(self, args):
        jid = args.additionalInfo
        with _jobs_lock:
            job = _jobs.get(jid)
            if job is None or job["state"] != "queued":
                return
            job["state"] = "running"
        try:
            fn = OPS[job["op"]]
            job["result"] = {"ok": True, "result": fn(job["args"])}
        except BridgeError as ex:
            job["result"] = {"ok": False, "error": {"code": ex.code, "message": str(ex)}}
        except Exception as ex:
            job["result"] = {"ok": False, "error": {"code": "fusion_error", "message": str(ex)[:500],
                                                    "trace": traceback.format_exc()[-1500:]}}
        job["state"] = "done"
        job["done"].set()


def _reply(conn, obj):
    conn.sendall((json.dumps(obj, default=str) + "\n").encode("utf-8"))


def _serve_conn(conn):
    try:
        conn.settimeout(15)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
            if len(buf) > 4_000_000:
                _reply(conn, {"v": PROTOCOL, "ok": False, "error": {"code": "too_large", "message": "request too large"}})
                return
        req = json.loads(buf.decode("utf-8"))
        rid = req.get("id")
        if not hmac.compare_digest(str(req.get("token", "")), _token):
            _reply(conn, {"v": PROTOCOL, "id": rid, "ok": False, "error": {"code": "unauthorized", "message": "bad token"}})
            return
        op = req.get("op")
        if op not in OPS:
            _reply(conn, {"v": PROTOCOL, "id": rid, "ok": False, "error": {"code": "unknown_op", "message": f"unknown op {op!r}"}})
            return
        timeout = min(float(req.get("timeout") or DEFAULT_TIMEOUT), 600.0)
        jid = uuid.uuid4().hex
        job = {"op": op, "args": req.get("args") or {}, "done": threading.Event(), "state": "queued", "result": None}
        with _jobs_lock:
            _jobs[jid] = job
        _app.fireCustomEvent(EVENT_ID, jid)
        if job["done"].wait(timeout):
            res = job["result"]
        else:
            with _jobs_lock:
                state = job["state"]
                if state == "queued":
                    job["state"] = "cancelled"
            res = {"ok": False, "error": {"code": "timeout", "message": (
                f"timed out after {timeout}s before Fusion started it (not run)" if state == "queued" else
                f"timed out after {timeout}s while running in Fusion; it may still complete, "
                "check state before retrying (a modal dialog may be open)")}}
        with _jobs_lock:
            _jobs.pop(jid, None)
        res.update({"v": PROTOCOL, "id": rid})
        _reply(conn, res)
    except Exception:
        with contextlib.suppress(Exception):
            _reply(conn, {"v": PROTOCOL, "ok": False, "error": {"code": "bridge_error", "message": traceback.format_exc()[-800:]}})
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _accept(sock):
    sock.settimeout(0.5)
    while not _stop.is_set():
        try:
            conn, _ = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=_serve_conn, args=(conn,), daemon=True).start()


def run(context):
    global _app, _ui, _event, _handler, _server, _token
    _app = adsk.core.Application.get()
    _ui = _app.userInterface
    try:
        with contextlib.suppress(Exception):
            _app.unregisterCustomEvent(EVENT_ID)
        _event = _app.registerCustomEvent(EVENT_ID)
        _handler = _Handler()
        _event.add(_handler)
        _token = secrets.token_hex(24)
        _stop.clear()
        _server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _server.bind(("127.0.0.1", 0))
        _server.listen(8)
        os.makedirs(INFO_DIR, exist_ok=True)
        with open(INFO_PATH, "w", encoding="utf-8") as f:
            json.dump({"port": _server.getsockname()[1], "token": _token, "pid": os.getpid(),
                       "protocol": PROTOCOL, "addin_version": ADDIN_VERSION,
                       "started": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)
        threading.Thread(target=_accept, args=(_server,), daemon=True).start()
    except Exception:
        _ui.messageBox(traceback.format_exc(), "Fusion Electronics MCP failed to start")


def stop(context):
    _stop.set()
    with contextlib.suppress(Exception):
        _server.close()
    with contextlib.suppress(Exception):
        if _event and _handler:
            _event.remove(_handler)
        _app.unregisterCustomEvent(EVENT_ID)
    with contextlib.suppress(Exception):
        os.remove(INFO_PATH)
    with contextlib.suppress(Exception):
        shutil.rmtree(WORK_DIR, ignore_errors=True)
