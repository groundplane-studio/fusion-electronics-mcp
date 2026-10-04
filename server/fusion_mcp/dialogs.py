"""Find, read and answer Fusion windows that interfere with automation.

`Electron.run` always returns an empty string, and Fusion talks back through
windows instead. Three kinds were observed on Fusion 2705.1.15 (Windows):

- message boxes: owned Qt top-level window titled "Fusion" with text and a
  few buttons ("Part 'R2' has no user definable value. Do you want to change
  it anyway?" [Yes/No]; "Merge net segment 'A' into given net 'B'?"; internal
  assertion boxes [OK]). They block the main thread until answered.
- form dialogs: owned window with inputs, e.g. "Name" (automation id
  cStringDialog: a name field, "this Segment" / "every Segment on this Sheet"
  radios, "Place label" checkbox, OK/Cancel) shown when renaming a net that
  has several segments. They block too.
- notification toasts: window titled "Fusion360" holding a
  QTNotificationMessage ("1 error(s)", "Unknown element: NOPE99"). They do
  not block; they are the only place Fusion reports why a command failed.

Policy: blocking windows get the SAFE answer (Cancel, No, Close, OK; never
Yes) unless the caller registered an expected answer for that exact window.
Toasts are only read. Enumeration uses ctypes (milliseconds); reading and
pressing use UI Automation through the PowerShell that ships with Windows,
so there are no dependencies. Other platforms: `supported()` is False and
the bridge falls back to a timeout that says a dialog may be open.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field

SAFE_ORDER = ["Cancel", "No", "Close", "OK", "Ok", "Abort", "Ignore"]
NEVER = {"Yes", "Yes to All", "Save", "Delete", "Overwrite", "Replace", "Continue"}

# NB: PowerShell variable names are case-insensitive; never reuse $A/$D/$el.
_PS_HEAD = r"""
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$A=[System.Windows.Automation.AutomationElement]; $D=[System.Windows.Automation.TreeScope]::Descendants
$el=$A::FromHandle([IntPtr]$Hwnd)
function Named($n){ $el.FindFirst($D,(New-Object System.Windows.Automation.PropertyCondition($A::NameProperty,$n))) }
"""

_PS_READ = _PS_HEAD + r"""
$o=@{ title=$el.Current.Name; ids=@(); texts=@(); buttons=@(); radios=@(); checks=@(); edits=@(); items=@(); others=0 }
foreach ($e in $el.FindAll($D,[System.Windows.Automation.Condition]::TrueCondition)) {
  $t=$e.Current.ControlType.ProgrammaticName; $n=$e.Current.Name
  if ($e.Current.AutomationId) { $o.ids += $e.Current.AutomationId }
  switch ($t) {
    'ControlType.Text'        { if ($n) { $o.texts += $n } }
    'ControlType.Button'      { if ($n) { $o.buttons += $n } }
    'ControlType.RadioButton' { $o.radios += @{ name=$n; on=$e.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Current.IsSelected } }
    'ControlType.CheckBox'    { $o.checks += @{ name=$n; on=($e.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern).Current.ToggleState -eq 'On') } }
    'ControlType.Edit'        { $v=''; try { $v=$e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value } catch {}; $o.edits += @{ name=$n; value=$v } }
    'ControlType.ListItem'    { if ($n) { $o.items += $n } }
    'ControlType.List'        { }
    'ControlType.Group'       { }
    'ControlType.Image'       { }
    default                   { $o.others++ }
  }
}
$o | ConvertTo-Json -Compress -Depth 4
"""

_PS_ACT = _PS_HEAD + r"""
$done=@()
foreach ($step in ($Actions | ConvertFrom-Json)) {
  $x = Named $step.target
  if (-not $x) { $done += "missing:" + $step.target; continue }
  switch ($step.do) {
    'select' { $x.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select() }
    'off'    { $p=$x.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern); if ($p.Current.ToggleState -eq 'On') { $p.Toggle() } }
    'on'     { $p=$x.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern); if ($p.Current.ToggleState -ne 'On') { $p.Toggle() } }
    'press'  { $x.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
    'activate' { $x.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
  }
  $done += $step.do + ":" + $step.target
}
$done -join ','
"""


def supported() -> bool:
    return sys.platform == "win32"


@dataclass
class Window:
    hwnd: int
    title: str
    cls: str

    @property
    def is_toast(self) -> bool:
        return self.title == "Fusion360" and "ToolSaveBits" in self.cls

    @property
    def is_dialog(self) -> bool:
        return self.cls.endswith("QWindowIcon")


@dataclass
class Seen:
    kind: str                      # message_box | form | toast | unknown
    title: str
    text: str
    buttons: list[str] = field(default_factory=list)
    answer: str | None = None      # what was pressed / filled
    expected: bool = False
    raw: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "title": self.title, "text": self.text, "buttons": self.buttons,
                "answer": self.answer, "expected": self.expected}


def windows(pid: int) -> list[Window]:
    """Visible top-level windows of `pid` owned by another window (i.e. not
    the main window itself), excluding the browser and message-tray panes."""
    if not supported():
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    out: list[Window] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd) and user32.GetWindow(hwnd, 4):
            title = ctypes.create_unicode_buffer(256)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 256)
            user32.GetClassNameW(hwnd, cls, 256)
            if title.value not in ("BROWSER",) and not title.value.startswith("Msg:QTView"):
                out.append(Window(int(hwnd), title.value, cls.value))
        return True

    user32.EnumWindows(proto(cb), 0)
    return out


def _ps(script: str, **params) -> str:
    args = " ".join(f"-{k} {v}" for k, v in params.items())
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
         f"& {{ param([long]$Hwnd, [string]$Actions) {script} }} {args}"],
        capture_output=True, text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return proc.stdout.strip()


def read(w: Window) -> dict:
    try:
        info = json.loads(_ps(_PS_READ, Hwnd=w.hwnd) or "{}")
    except Exception as ex:
        return {"error": str(ex)[:200], "texts": [], "buttons": []}
    for k in ("ids", "texts", "buttons", "radios", "checks", "edits", "items"):
        v = info.get(k)
        info[k] = [] if v is None else (v if isinstance(v, list) else [v])
    return info


def act(w: Window, actions: list[dict]) -> str:
    payload = json.dumps(actions).replace("'", "''")
    try:
        return _ps(_PS_ACT, Hwnd=w.hwnd, Actions=f"'{payload}'")
    except Exception as ex:
        return f"error:{ex}"[:200]


def _text(info: dict) -> str:
    return " ".join(t.replace("\r", " ").replace("\n", " ").strip() for t in info.get("texts", [])).strip()


def classify(w: Window, info: dict) -> str:
    if w.is_toast or any("QTNotificationMessage" in i for i in info.get("ids", [])):
        return "toast"
    inputs = len(info["radios"]) + len(info["checks"]) + len(info["edits"]) + info.get("others", 0)
    if info["buttons"] and inputs == 0 and len(info["buttons"]) <= 4:
        return "message_box"
    if info["buttons"]:
        return "form"
    return "unknown"


# A form answer: (title regex, required radio/label names, actions)
FORM_RENAME_THIS_SEGMENT = {
    "title": r"^Name$",
    "requires": ["this Segment"],
    "actions": [{"do": "select", "target": "this Segment"},
                {"do": "off", "target": "Place label"},
                {"do": "press", "target": "OK"}],
    "label": "renamed this segment only",
}

# ADD of a power symbol whose net name follows its value (allow_supply_override,
# e.g. GPLIB's power bars) asks for the value, prefilled with the default; keep it
# (OK) and set the real value with VALUE in a later script, which does not ask.
# Seen on 2705.1.15.
FORM_PUSH_3D = {
    "title": r"^PUSH TO 3D PCB$",
    "requires": [],
    "actions": [{"do": "press", "target": "Push"}],
    "label": "pushed the board to its 3D PCB with Fusion's current settings",
}

FORM_SUPPLY_VALUE = {
    "title": r"^Value$",
    "requires": [],
    "actions": [{"do": "press", "target": "OK"}],
    "label": "kept the power symbol's default value (set afterwards)",
}

FORM_RENAME_ALL_SEGMENTS = {
    "title": r"^Name$",
    "requires": ["every Segment on this Sheet"],
    "actions": [{"do": "select", "target": "every Segment on this Sheet"},
                {"do": "off", "target": "Place label"},
                {"do": "press", "target": "OK"}],
    "label": "renamed every segment on the sheet",
}


def handle(w: Window, answers: list[tuple[str, str]] | None = None,
           forms: list[dict] | None = None) -> Seen:
    """Read one window and answer it per the policy above."""
    info = read(w)
    kind = classify(w, info)
    seen = Seen(kind, info.get("title", w.title), _text(info), info.get("buttons", []), raw=info)
    if kind == "toast" or kind == "unknown":
        return seen
    if kind == "message_box":
        for pat, btn in answers or []:
            if re.search(pat, seen.text, re.I) and btn in seen.buttons:
                act(w, [{"do": "press", "target": btn}])
                seen.answer, seen.expected = btn, True
                return seen
    if kind == "form":
        names = {r.get("name") for r in info["radios"]} | {c.get("name") for c in info["checks"]}
        for f in forms or []:
            if re.search(f["title"], seen.title) and all(r in names for r in f["requires"]):
                act(w, f["actions"])
                seen.answer, seen.expected = f["label"], True
                return seen
    for btn in SAFE_ORDER:
        if btn in seen.buttons and btn not in NEVER:
            act(w, [{"do": "press", "target": btn}])
            seen.answer = btn
            break
    return seen


# -- keyboard focus -----------------------------------------------------------
# Fusion pulls itself to the foreground when one of its dialogs opens or a
# document is activated; whatever the person was typing (e.g. into the chat)
# then lands in Fusion as shortcuts. The bridge notes the foreground window
# before each operation and hands it back when Fusion took it.

def foreground() -> int:
    if not supported():
        return 0
    import ctypes
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def window_pid(hwnd: int) -> int:
    import ctypes
    from ctypes import wintypes
    p = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
    return int(p.value)


def give_back_focus(prev: int, fusion_pid: int) -> bool:
    """If Fusion took the foreground from window `prev` (not Fusion's own),
    return it there. No input is synthesised: the foreground thread's input
    is attached for the switch, the documented way for a background process."""
    if not supported() or not prev or not fusion_pid:
        return False
    import ctypes
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    cur = int(u.GetForegroundWindow() or 0)
    if not cur or cur == prev or window_pid(cur) != fusion_pid or window_pid(prev) == fusion_pid:
        return False
    if not u.IsWindow(prev):
        return False
    fg_thread = u.GetWindowThreadProcessId(cur, None)
    me = k.GetCurrentThreadId()
    u.AttachThreadInput(me, fg_thread, True)
    try:
        ok = bool(u.SetForegroundWindow(prev))
    finally:
        u.AttachThreadInput(me, fg_thread, False)
    return ok
