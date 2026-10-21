"""Torob in-store shopping agent — web app (one origin: UI, API and admin; spec D19)."""

import sqlite3
from functools import lru_cache

from fastapi import Cookie, FastAPI, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.agent_loop import Emitter
from core.llm import FakeLLM, LLMClient, from_env
from torob import store
from torob.appdb import AppDB
from torob.models import Chip
from torob.router import rule_route, run_turn

app = FastAPI(title="Torob in-store agent")
_db: sqlite3.Connection | None = None
_db_sig: tuple[int, int] | None = None
STREAM_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}


def db() -> sqlite3.Connection:
    """Dataset connection, reopened whenever the DB file changes (e.g. a finished upload replaces a partial one)."""
    global _db, _db_sig
    sig = store.file_signature()
    if sig is None:
        raise FileNotFoundError(store.DB_PATH)
    if _db is None or sig != _db_sig:
        if _db is not None:
            _db.close()
        _db, _db_sig = store.connect(), sig
    return _db


def _reset() -> None:
    global _db, _db_sig
    if _db is not None:
        _db.close()
    _db = _db_sig = None


def _fake_script(role: str, messages: list[dict]) -> dict:
    """With LLM_ENABLED off, the router 'model' is the rule-based parser: same JSON shape, zero cost."""
    return rule_route(messages[-1]["content"]).model_dump(mode="json")


@lru_cache(maxsize=1)
def llm() -> LLMClient:
    return from_env(fake=FakeLLM(_fake_script))


@lru_cache(maxsize=1)
def appdb() -> AppDB:
    return AppDB()


@app.get("/health")
def health() -> dict:
    try:
        m = store.meta(db())
        return {"status": "ok", "dataset": m.get("dataset_version"), "built_at": m.get("built_at"),
                "llm_enabled": llm().enabled}
    except Exception as e:  # DB missing or still uploading: answer 200 so the host doesn't kill the container
        _reset()
        return {"status": "building", "detail": type(e).__name__}


@app.get("/api/areas")
def areas() -> list[dict]:
    try:
        return store.areas(db())
    except (FileNotFoundError, sqlite3.DatabaseError):
        _reset()
        raise HTTPException(503, "داده‌ها در حال آماده‌سازی…")


class ChatIn(BaseModel):
    text: str | None = None
    chip: Chip | None = None


@app.post("/api/chat")
def chat(body: ChatIn, response: Response, sid: str | None = Cookie(default=None)) -> StreamingResponse:
    adb = appdb()
    sid, state = adb.load(sid)
    em = Emitter(sid, sink=adb.log_event)

    def stream():
        try:
            yield from run_turn(llm(), state, em, text=body.text, chip=body.chip)
        finally:
            adb.save(sid, state)   # also when the client disconnects mid-stream

    resp = StreamingResponse(stream(), media_type="text/event-stream", headers=STREAM_HEADERS)
    resp.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=7 * 24 * 3600)
    return resp
