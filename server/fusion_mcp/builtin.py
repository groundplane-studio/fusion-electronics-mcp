"""Transport through Autodesk's built-in Fusion MCP server (no add-in needed).

Fusion (2705.x) runs its own MCP server on http://127.0.0.1:27182/mcp. Its
fusion_mcp_execute tool, featureType "script", runs a Python script inside
Fusion on the main thread. We send the add-in's own operation code (the part
of FusionElectronicsMCP.py before its socket server) plus a small dispatcher,
so both transports run exactly the same operations, verified on 2705.1.15:
exports, writes, one undo step per write.

Things this transport has to handle (the add-in does not):
- an EAGLE command run from a script leaves its interactive tool active, and
  Autodesk's server refuses every non-read-only script while any command is
  active, so a write ends with UserInterface.terminateActiveCommand();
- when the PERSON is in the middle of a command, writes are refused with
  "Cannot perform 'script' while a command dialog is open": reported as
  fusion_busy, never cancelled on their behalf. Reads run as read-only
  scripts, which Autodesk allows mid-command;
- before each script Autodesk's runner wraps sys.stdout/sys.stderr in a fresh
  _NsSanitizedWriter (whose __getattr__ forwards to self._original) and does
  not always unwrap it, notably when scripts overlap. The wrappers nest, one
  per leaked run, until a single attribute lookup on stdout recurses a few
  hundred levels deep and overflows Fusion's 1 MB main-thread stack
  (RecursionError at "<string>", line 25, in __getattr__). From then on every
  print fails, so no script can return a result until Fusion restarts. Each
  script therefore starts by collapsing the chain (STREAM_REPAIR), and calls
  are serialised so they never overlap.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import __version__

URL = os.environ.get("FUSION_MCP_BUILTIN_URL", "http://127.0.0.1:27182/mcp")
ADDIN_SOURCE = os.path.join(os.path.dirname(__file__), "addin", "FusionElectronicsMCP", "FusionElectronicsMCP.py")
MARK = "@@FEMCP@@"
# operations that only read the design (run as read-only scripts: allowed while
# the person is in the middle of a command)
READ_ONLY_OPS = {"ping", "context", "export", "design_rules", "layers", "pours", "list_designs", "errors", "elements3d", "lib_device3d", "pcb3d_bodies"}
_BUSY = "while a command dialog is open"
_NO_SESSION = "not initialized"      # Fusion forgot our MCP session: initialize again

# Runs first in every script, before anything prints. Collapses stacked
# _NsSanitizedWriter layers (one per leaked run; each run defines a new class,
# so match by name) so the outermost wrapper writes straight to the first real
# stream. A layer without _original would make its __getattr__ recurse
# forever, so it is pointed at the interpreter's own stream instead.
STREAM_REPAIR = '''
def _femcp_repair_streams(_sys):
    for _name in ("stdout", "stderr"):
        _top = getattr(_sys, _name, None)
        if type(_top).__name__ != "_NsSanitizedWriter":
            continue
        _chain, _cur, _depth = [], _top, 0
        while type(_cur).__name__ == "_NsSanitizedWriter" and _depth < 100000:
            _chain.append(_cur)
            _nxt = vars(_cur).get("_original")
            if _nxt is None:
                _nxt = getattr(_sys, "__" + _name + "__", None)
                if _nxt is None:
                    import io
                    _nxt = io.StringIO()
            _cur, _depth = _nxt, _depth + 1
        # every layer, not just the top: a runner that unwraps one level later puts an inner
        # wrapper back as sys.stdout, and its own _original link must not lead into the old chain
        for _layer in _chain:
            if vars(_layer).get("_original") is not _cur:
                vars(_layer)["_original"] = _cur


import sys as _femcp_sys
_femcp_repair_streams(_femcp_sys)
'''


class BuiltinError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


_source_cache: tuple[float, str] | None = None


def _ops_source() -> str:
    """The add-in's operations: everything before its request handler."""
    global _source_cache
    mtime = os.path.getmtime(ADDIN_SOURCE)
    if _source_cache is None or _source_cache[0] != mtime:
        with open(ADDIN_SOURCE, encoding="utf-8") as f:
            src = f.read()
        _source_cache = (mtime, src[:src.index("class _Handler")])
    return _source_cache[1]


def _literal(text: str, width: int = 1000) -> str:
    """A Python string literal for `text`, split over lines of at most ~width chars
    (adjacent literals concatenate): Fusion's MCP server cut a 10 KB single line
    (a 50-pin part's build script) and the script failed to parse."""
    parts = [json.dumps(text[i:i + width]) for i in range(0, len(text), width)] or ['""']
    return "(\n        " + "\n        ".join(parts) + ")"


def build_script(op: str, args: dict, read_only: bool) -> str:
    end = "" if read_only else "\n    _ui.terminateActiveCommand()   # an EAGLE command leaves its tool active"
    return STREAM_REPAIR + _ops_source() + f'''

_ARGS = {_literal(json.dumps(args))}


def run(_context: str):
    global _app, _ui
    _app = adsk.core.Application.get()
    _ui = _app.userInterface
    try:
        res = {{"ok": True, "result": OPS[{op!r}](json.loads(_ARGS))}}
    except BridgeError as ex:
        res = {{"ok": False, "error": {{"code": ex.code, "message": str(ex)}}}}{end}
    print({MARK!r} + json.dumps(res, default=str))
'''


