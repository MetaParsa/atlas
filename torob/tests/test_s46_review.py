"""One test per finding of the S4/S6 review (real dataset, pinned clock, no LLM)."""

from datetime import datetime

import pytest

from core.agent_loop import Emitter
from core.llm import FakeLLM, LLMClient
from torob import clock, store
from torob.agents import call as C
from torob.agents import followup as F
from torob.models import Chip, Conditions, SessionState
from torob.router import _resolve_shop, rule_answer, rule_route, run_turn

pytestmark = pytest.mark.skipif(not store.DB_PATH.exists(), reason="run torob.etl.build_db first")


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    clock.set_now(datetime(2026, 10, 22, 18, 5))
    monkeypatch.setenv("SIM_SEED_DATE", "2026-10-22")
    yield
    clock.set_now(None)
    C.set_overrides(None)


def turn(state, **kw):
    fake = FakeLLM(lambda role, msgs: rule_route(msgs[-1]["content"]).model_dump(mode="json"))
    em = Emitter("s")
    list(run_turn(LLMClient([], {"router": "x"}, enabled=False, fake=fake), state, em, **kw))
    return em.events


def to_shops(s, need="شارژر اصل آیفون"):
    ev = turn(s, text=need)
    pid = dict(ev)["product_cards"]["items"][0]["product_id"]
    ev = turn(s, chip=Chip(label="x", kind="direct_call", tool="show_shops", args={"product_id": pid}))
    return dict(ev)["shop_cards"]["items"]


def test_new_need_then_typed_call_does_not_call_old_product():
    s = SessionState()
    to_shops(s)
    turn(s, text="یه هندزفری بلوتوث")
    assert s.selected_product_id is None and not s.shops_shown and not s.call_ok_ids and s.plan is None
    ev = turn(s, text="به اولی زنگ بزن")
    assert "call_start" not in [e for e, _ in ev]


def test_resolve_shop_never_guesses():
    s = SessionState(shops_shown=[{"shop_id": "a", "name": "موبایل آرمان"}, {"shop_id": "b", "name": "موبایل نیک"},
                                  {"shop_id": "c", "name": "تک شاپ"}])
    assert _resolve_shop(s, "به پنجمی زنگ بزن") is None                 # out of range
    assert _resolve_shop(s, "برای پاوربانک بپرس") is None               # no match
    assert _resolve_shop(s, "به موبایل نیک زنگ بزن")["shop_id"] == "b"   # full name wins over a shared word
    assert _resolve_shop(s, "به دومی زنگ بزن")["shop_id"] == "b"
    one = SessionState(shops_shown=[{"shop_id": "a", "name": "x"}])
    assert _resolve_shop(one, "زنگ بزن")["shop_id"] == "a"


def test_unclear_call_target_asks_with_chips():
    s = SessionState()
    shops = to_shops(s)
    ev = turn(s, text="برای پاوربانک بپرس")
    q = dict(ev)["question"]
    assert "call_start" not in [e for e, _ in ev] and len(q["chips"]) == len(shops)


def test_kodoomeshoon_is_a_question_not_a_call():
    assert rule_route("بپرس ببین کدومشون قسطی دارن").intent == "ask_results"
    assert rule_route("به دومی زنگ بزن").target == "دومی"


def test_tried_is_scoped_by_product():
    s = SessionState()
    shops = to_shops(s)
    C.set_overrides({shops[0]["shop_id"]: "no_answer"})
    turn(s, chip=Chip(label="x", kind="direct_call", tool="call_shop",
                      args={"shop_id": shops[0]["shop_id"], "product_id": s.selected_product_id}))
    charger = s.selected_product_id
    assert any(k.startswith(f"{charger}|") for k in s.tried_shop_ids)
    from torob.router import _excluded
    s.selected_product_id = "product_other"
    assert _excluded(s) == set()


