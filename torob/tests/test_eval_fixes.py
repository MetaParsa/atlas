"""Regressions for behaviour found by the expectation-scored evaluation (torob/eval, 2026-10-23)."""

from core.llm import to_anthropic_messages
from torob.agents.advisor import AFFORDABLE_FIRST, _affordable_first
from torob.search import Candidate


def cand(i: int, price: int | None) -> Candidate:
    return Candidate(f"p{i}", f"t{i}", f"t{i}", None, "c", None, "", 0, 1, price, price, price, 0, i)


def test_router_prompt_is_a_cached_block_before_the_changing_part():
    system, msgs = to_anthropic_messages([{"role": "system", "content": "ثابت", "cache": True},
                                          {"role": "system", "content": "روی صفحه"}, {"role": "user", "content": "سلام"}])
    assert system[0] == {"type": "text", "text": "ثابت", "cache_control": {"type": "ephemeral"}}
    assert system[1] == {"type": "text", "text": "روی صفحه"} and msgs == [{"role": "user", "content": "سلام"}]
    plain, _ = to_anthropic_messages([{"role": "system", "content": "sys"}, {"role": "user", "content": "x"}])
    assert plain == "sys"   # no cache flag: unchanged plain string


def test_judge_sees_affordable_matches_even_when_they_rank_low():
    """«هندزفری تا ۲ میلیون»: the famous 10-million models rank first; the judge reads 40, so the cheap ones must be in."""
    reps = [cand(i, 10_000_000) for i in range(60)] + [cand(100 + i, 1_500_000) for i in range(10)]
    out = _affordable_first(reps, 2_000_000, cheap=False)
    assert [c.product_id for c in out[:10]] == [f"p{100 + i}" for i in range(10)]
    assert sorted(c.product_id for c in out) == sorted(c.product_id for c in reps)   # nothing lost or doubled


def test_affordable_first_still_leaves_room_for_the_real_product():
    """If every cheap match is an accessory, the judge must still see the real (pricier) products that ranked first."""
    reps = [cand(i, 9_000_000) for i in range(10)] + [cand(100 + i, 500_000) for i in range(50)]
    top40 = {c.product_id for c in _affordable_first(reps, 1_000_000, cheap=False)[:40]}
    assert {f"p{i}" for i in range(10)} <= top40
    assert len([p for p in top40 if p.startswith("p1")]) >= AFFORDABLE_FIRST


def test_no_budget_no_reorder():
    reps = [cand(i, 1_000 * (10 - i)) for i in range(10)]
    assert _affordable_first(reps, None, cheap=False) == reps


def test_nothing_under_budget_question_is_marked_as_a_fact():
    """It must outrank the model's softer «برای چی می‌خوای؟», or the shopper sees over-budget cards with no offer."""
    import pytest
    from torob import store
    if not store.DB_PATH.exists():
        pytest.skip("no dataset")
    from torob.agents.advisor import pick_products
    from torob.models import Conditions
    from torob.router import rule_route
    need = rule_route("هندزفری بلوتوث").need
    res = pick_products(need, Conditions(budget_max=1_000))
    assert res.question and res.question.get("fact") is True and "بودجه" in res.question["text"]


def test_a_name_found_only_in_addresses_says_where_it_was_placed():
    """«تو پردیسم»: Pardis the city has no shops in the data; «پردیس» in shop addresses must not pass for it silently."""
    import pytest
    from torob import store
    if not store.DB_PATH.exists():
        pytest.skip("no dataset")
    from torob.agents.places import locate
    from torob.models import SessionState
    from torob.router import apply_patch
    assert locate("فردوسی")[2] is None              # a real neighbourhood
    spot = locate("پردیس")
    if spot is None:
        pytest.skip("no address mentions it")
    st = SessionState()
    changed = apply_patch(st, {"area": "پردیس"})
    assert "center" in changed and any(c.startswith("center_by_address:") for c in changed)


