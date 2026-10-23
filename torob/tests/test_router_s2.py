"""S2 wiring: advisor through run_turn, chips without the LLM."""

import pytest

from core.agent_loop import Emitter
from core.llm import FakeLLM, LLMClient
from torob import store
from torob.models import Chip, SessionState
from torob.router import rule_route, run_turn

pytestmark = pytest.mark.skipif(not store.DB_PATH.exists(), reason="run torob.etl.build_db first")


def fake_llm():
    fake = FakeLLM(lambda role, msgs: rule_route(msgs[-1]["content"]).model_dump(mode="json"))
    return LLMClient([], {"router": "x"}, enabled=False, fake=fake), fake


def turn(state, **kw):
    llm, fake = fake_llm()
    em = Emitter("s")
    list(run_turn(llm, state, em, **kw))
    return em.events, fake


def test_need_gives_cards_with_buttons_and_counted_status():
    s = SessionState()
    events, _ = turn(s, text="اسپیکر بلوتوثی")
    kinds = [e for e, _ in events]
    assert kinds[-1] == "done" and "product_cards" in kinds and "question" in kinds
    cards = dict(events)["product_cards"]["items"]
    assert all(c["button"]["tool"] == "show_shops" and c["button"]["args"]["product_id"] == c["product_id"] for c in cards)
    status = [d["text"] for e, d in events if e == "status" and d.get("done")][0]
    assert "مدل پیدا شد" in status and "نزدیکت" in status
    assert s.product_ids == [c["product_id"] for c in cards]


def test_budget_chip_reruns_without_llm():
    s = SessionState()
    turn(s, text="اسپیکر بلوتوثی")
    chip = Chip(label="تا ۶٫۵ میلیون", kind="direct_call", tool="set_budget", args={"budget_max": 6_500_000})
    events, fake = turn(s, chip=chip)
    assert fake.calls == [] and s.conditions.budget_max == 6_500_000
    cards = dict(events)["product_cards"]["items"]
    assert all(c["over_budget_pct"] is None or c["over_budget_pct"] <= 10 for c in cards)


def test_show_shops_only_for_shown_products():
    s = SessionState()
    events, _ = turn(s, text="اسپیکر بلوتوثی")
    pid = dict(events)["product_cards"]["items"][0]["product_id"]
    events, _ = turn(s, chip=Chip(label="x", kind="direct_call", tool="show_shops", args={"product_id": "product_not_shown"}))
    assert [e for e, _ in events] == ["error", "done"]
    events, _ = turn(s, chip=Chip(label="x", kind="direct_call", tool="show_shops", args={"product_id": pid}))
    assert "shop_cards" in [e for e, _ in events] and s.selected_product_id == pid


def test_named_model_skips_to_shops():
    from datetime import datetime
    from torob import clock
    clock.set_now(datetime(2026, 10, 22, 12, 0))   # midday: after ~21:00 most shops are closed and a question is right
    try:
        s = SessionState()
        events, _ = turn(s, text="گوشی سامسونگ S25 FE")
    finally:
        clock.set_now(None)
    kinds = [e for e, _ in events]
    assert "question" not in kinds and "shop_cards" in kinds
    assert len(dict(events)["product_cards"]["items"]) == 1 and s.selected_product_id


@pytest.mark.skipif(not __import__("torob").store.DB_PATH.exists(), reason="run torob.etl.build_db first")
@pytest.mark.parametrize("text,hints,bad", [
    ("هندزفری بلوتوثی", ["هندزفری", "لوازم جانبی موبایل"], "کاور"),       # the live case: a silicone case for earbuds
    ("گوشی سامسونگ", ["گوشی موبایل", "لوازم جانبی موبایل"], "باتری"),     # hint matched «باتری گوشی موبایل»
    ("ساعت هوشمند", ["ساعت هوشمند"], "شارژر"),                             # hint matched «شارژر ساعت هوشمند»
])
def test_model_category_hints_never_pull_in_parts_or_accessories(text, hints, bad):
    from torob.agents import advisor
    from torob.models import Conditions, Need
    res = advisor.pick_products(Need(product_type=text, keywords=[text], category_hints=hints), Conditions(radius_km=None))
    assert res.picks and not any(bad in p.c.category or p.c.title.startswith(bad) for p in res.picks)


@pytest.mark.skipif(not __import__("torob").store.DB_PATH.exists(), reason="run torob.etl.build_db first")
def test_asking_for_an_accessory_still_finds_accessories():
    from torob.agents import advisor
    from torob.models import Conditions, Need
    res = advisor.pick_products(Need(product_type="کاور ایرپاد", keywords=["کاور ایرپاد"]), Conditions(radius_km=None))
    assert res.picks and all("کاور" in p.c.category for p in res.picks)


