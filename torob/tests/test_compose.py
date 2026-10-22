"""Generative UI (torob/agents/compose.py): the model picks blocks, the code fills every value from the cards."""

from types import SimpleNamespace as NS

import pytest

from core.agent_loop import Emitter
from core.llm import FakeLLM
from torob import store
from torob.agents import compose
from torob.models import SessionState
from torob.router import _ui_chip, rule_route, run_turn

needs_db = pytest.mark.skipif(not store.DB_PATH.exists(), reason="run torob.etl.build_db first")

PRODUCTS = [
    {"product_id": "a", "title": "هندزفری انکر R50i", "label": "بهترین ارزش", "price_min": 1_400_000, "price_max": 1_600_000,
     "scope": "نزدیکت", "sellers": 5, "specs": {"مقاومت در برابر آب": "IPX5", "عمر باتری": "۱۰ ساعت"}},
    {"product_id": "b", "title": "هندزفری شیائومی Buds", "label": "ارزان‌ترین", "price_min": 900_000, "price_max": 900_000,
     "scope": "نزدیکت", "sellers": 2, "specs": {"عمر باتری": "۶ ساعت"}},
]
SHOPS = [
    {"shop_id": "s1", "name": "فروشگاه یک", "price_min": 1_500_000, "price_age": "۳ هفته پیش", "minutes": 6,
     "closes": "21:00", "credit": [], "where": "پاساژ علاءالدین"},
    {"shop_id": "s2", "name": "فروشگاه دو", "price_min": None, "price_age": "", "minutes": 11, "closes": None,
     "credit": ["ترب‌پی"], "where": "خیابان جمهوری"},
]


def board(**kw):
    return compose.Board(kw.get("products", PRODUCTS), kw.get("shops", []), kw.get("calls", {}), kw.get("live", {}))


def test_values_come_from_the_cards_and_bad_parts_are_dropped():
    raw = {"blocks": [
        {"type": "compare", "of": "products", "ids": ["P1", "P2", "P9"], "rows": ["price", "مقاومت در برابر آب", "made up"],
         "best": "P1"},
        {"type": "pick", "id": "P2", "reason": "فقط ۹۰۰ هزار تومنه"},            # a stated price: reason dropped
        {"type": "checklist", "items": ["پلمب جعبه رو ببین", "سریال رو با جعبه مقایسه کن", "۲۰ دقیقه تستش کن"]},
        {"type": "bars", "of": "products", "metric": "sellers", "ids": ["P1", "P2"]},   # over the 3-block limit
    ]}
    out = compose.fill(raw, board(), None, _ui_chip)
    assert [b["type"] for b in out] == ["compare", "pick", "checklist"]
    cmp = out[0]
    assert [c["title"] for c in cmp["cols"]] == ["انکر R50i", "شیائومی Buds"] and cmp["cols"][0]["best"]   # shared words go
    assert cmp["rows"] == [{"label": "قیمت ثبت‌شده", "values": ["۱٬۴۰۰٬۰۰۰ تا ۱٬۶۰۰٬۰۰۰", "۹۰۰٬۰۰۰"]},
                           {"label": "مقاومت در برابر آب", "values": ["IPX5", "ثبت نشده"]}]
    assert out[1]["reason"] is None and out[1]["chip"]["tool"] == "show_shops" and out[1]["chip"]["args"] == {"product_id": "b"}
    assert out[2]["items"] == ["پلمب جعبه رو ببین", "سریال رو با جعبه مقایسه کن"]


def test_shop_blocks_bars_and_actions():
    b = board(products=PRODUCTS[:1], shops=SHOPS, calls={"c1": {"shop_id": "s1", "outcome": "ok"}}, live={"s1": 1_450_000})
    raw = {"blocks": [
        {"type": "bars", "of": "shops", "metric": "minutes", "ids": ["S1", "S2"]},
        {"type": "compare", "of": "shops", "ids": ["S1", "S2"], "rows": ["price", "closes", "credit", "live", "stock"]},
        {"type": "actions", "items": [{"do": "call_shop", "id": "S2"}, {"do": "show_shops", "id": "P7"},
                                      {"do": "say", "text": "کدومش قسطی می‌ده؟"}]},
    ]}
    out = compose.fill(raw, b, "a", _ui_chip)
    bars = out[0]
    assert [(i["value"], i["best"]) for i in bars["items"]] == [(6, True), (11, False)]
    rows = {r["label"]: r["values"] for r in out[1]["rows"]}
    assert rows["قیمت ثبت‌شده"] == ["۱٬۵۰۰٬۰۰۰", "ثبت نشده"] and rows["بسته می‌شه"] == ["۲۱:۰۰", "ثبت نشده"]
    assert rows["قیمت الان در ترب"] == ["۱٬۴۵۰٬۰۰۰", "معلوم نیست"] and rows["موجودی"][0].startswith("موجود")
    chips = out[2]["chips"]
    assert chips[0]["tool"] == "call_shop" and chips[0]["args"] == {"shop_id": "s2", "product_id": "a"}
    assert chips[1] == {"label": "کدومش قسطی می‌ده؟", "value": "کدومش قسطی می‌ده؟", "kind": "say", "tool": None, "args": {}}
    assert compose.fill({"blocks": [{"type": "actions", "items": [{"do": "call_shop", "id": "S1"}]}]}, b, None, _ui_chip) == []


