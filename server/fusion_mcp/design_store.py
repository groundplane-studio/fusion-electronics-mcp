"""Per-design data the tools keep (length groups, net currents): one JSON file per design in
the per-user data folder, so it survives server restarts. It lives on this machine, not in the
Fusion design."""

from __future__ import annotations

import json
import os
import re

from . import data_dir


def path(design: str) -> str:
    safe = re.sub(r"[^\w.+-]+", "_", design or "unnamed").strip("_") or "unnamed"
    return data_dir("designs", safe + ".json")


def load(design: str) -> dict:
    try:
        with open(path(design), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(design: str, data: dict) -> str:
    p = path(design)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, p)
    return p
