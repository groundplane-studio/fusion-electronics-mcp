"""MCP server for Autodesk Fusion Electronics."""

import os
import sys

__version__ = "0.3.0"
PROJECT_URL = "https://github.com/groundplane-studio/fusion-electronics-mcp"


def data_dir(*parts: str) -> str:
    """Per-user data folder (component library, EasyEDA cache): %LOCALAPPDATA% on
    Windows, ~/Library/Application Support on macOS, else ~/.local/share."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "fusion-electronics-mcp", *parts)
