"""Torob in-store shopping agent — web app (one origin: UI, API and admin; spec D19)."""

import sqlite3
import threading
import time
from collections import defaultdict, deque
from functools import lru_cache

from fastapi import Cookie, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from core.agent_loop import Emitter
from core.llm import FakeLLM, LLMClient, from_env
from torob import store
from torob.appdb import AppDB
from torob.models import MAX_TEXT, Chip
from torob.router import rule_route, run_turn

app = FastAPI(title="Torob in-store agent")
STREAM_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}


def _fake_script(role: str, messages: list[dict]) -> dict | str:
    """With LLM_ENABLED off: the router 'model' is the rule-based parser (same JSON shape, zero cost);
    other roles get a plain sentence, never raw JSON in the chat."""
    if role == "router":
        return rule_route(messages[-1]["content"]).model_dump(mode="json")
    return "در حالت آزمایشی این جواب ساده است؛ از کارت‌ها و دکمه‌ها استفاده کن."


@lru_cache(maxsize=1)
def llm() -> LLMClient:
    return from_env(fake=FakeLLM(_fake_script))


@lru_cache(maxsize=1)
def appdb() -> AppDB:
    return AppDB()


# --- abuse limits (spec §8: 20 req/min/IP; one running turn per session) ---------------------------------------

class RateLimiter:
    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._hits) > 50_000:
                self._hits.clear()
            return True


chat_limit = RateLimiter(20, 60)
_busy: dict[str, float] = {}     # sid -> turn start; expires so a stream that never started can't lock a session
_busy_lock = threading.Lock()
BUSY_TTL_S = 60


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


# --- routes ------------------------------------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    try:
        m = store.meta(store.get())
        return {"status": "ok", "dataset": m.get("dataset_version"), "built_at": m.get("built_at"),
                "llm_enabled": llm().enabled}
    except Exception as e:  # DB missing or still uploading: answer 200 so the host doesn't kill the container
        store.reset()
        return {"status": "building", "detail": type(e).__name__}


@app.get("/api/areas")
def areas() -> list[dict]:
    try:
        return store.areas(store.get())
    except (FileNotFoundError, sqlite3.DatabaseError):
        store.reset()
        raise HTTPException(503, "داده‌ها در حال آماده‌سازی…")


class ChatIn(BaseModel):
    text: str | None = Field(None, max_length=MAX_TEXT)
    chip: Chip | None = None


@app.post("/api/chat")
def chat(body: ChatIn, request: Request, sid: str | None = Cookie(default=None, max_length=64)):
    if not chat_limit.allow(client_ip(request)):
        return JSONResponse({"error": "یه کم آروم‌تر؛ چند لحظه بعد دوباره امتحان کن."}, status_code=429)
    adb = appdb()
    sid, state = adb.load(sid)
    with _busy_lock:
        started = _busy.get(sid)
        if started and time.monotonic() - started < BUSY_TTL_S:   # double tap / chip during a stream
            return JSONResponse({"error": "صبر کن جواب قبلی تموم بشه."}, status_code=409)
        _busy[sid] = time.monotonic()
    em = Emitter(sid, sink=adb.log_event)

    def stream():
        try:
            yield from run_turn(llm(), state, em, text=body.text, chip=body.chip)
        finally:
            adb.save(sid, state)   # also when the client disconnects mid-stream
            with _busy_lock:
                _busy.pop(sid, None)

    resp = StreamingResponse(stream(), media_type="text/event-stream", headers=STREAM_HEADERS)
    resp.set_cookie("sid", sid, httponly=True, samesite="lax", secure=is_https(request), max_age=7 * 24 * 3600)
    return resp
