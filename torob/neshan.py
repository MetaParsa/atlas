"""Neshan (Iran's maps platform) for three facts the dataset can't give: where a typed place is, what a GPS point is
called, and how long the drive really takes (with traffic). Key from `NESHAN_API_KEY` only; never logged.

Every call is cached, has a short timeout and a daily cap, and returns None on any failure, so the app keeps its
own estimates when Neshan is off or slow. Place search isn't used (not enabled on our key).
"""

import os
import threading
import time
from datetime import date

import httpx

BASE = "https://api.neshan.org"
TIMEOUT_S = 3.0
DAILY_CAP = int(os.environ.get("NESHAN_DAILY_CAP", "3000"))
TEHRAN = (35.56, 35.83, 51.18, 51.62)   # the city itself (Pardis, Karaj, Parand are outside)

_lock = threading.Lock()
_cache: dict[tuple, tuple[float, object]] = {}
_day, _used = date.today(), 0
_http: httpx.Client | None = None


def enabled() -> bool:
    return bool(os.environ.get("NESHAN_API_KEY"))


def used_today() -> int:
    return _used


def _get(path: str, params: dict, ttl: float) -> dict | None:
    global _day, _used, _http
    key = (path, tuple(sorted(params.items())))
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        if date.today() != _day:
            _day, _used = date.today(), 0
        if not enabled() or _used >= DAILY_CAP:
            return None
        _used += 1
        if _http is None:
            _http = httpx.Client(timeout=TIMEOUT_S)
    try:
        r = _http.get(BASE + path, params=params, headers={"Api-Key": os.environ["NESHAN_API_KEY"]})
        data = r.json() if r.status_code == 200 else None
    except Exception:   # network, timeout, bad JSON: the caller keeps its own estimate
        data = None
    if isinstance(data, dict) and str(data.get("status", "OK")).upper() == "ERROR":
        data = None
    with _lock:
        _cache[key] = (now + (ttl if data is not None else 120), data)
        if len(_cache) > 5000:
            for k in list(_cache)[:1000]:
                _cache.pop(k, None)
    return data


def in_tehran(lat: float, lng: float) -> bool:
    return TEHRAN[0] <= lat <= TEHRAN[1] and TEHRAN[2] <= lng <= TEHRAN[3]


def geocode(place: str) -> tuple[float, float] | None:
    """A place the shopper typed («ونک», «پل مدیریت», «پاساژ علاءالدین») → (lat, lng), searched in Tehran."""
    place = " ".join(str(place).split())[:80]
    if len(place) < 2:
        return None
    def ask(address: str) -> tuple[float, float] | None:
        loc = (_get("/v6/geocoding", {"address": address}, ttl=7 * 86400) or {}).get("location") or {}
        try:
            return float(loc["y"]), float(loc["x"])
        except (KeyError, TypeError, ValueError):
            return None
    return ask(f"تهران، {place}")   # «میدان انقلاب» of Tehran, not of another city; «پردیس» still lands outside


def place_name(lat: float, lng: float) -> str | None:
    """A GPS point → a short readable name («کاووسیه، میدان ونک»), so the shopper can see it found the right spot."""
    d = _get("/v5/reverse", {"lat": f"{lat:.5f}", "lng": f"{lng:.5f}"}, ttl=86400) or {}
    hood = (d.get("neighbourhood") or "").strip()
    addr = (d.get("formatted_address") or "").replace("تهران،", "").strip(" ،")
    last = addr.split("،")[-1].strip() if addr else ""
    name = "، ".join(x for x in dict.fromkeys([hood, last]) if x)
    return name[:40] or None


def drive_minutes(origin: tuple[float, float], dests: list[tuple[float, float]]) -> list[int | None]:
    """Real driving time with traffic from one origin to up to 20 shops (one call); None where unknown."""
    if not dests:
        return []
    dests = dests[:20]
    o = f"{origin[0]:.5f},{origin[1]:.5f}"
    ds = "|".join(f"{a:.5f},{b:.5f}" for a, b in dests)
    d = _get("/v1/distance-matrix", {"type": "car", "origins": o, "destinations": ds}, ttl=900) or {}
    try:
        els = d["rows"][0]["elements"]
    except (KeyError, IndexError, TypeError):
        return [None] * len(dests)
    out = []
    for e in els[:len(dests)]:
        v = (e.get("duration") or {}).get("value") if str(e.get("status", "")).lower() == "ok" else None
        out.append(max(1, round(v / 60)) if isinstance(v, (int, float)) else None)
    return out + [None] * (len(dests) - len(out))
