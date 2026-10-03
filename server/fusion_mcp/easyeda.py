"""EasyEDA component data, cache-first (the footprints JLC places parts with).

The ONLY network access in this server besides 127.0.0.1, and only when a
tool that says so is called (check_jlc_orientation with fetch=true). It uses
the public, unofficial endpoint the EasyEDA web app and easyeda2kicad read.

Etiquette, because EasyEDA's CloudFront rate-limits bursts with 403s:
- every answer is cached on disk forever, so a part is fetched at most once;
- at least 15 s between live requests (FUSION_MCP_EASYEDA_INTERVAL_S), and none
  for 15 min after a 403;
- an honest client identity (generic library user agents are refused; this
  one is accepted as of 2026-10-03).
Cache: FUSION_MCP_EASYEDA_CACHE, else <per-user data folder>/fusion-electronics-mcp/
easyeda_cache. Files are the raw API responses ({"success", "result"}), so a
cache filled elsewhere (e.g. another machine) can simply be copied in.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

API = "https://easyeda.com/api/products/{code}/components?version=6.4.19.5"
from . import PROJECT_URL, __version__, data_dir

USER_AGENT = f"fusion-electronics-mcp/{__version__} (+{PROJECT_URL})"
MIN_INTERVAL_S = float(os.environ.get("FUSION_MCP_EASYEDA_INTERVAL_S", "15"))   # bursts get 403s
COOLDOWN_S = 900.0
_last = 0.0
_blocked_until = 0.0


class RateLimited(Exception):
    pass


def cache_dir() -> str:
    d = os.environ.get("FUSION_MCP_EASYEDA_CACHE") or data_dir("easyeda_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _path(code: str) -> str:
    code = code.strip().upper()
    if not code.startswith("C") or not code[1:].isdigit():
        raise ValueError(f"{code!r} is not a JLC/LCSC part number (C followed by digits)")
    return os.path.join(cache_dir(), f"{code}.json")


def cached(code: str) -> dict | None:
    """The cached component ('result'), or None. Never touches the network."""
    p = _path(code)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("result") or None


def fetch(code: str, timeout: float = 30.0) -> dict:
    """Cached component data, fetching it from easyeda.com once if needed."""
    global _last, _blocked_until
    hit = cached(code)
    if hit is not None:
        return hit
    now = time.time()
    if now < _blocked_until:
        raise RateLimited(f"EasyEDA asked us to slow down; no requests for {int(_blocked_until - now)} s more")
    wait = MIN_INTERVAL_S - (now - _last)
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(API.format(code=code.strip().upper()),
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    _last = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        if ex.code in (403, 429):
            _blocked_until = time.time() + COOLDOWN_S
            raise RateLimited(f"EasyEDA refused the request (HTTP {ex.code}); backing off "
                              f"{int(COOLDOWN_S / 60)} min. Cached parts still work.") from None
        raise
    if not data.get("result"):
        raise ValueError(f"EasyEDA has no component data for {code}")
    with open(_path(code), "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data["result"]
