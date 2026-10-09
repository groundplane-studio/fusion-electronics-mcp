import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from fusion_mcp.fusion_lock import FusionBusy, FusionLock
from fusion_mcp.session import Session, WriteFailed

HOLDER = """
import sys, time
from fusion_mcp.fusion_lock import FusionLock
lock = FusionLock(sys.argv[1], wait_s=5)
with lock.hold("route_pair"):
    print("held", flush=True)
    time.sleep(60)
"""


class FusionLockTest(unittest.TestCase):
    """2026-10-07: two servers (one per Claude session) were sending work to one Fusion when it hung."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "fusion.lock")
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
            p.stdout.close()

    def other_server(self):
        p = subprocess.Popen([sys.executable, "-c", HOLDER, self.path], stdout=subprocess.PIPE, text=True)
        self.procs.append(p)
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    def test_another_process_holding_it_gives_a_clear_message(self):
        p = self.other_server()
        lock = FusionLock(self.path, wait_s=0.5)
        with self.assertRaises(FusionBusy) as cm:
            with lock.hold("move_parts"):
                pass
        msg = str(cm.exception)
        # the holder's own PID (on Windows a venv python.exe is a launcher, so not always p.pid)
        m = re.search(r"Another Claude session is using Fusion \(server PID (\d+), doing route_pair", msg)
        self.assertTrue(m, msg)
        self.assertNotEqual(int(m.group(1)), os.getpid())
        self.assertIn("waited 0.5 s", msg)

    def test_a_dead_holder_leaves_no_stale_lock(self):
        p = self.other_server()
        p.kill()
        p.wait()
        lock = FusionLock(self.path, wait_s=2)
        with lock.hold("export"):                 # the OS dropped the dead process's lock
            self.assertEqual(lock.owner()["pid"], os.getpid())
        self.assertEqual(lock.owner(), {})        # owner note removed on release

    def test_waits_for_a_short_holder(self):
        p = self.other_server()
        threading.Timer(0.5, p.kill).start()
        with FusionLock(self.path, wait_s=10).hold("export"):
            pass

    def test_reentrant_in_one_process_and_exclusive_between_threads(self):
        lock = FusionLock(self.path, wait_s=0.3)
        order = []
        with lock.hold("write"):
            with lock.hold("export"):             # a write's inner calls take it again
                order.append("inner")

            def other_thread():
                with lock.hold("context"):
                    order.append("thread")
            t = threading.Thread(target=other_thread)
            t.start()
            time.sleep(0.1)
            order.append("outer still held")
        t.join(5)
        self.assertEqual(order, ["inner", "outer still held", "thread"])


class LockedWriteTest(unittest.TestCase):
    def test_write_refused_untouched_while_another_server_works(self):
        path = os.path.join(tempfile.mkdtemp(), "fusion.lock")
        p = subprocess.Popen([sys.executable, "-c", HOLDER, path], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "held")

            class Bridge:
                lock = FusionLock(path, wait_s=0.3)
                ran = []

                def call(self, op, args=None, **kw):
                    self.ran.append(op)
                    raise AssertionError("nothing may reach Fusion")
            b = Bridge()
            with self.assertRaises(WriteFailed) as cm:
                Session(b).verified_write("board", "MOVE 'R1' (1 1);", lambda a, c: (True, ""), schematic=False)
            self.assertIn("Another Claude session is using Fusion", str(cm.exception))
            self.assertIn("Nothing was written", str(cm.exception))
            self.assertEqual(b.ran, [])
        finally:
            p.kill()
            p.wait()
            p.stdout.close()


if __name__ == "__main__":
    unittest.main()
