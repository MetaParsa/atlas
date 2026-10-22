"""Live Torob prices (torob/live.py) against a fake Torob: matching, caching, politeness, location matching."""

import time

import httpx

from torob import live
from torob.live import LiveTorob, Polite, match_product

TITLE = "هدفون بلوتوثی انکر Soundcore R50i مدل A3949"
SEARCH = {"results": [
    {"name1": "هندزفری بلوتوثی انکر مدل Soundcore R50i NC A3959", "price": 984000, "random_key": "nc-ad", "is_adv": True},
    {"name1": "هندزفری بلوتوثی انکر مدل Soundcore R50i NC A3959", "price": 4079600, "random_key": "nc", "is_adv": False,
     "shop_text": "در ۵۲۶ فروشگاه", "web_client_absolute_url": "/p/nc/"},
    {"name1": "هدفون بلوتوثی انکر Soundcore R50i مدل A3949", "price": 3428631, "random_key": "r50i", "is_adv": False,
     "shop_text": "در ۳۸۲ فروشگاه", "web_client_absolute_url": "/p/r50i/"},
]}
AT = (35.6935, 51.4155)   # a dataset shop's coordinates
SELLERS = {"result": [
    {"price": 3300000, "last_price_change_date": "۲۵ دقیقه پیش", "is_open": True, "location": {"lat": AT[0], "lon": AT[1] + 0.0001}},
    {"price": 3500000, "location": {"lat": 35.70, "lon": 51.40}},                                    # elsewhere in Tehran
    {"price": 3428600, "location": {"lat": 36.29, "lon": 59.59}},                                    # Mashhad: not Tehran
    {"price": 100, "is_price_unreliable": True, "location": {"lat": 35.69, "lon": 51.41}},          # unreliable: ignored
]}


class Cache:
    def __init__(self):
        self.rows = {}

    def live_get(self, key, max_age):
        v = self.rows.get(key)
        return v[0] if v and time.time() - v[1] <= max_age else None

    def live_put(self, key, value):
        self.rows[key] = (value, time.time())


def client(handler, cache=None):
    lv = LiveTorob(cache or Cache(), transport=httpx.MockTransport(handler), enabled=True)
    lv.polite = Polite(gap=0, cap=50)
    return lv


def torob(calls, status=200):
    def handler(req: httpx.Request):
        calls.append(req.url.path)
        assert "contact-info" not in req.url.path                 # never: Torob logs it as a buy click
        body = SEARCH if req.url.path.endswith("/search/") else SELLERS
        return httpx.Response(status, json=body)
    return handler


def test_only_the_exact_product_matches():
    assert match_product(TITLE, SEARCH["results"])["random_key"] == "r50i"           # not the NC variant, not the ad
    assert match_product("هدفون بلوتوثی انکر Soundcore Q20i", SEARCH["results"]) is None


def test_product_price_is_cached_and_failures_are_not_retried_at_once():
    calls = []
    lv = client(torob(calls))
    p = lv.product("product_1", TITLE)
    assert p == {"status": "ok", "price_min": 3428631, "sellers": 382, "torob_title": TITLE, "url": "https://torob.com/p/r50i/"}
    lv.product("product_1", TITLE)
    assert len(calls) == 1                                       # second answer from the cache
    calls, lv = [], client(torob([], status=503))
    assert lv.product("product_2", TITLE)["status"] == "unavailable"
    assert lv.product("product_2", TITLE)["status"] == "unavailable" and lv.polite.used_today == 1   # not asked again


def test_shop_gets_a_live_price_only_when_one_seller_is_at_that_spot():
    lv = client(torob([]))
    shops = [{"shop_id": "shop_a", "lat": AT[0], "lon": AT[1]}, {"shop_id": "shop_b", "lat": 35.75, "lon": 51.30}]
    out = lv.shops("product_1", TITLE, shops)
    assert out["tehran"] == {"count": 2, "min": 3300000, "max": 3500000}                  # Mashhad and unreliable left out
    assert out["by_shop"] == {"shop_a": {"price": 3300000, "changed": "۲۵ دقیقه پیش", "open_now": True}}
    assert lv.cached_shop_price("product_1", shops[0]) == 3300000
    crowded = dict(SELLERS, result=SELLERS["result"] + [{"price": 3350000, "location": {"lat": AT[0], "lon": AT[1]}}])
    lv2 = client(lambda req: httpx.Response(200, json=SEARCH if req.url.path.endswith("/search/") else crowded))
    assert lv2.shops("product_1", TITLE, shops[:1])["by_shop"] == {"shop_a": {"spot_count": 2, "spot_min": 3300000}}  # no guess
    assert lv2.cached_shop_price("product_1", shops[0]) is None


def test_daily_cap_and_switch_are_respected():
    p = Polite(gap=0, cap=2)
    assert p.acquire() and p.acquire() and not p.acquire()
    calls = []
    off = LiveTorob(Cache(), transport=httpx.MockTransport(torob(calls)), enabled=False)
    assert off.product("product_1", TITLE)["status"] == "unavailable" and calls == []


def test_simulated_call_quotes_the_live_price():
    from datetime import datetime
    from types import SimpleNamespace as NS
    from torob.agents.call import make_truth
    o = NS(shop={"shop_id": "shop_a", "hours": "{}"}, price_min=3_000_000, priced=True, changed_at="2026-10-01T10:00:00+03:30",
           payments=[])
    now = datetime.fromisoformat("2026-10-22T12:00:00+03:30")
    o.shop.update({"floor": None, "unit": None})
    assert make_truth(o, {"product_id": "p"}, now, live_price=3_300_000).price_today == 3_300_000
    assert make_truth(o, {"product_id": "p"}, now).price_today != 3_300_000


def test_live_endpoints_check_input(monkeypatch):
    from fastapi.testclient import TestClient
    from torob import app as app_mod
    monkeypatch.setattr(app_mod, "live", lambda: client(torob([])))
    c = TestClient(app_mod.app)
    assert c.get("/api/live/product/" + "x" * 80).status_code == 422
    assert c.get("/api/live/shops/p?ids=" + ",".join(["y" * 70])).status_code in (404, 422)
