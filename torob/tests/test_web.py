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


@needs_db
def test_shop_price_carries_numbers_for_the_card():
    s = SessionState()
    items = dict(turn(s, text="شارژر اصل آیفون"))["product_cards"]["items"]
    ev = turn(s, chip=Chip(label="x", kind="direct_call", tool="show_shops", args={"product_id": items[0]["product_id"]}))
    price = dict(ev)["shop_cards"]["items"][0]["price"]
    assert price["min"] > 0 and price["max"] >= price["min"] and isinstance(price["per_metre"], bool)
    assert price["text"].startswith("قیمت ثبت‌شده")         # the honest label is still there for the plan card


def test_page_uses_self_hosted_font_only():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    css = (WEB / "app.css").read_text(encoding="utf-8")
    assert "fonts.googleapis" not in html + css and (WEB / "fonts" / "Vazirmatn-wght.woff2").exists()


def test_assets_are_versioned_and_revalidated(client):
    page = client.get("/").text
    assert "/static/app.css?v=" in page and "/static/app.js?v=" in page
    assert client.get("/static/app.css").headers["cache-control"] == "no-cache"
    assert "immutable" in client.get("/static/fonts/Vazirmatn-wght.woff2").headers["cache-control"]


def test_radish_sprites_match_their_generator(client, tmp_path, monkeypatch):
    """The committed sheet is exactly what torob/tools/radish_sprites.py builds, and every state's frames exist."""
    import importlib.util, json
    spec = importlib.util.spec_from_file_location("radish_sprites", WEB.parent / "tools" / "radish_sprites.py")
    rs = importlib.util.module_from_spec(spec); spec.loader.exec_module(rs)
    monkeypatch.setattr(rs, "WEB", tmp_path)
    rs.main()
    assert (tmp_path / "radish-sheet.png").read_bytes() == (WEB / "radish-sheet.png").read_bytes()
    js = (tmp_path / "radish-sprites.js").read_text(encoding="utf-8")
    assert js == (WEB / "radish-sprites.js").read_text(encoding="utf-8")
    meta = json.loads(js.split("=", 1)[1].rstrip().rstrip(";"))
    frames, _ = rs.build()
    for s in meta["states"].values():
        assert s["loop"] and all(0 <= i < len(frames) for i in s["in"] + s["loop"])
    assert "/static/radish-sprites.js?v=" in client.get("/").text
    assert client.get("/static/radish-sheet.png").status_code == 200


def test_searchable_keeps_private_text_off_the_start_screen():
    from torob.appdb import searchable
    assert searchable("  هندزفری   بلوتوثی ") == "هندزفری بلوتوثی"
    for bad in ["", "a", "شماره‌م ۰۹۱۲۳۴۵۶۷۸۹", "x@y.com", "https://t.me/a", "خ" * 41]:
        assert searchable(bad) is None


@needs_db
def test_popular_searches_are_first_searches_that_found_products(client):
    def search(text, fresh=True):
        if fresh:
            client.cookies.clear()                   # a new visitor (no session yet)
        assert client.post("/api/chat", json={"text": text}).status_code == 200
    assert client.get("/api/popular").json() == {"searches": []}
    search("شارژر آیفون")
    search("شارژر آیفون")
    search("ارزون‌تر", fresh=False)                  # a follow-up in the same session is not a new search
    search("هندزفری بلوتوثی")
    search("قققزززخخخ")                              # found nothing: not suggested to others
    assert client.get("/api/popular").json() == {"searches": ["شارژر آیفون", "هندزفری بلوتوثی"]}


@needs_db
def test_history_replays_only_this_sessions_chat(client):
    assert client.get("/api/history").json() == {"events": []}            # a new visitor has no chat
    client.post("/api/chat", json={"text": "شارژر آیفون"})
    evs = client.get("/api/history").json()["events"]
    assert evs[0] == {"event": "user", "data": {"text": "شارژر آیفون"}}
    assert any(e["event"] == "product_cards" and e["data"]["items"] for e in evs)
    pid = next(e for e in evs if e["event"] == "product_cards")["data"]["items"][0]["product_id"]
    client.post("/api/chat", json={"chip": {"label": "فروشنده‌ها", "kind": "direct_call", "tool": "show_shops",
                                            "args": {"product_id": pid}}, "bubble": "فروشنده‌های «شارژر»"})
    users = [e["data"]["text"] for e in client.get("/api/history").json()["events"] if e["event"] == "user"]
    assert users == ["شارژر آیفون", "فروشنده‌های «شارژر»"]                 # a tapped chip keeps its shown bubble
    client.cookies.clear()
    assert client.get("/api/history").json() == {"events": []}            # another visitor can't read it


def test_default_llm_model_is_claude_haiku(monkeypatch):
    from core.llm import from_env
    for k in ("MODEL_ROUTER", "MODEL_CHAT", "MODEL_CALL"):
        monkeypatch.delenv(k, raising=False)
    assert set(from_env().models.values()) == {"anthropic/claude-haiku-5.5"}


PHOTO = "data:image/jpeg;base64," + "A" * 400   # shape only; nothing decodes it without a model


def test_vision_reads_only_a_clear_answer():
    from torob.agents import vision
    ask = lambda answer: vision.identify(LLMClient([], {}, fake=FakeLLM(lambda r, m: answer)), PHOTO, "s")
    assert ask("«شارژر اپل ۲۰ وات».") == "شارژر اپل ۲۰ وات"
    assert ask("نامعلوم") is None and ask("") is None and ask("خ " * 40) is None
    assert vision.is_image(PHOTO, 10_000) and not vision.is_image("data:text/html;base64,AAAA", 10_000)
    assert not vision.is_image("javascript:alert(1)", 10_000) and not vision.is_image(PHOTO, 100)


def test_llm_budget_counts_a_photo_at_a_flat_rate():
    from core.llm import IMAGE_TOKENS, estimate_tokens
    big = "data:image/jpeg;base64," + "A" * 300_000
    msg = [{"role": "user", "content": [{"type": "text", "text": "x"}, {"type": "image_url", "image_url": {"url": big}}]}]
    assert IMAGE_TOKENS <= estimate_tokens(msg) < IMAGE_TOKENS + 100


@needs_db
def test_photo_without_a_model_asks_for_the_name_and_keeps_its_preview(client):
    r = client.post("/api/chat", json={"image": PHOTO, "thumb": PHOTO})
    assert r.status_code == 200 and "اسم یا مدلش رو بنویس" in r.text and "product_cards" not in r.text
    first = client.get("/api/history").json()["events"][0]
    assert first == {"event": "user", "data": {"text": "", "image": PHOTO}}
    assert client.post("/api/chat", json={"image": "data:text/html;base64,AAAA"}).status_code == 422   # not a photo, no text


@needs_db
def test_photo_that_the_model_reads_becomes_the_search(client, monkeypatch):
    from torob import app as app_mod
    script = lambda role, m: "شارژر آیفون" if role == "vision" else app_mod._fake_script(role, m)
    monkeypatch.setattr(app_mod, "llm", lambda: LLMClient([], {}, fake=FakeLLM(script)))
    r = client.post("/api/chat", json={"image": PHOTO, "thumb": PHOTO})
    assert "تو عکس: شارژر آیفون" in r.text and "product_cards" in r.text
