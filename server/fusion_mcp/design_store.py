"""Per-design data the tools keep (length groups, net currents): one JSON file per design in
the per-user data folder, so it survives server restarts. It lives on this machine, not in the
Fusion design.

The file name is the design name made safe for a file system plus a short hash of the exact
name (and folder / project when given), so "Board v1" and "Board_v1" keep separate data and a
name like NUL or COM1 still makes a valid file. Read-modify-write goes through update(), which
holds the lock every server process shares, so two sessions cannot lose each other's changes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile

from . import data_dir


class StoreError(ValueError):
    """The design's data file could not be read or written (the message says which and why)."""


def _safe(design: str) -> str:
    return re.sub(r"[^\w.+-]+", "_", design or "unnamed").strip("_.") or "unnamed"


def path(design: str, folder: str | None = None, project: str | None = None) -> str:
    """The data file for a design. The "d_" prefix keeps Windows reserved names (NUL, CON,
    COM1...) from being the file's stem."""
    exact = "\x1f".join([project or "", folder or "", design or "unnamed"])
    digest = hashlib.sha1(exact.encode("utf-8")).hexdigest()[:10]
    return data_dir("designs", f"d_{_safe(design)[:80]}_{digest}.json")


def _legacy_path(design: str) -> str:
    """Where data was kept before names were hashed (read only, when the new file is missing)."""
    safe = re.sub(r"[^\w.+-]+", "_", design or "unnamed").strip("_") or "unnamed"
    return os.path.join(os.path.dirname(_path(design)), safe + ".json")


def _path(design: str, folder: str | None = None, project: str | None = None) -> str:
    return path(design, folder, project) if (folder or project) else path(design)


def load(design: str, folder: str | None = None, project: str | None = None) -> dict:
    p = _path(design, folder, project)
    for c in ([p] if (folder or project) else [p, _legacy_path(design)]):
        try:
            with open(c, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as ex:
            if c == p:
                raise StoreError(f"the saved data for design {design!r} ({p}) could not be read ({ex}); fix or "
                                 "delete that file") from None
            continue                         # a legacy file that is unreadable (or a reserved name): ignore it
    return {}


def save(design: str, data: dict, folder: str | None = None, project: str | None = None) -> str:
    """Write the design's data atomically (a temp file in the same folder, then a replace)."""
    p = _path(design, folder, project)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=os.path.dirname(p))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, sort_keys=True)
            os.replace(tmp, p)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise
    except OSError as ex:
        raise StoreError(f"could not save the data for design {design!r} to {p}: {ex}") from None
    return p


@contextlib.contextmanager
def locked():
    """Hold the lock every server process shares (re-entrant) while reading, changing and saving."""
    from .fusion_lock import LOCK, FusionBusy
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(LOCK.hold("design data"))
        except FusionBusy as ex:
            raise StoreError(f"{ex} The design data was not changed.") from None
        yield


def update(design: str, change, folder: str | None = None, project: str | None = None) -> tuple[dict, str]:
    """Load the design's data, call change(data) to edit it in place, and save it, all under the
    shared lock. Returns (data, the file it was saved to)."""
    with locked():
        data = load(design, folder, project)
        change(data)
        return data, save(design, data, folder, project)