def test_all_closed_late_at_night_offers_the_next_morning():
    """22:45 Thursday: every seller is closed, the finder relaxes «open»; the shopper must still be offered a morning."""
    from datetime import datetime

    import pytest
    from torob import clock, store
    if not store.DB_PATH.exists():
        pytest.skip("no dataset")
    from core.agent_loop import Emitter
    from core.llm import FakeLLM, LLMClient
    from torob.models import Chip, SessionState
    from torob.router import rule_route, run_turn
    llm = LLMClient([], {"router": "x"}, enabled=False,
                    fake=FakeLLM(lambda role, m: rule_route(m[-1]["content"]).model_dump(mode="json")))
    clock.set_now(datetime(2026, 10, 22, 22, 45))
    try:
        st, em = SessionState(), Emitter("s")
        list(run_turn(llm, st, em, text="هندزفری بلوتوث"))
        card = next(d for e, d in em.events if e == "product_cards")["items"][0]
        em = Emitter("s")
        list(run_turn(llm, st, em, chip=Chip(**card["button"])))
        shops = next((d for e, d in em.events if e == "shop_cards"), None)
        items = shops["items"] if shops else []
        if items and all(s["visit"]["open_at_arrival"] == "no" for s in items):
            assert any(e == "question" and "۱۰ صبح" in d["text"] for e, d in em.events)
    finally:
        clock.set_now(None)


def test_budget_browse_finds_products_whose_titles_dont_share_the_words():
    """«هندزفری بلوتوث تا ۲ میلیون»: «هدفون بی سیم گرین لاین مدل Cairo» (≈۲ میلیون) never matched the text."""
    import pytest
    from torob import search, store
    if not store.DB_PATH.exists():
        pytest.skip("no dataset")
    from torob.router import rule_route
    need = rule_route("هندزفری بلوتوث").need
    plain, _ = search.candidates(need, wide=True)
    browsed, _ = search.candidates(need, wide=True, budget=2_000_000)
    added = {c.product_id for c in browsed} - {c.product_id for c in plain}
    assert added and all(c.price_median <= 2_200_000 for c in browsed if c.product_id in added)
    assert [c.product_id for c in browsed][:len(plain)] == [c.product_id for c in plain]   # text matches still lead


def _rule_llm():
    from core.llm import FakeLLM, LLMClient
    from torob.router import rule_route
    return LLMClient([], {"router": "x"}, enabled=False,
                     fake=FakeLLM(lambda role, m: rule_route(m[-1]["content"]).model_dump(mode="json")))


def test_product_cards_carry_the_nearest_seller_minutes():
    """«کدومش بهم نزدیک تره؟» was answered «distance isn't recorded»: the cards must know it."""
    import pytest
    from torob import store
    if not store.DB_PATH.exists():
        pytest.skip("no dataset")
    from core.agent_loop import Emitter
    from torob.models import SessionState
    from torob.router import run_turn
    st, em = SessionState(), Emitter("s")
    list(run_turn(_rule_llm(), st, em, text="پاوربانک"))
    items = next(d for e, d in em.events if e == "product_cards")["items"]
    assert all(isinstance(p["nearest_minutes"], int) for p in items if p["sellers_in_area"])
    assert all("nearest_minutes" in p for p in st.products_shown)


def test_which_model_is_not_asked_twice_in_a_row():
    import pytest
    from torob import store
    if not store.DB_PATH.exists():
        pytest.skip("no dataset")
    from core.agent_loop import Emitter
    from torob.models import SessionState
    from torob.router import _open_shops, run_turn
    st, em = SessionState(), Emitter("s")
    list(run_turn(_rule_llm(), st, em, text="پاوربانک"))
    em = Emitter("s")
    st.remember("user", "میخوام حضوری بخرم")
    list(_open_shops(st, em, ""))
    assert [e for e, _ in em.events] == ["question"] and "دقیقه" in em.events[0][1]["chips"][0]["label"]
    em = Emitter("s")
    st.remember("user", "هر کدوم")
    list(_open_shops(st, em, ""))
    assert any(e == "shop_cards" for e, _ in em.events) and not any(e == "question" and "کدوم مدل" in d["text"] for e, d in em.events)


