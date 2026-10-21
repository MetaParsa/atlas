"""One test per finding of the S1 review (no network, no real LLM)."""

import httpx
import pytest
from pydantic import ValidationError

from core.agent_loop import Emitter
from core.llm import FakeLLM, LLMClient, LLMError, Provider, SpendGuard
from torob import store
from torob.models import Chip, Conditions, SessionState
from torob.router import _budget, apply_patch, rule_route, run_turn

MODELS = {"router": "m", "chat": "m"}
needs_db = pytest.mark.skipif(not store.DB_PATH.exists(), reason="run torob.etl.build_db first")


def http_client(handler, guard=None):
    return LLMClient([Provider("p1", "https://p1.test/v1", "k"), Provider("p2", "https://p2.test/v1", "k")],
                     MODELS, guard, enabled=True, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("body", [{"choices": []}, {"choices": [{"message": None}]},
                                  {"choices": [{"message": {"content": "x"}}], "usage": {"prompt_tokens": None}}])
def test_malformed_replies_fail_over(body):
    def handler(req):
        return httpx.Response(200, json=body) if req.url.host == "p1.test" else \
            httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
    res = http_client(handler).complete("chat", [])
    assert res.provider in ("p1", "p2") and res.text in ("x", "ok")


def test_billed_bad_json_still_counts():
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 50}})
    c = http_client(handler)
    with pytest.raises(LLMError):
        c.complete("router", [], json_mode=True)
    assert c.guard.used_today() == 300          # both providers billed 150 each


def test_prompt_size_is_reserved():
    c = http_client(lambda r: httpx.Response(500), guard=SpendGuard(daily_tokens=1000))
    with pytest.raises(Exception, match="cap"):
        c.complete("chat", [{"role": "user", "content": "x" * 6000}], max_tokens=100)


def test_provider_repr_hides_key():
    assert "secret" not in repr(Provider("p", "https://x", "secret"))


def test_conditions_limits():
    for bad in ({"radius_km": -5}, {"radius_km": 0}, {"radius_km": float("nan")}, {"budget_max": -1},
                {"budget_min": 5, "budget_max": 1}):
        with pytest.raises(ValidationError):
            Conditions(**bad)
    s = SessionState()
    assert apply_patch(s, {"radius_km": -5, "budget_min": 9_000_000}) == ["budget_min"]
    assert apply_patch(s, {"budget_max": 1_000_000}) == []     # would be below budget_min → ignored


def test_arrive_at_parsed():
    s = SessionState()
    assert apply_patch(s, {"arrive_at": "۲۰:۳۰"}) == ["arrive_at"]
    assert s.conditions.arrive_at.strftime("%H:%M") == "20:30" and str(s.conditions.arrive_at.tzinfo) == "Asia/Tehran"
    assert apply_patch(s, {"arrive_at": "25:99"}) == []


@pytest.mark.parametrize("text,expect", [
    ("تا 3 میلیون", 3_000_000), ("تا 500 هزار", 500_000), ("زیر 300 تومن", 300_000),
    ("تا 2500000 تومان", 2_500_000), ("تا 8 شب بازه؟", None), ("تا 1.5 میلیون", 1_500_000), ("تا ...", None),
])
def test_budget_parsing(text, expect):
    assert _budget(text) == expect


def test_rule_route_never_crashes_on_odd_text():
    for t in ["تا ...", "", "؟؟؟", "تا 8 شب", "1" * 900]:
        rule_route(t)


def test_router_history_has_no_duplicate_and_remembers_assistant():
    seen = []
    def script(role, msgs):
        seen.append(msgs)
        return {"intent": "refine", "conditions_patch": {"budget_max": 2_000_000}, "message": "باشه، تا ۲ میلیون."}
    llm = LLMClient([], MODELS, enabled=False, fake=FakeLLM(script))
    s = SessionState()
    list(run_turn(llm, s, Emitter("s"), text="تا ۲ میلیون"))
    list(run_turn(llm, s, Emitter("s"), text="قسطی باشه"))
    second = [m["content"] for m in seen[1] if m["role"] != "system"]
    assert second == ["تا ۲ میلیون", "باشه، تا ۲ میلیون.", "قسطی باشه"]


def test_direct_call_rejects_unknown_args():
    llm = LLMClient([], MODELS, enabled=False, fake=FakeLLM(lambda r, m: {"intent": "other"}))
    em = Emitter("s")
    list(run_turn(llm, SessionState(), em, chip=Chip(label="x", kind="direct_call", tool="show_products",
                                                    args={"query": "x", "shop_id": "evil"})))
    assert [e for e, _ in em.events] == ["error", "done"]


def test_fake_script_never_shows_json_for_chat_role():
    from torob.app import _fake_script
    assert isinstance(_fake_script("chat", [{"role": "user", "content": "x"}]), str)


@needs_db
def test_busy_session_409_and_rate_limit(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from torob import app as app_mod
    from torob.appdb import AppDB
    adb = AppDB(tmp_path / "app.db")
    monkeypatch.setattr(app_mod, "appdb", lambda: adb)
    c = TestClient(app_mod.app)
    sid = c.post("/api/chat", json={"text": "شارژر"}).cookies["sid"]
    app_mod._busy[sid] = __import__("time").monotonic()
    assert c.post("/api/chat", json={"text": "شارژر"}).status_code == 409
    app_mod._busy.clear()
    monkeypatch.setattr(app_mod, "chat_limit", app_mod.RateLimiter(1, 60))
    assert c.post("/api/chat", json={"text": "a"}).status_code == 200
    assert c.post("/api/chat", json={"text": "a"}).status_code == 429
    assert c.post("/api/chat", json={"text": "x" * 2000}).status_code in (422, 429)


def test_anonymous_requests_create_no_session_rows(tmp_path):
    from torob.appdb import AppDB
    adb = AppDB(tmp_path / "app.db")
    adb.load(None)
    adb.load("nonexistent")
    assert adb._con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
