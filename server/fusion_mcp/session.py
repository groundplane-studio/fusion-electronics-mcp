"""Design session: exports as the source of truth, and verified writes.

Every write follows: build commands -> run in Fusion -> export -> parse ->
verify the intended change (and nothing else unexpected) -> on mismatch run
UNDO and raise with what was observed. The bridge's command return value is
always empty in Fusion, so read-back is the only success signal.
"""

from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass
from typing import Callable

from fusion_offline import design as D
from fusion_offline import eagle

from .bridge import Bridge, BridgeOpError, describe


class WriteFailed(Exception):
    pass


@dataclass
class Snapshot:
    board_xml: bytes | None = None
    sch_xml: bytes | None = None

    def board(self) -> D.BoardDesign:
        return D.parse_board_design(D.read_xml(self.board_xml))

    def fab(self) -> eagle.Board:
        return eagle.parse_board(self.board_xml)

    def schematic(self) -> D.SchematicDesign:
        return D.parse_schematic_design(D.read_xml(self.sch_xml))


class Session:
    def __init__(self, bridge: Bridge | None = None):
        self.bridge = bridge or Bridge()

    # -- primitives ----------------------------------------------------------
    def context(self) -> dict:
        return self.bridge.call("context")

    def activate(self, kind: str) -> dict:
        return self.bridge.call("activate", {"kind": kind})

    def export(self, kind: str) -> bytes:
        res = self.bridge.call("export", {"kind": kind}, timeout=120)
        path = res["path"]
        try:
            with open(path, "rb") as f:
                return f.read()
        finally:
            with contextlib.suppress(OSError):
                os.remove(path)

    def snapshot(self, board: bool = True, schematic: bool = True) -> Snapshot:
        snap = Snapshot()
        if board:
            try:
                snap.board_xml = self.export("board")
            except BridgeOpError as ex:
                if ex.code != "not_linked":
                    raise
        if schematic:
            try:
                snap.sch_xml = self.export("schematic")
            except BridgeOpError as ex:
                if ex.code != "not_linked":
                    raise
        return snap

    def errors(self, kind: str) -> dict:
        return self.bridge.call("errors", {"kind": kind})

    @staticmethod
    def with_grid(commands: str) -> str:
        """Run commands on a 0.0001 mm grid and restore the user's grid after.
        Verified on 2705.1.15: MOVE snaps relative to the part's current
        (possibly off-grid) position, so a coarse grid moves parts to the wrong
        spot; and a bare GRID MM would permanently change the user's grid.
        GRID LAST restores the exact previous setting (unit, distance, alt)."""
        body = re.sub(r"(?<![A-Za-z])GRID\s+MM(?:\s+[\d.]+)?\s*;", "", commands, flags=re.I)
        body = re.sub(r"\s+", " ", body).strip()
        if re.fullmatch(r"(?:\s*(UNDO|REDO)\s*;)+\s*", body, re.I):
            return body
        return f"GRID MM 0.0001; {body} GRID LAST;"

    def run(self, commands: str, editor: str, answers: list[tuple[str, str]] | None = None,
            forms: list[dict] | None = None, timeout: float = 60.0, check_dialogs: bool = True) -> dict:
        commands = self.with_grid(commands)
        self.activate(editor)
        res = self.bridge.call("run", {"commands": commands, "editor": editor}, answers=answers, forms=forms,
                               timeout=timeout)
        # a question nobody expected got the watchdog's safe answer (No / Cancel): the command most
        # likely did not do what was meant, so say so instead of carrying on (a "different version
        # of device set ... update?" answered No silently blocked a library update, 2026-10-04)
        unexpected = [d for d in (res or {}).get("dialogs") or [] if not d.get("expected")]
        if unexpected and check_dialogs:
            raise WriteFailed("Fusion asked something this call did not expect and it got the safe answer: "
                              + "; ".join(describe(d) for d in unexpected)
                              + f". Check the design; commands sent: {commands}")
        return res

    def run_script(self, script: str, editor: str) -> dict:
        return self.bridge.call("run_script", {"script": script, "editor": editor}, timeout=180)

    def undo(self, editor: str) -> None:
        self.run("UNDO;", editor)

    # -- verified write ------------------------------------------------------
    def verified_write(self, editor: str, commands: str,
                       verify: Callable[[Snapshot, Snapshot], tuple[bool, str]],
                       board: bool = True, schematic: bool = True,
                       answers: list[tuple[str, str]] | None = None,
                       forms: list[dict] | None = None, timeout: float = 60.0) -> tuple[Snapshot, str]:
        """Run `commands` in `editor` (the add-in groups them into ONE undo
        step), then check verify(before, after). Returns (after, detail).

        On failure the change is undone, but only if the design actually
        changed: an UNDO after a no-op would revert the previous tool call.
        Dialogs Fusion raised (and how they were answered) are included in
        the error so the agent learns why."""
        before = self.snapshot(board, schematic)
        pre, commands, post = split_settings(commands)
        if pre:                                   # editor settings (e.g. SET WIRE_BEND) cannot be grouped:
            self.run(pre, editor, timeout=30)     # sent on their own, so the write stays ONE undo step
        try:
            # verified_write reports and undoes on unexpected dialogs itself
            res = self.run(commands, editor, answers=answers, forms=forms, timeout=timeout, check_dialogs=False) or {}
        finally:
            if post:
                self.run(post, editor, timeout=30)
        shown = list(res.get("dialogs", []))
        messages = list(res.get("messages", []))
        after = self.snapshot(board, schematic)
        ok, detail = verify(before, after)
        unexpected = [d for d in shown if not d.get("expected")]
        note = ("; Fusion showed " + "; ".join(describe(d) for d in shown)) if shown else ""
        if messages:
            note += "; Fusion said: " + " | ".join(messages)
        if ok and not unexpected:
            return after, detail + note
        changed = after.board_xml != before.board_xml or after.sch_xml != before.sch_xml
        if changed:
            self.undo(editor)
            restored = self.snapshot(board, schematic)
            clean = restored.board_xml == before.board_xml and restored.sch_xml == before.sch_xml
            how = "The change was undone" + ("" if clean else
                  " (warning: the design does not exactly match its state before this call; check it)")
        else:
            how = "Nothing was changed"
        reason = detail if not ok else "an unexpected dialog appeared"
        raise WriteFailed(f"{reason}{note}. {how}. Commands sent: {commands}")


def split_settings(commands: str) -> tuple[str, str, str]:
    """(leading SETs, the rest, trailing SETs). The add-in cannot group a write containing
    SET into one undo step (SET is in its NO_GROUP list), and a SET in the middle of a write
    then left a multi-step write that one UNDO only partly reverted. Leading
    and trailing SETs are pulled out; a SET in the middle is moved to the front, as these are
    editor settings (wire bend) meant for the whole write."""
    parts = [c.strip() for c in commands.split(";") if c.strip()]
    is_set = lambda c: c.split(None, 1)[0].upper() == "SET"
    body = [c for c in parts if not is_set(c)]
    sets = [c for c in parts if is_set(c)]
    if not sets:
        return "", commands, ""
    pre, post, seen = [], [], set()
    for c in sets:                    # first value of a setting applies to the write, a later one restores it
        key = " ".join(c.split()[:2]).upper()
        (post if key in seen else pre).append(c)
        seen.add(key)
    j = lambda xs: " ".join(c + ";" for c in xs)
    return j(pre), j(body), j(post)

