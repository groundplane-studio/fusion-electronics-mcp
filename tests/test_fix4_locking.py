"""Cross-process lock: a timed-out built-in call keeps it, multi-call tools hold it throughout."""

import os
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from fusion_mcp import bridge as BR
from fusion_mcp import server as S
from fusion_mcp.bridge import Bridge, BridgeUnavailable
from fusion_mcp.fusion_lock import FusionBusy, FusionLock
from fusion_mcp.session import Session

from test_drc import DrcFusion
from test_move_parts import FakeFusion, parts_root


def lock(wait_s=0.3):
    return FusionLock(os.path.join(tempfile.mkdtemp(), "fusion.lock"), wait_s=wait_s)


class PinTest(unittest.TestCase):
    def test_pinned_lock_outlives_the_hold_until_unpinned(self):
        lk = lock()
        with lk.hold("run"):
            self.assertTrue(lk.pin())
        self.assertIsNotNone(lk._fh)                  # still held for the call Fusion is running
        busy = []

        def other():
            try:
                with lk.hold("export"):
                    pass
            except FusionBusy as ex:
                busy.append(str(ex))
        t = threading.Thread(target=other)
        t.start()
        t.join()
        self.assertIn("still running in Fusion", busy[0])
        lk.unpin()
        self.assertIsNone(lk._fh)
        with lk.hold("export"):                       # free again
            pass

    def test_waiter_gets_the_lock_when_the_pinned_call_ends(self):
        lk = lock(wait_s=5)
        with lk.hold("run"):
            lk.pin()
        threading.Timer(0.3, lk.unpin).start()
        t0 = time.monotonic()
        with lk.hold("export"):
            pass
        self.assertGreater(time.monotonic() - t0, 0.2)

    def test_pin_outside_a_hold_does_nothing(self):
        lk = lock()
        self.assertFalse(lk.pin())
        self.assertEqual(lk._pinned, 0)


class BuiltinTimeoutKeepsLockTest(unittest.TestCase):
    def test_timed_out_call_leaves_the_lock_to_its_worker(self):
        release = threading.Event()
        finished = threading.Event()

        def slow(op, args, timeout):
            release.wait(10)
            finished.set()
            return {"ok": True, "result": {}}
        b = Bridge(info_path=os.path.join(tempfile.mkdtemp(), "none.json"), watch_dialogs=False, keep_focus=False)
        b.lock = lock()
        ticks = {"n": 0}

        def mono():                                   # the deadline passes right after the call starts
            ticks["n"] += 1
            return time.monotonic() + (0 if ticks["n"] == 1 else 1000)
        fake_time = types.SimpleNamespace(monotonic=mono, sleep=time.sleep)
        with mock.patch.dict(os.environ, {"FUSION_MCP_TRANSPORT": "builtin"}), \
                mock.patch.object(Bridge, "_builtin_pid", 1, create=True), \
                mock.patch.object(Bridge, "_builtin_call", staticmethod(slow)), \
                mock.patch.object(BR, "time", fake_time), mock.patch.object(BR, "POLL_S", 0.05):
            with self.assertRaises(BridgeUnavailable) as cm:
                b.call("run", {"commands": "MOVE;"}, timeout=1)
            self.assertIn("next call waits", str(cm.exception))
            self.assertEqual(b.lock._pinned, 1)
            self.assertIsNotNone(b.lock._fh)          # Fusion is still busy: the lock stays held
            release.set()
            finished.wait(5)
            for _ in range(50):
                if b.lock._fh is None:
                    break
                time.sleep(0.02)
        self.assertEqual(b.lock._pinned, 0)
        self.assertIsNone(b.lock._fh)                 # released once the worker finished


class RecordingLock:
    def __init__(self):
        self.depth = 0

    def hold(self, what=""):
        import contextlib

        @contextlib.contextmanager
        def cm():
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1
        return cm()


class LockedFake(FakeFusion):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.lock = RecordingLock()
        self.unlocked = []

    def call(self, op, args=None, timeout=60, answers=None, forms=None):
        if self.lock.depth == 0:
            self.unlocked.append(op)
        return super().call(op, args, timeout, answers, forms)


class LockedDrcFake(DrcFusion):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.lock = RecordingLock()
        self.unlocked = []

    def call(self, op, args=None, timeout=60, answers=None, forms=None):
        if self.lock.depth == 0:
            self.unlocked.append(op)
        return super().call(op, args, timeout, answers, forms)


class MultiCallToolsTest(unittest.TestCase):
    def test_ignore_violators_holds_the_lock_from_set_to_restore(self):
        fake = LockedFake(parts_root())
        with mock.patch.object(S, "session", Session(fake)):
            with S._IgnoreViolators():
                self.assertEqual(fake.lock.depth, 1)
                fake.call("context")
        self.assertEqual(fake.lock.depth, 0)
        self.assertEqual(fake.modes, ["ignore", "push"])
        self.assertEqual(fake.unlocked, [])

    def test_ignore_violators_does_not_swallow_a_busy_fusion(self):
        class Busy(FakeFusion):
            def call(self, op, args=None, **kw):
                raise BridgeUnavailable("Another Claude session is using Fusion")
        with mock.patch.object(S, "session", Session(Busy(parts_root()))):
            with self.assertRaises(BridgeUnavailable):
                with S._IgnoreViolators():
                    self.fail("the edit must not run when the mode could not be set")

    def test_check_drc_runs_and_reads_in_one_hold(self):
        S._DRC_LAST.clear()
        fake = LockedDrcFake(parts_root())
        with mock.patch.object(S, "session", Session(fake)):
            S.check_drc()
        self.assertEqual(fake.unlocked, [])


if __name__ == "__main__":
    unittest.main()