def _session_lost(resp) -> bool:
    """Fusion's server says our MCP session is gone. Only the protocol error counts: the
    script's own output (in the result) may contain any text, and a write whose output said
    "not initialized" was otherwise sent twice."""
    err = (resp or {}).get("error") if isinstance(resp, dict) else None
    if not isinstance(err, dict):
        return False
    return _NO_SESSION in str(err.get("message") or "").lower()


class BuiltinClient:
    """Minimal MCP (streamable HTTP) client for Fusion's built-in server."""

    def __init__(self, url: str = URL):
        self.url = url
        self.session: str | None = None
        self._id = 0
        self._checked: tuple[float, bool] | None = None
        # one script at a time: overlapping runs are what leak Fusion's stdout
        # wrappers, and _initialize must not swap the session mid-call
        self._lock = threading.Lock()

    def _post(self, body: dict, timeout: float) -> tuple[str | None, dict | None]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            sid = r.headers.get("Mcp-Session-Id")
            text = r.read().decode("utf-8")
        if not text.strip():
            return sid, None
        if text.lstrip().startswith("{"):
            return sid, json.loads(text)
        for line in text.splitlines():          # server-sent events: the JSON is in a data: line
            if line.startswith("data:"):
                return sid, json.loads(line[5:].strip())
        return sid, None

    def _initialize(self, timeout: float = 5.0) -> None:
        self.session = None
        self._id += 1
        sid, _ = self._post({"jsonrpc": "2.0", "id": self._id, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "fusion-electronics-mcp", "version": __version__}}},
                            timeout)
        self.session = sid
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout)

    def available(self) -> bool:
        """Fusion's built-in MCP server answers (cached for 30 s)."""
        now = time.monotonic()
        if self._checked and now - self._checked[0] < 30:
            return self._checked[1]
        if not self._lock.acquire(blocking=False):
            return True                      # a call is in flight, so the server answered
        try:
            self._initialize(timeout=1.5)
            ok = True
        except (OSError, urllib.error.URLError, ValueError):
            ok = False
        finally:
            self._lock.release()
        self._checked = (now, ok)
        return ok

    def call(self, op: str, args: dict, timeout: float) -> dict:
        """Run one add-in operation; returns the add-in's {'ok', 'result' | 'error'}."""
        started = time.monotonic()
        if not self._lock.acquire(timeout=timeout):
            raise BuiltinError("fusion_busy", f"a previous operation was still running in Fusion after {timeout:.0f}s; "
                               "retry once it finishes")
        try:
            # the wait for the previous call counts against this call's timeout, so the
            # worst case stays `timeout`, not twice it
            left = timeout - (time.monotonic() - started)
            if left <= 1.0:
                raise BuiltinError("fusion_busy", f"a previous operation kept Fusion busy for {timeout:.0f}s; "
                                   "retry once it finishes")
            return self._call(op, args, left)
        except BuiltinError as ex:
            if ex.code not in ("fusion_busy", "fusion_error", "fusion_stdout_corrupt"):
                self.session, self._checked = None, None     # a protocol problem: start a fresh MCP session
            raise
        except Exception:
            # transport or protocol failure (timeouts, HTTP errors, bad JSON): fresh MCP session next time
            self.session, self._checked = None, None
            raise
        finally:
            self._lock.release()

    def _call(self, op: str, args: dict, timeout: float) -> dict:
        read_only = op in READ_ONLY_OPS
        script = build_script(op, args or {}, read_only)
        obj = {"script": script}
        if read_only:
            obj["readOnly"] = True
        for attempt in (1, 2):
            if self.session is None:
                self._initialize()
            self._id += 1
            body = {"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                    "params": {"name": "fusion_mcp_execute", "arguments": {"featureType": "script", "object": obj}}}
            try:
                _, resp = self._post(body, timeout)
            except urllib.error.HTTPError as ex:
                if ex.code in (400, 404) and attempt == 1:   # session expired (Fusion restarted)
                    self.session = None
                    continue
                raise
            if attempt == 1 and _session_lost(resp):
                self.session = None                          # "Session not initialized. Call 'initialize' first."
                continue
            break
        if resp is None or "error" in resp and "result" not in resp:
            raise BuiltinError("bridge_error", f"Fusion's MCP server returned {resp!r}"[:500])
        content = resp["result"].get("content") or [{}]
        try:
            inner = json.loads(content[0].get("text") or "{}")
        except ValueError:
            inner = {"success": False, "error": content[0].get("text")}
        if not inner.get("success", False):
            err = str(inner.get("error") or inner)
            if _BUSY in err:
                raise BuiltinError("fusion_busy", "Fusion is in the middle of a command (a tool or command dialog "
                                   "is active). Finish or cancel it in Fusion, then retry.")
            if "RecursionError" in err and "__getattr__" in err:
                raise BuiltinError("fusion_stdout_corrupt", "Fusion's script output streams are nested too deep "
                                   "(Autodesk MCP server stdout wrappers leaked by earlier scripts) and this script "
                                   "overflowed the stack before it could repair them. Retry once; if it persists, "
                                   "restart Fusion or use FUSION_MCP_TRANSPORT=addin. Detail: " + err[-300:])
            raise BuiltinError("fusion_error", err[-1500:])
        msg = inner.get("message") or ""
        i = msg.rfind(MARK)
        if i < 0:
            raise BuiltinError("bridge_error", f"no result from the operation: {msg[-500:]!r}")
        return json.loads(msg[i + len(MARK):].strip())
