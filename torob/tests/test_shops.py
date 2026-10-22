"""S3 shop finder on the real dataset with a pinned Tehran clock (no LLM)."""

import time
from datetime import datetime

import pytest

from core.agent_loop import Emitter
from core.llm import FakeLLM, LLMClient
from torob import clock, store
from torob.agents import shops as S
from torob.logic.geo import estimate_minutes
from torob.models import Chip, Conditions, SessionState
from torob.router import rule_route, run_turn

pytestmark = pytest.mark.skipif(not store.DB_PATH.exists(), reason="run torob.etl.build_db first")
THU = datetime(2026, 10, 22, 18, 5)
FRI = datetime(2026, 10, 23, 11, 0)


@pytest.fixture(autouse=True)
def pinned_clock():
    clock.set_now(THU)
    yield
    clock.set_now(None)


@pytest.fixture(scope="module")
def charger():
    return store.get().execute("SELECT product_id FROM products WHERE title = 'شارژر اپل توان 20 وات سه پین' "
                               "ORDER BY sellers_total DESC").fetchone()[0]


def cards(res):
    return [S.card(o, res.product, Conditions(), clock.now()) for o in res.items]


def test_top_three_honest_rows(charger):
    t = time.perf_counter()
    res = S.find_shops(charger, Conditions())
    assert time.perf_counter() - t < 0.3
    assert 1 <= len(res.items) <= 3 and res.counts["found_tehran"] == 53 and res.counts["found"] == 51 and res.counts["suspicious_hidden"] >= 1
    for c in cards(res):
        assert c["stock_label"] == "موجودی: تأیید نشده" and c["stock"] == "listed"     # data never confirms stock
        assert c["price"]["text"].startswith(("قیمت ثبت‌شده", "قیمت ثبت نشده")) and c["price"]["age"]
        assert "(تخمینی)" in c["visit"]["text"]
        assert len(c["badges"]) <= 2


def test_known_open_ranks_before_unknown_and_closed_excluded(charger):
    res = S.find_shops(charger, Conditions(arrive_at=datetime(2026, 10, 22, 20, 30)))
    states = [o.open_state for o in res.items + res.more]
    assert "no" not in states
    rank = {"yes": 0, "tight": 0, "unknown": 1}
    assert [rank[s] for s in states] == sorted(rank[s] for s in states)


def test_walk_estimate_matches_distance(charger):
    for o in S.find_shops(charger, Conditions()).items:
        assert o.minutes == estimate_minutes(o.meters, o.mode)


def test_badges_are_unique_facts(charger):
    res = S.find_shops(charger, Conditions())
    for badge, key in (("cheapest", lambda o: o.price_min), ("nearest", lambda o: o.minutes)):
        holders = [o for o in res.items if badge in o.badges]
        assert len(holders) <= 1
        if holders:
            others = [key(o) for o in res.items if o is not holders[0] and key(o) is not None]
            assert all(key(holders[0]) < v for v in others)


def test_same_building_count_is_real(charger):
    res = S.find_shops(charger, Conditions())
    o = next((o for o in res.items if o.same_building_others), None)
    if o:
        n = store.get().execute("SELECT count(DISTINCT o.shop_id) FROM offers o JOIN shops s USING (shop_id) "
                                "WHERE o.product_id = ? AND s.building_gid = ?", (charger, o.shop["building_gid"])).fetchone()[0]
        assert o.same_building_others <= n - 1


def test_credit_filters(charger):
    res = S.find_shops(charger, Conditions(payment="credit"))
    assert res.items and all(o.payments for o in res.items + res.more)
    res = S.find_shops(charger, Conditions(payment="credit_in_person"))
    assert all(any(ch == "in_person" for _, ch in o.payments) for o in res.items + res.more)


def test_relax_exactly_one_condition_with_chip(charger):
    cond = Conditions(payment="credit_in_person", fast_delivery=True, radius_km=0.3)
    res = S.find_shops(charger, cond)
    assert res.relaxed and res.relaxed["chip"]["tool"] == "set_conditions"
    assert len(res.relaxed["chip"]["args"]) == 1 and res.notes


