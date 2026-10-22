"""S4 + S6: simulated call, plan card, alternatives, typed «بپرس» and questions (pinned clock, no LLM)."""

import re
from datetime import datetime

import pytest

from core.agent_loop import Emitter
from core.llm import FakeLLM, LLMClient
from core.text import to_latin_digits
from torob import clock, store
from torob.agents import call as C
from torob.agents import shops as S
from torob.models import Chip, Conditions, SessionState
from torob.router import _arrival, rule_answer, rule_route, run_turn

pytestmark = pytest.mark.skipif(not store.DB_PATH.exists(), reason="run torob.etl.build_db first")
THU = datetime(2026, 10, 22, 18, 5)


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    clock.set_now(THU)
    monkeypatch.setenv("SIM_SEED_DATE", "2026-10-22")
    yield
    clock.set_now(None)
    C.set_overrides(None)


@pytest.fixture(scope="module")
def charger():
    return store.get().execute("SELECT product_id FROM products WHERE title = 'شارژر اپل توان 20 وات سه پین' "
                               "ORDER BY sellers_total DESC").fetchone()[0]


def offers(pid, **cond):
    res = S.find_shops(pid, Conditions(**cond))
    return res, res.items + res.more


def test_truth_is_deterministic(charger):
    res, os_ = offers(charger)
    a = [C.make_truth(o, res.product, clock.now()) for o in os_]
    b = [C.make_truth(o, res.product, clock.now()) for o in os_]
    assert a == b


def test_truth_never_invents_hours_credit_or_location(charger):
    res, os_ = offers(charger, open="any", radius_km=None)
    for o in os_:
        t = C.make_truth(o, res.product, clock.now())
        if not o.shop["hours_known"]:
            assert t.closes_today is None
        assert set(t.credit) == {p for p, _ in o.payments}
        if not (o.shop["floor"] or o.shop["unit"]):
            assert t.location is None


def test_unknown_hours_script_says_no_time(charger):
    res, os_ = offers(charger, open="any", radius_km=None)
    o = next(o for o in os_ if not o.shop["hours_known"])
    C.set_overrides({o.shop["shop_id"]: "ok"})
    plan, r = C.run_call(o, res.product, "any", "c1")
    shop_text = " ".join(t for w, t in plan.lines if w == "shop")
    assert "hours" in plan.questions and r.closes_today is None
    assert not re.search(r"\d{1,2}:\d{2}", to_latin_digits(shop_text))     # no clock time invented


def test_overrides_force_outcomes(charger):
    res, os_ = offers(charger)
    a, b = os_[0], os_[1]
    C.set_overrides({a.shop["shop_id"]: "no_answer", b.shop["shop_id"]: "unavailable"})
    assert C.run_call(a, res.product, "any", "c1")[1].outcome == "no_answer"
    assert C.run_call(b, res.product, "any", "c2")[1].outcome == "unavailable"


def test_questions_come_from_gaps(charger):
    res, os_ = offers(charger)
    for o in os_[:5]:
        plan = C.plan_questions(o, res.product, "credit", clock.now(), want_hold=False)
        assert plan.questions[0] == "stock" and "credit" in plan.questions and plan.reasons
        assert ("location" in plan.questions) == bool(o.shop["building"])


def test_labels_always_say_simulated(charger):
    res, os_ = offers(charger)
    for outcome in ("ok", "no_answer", "unavailable"):
        C.set_overrides({os_[0].shop["shop_id"]: outcome})
        _, r = C.run_call(os_[0], res.product, "any", "c")
        assert "شبیه‌سازی" in C.result_label(r)
        if C.price_change_text(r):
            assert "شبیه‌سازی" in C.price_change_text(r)


def test_llm_rewording_cannot_change_numbers():
    assert C._same_numbers("امروز ۱٫۵۸ میلیون.", "امروز یک و پنجاه و هشت صدم… ۱٫۵۸ میلیون")
    assert not C._same_numbers("تا ۲۰:۰۰ هستیم.", "تا ۲۱:۰۰ هستیم.")


@pytest.mark.parametrize("text,expect", [
    ("ساعت 8:30 میام", "20:30"), ("ساعت ۸ و نیم شب می‌رسم", "20:30"), ("10 صبح میام", "10:00"),
    ("ساعت 19 میام", "19:00"), ("شارژر سامسونگ", None),
])
def test_arrival_parsing(text, expect):
    from core.text import normalize_fa
    assert _arrival(to_latin_digits(normalize_fa(text))) == expect


