"""Step machine + /api/chat streaming, on FakeLLM (no AI calls)."""

import json

import pytest

from core.agent_loop import Emitter
from core.llm import FakeLLM, LLMClient
from torob import store
from torob.models import Chip, SessionState
from torob.router import rule_route, run_turn, understand

needs_db = pytest.mark.skipif(not store.DB_PATH.exists(), reason="run torob.etl.build_db first")


def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_rule_route_budget_credit_call():
    r = rule_route("یه هندزفری بلوتوثی می‌خوام برای باشگاه، تا ۳ میلیون")
    assert r.intent == "need" and r.conditions_patch == {"budget_max": 3_000_000}
    assert "هندزفری" in r.need.keywords[0] and "میخوام" not in r.need.keywords[0]
    assert rule_route("قسطی باشه").conditions_patch == {"payment": "credit"}
    assert rule_route("تا ۵۰۰ هزار").conditions_patch == {"budget_max": 500_000}
    assert rule_route("زنگ بزن بپرس موجوده").intent == "call_shop"


def test_invalid_condition_patches_are_ignored():
    from torob.router import _apply_patch
    s = SessionState()
    assert _apply_patch(s, {"payment": "free", "radius_km": "far", "center": {"lat": 0}, "budget_max": 2_000_000}) == ["budget_max"]
    assert s.conditions.payment == "any" and s.conditions.budget_max == 2_000_000


def test_understand_falls_back_when_model_returns_junk():
    llm = LLMClient([], {"router": "x"}, enabled=False, fake=FakeLLM(lambda role, msgs: "I am not JSON"))
    out = understand(llm, SessionState(), "شارژر سامسونگ تا ۲ میلیون", "s")
    assert out.intent == "need" and out.conditions_patch["budget_max"] == 2_000_000


@needs_db
def test_run_turn_need_to_products_and_done():
    llm = LLMClient([], {"router": "x"}, enabled=False,
                    fake=FakeLLM(lambda role, msgs: rule_route(msgs[-1]["content"]).model_dump(mode="json")))
    state, em = SessionState(), Emitter("s")
    list(run_turn(llm, state, em, text="هندزفری بلوتوثی انکر تا ۳ میلیون"))
    kinds = [e for e, _ in em.events]
    assert kinds[0] == "conditions" and "product_cards" in kinds and kinds[-1] == "done"
    assert state.conditions.budget_max == 3_000_000 and state.product_ids


def test_direct_call_chip_skips_llm():
    fake = FakeLLM(lambda role, msgs: {"intent": "other"})
    llm = LLMClient([], {"router": "x"}, enabled=False, fake=fake)
    em = Emitter("s")
    list(run_turn(llm, SessionState(), em, chip=Chip(label="x", kind="direct_call", tool="no_such_tool")))
    assert fake.calls == [] and [e for e, _ in em.events] == ["error", "done"]


def test_errors_still_end_with_done():
    def boom(role, msgs):
        raise RuntimeError("x")
    llm = LLMClient([], {"router": "x"}, enabled=False, fake=FakeLLM(boom))
    em = Emitter("s")
    list(run_turn(llm, SessionState(), em, text="سلام"))
    assert em.events[-1][0] == "done"     # understand() fell back to rules, so no error either


@needs_db
def test_chat_endpoint_streams_and_persists(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from torob import app as app_mod
    from torob.appdb import AppDB
    monkeypatch.delenv("LLM_ENABLED", raising=False)
    app_mod.appdb.cache_clear()
    app_mod.llm.cache_clear()
    monkeypatch.setattr(app_mod, "appdb", lambda: _adb)
    _adb = AppDB(tmp_path / "app.db")
    c = TestClient(app_mod.app)
    r = c.post("/api/chat", json={"text": "شارژر سامسونگ ۲۵ وات"})
    assert r.headers["content-type"].startswith("text/event-stream") and r.headers["x-accel-buffering"] == "no"
    events = parse_sse(r.text)
    assert events[-1][0] == "done" and any(e == "product_cards" for e, _ in events)
    sid = r.cookies["sid"]
    assert [e["event"] for e in _adb.events(sid)] == [e for e, _ in events]
    # second turn reuses the session (cookie) and its conditions
    r2 = c.post("/api/chat", json={"text": "تا ۲ میلیون"})
    assert _adb.load(sid)[1].conditions.budget_max == 2_000_000 and r2.cookies.get("sid", sid) == sid
    assert c.get("/health").json()["llm_enabled"] is False