def test_both_model_steps_are_told_where_the_shopper_and_the_shops_are():
    """The router and the answer step must know the shopper's place and every card's distance, or they say «ثبت نشده»."""
    import json

    import pytest
    from torob import store
    if not store.DB_PATH.exists():
        pytest.skip("no dataset")
    from core.agent_loop import Emitter
    from torob.models import SessionState
    from torob.router import _board, _on_screen, _place_line, run_turn
    st, em = SessionState(), Emitter("s")
    list(run_turn(_rule_llm(), st, em, text="پاوربانک"))
    assert "پیش‌فرض" in _place_line(st) and "دقیقه" in _on_screen(st)
    facts = json.loads(_board(st).facts())
    assert facts["shopper"]["shopper_location_known"] is False
    assert all("nearest_seller_minutes" in p for p in facts["products"])


def test_known_tehran_places_are_placed_even_with_few_shops_there():
    """«تو ونکم»: one shop in the data mentions Vanak; the shopper is still in Vanak, not at the city-centre default."""
    from torob.agents.places import TEHRAN_PLACES, locate
    lat, lng, where = locate("میدان ونک")
    assert (lat, lng) == TEHRAN_PLACES["ونک"] and where is None


def _to_call(clock_at):
    """A search → sellers → the first shop shown, at a demo time when shops are open."""
    from core.agent_loop import Emitter
    from torob import clock
    from torob.models import Chip, SessionState
    from torob.router import run_turn
    clock.set_now(clock_at)
    st, em = SessionState(), Emitter("s")
    list(run_turn(_rule_llm(), st, em, text="پاوربانک"))
    card = next(d for e, d in em.events if e == "product_cards")["items"][0]
    em = Emitter("s")
    list(run_turn(_rule_llm(), st, em, chip=Chip(**card["button"])))
    shops = next(d for e, d in em.events if e == "shop_cards")["items"]
    return st, shops[0]


def test_unanswered_call_offers_a_simulated_text_that_agrees_with_the_call():
    from datetime import datetime

    import pytest
    from torob import clock, store
    if not store.DB_PATH.exists():
        pytest.skip("no dataset")
    from core.agent_loop import Emitter
    from torob.agents import call as call_engine
    from torob.models import Chip
    from torob.router import run_turn
    try:
        st, shop = _to_call(datetime(2026, 10, 22, 12, 0))
        call_engine.set_overrides({shop["shop_id"]: "no_answer"})
        em = Emitter("s")
        list(run_turn(_rule_llm(), st, em, chip=Chip(**shop["button"])))
        offer = next(d for e, d in em.events if e == "question" and "پیامک" in d["text"])
        sms = next(c for c in offer["chips"] if c["tool"] == "sms_shop")
        call_engine.set_overrides(None)
        em = Emitter("s")
        list(run_turn(_rule_llm(), st, em, chip=Chip(**sms)))
        start = next(d for e, d in em.events if e == "call_start")
        assert start["channel"] == "sms" and start["simulated"] is True
        label = next(d for e, d in em.events if e == "call_result")["label"]
        assert "شبیه‌سازی" in label and "پیامک" in label
        o, res = __import__("torob.router", fromlist=["_find_offer"])._find_offer(st, shop["shop_id"])
        truth = call_engine.make_truth(o, res.product, clock.now())
        result = next(d for e, d in em.events if e == "call_result")["result"]
        if result["outcome"] != "no_answer":
            assert result["in_stock"] == truth.in_stock   # the text and the call never disagree
    finally:
        call_engine.set_overrides(None)
        clock.set_now(None)


def test_a_text_to_a_shop_not_on_screen_is_refused():
    import pytest
    from torob import store
    if not store.DB_PATH.exists():
        pytest.skip("no dataset")
    from core.agent_loop import Emitter
    from torob.models import Chip, SessionState
    from torob.router import run_turn
    em = Emitter("s")
    list(run_turn(_rule_llm(), SessionState(), em, chip=Chip(label="x", kind="direct_call", tool="sms_shop",
                                                              args={"shop_id": "shop_fake", "product_id": "product_fake"})))
    assert any(e == "error" for e, _ in em.events) and not any(e == "call_start" for e, _ in em.events)
