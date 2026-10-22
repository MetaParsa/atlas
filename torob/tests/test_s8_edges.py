"""S8 edge cases (late night, Friday, outside coverage, budgets, prices) and the /admin page. No LLM."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from core.agent_loop import Emitter
from core.llm import FakeLLM, LLMClient
from torob import clock, store
from torob.models import Chip, SessionState
from torob.router import _next_shopping_morning, _parse_arrive_at, apply_patch, rule_route, run_turn
from torob.logic.hours import TEHRAN

pytestmark = pytest.mark.skipif(not store.DB_PATH.exists(), reason="run torob.etl.build_db first")


@pytest.fixture(autouse=True)
def reset_clock():
    yield
    clock.set_now(None)


def turn(state, **kw):
    fake = FakeLLM(lambda role, msgs: rule_route(msgs[-1]["content"]).model_dump(mode="json"))
    em = Emitter("s")
    list(run_turn(LLMClient([], {"router": "x"}, enabled=False, fake=fake), state, em, **kw))
    return em.events


def to_shops(s, need="شارژر اصل آیفون"):
    ev = turn(s, text=need)
    items = dict(ev)["product_cards"]["items"]
    return turn(s, chip=Chip(label="x", kind="direct_call", tool="show_shops", args={"product_id": items[0]["product_id"]}))


def test_next_shopping_morning_skips_friday():
    assert _next_shopping_morning(datetime(2026, 10, 22, 22, 30, tzinfo=TEHRAN)).date() == datetime(2026, 10, 24).date()  # Thu → Sat
    assert _next_shopping_morning(datetime(2026, 10, 23, 11, 0, tzinfo=TEHRAN)).date() == datetime(2026, 10, 24).date()   # Fri → Sat
    assert _next_shopping_morning(datetime(2026, 10, 24, 22, 0, tzinfo=TEHRAN)).hour == 10                                 # Sat → Sun 10:00


def test_late_night_offers_next_morning_and_chip_works():
    clock.set_now(datetime(2026, 10, 22, 22, 30))
    s = SessionState()
    ev = to_shops(s)
    q = [d for e, d in ev if e == "question"][-1]
    assert "شنبه ۱۰ صبح" in q["text"]
    ev = turn(s, chip=Chip(**q["chips"][0]))
    cond = dict(ev)["conditions"]
    assert cond["arrive_label"] == "رسیدن شنبه ۱۰:۰۰" and s.conditions.arrive_at.date() == datetime(2026, 10, 24).date()
    shops = dict(ev)["shop_cards"]["items"]
    assert shops and all("بسته" not in c["visit"]["text"] for c in shops)


def test_friday_says_so():
    clock.set_now(datetime(2026, 10, 23, 11, 0))
    ev = to_shops(SessionState(), "هندزفری بلوتوثی")
    q = [d for e, d in ev if e == "question"][-1]
    assert q["text"].startswith("جمعه‌ست") and "فردا" in q["text"]


def test_outside_coverage_offers_dense_area_or_whole_city():
    clock.set_now(datetime(2026, 10, 22, 18, 5))
    s = SessionState()
    turn(s, chip=Chip(label="x", kind="direct_call", tool="set_location",
                      args={"lat": 35.7196, "lng": 51.5055, "label": "تهران پارس"}))
    ev = turn(s, text="شارژر اصل آیفون")
    q = dict(ev)["question"]
    assert "جمهوری" in q["text"] and {c["tool"] for c in q["chips"]} == {"set_location", "set_conditions"}
    ev = turn(s, chip=Chip(**q["chips"][0]))
    assert s.conditions.center.label == "جمهوری / فردوسی"


def test_arrive_at_iso_is_bounded():
    clock.set_now(datetime(2026, 10, 22, 18, 5))
    assert _parse_arrive_at("2026-10-24T10:00:00+03:30").date() == datetime(2026, 10, 24).date()
    assert _parse_arrive_at("2026-11-30T10:00:00+03:30") is None          # more than a week ahead
    assert _parse_arrive_at("2026-10-20T10:00:00+03:30") is None          # in the past
    s = SessionState()
    assert apply_patch(s, {"arrive_at": "not a time"}) == []


def test_missing_price_shops_never_rank_first_and_say_so():
    clock.set_now(datetime(2026, 10, 22, 18, 5))
    ev = to_shops(SessionState())
    block = dict(ev)["shop_cards"]
    texts = [c["price"]["text"] for c in block["items"]]
    assert texts and texts[0] != "قیمت ثبت نشده"
    assert block["junk_line"] and "مشکوک" in block["junk_line"]


def test_admin_page_shows_numbers_and_no_secrets(tmp_path, monkeypatch):
    from torob import app as app_mod
    from torob.appdb import AppDB
    adb = AppDB(tmp_path / "app.db")
    monkeypatch.setattr(app_mod, "appdb", lambda: adb)
    monkeypatch.setenv("CALL_LINE_DELAY", "0")
    c = TestClient(app_mod.app)
    c.post("/api/chat", json={"text": "شارژر اصل آیفون"})
    r = c.get("/admin")
    assert r.status_code == 200 and "پشت صحنه" in r.text and "p50" in r.text and "شبیه‌سازی" in r.text
    assert "api_key" not in r.text.lower() and "Bearer" not in r.text
    st = adb.stats()
    assert st["turns"] >= 1 and st["p50_ms"] is not None
