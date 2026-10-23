"""Abuse limits per address, the body cap, security headers and scrubbing of what is kept on disk."""

import time

import pytest
from fastapi.testclient import TestClient

from torob import app as app_mod, store
from torob.appdb import AppDB, scrub

needs_db = pytest.mark.skipif(not store.DB_PATH.exists(), reason="run torob.etl.build_db first")


@pytest.fixture
def client(tmp_path, monkeypatch):
    adb = AppDB(tmp_path / "app.db")
    monkeypatch.setattr(app_mod, "appdb", lambda: adb)
    return TestClient(app_mod.app), adb


def test_security_headers_on_every_response(client):
    c, _ = client
    r = c.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "same-origin"


def test_oversized_body_is_refused_before_reading(client):
    c, _ = client
    r = c.post("/api/chat", content=b"x" * (app_mod.MAX_BODY + 1), headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert r.headers["x-content-type-options"] == "nosniff"


def test_every_route_has_a_per_ip_ceiling(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(app_mod, "api_limit", app_mod.RateLimiter(2, 60))
    assert c.get("/api/popular").status_code == 200
    assert c.get("/api/popular").status_code == 200
    assert c.get("/api/popular").status_code == 429
    assert c.get("/health").status_code == 200   # the health check is never limited


@needs_db
def test_dropping_the_cookie_does_not_reset_the_budget(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(app_mod, "new_session_limit", app_mod.RateLimiter(2, 3600))
    for _ in range(2):
        c.cookies.clear()
        assert c.post("/api/chat", json={"text": "شارژر"}).status_code == 200
    c.cookies.clear()
    assert c.post("/api/chat", json={"text": "شارژر"}).status_code == 429


@needs_db
def test_turns_per_address_are_capped(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(app_mod, "turn_hour_limit", app_mod.RateLimiter(1, 3600))
    assert c.post("/api/chat", json={"text": "شارژر"}).status_code == 200
    assert c.post("/api/chat", json={"text": "شارژر"}).status_code == 429


def test_parallel_turns_are_capped_per_address_and_in_total(client, monkeypatch):
    c, _ = client
    now = time.monotonic()
    app_mod._busy.update({"a": now, "b": now})
    app_mod._busy_ip.update({"a": "testclient", "b": "testclient"})
    assert c.post("/api/chat", json={"text": "شارژر"}).status_code == 429
    app_mod._busy_ip.update({"a": "10.0.0.1", "b": "10.0.0.2"})
    monkeypatch.setattr(app_mod, "MAX_TURNS_RUNNING", 2)
    assert c.post("/api/chat", json={"text": "شارژر"}).status_code == 503


def test_scrub_removes_contacts_and_secrets_but_keeps_prices():
    out = scrub({"text": "گوشی تا ۲۵۰۰۰۰۰۰ تومن، شماره‌م ۰۹۱۲۳۴۵۶۷۸۹ و mail me@x.ir",
                 "detail": "anthropic: AuthenticationError: x-api-key: sk-ant-api03-abcdefghijklmnop",
                 "card": "6037 9912 3456 7890", "image": "data:image/jpeg;base64,AAAA"})
    assert "۲۵۰۰۰۰۰۰" in out["text"]
    assert "۰۹۱۲۳۴۵۶۷۸۹" not in out["text"] and "me@x.ir" not in out["text"]
    assert "sk-ant" not in out["detail"]
    assert "7890" not in out["card"]
    assert out["image"] == "data:image/jpeg;base64,AAAA"


def test_user_text_is_scrubbed_on_disk(tmp_path):
    adb = AppDB(tmp_path / "app.db")
    adb.log_event("s1", "user", {"text": "زنگ بزن به 09121234567"})
    assert "09121234567" not in adb.events("s1")[0]["data"]["text"]
