"""Command line: run the MCP server, install the Fusion add-in, check the setup.

    fusion-electronics-mcp                  run the MCP server (stdio), what MCP clients start
    fusion-electronics-mcp install-addin    put the Fusion add-in in Fusion's AddIns folder
    fusion-electronics-mcp doctor           check Python, the add-in, Fusion and optional pieces
    fusion-electronics-mcp tools            list the MCP tools
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from . import PROJECT_URL, __version__

ADDIN_NAME = "FusionElectronicsMCP"
BUNDLED_ADDIN = os.path.join(os.path.dirname(__file__), "addin", ADDIN_NAME)


def fusion_addins_dir() -> str:
    """Fusion's per-user AddIns folder (Windows and macOS)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
        return os.path.join(base, "Autodesk", "Autodesk Fusion 360", "API", "AddIns")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns")
    raise SystemExit("Autodesk Fusion runs on Windows and macOS only.")


def manifest_version(folder: str) -> str | None:
    try:
        with open(os.path.join(folder, f"{ADDIN_NAME}.manifest"), encoding="utf-8") as f:
            return json.load(f).get("version")
    except (OSError, ValueError):
        return None


def _link(src: str, dst: str) -> None:
    if sys.platform == "win32":
        import _winapi                      # a junction needs no admin rights, unlike a symlink
        _winapi.CreateJunction(src, dst)
    else:
        os.symlink(src, dst, target_is_directory=True)


def install_addin(link: bool = False, force: bool = False) -> int:
    dest_root = fusion_addins_dir()
    dest = os.path.join(dest_root, ADDIN_NAME)
    os.makedirs(dest_root, exist_ok=True)
    if os.path.lexists(dest):
        same = os.path.realpath(dest) == os.path.realpath(BUNDLED_ADDIN)
        if same and link:
            print(f"Already linked: {dest} -> {BUNDLED_ADDIN}")
            return 0
        if not force:
            print(f"{dest} already exists (version {manifest_version(dest)}). "
                  "Run again with --force to replace it.")
            return 1
        if os.path.islink(dest) or (sys.platform == "win32" and same):
            os.unlink(dest) if os.path.islink(dest) else os.rmdir(dest)
        else:
            shutil.rmtree(dest)
    if link:
        _link(BUNDLED_ADDIN, dest)
        how = f"linked to {BUNDLED_ADDIN}"
    else:
        shutil.copytree(BUNDLED_ADDIN, dest, ignore=shutil.ignore_patterns("__pycache__", ".vscode", "*.pyc"))
        how = "copied"
    print(f"Add-in {manifest_version(BUNDLED_ADDIN)} {how}: {dest}")
    print("In Fusion: Utilities > Add-Ins > Scripts and Add-Ins > Add-Ins tab > "
          f"{ADDIN_NAME} > Run (tick 'Run on Startup' to start it with Fusion).")
    return 0


def doctor() -> int:
    ok = True

    def line(good: bool | None, what: str, detail: str = "") -> None:
        nonlocal ok
        mark = {True: "ok  ", False: "FAIL", None: "info"}[good]
        ok = ok and good is not False
        print(f"[{mark}] {what}" + (f": {detail}" if detail else ""))

    line(sys.version_info >= (3, 12), "Python", sys.version.split()[0] + " (3.12 or newer needed)")
    line(None, "fusion-electronics-mcp", __version__)
    bundled = manifest_version(BUNDLED_ADDIN)
    from .builtin import URL as BUILTIN_URL, BuiltinClient
    from .bridge import Bridge, BridgeOpError, BridgeUnavailable
    builtin = BuiltinClient().available()
    line(True if builtin else None, "Fusion's built-in MCP server (fallback)",
         f"answering at {BUILTIN_URL}" if builtin else
         "not answering (Fusion closed, or its MCP server is off)")
    try:
        dest = os.path.join(fusion_addins_dir(), ADDIN_NAME)
        installed = manifest_version(dest) if os.path.exists(dest) else None
        if installed:
            line(True if installed == bundled else False, "Add-in (recommended)",
                 f"{installed} at {dest}" + ("" if installed == bundled else
                 f"; this package ships {bundled}: fusion-electronics-mcp install-addin --force"))
        else:
            line(False, "Add-in (recommended)",
                 "not installed: fusion-electronics-mcp install-addin, then run it in Fusion (Utilities > "
                 "Add-Ins, Run on Startup)" + ("; the built-in server works meanwhile, without saves of "
                                              "libraries or 3D pushes" if builtin else ""))
    except SystemExit as ex:
        line(False, "Platform", str(ex))
    try:
        b = Bridge(keep_focus=False)
        mode = b.transport()
        info = b.call("ping", {}, timeout=15)
        line(True, "Connected to Fusion", f"via {'the built-in MCP server' if mode == 'builtin' else 'the add-in'}, "
             f"Fusion {info.get('fusion_version')}")
        if mode == "addin" and info.get("addin_version") != bundled:
            line(False, "Running add-in version", f"{info.get('addin_version')} != {bundled}; restart the add-in in Fusion")
    except (BridgeUnavailable, BridgeOpError) as ex:
        line(False, "Connected to Fusion", str(ex))
    from . import dialogs
    line(dialogs.supported() or None, "Dialog watchdog",
         "available" if dialogs.supported() else "Windows only so far: dialogs on this OS need a person")
    try:
        import matplotlib  # noqa: F401
        line(True, "render_board", "matplotlib installed")
    except ImportError:
        line(None, "render_board", 'optional; pip install "fusion-electronics-mcp[render]"')
    from .library import Library
    lib = Library()
    n = len([f for f in os.listdir(lib.directory) if f.endswith(".json")]) if os.path.isdir(lib.directory) else 0
    line(None, "Component library", f"{lib.directory} ({n} parts; FUSION_MCP_LIBRARY to change)")
    from . import easyeda
    line(None, "EasyEDA footprint cache", f"{easyeda.cache_dir()} ({len(os.listdir(easyeda.cache_dir()))} parts)")
    print("\nAll good." if ok else f"\nSomething needs attention (see FAIL lines). Help: {PROJECT_URL}")
    return 0 if ok else 1


def list_tools() -> int:
    import asyncio
    from .server import mcp
    for t in asyncio.run(mcp.list_tools()):
        a = t.annotations
        kind = "read" if a and a.read_only_hint else ("change" if a and a.destructive_hint else "add")
        print(f"{t.name:28s} {kind:6s} {(t.description or '').strip().splitlines()[0]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fusion-electronics-mcp",
                                description="MCP server for Autodesk Fusion Electronics.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    ia = sub.add_parser("install-addin", help="install the Fusion add-in into Fusion's AddIns folder")
    ia.add_argument("--link", action="store_true", help="link instead of copy (for development)")
    ia.add_argument("--force", action="store_true", help="replace an existing install")
    sub.add_parser("doctor", help="check the setup")
    sub.add_parser("tools", help="list the MCP tools")
    sub.add_parser("serve", help="run the MCP server on stdio (the default)")
    args = p.parse_args(argv)
    if args.cmd == "install-addin":
        return install_addin(args.link, args.force)
    if args.cmd == "doctor":
        return doctor()
    if args.cmd == "tools":
        return list_tools()
    from .server import mcp
    mcp.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
