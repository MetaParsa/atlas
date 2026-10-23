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


# --- Claude through the official SDK (a stand-in client; no network) -------------------------------------------

from types import SimpleNamespace as _NS


class _FakeMessages:
    def __init__(self, content, stop="end_turn"):
        self.content, self.stop, self.calls = content, stop, []

    def create(self, **kw):
        self.calls.append(kw)
        return _NS(content=self.content, stop_reason=self.stop, usage=_NS(input_tokens=50, output_tokens=7))


def _claude(content, stop="end_turn"):
    from core.llm import AnthropicProvider, LLMClient
    fake = _FakeMessages(content, stop)
    p = AnthropicProvider("anthropic", "test-key", _client=_NS(messages=fake))
    return LLMClient([p], {"router": "claude-haiku-5-5", "chat": "claude-haiku-5-5", "vision": "claude-haiku-5-5"},
                     enabled=True), fake


def test_claude_request_translates_photo_tools_and_history():
    llm, fake = _claude([_NS(type="text", text="شارژر اپل")])
    photo = "data:image/jpeg;base64,QUJD"
    res = llm.complete("vision", [{"role": "system", "content": "sys"}, {"role": "assistant", "content": "قبلی"},
                                  {"role": "user", "content": [{"type": "text", "text": "چیه؟"},
                                                               {"type": "image_url", "image_url": {"url": photo}}]}])
    kw = fake.calls[0]
    assert res.text == "شارژر اپل" and res.provider == "anthropic" and kw["model"] == "claude-haiku-5-5"
    assert kw["system"] == "sys" and kw["thinking"] == {"type": "disabled"} and kw["output_config"] == {"effort": "low"}
    assert "temperature" not in kw
    assert kw["messages"] == [{"role": "user", "content": [   # a leading assistant turn is dropped
        {"type": "text", "text": "چیه؟"},
        {"type": "image", "source": {"type": "base64", "data": "QUJD", "media_type": "image/jpeg"}}]}]


def test_sonnet_gets_its_own_thinking_setting_and_refusal_fallbacks():
    from core.llm import AnthropicProvider, LLMClient
    fake = _FakeMessages([_NS(type="text", text='{"a": 1}')], "end_turn")
    p = AnthropicProvider("anthropic", "test-key", _client=_NS(messages=fake))
    llm = LLMClient([p], {"router": "claude-sonnet-5-5"}, enabled=True)
    llm.complete("router", [{"role": "user", "content": "سلام"}], schema={"type": "object"})
    kw = fake.calls[0]
    assert kw["thinking"] == {"type": "between_tools"}            # Sonnet 5.5 rejects «disabled»
    assert kw["extra_body"] == {"fallbacks": "default"} and "server-side-fallback" in kw["extra_headers"]["anthropic-beta"]
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}


def test_claude_tool_calls_round_trip_in_openai_shape():
    llm, fake = _claude([_NS(type="text", text=""), _NS(type="tool_use", id="tu1", name="lookup", input={"q": "x"})])
    tools = [{"type": "function", "function": {"name": "lookup", "description": "d",
                                               "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}]
    res = llm.complete("chat", [{"role": "user", "content": "سلام"}], tools=tools)
    assert res.tool_calls == [{"id": "tu1", "type": "function", "function": {"name": "lookup", "arguments": '{"q": "x"}'}}]
    assert fake.calls[0]["tools"][0]["input_schema"]["properties"] == {"q": {"type": "string"}}
    from core.llm import to_anthropic_messages
    _, msgs = to_anthropic_messages([{"role": "user", "content": "سلام"},
                                     {"role": "assistant", "content": None, "tool_calls": res.tool_calls},
                                     {"role": "tool", "tool_call_id": "tu1", "content": '{"ok": 1}'}])
    assert msgs[1]["content"] == [{"type": "tool_use", "id": "tu1", "name": "lookup", "input": {"q": "x"}}]
    assert msgs[2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": '{"ok": 1}'}]}


def test_claude_refusal_and_bad_json_fail_cleanly():
    import pytest
    from core.llm import LLMError
    llm, _ = _claude([_NS(type="text", text="no")], stop="refusal")
    with pytest.raises(LLMError):
        llm.complete("router", [{"role": "user", "content": "x"}])
    llm, fake = _claude([_NS(type="text", text="not json")])
    with pytest.raises(LLMError):
        llm.complete("router", [{"role": "user", "content": "x"}], json_mode=True)
    assert "JSON object only" in fake.calls[0]["system"]


def test_anthropic_key_means_claude_only_no_gateway(monkeypatch):
    from core.llm import AnthropicProvider, from_env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PRIMARY_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("LLM_PRIMARY_API_KEY", "gw-key")
    providers = from_env().providers
    assert len(providers) == 1 and isinstance(providers[0], AnthropicProvider) and "test-key" not in repr(providers[0])


def test_chat_answers_are_plain_text():
    from core.agent_loop import plain_text
    md = "نزدیک‌ترین‌ها:\n\n- **دیسورا پاکیار**: حدود ۶ دقیقه\n* __نودیس آران__: ۹ دقیقه\n## نکته: ساعت ثبت نشده"
    out = plain_text(md)
    assert "**" not in out and "__" not in out and "#" not in out
    assert "• دیسورا پاکیار: حدود ۶ دقیقه" in out and "• نودیس آران: ۹ دقیقه" in out and out.endswith("نکته: ساعت ثبت نشده")
