"""One server at a time talks to Fusion (a lock shared by every server process).

Each Claude session runs its own server. On 2026-10-07 two of them were sending
work to the same Fusion when it hung; the per-server serialisation (builtin.py)
cannot see the other process. This is an OS file lock in the per-user data
folder: msvcrt on Windows, flock elsewhere. The OS drops it when the owning
process exits or dies, so a crashed server never leaves a stale lock; the
owner file next to it only says who holds it, for the message.

Re-entrant within a process: a write holds it from its "before" export to its
read-back (and undo), and the bridge calls inside take it again for free.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time

from . import data_dir

WAIT_S = float(os.environ.get("FUSION_MCP_LOCK_WAIT_S", "30"))


class FusionBusy(Exception):
    pass


class FusionLock:
    def __init__(self, path: str | None = None, wait_s: float | None = None):
        self.path = path or data_dir("fusion.lock")
        self.wait_s = WAIT_S if wait_s is None else wait_s
        self._mutex = threading.RLock()        # threads of this process
        self._depth = 0
        self._fh = None

    @property
    def owner_path(self) -> str:
        return self.path + ".owner"

    def _try_os_lock(self) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def _release_os_lock(self):
        fh, self._fh = self._fh, None
        if fh is None:
            return
        with contextlib.suppress(OSError):
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()

    def owner(self) -> dict:
        try:
            with open(self.owner_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def busy_message(self) -> str:
        o = self.owner()
        who = f"server PID {o['pid']}" if o.get("pid") else "another server"
        what = f", doing {o['what']}" if o.get("what") else ""
        since = f" for {time.time() - o['since']:.0f} s" if o.get("since") else ""
        return (f"Another Claude session is using Fusion ({who}{what}{since}). Only one session talks to "
                f"Fusion at a time; waited {self.wait_s:g} s. Try again when it has finished. If Fusion "
                "looks frozen, check it for an open dialog before retrying.")

    @contextlib.contextmanager
    def hold(self, what: str = ""):
        """Hold the lock for the block; waits up to wait_s for another process, then FusionBusy."""
        with self._mutex:
            if self._depth == 0:
                deadline = time.monotonic() + self.wait_s
                while not self._try_os_lock():
                    if time.monotonic() >= deadline:
                        raise FusionBusy(self.busy_message())
                    time.sleep(0.2)
                with contextlib.suppress(OSError):
                    with open(self.owner_path, "w", encoding="utf-8") as f:
                        json.dump({"pid": os.getpid(), "what": what, "since": time.time()}, f)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    with contextlib.suppress(OSError):
                        os.remove(self.owner_path)
                    self._release_os_lock()


LOCK = FusionLock()
