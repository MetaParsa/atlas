"""One test per finding of the S3 review (real dataset, pinned clock, no LLM)."""

from datetime import datetime

import pytest

from core.agent_loop import Emitter
from core.llm import FakeLLM, LLMClient
from torob import clock, store
from torob.agents import shops as S
from torob.logic.hours import parse_weekly_hours, status_at, TEHRAN
from torob.models import Chip, Conditions, SessionState
from torob.router import apply_patch, rule_route, run_turn

pytestmark = pytest.mark.skipif(not store.DB_PATH.exists(), reason="run torob.etl.build_db first")
THU = datetime(2026, 10, 22, 18, 5)


@pytest.fixture(autouse=True)
def pinned():
    clock.set_now(THU)
    yield
    clock.set_now(None)


@pytest.fixture(scope="module")
def charger():
    return store.get().execute("SELECT product_id FROM products WHERE title = 'شارژر اپل توان 20 وات سه پین' "
                               "ORDER BY sellers_total DESC").fetchone()[0]


def test_one_second_friday_shift_is_not_open():
    raw = '{"fri": {"status": "declared", "shifts": [{"start_time": "00:00:00", "end_time": "00:00:01"}]}}'
    week = parse_weekly_hours(raw)
    assert week[6].status == "unknown"
    assert status_at(week, datetime(2026, 10, 23, 11, 0, tzinfo=TEHRAN))[0] == "unknown"


def test_db_has_no_placeholder_shifts():
    n = store.get().execute("""SELECT count(*) FROM shops WHERE hours LIKE '%["00:00", "00:00"]%'""").fetchone()[0]
    assert n == 0


def test_badges_hold_over_the_whole_compared_set(charger):
    res = S.find_shops(charger, Conditions())
    compared = res.items + res.more
    for o in res.items:
        if "cheapest" in o.badges:
            assert o.price_min < min(x.price_min for x in compared if x is not o and x.priced)
        if "nearest" in o.badges:
            assert o.minutes < min(x.minutes for x in compared if x is not o)


def test_budget_never_passes_unpriced_shops(charger):
    for budget in (500_000, 1_400_000, 2_000_000):
        res = S.find_shops(charger, Conditions(budget_max=budget))
        if res.relaxed and res.relaxed["field"] == "budget_max":
            continue
        assert all(o.priced and o.price_min <= budget * 1.10 for o in res.items + res.more)


def test_category_only_excludes_junk_price_sellers(charger):
    _, _, suspicious = S.load_offers(charger)
    S.CATEGORY_ONLY_MAX, old = 10_000, S.CATEGORY_ONLY_MAX
    try:
        res = S.find_shops(charger, Conditions(radius_km=None))
    finally:
        S.CATEGORY_ONLY_MAX = old
    assert suspicious and not suspicious & {s["shop_id"] for s in res.category_only}


def test_km_label():
    assert S.km_label(0.4) == "۴۰۰ متر" and S.km_label(2) == "۲ کیلومتر" and S.km_label(2.5) == "۲٫۵ کیلومتر"


def test_funnel_adds_up(charger):
    c = S.find_shops(charger, Conditions(payment="credit_in_person")).counts
    assert c["found"] == c["terms_mismatch"] + c["closed_at_arrival"] + c["passing"]


def turn(state, **kw):
    fake = FakeLLM(lambda role, msgs: rule_route(msgs[-1]["content"]).model_dump(mode="json"))
    em = Emitter("s")
    list(run_turn(LLMClient([], {"router": "x"}, enabled=False, fake=fake), state, em, **kw))
    return em.events


def test_arrive_at_uses_demo_clock_date():
    clock.set_now(datetime(2025, 3, 1, 18, 0))          # a date that is surely not today
    s = SessionState()
    apply_patch(s, {"arrive_at": "20:30"})
    assert s.conditions.arrive_at.date() == datetime(2025, 3, 1).date()


def test_arrive_at_can_be_cleared():
    s = SessionState()
    apply_patch(s, {"arrive_at": "20:30"})
    turn(s, chip=Chip(label="الان", kind="direct_call", tool="set_conditions", args={"arrive_at": None}))
    assert s.conditions.arrive_at is None


def test_call_guard_rejects_category_only_shops():
    s = SessionState()
    events = turn(s, text="شارژر اصل آیفون")
    pid = dict(events)["product_cards"]["items"][0]["product_id"]
    events = turn(s, chip=Chip(label="x", kind="direct_call", tool="show_shops", args={"product_id": pid}))
    block = dict(events)["shop_cards"]
    if block["category_only"]:
        cat_shop = block["category_only"][0]["shop_id"]
        events = turn(s, chip=Chip(label="x", kind="direct_call", tool="call_shop",
                                   args={"shop_id": cat_shop, "product_id": pid}))
        assert [e for e, _ in events] == ["error", "done"]
    ok_shop = block["items"][0]["shop_id"]
    events = turn(s, chip=Chip(label="x", kind="direct_call", tool="call_shop", args={"shop_id": ok_shop, "product_id": pid}))
    assert "call_start" in [e for e, _ in events]


def test_aladdin_cards_agree_on_same_passage():
    from torob.logic.geo import haversine_m
    rows = store.get().execute("SELECT building_gid, lat, lon, area FROM shops WHERE building_key = 'علدین'").fetchall()
    main = max({r["building_gid"] for r in rows}, key=lambda g: sum(r["building_gid"] == g for r in rows))
    pts = [r for r in rows if r["building_gid"] == main]
    clat, clon = sum(r["lat"] for r in pts) / len(pts), sum(r["lon"] for r in pts) / len(pts)
    near = [r for r in rows if haversine_m(clat, clon, r["lat"], r["lon"]) <= 300]
    assert {r["building_gid"] for r in near} == {main}        # one Aladdin near its centre…
    assert len({r["area"] for r in pts}) >= 2                 # …even though it spans two neighbourhood labels
