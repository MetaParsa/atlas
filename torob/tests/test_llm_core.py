"""core/llm.py and core/agent_loop.py — no network: HTTP goes through httpx.MockTransport."""

import json

import httpx
import pytest

from core.agent_loop import Emitter, Tool, tool_loop
from core.llm import BudgetExceeded, FakeLLM, LLMClient, LLMDisabled, LLMError, Provider, SpendGuard

MODELS = {"router": "m-router", "chat": "m-chat"}


def ok_body(content="hi", usage=(10, 5)):
    return {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}


def client(handler, enabled=True, guard=None, n=2):
    providers = [Provider(f"p{i}", f"https://p{i}.test/v1", "k") for i in range(1, n + 1)]
    return LLMClient(providers, MODELS, guard, enabled=enabled, transport=httpx.MockTransport(handler))


def test_disabled_never_touches_network():
    hits = []
    c = client(lambda r: hits.append(r) or httpx.Response(200, json=ok_body()), enabled=False)
    with pytest.raises(LLMDisabled):
        c.complete("router", [{"role": "user", "content": "x"}])
    c.fake = FakeLLM(lambda role, msgs: {"intent": "other"})
    assert c.complete("router", []).json() == {"intent": "other"}
    assert hits == []


def test_failover_on_5xx_and_records_usage():
    def handler(req):
        return httpx.Response(503) if req.url.host == "p1.test" else httpx.Response(200, json=ok_body("از دومی"))
    c = client(handler)
    res = c.complete("chat", [{"role": "user", "content": "سلام"}], session_id="s")
    assert (res.text, res.provider) == ("از دومی", "p2")
    assert c.guard.used_today() == 15


def test_bad_json_counts_as_failure():
    def handler(req):
        content = "not json" if req.url.host == "p1.test" else '```json\n{"intent": "need"}\n```'
        return httpx.Response(200, json=ok_body(content))
    assert client(handler).complete("router", [], json_mode=True).json() == {"intent": "need"}


def test_all_fail_raises():
    with pytest.raises(LLMError):
        client(lambda r: httpx.Response(500)).complete("chat", [])


def test_spend_guard_blocks_before_calling():
    hits = []
    c = client(lambda r: hits.append(r) or httpx.Response(200, json=ok_body()), guard=SpendGuard(daily_tokens=100))
    with pytest.raises(BudgetExceeded):
        c.complete("chat", [], max_tokens=400)
    assert hits == []


def test_key_sent_only_as_header():
    seen = {}
    def handler(req):
        seen["auth"], seen["body"] = req.headers.get("authorization"), req.content.decode()
        return httpx.Response(200, json=ok_body())
    client(handler, n=1).complete("chat", [{"role": "user", "content": "x"}])
    assert seen["auth"] == "Bearer k" and '"k"' not in seen["body"]


def test_tool_loop_runs_tool_then_answers():
    script = iter([
        {"tool_calls": [{"id": "1", "function": {"name": "closing_time", "arguments": json.dumps({"shop": "A"})}}]},
        "فروشگاه A ساعت ۲۰ می‌بنده.",
    ])
    c = LLMClient([], MODELS, enabled=False, fake=FakeLLM(lambda role, msgs: next(script)))
    tool = Tool("closing_time", "closing time of a shown shop", {"type": "object", "properties": {"shop": {"type": "string"}}},
                lambda shop: {"shop": shop, "closes": "20:00"})
    em = Emitter("s")
    out = list(tool_loop(c, [{"role": "user", "content": "کی می‌بنده؟"}], [tool], em))
    assert [e for e, _ in em.events] == ["step", "message"] and len(out) == 2
    assert em.events[-1][1]["text"].startswith("فروشگاه A")


def test_tool_loop_survives_bad_tool_args_and_stops():
    bad = {"tool_calls": [{"id": "1", "function": {"name": "closing_time", "arguments": "{not json"}}]}
    c = LLMClient([], MODELS, enabled=False, fake=FakeLLM(lambda role, msgs: bad))
    tool = Tool("closing_time", "", {"type": "object"}, lambda **_: {})
    em = Emitter("s")
    list(tool_loop(c, [], [tool], em, max_steps=2))
    assert em.events[-1][0] == "message" and sum(e == "step" for e, _ in em.events) == 2
