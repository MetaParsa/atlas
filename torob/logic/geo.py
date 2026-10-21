"""Distances. Travel time is an estimate by default (spec §0.2 R13): straight line × 1.3 at walking speed."""

import math

WALK_KMH = 4.5
CAR_KMH = 18.0
DETOUR = 1.3


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def estimate_minutes(meters: float, mode: str = "walk") -> int:
    kmh = WALK_KMH if mode == "walk" else CAR_KMH
    return max(1, round(meters * DETOUR / 1000 / kmh * 60))


def bbox(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """Cheap SQL prefilter box around a point (then filter exactly with haversine)."""
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * math.cos(math.radians(lat)))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon
