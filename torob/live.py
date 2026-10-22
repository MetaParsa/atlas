"""Live prices from torob.com's public endpoints: on demand, cached and polite, next to the dataset, never instead of it.

The dataset's prices are a week old (and some much older). For the products and shops on screen, this asks Torob
what they cost *now*:

- product: `base-product/search` gives each product's lowest price today (online and in person) and its seller count.
  The dataset title is searched and only an exact match counts (same model codes, close title); otherwise nothing.
- shops: `base-product/map/sellers` gives every in-person seller with its live price, how long ago it changed and
  its real coordinates. Dataset shops have real coordinates too, so a shop card gets Torob's live price only when
  exactly one live seller sits at that spot. In a crowded passage there is no reliable match, so the card says only
  what is true: how many sellers in that building have it on Torob now, and from what price.
  Real shop names are never shown: the dataset hides them on purpose.

Politeness (Torob has no official API): one request every 2 s across the server, a daily cap, an hour's cache for
prices and a day for matches, short timeouts, and `contact-info` is never called (Torob logs it as a buy click).
"""

import os
import threading
import time
from datetime import date

import httpx

from core.text import normalize_fa, to_latin_digits
from torob import search
from torob.logic.geo import haversine_m

BASE = "https://api.torob.com/v4/base-product"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; dastyar-kharid-hozoori/1.0)", "Accept": "application/json"}
MIN_GAP_S = 2.0            # at most one request every 2 seconds, whole server
MAX_WAIT_S = 6.0           # a caller waits at most this long for its turn, then gets «busy»
DAILY_CAP = int(os.environ.get("TOROB_LIVE_DAILY_CAP", "400"))
TTL_PRICE, TTL_SELLERS, TTL_MATCH, TTL_FAIL = 3600, 1800, 86400, 120
SAME_SPOT_M = 30           # a live seller this close to a dataset shop is the same shop (if it's the only one)
TEHRAN = (35.55, 35.85, 51.10, 51.65)   # lat_min, lat_max, lon_min, lon_max


class Polite:
    """A global gap between requests plus a daily cap. Never sleeps longer than MAX_WAIT_S."""

    def __init__(self, gap: float = MIN_GAP_S, cap: int = DAILY_CAP):
        self.gap, self.cap = gap, cap
        self._lock = threading.Lock()
        self._next = 0.0
        self._day, self.used_today = date.today(), 0

    def acquire(self) -> bool:
        with self._lock:
            if date.today() != self._day:
                self._day, self.used_today = date.today(), 0
            if self.used_today >= self.cap:
                return False
            now = time.monotonic()
            wait = max(0.0, self._next - now)
            if wait > MAX_WAIT_S:
                return False
            self._next = max(now, self._next) + self.gap
            self.used_today += 1
        if wait:
            time.sleep(wait)
        return True


CURRENT: "LiveTorob | None" = None   # set by the app; the router reads cached prices through it


