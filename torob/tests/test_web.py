"""S5: the one-screen web page and its endpoints (no browser; the page itself was checked in the preview)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.agent_loop import Emitter
from core.llm import FakeLLM, LLMClient
from torob import store
from torob.models import Chip, SessionState
from torob.router import rule_route, run_turn

WEB = Path(__file__).resolve().parents[1] / "web"
needs_db = pytest.mark.skipif(not store.DB_PATH.exists(), reason="run torob.etl.build_db first")


@pytest.fixture
def client(tmp_path, monkeypatch):
    from torob import app as app_mod
    from torob.appdb import AppDB
    adb = AppDB(tmp_path / "app.db")
    monkeypatch.setattr(app_mod, "appdb", lambda: adb)
    monkeypatch.setenv("CALL_LINE_DELAY", "0")
    return TestClient(app_mod.app)


def test_index_and_static_served(client):
    r = client.get("/")
    assert r.status_code == 200 and 'dir="rtl"' in r.text and 'id="strip"' in r.text and "شبیه‌سازی" in r.text
    assert r.headers["cache-control"] == "no-cache"
    assert client.get("/static/app.js").status_code == 200 and client.get("/static/app.css").status_code == 200


def test_script_never_parses_server_text_as_html():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "outerHTML" not in js


def test_simulated_style_exists_and_is_used():
    css = (WEB / "app.css").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert ".sim {" in css and "dashed" in css
    assert js.count("simTag()") >= 2                       # call header and simulated alternatives carry the tag
    assert 'card.classList.add("sim")' not in js            # dataset rows of a shop card are never restyled
    assert 'class: "simbox sim"' in js                      # simulated facts get their own row


def test_state_and_reset(client):
    s = client.get("/api/state").json()
    assert s["has_results"] is False and s["conditions"]["area"]
    r = client.post("/api/reset")
    assert r.status_code == 200 and 'sid=""' in r.headers.get("set-cookie", "") or "Max-Age=0" in r.headers.get("set-cookie", "")


def turn(state, **kw):
    fake = FakeLLM(lambda role, msgs: rule_route(msgs[-1]["content"]).model_dump(mode="json"))
    em = Emitter("s")
    list(run_turn(LLMClient([], {"router": "x"}, enabled=False, fake=fake), state, em, **kw))
    return em.events


def test_set_location_inside_tehran_only():
    s = SessionState()
    ev = turn(s, chip=Chip(label="x", kind="direct_call", tool="set_location",
                           args={"lat": 35.72, "lng": 51.42, "label": "سنائی", "source": "area"}))
    assert s.conditions.center.label == "سنائی" and dict(ev)["conditions"]["area"] == "سنائی"
    ev = turn(s, chip=Chip(label="x", kind="direct_call", tool="set_location",
                           args={"lat": 32.6, "lng": 51.6, "label": "اصفهان"}))
    assert [e for e, _ in ev] == ["error", "done"] and s.conditions.center.label == "سنائی"


@needs_db
def test_result_blocks_always_bring_the_strip():
    s = SessionState()
    ev = turn(s, text="اسپیکر بلوتوثی")        # no condition changed, the strip must still appear
    kinds = [e for e, _ in ev]
    assert kinds.index("conditions") < kinds.index("product_cards")


@needs_db
def test_full_page_flow_over_http(client):
    r = client.post("/api/chat", json={"text": "شارژر اصل آیفون"})
    assert "event: product_cards" in r.text
    r = client.post("/api/refine", json={"conditions": {"payment": "credit"}})
    assert r.status_code == 200 and "event: conditions" in r.text
