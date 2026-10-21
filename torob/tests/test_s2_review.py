"""One test per finding of the S2 review (real dataset, rule-based understanding, no LLM)."""

import statistics

import pytest

from core.agent_loop import Emitter
from core.llm import FakeLLM, LLMClient
from core.text import format_million
from torob import store
from torob.agents.advisor import pick_products
from torob.models import Chip, Conditions, SessionState
from torob.router import rule_route, run_turn
from torob.search import model_tokens

pytestmark = pytest.mark.skipif(not store.DB_PATH.exists(), reason="run torob.etl.build_db first")


def run(text):
    r = rule_route(text)
    return pick_products(r.need, Conditions(budget_max=r.conditions_patch.get("budget_max")))


def turn(state, **kw):
    fake = FakeLLM(lambda role, msgs: rule_route(msgs[-1]["content"]).model_dump(mode="json"))
    em = Emitter("s")
    list(run_turn(LLMClient([], {"router": "x"}, enabled=False, fake=fake), state, em, **kw))
    return em.events


@pytest.mark.parametrize("text", ["مودم 4G", "منبع تغذیه 12V", "کلید 25A", "پاوربانک 20000mah", "هارد اکسترنال 1TB"])
def test_unit_tokens_are_not_model_names(text):
    assert model_tokens(text) == []
    res = run(text)
    assert res.named is None and len(res.picks) >= 2


@pytest.mark.parametrize("text,must,must_not", [
    ("گوشی سامسونگ S25 FE", "S25 FE", "Ultra"),
    ("گوشی سامسونگ S25 اولترا", "S25 Ultra", "FE"),
    ("هدفون انکر R50i", "R50i", "NC"),
])
def test_named_model_respects_variants(text, must, must_not):
    res = run(text)
    assert res.named and must in res.named.c.title and must_not not in res.named.c.title


def test_named_needs_brand_in_title_and_no_unasked_variant():
    res = run("گوشی سامسونگ S25")         # no plain Samsung S25 in the data; a «یونیوا S25» is mis-tagged Samsung
    assert res.named is None


@pytest.mark.parametrize("text", ["گوشی تا ۱۰۰ هزار", "هندزفری تا ۵۰ هزار"])
def test_budget_below_every_price_is_honest(text):
    res = run(text)
    assert res.in_area and res.picks and res.notes and "پیدا نشد" in res.notes[0]
    assert all(p.over_budget_pct is None or p.over_budget_pct <= 10 for p in res.picks)
    assert res.question and res.question["chips"][0]["tool"] == "set_budget"


@pytest.mark.parametrize("text", ["ژنراتور", "گوشی سامسونگ S25", "اسپیکر بلوتوثی", "شارژر اصل آیفون"])
def test_best_value_label_is_true(text):
    res = run(text)
    first = res.picks[0]
    prices = [p.price for p in res.picks]
    sellers = [p.area.sellers for p in res.picks]
    if first.role == "best_value":
        assert first.price <= statistics.median(prices) and first.area.sellers >= statistics.median(sellers)
    else:
        assert first.role == "closest_match"


def test_relaxation_note_matches_shown_cards():
    for text in ["اسپیکر بلوتوثی تا ۳ میلیون", "کتونی", "اسپیکر بلوتوثی"]:
        res = run(text)
        weakest = min(p.area.sellers for p in res.picks)
        has_note = any("فقط" in n for n in res.notes)
        assert has_note == (weakest < 3)


def test_rule_route_keeps_spec_numbers():
    assert "20" in rule_route("شارژر اپل ۲۰ وات").need.keywords[0]
    assert "20000" in rule_route("پاوربانک ۲۰۰۰۰").need.keywords[0]
    assert rule_route("شارژر تا ۲ میلیون").need.keywords[0] == "شارژر"


def test_budget_chip_clear_really_clears():
    s = SessionState()
    turn(s, text="اسپیکر بلوتوثی تا ۳ میلیون")
    assert s.conditions.budget_max == 3_000_000
    events = turn(s, chip=Chip(label="فرقی نداره", kind="direct_call", tool="set_budget", args={"budget_max": None}))
    assert s.conditions.budget_max is None and s.need.budget_max is None
    prices = [c["price_max"] for c in dict(events)["product_cards"]["items"]]
    assert max(prices) > 3_300_000            # no longer filtered by the old 3 M


def test_brand_chip_switches_brand_without_stacking():
    s = SessionState()
    turn(s, text="پاوربانک")
    base = list(s.need.keywords)
    turn(s, chip=Chip(label="شیائومی", kind="direct_call", tool="prefer_brand", args={"brand": "شیائومی"}))
    assert s.need.keywords == base and s.need.brand_pref == ["شیائومی"]
    events = turn(s, chip=Chip(label="انکر", kind="direct_call", tool="prefer_brand", args={"brand": "انکر"}))
    cards = dict(events)["product_cards"]
    assert s.need.keywords == base
    assert all(c["brand"] == "انکر" for c in cards["items"]) or cards["notes"]


def test_text_refine_reruns_cards():
    s = SessionState()
    turn(s, text="اسپیکر بلوتوثی")
    events = turn(s, text="تا ۵ میلیون")
    assert "product_cards" in [e for e, _ in events] and s.conditions.budget_max == 5_000_000


def test_budget_chip_rejects_silly_values():
    s = SessionState()
    events = turn(s, chip=Chip(label="x", kind="direct_call", tool="set_budget", args={"budget_max": 5}))
    assert [e for e, _ in events] == ["error", "done"]


def test_whole_city_never_says_nearby():
    r = rule_route("اسپیکر بلوتوثی")
    res = pick_products(r.need, Conditions(radius_km=None))
    assert all("نزدیکت" not in p.why for p in res.picks)


def test_billion_format():
    assert format_million(2_080_560_000) == "۲٫۰۸ میلیارد"