def test_prose_never_shows_ids_and_intro_keeps_to_choosing_help():
    b = board()
    assert b.name_ids("P1 برای باشگاه بهتره، P2 ارزون‌تره، P9 نه") == "«انکر R50i» برای باشگاه بهتره، «شیائومی Buds» ارزون‌تره، P9 نه"
    raw = {"blocks": [{"type": "pick", "id": "P1"}, {"type": "checklist", "items": ["پلمب", "سریال"]}]}
    raw["blocks"].append({"type": "compare", "of": "products", "ids": ["P1", "P2"], "rows": ["price"]})
    assert [x["type"] for x in compose.fill(raw, b, None, _ui_chip, intro=True)] == ["compare"]
    assert compose._trim("یک جمله‌ی کوتاه. و یه جمله‌ی خیلی خیلی بلندتر که جا نمی‌شه", 30) == "یک جمله‌ی کوتاه."
    assert compose.Board([dict(PRODUCTS[0], specs={"باتری": "40ساعت"})], [], {}, {}).row_value(
        "products", dict(PRODUCTS[0], specs={"باتری": "40ساعت"}), "باتری") == "40 ساعت"


def test_bars_never_repeat_a_compared_measure():
    b = board(products=PRODUCTS[:1], shops=SHOPS)
    raw = {"blocks": [{"type": "bars", "of": "shops", "metric": "price", "ids": ["S1", "S2"]},
                      {"type": "bars", "of": "shops", "metric": "minutes", "ids": ["S1", "S2"]},
                      {"type": "compare", "of": "shops", "ids": ["S1", "S2"], "rows": ["price", "closes"]}]}
    assert [(x["type"], x.get("metric")) for x in compose.fill(raw, b, "a", _ui_chip)] == [("bars", "minutes"), ("compare", None)]


def test_unusable_answers_give_none():
    llm = NS(enabled=True, complete=FakeLLM(lambda r, m: "not json").complete)
    assert compose.compose(llm, board(), "کدوم؟", "s", intro=False, selected_product_id=None, chip_factory=_ui_chip) is None
    llm = NS(enabled=True, complete=FakeLLM(lambda r, m: {"text": "", "blocks": [{"type": "pick", "id": "P9"}]}).complete)
    assert compose.compose(llm, board(), "کدوم؟", "s", intro=False, selected_product_id=None, chip_factory=_ui_chip) is None


@needs_db
def test_search_then_question_composes_blocks_from_the_shown_cards():
    seen = []

    def script(role, msgs):
        seen.append(role)
        if role == "router":
            text = msgs[-1]["content"]
            return {"intent": "ask_results"} if "کدوم" in text else rule_route(text).model_dump(mode="json")
        if role == "ui":
            return {"text": "اولی برای باشگاه بهتره.", "blocks": [
                {"type": "compare", "of": "products", "ids": ["P1", "P2"], "rows": ["price", "sellers"], "best": "P1"},
                {"type": "actions", "items": [{"do": "show_shops", "id": "P1"}]}]}
        return "not json"   # the relevance judge: rules stand

    llm = NS(enabled=True, complete=FakeLLM(script).complete, log=[])
    s = SessionState()
    em = Emitter("s")
    list(run_turn(llm, s, em, text="اسپیکر بلوتوثی"))
    kinds = [e for e, _ in em.events]
    assert kinds.index("product_cards") < kinds.index("ui") and dict(em.events)["ui"]["mode"] == "intro"
    assert s.products_shown and all(p["product_id"] in s.product_ids for p in s.products_shown)
    em = Emitter("s")
    list(run_turn(llm, s, em, text="کدوم برای باشگاه بهتره؟"))
    ui = dict(em.events)["ui"]
    assert ui["mode"] == "answer" and ui["text"] == "اولی برای باشگاه بهتره."
    assert ui["blocks"][1]["chips"][0]["args"] == {"product_id": s.product_ids[0]}
