"""Built-in transport: session loss from the protocol error only, whole-chain stream repair,
one timeout budget, and the MCP session kept on ordinary Fusion errors."""

import io
import json
import threading
import time
import types
import unittest

from fusion_mcp import builtin as B

from test_server import _fusion_writer_class


def ok(payload):
    text = json.dumps({"success": True, "message": "@@FEMCP@@" + json.dumps(payload)})
    return None, {"result": {"content": [{"text": text}]}}


def client(post):
    c = B.BuiltinClient("http://127.0.0.1:1/mcp")
    c._initialize = lambda timeout=5.0: setattr(c, "session", "s%d" % c._id)
    c._post = post
    return c


class SessionLossTest(unittest.TestCase):
    def test_script_output_saying_not_initialized_runs_once(self):
        calls = []

        def post(body, timeout):
            calls.append(body)
            return ok({"ok": True, "result": {"raw": "variable x not initialized"}})
        c = client(post)
        self.assertEqual(c.call("run", {}, 5)["result"]["raw"], "variable x not initialized")
        self.assertEqual(len(calls), 1)                  # a write must never be sent twice

    def test_protocol_error_still_reinitialises(self):
        self.assertTrue(B._session_lost({"error": {"message": "Session not initialized. Call 'initialize' first."}}))
        self.assertFalse(B._session_lost({"result": {"content": [{"text": "Session not initialized"}]}}))
        self.assertFalse(B._session_lost(None))


class SessionKeptTest(unittest.TestCase):
    def test_fusion_busy_keeps_the_session(self):
        def post(body, timeout):
            text = json.dumps({"success": False, "error": "Cannot perform 'script' while a command dialog is open"})
            return None, {"result": {"content": [{"text": text}]}}
        c = client(post)
        c.session = "s-keep"
        with self.assertRaises(B.BuiltinError) as cm:
            c.call("run", {}, 5)
        self.assertEqual(cm.exception.code, "fusion_busy")
        self.assertEqual(c.session, "s-keep")

    def test_protocol_error_clears_it(self):
        c = client(lambda body, timeout: (None, None))
        c.session = "s-old"
        with self.assertRaises(B.BuiltinError) as cm:
            c.call("run", {}, 5)
        self.assertEqual(cm.exception.code, "bridge_error")
        self.assertIsNone(c.session)


class TimeoutBudgetTest(unittest.TestCase):
    def test_time_waiting_for_the_previous_call_comes_off_the_timeout(self):
        seen = []

        def post(body, timeout):
            seen.append(timeout)
            return ok({"ok": True, "result": 1})
        c = client(post)
        c._lock.acquire()
        threading.Timer(0.5, c._lock.release).start()
        c.call("context", {}, 3)
        self.assertLess(seen[-1], 2.7)

    def test_no_time_left_is_busy(self):
        c = client(lambda body, timeout: ok({"ok": True, "result": 1}))
        c._lock.acquire()
        threading.Timer(0.3, c._lock.release).start()
        with self.assertRaises(B.BuiltinError) as cm:
            c.call("context", {}, 1.2)
        self.assertEqual(cm.exception.code, "fusion_busy")


class WholeChainRepairTest(unittest.TestCase):
    def test_every_layer_points_at_the_real_stream(self):
        ns: dict = {}
        exec(B.STREAM_REPAIR.split("import sys as _femcp_sys")[0], ns)
        real = io.StringIO()
        layers, out = [], real
        for _ in range(50):
            out = _fusion_writer_class()(out)
            layers.append(out)
        fake_sys = types.SimpleNamespace(stdout=out, stderr=real, __stdout__=real, __stderr__=real)
        ns["_femcp_repair_streams"](fake_sys)
        for layer in layers:
            self.assertIs(vars(layer)["_original"], real)
        fake_sys.stdout = layers[10]                     # the runner unwraps to an inner layer later
        fake_sys.stdout.write("x")
        self.assertEqual(real.getvalue(), "x")


if __name__ == "__main__":
    unittest.main()