def test_relevance_judge_keeps_what_the_model_names_and_falls_back_cleanly():
    from types import SimpleNamespace as NS
    from torob.agents import relevance
    cands = [NS(product_id="p1", title="هندزفری بلوتوثی انکر", category="هدفون", price_median=1_400_000),
             NS(product_id="p2", title="کاور سیلیکونی هایلو", category="کیف و کاور", price_median=300_000),
             NS(product_id="p3", title="هندزفری کیو سی وای", category="هدفون", price_median=900_000)]
    seen = []
    def script(role, msgs):
        seen.append((role, msgs[0]["content"]))
        return {"items": [{"i": 0, "show": True}, {"i": 1, "show": False, "copy": True}, {"i": 2, "show": True, "copy": True},
                          {"i": 99, "show": True}]}   # a copy that is not shown, and an unknown index, are ignored
    judged = relevance.keep(LLMClient([], {}, fake=FakeLLM(script)), "هندزفری برای باشگاه", cands, "s")
    assert judged.kept == [cands[0], cands[2]] and judged.copies == {"p3"} and seen[0][0] == "judge"
    assert "1 | کاور سیلیکونی هایلو | کیف و کاور | 300000" in seen[0][1]
    assert relevance.keep(LLMClient([], {}, fake=FakeLLM(lambda r, m: {"items": [{"i": 0, "show": False}]})), "x", cands, "s") is None   # nothing → no judgement
    assert relevance.keep(LLMClient([], {}, fake=FakeLLM(lambda r, m: "not json")), "x", cands, "s") is None      # bad answer → rules


def test_a_full_no_from_the_judge_means_nothing_fits():
    from types import SimpleNamespace as NS
    from torob.agents import relevance
    cands = [NS(product_id=f"p{i}", title=f"کفش کالج زنانه {i}", category="کفش کالج زنانه", price_median=1) for i in range(5)]
    all_no = {"items": [{"i": i, "show": False} for i in range(5)]}
    j = relevance.keep(LLMClient([], {}, fake=FakeLLM(lambda r, m: all_no)), "کفش دویدن", cands, "s")
    assert j is not None and j.kept == []                    # a verdict on every item: a real «none of these»


@pytest.mark.skipif(not __import__("torob").store.DB_PATH.exists(), reason="run torob.etl.build_db first")
def test_advisor_says_nothing_fits_instead_of_showing_rejects():
    from torob.agents import advisor, relevance
    from torob.models import Conditions, Need
    res = advisor.pick_products(Need(product_type="کفش دویدن", keywords=["کفش دویدن"]), Conditions(radius_km=None),
                                judge=lambda cands: relevance.Judgement([]))
    assert res.picks == [] and "دقیقاً همین" in res.notes[0] and res.n_matching == 0
    fallback = advisor.pick_products(Need(product_type="هندزفری بلوتوثی", keywords=["هندزفری بلوتوثی"]),
                                     Conditions(radius_km=None), judge=lambda cands: None)
    assert fallback.picks                                     # judge unavailable: the rule-picked categories still answer


def test_nothing_found_is_said_once():
    s = SessionState()
    events, _ = turn(s, text="ززقپچخ طلقثی")
    kinds = [e for e, _ in events]
    assert "product_cards" not in kinds and kinds.count("message") == 1


def test_budget_buttons_follow_the_matching_products_not_luxury_outliers():
    from torob.agents.advisor import budget_tiers
    prices = [900_000, 1_200_000, 1_500_000, 2_000_000, 2_500_000, 3_000_000, 4_000_000, 60_000_000, 200_000_000]
    assert budget_tiers(prices, [1_500_000, 3_000_000, 5_000_000]) == [1_500_000, 3_000_000, 5_000_000]   # the model, inside real prices
    assert max(budget_tiers(prices)) <= 4_000_000                                                    # no model: lower spread
    assert budget_tiers(prices, [500_000_000]) == budget_tiers(prices)                               # out of range: ignored


@pytest.mark.skipif(not __import__("torob").store.DB_PATH.exists(), reason="run torob.etl.build_db first")
def test_likely_copies_step_aside_when_a_real_one_is_found():
    from torob.agents import advisor, relevance
    from torob.models import Conditions, Need
    need = Need(product_type="هندزفری بلوتوثی", keywords=["هندزفری بلوتوثی"])
    def judge(cands):
        return relevance.Judgement(cands[:6], copies={c.product_id for c in cands[1:6]})   # one real, five copies
    res = advisor.pick_products(need, Conditions(radius_km=None), judge=judge)
    assert res.picks and all(not p.copy_suspect for p in res.picks)
    assert any("کپی" in n for n in res.notes)
