"""Client for Fusion: through Autodesk's built-in MCP server or our add-in.

Two transports run the same operations (the add-in's op functions):
- builtin: Fusion's own MCP server (127.0.0.1:27182) runs them as scripts;
  nothing to install in Fusion (see builtin.py);
- addin: the FusionElectronicsMCP add-in's token-gated socket (bridge.json).
FUSION_MCP_TRANSPORT=builtin|addin forces one; by default the add-in is used
when it is running and the built-in server otherwise. The add-in is the
reliable one: on Fusion 2705 the built-in server does not save libraries (the
call returns, nothing is written), stops answering after about a minute, and
cancels any command still open at the end of a write, so steps that need a
Fusion dialog answered cannot run through it. Those operations refuse to run
on it (NEEDS_ADDIN) instead of quietly doing nothing. The dialog watchdog and
the focus guard run here, around either transport.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import uuid

from . import dialogs
from .builtin import BuiltinClient, BuiltinError
from .fusion_lock import LOCK, FusionBusy

INFO_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                         "fusion-electronics-mcp", "bridge.json")
PROTOCOL = 1
POLL_S = 1.0   # how often to look for a blocking dialog while a call is in flight


class BridgeUnavailable(Exception):
    """Fusion or the add-in is not running / not reachable."""


class BridgeOpError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


_BUILTIN = BuiltinClient()
# operations the built-in server cannot do properly (see the module docstring)
NEEDS_ADDIN = {"save", "push_3d"}


def addin_running(info_path: str | None = None) -> bool:
    """The add-in's socket (from bridge.json) accepts a connection."""
    try:
        with open(info_path or INFO_PATH, encoding="utf-8") as f:
            port = int(json.load(f)["port"])
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except (OSError, ValueError, KeyError):
        return False