def test_second_ok_call_offers_to_move_plan():
    s = SessionState()
    shops = to_shops(s)
    C.set_overrides({shops[0]["shop_id"]: "ok", shops[1]["shop_id"]: "ok"})
    turn(s, text="به اولی زنگ بزن")
    ev = turn(s, text="به دومی زنگ بزن")
    q = dict(ev)["question"]
    assert s.plan["shop_id"] == shops[0]["shop_id"] and q["chips"][0]["tool"] == "set_plan"
    ev = turn(s, chip=Chip(**q["chips"][0]))
    assert s.plan["shop_id"] == shops[1]["shop_id"] and "plan" in [e for e, _ in ev]


def test_recheck_recomputes_backup_and_flags_short_hold():
    s = SessionState()
    shops = to_shops(s)
    C.set_overrides({shops[0]["shop_id"]: "ok"})
    turn(s, text="به اولی زنگ بزن")
    turn(s, chip=Chip(label="x", kind="direct_call", tool="call_shop",
                      args={"shop_id": shops[0]["shop_id"], "product_id": s.selected_product_id, "hold": True}))
    ev = turn(s, text="ساعت 8:30 میام")
    plan = dict(ev)["plan"]
    if plan["backup"]:
        assert "تا برسی بسته‌ست" not in plan["backup"]["visit"]
    hold = next((c["hold_until"] for c in s.calls.values() if c.get("hold_until")), None)
    if hold:
        assert plan["simulated"]["hold_short"] == (hold < "20:30")


def test_midnight_closing_is_latest_not_earliest():
    shown = [{"name": "شب‌باز", "closes": "00:00", "closes_iso": "2026-10-23T00:00:00+03:30", "price_min": 1, "price_age": "",
              "minutes": 5, "credit": [], "where": ""},
             {"name": "عصر", "closes": "20:00", "closes_iso": "2026-10-22T20:00:00+03:30", "price_min": 1, "price_age": "",
              "minutes": 5, "credit": [], "where": ""}]
    a = rule_answer(shown, "کدومش زودتر می‌بنده؟")
    assert a.startswith("«عصر» زودتر") and not a.endswith("؟.")


def test_similar_product_respects_budget_and_skips_same_title():
    con = store.get()
    pid = con.execute("SELECT product_id FROM products WHERE title = 'شارژر اپل توان 20 وات سه پین' "
                      "ORDER BY sellers_total DESC").fetchone()[0]
    shop = {"name": "x", "building_gid": None}
    sim = F.similar_product(pid, Conditions(budget_max=2_000_000), shop)
    if sim:
        assert sim["price_min"] <= 2_200_000 and sim["title"] != "شارژر اپل توان 20 وات سه پین"
    sim = F.similar_product(pid, Conditions(payment="credit"), shop)
    if sim:
        assert store.get().execute("SELECT count(*) FROM offers o JOIN shops s USING (shop_id) "
                                   "WHERE o.product_id = ? AND s.has_credit = 1", (sim["product_id"],)).fetchone()[0] >= 1


def test_alternative_states_price_difference():
    s = SessionState()
    shops = to_shops(s)
    C.set_overrides({shops[0]["shop_id"]: "no_answer"})
    ev = turn(s, chip=Chip(label="x", kind="direct_call", tool="call_shop",
                           args={"shop_id": shops[0]["shop_id"], "product_id": s.selected_product_id}))
    alt = dict(ev)["alternative"]
    assert "قیمت" in alt["text"] and ("بیشتر" in alt["text"] or "کمتر" in alt["text"] or "همون قیمت" in alt["text"])


def test_ask_with_nothing_shown_becomes_search():
    ev = turn(SessionState(), text="ارزون‌ترین پاوربانک")
    assert "product_cards" in [e for e, _ in ev]


def test_call_chip_rejects_wrong_product_or_unshown_shop():
    s = SessionState()
    shops = to_shops(s)
    for args in ({"shop_id": shops[0]["shop_id"], "product_id": "product_other"},
                 {"shop_id": "shop_nope", "product_id": s.selected_product_id}):
        ev = turn(s, chip=Chip(label="x", kind="direct_call", tool="call_shop", args=args))
        assert [e for e, _ in ev] == ["error", "done"]