def test_friday_shows_next_opening(charger):
    clock.set_now(FRI)
    res = S.find_shops(charger, Conditions(open="any"))
    closed = [o for o in res.items + res.more if o.open_state == "no"]
    assert closed and all(o.next_open is None or o.next_open > FRI.replace(tzinfo=o.next_open.tzinfo) for o in closed)
    texts = [S.visit_line(o, clock.now()) for o in closed]
    assert any("باز می‌کنه" in t for t in texts)


def test_arrival_time_already_past_means_tomorrow(charger):
    res = S.find_shops(charger, Conditions(arrive_at=datetime(2026, 10, 22, 10, 0)))   # 10:00 when it's 18:05
    assert all(o.arrival.date() == datetime(2026, 10, 23).date() for o in res.items + res.more)


def test_category_only_shops_are_not_sellers(charger):
    res = S.find_shops(charger, Conditions())
    sellers = {o.shop["shop_id"] for o in res.items + res.more}
    assert res.category_only and not sellers & {s["shop_id"] for s in res.category_only}
    assert all(s["stock_label"].endswith("موجودی نامعلوم") for s in res.category_only)
    assert S.shop_categories_for("شارژر گوشی")      # learned broad categories exist


def test_where_line_uses_parsed_fields_only():
    assert S.where_line({"building_kind": "پاساژ", "building": "علاءالدین", "floor": "۲", "unit": "214"}) == \
        "پاساژ علاءالدین، طبقه ۲، واحد ۲۱۴"
    assert S.where_line({"floor": "همکف", "plaque": "7"}) == "همکف، پلاک ۷"
    assert S.where_line({"address": "خیابان X"}) == "خیابان X"


# --- through the router ------------------------------------------------------------------------------------------

def turn(state, **kw):
    fake = FakeLLM(lambda role, msgs: rule_route(msgs[-1]["content"]).model_dump(mode="json"))
    em = Emitter("s")
    list(run_turn(LLMClient([], {"router": "x"}, enabled=False, fake=fake), state, em, **kw))
    return em.events, fake


def test_router_show_shops_then_refine_then_call_guard():
    s = SessionState()
    events, _ = turn(s, text="شارژر اصل آیفون")
    pid = dict(events)["product_cards"]["items"][0]["product_id"]
    events, fake = turn(s, chip=Chip(label="x", kind="direct_call", tool="show_shops", args={"product_id": pid}))
    block = dict(events)["shop_cards"]
    assert fake.calls == [] and block["items"] and block["disclaimer"] and block["demo_time"] == "18:05"
    status = [d["text"] for e, d in events if e == "status" and d.get("done")][0]
    assert "ثبت کرده‌اند" in status and "نزدیکت" in status
    events, _ = turn(s, chip=Chip(label="x", kind="direct_call", tool="set_conditions", args={"payment": "credit"}))
    assert s.conditions.payment == "credit" and "shop_cards" in [e for e, _ in events]
    events, _ = turn(s, chip=Chip(label="x", kind="direct_call", tool="set_conditions", args={"payment": "free"}))
    assert [e for e, _ in events] == ["error", "done"]
    events, _ = turn(s, chip=Chip(label="x", kind="direct_call", tool="call_shop",
                                  args={"shop_id": "shop_not_shown", "product_id": pid}))
    assert [e for e, _ in events] == ["error", "done"]
    events, _ = turn(s, chip=Chip(label="x", kind="direct_call", tool="set_conditions", args={"radius_km": None}))
    assert s.conditions.radius_km is None


def test_refine_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from torob import app as app_mod
    from torob.appdb import AppDB
    adb = AppDB(tmp_path / "app.db")
    monkeypatch.setattr(app_mod, "appdb", lambda: adb)
    c = TestClient(app_mod.app)
    c.post("/api/chat", json={"text": "شارژر اصل آیفون"})
    r = c.post("/api/refine", json={"conditions": {"budget_max": 2_000_000}})
    assert r.status_code == 200 and "event: product_cards" in r.text
