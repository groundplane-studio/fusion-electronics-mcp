"""JLCPCB's published limits (data/jlc_capabilities.json, with source and date), for checks
and defaults instead of numbers scattered through the code."""

from __future__ import annotations

import functools
import json
import os

PATH = os.path.join(os.path.dirname(__file__), "data", "jlc_capabilities.json")


@functools.lru_cache(maxsize=1)
def table() -> dict:
    with open(PATH, encoding="utf-8") as f:
        return json.load(f)


def limit(name: str, copper_layers: int = 2, which: str | None = None) -> float:
    """A limit in mm: `which` picks a column; else the 2-layer or multilayer value by
    copper_layers, else the value for all boards."""
    row = table()["limits"][name]
    if which:
        return float(row[which])
    if copper_layers > 2 and "multilayer" in row:
        return float(row["multilayer"])
    if "two_layer" in row:
        return float(row["two_layer"])
    return float(row["all"])