class LiveTorob:
    def __init__(self, cache, transport: httpx.BaseTransport | None = None, enabled: bool | None = None):
        """`cache`: an object with live_get(key, max_age) -> dict | None and live_put(key, dict) (the app DB)."""
        self.cache = cache
        self.enabled = os.environ.get("TOROB_LIVE", "1") == "1" if enabled is None else enabled
        self.polite = Polite()
        self._http = httpx.Client(headers=HEADERS, timeout=8, follow_redirects=True, transport=transport)

    # --- one cached, polite GET -------------------------------------------------------------------------------

    def _get(self, key: str, ttl: int, path: str, params: dict) -> dict | None:
        """Cached JSON, or None (off, over the cap, busy, or Torob didn't answer). Failures are cached briefly too,
        so a struggling endpoint isn't asked again on every card."""
        hit = self.cache.live_get(key, ttl)
        if hit is not None:
            return hit.get("data")
        if self.cache.live_get(key + "|fail", TTL_FAIL) is not None or not self.enabled or not self.polite.acquire():
            return None
        try:
            r = self._http.get(f"{BASE}/{path}", params=params)
            data = r.json() if r.status_code == 200 else None
        except Exception:
            data = None
        if data is None:
            self.cache.live_put(key + "|fail", {})
        else:
            self.cache.live_put(key, {"data": data})
        return data

    # --- product: today's lowest price on Torob ----------------------------------------------------------------

    def product(self, product_id: str, title: str) -> dict:
        """{"status": ok | no_match | unavailable, "price_min", "sellers", "torob_title", "url"}."""
        q = title.split("|")[0].strip()[:90]
        data = self._get(f"search|{normalize_fa(q)}", TTL_PRICE, "search/",
                         {"page": 0, "sort": "popularity", "size": 8, "query": q, "q": q, "source": "next_desktop"})
        if data is None:
            return {"status": "unavailable"}
        hit = match_product(title, data.get("results") or [])
        if hit is None:
            return {"status": "no_match"}
        self.cache.live_put(f"prk|{product_id}", {"prk": hit["random_key"]})
        return {"status": "ok", "price_min": int(hit["price"]), "sellers": _shop_count(hit.get("shop_text")),
                "torob_title": hit.get("name1"), "url": "https://torob.com" + (hit.get("web_client_absolute_url") or "")}

    # --- shops: live in-person prices, matched to dataset shops by location -----------------------------------

    def shops(self, product_id: str, title: str, shops: list[dict]) -> dict:
        """{"status", "tehran": {"count", "min", "max"},
            "by_shop": {shop_id: {"price", "changed", "open_now"} | {"spot_count", "spot_min"}}}."""
        prk = (self.cache.live_get(f"prk|{product_id}", TTL_MATCH) or {}).get("prk")
        if not prk:
            p = self.product(product_id, title)
            if p["status"] != "ok":
                return {"status": p["status"]}
            prk = (self.cache.live_get(f"prk|{product_id}", TTL_MATCH) or {}).get("prk")
        data = self._get(f"sellers|{prk}", TTL_SELLERS, "map/sellers/", {"prk": prk})
        if data is None:
            return {"status": "unavailable"}
        live = [s for s in (data.get("result") or []) if _in_tehran(s) and s.get("price") and not s.get("is_price_unreliable")]
        prices = sorted(int(s["price"]) for s in live)
        out = {"status": "ok", "tehran": {"count": len(live), "min": prices[0] if prices else None,
                                          "max": prices[-1] if prices else None}, "by_shop": {}}
        for shop in shops:
            near = [s for s in live if haversine_m(shop["lat"], shop["lon"], s["location"]["lat"], s["location"]["lon"]) <= SAME_SPOT_M]
            if len(near) == 1:   # one live seller at this spot: the same shop. Several (a passage): no guess.
                s = near[0]
                out["by_shop"][shop["shop_id"]] = {"price": int(s["price"]), "changed": s.get("last_price_change_date"),
                                                   "open_now": s.get("is_open")}
            elif near:
                out["by_shop"][shop["shop_id"]] = {"spot_count": len(near), "spot_min": min(int(s["price"]) for s in near)}
        return out


    def cached_shop_price(self, product_id: str, shop: dict) -> int | None:
        """The live price for one dataset shop, from cache only (no request): what the shop cards already showed."""
        prk = (self.cache.live_get(f"prk|{product_id}", TTL_MATCH) or {}).get("prk")
        data = (self.cache.live_get(f"sellers|{prk}", TTL_SELLERS) or {}).get("data") if prk else None
        if not data:
            return None
        near = [s for s in (data.get("result") or []) if _in_tehran(s) and s.get("price") and not s.get("is_price_unreliable")
                and haversine_m(shop["lat"], shop["lon"], s["location"]["lat"], s["location"]["lon"]) <= SAME_SPOT_M]
        return int(near[0]["price"]) if len(near) == 1 else None


def match_product(title: str, results: list[dict]) -> dict | None:
    """The search result that is this exact product: every model code of the dataset title appears in it and the
    titles are close. Ads, duplicates and priceless results don't count."""
    want = search.title_tokens(normalize_fa(title.split("|")[0]))
    codes = {c.lower() for c in search.model_tokens(title)}
    best, best_score = None, 0.0
    for r in results:
        if r.get("is_adv") or not r.get("price") or not r.get("random_key"):
            continue
        name = r.get("name1") or ""
        have = search.title_tokens(normalize_fa(name))
        if codes and not codes <= {c.lower() for c in search.model_tokens(name)}:
            continue
        score = len(want & have) / max(1, len(want | have))
        if score > best_score:
            best, best_score = r, score
    return best if best_score >= 0.5 else None


def _in_tehran(s: dict) -> bool:
    loc = s.get("location") or {}
    lat, lon = loc.get("lat"), loc.get("lon")
    return lat is not None and lon is not None and TEHRAN[0] <= lat <= TEHRAN[1] and TEHRAN[2] <= lon <= TEHRAN[3]


def _shop_count(text: str | None) -> int | None:
    """«در ۳۸۲ فروشگاه» → 382."""
    digits = "".join(ch for ch in to_latin_digits(text or "") if ch.isdigit())
    return int(digits) if digits else None
