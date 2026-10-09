"""Is this server running older code than is on disk?

The MCP client keeps a server process for as long as the connector is on, so after an update
(git pull, an editable install) the running server still has the old code. On 2026-10-07 a
stale server kept failing ("bridge_error: no result / output exceeded 1 MiB") while a fresh
process with the fix on disk worked. Each tool reply now says so when the code changed.
"""

from __future__ import annotations

import glob
import os
import time

_DIRS = [os.path.dirname(__file__),
         os.path.join(os.path.dirname(__file__), "addin", "FusionElectronicsMCP"),
         os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "offline", "fusion_offline")]
CHECK_EVERY_S = 10.0


def newest() -> tuple[float, str]:
    """(mtime, path) of the newest source file of this package and fusion_offline."""
    best = (0.0, "")
    dirs = list(_DIRS)
    try:
        import fusion_offline
        dirs.append(os.path.dirname(fusion_offline.__file__))
    except ImportError:
        pass
    for d in dict.fromkeys(dirs):
        for p in glob.glob(os.path.join(d, "*.py")):
            try:
                best = max(best, (os.path.getmtime(p), p))
            except OSError:
                pass
    return best


STARTED = newest()[0]
_cache = {"at": -1e9, "note": None}


def note(clock=time.monotonic) -> str | None:
    """A warning when a source file is newer than the code this process loaded (checked at most
    every CHECK_EVERY_S seconds), else None."""
    now = clock()
    if now - _cache["at"] < CHECK_EVERY_S:
        return _cache["note"]
    _cache["at"] = now
    t, path = newest()
    _cache["note"] = None if t <= STARTED + 1 else (
        f"this fusion-electronics server is running older code than is on disk ({os.path.basename(path)} changed "
        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(t))}): toggle fusion-electronics off and on in "
        "Connectors (or restart Claude) to load it")
    return _cache["note"]


def attach(result, msg: str | None):
    """The tool result with the warning added: a key on a dict, a line on a list or a string."""
    if not msg:
        return result
    if isinstance(result, dict):
        return {**result, "server_outdated": msg}
    if isinstance(result, list):
        return result + [f"Note: {msg}"]
    if isinstance(result, str):
        return f"{result}\nNote: {msg}"
    return result
