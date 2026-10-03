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
  scripts, which Autodesk allows mid-command.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from . import __version__

URL = os.environ.get("FUSION_MCP_BUILTIN_URL", "http://127.0.0.1:27182/mcp")
ADDIN_SOURCE = os.path.join(os.path.dirname(__file__), "addin", "FusionElectronicsMCP", "FusionElectronicsMCP.py")
MARK = "@@FEMCP@@"
# operations that only read the design (run as read-only scripts: allowed while
# the person is in the middle of a command)
READ_ONLY_OPS = {"ping", "context", "export", "design_rules", "layers", "pours", "list_designs", "errors"}
_BUSY = "while a command dialog is open"


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


def build_script(op: str, args: dict, read_only: bool) -> str:
    end = "" if read_only else "\n    _ui.terminateActiveCommand()   # an EAGLE command leaves its tool active"
    return _ops_source() + f'''

def run(_context: str):
    global _app, _ui
    _app = adsk.core.Application.get()
    _ui = _app.userInterface
    try:
        res = {{"ok": True, "result": OPS[{op!r}](json.loads({json.dumps(json.dumps(args))}))}}
    except BridgeError as ex:
        res = {{"ok": False, "error": {{"code": ex.code, "message": str(ex)}}}}{end}
    print({MARK!r} + json.dumps(res, default=str))
'''


class BuiltinClient:
    """Minimal MCP (streamable HTTP) client for Fusion's built-in server."""

    def __init__(self, url: str = URL):
        self.url = url
        self.session: str | None = None
        self._id = 0
        self._checked: tuple[float, bool] | None = None

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
        try:
            self._initialize(timeout=1.5)
            ok = True
        except (OSError, urllib.error.URLError, ValueError):
            ok = False
        self._checked = (now, ok)
        return ok

    def call(self, op: str, args: dict, timeout: float) -> dict:
        """Run one add-in operation; returns the add-in's {'ok', 'result' | 'error'}."""
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
                break
            except urllib.error.HTTPError as ex:
                if ex.code in (400, 404) and attempt == 1:   # session expired (Fusion restarted)
                    self.session = None
                    continue
                raise
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
            raise BuiltinError("fusion_error", err[-1500:])
        msg = inner.get("message") or ""
        i = msg.rfind(MARK)
        if i < 0:
            raise BuiltinError("bridge_error", f"no result from the operation: {msg[-500:]!r}")
        return json.loads(msg[i + len(MARK):].strip())