class Bridge:
    def __init__(self, info_path: str = INFO_PATH, watch_dialogs: bool = True, keep_focus: bool | None = None):
        self.info_path = info_path
        # hand keyboard focus back when Fusion grabs it (FUSION_MCP_KEEP_FOCUS=0 turns it off)
        self.keep_focus = (os.environ.get("FUSION_MCP_KEEP_FOCUS", "1") != "0") if keep_focus is None else keep_focus
        self.watch_dialogs = watch_dialogs
        self.last_dialogs: list[dict] = []
        self._toasts: dict[int, str] = {}     # toast hwnd -> last text already reported
        self.lock = LOCK                      # one server process at a time talks to Fusion
        # DRC before and after every verified write, the reply listing only new errors
        # (FUSION_MCP_DRC_AFTER_WRITES=0 turns it off); FUSION_MCP_UNDO_ON_NEW_DRC=1 also undoes
        # a write that adds copper errors
        self.drc_after_writes = os.environ.get("FUSION_MCP_DRC_AFTER_WRITES", "1") != "0"
        self.undo_on_new_drc = os.environ.get("FUSION_MCP_UNDO_ON_NEW_DRC", "0") == "1"

    def transport(self) -> str:
        mode = os.environ.get("FUSION_MCP_TRANSPORT", "auto").strip().lower()
        if mode in ("builtin", "addin"):
            return mode
        if addin_running(self.info_path):
            return "addin"
        return "builtin" if _BUILTIN.available() else "addin"

    def _info(self) -> dict:
        if self.transport() == "builtin":
            if not getattr(Bridge, "_builtin_pid", 0):
                res = self._builtin_call("ping", {}, 30.0)
                Bridge._builtin_pid = int((res.get("result") or {}).get("pid") or 0)
            return {"pid": Bridge._builtin_pid, "protocol": PROTOCOL, "transport": "builtin"}
        try:
            with open(self.info_path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            raise BridgeUnavailable(
                "The Fusion add-in is not running (no bridge.json). Start Fusion and run the "
                "FusionElectronicsMCP add-in (Utilities > Add-Ins), or turn on Fusion's built-in "
                "MCP server.") from None

    @staticmethod
    def _builtin_call(op: str, args: dict, timeout: float) -> dict:
        try:
            return _BUILTIN.call(op, args, timeout)
        except BuiltinError as ex:
            return {"ok": False, "error": {"code": ex.code, "message": ex.message}}
        except OSError as ex:          # URLError, timeouts, connection refused
            raise BridgeUnavailable(f"Fusion's built-in MCP server did not answer ({ex}). Is Fusion running?") from None

    def call(self, op: str, args: dict | None = None, timeout: float = 60.0,
             answers: list[tuple[str, str]] | None = None, forms: list[dict] | None = None) -> dict:
        """Run one op while holding the lock every server process shares (see fusion_lock)."""
        try:
            with self.lock.hold(op):
                return self._call(op, args, timeout, answers, forms)
        except FusionBusy as ex:
            raise BridgeUnavailable(str(ex)) from None

    def _call(self, op: str, args: dict | None = None, timeout: float = 60.0,
              answers: list[tuple[str, str]] | None = None, forms: list[dict] | None = None) -> dict:
        """Run one add-in op. `answers` lists (regex, button) pairs for message
        boxes the caller expects (e.g. confirming a net merge the user asked
        for); `forms` lists form answers (see dialogs.FORM_*). Any other
        blocking window gets the safe answer. For `run`/`run_script`, new
        Fusion notification toasts (its only error channel) are returned under
        "messages"."""
        mode = self.transport()
        if mode == "builtin" and op in NEEDS_ADDIN:
            raise BridgeOpError("needs_addin",
                                f"'{op}' needs the FusionElectronicsMCP add-in: Fusion's built-in MCP server "
                                "does not do it reliably (library saves are silently dropped; Fusion's dialogs "
                                "are cancelled). Install it with `fusion-electronics-mcp install-addin` and run it "
                                "from Utilities > Add-Ins.")
        info = self._info()
        if info.get("protocol") != PROTOCOL:
            raise BridgeUnavailable(f"add-in protocol {info.get('protocol')} != server protocol {PROTOCOL}; "
                                    "update the add-in")
        self.last_dialogs = []
        pid = int(info.get("pid") or 0)
        watch = self.watch_dialogs and dialogs.supported() and pid
        deadline = time.monotonic() + timeout + 20
        prev_fg = dialogs.foreground() if (self.keep_focus and pid) else 0
        if prev_fg and dialogs.window_pid(prev_fg) == pid:
            prev_fg = 0                      # the person is in Fusion already: leave focus alone
        guard = {"on": bool(prev_fg), "returned": False, "dialogs": 0}

        def keep_focus():
            if not guard["on"]:
                return
            fg = dialogs.foreground()
            if not fg or fg == prev_fg or dialogs.window_pid(fg) != pid:
                return
            if guard["returned"] and len(self.last_dialogs) == guard["dialogs"]:
                guard["on"] = False          # Fusion came back with no new dialog: the person chose it
                return
            if dialogs.give_back_focus(prev_fg, pid):
                guard["returned"], guard["dialogs"] = True, len(self.last_dialogs)
        if mode == "builtin":
            box: dict = {}

            def work():
                try:
                    box["res"] = self._builtin_call(op, args or {}, timeout + 20)
                except BaseException as ex:          # surfaced on this thread
                    box["exc"] = ex
            th = threading.Thread(target=work, daemon=True)
            th.start()
            started = time.monotonic()
            try:
                while th.is_alive():
                    th.join(POLL_S)
                    if not th.is_alive():
                        break
                    if time.monotonic() > deadline:
                        raise BridgeUnavailable(f"No answer from Fusion within {timeout + 20:.0f}s; "
                                                "a dialog may be open in Fusion that needs a person to close it.")
                    if watch and time.monotonic() - started > POLL_S:
                        self._dismiss_dialogs(pid, answers or [], forms or [])
                    keep_focus()
            finally:
                keep_focus()
            if "exc" in box:
                raise box["exc"]
            return self._finish(box["res"], op, watch, pid)
        req = {"v": PROTOCOL, "id": uuid.uuid4().hex, "token": info["token"], "op": op,
               "args": args or {}, "timeout": timeout}
        try:
            with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=10) as s:
                s.sendall((json.dumps(req) + "\n").encode("utf-8"))
                s.settimeout(POLL_S)
                buf = b""
                started = time.monotonic()
                while not buf.endswith(b"\n"):
                    try:
                        chunk = s.recv(1 << 20)
                    except socket.timeout:
                        if time.monotonic() > deadline:
                            raise
                        if watch and time.monotonic() - started > POLL_S:
                            self._dismiss_dialogs(pid, answers or [], forms or [])
                        keep_focus()
                        continue
                    if not chunk:
                        break
                    buf += chunk
        except (ConnectionRefusedError, ConnectionResetError) as ex:
            raise BridgeUnavailable(
                f"Cannot reach the Fusion add-in on port {info.get('port')} (pid {info.get('pid')}): {ex}. "
                "Fusion may have closed or crashed; restart it and the FusionElectronicsMCP add-in.") from None
        except socket.timeout:
            raise BridgeUnavailable(f"No answer from Fusion within {timeout + 20:.0f}s; "
                                    "a dialog may be open in Fusion that needs a person to close it.") from None
        finally:
            keep_focus()
        if not buf:
            raise BridgeUnavailable("The Fusion add-in closed the connection without answering.")
        return self._finish(json.loads(buf.decode("utf-8")), op, watch, pid)

    def _finish(self, res: dict, op: str, watch, pid: int):
        if not res.get("ok"):
            err = res.get("error") or {}
            msg = err.get("message", "unknown error")
            if err.get("code") == "timeout" and "before Fusion started it" in msg:
                msg += (". Fusion never started this call: it is still busy with an earlier one, loading a "
                        "design, or showing a dialog (save/discard, sign-in, a newer version). Look at Fusion: "
                        "close the dialog or press Esc and wait until it responds. Calls will keep failing "
                        "this way until it does; don't retry in a loop.")
            if self.last_dialogs:
                msg += " | Fusion showed: " + "; ".join(describe(d) for d in self.last_dialogs)
            raise BridgeOpError(err.get("code", "error"), msg)
        result = res.get("result")
        if isinstance(result, dict):
            if self.last_dialogs:
                result["dialogs"] = self.last_dialogs
            if watch and op in ("run", "run_script"):
                msgs = self._new_toasts(pid)
                if msgs:
                    result["messages"] = msgs
        return result

    def _dismiss_dialogs(self, pid: int, answers: list[tuple[str, str]], forms: list[dict]) -> None:
        for w in dialogs.windows(pid):
            if w.is_toast or any(d.get("hwnd") == w.hwnd for d in self.last_dialogs):
                continue
            seen = dialogs.handle(w, answers, forms)
            if seen.kind in ("message_box", "form") or seen.text:
                d = seen.as_dict()
                d["hwnd"] = w.hwnd
                self.last_dialogs.append(d)

    def _new_toasts(self, pid: int) -> list[str]:
        """Text of notification toasts that appeared or changed since the last
        call. Toasts show a moment after the command returns, so look twice."""
        out: list[str] = []
        for delay in (0.3, 0.7):
            time.sleep(delay)
            for w in dialogs.windows(pid):
                if not w.is_toast:
                    continue
                text = dialogs._text(dialogs.read(w))
                if text and self._toasts.get(w.hwnd) != text:
                    self._toasts[w.hwnd] = text
                    out.append(text)
            if out:
                break
        return out


def describe(d: dict) -> str:
    ans = d.get("answer")
    what = f"{d.get('kind', 'dialog')} \"{d.get('title', '')}\""
    return (f"{what}: \"{d.get('text') or '(no text)'}\" [{'/'.join(d.get('buttons') or [])}]"
            + (f", answered {ans}" if ans else ", left open (could not answer it safely)"))
