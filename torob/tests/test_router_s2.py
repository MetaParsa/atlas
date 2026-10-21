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
    s = SessionState()
    events, _ = turn(s, text="گوشی سامسونگ S25 FE")
    kinds = [e for e, _ in events]
    assert "question" not in kinds and "shop_cards" in kinds
    assert len(dict(events)["product_cards"]["items"]) == 1 and s.selected_product_id
