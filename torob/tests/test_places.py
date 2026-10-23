"""Maps and «بورس» (torob/agents/places.py, the tile proxy and map endpoints, the hub turn)."""

from types import SimpleNamespace as NS

import pytest

from core.agent_loop import Emitter
from core.llm import FakeLLM, LLMClient
from torob import store
from torob.agents import places
from torob.models import Chip, Conditions, SessionState
from torob.router import rule_route, run_turn

needs_db = pytest.mark.skipif(not store.DB_PATH.exists(), reason="run torob.etl.build_db first")
EARBUDS = "هدفون، هدست و هندزفری"


def turn(state, **kw):
    llm = LLMClient([], {}, enabled=False, fake=FakeLLM(lambda r, m: rule_route(m[-1]["content"]).model_dump(mode="json")))
    em = Emitter("s")
    list(run_turn(llm, state, em, **kw))
    return em.events


def test_rule_router_reads_a_bourse_question():
    out = rule_route("بورس هندزفری کجاست؟")
    assert out.intent == "hub" and out.need.keywords == ["هندزفری"]
    assert rule_route("بورس کجاست").intent == "hub" and rule_route("بورس کجاست").need is None
    assert rule_route("لوازم برقی رو کجا بخرم").intent == "hub"


def test_hub_sentence_is_measured_and_honest_when_scattered():
    h = {"total": 120, "spread": False, "areas": [{"name": "شیخ هادی"}],
         "buildings": [{"kind": "پاساژ", "name": "علاالدین", "shops": 30}, {"kind": "پاساژ", "name": "چارسو", "shops": 12}]}
    s = places.hub_sentence("هندزفری", h)
    assert "پاساژ علاالدین (۳۰ فروشنده از ۱۲۰ در تهران)" in s and "چارسو (۱۲)" in s and "شیخ هادی" in s
    assert "بورس مشخصی نداره" in places.hub_sentence("سیم", {**h, "spread": True})
    assert "پیدا نکردم" in places.hub_sentence("x", {"total": 0, "buildings": [], "areas": [], "spread": True})


@needs_db
def test_hubs_from_the_dataset():
    h = places.hubs((EARBUDS,))
    assert h["total"] > 100 and not h["spread"]
    top = h["buildings"][0]
    assert top["shops"] >= h["buildings"][-1]["shops"] >= places.HUB_MIN_SHOPS and 35.6 < top["lat"] < 35.8
    assert sum(b["shops"] for b in h["buildings"]) <= h["total"]


@needs_db
def test_bourse_turn_then_search_there():
    s = SessionState()
    ev = dict(turn(s, text="بورس هندزفری کجاست؟"))
    hub = ev["hub"]
    assert hub["buildings"] and hub["text"].startswith("بورس «هندزفری»") and hub["categories"]
    go = next(c for c in hub["chips"] if c["tool"] == "go_hub")
    ev = turn(s, chip=Chip(**go))
    assert s.conditions.radius_km == 0.5 and s.conditions.center.label == go["args"]["label"]
    assert "product_cards" in [e for e, _ in ev]


@needs_db
def test_any_seller_of_the_selected_product_can_be_called_from_the_map():
    s = SessionState()
    ev = dict(turn(s, text="اسپیکر بلوتوثی"))
    pid = ev["product_cards"]["items"][0]["product_id"]
    turn(s, chip=Chip(label="x", kind="direct_call", tool="show_shops", args={"product_id": pid}))
    m = places.product_map(pid, s.conditions, [x["shop_id"] for x in s.shops_shown])
    off_card = next((p for p in m["points"] if p["shop_id"] not in s.call_ok_ids), None)
    if off_card is None:
        pytest.skip("every seller is on the cards")
    ev = [e for e, _ in turn(s, chip=Chip(label="x", kind="direct_call", tool="call_shop",
                                          args={"shop_id": off_card["shop_id"], "product_id": pid}))]
    assert "call_start" in ev
    ev = [e for e, _ in turn(s, chip=Chip(label="x", kind="direct_call", tool="call_shop",
                                          args={"shop_id": "shop_not_a_seller", "product_id": pid}))]
    assert ev == ["error", "done"]


def test_tiles_only_for_tehran_and_served_from_the_cache(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from torob import app as app_mod
    monkeypatch.setattr(app_mod, "appdb", lambda: NS(path=tmp_path / "app.db"))
    def no_network():
        raise AssertionError("must not fetch")
    monkeypatch.setattr(app_mod, "_tile_http", no_network)
    c = TestClient(app_mod.app)
    x0, x1, y0, y1 = app_mod._tile_range(15)
    cached = tmp_path / "tiles" / "osm" / "15" / str(x0) / f"{y0}.png"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"\x89PNG fake")
    r = c.get(f"/tiles/osm/15/{x0}/{y0}.png")
    assert r.status_code == 200 and r.content == b"\x89PNG fake" and "immutable" in r.headers["cache-control"]
    assert c.get(f"/tiles/osm/15/{x1 + 50}/{y0}.png").status_code == 404     # outside Tehran
    assert c.get(f"/tiles/osm/9/{x0}/{y0}.png").status_code == 404           # zoomed out too far
    assert c.get(f"/tiles/carto/15/{x0}/{y0}.png").status_code == 404        # not a source we proxy


@needs_db
def test_map_endpoints_check_what_is_on_screen():
    from fastapi.testclient import TestClient
    from torob import app as app_mod
    c = TestClient(app_mod.app)
    assert c.get("/api/map/product/product_x").status_code == 404
    assert c.get("/api/map/category", params={"c": "دسته‌ای که نیست"}).status_code == 404
    d = c.get("/api/map/category", params={"c": EARBUDS}).json()
    assert d["dots"] and d["buildings"] and "center" not in d


@needs_db
def test_after_a_bourse_question_the_judge_hears_the_item_not_the_place():
    s = SessionState()
    turn(s, text="بورس هندزفری کجاست؟")
    assert [h["content"] for h in s.history if h["role"] == "user"] == ["هندزفری"]


@needs_db
def test_places_the_shopper_names_move_the_search():
    from torob.router import apply_patch
    s = SessionState()
    assert apply_patch(s, {"area": "تجریش"}) == ["center"] and s.conditions.center.lat > 35.78
    assert apply_patch(s, {"area": "جایی که نیست"}) == []                       # unknown: nothing moves
    assert apply_patch(s, {"budget_max": 500}) == ["budget_max"] and s.conditions.budget_max == 500_000   # «۵۰۰ تومن»
    apply_patch(s, {"budget_max": 2})
    assert s.conditions.budget_max == 2_000_000                               # «۲ تومن»


@needs_db
def test_cheap_preference_brings_cheaper_picks():
    from torob.agents import advisor
    from torob.models import Need
    base = Need(product_type="ساعت هوشمند", keywords=["ساعت هوشمند"])
    any_ = advisor.pick_products(base, Conditions())
    cheap = advisor.pick_products(base.model_copy(update={"price_pref": "cheap"}), Conditions())
    top = lambda r: max(p.price for p in r.picks)
    assert cheap.picks and top(cheap) <= top(any_)