def test_rule_answers_quote_only_facts():
    shown = [{"name": "الف", "closes": "19:30", "price_min": 1_560_000, "price_age": "۲ هفته پیش", "minutes": 6,
              "credit": [], "where": "x"},
             {"name": "ب", "closes": None, "price_min": None, "price_age": "تاریخ ثبت نشده", "minutes": 4,
              "credit": ["اسنپ‌پی"], "where": "y"}]
    assert "۱۹:۳۰" in rule_answer(shown, "کدومش زودتر می‌بنده؟") and "ثبت نشده" in rule_answer(shown, "کدوم زودتر می‌بنده")
    assert "تأیید نشده" in rule_answer(shown, "کدوم ارزون‌تره؟")
    assert "«ب»" in rule_answer(shown, "کدوم نزدیک‌تره؟") and "اسنپ‌پی" in rule_answer(shown, "کدوم قسطی داره؟")


# --- through the router ------------------------------------------------------------------------------------------

def turn(state, **kw):
    fake = FakeLLM(lambda role, msgs: rule_route(msgs[-1]["content"]).model_dump(mode="json"))
    em = Emitter("s")
    list(run_turn(LLMClient([], {"router": "x"}, enabled=False, fake=fake), state, em, **kw))
    return em.events


def to_shops(s):
    events = turn(s, text="شارژر اصل آیفون")
    pid = dict(events)["product_cards"]["items"][0]["product_id"]
    events = turn(s, chip=Chip(label="x", kind="direct_call", tool="show_shops", args={"product_id": pid}))
    return dict(events)["shop_cards"]["items"]


def test_full_flow_call_plan_alternative_recheck():
    s = SessionState()
    shops = to_shops(s)
    C.set_overrides({shops[0]["shop_id"]: "ok", shops[1]["shop_id"]: "no_answer"})
    ev = turn(s, text="به اولی زنگ بزن بپرس")
    kinds = [e for e, _ in ev]
    assert kinds.index("call_start") < kinds.index("call_line") < kinds.index("call_result") < kinds.index("card_update")
    assert "plan" in kinds and s.plan["shop_id"] == shops[0]["shop_id"]
    assert dict(ev)["card_update"]["stock"] == "simulated_confirmed" and "شبیه‌سازی" in dict(ev)["call_start"]["title"]
    ev = turn(s, text="به دومی زنگ بزن")
    alt = dict(ev)["alternative"]
    assert alt["reason"].endswith("جواب نداد") and alt["shop"]["shop_id"] not in (shops[0]["shop_id"], shops[1]["shop_id"])
    assert alt["chip"]["tool"] == "call_shop" and "plan" not in [e for e, _ in ev]     # no auto-call, plan untouched
    ev = turn(s, text="ساعت 8:30 میام")
    plan = dict(ev).get("plan")
    assert s.conditions.arrive_at.strftime("%H:%M") == "20:30" and plan
    assert plan["status"] == "closed_at_arrival"                     # shop 1 closes at 20:00
    assert plan["switch_to"]["reason"].endswith("تا برسی بسته‌ست")
    assert plan["switch_to"]["chip"]["args"].get("hold") is True


def test_typed_call_without_shops_asks_first():
    ev = turn(SessionState(), text="زنگ بزن بپرس")
    assert [e for e, _ in ev] == ["message", "done"]


def test_alternative_chip_shop_is_callable():
    s = SessionState()
    shops = to_shops(s)
    C.set_overrides({shops[0]["shop_id"]: "unavailable"})
    ev = turn(s, chip=Chip(label="x", kind="direct_call", tool="call_shop",
                           args={"shop_id": shops[0]["shop_id"], "product_id": s.selected_product_id}))
    alt = dict(ev)["alternative"]
    ev = turn(s, chip=Chip(**alt["chip"]))
    assert "call_start" in [e for e, _ in ev]


def test_ask_results_answers_from_shown_shops():
    s = SessionState()
    to_shops(s)
    ev = turn(s, text="کدومش زودتر می‌بنده؟")
    assert [e for e, _ in ev][-2:] == ["message", "done"] and "می‌بنده" in dict(ev)["message"]["text"]


def test_a_call_back_asks_only_whats_new():
    import random
    from datetime import datetime
    from types import SimpleNamespace as NS
    from torob.agents.call import Truth, plan_questions, script
    o = NS(priced=True, changed_at="2026-09-01T10:00:00+03:30", open_state="unknown",
           shop={"hours_known": False, "building": "پاساژ علاءالدین"})
    now = datetime.fromisoformat("2026-10-22T20:40:00+03:30")
    first = plan_questions(o, {}, "any", now, want_hold=False)
    assert {"stock", "price", "hours", "location"} <= set(first.questions) and not first.callback
    again = plan_questions(o, {}, "any", now, want_hold=True, asked=frozenset(first.questions))
    assert again.callback and again.questions == ["hold"] and again.reasons == ["خواستی نگهش دارن"]
    truth = Truth(True, True, 3_020_000, True, None, [], "طبقه ۵، واحد ۵۰۶", "21:00")
    lines = script(again, truth, "هدفون انکر R50i", random.Random(1))
    assert lines[0][1].startswith("سلام دوباره") and "قیمت" not in " ".join(t for _, t in lines)
    assert "نگهش می‌دارید" in lines[-2][1] and "۲۱:۰۰" in lines[-1][1]
