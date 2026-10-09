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

ADDIN_DIR = os.path.join(os.path.dirname(__file__), "addin", "FusionElectronicsMCP")
_DIRS = [os.path.dirname(__file__),
         ADDIN_DIR,
         os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "offline", "fusion_offline")]
CHECK_EVERY_S = 10.0


def _is_addin(path: str) -> bool:
    return os.path.normcase(os.path.abspath(os.path.dirname(path))) == os.path.normcase(os.path.abspath(ADDIN_DIR))


def newest(server_only: bool = False) -> tuple[float, str]:
    """(mtime, path) of the newest source file of this package, the add-in and fusion_offline
    (server_only: without the add-in)."""
    best = (0.0, "")
    dirs = [d for d in _DIRS if not (server_only and d == ADDIN_DIR)]
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
    when = lambda t: time.strftime('%Y-%m-%d %H:%M', time.localtime(t))
    server_msg = ("this fusion-electronics server is running older code than is on disk ({} changed {}): toggle "
                  "fusion-electronics off and on in Connectors (or restart Claude) to load it")
    if t <= STARTED + 1:
        _cache["note"] = None
    elif _is_addin(path):
        # restarting the connector does not reload the add-in: Fusion runs its own copy
        msgs = [f"the Fusion add-in changed on disk ({os.path.basename(path)} changed {when(t)}). The built-in "
                "transport picks it up by itself; for the add-in transport reinstall it "
                "(fusion-electronics-mcp install-addin --force, unless it was installed with --link) and then "
                "stop and run FusionElectronicsMCP again in Fusion (Utilities > Scripts and Add-Ins > Add-Ins)"]
        ts, ps = newest(server_only=True)
        if ts > STARTED + 1:
            msgs.append(server_msg.format(os.path.basename(ps), when(ts)))
        _cache["note"] = "; also ".join(msgs)
    else:
        _cache["note"] = server_msg.format(os.path.basename(path), when(t))
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
